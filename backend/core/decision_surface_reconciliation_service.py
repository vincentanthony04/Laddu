"""Reconcile Today Entries, Signal Ledger and Model Paper projections.

One canonical DecisionRecord must remain authoritative across all operator
surfaces. This service is read-only and reports missing/mismatched projections;
it never repairs or promotes a decision.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping

from core.production_mode_policy import PRODUCTION_MODES
from core.production_ranking_service import RANKING_VERSION

SERVICE_VERSION = "decision-surface-reconciliation-1.0.0"
CONTRACT_VERSION = "canonical-decision-surfaces-1.0.0"


def _map(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Dict[str, Any]]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return []
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _id(row: Mapping[str, Any]) -> str:
    return str(
        row.get("decision_id") or row.get("signal_id") or row.get("source_signal_id")
        or row.get("source_decision_id") or ""
    ).strip()


def _text(row: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _number(row: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        value = row.get(name)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _same_number(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return True  # A projection may intentionally omit a non-authoritative field.
    return abs(left - right) <= max(0.01, abs(left) * 1e-6, abs(right) * 1e-6)


class DecisionSurfaceReconciliationService:
    def __init__(self, app: Any):
        self.app = app
        self.store = getattr(app, "store", None)

    def _canonical(self) -> list[Dict[str, Any]]:
        repo = getattr(self.store, "production_canonical_decision_repository", None)
        getter = getattr(repo, "latest_decisions", None) or getattr(self.store, "latest_decisions", None)
        if not callable(getter):
            return []
        try:
            return _rows(getter("all", 1000))
        except TypeError:
            try:
                return _rows(getter("all", limit=1000))
            except Exception:
                return []
        except Exception:
            return []

    def _today(self) -> list[Dict[str, Any]]:
        repo = getattr(self.store, "production_canonical_decision_repository", None)
        getter = getattr(repo, "today_entries", None)
        if callable(getter):
            try:
                return _rows(getter("all", limit=1000))
            except TypeError:
                try:
                    return _rows(getter("all", None, 1000))
                except Exception:
                    pass
            except Exception:
                pass
        # The compatibility route still reads the canonical repository through
        # store.latest_decisions; do not fall back to SQLite production tables.
        return []

    def _signals(self) -> list[Dict[str, Any]]:
        getter = getattr(self.store, "selected_signals", None)
        if not callable(getter):
            return []
        try:
            return _rows(getter("all", 1000))
        except TypeError:
            try:
                return _rows(getter("all", limit=1000))
            except Exception:
                return []
        except Exception:
            return []

    def _positions(self) -> list[Dict[str, Any]]:
        repo = getattr(self.app, "model_portfolio_repository", None)
        getter = getattr(repo, "list_positions", None)
        if not callable(getter):
            return []
        try:
            return _rows(getter())
        except Exception:
            return []

    @staticmethod
    def _core_conflicts(source: Mapping[str, Any], projection: Mapping[str, Any], surface: str) -> list[Dict[str, Any]]:
        conflicts: list[Dict[str, Any]] = []
        text_fields = {
            "symbol": ("symbol",),
            "mode": ("mode", "trade_mode", "desk"),
            "side": ("side", "direction"),
            "ranking_trace_id": ("ranking_trace_id",),
        }
        for label, aliases in text_fields.items():
            left = _text(source, *aliases).lower()
            right = _text(projection, *aliases).lower()
            if left and right and left != right:
                conflicts.append({"surface": surface, "field": label, "canonical": left, "projection": right})
        number_fields = {
            "entry": ("entry", "entry_price"),
            "target": ("t1", "target", "target_price"),
            "stop_loss": ("sl", "stop_loss", "stop_price"),
        }
        for label, aliases in number_fields.items():
            left = _number(source, *aliases)
            right = _number(projection, *aliases)
            if not _same_number(left, right):
                conflicts.append({"surface": surface, "field": label, "canonical": left, "projection": right})
        return conflicts

    def status(self) -> Dict[str, Any]:
        canonical = self._canonical()
        today = self._today()
        signals = self._signals()
        positions = self._positions()
        canonical_by_id: Dict[str, Dict[str, Any]] = {}
        conflicts: list[Dict[str, Any]] = []
        duplicate_ids: list[str] = []
        invalid_modes: list[Dict[str, Any]] = []
        for row in canonical:
            key = _id(row)
            if key:
                if key in canonical_by_id:
                    duplicate_ids.append(key)
                canonical_by_id[key] = row
            mode = _text(row, "mode").lower()
            if mode and mode not in PRODUCTION_MODES:
                invalid_modes.append({"surface": "canonical", "decision_id": key, "mode": mode})

        projections = {"today_entries": today, "signal_ledger": signals, "model_paper": positions}
        matched = {name: 0 for name in projections}
        orphaned: Dict[str, list[Dict[str, Any]]] = {name: [] for name in projections}
        for surface, rows in projections.items():
            seen: set[str] = set()
            for row in rows:
                key = _id(row)
                mode = _text(row, "mode", "trade_mode", "desk").lower()
                if mode and mode not in PRODUCTION_MODES:
                    invalid_modes.append({"surface": surface, "decision_id": key, "mode": mode})
                if key and key in seen:
                    conflicts.append({"surface": surface, "decision_id": key, "field": "duplicate_projection"})
                if key:
                    seen.add(key)
                source = canonical_by_id.get(key)
                if source is None:
                    # Closed legacy positions may predate canonical migration;
                    # only current/open projections are release-gate relevant.
                    state = _text(row, "status", "state", "position_state").upper()
                    if surface != "model_paper" or state in {"OPEN", "ACTIVE", "TRIGGERED", "CONFIRMED", ""}:
                        orphaned[surface].append({"decision_id": key, "symbol": row.get("symbol"), "state": state})
                    continue
                matched[surface] += 1
                for conflict in self._core_conflicts(source, row, surface):
                    conflict["decision_id"] = key
                    conflicts.append(conflict)

        # Canonical active Delivery duplicates are prohibited independently of
        # the fact that Today Entries and Signal Ledger are projections of the
        # same row.
        active_delivery: Dict[tuple[str, str], list[str]] = {}
        active_states = {"PREPARED", "TRIGGERED", "CONFIRMED", "WEAKENING", "SIGNAL_OPEN", "OPEN"}
        for row in canonical:
            if _text(row, "mode").lower() != "delivery":
                continue
            if _text(row, "state", "status").upper() not in active_states and row.get("active") is not True:
                continue
            key = (_text(row, "symbol").upper(), _text(row, "side", "direction").upper())
            active_delivery.setdefault(key, []).append(_id(row))
        delivery_duplicates = [
            {"symbol": key[0], "side": key[1], "decision_ids": ids}
            for key, ids in active_delivery.items() if key[0] and len(set(ids)) > 1
        ]

        governed = [row for row in canonical if row.get("ranking_version") == RANKING_VERSION]
        observations_ready = bool(governed)
        passed = bool(
            observations_ready and not duplicate_ids and not invalid_modes and not conflicts
            and not any(orphaned.values()) and not delivery_duplicates
        )
        missing = []
        if not observations_ready:
            missing.append("at least one current governed canonical decision")
        if duplicate_ids:
            missing.append("unique canonical decision IDs")
        if invalid_modes:
            missing.append("Intraday/Delivery-only values on every surface")
        if conflicts:
            missing.append("identical canonical fields across operator projections")
        if any(orphaned.values()):
            missing.append("zero active orphan Signal Ledger/Model Paper projections")
        if delivery_duplicates:
            missing.append("zero duplicate active Delivery theses for the same symbol and side")
        return {
            "ok": passed,
            "version": SERVICE_VERSION,
            "contract_version": CONTRACT_VERSION,
            "state": "PASS" if passed else "PENDING_EVIDENCE" if not observations_ready else "FAILED",
            "passed": passed,
            "counts": {
                "canonical": len(canonical), "governed_canonical": len(governed),
                "today_entries": len(today), "signal_ledger": len(signals), "model_paper": len(positions),
            },
            "matched": matched,
            "duplicate_canonical_ids": sorted(set(duplicate_ids)),
            "invalid_modes": invalid_modes,
            "orphaned": orphaned,
            "conflicts": conflicts[:100],
            "delivery_duplicate_theses": delivery_duplicates,
            "missing_gates": missing,
            "authority": "POSTGRESQL_CANONICAL_DECISION_RECONCILIATION",
            "production_change_allowed": False,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }
