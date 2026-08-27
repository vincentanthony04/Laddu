from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Dict, Mapping

from .postgres import PostgresAuthority


class ProductionRiskRepository:
    """Operational PostgreSQL persistence for risk control and audit truth."""

    production_authority = True

    def __init__(self, operational: PostgresAuthority):
        self.operational = operational

    @staticmethod
    def _normalise(row: Mapping[str, Any] | None) -> Dict[str, Any]:
        if not row:
            return {}
        out = dict(row)
        if "reason" in out:
            out["operator_reason"] = out.get("reason")
        if "updated_by" in out:
            out["operator_actor"] = out.get("updated_by")
        for key, value in list(out.items()):
            if isinstance(value, datetime):
                out[key] = value.isoformat().replace("+00:00", "Z")
        return out

    def state_row(self) -> Dict[str, Any]:
        return self._normalise(self.operational.execute(
            "SELECT * FROM risk.control_state WHERE singleton_id=1", fetch="one"
        ))

    def set_operator_stop(self, enabled: bool, reason: str | None, actor: str) -> None:
        self.operational.execute(
            """UPDATE risk.control_state
                  SET operator_stop=%s,reason=%s,updated_by=%s,updated_at=clock_timestamp()
                WHERE singleton_id=1""",
            (bool(enabled), reason or None, actor),
        )

    def update_account_state(self, *, daily_pnl: float | None, equity: float | None, peak: float | None, actor: str, as_of: str) -> None:
        self.operational.execute(
            """UPDATE risk.control_state SET
                   external_daily_pnl=COALESCE(%s,external_daily_pnl),
                   external_equity=COALESCE(%s,external_equity),
                   equity_peak=COALESCE(%s,equity_peak),
                   account_as_of=%s,updated_by=%s,updated_at=clock_timestamp()
                WHERE singleton_id=1""",
            (daily_pnl, equity, peak, as_of, actor),
        )

    def model_paper_account_rows(self) -> list[Dict[str, Any]]:
        rows = self.operational.execute(
            """SELECT status,net_pnl,closed_at,updated_at
                 FROM trading.model_paper_positions
                ORDER BY COALESCE(closed_at,updated_at),position_id""",
            fetch="all",
        )
        return [self._normalise(row) for row in rows]

    def open_positions(self) -> list[Dict[str, Any]]:
        rows = self.operational.execute(
            "SELECT * FROM trading.model_paper_positions WHERE status='OPEN' ORDER BY opened_at",
            fetch="all",
        )
        out = []
        for raw in rows:
            row = self._normalise(raw)
            payload = row.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            row["payload"] = payload
            row["entry"] = row.get("entry_price")
            row["sl"] = row.get("managed_stop") or row.get("original_stop")
            row["sector"] = payload.get("sector") or payload.get("sector_label") or "Unknown"
            out.append(row)
        return out

    def record_admission(self, candidate: Mapping[str, Any], report: Mapping[str, Any]) -> None:
        basis = {
            "symbol": report.get("symbol"), "mode": report.get("mode"),
            "as_of": report.get("as_of"), "state": report.get("admission_state"),
            "reasons": report.get("hard_blocks"),
        }
        admission_id = hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()[:24]
        payload = json.dumps(dict(candidate), sort_keys=True, default=str)
        material = json.dumps(dict(report), sort_keys=True, default=str)
        with self.operational.transaction(statement_timeout_ms=4000) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO risk.candidate_admissions(
                           admission_id,occurred_at,symbol,mode,admission_state,reason_codes,input_snapshot,report)
                       VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
                       ON CONFLICT(admission_id) DO NOTHING""",
                    (
                        admission_id, report.get("as_of"), report.get("symbol"), report.get("mode"),
                        report.get("admission_state"), json.dumps(report.get("hard_blocks") or []), payload, material,
                    ),
                )
                cur.execute(
                    """INSERT INTO integration.transactional_outbox(
                           event_key,aggregate_type,aggregate_id,event_type,payload,occurred_at)
                       VALUES(%s,'risk_admission',%s,'RISK_ADMISSION_RECORDED',%s::jsonb,%s)
                       ON CONFLICT(event_key) DO NOTHING""",
                    ("risk:" + admission_id, admission_id, material, report.get("as_of")),
                )
