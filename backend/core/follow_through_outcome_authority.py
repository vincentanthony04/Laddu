"""Deterministic post-exit follow-through measurement for learning.

The original settlement result is immutable. This authority measures what the
market did afterwards at explicit, versioned horizons supplied by a validated
market-data projection. It never changes the historical trade result.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping

from core.numeric_semantics import finite_number


class FollowThroughOutcomeAuthority:
    authority = "FollowThroughOutcomeAuthority"
    authority_version = "1.0.0-fixed-horizon-r"
    neutral_band_r = 0.25
    horizons = {
        "intraday": ("15m", "30m", "60m", "close"),
        "delivery": ("1D", "3D", "5D", "10D", "20D"),
    }
    primary_horizon = {"intraday": "60m", "delivery": "5D"}

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
    def _directional(cls, side: str, start: float, end: float) -> float:
        return (end - start) if side == "LONG" else (start - end)

    @classmethod
    def measure(cls, record: Mapping[str, Any], horizon_evidence: Mapping[str, Any] | None) -> dict[str, Any]:
        row = dict(record or {})
        mode = str(row.get("mode") or "").lower()
        side = str(row.get("side") or "").upper()
        entry = cls._float(row.get("entry_price") if row.get("entry_price") is not None else row.get("original_entry"))
        stop = cls._float(row.get("original_stop"))
        exit_price = cls._float(row.get("exit_price"))
        exit_reason = str(row.get("exit_reason") or row.get("result") or "").upper()
        expected = cls.horizons.get(mode)
        if not expected or side not in {"LONG", "SHORT"} or None in {entry, stop, exit_price}:
            return cls._unavailable("identity_or_geometry_incomplete", mode=mode)
        initial_r = abs(float(entry) - float(stop))
        if initial_r <= 0:
            return cls._unavailable("zero_initial_risk", mode=mode)

        closed_at = cls._dt(row.get("closed_at"))
        evidence = dict(horizon_evidence or {})
        measured: dict[str, Any] = {}
        for horizon in expected:
            raw = evidence.get(horizon)
            if not isinstance(raw, Mapping) or raw.get("complete") is not True:
                measured[horizon] = {"state": "UNAVAILABLE", "reason": "complete_exact_horizon_evidence_required"}
                continue
            price = cls._float(raw.get("price"))
            if price is None or price <= 0:
                measured[horizon] = {"state": "UNAVAILABLE", "reason": "finite_positive_price_required"}
                continue
            observed_at = cls._dt(raw.get("observed_at"))
            if closed_at is None or observed_at is None or observed_at <= closed_at:
                measured[horizon] = {"state": "UNAVAILABLE", "reason": "causal_post_exit_observation_required"}
                continue
            move_r = cls._directional(side, float(exit_price), price) / initial_r
            recovered = (
                any(token in exit_reason for token in ("SL_HIT", "STOP_HIT", "MANAGED_STOP_HIT"))
                and ((side == "LONG" and price >= float(entry)) or (side == "SHORT" and price <= float(entry)))
            )
            if recovered:
                state = "RECOVERED"
            elif move_r >= cls.neutral_band_r:
                state = "CONTINUED"
            elif move_r <= -cls.neutral_band_r:
                state = "REVERSED"
            else:
                state = "FLAT"
            measured[horizon] = {
                "state": state,
                "price": round(price, 6),
                "move_r_from_exit": round(move_r, 6),
                "observed_at": raw.get("observed_at"),
                "source": raw.get("source"),
                "complete": True,
            }

        preferred = cls.primary_horizon[mode]
        primary = measured.get(preferred) or {}
        primary_used = preferred if primary.get("complete") is True else None
        if primary_used is None:
            for horizon in reversed(expected):
                if (measured.get(horizon) or {}).get("complete") is True:
                    primary_used = horizon
                    primary = measured[horizon]
                    break
        return {
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "state": "READY" if primary_used else "EVIDENCE_PENDING",
            "after": primary.get("state") if primary_used else None,
            "after_horizon": primary_used,
            "neutral_band_r": cls.neutral_band_r,
            "horizons": measured,
            "result_is_immutable": True,
            "original_exit_reason": exit_reason or None,
            "policy": "post-exit observation only; exact completed horizon evidence; no interpolation; never rewrites settlement result",
        }

    @classmethod
    def _unavailable(cls, reason: str, *, mode: str) -> dict[str, Any]:
        return {
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "state": "UNAVAILABLE",
            "after": None,
            "after_horizon": None,
            "reason": reason,
            "mode": mode or None,
            "result_is_immutable": True,
        }


DEFAULT_FOLLOW_THROUGH_OUTCOME_AUTHORITY = FollowThroughOutcomeAuthority()
