"""Local-history scheduling prior for scanner capacity allocation.

This service has *no* trade/promotion authority. It uses only already-retained
canonical daily candles to decide which symbols should receive scarce deep
analysis sooner. The mathematical Evidence Engine remains the only source of
trade conviction; a missing/failed scheduling prior can never remove a symbol
from the canonical scanner population.

The service is intentionally asynchronous and cache-first so scanner latency is
not coupled to historical Parquet/DuckDB reads. Each completed refresh merges
into a process cache, allowing successive immutable-universe batches to build
coverage over a full sweep without provider I/O.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Dict, Iterable, Mapping


SERVICE_VERSION = "historical-alpha-scheduling-1.0.0"


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", "—"):
            return default
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _pct_change(values: list[float], lookback: int) -> float | None:
    if len(values) <= lookback:
        return None
    base = values[-lookback - 1]
    latest = values[-1]
    if base <= 0:
        return None
    return latest / base - 1.0


def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    avg = sum(values) / len(values)
    var = sum((x - avg) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(max(0.0, var))


class HistoricalAlphaSchedulingService:
    """Asynchronous retained-history prior for analysis scheduling only."""

    def __init__(self, store: Any, *, ttl_seconds: float = 12 * 3600.0, max_symbols_per_refresh: int = 96):
        self.store = store
        self.ttl_seconds = max(300.0, float(ttl_seconds))
        self.max_symbols_per_refresh = max(8, int(max_symbols_per_refresh))
        self._lock = threading.RLock()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._running = False
        self._last_error: str | None = None
        self._last_refresh_at: float = 0.0
        self._total_refreshed = 0

    @staticmethod
    def _symbol(row: Mapping[str, Any]) -> str:
        return str(row.get("trading_symbol") or row.get("symbol") or "").upper().strip()

    @staticmethod
    def _score_rows(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
        clean: list[dict[str, Any]] = []
        for raw in rows or []:
            row = dict(raw or {})
            close = _num(row.get("close"))
            if close is None or close <= 0:
                continue
            clean.append(row)
        clean.sort(key=lambda row: str(row.get("timestamp") or row.get("ts") or ""))
        if not clean:
            return {
                "state": "NO_HISTORY", "depth": 0,
                "delivery_score": 50.0, "intraday_score": 50.0,
                "trade_confidence_affected": False,
            }
        closes = [_num(row.get("close"), 0.0) or 0.0 for row in clean]
        volumes = [max(0.0, _num(row.get("volume"), 0.0) or 0.0) for row in clean]
        depth = len(closes)
        latest = closes[-1]
        returns = {}
        for horizon in (20, 63, 126, 252, 504, 756):
            returns[horizon] = _pct_change(closes, horizon)

        def sma(window: int) -> float | None:
            return _mean(closes[-window:]) if len(closes) >= window else None

        sma20, sma50, sma200 = sma(20), sma(50), sma(200)
        highs = closes[-252:] if len(closes) >= 20 else closes
        high252 = max(highs) if highs else latest
        location252 = latest / high252 if high252 > 0 else 1.0
        daily_returns = []
        for prev, cur in zip(closes[-64:-1], closes[-63:]):
            if prev > 0:
                daily_returns.append(cur / prev - 1.0)
        vol63 = _stdev(daily_returns)
        annualized_vol = (vol63 * math.sqrt(252.0)) if vol63 is not None else None
        avg20_volume = _mean([v for v in volumes[-20:] if v > 0])
        volume_ratio = (volumes[-1] / avg20_volume) if avg20_volume and avg20_volume > 0 else None

        # Delivery scheduling favours multi-horizon persistent strength, not a
        # single recent jump. Longer history receives small stabilising weight.
        delivery = 50.0
        weights = {20: 12.0, 63: 12.0, 126: 10.0, 252: 8.0, 504: 4.0, 756: 2.0}
        for horizon, weight in weights.items():
            ret = returns[horizon]
            if ret is not None:
                delivery += _clamp(ret * 100.0, -25.0, 25.0) / 25.0 * weight
        for moving_average, weight in ((sma20, 3.0), (sma50, 4.0), (sma200, 5.0)):
            if moving_average and moving_average > 0:
                delivery += weight if latest >= moving_average else -weight
        if location252 >= 0.95:
            delivery += 4.0
        elif location252 <= 0.65:
            delivery -= 4.0
        if volume_ratio is not None:
            delivery += _clamp((volume_ratio - 1.0) * 4.0, -3.0, 5.0)
        if annualized_vol is not None and annualized_vol > 0.70:
            delivery -= 4.0

        # Intraday scheduling values participation and movement potential.  The
        # direction itself is irrelevant because cash intraday may be long/short.
        intraday = 50.0
        for horizon, weight in ((20, 8.0), (63, 7.0), (126, 5.0)):
            ret = returns[horizon]
            if ret is not None:
                intraday += min(weight, abs(ret) * 100.0 / 20.0 * weight)
        if annualized_vol is not None:
            intraday += _clamp(annualized_vol * 12.0, 0.0, 10.0)
        if volume_ratio is not None:
            intraday += _clamp((volume_ratio - 1.0) * 5.0, -2.0, 8.0)
        if sma20 and sma50 and sma20 > 0 and sma50 > 0:
            intraday += min(6.0, abs(sma20 / sma50 - 1.0) * 100.0 * 2.0)

        first_ts = str(clean[0].get("timestamp") or clean[0].get("ts") or "") or None
        last_ts = str(clean[-1].get("timestamp") or clean[-1].get("ts") or "") or None
        return {
            "state": "READY" if depth >= 63 else "SHALLOW_HISTORY",
            "depth": depth,
            "first": first_ts,
            "last": last_ts,
            "delivery_score": round(_clamp(delivery, 0.0, 100.0), 4),
            "intraday_score": round(_clamp(intraday, 0.0, 100.0), 4),
            "returns": {str(k): (round(v, 8) if v is not None else None) for k, v in returns.items()},
            "annualized_volatility_63": round(annualized_vol, 8) if annualized_vol is not None else None,
            "volume_ratio_20": round(volume_ratio, 6) if volume_ratio is not None else None,
            "price_location_252": round(location252, 6),
            "trade_confidence_affected": False,
            "authority": "LOCAL_RETAINED_DAILY_HISTORY_SCHEDULING_ONLY",
            "service_version": SERVICE_VERSION,
        }

    def score_for(self, symbol: str, mode: str) -> Dict[str, Any]:
        key = str(symbol or "").upper().strip()
        with self._lock:
            row = dict(self._cache.get(key) or {})
        score_key = "intraday_score" if str(mode or "").lower() == "intraday" else "delivery_score"
        return {
            "score": float(row.get(score_key) or 50.0),
            "state": row.get("state") or "WARMING",
            "depth": int(row.get("depth") or 0),
            "last": row.get("last"),
            "service_version": SERVICE_VERSION,
            "trade_confidence_affected": False,
        }

    def refresh_async(self, instruments: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
        now = time.time()
        pending: list[dict[str, Any]] = []
        with self._lock:
            if self._running:
                return {"ok": True, "state": "ALREADY_RUNNING", "cached": len(self._cache)}
            for source in instruments or []:
                row = dict(source or {})
                symbol = self._symbol(row)
                instrument_key = str(row.get("instrument_key") or "").strip()
                if not symbol or not instrument_key:
                    continue
                cached = self._cache.get(symbol) or {}
                refreshed_at = float(cached.get("_refreshed_at") or 0.0)
                if cached and now - refreshed_at < self.ttl_seconds:
                    continue
                pending.append({"symbol": symbol, "instrument_key": instrument_key})
                if len(pending) >= self.max_symbols_per_refresh:
                    break
            if not pending:
                return {"ok": True, "state": "CURRENT", "cached": len(self._cache)}
            self._running = True

        def worker() -> None:
            updates: Dict[str, Dict[str, Any]] = {}
            error = None
            try:
                for item in pending:
                    try:
                        rows = self.store.get_candles(item["instrument_key"], "1d", limit=756) or []
                    except TypeError:
                        rows = self.store.get_candles(item["instrument_key"], "1d", 756) or []
                    result = self._score_rows(rows)
                    result.update({
                        "symbol": item["symbol"],
                        "instrument_key": item["instrument_key"],
                        "_refreshed_at": time.time(),
                    })
                    updates[item["symbol"]] = result
            except Exception as exc:  # fail-soft; scanner coverage must continue
                error = f"{type(exc).__name__}:{str(exc)[:200]}"
            finally:
                with self._lock:
                    self._cache.update(updates)
                    self._total_refreshed += len(updates)
                    self._last_refresh_at = time.time()
                    self._last_error = error
                    self._running = False

        threading.Thread(target=worker, name="historical-alpha-scheduling", daemon=True).start()
        return {"ok": True, "state": "STARTED", "scheduled": len(pending), "cached": len(self._cache)}

    def status(self) -> Dict[str, Any]:
        with self._lock:
            depths = [int(row.get("depth") or 0) for row in self._cache.values()]
            return {
                "ok": True,
                "service_version": SERVICE_VERSION,
                "state": "REFRESHING" if self._running else "READY",
                "cached_symbols": len(self._cache),
                "ge_252": sum(depth >= 252 for depth in depths),
                "ge_504": sum(depth >= 504 for depth in depths),
                "ge_756": sum(depth >= 756 for depth in depths),
                "total_refreshed": self._total_refreshed,
                "last_refresh_at_epoch": self._last_refresh_at or None,
                "last_error": self._last_error,
                "policy": "retained-history scheduling only; never trade confidence or promotion authority",
            }
