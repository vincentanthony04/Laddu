"""Priority-aware workload admission for Project Laddu.

The governor protects interactive stock intelligence, live market/risk work and
operator controls from bulk history/research activity.  It is intentionally
small and deterministic: it does not change trading maths, model weights,
canonical decisions or risk limits.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Dict


@dataclass(frozen=True)
class PriorityTier:
    name: str
    rank: int
    description: str


TIERS = {
    "P0": PriorityTier("P0", 0, "risk, open Model Paper positions and ultra-scalp selected stock"),
    "P1": PriorityTier("P1", 1, "selected stocks and required market/sector indices"),
    "P2": PriorityTier("P2", 2, "Intraday universe and live tick processing"),
    "P3": PriorityTier("P3", 3, "active Delivery scanner batch"),
    "P4": PriorityTier("P4", 4, "operational history convergence"),
    "P5": PriorityTier("P5", 5, "research, ML and deep listing-age backfill"),
}


class WorkloadGovernor:
    VERSION = "workload-governor-1.4.0-generation-authority-pressure"

    def __init__(self, app: Any):
        self.app = app
        self._lock = threading.RLock()
        self._interactive_until = 0.0
        self._selected_symbol = ""
        self._selected_mode = "delivery"
        self._selected_instrument_key = ""
        self._selected_interval = "day"
        self._last_activation_at = 0.0
        self._activation_count = 0
        self._bulk_yields = 0
        self._last_yield_reason = ""
        self._manual_bulk_pause_until = 0.0

    def activate_selected(self, symbol: str, mode: str, *, ttl_seconds: float = 45.0, instrument_key: str = "", interval: str = "") -> Dict[str, Any]:
        symbol = str(symbol or "").strip().upper()
        mode = str(mode or "delivery").strip().lower()
        ttl = max(5.0, min(float(ttl_seconds or 45.0), 180.0))
        now = time.time()
        with self._lock:
            self._selected_symbol = symbol
            self._selected_mode = mode
            if instrument_key:
                self._selected_instrument_key = str(instrument_key).strip()
            if interval:
                self._selected_interval = str(interval).strip()
            elif mode == "intraday":
                self._selected_interval = "5minute"
            self._interactive_until = max(self._interactive_until, now + ttl)
            self._last_activation_at = now
            self._activation_count += 1
        try:
            self.app.rate.prioritize_interactive(min(2.0, ttl))
        except Exception:
            pass
        # Foreground activation is deliberately O(1).  Older builds returned
        # ``snapshot()`` here even though every caller ignored it; snapshot()
        # samples PostgreSQL pools and scanner state and could therefore turn a
        # chart/history click into a blocking observability request.  Operator
        # surfaces still call snapshot() explicitly when they actually need it.
        return {
            "version": self.VERSION,
            "selected_stock": symbol or None,
            "selected_mode": mode,
            "selected_interval": self._selected_interval,
            "interactive_priority_active": True,
            "activation_count": self._activation_count,
        }

    def activate_surface(self, surface: str, *, ttl_seconds: float = 3.0) -> Dict[str, Any]:
        """Constant-time P1 protection for a latency-critical UI read."""
        ttl = max(1.0, min(float(ttl_seconds or 3.0), 15.0))
        now = time.time()
        with self._lock:
            self._interactive_until = max(self._interactive_until, now + ttl)
            self._last_activation_at = now
            self._activation_count += 1
            self._last_yield_reason = f"interactive surface {str(surface or 'ui')[:80]}"
        try:
            self.app.rate.prioritize_interactive(min(2.0, ttl))
        except Exception:
            pass
        return {"version": self.VERSION, "surface": surface, "interactive_priority_active": True}

    def pause_bulk(self, *, seconds: float = 60.0, reason: str = "operator") -> Dict[str, Any]:
        bounded = max(5.0, min(float(seconds or 60.0), 900.0))
        with self._lock:
            self._manual_bulk_pause_until = max(self._manual_bulk_pause_until, time.time() + bounded)
            self._last_yield_reason = str(reason or "operator")[:240]
        return self.snapshot()

    def resume_bulk(self) -> Dict[str, Any]:
        with self._lock:
            self._manual_bulk_pause_until = 0.0
            self._last_yield_reason = ""
        return self.snapshot()

    @staticmethod
    def _authority_pressure(authority: Any) -> Dict[str, Any]:
        try:
            health = dict(authority.pool_health() or {})
            stats = dict(health.get("stats") or {})
            recovery = dict(health.get("recovery") or {})
        except Exception as exc:
            health = {
                "state": "UNAVAILABLE",
                "usable": False,
                "last_pool_error": f"health_read_failed:{type(exc).__name__}",
            }
            stats = {}
            recovery = {"state": "UNAVAILABLE"}
        size = int(stats.get("pool_size") or 0)
        available = int(stats.get("pool_available") or 0)
        waiting = int(stats.get("requests_waiting") or 0)
        queued = int(stats.get("requests_queued") or 0)
        canonical_health = any(key in health for key in ("state", "usable", "recovery"))
        if canonical_health:
            state = str(health.get("state") or recovery.get("state") or "UNAVAILABLE").upper()
            recovery_state = str(recovery.get("state") or state).upper()
            usable = health.get("usable") is True
        else:
            # Compatibility for focused unit-test doubles. Installed authorities
            # always publish canonical generation state and never infer liveness
            # from capacity statistics.
            state = "HEALTHY" if size > 0 else "UNAVAILABLE"
            recovery_state = state
            usable = size > 0
        recovering = state in {"STARTING", "DEGRADED", "RECOVERING", "UNAVAILABLE"}
        saturated = bool(size > 0 and available <= 0 and waiting > 0)
        unavailable = bool(not usable and state != "CLOSED")
        pressured = bool(
            saturated
            or waiting >= 1
            or recovering
            or unavailable
            or (size > 0 and available / max(1, size) <= 0.125)
        )
        return {
            "pool_size": size, "pool_available": available,
            "requests_waiting": waiting, "requests_queued": queued,
            "pressured": pressured, "saturated": saturated,
            "recovering": recovering, "usable": usable,
            "state": state, "recovery_state": recovery_state,
            "pool_generation": int(recovery.get("pool_generation") or 0),
            "recovery_epoch": int(recovery.get("recovery_epoch") or 0),
            "replacement_policy": recovery.get("pool_replacement_policy"),
            "admission_waiters": int(recovery.get("admission_waiters") or 0),
            "reconnect_failures": int(health.get("reconnect_failures") or 0),
            "last_pool_error": health.get("last_pool_error"),
        }

    def _pool_pressure(self) -> Dict[str, Any]:
        plane = getattr(self.app, "production_data_plane", None)
        operational = self._authority_pressure(getattr(plane, "operational", None))
        interactive = self._authority_pressure(getattr(plane, "interactive", None))
        governance = self._authority_pressure(getattr(plane, "governance", None))
        governance_read = self._authority_pressure(getattr(plane, "governance_read", None))
        authorities = (operational, interactive, governance, governance_read)
        return {
            "pool_size": sum(row["pool_size"] for row in authorities),
            "pool_available": sum(row["pool_available"] for row in authorities),
            "requests_waiting": sum(row["requests_waiting"] for row in authorities),
            "requests_queued": sum(row["requests_queued"] for row in authorities),
            "admission_waiters": sum(row["admission_waiters"] for row in authorities),
            "pressured": any(row["pressured"] for row in authorities),
            "saturated": any(row["saturated"] for row in authorities),
            "required_database_recovery": any(row["recovering"] or not row["usable"] for row in authorities),
            "operational": operational,
            "interactive": interactive,
            "governance": governance,
            "governance_read": governance_read,
        }

    def interactive_active(self) -> bool:
        with self._lock:
            return time.time() < self._interactive_until

    def should_yield(self, tier: str, *, record: bool = True) -> tuple[bool, str]:
        tier = str(tier or "P5").upper()
        rank = TIERS.get(tier, TIERS["P5"]).rank
        now = time.time()
        with self._lock:
            manual_pause = now < self._manual_bulk_pause_until
            interactive = now < self._interactive_until
        pressure = self._pool_pressure()
        reason = ""
        should = False
        # Any background desk at P3 or lower must yield to a selected-stock
        # P0/P1 request or database pressure.  This restores the immediate local
        # read/priority-enrichment behaviour without reverting to SQLite authority.
        if rank >= TIERS["P3"].rank and manual_pause:
            should, reason = True, "manual background-work pause active"
        elif rank >= TIERS["P3"].rank and interactive:
            should, reason = True, f"selected-stock priority active for {self._selected_symbol or 'interactive request'}"
        elif rank >= TIERS["P3"].rank and (pressure.get("interactive") or {}).get("pressured"):
            should, reason = True, "interactive PostgreSQL pool pressure"
        elif rank >= TIERS["P3"].rank and (pressure.get("operational") or {}).get("pressured"):
            should, reason = True, "operational PostgreSQL pool pressure"
        elif rank >= TIERS["P3"].rank and (pressure.get("governance") or {}).get("pressured"):
            should, reason = True, "governance PostgreSQL pool pressure"
        elif rank >= TIERS["P3"].rank and (pressure.get("governance_read") or {}).get("pressured"):
            should, reason = True, "governance read PostgreSQL pool pressure"
        elif rank >= TIERS["P3"].rank and pressure.get("required_database_recovery"):
            should, reason = True, "required PostgreSQL authority recovering"
        elif rank >= TIERS["P5"].rank and self._scanner_pressure():
            should, reason = True, "scanner analysis capacity saturated"
        if should and record:
            with self._lock:
                self._bulk_yields += 1
                self._last_yield_reason = reason
        return should, reason

    def _scanner_pressure(self) -> bool:
        try:
            scanner = dict((self.app.status.get("mode_scanners") or {}).get("delivery") or {})
            analysis = dict(scanner.get("analysis") or {})
            workers = dict(analysis.get("analysis_workers") or {})
            return str(workers.get("state") or "").upper() == "SATURATED" or int(workers.get("available") or 0) <= 0 and int(workers.get("active") or 0) > 0
        except Exception:
            return False

    def history_policy(self, base: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(base or {})
        should, reason = self.should_yield("P5")
        if should:
            out.update({
                "state": "yielding_to_higher_priority",
                "batch_size": 0,
                "workers": 0,
                "cycle_sleep_seconds": max(2, min(int(out.get("cycle_sleep_seconds") or 4), 10)),
                "yield_reason": reason,
            })
            return out
        pressure = self._pool_pressure()
        if pressure.get("requests_waiting"):
            out["batch_size"] = min(int(out.get("batch_size") or 1), 4)
            out["workers"] = 1
            out["state"] = "running_throttled_for_database"
        return out

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            remaining = max(0.0, self._interactive_until - now)
            manual_remaining = max(0.0, self._manual_bulk_pause_until - now)
            selected_symbol = self._selected_symbol
            selected_mode = self._selected_mode
            selected_instrument_key = self._selected_instrument_key
            selected_interval = self._selected_interval
            last_activation = self._last_activation_at
            activations = self._activation_count
            yields = self._bulk_yields
            last_reason = self._last_yield_reason
        return {
            "version": self.VERSION,
            "selected_stock": selected_symbol or None,
            "selected_mode": selected_mode,
            "selected_instrument_key": selected_instrument_key or None,
            "selected_interval": selected_interval,
            "interactive_priority_active": remaining > 0,
            "interactive_priority_remaining_sec": round(remaining, 1),
            "manual_bulk_pause_remaining_sec": round(manual_remaining, 1),
            "last_activation_at_epoch": last_activation or None,
            "activation_count": activations,
            "bulk_yield_count": yields,
            "last_yield_reason": last_reason or None,
            "database_pressure": self._pool_pressure(),
            "scanner_saturated": self._scanner_pressure(),
            "tiers": {key: {"rank": row.rank, "description": row.description} for key, row in TIERS.items()},
        }
