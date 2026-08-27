"""Canonical Upstox V3 live-market gateway.

The gateway is the only live quote authority inside Project Laddu.  It accepts
messages from the official Upstox ``MarketDataStreamerV3`` SDK, normalises the
V3 feed shapes, rejects late/duplicate observations and publishes small cursor-
based deltas to every consumer.  HTTP quote snapshots may be ingested as a
labelled fallback, but they never supersede a newer streaming event.

No broker order functionality exists in this module.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
import math
import threading
import time
from typing import Any, Callable, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.india_time import INDIA_TZ
from core.execution_slippage_calibration_authority import DEFAULT_EXECUTION_SLIPPAGE_CALIBRATION_AUTHORITY

LIVE_GATEWAY_VERSION = "live-market-gateway-1.3.0-live-depth-book"

# Browser consumers need only scalar quote/provenance fields. Provider response
# bodies and depth trees are deliberately excluded: retaining them in cursor
# deltas made a cold dashboard request several megabytes even for two symbols.
BROWSER_QUOTE_FIELDS: Tuple[str, ...] = (
    "instrument_key", "symbol", "exchange",
    "ltp", "old_ltp", "new_ltp", "open", "high", "low", "close",
    "previous_close", "session_close",
    "rupee_change", "change_pct", "change_source", "changed", "direction",
    "volume", "volume_traded_today", "last_traded_quantity", "oi",
    "average_traded_price", "bid_price", "bid_quantity", "ask_price",
    "ask_quantity", "total_buy_quantity", "total_sell_quantity",
    "provider_ts_ms", "provider_timestamp", "feed_ts_ms", "feed_timestamp",
    "received_ts_ms", "received_time", "timestamp", "source_time",
    "source", "stream_mode",
    "identity_verified", "provider_timestamp_verified",
    "freshness_state", "freshness_reason", "freshness", "age_seconds",
    "usable_for_promotion", "display_as_live", "stale",
    "quote_seq", "delta_id",
)


def browser_quote_projection(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the bounded scalar quote contract exposed to browser clients."""
    if not isinstance(row, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for key in BROWSER_QUOTE_FIELDS:
        if key not in row:
            continue
        value = row.get(key)
        if value is None or isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str):
            # Provenance strings are useful in tooltips, but never unbounded.
            out[key] = value[:256]
    return out


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc_iso_from_epoch_ms(value: Any) -> Optional[str]:
    millis = _int(value)
    if millis is None or millis <= 0:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _epoch_ms(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    numeric = _int(value)
    if numeric is not None:
        # Provider timestamps are milliseconds.  Accept seconds defensively.
        return numeric * 1000 if numeric < 10_000_000_000 else numeric
    try:
        text = str(value).strip().replace("Z", "+00:00")
        stamp = datetime.fromisoformat(text)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return int(stamp.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _connection_is_closed_error(value: Any) -> bool:
    text = str(value or "").strip().casefold()
    if not text:
        return False
    markers = (
        "socket is already closed", "socket closed", "connection is already closed",
        "connection closed", "websocket is closed", "not connected",
        "connection reset", "broken pipe", "remote host closed",
    )
    return any(marker in text for marker in markers)


def _mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    # The SDK normally emits dictionaries.  This fallback makes generated
    # protobuf/model objects testable without coupling to one SDK release.
    if hasattr(value, "to_dict"):
        try:
            out = value.to_dict()
            return dict(out) if isinstance(out, Mapping) else {}
        except Exception:
            return {}
    if hasattr(value, "__dict__"):
        try:
            return {k: v for k, v in vars(value).items() if not str(k).startswith("_")}
        except Exception:
            return {}
    return {}


def _find_casefold(mapping: Mapping[str, Any], *names: str) -> Any:
    direct = dict(mapping or {})
    for name in names:
        if name in direct:
            return direct[name]
    folded = {str(key).casefold(): value for key, value in direct.items()}
    for name in names:
        if name.casefold() in folded:
            return folded[name.casefold()]
    return None


def _extract_ltpc(feed: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return ``(ltpc, rich_context)`` across documented V3 feed shapes."""
    root = _mapping(feed)
    candidates: List[Dict[str, Any]] = [root]
    for key in ("ff", "fullFeed", "full_feed", "firstLevelWithGreeks", "first_level_with_greeks"):
        child = _mapping(_find_casefold(root, key))
        if child:
            candidates.append(child)
    for parent in list(candidates):
        for key in ("marketFF", "market_ff", "indexFF", "index_ff"):
            child = _mapping(_find_casefold(parent, key))
            if child:
                candidates.append(child)

    ltpc: Dict[str, Any] = {}
    rich: Dict[str, Any] = {}
    for candidate in candidates:
        maybe = _mapping(_find_casefold(candidate, "ltpc"))
        if maybe and not ltpc:
            ltpc = maybe
        for key in ("marketLevel", "market_level", "firstDepth", "first_depth", "marketOHLC", "market_ohlc"):
            value = _find_casefold(candidate, key)
            if value not in (None, {}, []):
                rich[key] = value
        for key in ("atp", "vtt", "tbq", "tsq", "oi", "iv", "yh", "yl", "lc", "uc"):
            value = _find_casefold(candidate, key)
            if value not in (None, ""):
                rich[key] = value
    return ltpc, rich


def _best_depth(rich: Mapping[str, Any]) -> Dict[str, Any]:
    first = _mapping(_find_casefold(rich, "firstDepth", "first_depth"))
    if first:
        return {
            "bid_price": _finite(_find_casefold(first, "bidP", "bid_price", "bp")),
            "bid_quantity": _finite(_find_casefold(first, "bidQ", "bid_quantity", "bq")),
            "ask_price": _finite(_find_casefold(first, "askP", "ask_price", "ap")),
            "ask_quantity": _finite(_find_casefold(first, "askQ", "ask_quantity", "aq")),
        }
    levels = _mapping(_find_casefold(rich, "marketLevel", "market_level"))
    quotes = _find_casefold(levels, "bidAskQuote", "bid_ask_quote")
    if isinstance(quotes, Sequence) and not isinstance(quotes, (str, bytes)) and quotes:
        row = _mapping(quotes[0])
        return {
            "bid_price": _finite(_find_casefold(row, "bidP", "bid_price", "bp")),
            "bid_quantity": _finite(_find_casefold(row, "bidQ", "bid_quantity", "bq")),
            "ask_price": _finite(_find_casefold(row, "askP", "ask_price", "ap")),
            "ask_quantity": _finite(_find_casefold(row, "askQ", "ask_quantity", "aq")),
        }
    return {}


def _depth_book(rich: Mapping[str, Any], *, limit: int = 5) -> Dict[str, List[Dict[str, Any]]]:
    """Return a bounded five-level bid/ask book from the documented V3 feed.

    The provider emits one combined ``bidAskQuote`` row per level.  Laddu keeps
    only price, quantity and order count; no raw protobuf/provider body is
    retained or exposed to the browser.
    """
    levels = _mapping(_find_casefold(rich, "marketLevel", "market_level"))
    quotes = _find_casefold(levels, "bidAskQuote", "bid_ask_quote")
    if not isinstance(quotes, Sequence) or isinstance(quotes, (str, bytes)):
        first = _mapping(_find_casefold(rich, "firstDepth", "first_depth"))
        quotes = [first] if first else []
    buys: List[Dict[str, Any]] = []
    sells: List[Dict[str, Any]] = []
    for value in list(quotes or [])[: max(1, min(int(limit or 5), 5))]:
        row = _mapping(value)
        bid_price = _finite(_find_casefold(row, "bidP", "bid_price", "bp"))
        bid_quantity = _finite(_find_casefold(row, "bidQ", "bid_quantity", "bq"))
        bid_orders = _int(_find_casefold(row, "bidNo", "bid_orders", "bno", "bidN"))
        ask_price = _finite(_find_casefold(row, "askP", "ask_price", "ap"))
        ask_quantity = _finite(_find_casefold(row, "askQ", "ask_quantity", "aq"))
        ask_orders = _int(_find_casefold(row, "askNo", "ask_orders", "ano", "askN"))
        if bid_price is not None or bid_quantity is not None:
            buys.append({"price": bid_price, "quantity": bid_quantity, "orders": bid_orders})
        if ask_price is not None or ask_quantity is not None:
            sells.append({"price": ask_price, "quantity": ask_quantity, "orders": ask_orders})
    return {"buy": buys, "sell": sells}


def normalise_v3_message(message: Any, *, symbol_by_key: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """Normalise one SDK V3 message into canonical quote observations."""
    payload = _mapping(message)
    if not payload:
        return []
    feeds = _find_casefold(payload, "feeds")
    if not isinstance(feeds, Mapping):
        return []
    current_ms = _epoch_ms(_find_casefold(payload, "currentTs", "current_ts"))
    symbol_by_key = {str(k): str(v).upper() for k, v in dict(symbol_by_key or {}).items()}
    rows: List[Dict[str, Any]] = []
    for instrument_key, feed_raw in feeds.items():
        key = str(instrument_key or "").strip()
        if not key:
            continue
        ltpc, rich = _extract_ltpc(_mapping(feed_raw))
        ltp = _finite(_find_casefold(ltpc, "ltp"))
        if ltp is None:
            continue
        source_ms = _epoch_ms(_find_casefold(ltpc, "ltt")) or current_ms
        close = _finite(_find_casefold(ltpc, "cp", "close"))
        rupee_change = (ltp - close) if close not in (None, 0) else None
        mapped_symbol = symbol_by_key.get(key)
        row: Dict[str, Any] = {
            "instrument_key": key,
            "symbol": mapped_symbol or key,
            "ltp": ltp,
            "previous_close": close,
            "rupee_change": round(rupee_change, 8) if rupee_change is not None else None,
            "change_pct": round(rupee_change / close * 100.0, 6) if rupee_change is not None and close else None,
            "last_traded_quantity": _finite(_find_casefold(ltpc, "ltq")),
            "provider_ts_ms": source_ms,
            "provider_timestamp": _utc_iso_from_epoch_ms(source_ms),
            "feed_ts_ms": current_ms,
            "feed_timestamp": _utc_iso_from_epoch_ms(current_ms),
            "source": "upstox_market_data_v3",
            "stream_mode": "full" if rich else "ltpc",
            # A provider instrument key alone is not a verified Laddu identity.
            # Only the canonical resolver/registration map may establish the
            # symbol relationship used by UI and promotion authorities.
            "identity_verified": bool(mapped_symbol),
        }
        row.update({k: v for k, v in _best_depth(rich).items() if v is not None})
        depth_book = _depth_book(rich)
        if depth_book.get("buy") or depth_book.get("sell"):
            row["depth"] = depth_book
        row["average_traded_price"] = _finite(_find_casefold(rich, "atp"))
        row["volume_traded_today"] = _finite(_find_casefold(rich, "vtt"))
        row["total_buy_quantity"] = _finite(_find_casefold(rich, "tbq"))
        row["total_sell_quantity"] = _finite(_find_casefold(rich, "tsq"))
        rows.append(row)
    return rows


@dataclass(frozen=True)
class IngestDisposition:
    accepted: bool
    reason: str
    delta_id: Optional[int] = None


class CanonicalQuoteStore:
    """Lock-protected monotonic quote state plus cursor-based deltas."""

    def __init__(self, *, max_deltas: int = 20_000):
        self._lock = threading.RLock()
        self._by_key: Dict[str, Dict[str, Any]] = {}
        self._key_by_symbol: Dict[str, str] = {}
        self._symbol_by_key: Dict[str, str] = {}
        self._deltas: Deque[Dict[str, Any]] = deque(maxlen=max(1_000, int(max_deltas)))
        self._cursor = 0
        self._stats = {
            "accepted": 0,
            "duplicates": 0,
            "out_of_order": 0,
            "future_timestamp": 0,
            "invalid": 0,
        }

    def register_identity(self, instrument_key: str, symbol: str) -> None:
        key = str(instrument_key or "").strip()
        sym = str(symbol or "").upper().strip()
        if not key or not sym:
            return
        with self._lock:
            self._symbol_by_key[key] = sym
            self._key_by_symbol[sym] = key

    @property
    def symbol_by_key(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._symbol_by_key)

    @property
    def cursor(self) -> int:
        with self._lock:
            return self._cursor

    def ingest(self, observation: Mapping[str, Any], *, receive_time: Optional[str] = None) -> IngestDisposition:
        row = dict(observation or {})
        key = str(row.get("instrument_key") or "").strip()
        ltp = _finite(row.get("ltp"))
        provider_ms = _epoch_ms(row.get("provider_ts_ms") or row.get("provider_timestamp") or row.get("timestamp"))
        if not key or ltp is None or ltp <= 0 or provider_ms is None:
            with self._lock:
                self._stats["invalid"] += 1
            return IngestDisposition(False, "invalid_identity_price_or_timestamp")
        receive = receive_time or _iso_now()
        receive_ms = _epoch_ms(receive) or int(time.time() * 1000)
        if provider_ms > receive_ms + 5_000:
            with self._lock:
                self._stats["future_timestamp"] += 1
            return IngestDisposition(False, "future_timestamp")
        with self._lock:
            registered_symbol = self._symbol_by_key.get(key)
            claimed_symbol = str(row.get("symbol") or "").upper().strip()
            identity_verified = bool(
                registered_symbol
                and (not claimed_symbol or claimed_symbol in {registered_symbol, key.upper()})
            )
            # Verification comes from the store registration, never from the
            # provider observation itself.  This permits an initially opaque
            # feed row to become verified after the canonical resolver has
            # registered the instrument key.
            symbol = registered_symbol or claimed_symbol or key.upper()
            old = self._by_key.get(key)
            old_ms = _epoch_ms((old or {}).get("provider_ts_ms"))
            if old_ms is not None and provider_ms < old_ms:
                self._stats["out_of_order"] += 1
                return IngestDisposition(False, "out_of_order")
            if old_ms == provider_ms and old is not None:
                materially_same = (
                    _finite(old.get("ltp")) == ltp
                    and _finite(old.get("volume_traded_today")) == _finite(row.get("volume_traded_today"))
                    and _finite(old.get("bid_price")) == _finite(row.get("bid_price"))
                    and _finite(old.get("ask_price")) == _finite(row.get("ask_price"))
                )
                if materially_same:
                    self._stats["duplicates"] += 1
                    return IngestDisposition(False, "duplicate")
            old_ltp = _finite((old or {}).get("ltp"))
            direction = "up" if old_ltp is not None and ltp > old_ltp else "down" if old_ltp is not None and ltp < old_ltp else "flat"
            self._cursor += 1
            canonical = {
                **(old or {}),
                **row,
                "instrument_key": key,
                "symbol": symbol,
                "ltp": ltp,
                "old_ltp": old_ltp,
                "new_ltp": ltp,
                "changed": bool(old_ltp is not None and old_ltp != ltp),
                "direction": direction,
                "provider_ts_ms": provider_ms,
                "provider_timestamp": row.get("provider_timestamp") or _utc_iso_from_epoch_ms(provider_ms),
                "received_ts_ms": receive_ms,
                "received_time": receive,
                "freshness_state": "live" if identity_verified else "unverified",
                "freshness_reason": "verified_upstox_v3_stream" if identity_verified else "instrument_identity_unverified",
                "identity_verified": identity_verified,
                "provider_timestamp_verified": True,
                "usable_for_promotion": identity_verified,
                "display_as_live": identity_verified,
                "stale": False,
                "quote_seq": self._cursor,
                "delta_id": self._cursor,
            }
            self._by_key[key] = canonical
            self._symbol_by_key[key] = symbol
            self._key_by_symbol[symbol] = key
            self._deltas.append(dict(canonical))
            self._stats["accepted"] += 1
            return IngestDisposition(True, "accepted", self._cursor)

    def ingest_many(self, rows: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
        counts = {
            "accepted": 0,
            "duplicate": 0,
            "out_of_order": 0,
            "future_timestamp": 0,
            "invalid": 0,
        }
        for row in rows or []:
            disposition = self.ingest(row)
            key = "accepted" if disposition.accepted else disposition.reason if disposition.reason in counts else "invalid"
            counts[key] += 1
        return counts

    def _with_freshness(self, row: Dict[str, Any], *, now_ms: int, market_open: bool, max_age_sec: float) -> Dict[str, Any]:
        out = dict(row)
        source_ms = _epoch_ms(out.get("provider_ts_ms"))
        age = max(0.0, (now_ms - source_ms) / 1000.0) if source_ms else None
        identity_verified = out.get("identity_verified") is True
        if not identity_verified:
            state = "unverified"
            reason = "instrument_identity_unverified"
        elif not market_open:
            state = "closed_market"
            reason = "market_closed_last_verified_tick"
        elif age is None or age > max_age_sec:
            state = "stale"
            reason = "stream_tick_age_exceeded"
        else:
            state = "live"
            reason = "verified_upstox_v3_stream"
        out.update({
            "age_seconds": round(age, 3) if age is not None else None,
            "freshness_state": state,
            "freshness_reason": reason,
            "freshness": state.replace("_", " ") + (f" @ {out.get('provider_timestamp')}" if out.get("provider_timestamp") else ""),
            "usable_for_promotion": identity_verified and state == "live",
            "display_as_live": identity_verified and state == "live",
            "stale": state == "stale",
        })
        return out

    def snapshot(self, symbols: Optional[Iterable[str]] = None, *, market_open: bool = True, max_age_sec: float = 8.0) -> Dict[str, Dict[str, Any]]:
        requested = {str(s).upper().strip() for s in (symbols or []) if str(s).strip()}
        now_ms = int(time.time() * 1000)
        with self._lock:
            rows = list(self._by_key.values())
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if requested and symbol not in requested:
                continue
            out[symbol] = self._with_freshness(row, now_ms=now_ms, market_open=market_open, max_age_sec=max_age_sec)
        return out

    def deltas_since(self, cursor: int, *, symbols: Optional[Iterable[str]] = None, market_open: bool = True, max_age_sec: float = 8.0, limit: int = 2_000) -> Dict[str, Any]:
        since = max(0, int(cursor or 0))
        requested = {str(s).upper().strip() for s in (symbols or []) if str(s).strip()}
        with self._lock:
            first_available = int(self._deltas[0]["delta_id"]) if self._deltas else self._cursor + 1
            current = self._cursor
            raw = [dict(row) for row in self._deltas if int(row.get("delta_id") or 0) > since and (not requested or str(row.get("symbol") or "").upper() in requested)]
        overflow = bool(since and since < first_available - 1)
        if len(raw) > max(1, int(limit)):
            raw = raw[-int(limit):]
            overflow = True
        now_ms = int(time.time() * 1000)
        return {
            "ok": True,
            "cursor": current,
            "from_cursor": since,
            "overflow": overflow,
            "deltas": [self._with_freshness(row, now_ms=now_ms, market_open=market_open, max_age_sec=max_age_sec) for row in raw],
            "gateway_version": LIVE_GATEWAY_VERSION,
            "time": _iso_now(),
        }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            latest_ms = max((_epoch_ms(row.get("provider_ts_ms")) or 0 for row in self._by_key.values()), default=0)
            return {
                "gateway_version": LIVE_GATEWAY_VERSION,
                "cursor": self._cursor,
                "instrument_count": len(self._by_key),
                "latest_provider_timestamp": _utc_iso_from_epoch_ms(latest_ms),
                **dict(self._stats),
            }


class SubscriptionBook:
    """Desired and applied subscription modes with incremental reconciliation."""

    MODE_ORDER = {"ltpc": 1, "full": 2, "full_d30": 3}

    def __init__(self):
        self._lock = threading.RLock()
        self._desired: Dict[str, str] = {}
        self._applied: Dict[str, str] = {}

    def replace(self, *, ltpc: Iterable[str] = (), full: Iterable[str] = (), full_d30: Iterable[str] = ()) -> Dict[str, str]:
        desired: Dict[str, str] = {}
        for mode, keys in (("ltpc", ltpc), ("full", full), ("full_d30", full_d30)):
            for key in keys or []:
                k = str(key or "").strip()
                if not k:
                    continue
                if self.MODE_ORDER[mode] >= self.MODE_ORDER.get(desired.get(k, ""), 0):
                    desired[k] = mode
        with self._lock:
            self._desired = desired
            return dict(desired)

    def desired(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._desired)

    def reset_applied(self) -> None:
        with self._lock:
            self._applied = {}

    def operations(self) -> Dict[str, Any]:
        with self._lock:
            desired, applied = dict(self._desired), dict(self._applied)
        unsub = sorted(set(applied) - set(desired))
        subscribe: Dict[str, List[str]] = {"ltpc": [], "full": [], "full_d30": []}
        change: Dict[str, List[str]] = {"ltpc": [], "full": [], "full_d30": []}
        for key, mode in desired.items():
            if key not in applied:
                subscribe[mode].append(key)
            elif applied[key] != mode:
                change[mode].append(key)
        return {"unsubscribe": unsub, "subscribe": {k: sorted(v) for k, v in subscribe.items() if v}, "change_mode": {k: sorted(v) for k, v in change.items() if v}}

    def mark_applied(self, operations: Mapping[str, Any]) -> None:
        with self._lock:
            for key in operations.get("unsubscribe") or []:
                self._applied.pop(str(key), None)
            for operation in ("subscribe", "change_mode"):
                for mode, keys in dict(operations.get(operation) or {}).items():
                    for key in keys:
                        self._applied[str(key)] = str(mode)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            def count(values: Mapping[str, str]) -> Dict[str, int]:
                return {mode: sum(1 for value in values.values() if value == mode) for mode in self.MODE_ORDER}
            return {"desired": count(self._desired), "applied": count(self._applied), "desired_total": len(self._desired), "applied_total": len(self._applied)}


class LiveMarketGateway:
    """Official-SDK stream adapter with labelled HTTP fallback ingestion."""

    def __init__(self, token_fn: Callable[[], Optional[str]], *, event_fn: Optional[Callable[..., Any]] = None, accepted_observation_fn: Optional[Callable[[Mapping[str, Any]], Any]] = None):
        self.token_fn = token_fn
        self.event_fn = event_fn or (lambda *args, **kwargs: None)
        self.accepted_observation_fn = accepted_observation_fn or (lambda _row: None)
        self.quotes = CanonicalQuoteStore()
        self.subscriptions = SubscriptionBook()
        self._lock = threading.RLock()
        self._streamer: Any = None
        self._connected = False
        self._state = "starting"
        self._last_message_at: Optional[str] = None
        self._last_message_monotonic: Optional[float] = None
        self._connected_monotonic: Optional[float] = None
        self._plan_applied_monotonic: Optional[float] = None
        self._last_error: Optional[str] = None
        self._reconnects = 0
        self._forced_reconnects = 0
        self._last_watchdog_reason: Optional[str] = None
        self._sdk_available: Optional[bool] = None
        self._stop = threading.Event()

    def register_identity(self, instrument_key: str, symbol: str) -> None:
        self.quotes.register_identity(instrument_key, symbol)

    def set_plan(self, *, ltpc: Iterable[str] = (), full: Iterable[str] = (), full_d30: Iterable[str] = ()) -> Dict[str, Any]:
        self.subscriptions.replace(ltpc=ltpc, full=full, full_d30=full_d30)
        self._apply_plan()
        return self.status()

    def ingest_stream_message(self, message: Any) -> Dict[str, int]:
        rows = normalise_v3_message(message, symbol_by_key=self.quotes.symbol_by_key)
        result = {
            "accepted": 0,
            "duplicate": 0,
            "out_of_order": 0,
            "future_timestamp": 0,
            "invalid": 0,
        }
        for row in rows:
            disposition = self.quotes.ingest(row)
            key = "accepted" if disposition.accepted else disposition.reason if disposition.reason in result else "invalid"
            result[key] += 1
            if disposition.accepted:
                try:
                    self.accepted_observation_fn(dict(row))
                except Exception:
                    pass
        if result.get("accepted"):
            with self._lock:
                self._last_message_at = _iso_now()
                self._last_message_monotonic = time.monotonic()
                self._state = "live"
        return result

    def ingest_http_snapshot(self, rows: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
        observations = []
        now_ms = int(time.time() * 1000)
        for value in rows or []:
            row = dict(value or {})
            key = str(row.get("instrument_key") or "").strip()
            source_ms = _epoch_ms(row.get("timestamp") or row.get("source_time") or row.get("received_at")) or now_ms
            if not key:
                continue
            observations.append({
                **row,
                "provider_ts_ms": source_ms,
                "provider_timestamp": _utc_iso_from_epoch_ms(source_ms),
                "source": str(row.get("source") or "upstox_http_verified_fallback"),
                "identity_verified": bool(row.get("identity_verified", True)),
                "stream_mode": "http_fallback",
            })
        result = {
            "accepted": 0,
            "duplicate": 0,
            "out_of_order": 0,
            "future_timestamp": 0,
            "invalid": 0,
        }
        for row in observations:
            disposition = self.quotes.ingest(row)
            key = "accepted" if disposition.accepted else disposition.reason if disposition.reason in result else "invalid"
            result[key] += 1
            if disposition.accepted:
                try:
                    self.accepted_observation_fn(dict(row))
                except Exception:
                    pass
        return result

    def execution_calibration_snapshot(
        self, *, mode: str, quantity: int, max_deltas: int = 2000, at: datetime | None = None,
    ) -> Dict[str, Any]:
        """Build a read-only empirical calibration snapshot from retained canonical full-depth deltas.

        This never calls the provider, changes subscriptions, or grants execution
        authority.  Insufficient fresh breadth returns CALIBRATION_PENDING.
        """
        current = self.quotes.cursor
        bounded = max(30, min(int(max_deltas or 2000), 20_000))
        result = self.quotes.deltas_since(
            max(0, current - bounded), market_open=True,
            max_age_sec=DEFAULT_EXECUTION_SLIPPAGE_CALIBRATION_AUTHORITY.MAX_SAMPLE_AGE_SEC,
            limit=bounded,
        )
        observations = [
            DEFAULT_EXECUTION_SLIPPAGE_CALIBRATION_AUTHORITY.observe(row, quantity=quantity)
            for row in (result.get("deltas") or [])
        ]
        snapshot = DEFAULT_EXECUTION_SLIPPAGE_CALIBRATION_AUTHORITY.build_snapshot(
            observations, mode=mode, at=at,
        )
        return {
            **snapshot,
            "live_gateway_version": LIVE_GATEWAY_VERSION,
            "quote_cursor": current,
            "delta_overflow": bool(result.get("overflow")),
            "source": "CANONICAL_LIVE_MARKET_GATEWAY_RETAINED_DEPTH_DELTAS",
            "provider_fetch_performed": False,
        }

    def browser_deltas(
        self,
        *,
        since: int = 0,
        symbols_csv: str = "",
        market_open: bool = True,
    ) -> Dict[str, Any]:
        """Return a bounded, sanitized browser snapshot/delta contract.

        ``since=0`` bootstraps at the current cursor without replaying the
        retained tick tape. Later requests collapse to the newest scalar delta
        per visible symbol; raw provider bodies never leave the gateway.
        """
        symbols: List[str] = []
        seen = set()
        for value in str(symbols_csv or "").split(","):
            symbol = value.strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
            if len(symbols) >= 60:
                break
        bootstrap = int(since or 0) <= 0
        if bootstrap:
            out: Dict[str, Any] = {
                "ok": True, "cursor": int(self.quotes.cursor), "from_cursor": 0,
                "overflow": False, "deltas": [], "gateway_version": LIVE_GATEWAY_VERSION,
                "time": _iso_now(),
            }
        else:
            out = self.quotes.deltas_since(
                since, symbols=symbols, market_open=market_open,
                max_age_sec=8.0, limit=240,
            )
        snapshot = self.quotes.snapshot(
            symbols, market_open=market_open, max_age_sec=8.0
        ) if symbols else {}
        quotes = {symbol: browser_quote_projection(row) for symbol, row in snapshot.items()}
        latest: Dict[str, Dict[str, Any]] = {}
        for row in out.get("deltas") or []:
            projected = browser_quote_projection(row)
            symbol = str(projected.get("symbol") or "").upper()
            if symbol:
                latest[symbol] = projected
        out["deltas"] = sorted(latest.values(), key=lambda row: int(row.get("delta_id") or 0))
        out["cursor"] = max(
            int(out.get("cursor") or 0),
            max(
                (int(row.get("delta_id") or row.get("quote_seq") or 0) for row in quotes.values()),
                default=0,
            ),
        )
        out.update({
            "bootstrap": bootstrap,
            "quotes": quotes,
            "live_count": sum(1 for row in quotes.values() if row.get("freshness_state") == "live"),
            "closed_count": sum(1 for row in quotes.values() if row.get("freshness_state") == "closed_market"),
            "market_open": market_open,
            "served_from": "upstox_v3_canonical_stream_memory",
            "stream": self.status(),
        })
        return out

    def _emit(self, level: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.event_fn(level, "live_market_gateway", message, detail or {})
        except Exception:
            pass

    def _sdk(self) -> Any:
        try:
            module = importlib.import_module("upstox_client")
            self._sdk_available = True
            return module
        except Exception as exc:
            self._sdk_available = False
            raise RuntimeError("UPSTOX_PYTHON_SDK_UNAVAILABLE") from exc

    def _callback(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            value = args[-1] if args else kwargs
            return fn(value)
        return wrapped

    def _on_open(self, _value: Any = None) -> None:
        with self._lock:
            self._connected = True
            self._connected_monotonic = time.monotonic()
            self._state = "connected"
            self._last_error = None
            self._last_watchdog_reason = None
        self.subscriptions.reset_applied()
        self._emit("INFO", "Upstox V3 market stream connected")
        self._apply_plan()

    def _on_close(self, value: Any = None) -> None:
        with self._lock:
            self._connected = False
            self._connected_monotonic = None
            self._state = "disconnected"
        self._emit("WARN", "Upstox V3 market stream closed", {"detail": str(value)[:240]})

    def _on_error(self, value: Any = None) -> None:
        error = str(value)[:500]
        closed = _connection_is_closed_error(error)
        with self._lock:
            self._last_error = error
            self._state = "disconnected" if closed else "degraded"
            if closed:
                # Never report a closed SDK socket as connected.  Reset the
                # applied plan so the next connection must prove every desired
                # subscription again before the feed can become operational.
                self._connected = False
                self._connected_monotonic = None
        if closed:
            self.subscriptions.reset_applied()
        self._emit("WARN", "Upstox V3 market stream error", {"error": self._last_error, "connection_closed": closed})

    def _on_reconnecting(self, value: Any = None) -> None:
        with self._lock:
            self._reconnects += 1
            self._state = "reconnecting"
        self._emit("WARN", "Upstox V3 market stream reconnecting", {"count": self._reconnects, "detail": str(value)[:160]})

    def _apply_plan(self) -> None:
        with self._lock:
            streamer = self._streamer
            connected = self._connected
        if streamer is None or not connected:
            return
        operations = self.subscriptions.operations()
        try:
            if operations["unsubscribe"]:
                streamer.unsubscribe(operations["unsubscribe"])
            for mode, keys in operations["subscribe"].items():
                streamer.subscribe(keys, mode)
            for mode, keys in operations["change_mode"].items():
                if hasattr(streamer, "change_mode"):
                    streamer.change_mode(keys, mode)
                else:
                    streamer.unsubscribe(keys)
                    streamer.subscribe(keys, mode)
            self.subscriptions.mark_applied(operations)
            if operations.get("unsubscribe") or any(operations.get(name) for name in ("subscribe", "change_mode")):
                with self._lock:
                    self._plan_applied_monotonic = time.monotonic()
        except Exception as exc:
            self._on_error(exc)

    def _watchdog_reason(self, *, market_open: bool, now: Optional[float] = None) -> Optional[str]:
        """Detect a connected-but-dead stream without touching network or storage.

        Connection/subscription consistency is enforced even after market
        close.  Message-freshness thresholds are market-hours-only.
        """
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            connected = self._connected
            connected_at = self._connected_monotonic
            last_message = self._last_message_monotonic
            last_error = self._last_error
        subscriptions = self.subscriptions.status()
        desired = int(subscriptions.get("desired_total") or 0)
        applied = int(subscriptions.get("applied_total") or 0)
        if connected and _connection_is_closed_error(last_error):
            return "sdk_socket_closed_but_marked_connected"
        if not connected or desired <= 0:
            return None
        connected_age = now - connected_at if connected_at is not None else 0.0
        if applied <= 0 and connected_age > 20.0:
            return "subscriptions_not_applied"
        if not market_open:
            return None
        if applied > 0:
            if last_message is None and connected_age > 30.0:
                return "no_stream_messages_after_subscribe"
            if last_message is not None and (now - last_message) > 20.0:
                return "stream_messages_stale"
        return None

    def _force_reconnect(self, streamer: Any, reason: str) -> None:
        with self._lock:
            self._forced_reconnects += 1
            self._last_watchdog_reason = reason
            self._state = "reconnecting"
        self._emit("WARN", "Live stream watchdog forcing reconnect", {"reason": reason, "forced_reconnects": self._forced_reconnects})
        try:
            if streamer is not None and hasattr(streamer, "disconnect"):
                streamer.disconnect()
        except Exception:
            pass

    def run(self, supervisor: Any = None, *, running_fn: Callable[[], bool] = lambda: True, market_open_fn: Callable[[], bool] = lambda: False) -> None:
        """Supervisor-owned connector loop; reconnects without blocking consumers."""
        while running_fn() and not self._stop.is_set() and (supervisor is None or supervisor.running):
            if supervisor:
                supervisor.beat("live_market_stream")
            token = self.token_fn()
            if not token:
                with self._lock:
                    self._state = "token_missing"
                if supervisor:
                    supervisor.progress(
                        "live_market_stream", token="token_missing", stage="waiting_for_token",
                        waiting_on="broker token unavailable", expected_idle=True,
                    )
                time.sleep(10)
                continue
            try:
                sdk = self._sdk()
                config = sdk.Configuration()
                config.access_token = token
                streamer = sdk.MarketDataStreamerV3(sdk.ApiClient(config))
                streamer.on("open", self._callback(self._on_open))
                streamer.on("message", self._callback(self.ingest_stream_message))
                streamer.on("close", self._callback(self._on_close))
                streamer.on("error", self._callback(self._on_error))
                streamer.on("reconnecting", self._callback(self._on_reconnecting))
                if hasattr(streamer, "auto_reconnect"):
                    try:
                        streamer.auto_reconnect(True, 5, 50)
                    except TypeError:
                        streamer.auto_reconnect(True, 5)
                with self._lock:
                    self._streamer = streamer
                    self._state = "connecting"
                streamer.connect()
                while running_fn() and not self._stop.wait(1.0) and (supervisor is None or supervisor.running):
                    market_open = bool(market_open_fn())
                    self._apply_plan()
                    if supervisor:
                        current = self.status()
                        quote_state = dict(current.get("quotes") or {})
                        subscriptions = dict(current.get("subscriptions") or {})
                        cursor = int(quote_state.get("cursor") or 0)
                        last_message = current.get("last_message_at")
                        applied = int(subscriptions.get("applied_total") or 0)
                        desired = int(subscriptions.get("desired_total") or 0)
                        supervisor.progress(
                            "live_market_stream",
                            token=f"{cursor}:{last_message}:{applied}:{desired}:{current.get('operational_state')}",
                            stage=str(current.get("operational_state") or "stream"),
                            completed_units=applied, total_units=desired,
                            waiting_on=("market closed; stream freshness not required" if not market_open else None),
                            expected_idle=not market_open,
                        )
                    reason = self._watchdog_reason(market_open=market_open)
                    if reason:
                        self._force_reconnect(streamer, reason)
                        break
                    with self._lock:
                        if self._state in {"disconnected", "degraded"} and not self._connected:
                            break
            except Exception as exc:
                self._on_error(exc)
                time.sleep(10)
            finally:
                with self._lock:
                    streamer = self._streamer
                    self._streamer = None
                    self._connected = False
                    self._connected_monotonic = None
                try:
                    if streamer is not None and hasattr(streamer, "disconnect"):
                        streamer.disconnect()
                except Exception:
                    pass

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            streamer = self._streamer
        try:
            if streamer is not None and hasattr(streamer, "disconnect"):
                streamer.disconnect()
        except Exception:
            pass

    def status(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            feed_age = round(now - self._last_message_monotonic, 1) if self._last_message_monotonic is not None else None
            connected_age = round(now - self._connected_monotonic, 1) if self._connected_monotonic is not None else None
            runtime = {
                "state": self._state,
                "connected": self._connected,
                "sdk_available": self._sdk_available,
                "last_message_at": self._last_message_at,
                "feed_age_sec": feed_age,
                "connected_age_sec": connected_age,
                "last_error": self._last_error,
                "reconnects": self._reconnects,
                "forced_reconnects": self._forced_reconnects,
                "last_watchdog_reason": self._last_watchdog_reason,
                "broker_orders": False,
            }
        subscriptions = self.subscriptions.status()
        desired = int(subscriptions.get("desired_total") or 0)
        applied = int(subscriptions.get("applied_total") or 0)
        stale = bool(runtime["connected"] and applied > 0 and feed_age is not None and feed_age > 20.0)
        if runtime["connected"] and applied > 0 and not stale:
            operational_state = "live" if runtime["last_message_at"] else "warming"
        elif runtime["connected"]:
            operational_state = "degraded"
        else:
            operational_state = runtime["state"]
        return {**runtime, "stale": stale, "operational_state": operational_state, "quotes": self.quotes.status(), "subscriptions": subscriptions, "gateway_version": LIVE_GATEWAY_VERSION}
