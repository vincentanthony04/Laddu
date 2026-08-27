"""Background maturity/evidence projection for the autonomic controller.

Level-5 maturity evaluation is intentionally expensive: it reconciles scanners,
decisions, official data, research and installed evidence.  That work must never
run on the controller loop or an HTTP request thread.  This service owns the
heavy evaluation and publishes a bounded immutable last-known snapshot.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Dict

from core.level5_operational_proof_service import Level5OperationalProofService
from core.product_maturity_service import ProductMaturityService
from models import now_iso


class MaturityProjectionService:
    VERSION = "maturity-projection-1.1.0-bounded-governance-sync"

    def __init__(self, app: Any):
        self.app = app
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._snapshot: Dict[str, Any] = {
            "ok": True,
            "state": "STARTING",
            "version": self.VERSION,
            "product": {"ok": False, "maturity_level": 0, "missing_level4_gates": ["maturity_projection_warming"]},
            "proof": {"ok": False, "passed": False, "state": "STARTING", "missing_gates": ["maturity_projection_warming"], "gates": {}},
            "projected_at": None,
        }
        self._projected_monotonic = time.monotonic()
        self._last_business_signature: str | None = None
        self._component_lock = threading.RLock()
        self._component_runs: Dict[str, Dict[str, Any]] = {}
        # Restore the last Level-5 proof immediately when available. This is a
        # read-only last-known snapshot, never a new maturity assertion.
        try:
            proof = app.store.get_kv(Level5OperationalProofService.KV_KEY, {}) or {}
            if isinstance(proof, dict) and proof:
                self._snapshot["proof"] = dict(proof)
                self._snapshot["state"] = "RESTORED_LAST_KNOWN"
        except Exception:
            pass


    def _bounded_component(self, label: str, fn: Callable[[], Dict[str, Any]], *, timeout_sec: float = 30.0) -> Dict[str, Any]:
        """Evaluate one maturity component without letting it pin the supervised loop.

        Python cannot safely kill an arbitrary blocked thread.  Therefore one
        daemon generation per component is allowed at a time.  A timeout is
        published explicitly and later cycles observe the same in-flight
        generation instead of stacking more database/research work.
        """
        timeout_sec = max(1.0, float(timeout_sec))
        with self._component_lock:
            prior = self._component_runs.get(label)
            if prior and prior.get("thread") and prior["thread"].is_alive():
                age = max(0.0, time.monotonic() - float(prior.get("started") or time.monotonic()))
                return {
                    "ok": False, "state": "COMPONENT_IN_FLIGHT_TIMEOUT",
                    "component": label, "age_sec": round(age, 2),
                    "error": f"{label} evaluation is still in flight from a prior bounded generation",
                }
            box: queue.Queue = queue.Queue(maxsize=1)

            def work() -> None:
                try:
                    box.put((True, dict(fn() or {})), block=False)
                except Exception as exc:
                    try:
                        box.put((False, f"{type(exc).__name__}: {exc}"), block=False)
                    except Exception:
                        pass

            thread = threading.Thread(target=work, name=f"LadduMaturity-{label}", daemon=True)
            row = {"thread": thread, "started": time.monotonic(), "box": box}
            self._component_runs[label] = row
            thread.start()
        thread.join(timeout=timeout_sec)
        if thread.is_alive():
            return {
                "ok": False, "state": "COMPONENT_TIMEOUT", "component": label,
                "timeout_sec": timeout_sec,
                "error": f"{label} exceeded the bounded maturity evaluation deadline",
            }
        with self._component_lock:
            self._component_runs.pop(label, None)
        try:
            ok, value = box.get_nowait()
        except Exception:
            return {"ok": False, "state": "COMPONENT_NO_RESULT", "component": label}
        if ok:
            return dict(value or {})
        return {"ok": False, "state": "COMPONENT_FAILED", "component": label, "error": str(value)[:300]}

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            payload = {
                **self._snapshot,
                "product": dict(self._snapshot.get("product") or {}),
                "proof": dict(self._snapshot.get("proof") or {}),
                "projection_age_sec": round(max(0.0, time.monotonic() - self._projected_monotonic), 3),
            }
        return payload

    def refresh(self) -> Dict[str, Any]:
        if not self._refresh_lock.acquire(blocking=False):
            return self.snapshot()
        started = time.monotonic()
        try:
            service = getattr(self.app, "level5_forward_maturity", None)
            forward_maturity = (
                self._bounded_component(
                    "forward_maturity", lambda: service.run_checkpoint(sync_max_batches=1), timeout_sec=30.0
                ) if service is not None else {"ok": False, "state": "NOT_CONFIGURED"}
            )
            product = self._bounded_component(
                "product_maturity", lambda: ProductMaturityService(self.app).status(), timeout_sec=30.0
            )
            if not product.get("ok") and product.get("maturity_level") is None:
                product.setdefault("maturity_level", 0)
                product.setdefault("missing_level4_gates", ["product_maturity_unavailable"])
            proof = self._bounded_component(
                "level5_proof", lambda: Level5OperationalProofService(self.app).status(), timeout_sec=30.0
            )
            if not proof.get("ok") and proof.get("passed") is None:
                proof.setdefault("passed", False)
                proof.setdefault("missing_gates", ["level5_proof_unavailable"])
                proof.setdefault("gates", {})
            payload = {
                "ok": bool(product.get("ok", True)) and bool(proof.get("ok", True)),
                "state": "READY" if product or proof else "UNAVAILABLE",
                "version": self.VERSION,
                "product": dict(product or {}),
                "proof": dict(proof or {}),
                "forward_maturity": dict(forward_maturity or {}),
                "projected_at": now_iso(),
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 2),
            }
            with self._lock:
                self._snapshot = payload
                self._projected_monotonic = time.monotonic()
            return self.snapshot()
        finally:
            self._refresh_lock.release()

    def run(self, supervisor: Any, *, running_fn) -> None:
        name = "maturity_projection"
        while supervisor.running and running_fn():
            supervisor.beat(name)
            try:
                with supervisor.heartbeat_guard(name):
                    payload = self.refresh()
                product = dict(payload.get("product") or {})
                proof = dict(payload.get("proof") or {})
                missing = len(list(proof.get("missing_gates") or []))
                proof_state = str(proof.get("state") or "UNKNOWN")
                settled = int(proof.get("settled_observations") or proof.get("settled_observation_count") or 0)
                signature = f"{int(product.get('maturity_level') or 0)}:{missing}:{proof_state}:{settled}"
                unchanged = self._last_business_signature == signature
                self._last_business_signature = signature
                supervisor.progress(
                    name,
                    token=signature,
                    stage="project_level5_evidence",
                    completed_units=max(0, 5 - missing),
                    total_units=5,
                    waiting_on="awaiting new settled/qualification evidence" if unchanged else None,
                    expected_idle=unchanged,
                )
            except Exception as exc:
                try:
                    self.app.event("ERROR", "maturity_projection", "Maturity projection failed", {"error": str(exc)[:300]})
                except Exception:
                    pass
            # Maturity is evidence work, not a foreground loop. One minute is
            # sufficiently responsive while avoiding continuous heavy queries.
            for _ in range(60):
                if not supervisor.running or not running_fn():
                    return
                supervisor.beat(name)
                time.sleep(1)
