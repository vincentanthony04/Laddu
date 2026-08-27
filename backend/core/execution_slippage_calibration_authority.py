"""Versioned cash-equity execution/slippage calibration authority.

This authority never places orders and never claims calibration from one quote.
It converts canonical live bid/ask/depth observations into empirical spread and
depth-impact samples, qualifies a bounded calibration snapshot, and freezes the
execution-cost assumptions used by both admission and Model Paper settlement.

A source build may prove this machinery while the live empirical gate remains
CALIBRATION_PENDING.  Current-market calibration requires sufficient fresh
observations on the installed target.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Iterable, Mapping


class ExecutionSlippageCalibrationAuthority:
    authority = "ExecutionSlippageCalibrationAuthority"
    authority_version = "1.0.0"
    execution_model_version = "cash-execution-model-1.0.0"
    calibration_policy_version = "cash-microstructure-calibration-1.0.0"
    broker_authority = "NONE"

    MIN_SAMPLES = 30
    MIN_INSTRUMENTS = 5
    MAX_SAMPLE_AGE_SEC = 30 * 60
    QUANTILE = 0.90

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            out = float(value)
            return out if math.isfinite(out) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _time(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            dt = value
        elif value not in (None, ""):
            try:
                dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
        else:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _hash(payload: Mapping[str, Any]) -> str:
        body = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(body.encode()).hexdigest()

    @staticmethod
    def _quantile(values: Iterable[float], q: float) -> float | None:
        rows = sorted(float(v) for v in values if math.isfinite(float(v)))
        if not rows:
            return None
        if len(rows) == 1:
            return rows[0]
        position = (len(rows) - 1) * max(0.0, min(1.0, q))
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return rows[lower]
        weight = position - lower
        return rows[lower] * (1.0 - weight) + rows[upper] * weight

    @classmethod
    def _depth_vwap(cls, levels: Any, quantity: int) -> tuple[float | None, float]:
        required = max(0, int(quantity or 0))
        if required <= 0 or not isinstance(levels, list):
            return None, 0.0
        remaining = float(required)
        value = filled = 0.0
        for raw in levels[:5]:
            if not isinstance(raw, Mapping):
                continue
            price = cls._number(raw.get("price"))
            available = cls._number(raw.get("quantity"))
            if price is None or price <= 0 or available is None or available <= 0:
                continue
            take = min(remaining, available)
            value += take * price
            filled += take
            remaining -= take
            if remaining <= 1e-9:
                break
        coverage = min(1.0, filled / float(required)) if required else 0.0
        return ((value / filled) if filled > 0 else None), coverage

    def observe(self, quote: Mapping[str, Any] | None, *, quantity: int) -> dict[str, Any]:
        row = dict(quote or {})
        bid = self._number(row.get("bid_price"))
        ask = self._number(row.get("ask_price"))
        observed_at = self._time(
            row.get("provider_timestamp") or row.get("timestamp") or row.get("source_time")
        )
        instrument_key = str(row.get("instrument_key") or "").strip() or None
        symbol = str(row.get("symbol") or "").strip().upper() or None
        valid_top = bool(bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid)
        mid = (bid + ask) / 2.0 if valid_top else None
        spread_bps = ((ask - bid) / mid * 10_000.0) if mid else None
        depth = row.get("depth") if isinstance(row.get("depth"), Mapping) else {}
        buy_vwap, buy_coverage = self._depth_vwap(list(depth.get("sell") or []), quantity)
        sell_vwap, sell_coverage = self._depth_vwap(list(depth.get("buy") or []), quantity)
        buy_impact = None
        sell_impact = None
        if mid and ask and buy_vwap is not None and buy_coverage >= 1.0:
            buy_impact = max(0.0, (buy_vwap - ask) / mid * 10_000.0)
        if mid and bid and sell_vwap is not None and sell_coverage >= 1.0:
            sell_impact = max(0.0, (bid - sell_vwap) / mid * 10_000.0)
        usable = bool(valid_top and observed_at and instrument_key and buy_impact is not None and sell_impact is not None)
        result = {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "instrument_key": instrument_key,
            "symbol": symbol,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z") if observed_at else None,
            "quote_sequence": row.get("quote_seq") or row.get("delta_id"),
            "quantity": max(0, int(quantity or 0)),
            "bid_price": bid,
            "ask_price": ask,
            "mid_price": mid,
            "spread_bps": round(spread_bps, 6) if spread_bps is not None else None,
            "buy_depth_vwap": round(buy_vwap, 8) if buy_vwap is not None else None,
            "sell_depth_vwap": round(sell_vwap, 8) if sell_vwap is not None else None,
            "buy_depth_coverage": round(buy_coverage, 6),
            "sell_depth_coverage": round(sell_coverage, 6),
            "buy_depth_impact_bps": round(buy_impact, 6) if buy_impact is not None else None,
            "sell_depth_impact_bps": round(sell_impact, 6) if sell_impact is not None else None,
            "usable_for_calibration": usable,
            "broker_authority": self.broker_authority,
        }
        result["observation_hash"] = self._hash(result)
        return result

    def build_snapshot(
        self,
        observations: Iterable[Mapping[str, Any]],
        *,
        mode: str,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        desk = str(mode or "").strip().lower()
        if desk not in {"intraday", "delivery"}:
            raise ValueError("mode must be intraday or delivery")
        now = (at or datetime.now(timezone.utc))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        accepted: list[dict[str, Any]] = []
        for raw in observations or []:
            row = dict(raw or {})
            if row.get("usable_for_calibration") is not True:
                continue
            stamp = self._time(row.get("observed_at"))
            if stamp is None:
                continue
            age = max(0.0, (now - stamp).total_seconds())
            if age > self.MAX_SAMPLE_AGE_SEC:
                continue
            accepted.append(row)
        spreads = [self._number(r.get("spread_bps")) for r in accepted]
        impacts = [
            max(self._number(r.get("buy_depth_impact_bps")) or 0.0, self._number(r.get("sell_depth_impact_bps")) or 0.0)
            for r in accepted
        ]
        instruments = {str(r.get("instrument_key") or "").strip() for r in accepted if str(r.get("instrument_key") or "").strip()}
        sample_count = len(accepted)
        state = "CALIBRATED" if sample_count >= self.MIN_SAMPLES and len(instruments) >= self.MIN_INSTRUMENTS else "CALIBRATION_PENDING"
        snapshot = {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "calibration_policy_version": self.calibration_policy_version,
            "execution_model_version": self.execution_model_version,
            "mode": desk,
            "as_of": now.isoformat().replace("+00:00", "Z"),
            "state": state,
            "sample_count": sample_count,
            "instrument_count": len(instruments),
            "minimum_samples": self.MIN_SAMPLES,
            "minimum_instruments": self.MIN_INSTRUMENTS,
            "max_sample_age_seconds": self.MAX_SAMPLE_AGE_SEC,
            "quantile": self.QUANTILE,
            "spread_p90_bps": round(self._quantile([x for x in spreads if x is not None], self.QUANTILE) or 0.0, 6),
            "depth_impact_p90_bps_each_side": round(self._quantile(impacts, self.QUANTILE) or 0.0, 6),
            "observation_hashes": sorted({str(r.get("observation_hash") or "") for r in accepted if r.get("observation_hash")}),
            "broker_authority": self.broker_authority,
            "claim_boundary": "CALIBRATED only means this fresh observed spread/depth sample met the versioned sample policy; it is not broker-fill proof.",
        }
        snapshot["snapshot_hash"] = self._hash(snapshot)
        return snapshot

    def validate_contract(self, value: Any, *, mode: str | None = None) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        row = dict(value)
        if str(row.get("execution_model_version") or "") != self.execution_model_version:
            return None
        if str(row.get("calibration_policy_version") or "") != self.calibration_policy_version:
            return None
        if str(row.get("calibration_state") or "") not in {"CALIBRATED", "CALIBRATION_PENDING"}:
            return None
        supplied_hash = str(row.get("contract_hash") or "")
        unsigned = dict(row)
        unsigned.pop("contract_hash", None)
        if not supplied_hash or self._hash(unsigned) != supplied_hash:
            return None
        return row

    def _validated_snapshot(self, value: Any, *, mode: str) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        row = dict(value)
        if str(row.get("execution_model_version") or "") != self.execution_model_version:
            return None
        if str(row.get("calibration_policy_version") or "") != self.calibration_policy_version:
            return None
        if str(row.get("mode") or "").lower() != str(mode or "").lower():
            return None
        supplied_hash = str(row.get("snapshot_hash") or "")
        unsigned = dict(row)
        unsigned.pop("snapshot_hash", None)
        if not supplied_hash or self._hash(unsigned) != supplied_hash:
            return None
        return row

    def contract(
        self,
        candidate: Mapping[str, Any] | None,
        *,
        mode: str,
        quantity: int,
        schedule_slippage_bps: float,
    ) -> dict[str, Any]:
        row = dict(candidate or {})
        quote = row.get("selected_quote") if isinstance(row.get("selected_quote"), Mapping) else row.get("quote") if isinstance(row.get("quote"), Mapping) else row
        observation = self.observe(quote, quantity=quantity)
        snapshot = self._validated_snapshot(row.get("execution_calibration_snapshot"), mode=mode)
        calibrated = bool(snapshot and snapshot.get("state") == "CALIBRATED")

        explicit_spread = self._number(row.get("spread_bps"))
        current_spread = explicit_spread if explicit_spread is not None else self._number(observation.get("spread_bps"))
        current_impact = max(
            self._number(row.get("market_impact_bps")) or self._number(row.get("impact_bps")) or 0.0,
            self._number(observation.get("buy_depth_impact_bps")) or 0.0,
            self._number(observation.get("sell_depth_impact_bps")) or 0.0,
        )
        empirical_spread = self._number((snapshot or {}).get("spread_p90_bps")) or 0.0
        empirical_impact = self._number((snapshot or {}).get("depth_impact_p90_bps_each_side")) or 0.0
        spread_reserve = max(0.0, current_spread or 0.0, empirical_spread if calibrated else 0.0)
        impact_reserve = max(0.0, current_impact, empirical_impact if calibrated else 0.0)
        slippage_reserve = max(0.0, float(schedule_slippage_bps or 0.0))
        contract = {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "execution_model_version": self.execution_model_version,
            "calibration_policy_version": self.calibration_policy_version,
            "calibration_state": "CALIBRATED" if calibrated else "CALIBRATION_PENDING",
            "calibration_snapshot_hash": (snapshot or {}).get("snapshot_hash"),
            "calibration_sample_count": int((snapshot or {}).get("sample_count") or 0),
            "calibration_instrument_count": int((snapshot or {}).get("instrument_count") or 0),
            "slippage_bps_each_side": round(slippage_reserve, 6),
            "spread_bps_round_trip": round(spread_reserve, 6),
            "impact_bps_each_side": round(impact_reserve, 6),
            "current_observation_hash": observation.get("observation_hash") if observation.get("usable_for_calibration") else None,
            "current_spread_bps": current_spread,
            "current_depth_impact_bps_each_side": current_impact,
            "empirical_spread_p90_bps": empirical_spread if calibrated else None,
            "empirical_depth_impact_p90_bps_each_side": empirical_impact if calibrated else None,
            "broker_authority": self.broker_authority,
            "live_empirical_acceptance": calibrated,
            "claim_boundary": "CALIBRATION_PENDING is conservative reserve-only evidence and must not be reported as empirically calibrated.",
        }
        contract["contract_hash"] = self._hash(contract)
        return contract


DEFAULT_EXECUTION_SLIPPAGE_CALIBRATION_AUTHORITY = ExecutionSlippageCalibrationAuthority()
