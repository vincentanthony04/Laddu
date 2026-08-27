from __future__ import annotations

"""Reconciled v67 SQLite -> v68 operational PostgreSQL migration.

Every retained source row receives exactly one disposition:

* migrated into the v68 operational authority;
* rejected into the external migration evidence report; or
* blocked because it represents unresolved current economic/risk state.

The source SQLite files are opened read-only and are never modified.  The tool
supports a pre-deployment ``--dry-run`` that requires no PostgreSQL connection.
Console output is deliberately bounded; complete evidence is written to JSON.
"""

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Mapping, Sequence

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))



ALLOWED_MODES = {"intraday", "delivery"}
ALLOWED_STATES = {
    "WATCHING", "PREPARED", "TRIGGERED", "CONFIRMED", "WEAKENING",
    "INVALIDATED", "COMPLETED", "REJECTED",
}
TERMINAL_STATES = {"INVALIDATED", "COMPLETED", "REJECTED"}
ALLOWED_PUBLICATION = {"NOT_PUBLISHABLE", "MODEL_PAPER", "CAPITAL"}
ALLOWED_EXECUTION = {"BLOCKED", "CAPITAL_ALLOWED"}
REPORT_SAMPLE_LIMIT = 20
SOURCE_AUTHORITY_POLICY = {
    "legacy_root": 10,
    "focused_operational": 20,
    "single_source": 20,
    "peer": 10,
}

SIDE_ALIASES = {
    "LONG": "LONG",
    "BUY": "LONG",
    "BULLISH": "LONG",
    "SHORT": "SHORT",
    "SELL": "SHORT",
    "BEARISH": "SHORT",
}
EXCHANGE_ALIASES = {
    "NSE": "NSE",
    "NSE_EQ": "NSE",
    "NSE_INDEX": "NSE",
    "BSE": "BSE",
    "BSE_EQ": "BSE",
    "BSE_INDEX": "BSE",
}
PUBLICATION_ALIASES = {
    "": "NOT_PUBLISHABLE",
    "NONE": "NOT_PUBLISHABLE",
    "BLOCKED": "NOT_PUBLISHABLE",
    "NOT_PUBLISHABLE": "NOT_PUBLISHABLE",
    "PAPER": "MODEL_PAPER",
    "MODEL_PAPER": "MODEL_PAPER",
    "CAPITAL": "CAPITAL",
}
EXECUTION_ALIASES = {
    "": "BLOCKED",
    "NONE": "BLOCKED",
    "BLOCKED": "BLOCKED",
    "CAPITAL_ALLOWED": "CAPITAL_ALLOWED",
    "ALLOWED": "CAPITAL_ALLOWED",
}


class MigrationValidationError(RuntimeError):
    def __init__(self, report: Mapping[str, Any]):
        self.report = dict(report)
        compact = {
            "blocker_count": int(report.get("blocker_count") or 0),
            "blocker_reason_counts": report.get("blocker_reason_counts") or {},
            "blocker_samples": (report.get("blocker_samples") or [])[:REPORT_SAMPLE_LIMIT],
            "report": report.get("report_path"),
        }
        super().__init__(
            "PRODUCTION_STATE_MIGRATION_VALIDATION_FAILED: "
            + json.dumps(compact, sort_keys=True, default=str)
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_dbs(install_dir: Path) -> list[Path]:
    """Return retained sources, oldest first and focused operational last."""
    candidates = (
        install_dir / "data" / "project_laddu.sqlite3",
        install_dir / "data" / "operational" / "project_laddu_ops.sqlite3",
    )
    found = [path.resolve() for path in candidates if path.exists()]
    if not found:
        raise FileNotFoundError("operational SQLite migration source was not found")
    return found


def _source_authority_for_path(path: Path) -> dict[str, Any]:
    normalized = path.as_posix().lower()
    role = "focused_operational" if "/data/operational/" in normalized else "legacy_root"
    return {"role": role, "rank": SOURCE_AUTHORITY_POLICY[role], "path": str(path)}


def _default_connection_authorities(connections: Sequence[sqlite3.Connection]) -> list[dict[str, Any]]:
    """Compatibility policy for callers that cannot provide source paths.

    The established migration API orders retained sources oldest/legacy first
    and the focused operational authority last.  This converts that API
    contract into explicit named authority evidence instead of using raw path
    order as an unreported tie-break.
    """
    if len(connections) == 1:
        return [{"role": "single_source", "rank": SOURCE_AUTHORITY_POLICY["single_source"]}]
    return [
        {
            "role": "focused_operational" if index == len(connections) - 1 else "legacy_root",
            "rank": SOURCE_AUTHORITY_POLICY[
                "focused_operational" if index == len(connections) - 1 else "legacy_root"
            ],
        }
        for index, _conn in enumerate(connections)
    ]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _source_database(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        value = str(row[2] if row and len(row) > 2 else "").strip()
        if value:
            return value
    except Exception:
        pass
    return f"<sqlite-memory:{id(conn)}>"


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, table):
        return []
    source_database = _source_database(conn)
    result: list[dict[str, Any]] = []
    for raw in conn.execute(f'SELECT * FROM "{table}"').fetchall():
        row = dict(raw)
        row["__source_database"] = source_database
        row["__source_table"] = table
        result.append(row)
    return result


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k): v for k, v in row.items() if not str(k).startswith("__")}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _row_hash(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(_public_row(row)).encode("utf-8")).hexdigest()


def _rank_int(row: Mapping[str, Any], *fields: str) -> int:
    values: list[int] = []
    for field in fields:
        try:
            values.append(int(row.get(field) or 0))
        except (TypeError, ValueError):
            continue
    return max(values or [0])


def _rank_time(row: Mapping[str, Any]) -> float:
    values: list[float] = []
    for field in ("updated_at", "occurred_at", "closed_at", "opened_at", "created_at", "account_as_of"):
        raw = row.get(field)
        if raw in (None, ""):
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            values.append(dt.timestamp())
        except Exception:
            continue
    return max(values or [0.0])


def _current_authority_hint(row: Mapping[str, Any]) -> bool:
    state = str(row.get("state") or row.get("status") or "").upper()
    publication = _normalise_publication(row.get("publication_authority"))
    execution = _normalise_execution(row.get("execution_authority"))
    return bool(
        _bool(row.get("active"))
        or state == "OPEN"
        or publication in {"MODEL_PAPER", "CAPITAL"}
        or execution == "CAPITAL_ALLOWED"
    ) and state not in TERMINAL_STATES


def _authority_rank(
    row: Mapping[str, Any], source_authority: Mapping[str, Any]
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    current = 1 if _current_authority_hint(row) else 0
    version = _rank_int(row, "record_version", "version", "revision", "state_version")
    sequence = _rank_int(row, "event_sequence", "sequence", "event_id")
    updated = _rank_time(row)
    digest = _row_hash(row)
    authority_rank = int(source_authority.get("rank") or 0)
    authority_role = str(source_authority.get("role") or "peer")
    details = {
        "current_authority_hint": bool(current),
        "record_version": version,
        "event_sequence": sequence,
        "latest_timestamp_epoch": updated,
        "source_authority": authority_role,
        "source_authority_rank": authority_rank,
        "payload_sha256": digest,
    }
    return (current, version, sequence, updated, authority_rank, digest), details


def _selection_basis(winner: tuple[Any, ...], loser: tuple[Any, ...]) -> str:
    labels = (
        "current_authority_hint", "record_version", "event_sequence",
        "latest_timestamp", "source_authority", "payload_hash_tiebreak",
    )
    for index, label in enumerate(labels):
        if winner[index] != loser[index]:
            return label
    return "identical"


def _merge_rows(
    connections: Sequence[sqlite3.Connection],
    table: str,
    key_fn,
    source_authorities: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge by explicit authority evidence, never by path order alone."""
    merged: dict[str, dict[str, Any]] = {}
    merged_rank: dict[str, tuple[Any, ...]] = {}
    merged_rank_detail: dict[str, dict[str, Any]] = {}
    superseded: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    ambiguous_blockers: list[dict[str, Any]] = []
    missing_counter = 0
    authorities = list(source_authorities or ({"role": "peer", "rank": SOURCE_AUTHORITY_POLICY["peer"]} for _ in connections))
    if len(authorities) != len(connections):
        raise ValueError("source authority count must match connection count")
    for source_order, (conn, source_authority) in enumerate(zip(connections, authorities), start=1):
        for source in _rows(conn, table):
            row = dict(source)
            row["__source_order"] = source_order
            row["__source_authority"] = dict(source_authority)
            key = str(key_fn(row) or "").strip()
            if not key:
                missing_counter += 1
                key = f"__MISSING_KEY__:{table}:{missing_counter}:{_row_hash(row)}"
            rank, rank_detail = _authority_rank(row, source_authority)
            if key not in merged:
                merged[key] = row
                merged_rank[key] = rank
                merged_rank_detail[key] = rank_detail
                continue
            existing = dict(merged[key])
            existing_rank = merged_rank[key]
            existing_detail = merged_rank_detail[key]
            if rank > existing_rank:
                winner, winner_rank, winner_detail = row, rank, rank_detail
                loser, loser_rank, loser_detail = existing, existing_rank, existing_detail
                merged[key] = row
                merged_rank[key] = rank
                merged_rank_detail[key] = rank_detail
            else:
                winner, winner_rank, winner_detail = existing, existing_rank, existing_detail
                loser, loser_rank, loser_detail = row, rank, rank_detail
            payload_equal = winner_detail["payload_sha256"] == loser_detail["payload_sha256"]
            authority_equal = winner_rank[:5] == loser_rank[:5]
            ambiguous = bool(
                not payload_equal
                and authority_equal
                and winner_detail["current_authority_hint"]
                and loser_detail["current_authority_hint"]
            )
            proof = {
                "table": table,
                "key": key,
                "selection_basis": _selection_basis(winner_rank, loser_rank),
                "payload_equal": payload_equal,
                "ambiguous_current_authority": ambiguous,
                "winner": {
                    "source_database": winner.get("__source_database"),
                    **winner_detail,
                },
                "loser": {
                    "source_database": loser.get("__source_database"),
                    **loser_detail,
                },
            }
            conflicts.append(proof)
            loser["__superseded_by_database"] = winner.get("__source_database")
            loser["__conflict_proof"] = proof
            if ambiguous:
                ambiguous_blockers.append(loser)
            else:
                superseded.append(loser)
    return list(merged.values()), superseded, conflicts, ambiguous_blockers


def _json_value(value: Any, *, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list, tuple, bool, int, float)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _json_text(value: Any, *, default: Any) -> str:
    return _canonical_json(_json_value(value, default=default))


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _valid_date(value: Any) -> bool:
    try:
        date.fromisoformat(str(value or "")[:10])
        return True
    except Exception:
        return False


def _positive(value: Any) -> bool:
    try:
        return float(value) > 0
    except Exception:
        return False


def _normalise_side(value: Any) -> str:
    return SIDE_ALIASES.get(str(value or "").upper().strip(), "")


def _normalise_exchange(value: Any) -> str:
    return EXCHANGE_ALIASES.get(str(value or "NSE").upper().strip(), "")


def _normalise_publication(value: Any) -> str:
    return PUBLICATION_ALIASES.get(str(value or "").upper().strip(), "")


def _normalise_execution(value: Any) -> str:
    return EXECUTION_ALIASES.get(str(value or "").upper().strip(), "")


def _source_primary_key(row: Mapping[str, Any]) -> str:
    for field in ("decision_id", "position_id", "event_key", "event_id"):
        value = str(row.get(field) or "").strip()
        if value:
            return f"{field}:{value}"
    return "sha256:" + _row_hash(row)


def _disposition(
    row: Mapping[str, Any],
    reason: str,
    *,
    problems: Sequence[str] | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    public = _public_row(row)
    row_sha = _row_hash(row)
    reason_detail: dict[str, Any] = {}
    if problems:
        reason_detail["problems"] = list(problems)
    if detail:
        reason_detail.update(dict(detail))
    source_database = str(row.get("__source_database") or "unknown")
    source_table = str(row.get("__source_table") or "unknown")
    source_key = _source_primary_key(row)
    identity_material = "|".join((source_database, source_table, source_key, row_sha, reason))
    rejection_id = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
    return {
        "rejection_id": rejection_id,
        "source_database": source_database,
        "source_table": source_table,
        "source_primary_key": source_key,
        "reason": reason,
        "reason_detail": reason_detail,
        "source_payload": public,
        "source_payload_sha256": row_sha,
        "decision_id": row.get("decision_id"),
        "position_id": row.get("position_id"),
        "event_id": row.get("event_id"),
        "mode": str(row.get("mode") or "").lower() or None,
        "active_hint": _bool(row.get("active")),
        "trading_date": str(row.get("trading_date") or "")[:10] or None,
    }


def _production_authority_claimed(row: Mapping[str, Any]) -> bool:
    publication = _normalise_publication(row.get("publication_authority"))
    execution = _normalise_execution(row.get("execution_authority"))
    raw_publication = str(row.get("publication_authority") or "").upper().strip()
    raw_execution = str(row.get("execution_authority") or "").upper().strip()
    return (
        publication in {"MODEL_PAPER", "CAPITAL"}
        or execution == "CAPITAL_ALLOWED"
        or (raw_publication not in {"", "NONE", "BLOCKED", "NOT_PUBLISHABLE"})
        or (raw_execution not in {"", "NONE", "BLOCKED"})
    )


def _canonical_rows(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[list[tuple[Any, ...]], list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[tuple[Any, ...]] = []
    rejected_rows: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        mode = str(row.get("mode") or "").lower()
        if mode not in ALLOWED_MODES:
            rejected_rows.append(_disposition(row, "LEGACY_UNSUPPORTED_MODE"))
            continue
        decision_id = str(row.get("decision_id") or "").strip()
        symbol = str(row.get("symbol") or "").upper().strip()
        exchange = _normalise_exchange(row.get("exchange"))
        state = str(row.get("state") or "").upper().strip()
        side = _normalise_side(row.get("side"))
        publication = _normalise_publication(row.get("publication_authority"))
        execution = _normalise_execution(row.get("execution_authority"))
        trading_date = str(row.get("trading_date") or "")[:10]
        problems: list[str] = []
        if not decision_id:
            problems.append("MISSING_DECISION_ID")
        if not symbol:
            problems.append("MISSING_SYMBOL")
        if not exchange:
            problems.append("INVALID_EXCHANGE")
        if state not in ALLOWED_STATES:
            problems.append("INVALID_STATE")
        if not side:
            problems.append("INVALID_SIDE")
        if publication not in ALLOWED_PUBLICATION:
            problems.append("INVALID_PUBLICATION_AUTHORITY")
        if execution not in ALLOWED_EXECUTION:
            problems.append("INVALID_EXECUTION_AUTHORITY")
        if not _valid_date(trading_date):
            problems.append("INVALID_TRADING_DATE")
        if problems:
            item = _disposition(row, "INVALID_PRODUCTION_DECISION", problems=problems)
            if _production_authority_claimed(row):
                blockers.append(item)
            else:
                rejected_rows.append(item)
            continue

        active = _bool(row.get("active")) and state not in TERMINAL_STATES
        accepted.append((
            decision_id,
            str(row.get("thesis_id") or decision_id),
            str(row.get("thesis_key") or decision_id),
            str(row.get("signal_id") or decision_id),
            symbol,
            exchange,
            mode,
            side,
            str(row.get("setup_family") or "unknown"),
            str(row.get("activation_window") or "unknown"),
            trading_date,
            state,
            row.get("decision_action"),
            publication,
            execution,
            _json_text(row.get("entry_plan_json"), default={}),
            _json_text(row.get("risk_plan_json"), default={}),
            _json_text(row.get("candidate_snapshot_json"), default={}),
            None if row.get("frozen_evidence_json") in (None, "") else _json_text(row.get("frozen_evidence_json"), default={}),
            row.get("frozen_evidence_hash"),
            _json_text(row.get("live_snapshot_json"), default={}),
            _json_text(row.get("confidence_json"), default={}),
            _json_text(row.get("data_lineage_json"), default={}),
            _json_text(row.get("rejection_reasons_json"), default=[]),
            _json_text(row.get("latest_payload_json"), default={}),
            None if row.get("outcome_json") in (None, "") else _json_text(row.get("outcome_json"), default={}),
            row.get("model_version"),
            row.get("policy_version"),
            row.get("pipeline_version"),
            max(1, int(row.get("record_version") or 1)),
            active,
            row.get("created_at") or _now(),
            row.get("updated_at") or row.get("created_at") or _now(),
            row.get("activated_at"),
            row.get("closed_at"),
            str(row.get("contract_version") or "canonical-decision-record-1.0.0"),
        ))
    return accepted, rejected_rows, blockers


def _position_rows(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[list[tuple[Any, ...]], list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[tuple[Any, ...]] = []
    rejected_rows: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    now = _now()
    for source in rows:
        row = dict(source)
        mode = str(row.get("mode") or "").lower()
        if mode not in ALLOWED_MODES:
            rejected_rows.append(_disposition(row, "LEGACY_UNSUPPORTED_MODE"))
            continue
        position_id = str(row.get("position_id") or "").strip()
        symbol = str(row.get("symbol") or "").upper().strip()
        exchange = _normalise_exchange(row.get("exchange"))
        side = _normalise_side(row.get("side"))
        status = str(row.get("status") or "").upper().strip()
        try:
            quantity = int(row.get("quantity") or 0)
        except Exception:
            quantity = -1
        closed_at = row.get("closed_at")
        problems: list[str] = []
        if not position_id:
            problems.append("MISSING_POSITION_ID")
        if not symbol:
            problems.append("MISSING_SYMBOL")
        if not exchange:
            problems.append("INVALID_EXCHANGE")
        if not side:
            problems.append("INVALID_SIDE")
        if status not in {"OPEN", "CLOSED"}:
            problems.append("INVALID_STATUS")
        if quantity < 0 or (status == "OPEN" and quantity <= 0):
            problems.append("INVALID_QUANTITY")
        for field in ("original_entry", "original_target", "original_stop", "entry_price"):
            if not _positive(row.get(field)):
                problems.append(f"INVALID_{field.upper()}")
        if status == "OPEN" and closed_at:
            problems.append("OPEN_POSITION_HAS_CLOSED_AT")
        if problems:
            item = _disposition(row, "INVALID_PRODUCTION_MODEL_PAPER_POSITION", problems=problems)
            open_like = status == "OPEN" or (not closed_at and quantity > 0)
            (blockers if open_like else rejected_rows).append(item)
            continue
        if status == "CLOSED" and not closed_at:
            closed_at = row.get("updated_at") or now
        managed_stop = row.get("managed_stop") or row.get("original_stop")
        last_price = row.get("last_price") or row.get("entry_price") or row.get("original_entry")
        if not _positive(managed_stop) or not _positive(last_price):
            item = _disposition(
                row,
                "INVALID_PRODUCTION_MODEL_PAPER_POSITION",
                problems=["INVALID_MANAGED_STOP_OR_LAST_PRICE"],
            )
            (blockers if status == "OPEN" else rejected_rows).append(item)
            continue
        accepted.append((
            position_id,
            str(row.get("source_signal_id") or position_id),
            symbol,
            exchange,
            mode,
            side,
            status,
            quantity,
            float(row.get("original_entry")),
            float(row.get("original_target")),
            float(row.get("original_stop")),
            float(managed_stop),
            float(row.get("entry_price")),
            float(last_price),
            row.get("exit_price"),
            max(0.0, float(row.get("notional") or 0)),
            max(0.0, float(row.get("reserved_cost") or 0)),
            float(row.get("gross_pnl") or 0),
            max(0.0, float(row.get("total_cost") or 0)),
            float(row.get("net_pnl") or 0),
            max(0.0, float(row.get("open_risk") or 0)),
            row.get("high_watermark"),
            row.get("low_watermark"),
            str(row.get("hit_status") or "NONE"),
            str(row.get("action") or "HOLD"),
            row.get("exit_reason"),
            row.get("economic_outcome"),
            row.get("signal_outcome"),
            _bool(row.get("data_failure")),
            row.get("opened_at") or now,
            row.get("updated_at") or now,
            closed_at,
            str(row.get("cost_version") or "legacy-migration"),
            _json_text(row.get("payload_json"), default={}),
        ))
    return accepted, rejected_rows, blockers


def _reason_counts(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(item.get("reason") or "UNKNOWN") for item in items).items()))


def _sample(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "reason", "source_database", "source_table", "source_primary_key",
        "decision_id", "position_id", "event_id", "mode", "active_hint",
        "trading_date", "source_payload_sha256", "reason_detail",
    )
    return [{field: item.get(field) for field in fields if item.get(field) not in (None, "", {}, [])}
            for item in items[:REPORT_SAMPLE_LIMIT]]


def _build_plan(
    *,
    decision_rows: Sequence[Mapping[str, Any]],
    event_rows: Sequence[Mapping[str, Any]],
    position_rows: Sequence[Mapping[str, Any]],
    risk_state: Mapping[str, Any] | None,
    risk_event_rows: Sequence[Mapping[str, Any]],
    pre_rejected: Sequence[Mapping[str, Any]] = (),
    pre_blockers: Sequence[Mapping[str, Any]] = (),
    authority_conflicts: Sequence[Mapping[str, Any]] = (),
    source_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    def labelled(rows: Sequence[Mapping[str, Any]], table: str) -> list[dict[str, Any]]:
        result = []
        for source in rows:
            row = dict(source)
            row.setdefault("__source_database", "<direct-input>")
            row.setdefault("__source_table", table)
            result.append(row)
        return result

    decision_rows = labelled(decision_rows, "canonical_decisions")
    event_rows = labelled(event_rows, "canonical_decision_events")
    position_rows = labelled(position_rows, "model_portfolio_positions")
    risk_event_rows = labelled(risk_event_rows, "production_risk_events")
    if risk_state is not None:
        risk_state = dict(risk_state)
        risk_state.setdefault("__source_database", "<direct-input>")
        risk_state.setdefault("__source_table", "production_risk_state")

    decisions, decision_rejected, decision_blockers = _canonical_rows(decision_rows)
    positions, position_rejected, position_blockers = _position_rows(position_rows)
    rejected_rows: list[dict[str, Any]] = [dict(item) for item in pre_rejected]
    rejected_rows.extend(decision_rejected)
    rejected_rows.extend(position_rejected)
    blockers: list[dict[str, Any]] = [dict(item) for item in pre_blockers]
    blockers.extend([*decision_blockers, *position_blockers])

    accepted_decision_ids = {str(row[0]) for row in decisions}
    migrated_events: list[tuple[Any, ...]] = []
    for source in event_rows:
        event = dict(source)
        decision_id = str(event.get("decision_id") or "").strip()
        if decision_id not in accepted_decision_ids:
            rejected_rows.append(_disposition(event, "ORPHAN_CANONICAL_DECISION_EVENT"))
            continue
        event_key = str(event.get("event_key") or "").strip()
        if not event_key:
            event_key = f"legacy:{decision_id}:{event.get('event_id')}"
        migrated_events.append((
            event_key,
            decision_id,
            str(event.get("thesis_id") or decision_id),
            str(event.get("event_type") or "MIGRATED"),
            event.get("from_state"),
            event.get("to_state"),
            event.get("reason"),
            _json_text(event.get("payload_json"), default={}),
            event.get("occurred_at") or _now(),
            str(event.get("contract_version") or "canonical-decision-events-1.0.0"),
        ))

    admissions: list[tuple[Any, ...]] = []
    for source in risk_event_rows:
        event = dict(source)
        mode = str(event.get("mode") or "").lower() or None
        if mode not in {None, *ALLOWED_MODES}:
            rejected_rows.append(_disposition(event, "LEGACY_UNSUPPORTED_MODE"))
            continue
        admission_id = str(event.get("event_id") or "").strip()
        if not admission_id:
            rejected_rows.append(_disposition(event, "MISSING_RISK_EVENT_ID"))
            continue
        report = _json_value(event.get("payload_json"), default={})
        input_snapshot = report.get("input_snapshot") if isinstance(report, dict) else {}
        admissions.append((
            admission_id,
            event.get("occurred_at") or _now(),
            event.get("symbol"),
            mode,
            str(event.get("admission_state") or "UNKNOWN"),
            _json_text(event.get("reasons_json"), default=[]),
            _json_text(input_snapshot, default={}),
            _json_text(report, default={}),
        ))

    migrated_counts = {
        "canonical_decisions": len(decisions),
        "canonical_decision_events": len(migrated_events),
        "model_portfolio_positions": len(positions),
        "production_risk_state": 1 if risk_state else 0,
        "production_risk_events": len(admissions),
    }
    source_counts_final = dict(source_counts or {
        "canonical_decisions": len(decision_rows),
        "canonical_decision_events": len(event_rows),
        "model_portfolio_positions": len(position_rows),
        "production_risk_state": 1 if risk_state else 0,
        "production_risk_events": len(risk_event_rows),
    })
    rejection_counts = Counter(str(item.get("source_table") or "unknown") for item in rejected_rows)
    blocker_counts = Counter(str(item.get("source_table") or "unknown") for item in blockers)
    reconciliation: dict[str, Any] = {}
    reconciliation_ok = True
    for table, source_count in source_counts_final.items():
        migrated = int(migrated_counts.get(table) or 0)
        rejected = int(rejection_counts.get(table) or 0)
        blocked = int(blocker_counts.get(table) or 0)
        ok = int(source_count) == migrated + rejected + blocked
        reconciliation[table] = {
            "source": int(source_count),
            "migrated": migrated,
            "rejected": rejected,
            "blocked": blocked,
            "ok": ok,
        }
        reconciliation_ok = reconciliation_ok and ok

    source_hashes = [
        _row_hash(row)
        for row in [*decision_rows, *event_rows, *position_rows, *risk_event_rows]
    ]
    if risk_state:
        source_hashes.append(_row_hash(risk_state))
    source_hashes.extend(
        str(item.get("source_payload_sha256"))
        for item in pre_rejected
        if item.get("source_payload_sha256")
    )
    source_content_sha256 = hashlib.sha256(
        "\n".join(sorted(source_hashes)).encode("utf-8")
    ).hexdigest()

    all_hashes = sorted(
        [str(item.get("source_payload_sha256")) for item in rejected_rows + blockers]
        + [hashlib.sha256(_canonical_json(list(row)).encode()).hexdigest() for row in decisions]
        + [hashlib.sha256(_canonical_json(list(row)).encode()).hexdigest() for row in migrated_events]
        + [hashlib.sha256(_canonical_json(list(row)).encode()).hexdigest() for row in positions]
        + [hashlib.sha256(_canonical_json(list(row)).encode()).hexdigest() for row in admissions]
    )
    migration_run_id = hashlib.sha256("\n".join(all_hashes).encode("utf-8")).hexdigest()

    return {
        "migration_run_id": migration_run_id,
        "source_content_sha256": source_content_sha256,
        "decisions": decisions,
        "events": migrated_events,
        "positions": positions,
        "risk_state": dict(risk_state) if risk_state else None,
        "admissions": admissions,
        "rejected_rows": rejected_rows,
        "blockers": blockers,
        "counts": {
            "canonical_decisions": len(decisions),
            "canonical_decision_events": len(migrated_events),
            "model_paper_positions": len(positions),
            "risk_control_state": 1 if risk_state else 0,
            "candidate_admissions": len(admissions),
            "external_rejections": len(rejected_rows),
        },
        "source_counts": source_counts_final,
        "external_rejection_reason_counts": _reason_counts(rejected_rows),
        "blocker_reason_counts": _reason_counts(blockers),
        "external_rejection_samples": _sample(rejected_rows),
        "blocker_samples": _sample(blockers),
        "external_rejection_count": len(rejected_rows),
        "blocker_count": len(blockers),
        "reconciliation": reconciliation,
        "reconciliation_ok": reconciliation_ok,
        "authority_conflicts": [dict(item) for item in authority_conflicts],
        "authority_conflict_count": len(authority_conflicts),
        "ambiguous_authority_conflict_count": sum(1 for item in authority_conflicts if item.get("ambiguous_current_authority")),
    }


def _verify_identifiers(
    authority: Any,
    *,
    table: str,
    column: str,
    identifiers: Sequence[str],
) -> dict[str, Any]:
    expected = {str(value) for value in identifiers if str(value)}
    if not expected:
        return {"expected": 0, "found": 0, "missing": [], "ok": True}
    rows = authority.execute(
        f"SELECT {column} FROM {table} WHERE {column} = ANY(%s)",
        (list(expected),),
        fetch="all",
        statement_timeout_ms=10000,
    )
    found = {str(dict(row).get(column)) for row in rows or []}
    missing = sorted(expected - found)
    return {
        "expected": len(expected),
        "found": len(found),
        "missing": missing[:100],
        "ok": not missing,
    }


def _apply_plan(plan: Mapping[str, Any], authority: Any) -> dict[str, Any]:
    if not plan.get("reconciliation_ok"):
        report = _public_plan_report(plan, state="SOURCE_DISPOSITION_RECONCILIATION_FAILED")
        report["blocker_count"] = max(1, int(report.get("blocker_count") or 0))
        raise MigrationValidationError(report)
    if int(plan.get("blocker_count") or 0):
        raise MigrationValidationError(_public_plan_report(plan, state="BLOCKED"))

    decisions = list(plan["decisions"])
    migrated_events = list(plan["events"])
    positions = list(plan["positions"])
    risk_state = plan.get("risk_state")
    admissions = list(plan["admissions"])
    rejected_evidence = list(plan["rejected_rows"])
    migration_run_id = str(plan["migration_run_id"])

    with authority.transaction(
        isolation_level="serializable",
        lock_timeout_ms=2000,
        statement_timeout_ms=30000,
        idle_timeout_ms=10000,
    ) as pg:
        with pg.cursor() as cur:
            if decisions:
                cur.executemany(
                    """INSERT INTO trading.canonical_decisions(
                           decision_id,thesis_id,thesis_key,signal_id,symbol,exchange,mode,side,setup_family,
                           activation_window,trading_date,state,decision_action,publication_authority,execution_authority,
                           entry_plan,risk_plan,candidate_snapshot,frozen_evidence,frozen_evidence_hash,live_snapshot,
                           confidence,data_lineage,rejection_reasons,latest_payload,outcome,model_version,policy_version,
                           pipeline_version,record_version,active,created_at,updated_at,activated_at,closed_at,contract_version)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::date,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(decision_id) DO NOTHING""",
                    decisions,
                )
            if migrated_events:
                cur.executemany(
                    """INSERT INTO trading.canonical_decision_events(
                           event_key,decision_id,thesis_id,event_type,from_state,to_state,reason,payload,occurred_at,contract_version)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                       ON CONFLICT(event_key) DO NOTHING""",
                    migrated_events,
                )
            if positions:
                cur.executemany(
                    """INSERT INTO trading.model_paper_positions(
                           position_id,source_signal_id,symbol,exchange,mode,side,status,quantity,original_entry,
                           original_target,original_stop,managed_stop,entry_price,last_price,exit_price,notional,
                           reserved_cost,gross_pnl,total_cost,net_pnl,open_risk,high_watermark,low_watermark,
                           hit_status,action,exit_reason,economic_outcome,signal_outcome,data_failure,opened_at,
                           updated_at,closed_at,cost_version,payload)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT(position_id) DO NOTHING""",
                    positions,
                )
            if risk_state:
                cur.execute(
                    """UPDATE risk.control_state SET operator_stop=%s,reason=%s,updated_by=%s,
                           external_daily_pnl=%s,external_equity=%s,equity_peak=%s,account_as_of=%s,updated_at=%s
                       WHERE singleton_id=1""",
                    (
                        _bool(risk_state.get("operator_stop")),
                        risk_state.get("operator_reason"),
                        str(risk_state.get("operator_actor") or "legacy-migration"),
                        risk_state.get("external_daily_pnl"),
                        risk_state.get("external_equity"),
                        risk_state.get("equity_peak"),
                        risk_state.get("account_as_of"),
                        risk_state.get("updated_at") or _now(),
                    ),
                )
            if admissions:
                cur.executemany(
                    """INSERT INTO risk.candidate_admissions(
                           admission_id,occurred_at,symbol,mode,admission_state,reason_codes,input_snapshot,report)
                       VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
                       ON CONFLICT(admission_id) DO NOTHING""",
                    admissions,
                )

    verification = {
        "canonical_decisions": _verify_identifiers(
            authority, table="trading.canonical_decisions", column="decision_id",
            identifiers=[str(row[0]) for row in decisions],
        ),
        "canonical_decision_events": _verify_identifiers(
            authority, table="trading.canonical_decision_events", column="event_key",
            identifiers=[str(row[0]) for row in migrated_events],
        ),
        "model_paper_positions": _verify_identifiers(
            authority, table="trading.model_paper_positions", column="position_id",
            identifiers=[str(row[0]) for row in positions],
        ),
        "candidate_admissions": _verify_identifiers(
            authority, table="risk.candidate_admissions", column="admission_id",
            identifiers=[str(row[0]) for row in admissions],
        ),
    }
    verification_ok = all(item.get("ok") is True for item in verification.values())
    if not verification_ok:
        raise RuntimeError(
            "PRODUCTION_STATE_MIGRATION_POSTGRES_VERIFICATION_FAILED: "
            + json.dumps(verification, sort_keys=True, default=str)
        )
    return {
        **_public_plan_report(plan, state="POSTGRES_OPERATIONAL_AUTHORITY_MIGRATED"),
        "verification": verification,
        "verification_ok": True,
        "external_rejection_evidence": rejected_evidence,
        "external_rejection_count": len(rejected_evidence),
        "fatal_rejection_count": 0,
    }


def _public_plan_report(plan: Mapping[str, Any], *, state: str) -> dict[str, Any]:
    return {
        "ok": int(plan.get("blocker_count") or 0) == 0 and bool(plan.get("reconciliation_ok")),
        "state": state,
        "migration_run_id": plan.get("migration_run_id"),
        "source_content_sha256": plan.get("source_content_sha256"),
        "counts": dict(plan.get("counts") or {}),
        "source_counts": dict(plan.get("source_counts") or {}),
        "external_rejection_count": int(plan.get("external_rejection_count") or 0),
        "external_rejection_reason_counts": dict(plan.get("external_rejection_reason_counts") or {}),
        "external_rejection_samples": list(plan.get("external_rejection_samples") or []),
        "blocker_count": int(plan.get("blocker_count") or 0),
        "blocker_reason_counts": dict(plan.get("blocker_reason_counts") or {}),
        "blocker_samples": list(plan.get("blocker_samples") or []),
        "reconciliation": dict(plan.get("reconciliation") or {}),
        "reconciliation_ok": bool(plan.get("reconciliation_ok")),
        "authority_conflict_count": int(plan.get("authority_conflict_count") or 0),
        "ambiguous_authority_conflict_count": int(plan.get("ambiguous_authority_conflict_count") or 0),
        "authority_conflict_samples": list(plan.get("authority_conflicts") or [])[:REPORT_SAMPLE_LIMIT],
        "authority_selection_policy": "current authority, record version, event sequence, latest timestamp, source authority, deterministic hash tie-break",
        "console_output_policy": "BOUNDED_SUMMARY_FULL_EVIDENCE_IN_EXTERNAL_REPORT",
    }


def _inventory_connections(
    connections: Sequence[sqlite3.Connection],
    source_authorities: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    decisions, decisions_superseded, decision_conflicts, decision_ambiguous = _merge_rows(
        connections, "canonical_decisions", lambda row: row.get("decision_id"), source_authorities
    )
    events, events_superseded, event_conflicts, event_ambiguous = _merge_rows(
        connections,
        "canonical_decision_events",
        lambda row: row.get("event_key") or f"{row.get('decision_id')}:{row.get('event_id')}",
        source_authorities,
    )
    positions, positions_superseded, position_conflicts, position_ambiguous = _merge_rows(
        connections, "model_portfolio_positions", lambda row: row.get("position_id"), source_authorities
    )
    risk_events, risk_events_superseded, risk_event_conflicts, risk_event_ambiguous = _merge_rows(
        connections, "production_risk_events", lambda row: row.get("event_id"), source_authorities
    )
    risk_states, risk_state_superseded, risk_state_conflicts, risk_state_ambiguous = _merge_rows(
        connections, "production_risk_state", lambda _row: "singleton", source_authorities
    )
    risk_state = risk_states[0] if risk_states else None

    pre_rejected: list[dict[str, Any]] = []
    for row in [
        *decisions_superseded,
        *events_superseded,
        *positions_superseded,
        *risk_events_superseded,
        *risk_state_superseded,
    ]:
        proof = dict(row.get("__conflict_proof") or {})
        pre_rejected.append(
            _disposition(
                row,
                "SUPERSEDED_BY_HIGHER_AUTHORITY_SOURCE",
                detail=proof,
            )
        )

    pre_blockers: list[dict[str, Any]] = []
    for row in [
        *decision_ambiguous,
        *event_ambiguous,
        *position_ambiguous,
        *risk_event_ambiguous,
        *risk_state_ambiguous,
    ]:
        pre_blockers.append(
            _disposition(
                row,
                "AMBIGUOUS_CURRENT_AUTHORITY_CONFLICT",
                detail=dict(row.get("__conflict_proof") or {}),
            )
        )

    authority_conflicts = [
        *decision_conflicts, *event_conflicts, *position_conflicts,
        *risk_event_conflicts, *risk_state_conflicts,
    ]
    source_counts = {
        "canonical_decisions": len(decisions) + len(decisions_superseded) + len(decision_ambiguous),
        "canonical_decision_events": len(events) + len(events_superseded) + len(event_ambiguous),
        "model_portfolio_positions": len(positions) + len(positions_superseded) + len(position_ambiguous),
        "production_risk_state": len(risk_states) + len(risk_state_superseded) + len(risk_state_ambiguous),
        "production_risk_events": len(risk_events) + len(risk_events_superseded) + len(risk_event_ambiguous),
    }
    return _build_plan(
        decision_rows=decisions,
        event_rows=events,
        position_rows=positions,
        risk_state=risk_state,
        risk_event_rows=risk_events,
        pre_rejected=pre_rejected,
        pre_blockers=pre_blockers,
        authority_conflicts=authority_conflicts,
        source_counts=source_counts,
    )


def migrate_rows(
    *,
    decision_rows: Sequence[Mapping[str, Any]],
    event_rows: Sequence[Mapping[str, Any]],
    position_rows: Sequence[Mapping[str, Any]],
    risk_state: Mapping[str, Any] | None,
    risk_event_rows: Sequence[Mapping[str, Any]],
    authority: Any,
) -> dict[str, Any]:
    plan = _build_plan(
        decision_rows=decision_rows,
        event_rows=event_rows,
        position_rows=position_rows,
        risk_state=risk_state,
        risk_event_rows=risk_event_rows,
    )
    return _apply_plan(plan, authority)


def migrate_connections(
    connections: Sequence[sqlite3.Connection],
    authority: Any,
    source_authorities: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    authorities = list(source_authorities or _default_connection_authorities(connections))
    return _apply_plan(_inventory_connections(connections, authorities), authority)


def migrate(conn: sqlite3.Connection, authority: Any) -> dict[str, Any]:
    """Compatibility entry point retained for isolated unit tests/tools."""
    return migrate_connections([conn], authority)


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def _print_summary(report: Mapping[str, Any], report_path: Path) -> None:
    summary = {
        "ok": report.get("ok"),
        "state": report.get("state"),
        "source_counts": report.get("source_counts"),
        "migrated_counts": report.get("counts"),
        "external_rejection_count": report.get("external_rejection_count"),
        "external_rejection_reason_counts": report.get("external_rejection_reason_counts"),
        "blocker_count": report.get("blocker_count"),
        "blocker_reason_counts": report.get("blocker_reason_counts"),
        "reconciliation_ok": report.get("reconciliation_ok"),
        "authority_conflict_count": report.get("authority_conflict_count"),
        "ambiguous_authority_conflict_count": report.get("ambiguous_authority_conflict_count"),
        "report": str(report_path),
    }
    print(json.dumps(summary, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    install_dir = args.install_dir.resolve()
    report_path = args.report or (
        install_dir / "logs" / (
            "postgres-operational-state-dry-run.json"
            if args.dry_run else "postgres-operational-state-migration.json"
        )
    )
    sources = _find_dbs(install_dir)
    connections: list[sqlite3.Connection] = []
    authority: Any | None = None
    try:
        for source in sources:
            conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            connections.append(conn)
        source_authorities = [_source_authority_for_path(path) for path in sources]
        plan = _inventory_connections(connections, source_authorities)
        base_report = {
            **_public_plan_report(plan, state="DRY_RUN_RECONCILED" if args.dry_run else "PLANNED"),
            "source_sqlites": [str(path) for path in sources],
            "source_authorities": source_authorities,
            "legacy_source_disposition": "MIGRATED_OR_EXTERNALLY_REJECTED",
            "service_version": "operational-state-reconciliation-2.2.0-explicit-source-authority",
        }
        if args.dry_run:
            _write_report(report_path, base_report)
            _print_summary(base_report, report_path)
            return 0 if base_report["ok"] else 2

        dsn = os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", "").strip()
        if not dsn:
            raise RuntimeError("PROJECT_LADDU_OPERATIONAL_DSN is required")
        from core.data_plane.postgres import PostgresAuthority

        authority = PostgresAuthority(
            dsn, role="operational-state-migration", min_size=1, max_size=2
        )
        authority.open()
        try:
            result = _apply_plan(plan, authority)
        except MigrationValidationError as exc:
            failed = {
                **base_report,
                **exc.report,
                "ok": False,
                "state": "BLOCKED",
            }
            _write_report(report_path, failed)
            _print_summary(failed, report_path)
            return 2
        report = {
            "ok": True,
            "state": "POSTGRES_OPERATIONAL_AUTHORITY_MIGRATED",
            "source_sqlites": [str(path) for path in sources],
            **result,
            "legacy_source_disposition": "MIGRATED_OR_EXTERNALLY_REJECTED",
            "service_version": "operational-state-reconciliation-2.2.0-explicit-source-authority",
        }
        _write_report(report_path, report)
        _print_summary(report, report_path)
        return 0
    finally:
        for conn in connections:
            conn.close()
        if authority is not None:
            authority.close()


if __name__ == "__main__":
    raise SystemExit(main())
