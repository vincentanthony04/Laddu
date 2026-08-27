"""
RateController: single source of truth for outbound-network backpressure.

Problem this solves
--------------------
Before this module, concurrency control was scattered:
  - a semaphore (_net_gate) capping total concurrent Upstox connections
  - a separate dict (_hist_revalidate_at) throttling background
    revalidation per (instrument_key, interval)
  - both lived as ad hoc instance attributes on LadduRuntime, so any new
    code path that opens an Upstox connection has to remember to use them
    correctly, by convention, with nothing enforcing it.

That's how WinError 10053 (socket resets under load) crept back in
before: a new caller forgot to acquire the gate.

What RateController does
--------------------------
- Wraps the concurrency cap as a context manager (`with rc.net_slot():`)
  so acquiring it is a one-line, hard-to-forget pattern instead of manual
  semaphore bookkeeping at every call site.
- Owns the revalidation throttle as a method (`rc.should_revalidate(key)`)
  instead of a bare dict any code can poke at directly.
- Exposes a snapshot() for /api/health so current in-flight connection
  count and throttle state are actually visible, not just inferred from
  symptoms.

This is intentionally dependency-free (stdlib only) so it can be dropped
into the existing threading-based runtime today, and ported to an
asyncio.Semaphore-based version later without changing its call sites'
shape (`with rc.net_slot():` / `async with rc.net_slot_async():`).
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Tuple

from core.timeframe import Timeframe, storage_interval


class SlotBusy(Exception):
    """Raised by net_slot() when a slot could not be acquired within the
    given timeout. Callers (esp. interactive/user-triggered ones) should
    catch this and fail fast -- e.g. serve cached data -- rather than let
    the caller (browser) hang until it aborts the socket (WinError 10053)."""


class RateController:
    def __init__(self, net_concurrency_cap: int = 8,
                 revalidate_min_interval_sec: float = 45.0,
                 interactive_reserved: int = 2):
        """v37.1: the single flat semaphore let background
        stale_while_revalidate traffic (which runs almost continuously)
        queue ahead of user clicks with no priority and no timeout, so a
        click could sit blocked for 20-70s until the browser gave up
        (WinError 10053). Now split into two pools out of the same total
        cap: `interactive_reserved` slots only background jobs can't touch,
        and the remainder shared by everyone. Interactive callers try the
        reserved pool first (instant if free), then fall back to the shared
        pool with a bounded wait. Background callers only ever use the
        shared pool, so they can never fully starve a click."""
        self.net_concurrency_cap = net_concurrency_cap
        interactive_reserved = max(0, min(interactive_reserved, net_concurrency_cap - 1))
        shared_cap = net_concurrency_cap - interactive_reserved
        self.interactive_reserved = interactive_reserved
        self.shared_capacity = shared_cap
        self._interactive_gate = threading.Semaphore(interactive_reserved) if interactive_reserved else None
        self._shared_gate = threading.Semaphore(shared_cap)
        self._in_flight = 0
        self._in_flight_lock = threading.RLock()

        self._revalidate_min_interval_sec = revalidate_min_interval_sec
        # v37.5: was one flat 45s window for EVERY interval, so a `day` candle
        # (which cannot change more than once a session) was re-fetched from
        # Upstox on the same cadence as a `1minute` candle -- confirmed in
        # backend.log: Nifty Bank/day refetched 4x in a 3.5-minute window.
        # Real trading systems gate revalidation on the timeframe's own
        # candle-close boundary, not a single arbitrary constant. Day interval
        # deliberately gets a long window (checked ~twice a session is enough:
        # once shortly after open, once after close) instead of continuous
        # polling all day for data that hasn't changed.
        # Keys are the canonical *storage* intervals because every public/provider
        # token is normalised through storage_interval() before lookup.  Keeping
        # provider spellings here (``day``, ``5minute``) silently collapsed every
        # interval to the generic 45-second fallback.
        self._revalidate_min_interval_by_key = {
            storage_interval(Timeframe.M1): 45.0,
            storage_interval(Timeframe.M3): 90.0,
            storage_interval(Timeframe.M5): 300.0,
            storage_interval(Timeframe.M15): 900.0,
            storage_interval(Timeframe.M30): 1800.0,
            storage_interval(Timeframe.H1): 3600.0,
            storage_interval(Timeframe.H4): 7200.0,
            storage_interval(Timeframe.D1): 21600.0,   # ~6h: near-open/after-close revalidation
            storage_interval(Timeframe.W1): 21600.0,
            storage_interval(Timeframe.MN1): 21600.0,
        }
        self._revalidate_at: Dict[Tuple[str, str], float] = {}
        self._revalidate_lock = threading.RLock()

        # simple rolling counters for visibility, not correctness
        self._total_acquired = 0
        self._total_waited_sec = 0.0
        self._total_busy_rejected = 0
        self._revalidate_successes = 0
        self._revalidate_failures = 0
        self._interactive_until = 0.0

    def prioritize_interactive(self, seconds: float = 2.0) -> None:
        """Give the selected stock a brief head start without freezing scanners.

        Interactive callers already have two reserved sockets.  A long global
        pause was observed starving exact-gap and intraday work for whole scan
        budgets, so the shared-lane pause is deliberately capped at two seconds.
        """
        bounded = max(0.25, min(2.0, float(seconds or 2.0)))
        with self._in_flight_lock:
            self._interactive_until = max(self._interactive_until, time.time() + bounded)

    @contextmanager
    def net_slot(self, priority: str = "interactive", timeout: float = 3.0):
        """Acquire one of the shared Upstox connection slots.
        Every code path that opens a real socket to Upstox -- historical
        fetch, live quote, deep-scan prefetch, fundamentals -- must go
        through this, no exceptions. That's what keeps the total number of
        concurrent sockets bounded regardless of how many logical thread
        pools exist above it.

        priority='interactive' (default): a real user click. Tries the
        reserved pool first (non-blocking), then the shared pool, waiting
        up to `timeout` seconds total. Raises SlotBusy on timeout instead
        of blocking indefinitely -- callers should catch this and fall
        back to cached data / a clear 'busy' response.
        priority='background': passive revalidation only. Never touches
        the reserved pool, waits up to `timeout` seconds on the shared
        pool, then raises SlotBusy (background work should just skip this
        cycle, not pile up)."""
        start = time.time()
        gate = None
        if priority == "background":
            with self._in_flight_lock:
                interactive_until = self._interactive_until
            if time.time() < interactive_until:
                self._total_busy_rejected += 1
                raise SlotBusy("background network work paused for selected-stock acquisition")
        if priority == "interactive" and self._interactive_gate is not None:
            if self._interactive_gate.acquire(blocking=False):
                gate = self._interactive_gate
        if gate is None:
            remaining = max(0.0, timeout - (time.time() - start))
            if not self._shared_gate.acquire(timeout=remaining):
                with self._in_flight_lock:
                    self._total_busy_rejected += 1
                raise SlotBusy(f"no net_slot available within {timeout}s (priority={priority})")
            gate = self._shared_gate
        waited = time.time() - start
        with self._in_flight_lock:
            self._in_flight += 1
            self._total_acquired += 1
            self._total_waited_sec += waited
        try:
            yield
        finally:
            with self._in_flight_lock:
                self._in_flight -= 1
            gate.release()

    def should_revalidate(self, instrument_key: str, interval: str) -> bool:
        """Return whether passive refresh is due without consuming the window.

        Older builds stamped the long timeframe cooldown before a socket was
        acquired. A SlotBusy/HTTP failure could therefore suppress a 60-minute
        retry for an hour and a daily retry for six hours. Single-flight locking
        already prevents duplicate jobs; the cooldown is now committed only
        after a successful provider response.
        """
        norm_interval = storage_interval(interval)
        key = (str(instrument_key), norm_interval)
        now = time.time()
        with self._revalidate_lock:
            return now >= float(self._revalidate_at.get(key, 0.0) or 0.0)

    def mark_revalidated(self, instrument_key: str, interval: str) -> None:
        norm_interval = storage_interval(interval)
        key = (str(instrument_key), norm_interval)
        delay = self._revalidate_min_interval_by_key.get(norm_interval, self._revalidate_min_interval_sec)
        with self._revalidate_lock:
            self._revalidate_at[key] = time.time() + delay
            self._revalidate_successes += 1

    def mark_revalidate_failure(self, instrument_key: str, interval: str, retry_after_sec: float = 8.0) -> None:
        """Short failure backoff; never convert one transient miss into hours."""
        norm_interval = storage_interval(interval)
        key = (str(instrument_key), norm_interval)
        with self._revalidate_lock:
            self._revalidate_at[key] = time.time() + max(1.0, min(float(retry_after_sec or 8.0), 30.0))
            self._revalidate_failures += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._in_flight_lock:
            avg_wait = (self._total_waited_sec / self._total_acquired) if self._total_acquired else 0.0
            return {
                "net_concurrency_cap": self.net_concurrency_cap,
                "interactive_reserved": self.interactive_reserved,
                "shared_capacity": self.shared_capacity,
                "interactive_priority_remaining_sec": round(max(0.0, self._interactive_until - time.time()), 3),
                "in_flight": self._in_flight,
                "total_acquired": self._total_acquired,
                "total_busy_rejected": self._total_busy_rejected,
                "avg_wait_ms": round(avg_wait * 1000, 1),
                "revalidate_min_interval_sec": self._revalidate_min_interval_sec,
                "revalidate_keys_tracked": len(self._revalidate_at),
                "revalidate_successes": self._revalidate_successes,
                "revalidate_failures": self._revalidate_failures,
                "interactive_priority_active": time.time() < self._interactive_until,
            }
