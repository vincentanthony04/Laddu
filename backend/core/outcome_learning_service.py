from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict


_FRESHNESS_ALIASES = {
    "fresh": "live", "current": "live", "verified": "live",
    "closed-market": "closed_market", "closed market": "closed_market",
    "lkg": "stale", "historical": "stale", "delayed": "stale",
    "loading": "pending", "warming": "pending",
    "error": "failed", "invalid": "failed",
}


def _canonical_freshness(value: Any) -> str | None:
    raw = str(value or "").strip().lower().replace("-", "_")
    if not raw:
        return None
    raw = _FRESHNESS_ALIASES.get(raw.replace("_", " "), _FRESHNESS_ALIASES.get(raw, raw))
    if raw in {"live", "closed_market", "stale", "pending", "failed", "partial", "unverified", "missing"}:
        return raw
    text = raw.replace("_", " ")
    if "closed market" in text:
        return "closed_market"
    if any(token in text for token in ("lkg", "stale", "historical", "delayed")):
        return "stale"
    if any(token in text for token in ("live", "verified", "fresh", "current")):
        return "live"
    if any(token in text for token in ("pending", "loading", "warming", "refreshing")):
        return "pending"
    if any(token in text for token in ("failed", "error", "invalid")):
        return "failed"
    if "unverified" in text:
        return "unverified"
    return None


def _decision_freshness(row: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    price = _canonical_freshness(
        payload.get("price_freshness_state")
        or payload.get("freshness_state")
        or row.get("price_freshness_state")
        or row.get("freshness_state")
        or payload.get("price_freshness")
        or row.get("price_freshness")
    )
    candle = _canonical_freshness(
        payload.get("candle_freshness_state")
        or payload.get("candle_state")
        or row.get("candle_freshness_state")
        or row.get("candle_state")
    )
    combined = price or candle or "unknown"
    return {
        "freshness_state": combined,
        "price_freshness_state": price or "unknown",
        "candle_freshness_state": candle or "unknown",
    }


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def attribute_outcome(result: str, pnl_points: Any, payload: Dict[str, Any]) -> str:
    result_text = str(result or "").upper()
    pnl = _finite(pnl_points)

    # A textual terminal result can still classify lifecycle semantics, but
    # missing economics are never manufactured as a flat zero observation.
    if result_text.startswith("SUCCESS"):
        return "successful_resolution"
    if "AMBIGUOUS" in result_text:
        return "evidence_ambiguous"
    if "EXPIRED" in result_text or "TIME" in result_text:
        return "time_stop_no_resolution"
    if "SL" in result_text:
        mfe = _finite(payload.get("mfe"))
        if mfe is None:
            return "stop_outcome_excursion_unavailable"
        return "stop_after_progress" if mfe > 0 else "initial_thesis_failed"

    conflicts = " ".join(str(value) for value in (payload.get("rank_conflicts") or []))
    if "false breakout" in conflicts.lower():
        return "false_breakout"
    if pnl is None:
        return "unscorable_missing_economics"
    if pnl > 0:
        return "successful_resolution"
    if pnl < 0:
        return "unclassified_loss"
    return "flat_or_unresolved"


def learning_features(row: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "score", "fundamental_score", "technical_score", "mtf_score",
        "market_structure", "volume_state", "institutional_stage",
        "institutional_score", "recent_volume_vs_base", "rsi", "adx",
        "sector", "pattern", "setup", "ranking_version", "rr", "room_to_target",
        "mtf_readiness", "regime", "spread_bps", "entry_map_state",
        "quote_age_seconds", "candle_age_seconds", "stale_guard",
        "identity_verified", "decision_as_of", "quote_source_time",
    )
    features = {key: payload.get(key, row.get(key)) for key in keys if payload.get(key, row.get(key)) is not None}
    # v65.26.33: persist scale-invariant outcome geometry. Raw price points are
    # not comparable between a low-priced and high-priced stock, so calibrated
    # edge and drift controls consume R-multiples whenever the immutable entry/
    # original-stop map is available. Existing rows remain auditable and are
    # treated as legacy/unscaled until repaired from their signal payload.
    entry = payload.get("entry", row.get("entry"))
    stop = payload.get("original_sl", payload.get("sl", row.get("sl")))
    pnl = row.get("pnl_points")
    entry_f = _finite(entry)
    stop_f = _finite(stop)
    pnl_f = _finite(pnl)
    if entry_f is not None and stop_f is not None and pnl_f is not None:
        risk_points = abs(entry_f - stop_f)
        if entry_f > 0:
            features["outcome_return_pct"] = round(pnl_f / entry_f * 100.0, 8)
        if risk_points > 0:
            features["initial_risk_points"] = round(risk_points, 8)
            features["outcome_r_multiple"] = round(pnl_f / risk_points, 8)
            features["outcome_scale"] = "initial_r_multiple"
    features.update(_decision_freshness(row, payload))
    return features



class OutcomeLearningService:
    MIN_SAMPLE = 30
    MIN_PROFIT_FACTOR = 1.15

    def __init__(self, store):
        self.store = store

    @staticmethod
    def _minutes(opened_at: Any, closed_at: Any):
        def parse(value):
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except Exception:
                return None
        opened, closed = parse(opened_at), parse(closed_at)
        if not opened or not closed or closed < opened:
            return None
        return round((closed - opened).total_seconds() / 60.0, 1)

    def backfill_closed_outcomes(self, limit: int = 5000) -> int:
        rows = self.store.conn.execute("""SELECT * FROM signal_ledger
            WHERE status IN ('SUCCESS','FAIL','EXPIRED','AMBIGUOUS')
            ORDER BY COALESCE(closed_at,last_update) DESC LIMIT ?""", (limit,)).fetchall()
        inserted = 0
        with self.store.write_lock:
            for raw in rows:
                row = dict(raw)
                try:
                    payload = json.loads(row.get("payload_json") or "{}")
                except Exception:
                    payload = {}
                result = row.get("result")
                pnl = row.get("pnl_points")
                cursor = self.store.conn.execute("""INSERT OR IGNORE INTO outcome_learning(
                    signal_id,symbol,mode,side,result,pnl_points,holding_minutes,attribution,
                    feature_json,proof_json,model_version,closed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    row.get("signal_id"), row.get("symbol"), row.get("mode"), row.get("side"), result, pnl,
                    self._minutes(row.get("opened_at"), row.get("closed_at") or row.get("last_update")),
                    attribute_outcome(result, pnl, payload), json.dumps(learning_features(row, payload)),
                    json.dumps({"source": "ledger_backfill"}), payload.get("ranking_version") or payload.get("model_version") or "rules-current",
                    row.get("closed_at") or row.get("last_update"),
                ))
                inserted += max(0, int(cursor.rowcount or 0))
            if inserted:
                self.store.conn.commit()
        return inserted

    def repair_freshness_features(self, limit: int = 5000) -> int:
        """Repair old learning rows from their immutable signal payload.

        Only missing/unknown freshness fields are filled. Historical outcomes are
        never reclassified and P&L/result values are never changed.
        """
        rows = self.store.conn.execute("""
            SELECT o.signal_id, o.feature_json, s.payload_json
            FROM outcome_learning o
            JOIN signal_ledger s ON s.signal_id=o.signal_id
            ORDER BY COALESCE(o.closed_at,o.created_at) DESC LIMIT ?
        """, (limit,)).fetchall()
        repaired = 0
        with self.store.write_lock:
            for raw in rows:
                row = dict(raw)
                try:
                    features = json.loads(row.get("feature_json") or "{}")
                    features = dict(features) if isinstance(features, dict) else {}
                except Exception:
                    features = {}
                if all(str(features.get(key) or "unknown").lower() not in {"", "unknown", "none"}
                       for key in ("freshness_state", "price_freshness_state", "candle_freshness_state")):
                    continue
                try:
                    payload = json.loads(row.get("payload_json") or "{}")
                    payload = dict(payload) if isinstance(payload, dict) else {}
                except Exception:
                    payload = {}
                derived = _decision_freshness({}, payload)
                changed = False
                for key, value in derived.items():
                    if str(features.get(key) or "unknown").lower() in {"", "unknown", "none"} and value != "unknown":
                        features[key] = value
                        changed = True
                if changed:
                    self.store.conn.execute("UPDATE outcome_learning SET feature_json=? WHERE signal_id=?",
                                            (json.dumps(features), row.get("signal_id")))
                    repaired += 1
            if repaired:
                self.store.conn.commit()
        return repaired

    def repair_normalized_outcome_features(self, limit: int = 5000) -> int:
        """Backfill scale-invariant R/return features from immutable ledger maps."""
        rows = self.store.conn.execute("""
            SELECT o.signal_id,o.pnl_points,o.feature_json,s.payload_json
            FROM outcome_learning o
            JOIN signal_ledger s ON s.signal_id=o.signal_id
            ORDER BY COALESCE(o.closed_at,o.created_at) DESC LIMIT ?
        """, (limit,)).fetchall()
        repaired = 0
        with self.store.write_lock:
            for raw in rows:
                row = dict(raw)
                try:
                    features = json.loads(row.get("feature_json") or "{}")
                    features = dict(features) if isinstance(features, dict) else {}
                except Exception:
                    features = {}
                if features.get("outcome_r_multiple") is not None and features.get("outcome_scale") == "initial_r_multiple":
                    continue
                try:
                    payload = json.loads(row.get("payload_json") or "{}")
                    payload = dict(payload) if isinstance(payload, dict) else {}
                except Exception:
                    payload = {}
                entry = payload.get("entry") or payload.get("planned_entry")
                stop = payload.get("original_sl") or payload.get("sl") or payload.get("planned_sl")
                entry_f = _finite(entry)
                stop_f = _finite(stop)
                pnl_f = _finite(row.get("pnl_points"))
                if entry_f is None or stop_f is None or pnl_f is None:
                    continue
                risk = abs(entry_f - stop_f)
                changed = False
                if entry_f > 0 and features.get("outcome_return_pct") is None:
                    features["outcome_return_pct"] = round(pnl_f / entry_f * 100.0, 8)
                    changed = True
                if risk > 0:
                    features["initial_risk_points"] = round(risk, 8)
                    features["outcome_r_multiple"] = round(pnl_f / risk, 8)
                    features["outcome_scale"] = "initial_r_multiple"
                    changed = True
                if changed:
                    self.store.conn.execute(
                        "UPDATE outcome_learning SET feature_json=? WHERE signal_id=?",
                        (json.dumps(features), row.get("signal_id")),
                    )
                    repaired += 1
            if repaired:
                self.store.conn.commit()
        return repaired

    def summary(self) -> Dict[str, Any]:
        backfilled = self.backfill_closed_outcomes()
        freshness_repaired = self.repair_freshness_features()
        normalized_repaired = self.repair_normalized_outcome_features()
        rows = self.store.outcome_learning_rows(limit=5000)
        grouped = defaultdict(list)
        for row in rows:
            grouped[(str(row.get("mode") or "unknown"), str(row.get("side") or "unknown"))].append(row)
        segments = []
        for (mode, side), items in sorted(grouped.items()):
            pnls = [value for item in items if (value := _finite(item.get("pnl_points"))) is not None]
            wins = [value for value in pnls if value > 0]
            losses = [value for value in pnls if value < 0]
            gross_win = sum(wins)
            gross_loss = abs(sum(losses))
            profit_factor = round(gross_win / gross_loss, 3) if gross_loss else (999.0 if gross_win else 0.0)
            normalized_r = []
            for item in items:
                try:
                    features = json.loads(item.get("feature_json") or "{}")
                    value = features.get("outcome_r_multiple") if isinstance(features, dict) else None
                    finite_value = _finite(value)
                    if finite_value is not None:
                        normalized_r.append(finite_value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            categories = Counter(str(item.get("attribution") or "unknown") for item in items)
            scorable_samples = len(pnls)
            sample_ready = scorable_samples >= self.MIN_SAMPLE
            candidate_ready = sample_ready and profit_factor >= self.MIN_PROFIT_FACTOR and bool(pnls) and (sum(pnls) / scorable_samples) > 0
            dominant = categories.most_common(1)[0] if categories else ("none", 0)
            recommendation = "Collect more closed trades"
            if sample_ready and dominant[0] not in ("successful_resolution", "none"):
                recommendation = f"Shadow-review {dominant[0].replace('_', ' ')} gate"
            elif candidate_ready:
                recommendation = "Eligible for walk-forward challenger evaluation"
            segments.append({
                "mode": mode,
                "side": side,
                "samples": len(items),
                "scorable_samples": scorable_samples,
                "excluded_missing_or_nonfinite_pnl": len(items) - scorable_samples,
                "win_rate": round(len(wins) * 100.0 / scorable_samples, 2) if scorable_samples else None,
                "average_pnl_points": round(sum(pnls) / scorable_samples, 3) if scorable_samples else None,
                "average_r_multiple": round(sum(normalized_r) / len(normalized_r), 4) if normalized_r else None,
                "scale_valid_samples": len(normalized_r),
                "profit_factor": profit_factor,
                "dominant_attribution": dominant[0],
                "dominant_count": dominant[1],
                "sample_gate": sample_ready,
                "challenger_gate": candidate_ready,
                "recommendation": recommendation,
                "production_change_allowed": False,
            })
        return {
            "ok": True,
            "closed_outcomes": len(rows),
            "backfilled_now": backfilled,
            "freshness_repaired_now": freshness_repaired,
            "normalized_outcomes_repaired_now": normalized_repaired,
            "segments": segments,
            "policy": {
                "learning_mode": "shadow_only",
                "minimum_samples": self.MIN_SAMPLE,
                "minimum_profit_factor": self.MIN_PROFIT_FACTOR,
                "walk_forward_required": True,
                "automatic_production_weight_changes": False,
            },
        }
