"""Read-only continuity projection for signals created before Model Paper.

The governed ``model_portfolio_positions`` ledger has stronger evidence than
the older ``signal_ledger``: it freezes quantity, capital, costs and the exact
admission result.  Older signal rows must therefore never be silently promoted
into the governed book.  They are still legitimate persisted signal evidence,
however, and hiding them makes an upgrade look like data loss.

This service exposes unlinked signal-ledger rows with explicit provenance.  It
never writes, invents quantity/costs, or contributes to Model Paper P&L.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List

from core.india_time import INDIA_TZ
from core.production_mode_policy import require_production_mode

_EVIDENCE_STATUSES = {"OPEN", "SUCCESS", "FAIL", "AMBIGUOUS", "EXPIRED"}
_SETTLED_STATUSES = {"SUCCESS", "FAIL", "AMBIGUOUS", "EXPIRED"}


def _canonical_mode(value: Any) -> str | None:
    try:
        return require_production_mode(value)
    except ValueError:
        return None


def _payload(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        value = json.loads(row.get("payload_json") or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        # SQLite CURRENT_TIMESTAMP is UTC; explicit application timestamps
        # carry an offset and are handled by the branch above.
        parsed = parsed.replace(tzinfo=INDIA_TZ if ("+" in text[10:] or text.endswith("IST")) else timezone.utc)
    return parsed.astimezone(INDIA_TZ)


def _row_date(row: Dict[str, Any]) -> date | None:
    raw = str(row.get("trade_date") or "").strip()
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        parsed = _timestamp(row.get("opened_at") or row.get("closed_at"))
        return parsed.date() if parsed else None


class SignalLedgerContinuityService:
    """Project unlinked legacy signal evidence without mutating either book."""

    def __init__(self, store: Any):
        self.store = store

    def _table_exists(self, name: str) -> bool:
        return self.store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def rows(self) -> List[Dict[str, Any]]:
        if not self._table_exists("signal_ledger"):
            return []
        if self._table_exists("model_portfolio_positions"):
            sql = """
                SELECT sl.*
                FROM signal_ledger sl
                LEFT JOIN model_portfolio_positions mp
                  ON mp.source_signal_id=sl.signal_id
                WHERE mp.position_id IS NULL
                ORDER BY sl.opened_at DESC
            """
        else:
            sql = "SELECT * FROM signal_ledger ORDER BY opened_at DESC"
        return [dict(row) for row in self.store.conn.execute(sql).fetchall()]

    @staticmethod
    def _aggregate(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        retained = [
            row for row in rows
            if str(row.get("status") or "").upper() in _EVIDENCE_STATUSES
        ]
        settled = [
            row for row in retained
            if str(row.get("status") or "").upper() in _SETTLED_STATUSES
        ]
        success = sum(str(row.get("status") or "").upper() == "SUCCESS" for row in settled)
        failure = sum(str(row.get("status") or "").upper() == "FAIL" for row in settled)
        ambiguous = sum(str(row.get("status") or "").upper() == "AMBIGUOUS" for row in settled)
        expired = sum(str(row.get("status") or "").upper() == "EXPIRED" for row in settled)
        reliable_points: List[float] = []
        quality_excluded = 0
        for row in settled:
            points = _number(row.get("pnl_points"))
            entry = _number(row.get("entry"))
            if points is None:
                continue
            if entry and abs(points) > abs(entry) * 0.5:
                quality_excluded += 1
                continue
            reliable_points.append(points)
        scored = success + failure
        return {
            "signals": len(retained),
            "open": sum(str(row.get("status") or "").upper() == "OPEN" for row in retained),
            "settled": len(settled),
            "scored": scored,
            "success": success,
            "failure": failure,
            "ambiguous": ambiguous,
            "expired": expired,
            "accuracy_pct": round(success / scored * 100, 2) if scored else None,
            "pnl_points": round(sum(reliable_points), 2) if reliable_points else 0.0,
            "pnl_points_rows": len(reliable_points),
            "quality_excluded": quality_excluded,
            "currency_pnl_available": False,
        }

    def report(self, *, as_of: datetime | None = None) -> Dict[str, Any]:
        now = as_of or datetime.now(INDIA_TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=INDIA_TZ)
        now = now.astimezone(INDIA_TZ)
        all_rows = self.rows()
        evidence_rows = [
            dict(row, canonical_mode=_canonical_mode(row.get("mode")))
            for row in all_rows
            if _canonical_mode(row.get("mode")) is not None
        ]
        starts = {
            "today": now.date(),
            "week": now.date() - timedelta(days=now.weekday()),
            "month": now.date().replace(day=1),
            "all": None,
        }
        filters: Dict[str, Any] = {}
        for label, start in starts.items():
            period = [
                row for row in evidence_rows
                if start is None or (_row_date(row) is not None and _row_date(row) >= start)
            ]
            filters[label] = {
                "all": self._aggregate(period),
                "intraday": self._aggregate(
                    row for row in period if row.get("canonical_mode") == "intraday"
                ),
                "delivery": self._aggregate(
                    row for row in period if row.get("canonical_mode") == "delivery"
                ),
            }
        return {
            "source": "signal_ledger",
            "classification": "PRE_GOVERNED_OR_UNLINKED_SIGNAL_EVIDENCE",
            "included_in_model_paper": False,
            "included_in_model_paper_pnl": False,
            "retained_rows": len(evidence_rows),
            "filters": filters,
            "reason": (
                "Persisted signal outcomes are retained for continuity, but lack a "
                "proved governed quantity/capital/cost admission and cannot be "
                "converted into Model Paper rupee P&L."
            ),
        }

    def open_research_rows(self, *, as_of: datetime | None = None, limit: int = 50) -> List[Dict[str, Any]]:
        now = as_of or datetime.now(INDIA_TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=INDIA_TZ)
        today = now.astimezone(INDIA_TZ).date()
        output: List[Dict[str, Any]] = []
        for row in self.rows():
            if str(row.get("status") or "").upper() != "OPEN":
                continue
            mode = _canonical_mode(row.get("mode"))
            if mode is None:
                continue
            if mode == "intraday" and _row_date(row) != today:
                continue
            source = _payload(row)
            signal_id = str(row.get("signal_id") or "")
            output.append({
                "row_id": f"legacy-signal:{signal_id}",
                "source_signal_id": signal_id,
                "book": "LEGACY_SIGNAL_LEDGER",
                "segment": "research",
                "symbol": row.get("symbol"),
                "exchange": row.get("exchange") or source.get("exchange") or "NSE",
                "mode": mode,
                "side": row.get("side") or source.get("side"),
                "status": "RESEARCH_CONTINUITY",
                "ltp": _number(row.get("ltp")),
                "rupee_change": _number(source.get("rupee_change")),
                "change_pct": _number(source.get("change_pct")),
                "entry": _number(row.get("entry")),
                "target": _number(row.get("t1")),
                "original_stop": _number(row.get("sl")),
                "active_stop": _number(source.get("managed_sl") or row.get("sl")),
                "hit_status": "NOT_GOVERNED_MODEL_POSITION",
                "quantity": 0,
                "capital": 0.0,
                "net_pnl": None,
                "action": "OPEN LEDGER EVIDENCE - NOT ADMITTED TO GOVERNED MODEL PAPER",
                "outcome": "RETAINED UNDER RESEARCH",
                "occurred_at": row.get("opened_at"),
                "continuity_reason": (
                    "The persisted signal is visible, but quantity, capital reservation "
                    "and cost-aware Model Paper admission were not proved."
                ),
                "chart_binding": {"symbol": row.get("symbol"), "mode": mode},
            })
            if len(output) >= max(1, int(limit)):
                break
        return output
