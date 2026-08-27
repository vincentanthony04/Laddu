"""Governed search for sustainable post-cost risk-adjusted Model-Paper edge.

The optimizer is proposal-only. Thresholds are searched on an earlier
chronological window and must survive an untouched later holdout. Ranking uses
lower-confidence post-cost net-R, time/capital efficiency, drawdown and regime
robustness; win rate is a secondary diagnostic only. No result can mutate a
production rule without human approval.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import statistics
from typing import Any, Iterable, Mapping

from core.signal_age_authority import DEFAULT_SIGNAL_AGE_AUTHORITY


class Level5EdgeOptimizationService:
    authority = "Level5EdgeOptimizationService"
    version = "level5-edge-optimizer-2.0.0"
    MIN_SAMPLE = 30
    MIN_DECISIVE = 20
    MIN_SEARCH_SAMPLE = 24
    MIN_SEARCH_DECISIVE = 16
    MIN_VALIDATION_SAMPLE = 10
    MIN_VALIDATION_DECISIVE = 6
    Z90 = 1.6448536269514722
    SEARCH_FRACTION = 0.70

    @staticmethod
    def _json(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        try:
            out = json.loads(str(value or "{}"))
            return dict(out) if isinstance(out, Mapping) else {}
        except Exception:
            return {}

    @classmethod
    def _normalise(cls, rows: Iterable[Mapping[str, Any]], mode: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in rows or ():
            if str(raw.get("mode") or "").lower() != mode:
                continue
            payload = cls._json(raw.get("latest_payload") or raw.get("payload") or raw.get("payload_json"))
            score = payload.get("rank_score", payload.get("score", raw.get("score")))
            try:
                score_f = float(score)
                net_pnl = float(raw.get("net_pnl"))
                qty = int(raw.get("quantity") or 0)
                entry = float(raw.get("entry_price") or raw.get("original_entry"))
                stop = float(raw.get("original_stop"))
            except (TypeError, ValueError):
                continue
            initial_risk_cash = abs(entry - stop) * qty
            if initial_risk_cash <= 0 or not math.isfinite(net_pnl):
                continue
            try:
                net_r = float(raw.get("realized_r")) if raw.get("realized_r") is not None else net_pnl / initial_risk_cash
            except (TypeError, ValueError):
                net_r = net_pnl / initial_risk_cash
            if not math.isfinite(net_r):
                continue
            closed = str(raw.get("closed_at") or raw.get("updated_at") or "")
            opened = str(raw.get("opened_at") or "")
            duration_hours = None
            try:
                if raw.get("holding_seconds") is not None:
                    duration_hours = max(float(raw.get("holding_seconds")) / 3600.0, 1.0 / 60.0)
                else:
                    open_dt = datetime.fromisoformat(opened.replace("Z", "+00:00"))
                    close_dt = datetime.fromisoformat(closed.replace("Z", "+00:00"))
                    duration_hours = max((close_dt - open_dt).total_seconds() / 3600.0, 1.0 / 60.0)
            except Exception:
                pass
            try:
                mfe_r = float(raw.get("mfe_r")) if raw.get("mfe_r") is not None else None
                mae_r = float(raw.get("mae_r")) if raw.get("mae_r") is not None else None
            except (TypeError, ValueError):
                mfe_r = mae_r = None
            regime = str(payload.get("regime") or payload.get("market_structure") or "unknown").lower()
            age = DEFAULT_SIGNAL_AGE_AUTHORITY.enrich(raw, at=closed or raw.get("updated_at"))
            out.append({
                "score": score_f, "net_r": net_r, "closed_at": closed, "opened_at": opened,
                "duration_hours": duration_hours, "mfe_r": mfe_r, "mae_r": mae_r,
                "regime": regime,
                # Decision-delay bucket is the relevant entry-time signal-age
                # dimension. Holding age is evaluated separately in lifecycle
                # and management-effectiveness learning.
                "signal_age_bucket": age.get("decision_delay_bucket"),
                "age_bucket_policy_version": age.get("age_bucket_policy_version"),
            })
        out.sort(key=lambda x: x["closed_at"])
        return out

    @classmethod
    def _wilson_low(cls, wins: int, total: int) -> float | None:
        if total <= 0:
            return None
        p = wins / total
        z = cls.Z90
        denom = 1 + z * z / total
        centre = (p + z * z / (2 * total)) / denom
        margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
        return max(0.0, centre - margin)

    @classmethod
    def _candidate(
        cls,
        threshold: float,
        rows: list[dict[str, Any]],
        *,
        min_sample: int | None = None,
        min_decisive: int | None = None,
        require_temporal_halves: bool = True,
    ) -> dict[str, Any]:
        min_sample = cls.MIN_SAMPLE if min_sample is None else int(min_sample)
        min_decisive = cls.MIN_DECISIVE if min_decisive is None else int(min_decisive)
        sample_rows = [r for r in rows if r["score"] >= threshold]
        values = [r["net_r"] for r in sample_rows]
        wins = sum(v > 0 for v in values)
        losses = sum(v < 0 for v in values)
        decisive = wins + losses
        mean = statistics.fmean(values) if values else None
        stdev = statistics.stdev(values) if len(values) >= 2 else None
        lower_mean = (mean - cls.Z90 * stdev / math.sqrt(len(values))) if mean is not None and stdev is not None else mean
        time_weighted = [r["net_r"] / max(float(r["duration_hours"]), 1.0 / 60.0) * 24.0 for r in sample_rows if r.get("duration_hours") is not None]
        mean_time_weighted = statistics.fmean(time_weighted) if time_weighted else None
        total_exposure_days = sum(max(float(r["duration_hours"]), 1.0 / 60.0) / 24.0 for r in sample_rows if r.get("duration_hours") is not None)
        capital_efficiency = (sum(values) / total_exposure_days) if values and total_exposure_days > 0 else None
        mfe_values = [float(r["mfe_r"]) for r in sample_rows if r.get("mfe_r") is not None]
        mae_values = [float(r["mae_r"]) for r in sample_rows if r.get("mae_r") is not None]
        mean_mfe_r = statistics.fmean(mfe_values) if mfe_values else None
        mean_mae_r = statistics.fmean(mae_values) if mae_values else None
        gross_win = sum(v for v in values if v > 0)
        gross_loss = abs(sum(v for v in values if v < 0))
        pf = gross_win / gross_loss if gross_loss else (999.0 if gross_win else 0.0)

        curve = peak = 0.0
        max_drawdown = 0.0
        for value in values:
            curve += value
            peak = max(peak, curve)
            max_drawdown = min(max_drawdown, curve - peak)
        max_drawdown_r = abs(max_drawdown)
        lower_mean_to_drawdown = (lower_mean / max(max_drawdown_r, 0.25)) if lower_mean is not None else None

        split = max(1, len(values) // 2)
        early = values[:split]
        late = values[split:]
        early_mean = statistics.fmean(early) if early else None
        late_mean = statistics.fmean(late) if late else None

        regime_values: dict[str, list[float]] = {}
        for row in sample_rows:
            regime = str(row.get("regime") or "unknown")
            if regime in {"", "unknown", "none"}:
                continue
            regime_values.setdefault(regime, []).append(float(row["net_r"]))
        regime_means = {key: statistics.fmean(vals) for key, vals in regime_values.items() if vals}
        regime_min_mean = min(regime_means.values()) if regime_means else None
        positive_regime_fraction = (sum(v > 0 for v in regime_means.values()) / len(regime_means)) if regime_means else None
        regime_gate = True if len(regime_means) < 2 else bool(regime_min_mean is not None and regime_min_mean > 0)

        age_values: dict[str, list[float]] = {}
        for row in sample_rows:
            bucket = str(row.get("signal_age_bucket") or "MISSING")
            if bucket in {"", "MISSING", "none", "unknown"}:
                continue
            age_values.setdefault(bucket, []).append(float(row["net_r"]))
        age_means = {key: statistics.fmean(vals) for key, vals in age_values.items() if vals}
        age_min_mean = min(age_means.values()) if age_means else None
        positive_age_fraction = (sum(v > 0 for v in age_means.values()) / len(age_means)) if age_means else None
        age_gate = True if len(age_means) < 2 else bool(age_min_mean is not None and age_min_mean > 0)

        temporal_gate = True if not require_temporal_halves else bool(
            early_mean is not None and early_mean > 0 and late_mean is not None and late_mean > 0
        )
        eligible = bool(
            len(values) >= min_sample
            and decisive >= min_decisive
            and lower_mean is not None and lower_mean > 0
            and pf > 1.0
            and temporal_gate
            and regime_gate
            and age_gate
        )
        return {
            "minimum_score": round(threshold, 4),
            "sample": len(values), "decisive": decisive, "wins": wins, "losses": losses,
            "win_rate_pct": round(100.0 * wins / decisive, 3) if decisive else None,
            "win_rate_wilson90_lower_pct": round(100.0 * cls._wilson_low(wins, decisive), 3) if decisive else None,
            "mean_post_cost_net_r": round(mean, 6) if mean is not None else None,
            "lower90_mean_post_cost_net_r": round(lower_mean, 6) if lower_mean is not None else None,
            "mean_time_weighted_post_cost_net_r_per_24h": round(mean_time_weighted, 6) if mean_time_weighted is not None else None,
            "capital_efficiency_net_r_per_exposure_day": round(capital_efficiency, 6) if capital_efficiency is not None else None,
            "max_drawdown_r": round(max_drawdown_r, 6),
            "lower90_mean_to_drawdown": round(lower_mean_to_drawdown, 6) if lower_mean_to_drawdown is not None else None,
            "mean_mfe_r": round(mean_mfe_r, 6) if mean_mfe_r is not None else None,
            "mean_mae_r": round(mean_mae_r, 6) if mean_mae_r is not None else None,
            "profit_factor": round(pf, 6),
            "early_half_mean_net_r": round(early_mean, 6) if early_mean is not None else None,
            "late_half_mean_net_r": round(late_mean, 6) if late_mean is not None else None,
            "regime_count": len(regime_means),
            "regime_min_mean_net_r": round(regime_min_mean, 6) if regime_min_mean is not None else None,
            "positive_regime_fraction": round(positive_regime_fraction, 6) if positive_regime_fraction is not None else None,
            "signal_age_bucket_count": len(age_means),
            "signal_age_bucket_min_mean_net_r": round(age_min_mean, 6) if age_min_mean is not None else None,
            "positive_signal_age_bucket_fraction": round(positive_age_fraction, 6) if positive_age_fraction is not None else None,
            "age_bucket_policy_version": DEFAULT_SIGNAL_AGE_AUTHORITY.attribution_policy_version,
            "eligible": eligible,
            "eligibility_gates": {
                "minimum_sample": min_sample, "minimum_decisive": min_decisive,
                "positive_lower90_expectancy": bool(lower_mean is not None and lower_mean > 0),
                "profit_factor_above_one": pf > 1.0,
                "temporal_halves_positive": temporal_gate,
                "regime_robust": regime_gate,
                "signal_age_robust": age_gate,
            },
        }

    @classmethod
    def optimize(cls, rows: Iterable[Mapping[str, Any]], *, mode: str) -> dict[str, Any]:
        desk = str(mode or "").lower()
        if desk not in {"intraday", "delivery"}:
            raise ValueError("mode must be intraday or delivery")
        data = cls._normalise(rows, desk)
        if len(data) < cls.MIN_SAMPLE:
            return cls._empty(desk, "INSUFFICIENT_SETTLED_POST_COST_MODEL_PAPER_ROWS")

        split = int(len(data) * cls.SEARCH_FRACTION)
        split = max(cls.MIN_SEARCH_SAMPLE, split)
        split = min(split, len(data) - cls.MIN_VALIDATION_SAMPLE)
        if split < cls.MIN_SEARCH_SAMPLE or len(data) - split < cls.MIN_VALIDATION_SAMPLE:
            return cls._empty(desk, "INSUFFICIENT_UNTOUCHED_VALIDATION_WINDOW")
        search_rows = data[:split]
        validation_rows = data[split:]

        scores = sorted({r["score"] for r in search_rows})
        if len(scores) > 21:
            indexes = sorted({round(i * (len(scores) - 1) / 20) for i in range(21)})
            thresholds = [scores[i] for i in indexes]
        else:
            thresholds = scores

        evaluations = []
        for threshold in thresholds:
            search = cls._candidate(
                threshold, search_rows,
                min_sample=cls.MIN_SEARCH_SAMPLE,
                min_decisive=cls.MIN_SEARCH_DECISIVE,
                require_temporal_halves=True,
            )
            validation = cls._candidate(
                threshold, validation_rows,
                min_sample=cls.MIN_VALIDATION_SAMPLE,
                min_decisive=cls.MIN_VALIDATION_DECISIVE,
                require_temporal_halves=False,
            )
            full = cls._candidate(threshold, data, min_sample=cls.MIN_SAMPLE, min_decisive=cls.MIN_DECISIVE)
            untouched_pass = bool(search["eligible"] and validation["eligible"] and full["eligible"])
            evaluations.append({
                "minimum_score": round(threshold, 4),
                "search": search,
                "untouched_validation": validation,
                "full_population_diagnostic": full,
                "untouched_validation_pass": untouched_pass,
            })

        eligible = [e for e in evaluations if e["untouched_validation_pass"]]
        chosen = max(
            eligible,
            key=lambda e: (
                float(e["untouched_validation"].get("lower90_mean_post_cost_net_r") or -999),
                float(e["untouched_validation"].get("lower90_mean_to_drawdown") or -999),
                float(e["untouched_validation"].get("capital_efficiency_net_r_per_exposure_day") or -999),
                -float(e["untouched_validation"].get("max_drawdown_r") or 999),
                float(e["untouched_validation"].get("regime_min_mean_net_r") or -999),
                float(e["untouched_validation"].get("signal_age_bucket_min_mean_net_r") or -999),
                float(e["search"].get("lower90_mean_post_cost_net_r") or -999),
                int(e["full_population_diagnostic"].get("sample") or 0),
                float(e["full_population_diagnostic"].get("win_rate_wilson90_lower_pct") or -1),
            ),
        ) if eligible else None

        champion = None
        if chosen:
            champion = {
                **dict(chosen["full_population_diagnostic"]),
                "search_metrics": chosen["search"],
                "untouched_validation": chosen["untouched_validation"],
                "untouched_validation_pass": True,
                "selection_priority": [
                    "untouched lower-90 post-cost net-R",
                    "untouched lower-90 expectancy / drawdown",
                    "untouched capital efficiency",
                    "lower drawdown",
                    "regime minimum mean-R",
                    "signal-age bucket minimum mean-R",
                    "search lower-90 post-cost net-R",
                    "win-rate lower bound only as secondary diagnostic",
                ],
            }

        return {
            "ok": True,
            "state": "PROPOSAL_READY" if champion else "COLLECT_MORE_EVIDENCE",
            "mode": desk,
            "settled_post_cost_rows": len(data),
            "search_rows": len(search_rows),
            "untouched_validation_rows": len(validation_rows),
            "champion_proposal": champion,
            "evaluated_thresholds": evaluations,
            "objective": "highest sustainable post-cost risk-adjusted edge validated on an untouched chronological holdout; win rate is secondary",
            "validation_scheme": "chronological search window -> untouched tail holdout; thresholds are discovered only in search data",
            "anti_gaming": {
                "minimum_full_sample": cls.MIN_SAMPLE,
                "minimum_search_sample": cls.MIN_SEARCH_SAMPLE,
                "minimum_validation_sample": cls.MIN_VALIDATION_SAMPLE,
                "threshold_search_cap": 21,
                "post_cost_source": "canonical FinalExcursionAttributionAuthority.realized_r",
                "risk_adjusted_dimensions": ["lower90_expectancy", "time_weighted_edge", "drawdown", "capital_efficiency", "regime_robustness", "signal_age_robustness"],
                "signal_age_bucket_policy_version": DEFAULT_SIGNAL_AGE_AUTHORITY.attribution_policy_version,
                "win_rate_primary_objective": False,
                "untouched_validation_required": True,
                "automatic_production_mutation": False,
                "human_approval_required": True,
            },
            "authority": cls.authority,
            "version": cls.version,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    @classmethod
    def _empty(cls, desk: str, reason: str) -> dict[str, Any]:
        return {
            "ok": True, "state": "COLLECT_MORE_EVIDENCE", "mode": desk,
            "settled_post_cost_rows": 0, "champion_proposal": None,
            "evaluated_thresholds": [], "reason": reason,
            "objective": "highest sustainable post-cost risk-adjusted Model-Paper edge with untouched validation",
            "validation_scheme": "chronological search window -> untouched tail holdout",
            "anti_gaming": {
                "minimum_full_sample": cls.MIN_SAMPLE,
                "minimum_search_sample": cls.MIN_SEARCH_SAMPLE,
                "minimum_validation_sample": cls.MIN_VALIDATION_SAMPLE,
                "win_rate_primary_objective": False,
                "untouched_validation_required": True,
                "automatic_production_mutation": False,
                "human_approval_required": True,
            },
            "authority": cls.authority, "version": cls.version,
        }
