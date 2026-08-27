"""Bounded first-useful-mode bootstrap for the next NSE session.

This service does not create a new trading authority. It prioritises a small,
liquidity-ranked Delivery research cohort through the existing canonical
history, scanner, decision and Model Paper authorities so the product can
produce useful completed-session research while the full universe continues
in the background.

Safety boundaries:
- Delivery research only before the market opens; Intraday remains session-bound.
- No broker authority and no ML production influence.
- The full immutable Delivery population remains the canonical universe.
- Cohort rows are priority scheduling only, never a replacement universe.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Dict, Iterable, List

from core.market_clock import india_now
from models import now_iso


SERVICE_VERSION = "first-useful-mode-1.0.0"
STATE_KEY = "first_useful_mode:state"
COHORT_KEY = "first_useful_mode:cohort"
DEFAULT_COHORT_SIZE = 96
DEFAULT_BATCH_SIZE = 12
MIN_DAILY_BARS = 252
MIN_30M_BARS = 60
MIN_RESEARCH_ROWS = 5


@dataclass(frozen=True)
class CohortRow:
    symbol: str
    instrument_key: str
    exchange: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "symbol": self.symbol,
            "instrument_key": self.instrument_key,
            "exchange": self.exchange,
        }


class FirstUsefulModeService:
    """Prioritise one bounded research cohort through existing authorities."""

    def __init__(self, host: Any):
        self.host = host
        self.store = host.store
        self._status_cache: Dict[str, Any] | None = None
        self._status_cache_at = 0.0
        self._status_lock = threading.Lock()

    @staticmethod
    def _symbol(row: Dict[str, Any]) -> str:
        return str(row.get("trading_symbol") or row.get("symbol") or "").upper().strip()

    @staticmethod
    def _key(row: Dict[str, Any]) -> str:
        return str(row.get("instrument_key") or row.get("provider_instrument_key") or "").strip()

    @staticmethod
    def _exchange(row: Dict[str, Any]) -> str:
        return str(row.get("exchange") or row.get("segment") or "NSE").upper().strip()

    @classmethod
    def _eligible_row(cls, row: Dict[str, Any]) -> bool:
        symbol = cls._symbol(row)
        key = cls._key(row)
        exchange = cls._exchange(row)
        instrument_type = str(row.get("instrument_type") or row.get("asset_class") or "EQ").upper().strip()
        option_type = str(row.get("option_type") or "").strip()
        return bool(
            symbol
            and key
            and exchange.startswith("NSE")
            and option_type == ""
            and instrument_type not in {"FUT", "FUTIDX", "FUTSTK", "CE", "PE", "OPTIDX", "OPTSTK"}
        )

    def _snapshot_id(self) -> str:
        authority = dict(getattr(self.host, "status", {}).get("universe_authority") or {})
        return str(dict(authority.get("snapshots") or {}).get("delivery", {}).get("snapshot_id") or "")

    def _load_saved_cohort(self) -> List[CohortRow]:
        try:
            saved = dict(self.store.get_kv(COHORT_KEY, {}) or {})
        except Exception:
            return []
        if saved.get("snapshot_id") != self._snapshot_id():
            return []
        rows = []
        for raw in saved.get("rows") or []:
            if not isinstance(raw, dict):
                continue
            row = CohortRow(
                symbol=str(raw.get("symbol") or "").upper().strip(),
                instrument_key=str(raw.get("instrument_key") or "").strip(),
                exchange=str(raw.get("exchange") or "NSE").upper().strip(),
            )
            if row.symbol and row.instrument_key:
                rows.append(row)
        return rows

    def _source_rows(self, limit: int) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        try:
            rows = list(self.host.immutable_scan_population("delivery") or [])
        except Exception:
            rows = []
        if not rows:
            try:
                rows = list(self.store.liquid_wide_universe(limit=max(limit * 3, 250)) or [])
            except Exception:
                rows = []
        return rows

    def cohort(self, cohort_size: int = DEFAULT_COHORT_SIZE) -> List[CohortRow]:
        cohort_size = max(24, min(int(cohort_size or DEFAULT_COHORT_SIZE), 150))
        saved = self._load_saved_cohort()
        if len(saved) >= min(24, cohort_size):
            return saved[:cohort_size]

        seen: set[str] = set()
        cohort: List[CohortRow] = []
        for raw in self._source_rows(cohort_size):
            if not self._eligible_row(raw):
                continue
            symbol = self._symbol(raw)
            if symbol in seen:
                continue
            seen.add(symbol)
            cohort.append(CohortRow(symbol, self._key(raw), self._exchange(raw)))
            if len(cohort) >= cohort_size:
                break

        payload = {
            "version": SERVICE_VERSION,
            "snapshot_id": self._snapshot_id(),
            "created_at": now_iso(),
            "cohort_size": len(cohort),
            "rows": [row.as_dict() for row in cohort],
            "policy": "priority scheduling only; full immutable Delivery population remains authoritative",
        }
        try:
            self.store.set_kv(COHORT_KEY, payload)
        except Exception:
            pass
        return cohort

    def _coverage(self, row: CohortRow) -> Dict[str, Any]:
        def read(interval: str) -> Dict[str, Any]:
            try:
                repository = getattr(self.store, "production_candle_repository", None)
                if repository is not None:
                    return dict(repository.candle_coverage(row.instrument_key, interval) or {})
                return dict(self.store.candle_coverage(row.instrument_key, interval) or {})
            except Exception:
                return {}

        day = read("day")
        if not day.get("count"):
            alt = read("1d")
            if int(alt.get("count") or 0) > int(day.get("count") or 0):
                day = alt
        m30 = read("30minute")
        if not m30.get("count"):
            alt = read("30m")
            if int(alt.get("count") or 0) > int(m30.get("count") or 0):
                m30 = alt
        daily_count = int(day.get("count") or 0)
        m30_count = int(m30.get("count") or 0)
        return {
            "symbol": row.symbol,
            "instrument_key": row.instrument_key,
            "daily_count": daily_count,
            "m30_count": m30_count,
            "daily_ready": daily_count >= MIN_DAILY_BARS,
            "m30_ready": m30_count >= MIN_30M_BARS,
            "decision_ready": daily_count >= MIN_DAILY_BARS,
        }

    def _today_decision_symbols(self, cohort_symbols: set[str]) -> set[str]:
        today = india_now().date().isoformat()
        try:
            rows = list(self.store.latest_decisions("delivery", limit=1500) or [])
        except Exception:
            rows = []
        out: set[str] = set()
        for row in rows:
            symbol = str(row.get("symbol") or row.get("trading_symbol") or "").upper().strip()
            trading_date = str(row.get("trading_date") or row.get("date") or row.get("created_at") or "")[:10]
            if symbol in cohort_symbols and trading_date == today:
                out.add(symbol)
        return out

    def _cards_counts(self) -> Dict[str, int]:
        try:
            cached = dict(getattr(self.host, "status", {}).get("dashboard_counts") or {})
            if cached:
                return {"research": int(cached.get("research") or 0), "watchlist": int(cached.get("watchlist") or 0), "final": int(cached.get("final") or 0)}
            cards = dict(self.host.dashboard_cards_data("all") or {})
        except Exception:
            cards = {}
        research = list(cards.get("research_candidates") or [])
        watch = list(cards.get("next_session_watchlist") or cards.get("watch_queue") or [])
        selected = list(cards.get("selected") or [])
        return {
            "research": len(research),
            "watchlist": len(watch),
            "final": len(selected),
        }

    def status(self, cohort_size: int = DEFAULT_COHORT_SIZE, *, refresh: bool = False) -> Dict[str, Any]:
        # The status endpoint is polled by installers and operators.  Reusing a
        # short-lived snapshot prevents every poll from scanning the cohort,
        # reading coverage and rebuilding dashboard cards while the scanner is
        # already under load.
        now = time.monotonic()
        with self._status_lock:
            if not refresh and self._status_cache is not None and now - self._status_cache_at < 10.0:
                return dict(self._status_cache)
        cohort = self.cohort(cohort_size)
        coverage = [self._coverage(row) for row in cohort]
        symbols = {row.symbol for row in cohort}
        decisions = self._today_decision_symbols(symbols)
        counts = self._cards_counts()
        daily_ready = sum(1 for row in coverage if row["daily_ready"])
        m30_ready = sum(1 for row in coverage if row["m30_ready"])
        researched = max(len(decisions), counts["research"] + counts["watchlist"] + counts["final"])
        useful = researched >= MIN_RESEARCH_ROWS
        state = "FIRST_MODE_READY" if useful else "BUILDING_RESEARCH_COHORT"
        try:
            saved = dict(self.store.get_kv(STATE_KEY, {}) or {})
        except Exception:
            saved = {}
        payload = {
            "ok": True,
            "version": SERVICE_VERSION,
            "state": state,
            "desk": "delivery",
            "cohort_size": len(cohort),
            "snapshot_id": self._snapshot_id(),
            "history": {
                "daily_ready": daily_ready,
                "m30_ready": m30_ready,
                "daily_required": MIN_DAILY_BARS,
                "m30_required": MIN_30M_BARS,
            },
            "today": {
                "canonical_decisions": len(decisions),
                **counts,
                "research_rows": researched,
            },
            "useful": useful,
            "next": (
                "start pre-market priority and forward evidence capture"
                if useful
                else "continue exact-gap history and bounded Delivery analysis"
            ),
            "last_activation": saved.get("last_activation"),
            "last_batch": saved.get("last_batch") or [],
            "production_influence": 0.0,
            "broker_authority": "NONE",
            "full_delivery_universe_preserved": True,
            "time": now_iso(),
        }
        with self._status_lock:
            self._status_cache = dict(payload)
            self._status_cache_at = time.monotonic()
        return payload

    def _invalidate_status_cache(self) -> None:
        with self._status_lock:
            self._status_cache = None
            self._status_cache_at = 0.0

    @staticmethod
    def _merge_unique(values: Iterable[str], additions: Iterable[str], limit: int = 48) -> List[str]:
        out: List[str] = []
        seen: set[str] = set()
        for raw in list(values or []) + list(additions or []):
            value = str(raw or "").upper().strip()
            if value and value not in seen:
                seen.add(value)
                out.append(value)
            if len(out) >= limit:
                break
        return out

    def activate(self, *, cohort_size: int = DEFAULT_COHORT_SIZE, batch_size: int = DEFAULT_BATCH_SIZE) -> Dict[str, Any]:
        self._invalidate_status_cache()
        cohort = self.cohort(cohort_size)
        if not cohort:
            return {
                "ok": False,
                "state": "COHORT_UNAVAILABLE",
                "error": "No authoritative NSE cash-equity Delivery cohort is available.",
                "production_influence": 0.0,
                "broker_authority": "NONE",
            }

        batch_size = max(4, min(int(batch_size or DEFAULT_BATCH_SIZE), 24))
        coverage = [self._coverage(row) for row in cohort]
        coverage_by_symbol = {row["symbol"]: row for row in coverage}
        decisions = self._today_decision_symbols({row.symbol for row in cohort})

        # History gaps first, then rows not yet analysed today, then a bounded
        # refresh rotation. This is deterministic and never changes the full
        # immutable Delivery population.
        ordered = sorted(
            cohort,
            key=lambda row: (
                0 if not coverage_by_symbol[row.symbol]["daily_ready"] else 1,
                0 if row.symbol not in decisions else 1,
                row.symbol,
            ),
        )
        try:
            saved = dict(self.store.get_kv(STATE_KEY, {}) or {})
        except Exception:
            saved = {}
        cursor = int(saved.get("cursor") or 0)
        if cursor >= len(ordered):
            cursor = 0
        batch = ordered[cursor:cursor + batch_size]
        if len(batch) < batch_size:
            batch += ordered[:batch_size - len(batch)]
        next_cursor = (cursor + len(batch)) % max(1, len(ordered))
        symbols = [row.symbol for row in batch]

        try:
            existing_queue = list(self.store.get_kv("scan_priority_queue", []) or [])
        except Exception:
            existing_queue = []
        queue = self._merge_unique(symbols, existing_queue, limit=48)
        try:
            self.store.set_kv("scan_priority_queue", queue)
        except Exception:
            pass

        scheduled_daily = 0
        scheduled_m30 = 0
        for row in batch:
            try:
                self.store.add_priority(row.symbol, row.exchange, "delivery", "first_useful_mode")
            except Exception:
                pass
            cov = coverage_by_symbol[row.symbol]
            if not cov["daily_ready"]:
                try:
                    if self.host.market_data.schedule_historical_refresh(
                        row.instrument_key, "day", 1095, reason="first_useful_mode_daily"
                    ) is not None:
                        scheduled_daily += 1
                except Exception:
                    pass
            if not cov["m30_ready"]:
                try:
                    if self.host.market_data.schedule_historical_refresh(
                        row.instrument_key, "30minute", 120, reason="first_useful_mode_mtf"
                    ) is not None:
                        scheduled_m30 += 1
                except Exception:
                    pass

        try:
            scan_request = self.host.scan_orchestration.request_scan("delivery")
        except Exception as exc:
            scan_request = {"ok": False, "error": str(exc)[:180]}

        state = {
            "version": SERVICE_VERSION,
            "desk": "delivery",
            "cursor": next_cursor,
            "cohort_size": len(cohort),
            "snapshot_id": self._snapshot_id(),
            "last_activation": now_iso(),
            "last_batch": symbols,
            "scheduled_daily": scheduled_daily,
            "scheduled_m30": scheduled_m30,
            "scan_request": scan_request,
            "production_influence": 0.0,
            "broker_authority": "NONE",
        }
        try:
            self.store.set_kv(STATE_KEY, state)
        except Exception:
            pass
        try:
            with self.host.lock:
                self.host.status["first_useful_mode"] = dict(state)
        except Exception:
            pass
        return {
            "ok": True,
            "state": "FIRST_MODE_BATCH_QUEUED",
            "batch": symbols,
            "scheduled_daily": scheduled_daily,
            "scheduled_m30": scheduled_m30,
            "scan_request": scan_request,
            "status": self.status(cohort_size, refresh=True),
            "time": now_iso(),
        }
