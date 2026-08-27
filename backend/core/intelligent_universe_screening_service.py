"""Cheap full-universe screening authority for scarce Intraday analysis capacity.

This service is deliberately *not* a trade-selection or promotion authority.
It answers only one operational question: which currently observable cash
securities deserve expensive analysis first?

The complete immutable Intraday cash-equity snapshot is the input population.
Every row is cheaply classified from already-ingested quote/freshness evidence,
trailing-liquidity rank, and protected operator priority.  The resulting
``screening_score`` is an analysis-scheduling score only; Evidence Engine,
risk admission, Model Paper and Final promotion remain separate authorities.
"""
from __future__ import annotations

from collections import Counter
from math import log10
from typing import Any, Dict, Iterable, Mapping

from config import MIN_ELIGIBLE_PRICE_INR


SCREENING_VERSION = "intelligent-universe-screening-1.0.0"


def _symbol(row: Mapping[str, Any]) -> str:
    return str(row.get("trading_symbol") or row.get("symbol") or "").upper().strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "", "—") else float(default)
    except (TypeError, ValueError):
        return float(default)


class IntelligentUniverseScreeningService:
    """Classify the whole observable cash universe before deep analysis.

    Hard screening rules are intentionally limited to evidence/tradability
    requirements that are cheap and deterministic.  Activity, liquidity rank,
    spread and explicit priority affect scheduling order only; they can never
    create trade conviction or bypass downstream admission gates.
    """

    def __init__(self, *, min_price: float = MIN_ELIGIBLE_PRICE_INR):
        self.min_price = max(0.0, float(min_price))

    @staticmethod
    def _quote_map(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for source in rows or []:
            row = dict(source or {})
            symbol = _symbol(row)
            if not symbol:
                continue
            previous = out.get(symbol)
            current_verified = str(row.get("freshness_state") or "") in {"live", "closed_market"}
            previous_verified = str((previous or {}).get("freshness_state") or "") in {"live", "closed_market"}
            if previous is None or current_verified or not previous_verified:
                out[symbol] = row
        return out

    @staticmethod
    def _spread_bps(quote: Mapping[str, Any], ltp: float) -> float | None:
        bid = _number(quote.get("best_bid") or quote.get("bid_price") or quote.get("bid"), 0.0)
        ask = _number(quote.get("best_ask") or quote.get("ask_price") or quote.get("ask"), 0.0)
        if bid <= 0 or ask <= 0 or ask < bid or ltp <= 0:
            return None
        mid = (bid + ask) / 2.0
        return ((ask - bid) / mid * 10_000.0) if mid > 0 else None

    def classify(
        self,
        universe_rows: Iterable[Mapping[str, Any]],
        quote_rows: Iterable[Mapping[str, Any]],
        *,
        liquidity_rank_by_symbol: Mapping[str, int] | None = None,
        priority_symbols: Iterable[str] | None = None,
        market_open: bool,
    ) -> Dict[str, Any]:
        quotes = self._quote_map(quote_rows)
        liquidity_rank = {
            str(symbol or "").upper().strip(): max(1, int(rank))
            for symbol, rank in dict(liquidity_rank_by_symbol or {}).items()
            if str(symbol or "").strip()
        }
        protected = {str(value or "").upper().strip() for value in (priority_symbols or []) if str(value or "").strip()}
        rank_span = max(liquidity_rank.values(), default=1)

        eligible: list[Dict[str, Any]] = []
        rejected: list[Dict[str, Any]] = []
        pending: list[Dict[str, Any]] = []
        reasons = Counter()
        pending_reasons = Counter()
        total = observed = 0

        for source in universe_rows or []:
            instrument = dict(source or {})
            symbol = _symbol(instrument)
            key = str(instrument.get("instrument_key") or "").strip()
            if not symbol:
                continue
            total += 1
            quote = dict(quotes.get(symbol) or {})
            data_reasons: list[str] = []
            filter_reasons: list[str] = []
            if not key:
                filter_reasons.append("INSTRUMENT_IDENTITY_MISSING")
            if not quote:
                data_reasons.append("QUOTE_NOT_OBSERVED")
            else:
                observed += 1
                expected_freshness = "live" if market_open else "closed_market"
                freshness = str(quote.get("freshness_state") or "unverified")
                if freshness != expected_freshness:
                    data_reasons.append("QUOTE_NOT_LIVE" if market_open else "COMPLETED_SESSION_QUOTE_NOT_VERIFIED")
                if quote.get("identity_verified") is not True:
                    data_reasons.append("QUOTE_IDENTITY_UNVERIFIED")
            ltp = _number(quote.get("ltp"), 0.0)
            if quote and ltp <= 0:
                data_reasons.append("PRICE_UNAVAILABLE")
            elif quote and ltp < self.min_price:
                filter_reasons.append("PRICE_BELOW_SCREEN_FLOOR")

            volume = max(0.0, _number(quote.get("volume") or quote.get("total_traded_volume"), 0.0))
            liq_rank = liquidity_rank.get(symbol)
            # Current activity OR a governed trailing-liquidity observation is
            # enough for analysis scheduling. Protected/manual symbols may be
            # inspected even when the liquidity evidence is still accumulating;
            # downstream risk/admission remains fail-closed.
            if quote and not data_reasons and volume <= 0 and liq_rank is None and symbol not in protected:
                filter_reasons.append("LIQUIDITY_EVIDENCE_MISSING")

            if data_reasons:
                unique = sorted(set(data_reasons))
                pending_reasons.update(unique)
                pending.append({
                    "symbol": symbol,
                    "instrument_key": key or None,
                    "screening_state": "PENDING_CURRENT_EVIDENCE",
                    "screening_reasons": unique,
                    "screening_version": SCREENING_VERSION,
                })
                continue
            if filter_reasons:
                unique = sorted(set(filter_reasons))
                reasons.update(unique)
                rejected.append({
                    "symbol": symbol,
                    "instrument_key": key or None,
                    "screening_state": "FILTERED_OUT",
                    "screening_reasons": unique,
                    "screening_version": SCREENING_VERSION,
                })
                continue

            movement = abs(_number(quote.get("change_pct") or quote.get("pChange"), 0.0))
            activity_points = min(30.0, movement * 6.0)
            volume_points = min(25.0, log10(volume + 1.0) * 4.0) if volume > 0 else 0.0
            liquidity_points = 0.0
            if liq_rank is not None:
                liquidity_points = 25.0 * (1.0 - ((liq_rank - 1) / max(1, rank_span)))
            spread = self._spread_bps(quote, ltp)
            spread_points = 0.0
            if spread is not None:
                spread_points = 10.0 if spread <= 20.0 else 5.0 if spread <= 35.0 else -10.0 if spread > 50.0 else 0.0
            protected_points = 10.0 if symbol in protected else 0.0
            score = max(0.0, min(100.0, activity_points + volume_points + liquidity_points + spread_points + protected_points))

            merged = dict(instrument, **quote)
            merged.update({
                "symbol": symbol,
                "trading_symbol": instrument.get("trading_symbol") or symbol,
                "instrument_key": key,
                "screening_state": "ELIGIBLE_FOR_ANALYSIS_RANKING",
                "screening_score": round(score, 4),
                # SelectionFairnessService consumes priority_score only to
                # schedule scarce analysis capacity.  It is not trade confidence.
                "priority_score": round(score, 4),
                "screening_score_breakdown": {
                    "current_movement": round(activity_points, 4),
                    "current_volume": round(volume_points, 4),
                    "trailing_liquidity_rank": round(liquidity_points, 4),
                    "observed_spread": round(spread_points, 4),
                    "protected_priority": round(protected_points, 4),
                    "trade_confidence_affected": False,
                },
                "trailing_liquidity_rank": liq_rank,
                "spread_bps": round(spread, 4) if spread is not None else None,
                "screening_version": SCREENING_VERSION,
                "source": "full_universe_intelligent_screen",
            })
            eligible.append(merged)

        eligible.sort(key=lambda row: (-float(row.get("screening_score") or 0.0), row["symbol"]))
        return {
            "version": SCREENING_VERSION,
            "population_count": total,
            "observed_count": observed,
            "eligible_count": len(eligible),
            "rejected_count": len(rejected),
            "pending_count": len(pending),
            "rejection_reasons": dict(sorted(reasons.items())),
            "pending_reasons": dict(sorted(pending_reasons.items())),
            "eligible_rows": eligible,
            "rejected_rows": rejected,
            "pending_rows": pending,
            "policy": "whole canonical intraday cash universe; cheap evidence/tradability screen only; no promotion authority",
            "trade_confidence_affected": False,
        }
