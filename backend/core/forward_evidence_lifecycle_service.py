"""Automatic forward-evidence lifecycle for both active Project Laddu desks.

The service closes the gap between point-in-time selector capture and the
read-only ForwardEvidenceClock.  It periodically finds candidates that still
lack one or more governed outcomes, settles them from already-authoritative
stored candles, and publishes a bounded lifecycle status.  It never changes a
production decision, broker authority, or model weight.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import threading
import time
from typing import Any, Callable, Dict, List, Tuple

from core.forward_evidence_clock_service import ForwardEvidenceClockService
from core.selection_outcome_settlement_service import SelectionOutcomeSettlementService
from core.forward_horizon_policy import PRIMARY_HORIZON


SERVICE_VERSION = "forward-evidence-lifecycle-1.1.0-nonblocking-maturity"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ForwardEvidenceLifecycleService:
    """Settle pending selector candidates from local canonical candle authority."""

    def __init__(self, store: Any, *, governance_repository: Any = None, maturity_service: Any = None):
        self.store = store
        self.settlement = SelectionOutcomeSettlementService(store)
        self.governance_repository = governance_repository
        self.maturity_service = maturity_service
        self._run_lock = threading.Lock()
        self.last_status: Dict[str, Any] = {
            "ok": True,
            "version": SERVICE_VERSION,
            "state": "NOT_RUN",
            "last_run_at": None,
            "production_ml_influence": 0.0,
            "broker_authority": "NONE",
        }


    def _schema_state(self) -> Tuple[bool, List[str]]:
        required = {
            "candidate_populations",
            "candidate_population_observations",
            "shadow_selector_predictions",
            "selector_candidate_outcomes",
        }
        try:
            rows = self.store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            present = {str(row[0]) for row in rows}
        except Exception:
            return False, sorted(required)
        missing = sorted(required - present)
        return not missing, missing

    def _pending_symbols(self, limit: int = 80) -> List[Dict[str, Any]]:
        """Return unique symbols with at least one missing governed horizon."""
        try:
            rows = self.store.conn.execute(
                """SELECT o.symbol,o.mode,o.instrument_key,MIN(o.observed_at) AS first_observed_at,
                          COUNT(DISTINCT o.candidate_id) AS candidate_count
                     FROM candidate_population_observations o
                     JOIN shadow_selector_predictions p ON p.candidate_id=o.candidate_id
                    WHERE o.mode IN ('intraday','delivery')
                      AND (
                        SELECT COUNT(DISTINCT px.arm)
                          FROM shadow_selector_predictions px
                         WHERE px.candidate_id=o.candidate_id
                           AND px.arm IN ('heuristic','quant','hybrid')
                      ) = 3
                      AND (
                        (o.mode='intraday' AND (
                          SELECT COUNT(DISTINCT x.horizon)
                            FROM selector_candidate_outcomes x
                           WHERE x.candidate_id=o.candidate_id
                             AND x.horizon IN ('5m','15m','30m','60m','eod')
                        ) < 5)
                        OR
                        (o.mode='delivery' AND (
                          SELECT COUNT(DISTINCT x.horizon)
                            FROM selector_candidate_outcomes x
                           WHERE x.candidate_id=o.candidate_id
                             AND x.horizon IN ('1d','3d','5d','10d','20d')
                        ) < 5)
                      )
                    GROUP BY o.symbol,o.mode,o.instrument_key
                    ORDER BY first_observed_at
                    LIMIT ?""",
                (max(1, int(limit)),),
            ).fetchall()
        except Exception:
            return []
        return [dict(row) for row in rows]

    @staticmethod
    def _proposal_signature(clock: Dict[str, Any]) -> str:
        by_desk_arm = {}
        for desk, arm_map in sorted((clock.get("by_desk_arm") or {}).items()):
            by_desk_arm[desk] = {}
            for arm, row in sorted((arm_map or {}).items()):
                item = dict(row or {})
                by_desk_arm[desk][arm] = {
                    "candidate_count": int(item.get("candidate_count") or 0),
                    "settled_observation_count": int(item.get("settled_observation_count") or 0),
                    "last_prediction_at": item.get("last_prediction_at"),
                    "last_settled_at": item.get("last_settled_at"),
                }
        material = {
            "state": clock.get("state"),
            "started_at_by_desk": clock.get("started_at_by_desk") or {},
            "by_desk_arm": by_desk_arm,
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()

    def cycle_inflight(self) -> bool:
        return self._run_lock.locked()

    def run_once(self, limit: int = 80, progress_fn: Callable[..., None] | None = None) -> Dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {
                "ok": False, "version": SERVICE_VERSION, "state": "CYCLE_IN_FLIGHT",
                "last_run_at": _now(), "pending_symbol_count": 0,
                "processed_symbol_count": 0, "settled_outcome_count": 0,
                "production_ml_influence": 0.0, "broker_authority": "NONE",
            }
        try:
            return self._run_once_body(limit=limit, progress_fn=progress_fn)
        finally:
            self._run_lock.release()

    def _run_once_body(self, limit: int = 80, progress_fn: Callable[..., None] | None = None) -> Dict[str, Any]:
        started = _now()

        def emit(stage: str, *, token: str, completed: int | None = None, total: int | None = None, waiting_on: str | None = None) -> None:
            if progress_fn is None:
                return
            try:
                progress_fn(stage=stage, token=token, completed=completed, total=total, waiting_on=waiting_on)
            except Exception:
                pass

        emit("schema_check", token="schema_check")
        schema_ready, missing_tables = self._schema_state()
        if not schema_ready:
            status = {
                "ok": False,
                "version": SERVICE_VERSION,
                "state": "SCHEMA_NOT_READY",
                "started_at": started,
                "last_run_at": _now(),
                "missing_tables": missing_tables,
                "pending_symbol_count": 0,
                "processed_symbol_count": 0,
                "settled_outcome_count": 0,
                "production_ml_influence": 0.0,
                "production_weight_policy": "GOVERNED_ACTIVE_CAP_15_WITH_DETERMINISTIC_FALLBACK",
                "broker_authority": "NONE",
            }
            self.last_status = status
            return status
        pending = self._pending_symbols(limit=limit)
        emit("pending_population", token=f"pending:{len(pending)}", completed=0, total=len(pending))
        processed = updated = failed = no_candles = 0
        details = []
        for row in pending:
            symbol = str(row.get("symbol") or "").upper().strip()
            mode = str(row.get("mode") or "").lower().strip()
            instrument_key = str(row.get("instrument_key") or "").strip()
            if not symbol or mode not in {"intraday", "delivery"} or not instrument_key:
                continue
            interval = "5minute" if mode == "intraday" else "day"
            limit_rows = 1200 if mode == "intraday" else 120
            try:
                candles = self.store.get_candles(instrument_key, interval, limit=limit_rows)
                if not candles:
                    no_candles += 1
                    details.append({"symbol": symbol, "mode": mode, "state": "NO_LOCAL_CANDLES"})
                    continue
                result = self.settlement.settle_symbol(symbol, mode, candles)
                processed += 1
                updated += int(result.get("updated") or 0)
                details.append({
                    "symbol": symbol,
                    "mode": mode,
                    "state": result.get("state") or "SETTLED",
                    "updated": int(result.get("updated") or 0),
                    "pending": int(result.get("pending") or 0),
                })
            except Exception as exc:
                failed += 1
                details.append({"symbol": symbol, "mode": mode, "state": "FAILED", "error": str(exc)[:240]})
            emit(
                "settling_candidates",
                token=f"settle:{processed}:{updated}:{no_candles}:{failed}:{symbol}",
                completed=processed + no_candles + failed,
                total=len(pending),
            )
        emit("forward_clock", token=f"settlement-pass:{processed}:{updated}:{no_candles}:{failed}", completed=len(pending), total=len(pending))
        clock = ForwardEvidenceClockService(self.store).status()
        proposal_generation: Dict[str, Any]
        proposal_reconciliation: Dict[str, Any]
        emit("governance_proposals", token=f"clock:{self._proposal_signature(clock)}", completed=len(pending), total=len(pending))
        try:
            from core.improvement_proposal_service import ImprovementProposalService
            workflow = ImprovementProposalService(self.store)
            signature = self._proposal_signature(clock)
            getter = getattr(self.store, "get_kv", None)
            setter = getattr(self.store, "set_kv", None)
            previous_signature = getter("forward_evidence_lifecycle:proposal_signature", None) if callable(getter) else None
            generated = []
            if signature != previous_signature:
                for desk, horizon in (("delivery", PRIMARY_HORIZON["delivery"]), ("intraday", PRIMARY_HORIZON["intraday"])):
                    try:
                        proposal = workflow.create(
                            mode=desk, horizon=horizon, actor="forward_evidence_lifecycle"
                        )
                        generated.append({
                            "proposal_id": proposal.get("proposal_id"),
                            "mode": desk,
                            "horizon": horizon,
                            "recommendation": proposal.get("recommendation"),
                            "status": proposal.get("status"),
                            "model_version": proposal.get("model_version"),
                        })
                    except Exception as exc:
                        generated.append({
                            "mode": desk, "horizon": horizon, "status": "CREATE_FAILED",
                            "error": str(exc)[:240],
                        })
                creation_failed = any(row.get("status") == "CREATE_FAILED" for row in generated)
                if not creation_failed and callable(setter):
                    setter("forward_evidence_lifecycle:proposal_signature", signature)
                proposal_generation = {
                    "ok": not creation_failed,
                    "state": (
                        "PROPOSAL_REFRESH_PARTIAL_RETRY_REQUIRED"
                        if creation_failed else "EVIDENCE_CHANGE_PROPOSALS_PERSISTED"
                    ),
                    "signature": signature,
                    "signature_persisted": not creation_failed,
                    "retry_on_next_cycle": creation_failed,
                    "rows": generated,
                    "human_approval_required": True,
                    "production_influence": 0.0,
                    "broker_authority": "NONE",
                }
            else:
                proposal_generation = {
                    "ok": True,
                    "state": "NO_EVIDENCE_CHANGE",
                    "signature": signature,
                    "rows": [],
                    "human_approval_required": True,
                    "production_influence": 0.0,
                    "broker_authority": "NONE",
                }
            proposal_reconciliation = workflow.reconcile()
        except Exception as exc:
            proposal_generation = {
                "ok": False,
                "state": "PROPOSAL_GENERATION_UNAVAILABLE",
                "error": str(exc)[:240],
                "production_influence": 0.0,
                "broker_authority": "NONE",
            }
            proposal_reconciliation = {
                "ok": False,
                "state": "RECONCILE_UNAVAILABLE",
                "error": str(exc)[:240],
                "production_influence": 0.0,
                "broker_authority": "NONE",
            }
        if updated:
            state = "SETTLED"
        elif pending and no_candles:
            state = "PENDING_LOCAL_CANDLES"
        elif pending:
            state = "PENDING_HORIZONS"
        elif clock.get("started_at") or any((clock.get("started_at_by_desk") or {}).values()):
            state = "NO_PENDING_CANDIDATES"
        else:
            state = "WAITING_FOR_SAME_POPULATION_CAPTURE"
        if failed:
            state = "PARTIAL_FAILURE"
        # Settlement is latency-sensitive operational evidence work. Governance
        # draining and walk-forward replay belong to maturity_projection and
        # must never hold this worker open while its heartbeat appears healthy.
        governance_sync: Dict[str, Any] = {
            "ok": True, "state": "DEFERRED_TO_MATURITY_PROJECTION", "fully_drained": False
        }
        maturity_checkpoint: Dict[str, Any] = {"ok": False, "state": "NOT_CONFIGURED"}
        if self.maturity_service is not None:
            try:
                maturity_checkpoint = self.maturity_service.status()
                governance_sync = dict(maturity_checkpoint.get("governance_sync") or governance_sync)
            except Exception as exc:
                maturity_checkpoint = {"ok": False, "state": "STATUS_UNAVAILABLE", "error": str(exc)[:240]}
        status = {
            "ok": failed == 0,
            "version": SERVICE_VERSION,
            "state": state,
            "started_at": started,
            "last_run_at": _now(),
            "pending_symbol_count": len(pending),
            "processed_symbol_count": processed,
            "settled_outcome_count": updated,
            "no_local_candle_count": no_candles,
            "failed_symbol_count": failed,
            "details": details[:40],
            "forward_clock": clock,
            "proposal_generation": proposal_generation,
            "proposal_reconciliation": proposal_reconciliation,
            "governance_sync": governance_sync,
            "level5_maturity_checkpoint": maturity_checkpoint,
            "production_ml_influence": 0.0,
            "production_weight_policy": "GOVERNED_ACTIVE_CAP_15_WITH_DETERMINISTIC_FALLBACK",
            "broker_authority": "NONE",
        }
        self.last_status = status
        setter = getattr(self.store, "set_kv", None)
        if callable(setter):
            try:
                setter("forward_evidence_lifecycle:last", status)
            except Exception:
                pass
        return status

    def status(self) -> Dict[str, Any]:
        getter = getattr(self.store, "get_kv", None)
        if callable(getter):
            try:
                stored = getter("forward_evidence_lifecycle:last", None)
                if isinstance(stored, dict):
                    return stored
            except Exception:
                pass
        return dict(self.last_status)

    def loop(self, supervisor=None, *, running_fn=lambda: True, interval_seconds: int = 300) -> None:
        time.sleep(20)
        while running_fn() and (supervisor is None or supervisor.running):
            if supervisor is not None:
                supervisor.beat("forward_evidence_lifecycle")
            try:
                if supervisor is not None:
                    def _progress(**row):
                        supervisor.progress(
                            "forward_evidence_lifecycle",
                            token=row.get("token"),
                            stage=row.get("stage"),
                            completed_units=row.get("completed"),
                            total_units=row.get("total"),
                            waiting_on=row.get("waiting_on"),
                            expected_idle=False,
                        )
                    with supervisor.heartbeat_guard("forward_evidence_lifecycle"):
                        result = self.run_once(limit=24, progress_fn=_progress)
                else:
                    result = self.run_once(limit=24)
                if supervisor is not None:
                    payload = dict(result or {})
                    clock = dict(payload.get("forward_clock") or {})
                    by_desk = dict(clock.get("by_desk_arm") or {})
                    settled = 0
                    predicted = 0
                    for desk_rows in by_desk.values():
                        for arm_row in dict(desk_rows or {}).values():
                            settled += int((arm_row or {}).get("settled_observation_count") or 0)
                            predicted += int((arm_row or {}).get("prediction_count") or 0)
                    completed = int(payload.get("settled_outcome_count") or settled or 0)
                    total = max(int(predicted or payload.get("pending_symbol_count") or completed), completed, 1)
                    state = str(payload.get("state") or "forward_evidence")
                    expected_wait = state in {
                        "PENDING_HORIZONS", "NO_PENDING_CANDIDATES",
                        "WAITING_FOR_SAME_POPULATION_CAPTURE",
                    }
                    supervisor.progress(
                        "forward_evidence_lifecycle",
                        token=(
                            f"{state}:{payload.get('pending_symbol_count')}:{payload.get('processed_symbol_count')}:"
                            f"{completed}:{payload.get('no_local_candle_count')}:{payload.get('failed_symbol_count')}"
                        ),
                        stage=state, completed_units=completed, total_units=total,
                        waiting_on=(
                            "future governed horizon" if state == "PENDING_HORIZONS"
                            else "next immutable three-arm population" if state == "WAITING_FOR_SAME_POPULATION_CAPTURE"
                            else "next forward-evidence cadence"
                        ),
                        expected_idle=expected_wait,
                    )
            except Exception as exc:
                self.last_status = {
                    "ok": False,
                    "version": SERVICE_VERSION,
                    "state": "LOOP_FAILED",
                    "last_run_at": _now(),
                    "error": str(exc)[:240],
                    "production_ml_influence": 0.0,
                    "broker_authority": "NONE",
                }
            if supervisor is not None:
                supervisor.set_expected_idle("forward_evidence_lifecycle", True, waiting_on=f"next cycle in {max(60, int(interval_seconds))}s")
            time.sleep(max(60, int(interval_seconds)))
