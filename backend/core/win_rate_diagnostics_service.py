"""Auditable win-rate diagnostics for the two supported Project Laddu desks.

This service diagnoses observed outcomes; it never changes live thresholds or
model weights.  Win rate is reported beside expectancy and profit factor so a
high hit rate cannot conceal poor payoff asymmetry.
"""
from __future__ import annotations

from core.production_mode_policy import require_production_mode

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _features(row: Mapping[str, Any]) -> Dict[str, Any]:
    raw = row.get("feature_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
        return dict(value) if isinstance(value, Mapping) else {}
    except Exception:
        return {}


def _desk(value: Any) -> str | None:
    raw = str(value or "").lower().strip()
    try:
        return require_production_mode(raw)
    except ValueError:
        return ""
    return None


def _band(value: Any, cuts: tuple[float, ...], labels: tuple[str, ...], unknown: str = "unknown") -> str:
    number = _num(value)
    if number is None:
        return unknown
    for cut, label in zip(cuts, labels):
        if number < cut:
            return label
    return labels[-1]


@dataclass(frozen=True)
class WinRateDiagnosticsService:
    minimum_sample: int = 30

    @staticmethod
    def _metrics(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        pnls = [float(item["pnl"]) for item in items]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        flats = [p for p in pnls if p == 0]
        decisive = len(wins) + len(losses)
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        average_win = gross_win / len(wins) if wins else None
        average_loss = abs(sum(losses) / len(losses)) if losses else None
        expectancy = sum(pnls) / len(pnls) if pnls else None
        profit_factor = gross_win / gross_loss if gross_loss else (999.0 if gross_win else 0.0)
        payoff = average_win / average_loss if average_win is not None and average_loss not in (None, 0) else None
        ci_low = ci_high = None
        if decisive:
            z = 1.959963984540054
            p_hat = len(wins) / decisive
            denom = 1.0 + (z * z / decisive)
            centre = (p_hat + (z * z / (2.0 * decisive))) / denom
            margin = z * math.sqrt((p_hat * (1.0 - p_hat) / decisive) + (z * z / (4.0 * decisive * decisive))) / denom
            ci_low = max(0.0, centre - margin) * 100.0
            ci_high = min(1.0, centre + margin) * 100.0
        absolute_pnls = sorted((abs(p) for p in pnls if p != 0), reverse=True)
        total_abs = sum(absolute_pnls)
        top_outcome_share = (absolute_pnls[0] * 100.0 / total_abs) if absolute_pnls and total_abs else None
        freshness_known = sum(1 for item in items if str(item.get("freshness") or "unknown").lower() not in {"", "unknown", "none"})
        return {
            "samples": len(items),
            "wins": len(wins),
            "losses": len(losses),
            "flat_or_non_decisive": len(flats),
            "decisive_win_rate": round(len(wins) * 100.0 / decisive, 2) if decisive else None,
            "decisive_win_rate_ci95": ([round(ci_low, 2), round(ci_high, 2)] if ci_low is not None else None),
            "settled_success_rate": round(len(wins) * 100.0 / len(items), 2) if items else None,
            "expectancy_points": round(expectancy, 4) if expectancy is not None else None,
            "profit_factor": round(profit_factor, 4),
            "average_win_points": round(average_win, 4) if average_win is not None else None,
            "average_loss_points": round(average_loss, 4) if average_loss is not None else None,
            "payoff_ratio": round(payoff, 4) if payoff is not None else None,
            "top_outcome_abs_pnl_share_pct": round(top_outcome_share, 2) if top_outcome_share is not None else None,
            "freshness_coverage_pct": round(freshness_known * 100.0 / len(items), 2) if items else None,
            "confidence": "high" if decisive >= 100 else "medium" if decisive >= 30 else "low",
        }

    def analyze(self, rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
        observations: List[Dict[str, Any]] = []
        for raw in rows or ():
            desk = _desk(raw.get("mode"))
            if desk not in {"intraday", "delivery"}:
                continue
            pnl = _num(raw.get("pnl_points"))
            if pnl is None:
                continue
            features = _features(raw)
            freshness = str(
                features.get("candle_freshness_state")
                or features.get("price_freshness_state")
                or features.get("freshness_state")
                or "unknown"
            ).lower()
            observations.append({
                "mode": desk,
                "side": str(raw.get("side") or "unknown").lower(),
                "pnl": pnl,
                "attribution": str(raw.get("attribution") or "unknown"),
                "model_version": str(raw.get("model_version") or features.get("ranking_version") or "unknown"),
                "score_band": _band(features.get("score"), (60, 70, 80), ("<60", "60-69", "70-79", "80+")),
                "rr_band": _band(features.get("rr"), (1.5, 2.0, 3.0), ("<1.5", "1.5-1.99", "2.0-2.99", "3.0+")),
                "freshness": freshness,
                "regime": str(features.get("regime") or features.get("market_structure") or "unknown").lower(),
            })

        overall = self._metrics(observations)
        segments: Dict[str, List[Dict[str, Any]]] = {}
        for dimension in ("mode", "side", "score_band", "rr_band", "freshness", "regime", "model_version"):
            grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for item in observations:
                grouped[str(item.get(dimension) or "unknown")].append(item)
            segments[dimension] = [
                {dimension: key, **self._metrics(items)}
                for key, items in sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0]))
            ]

        attributions = Counter(item["attribution"] for item in observations)
        root_causes: List[Dict[str, Any]] = []
        sample_count = int(overall["samples"] or 0)
        if sample_count < self.minimum_sample:
            root_causes.append({
                "severity": "high", "code": "sample_insufficient",
                "message": f"Only {sample_count} settled outcomes; at least {self.minimum_sample} are required before changing production logic.",
            })
        if sample_count < self.minimum_sample and (overall.get("top_outcome_abs_pnl_share_pct") or 0) >= 50:
            root_causes.append({
                "severity": "high", "code": "pnl_concentration_unstable",
                "message": "At least half of absolute P&L comes from one outcome; expectancy and profit factor are not yet stable.",
            })
        if overall["expectancy_points"] is not None and overall["expectancy_points"] <= 0:
            root_causes.append({
                "severity": "critical", "code": "negative_expectancy",
                "message": "Average settled P&L is non-positive after the recorded outcome calculation.",
            })
        if overall["profit_factor"] < 1.0 and sample_count:
            root_causes.append({
                "severity": "critical", "code": "profit_factor_below_one",
                "message": "Gross losses exceed gross wins; increasing hit rate alone will not fix the payoff imbalance.",
            })
        unknown_fresh = next((x for x in segments["freshness"] if x["freshness"] == "unknown"), None)
        if unknown_fresh and unknown_fresh["samples"] / max(1, sample_count) >= 0.25:
            root_causes.append({
                "severity": "high", "code": "freshness_telemetry_gap",
                "message": "At least 25% of outcomes cannot be linked to a recorded price/candle freshness state.",
            })
        stale = next((x for x in segments["freshness"] if x["freshness"] in {"stale", "failed", "partial", "unverified"}), None)
        if stale and stale["samples"] >= 5 and (stale["expectancy_points"] or 0) < 0:
            root_causes.append({
                "severity": "critical", "code": "stale_data_loss_cluster",
                "message": "Signals associated with stale/failed evidence have negative expectancy and should remain hard-blocked.",
            })
        low_rr = next((x for x in segments["rr_band"] if x["rr_band"] == "<1.5"), None)
        if low_rr and low_rr["samples"] >= 5 and (low_rr["expectancy_points"] or 0) <= 0:
            root_causes.append({
                "severity": "high", "code": "weak_rr_cluster",
                "message": "Sub-1.5 R:R outcomes are not paying for their losses; review room-to-target and execution costs in shadow mode.",
            })
        for code, label in (
            ("initial_thesis_failed", "Initial thesis failure is the dominant loss attribution."),
            ("false_breakout", "False breakouts are a material source of losses."),
            ("time_stop_no_resolution", "Too many signals expire without resolving."),
            ("stop_after_progress", "Signals often progress before reversing into the stop."),
        ):
            count = attributions.get(code, 0)
            if count >= max(5, int(sample_count * 0.2)):
                root_causes.append({"severity": "high", "code": code, "message": label, "count": count})

        recommendations = [
            "Do not change production thresholds until the minimum sample and walk-forward gates pass.",
            "Prioritise positive expectancy and profit factor over headline win rate.",
            "Run score-band, R:R-band, regime and freshness ablations in shadow mode before promoting a challenger.",
        ]
        if any(c["code"] == "freshness_telemetry_gap" for c in root_causes):
            recommendations.insert(0, "Backfill signal-time freshness evidence into the immutable learning log.")
        if any(c["code"] == "weak_rr_cluster" for c in root_causes):
            recommendations.insert(0, "Test a higher minimum post-cost R:R as a challenger, not as an immediate production change.")

        return {
            "ok": True,
            "state": "measured" if sample_count >= self.minimum_sample else "collecting",
            "overall": overall,
            "segments": segments,
            "attributions": [{"name": key, "count": count} for key, count in attributions.most_common()],
            "root_causes": root_causes,
            "recommendations": recommendations,
            "policy": {
                "supported_modes": ["intraday", "delivery"],
                "minimum_sample": self.minimum_sample,
                "automatic_production_changes": False,
                "walk_forward_required": True,
                "shadow_challenger_required": True,
            },
        }
