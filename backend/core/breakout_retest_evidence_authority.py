"""Canonical breakout/retest evidence semantics.

This authority does not discover structural price levels.  The canonical
support/resistance and master-candle services own those levels.  It answers a
narrower question: given a governed trigger level and completed-candle context,
is price currently confirming a breakout/retest, waiting near the trigger, or
already beyond the level without structural proof?

Keeping this boundary explicit prevents Stock Report, scanner and execution
surfaces from implementing subtly different breakout tests.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Mapping, Optional


class BreakoutRetestEvidenceAuthority:
    authority = "BreakoutRetestEvidenceAuthority"
    authority_version = "1.0.0"

    _LONG_PROOF = {"BREAKOUT_UP", "RETEST"}
    _SHORT_PROOF = {"BREAKDOWN", "RETEST"}

    @staticmethod
    def _num(value: Any) -> Optional[float]:
        try:
            out = float(value)
            return out if math.isfinite(out) else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _last_candle_direction(cls, candles: Iterable[Mapping[str, Any]] | None) -> str:
        rows = list(candles or [])
        if not rows:
            return "UNKNOWN"
        row = rows[-1]
        opened = cls._num(row.get("open"))
        closed = cls._num(row.get("close"))
        if opened is None or closed is None:
            return "UNKNOWN"
        if closed > opened:
            return "BULLISH"
        if closed < opened:
            return "BEARISH"
        return "FLAT"

    @classmethod
    def evaluate_trigger(
        cls,
        *,
        side: str,
        trigger_level: Any,
        current_price: Any,
        atr: Any,
        candles: Iterable[Mapping[str, Any]] | None,
        structural_state: Any = "UNTESTED",
        mode: str = "delivery",
    ) -> Dict[str, Any]:
        side_u = str(side or "").upper()
        mode_l = str(mode or "").lower()
        level = cls._num(trigger_level)
        current = cls._num(current_price)
        atr_value = cls._num(atr)
        state = str(structural_state or "UNTESTED").upper()
        if side_u not in {"LONG", "SHORT"}:
            return cls._unavailable("side must be LONG or SHORT")
        if level is None or level <= 0 or current is None or current <= 0:
            return cls._unavailable("trigger level and current price are required")
        if atr_value is None or atr_value <= 0:
            return cls._unavailable("positive ATR is required")

        # Preserve the established desk tolerances.  A small price-relative
        # floor prevents an abnormally tiny ATR from making the trigger test
        # numerically meaningless.
        atr_multiplier = 0.30 if mode_l == "intraday" else 0.45
        tolerance = max(atr_value * atr_multiplier, current * 0.0025)
        distance = current - level
        abs_distance = abs(distance)
        near_trigger = abs_distance <= tolerance
        candle_direction = cls._last_candle_direction(candles)
        compatible_proof = state in (cls._LONG_PROOF if side_u == "LONG" else cls._SHORT_PROOF)

        if side_u == "LONG":
            moved_beyond = current > level + tolerance
            on_breakout_side = current >= level
            candle_retest = candle_direction == "BULLISH"
            breakout_state_ok = state == "BREAKOUT_UP"
        else:
            moved_beyond = current < level - tolerance
            on_breakout_side = current <= level
            candle_retest = candle_direction == "BEARISH"
            breakout_state_ok = state == "BREAKDOWN"

        crossed_without_proof = moved_beyond and not compatible_proof
        breakout_confirmed = near_trigger and on_breakout_side and (breakout_state_ok or state == "UNTESTED")
        # RETEST can be explicitly supplied by structural S/R, or confirmed at
        # the level by a side-aware completed candle.  This deliberately does
        # not use future bars.
        retest_confirmed = near_trigger and candle_retest and (state == "RETEST" or not moved_beyond)
        trigger_confirmed = bool((breakout_confirmed or retest_confirmed) and not crossed_without_proof)

        if crossed_without_proof:
            evidence_state = "CROSSED_WITHOUT_STRUCTURAL_PROOF"
        elif retest_confirmed:
            evidence_state = "RETEST_CONFIRMED"
        elif breakout_confirmed:
            evidence_state = "BREAKOUT_CONFIRMED"
        elif near_trigger:
            evidence_state = "AT_TRIGGER_AWAITING_CONFIRMATION"
        elif compatible_proof:
            evidence_state = "STRUCTURAL_BREAK_CONFIRMED_AWAITING_RETEST"
        else:
            evidence_state = "AWAITING_TRIGGER"

        return {
            "ok": True,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "state": evidence_state,
            "side": side_u,
            "mode": mode_l,
            "trigger_level": round(level, 4),
            "current_price": round(current, 4),
            "atr": round(atr_value, 4),
            "tolerance": round(tolerance, 4),
            "distance": round(distance, 4),
            "near_trigger": bool(near_trigger),
            "structural_state": state,
            "structural_proof_compatible": bool(compatible_proof),
            "latest_completed_candle_direction": candle_direction,
            "breakout_confirmed": bool(breakout_confirmed),
            "retest_confirmed": bool(retest_confirmed),
            "trigger_confirmed": bool(trigger_confirmed),
            "crossed_without_proof": bool(crossed_without_proof),
            "future_bars_used": False,
            "policy": "Canonical levels come from S/R or Master Candle; this authority evaluates trigger confirmation only from current price plus already-completed evidence.",
        }

    @classmethod
    def project_structure_retest_zone(
        cls,
        *,
        candles: Iterable[Mapping[str, Any]] | None,
        structure: Mapping[str, Any] | None,
    ) -> Dict[str, Any]:
        """Compatibility projection for a market-structure BOS retest zone.

        This preserves the established 0.4x recent-average-range buffer while
        giving it an explicit authority/version and preventing another caller
        from reimplementing the zone calculation.
        """
        rows = list(candles or [])
        structure_d = dict(structure or {})
        if len(rows) < 15 or not structure_d.get("ok"):
            return cls._unavailable("need 15+ completed candles and valid structure")
        structure_state = str(structure_d.get("state") or "")
        if structure_state not in {"break_of_structure_up", "break_of_structure_down"}:
            return cls._unavailable("no active break of structure")
        ranges = []
        for row in rows[-15:]:
            high = cls._num(row.get("high")); low = cls._num(row.get("low"))
            if high is not None and low is not None and high >= low:
                ranges.append(high - low)
        if not ranges:
            return cls._unavailable("recent candle ranges unavailable")
        level = cls._num(structure_d.get("resistance") if structure_state == "break_of_structure_up" else structure_d.get("support"))
        if level is None:
            return cls._unavailable("broken structural level unavailable")
        buffer_value = (sum(ranges) / len(ranges)) * 0.4
        direction = "long" if structure_state == "break_of_structure_up" else "short"
        return {
            "ok": True,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "state": "RETEST_ZONE_READY",
            "direction": direction,
            "level": round(level, 2),
            "zone_low": round(level - buffer_value, 2),
            "zone_high": round(level + buffer_value, 2),
            "buffer": round(buffer_value, 4),
            "future_bars_used": False,
            "note": "retest of broken " + ("resistance-turned-support" if direction == "long" else "support-turned-resistance"),
        }

    @classmethod
    def _unavailable(cls, reason: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "state": "UNAVAILABLE",
            "reason": reason,
            "future_bars_used": False,
        }


DEFAULT_BREAKOUT_RETEST_EVIDENCE_AUTHORITY = BreakoutRetestEvidenceAuthority()
