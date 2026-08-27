from __future__ import annotations

"""Pure completed-candle projection authority for Project Laddu.

This module owns timeframe resampling and completed-period filtering.  It has no
provider client, storage authority, scanner, runtime, or application dependency.
Every consumer (Chart, MTF, scanner compatibility, backtest compatibility) must
use this implementation rather than private helpers on ``MarketDataService``.
"""

from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List
import math
import statistics

from core.candle_freshness_service import CandleFreshnessService
from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY
from core.market_clock import IST, india_now
from core.timeframe import Timeframe, parse_timeframe
from core.numeric_semantics import finite_number
from session_candles import candle_datetime, closed_candles


class CanonicalCandleProjectionService:
    VERSION = "canonical-candle-projection-3.1.0-strict-finite-ohlcv-boundaries"
    NSE_OPEN_MINUTE = 9 * 60 + 15
    NSE_CLOSE_MINUTE = 15 * 60 + 30

    @staticmethod
    def _clock(now: datetime | None = None) -> datetime:
        return (now or india_now()).astimezone(IST)

    @staticmethod
    def _finite(value: Any) -> float | None:
        return finite_number(value)

    @classmethod
    def _positive_int(cls, name: str, value: Any) -> int:
        out = cls._finite(value)
        if out is None or not float(out).is_integer() or int(out) <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return int(out)

    @classmethod
    def _valid_ohlcv(cls, row: Dict[str, Any]) -> bool:
        o, h, l, c = (cls._finite(row.get(k)) for k in ("open", "high", "low", "close"))
        v = cls._finite(row.get("volume"))
        if None in (o, h, l, c) or v is None or v < 0:
            return False
        if min(o, h, l, c) <= 0:
            return False
        return bool(h >= max(o, c, l) and l <= min(o, c, h))

    @classmethod
    def _expected_trading_days(cls, start: date, end: date) -> list[date]:
        sessions = DEFAULT_TRADING_SESSION_AUTHORITY
        out: list[date] = []
        cursor = start
        while cursor <= end:
            if not sessions.calendar_covered(cursor):
                raise RuntimeError(f"calendar coverage unavailable for {cursor.isoformat()}")
            if sessions.is_trading_day(cursor):
                out.append(cursor)
            cursor += timedelta(days=1)
        return out

    @classmethod
    def _source_grid_complete(
        cls, chunk: list[tuple[datetime, Dict[str, Any], datetime]], *,
        bucket_start: datetime, effective_end: datetime, source_minutes: int,
    ) -> bool:
        expected = [bucket_start + timedelta(minutes=offset) for offset in range(0, max(0, int((effective_end-bucket_start).total_seconds()//60)), source_minutes)]
        actual = [item[0].replace(second=0, microsecond=0) for item in chunk]
        if len(actual) != len(set(actual)):
            return False
        return actual == expected

    @classmethod
    def resample_intraday(
        cls,
        candles: Iterable[Dict[str, Any]],
        target_minutes: int,
        *,
        source_minutes: int = 1,
        now: datetime | None = None,
    ) -> List[Dict[str, Any]]:
        """Resample a complete source grid into closed exchange-session buckets.

        Trading-day identity and session bounds come from the versioned session
        authority. Weekend/holiday/out-of-calendar rows are never accepted just
        because their wall-clock time resembles a market candle. Buckets are
        anchored to the actual session open, including special sessions.
        """
        groups: Dict[tuple[str, int], list[tuple[datetime, Dict[str, Any], datetime, datetime]]] = {}
        source_minutes = cls._positive_int("source_minutes", source_minutes)
        target_minutes = cls._positive_int("target_minutes", target_minutes)
        if target_minutes % source_minutes != 0:
            raise ValueError("target timeframe must be an integer multiple of source timeframe")
        clock = cls._clock(now)
        sessions = DEFAULT_TRADING_SESSION_AUTHORITY

        for candle in candles or []:
            dt = candle_datetime(candle)
            if dt is None:
                continue
            local = dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)
            if not sessions.calendar_covered(local.date()):
                continue
            window = sessions.session_window(local.date())
            if window is None:
                continue
            session_open = window.open_at()
            session_close = window.close_at()
            if local < session_open or local >= session_close:
                continue
            minute_of_session = int((local.replace(second=0, microsecond=0) - session_open).total_seconds() // 60)
            if minute_of_session < 0:
                continue
            bucket_index = minute_of_session // target_minutes
            bucket_start = session_open + timedelta(minutes=bucket_index * target_minutes)
            groups.setdefault((local.date().isoformat(), bucket_index), []).append(
                (local, dict(candle), bucket_start, session_close)
            )

        out: List[Dict[str, Any]] = []
        for key in sorted(groups):
            chunk = sorted(groups[key], key=lambda item: item[0])
            bucket_start = chunk[0][2]
            session_end = chunk[0][3]
            bucket_end = bucket_start + timedelta(minutes=target_minutes)
            effective_end = min(bucket_end, session_end)
            if bucket_start.date() == clock.date() and effective_end > clock:
                continue

            usable_minutes = max(0, int((effective_end - bucket_start).total_seconds() // 60))
            expected_bars = max(1, (usable_minutes + source_minutes - 1) // source_minutes)
            grid_chunk = [(item[0], item[1], item[2]) for item in chunk]
            if len(chunk) != expected_bars or not cls._source_grid_complete(
                grid_chunk, bucket_start=bucket_start, effective_end=effective_end, source_minutes=source_minutes
            ):
                continue

            bars = [item[1] for item in chunk]
            if not all(cls._valid_ohlcv(row) for row in bars):
                continue
            highs = [float(row["high"]) for row in bars]
            lows = [float(row["low"]) for row in bars]
            actual_end = effective_end
            actual_span_minutes = max(0, int((actual_end - bucket_start).total_seconds() // 60))
            session_partial = actual_span_minutes < target_minutes
            out.append(
                {
                    "timestamp": bucket_start.isoformat(),
                    "bar_end": actual_end.isoformat(),
                    "period_end": actual_end.isoformat(),
                    "open": bars[0].get("open"),
                    "high": max(highs),
                    "low": min(lows),
                    "close": bars[-1].get("close"),
                    "volume": sum(float(row.get("volume") or 0) for row in bars),
                    "oi": bars[-1].get("oi"),
                    "source": f"resampled_{source_minutes}m_to_{target_minutes}m",
                    "bar_count": len(bars),
                    "is_closed": True,
                    "forming": False,
                    "session_partial": session_partial,
                    "expected_minutes": target_minutes,
                    "actual_span_minutes": actual_span_minutes,
                    "pattern_eligible": not session_partial,
                    "session_authority": sessions.authority,
                    "session_authority_version": sessions.authority_version,
                    "projection_version": cls.VERSION,
                }
            )
        return out

    @classmethod
    def resample_weekly(
        cls, candles: Iterable[Dict[str, Any]], *, now: datetime | None = None
    ) -> List[Dict[str, Any]]:
        clock = cls._clock(now)
        latest_completed_week = CandleFreshnessService.expected_completed_week(clock)
        groups: Dict[tuple[int, int], list[tuple[datetime, Dict[str, Any]]]] = {}
        for candle in candles or []:
            dt = candle_datetime(candle)
            if dt is None:
                continue
            iso = dt.isocalendar()
            groups.setdefault((iso.year, iso.week), []).append((dt, dict(candle)))

        out: List[Dict[str, Any]] = []
        for week_key in sorted(groups):
            if week_key > latest_completed_week:
                continue
            chunk = sorted(groups[week_key], key=lambda item: item[0])
            monday = chunk[0][0].date() - timedelta(days=chunk[0][0].date().weekday())
            sunday = monday + timedelta(days=6)
            try:
                expected_days = cls._expected_trading_days(monday, sunday)
            except RuntimeError:
                continue
            actual_days = [item[0].date() for item in chunk]
            if len(actual_days) != len(set(actual_days)) or actual_days != expected_days:
                continue
            bars = [item[1] for item in chunk]
            if not all(cls._valid_ohlcv(row) for row in bars):
                continue
            highs = [float(row["high"]) for row in bars]
            lows = [float(row["low"]) for row in bars]
            out.append(
                {
                    "timestamp": chunk[0][0].isoformat(),
                    "period_end": chunk[-1][0].isoformat(),
                    "open": bars[0].get("open"),
                    "high": max(highs),
                    "low": min(lows),
                    "close": bars[-1].get("close"),
                    "volume": sum(float(row.get("volume") or 0) for row in bars),
                    "oi": bars[-1].get("oi"),
                    "source": "resampled_day_to_week",
                    "bar_count": len(bars),
                    "source_sessions": len(chunk),
                    "is_closed": True,
                    "forming": False,
                    "session_partial": False,
                    "pattern_eligible": True,
                    "projection_version": cls.VERSION,
                }
            )
        return out

    @classmethod
    def resample_monthly(
        cls, candles: Iterable[Dict[str, Any]], *, now: datetime | None = None
    ) -> List[Dict[str, Any]]:
        clock = cls._clock(now)
        latest_completed_month = CandleFreshnessService.expected_completed_month(clock)
        groups: Dict[tuple[int, int], list[tuple[datetime, Dict[str, Any]]]] = {}
        for candle in candles or []:
            dt = candle_datetime(candle)
            if dt is None:
                continue
            groups.setdefault((dt.year, dt.month), []).append((dt, dict(candle)))

        out: List[Dict[str, Any]] = []
        for month_key in sorted(groups):
            if month_key > latest_completed_month:
                continue
            chunk = sorted(groups[month_key], key=lambda item: item[0])
            year, month = month_key
            month_start = date(year, month, 1)
            month_end = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
            try:
                expected_days = cls._expected_trading_days(month_start, month_end)
            except RuntimeError:
                continue
            actual_days = [item[0].date() for item in chunk]
            if len(actual_days) != len(set(actual_days)) or actual_days != expected_days:
                continue
            bars = [item[1] for item in chunk]
            if not all(cls._valid_ohlcv(row) for row in bars):
                continue
            highs = [float(row["high"]) for row in bars]
            lows = [float(row["low"]) for row in bars]
            out.append(
                {
                    "timestamp": chunk[0][0].isoformat(),
                    "period_end": chunk[-1][0].isoformat(),
                    "open": bars[0].get("open"),
                    "high": max(highs),
                    "low": min(lows),
                    "close": bars[-1].get("close"),
                    "volume": sum(float(row.get("volume") or 0) for row in bars),
                    "oi": bars[-1].get("oi"),
                    "source": "resampled_day_to_month",
                    "bar_count": len(bars),
                    "source_sessions": len(chunk),
                    "is_closed": True,
                    "forming": False,
                    "session_partial": False,
                    "pattern_eligible": True,
                    "projection_version": cls.VERSION,
                }
            )
        return out

    @classmethod
    def completed_daily(
        cls, candles: Iterable[Dict[str, Any]], *, now: datetime | None = None
    ) -> List[Dict[str, Any]]:
        clock = cls._clock(now)
        sessions = DEFAULT_TRADING_SESSION_AUTHORITY
        current_window = sessions.session_window(clock.date()) if sessions.calendar_covered(clock.date()) else None
        current_session_closed = bool(current_window and clock > current_window.close_at())
        out: List[Dict[str, Any]] = []
        for candle in candles or []:
            dt = candle_datetime(candle)
            if dt is None:
                continue
            day = dt.date()
            # Decision-usable completed daily evidence must be provably tied to a
            # trading session and must satisfy the OHLCV contract. Historical
            # weekends/holidays or dates outside the effective-dated calendar are
            # not silently accepted as canonical evidence.
            if not sessions.calendar_covered(day) or not sessions.is_trading_day(day):
                continue
            if not cls._valid_ohlcv(dict(candle)):
                continue
            if day < clock.date() or (day == clock.date() and current_session_closed):
                out.append(
                    {
                        **dict(candle),
                        "is_closed": True,
                        "forming": False,
                        "session_partial": False,
                        "pattern_eligible": True,
                        "session_authority": sessions.authority,
                        "session_authority_version": sessions.authority_version,
                    }
                )
        return out

    @classmethod
    def completed_chart(
        cls,
        candles: Iterable[Dict[str, Any]],
        interval: Any,
        *,
        now: datetime | None = None,
    ) -> List[Dict[str, Any]]:
        rows = list(candles or [])
        tf = parse_timeframe(interval)
        clock = cls._clock(now)
        if tf == Timeframe.D1:
            return cls.completed_daily(rows, now=clock)
        if tf == Timeframe.W1:
            try:
                latest_completed = CandleFreshnessService.expected_completed_week(clock)
            except RuntimeError:
                # Outside proved current-calendar coverage, do not manufacture
                # current-period completeness. Historical rows before the
                # current week remain immutable/readable.
                current = clock.isocalendar()
                latest_completed = (int(current.year), int(current.week) - 1)
            return [
                row
                for row in rows
                if (dt := candle_datetime(row)) is not None
                and (int(dt.isocalendar().year), int(dt.isocalendar().week)) <= latest_completed
                and cls._valid_ohlcv(dict(row))
            ]
        if tf == Timeframe.MN1:
            try:
                latest_completed = CandleFreshnessService.expected_completed_month(clock)
            except RuntimeError:
                latest_completed = (clock.year, clock.month - 1) if clock.month > 1 else (clock.year - 1, 12)
            return [
                row
                for row in rows
                if (dt := candle_datetime(row)) is not None
                and (dt.year, dt.month) <= latest_completed
                and cls._valid_ohlcv(dict(row))
            ]
        return [row for row in closed_candles(rows, interval, now=clock) if cls._valid_ohlcv(dict(row))]

    @classmethod
    def derive_completed_session_daily(
        cls, intraday_rows: Iterable[Dict[str, Any]], *, now: datetime | None = None
    ) -> Dict[str, Any] | None:
        """Operational-only daily continuity from a completed intraday session."""
        clock = cls._clock(now)
        expected = CandleFreshnessService.expected_daily_date(clock)
        sessions = DEFAULT_TRADING_SESSION_AUTHORITY
        window = sessions.session_window(expected)
        if window is None:
            return None
        selected: list[tuple[datetime, Dict[str, Any]]] = []
        for row in intraday_rows or []:
            dt = candle_datetime(row)
            if dt is None or dt.date().isoformat() != expected:
                continue
            local = dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)
            if local < window.open_at() or local > window.close_at():
                continue
            selected.append((local, dict(row)))
        selected.sort(key=lambda item: item[0])
        if not selected:
            return None
        if not all(cls._valid_ohlcv(item[1]) for item in selected):
            return None
        # Derivation is allowed only from a complete regular source grid. Infer
        # one supported source cadence from actual timestamps; sparse arbitrary
        # endpoints are not sufficient evidence for a daily bar.
        distinct = [item[0].replace(second=0, microsecond=0) for item in selected]
        if len(distinct) != len(set(distinct)):
            return None
        diffs = [int((b-a).total_seconds()//60) for a,b in zip(distinct, distinct[1:]) if b > a]
        if not diffs:
            return None
        source_minutes = int(statistics.median(diffs))
        if source_minutes not in {1, 3, 5, 15}:
            return None
        expected_slots = [window.open_at() + timedelta(minutes=offset) for offset in range(0, int((window.close_at()-window.open_at()).total_seconds()//60), source_minutes)]
        if distinct != expected_slots:
            return None
        last_dt = selected[-1][0]
        if clock.date().isoformat() == expected and clock > window.close_at():
            if last_dt < (window.close_at() - timedelta(minutes=2)):
                return None
        bars = [item[1] for item in selected]
        opens = [float(row["open"]) for row in bars if row.get("open") is not None]
        highs = [float(row["high"]) for row in bars if row.get("high") is not None]
        lows = [float(row["low"]) for row in bars if row.get("low") is not None]
        closes = [float(row["close"]) for row in bars if row.get("close") is not None]
        if not opens or not highs or not lows or not closes:
            return None
        return {
            "timestamp": expected + "T00:00:00+05:30",
            "period_end": window.close_at().isoformat(timespec="seconds"),
            "provider_timestamp": last_dt.isoformat(),
            "open": opens[0],
            "high": max(highs),
            "low": min(lows),
            "close": closes[-1],
            "volume": sum(float(row.get("volume") or 0) for row in bars),
            "oi": bars[-1].get("oi"),
            "source": "derived_completed_session_intraday_to_day",
            "source_interval": f"{source_minutes}minute",
            "source_bar_count": len(bars),
            "expected_source_bar_count": len(expected_slots),
            "source_grid_complete": True,
            "operational_derived": True,
            "research_authority": False,
            "is_closed": True,
            "forming": False,
            "session_partial": False,
            "pattern_eligible": True,
            "projection_version": cls.VERSION,
        }

    @staticmethod
    def append_preferred_daily(
        daily_rows: Iterable[Dict[str, Any]], derived: Dict[str, Any] | None
    ) -> List[Dict[str, Any]]:
        rows = [dict(row) for row in daily_rows or []]
        if not derived:
            return rows
        by_date: Dict[str, Dict[str, Any]] = {}
        for row in rows + [dict(derived)]:
            dt = candle_datetime(row)
            key = dt.date().isoformat() if dt else str(row.get("timestamp") or "")[:10]
            if not key:
                continue
            prior = by_date.get(key)
            if prior is None or prior.get("operational_derived") is True:
                by_date[key] = row
        return [by_date[key] for key in sorted(by_date)]
