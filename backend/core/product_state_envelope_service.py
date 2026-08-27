"""Canonical product-state envelope for every operator surface.

The browser must never assemble core truth from a fan-out of independently timed
HTTP calls.  This service projects one immutable, cache-only snapshot from the
already-published runtime authorities.  Heavy database/provider work remains in
its owning background workers; the HTTP route only returns :meth:`snapshot`.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from typing import Any, Dict, Mapping

from config import APP_VERSION
from models import now_iso


class ProductStateEnvelopeService:
    VERSION = "product-state-envelope-1.1.0-unified-system-operations-control-plane"
    STALE_AFTER_SEC = 15.0
    ACTIONABLE = {"BLOCKED", "FAILED", "STUCK", "CIRCUIT_OPEN", "NO_PROGRESS", "UNINSTRUMENTED"}
    SEVERITY = {
        "FAILED": 0,
        "CIRCUIT_OPEN": 1,
        "STUCK": 2,
        "BLOCKED": 3,
        "NO_PROGRESS": 4,
        "UNINSTRUMENTED": 5,
        "DEGRADED": 6,
        "WAITING": 7,
        "EXPECTED_IDLE": 8,
    }

    def __init__(self, app: Any):
        self.app = app
        self._lock = threading.RLock()
        self._snapshot: Dict[str, Any] = {
            "ok": True,
            "service_version": self.VERSION,
            "build": APP_VERSION,
            "snapshot_id": "warming",
            "state": "STARTING",
            "generated_at": None,
            "broker_authority": "NONE",
            "product_mode": "AUTOMATIC_MODEL_PAPER_ONLY",
            "sources": {},
            "desks": {"delivery": {}, "intraday": {}},
            "history": {},
            "performance": {},
            "maturity": {},
            "operations": {"authority": "CANONICAL_PRODUCT_STATE_CONTROL_PLANE", "read_endpoint": "/api/product-state", "action_endpoint": "/api/control-plane/action"},
            "blockers": [],
            "primary_blocker": None,
        }
        self._published_monotonic = time.monotonic()
        self._last_business_signature: str | None = None

    @staticmethod
    def _int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float(value: Any, default: float | None = None) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _map(value: Any) -> Dict[str, Any]:
        return dict(value) if isinstance(value, Mapping) else {}

    @classmethod
    def _scanner_stage(cls, node: Mapping[str, Any] | None) -> Dict[str, Any]:
        node = cls._map(node)
        analysis = cls._map(node.get("analysis")) or node
        progress = cls._map(analysis.get("progress_contract")) or cls._map(node.get("progress_contract"))
        cumulative = cls._map(analysis.get("sweep_stage_counts"))

        def first(*keys: str, default: int = 0) -> int:
            for key in keys:
                if key in cumulative and cumulative.get(key) is not None:
                    return cls._int(cumulative.get(key), default)
                if analysis.get(key) is not None:
                    return cls._int(analysis.get(key), default)
                if node.get(key) is not None:
                    return cls._int(node.get(key), default)
            return default

        universe = first("universe_size", default=cls._int(progress.get("population_count"), 0))
        attempted = first("attempted", "current_sweep_attempted", "current_sweep_scanned", "sweep_scanned")
        if attempted <= 0:
            attempted = cls._int(progress.get("current_sweep_scanned") or progress.get("last_completed_sweep_count"), 0)
        state = str(progress.get("state") or analysis.get("state") or node.get("state") or "UNKNOWN").upper()
        coverage_complete = bool(
            universe > 0
            and attempted >= universe
            and analysis.get("sweep_complete") is True
        )
        return {
            "state": state,
            "sweep_number": cls._int(progress.get("current_sweep_number") or analysis.get("sweep_number"), 0),
            "snapshot_id": analysis.get("checkpoint_snapshot_id") or progress.get("snapshot_id"),
            "universe": universe,
            "attempted": attempted,
            "quote_ready": first("quote_ready", "cycle_quote_ready"),
            "shortlisted": first("shortlisted", "cycle_shortlisted"),
            "data_pending": first("data_pending", "cycle_data_missing"),
            "analysed": first("analysed", "analysis_ready", "cycle_scanned"),
            "deferred": first("deferred", "capacity_deferred", "cycle_capacity_deferred"),
            "blocked": first("blocked", "cycle_blocked", "cycle_analysis_errors"),
            "mathematically_rejected": first("mathematically_rejected", "cycle_mathematically_rejected"),
            "analysis_terminal": first("analysis_terminal"),
            "analysis_pending": first("analysis_pending"),
            "analysis_unresolved": first("analysis_unresolved"),
            "analysis_complete": bool(first("analysis_complete")),
            "coverage_complete": coverage_complete,
            "map": first("trade_map", "map", "map_ready"),
            "rr": first("rr", "risk_reward", "rr_ready"),
            "final": first("final", "promoted", "cycle_promoted"),
            "last_progress_at": progress.get("last_progress_at") or analysis.get("last_progress_at") or node.get("last_run"),
            "next_run_at": progress.get("next_run_at") or analysis.get("next_run") or node.get("next_run"),
            "waiting_on": analysis.get("waiting_on") or progress.get("pause_reason") or node.get("message"),
            # Compatibility alias: this is cursor/universe coverage only, not
            # proof that deep analysis completed for every security.
            "sweep_complete": coverage_complete,
        }

    @classmethod
    def _research_stage(cls, research: Mapping[str, Any] | None, desk: str) -> Dict[str, Any]:
        research = cls._map(research)
        reconciliation = cls._map(research.get("reconciliation"))
        row = cls._map(cls._map(reconciliation.get("by_desk")).get(desk))
        stages = cls._map(row.get("stages"))
        state = str(row.get("state") or research.get("state") or "NOT_STARTED").upper()
        return {
            "state": state,
            "completion_state": str(row.get("completion_state") or state).upper(),
            "blockers": [str(item) for item in list(row.get("blockers") or [])[:8]],
            "population_fingerprint": row.get("population_fingerprint"),
            "population": cls._int(stages.get("captured") or row.get("candidate_count"), 0),
            "features": cls._int(stages.get("feature_complete"), 0),
            "baseline": cls._int(stages.get("baseline_predicted"), 0),
            "ml": cls._int(stages.get("ml_predicted"), 0),
            "hybrid": cls._int(stages.get("hybrid_predicted"), 0),
            "paper": cls._int(stages.get("paper_opened"), 0),
            "monitoring": cls._int(stages.get("monitoring"), 0),
            "settled": cls._int(stages.get("settled"), 0),
            "ledger": cls._int(stages.get("research_ledger"), 0),
            "performance": cls._int(stages.get("performance_attributed"), 0),
            "next_action": row.get("next_action") or research.get("waiting_on"),
            "paper_admission": cls._map(row.get("paper_admission")),
            "last_business_progress_at": research.get("last_business_progress_at"),
            "business_progress_age_sec": cls._float(research.get("business_progress_age_sec")),
            "expected_wait": bool(research.get("expected_wait")),
        }

    @classmethod
    def _source(cls, *, state: str, age: float | None = None, error: Any = None, detail: Any = None) -> Dict[str, Any]:
        value = str(state or "UNKNOWN").upper()
        return {
            "state": value,
            "age_sec": round(float(age), 3) if age is not None else None,
            "error": str(error)[:500] if error else None,
            "detail": detail,
            "available": value not in {"FAILED", "UNAVAILABLE", "STARTING"},
        }

    @classmethod
    def _stable_business_payload(cls, payload: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "state": payload.get("state"),
            "desks": payload.get("desks"),
            "history": payload.get("history"),
            "performance": payload.get("performance"),
            "maturity": payload.get("maturity"),
            "operations": {
                "counts": cls._map(payload.get("operations")).get("counts"),
                "primary_blocker": cls._map(payload.get("operations")).get("primary_blocker"),
            },
            "blockers": payload.get("blockers"),
        }

    @classmethod
    def _signature(cls, payload: Mapping[str, Any]) -> str:
        raw = json.dumps(cls._stable_business_payload(payload), sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _read_status(self) -> Dict[str, Any]:
        reader = getattr(self.app, "snapshot_status", None)
        if callable(reader):
            return dict(reader() or {})
        return copy.deepcopy(dict(getattr(self.app, "status", {}) or {}))

    def refresh(self) -> Dict[str, Any]:
        generated_at = now_iso()
        errors: Dict[str, str] = {}
        try:
            status = self._read_status()
        except Exception as exc:
            status = {}
            errors["runtime"] = f"{type(exc).__name__}: {exc}"

        modes = self._map(status.get("mode_scanners"))
        delivery_scan = self._scanner_stage(modes.get("delivery"))
        intraday_scan = self._scanner_stage(modes.get("intraday"))
        deep_history = self._map(status.get("deep_history_backfill"))
        history_total = self._int(deep_history.get("total"), 0)
        history_ready = self._int(deep_history.get("operational_ready") if deep_history.get("operational_ready") is not None else deep_history.get("done"), 0)
        history_terminal = self._int(deep_history.get("terminal_failures"), 0)
        history_accounted = self._int(deep_history.get("accounted"), history_ready + history_terminal)
        history_retry = self._int(deep_history.get("retry_scheduled"), 0)
        history_remaining_unaccounted = self._int(deep_history.get("remaining_unaccounted"), max(0, history_total - history_accounted))
        history_remaining_operational = self._int(deep_history.get("remaining_operational"), max(0, history_total - history_ready))
        history_state = str(deep_history.get("state") or ("STARTING" if not history_total else "UNKNOWN")).upper()
        history = {
            "state": history_state,
            "total": history_total,
            "operational_ready": history_ready,
            "research_ready": self._int(deep_history.get("research_ready"), 0),
            "deep_enriched": self._int(deep_history.get("deep_enriched"), 0),
            "accounted": history_accounted,
            "terminal_failures": history_terminal,
            "retry_scheduled": history_retry,
            "remaining_unaccounted": history_remaining_unaccounted,
            "remaining_operational": history_remaining_operational,
            "provider_depth_complete": self._int(deep_history.get("provider_depth_complete"), 0),
            "listing_history_complete": self._int(deep_history.get("listing_history_complete"), 0),
            "last_run": deep_history.get("last_run"),
            "current_item": deep_history.get("current_item"),
            "yield_reason": deep_history.get("yield_reason"),
            "accounted_complete": bool(history_total and history_accounted >= history_total),
            "operational_complete": bool(history_total and history_ready >= history_total),
        }

        try:
            conveyor = dict(self.app.data_conveyor.status() or {})
        except Exception as exc:
            conveyor = self._map(status.get("data_conveyor"))
            errors["research"] = f"{type(exc).__name__}: {exc}"
        research = self._map(conveyor.get("research"))
        official = self._map(conveyor.get("official"))
        official_summary = self._map(official.get("authority_summary"))
        delivery_research = self._research_stage(research, "delivery")
        intraday_research = self._research_stage(research, "intraday")

        try:
            maturity_projection = dict(self.app.maturity_projection.snapshot() or {})
        except Exception as exc:
            maturity_projection = {}
            errors["maturity"] = f"{type(exc).__name__}: {exc}"
        product_maturity = self._map(maturity_projection.get("product"))
        proof = self._map(maturity_projection.get("proof"))
        maturity_level = self._int(product_maturity.get("maturity_level"), 0)
        maturity = {
            "state": str(proof.get("state") or maturity_projection.get("state") or "UNAVAILABLE").upper(),
            "market_cycle_and_sector_rotation": copy.deepcopy(self._map(product_maturity.get("market_cycle_and_sector_rotation"))),
            "level": maturity_level,
            "level5_ready": bool(
                maturity_level == 5
                and product_maturity.get("level5_ready") is True
                and proof.get("passed") is True
            ),
            "evidence_score": self._float(proof.get("evidence_score") or product_maturity.get("evidence_score"), 0.0),
            "settled_observations": self._int(proof.get("settled_observations") or proof.get("settled_observation_count"), 0),
            "evidence_days": self._int(proof.get("evidence_days") or proof.get("trading_days"), 0),
            "regimes": self._int(proof.get("regimes") or proof.get("regime_count"), 0),
            "missing_gates": list(proof.get("missing_gates") or product_maturity.get("missing_level4_gates") or []),
            "projection_age_sec": self._float(maturity_projection.get("projection_age_sec")),
        }

        try:
            operations = dict(self.app.operations_control.summary() or {})
        except Exception as exc:
            operations = {}
            errors["operations"] = f"{type(exc).__name__}: {exc}"
        op_jobs = [dict(row) for row in list(operations.get("jobs") or []) if isinstance(row, Mapping)]
        op_counts = self._map(operations.get("counts"))
        controller = self._map(operations.get("controller"))

        blockers = []
        for row in op_jobs:
            state = str(row.get("state") or "UNKNOWN").upper()
            if state not in self.ACTIONABLE:
                continue
            blockers.append({
                "key": f"worker:{row.get('component') or row.get('job_id')}",
                "state": state,
                "title": row.get("title") or row.get("component") or row.get("job_id"),
                "detail": row.get("waiting_on") or row.get("last_error") or row.get("stage"),
                "owner": row.get("action_component") or row.get("component"),
                "source": "operations",
            })
        for desk, row in (("delivery", delivery_research), ("intraday", intraday_research)):
            state = str(row.get("state") or "").upper()
            if state in self.ACTIONABLE or state.endswith("_BLOCKED") or state.endswith("_PENDING"):
                blockers.append({
                    "key": f"research:{desk}", "state": "BLOCKED" if "BLOCKED" in state else "NO_PROGRESS",
                    "title": f"{desk.title()} research lifecycle",
                    "detail": row.get("next_action") or state,
                    "owner": "data_conveyor", "source": "research",
                })
        if history_total and history_remaining_unaccounted > 0 and history_state in {"FAILED", "STUCK", "NO_PROGRESS"}:
            blockers.append({
                "key": "history:deep_backfill",
                "state": history_state if history_state in self.ACTIONABLE else "NO_PROGRESS",
                "title": "Historical coverage",
                "detail": f"{history_ready}/{history_total} operational-ready · {history_retry} retry · {history_terminal} terminal · {history_remaining_unaccounted} unaccounted",
                "owner": "deep_history_backfill",
                "source": "history",
            })

        # De-duplicate by key/state while retaining the highest-severity record.
        dedup: Dict[str, Dict[str, Any]] = {}
        for blocker in blockers:
            key = str(blocker.get("key") or "unknown")
            prior = dedup.get(key)
            if prior is None or self.SEVERITY.get(str(blocker.get("state")), 99) < self.SEVERITY.get(str(prior.get("state")), 99):
                dedup[key] = blocker
        blockers = sorted(dedup.values(), key=lambda row: (self.SEVERITY.get(str(row.get("state")), 99), str(row.get("key"))))
        primary = blockers[0] if blockers else self._map(controller.get("primary_blocker")) or None

        research_settled = delivery_research["settled"] + intraday_research["settled"]
        research_paper = delivery_research["paper"] + intraday_research["paper"]
        performance = {
            "state": "EVIDENCE_AVAILABLE" if research_settled else "WAITING_SETTLEMENT" if research_paper else "WAITING_ADMISSION",
            "model_paper": {
                "open": research_paper,
                "settled": research_settled,
                "accuracy": None,
                "net_pnl": None,
                "authority": "CURRENT_RESEARCH_LIFECYCLE",
            },
            "production": {
                "state": "NOT_APPLICABLE",
                "reason": "Broker authority NONE; Project Laddu is Model Paper only",
                "broker_authority": "NONE",
            },
        }

        critical_ops = sum(self._int(op_counts.get(key), 0) for key in ("FAILED", "STUCK", "CIRCUIT_OPEN"))
        no_progress = sum(self._int(op_counts.get(key), 0) for key in ("NO_PROGRESS", "UNINSTRUMENTED"))
        if critical_ops:
            overall = "FAILED"
        elif blockers or no_progress:
            overall = "BLOCKED"
        elif any(row.get("state") in {"RUNNING", "SCANNING", "ACTIVE"} for row in (delivery_scan, intraday_scan)):
            overall = "IN_PROGRESS"
        elif delivery_research.get("expected_wait") or intraday_research.get("expected_wait"):
            overall = "WAITING"
        else:
            overall = "READY"

        sources = {
            "runtime": self._source(state="DEGRADED" if errors.get("runtime") else "READY", error=errors.get("runtime")),
            "scanner": self._source(state="READY" if modes else "UNAVAILABLE", detail={"delivery": delivery_scan.get("state"), "intraday": intraday_scan.get("state")}),
            "history": self._source(state=history_state, detail={"operational_ready": history_ready, "total": history_total, "retry_scheduled": history_retry, "terminal_failures": history_terminal}),
            "research": self._source(state=str(conveyor.get("state") or "UNAVAILABLE"), error=errors.get("research"), detail=research.get("waiting_on")),
            "official_data": self._source(state=str(official.get("state") or "UNAVAILABLE"), detail={"trade_date": official.get("trade_date"), "critical_current": official_summary.get("critical_current"), "critical_total": official_summary.get("critical_total") or official_summary.get("critical_required"), "evidence_start_ready": official_summary.get("evidence_start_ready")}),
            "maturity": self._source(state=str(maturity_projection.get("state") or "UNAVAILABLE"), age=self._float(maturity_projection.get("projection_age_sec")), error=errors.get("maturity")),
            "operations": self._source(state=str(operations.get("state") or "UNAVAILABLE"), age=self._float(operations.get("projection_age_sec")), error=errors.get("operations")),
        }

        payload: Dict[str, Any] = {
            "ok": True,
            "service_version": self.VERSION,
            "build": APP_VERSION,
            "state": overall,
            "generated_at": generated_at,
            "broker_authority": "NONE",
            "product_mode": "AUTOMATIC_MODEL_PAPER_ONLY",
            "sources": sources,
            "desks": {
                "delivery": {"scanner": delivery_scan, "research": delivery_research},
                "intraday": {"scanner": intraday_scan, "research": intraday_research},
            },
            "history": history,
            "performance": performance,
            "data_authority": {"state": str(official.get("state") or "UNAVAILABLE").upper(), "summary": official_summary, "trade_date": official.get("trade_date"), "error": official.get("error")},
            "maturity": maturity,
            "operations": {
                "authority": "CANONICAL_PRODUCT_STATE_CONTROL_PLANE",
                "read_endpoint": "/api/product-state",
                "action_endpoint": "/api/control-plane/action",
                "state": str(operations.get("state") or "UNAVAILABLE").upper(),
                "counts": op_counts,
                "jobs": op_jobs,
                "primary_blocker": primary,
                "controller": controller,
                "database_pools": self._map(operations.get("database_pools")),
                "workload_governor": self._map(operations.get("workload_governor")),
                "recent_actions": [dict(row) for row in list(operations.get("recent_actions") or [])[-20:] if isinstance(row, Mapping)],
                "business_signature": operations.get("business_signature"),
                "projection_generation": self._int(operations.get("projection_generation"), 0),
                "projection_age_sec": self._float(operations.get("projection_age_sec")),
                "policy": "background materialization; cache-only operator read; governed command dispatch",
            },
            "blockers": blockers,
            "primary_blocker": primary,
            "source_errors": errors,
        }
        signature = self._signature(payload)
        payload["business_signature"] = signature
        payload["snapshot_id"] = f"{APP_VERSION}:{signature}"
        with self._lock:
            self._snapshot = copy.deepcopy(payload)
            self._published_monotonic = time.monotonic()
        return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            payload = copy.deepcopy(self._snapshot)
            payload["snapshot_age_sec"] = round(max(0.0, time.monotonic() - self._published_monotonic), 3)
            payload["stale"] = payload["snapshot_age_sec"] > self.STALE_AFTER_SEC
        return payload

    def run(self, supervisor: Any, *, running_fn) -> None:
        name = "product_state_envelope"
        while supervisor.running and running_fn():
            supervisor.beat(name)
            try:
                payload = self.refresh()
                signature = str(payload.get("business_signature") or "")
                unchanged = bool(signature and signature == self._last_business_signature)
                self._last_business_signature = signature or self._last_business_signature
                blockers = len(list(payload.get("blockers") or []))
                supervisor.progress(
                    name,
                    token=signature or "warming",
                    stage="canonical_product_state",
                    completed_units=max(0, 5 - min(blockers, 5)),
                    total_units=5,
                    waiting_on=(str((payload.get("primary_blocker") or {}).get("detail") or "")[:500] if blockers else None),
                    expected_idle=unchanged and blockers == 0,
                )
            except Exception as exc:
                try:
                    self.app.event("ERROR", name, "Product-state envelope projection failed", {"error": str(exc)[:300]})
                except Exception:
                    pass
            for _ in range(10):
                if not supervisor.running or not running_fn():
                    return
                supervisor.beat(name)
                time.sleep(0.5)
