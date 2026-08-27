from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.production_mode_policy import require_production_mode


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value in (None, "", "—"):
            return default
        n = float(value)
        if math.isfinite(n):
            return n
    except Exception:
        pass
    return default


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


def _state_score(text: Any, good_words: tuple[str, ...], bad_words: tuple[str, ...], neutral: float = 50.0) -> float:
    raw = str(text or "").lower()
    if any(w in raw for w in good_words):
        return 80.0
    if any(w in raw for w in bad_words):
        return 20.0
    if raw.strip():
        return neutral
    return 35.0


class DecisionLedger:
    """v39: auditable reasoning/calculation ledger for Stock Intelligence.

    This is deliberately pure-Python and dependency-light. Advanced libraries
    (TA-Lib, pandas-ta, vectorbt, qlib-style factors) belong behind this stable
    registry/ledger boundary, not inside the live UI path. The product contract is
    that every decision records the evidence, formula/factor contribution,
    contradictions and replayability used by the card.
    """

    def __init__(self, store, logger=None):
        self.store = store
        self.logger = logger
        self.ensure_schema()

    @property
    def conn(self):
        return self.store.conn

    def ensure_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS decision_runs (
              run_id TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              exchange TEXT DEFAULT 'NSE',
              instrument_key TEXT,
              mode TEXT,
              run_ts TEXT NOT NULL,
              data_from_ts TEXT,
              data_to_ts TEXT,
              decision TEXT,
              side TEXT,
              confidence TEXT,
              final_score REAL,
              freshness_status TEXT,
              replayable INTEGER DEFAULT 0,
              source TEXT DEFAULT 'stock_intelligence',
              summary TEXT,
              payload_json TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_decision_runs_symbol_mode_ts ON decision_runs(symbol, mode, run_ts);

            CREATE TABLE IF NOT EXISTS decision_factors (
              run_id TEXT NOT NULL,
              factor_name TEXT NOT NULL,
              raw_value TEXT,
              normalized_score REAL,
              weight REAL,
              contribution REAL,
              status TEXT,
              explanation TEXT,
              PRIMARY KEY(run_id, factor_name)
            );

            CREATE TABLE IF NOT EXISTS decision_evidence (
              run_id TEXT NOT NULL,
              evidence_type TEXT NOT NULL,
              source_table TEXT,
              source_ts TEXT,
              value_json TEXT,
              freshness_sec REAL,
              status TEXT,
              PRIMARY KEY(run_id, evidence_type, source_table)
            );

            CREATE TABLE IF NOT EXISTS decision_contradictions (
              run_id TEXT NOT NULL,
              issue TEXT NOT NULL,
              severity TEXT,
              penalty REAL,
              explanation TEXT,
              PRIMARY KEY(run_id, issue)
            );
            """
        )
        self.conn.commit()

    def _price_snapshot_summary(self, symbol: str, instrument_key: str = "") -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        try:
            rows = self.store.price_snapshots(symbol=symbol, instrument_key=instrument_key, limit=500)
        except Exception:
            rows = []
        if not rows:
            return {"count": 0, "latest": None, "first": None, "status": "missing", "ltp_path": []}
        # price_snapshots() returns newest-first.
        ltp_path = [r for r in reversed(rows[-20:]) if _num(r.get("ltp")) is not None]
        return {
            "count": len(rows),
            "latest": rows[0].get("captured_at"),
            "first": rows[-1].get("captured_at"),
            "status": "ok" if len(rows) >= 2 else "thin",
            "ltp_path": [{"ts": r.get("captured_at"), "ltp": r.get("ltp"), "change_pct": r.get("change_pct")} for r in ltp_path],
        }

    def _delivery_summary(self, symbol: str) -> Dict[str, Any]:
        try:
            rows = self.store.get_delivery_data(symbol, days=20)
        except Exception:
            rows = []
        if not rows:
            return {"count": 0, "latest_date": None, "avg_pct": None, "latest_pct": None, "status": "missing"}
        pcts = [_num(r.get("delivery_pct")) for r in rows]
        pcts = [p for p in pcts if p is not None]
        latest_pct = _num(rows[0].get("delivery_pct"))
        avg_pct = statistics.mean(pcts) if pcts else None
        return {
            "count": len(rows),
            "latest_date": rows[0].get("trade_date"),
            "avg_pct": avg_pct,
            "latest_pct": latest_pct,
            "status": "ok" if latest_pct is not None else "thin",
        }

    def _candle_summary(self, hist: Dict[str, Any], candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        count = int(hist.get("count") or len(candles) or 0)
        last = (hist.get("last_candle") or {}).get("timestamp")
        if not last and candles:
            last = candles[-1].get("timestamp") or candles[-1].get("ts")
        first = candles[0].get("timestamp") if candles else None
        return {
            "count": count,
            "interval": hist.get("interval") or "day",
            "first": first,
            "latest": last,
            "status": hist.get("data_status") or ("ok" if count else "missing"),
        }

    def _compute_factor_rows(self, symbol: str, mode: str, decision: Dict[str, Any], hist: Dict[str, Any],
                             candles: List[Dict[str, Any]], price_summary: Dict[str, Any],
                             delivery_summary: Dict[str, Any], selected_truth: Dict[str, Any]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        factors: List[Dict[str, Any]] = []
        contradictions: List[Dict[str, Any]] = []

        def add(name: str, raw: Any, score: float, weight: float, status: str, explanation: str):
            score = _clamp(score)
            factors.append({
                "factor_name": name,
                "raw_value": raw,
                "normalized_score": round(score, 2),
                "weight": weight,
                "contribution": round(score * weight / 100.0, 2),
                "status": status,
                "explanation": explanation,
            })

        rsi = _num(decision.get("rsi"))
        if rsi is None:
            rsi_score, rsi_status = 40.0, "missing"
        elif 45 <= rsi <= 65:
            rsi_score, rsi_status = 78.0, "healthy"
        elif 35 <= rsi < 45 or 65 < rsi <= 72:
            rsi_score, rsi_status = 58.0, "watch"
        elif rsi > 80 or rsi < 25:
            rsi_score, rsi_status = 25.0, "risk"
        else:
            rsi_score, rsi_status = 45.0, "neutral"
        add("RSI", rsi if rsi is not None else "missing", rsi_score, 8.0, rsi_status, "Momentum health from selected-mode technical context.")

        adx = _num(decision.get("adx"))
        adx_score = 78.0 if adx is not None and adx >= 22 else 55.0 if adx is not None and adx >= 15 else 35.0
        add("ADX trend strength", adx if adx is not None else "missing", adx_score, 8.0, "ok" if adx_score >= 70 else "watch", "Trend-strength evidence; weak ADX reduces conviction.")

        volume_state = decision.get("volume_state") or decision.get("volume_profile") or ""
        vol_score = _state_score(volume_state, ("spike", "expansion", "strong", "above", "high"), ("weak", "thin", "low", "dry"), 55.0)
        add("Volume confirmation", volume_state or "missing", vol_score, 12.0, "ok" if vol_score >= 70 else "watch", "Volume should confirm breakout/continuation/reversal.")

        hist_count = int(hist.get("count") or len(candles) or selected_truth.get("historical_count") or 0)
        required = int(selected_truth.get("required_candles") or (120 if mode == "delivery" else 20))
        cov_score = 100.0 if hist_count >= required else 65.0 if hist_count >= max(5, required // 2) else 25.0 if hist_count else 0.0
        add("Candle coverage", f"{hist_count}/{required}", cov_score, 15.0, "ok" if hist_count >= required else "partial", "Technical analysis is only trusted when stored candle coverage is enough.")

        ps_count = int(price_summary.get("count") or 0)
        ps_score = 95.0 if ps_count >= 20 else 70.0 if ps_count >= 5 else 40.0 if ps_count else 0.0
        add("All price snapshots", f"{ps_count} snapshots", ps_score, 12.0, "ok" if ps_count >= 5 else "missing", "Uses stored quote/price path, not only candle OHLC.")

        delivery_pct = _num(delivery_summary.get("latest_pct"))
        delivery_avg = _num(delivery_summary.get("avg_pct"))
        if mode == "delivery":
            if delivery_pct is None:
                delivery_score, delivery_status = 20.0, "missing"
            elif delivery_avg is not None and delivery_pct >= delivery_avg:
                delivery_score, delivery_status = 78.0, "ok"
            else:
                delivery_score, delivery_status = 55.0, "watch"
            add("Delivery accumulation", f"latest {delivery_pct} avg {delivery_avg}", delivery_score, 12.0, delivery_status, "Delivery confidence needs NSE delivery evidence.")
        else:
            add("Delivery accumulation", "not required for fast desk", 50.0, 2.0, "neutral", "Intraday uses live price/candle evidence first.")

        rr = _num(decision.get("rr"))
        rr_score = 88.0 if rr is not None and rr >= 2.0 else 70.0 if rr is not None and rr >= 1.5 else 40.0 if rr is not None else 20.0
        add("Risk reward", rr if rr is not None else "missing", rr_score, 11.0, "ok" if rr_score >= 70 else "risk", "Promotion should not ignore R:R.")

        market_raw = " ".join(str(decision.get(k) or "") for k in ("index_context", "sector_context", "market_context"))
        market_score = _state_score(market_raw, ("support", "bull", "green", "strong"), ("weak", "bear", "red", "conflict"), 55.0)
        add("Index/sector support", market_raw or "pending", market_score, 10.0, "ok" if market_score >= 70 else "watch", "NIFTY/sector context should agree with the selected-side idea.")

        valid_map = bool(selected_truth.get("valid_trade_map"))
        add("Side-aware level map", "valid" if valid_map else "invalid/pending", 90.0 if valid_map else 25.0, 10.0, "ok" if valid_map else "fail", "Long SL must be below entry; short SL must be above entry; no fake level map.")

        freshness = selected_truth.get("data_status") or hist.get("data_status") or "pending"
        fresh_score = _state_score(freshness, ("full", "ok", "fresh"), ("stale", "missing", "pending"), 55.0)
        add("Freshness", freshness, fresh_score, 10.0, "ok" if fresh_score >= 70 else "watch", "Stale/missing data is a penalty, not hidden.")

        # Contradictions / penalties. These are visible in the UI and persisted.
        entry = _num(decision.get("entry"))
        sl = _num(decision.get("sl"))
        side = str(decision.get("side") or "").upper()
        if entry is not None and sl is not None:
            if ("LONG" in side or side in ("BUY", "BULLISH", "ACCUMULATE")) and sl >= entry:
                contradictions.append({"issue": "invalid_long_sl", "severity": "FAIL", "penalty": -20.0, "explanation": "Long stop-loss is not below entry."})
            if ("SHORT" in side or side in ("SELL", "BEARISH")) and sl <= entry:
                contradictions.append({"issue": "invalid_short_sl", "severity": "FAIL", "penalty": -20.0, "explanation": "Short stop-loss is not above entry."})
        if hist_count < required:
            contradictions.append({"issue": "thin_candle_history", "severity": "WARN", "penalty": -8.0, "explanation": f"Only {hist_count}/{required} candles available."})
        if ps_count == 0:
            contradictions.append({"issue": "missing_price_snapshots", "severity": "FAIL", "penalty": -15.0, "explanation": "No stored quote/price snapshots available; candle-only proof is insufficient."})
        if mode == "delivery" and delivery_summary.get("status") == "missing":
            contradictions.append({"issue": "missing_delivery_data", "severity": "WARN", "penalty": -8.0, "explanation": "Delivery mode lacks NSE delivery data."})
        return factors, contradictions

    def build_and_store(self, *, symbol: str, mode: str, inst: Dict[str, Any], hist: Dict[str, Any],
                        analysis: Dict[str, Any], selected_truth: Dict[str, Any],
                        candles: Optional[List[Dict[str, Any]]] = None,
                        quote_payload: Optional[Dict[str, Any]] = None,
                        research_result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.ensure_schema()
        symbol = str(symbol or "").upper().strip()
        mode = require_production_mode(mode or "delivery")
        inst = inst or {}
        decision = (analysis or {}).get("decision") if isinstance(analysis, dict) else {}
        decision = decision if isinstance(decision, dict) else {}
        candles = candles or []
        instrument_key = inst.get("instrument_key") or decision.get("instrument_key") or ""
        exchange = inst.get("exchange") or decision.get("exchange") or "NSE"
        price_summary = self._price_snapshot_summary(symbol, instrument_key)
        delivery_summary = self._delivery_summary(symbol)
        candle_summary = self._candle_summary(hist or {}, candles)
        factors, contradictions = self._compute_factor_rows(symbol, mode, decision, hist or {}, candles, price_summary, delivery_summary, selected_truth or {})
        research_result = research_result or {}
        research_factors = []
        if isinstance(research_result, dict) and research_result.get("ok"):
            for f in (research_result.get("factors") or []):
                if not isinstance(f, dict):
                    continue
                # Namespace external-library factors so the UI clearly shows what came from the research adapter.
                name = str(f.get("factor_name") or "Research factor")
                lib = str(f.get("library") or research_result.get("source") or "research_venv")
                research_factors.append({
                    "factor_name": name if name.lower().startswith(("ta:", "pandas-ta", "qlib", "hkuds", "backtesting", "smc", "price path", "nse")) else f"Research: {name}",
                    "raw_value": f.get("raw_value"),
                    "normalized_score": _clamp(_num(f.get("normalized_score"), 50.0) or 50.0),
                    "weight": _num(f.get("weight"), 1.0) or 1.0,
                    "contribution": _num(f.get("contribution"), 0.0) or 0.0,
                    "status": f.get("status") or "ok",
                    "explanation": f"[{lib}] {f.get('explanation') or 'Research adapter factor'}",
                })
            factors.extend(research_factors)
        elif isinstance(research_result, dict) and research_result and research_result.get("status") not in ("deferred", "pending", "not_run"):
            contradictions.append({"issue": "research_adapter_unavailable", "severity": "WARN", "penalty": -3.0, "explanation": research_result.get("summary") or research_result.get("status") or "Research adapter unavailable."})
        base_score = sum(float(f.get("contribution") or 0.0) for f in factors)
        penalty = sum(float(c.get("penalty") or 0.0) for c in contradictions)
        final_score = _clamp(base_score + penalty)
        replayable = bool((candle_summary.get("count") or 0) > 0 and (price_summary.get("count") or 0) > 0)
        run_ts = _now_iso()
        run_id = f"{symbol}:{mode}:{run_ts}"
        data_times = [x for x in (candle_summary.get("first"), price_summary.get("first")) if x]
        data_to = max([x for x in (candle_summary.get("latest"), price_summary.get("latest"), delivery_summary.get("latest_date")) if x], default=None)
        data_from = min(data_times, default=None)
        evidences = [
            {"evidence_type": "candles", "source_table": "candles", "source_ts": candle_summary.get("latest"), "status": candle_summary.get("status"), "value": candle_summary},
            {"evidence_type": "all_price_snapshots", "source_table": "price_snapshots", "source_ts": price_summary.get("latest"), "status": price_summary.get("status"), "value": price_summary},
            {"evidence_type": "nse_delivery", "source_table": "delivery_data", "source_ts": delivery_summary.get("latest_date"), "status": delivery_summary.get("status"), "value": delivery_summary},
            {"evidence_type": "latest_quote", "source_table": "quotes", "source_ts": (quote_payload or {}).get("timestamp") or (quote_payload or {}).get("source_time"), "status": "ok" if quote_payload else "pending", "value": quote_payload or {}},
            {"evidence_type": "research_adapter", "source_table": "research_venv", "source_ts": (research_result or {}).get("run_ts"), "status": (research_result or {}).get("status") or ("ok" if research_factors else "missing"), "value": {"ok": bool((research_result or {}).get("ok")), "source": (research_result or {}).get("source"), "score_contribution": (research_result or {}).get("score_contribution"), "factor_count": len(research_factors), "evidence": (research_result or {}).get("evidence") or {}}},
        ]
        payload = {
            "run_id": run_id,
            "symbol": symbol,
            "exchange": exchange,
            "instrument_key": instrument_key,
            "mode": mode,
            "run_ts": run_ts,
            "decision": decision.get("decision") or decision.get("status") or "WATCH",
            "side": decision.get("side") or "NEUTRAL",
            "confidence": (selected_truth or {}).get("confidence") or decision.get("confidence") or "Low",
            "final_score": round(final_score, 2),
            "base_score": round(base_score, 2),
            "penalty": round(penalty, 2),
            "freshness_status": (selected_truth or {}).get("data_status") or candle_summary.get("status") or "pending",
            "replayable": replayable,
            "data_from_ts": data_from,
            "data_to_ts": data_to,
            "factors": factors,
            "evidence": evidences,
            "contradictions": contradictions,
            "summary": "Replayable from stored candles + all price snapshots" if replayable else "Not replayable yet: missing stored candle/price evidence",
            "research_adapter": research_result or {"ok": False, "status": "not_run"},
            "library_policy": {
                "core": ["pandas/numpy-compatible factor interface", "built-in pure Python ledger", "subprocess research_venv adapter"],
                "active_research_plugins": ["ta", "pandas-ta-classic", "backtesting.py", "Qlib runtime", "HKUDS Vibe-Trading runtime", "smartmoneyconcepts availability"],
                "disabled_or_separate_env": ["vectorbt", "pyharmonics/alpaca"],
                "live_rule": "Only replay-validated factors should influence live Selected Candidates.",
            },
        }
        try:
            with self.store.write_lock:
                self.conn.execute("""INSERT OR REPLACE INTO decision_runs(run_id,symbol,exchange,instrument_key,mode,run_ts,data_from_ts,data_to_ts,decision,side,confidence,final_score,freshness_status,replayable,source,summary,payload_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, symbol, exchange, instrument_key, mode, run_ts, data_from, data_to, payload["decision"], payload["side"], payload["confidence"], final_score, payload["freshness_status"], 1 if replayable else 0, "stock_intelligence", payload["summary"], json.dumps(payload)))
                self.conn.executemany("""INSERT OR REPLACE INTO decision_factors(run_id,factor_name,raw_value,normalized_score,weight,contribution,status,explanation)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    [(run_id, f["factor_name"], json.dumps(f.get("raw_value")), f.get("normalized_score"), f.get("weight"), f.get("contribution"), f.get("status"), f.get("explanation")) for f in factors])
                self.conn.executemany("""INSERT OR REPLACE INTO decision_evidence(run_id,evidence_type,source_table,source_ts,value_json,freshness_sec,status)
                    VALUES(?,?,?,?,?,?,?)""",
                    [(run_id, e["evidence_type"], e.get("source_table"), e.get("source_ts"), json.dumps(e.get("value") or {}), None, e.get("status")) for e in evidences])
                self.conn.executemany("""INSERT OR REPLACE INTO decision_contradictions(run_id,issue,severity,penalty,explanation)
                    VALUES(?,?,?,?,?)""",
                    [(run_id, c["issue"], c.get("severity"), c.get("penalty"), c.get("explanation")) for c in contradictions])
                self.conn.commit()
        except Exception as exc:
            payload["persist_error"] = str(exc)[:180]
        return payload

    def latest(self, symbol: str, mode: str = "") -> Dict[str, Any]:
        self.ensure_schema()
        params: List[Any] = [str(symbol or "").upper().strip()]
        where = "UPPER(symbol)=?"
        if mode:
            where += " AND mode=?"
            params.append(str(mode).lower())
        row = self.conn.execute(f"SELECT payload_json FROM decision_runs WHERE {where} ORDER BY run_ts DESC LIMIT 1", tuple(params)).fetchone()
        if not row:
            return {"ok": False, "symbol": symbol, "mode": mode, "error": "no decision ledger run yet"}
        try:
            payload = json.loads(row["payload_json"] or "{}")
            payload["ok"] = True
            return payload
        except Exception:
            return {"ok": False, "symbol": symbol, "mode": mode, "error": "ledger payload unreadable"}
