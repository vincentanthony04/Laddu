"""Portable India Standard Time helpers.

India has used a fixed UTC+05:30 offset for the market sessions supported by
Project Laddu.  Using a fixed-offset tzinfo avoids a hard runtime dependency on
the optional Windows IANA tzdata package while preserving the public
"Asia/Kolkata" label in API metadata.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

MARKET_TIMEZONE_NAME = "Asia/Kolkata"
INDIA_TZ = timezone(timedelta(hours=5, minutes=30), name="IST")


def india_now() -> datetime:
    return datetime.now(INDIA_TZ)


def as_india(value: datetime, *, assume_tz: Optional[timezone] = timezone.utc) -> datetime:
    """Return *value* in IST without consulting the operating-system tz DB.

    Naive timestamps are interpreted with ``assume_tz`` (UTC by default),
    because provider/account timestamps without an offset must never be
    silently interpreted using the Windows machine's local timezone.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=assume_tz or timezone.utc)
    return value.astimezone(INDIA_TZ)


def trading_date_ist(value: Optional[datetime] = None) -> str:
    """Return the canonical NSE/India calendar date.

    Business-date filters must not depend on the host OS timezone or SQLite
    UTC ``date('now')`` semantics.
    """
    current = value or india_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=INDIA_TZ)
    else:
        current = current.astimezone(INDIA_TZ)
    return current.date().isoformat()
