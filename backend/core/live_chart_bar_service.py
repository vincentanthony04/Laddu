from __future__ import annotations

"""Read-only projection of backend-formed live OHLCV bars for the chart."""

from datetime import datetime, timezone
from typing import Any, Dict

from core.canonical_presentation_service import CanonicalPresentationService
from core.db_utils import canonical_interval
from core.runtime_primitives import is_india_market_open


class LiveChartBarService:
    VERSION = "canonical-live-chart-bar-1.1.0-live-quote"
    ALLOWED = {"1m", "3m", "5m", "15m", "30m", "60m", "240m"}

    def __init__(self, app: Any):
        self.app = app
        self.presentation = CanonicalPresentationService(app.store)

    def read(self, symbol_or_key: str, interval: str) -> Dict[str, Any]:
        identity = self.presentation.resolve(symbol_or_key)
        norm = canonical_interval(interval)
        now = datetime.now(timezone.utc).isoformat()
        if not identity.ok or not identity.instrument_key:
            return {"ok": False, "state": "IDENTITY_UNAVAILABLE", "interval": norm, "forming_bar": None}
        market_open = is_india_market_open()
        quote = {}
        gateway_quotes = getattr(getattr(self.app, "live_market", None), "quotes", None)
        if gateway_quotes is not None and callable(getattr(gateway_quotes, "snapshot", None)):
            quote = dict((gateway_quotes.snapshot(
                [identity.symbol], market_open=market_open, max_age_sec=8.0
            ) or {}).get(identity.symbol, {}) or {})
        if not quote:
            quotes = list(self.app.runtime_market_state.latest_quotes([identity.symbol]) or [])
            quote = dict(quotes[-1] or {}) if quotes else {}
        freshness = str(quote.get("freshness_state") or "").upper()
        quote_state = "LIVE" if freshness == "LIVE" and market_open else "CLOSED" if not market_open and quote else "STALE" if quote else "WARMING"
        if norm not in self.ALLOWED:
            return {
                "ok": bool(quote), "state": quote_state, "service_version": self.VERSION,
                "authority": "HOT_RUNTIME_MARKET_STATE", "symbol": identity.symbol,
                "instrument_key": identity.instrument_key, "interval": norm,
                "forming_bar": None, "last_runtime_bar": None, "quote": quote,
                "quote_freshness": quote.get("freshness_state"),
                "provider_timestamp": quote.get("provider_timestamp"),
                "canonical_sequence": quote.get("canonical_sequence"),
                "server_time": now,
                "ultra_scalp_chart_ready": False,
                "policy": "Live quote projection is available for every chart timeframe; forming OHLCV exists only for supported intraday intervals.",
            }
        rows = list(self.app.runtime_market_state.canonical_bars(
            identity.instrument_key, norm, limit=2, include_forming=True
        ) or [])
        forming = next((dict(row) for row in reversed(rows) if row.get("forming") is True), None)
        last = dict(rows[-1]) if rows else None
        state = "LIVE" if forming and freshness == "LIVE" else "CLOSED" if not market_open and (rows or quote) else "STALE" if rows or quote else "WARMING"
        return {
            "ok": bool(rows or quote), "state": state, "service_version": self.VERSION,
            "authority": "HOT_RUNTIME_MARKET_STATE", "symbol": identity.symbol,
            "instrument_key": identity.instrument_key, "interval": norm,
            "forming_bar": forming, "last_runtime_bar": last, "quote": quote,
            "quote_freshness": quote.get("freshness_state"),
            "provider_timestamp": quote.get("provider_timestamp"),
            "canonical_sequence": quote.get("canonical_sequence"),
            "server_time": now,
            "ultra_scalp_chart_ready": bool(forming and freshness == "LIVE" and forming.get("tick_count", 0) > 0),
            "policy": "Quote and OHLCV are projected from ordered, identity-verified hot runtime observations; the browser performs no market-data aggregation.",
        }
