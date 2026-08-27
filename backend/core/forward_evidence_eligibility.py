"""Eligibility filter for Level-5 prospective selector evidence.

Old/migrated evidence is never deleted.  Rows that cannot prove a genuine
point-in-time three-arm prediction followed by a later outcome are excluded
from forward-maturity statistics and reported as ineligible diagnostics.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping

SERVICE_VERSION = "forward-evidence-eligibility-1.0.0"
REQUIRED_ARMS = ("heuristic", "quant", "hybrid")


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def classify_forward_evidence(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return eligible rows plus explicit exclusion evidence.

    Eligibility is candidate/population scoped.  A candidate is eligible only
    when all three governed arms exist exactly once, every timestamp is
    parseable, the population identity is consistent, each prediction is made
    no earlier than the candidate observation and strictly before settlement,
    and settlement is strictly after the candidate observation.
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    raw_rows = [dict(row) for row in rows]
    for row in raw_rows:
        key = (str(row.get("population_fingerprint") or ""), str(row.get("candidate_id") or ""))
        grouped.setdefault(key, []).append(row)

    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}

    for (population, candidate_id), items in grouped.items():
        reasons: set[str] = set()
        arms = [str(item.get("arm") or "").lower() for item in items]
        if sorted(arms) != sorted(REQUIRED_ARMS):
            reasons.add("INCOMPLETE_OR_DUPLICATE_THREE_ARM_SET")
        if not population or not candidate_id:
            reasons.add("MISSING_POPULATION_OR_CANDIDATE_IDENTITY")

        for item in items:
            observed = _parse(item.get("observed_at"))
            prediction = _parse(item.get("prediction_at") or item.get("created_at"))
            settled = _parse(item.get("settled_at"))
            outcome_population = str(item.get("outcome_population_fingerprint") or population)
            if observed is None:
                reasons.add("OBSERVED_AT_INVALID")
            if prediction is None:
                reasons.add("PREDICTION_AT_INVALID")
            if settled is None:
                reasons.add("SETTLED_AT_INVALID")
            if outcome_population != population:
                reasons.add("POPULATION_FINGERPRINT_MISMATCH")
            if observed is not None and settled is not None and settled <= observed:
                reasons.add("OUTCOME_NOT_STRICTLY_FUTURE")
            if observed is not None and prediction is not None and prediction < observed:
                reasons.add("PREDICTION_PRECEDES_CANDIDATE_OBSERVATION")
            if prediction is not None and settled is not None and prediction >= settled:
                reasons.add("PREDICTION_NOT_BEFORE_SETTLEMENT")

        if reasons:
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            excluded.append({
                "population_fingerprint": population,
                "candidate_id": candidate_id,
                "reasons": sorted(reasons),
            })
        else:
            eligible.extend(items)

    eligible_candidates = len({str(row.get("candidate_id") or "") for row in eligible})
    eligible_populations = len({str(row.get("population_fingerprint") or "") for row in eligible})
    return {
        "version": SERVICE_VERSION,
        "rows": eligible,
        "raw_row_count": len(raw_rows),
        "eligible_row_count": len(eligible),
        "eligible_candidate_count": eligible_candidates,
        "eligible_population_count": eligible_populations,
        "excluded_candidate_count": len(excluded),
        "exclusion_reason_counts": dict(sorted(reason_counts.items())),
        "excluded_candidates": excluded[:50],
        "policy": "IMMUTABLE_OLD_ROWS_RETAINED_BUT_ONLY_GENUINE_THREE_ARM_PROSPECTIVE_COHORTS_COUNT_TOWARD_FORWARD_MATURITY",
    }
