"""Canonical post-exit follow-through projection for settled Model Paper trades.

This service is deliberately observational.  It never changes the immutable
settlement result.  It resolves the exact instrument, reads only persisted
canonical candles, constructs explicit post-exit horizon evidence, and delegates
classification to FollowThroughOutcomeAuthority.
"""
from __future__ import annotations

from datetime import datetime, timedelta, time as clock_time, timezone
import math
from typing import Any, Iterable, Mapping

from core.follow_through_outcome_authority import DEFAULT_FOLLOW_THROUGH_OUTCOME_AUTHORITY
from core.india_time import INDIA_TZ
from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY


class FollowThroughProjectionService:
    VERSION = "follow-through-projection-1.0.0-canonical-candle-causal"

    def __init__(self, app: Any):
        self.app = app

    @staticmethod
    def _dt(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            out = value
        else:
            try:
                out = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            except Exception:
                return None
        if out.tzinfo is None:
            out = out.replace(tzinfo=timezone.utc)
        return out.astimezone(INDIA_TZ)

    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) else None

    @classmethod
    def _candle(cls, raw: Mapping[str, Any], *, minutes: int | None = None) -> dict[str, Any] | None:
        start = cls._dt(raw.get("timestamp") or raw.get("bar_start_ts") or raw.get("time") or raw.get("date") or raw.get("ts"))
        close = cls._number(raw.get("close"))
        if start is None or close is None or close <= 0:
            return None
        end = start + timedelta(minutes=minutes) if minutes else start
        return {"start": start, "end": end, "close": close, "raw": dict(raw)}

    def _instrument_key(self, row: Mapping[str, Any]) -> str | None:
        direct = str(row.get("instrument_key") or row.get("provider_instrument_key") or "").strip()
        if direct:
            return direct
        symbol = str(row.get("symbol") or row.get("trading_symbol") or "").strip().upper()
        if not symbol:
            return None
        resolver = getattr(self.app, "instrument_resolver", None)
        resolve = getattr(resolver, "resolve", None)
        if not callable(resolve):
            return None
        try:
            resolved = resolve(symbol, prefer_index=False)
        except Exception:
            return None
        if not isinstance(resolved, Mapping):
            return None
        key = str(resolved.get("instrument_key") or "").strip()
        return key or None

    def _stored(self, key: str, interval: str, limit: int) -> list[Mapping[str, Any]]:
        market_data = getattr(self.app, "market_data", None)
        reader = getattr(market_data, "stored_candles", None)
        if not callable(reader):
            return []
        try:
            return [dict(x or {}) for x in (reader(key, interval, limit=limit) or []) if isinstance(x, Mapping)]
        except Exception:
            return []

    def _intraday_horizons(self, key: str, closed_at: datetime, *, now: datetime) -> dict[str, Any]:
        rows = [self._candle(raw, minutes=1) for raw in self._stored(key, "1minute", 5000)]
        bars = sorted((x for x in rows if x is not None), key=lambda x: x["start"])
        session_date = closed_at.astimezone(INDIA_TZ).date()
        bars = [x for x in bars if x["start"].date() == session_date and x["end"] <= now]
        out: dict[str, Any] = {}
        for label, minutes in (("15m", 15), ("30m", 30), ("60m", 60)):
            target = closed_at + timedelta(minutes=minutes)
            # Exact one-minute evidence only: a later sparse bar may not stand
            # in for a missing horizon observation.
            match = next((x for x in bars if target <= x["end"] <= target + timedelta(minutes=1)), None)
            if match is None:
                out[label] = {"complete": False, "reason": "exact_completed_post_exit_bar_pending"}
            else:
                out[label] = {
                    "complete": True,
                    "price": match["close"],
                    "observed_at": match["end"].isoformat(),
                    "source": "CANONICAL_STORED_1MINUTE_CANDLE",
                }
        session_close = datetime.combine(session_date, clock_time(15, 30), tzinfo=INDIA_TZ)
        close_bar = next((x for x in reversed(bars) if x["end"] <= session_close and x["end"] >= session_close - timedelta(minutes=2)), None)
        if now >= session_close and close_bar is not None and close_bar["end"] > closed_at:
            out["close"] = {
                "complete": True,
                "price": close_bar["close"],
                "observed_at": close_bar["end"].isoformat(),
                "source": "CANONICAL_STORED_SESSION_CLOSE_CANDLE",
            }
        else:
            out["close"] = {"complete": False, "reason": "session_close_evidence_pending"}
        return out

    def _delivery_horizons(self, key: str, closed_at: datetime, *, now: datetime) -> dict[str, Any]:
        parsed = [self._candle(raw) for raw in self._stored(key, "day", 200)]
        by_date: dict[Any, dict[str, Any]] = {}
        for item in sorted((x for x in parsed if x is not None), key=lambda x: x["start"]):
            d = item["start"].date()
            if d > closed_at.date():
                by_date[d] = item
        sessions = DEFAULT_TRADING_SESSION_AUTHORITY
        out: dict[str, Any] = {}
        expected_dates=[]
        cursor=closed_at.date()
        try:
            for _ in range(20):
                cursor=sessions.next_trading_day(cursor)
                expected_dates.append(cursor)
        except Exception:
            expected_dates=[]
        for label, count in (("1D", 1), ("3D", 3), ("5D", 5), ("10D", 10), ("20D", 20)):
            if len(expected_dates) < count:
                out[label] = {"complete": False, "reason": "trading_calendar_horizon_unavailable"}
                continue
            required=expected_dates[:count]
            missing=[d.isoformat() for d in required if d not in by_date]
            if missing:
                out[label] = {"complete": False, "reason": "complete_expected_trading_day_sequence_required", "missing_sessions": missing}
                continue
            item=by_date[required[-1]]
            observed_at = datetime.combine(required[-1], clock_time(15, 30), tzinfo=INDIA_TZ)
            if observed_at > now or observed_at <= closed_at:
                out[label] = {"complete": False, "reason": "causal_completed_trading_day_required"}
                continue
            out[label] = {
                "complete": True,
                "price": item["close"],
                "observed_at": observed_at.isoformat(),
                "source": "CANONICAL_STORED_DAILY_CANDLE",
                "trading_sessions": [d.isoformat() for d in required],
            }
        return out

    @staticmethod
    def _ids(row: Mapping[str, Any]) -> set[str]:
        return {
            str(value).strip()
            for value in (
                row.get("decision_id"), row.get("position_decision_id"), row.get("source_decision_id"),
                row.get("source_signal_id"), row.get("signal_id"),
            )
            if str(value or "").strip()
        }

    def enrich_record(self, lifecycle_row: Mapping[str, Any], settled_row: Mapping[str, Any] | None = None, *, now: datetime | None = None) -> dict[str, Any]:
        base = dict(lifecycle_row or {})
        settled = dict(settled_row or {})
        merged = {**settled, **base}
        # The canonical lifecycle geometry wins; Model Paper settlement fills
        # fields that the canonical decision projection does not expose.
        merged.setdefault("entry_price", base.get("entry") if base.get("entry") is not None else settled.get("entry_price"))
        merged.setdefault("original_entry", base.get("entry") if base.get("entry") is not None else settled.get("original_entry"))
        merged.setdefault("original_stop", base.get("stop") if base.get("stop") is not None else settled.get("original_stop"))
        merged.setdefault("exit_price", base.get("exit") if base.get("exit") is not None else settled.get("exit_price"))
        merged.setdefault("exit_reason", base.get("exit_reason") or base.get("result") or settled.get("exit_reason"))
        merged.setdefault("closed_at", base.get("closed_at") or settled.get("closed_at"))
        merged.setdefault("side", base.get("side") or settled.get("side"))
        merged.setdefault("mode", base.get("mode") or settled.get("mode"))
        merged.setdefault("symbol", base.get("symbol") or settled.get("symbol"))

        closed = self._dt(merged.get("closed_at"))
        current = (now or datetime.now(INDIA_TZ)).astimezone(INDIA_TZ)
        key = self._instrument_key(merged)
        if closed is None or key is None:
            block = {
                "authority": DEFAULT_FOLLOW_THROUGH_OUTCOME_AUTHORITY.authority,
                "authority_version": DEFAULT_FOLLOW_THROUGH_OUTCOME_AUTHORITY.authority_version,
                "state": "UNAVAILABLE",
                "after": None,
                "after_horizon": None,
                "reason": "settlement_time_or_instrument_identity_unavailable",
                "result_is_immutable": True,
            }
        else:
            mode = str(merged.get("mode") or "").lower()
            horizons = self._intraday_horizons(key, closed, now=current) if mode == "intraday" else self._delivery_horizons(key, closed, now=current) if mode == "delivery" else {}
            block = DEFAULT_FOLLOW_THROUGH_OUTCOME_AUTHORITY.measure(merged, horizons)
            block["instrument_key"] = key
            block["projection_version"] = self.VERSION
        base["follow_through"] = block
        base["after"] = block.get("after")
        base["after_state"] = block.get("after") or ("PENDING" if block.get("state") == "EVIDENCE_PENDING" else None)
        base["follow_through_state"] = block.get("after")
        base["after_horizon"] = block.get("after_horizon")
        base["result_is_immutable"] = True
        return base

    def enrich_lifecycle(self, lifecycle: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(lifecycle or {})
        records = [dict(x or {}) for x in (payload.get("records") or []) if isinstance(x, Mapping)]
        repository = getattr(self.app, "model_portfolio_repository", None)
        settled_rows: list[dict[str, Any]] = []
        reader = getattr(repository, "settled_learning_rows", None)
        if callable(reader):
            try:
                settled_rows = [dict(x or {}) for x in (reader(limit=10000) or []) if isinstance(x, Mapping)]
            except Exception:
                settled_rows = []
        index: dict[str, dict[str, Any]] = {}
        for row in settled_rows:
            for ident in self._ids(row):
                index.setdefault(ident, row)
        enriched=[]
        for row in records:
            match = next((index[x] for x in self._ids(row) if x in index), None)
            if row.get("settled") is True or row.get("accuracy_eligible") is True or row.get("performance_eligible") is True:
                enriched.append(self.enrich_record(row, match))
            else:
                enriched.append(row)
        payload["records"] = enriched
        payload["follow_through_projection"] = {
            "version": self.VERSION,
            "authority": DEFAULT_FOLLOW_THROUGH_OUTCOME_AUTHORITY.authority,
            "settled_rows_available": len(settled_rows),
            "records_enriched": sum(1 for x in enriched if isinstance(x.get("follow_through"), Mapping)),
            "policy": "immutable settlement result plus separate causal post-exit observation; missing horizon evidence remains pending",
        }
        return payload
