"""Persistent priority queue and truthful readiness classification for daily history.

v97 replaces cursor sweeps with a durable queue of unfinished instruments.  The
queue never counts an HTTP request as progress: readiness is derived only from
physically persisted candles plus provider/listing depth evidence.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Mapping, Optional

QUEUE_KEY = "historical_backfill_queue_v97"
QUEUE_VERSION = "historical-backfill-queue-1.0.0"
TRADING_DAYS_PER_YEAR = 252
MAX_AUTOMATIC_NO_PROGRESS_ATTEMPTS = 8
TERMINAL_RETRY_STATE = "TERMINAL_RETRY_EXHAUSTED"


def _parse_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        return None


def _parse_ts(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _years_between(first: Optional[datetime], last: Optional[datetime]) -> float:
    if not first or not last or last < first:
        return 0.0
    return max(0.0, (last - first).total_seconds() / (365.2425 * 86400.0))


def listing_date_from_instrument(instrument: Mapping[str, Any]) -> Optional[date]:
    for key in ("listing_date", "date_of_listing", "listed_on", "list_date"):
        parsed = _parse_date(instrument.get(key))
        if parsed:
            return parsed
    return None


def classify_daily_history(
    coverage: Mapping[str, Any] | None,
    *,
    listing_date: date | None = None,
    provider_state: str = "",
    today: date | None = None,
) -> Dict[str, Any]:
    """Return operational/research/deep readiness without demanding impossible age.

    A younger listing is measured against its actual listing age.  Provider depth
    or complete listing coverage is terminal and must not be retried forever.
    """
    cov = dict(coverage or {})
    today = today or date.today()
    count = max(0, int(cov.get("count") or 0))
    first_dt = _parse_ts(cov.get("first"))
    last_dt = _parse_ts(cov.get("last"))
    span_years = _years_between(first_dt, last_dt)
    listing_age_years = None
    if listing_date and listing_date <= today:
        listing_age_years = max(0.0, (today - listing_date).days / 365.2425)

    operational_target_years = min(1.0, listing_age_years) if listing_age_years is not None else 1.0
    operational_min_rows = max(60, int(max(0.25, operational_target_years) * TRADING_DAYS_PER_YEAR * 0.78))
    operational_min_span = max(0.20, operational_target_years * 0.72)
    operational_ready = count >= operational_min_rows and span_years >= operational_min_span

    research_target_years = min(10.0, listing_age_years) if listing_age_years is not None else 10.0
    research_target_years = max(0.25, research_target_years)
    research_min_rows = max(60, int(research_target_years * TRADING_DAYS_PER_YEAR * 0.82))
    research_min_span = max(0.20, research_target_years * 0.88)
    research_ready = count >= research_min_rows and span_years >= research_min_span

    deep_target_years = min(15.0, listing_age_years) if listing_age_years is not None else 15.0
    deep_target_years = max(0.25, deep_target_years)
    deep_min_rows = max(60, int(deep_target_years * TRADING_DAYS_PER_YEAR * 0.82))
    deep_min_span = max(0.20, deep_target_years * 0.88)
    deep_ready = count >= deep_min_rows and span_years >= deep_min_span

    first_date = first_dt.date() if first_dt else None
    last_date = last_dt.date() if last_dt else None
    listing_history_complete = bool(
        listing_date
        and first_date
        and first_date <= listing_date + timedelta(days=45)
        and last_date
        and last_date >= today - timedelta(days=14)
        and operational_ready
    )
    provider_depth_reported = str(provider_state or "").upper() in {
        "COMPLETE_PROVIDER_DEPTH", "PROVIDER_DEPTH_COMPLETE"
    }
    # An empty/very shallow provider response is not proof of complete history.
    # Treat provider depth as terminal only after enough persisted candles exist
    # for operational use; otherwise surface a data/identity failure for repair.
    provider_depth_complete = bool(provider_depth_reported and operational_ready)
    provider_depth_insufficient = bool(provider_depth_reported and not operational_ready)

    terminal = bool(deep_ready or listing_history_complete or provider_depth_complete)
    if deep_ready:
        state = "DEEP_ENRICHED"
    elif listing_history_complete:
        state = "LISTING_HISTORY_COMPLETE"
    elif provider_depth_complete:
        state = "PROVIDER_DEPTH_COMPLETE"
    elif research_ready:
        state = "RESEARCH_READY"
    elif operational_ready:
        state = "OPERATIONAL_READY"
    elif provider_depth_insufficient:
        state = "PROVIDER_DEPTH_INSUFFICIENT"
    elif count:
        state = "BACKFILLING"
    else:
        state = "PENDING"

    blockers = []
    if not operational_ready:
        blockers.append(
            f"operational history {count} rows/{span_years:.2f}y below "
            f"{operational_min_rows} rows/{operational_min_span:.2f}y"
        )
        if provider_depth_insufficient:
            blockers.append("provider returned no older candles before operational readiness; verify identity/listing/provider support")
    elif not research_ready:
        blockers.append(
            f"research history {count} rows/{span_years:.2f}y below "
            f"{research_min_rows} rows/{research_min_span:.2f}y"
        )
    elif not terminal:
        blockers.append(
            f"deep enrichment {count} rows/{span_years:.2f}y below "
            f"{deep_min_rows} rows/{deep_min_span:.2f}y"
        )

    return {
        "state": state,
        "terminal": terminal,
        "count": count,
        "first": cov.get("first"),
        "last": cov.get("last"),
        "span_years": round(span_years, 3),
        "listing_date": listing_date.isoformat() if listing_date else None,
        "listing_age_years": round(listing_age_years, 3) if listing_age_years is not None else None,
        "operational_ready": operational_ready,
        "research_ready": research_ready,
        "deep_enriched": deep_ready,
        "listing_history_complete": listing_history_complete,
        "provider_depth_complete": provider_depth_complete,
        "provider_depth_reported": provider_depth_reported,
        "provider_depth_insufficient": provider_depth_insufficient,
        "operational_min_rows": operational_min_rows,
        "research_min_rows": research_min_rows,
        "deep_min_rows": deep_min_rows,
        "operational_target_years": round(operational_target_years, 2),
        "research_target_years": round(research_target_years, 2),
        "deep_target_years": round(deep_target_years, 2),
        "blockers": blockers,
    }


class HistoricalBackfillQueueService:
    """KV-backed priority queue; only attempted entries advance."""

    def __init__(self, store: Any):
        self.store = store

    def load(self) -> list[Dict[str, Any]]:
        try:
            rows = self.store.get_kv(QUEUE_KEY, []) or []
        except Exception:
            rows = []
        return [dict(row) for row in rows if isinstance(row, Mapping) and row.get("instrument_key")]

    def save(self, rows: Iterable[Mapping[str, Any]]) -> None:
        payload = [dict(row) for row in rows]
        try:
            self.store.set_kv(QUEUE_KEY, payload)
        except Exception:
            pass

    def reconcile(
        self,
        universe: Iterable[Mapping[str, Any]],
        *,
        priority_by_key: Mapping[str, int] | None = None,
        priority_reason_by_key: Mapping[str, str] | None = None,
        provider_state_by_key: Mapping[str, str] | None = None,
    ) -> list[Dict[str, Any]]:
        existing = {str(row.get("instrument_key")): dict(row) for row in self.load()}
        priority_by_key = dict(priority_by_key or {})
        priority_reason_by_key = dict(priority_reason_by_key or {})
        provider_state_by_key = dict(provider_state_by_key or {})
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        rows = []
        for raw in universe:
            inst = dict(raw or {})
            key = str(inst.get("instrument_key") or "").strip()
            if not key:
                continue
            symbol = str(inst.get("trading_symbol") or inst.get("symbol") or key).upper().strip()
            old = existing.get(key, {})
            try:
                coverage = self.store.candle_coverage(key, "day") or {}
            except Exception:
                coverage = {}
            listing_date = listing_date_from_instrument(inst)
            readiness = classify_daily_history(
                coverage,
                listing_date=listing_date,
                provider_state=provider_state_by_key.get(key) or old.get("provider_state") or "",
            )
            priority_raw = priority_by_key[key] if key in priority_by_key else old.get("priority", 50)
            priority = int(50 if priority_raw is None else priority_raw)
            if readiness["research_ready"] and not readiness["terminal"]:
                priority = max(priority, 80)  # deep enrichment yields to operational blockers
            # Preserve an explicitly exhausted automatic-retry classification
            # until physical candle coverage changes. A routine queue reconcile
            # must not silently re-arm an instrument that already consumed its
            # governed retry budget. Operators can still repair/reseed the data;
            # any newly persisted rows automatically clear the terminal failure.
            old_terminal_failure = str(old.get("state") or "").upper() == TERMINAL_RETRY_STATE
            old_coverage_count = int(old.get("terminal_coverage_count") or old.get("coverage", {}).get("count") or 0)
            current_coverage_count = int(readiness.get("count") or 0)
            retain_terminal_failure = bool(old_terminal_failure and current_coverage_count <= old_coverage_count)
            row = {
                **old,
                "version": QUEUE_VERSION,
                "instrument_key": key,
                "symbol": symbol,
                "exchange": inst.get("exchange") or old.get("exchange") or "NSE",
                "listing_date": readiness.get("listing_date"),
                "priority": priority,
                "priority_reason": priority_reason_by_key.get(key) or old.get("priority_reason") or "governed_universe",
                "state": readiness["state"],
                "terminal": bool(readiness["terminal"] or retain_terminal_failure),
                "terminal_failure": retain_terminal_failure,
                "operational_ready": readiness["operational_ready"],
                "research_ready": readiness["research_ready"],
                "deep_enriched": readiness["deep_enriched"],
                "coverage": readiness,
                "updated_at": now,
                "attempts": int(old.get("attempts") or 0),
                "no_progress_attempts": int(old.get("no_progress_attempts") or 0),
                "next_retry_at": old.get("next_retry_at"),
                "provider_state": provider_state_by_key.get(key) or old.get("provider_state"),
                "last_error": old.get("last_error"),
                "last_rows_saved": int(old.get("last_rows_saved") or 0),
                "terminal_coverage_count": old.get("terminal_coverage_count") if retain_terminal_failure else None,
                "terminal_reason": old.get("terminal_reason") if retain_terminal_failure else None,
            }
            if retain_terminal_failure:
                row["state"] = TERMINAL_RETRY_STATE
                row["next_retry_at"] = None
            rows.append(row)
        rows.sort(key=lambda row: (bool(row.get("terminal")), int(50 if row.get("priority") is None else row.get("priority")), str(row.get("next_retry_at") or ""), row.get("symbol") or ""))
        self.save(rows)
        return rows

    @staticmethod
    def due(rows: Iterable[Mapping[str, Any]], *, limit: int, market_open: bool) -> list[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        due = []
        for raw in rows:
            row = dict(raw)
            if row.get("terminal"):
                continue
            if market_open and row.get("research_ready"):
                continue  # optional deep enrichment is after-hours only
            retry_at = _parse_ts(row.get("next_retry_at"))
            if retry_at and retry_at > now:
                continue
            due.append(row)
            if len(due) >= max(1, int(limit)):
                break
        return due

    def apply_result(
        self,
        rows: list[Dict[str, Any]],
        *,
        instrument_key: str,
        rows_saved: int,
        provider_state: str,
        error: str = "",
    ) -> Dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        for row in rows:
            if str(row.get("instrument_key")) != str(instrument_key):
                continue
            try:
                coverage = self.store.candle_coverage(instrument_key, "day") or {}
            except Exception:
                coverage = {}
            readiness = classify_daily_history(
                coverage,
                listing_date=_parse_date(row.get("listing_date")),
                provider_state=provider_state,
            )
            row.update({
                "state": readiness["state"],
                "terminal": readiness["terminal"],
                "operational_ready": readiness["operational_ready"],
                "research_ready": readiness["research_ready"],
                "deep_enriched": readiness["deep_enriched"],
                "coverage": readiness,
                "provider_state": provider_state or None,
                "attempts": int(row.get("attempts") or 0) + 1,
                "last_attempt_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "last_rows_saved": max(0, int(rows_saved or 0)),
                "last_error": error or None,
                "no_progress_attempts": 0 if rows_saved > 0 else int(row.get("no_progress_attempts") or 0) + 1,
                "updated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            })
            if not readiness["terminal"]:
                failures = int(row.get("no_progress_attempts") or 0)
                if rows_saved <= 0 and failures >= MAX_AUTOMATIC_NO_PROGRESS_ATTEMPTS:
                    row["terminal"] = True
                    row["terminal_failure"] = True
                    row["terminal_coverage_count"] = int(readiness.get("count") or 0)
                    row["terminal_reason"] = (
                        error or readiness.get("blockers", [None])[0] or
                        "automatic exact-gap retries produced no new persisted daily candles"
                    )
                    row["state"] = TERMINAL_RETRY_STATE
                    row["next_retry_at"] = None
                else:
                    delay_minutes = min(24 * 60, 5 * (2 ** min(8, max(0, failures - 1)))) if failures else 1
                    row["next_retry_at"] = (now + timedelta(minutes=delay_minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")
                    row["state"] = "RETRY_SCHEDULED" if rows_saved <= 0 else readiness["state"]
                    row["terminal_failure"] = False
                    row["terminal_reason"] = None
                    row["terminal_coverage_count"] = None
            else:
                row["next_retry_at"] = None
                row["terminal_failure"] = False
                row["terminal_reason"] = None
                row["terminal_coverage_count"] = None
            self.save(rows)
            return row
        return None


def summarize_queue(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = [dict(row) for row in rows]
    counts = {
        "total": len(rows),
        "operational_ready": sum(bool(row.get("operational_ready")) for row in rows),
        "research_ready": sum(bool(row.get("research_ready")) for row in rows),
        "deep_enriched": sum(bool(row.get("deep_enriched")) for row in rows),
        "backfilling": sum(str(row.get("state")) in {"BACKFILLING", "PENDING", "OPERATIONAL_READY", "RESEARCH_READY"} and not row.get("terminal") for row in rows),
        "retry_scheduled": sum(str(row.get("state")) == "RETRY_SCHEDULED" for row in rows),
        "provider_depth_complete": sum(str(row.get("state")) == "PROVIDER_DEPTH_COMPLETE" for row in rows),
        "listing_history_complete": sum(str(row.get("state")) == "LISTING_HISTORY_COMPLETE" for row in rows),
        "failed": sum(bool(row.get("last_error")) and int(row.get("no_progress_attempts") or 0) >= 3 for row in rows),
        "terminal_failures": sum(bool(row.get("terminal_failure")) or str(row.get("state") or "").upper() == TERMINAL_RETRY_STATE for row in rows),
    }
    counts["accounted"] = min(counts["total"], counts["operational_ready"] + counts["terminal_failures"])
    counts["remaining_unaccounted"] = max(0, counts["total"] - counts["accounted"])
    counts["remaining_operational"] = max(0, counts["total"] - counts["operational_ready"])
    counts["remaining_research"] = max(0, counts["total"] - counts["research_ready"])
    counts["remaining_deep"] = max(0, counts["total"] - counts["deep_enriched"] - counts["provider_depth_complete"] - counts["listing_history_complete"])
    return counts
