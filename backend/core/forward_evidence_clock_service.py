"""Read-only forward-evidence clock for the governed selector experiment.

The clock starts only when an immutable candidate population has complete
Mathematical Baseline (heuristic), ML Challenger (quant), and Hybrid
predictions for every candidate in that population.  It never treats process
health, a backtest, or a renamed package as forward evidence.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping


SERVICE_VERSION = "forward-evidence-clock-1.0.0"
REQUIRED_ARMS = ("heuristic", "quant", "hybrid")


def _dict_rows(cursor: Any, rows: Iterable[Any]) -> List[Dict[str, Any]]:
    columns = [str(item[0]) for item in (cursor.description or [])]
    output = []
    for row in rows:
        try:
            output.append(dict(row))
        except (TypeError, ValueError):
            output.append({name: row[index] for index, name in enumerate(columns)})
    return output


class ForwardEvidenceClockService:
    """Expose truthful forward observation counts without mutating models."""

    def __init__(self, store: Any):
        self.store = store

    def _rows(self, sql: str) -> List[Dict[str, Any]]:
        cursor = self.store.conn.execute(sql)
        return _dict_rows(cursor, cursor.fetchall())

    def status(self) -> Dict[str, Any]:
        required_tables = (
            "candidate_populations",
            "shadow_selector_predictions",
            "selector_candidate_outcomes",
        )
        missing = []
        for table in required_tables:
            try:
                self.store.conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            except Exception:
                missing.append(table)
        if missing:
            return {
                "ok": True,
                "version": SERVICE_VERSION,
                "state": "NOT_STARTED",
                "started_at": None,
                "first_settled_at": None,
                "missing_tables": missing,
                "reason": "forward evidence ledgers have not been initialized",
                "required_arms": list(REQUIRED_ARMS),
                "production_ml_influence": 0.0,
                "broker_authority": "NONE",
            }

        complete_populations = self._rows(
            """SELECT p.population_fingerprint,p.mode,MIN(pop.observed_at) AS observed_at,
                      COUNT(DISTINCT p.candidate_id) AS candidate_count,
                      MAX(pop.candidate_count) AS expected_candidate_count,
                      COUNT(*) AS arm_prediction_count
                 FROM shadow_selector_predictions p
                 JOIN candidate_populations pop
                   ON pop.population_fingerprint=p.population_fingerprint
                WHERE p.arm IN ('heuristic','quant','hybrid')
                GROUP BY p.population_fingerprint,p.mode
               HAVING COUNT(*)=COUNT(DISTINCT p.candidate_id)*3
                  AND COUNT(DISTINCT p.arm)=3
                  AND COUNT(DISTINCT p.candidate_id)=MAX(pop.candidate_count)
                ORDER BY observed_at"""
        )
        complete_ids = {str(row["population_fingerprint"]) for row in complete_populations}
        started_at_by_desk = {}
        for desk in ("delivery", "intraday"):
            candidates = [
                str(row.get("observed_at"))
                for row in complete_populations
                if str(row.get("mode")) == desk and row.get("observed_at")
            ]
            started_at_by_desk[desk] = min(candidates) if candidates else None

        desk_rows = self._rows(
            """SELECT mode,COUNT(*) AS population_count,
                      SUM(candidate_count) AS candidate_count,
                      MIN(observed_at) AS first_population_at,
                      MAX(observed_at) AS latest_population_at
                 FROM candidate_populations
                GROUP BY mode ORDER BY mode"""
        )
        arm_rows = self._rows(
            """SELECT p.mode,p.arm,
                      COUNT(*) AS prediction_count,
                      COUNT(DISTINCT p.population_fingerprint) AS population_count,
                      COUNT(DISTINCT o.candidate_id || '|' || o.horizon) AS settled_observation_count,
                      MIN(p.created_at) AS first_prediction_at,
                      MAX(p.created_at) AS latest_prediction_at,
                      MIN(o.settled_at) AS first_settled_at,
                      MAX(o.settled_at) AS latest_settled_at,
                      SUM(CASE WHEN o.result IN ('WIN','TARGET','SUCCESS') THEN 1 ELSE 0 END) AS success_count,
                      SUM(CASE WHEN o.result IN ('LOSS','STOP','FAIL','STOP_FIRST') THEN 1 ELSE 0 END) AS failure_count,
                      AVG(o.net_return_bps) AS mean_net_return_bps,
                      AVG(o.actual_cost_bps) AS mean_actual_cost_bps
                 FROM shadow_selector_predictions p
                 LEFT JOIN selector_candidate_outcomes o
                   ON o.candidate_id=p.candidate_id
                  AND o.population_fingerprint=p.population_fingerprint
                WHERE p.arm IN ('heuristic','quant','hybrid')
                GROUP BY p.mode,p.arm
                ORDER BY p.mode,p.arm"""
        )

        # SQLite and PostgreSQL differ in string concatenation corner cases;
        # recompute the settled total from returned integer counts only.
        settled_total = sum(int(row.get("settled_observation_count") or 0) for row in arm_rows)
        first_settled_candidates = [
            str(row.get("first_settled_at"))
            for row in arm_rows if row.get("first_settled_at")
        ]
        first_settled_at_any_desk = min(first_settled_candidates) if first_settled_candidates else None
        first_settled_at_by_desk = {}
        for desk in ("delivery", "intraday"):
            candidates = [
                str(row.get("first_settled_at"))
                for row in arm_rows
                if str(row.get("mode")) == desk and row.get("first_settled_at")
            ]
            first_settled_at_by_desk[desk] = min(candidates) if candidates else None

        by_desk_arm: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for raw in arm_rows:
            row = dict(raw)
            mode = str(row.pop("mode"))
            arm = str(row.pop("arm"))
            row["complete_population_prediction_count"] = sum(
                int(item.get("candidate_count") or 0)
                for item in complete_populations
                if str(item.get("mode")) == mode
            )
            row["production_influence"] = 0.0
            row["authority"] = "ISOLATED_MODEL_PAPER_EVIDENCE"
            by_desk_arm.setdefault(mode, {})[arm] = row

        complete_by_desk = {
            mode: sum(1 for row in complete_populations if str(row.get("mode")) == mode)
            for mode in ("delivery", "intraday")
        }
        all_desks_started = all(complete_by_desk.get(mode, 0) > 0 for mode in ("delivery", "intraday"))
        started_desks = [desk for desk, value in started_at_by_desk.items() if value]
        # The product-level clock starts only when both active desks have a
        # complete same-population three-arm set.  Each desk keeps its own
        # start timestamp so one desk can be diagnosed without falsely
        # claiming the complete Delivery+Intraday experiment has started.
        started_at = (
            max(str(value) for value in started_at_by_desk.values() if value)
            if all_desks_started else None
        )
        first_settled_at = first_settled_at_any_desk if all_desks_started else None
        if not started_desks:
            state = "NOT_STARTED"
            reason = "no complete same-population Baseline/Challenger/Hybrid prediction set"
        elif not all_desks_started:
            state = "PARTIAL_START"
            missing_desks = [desk for desk in ("delivery", "intraday") if desk not in started_desks]
            reason = "forward evidence is active for " + ", ".join(started_desks) + "; waiting for " + ", ".join(missing_desks)
        elif settled_total == 0:
            state = "COLLECTING_UNSETTLED"
            reason = "both desks have point-in-time predictions; waiting for independently settled outcomes"
        else:
            state = "COLLECTING_SETTLED"
            reason = "both desks are accumulating forward outcomes; alpha is not yet validated"

        return {
            "ok": True,
            "version": SERVICE_VERSION,
            "state": state,
            "reason": reason,
            "started_at": started_at,
            "started_at_by_desk": started_at_by_desk,
            "first_settled_at": first_settled_at,
            "first_settled_at_by_desk": first_settled_at_by_desk,
            "required_arms": list(REQUIRED_ARMS),
            "complete_population_count": len(complete_ids),
            "complete_population_count_by_desk": complete_by_desk,
            "both_desks_started": all_desks_started,
            "settled_observation_count_across_arms": settled_total,
            "populations": desk_rows,
            "by_desk_arm": by_desk_arm,
            "integrity": {
                "same_population_required": True,
                "all_three_arms_required": True,
                "point_in_time_predictions_required": True,
                "future_outcome_required_for_settlement": True,
                "costs_reported_separately": True,
                "backtest_is_not_forward_evidence": True,
            },
            "production_ml_influence": 0.0,
            "production_weight_policy": "GOVERNED_ACTIVE_CAP_15_WITH_DETERMINISTIC_FALLBACK",
            "broker_authority": "NONE",
            "alpha_claim": "NOT_VALIDATED",
        }
