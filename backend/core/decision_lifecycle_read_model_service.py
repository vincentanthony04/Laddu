"""Canonical decision-to-settlement lineage and accuracy eligibility.

Accuracy is derived only from settled, geometry-complete canonical decisions.
Open, rejected, research-only or incomplete rows remain visible as blockers and
can never be silently included in win-rate or performance totals.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, Mapping

from models import now_iso
from core.outcome_accuracy_taxonomy import DEFAULT_OUTCOME_ACCURACY_TAXONOMY


SETTLED = {"SUCCESS", "FAIL", "AMBIGUOUS", "EXPIRED", "INVALIDATED", "CLOSED", "SETTLED", "COMPLETED", "WIN", "LOSS", "TARGET_HIT", "SL_HIT", "STOP_HIT"}
WINS = {"SUCCESS", "WIN", "TARGET_HIT"}
LOSSES = {"FAIL", "LOSS", "SL_HIT", "STOP_HIT"}


class DecisionLifecycleReadModelService:
    VERSION = "canonical-decision-lifecycle-2.4.0-transactional-relational-projection"

    def __init__(self, app: Any):
        self.app = app

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value) if value not in (None, "") else None
        except Exception:
            return None

    @staticmethod
    def _status(row: Mapping[str, Any]) -> str:
        return str(row.get("result") or row.get("outcome") or row.get("status") or "UNKNOWN").upper().strip()

    def _rows(self, mode: str = "all", limit: int = 5000) -> list[Dict[str, Any]]:
        # Production has exactly one lifecycle read authority. An authoritative
        # empty result is valid; an authority failure is not equivalent to empty
        # and must never fall back to the compatibility trade journal.
        store = getattr(self.app, "store", None)
        reader = getattr(store, "lifecycle_decisions", None)
        if not callable(reader):
            reader = getattr(store, "latest_decisions", None)
        if not callable(reader):
            raise RuntimeError("CANONICAL_DECISION_READ_AUTHORITY_UNAVAILABLE")
        rows = reader(mode=mode, limit=limit)
        return [dict(row or {}) for row in (rows or [])]

    def _classify(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        entry = self._number(row.get("entry"))
        target = self._number(row.get("target") if row.get("target") is not None else row.get("t1"))
        stop = self._number(row.get("stop") if row.get("stop") is not None else row.get("sl"))
        exit_price = self._number(row.get("exit") if row.get("exit") is not None else row.get("exit_price"))
        pnl = self._number(row.get("net_pnl") if row.get("net_pnl") is not None else row.get("pnl"))
        raw_costs = row.get("costs") if row.get("costs") is not None else row.get("charges")
        if isinstance(raw_costs, Mapping):
            raw_costs = raw_costs.get("total")
        costs = self._number(raw_costs)
        gross_pnl = self._number(row.get("gross_pnl"))
        quantity = self._number(row.get("quantity"))
        settlement_id = str(row.get("settlement_id") or row.get("position_id") or "").strip()
        status = self._status(row)
        canonical_state = str(row.get("canonical_state") or row.get("state") or "").upper().strip()
        taxonomy = DEFAULT_OUTCOME_ACCURACY_TAXONOMY
        # Prefer explicit canonical signal/economic outcomes. For older but
        # geometry-complete Model Paper mirrors, result/status is a one-way
        # compatibility source for signal semantics and net P&L may recover the
        # economic class. Economic sign is never used to infer signal quality.
        signal_outcome = taxonomy.normalize_signal(row.get("signal_outcome"))
        if signal_outcome is None:
            signal_outcome = taxonomy.normalize_signal(status)
        economic_outcome = taxonomy.normalize_economic(row.get("economic_outcome"))
        if economic_outcome is None and pnl is not None:
            economic_outcome = taxonomy.economic_from_pnl(pnl)
        missing_geometry = [name for name, value in (("entry", entry), ("target", target), ("stop", stop)) if value is None]
        settled = (
            (status in SETTLED or canonical_state in {"COMPLETED", "INVALIDATED"} or bool(settlement_id and economic_outcome))
            and status not in {"OPEN", "ACTIVE", "MONITORING"}
        )
        missing_outcome = []
        if settled:
            if exit_price is None:
                missing_outcome.append("exit")
            if pnl is None:
                missing_outcome.append("net_pnl")
            if signal_outcome is None:
                missing_outcome.append("signal_outcome")
            if economic_outcome is None:
                missing_outcome.append("economic_outcome")
        geometry_complete = not missing_geometry
        outcome_complete = settled and not missing_outcome
        # Signal accuracy requires a decisive SUCCESS/FAILURE signal outcome.
        # A settled NEUTRAL row remains economically measurable but is excluded
        # from the accuracy denominator. Legacy points-only closures remain
        # ineligible without the immutable Model Paper settlement identity.
        accuracy_eligible = bool(
            geometry_complete and outcome_complete and settlement_id
            and taxonomy.accuracy_eligible(signal_outcome)
        )
        performance_eligible = bool(
            geometry_complete and outcome_complete and settlement_id
            and taxonomy.performance_eligible(signal_outcome, economic_outcome)
            and quantity is not None and quantity > 0
            and gross_pnl is not None and costs is not None and pnl is not None
        )
        decision_id = str(row.get("decision_id") or row.get("signal_id") or row.get("id") or "").strip()
        return {
            **dict(row),
            "decision_id": decision_id or None,
            "entry": entry,
            "target": target,
            "stop": stop,
            "exit": exit_price,
            "net_pnl": pnl,
            "gross_pnl": gross_pnl,
            "costs": costs,
            "quantity": quantity,
            "settlement_id": settlement_id or None,
            "signal_outcome": signal_outcome,
            "economic_outcome": economic_outcome,
            "outcome_taxonomy_authority": taxonomy.authority,
            "outcome_taxonomy_version": taxonomy.authority_version,
            "lifecycle_status": status,
            "canonical_state": canonical_state or None,
            "geometry_complete": geometry_complete,
            "settled": settled,
            "outcome_complete": outcome_complete,
            "accuracy_eligible": accuracy_eligible,
            "performance_eligible": performance_eligible,
            "missing_fields": missing_geometry + missing_outcome,
        }

    @staticmethod
    def _metrics(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
        rows = list(rows)
        eligible = [row for row in rows if row.get("accuracy_eligible")]
        performance = [row for row in rows if row.get("performance_eligible")]
        wins = [row for row in eligible if row.get("signal_outcome") == "SUCCESS"]
        losses = [row for row in eligible if row.get("signal_outcome") == "FAILURE"]
        neutral = [row for row in rows if row.get("settled") and row.get("signal_outcome") == "NEUTRAL"]
        unscorable = [row for row in rows if row.get("signal_outcome") == "UNSCORABLE"]
        decisive = len(wins) + len(losses)
        pnls = [float(row["net_pnl"]) for row in performance if row.get("net_pnl") is not None]
        costs = [float(row["costs"]) for row in performance if row.get("costs") is not None]
        return {
            "records": len(rows),
            "geometry_complete": sum(bool(row.get("geometry_complete")) for row in rows),
            "open": sum(not bool(row.get("settled")) for row in rows),
            "settled": sum(bool(row.get("settled")) for row in rows),
            "outcome_complete": sum(bool(row.get("outcome_complete")) for row in rows),
            "accuracy_eligible": len(eligible),
            "performance_eligible": len(performance),
            "wins": len(wins),
            "losses": len(losses),
            "neutral": len(neutral),
            "unscorable": len(unscorable),
            "accuracy_denominator": decisive,
            "accuracy_pct": round(len(wins) * 100.0 / decisive, 2) if decisive else None,
            "neutral_excluded_from_accuracy": True,
            "net_pnl": round(sum(pnls), 2) if pnls else None,
            "costs": round(sum(costs), 2) if costs else None,
            "excluded_incomplete": len(rows) - len(eligible),
        }

    def status(self, *, mode: str = "all", limit: int = 5000) -> Dict[str, Any]:
        try:
            source_rows = self._rows(mode=mode, limit=limit)
        except Exception as exc:
            return {
                "ok": False,
                "version": self.VERSION,
                "state": "UNAVAILABLE",
                "authority": "POSTGRESQL_CANONICAL_DECISIONS",
                "data_available": False,
                "fallback_used": False,
                "error": str(exc)[:300],
                "overall": None,
                "by_desk": {},
                "records": [],
                "evaluated_at": now_iso(),
                "policy": "canonical lifecycle authority failure is explicit; compatibility journals and empty-list substitution are forbidden",
            }
        classified = [self._classify(row) for row in source_rows]
        by_desk: Dict[str, Any] = {}
        for desk in ("delivery", "intraday"):
            desk_rows = [row for row in classified if str(row.get("mode") or "").lower() == desk]
            blockers = Counter(field for row in desk_rows for field in row.get("missing_fields") or [])
            by_desk[desk] = {
                **self._metrics(desk_rows),
                "blockers": [{"field": field, "count": count} for field, count in blockers.most_common()],
                "sample_incomplete": [
                    {"decision_id": row.get("decision_id"), "symbol": row.get("symbol"), "status": row.get("lifecycle_status"), "missing_fields": row.get("missing_fields")}
                    for row in desk_rows if not row.get("accuracy_eligible")
                ][:50],
            }
        overall = self._metrics(classified)
        state = "SETTLEMENT_EVIDENCE_READY" if overall["accuracy_eligible"] else "NO_COMPLETE_SETTLED_LIFECYCLE"
        return {
            "ok": True,
            "version": self.VERSION,
            "state": state,
            "authority": "POSTGRESQL_CANONICAL_DECISIONS",
            "data_available": True,
            "fallback_used": False,
            "outcome_taxonomy_authority": DEFAULT_OUTCOME_ACCURACY_TAXONOMY.authority,
            "outcome_taxonomy_version": DEFAULT_OUTCOME_ACCURACY_TAXONOMY.authority_version,
            "accuracy_policy": "SUCCESS+FAILURE only; NEUTRAL and UNSCORABLE excluded from accuracy denominator; Model Paper settlement lineage plus complete geometry (entry+target+stop+exit) required",
            "performance_policy": "settled SUCCESS/FAILURE/NEUTRAL with position/settlement_id+quantity+gross P&L+costs+net P&L; UNSCORABLE and points-only signal movement excluded",
            "overall": overall,
            "by_desk": by_desk,
            "records": classified[:500],
            "evaluated_at": now_iso(),
        }
