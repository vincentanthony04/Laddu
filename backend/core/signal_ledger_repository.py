"""Decisions/signals engine: decision writes, signal-ledger key/result
computation, candle-based signal proof evaluation, live-quote signal
refresh, and the selected/expiry/settlement read paths over signal_ledger.
Extracted verbatim from storage.py's Store class (v51 storage split,
cluster 3 -- most logic-dense cluster, touches signal ledger math). No
behavior change.

Constructed fresh per call (same pattern as ManualWatchRepository /
MarketDataRepository / ReferenceDataRepository), against the exact deps it
needs: self.conn (per-thread connection property) and self.event (for the
ledger-write observability log in save_decision -- system_health_repository
already owns event()/events(), so this repo calls back into Store for it
rather than duplicating that table's schema here).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from actionability import is_actionable_signal
from core.db_utils import to_float
from core.canonical_decision_repository import CanonicalDecisionRepository
from core.decision_write_dedup_filter import DecisionWriteDedupFilter
from core.outcome_learning_service import attribute_outcome, learning_features
from core.india_time import as_india
from core.position_lifecycle_service import PositionLifecycleService
from core.production_mode_policy import require_production_mode, is_production_mode, normalise_mode
from models import now_iso


def _desk_modes(mode):
    return (require_production_mode(mode),)


class SignalLedgerRepository:
    def __init__(self, connection, event_fn: Callable[..., None], write_lock=None):
        self.conn = connection
        self._event = event_fn
        # v60.5: Store.write_lock exists specifically for multi-statement write
        # sequences on the shared connection (see storage.py __init__ comment,
        # "decision ledger writes 4 statements + commit with no serialization").
        # This repo does exactly that in save_decision (INSERT decisions ->
        # SELECT+UPDATE signal_ledger -> INSERT/UPSERT signal_ledger -> commit)
        # and in refresh_open_signals_from_quotes (SELECT -> N UPDATEs -> commit),
        # but was never given the lock when it was split out of Store in v51.
        # Falls back to a private lock if not supplied so this repo is never
        # unsynchronized, but Store always passes its shared one.
        self._write_lock = write_lock or threading.Lock()
        self.conn.execute("""CREATE TABLE IF NOT EXISTS outcome_learning (
            signal_id TEXT PRIMARY KEY, symbol TEXT, mode TEXT, side TEXT, result TEXT,
            pnl_points REAL, holding_minutes REAL, attribution TEXT, feature_json TEXT,
            proof_json TEXT, model_version TEXT, closed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

    def save_decision(self, d: Dict[str, Any]) -> None:
        # v60.5: whole check-then-write sequence under write_lock -- the
        # duplicate-stale check (a SELECT) and the subsequent INSERT/UPDATE
        # sequence must be atomic together, or two threads can both pass the
        # duplicate check before either writes (classic check-then-act race).
        with self._write_lock:
            if DecisionWriteDedupFilter.reject_unsupported_mode(d):
                self._event("WARN", "decision_store", "Unsupported production mode suppressed", {"symbol": d.get("symbol"), "mode": d.get("mode")})
                return
            d = dict(d)
            d["mode"] = require_production_mode(d.get("mode"))
            # Stale scanner ticks are operational state, not new research evidence.
            if DecisionWriteDedupFilter.suppress_duplicate_stale(self.conn,d):
                self._event("DEBUG","decision_store","Duplicate stale decision suppressed",{"symbol":d.get("symbol"),"mode":d.get("mode")}); return
            self.conn.execute(
                "INSERT INTO decisions(decision_id,thesis_id,signal_id,symbol,exchange,mode,side,decision,status,score,payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (d.get("decision_id"), d.get("thesis_id"), d.get("signal_id"), d.get("symbol"), d.get("exchange"),
                 d.get("mode"), d.get("side"), d.get("decision"), d.get("status"), d.get("score"), json.dumps(d)),
            )
            self._update_signal_progress_from_decision(d)
            # v37.5: Phase 0 observability -- this is the ONLY gate that decides
            # whether a decision becomes a signal_ledger row. Previously this was
            # silent: if a promoted-looking decision never entered the ledger,
            # there was no way to tell whether the gate rejected it or whether
            # promotion never happened at all. Log both outcomes explicitly.
            is_signal = self._decision_is_signal(d)
            self._event(
                "INFO" if is_signal else "DEBUG", "signal_ledger",
                "Ledger write" if is_signal else "Ledger write skipped",
                {
                    "symbol": d.get("symbol"), "mode": d.get("mode"), "side": d.get("side"),
                    "decision": d.get("decision"), "status": d.get("status"),
                    "entry_present": self._f(d.get("entry") or d.get("ltp")) is not None,
                    "wrote_ledger": is_signal,
                },
            )
            if is_signal:
                self._upsert_signal_from_decision(d)
            self.conn.commit()

    def _f(self, v):
        return to_float(v)

    @staticmethod
    def _verified_lifecycle_price(evidence: Dict[str, Any]) -> bool:
        """Allow lifecycle mutation only from identity-verified, non-stale prices.

        Coverage snapshots and labelled last-known fallbacks are useful for
        observation, but they must never settle or advance an open position.
        ``usable_for_promotion=False`` is an explicit veto even when the
        instrument identity itself is known.
        """
        if not isinstance(evidence, dict) or evidence.get("identity_verified") is not True:
            return False
        if evidence.get("stale") is True or evidence.get("usable_for_promotion") is False:
            return False
        freshness = str(evidence.get("freshness_state") or evidence.get("price_freshness_state") or "").lower().strip()
        return freshness in {"live", "live_current", "closed_market"}

    def _decision_is_signal(self, d: Dict[str, Any]) -> bool:
        return is_actionable_signal(d)

    def _signal_key(self, d: Dict[str, Any]) -> str:
        mode = require_production_mode(d.get("mode"))
        supplied = str(d.get("decision_id") or d.get("signal_id") or "").strip()
        if supplied:
            return supplied
        sym = str(d.get("symbol") or "").upper().strip()
        side = str(d.get("side") or "WAIT").upper().strip()
        if mode == "intraday":
            return f"{now_iso()[:10]}:{sym}:intraday:{side}"
        return f"CARRY:{sym}:delivery:{side}"

    def _result_for(self, side: str, entry, t1, t2, sl, ltp, t1_hit_already: bool = False):
        """Stable tuple adapter over the governed holding lifecycle.

        Callers consume a five-tuple.  The real update paths pass the
        complete payload to ``_lifecycle_result`` so managed stops, high-water
        marks and partial-profit state persist across ticks.
        """
        payload = {"secured_fraction": 0.5 if t1_hit_already else 0.0, "secured_price": t1 if t1_hit_already else None}
        out = PositionLifecycleService.evaluate_tick(
            {"side": side, "entry": entry, "t1": t1, "t2": t2, "sl": sl}, payload, ltp
        )
        return out.get("result") or "OPEN", out.get("status") or "OPEN", out.get("exit"), out.get("pnl"), bool((out.get("payload") or {}).get("secured_fraction"))

    def _lifecycle_result(self, row: Dict[str, Any], payload: Dict[str, Any], ltp) -> Dict[str, Any]:
        return PositionLifecycleService.evaluate_tick(row, payload, ltp)

    def _parse_dt(self, value: Any) -> Optional[datetime]:
        if value in (None, ""):
            return None
        s = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            try:
                dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
                except Exception:
                    return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _payload(self, row: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return json.loads(row.get("payload_json") or "{}") if isinstance(row, dict) else json.loads(row["payload_json"] or "{}")
        except Exception:
            return {}

    def _pnl_for_exit(self, side: str, entry, exit_price):
        entry = self._f(entry); exit_price = self._f(exit_price); side = str(side or "").upper()
        if entry is None or exit_price is None:
            return None
        return round(exit_price - entry, 2) if side == "LONG" else round(entry - exit_price, 2)

    def _mfe_mae_from_price(self, side: str, entry, price, prior_mfe=None, prior_mae=None):
        entry = self._f(entry); price = self._f(price); side = str(side or "").upper()
        mfe = self._f(prior_mfe); mae = self._f(prior_mae)
        if entry is None or price is None or side not in ("LONG", "SHORT"):
            return mfe, mae
        move = price - entry if side == "LONG" else entry - price
        mfe = max(mfe if mfe is not None else move, move)
        mae = min(mae if mae is not None else move, move)
        return round(mfe, 2), round(mae, 2)

    def _record_learning_outcome(self, row: Dict[str, Any], result: str, pnl_points, payload: Dict[str, Any], proof: Optional[Dict[str, Any]] = None) -> None:
        record = dict(row)
        signal_id = str(record.get("signal_id") or "").strip()
        if not signal_id:
            return
        opened = self._parse_dt(record.get("opened_at"))
        closed = self._parse_dt(record.get("closed_at") or now_iso())
        holding_minutes = round((closed - opened).total_seconds() / 60.0, 1) if opened and closed else None
        features = learning_features(record, payload)
        attribution = attribute_outcome(result, pnl_points, payload)
        self.conn.execute("""INSERT OR IGNORE INTO outcome_learning(
            signal_id,symbol,mode,side,result,pnl_points,holding_minutes,attribution,
            feature_json,proof_json,model_version,closed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
            signal_id, record.get("symbol"), record.get("mode"), record.get("side"), result,
            self._f(pnl_points), holding_minutes, attribution, json.dumps(features),
            json.dumps(proof or {}), payload.get("ranking_version") or payload.get("model_version") or "rules-current",
            record.get("closed_at") or now_iso(),
        ))

    def evaluate_signal_from_candles(self, row: Dict[str, Any], candles: List[Dict[str, Any]], interval: str = "1minute") -> Dict[str, Any]:
        """Replay the same managed lifecycle used by live quote updates.

        Candle OHLC does not reveal the path inside a bar.  A target and an
        already-active stop in the same candle remain AMBIGUOUS.  A protective
        stop first earned by that candle becomes effective from the next candle,
        matching the bar-close lifecycle contract; it cannot retroactively stop
        the same candle.  T1 secures only the configured fraction.
        """
        d = dict(row)
        payload = self._payload(d)
        side = str(d.get("side") or payload.get("side") or "").upper()
        entry = self._f(d.get("entry") if d.get("entry") is not None else payload.get("entry"))
        t1 = self._f(d.get("t1") if d.get("t1") is not None else payload.get("t1"))
        t2 = self._f(d.get("t2") if d.get("t2") is not None else payload.get("t2"))
        sl = self._f(d.get("sl") if d.get("sl") is not None else payload.get("sl"))
        opened_at = self._parse_dt(d.get("opened_at") or d.get("triggered_at"))
        if side not in ("LONG", "SHORT") or entry is None or sl is None or not candles:
            return {"status": "OPEN", "result": "OPEN", "proof": "insufficient_signal_or_candles", "mfe": None, "mae": None, "payload": payload}

        relevant = []
        for c in candles:
            ts = self._parse_dt(c.get("timestamp") or c.get("time") or c.get("date"))
            if opened_at and ts and ts < opened_at:
                continue
            if self._f(c.get("high")) is None or self._f(c.get("low")) is None:
                continue
            relevant.append(c)
        if not relevant:
            return {"status": "OPEN", "result": "OPEN", "proof": "no_post_trigger_candles", "mfe": None, "mae": None, "payload": payload}

        working_row = {"side": side, "entry": entry, "t1": t1, "t2": t2, "sl": sl}
        last_close = None
        last_ts = None

        def hit_up(level, hi):
            return level is not None and hi is not None and hi >= level

        def hit_down(level, lo):
            return level is not None and lo is not None and lo <= level

        for c in relevant:
            hi = self._f(c.get("high")); lo = self._f(c.get("low")); close = self._f(c.get("close"))
            ts = c.get("timestamp") or c.get("time") or c.get("date")
            last_close = close if close is not None else last_close
            last_ts = ts or last_ts

            prior_payload = dict(payload)
            prior_managed = self._f(prior_payload.get("managed_sl"))
            active_before = prior_managed if prior_managed is not None else sl
            secured_before = self._f(prior_payload.get("secured_fraction")) or 0.0
            raw_t1_hit = hit_up(t1, hi) if side == "LONG" else hit_down(t1, lo)
            target1_hit = raw_t1_hit and secured_before < PositionLifecycleService.PARTIAL_FRACTION
            target2_hit = hit_up(t2, hi) if side == "LONG" else hit_down(t2, lo)
            stop_before_hit = hit_down(active_before, lo) if side == "LONG" else hit_up(active_before, hi)

            # With only OHLC, target-versus-stop sequence inside one candle is
            # unknowable. Preserve that uncertainty before changing lifecycle.
            if (target1_hit or target2_hit) and stop_before_hit:
                probe = close if close is not None else entry
                out = PositionLifecycleService.evaluate_tick(working_row, prior_payload, probe)
                p = out.get("payload") or prior_payload
                return {
                    "status": "AMBIGUOUS", "result": "AMBIGUOUS_TARGET_AND_STOP_SAME_CANDLE",
                    "exit": probe, "pnl": self._pnl_for_exit(side, entry, probe),
                    "mfe": p.get("mfe"), "mae": p.get("mae"), "payload": p,
                    "proof": "same_candle_target_and_active_stop", "proof_ts": ts, "interval": interval,
                }

            # An already-active managed/original stop is deterministic when no
            # target is also present in the bar.
            if stop_before_hit:
                out = PositionLifecycleService.evaluate_tick(working_row, prior_payload, active_before)
                p = out.get("payload") or prior_payload
                return {**out, "mfe": p.get("mfe"), "mae": p.get("mae"), "proof": "post_trigger_candle_managed_stop", "proof_ts": ts, "interval": interval}

            favourable_extreme = hi if side == "LONG" else lo
            if favourable_extreme is not None:
                out = PositionLifecycleService.evaluate_tick(working_row, prior_payload, favourable_extreme)
                payload = dict(out.get("payload") or prior_payload)

                # A stop first earned by this completed candle becomes active
                # from the next candle.  Applying it retroactively to the same
                # OHLC bar creates a false ambiguity and contradicts the managed
                # bar-close lifecycle.  An already-active stop was handled above.
                new_managed = self._f(payload.get("managed_sl"))
                new_protection = new_managed is not None and (prior_managed is None or abs(new_managed - prior_managed) > 1e-9)
                if new_protection:
                    payload["managed_stop_effective_from_next_candle"] = True
                    payload["managed_stop_earned_at"] = ts
                if out.get("status") != "OPEN":
                    return {**out, "mfe": payload.get("mfe"), "mae": payload.get("mae"), "proof": "post_trigger_candle_lifecycle", "proof_ts": ts, "interval": interval}

            # Update adverse excursion using the opposite extreme without
            # triggering a stop already ruled out above.
            adverse_extreme = lo if side == "LONG" else hi
            if adverse_extreme is not None:
                newly_touched_obstacle = bool(payload.get("obstacle_touched")) and not bool(prior_payload.get("obstacle_touched"))
                if new_protection and not newly_touched_obstacle:
                    # Record same-bar adverse excursion without activating the
                    # newly earned stop until the next completed candle.
                    if side == "LONG":
                        low_water = min(adverse_extreme, self._f(payload.get("low_water_price")) or adverse_extreme)
                        payload["low_water_price"] = round(low_water, 2)
                        payload["mae"] = round(low_water - entry, 2)
                    else:
                        high_water = max(adverse_extreme, self._f(payload.get("high_water_price")) or adverse_extreme)
                        payload["high_water_price"] = round(high_water, 2)
                        payload["mae"] = round(entry - high_water, 2)
                    continue
                adverse_out = PositionLifecycleService.evaluate_tick(working_row, payload, adverse_extreme)
                if adverse_out.get("status") != "OPEN":
                    p = adverse_out.get("payload") or payload
                    if adverse_out.get("result") == "STRUCTURAL_REJECTION_EXIT" and not prior_payload.get("obstacle_touched"):
                        return {
                            "status": "AMBIGUOUS", "result": "AMBIGUOUS_OBSTACLE_TOUCH_AND_REJECTION_SAME_CANDLE",
                            "exit": close if close is not None else entry,
                            "pnl": self._pnl_for_exit(side, entry, close if close is not None else entry),
                            "mfe": p.get("mfe"), "mae": p.get("mae"), "payload": p,
                            "proof": "same_candle_obstacle_touch_and_retrace", "proof_ts": ts, "interval": interval,
                        }
                    return {**adverse_out, "mfe": p.get("mfe"), "mae": p.get("mae"), "proof": "post_trigger_candle_managed_stop", "proof_ts": ts, "interval": interval}
                payload = dict(adverse_out.get("payload") or payload)

        return {
            "status": "OPEN",
            "result": "T1_PARTIAL_SECURED" if (self._f(payload.get("secured_fraction")) or 0) > 0 else ("OPEN_PROTECTED" if payload.get("managed_sl") is not None else "OPEN"),
            "exit": None, "pnl": None, "mfe": payload.get("mfe"), "mae": payload.get("mae"),
            "last_price": last_close, "payload": payload,
            "proof": "post_trigger_candles_open", "proof_ts": last_ts, "interval": interval,
        }

    def _target_stage_label(self, result: str, status: str, t1_hit: bool, payload: Optional[Dict[str, Any]] = None) -> str:
        payload = payload or {}
        state = str(payload.get("lifecycle_state") or "").upper()
        if status == "SUCCESS":
            if result == "SUCCESS_T2_MANAGED":
                return "Closed - T2 Hit (managed)"
            if result == "PROTECTED_EXIT_AFTER_T1":
                return "Closed - Profit Protected"
            if result == "BREAKEVEN_OR_TRAILING_EXIT":
                return "Closed - Managed Exit"
            if result == "STRUCTURAL_REJECTION_EXIT":
                return "Closed - Resistance Rejection Profit"
            return "Closed - Target Hit"
        if status == "FAIL":
            return "Closed - Original SL" if result == "FAIL_SL" else "Closed - Managed Exit"
        if state == "TRAILING_PROFIT":
            return "Trailing Profit"
        if state == "BREAKEVEN_PROTECTED":
            return "Break-even Protected"
        if t1_hit or float(payload.get("secured_fraction") or 0) > 0:
            return "T1 Secured - Remainder Protected"
        return "Awaiting Profit Protection"

    def _stage_remarks(self, t1_hit: bool, sl, payload: Optional[Dict[str, Any]] = None) -> str:
        payload = payload or {}
        managed = self._f(payload.get("managed_sl"))
        secured = self._f(payload.get("secured_fraction")) or 0.0
        if str(payload.get("lifecycle_state") or "").upper() == "CLOSED_STRUCTURAL_REJECTION":
            return str(payload.get("lifecycle_reason") or "Structural obstacle rejection exit")
        if secured > 0 and managed is not None:
            return f"{int(round(secured*100))}% secured; managed SL ₹{managed} (original ₹{sl})"
        if managed is not None:
            return f"Profit protected; managed SL ₹{managed} (original ₹{sl})"
        return "Original SL active until position earns protection"

    def _upsert_signal_from_decision(self, d: Dict[str, Any]) -> None:
        canonical_mode = require_production_mode(d.get("mode"))
        d = dict(d)
        d["mode"] = canonical_mode
        signal_id = self._signal_key(d)
        existing_row = None
        existing_payload: Dict[str, Any] = {}

        # Reuse the immutable open thesis and preserve its lifecycle state. A
        # scanner re-evaluation may update evidence, but it must never erase a
        # managed stop, secured profit or original execution levels.
        if canonical_mode == "delivery":
            existing_row = self.conn.execute(
                "SELECT * FROM signal_ledger WHERE status='OPEN' AND UPPER(symbol)=? AND UPPER(side)=? "
                "AND LOWER(mode)='delivery' ORDER BY opened_at ASC LIMIT 1",
                (str(d.get("symbol") or "").upper().strip(), str(d.get("side") or "").upper().strip()),
            ).fetchone()
            if existing_row:
                signal_id = existing_row["signal_id"]
                for key in ("entry", "t1", "t2", "sl"):
                    if self._f(existing_row[key]) is not None:
                        d[key] = existing_row[key]
                try:
                    existing_payload = json.loads(existing_row["payload_json"] or "{}")
                except Exception:
                    existing_payload = {}

        entry = self._f(d.get("entry") or d.get("ltp")); t1 = self._f(d.get("t1")); t2 = self._f(d.get("t2")); sl = self._f(d.get("sl")); ltp = self._f(d.get("ltp"))
        payload = dict(existing_payload)
        lifecycle_keys = {
            key: existing_payload.get(key) for key in (
                "lifecycle_version", "lifecycle_state", "lifecycle_reason", "lifecycle_transitions",
                "original_sl", "managed_sl", "breakeven_price", "secured_fraction", "secured_price",
                "high_water_price", "low_water_price", "mfe", "mae", "mfe_r", "mfe_retrace", "mfe_retrace_fraction",
                "obstacle_touched", "obstacle_touch_price", "add_allowed", "fomo_guard", "reentry_policy",
                "first_obstacle", "first_obstacle_low", "first_obstacle_high", "first_obstacle_touches",
                "structural_target_state", "structural_target_reason", "profit_protection_plan"
            ) if key in existing_payload
        }
        payload.update(d)
        payload.update(lifecycle_keys)
        lifecycle = self._lifecycle_result({"side": d.get("side"), "entry": entry, "t1": t1, "t2": t2, "sl": sl}, payload, ltp)
        payload = lifecycle.get("payload") or payload
        result = lifecycle.get("result") or "OPEN"; status = lifecycle.get("status") or "OPEN"
        exit_price = lifecycle.get("exit"); pnl = lifecycle.get("pnl")
        t1_hit = bool((payload.get("secured_fraction") or 0) > 0)
        payload.setdefault("decision_as_of", now_iso())
        payload.setdefault("price_freshness_state", payload.get("freshness_state") or "unknown")
        payload.setdefault("candle_freshness_state", payload.get("candle_state") or "unknown")
        payload.update({"signal_id": signal_id, "result": result, "signal_status": status, "pnl_points": pnl,
                         "t1_hit": t1_hit, "target_stage": self._target_stage_label(result, status, t1_hit, payload),
                         "stage_remarks": self._stage_remarks(t1_hit, sl, payload)})
        self.conn.execute("""INSERT INTO signal_ledger(signal_id,trade_date,symbol,exchange,mode,side,decision,entry,t1,t2,sl,ltp,exit,score,rr,result,status,reason,payload_json,opened_at,last_update,closed_at,pnl_points,source)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?,?,?)
        ON CONFLICT(signal_id) DO UPDATE SET ltp=excluded.ltp, exit=COALESCE(signal_ledger.exit, excluded.exit), result=CASE WHEN signal_ledger.status IN ('SUCCESS','FAIL') THEN signal_ledger.result ELSE excluded.result END,
          status=CASE WHEN signal_ledger.status IN ('SUCCESS','FAIL') THEN signal_ledger.status ELSE excluded.status END, score=excluded.score, payload_json=excluded.payload_json, last_update=CURRENT_TIMESTAMP, closed_at=CASE WHEN signal_ledger.closed_at IS NOT NULL THEN signal_ledger.closed_at WHEN excluded.status IN ('SUCCESS','FAIL') THEN CURRENT_TIMESTAMP ELSE NULL END, pnl_points=COALESCE(signal_ledger.pnl_points, excluded.pnl_points)""",
          (signal_id, now_iso()[:10], str(d.get("symbol") or "").upper(), d.get("exchange"), canonical_mode, d.get("side"), d.get("decision"), entry, t1, t2, sl, ltp, exit_price, d.get("score"), d.get("rr"), result, status, d.get("reason"), json.dumps(payload), now_iso() if status in ("SUCCESS","FAIL") else None, pnl, "scanner_selected"))

    def _update_signal_progress_from_decision(self, d: Dict[str, Any]) -> None:
        sym = str(d.get("symbol") or "").upper().strip(); mode = str(d.get("mode") or "").lower().strip(); side = str(d.get("side") or "").upper().strip()
        if not sym or not mode or side not in ("LONG", "SHORT"):
            return
        if not is_production_mode(mode):
            return
        canonical = require_production_mode(mode)
        if canonical == "delivery":
            row = self.conn.execute("SELECT * FROM signal_ledger WHERE symbol=? AND UPPER(side)=? AND status='OPEN' AND LOWER(mode)='delivery' ORDER BY opened_at ASC LIMIT 1", (sym, side)).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM signal_ledger WHERE trade_date=? AND symbol=? AND LOWER(mode)='intraday' AND UPPER(side)=? AND status='OPEN' ORDER BY opened_at DESC LIMIT 1", (now_iso()[:10], sym, side)).fetchone()
        if not row or self._f(d.get("ltp")) is None or not self._verified_lifecycle_price(d):
            return
        payload = json.loads(row["payload_json"] or "{}")
        lifecycle = self._lifecycle_result(dict(row), payload, d.get("ltp"))
        payload = lifecycle.get("payload") or payload
        result = lifecycle.get("result") or "OPEN"; status = lifecycle.get("status") or "OPEN"
        exit_price = lifecycle.get("exit"); pnl = lifecycle.get("pnl")
        t1_hit = bool((payload.get("secured_fraction") or 0) > 0)
        payload.update({"ltp": d.get("ltp"), "result": result, "signal_status": status, "last_ai_validation": d.get("last_ai_validation"),
                         "t1_hit": t1_hit, "target_stage": self._target_stage_label(result, status, t1_hit, payload),
                         "stage_remarks": self._stage_remarks(t1_hit, row["sl"], payload)})
        self.conn.execute("UPDATE signal_ledger SET ltp=?, result=?, status=?, exit=COALESCE(exit, ?), pnl_points=COALESCE(pnl_points, ?), payload_json=?, last_update=CURRENT_TIMESTAMP, closed_at=CASE WHEN ? IN ('SUCCESS','FAIL') THEN COALESCE(closed_at, CURRENT_TIMESTAMP) ELSE closed_at END WHERE signal_id=?",
          (self._f(d.get("ltp")), result, status, exit_price, pnl, json.dumps(payload), status, row["signal_id"]))
        if status in ("SUCCESS", "FAIL"):
            self._record_learning_outcome(dict(row), result, pnl, payload, {"source": "decision_refresh", "ltp": d.get("ltp")})

    def refresh_open_signals_from_quotes(self, quotes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        clean = {}
        ignored_unverified = 0
        for sym, q in (quotes or {}).items():
            s = str(sym or "").upper().strip()
            if not s or not isinstance(q, dict) or self._f(q.get("ltp")) is None:
                continue
            if not self._verified_lifecycle_price(q):
                ignored_unverified += 1
                continue
            clean[s] = q
        if not clean:
            return {"updated": 0, "closed": 0, "closed_symbols": [], "ignored_unverified": ignored_unverified}
        marks = ",".join("?" for _ in clean)
        updated = 0
        closed = 0
        closed_symbols = []
        changes = []
        # v60.5: SELECT-then-UPDATE-per-row under write_lock -- without it, a
        # concurrent save_decision() for the same signal_id between this
        # SELECT and its matching UPDATE can be silently overwritten (lost
        # update), since neither side knew about the other's in-flight write.
        with self._write_lock:
            rows = self.conn.execute(f"SELECT * FROM signal_ledger WHERE status='OPEN' AND UPPER(symbol) IN ({marks})", tuple(clean.keys())).fetchall()
            for row in rows:
                sym = str(row["symbol"] or "").upper()
                q = clean.get(sym) or {}
                ltp = self._f(q.get("ltp"))
                if ltp is None:
                    continue
                payload = json.loads(row["payload_json"] or "{}")
                lifecycle = self._lifecycle_result(dict(row), payload, ltp)
                payload = lifecycle.get("payload") or payload
                result = lifecycle.get("result") or "OPEN"; status = lifecycle.get("status") or "OPEN"
                exit_price = lifecycle.get("exit"); pnl = lifecycle.get("pnl")
                t1_hit = bool((payload.get("secured_fraction") or 0) > 0)
                tick_ts = q.get("timestamp") or now_iso()
                payload.update({
                    "ltp": ltp, "result": result, "signal_status": status, "last_quote_tick": tick_ts,
                    "price_freshness": "live @ " + str(tick_ts),
                    "validation_source": "live_quote_tick", "validation_policy": "managed lifecycle on verified ticks; candle audit proves intrabar sequence",
                    "t1_hit": t1_hit, "target_stage": self._target_stage_label(result, status, t1_hit, payload),
                    "stage_remarks": self._stage_remarks(t1_hit, row["sl"], payload)
                })
                self.conn.execute("UPDATE signal_ledger SET ltp=?, result=?, status=?, exit=COALESCE(exit, ?), pnl_points=COALESCE(pnl_points, ?), payload_json=?, last_update=CURRENT_TIMESTAMP, closed_at=CASE WHEN ? IN ('SUCCESS','FAIL','AMBIGUOUS') THEN COALESCE(closed_at, CURRENT_TIMESTAMP) ELSE closed_at END WHERE signal_id=?",
                  (ltp, result, status, exit_price, pnl, json.dumps(payload), status, row["signal_id"]))
                updated += 1
                changes.append({"symbol": sym, "mode": row["mode"], "side": row["side"], "status": status, "result": result, "ltp": ltp, "exit": exit_price, "pnl_points": pnl})
                if status in ("SUCCESS", "FAIL"):
                    self._record_learning_outcome(dict(row), result, pnl, payload, {"source": "live_quote_tick", "ltp": ltp})
                    closed += 1
                    closed_symbols.append(sym)
            if updated:
                self.conn.commit()
        return {"updated": updated, "closed": closed, "closed_symbols": sorted(set(closed_symbols)), "changes": changes, "ignored_unverified": ignored_unverified}

    def latest_decisions(self, mode: str = "all", limit: int = 50) -> List[Dict[str, Any]]:
        # Compatibility/read-model callers need the persisted timestamp and IST
        # trading date even when older payloads did not embed them.  Production
        # PostgreSQL already stores these as canonical columns; mirror that read
        # contract here so tests and rollback projections do not depend on a
        # payload-format accident.
        if mode == "all":
            rows = self.conn.execute(
                "SELECT payload_json,created_at,mode FROM decisions "
                "WHERE LOWER(mode) IN ('intraday','delivery') ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            modes = _desk_modes(mode); marks = ",".join("?" for _ in modes)
            rows = self.conn.execute(
                f"SELECT payload_json,created_at,mode FROM decisions "
                f"WHERE LOWER(mode) IN ({marks}) ORDER BY id DESC LIMIT ?",
                (*modes, limit),
            ).fetchall()
        out = []
        seen = set()
        for r in rows:
            d = json.loads(r["payload_json"] or "{}")
            persisted_at = str(r["created_at"] or "").strip()
            d.setdefault("mode", str(r["mode"] or "").lower())
            if persisted_at:
                d.setdefault("created_at", persisted_at)
                try:
                    parsed = datetime.fromisoformat(persisted_at.replace("Z", "+00:00"))
                    d.setdefault("trading_date", as_india(parsed).date().isoformat())
                except (TypeError, ValueError):
                    pass
            key = (d.get("symbol"), d.get("mode"))
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
        return out

    def open_signal_rows(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Return only current two-desk lifecycle rows.

        Retired modes remain preserved as audit evidence but are never advanced,
        settled, projected, or silently translated into Delivery/Intraday.
        """
        rows = self.conn.execute(
            """SELECT * FROM signal_ledger
               WHERE status='OPEN'
                 AND LOWER(COALESCE(mode,'')) IN ('intraday','delivery')
               ORDER BY trade_date ASC, opened_at ASC LIMIT ?""",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def settle_signal_by_id(self, signal_id: str, status: str, result: str, exit_price, pnl, proof: Optional[Dict[str, Any]] = None) -> None:
        """Settle a specific OPEN signal by immutable id. The proof payload carries
        post-trigger candle audit fields (MFE/MAE/interval/proof timestamp) so
        Selected Candidates, Daily Performance and Trade Journal all read the same ledger."""
        row = self.conn.execute("SELECT * FROM signal_ledger WHERE signal_id=?", (signal_id,)).fetchone()
        payload: Dict[str, Any] = {}
        if row:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
        if proof:
            payload.update({k: v for k, v in proof.items() if v is not None})
        payload.update({"result": result, "signal_status": status, "exit": exit_price, "pnl_points": pnl, "last_lifecycle_update": now_iso()})
        self.conn.execute(
            "UPDATE signal_ledger SET status=?, result=?, exit=COALESCE(exit, ?), pnl_points=COALESCE(pnl_points, ?), payload_json=?, "
            "last_update=CURRENT_TIMESTAMP, closed_at=COALESCE(closed_at, CURRENT_TIMESTAMP) WHERE signal_id=? AND status='OPEN'",
            (status, result, exit_price, pnl, json.dumps(payload), signal_id)
        )
        if row and status in ("SUCCESS", "FAIL", "EXPIRED", "AMBIGUOUS"):
            closed_row = dict(row)
            closed_row["closed_at"] = now_iso()
            self._record_learning_outcome(closed_row, result, pnl, payload, proof)
        self.conn.commit()
        if row and status in ("SUCCESS", "FAIL", "EXPIRED", "AMBIGUOUS"):
            CanonicalDecisionRepository(self.conn, self._write_lock, self._event, ensure_schema=False).record_outcome(signal_id, {
                "status": status, "result": result, "exit": exit_price, "pnl_points": pnl,
                "proof": proof or {}, "closed_at": now_iso(),
            })

    def selected_signals(self, mode: str = "all", limit: int = 50) -> List[Dict[str, Any]]:
        """Return open lifecycle rows for Intraday and Delivery only.

        Only exact Intraday and Delivery rows can enter customer, lifecycle,
        risk, performance, or learning projections.
        """
        today = now_iso()[:10]
        requested = normalise_mode(mode or "all")
        if requested not in ("all", "intraday", "delivery"):
            raise ValueError(f"unsupported production mode '{requested}'; allowed modes are intraday and delivery")
        params: List[Any] = []
        if requested == "all":
            sql = """SELECT * FROM signal_ledger
                     WHERE status='OPEN'
                       AND ((LOWER(mode)='intraday' AND trade_date=?)
                            OR LOWER(mode)='delivery')"""
            params.append(today)
        elif requested == "intraday":
            sql = """SELECT * FROM signal_ledger WHERE status='OPEN' AND trade_date=?
                     AND LOWER(mode)='intraday'"""
            params.append(today)
        else:
            sql = """SELECT * FROM signal_ledger WHERE status='OPEN'
                     AND LOWER(mode)='delivery'"""
        sql += " ORDER BY opened_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        out = []
        for r in rows:
            canonical_mode = require_production_mode(r["mode"])
            d = json.loads(r["payload_json"] or "{}")
            live_decision = str(d.get("decision") or "").upper()
            opened_decision = str(r["decision"] or "").upper()
            if live_decision and opened_decision and live_decision != opened_decision:
                d["live_structure_note"] = f"Opened as {opened_decision}; current read: {live_decision}"
            d["decision"] = r["decision"] or d.get("decision")
            d["side"] = r["side"] or d.get("side")
            d["mode"] = canonical_mode
            status = str(r["status"] or "OPEN")
            lifecycle = "same_session_only" if canonical_mode == "intraday" else "persistent_until_success_fail_exit_or_invalidation"
            d.update({
                "signal_id": r["signal_id"], "signal_status": status, "result": r["result"], "entry": r["entry"], "t1": r["t1"], "t2": r["t2"], "sl": r["sl"],
                "ltp": r["ltp"], "exit": r["exit"], "pnl_points": r["pnl_points"], "opened_at": r["opened_at"], "triggered_at": d.get("triggered_at") or r["opened_at"],
                "last_update": r["last_update"], "closed_at": r["closed_at"], "status": "SIGNAL_" + status,
                "target_stage": d.get("target_stage") or self._target_stage_label(r["result"] or "OPEN", status, bool(d.get("t1_hit")), d),
                "stage_remarks": d.get("stage_remarks") or self._stage_remarks(bool(d.get("t1_hit")), r["sl"], d),
                "mfe": d.get("mfe"), "mae": d.get("mae"), "validation_source": d.get("validation_source"), "proof_ts": d.get("proof_ts"),
                "selected_lifecycle": lifecycle,
                "validation_policy": d.get("validation_policy") or "post-trigger ledger validation",
            })
            out.append(d)
        return out

    def cancel_invalid_carry_shorts(self) -> int:
        """Close invalid Delivery SHORT rows that violate the current product policy.

        Delivery is long-only. These rows are cancelled rather than labelled
        success/failure because no market outcome should be fabricated for a
        signal the product should never have admitted.
        """
        with self._write_lock:
            cursor = self.conn.execute(
                """UPDATE signal_ledger
                   SET status='CANCELLED',
                       result='CANCELLED_POLICY_CARRY_SHORT',
                       closed_at=COALESCE(closed_at, CURRENT_TIMESTAMP),
                       last_update=CURRENT_TIMESTAMP
                   WHERE status='OPEN'
                     AND UPPER(COALESCE(side,''))='SHORT'
                     AND LOWER(COALESCE(mode,''))='delivery'"""
            )
            changed = max(0, int(cursor.rowcount or 0))
            if changed:
                self.conn.commit()
                self._event(
                    "WARN", "signal_ledger",
                    "Cancelled invalid Delivery short signals",
                    {"count": changed, "result": "CANCELLED_POLICY_CARRY_SHORT"},
                )
            return changed

    def _parse_ts(self, s: Optional[str]):
        # Duplicated intentionally from storage.py's Store._parse_ts (used by
        # cluster 9's trade-journal code too, not yet extracted): a small,
        # pure date-parsing helper is cheaper to keep in sync than to wire a
        # cross-repository dependency for. Same logic, no behavior change.
        if not s:
            return None
        s = str(s).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s[:19] if "%z" not in fmt else s, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None

    def expire_fast_desk_signals(self, reason: str = "trade_window_expired") -> int:
        """Move stale Intraday and legacy same-day rows out of Selected Candidates.

        These rows become Day Performance + Trade Journal rows by changing the
        ledger status to EXPIRED. The live selected list then contains only
        actionable opportunities, while proof/history remains available.
        """
        today = now_iso()[:10]
        rows = self.conn.execute(
            "SELECT * FROM signal_ledger WHERE status='OPEN' AND LOWER(mode)='intraday'"
        ).fetchall()
        now = datetime.now()
        expired = 0
        for r in rows:
            trade_date = str(r["trade_date"] or "")
            opened = self._parse_ts(r["opened_at"])
            age_min = ((now - opened).total_seconds()/60.0) if opened else None
            # Fast desks are intraday instruments. After market/day window, or after
            # several hours, they are no longer actionable selected candidates.
            should_expire = trade_date < today or (age_min is not None and age_min > 390)
            if not should_expire:
                continue
            exit_price = self._f(r["ltp"] if r["ltp"] is not None else r["entry"])
            pnl = self._pnl_for_exit(r["side"], r["entry"], exit_price) if exit_price is not None else None
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except Exception:
                payload = {}
            payload.update({
                "signal_status": "EXPIRED", "result": "EXPIRED_WINDOW", "exit": exit_price,
                "pnl_points": pnl, "validation_source": reason,
                "validation_policy": "Fast-desk signal left Selected Candidates because its actionable intraday window expired.",
                "last_lifecycle_update": now_iso()
            })
            self.conn.execute(
                "UPDATE signal_ledger SET status='EXPIRED', result='EXPIRED_WINDOW', exit=COALESCE(exit, ?), pnl_points=COALESCE(pnl_points, ?), payload_json=?, last_update=CURRENT_TIMESTAMP, closed_at=COALESCE(closed_at, CURRENT_TIMESTAMP) WHERE signal_id=? AND status='OPEN'",
                (exit_price, pnl, json.dumps(payload), r["signal_id"])
            )
            expired += 1
        if expired:
            self.conn.commit()
        return expired
