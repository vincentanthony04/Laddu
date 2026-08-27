"""Frozen ChartInk-style daily range-compression research rule.

The rule is intentionally literal and immutable:

    range_0 < range_1 AND ... AND range_0 < range_6

where ``range_i = high_i - low_i`` on completed daily NSE bars and ``range_0``
is the latest completed session.  It is a Delivery research hypothesis only.
It cannot mutate the canonical Final book, production rank, risk thresholds or
broker state.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from core.india_time import INDIA_TZ


RULE_ID = "RC_RANGE_COMPRESS_1TO6_v1"
RULE_VERSION = "range-compression-rule-1.0.0"
RULE_MODE = "delivery"
PRIMARY_TOP_FRACTION = 0.01
SECONDARY_TOP_FRACTION = 0.05


def _number(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any, length: int = 64) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8", errors="replace")).hexdigest()[:length]


def _timestamp(row: Mapping[str, Any]) -> str:
    return str(row.get("timestamp") or row.get("ts") or row.get("date") or "").strip()


def _session_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.date().isoformat()
    except (TypeError, ValueError):
        return text[:10] if len(text) >= 10 else ""


def _is_forming(row: Mapping[str, Any]) -> bool:
    return bool(
        row.get("forming") is True
        or row.get("is_forming") is True
        or row.get("completed") is False
        or row.get("is_complete") is False
    )


class RangeCompressionRuleService:
    """Evaluate and rank the frozen Delivery range-compression hypothesis."""

    @staticmethod
    def rule_card() -> Dict[str, Any]:
        specification = {
            "rule_id": RULE_ID,
            "version": RULE_VERSION,
            "desk": RULE_MODE,
            "timeframe": "1D completed sessions",
            "direction": "LONG_PAPER_STRATEGY",
            "predicate": "range_0 < range_1 AND range_0 < range_2 AND range_0 < range_3 AND range_0 < range_4 AND range_0 < range_5 AND range_0 < range_6",
            "range_definition": "range_i = high_i - low_i; i=0 is latest completed daily session",
            "minimum_history": 7,
            "forming_bar_policy": "EXCLUDED",
            "primary_cohort": "top 1% of qualified rows by compression strength",
            "secondary_cohort": "top 5% benchmark for automatic paper selection",
            "compression_strength": "100 * max(0, 1 - range_0 / min(range_1..range_6))",
            "entry": "next verified evaluation quote after observation",
            "stop": "latest completed session low",
            "target": "entry + 2 * (entry - stop)",
            "horizon": "20d or target/stop first",
            "costs": "canonical India cash costs plus +5/+10/+20 bps stress",
            "prediction_state": "QUANT_EVALUATION_PAPER",
            "authority": "QUANT_EVALUATION_PAPER",
            "decision_weight": 0.0,
            "paper_weight": 0.0,
            "production_weight": 0.0,
            "broker_order_authority": "NONE",
            "statistical_state": "NOT_YET_STATISTICALLY_VALIDATED",
        }
        return {
            **specification,
            "immutable_spec_hash": _hash(specification),
        }

    @classmethod
    def evaluate(
        cls,
        candles: Iterable[Mapping[str, Any]],
        *,
        as_of: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        current = as_of or datetime.now(INDIA_TZ)
        current = current.astimezone(INDIA_TZ) if current.tzinfo else current.replace(tzinfo=INDIA_TZ)
        by_session: Dict[str, Dict[str, Any]] = {}
        rejected = 0
        for raw in candles or []:
            row = dict(raw or {})
            if _is_forming(row):
                continue
            high = _number(row.get("high"))
            low = _number(row.get("low"))
            close = _number(row.get("close"))
            stamp = _timestamp(row)
            session = _session_date(stamp)
            if session == current.date().isoformat():
                from core.completeness_freshness_authority import DEFAULT_COMPLETENESS_FRESHNESS_AUTHORITY
                completion = DEFAULT_COMPLETENESS_FRESHNESS_AUTHORITY.completed_period(
                    "day", {"timestamp": stamp}, at=current
                )
                if completion.get("decision_usable") is not True:
                    continue
            if not session or high is None or low is None or close is None or high < low or close <= 0:
                rejected += 1
                continue
            candidate = {
                "session": session,
                "timestamp": stamp,
                "high": high,
                "low": low,
                "close": close,
                "range": high - low,
                "range_pct": (high - low) / close * 100.0,
            }
            previous = by_session.get(session)
            if previous is None or str(candidate["timestamp"]) >= str(previous["timestamp"]):
                by_session[session] = candidate
        rows = sorted(by_session.values(), key=lambda item: (item["session"], item["timestamp"]))
        if len(rows) < 7:
            return {
                "ok": True,
                "state": "INSUFFICIENT_COMPLETED_DAILY_BARS",
                "qualified": False,
                "rule_id": RULE_ID,
                "rule_version": RULE_VERSION,
                "completed_sessions": len(rows),
                "required_sessions": 7,
                "rejected_rows": rejected,
                "prediction_state": "MODEL_UNAVAILABLE",
                "authority": "MODEL_UNAVAILABLE",
                "decision_weight": 0.0,
            }
        selected = rows[-7:]
        latest = selected[-1]
        prior = list(reversed(selected[:-1]))
        range_0 = float(latest["range"])
        prior_ranges = [float(item["range"]) for item in prior]
        strict_comparisons = [range_0 < value for value in prior_ranges]
        qualified = bool(range_0 > 0 and all(value > 0 for value in prior_ranges) and all(strict_comparisons))
        minimum_prior = min(prior_ranges) if prior_ranges else None
        ratio_to_min = range_0 / minimum_prior if minimum_prior and minimum_prior > 0 else None
        strength = max(0.0, min(100.0, (1.0 - ratio_to_min) * 100.0)) if ratio_to_min is not None else 0.0
        evidence = {
            "range_0": round(range_0, 8),
            "range_1_to_6": [round(value, 8) for value in prior_ranges],
            "range_pct_0": round(float(latest["range_pct"]), 8),
            "range_pct_1_to_6": [round(float(item["range_pct"]), 8) for item in prior],
            "comparisons": strict_comparisons,
            "ratio_to_min_prior": round(ratio_to_min, 10) if ratio_to_min is not None else None,
            "compression_strength": round(strength, 6),
            "latest_session": latest["session"],
            "latest_close": round(float(latest["close"]), 8),
            "latest_low": round(float(latest["low"]), 8),
            "latest_high": round(float(latest["high"]), 8),
            "session_dates": [latest["session"]] + [item["session"] for item in prior],
        }
        lineage = {
            "rule_id": RULE_ID,
            "rule_version": RULE_VERSION,
            "bar_count": 7,
            "forming_bars_excluded": True,
            "evidence_hash": _hash(evidence),
            "immutable_spec_hash": cls.rule_card()["immutable_spec_hash"],
        }
        return {
            "ok": True,
            "state": "QUALIFIED" if qualified else "RULE_NOT_TRIGGERED",
            "qualified": qualified,
            "rule_id": RULE_ID,
            "rule_version": RULE_VERSION,
            "score": round(strength, 6),
            "evidence": evidence,
            "lineage": lineage,
            "completed_sessions": len(rows),
            "rejected_rows": rejected,
            "prediction_state": "QUANT_EVALUATION_PAPER",
            "authority": "QUANT_EVALUATION_PAPER",
            "decision_weight": 0.0,
            "paper_weight": 0.0,
            "production_weight": 0.0,
            "broker_order_authority": "NONE",
        }

    @staticmethod
    def _attached(row: Mapping[str, Any]) -> Dict[str, Any]:
        raw = row.get("range_compression_rule")
        return dict(raw) if isinstance(raw, Mapping) else {}

    @classmethod
    def rank_population(cls, rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        population = [dict(row or {}) for row in rows]
        qualified = []
        for row in population:
            rule = cls._attached(row)
            if str(row.get("mode") or "").lower() != RULE_MODE:
                continue
            if rule.get("qualified") is not True or str(rule.get("rule_id") or "") != RULE_ID:
                continue
            score = _number(rule.get("score"))
            evidence = rule.get("evidence") if isinstance(rule.get("evidence"), Mapping) else {}
            if score is None or not evidence.get("latest_session"):
                continue
            qualified.append({"row": row, "rule": rule, "score": score})
        qualified.sort(key=lambda item: (-item["score"], str(item["row"].get("symbol") or "")))
        universe_size = len(population)
        qualified_count = len(qualified)
        primary_count = max(1, math.ceil(universe_size * PRIMARY_TOP_FRACTION)) if qualified_count else 0
        secondary_count = max(1, math.ceil(universe_size * SECONDARY_TOP_FRACTION)) if qualified_count else 0
        predictions = []
        for rank, item in enumerate(qualified, start=1):
            percentile = 100.0 if qualified_count == 1 else (qualified_count - rank) * 100.0 / (qualified_count - 1)
            cohort = "TOP_1_PERCENT" if rank <= primary_count else "TOP_5_PERCENT" if rank <= secondary_count else "QUALIFIED_OUTSIDE_TOP_5"
            row = item["row"]
            rule = item["rule"]
            evidence = dict(rule.get("evidence") or {})
            predictions.append({
                "candidate_id": row.get("candidate_id"),
                "symbol": str(row.get("symbol") or "").upper(),
                "mode": RULE_MODE,
                "arm": "range_compression",
                "score": round(item["score"], 6),
                "rank": rank,
                "percentile": round(percentile, 6),
                "feature_coverage": 1.0,
                "setup_family": "RC_RANGE_COMPRESS_1TO6",
                "rank_tier": "HIGH" if cohort == "TOP_1_PERCENT" else "MIDDLE" if cohort == "TOP_5_PERCENT" else "LOW",
                "cohort": cohort,
                "qualified": True,
                "rule_id": RULE_ID,
                "rule_version": RULE_VERSION,
                "range_compression_rule": rule,
                "latest_session": evidence.get("latest_session"),
                "latest_low": evidence.get("latest_low"),
                "latest_close": evidence.get("latest_close"),
                "compression_ratio": evidence.get("ratio_to_min_prior"),
                "feature_contributions": {
                    "frozen_range_predicate": {
                        "value": True,
                        "compression_strength": round(item["score"], 6),
                        "cohort": cohort,
                        "state": "available",
                    }
                },
                "meta_label": None,
                "probability_positive": None,
                "expected_net_return": None,
                "calibration_state": "FORWARD_EVALUATION_REQUIRED",
                "prediction_state": "QUANT_EVALUATION_PAPER",
                "authority": "QUANT_EVALUATION_PAPER",
                "decision_weight": 0.0,
                "broker_execution_weight": 0.0,
                "eligible_for_paper_decision": True,
                "production_status": row.get("production_status") or row.get("status"),
                "production_decision": row.get("production_decision") or row.get("decision"),
                "model_version": RULE_VERSION,
            })
        return {
            "ok": True,
            "state": "QUALIFIED_ROWS" if predictions else "NO_QUALIFIED_ROWS",
            "rule": cls.rule_card(),
            "universe_size": universe_size,
            "qualified_count": qualified_count,
            "primary_top_1_count": min(primary_count, qualified_count),
            "secondary_top_5_count": min(secondary_count, qualified_count),
            "predictions": predictions,
            "top_1_percent": predictions[:primary_count],
            "top_5_percent": predictions[:secondary_count],
            "prediction_state": "QUANT_EVALUATION_PAPER",
            "authority": "QUANT_EVALUATION_PAPER",
            "decision_weight": 0.0,
            "production_weight": 0.0,
            "broker_order_authority": "NONE",
            "statistical_state": "NOT_YET_STATISTICALLY_VALIDATED",
        }
