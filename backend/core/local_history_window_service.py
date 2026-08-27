"""Bounded local historical-window read selection for interactive consumers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

SERVICE_VERSION = "local-history-window-read-1.0.0-indexed-local-authority"


def read_local_history_window(
    store,
    instrument_key: str | None,
    interval: str,
    *,
    days: int,
    limit: int,
    recent_only: bool = False,
    fallback: Callable[..., list] | None = None,
) -> list:
    """Read the established local tail without provider I/O or broad file discovery.

    ``since`` is a cold-file pruning hint only. The repository is responsible
    for deterministic indexed expansion when the requested tail requires older
    retained files, so this helper cannot weaken candle-depth semantics.
    """
    if not instrument_key:
        return []
    if recent_only:
        reader = getattr(store, "get_recent_candles", None)
        return list(reader(instrument_key, interval, limit=limit) or []) if callable(reader) else []
    reader = getattr(store, "get_candles_window", None)
    if callable(reader):
        cushion_days = max(7, min(45, int(days) // 4))
        since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)) + cushion_days)).isoformat()
        return list(reader(instrument_key, interval, since=since, limit=limit) or [])
    return list(fallback(instrument_key, interval, limit=limit) or []) if callable(fallback) else []
