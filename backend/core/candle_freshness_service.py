"""Compatibility facade for canonical session/candle freshness authorities.

Legacy callers keep the ``CandleFreshnessService`` API, but no weekday/Friday
calendar math lives here.  TradingSessionAuthority decides which sessions
exist; CompletenessFreshnessAuthority decides decision-use completeness.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict

from core.india_time import INDIA_TZ
from session_candles import candle_datetime
from core.timeframe import Timeframe, interval_minutes, parse_timeframe
from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY
from core.completeness_freshness_authority import DEFAULT_COMPLETENESS_FRESHNESS_AUTHORITY


class CandleFreshnessService:
    sessions = DEFAULT_TRADING_SESSION_AUTHORITY
    completeness = DEFAULT_COMPLETENESS_FRESHNESS_AUTHORITY

    @classmethod
    def expected_intraday_date(cls, now: datetime) -> str:
        current = now.astimezone(INDIA_TZ) if now.tzinfo else now.replace(tzinfo=INDIA_TZ)
        if not cls.sessions.calendar_covered(current.date()):
            raise RuntimeError("intraday expected session unavailable: exchange calendar is unverified")
        window = cls.sessions.session_window(current.date())
        if window is not None and current >= window.open_at():
            return current.date().isoformat()
        return cls.sessions.previous_trading_day(current.date()).isoformat()

    @classmethod
    def expected_daily_date(cls, now: datetime) -> str:
        current = now.astimezone(INDIA_TZ) if now.tzinfo else now.replace(tzinfo=INDIA_TZ)
        if not cls.sessions.calendar_covered(current.date()):
            raise RuntimeError("daily expected session unavailable: exchange calendar is unverified")
        return cls.sessions.last_completed_trading_day(current).isoformat()

    @classmethod
    def _latest_completed_period_anchor(cls, now: datetime, interval: str):
        current = now.astimezone(INDIA_TZ) if now.tzinfo else now.replace(tzinfo=INDIA_TZ)
        completed = cls.sessions.last_completed_trading_day(current)
        if interval == "week":
            current_period_end = cls.sessions.last_trading_day_of_week(completed)
            if completed >= current_period_end:
                return completed
            return cls.sessions.previous_trading_day(completed - timedelta(days=completed.weekday()))
        if interval == "month":
            current_period_end = cls.sessions.last_trading_day_of_month(completed)
            if completed >= current_period_end:
                return completed
            first = completed.replace(day=1)
            return cls.sessions.previous_trading_day(first)
        raise ValueError(interval)

    @classmethod
    def expected_completed_week(cls, now: datetime) -> tuple[int, int]:
        anchor = cls._latest_completed_period_anchor(now, "week")
        iso = anchor.isocalendar()
        return int(iso.year), int(iso.week)

    @classmethod
    def expected_completed_month(cls, now: datetime) -> tuple[int, int]:
        anchor = cls._latest_completed_period_anchor(now, "month")
        return anchor.year, anchor.month

    @staticmethod
    def _raw_date(value: Any) -> str | None:
        match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
        return match.group(0) if match else None

    @classmethod
    def classify(cls, interval: str, last_candle: Dict[str, Any] | None, *, now: datetime) -> Dict[str, Any]:
        """Classify only completed bars; current daily/intraday bars are external."""
        now = now.astimezone(INDIA_TZ) if now.tzinfo else now.replace(tzinfo=INDIA_TZ)
        raw_ts = (
            (last_candle or {}).get("timestamp")
            or (last_candle or {}).get("time")
            or (last_candle or {}).get("date")
        )
        latest_dt = candle_datetime(raw_ts)
        if latest_dt and latest_dt.tzinfo:
            latest_dt = latest_dt.astimezone(INDIA_TZ)
        latest = latest_dt.date().isoformat() if latest_dt else cls._raw_date(raw_ts)
        tf = parse_timeframe(interval)
        daily = tf == Timeframe.D1
        weekly = tf == Timeframe.W1
        monthly = tf == Timeframe.MN1
        intraday = tf in {
            Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M10,
            Timeframe.M15, Timeframe.M30, Timeframe.H1, Timeframe.H4,
        }
        try:
            if daily:
                expected = cls.expected_daily_date(now)
            elif weekly:
                expected_week = cls.expected_completed_week(now)
                expected = datetime.fromisocalendar(*expected_week, 1).date().isoformat()
            elif monthly:
                expected_month = cls.expected_completed_month(now)
                expected = f"{expected_month[0]:04d}-{expected_month[1]:02d}-01"
            else:
                expected = cls.expected_intraday_date(now)
            calendar_verified = True
        except RuntimeError as exc:
            return {
                "authority": cls.completeness.authority,
                "authority_version": cls.completeness.authority_version,
                "session_authority": cls.sessions.authority,
                "session_authority_version": cls.sessions.authority_version,
                "latest_candle_date": latest,
                "latest_candle_time": latest_dt.isoformat() if latest_dt else None,
                "expected_candle_date": None,
                "expected_latest_closed_candle_time": None,
                "candle_lag_seconds": None,
                "max_candle_lag_seconds": None,
                "stale_candles": True,
                "stale_message": f"STALE CANDLES: {exc}",
                "freshness_state": "calendar_unverified",
                "usable_for_live_confirmation": False,
            }

        stale = False
        reason = None
        lag_seconds = None
        expected_latest = None
        max_lag_seconds = None

        if daily and last_candle:
            if latest_dt is None:
                stale, reason = True, "daily candle timestamp is missing or invalid"
            elif latest != expected:
                stale, reason = True, f"latest completed daily session {latest or 'unknown'}, expected {expected}"
        elif intraday and last_candle:
            if latest_dt is None:
                stale, reason = True, "intraday candle timestamp is missing or invalid"
            elif latest != expected:
                stale, reason = True, f"latest candle session {latest or 'unknown'}, expected {expected}"
            else:
                window = cls.sessions.session_window(now.date())
                if window is not None and now >= window.open_at() and latest_dt.date() == now.date():
                    effective_now = min(now, window.close_at())
                    minutes = max(1, interval_minutes(interval, default=5))
                    elapsed = max(0, int((effective_now - window.open_at()).total_seconds() // 60))
                    completed = elapsed // minutes
                    if completed > 0:
                        expected_start = window.open_at() + timedelta(minutes=(completed - 1) * minutes)
                        expected_latest = expected_start.isoformat()
                        lag_seconds = max(0, int((effective_now - (latest_dt + timedelta(minutes=minutes))).total_seconds()))
                        max_lag_seconds = max(180, minutes * 60 + 120)
                        if lag_seconds > max_lag_seconds:
                            stale = True
                            reason = (
                                f"latest closed candle {latest_dt.strftime('%H:%M')} IST; "
                                f"expected around {expected_start.strftime('%H:%M')} IST"
                            )
        elif weekly and last_candle:
            if latest_dt is None:
                stale, reason = True, "weekly candle timestamp is missing or invalid"
            else:
                latest_period = latest_dt.isocalendar()
                latest_week = (int(latest_period.year), int(latest_period.week))
                expected_week = cls.expected_completed_week(now)
                if latest_week != expected_week:
                    stale, reason = True, (
                        f"latest completed weekly period {latest_week[0]}-W{latest_week[1]:02d}, "
                        f"expected {expected_week[0]}-W{expected_week[1]:02d}"
                    )
        elif monthly and last_candle:
            if latest_dt is None:
                stale, reason = True, "monthly candle timestamp is missing or invalid"
            else:
                latest_month = (latest_dt.year, latest_dt.month)
                expected_month = cls.expected_completed_month(now)
                if latest_month != expected_month:
                    stale, reason = True, (
                        f"latest completed monthly period {latest_month[0]}-{latest_month[1]:02d}, "
                        f"expected {expected_month[0]}-{expected_month[1]:02d}"
                    )

        return {
            "authority": cls.completeness.authority,
            "authority_version": cls.completeness.authority_version,
            "session_authority": cls.sessions.authority,
            "session_authority_version": cls.sessions.authority_version,
            "calendar_verified": calendar_verified,
            "latest_candle_date": latest,
            "latest_candle_time": latest_dt.isoformat() if latest_dt else None,
            "expected_candle_date": expected,
            "expected_latest_closed_candle_time": expected_latest,
            "candle_lag_seconds": lag_seconds,
            "max_candle_lag_seconds": max_lag_seconds,
            "stale_candles": stale,
            "stale_message": f"STALE CANDLES: {reason}" if stale else None,
            "freshness_state": "stale" if stale else ("fresh" if last_candle and latest_dt else "unknown"),
            "usable_for_live_confirmation": bool(last_candle and latest_dt and not stale),
        }
