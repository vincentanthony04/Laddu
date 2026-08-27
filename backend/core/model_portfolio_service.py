"""Governed, advisory-only model portfolio.

This is a simulation ledger. It has no broker client and accepts only approved
production Final decisions plus fresh, verified executable quotes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import nullcontext
import hashlib
import json
import math
import threading
from typing import Any, Dict, Iterable, List

from core.candidate_eligibility_authority import DEFAULT_CANDIDATE_ELIGIBILITY_AUTHORITY
from core.india_cash_cost_service import IndiaCashCostService
from core.execution_slippage_calibration_authority import DEFAULT_EXECUTION_SLIPPAGE_CALIBRATION_AUTHORITY
from core.current_managed_risk_authority import DEFAULT_CURRENT_MANAGED_RISK_AUTHORITY
from core.intraday_session_policy import IntradaySessionPolicy
from core.model_portfolio_risk_service import ModelPortfolioRiskService
from core.model_paper_lifecycle_authority import DEFAULT_MODEL_PAPER_LIFECYCLE_AUTHORITY
from core.open_position_gap_recovery_authority import DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY
from core.outcome_accuracy_taxonomy import DEFAULT_OUTCOME_ACCURACY_TAXONOMY
from core.signal_age_authority import DEFAULT_SIGNAL_AGE_AUTHORITY
from core.india_time import INDIA_TZ


def _utc(at: datetime | None = None) -> str:
    value = at or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=INDIA_TZ)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _json(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str)


class ModelPortfolioService:
    POLICY_VERSION = "model-paper-portfolio-1.0.0"

    def __init__(
        self,
        store: Any,
        *,
        equity: float = 500_000.0,
        intraday_cap: float = 100_000.0,
        cost_service: IndiaCashCostService | None = None,
        risk_service: ModelPortfolioRiskService | None = None,
        session_policy: IntradaySessionPolicy | None = None,
        repository: Any | None = None,
        settlement_sink: Any | None = None,
    ):
        self.store = store
        self.repository = repository
        self.settlement_sink = settlement_sink
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.RLock()
        self.equity = float(equity)
        self.intraday_cap = float(intraday_cap)
        self.costs = cost_service or IndiaCashCostService()
        self.risk = risk_service or ModelPortfolioRiskService(
            equity=self.equity, intraday_cap=self.intraday_cap, cost_service=self.costs
        )
        self.session = session_policy or IntradaySessionPolicy()
        if self.repository is None:
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_portfolio_positions (
                  position_id TEXT PRIMARY KEY,
                  source_signal_id TEXT NOT NULL UNIQUE,
                  symbol TEXT NOT NULL,
                  exchange TEXT NOT NULL,
                  bse_group TEXT,
                  mode TEXT NOT NULL CHECK(mode IN ('intraday','delivery')),
                  side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
                  status TEXT NOT NULL,
                  quantity INTEGER NOT NULL,
                  original_entry REAL NOT NULL,
                  original_target REAL NOT NULL,
                  original_stop REAL NOT NULL,
                  managed_stop REAL NOT NULL,
                  entry_price REAL NOT NULL,
                  last_price REAL NOT NULL,
                  exit_price REAL,
                  notional REAL NOT NULL,
                  reserved_cost REAL NOT NULL,
                  gross_pnl REAL NOT NULL DEFAULT 0,
                  total_cost REAL NOT NULL DEFAULT 0,
                  net_pnl REAL NOT NULL DEFAULT 0,
                  open_risk REAL NOT NULL,
                  current_managed_risk REAL,
                  secured_profit REAL NOT NULL DEFAULT 0,
                  managed_risk_state TEXT,
                  high_watermark REAL,
                  low_watermark REAL,
                  hit_status TEXT NOT NULL DEFAULT 'NONE',
                  action TEXT NOT NULL,
                  exit_reason TEXT,
                  economic_outcome TEXT,
                  signal_outcome TEXT,
                  data_failure INTEGER NOT NULL DEFAULT 0,
                  opened_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  closed_at TEXT,
                  cost_version TEXT NOT NULL,
                  execution_model_version TEXT,
                  execution_model_contract_hash TEXT,
                  execution_calibration_state TEXT,
                  execution_calibration_snapshot_hash TEXT,
                  execution_model_json TEXT,
                  payload_json TEXT NOT NULL,
                  CHECK(
                    (exchange='NSE' AND bse_group IS NULL)
                    OR (exchange='BSE' AND bse_group IN
                        ('A','B','X','XT','Z','ZP','XC','XD','T','SS','ST'))
                  )
                );
                CREATE INDEX IF NOT EXISTS ix_model_portfolio_status_mode
                  ON model_portfolio_positions(status,mode,opened_at);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_model_portfolio_open_symbol
                  ON model_portfolio_positions(symbol) WHERE status='OPEN';
                CREATE TABLE IF NOT EXISTS model_portfolio_research (
                  research_id TEXT PRIMARY KEY,
                  source_signal_id TEXT,
                  symbol TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  disposition TEXT NOT NULL,
                  observed_price REAL,
                  occurred_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_model_research_time
                  ON model_portfolio_research(occurred_at DESC);
                CREATE TABLE IF NOT EXISTS signal_lifecycle_events (
                  event_key TEXT PRIMARY KEY,
                  signal_id TEXT NOT NULL,
                  position_id TEXT,
                  decision_id TEXT,
                  event_type TEXT NOT NULL,
                  thesis_state TEXT,
                  occurred_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_signal_lifecycle_events_signal
                  ON signal_lifecycle_events(signal_id, occurred_at);
                """
            )
            columns = {
                str(row[1])
                for row in self.store.conn.execute(
                    "PRAGMA table_info(model_portfolio_positions)"
                ).fetchall()
            }
            if "bse_group" not in columns:
                self.store.conn.execute(
                    "ALTER TABLE model_portfolio_positions ADD COLUMN bse_group TEXT"
                )
            for column, sql_type in (
                ("current_managed_risk", "REAL"),
                ("secured_profit", "REAL NOT NULL DEFAULT 0"),
                ("managed_risk_state", "TEXT"),
                ("execution_model_version", "TEXT"),
                ("execution_model_contract_hash", "TEXT"),
                ("execution_calibration_state", "TEXT"),
                ("execution_calibration_snapshot_hash", "TEXT"),
                ("execution_model_json", "TEXT"),
            ):
                if column not in columns:
                    self.store.conn.execute(
                        f"ALTER TABLE model_portfolio_positions ADD COLUMN {column} {sql_type}"
                    )
            self.store.conn.execute(
                """UPDATE model_portfolio_positions
                      SET bse_group=UPPER(NULLIF(json_extract(payload_json,'$.bse_group'),''))
                    WHERE exchange='BSE' AND COALESCE(bse_group,'')=''"""
            )
            self.store.conn.execute(
                "UPDATE model_portfolio_positions SET bse_group=NULL WHERE exchange='NSE'"
            )
            managed_risk_columns = {"entry_price", "managed_stop", "quantity", "original_stop", "side", "status", "open_risk"}
            if managed_risk_columns.issubset(columns):
                self.store.conn.execute(
                    """UPDATE model_portfolio_positions
                          SET current_managed_risk=CASE
                                WHEN status='CLOSED' THEN 0
                                WHEN side='LONG' THEN ROUND(MAX(0,(entry_price-managed_stop)*quantity),2)
                                ELSE ROUND(MAX(0,(managed_stop-entry_price)*quantity),2) END,
                              secured_profit=CASE
                                WHEN side='LONG' THEN ROUND(MAX(0,(managed_stop-entry_price)*quantity),2)
                                ELSE ROUND(MAX(0,(entry_price-managed_stop)*quantity),2) END,
                              managed_risk_state=CASE
                                WHEN status='CLOSED' THEN 'CLOSED'
                                WHEN side='LONG' AND managed_stop>entry_price THEN 'PROFIT_SECURED'
                                WHEN side='SHORT' AND managed_stop<entry_price THEN 'PROFIT_SECURED'
                                WHEN managed_stop=entry_price THEN 'BREAKEVEN_PROTECTED'
                                WHEN (side='LONG' AND managed_stop>original_stop) OR (side='SHORT' AND managed_stop<original_stop) THEN 'RISK_REDUCED'
                                ELSE 'ORIGINAL_RISK' END
                        WHERE current_managed_risk IS NULL OR managed_risk_state IS NULL"""
                )
            unresolved = self.store.conn.execute(
                """SELECT position_id FROM model_portfolio_positions
                    WHERE exchange IS NULL
                       OR exchange NOT IN ('NSE','BSE')
                       OR (exchange='BSE' AND COALESCE(bse_group,'') NOT IN
                           ('A','B','X','XT','Z','ZP','XC','XD','T','SS','ST'))
                    LIMIT 1"""
            ).fetchone()
            if unresolved:
                raise RuntimeError(
                    f"MODEL_PAPER_VENUE_IDENTITY_UNRESOLVED:{unresolved[0]}"
                )
            self.store.conn.executescript(
                """DROP TRIGGER IF EXISTS trg_model_portfolio_venue_required;
                   CREATE TRIGGER trg_model_portfolio_venue_required
                   BEFORE INSERT ON model_portfolio_positions
                   FOR EACH ROW WHEN
                       NEW.exchange IS NULL
                       OR NEW.exchange NOT IN ('NSE','BSE')
                       OR (NEW.exchange='NSE' AND NEW.bse_group IS NOT NULL)
                       OR (NEW.exchange='BSE' AND COALESCE(NEW.bse_group,'') NOT IN
                           ('A','B','X','XT','Z','ZP','XC','XD','T','SS','ST'))
                   BEGIN
                     SELECT RAISE(ABORT,'MODEL_PAPER_VENUE_IDENTITY_REQUIRED');
                   END;
                   DROP TRIGGER IF EXISTS trg_model_portfolio_venue_immutable;
                   CREATE TRIGGER trg_model_portfolio_venue_immutable
                   BEFORE UPDATE OF exchange,bse_group ON model_portfolio_positions
                   FOR EACH ROW WHEN
                       NEW.exchange IS NOT OLD.exchange
                       OR NEW.bse_group IS NOT OLD.bse_group
                   BEGIN
                     SELECT RAISE(ABORT,'MODEL_PAPER_VENUE_IDENTITY_IS_IMMUTABLE');
                   END;
                """
            )
            if managed_risk_columns.issubset(columns):
                self.store.conn.executescript(
                    """DROP TRIGGER IF EXISTS trg_model_portfolio_initial_risk_immutable;
                       CREATE TRIGGER trg_model_portfolio_initial_risk_immutable
                       BEFORE UPDATE OF open_risk ON model_portfolio_positions
                       FOR EACH ROW WHEN NEW.open_risk IS NOT OLD.open_risk
                       BEGIN
                         SELECT RAISE(ABORT,'MODEL_PAPER_INITIAL_RISK_IS_IMMUTABLE');
                       END;
                       DROP TRIGGER IF EXISTS trg_model_portfolio_managed_stop_non_widening;
                       CREATE TRIGGER trg_model_portfolio_managed_stop_non_widening
                       BEFORE UPDATE OF managed_stop ON model_portfolio_positions
                       FOR EACH ROW WHEN
                           (OLD.side='LONG' AND NEW.managed_stop < OLD.managed_stop)
                           OR (OLD.side='SHORT' AND NEW.managed_stop > OLD.managed_stop)
                       BEGIN
                         SELECT RAISE(ABORT,'MODEL_PAPER_MANAGED_STOP_CANNOT_WIDEN_RISK');
                       END;"""
                )
            self.store.conn.commit()

    @staticmethod
    def _quote(quote: Any) -> Dict[str, Any]:
        if isinstance(quote, (int, float)):
            return {"ltp": float(quote), "verified": False, "fresh": False, "executable": False}
        return dict(quote or {})

    @classmethod
    def _quote_valid(cls, quote: Any) -> bool:
        row = cls._quote(quote)
        price = _float(row.get("ltp", row.get("price")))
        return bool(
            price and price > 0
            and row.get("verified") is True
            and row.get("fresh") is True
            and row.get("executable", True) is True
        )

    def _research(self, candidate: Dict[str, Any], disposition: str, at: datetime, price: float | None = None) -> Dict[str, Any]:
        signal_id = str(candidate.get("signal_id") or candidate.get("source_signal_id") or "")
        symbol = str(candidate.get("symbol") or candidate.get("stock") or "").upper()
        mode = str(candidate.get("mode") or "").lower()
        # Stable identity prevents the quote loop from appending the same
        # disposition every few seconds. A changed disposition receives a new
        # row; repeated observations refresh price/time in place.
        raw = f"{signal_id}|{symbol}|{mode}|{disposition}"
        research_id = hashlib.sha256(raw.encode()).hexdigest()[:28]
        if self.repository is not None:
            self.repository.research_observation({
                "observation_id": research_id, "source_signal_id": signal_id or None,
                "symbol": symbol, "mode": mode, "disposition": disposition,
                "observed_price": price, "occurred_at": _utc(at), "payload": candidate,
            })
        else:
            with self.store.write_lock:
                self.store.conn.execute(
                    """INSERT INTO model_portfolio_research
                       (research_id,source_signal_id,symbol,mode,disposition,observed_price,occurred_at,payload_json)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(research_id) DO UPDATE SET
                         observed_price=excluded.observed_price,
                         occurred_at=excluded.occurred_at,
                         payload_json=excluded.payload_json""",
                    (research_id, signal_id or None, symbol, mode, disposition, price, _utc(at), _json(candidate)),
                )
                self.store.conn.commit()
        return {"state": "RESEARCH", "disposition": disposition, "symbol": symbol, "mode": mode}

    @staticmethod
    def _execution_model_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
        direct = row.get("execution_model")
        if isinstance(direct, dict):
            return dict(direct)
        raw = row.get("execution_model_json")
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else None
        if payload is None:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except Exception:
                payload = {}
        model = (payload or {}).get("execution_model")
        return dict(model) if isinstance(model, dict) else {}

    def _execution_model_contract(self, row: Dict[str, Any], quote: Dict[str, Any], *, mode: str, quantity: int) -> Dict[str, Any]:
        candidate = dict(row or {})
        candidate["quote"] = dict(quote or {})
        schedule = self.costs.schedule_for()
        slip = schedule.intraday_slippage_bps if mode == "intraday" else schedule.delivery_slippage_bps
        return DEFAULT_EXECUTION_SLIPPAGE_CALIBRATION_AUTHORITY.contract(
            candidate, mode=mode, quantity=quantity, schedule_slippage_bps=slip,
        )

    def _lifecycle_event_record(self, event_type: str, row: Dict[str, Any], *, at: datetime, payload: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
        signal_id = str(row.get("source_signal_id") or row.get("signal_id") or row.get("decision_id") or row.get("position_id") or "").strip()
        if not signal_id:
            return None
        detail = dict(payload or {})
        source_payload = {}
        try:
            source_payload = json.loads(row.get("payload_json") or "{}") if not isinstance(row.get("payload"), dict) else dict(row.get("payload") or {})
        except Exception:
            source_payload = {}
        detail.setdefault("symbol", row.get("symbol"))
        detail.setdefault("mode", row.get("mode"))
        detail.setdefault("side", row.get("side"))
        detail.setdefault("model_version", row.get("model_version"))
        detail.setdefault("policy_version", row.get("policy_version") or self.POLICY_VERSION)
        detail.setdefault("lifecycle_authority_version", DEFAULT_MODEL_PAPER_LIFECYCLE_AUTHORITY.authority_version)
        if "signal_age" not in detail:
            detail["signal_age"] = DEFAULT_SIGNAL_AGE_AUTHORITY.measure(
                generated_at=source_payload.get("generated_at") or source_payload.get("decision_generated_at") or source_payload.get("created_at"),
                opened_at=row.get("opened_at"), at=at, mode=row.get("mode"),
                approved_policy=(source_payload.get("approved_age_risk_policy") if isinstance(source_payload.get("approved_age_risk_policy"), dict) else None),
            )
        reassessment = detail.get("thesis_reassessment") if isinstance(detail.get("thesis_reassessment"), dict) else {}
        current_evidence = reassessment.get("current_thesis_evidence") if isinstance(reassessment.get("current_thesis_evidence"), dict) else {}
        age = detail.get("signal_age") if isinstance(detail.get("signal_age"), dict) else {}
        generated_at = detail.get("generated_at") or source_payload.get("generated_at") or source_payload.get("decision_generated_at") or source_payload.get("created_at")
        opened_at = row.get("opened_at") or detail.get("opened_at")
        evidence_hash = (
            detail.get("evidence_hash") or detail.get("canonical_snapshot_hash")
            or current_evidence.get("packet_hash") or current_evidence.get("canonical_snapshot_hash")
            or source_payload.get("evidence_hash") or source_payload.get("evidence_snapshot_hash")
            or source_payload.get("canonical_snapshot_hash")
        )
        return {
            "signal_id": signal_id,
            "position_id": row.get("position_id"),
            "decision_id": row.get("decision_id") or row.get("source_signal_id"),
            "event_type": str(event_type).upper(),
            "thesis_state": detail.get("thesis_state") or (reassessment.get("state") if isinstance(reassessment, dict) else None),
            "occurred_at": _utc(at),
            "generated_at": generated_at,
            "opened_at": opened_at,
            "model_version": detail.get("model_version") or row.get("model_version") or source_payload.get("model_version"),
            "policy_version": detail.get("policy_version") or row.get("policy_version") or source_payload.get("policy_version") or source_payload.get("model_policy") or self.POLICY_VERSION,
            "evidence_hash": evidence_hash,
            "generation_age_seconds": age.get("generation_age_seconds"),
            "open_age_seconds": age.get("open_age_seconds"),
            "mode": row.get("mode"),
            "payload": detail,
        }

    def _append_lifecycle_record(self, event: Dict[str, Any] | None) -> None:
        if not event:
            return
        signal_id = str(event.get("signal_id") or "")
        if self.repository is not None and callable(getattr(self.repository, "append_signal_lifecycle_event", None)):
            try:
                self.repository.append_signal_lifecycle_event(event)
            except Exception as exc:
                # Evidence projection remains non-blocking. In production the
                # position transaction carries an exact lifecycle intent in its
                # PostgreSQL outbox; SignalLifecycleReconciliationService replays it.
                try:
                    self.store.event("WARN", "signal_lifecycle", "Lifecycle evidence append failed", {
                        "signal_id": signal_id, "event_type": event.get("event_type"), "error": str(exc)[:220],
                    })
                except Exception:
                    pass
            return
        detail = dict(event.get("payload") or {})
        raw = f"{signal_id}|{event['event_type']}|{event['occurred_at']}|{event.get('thesis_state') or ''}|{detail.get('action') or ''}"
        event_key = hashlib.sha256(raw.encode()).hexdigest()
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT OR IGNORE INTO signal_lifecycle_events(event_key,signal_id,position_id,decision_id,event_type,thesis_state,occurred_at,payload_json)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (event_key, signal_id, event.get("position_id"), event.get("decision_id"), event["event_type"], event.get("thesis_state"), event["occurred_at"], _json(detail)),
            )
            self.store.conn.commit()

    def _append_lifecycle_event(self, event_type: str, row: Dict[str, Any], *, at: datetime, payload: Dict[str, Any] | None = None) -> None:
        self._append_lifecycle_record(self._lifecycle_event_record(event_type, row, at=at, payload=payload))

    def _mirror_runtime_risk(self, payload: Dict[str, Any]) -> None:
        """Best-effort live-risk recovery mirror; never owns admission."""
        runtime = getattr(self.store, "runtime_market_state", None)
        writer = getattr(runtime, "record_risk_state", None)
        if not callable(writer):
            return
        try:
            row = dict(payload or {})
            row.setdefault("decision_id", row.get("position_id") or row.get("source_signal_id"))
            row.setdefault("state", row.get("status") or "WATCHING")
            row.setdefault("stop_price", row.get("managed_stop") or row.get("original_stop"))
            row.setdefault("target_price", row.get("original_target"))
            writer(row)
        except Exception:
            # Runtime recovery is intentionally non-authoritative and must not
            # interrupt the canonical operational transaction.
            pass

    def _open_metrics(self) -> Dict[str, Any]:
        rows = self.positions(status="OPEN")
        realized = sum(float(row.get("net_pnl") or 0) for row in self.positions(status="CLOSED"))
        settled_equity = self.equity + realized
        notional = sum(float(row["notional"]) for row in rows)
        reserved = sum(float(row["reserved_cost"]) for row in rows)
        managed_risk = DEFAULT_CURRENT_MANAGED_RISK_AUTHORITY.portfolio(rows)
        return {
            "rows": rows,
            "settled_equity": settled_equity,
            "free_cash": max(0.0, settled_equity - notional - reserved),
            "intraday_used": sum(float(row["notional"]) + float(row["reserved_cost"]) for row in rows if row["mode"] == "intraday"),
            # Admission deliberately remains on immutable initial risk. Managed
            # risk is analytics-only and cannot release automatic capacity.
            "open_risk": managed_risk["initial_open_risk"],
            "current_managed_risk": managed_risk["current_managed_risk"],
            "secured_profit": managed_risk["secured_profit"],
            "managed_risk": managed_risk,
        }

    def _admission_block(self, at: datetime, open_rows: List[Dict[str, Any]]) -> str | None:
        if len(open_rows) >= 10:
            return "MAXIMUM OPEN POSITIONS — NO TRADE"
        try:
            if self.repository is not None:
                if self.repository.operator_stop():
                    return "OPERATOR KILL SWITCH — NO TRADE"
            else:
                exists = self.store.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='production_risk_state'"
                ).fetchone()
                if exists:
                    authority = self.store.conn.execute(
                        "SELECT operator_stop FROM production_risk_state WHERE singleton_id=1"
                    ).fetchone()
                    if authority and bool(authority[0]):
                        return "OPERATOR KILL SWITCH — NO TRADE"
        except Exception:
            return "RISK AUTHORITY UNAVAILABLE — NO TRADE"
        closed = self.positions(status="CLOSED")
        local_date = self.session.local(at).date()
        today_net = 0.0
        total_realized = 0.0
        for row in closed:
            pnl = float(row.get("net_pnl") or 0)
            total_realized += pnl
            try:
                closed_at = datetime.fromisoformat(str(row.get("closed_at") or "").replace("Z", "+00:00"))
                if self.session.local(closed_at).date() == local_date:
                    today_net += pnl
            except Exception:
                pass
        if today_net <= -(self.equity * 0.02):
            return "DAILY LOSS SUSPENSION — NO TRADE"
        open_mtm = sum(float(row.get("net_pnl") or 0) for row in open_rows)
        if total_realized + open_mtm <= -(self.equity * 0.08):
            return "DRAWDOWN SUSPENSION — NO TRADE"
        return None

    def admit(self, candidate: Dict[str, Any], quote: Any, *, at: datetime | None = None) -> Dict[str, Any]:
        """Admit one Final decision without holding a DB transaction over I/O.

        v103 architecture rule: validation, quote checks and Research writes run
        outside the operational admission transaction.  Only the shared-capital
        duplicate/risk/sizing/insert section is serialized.  This prevents a
        governance write (or any future provider call) from leaving PostgreSQL
        idle-in-transaction while the admission lock is held.
        """
        at = self.session.local(at)
        row = dict(candidate or {})
        symbol = str(row.get("symbol") or row.get("stock") or "").upper().strip()
        mode = str(row.get("mode") or "").lower().strip()
        side = str(row.get("side") or row.get("direction") or "").upper().strip()
        signal_id = str(row.get("signal_id") or row.get("source_signal_id") or "").strip()
        entry = _float(row.get("entry", row.get("planned_entry")))
        target = _float(row.get("target", row.get("t1")))
        stop = _float(row.get("sl", row.get("stop")))
        q = self._quote(quote)
        price = _float(q.get("ltp", q.get("price")))
        if mode not in {"intraday", "delivery"} or side not in {"LONG", "SHORT"} or not symbol or not signal_id:
            return self._research(row, "INVALID FINAL — IDENTITY OR MODE", at, price)
        try:
            venue = self.costs.venue_identity(
                exchange=str(row.get("exchange") or ""),
                bse_group=str(row.get("bse_group") or "").strip().upper() or None,
            )
        except ValueError as exc:
            return self._research(row, f"VENUE COST IDENTITY BLOCKED — {exc}", at, price)
        row["exchange"] = venue["exchange"]
        row["bse_group"] = venue["bse_group"]
        # Admission is a capital-owning boundary, so it re-proves the canonical
        # Final eligibility contract itself.  The bridge performs the same check
        # as a fast pre-filter, but no direct caller may bypass final promotion,
        # evidence readiness, desk, fundamental, freshness or capital-admission
        # invariants merely by reaching ModelPortfolioService directly.
        eligibility = DEFAULT_CANDIDATE_ELIGIBILITY_AUTHORITY.evaluate(row)
        if eligibility.get("eligible") is not True:
            blockers = ", ".join(eligibility.get("blockers") or ["FINAL_ELIGIBILITY_UNPROVEN"])
            return self._research(row, f"FINAL ELIGIBILITY BLOCKED — {blockers}", at, price)
        row["model_paper_eligibility"] = eligibility
        authority = str(row.get("authority") or row.get("selection_authority") or "").upper()
        if authority and authority not in {"PRODUCTION_FINAL", "PRODUCTION"}:
            return self._research(row, "NON-PRODUCTION AUTHORITY — NO TRADE", at, price)
        if mode == "delivery" and side == "SHORT":
            return self._research(row, "UNSUPPORTED DELIVERY SHORT — NO TRADE", at, price)
        phase = self.session.at(at)
        if mode == "intraday" and not phase["new_entry_allowed"]:
            return self._research(row, "LATE SIGNAL — NO TRADE", at, price)
        if not self._quote_valid(q):
            return self._research(row, "UNVERIFIED QUOTE — NO TRADE", at, price)
        if not all(value is not None and value > 0 for value in (entry, target, stop)):
            return self._research(row, "INCOMPLETE TRADE MAP — NO TRADE", at, price)
        if side == "LONG" and not (stop < entry < target):
            return self._research(row, "INVALID LONG MAP — NO TRADE", at, price)
        if side == "SHORT" and not (target < entry < stop):
            return self._research(row, "INVALID SHORT MAP — NO TRADE", at, price)
        crossed = price >= entry if side == "LONG" else price <= entry
        chase_pct = abs(price - entry) / entry * 100.0
        if not crossed:
            return self._research(row, "WAITING FOR ENTRY", at, price)
        if chase_pct > float(row.get("max_chase_pct") or 0.75):
            return self._research(row, "DO NOT CHASE — AVOID FOMO", at, price)
        position_id = hashlib.sha256(f"{signal_id}|{symbol}|{mode}".encode()).hexdigest()[:28]
        now = _utc(at)
        disposition_after_guard = None
        existing_after_guard = None
        sizing = None
        try:
            guard = getattr(self.repository, "admission_guard", None) if self.repository is not None else None
            context = guard() if callable(guard) else self.store.write_lock
            with context:
                existing_signal = (
                    self.repository.find_by_signal(signal_id) if self.repository is not None else
                    self.store.conn.execute(
                        "SELECT position_id,status,mode FROM model_portfolio_positions WHERE source_signal_id=? LIMIT 1",
                        (signal_id,),
                    ).fetchone()
                )
                if existing_signal:
                    existing_after_guard = dict(existing_signal)
                else:
                    already = (
                        self.repository.find_open_by_symbol(symbol) if self.repository is not None else
                        self.store.conn.execute(
                            "SELECT position_id,mode FROM model_portfolio_positions WHERE status='OPEN' AND symbol=? LIMIT 1",
                            (symbol,),
                        ).fetchone()
                    )
                    if already:
                        disposition_after_guard = "DUPLICATE — DELIVERY ALREADY OPEN" if str(already["mode"]) == "delivery" else "DUPLICATE — INTRADAY TRADE ALREADY OPEN"
                    else:
                        metrics = self._open_metrics()
                        risk_block = self._admission_block(at, metrics["rows"])
                        if risk_block:
                            disposition_after_guard = risk_block
                        else:
                            symbol_used = sum(float(item["notional"]) for item in metrics["rows"] if item["symbol"] == symbol)
                            sector = str(row.get("sector") or row.get("sector_label") or "Unknown")
                            sector_used = 0.0
                            for item in metrics["rows"]:
                                item_payload = {}
                                try:
                                    item_payload = json.loads(item.get("payload_json") or "{}")
                                except Exception:
                                    pass
                                if str(item_payload.get("sector") or item_payload.get("sector_label") or "Unknown") == sector:
                                    sector_used += float(item["notional"])
                            signal_age = DEFAULT_SIGNAL_AGE_AUTHORITY.measure(
                                generated_at=row.get("generated_at") or row.get("decision_generated_at") or row.get("created_at"),
                                opened_at=now, at=at, mode=mode,
                                approved_policy=(row.get("approved_age_risk_policy") if isinstance(row.get("approved_age_risk_policy"), dict) else None),
                            )
                            sizing_kwargs = dict(
                                mode=mode, side=side, exchange=str(venue["exchange"]),
                                bse_group=venue["bse_group"], entry=price, stop=stop,
                                free_cash=metrics["free_cash"], intraday_used=metrics["intraday_used"],
                                symbol_used=symbol_used, sector_used=sector_used, open_risk=metrics["open_risk"],
                                avg_daily_value=_float(row.get("avg_daily_value")), equity=metrics["settled_equity"],
                                risk_scale=float(signal_age["age_risk_multiplier"]),
                                risk_policy_approved=str(signal_age.get("age_risk_state") or "").startswith("APPROVED_"),
                                risk_policy_version=(signal_age.get("age_risk_policy_version") if str(signal_age.get("age_risk_state") or "").startswith("APPROVED_") else None),
                                risk_ceiling_quantity=(
                                    int(row.get("risk_ceiling_quantity") or row.get("risk_quantity"))
                                    if (row.get("risk_ceiling_quantity") or row.get("risk_quantity")) is not None else None
                                ),
                            )
                            sizing = self.risk.size(**sizing_kwargs)
                            execution_model = self._execution_model_contract(
                                row, q, mode=mode, quantity=max(1, int(sizing.get("quantity") or 0)),
                            )
                            # Final allocation reserves the same frozen spread/slippage/impact
                            # assumptions that will later price Model Paper marks/settlement.
                            sizing = self.risk.size(**sizing_kwargs, execution_model=execution_model)
                            if sizing.get("quantity", 0) > 0:
                                refined = self._execution_model_contract(
                                    row, q, mode=mode, quantity=int(sizing["quantity"]),
                                )
                                if refined.get("contract_hash") != execution_model.get("contract_hash"):
                                    execution_model = refined
                                    sizing = self.risk.size(**sizing_kwargs, execution_model=execution_model)
                            if sizing["quantity"] < 1:
                                disposition_after_guard = "CAPITAL OR RISK LIMIT — NO TRADE"
                            else:
                                payload = dict(row, sizing=sizing, signal_age=signal_age, execution_model=execution_model, model_policy=self.POLICY_VERSION, broker_orders=False)
                                if self.repository is not None:
                                    inserted = self.repository.insert_position({
                                        "position_id": position_id, "source_signal_id": signal_id,
                                        "decision_id": row.get("decision_id"),
                                        "instrument_key": row.get("instrument_key") or row.get("provider_instrument_key"),
                                        "generated_at": row.get("generated_at") or row.get("decision_generated_at") or row.get("created_at"),
                                        "model_version": row.get("model_version"),
                                        "policy_version": row.get("policy_version") or self.POLICY_VERSION,
                                        "evidence_snapshot_id": row.get("evidence_snapshot_id") or row.get("canonical_snapshot_id"),
                                        "evidence_hash": row.get("evidence_hash") or row.get("evidence_snapshot_hash") or row.get("canonical_snapshot_hash"),
                                        "feature_manifest_hash": row.get("feature_manifest_hash"),
                                        "symbol": symbol, "exchange": venue["exchange"], "bse_group": venue["bse_group"],
                                        "mode": mode, "side": side,
                                        "status": "OPEN", "quantity": sizing["quantity"], "original_entry": entry,
                                        "original_target": target, "original_stop": stop, "managed_stop": stop,
                                        "entry_price": price, "last_price": price, "notional": sizing["notional"],
                                        "reserved_cost": sizing["cost_reserve"], "open_risk": sizing["risk_cash"],
                                        "current_managed_risk": sizing["risk_cash"], "secured_profit": 0.0,
                                        "managed_risk_state": "ORIGINAL_RISK",
                                        "high_watermark": price, "low_watermark": price, "hit_status": "NONE",
                                        "action": "ENTER", "opened_at": now, "updated_at": now,
                                        "last_market_observation_at": (DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY.observation_time(q) or at),
                                        "last_market_observation_sequence": DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY.observation_sequence(q),
                                        "gap_recovery_state": "OPENED",
                                        "execution_model_version": execution_model.get("execution_model_version"),
                                        "execution_model_contract_hash": execution_model.get("contract_hash"),
                                        "execution_calibration_state": execution_model.get("calibration_state"),
                                        "execution_calibration_snapshot_hash": execution_model.get("calibration_snapshot_hash"),
                                        "execution_model": execution_model,
                                        "cost_version": self.costs.schedule.version, "payload": payload,
                                    })
                                    if not inserted:
                                        disposition_after_guard = "DUPLICATE — SIGNAL ALREADY OBSERVED"
                                else:
                                    self.store.conn.execute(
                                        """INSERT INTO model_portfolio_positions(
                                           position_id,source_signal_id,symbol,exchange,bse_group,mode,side,status,quantity,
                                           original_entry,original_target,original_stop,managed_stop,entry_price,last_price,
                                           notional,reserved_cost,open_risk,current_managed_risk,secured_profit,managed_risk_state,high_watermark,low_watermark,hit_status,action,
                                           opened_at,updated_at,cost_version,execution_model_version,execution_model_contract_hash,execution_calibration_state,
                                           execution_calibration_snapshot_hash,execution_model_json,payload_json)
                                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                        (position_id, signal_id, symbol, venue["exchange"], venue["bse_group"], mode, side, "OPEN",
                                         sizing["quantity"], entry, target, stop, stop, price, price, sizing["notional"],
                                         sizing["cost_reserve"], sizing["risk_cash"], sizing["risk_cash"], 0.0, "ORIGINAL_RISK",
                                         price, price, "NONE", "ENTER",
                                         now, now, self.costs.schedule.version, execution_model.get("execution_model_version"),
                                         execution_model.get("contract_hash"), execution_model.get("calibration_state"), execution_model.get("calibration_snapshot_hash"),
                                         _json(execution_model), _json(payload)),
                                    )
                                    self.store.conn.commit()
        except Exception as exc:
            if "UNIQUE" in str(exc).upper() or "DUPLICATE" in str(exc).upper():
                return self._research(row, "DUPLICATE — SIGNAL ALREADY OBSERVED", at, price)
            raise
        # No Research/governance write occurs while the operational transaction
        # is held.  Resolve all non-admission outcomes only after it closed.
        if existing_after_guard:
            return {
                "state": "ALREADY_ADMITTED", "position_id": existing_after_guard.get("position_id"),
                "status": existing_after_guard.get("status"), "symbol": symbol,
                "mode": existing_after_guard.get("mode") or mode,
            }
        if disposition_after_guard:
            return self._research(row, disposition_after_guard, at, price)
        if not sizing:
            return self._research(row, "ADMISSION STATE UNAVAILABLE — NO TRADE", at, price)
        self._append_lifecycle_event("OPENED", {
            **row, "position_id": position_id, "source_signal_id": signal_id, "symbol": symbol, "mode": mode, "side": side,
        }, at=at, payload={"entry_price": price, "target": target, "stop": stop, "quantity": sizing["quantity"], "signal_age": signal_age})
        self._mirror_runtime_risk({
            "decision_id": position_id, "position_id": position_id, "source_signal_id": signal_id,
            "symbol": symbol, "mode": mode, "side": side, "state": "OPEN", "status": "OPEN",
            "last_price": price, "entry_price": price, "original_entry": entry,
            "managed_stop": stop, "original_stop": stop, "original_target": target,
            "quantity": sizing["quantity"], "updated_at": now,
        })
        return {"state": "OPENED", "position_id": position_id, "symbol": symbol, "mode": mode, "sizing": sizing}

    def _settle(self, row: Dict[str, Any], price: float, at: datetime, reason: str, *, unscorable: bool = False, pre_lifecycle_events: List[Dict[str, Any]] | None = None, observed_price: float | None = None) -> Dict[str, Any]:
        frozen_execution_model = self._execution_model_from_row(row)
        report = self.costs.round_trip(
            row["mode"], row["side"], row["entry_price"], price, row["quantity"],
            exchange=str(row.get("exchange") or ""),
            bse_group=str(row.get("bse_group") or "").strip().upper() or None,
            traded_on=at.date(), execution_model=frozen_execution_model or None,
        )
        taxonomy = DEFAULT_OUTCOME_ACCURACY_TAXONOMY
        economic = taxonomy.economic_from_pnl(report["net_pnl"], unscorable=unscorable)
        signal = taxonomy.signal_from_exit_reason(reason, unscorable=unscorable)
        now = _utc(at)
        observed = float(observed_price if observed_price is not None else price)
        final_high = max(float(row.get("high_watermark") or row.get("entry_price") or observed), observed)
        final_low = min(float(row.get("low_watermark") or row.get("entry_price") or observed), observed)
        settled_payload = {
            "exit_price": price, "exit_reason": reason, "gross_pnl": report["gross_pnl"],
            "total_cost": report["costs"]["total"], "net_pnl": report["net_pnl"],
            "economic_outcome": economic, "signal_outcome": signal,
            "outcome_taxonomy_authority": taxonomy.authority,
            "outcome_taxonomy_version": taxonomy.authority_version,
            "execution_model_version": report.get("execution_model_version"),
            "execution_model_contract_hash": report.get("execution_model_contract_hash"),
            "execution_calibration_state": report.get("execution_calibration_state"),
            "execution_calibration_snapshot_hash": report.get("execution_calibration_snapshot_hash"),
            "material_transition": True,
        }
        settled_event = self._lifecycle_event_record("SETTLED", row, at=at, payload=settled_payload)
        lifecycle_events = [event for event in (pre_lifecycle_events or []) if event]
        if settled_event:
            lifecycle_events.append(settled_event)
        if self.repository is not None:
            fields = {
                "status": "CLOSED", "last_price": price, "exit_price": price,
                "high_watermark": final_high, "low_watermark": final_low,
                "gross_pnl": report["gross_pnl"], "total_cost": report["costs"]["total"],
                "net_pnl": report["net_pnl"], "hit_status": reason, "action": "CLOSED",
                "current_managed_risk": 0.0, "managed_risk_state": "CLOSED",
                "exit_reason": reason, "economic_outcome": economic, "signal_outcome": signal,
                "closed_at": now, "updated_at": now,
            }
            if row.get("last_market_observation_at") is not None:
                fields["last_market_observation_at"] = row.get("last_market_observation_at")
            if row.get("last_market_observation_sequence") is not None:
                fields["last_market_observation_sequence"] = row.get("last_market_observation_sequence")
            if row.get("gap_recovery_state") is not None:
                fields["gap_recovery_state"] = row.get("gap_recovery_state")
            atomic_update = getattr(self.repository, "update_position_with_lifecycle", None)
            if callable(atomic_update):
                atomic_update(row["position_id"], fields, lifecycle_events, expected_row_version=int(row.get("row_version") or 1))
            else:
                self.repository.update_position(row["position_id"], fields, expected_row_version=int(row.get("row_version") or 1))
        else:
            self.store.conn.execute(
                """UPDATE model_portfolio_positions SET status='CLOSED',last_price=?,exit_price=?,high_watermark=?,low_watermark=?,
                   gross_pnl=?,total_cost=?,net_pnl=?,hit_status=?,action='CLOSED',current_managed_risk=0,managed_risk_state='CLOSED',exit_reason=?,
                   economic_outcome=?,signal_outcome=?,closed_at=?,updated_at=? WHERE position_id=?""",
                (price, price, final_high, final_low, report["gross_pnl"], report["costs"]["total"], report["net_pnl"],
                 reason, reason, economic, signal, now, now, row["position_id"]),
            )
        settlement = {
            **dict(row),
            "status": "CLOSED", "last_price": price, "exit_price": price,
            "high_watermark": final_high, "low_watermark": final_low,
            "gross_pnl": report["gross_pnl"], "total_cost": report["costs"]["total"],
            "net_pnl": report["net_pnl"], "hit_status": reason, "action": "CLOSED",
            "current_managed_risk": 0.0, "managed_risk_state": "CLOSED",
            "exit_reason": reason, "economic_outcome": economic, "signal_outcome": signal,
            "outcome_taxonomy_authority": taxonomy.authority,
            "outcome_taxonomy_version": taxonomy.authority_version,
            "closed_at": now, "updated_at": now,
        }
        for event in lifecycle_events:
            self._append_lifecycle_record(event)
        # Canonical lineage is a post-commit projection.  It is intentionally
        # outside the model-paper position transaction so a lineage failure can
        # never roll back or strand risk authority state.
        if self.settlement_sink is not None:
            try:
                self.settlement_sink.record(settlement)
            except Exception:
                # The transactional outbox remains the durable recovery source;
                # the next reconciliation cycle may replay this projection.
                pass
        if self.repository is not None:
            try:
                from core.level5_learning_loop_service import Level5LearningLoopService
                Level5LearningLoopService(
                    self.repository,
                    getattr(self.store, "production_model_governance_repository", None),
                ).checkpoint()
            except Exception:
                # Learning evidence is append-only and proposal-only. A research
                # plane failure must never roll back or block portfolio settlement.
                pass
        return {
            "symbol": row["symbol"], "status": "CLOSED", "exit_reason": reason,
            "economic_outcome": economic, "signal_outcome": signal, "net_pnl": report["net_pnl"],
            "outcome_taxonomy_version": taxonomy.authority_version,
        }

    def mark_quotes(self, quotes: Dict[str, Any], *, at: datetime | None = None, mode: str | None = None, thesis_evidence: Dict[str, Any] | None = None, gap_bars: Dict[str, List[Dict[str, Any]]] | None = None) -> Dict[str, Any]:
        """Advance only the requested desk's open Model Paper positions.

        Installed runtime always supplies ``mode`` from DeskPositionLifecycleAuthority.
        This prevents the Intraday and Delivery lifecycle workers from both
        touching the same positions. The all-desk default is retained only for
        isolated compatibility tests/tools.
        """
        at = self.session.local(at)
        phase = self.session.at(at)
        updates: List[Dict[str, Any]] = []
        canonical_mode = str(mode or "").lower().strip() or None
        lock_context = nullcontext() if self.repository is not None else self.store.write_lock
        with lock_context:
            if self.repository is not None:
                open_rows = self.repository.list_open_ordered(canonical_mode)
            else:
                if canonical_mode:
                    open_rows = [dict(item) for item in self.store.conn.execute(
                        "SELECT * FROM model_portfolio_positions WHERE status='OPEN' AND mode=? ORDER BY opened_at",
                        (canonical_mode,),
                    ).fetchall()]
                else:
                    open_rows = [dict(item) for item in self.store.conn.execute(
                        "SELECT * FROM model_portfolio_positions WHERE status='OPEN' ORDER BY opened_at"
                    ).fetchall()]
            for db_row in open_rows:
                row = dict(db_row)
                q = quotes.get(row["symbol"]) or quotes.get(str(row["symbol"]).upper())
                if not self._quote_valid(q):
                    if row["mode"] == "intraday" and phase.get("mandatory_flat"):
                        # The simulated book must be flat by the governed Intraday
                        # mandatory-flat authority even when the provider has failed.  Use only the last already-recorded
                        # verified mark (or the verified admission fill when no
                        # later mark exists), label the result unscorable and
                        # never present it as a confirmed market execution.
                        fallback = float(row.get("last_price") or row.get("entry_price") or 0.0)
                        if fallback > 0:
                            updates.append(self._settle(row, fallback, at, "TIME_EXIT_DATA_FAILURE", unscorable=True))
                        else:
                            now = _utc(at)
                            if self.repository is not None:
                                self.repository.update_position(row["position_id"], {
                                    "status": "CLOSED", "data_failure": True,
                                    "current_managed_risk": 0.0, "managed_risk_state": "CLOSED",
                                    "hit_status": "TIME_EXIT_UNPRICED", "action": "CLOSED — UNSCORABLE",
                                    "exit_reason": "TIME_EXIT_UNPRICED", "economic_outcome": "UNSCORABLE",
                                    "signal_outcome": "UNSCORABLE", "closed_at": now, "updated_at": now,
                                }, expected_row_version=int(row.get("row_version") or 1))
                            else:
                                self.store.conn.execute(
                                    """UPDATE model_portfolio_positions SET status='CLOSED',data_failure=1,current_managed_risk=0,managed_risk_state='CLOSED',
                                       hit_status='TIME_EXIT_UNPRICED',action='CLOSED — UNSCORABLE',
                                       exit_reason='TIME_EXIT_UNPRICED',economic_outcome='UNSCORABLE',
                                       signal_outcome='UNSCORABLE',closed_at=?,updated_at=? WHERE position_id=?""",
                                    (now, now, row["position_id"]),
                                )
                            updates.append({"symbol": row["symbol"], "status": "CLOSED", "exit_reason": "TIME_EXIT_UNPRICED", "signal_outcome": "UNSCORABLE"})
                    elif row["mode"] == "intraday" and phase["mandatory_exit"]:
                        now = _utc(at)
                        if self.repository is not None:
                            self.repository.update_position(row["position_id"], {
                                "data_failure": True, "action": f"EXIT PENDING VERIFIED PRICE — FLAT BY {IntradaySessionPolicy.mandatory_flat_label()}",
                                "updated_at": now,
                            }, expected_row_version=int(row.get("row_version") or 1))
                        else:
                            self.store.conn.execute(
                                """UPDATE model_portfolio_positions SET data_failure=1,
                                   action=?,updated_at=? WHERE position_id=?""",
                                (f"EXIT PENDING VERIFIED PRICE — FLAT BY {IntradaySessionPolicy.mandatory_flat_label()}", now, row["position_id"]),
                            )
                        updates.append({"symbol": row["symbol"], "status": "DATA_FAILURE", "action": f"EXIT PENDING VERIFIED PRICE — FLAT BY {IntradaySessionPolicy.mandatory_flat_label()}"})
                    continue
                qrow = self._quote(q)
                price = float(qrow.get("ltp", qrow.get("price")))
                symbol_key = str(row.get("symbol") or "").upper()
                evidence_packet = dict((thesis_evidence or {}).get(symbol_key) or {})
                recovery = DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY.evaluate(
                    row, qrow, (gap_bars or {}).get(symbol_key) or []
                ) if DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY.needs_recovery(row, qrow) else {
                    "state": "NO_RECOVERY_REQUIRED", "recovery_required": False, "allow_current_quote": True,
                    "authority": DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY.authority,
                    "authority_version": DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY.authority_version,
                }
                row["last_market_observation_at"] = (DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY.observation_time(qrow) or at)
                row["last_market_observation_sequence"] = DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY.observation_sequence(qrow)
                row["gap_recovery_state"] = str(recovery.get("state") or "NO_RECOVERY_REQUIRED")
                if recovery.get("recovery_required"):
                    if recovery.get("managed_stop") is not None:
                        row["managed_stop"] = float(recovery.get("managed_stop"))
                    if recovery.get("high_watermark") is not None:
                        row["high_watermark"] = float(recovery.get("high_watermark"))
                    if recovery.get("low_watermark") is not None:
                        row["low_watermark"] = float(recovery.get("low_watermark"))
                    recovery_event = self._lifecycle_event_record("REASSESSED", row, at=at, payload={
                        "price": price, "market_gap_recovery": recovery,
                        "material_transition": True,
                    })
                    if recovery.get("exit_required"):
                        exit_price = float(recovery.get("exit_price") or price)
                        exit_reason = str(recovery.get("exit_reason") or "MARKET_DATA_GAP_AMBIGUOUS_EXIT")
                        if exit_reason == "MARKET_DATA_GAP_TARGET_HIT":
                            exit_reason = "TARGET_HIT"
                        elif exit_reason == "MARKET_DATA_GAP_STOP_HIT":
                            exit_reason = "STOP_HIT"
                        elif exit_reason == "MARKET_DATA_GAP_MANAGED_STOP_HIT":
                            exit_reason = "EXIT_INVALIDATED"
                        updates.append(self._settle(
                            row, exit_price, at, exit_reason,
                            unscorable=bool(recovery.get("unscorable")),
                            pre_lifecycle_events=[event for event in [recovery_event] if event],
                            observed_price=price,
                        ))
                        continue
                    if recovery.get("state") == "RECOVERED_NO_TOUCH":
                        row["managed_stop"] = float(recovery.get("managed_stop") or row.get("managed_stop"))
                        row["high_watermark"] = float(recovery.get("high_watermark") or row.get("high_watermark") or price)
                        row["low_watermark"] = float(recovery.get("low_watermark") or row.get("low_watermark") or price)
                lifecycle = DEFAULT_MODEL_PAPER_LIFECYCLE_AUTHORITY.evaluate(
                    row, qrow, phase, at=at, thesis_evidence=evidence_packet
                )
                reassessment = lifecycle.get("thesis_reassessment") or {}
                reassessed_event = self._lifecycle_event_record("REASSESSED", row, at=at, payload={
                    "price": price, "thesis_reassessment": reassessment,
                    "material_transition": str(reassessment.get("state") or "VALID").upper() != "VALID",
                })
                if lifecycle["operation"] == "EXIT":
                    updates.append(self._settle(
                        row, float(lifecycle["exit_price"]), at, str(lifecycle["exit_reason"]),
                        unscorable=bool(row["data_failure"]) if str(lifecycle["exit_reason"]).startswith("TIME_EXIT") else False,
                        pre_lifecycle_events=[event for event in [reassessed_event] if event],
                        observed_price=price,
                    ))
                    continue
                long = row["side"] == "LONG"
                high = float(lifecycle["high_watermark"])
                low = float(lifecycle["low_watermark"])
                managed = float(lifecycle["managed_stop"])
                action = str(lifecycle["action"])
                hit = str(lifecycle["hit_status"])
                gross = (price - row["entry_price"]) * row["quantity"] if long else (row["entry_price"] - price) * row["quantity"]
                mark_cost = self.costs.round_trip(
                    row["mode"], row["side"], row["entry_price"], price, row["quantity"],
                    exchange=str(row.get("exchange") or ""),
                    bse_group=str(row.get("bse_group") or "").strip().upper() or None,
                    traded_on=at.date(), execution_model=self._execution_model_from_row(row) or None,
                )
                now = _utc(at)
                managed_event = self._lifecycle_event_record("MANAGED", row, at=at, payload={
                    "price": price, "managed_stop": managed, "action": action, "hit_status": hit,
                    "managed_risk": lifecycle.get("managed_risk") or {},
                    "thesis_reassessment": reassessment,
                    "material_transition": (
                        str(action) != str(row.get("action") or "")
                        or abs(float(managed) - float(row.get("managed_stop") or managed)) > 1e-9
                        or str(reassessment.get("state") or "VALID").upper() != "VALID"
                    ),
                })
                lifecycle_events = [event for event in [reassessed_event, managed_event] if event]
                managed_risk = dict(lifecycle.get("managed_risk") or {})
                if not managed_risk:
                    managed_risk = DEFAULT_CURRENT_MANAGED_RISK_AUTHORITY.require_non_widening({
                        **row, "managed_stop": managed,
                    })
                fields = {
                    "last_price": price, "managed_stop": managed,
                    "current_managed_risk": managed_risk["current_managed_risk"],
                    "secured_profit": managed_risk["secured_profit"],
                    "managed_risk_state": managed_risk["state"],
                    "high_watermark": high,
                    "low_watermark": low, "gross_pnl": round(gross, 2),
                    "total_cost": mark_cost["costs"]["total"], "net_pnl": mark_cost["net_pnl"],
                    "hit_status": hit, "action": action, "updated_at": now,
                    "last_market_observation_at": (DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY.observation_time(qrow) or at),
                    "last_market_observation_sequence": DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY.observation_sequence(qrow),
                    "gap_recovery_state": str(recovery.get("state") or "NO_RECOVERY_REQUIRED"),
                }
                if self.repository is not None:
                    atomic_update = getattr(self.repository, "update_position_with_lifecycle", None)
                    if callable(atomic_update):
                        atomic_update(row["position_id"], fields, lifecycle_events, expected_row_version=int(row.get("row_version") or 1))
                    else:
                        self.repository.update_position(row["position_id"], fields, expected_row_version=int(row.get("row_version") or 1))
                else:
                    self.store.conn.execute(
                        """UPDATE model_portfolio_positions SET last_price=?,managed_stop=?,current_managed_risk=?,secured_profit=?,managed_risk_state=?,high_watermark=?,
                           low_watermark=?,gross_pnl=?,total_cost=?,net_pnl=?,hit_status=?,action=?,updated_at=?
                           WHERE position_id=?""",
                        (price, managed, managed_risk["current_managed_risk"], managed_risk["secured_profit"], managed_risk["state"], high, low, round(gross, 2), mark_cost["costs"]["total"], mark_cost["net_pnl"], hit, action, now, row["position_id"]),
                    )
                for event in lifecycle_events:
                    self._append_lifecycle_record(event)
                updates.append({"symbol": row["symbol"], "status": "OPEN", "last_price": price, "managed_stop": managed, "action": action})
            if self.repository is None:
                self.store.conn.commit()
        for update in updates:
            try:
                latest = (self.repository.latest_by_symbol(str(update.get("symbol") or "").upper())
                          if self.repository is not None else
                          self.store.conn.execute(
                              "SELECT * FROM model_portfolio_positions WHERE symbol=? ORDER BY updated_at DESC LIMIT 1",
                              (str(update.get("symbol") or "").upper(),),
                          ).fetchone())
                if latest:
                    self._mirror_runtime_risk(dict(latest))
            except Exception:
                pass
        return {
            "ok": True, "phase": phase, "updated": updates, "broker_orders": False, "mode": canonical_mode or "all",
            "lifecycle_authority": DEFAULT_MODEL_PAPER_LIFECYCLE_AUTHORITY.authority,
            "lifecycle_authority_version": DEFAULT_MODEL_PAPER_LIFECYCLE_AUTHORITY.authority_version,
        }

    def sync_final_signals(self, candidates: Iterable[Dict[str, Any]], quotes: Dict[str, Any], *, at: datetime | None = None) -> Dict[str, Any]:
        results = []
        for candidate in candidates or []:
            symbol = str(candidate.get("symbol") or candidate.get("stock") or "").upper()
            results.append(self.admit(candidate, quotes.get(symbol), at=at))
        return {"ok": True, "observed": len(results), "results": results, "authority": "production Final only"}

    def positions(self, status: str | None = None) -> List[Dict[str, Any]]:
        if self.repository is not None:
            return self.repository.list_positions(status)
        if status:
            rows = self.store.conn.execute(
                "SELECT * FROM model_portfolio_positions WHERE status=? ORDER BY opened_at DESC", (status,)
            ).fetchall()
        else:
            rows = self.store.conn.execute(
                "SELECT * FROM model_portfolio_positions ORDER BY opened_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def open_positions(self) -> List[Dict[str, Any]]:
        return self.positions(status="OPEN")

    def research_rows(self, limit: int = 100) -> List[Dict[str, Any]]:
        if self.repository is not None:
            return self.repository.research_rows(limit)
        return [
            dict(row)
            for row in self.store.conn.execute(
                "SELECT * FROM model_portfolio_research ORDER BY occurred_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        ]

    def capital_summary(self) -> Dict[str, Any]:
        open_rows = self.open_positions()
        closed = sorted(
            self.positions(status="CLOSED"),
            key=lambda row: str(row.get("closed_at") or row.get("updated_at") or ""),
        )
        realized = sum(float(row["net_pnl"] or 0) for row in closed)
        open_mtm = sum(float(row["net_pnl"] or 0) for row in open_rows)
        intraday_used = sum(float(row["notional"]) + float(row["reserved_cost"]) for row in open_rows if row["mode"] == "intraday")
        delivery_used = sum(float(row["notional"]) + float(row["reserved_cost"]) for row in open_rows if row["mode"] == "delivery")
        deployed = intraday_used + delivery_used
        equity = self.equity + realized
        curve = self.equity
        peak = self.equity
        for row in closed:
            curve += float(row.get("net_pnl") or 0)
            peak = max(peak, curve)
        current_equity = equity + open_mtm
        managed_risk = DEFAULT_CURRENT_MANAGED_RISK_AUTHORITY.portfolio(open_rows)
        return {
            "initial_equity": self.equity,
            "equity": round(current_equity, 2),
            "free_cash": round(max(0.0, equity - deployed), 2),
            "deployed": round(deployed, 2),
            "intraday_used": round(intraday_used, 2),
            "intraday_cap": self.intraday_cap,
            "delivery_used": round(delivery_used, 2),
            "open_risk": managed_risk["initial_open_risk"],
            "initial_open_risk": managed_risk["initial_open_risk"],
            "current_managed_risk": managed_risk["current_managed_risk"],
            "secured_profit": managed_risk["secured_profit"],
            "released_risk": managed_risk["released_risk"],
            "portfolio_heat_admission_measure": DEFAULT_CURRENT_MANAGED_RISK_AUTHORITY.admission_heat_measure,
            "managed_risk_usage": DEFAULT_CURRENT_MANAGED_RISK_AUTHORITY.managed_risk_usage,
            "managed_risk_authority_version": DEFAULT_CURRENT_MANAGED_RISK_AUTHORITY.authority_version,
            "realized_net_pnl": round(realized, 2),
            "open_mtm_net_pnl": round(open_mtm, 2),
            "drawdown": round(min(0.0, current_equity - peak), 2),
            "within_equity": deployed <= equity + 1e-9,
            "intraday_within_cap": intraday_used <= self.intraday_cap + 1e-9,
        }
