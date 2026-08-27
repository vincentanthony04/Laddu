"""Durable observation ledger for promoted, WATCH and blocked candidates.

Learning only from promoted signals creates selection bias.  This service stores
the same point-in-time candidate snapshot for every final decision and marks
shadow target/stop outcomes from identity-verified quotes.  It is observation
only: no row can alter scores, thresholds, direction or capital authority.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from core.production_mode_policy import is_production_mode, normalise_mode

COUNTERFACTUAL_VERSION = "counterfactual-observation-ledger-1.0.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _num(value: Any):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class CounterfactualLearningService:
    def __init__(self, store: Any):
        self.store = store
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS candidate_counterfactuals (
              observation_id TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              mode TEXT NOT NULL,
              side TEXT,
              final_status TEXT,
              final_decision TEXT,
              score REAL,
              entry REAL,
              stop REAL,
              target REAL,
              observed_at TEXT NOT NULL,
              outcome_status TEXT NOT NULL DEFAULT 'PENDING',
              outcome_price REAL,
              outcome_at TEXT,
              feature_json TEXT NOT NULL,
              policy_version TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_counterfactual_pending_symbol
              ON candidate_counterfactuals(outcome_status, symbol, observed_at);
            """)
            self.store.conn.commit()

    @staticmethod
    def _id(candidate: Mapping[str, Any]) -> str:
        material = "|".join(str(candidate.get(key) or "") for key in (
            "symbol", "mode", "side", "decision_as_of", "last_ai_validation",
            "entry", "sl", "t1", "status", "ranking_version",
        ))
        return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:32]

    def record(self, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        mode = normalise_mode(candidate.get("mode"))
        symbol = str(candidate.get("symbol") or "").upper().strip()
        if not symbol or not is_production_mode(mode):
            return {"ok": False, "state": "SKIPPED_INVALID_IDENTITY"}
        entry = _num(candidate.get("entry") if candidate.get("entry") is not None else candidate.get("planned_entry"))
        stop = _num(candidate.get("sl") or candidate.get("stop") or candidate.get("planned_sl"))
        target = _num(candidate.get("t1") or candidate.get("target") or candidate.get("planned_t1"))
        observed_at = str(candidate.get("decision_as_of") or candidate.get("last_ai_validation") or _now())
        oid = self._id(candidate)
        feature_keys = (
            "rank_score", "score", "rank_readiness", "rank_scoring_state", "ranking_version",
            "calibrated_edge", "execution_quality", "event_risk_policy", "performance_drift_guard",
            "index_context", "market_structure", "sector", "spread_bps", "rr",
            "freshness_state", "candle_freshness_state", "quote_age_seconds", "candle_age_seconds",
            "promotion_blocked_by", "governed_edge_gates",
        )
        features = {key: candidate.get(key) for key in feature_keys if candidate.get(key) is not None}
        with self.store.write_lock:
            cursor = self.store.conn.execute(
                """INSERT OR IGNORE INTO candidate_counterfactuals(
                    observation_id,symbol,mode,side,final_status,final_decision,score,
                    entry,stop,target,observed_at,feature_json,policy_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    oid, symbol, mode, str(candidate.get("side") or "").upper(),
                    str(candidate.get("status") or ""), str(candidate.get("decision") or ""),
                    _num(candidate.get("rank_score") if candidate.get("rank_score") is not None else candidate.get("score")),
                    entry, stop, target, observed_at, json.dumps(features, sort_keys=True, default=str),
                    COUNTERFACTUAL_VERSION,
                ),
            )
            self.store.conn.commit()
        return {"ok": True, "state": "RECORDED" if int(cursor.rowcount or 0) else "DUPLICATE", "observation_id": oid}

    def mark(self, quotes: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
        clean = {
            str(symbol).upper(): _num(row.get("ltp"))
            for symbol, row in (quotes or {}).items()
            if isinstance(row, Mapping)
            and row.get("identity_verified") is True
            and row.get("stale") is not True
            and str(row.get("freshness_state") or "").lower() in {"live", "closed_market"}
        }
        clean = {symbol: price for symbol, price in clean.items() if price is not None and price > 0}
        if not clean:
            return {"ok": True, "updated": 0}
        marks = ",".join("?" for _ in clean)
        rows = self.store.conn.execute(
            f"SELECT * FROM candidate_counterfactuals WHERE outcome_status='PENDING' AND symbol IN ({marks})",
            tuple(clean.keys()),
        ).fetchall()
        updated = 0
        with self.store.write_lock:
            for raw in rows:
                row = dict(raw)
                price = clean.get(str(row.get("symbol") or "").upper())
                entry, stop, target = _num(row.get("entry")), _num(row.get("stop")), _num(row.get("target"))
                side = str(row.get("side") or "LONG").upper()
                if price is None or entry is None or stop is None or target is None:
                    continue
                target_hit = price <= target if side == "SHORT" else price >= target
                stop_hit = price >= stop if side == "SHORT" else price <= stop
                outcome = "TARGET" if target_hit and not stop_hit else "STOP" if stop_hit and not target_hit else None
                if not outcome:
                    continue
                self.store.conn.execute(
                    "UPDATE candidate_counterfactuals SET outcome_status=?,outcome_price=?,outcome_at=? WHERE observation_id=? AND outcome_status='PENDING'",
                    (outcome, price, _now(), row.get("observation_id")),
                )
                updated += 1
            if updated:
                self.store.conn.commit()
        return {"ok": True, "updated": updated}

    def summary(self) -> Dict[str, Any]:
        rows = self.store.conn.execute(
            "SELECT final_status,outcome_status,COUNT(*) n FROM candidate_counterfactuals GROUP BY final_status,outcome_status"
        ).fetchall()
        return {
            "ok": True,
            "version": COUNTERFACTUAL_VERSION,
            "groups": [dict(row) for row in rows],
            "policy": "observation only; includes promoted, WATCH and blocked candidates to reduce selection bias",
        }
