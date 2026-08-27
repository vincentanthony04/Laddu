from __future__ import annotations

import json
import os
import subprocess
import uuid
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _programdata() -> Path:
    return Path(os.environ.get("ProgramData") or r"C:\ProgramData")


class ResearchAdapter:
    """Runs installed research libraries through a safe subprocess.

    The live backend must remain stable. Heavy libraries such as Qlib/Vibe are
    called from ProgramData\\ProjectLaddu\\research_venv\\Scripts\\python.exe when
    available. Results are returned as JSON and stored in DecisionLedger.
    """

    def __init__(self, base_dir: Path, store=None, logger=None, timeout_sec: int = 90) -> None:
        self.base_dir = Path(base_dir)
        self.store = store
        self.logger = logger
        self.timeout_sec = timeout_sec
        # fix_to_be_done #3: cross-sectional (rank/scale/zscore) zoo factors
        # need a real multi-symbol panel, not the per-symbol candles this
        # adapter otherwise deals with. scan_orchestration_service calls
        # refresh_cross_sectional() once per scan pass with that pass's
        # symbol universe; run() then reads from this cache automatically,
        # so no change is needed to analyze_one's call chain. Empty/stale
        # cache means run() just omits cross_sectional_scores from the
        # payload -- research_worker already zero-weight-excludes those
        # factors when the key is absent, so this is fail-safe by default.
        #
        # v61.9.1: the first version of this ran synchronously on whatever
        # thread called refresh_cross_sectional() -- the scanner loop thread,
        # for up to a 2000-symbol universe (run_deep_mode_scan). That's up to
        # 2000 sequential store.get_candles() calls plus 141 pandas factor
        # computations held under the GIL, competing with every concurrent
        # HTTP request thread for CPU. Confirmed in practice: it correlated
        # with /api/market-intelligence hanging past its 22s client timeout
        # with no corresponding backend.log line. Fixed by (a) capping the
        # universe passed into any one refresh, (b) running the actual compute
        # in a background daemon thread so it never blocks the caller, and (c)
        # a non-reentrant guard so overlapping scan passes can't pile up
        # concurrent refreshes against each other.
        self._cs_scores: Dict[str, Dict[str, float]] = {}
        self._cs_refreshed_at: float = 0.0
        self._cs_symbol_refreshed_at: Dict[str, float] = {}
        self._cs_ttl_sec: float = 900.0
        self._cs_lock = threading.Lock()
        self._cs_running = False
        self._cs_max_universe = 300

    def research_python(self) -> Optional[str]:
        env_py = os.environ.get("PROJECT_LADDU_RESEARCH_PYTHON")
        if env_py and Path(env_py).exists():
            return env_py
        pointer = _programdata() / "ProjectLaddu" / "runtime" / "research_python.txt"
        try:
            candidate = Path(pointer.read_text(encoding="utf-8").strip())
            if candidate.is_file():
                return str(candidate)
        except OSError:
            pass
        return None

    def available(self) -> Dict[str, Any]:
        py = self.research_python()
        worker = self.base_dir / "research_worker.py"
        ready = bool(py and worker.exists())
        return {
            "ok": ready,
            "state": "RUNTIME_READY" if ready else "RUNTIME_UNAVAILABLE",
            "runtime_ready": ready,
            "model_ready": False,
            "research_python": py,
            "worker": str(worker),
            "execution_authority": "NONE",
            "broker_authority": "NONE",
            "reason": "ready" if ready else "installed research runtime or worker missing",
            "policy": "Research runtime availability is not model readiness or production influence.",
        }

    def refresh_cross_sectional(self, symbol_to_instrument_key: Dict[str, str], interval: str = "day", force: bool = False) -> Dict[str, Any]:
        """Kick off a cross-sectional (rank/scale/zscore) factor refresh for
        the given scan-pass universe, if the cache is stale. Returns
        immediately -- the actual computation runs on a background daemon
        thread, never on the calling thread (scanner loop or otherwise), so
        this can never itself hang or slow down an HTTP request. Capped to
        the first `_cs_max_universe` symbols so a single pass can't turn into
        thousands of sequential DB reads; a subsequent pass will simply cover
        a different slice as scan_orchestration_service rotates its batches.
        """
        now = time.time()
        if not self.store or len(symbol_to_instrument_key) < 3:
            return {"ok": False, "reason": "no_store_or_universe_too_small"}
        # R20: freshness is per symbol, not one global TTL. The old global TTL
        # meant the first 300-name slice could prevent every later scanner
        # batch from receiving cross-sectional evidence for 15 minutes.
        requested = []
        for symbol, key in symbol_to_instrument_key.items():
            sym = str(symbol or "").upper().strip()
            if not sym or not key:
                continue
            refreshed = float(self._cs_symbol_refreshed_at.get(sym) or 0.0)
            if force or (now - refreshed) >= self._cs_ttl_sec or sym not in self._cs_scores:
                requested.append((sym, key))
            if len(requested) >= self._cs_max_universe:
                break
        if len(requested) < 3:
            return {"ok": True, "skipped": "requested_symbols_current", "symbols_cached": len(self._cs_scores)}
        with self._cs_lock:
            if self._cs_running:
                return {"ok": True, "skipped": "already_running", "symbols_cached": len(self._cs_scores)}
            self._cs_running = True

        capped = dict(requested)

        def _worker():
            try:
                from core.factors.universe_panel_service import UniversePanelService
                svc = UniversePanelService(self.store)
                scores = svc.compute_cross_sectional(capped, interval=interval, limit=400, time_budget_sec=45.0)
                stamp = time.time()
                # Merge rather than replace so successive immutable-universe
                # batches accumulate evidence across the full sweep.
                self._cs_scores.update(scores)
                for sym in scores:
                    self._cs_symbol_refreshed_at[sym] = stamp
                self._cs_refreshed_at = stamp
            except Exception as exc:
                if self.logger:
                    try:
                        self.logger("WARN", "research_adapter", "cross-sectional refresh failed", {"error": str(exc)[:200]})
                    except Exception:
                        pass
            finally:
                with self._cs_lock:
                    self._cs_running = False

        threading.Thread(target=_worker, name="cross-sectional-refresh", daemon=True).start()
        return {"ok": True, "started": True, "universe": len(capped)}

    def run(self, *, symbol: str, mode: str, inst: Dict[str, Any], hist: Dict[str, Any],
            candles: List[Dict[str, Any]], selected_truth: Dict[str, Any]) -> Dict[str, Any]:
        py = self.research_python()
        worker = self.base_dir / "research_worker.py"
        if not py or not worker.exists():
            return {"ok": False, "status": "missing_runtime", "summary": "Installed research runtime authority is unavailable.", "factors": [], "evidence": {}}
        instrument_key = (inst or {}).get("instrument_key") or ""
        price_rows: List[Dict[str, Any]] = []
        delivery_rows: List[Dict[str, Any]] = []
        try:
            if self.store:
                price_rows = self.store.price_snapshots(symbol=symbol, instrument_key=instrument_key, limit=300)
        except Exception as exc:
            price_rows = []
        try:
            if self.store:
                # Two completed trading years when retained data is available;
                # this remains local authority data and never triggers provider I/O.
                delivery_rows = self.store.get_delivery_data(symbol, days=504)
        except Exception:
            delivery_rows = []
        candle_tail = 600 if str(mode or "").lower() == "intraday" else 756
        payload = {
            "symbol": symbol,
            "mode": mode,
            "instrument_key": instrument_key,
            "exchange": (inst or {}).get("exchange") or "NSE",
            "candles": (candles or [])[-candle_tail:],
            "price_snapshots": price_rows,
            "delivery_data": delivery_rows,
            "historical": {k: v for k, v in (hist or {}).items() if k != "candles"},
            "selected_truth": selected_truth or {},
        }
        sym_upper = str(symbol or "").upper()
        if sym_upper in self._cs_scores:
            # {symbol: {factor_id: value}} -- matches what research_worker's
            # _factor_zoo_family expects via payload["cross_sectional_scores"].
            payload["cross_sectional_scores"] = {sym_upper: self._cs_scores[sym_upper]}
        tmp_path = ""
        try:
            # Runtime work belongs under ProgramData. C:\Temp\ProjectLaddu is
            # evidence/log collection only and must never host executable/runtime
            # payloads or transient research input.
            work_dir = _programdata() / "ProjectLaddu" / "runtime" / "research-adapter-work"
            work_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = work_dir / ("research-input-" + uuid.uuid4().hex + ".json")
            tmp_file.write_text(json.dumps(payload, default=str), encoding="utf-8")
            tmp_path = str(tmp_file)
            env = dict(os.environ)
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("PROJECT_LADDU_RESEARCH_FAST", "1")
            proc = subprocess.run([py, str(worker), tmp_path], capture_output=True, text=True, timeout=self.timeout_sec, cwd=str(self.base_dir), env=env)
            raw = (proc.stdout or "").strip()
            if proc.returncode != 0:
                return {"ok": False, "status": "worker_failed", "summary": (proc.stderr or raw or "research worker failed")[:500], "factors": [], "evidence": {"returncode": proc.returncode}}
            data = json.loads(raw or "{}")
            if not isinstance(data, dict):
                raise ValueError("research worker did not return a JSON object")
            data.setdefault("ok", True)
            data.setdefault("status", "ok")
            data.setdefault("research_python", py)
            return data
        except subprocess.TimeoutExpired:
            return {"ok": False, "status": "timeout", "summary": f"Research adapter timed out after {self.timeout_sec}s. Heavy research runtimes are isolated; if this repeats, run with a smaller stored candle window or check the installed research runtime authority.", "factors": [], "evidence": {"research_python": py}}
        except Exception as exc:
            return {"ok": False, "status": "error", "summary": str(exc).splitlines()[0][:500], "factors": [], "evidence": {"research_python": py}}
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass
