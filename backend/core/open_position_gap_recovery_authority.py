"""AC-074 canonical open-position market-data gap recovery authority.

A live Model-Paper position must never skip across an unobserved market-data
window and then continue as if target/stop/trailing levels could not have been
crossed.  This pure authority replays canonical one-minute OHLC evidence
between the last durable market observation and the newly verified quote.

The policy is deliberately conservative:
* complete bars can prove that a level was not touched;
* stop/target touches are resolved chronologically by bar;
* same-bar stop+target ambiguity is stop-first;
* a touch in the first partial minute is marked ambiguous because the bar may
  include price action that occurred before the last durable observation;
* missing bar coverage or ambiguous trail activation/order never becomes a
  silent "no touch"; it requires an unscorable conservative exit.

No broker execution authority exists here.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from core.india_time import INDIA_TZ
from core.intrabar_execution_policy import DEFAULT_INTRABAR_EXECUTION_POLICY
from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        out = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            out = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=INDIA_TZ)
    return out.astimezone(INDIA_TZ)


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class OpenPositionGapRecoveryAuthority:
    authority = "OpenPositionGapRecoveryAuthority"
    authority_version = "1.0.0"
    broker_authority = "NONE"
    bar_interval = "1m"
    threshold_seconds = {"intraday": 8.0, "delivery": 75.0}

    @classmethod
    def observation_time(cls, quote: Mapping[str, Any] | None) -> datetime | None:
        row = dict(quote or {})
        value = (
            row.get("provider_timestamp") or row.get("provider_ts")
            or row.get("source_time") or row.get("timestamp")
        )
        if value in (None, "") and row.get("provider_ts_ms") not in (None, ""):
            try:
                return datetime.fromtimestamp(float(row["provider_ts_ms"]) / 1000.0, tz=INDIA_TZ).astimezone(INDIA_TZ)
            except (TypeError, ValueError, OSError, OverflowError):
                return None
        return _dt(value)

    @classmethod
    def observation_sequence(cls, quote: Mapping[str, Any] | None) -> int | None:
        row = dict(quote or {})
        for key in ("canonical_sequence", "quote_seq", "delta_id", "provider_sequence"):
            try:
                value = int(row.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
        return None

    @classmethod
    def needs_recovery(cls, row: Mapping[str, Any], current_quote: Mapping[str, Any]) -> bool:
        position = dict(row or {})
        mode = str(position.get("mode") or "").lower()
        # AC-074 is activated only after the forward PostgreSQL watermark exists.
        # Legacy/local fixtures without that relational field retain their prior
        # quote lifecycle semantics; production migration 023 backfills every row.
        if position.get("last_market_observation_at") in (None, ""):
            return False
        last_at = _dt(position.get("last_market_observation_at"))
        current_at = cls.observation_time(current_quote)
        if last_at is None or current_at is None or current_at <= last_at:
            return True
        return (current_at - last_at).total_seconds() > float(cls.threshold_seconds.get(mode, 8.0))

    @classmethod
    def _expected_bucket_starts(cls, start: datetime, end: datetime) -> list[datetime] | None:
        if end <= start:
            return []
        cursor_day = start.date()
        out: list[datetime] = []
        for _ in range(370):
            if cursor_day > end.date():
                return out
            if not DEFAULT_TRADING_SESSION_AUTHORITY.calendar_covered(cursor_day):
                return None
            window = DEFAULT_TRADING_SESSION_AUTHORITY.session_window(cursor_day)
            if window is not None:
                opened, closed = window.open_at(), window.close_at()
                left = max(start, opened)
                right = min(end, closed)
                if right > left:
                    bucket = left.replace(second=0, microsecond=0)
                    if bucket < opened:
                        bucket = opened
                    while bucket <= right.replace(second=0, microsecond=0):
                        out.append(bucket)
                        bucket += timedelta(minutes=1)
            cursor_day += timedelta(days=1)
        return None

    @classmethod
    def _bars(cls, rows: Iterable[Mapping[str, Any]], start: datetime, end: datetime) -> dict[datetime, dict[str, Any]]:
        out: dict[datetime, dict[str, Any]] = {}
        for raw in rows or ():
            row = dict(raw or {})
            stamp = _dt(row.get("timestamp") or row.get("bar_start_ts") or row.get("bar_start"))
            high, low, close = _number(row.get("high")), _number(row.get("low")), _number(row.get("close"))
            if stamp is None or high is None or low is None or close is None or high < low:
                continue
            stamp = stamp.replace(second=0, microsecond=0)
            if stamp < start.replace(second=0, microsecond=0) or stamp > end.replace(second=0, microsecond=0):
                continue
            if row.get("quality_state") in {"REJECTED", "CORRUPT", "UNVERIFIED"}:
                continue
            out[stamp] = {**row, "_stamp": stamp, "high": high, "low": low, "close": close}
        return out

    @classmethod
    def evaluate(
        cls,
        row: Mapping[str, Any],
        current_quote: Mapping[str, Any],
        canonical_minute_bars: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        position = dict(row or {})
        current = dict(current_quote or {})
        mode = str(position.get("mode") or "").lower()
        last_at = _dt(position.get("last_market_observation_at") or position.get("opened_at"))
        current_at = cls.observation_time(current)
        result = {
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "broker_authority": cls.broker_authority,
            "bar_interval": cls.bar_interval,
            "state": "NO_RECOVERY_REQUIRED",
            "recovery_required": False,
            "allow_current_quote": True,
            "exit_required": False,
            "unscorable": False,
            "last_market_observation_at": last_at.isoformat() if last_at else None,
            "current_market_observation_at": current_at.isoformat() if current_at else None,
            "current_market_observation_sequence": cls.observation_sequence(current),
        }
        if last_at is None or current_at is None or current_at <= last_at:
            result.update({
                "state": "AMBIGUOUS_OBSERVATION_CLOCK",
                "recovery_required": True,
                "allow_current_quote": False,
                "exit_required": True,
                "unscorable": True,
                "exit_reason": "MARKET_DATA_GAP_CLOCK_AMBIGUOUS",
            })
            return result
        gap_seconds = (current_at - last_at).total_seconds()
        result["gap_seconds"] = round(gap_seconds, 3)
        if gap_seconds <= float(cls.threshold_seconds.get(mode, 8.0)):
            return result

        result["recovery_required"] = True
        expected = cls._expected_bucket_starts(last_at, current_at)
        bars = cls._bars(canonical_minute_bars, last_at, current_at)
        if expected is None:
            result.update({
                "state": "AMBIGUOUS_CALENDAR_COVERAGE",
                "allow_current_quote": False, "exit_required": True, "unscorable": True,
                "exit_reason": "MARKET_DATA_GAP_CALENDAR_AMBIGUOUS",
            })
            return result
        missing = [stamp for stamp in expected if stamp not in bars]
        result["expected_bar_count"] = len(expected)
        result["observed_bar_count"] = len(expected) - len(missing)
        if missing:
            result.update({
                "state": "AMBIGUOUS_MISSING_CANONICAL_BARS",
                "allow_current_quote": False, "exit_required": True, "unscorable": True,
                "exit_reason": "MARKET_DATA_GAP_COVERAGE_AMBIGUOUS",
                "missing_bar_starts": [stamp.isoformat() for stamp in missing[:20]],
            })
            return result

        side = str(position.get("side") or "").upper()
        long = side == "LONG"
        target = float(position["original_target"])
        original_stop = float(position["original_stop"])
        managed_stop = float(position.get("managed_stop") or original_stop)
        entry = float(position.get("entry_price") or position.get("original_entry"))
        initial_risk = abs(float(position.get("original_entry") or entry) - original_stop)
        high_water = float(position.get("high_watermark") or entry)
        low_water = float(position.get("low_watermark") or entry)
        first_partial = last_at.second != 0 or last_at.microsecond != 0
        trail_actions: list[dict[str, Any]] = []

        for index, stamp in enumerate(expected):
            bar = bars[stamp]
            high, low = float(bar["high"]), float(bar["low"])
            target_hit = high >= target if long else low <= target
            stop_hit = low <= managed_stop if long else high >= managed_stop

            # The first minute may contain observations before the durable
            # watermark. A touch in that mixed bucket cannot be ordered safely.
            if index == 0 and first_partial and (target_hit or stop_hit):
                result.update({
                    "state": "AMBIGUOUS_FIRST_PARTIAL_BAR_TOUCH",
                    "allow_current_quote": False, "exit_required": True, "unscorable": True,
                    "exit_reason": "MARKET_DATA_GAP_FIRST_BAR_AMBIGUOUS",
                    "ambiguous_bar_start": stamp.isoformat(),
                })
                return result

            resolution = DEFAULT_INTRABAR_EXECUTION_POLICY.resolve(stop_hit=stop_hit, target_hit=target_hit)
            if resolution.get("outcome") == "STOP":
                reason = "MARKET_DATA_GAP_STOP_HIT" if abs(managed_stop - original_stop) < 1e-9 else "MARKET_DATA_GAP_MANAGED_STOP_HIT"
                result.update({
                    "state": "RECOVERED_EXIT", "allow_current_quote": False, "exit_required": True,
                    "unscorable": bool(resolution.get("ambiguous")), "exit_reason": reason,
                    "exit_price": managed_stop, "resolved_bar_start": stamp.isoformat(),
                    "intrabar_resolution": resolution, "managed_stop": managed_stop,
                    "high_watermark": max(high_water, high), "low_watermark": min(low_water, low),
                })
                return result
            if resolution.get("outcome") == "TARGET":
                result.update({
                    "state": "RECOVERED_EXIT", "allow_current_quote": False, "exit_required": True,
                    "unscorable": False, "exit_reason": "MARKET_DATA_GAP_TARGET_HIT",
                    "exit_price": target, "resolved_bar_start": stamp.isoformat(),
                    "intrabar_resolution": resolution, "managed_stop": managed_stop,
                    "high_watermark": max(high_water, high), "low_watermark": min(low_water, low),
                })
                return result

            high_water = max(high_water, high)
            low_water = min(low_water, low)
            if initial_risk > 0:
                favorable = (high_water - entry) if long else (entry - low_water)
                proposed = managed_stop
                action = None
                if favorable >= 1.25 * initial_risk:
                    proposed = entry + (0.5 * initial_risk if long else -0.5 * initial_risk)
                    action = "TRAIL STOP"
                elif favorable >= 0.75 * initial_risk:
                    proposed = entry
                    action = "PROTECT AT BREAKEVEN"
                new_stop = max(managed_stop, proposed) if long else min(managed_stop, proposed)
                if action and abs(new_stop - managed_stop) > 1e-9:
                    # If the same bar both creates and touches the tighter stop,
                    # the order is unknowable from OHLC. Do not manufacture the
                    # favourable sequence; close unscorable at the current
                    # verified quote in the caller.
                    new_stop_touched = low <= new_stop if long else high >= new_stop
                    if new_stop_touched:
                        result.update({
                            "state": "AMBIGUOUS_TRAIL_ACTIVATION_ORDER",
                            "allow_current_quote": False, "exit_required": True, "unscorable": True,
                            "exit_reason": "MARKET_DATA_GAP_TRAIL_ORDER_AMBIGUOUS",
                            "ambiguous_bar_start": stamp.isoformat(),
                            "managed_stop": managed_stop,
                            "proposed_managed_stop": new_stop,
                            "high_watermark": high_water, "low_watermark": low_water,
                        })
                        return result
                    managed_stop = new_stop
                    trail_actions.append({"at": stamp.isoformat(), "action": action, "managed_stop": managed_stop})

        result.update({
            "state": "RECOVERED_NO_TOUCH",
            "allow_current_quote": True,
            "managed_stop": managed_stop,
            "high_watermark": high_water,
            "low_watermark": low_water,
            "trail_actions": trail_actions,
        })
        return result


DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY = OpenPositionGapRecoveryAuthority()
