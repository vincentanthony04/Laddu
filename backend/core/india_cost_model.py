"""Versioned Indian cash-equity transaction-cost models by trading desk.

Rates are configurable estimates, not tax advice.  Intraday and Delivery have
materially different STT, stamp-duty, brokerage and demat treatment, therefore
one generic cost profile must never be shared between their backtests.

The 2026 defaults follow the connected Upstox cash-equity tariff structure and
remain overrideable through environment variables.  Every result carries the
full config and version so a historical report cannot silently change later.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import os
from typing import Any, Dict

from core.india_cash_cost_service import IndiaCashCostSchedule, IndiaCashCostService


@dataclass(frozen=True)
class IndiaCashCostConfig:
    profile: str = "delivery"
    exchange: str = "NSE"
    bse_group: str | None = None
    version: str = "india-equity-delivery-2026-04-01"
    effective_from: str = "2026-04-01"
    stt_buy_pct: float = 0.10
    stt_sell_pct: float = 0.10
    stamp_buy_pct: float = 0.015
    exchange_each_side_pct: float = 0.00307
    sebi_each_side_pct: float = 0.0001
    ipft_each_side_pct: float = 0.0
    gst_pct: float = 18.0
    brokerage_pct_per_order: float = 2.5
    brokerage_flat_per_order: float = 0.0
    brokerage_cap_per_order: float = 20.0
    dp_sell_flat: float = 20.0
    slippage_each_side_pct: float = 0.05

    @classmethod
    def for_mode(cls, mode: str, *, exchange: str = "NSE", bse_group: str | None = None) -> "IndiaCashCostConfig":
        desk = str(mode or "delivery").strip().lower()
        venue = str(exchange or "NSE").strip().upper()
        if venue not in {"NSE", "BSE"}:
            raise ValueError("cash-equity exchange must be NSE or BSE")
        group = str(bse_group or "").strip().upper() or None
        if venue == "BSE" and not group:
            raise ValueError("BSE cash cost requires scrip group")
        if desk == "intraday":
            return cls(
                profile="intraday", exchange=venue, bse_group=group,
                version=(f"upstox-bse-cash-2026-03-01-group-{group}-v1" if venue == "BSE" else "upstox-nse-cash-2026-03-01-v1"),
                effective_from="2026-03-01",
                stt_buy_pct=0.0,
                stt_sell_pct=0.025,
                stamp_buy_pct=0.003,
                exchange_each_side_pct=0.00307,
                brokerage_pct_per_order=0.10,
                brokerage_flat_per_order=0.0,
                brokerage_cap_per_order=20.0,
                dp_sell_flat=0.0,
            )
        if desk != "delivery":
            raise ValueError("cash-equity cost mode must be intraday or delivery")
        return cls(
            profile="delivery", exchange=venue, bse_group=group,
            version=(f"upstox-bse-cash-2026-03-01-group-{group}-v1" if venue == "BSE" else "upstox-nse-cash-2026-03-01-v1"),
            effective_from="2026-03-01",
            brokerage_pct_per_order=2.5,
            brokerage_flat_per_order=0.0,
            brokerage_cap_per_order=20.0,
            exchange_each_side_pct=0.00307,
        )

    @classmethod
    def from_env(cls, mode: str = "delivery", *, exchange: str = "NSE", bse_group: str | None = None) -> "IndiaCashCostConfig":
        base = cls.for_mode(mode, exchange=exchange, bse_group=bse_group)
        prefix = "PROJECT_LADDU_INTRADAY_" if base.profile == "intraday" else "PROJECT_LADDU_DELIVERY_"

        def rate(name: str, default: float) -> float:
            raw = os.environ.get(prefix + name, os.environ.get("PROJECT_LADDU_" + name, str(default)))
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default

        return replace(
            base,
            version=os.environ.get(prefix + "COST_VERSION", os.environ.get("PROJECT_LADDU_COST_VERSION", base.version)),
            effective_from=os.environ.get(prefix + "COST_EFFECTIVE_FROM", os.environ.get("PROJECT_LADDU_COST_EFFECTIVE_FROM", base.effective_from)),
            stt_buy_pct=rate("STT_BUY_PCT", base.stt_buy_pct),
            stt_sell_pct=rate("STT_SELL_PCT", base.stt_sell_pct),
            stamp_buy_pct=rate("STAMP_BUY_PCT", base.stamp_buy_pct),
            exchange_each_side_pct=rate("EXCHANGE_PCT", base.exchange_each_side_pct),
            sebi_each_side_pct=rate("SEBI_PCT", base.sebi_each_side_pct),
            ipft_each_side_pct=rate("IPFT_PCT", base.ipft_each_side_pct),
            gst_pct=rate("GST_PCT", base.gst_pct),
            brokerage_pct_per_order=rate("BROKERAGE_PCT", base.brokerage_pct_per_order),
            brokerage_flat_per_order=rate("BROKERAGE_FLAT", base.brokerage_flat_per_order),
            brokerage_cap_per_order=rate("BROKERAGE_CAP", base.brokerage_cap_per_order),
            dp_sell_flat=rate("DP_SELL_FLAT", base.dp_sell_flat),
            slippage_each_side_pct=rate("SLIPPAGE_PCT", base.slippage_each_side_pct),
        )


class IndiaCashCostModel:
    def __init__(self, config: IndiaCashCostConfig | None = None, *, mode: str = "delivery", exchange: str = "NSE", bse_group: str | None = None):
        self.config = config or IndiaCashCostConfig.from_env(mode, exchange=exchange, bse_group=bse_group)

    @classmethod
    def for_mode(cls, mode: str, *, exchange: str = "NSE", bse_group: str | None = None) -> "IndiaCashCostModel":
        return cls(mode=mode, exchange=exchange, bse_group=bse_group)

    @classmethod
    def for_evidence(cls, mode: str, evidence: Dict[str, Any] | None = None) -> "IndiaCashCostModel":
        """Resolve the cost model from canonical listing evidence.

        NSE remains the normal primary listing.  A genuine BSE-only fallback
        must carry its BSE scrip group so the variable exchange tariff can be
        costed; missing group evidence fails closed rather than silently using
        NSE charges.
        """
        row = dict(evidence or {})
        exchange = str(
            row.get("exchange") or row.get("listing_exchange") or row.get("primary_exchange") or "NSE"
        ).strip().upper()
        if exchange.startswith("NSE"):
            exchange = "NSE"
        elif exchange.startswith("BSE"):
            exchange = "BSE"
        group = row.get("bse_group") or row.get("series") or row.get("scrip_group") or row.get("group")
        return cls.for_mode(mode, exchange=exchange, bse_group=group if exchange == "BSE" else None)

    @staticmethod
    def _pct(value: float, rate: float) -> float:
        return value * rate / 100.0

    def _brokerage(self, turnover: float) -> float:
        percentage = self._pct(turnover, self.config.brokerage_pct_per_order)
        cap = max(0.0, self.config.brokerage_cap_per_order)
        return min(percentage, cap) if cap else percentage

    def _canonical_service(self) -> IndiaCashCostService:
        """Translate the compatibility config into the single cost arithmetic authority.

        IndiaCashCostModel is retained because many research/live callers depend
        on its higher-level APIs.  It no longer owns fee arithmetic.
        """
        cfg = self.config
        schedule = IndiaCashCostSchedule(
            version=cfg.version,
            effective_from=cfg.effective_from,
            brokerage_cap=cfg.brokerage_cap_per_order,
            intraday_brokerage_rate=(cfg.brokerage_pct_per_order / 100.0) if cfg.profile == "intraday" else 0.001,
            delivery_brokerage_rate=(cfg.brokerage_pct_per_order / 100.0) if cfg.profile == "delivery" else 0.025,
            delivery_brokerage_flat=cfg.brokerage_flat_per_order if cfg.profile == "delivery" else 0.0,
            delivery_stt_rate=(cfg.stt_buy_pct / 100.0) if cfg.profile == "delivery" else 0.001,
            intraday_sell_stt_rate=(cfg.stt_sell_pct / 100.0) if cfg.profile == "intraday" else 0.00025,
            nse_transaction_rate=cfg.exchange_each_side_pct / 100.0,
            nse_ipft_rate=(cfg.ipft_each_side_pct / 100.0) if cfg.ipft_each_side_pct > 0 else 0.000000001,
            bse_ipft_rate=(cfg.ipft_each_side_pct / 100.0) if cfg.ipft_each_side_pct > 0 else 0.000000001,
            sebi_rate=cfg.sebi_each_side_pct / 100.0,
            delivery_buy_stamp_rate=(cfg.stamp_buy_pct / 100.0) if cfg.profile == "delivery" else 0.00015,
            intraday_buy_stamp_rate=(cfg.stamp_buy_pct / 100.0) if cfg.profile == "intraday" else 0.00003,
            gst_rate=cfg.gst_pct / 100.0,
            delivery_dp_sell=cfg.dp_sell_flat if cfg.profile == "delivery" else 20.0,
            intraday_slippage_bps=(cfg.slippage_each_side_pct * 100.0) if cfg.profile == "intraday" else 3.0,
            delivery_slippage_bps=(cfg.slippage_each_side_pct * 100.0) if cfg.profile == "delivery" else 5.0,
        )
        return IndiaCashCostService(schedule=schedule)

    def round_trip(self, buy_price: float, sell_price: float, quantity: int) -> Dict[str, Any]:
        q = max(0, int(quantity))
        if q <= 0:
            return {
                "config": asdict(self.config), "profile": self.config.profile,
                "buy_value": 0.0, "sell_value": 0.0, "gross_pnl": 0.0,
                "costs": {k: 0.0 for k in ("brokerage","exchange","sebi","ipft","dp","gst","stt","stamp","slippage","total")},
                "net_pnl": 0.0, "net_return_pct": 0.0,
                "cost_authority": "IndiaCashCostAuthority",
                "cost_authority_version": "1.2.0",
            }
        buy_px, sell_px = max(0.0, float(buy_price)), max(0.0, float(sell_price))
        report = self._canonical_service().round_trip(
            self.config.profile, "LONG", buy_px, sell_px, q,
            exchange=self.config.exchange, bse_group=self.config.bse_group,
        )
        raw = dict(report["costs"])
        costs = {
            "brokerage": raw.get("brokerage", 0.0),
            "exchange": raw.get("exchange_transaction", 0.0),
            "sebi": raw.get("sebi_fee", 0.0),
            "ipft": raw.get("ipft", 0.0),
            "dp": raw.get("dp_charge", 0.0),
            "gst": raw.get("gst", 0.0),
            "stt": raw.get("stt", 0.0),
            "stamp": raw.get("stamp_duty", 0.0),
            "slippage": raw.get("slippage", 0.0),
            "total": raw.get("total", 0.0),
        }
        buy = buy_px * q
        sell = sell_px * q
        return {
            "config": asdict(self.config),
            "profile": self.config.profile,
            "buy_value": round(buy, 2),
            "sell_value": round(sell, 2),
            "gross_pnl": report["gross_pnl"],
            "costs": costs,
            "net_pnl": report["net_pnl"],
            "net_return_pct": round(report["net_pnl"] / buy * 100, 4) if buy else 0.0,
            "cost_authority": "IndiaCashCostAuthority",
            "cost_authority_version": report.get("cost_authority_version") or "1.2.0",
            "tariff_schedule_version": report.get("tariff_schedule_version"),
            "execution_assumption_version": report.get("execution_assumption_version"),
        }

    def breakeven_exit(
        self,
        *,
        entry: float,
        side: str,
        quantity: int = 100,
        iterations: int = 64,
    ) -> Dict[str, Any]:
        """Solve the cost-adjusted exit price whose estimated net P&L is zero.

        This is the canonical breakeven authority for position lifecycle
        management.  It uses the same desk-specific taxes, brokerage, DP and
        slippage assumptions as every other live cost calculation.
        """
        entry_f = float(entry)
        q = max(1, int(quantity or 100))
        direction = str(side or "").strip().upper()
        if entry_f <= 0:
            raise ValueError("entry must be positive")
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")

        def estimate(exit_price: float) -> Dict[str, Any]:
            return self.round_trip(exit_price, entry_f, q) if direction == "SHORT" else self.round_trip(entry_f, exit_price, q)

        if direction == "LONG":
            low, high = entry_f, entry_f * 1.25
            while estimate(high)["net_pnl"] < 0:
                high *= 1.25
            for _ in range(max(16, int(iterations))):
                mid = (low + high) / 2.0
                if estimate(mid)["net_pnl"] >= 0:
                    high = mid
                else:
                    low = mid
            price = high
        else:
            low, high = max(0.01, entry_f * 0.75), entry_f
            while estimate(low)["net_pnl"] < 0 and low > 0.011:
                low = max(0.01, low * 0.75)
            for _ in range(max(16, int(iterations))):
                mid = (low + high) / 2.0
                if estimate(mid)["net_pnl"] >= 0:
                    low = mid
                else:
                    high = mid
            price = low

        result = estimate(price)
        return {
            "profile": self.config.profile,
            "cost_version": self.config.version,
            "side": direction,
            "entry": round(entry_f, 6),
            "quantity": q,
            "breakeven_exit": round(price, 6),
            "breakeven_move_points": round(abs(price - entry_f), 6),
            "estimated_cost": result["costs"]["total"],
            "estimated_net_pnl": result["net_pnl"],
            "cost_breakdown": result["costs"],
        }

    def post_cost_rr(
        self,
        *,
        entry: float,
        stop: float,
        target: float,
        side: str,
        quantity: int | None = None,
        assumed_notional: float = 100000.0,
        spread_bps: float | None = None,
    ) -> Dict[str, Any]:
        """Return a desk-aware, itemised post-cost reward-to-risk estimate.

        Reward and stop paths are costed separately because taxes, brokerage,
        DP charges and percentage slippage depend on the actual exit price.
        This method is the single authority used by both the live engine and
        the downstream execution-quality veto.
        """
        entry = float(entry)
        stop = float(stop)
        target = float(target)
        if entry <= 0 or stop <= 0 or target <= 0:
            raise ValueError("entry, stop and target must be positive")
        direction = str(side or "").strip().upper()
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        q = int(quantity or 0)
        if q <= 0:
            q = max(1, int(max(1.0, float(assumed_notional)) // entry))

        if direction == "LONG":
            reward_estimate = self.round_trip(entry, target, q)
            stop_estimate = self.round_trip(entry, stop, q)
        else:
            # A short cash-equity round trip sells at entry and buys back at
            # target/stop. round_trip expects buy then sell values, so reverse
            # the price arguments while keeping the same tax model.
            reward_estimate = self.round_trip(target, entry, q)
            stop_estimate = self.round_trip(stop, entry, q)

        reward_cost_per_share = float(reward_estimate["costs"]["total"]) / q
        stop_cost_per_share = float(stop_estimate["costs"]["total"]) / q
        spread_cost_per_share = entry * max(0.0, float(spread_bps or 0.0)) / 10000.0
        gross_reward = abs(target - entry)
        gross_risk = abs(entry - stop)
        net_reward = gross_reward - reward_cost_per_share - spread_cost_per_share
        net_risk = gross_risk + stop_cost_per_share + spread_cost_per_share
        post_cost_rr = max(0.0, net_reward) / net_risk if net_risk > 0 else 0.0
        return {
            "profile": self.config.profile,
            "cost_version": self.config.version,
            "assumed_quantity": q,
            "gross_reward_points": round(gross_reward, 6),
            "gross_risk_points": round(gross_risk, 6),
            "reward_exit_cost_per_share": round(reward_cost_per_share, 6),
            "stop_exit_cost_per_share": round(stop_cost_per_share, 6),
            "spread_cost_per_share": round(spread_cost_per_share, 6),
            "post_cost_reward_points": round(net_reward, 6),
            "post_cost_risk_points": round(net_risk, 6),
            "post_cost_rr": round(post_cost_rr, 6),
            "reward_cost_breakdown": reward_estimate["costs"],
            "stop_cost_breakdown": stop_estimate["costs"],
        }

