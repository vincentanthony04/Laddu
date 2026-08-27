"""Single canonical Model Paper trade lifecycle mathematics authority.

Production lifecycle state is intentionally simple:
READY -> ACTIVE -> CLOSED.
The immutable settlement result is separate from management observations and
from post-exit follow-through.  This authority owns ordered verified-quote
transition math and settled excursion/R attribution.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Dict, Mapping

from core.intraday_session_policy import IntradaySessionPolicy
from core.numeric_semantics import finite_number
from core.live_thesis_reassessment_service import DEFAULT_LIVE_THESIS_REASSESSMENT
from core.current_managed_risk_authority import DEFAULT_CURRENT_MANAGED_RISK_AUTHORITY


class CanonicalTradeLifecycleAuthority:
    authority = "CanonicalTradeLifecycleAuthority"
    authority_version = "1.0.0-single-production-lifecycle"
    policy = (
        "ordered verified quotes only; target fill capped at planned target; adverse stop fills use observed price; "
        "original result is immutable; excursion uses immutable original risk"
    )

    @staticmethod
    def _float(value: Any) -> float | None:
        return finite_number(value)

    @staticmethod
    def _dt(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            out = value
        else:
            try:
                out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except Exception:
                return None
        return out if out.tzinfo else out.replace(tzinfo=timezone.utc)

    @classmethod
    def evaluate(
        cls,
        row: Dict[str, Any],
        qrow: Dict[str, Any],
        phase: Dict[str, Any],
        *,
        at: Any = None,
        thesis_evidence: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        price = cls._float(qrow.get("ltp") if qrow.get("ltp") is not None else qrow.get("price"))
        if price is None or price <= 0:
            return cls._invalid("verified finite positive quote required")
        # The upstream ModelPortfolioService already rejects unverified/stale quotes;
        # re-check when those explicit flags are present so callers cannot bypass it.
        for flag in ("verified", "fresh", "executable"):
            if flag in qrow and qrow.get(flag) is not True:
                return cls._invalid("quote is not verified/fresh/executable")

        mode = str(row.get("mode") or "").lower()
        side = str(row.get("side") or "").upper()
        if mode not in {"intraday", "delivery"} or side not in {"LONG", "SHORT"}:
            return cls._invalid("supported mode and LONG/SHORT side required")
        target = cls._float(row.get("original_target"))
        managed_stop = cls._float(row.get("managed_stop"))
        original_stop = cls._float(row.get("original_stop"))
        entry = cls._float(row.get("entry_price"))
        original_entry = cls._float(row.get("original_entry"))
        if None in {target, managed_stop, original_stop, entry, original_entry}:
            return cls._invalid("entry/target/stop lifecycle geometry incomplete")
        if min(float(target), float(managed_stop), float(original_stop), float(entry), float(original_entry)) <= 0:
            return cls._invalid("entry/target/stop lifecycle geometry invalid")
        if side == "LONG":
            if not (float(original_stop) < float(original_entry) < float(target)):
                return cls._invalid("LONG lifecycle geometry must satisfy original_stop < original_entry < target")
            if not float(managed_stop) < float(target):
                return cls._invalid("LONG managed stop must remain below target")
        else:
            if not (float(target) < float(original_entry) < float(original_stop)):
                return cls._invalid("SHORT lifecycle geometry must satisfy target < original_entry < original_stop")
            if not float(managed_stop) > float(target):
                return cls._invalid("SHORT managed stop must remain above target")

        reassessment = DEFAULT_LIVE_THESIS_REASSESSMENT.evaluate(
            row, qrow, at=at, thesis_evidence=thesis_evidence
        )
        long = side == "LONG"

        if mode == "intraday" and phase.get("mandatory_exit") is True:
            return cls._exit(price, "TIME_EXIT_MANDATORY_FLAT_TARGET_NOT_HIT", state="MANDATORY_TIME_EXIT", reassessment=reassessment)

        target_hit = price >= target if long else price <= target
        stop_hit = price <= managed_stop if long else price >= managed_stop
        # If an ordered quote jumps across both logical boundaries, adverse stop
        # has priority only when it is actually on the adverse side for this side;
        # with valid long/short geometry both cannot be crossed by the same price.
        if stop_hit:
            reason = "STOP_HIT" if math.isclose(managed_stop, original_stop, rel_tol=0.0, abs_tol=1e-9) else "MANAGED_STOP_HIT"
            return cls._exit(price, reason, state="STOP_OR_MANAGED_STOP_HIT", reassessment=reassessment)
        if target_hit:
            return cls._exit(target, "TARGET_HIT", state="TARGET_HIT_CONSERVATIVE_FILL", reassessment=reassessment)

        high = max(cls._float(row.get("high_watermark")) or entry, entry, price)
        low = min(cls._float(row.get("low_watermark")) or entry, entry, price)
        initial_r = abs(original_entry - original_stop)
        favorable = max(0.0, (high - entry) if long else (entry - low))
        adverse = max(0.0, (entry - low) if long else (high - entry))

        if reassessment.get("state") == "INVALIDATED":
            return cls._exit(price, "EXIT_INVALIDATED", state="THESIS_INVALIDATED", reassessment=reassessment)
        weakening = reassessment.get("state") == "WEAKENING"
        if weakening and initial_r > 0 and adverse >= 0.5 * initial_r:
            return cls._exit(price, "EXIT_WEAKENED", state="THESIS_WEAKENED", reassessment=reassessment)

        proposed = managed_stop
        action = "CONTINUE TO HOLD"
        hit = "NONE"
        if initial_r > 0 and favorable >= 1.25 * initial_r:
            proposed = entry + (0.5 * initial_r if long else -0.5 * initial_r)
            action = "TRAIL STOP"
            hit = "TRAIL_ACTIVE"
        elif initial_r > 0 and favorable >= 0.75 * initial_r:
            proposed = entry
            action = "PROTECT AT BREAKEVEN"
            hit = "BREAKEVEN_PROTECTED"
        elif (price < entry if long else price > entry):
            action = "WAIT — THESIS UNDER PRESSURE · DO NOT ADD"
        if phase.get("manage_only") and mode == "intraday":
            action = f"MANAGE ONLY · FLAT BY {IntradaySessionPolicy.mandatory_flat_label()}"
        managed = max(managed_stop, proposed) if long else min(managed_stop, proposed)
        managed_risk = DEFAULT_CURRENT_MANAGED_RISK_AUTHORITY.require_non_widening({**row, "managed_stop": managed})
        return {
            "ok": True,
            "operation": "MARK",
            "state": "ACTIVE",
            "price": price,
            "managed_stop": managed,
            "managed_risk": managed_risk,
            "high_watermark": high,
            "low_watermark": low,
            "mfe_points": favorable,
            "mae_points": adverse,
            "action": action,
            "hit_status": hit,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "policy": cls.policy,
            "thesis_reassessment": reassessment,
        }

    @classmethod
    def _invalid(cls, reason: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "operation": "NO_CHANGE",
            "state": "DATA_UNAVAILABLE",
            "reason": reason,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "policy": cls.policy,
        }

    @classmethod
    def _exit(cls, price: float, reason: str, *, state: str, reassessment: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return {
            "ok": True,
            "operation": "EXIT",
            "state": "CLOSED",
            "transition_state": state,
            "exit_price": float(price),
            "exit_reason": reason,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "policy": cls.policy,
            "thesis_reassessment": reassessment or {},
        }

    @classmethod
    def enrich_settlement(cls, record: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(record or {})
        side = str(row.get("side") or "").upper()
        entry = cls._float(row.get("entry_price") if row.get("entry_price") is not None else row.get("original_entry"))
        stop = cls._float(row.get("original_stop"))
        high = cls._float(row.get("high_watermark"))
        low = cls._float(row.get("low_watermark"))
        qty = cls._float(row.get("quantity"))
        net_pnl = cls._float(row.get("net_pnl"))
        gross_pnl = cls._float(row.get("gross_pnl"))
        complete = side in {"LONG", "SHORT"} and all(v is not None for v in (entry, stop, high, low, qty)) and float(qty or 0) > 0
        initial_risk_points = abs(float(entry) - float(stop)) if complete else None
        initial_risk_cash = initial_risk_points * float(qty) if initial_risk_points is not None else None
        if complete and side == "LONG":
            mfe_points = max(0.0, float(high) - float(entry))
            mae_points = max(0.0, float(entry) - float(low))
        elif complete:
            mfe_points = max(0.0, float(entry) - float(low))
            mae_points = max(0.0, float(high) - float(entry))
        else:
            mfe_points = mae_points = None

        opened, closed = cls._dt(row.get("opened_at")), cls._dt(row.get("closed_at"))
        causal_valid = not (opened and closed and closed < opened)
        holding_seconds = int((closed - opened).total_seconds()) if opened and closed and causal_valid else None

        def points_r(points: float | None) -> float | None:
            return points / initial_risk_points if points is not None and initial_risk_points and initial_risk_points > 0 else None

        def cash_r(value: float | None) -> float | None:
            return value / initial_risk_cash if value is not None and initial_risk_cash and initial_risk_cash > 0 else None

        row.update({
            "mfe_points": round(mfe_points, 6) if mfe_points is not None else None,
            "mae_points": round(mae_points, 6) if mae_points is not None else None,
            "mfe_r": round(points_r(mfe_points), 6) if points_r(mfe_points) is not None else None,
            "mae_r": round(points_r(mae_points), 6) if points_r(mae_points) is not None else None,
            "initial_risk_points": round(initial_risk_points, 6) if initial_risk_points is not None else None,
            "initial_risk_cash": round(initial_risk_cash, 2) if initial_risk_cash is not None else None,
            "realized_r": round(cash_r(net_pnl), 6) if cash_r(net_pnl) is not None else None,
            "gross_r": round(cash_r(gross_pnl), 6) if cash_r(gross_pnl) is not None else None,
            "holding_seconds": holding_seconds,
            "holding_minutes": round(holding_seconds / 60.0, 2) if holding_seconds is not None else None,
            "lifecycle_causal_valid": causal_valid,
            "lifecycle_causal_violations": [] if causal_valid else ["closed_at_before_opened_at"],
            "excursion_attribution_authority": cls.authority,
            "excursion_attribution_version": cls.authority_version,
            "excursion_attribution_complete": bool(complete and initial_risk_points and initial_risk_points > 0 and holding_seconds is not None and causal_valid),
            "excursion_policy": "side-correct non-negative watermarks; immutable entry/original-stop risk; realized R uses net P&L after costs; impossible chronology rejected",
        })
        return row

    @classmethod
    def result_class(cls, *, exit_reason: Any, net_pnl: Any = None) -> str:
        reason = str(exit_reason or "").upper()
        if "TARGET_HIT" in reason:
            return "SUCCESS"
        if reason in {"STOP_HIT", "MANAGED_STOP_HIT"} or "STOP_HIT" in reason:
            return "FAILURE"
        pnl = cls._float(net_pnl)
        if pnl is None:
            return "UNSCORABLE"
        if pnl > 0:
            return "SUCCESS"
        if pnl < 0:
            return "FAILURE"
        return "NEUTRAL"


DEFAULT_CANONICAL_TRADE_LIFECYCLE_AUTHORITY = CanonicalTradeLifecycleAuthority()
