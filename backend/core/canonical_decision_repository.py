"""Canonical decision persistence and lifecycle authority.

This module owns the single stable decision identity shared by every operational
surface. Only the strict Intraday and Delivery contract is copied into the
active schema; installer-created external backups are the rollback evidence.

Key invariants
--------------
* only Intraday and Delivery decisions can be persisted;
* one active Delivery thesis per symbol/side is reinforced, not duplicated;
* Intraday theses are session-scoped and may coexist only when their explicit
  setup families differ;
* the evidence snapshot is frozen the first time a decision becomes prepared
  or actionable and is never overwritten by later live refreshes;
* state changes are append-only events and invalid transitions are recorded,
  not silently applied;
* publication authority and capital execution authority remain separate.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import threading
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
from core.numeric_semantics import finite_number

from core.india_time import trading_date_ist
from core.production_mode_policy import require_production_mode
from core.canonical_admission_policy import normalize_side

CANONICAL_DECISION_VERSION = "canonical-decision-record-1.0.0"
CANONICAL_EVENT_VERSION = "canonical-decision-events-1.0.0"

ACTIVE_STATES = {"WATCHING", "PREPARED", "TRIGGERED", "CONFIRMED", "WEAKENING"}
TERMINAL_STATES = {"INVALIDATED", "COMPLETED", "REJECTED"}
FREEZE_STATES = {"PREPARED", "TRIGGERED", "CONFIRMED", "WEAKENING", "COMPLETED"}
ALLOWED_STATES = ACTIVE_STATES | TERMINAL_STATES

_ALLOWED_TRANSITIONS = {
    "WATCHING": {"WATCHING", "PREPARED", "TRIGGERED", "CONFIRMED", "INVALIDATED", "REJECTED"},
    "PREPARED": {"PREPARED", "TRIGGERED", "CONFIRMED", "WEAKENING", "INVALIDATED", "REJECTED"},
    "TRIGGERED": {"TRIGGERED", "CONFIRMED", "WEAKENING", "INVALIDATED", "COMPLETED"},
    "CONFIRMED": {"CONFIRMED", "WEAKENING", "INVALIDATED", "COMPLETED"},
    "WEAKENING": {"WEAKENING", "CONFIRMED", "INVALIDATED", "COMPLETED"},
    "INVALIDATED": {"INVALIDATED"},
    "COMPLETED": {"COMPLETED"},
    "REJECTED": {"REJECTED"},
}

_CANONICAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_decisions (
  decision_id TEXT PRIMARY KEY,
  thesis_id TEXT NOT NULL,
  thesis_key TEXT NOT NULL,
  signal_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  exchange TEXT NOT NULL DEFAULT 'NSE',
  mode TEXT NOT NULL CHECK(mode IN ('intraday','delivery')),
  side TEXT NOT NULL,
  setup_family TEXT NOT NULL,
  activation_window TEXT NOT NULL,
  trading_date TEXT NOT NULL,
  state TEXT NOT NULL,
  decision_action TEXT,
  publication_authority TEXT NOT NULL,
  execution_authority TEXT NOT NULL,
  entry_plan_json TEXT NOT NULL,
  risk_plan_json TEXT NOT NULL,
  candidate_snapshot_json TEXT NOT NULL,
  frozen_evidence_json TEXT,
  frozen_evidence_hash TEXT,
  live_snapshot_json TEXT NOT NULL,
  confidence_json TEXT NOT NULL,
  data_lineage_json TEXT NOT NULL,
  rejection_reasons_json TEXT NOT NULL,
  latest_payload_json TEXT NOT NULL,
  outcome_json TEXT,
  model_version TEXT,
  policy_version TEXT,
  pipeline_version TEXT,
  record_version INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  activated_at TEXT,
  closed_at TEXT,
  contract_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_canonical_decisions_today
  ON canonical_decisions(trading_date, mode, state, updated_at);
CREATE INDEX IF NOT EXISTS ix_canonical_decisions_thesis
  ON canonical_decisions(thesis_key, active, updated_at);
CREATE INDEX IF NOT EXISTS ix_canonical_decisions_symbol
  ON canonical_decisions(symbol, mode, side, active, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_canonical_active_thesis
  ON canonical_decisions(thesis_key) WHERE active=1;

CREATE TABLE IF NOT EXISTS canonical_decision_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  decision_id TEXT NOT NULL,
  thesis_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT,
  reason TEXT,
  payload_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  contract_version TEXT NOT NULL,
  FOREIGN KEY(decision_id) REFERENCES canonical_decisions(decision_id)
);
CREATE INDEX IF NOT EXISTS ix_canonical_decision_events_decision
  ON canonical_decision_events(decision_id, event_id);
"""


def ensure_canonical_decision_schema(conn: Any) -> None:
    """Create or physically upgrade the canonical store.

    Some previous experimental builds created a table with the same name but a
    different column contract.  ``CREATE TABLE IF NOT EXISTS`` cannot repair
    that shape and index creation then fails on missing columns.  Detect that
    table, migrate only strict two-desk rows into the current contract, and
    remove the temporary source table. External installer evidence owns rollback.
    """
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_decisions'"
    ).fetchone()
    legacy_table = None
    legacy_rows = []
    if existing:
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(canonical_decisions)").fetchall()}
        required = {
            "decision_id", "thesis_id", "thesis_key", "signal_id", "active",
            "entry_plan_json", "risk_plan_json", "candidate_snapshot_json",
            "frozen_evidence_json", "live_snapshot_json", "latest_payload_json",
            "execution_authority", "record_version",
        }
        if not required.issubset(cols):
            raw = conn.execute("SELECT * FROM canonical_decisions").fetchall()
            names = [item[1] for item in conn.execute("PRAGMA table_info(canonical_decisions)").fetchall()]
            for row in raw:
                if isinstance(row, Mapping):
                    legacy_rows.append(dict(row))
                else:
                    legacy_rows.append(dict(zip(names, row)))
            base = "canonical_decisions_legacy_v67"
            legacy_table = base
            suffix = 1
            while conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (legacy_table,)
            ).fetchone():
                legacy_table = f"{base}_{suffix}"
                suffix += 1
            conn.execute(f"ALTER TABLE canonical_decisions RENAME TO {legacy_table}")
    conn.executescript(_CANONICAL_SCHEMA)

    for old in legacy_rows:
        payload = _loads(old.get("payload_json"), {})
        if not isinstance(payload, dict):
            payload = {}
        decision_id = str(old.get("decision_id") or payload.get("decision_id") or ("LEGACY-DEC-" + hashlib.sha256(_json(old).encode()).hexdigest()[:24]))
        thesis_id = str(old.get("thesis_id") or payload.get("thesis_id") or ("THS-" + hashlib.sha256(decision_id.encode()).hexdigest()[:24]))
        signal_id = str(old.get("signal_id") or payload.get("signal_id") or decision_id)
        state = str(old.get("state") or payload.get("canonical_state") or "WATCHING").upper()
        if state not in ALLOWED_STATES:
            state = "WATCHING"
        raw_mode = str(old.get("mode") or payload.get("mode") or "").lower()
        try:
            mode = require_production_mode(raw_mode)
        except Exception:
            # Unsupported desks are not copied into the active decision store.
            continue
        side = normalize_side(old.get("side") or payload.get("side"))
        if side is None:
            # WAIT/WATCH/research rows are not canonical trade decisions.
            continue
        setup_family = str(old.get("setup_family") or payload.get("setup_family") or "unspecified")
        trading_date = str(old.get("trading_date") or payload.get("trading_date") or payload.get("session_date") or trading_date_ist())[:10]
        activation_window = str(old.get("activation_window") or (trading_date if mode == "intraday" else "persistent"))
        thesis_key = str(old.get("thesis_key") or "|".join((str(old.get("symbol") or payload.get("symbol") or "").upper(), mode, side, setup_family, activation_window)))
        entry_plan = {
            "entry": old.get("entry", payload.get("entry")),
            "target_1": old.get("target_1", payload.get("t1")),
            "target_2": old.get("target_2", payload.get("t2")),
            "timeframe": payload.get("selected_timeframe") or payload.get("timeframe"),
            "expiry": payload.get("expiry_at") or payload.get("valid_until"),
        }
        risk_plan = {
            "stop": old.get("stop", payload.get("sl")),
            "risk_reward": payload.get("rr"),
            "invalidation": payload.get("invalidation") or payload.get("risk"),
        }
        live = _loads(old.get("data_freshness_json"), {})
        if not isinstance(live, dict):
            live = {}
        live.setdefault("ltp", payload.get("ltp"))
        confidence = {
            "value": old.get("evidence_score", payload.get("score")),
            "label": old.get("confidence_label", payload.get("confidence")),
            "rank_score": old.get("decision_quality_score", payload.get("rank_score")),
        }
        publication = str(old.get("publication_authority") or "NOT_PUBLISHABLE")
        capital = str(old.get("capital_authority") or old.get("broker_execution_authority") or "NONE").upper()
        execution = "CAPITAL_ALLOWED" if capital not in {"", "NONE", "BLOCKED"} else "BLOCKED"
        frozen_json = _json(payload) if state in FREEZE_STATES else None
        frozen_hash = hashlib.sha256(frozen_json.encode()).hexdigest() if frozen_json else None
        created = str(old.get("created_at") or _now())
        updated = str(old.get("updated_at") or created)
        outcome = {"outcome_id": old.get("outcome_id")} if old.get("outcome_id") else None
        conn.execute(
            """INSERT OR IGNORE INTO canonical_decisions(
                 decision_id,thesis_id,thesis_key,signal_id,symbol,exchange,mode,side,setup_family,activation_window,trading_date,
                 state,decision_action,publication_authority,execution_authority,entry_plan_json,risk_plan_json,candidate_snapshot_json,
                 frozen_evidence_json,frozen_evidence_hash,live_snapshot_json,confidence_json,data_lineage_json,rejection_reasons_json,
                 latest_payload_json,outcome_json,model_version,policy_version,pipeline_version,record_version,active,created_at,updated_at,
                 activated_at,closed_at,contract_version
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id, thesis_id, thesis_key, signal_id, str(old.get("symbol") or payload.get("symbol") or "").upper(),
                str(old.get("exchange") or payload.get("exchange") or "NSE"), mode, side, setup_family, activation_window, trading_date,
                state, payload.get("decision"), publication, execution, _json(entry_plan), _json(risk_plan), _json(payload),
                frozen_json, frozen_hash, _json(live), _json(confidence), _json({}),
                str(old.get("rejection_reasons_json") or "[]"), _json(payload), _json(outcome) if outcome else None,
                old.get("model_version"), old.get("policy_version"), old.get("pipeline_version"),
                max(1, int(old.get("reinforcement_count") or 0) + 1), 1 if state in ACTIVE_STATES else 0,
                created, updated, old.get("activated_at"), old.get("closed_at"), CANONICAL_DECISION_VERSION,
            ),
        )
    if legacy_table:
        conn.execute(f"DROP TABLE IF EXISTS {legacy_table}")
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value or ""))
        return parsed
    except Exception:
        return default


def _sha(value: Any, length: int = 24) -> str:
    return hashlib.sha256(_json(value).encode("utf-8", errors="replace")).hexdigest()[:length]


def _slug(value: Any, default: str = "unspecified") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text or default)[:64]


def _first(row: Mapping[str, Any], names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, "", [], {}):
            return value
    return default


def _num(value: Any) -> Optional[float]:
    return finite_number(value)



class CanonicalDecisionRepository:
    def __init__(self, connection: Any, write_lock=None, event_fn: Optional[Callable[..., None]] = None, *, ensure_schema: bool = True):
        self.conn = connection
        self.write_lock = write_lock or threading.RLock()
        self._event_fn = event_fn
        if ensure_schema:
            ensure_canonical_decision_schema(self.conn)

    @staticmethod
    def derive_state(decision: Mapping[str, Any]) -> str:
        explicit = str(decision.get("canonical_state") or decision.get("lifecycle_state") or "").upper().strip()
        if explicit in ALLOWED_STATES:
            return explicit
        status = str(decision.get("status") or "").upper().strip()
        final_state = str(decision.get("final_decision_state") or "").upper().strip()
        action = str(decision.get("decision") or "").upper().strip()
        result = str(decision.get("result") or decision.get("signal_status") or "").upper().strip()
        if result in {"SUCCESS", "FAIL", "COMPLETED", "CLOSED"} or status in {"SUCCESS", "FAIL", "COMPLETED", "CLOSED"}:
            return "COMPLETED"
        if any(token in status for token in ("INVALID", "CANCEL", "EXPIRED")) or any(token in result for token in ("INVALID", "CANCEL", "EXPIRED")):
            return "INVALIDATED"
        if status in {"BLOCKED", "REJECTED"} or action in {"REJECT", "AVOID", "AVOID_LONG"}:
            return "REJECTED"
        if final_state == "PROMOTED" or status in {"PROMOTED", "SIGNAL_OPEN"}:
            return "CONFIRMED"
        if final_state == "RESEARCH_ONLY" or str(decision.get("risk_admission_state") or "").upper() == "APPROVED_RESEARCH_ONLY":
            return "PREPARED"
        if status == "TRIGGERED" or decision.get("entry_confirmed") is True:
            return "TRIGGERED"
        if status == "WEAKENING":
            return "WEAKENING"
        return "WATCHING"

    @staticmethod
    def publication_authority(decision: Mapping[str, Any], state: str) -> str:
        risk_state = str(decision.get("risk_admission_state") or "").upper()
        if risk_state == "APPROVED_CAPITAL" and state in {"TRIGGERED", "CONFIRMED", "WEAKENING", "COMPLETED"}:
            return "CAPITAL"
        if risk_state == "APPROVED_RESEARCH_ONLY" and state in {"PREPARED", "TRIGGERED", "CONFIRMED", "WEAKENING"}:
            return "MODEL_PAPER"
        return "NOT_PUBLISHABLE"

    @staticmethod
    def execution_authority(decision: Mapping[str, Any]) -> str:
        return "CAPITAL_ALLOWED" if str(decision.get("risk_admission_state") or "").upper() == "APPROVED_CAPITAL" else "BLOCKED"

    @staticmethod
    def _setup_family(decision: Mapping[str, Any]) -> str:
        return _slug(_first(decision, ("setup_family", "pattern_family", "strategy_family", "engine_name", "strategy", "pattern")))

    @staticmethod
    def _trading_date(decision: Mapping[str, Any]) -> str:
        raw = str(_first(decision, ("trading_date", "trade_date", "session_date"), "") or "")[:10]
        return raw if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else trading_date_ist()

    @classmethod
    def _identity(cls, decision: Mapping[str, Any]) -> Dict[str, str]:
        mode = require_production_mode(decision.get("mode"))
        symbol = str(decision.get("symbol") or "").upper().strip()
        if not symbol:
            raise ValueError("canonical decision requires symbol")
        side = normalize_side(decision.get("side"))
        if side is None:
            raise ValueError("canonical decision requires LONG or SHORT side")
        setup_family = cls._setup_family(decision)
        trade_date = cls._trading_date(decision)
        activation_window = trade_date if mode == "intraday" else "persistent"
        thesis_key = "|".join((symbol, mode, side, setup_family, activation_window))
        thesis_id = "THS-" + _sha(thesis_key, 24)
        return {
            "mode": mode,
            "symbol": symbol,
            "side": side,
            "setup_family": setup_family,
            "trading_date": trade_date,
            "activation_window": activation_window,
            "thesis_key": thesis_key,
            "thesis_id": thesis_id,
        }

    def _legacy_signal_id(self, identity: Mapping[str, str]) -> str:
        try:
            if identity["mode"] == "delivery":
                row = self.conn.execute(
                    "SELECT signal_id FROM signal_ledger WHERE status='OPEN' AND UPPER(symbol)=? AND UPPER(side)=? "
                    "AND LOWER(mode)='delivery' ORDER BY opened_at ASC LIMIT 1",
                    (identity["symbol"], identity["side"]),
                ).fetchone()
            else:
                # Intraday may contain genuinely independent same-symbol theses
                # within one session.  Reusing an arbitrary legacy open row here
                # would collapse those identities.  A migration caller that
                # knows the legacy signal must pass signal_id explicitly.
                return ""
            return str(row["signal_id"] if row else "").strip()
        except Exception:
            return ""

    def _existing(self, identity: Mapping[str, str]) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM canonical_decisions WHERE thesis_key=? AND active=1 ORDER BY updated_at DESC LIMIT 1",
            (identity["thesis_key"],),
        ).fetchone()
        if row:
            return dict(row)
        # Delivery owns one persistent position thesis per symbol/side.  New
        # evidence from another setup family reinforces that position rather
        # than creating a second active Delivery decision.
        if identity["mode"] == "delivery":
            row = self.conn.execute(
                "SELECT * FROM canonical_decisions WHERE symbol=? AND mode='delivery' AND side=? AND active=1 "
                "ORDER BY created_at ASC LIMIT 1",
                (identity["symbol"], identity["side"]),
            ).fetchone()
            if row:
                return dict(row)
        # Repeated terminal evaluations on the same trading date update the
        # existing audit record instead of creating scanner-noise duplicates.
        row = self.conn.execute(
            "SELECT * FROM canonical_decisions WHERE thesis_key=? AND trading_date=? ORDER BY updated_at DESC LIMIT 1",
            (identity["thesis_key"], identity["trading_date"]),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _entry_plan(decision: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "entry": _num(_first(decision, ("entry", "planned_entry", "trigger"))),
            "target_1": _num(_first(decision, ("t1", "target_1", "target"))),
            "target_2": _num(_first(decision, ("t2", "target_2"))),
            "timeframe": _first(decision, ("selected_timeframe", "timeframe", "primary_timeframe")),
            "expiry": _first(decision, ("expiry_at", "trade_window_end", "valid_until")),
        }

    @staticmethod
    def _risk_plan(decision: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "stop": _num(_first(decision, ("sl", "stop", "planned_sl"))),
            "risk_reward": _num(decision.get("rr")),
            "invalidation": _first(decision, ("invalidation", "risk", "qualification_blocker")),
            "quantity": _num(decision.get("quantity")),
            "risk_cash": _num(decision.get("risk_cash")),
        }

    @staticmethod
    def _live_snapshot(decision: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "ltp": _num(_first(decision, ("ltp", "current", "current_price"))),
            "pnl": _num(_first(decision, ("pnl_points", "pnl"))),
            "freshness_state": _first(decision, ("freshness_state", "price_freshness_state", "price_freshness"), "unknown"),
            "candle_freshness_state": _first(decision, ("candle_freshness_state", "candle_state"), "unknown"),
            "decision_as_of": _first(decision, ("decision_as_of", "last_refresh", "last_ai_validation"), _now()),
            "source_as_of": _first(decision, ("source_as_of", "provider_time", "quote_time", "candle_as_of")),
            "received_at": _first(decision, ("received_at", "fetched_at", "updated_at")),
        }

    @staticmethod
    def _confidence(decision: Mapping[str, Any]) -> Dict[str, Any]:
        raw = _first(decision, ("confidence_pct", "confidence", "rank_score", "score"))
        return {
            "value": _num(raw),
            "label": str(decision.get("confidence") or "").upper() or None,
            "rank_score": _num(_first(decision, ("rank_score", "final_rank_score", "score"))),
            "calibration": decision.get("calibrated_edge") or decision.get("governed_edge_gates"),
        }

    @staticmethod
    def _lineage(decision: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "feature_version": _first(decision, ("feature_version", "feature_manifest_hash")),
            "feature_hash": decision.get("feature_hash"),
            "dataset_fingerprint": decision.get("dataset_fingerprint"),
            "source_as_of": _first(decision, ("source_as_of", "provider_time", "quote_time", "candle_as_of")),
            "received_at": _first(decision, ("received_at", "fetched_at", "updated_at")),
            "universe_id": decision.get("universe_id"),
            "universe_membership_as_of": decision.get("universe_membership_as_of"),
            "identity_verified": decision.get("identity_verified"),
            "data_quality_state": _first(decision, ("data_quality_state", "rank_scoring_state", "freshness_state")),
        }

    @staticmethod
    def _rejection_reasons(decision: Mapping[str, Any], state: str) -> list[str]:
        values = []
        for source in (
            decision.get("rejection_reasons"), decision.get("promotion_blocked_by"),
            decision.get("hard_blocks"), decision.get("capital_blocks"),
        ):
            if isinstance(source, (list, tuple, set)):
                values.extend(str(item).strip() for item in source if str(item).strip())
        if state in {"REJECTED", "INVALIDATED"}:
            reason = str(_first(decision, ("qualification_blocker", "reason", "risk"), "") or "").strip()
            if reason:
                values.append(reason)
        return list(dict.fromkeys(values))

    @staticmethod
    def _freeze_payload(decision: Mapping[str, Any]) -> Dict[str, Any]:
        # Freeze the complete point-in-time decision payload.  This is larger
        # than a hand-picked indicator subset, but prevents future attribution
        # from silently losing a field that later becomes important.
        return json.loads(_json(dict(decision)))

    def _emit_event(
        self,
        *,
        decision_id: str,
        thesis_id: str,
        event_type: str,
        from_state: Optional[str],
        to_state: Optional[str],
        reason: str,
        payload: Mapping[str, Any],
        occurred_at: Optional[str] = None,
    ) -> None:
        stamp = occurred_at or _now()
        material = {
            "decision_id": decision_id,
            "event_type": event_type,
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
            "payload": payload,
        }
        event_key = "EVT-" + _sha(material, 32)
        self.conn.execute(
            """INSERT OR IGNORE INTO canonical_decision_events(
                 event_key,decision_id,thesis_id,event_type,from_state,to_state,reason,payload_json,occurred_at,contract_version
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (event_key, decision_id, thesis_id, event_type, from_state, to_state, reason, _json(dict(payload)), stamp, CANONICAL_EVENT_VERSION),
        )

    def record(self, decision: Mapping[str, Any]) -> Dict[str, Any]:
        source = dict(decision or {})
        identity = self._identity(source)
        requested_state = self.derive_state(source)
        stamp = _now()
        publication = self.publication_authority(source, requested_state)
        execution = self.execution_authority(source)
        entry_plan = self._entry_plan(source)
        risk_plan = self._risk_plan(source)
        live = self._live_snapshot(source)
        confidence = self._confidence(source)
        lineage = self._lineage(source)
        rejection_reasons = self._rejection_reasons(source, requested_state)
        latest_payload = json.loads(_json(source))

        with self.write_lock:
            existing = self._existing(identity)
            if existing:
                decision_id = str(existing["decision_id"])
                thesis_id = str(existing["thesis_id"])
                signal_id = str(existing["signal_id"] or decision_id)
                current_state = str(existing["state"] or "WATCHING")
                next_state = requested_state
                transition_allowed = next_state in _ALLOWED_TRANSITIONS.get(current_state, {current_state})
                if not transition_allowed:
                    self._emit_event(
                        decision_id=decision_id, thesis_id=thesis_id,
                        event_type="TRANSITION_REJECTED", from_state=current_state, to_state=next_state,
                        reason="invalid canonical lifecycle transition", payload={"requested": next_state, "source_status": source.get("status")},
                    )
                    next_state = current_state
                frozen_json = existing.get("frozen_evidence_json")
                frozen_hash = existing.get("frozen_evidence_hash")
                if not frozen_json and next_state in FREEZE_STATES:
                    frozen = self._freeze_payload(source)
                    frozen_json = _json(frozen)
                    frozen_hash = _sha(frozen, 64)
                    self._emit_event(
                        decision_id=decision_id, thesis_id=thesis_id,
                        event_type="EVIDENCE_FROZEN", from_state=current_state, to_state=next_state,
                        reason="first publishable/actionable decision snapshot", payload={"frozen_evidence_hash": frozen_hash},
                    )
                active = 1 if next_state in ACTIVE_STATES else 0
                activated_at = existing.get("activated_at") or (stamp if next_state in FREEZE_STATES else None)
                closed_at = existing.get("closed_at") or (stamp if next_state in TERMINAL_STATES else None)
                self.conn.execute(
                    """UPDATE canonical_decisions SET
                         state=?,decision_action=?,publication_authority=?,execution_authority=?,entry_plan_json=?,risk_plan_json=?,
                         frozen_evidence_json=?,frozen_evidence_hash=?,live_snapshot_json=?,confidence_json=?,data_lineage_json=?,
                         rejection_reasons_json=?,latest_payload_json=?,model_version=?,policy_version=?,pipeline_version=?,
                         record_version=record_version+1,active=?,updated_at=?,activated_at=?,closed_at=?
                       WHERE decision_id=?""",
                    (
                        next_state, source.get("decision"), publication, execution, _json(entry_plan), _json(risk_plan),
                        frozen_json, frozen_hash, _json(live), _json(confidence), _json(lineage), _json(rejection_reasons),
                        _json(latest_payload), source.get("model_version") or source.get("ranking_version"), source.get("policy_version"),
                        source.get("decision_pipeline_version") or source.get("pipeline_version"), active, stamp, activated_at, closed_at,
                        decision_id,
                    ),
                )
                if next_state != current_state:
                    self._emit_event(
                        decision_id=decision_id, thesis_id=thesis_id,
                        event_type="STATE_CHANGED", from_state=current_state, to_state=next_state,
                        reason=str(source.get("reason") or "canonical state update"), payload={"publication_authority": publication, "execution_authority": execution},
                    )
                else:
                    event_type = "THESIS_REINFORCED" if identity["mode"] == "delivery" else "DECISION_REFRESHED"
                    self._emit_event(
                        decision_id=decision_id, thesis_id=thesis_id,
                        event_type=event_type, from_state=current_state, to_state=next_state,
                        reason="new evidence/live state merged into canonical decision", payload={"setup_family": identity["setup_family"], "live_snapshot": live},
                    )
            else:
                legacy_signal = str(source.get("signal_id") or source.get("decision_id") or "").strip() or self._legacy_signal_id(identity)
                decision_id = legacy_signal or ("DEC-" + _sha({"thesis_key": identity["thesis_key"], "trading_date": identity["trading_date"]}, 28))
                signal_id = legacy_signal or decision_id
                thesis_id = identity["thesis_id"]
                candidate_snapshot = self._freeze_payload(source)
                frozen_json = _json(candidate_snapshot) if requested_state in FREEZE_STATES else None
                frozen_hash = _sha(candidate_snapshot, 64) if frozen_json else None
                active = 1 if requested_state in ACTIVE_STATES else 0
                activated_at = stamp if requested_state in FREEZE_STATES else None
                closed_at = stamp if requested_state in TERMINAL_STATES else None
                self.conn.execute(
                    """INSERT INTO canonical_decisions(
                         decision_id,thesis_id,thesis_key,signal_id,symbol,exchange,mode,side,setup_family,activation_window,trading_date,
                         state,decision_action,publication_authority,execution_authority,entry_plan_json,risk_plan_json,candidate_snapshot_json,
                         frozen_evidence_json,frozen_evidence_hash,live_snapshot_json,confidence_json,data_lineage_json,rejection_reasons_json,
                         latest_payload_json,outcome_json,model_version,policy_version,pipeline_version,record_version,active,created_at,updated_at,
                         activated_at,closed_at,contract_version
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        decision_id, thesis_id, identity["thesis_key"], signal_id, identity["symbol"], str(source.get("exchange") or "NSE"),
                        identity["mode"], identity["side"], identity["setup_family"], identity["activation_window"], identity["trading_date"],
                        requested_state, source.get("decision"), publication, execution, _json(entry_plan), _json(risk_plan), _json(candidate_snapshot),
                        frozen_json, frozen_hash, _json(live), _json(confidence), _json(lineage), _json(rejection_reasons), _json(latest_payload),
                        None, source.get("model_version") or source.get("ranking_version"), source.get("policy_version"),
                        source.get("decision_pipeline_version") or source.get("pipeline_version"), 1, active, stamp, stamp, activated_at, closed_at,
                        CANONICAL_DECISION_VERSION,
                    ),
                )
                self._emit_event(
                    decision_id=decision_id, thesis_id=thesis_id,
                    event_type="DECISION_CREATED", from_state=None, to_state=requested_state,
                    reason=str(source.get("reason") or "canonical decision created"),
                    payload={"thesis_key": identity["thesis_key"], "publication_authority": publication, "execution_authority": execution},
                )
                if frozen_hash:
                    self._emit_event(
                        decision_id=decision_id, thesis_id=thesis_id,
                        event_type="EVIDENCE_FROZEN", from_state=None, to_state=requested_state,
                        reason="decision was publishable/actionable at creation", payload={"frozen_evidence_hash": frozen_hash},
                    )
            self.conn.commit()
            row = self.get(decision_id)

        if self._event_fn is not None:
            try:
                self._event_fn("INFO", "canonical_decision", "Canonical decision recorded", {
                    "decision_id": decision_id, "thesis_id": thesis_id, "symbol": identity["symbol"],
                    "mode": identity["mode"], "state": row.get("state"), "publication_authority": row.get("publication_authority"),
                })
            except Exception:
                pass
        return row

    def get(self, decision_id: str) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM canonical_decisions WHERE decision_id=?", (str(decision_id),)).fetchone()
        return self._decode(dict(row)) if row else {}

    @staticmethod
    def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
        for name, default in (
            ("entry_plan_json", {}), ("risk_plan_json", {}), ("candidate_snapshot_json", {}),
            ("frozen_evidence_json", {}), ("live_snapshot_json", {}), ("confidence_json", {}),
            ("data_lineage_json", {}), ("rejection_reasons_json", []), ("latest_payload_json", {}),
            ("outcome_json", {}),
        ):
            row[name[:-5] if name.endswith("_json") else name] = _loads(row.get(name), default)
        return row

    def events(self, decision_id: str) -> list[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM canonical_decision_events WHERE decision_id=? ORDER BY event_id ASC", (str(decision_id),)
        ).fetchall()
        out = []
        for raw in rows:
            row = dict(raw)
            row["payload"] = _loads(row.get("payload_json"), {})
            out.append(row)
        return out

    def record_outcome(self, decision_or_signal_id: str, outcome: Mapping[str, Any]) -> Dict[str, Any]:
        key = str(decision_or_signal_id or "").strip()
        if not key:
            return {}
        with self.write_lock:
            row = self.conn.execute(
                "SELECT * FROM canonical_decisions WHERE decision_id=? OR signal_id=? ORDER BY updated_at DESC LIMIT 1", (key, key)
            ).fetchone()
            if not row:
                return {}
            current = dict(row)
            decision_id = str(current["decision_id"])
            payload = dict(outcome or {})
            stamp = str(payload.get("closed_at") or _now())
            terminal_state = "INVALIDATED" if str(payload.get("status") or "").upper() in {"CANCELLED", "EXPIRED", "INVALIDATED"} else "COMPLETED"
            self.conn.execute(
                "UPDATE canonical_decisions SET state=?,active=0,outcome_json=?,updated_at=?,closed_at=COALESCE(closed_at,?) WHERE decision_id=?",
                (terminal_state, _json(payload), stamp, stamp, decision_id),
            )
            self._emit_event(
                decision_id=decision_id, thesis_id=str(current["thesis_id"]), event_type="OUTCOME_RECORDED",
                from_state=str(current["state"]), to_state=terminal_state,
                reason=str(payload.get("result") or payload.get("status") or "outcome recorded"), payload=payload, occurred_at=stamp,
            )
            self.conn.commit()
            return self.get(decision_id)

    def today_entries(self, mode: str = "all", trading_date: Optional[str] = None, limit: int = 100) -> list[Dict[str, Any]]:
        day = str(trading_date or trading_date_ist())[:10]
        where = ["trading_date=?", "publication_authority IN ('CAPITAL','MODEL_PAPER')", "state IN ('PREPARED','TRIGGERED','CONFIRMED','WEAKENING')"]
        params: list[Any] = [day]
        if str(mode or "all").lower() != "all":
            where.append("mode=?")
            params.append(require_production_mode(mode))
        params.append(max(1, int(limit)))
        rows = self.conn.execute(
            f"SELECT * FROM canonical_decisions WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT ?", tuple(params)
        ).fetchall()
        out = []
        for raw in rows:
            decoded = self._decode(dict(raw))
            latest = dict(decoded.get("latest_payload") or {})
            entry = decoded.get("entry_plan") or {}
            risk = decoded.get("risk_plan") or {}
            live = decoded.get("live_snapshot") or {}
            confidence = decoded.get("confidence") or {}
            latest.update({
                "decision_id": decoded["decision_id"],
                "thesis_id": decoded["thesis_id"],
                "signal_id": decoded["signal_id"],
                "symbol": decoded["symbol"],
                "exchange": decoded["exchange"],
                "mode": decoded["mode"],
                "side": decoded["side"],
                "canonical_state": decoded["state"],
                "publication_authority": decoded["publication_authority"],
                "execution_authority": decoded["execution_authority"],
                "paper_only": decoded["publication_authority"] == "MODEL_PAPER",
                "book": "MODEL_PAPER" if decoded["publication_authority"] == "MODEL_PAPER" else "CAPITAL",
                "entry": entry.get("entry"), "t1": entry.get("target_1"), "t2": entry.get("target_2"),
                "sl": risk.get("stop"), "rr": risk.get("risk_reward"),
                "ltp": live.get("ltp"), "pnl_points": live.get("pnl"),
                "confidence_value": confidence.get("value"),
                "trading_date": decoded["trading_date"], "opened_at": decoded.get("activated_at") or decoded.get("created_at"),
                "last_update": decoded.get("updated_at"),
                "frozen_evidence_hash": decoded.get("frozen_evidence_hash"),
            })
            out.append(latest)
        return out
