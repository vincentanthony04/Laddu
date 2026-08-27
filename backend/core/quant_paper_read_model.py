"""Paper-position marking, diagnostics and read models."""
from __future__ import annotations

from core.quant_paper_dependencies import *  # noqa: F401,F403


class QuantPaperReadModelMixin:
    def precomputed_candidate(self, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        """Read-only lookup safe for the per-candidate canonical ranker."""
        if bool(getattr(self.store, "production_model_governance_required", False)):
            return {
                "ok": True,
                "state": MODEL_UNAVAILABLE,
                "prediction_state": MODEL_UNAVAILABLE,
                "reason": "Legacy SQLite quant activation is compatibility/research evidence only; production weight requires a PostgreSQL champion assignment.",
                "paper_rank_score": float(_number(candidate.get("evidence_score")) or _number(candidate.get("rank_score")) or _number(candidate.get("score")) or 0.0),
                "decision_weight": 0.0,
                "active_model_id": None,
                "predictions": [],
                "broker_execution_weight": 0.0,
                "broker_order_authority": "NONE",
                "read_only": True,
                "authority": "SEPARATE_GOVERNANCE_POSTGRES",
            }
        candidate_id = str(candidate.get("candidate_id") or "")
        symbol = str(candidate.get("symbol") or candidate.get("stock") or "").upper()
        mode = require_production_mode(candidate.get("mode"))
        routed_horizon = _default_model_horizon(mode)
        if candidate_id:
            rows = self.store.conn.execute(
                """SELECT * FROM quant_paper_predictions
                   WHERE candidate_id=? AND mode=? AND horizon=?
                   ORDER BY observed_at DESC,model_id""",
                (candidate_id, mode, routed_horizon),
            ).fetchall()
        else:
            rows = self.store.conn.execute(
                """SELECT * FROM quant_paper_predictions
                   WHERE symbol=? AND mode=? AND horizon=?
                   ORDER BY observed_at DESC,model_id LIMIT 4""",
                (symbol, mode, routed_horizon),
            ).fetchall()
        predictions = [dict(row) for row in rows]
        scored = next((row for row in predictions if row.get("prediction_state") == "SCORED"), None)
        active = next(
            (
                row for row in predictions
                if row.get("model_state") == PREDICTION_ACTIVE
                and row.get("prediction_state") == "SCORED"
            ),
            None,
        )
        evidence = _number(candidate.get("evidence_score"))
        if evidence is None:
            evidence = _number(candidate.get("rank_score"))
        if evidence is None:
            evidence = _number(candidate.get("score")) or 0.0
        weight = float(active.get("paper_weight") or 0.0) if active else 0.0
        paper_rank = (
            (1.0 - weight) * evidence
            + weight * float(active["normalized_score"])
            if active else evidence
        )
        return {
            "ok": True,
            "state": PREDICTION_ACTIVE if active else MODEL_UNAVAILABLE,
            "prediction_state": PREDICTION_ACTIVE if active else MODEL_UNAVAILABLE,
            "paper_rank_score": round(max(0.0, min(100.0, paper_rank)), 4),
            "decision_weight": weight,
            "active_model_id": active.get("model_id") if active else None,
            "active_model_score": (float(active.get("normalized_score")) if active and _number(active.get("normalized_score")) is not None else None),
            "active_model_confidence": (float(active.get("confidence")) if active and _number(active.get("confidence")) is not None else None),
            "shadow_model_id": scored.get("model_id") if scored else None,
            "shadow_model_score": (float(scored.get("normalized_score")) if scored and _number(scored.get("normalized_score")) is not None else None),
            "shadow_model_confidence": (float(scored.get("confidence")) if scored and _number(scored.get("confidence")) is not None else None),
            "shadow_prediction_state": (scored.get("model_state") if scored else MODEL_UNAVAILABLE),
            "shadow_influence_weight": 0.0 if not active else weight,
            "predictions": predictions[:8],
            "broker_execution_weight": 0.0,
            "broker_order_authority": "NONE",
            "read_only": True,
            "routed_horizon": routed_horizon,
        }

    @staticmethod
    def _valid_quote(quote: Mapping[str, Any]) -> Optional[float]:
        price = _number(quote.get("ltp") if quote.get("ltp") is not None else quote.get("price"))
        fresh = str(quote.get("freshness_state") or "").lower() in {
            "live", "fresh", "live_current", "verified_close", "closed_market"
        }
        identity = quote.get("identity_verified") is True or quote.get("verified") is True
        if price and identity and fresh and quote.get("stale") is not True:
            return price
        return None

    @staticmethod
    def _timestamp(value: Any) -> Optional[datetime]:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _stress_pnl(
        net_pnl: float,
        *,
        entry: float,
        exit_price: float,
        quantity: int,
        extra_bps_each_side: float,
        ) -> float:
        extra = (float(entry) + float(exit_price)) * int(quantity) * float(extra_bps_each_side) / 10_000.0
        return round(float(net_pnl) - extra, 2)

    def _recheck_pending_bootstrap_admissions(self, quotes: Mapping[str, Any]) -> Dict[str, Any]:
        """Re-evaluate selected bootstrap candidates when a fresh quote arrives.

        Candidate populations are immutable by design, so a closed-market
        candidate keeps its completed-session quote forever.  Without this
        bridge, a legitimate PAPER_ADMISSION_WAITING state can never become an
        open Model Paper position at the next session.  Only the fresh quote
        fields are overlaid; the frozen thesis/features/trade map remain the
        original candidate evidence.
        """
        try:
            rows = self.store.conn.execute(
                """SELECT q.*,c.feature_json
                     FROM quant_paper_predictions q
                     JOIN candidate_population_observations c ON c.candidate_id=q.candidate_id
                    WHERE q.model_id LIKE 'bootstrap-%' AND q.prediction_state='SCORED'
                    ORDER BY q.observed_at DESC,q.prediction_id DESC LIMIT 250"""
            ).fetchall()
        except Exception:
            return {"rechecked": 0, "opened": 0, "waiting": 0, "skipped": 0}
        rechecked = opened = waiting = skipped = 0
        seen: set[str] = set()
        for raw in rows:
            row = dict(raw)
            candidate_id = str(row.get("candidate_id") or "")
            if not candidate_id or candidate_id in seen:
                continue
            seen.add(candidate_id)
            symbol = str(row.get("symbol") or "").upper().strip()
            quote = quotes.get(symbol) or quotes.get(str(symbol).upper())
            quote_row = dict(quote or {}) if isinstance(quote, Mapping) else {}
            freshness = str(quote_row.get("freshness_state") or "").lower()
            if (
                freshness not in {"live", "fresh", "live_current"}
                or quote_row.get("stale") is True
                or quote_row.get("usable_for_promotion") is False
                or self._valid_quote(quote_row) is None
            ):
                skipped += 1
                continue
            payload = _json_object(row.get("payload_json"))
            prior = payload.get("automatic_paper_admission")
            if isinstance(prior, Mapping) and str(prior.get("state") or "") in {
                "QUANT_EVALUATION_PAPER_OPENED", "ALREADY_OPEN"
            }:
                skipped += 1
                continue
            candidate = _json_object(row.get("feature_json"))
            if not candidate:
                skipped += 1
                continue
            price = self._valid_quote(quote_row)
            candidate.update({
                "candidate_id": candidate_id,
                "symbol": symbol,
                "ltp": price,
                "current_price": price,
                "quote_freshness_state": freshness,
                "price_freshness_state": freshness,
                "freshness_state": freshness,
                "stale": False,
                "usable_for_promotion": True,
                "execution_price_authority": True,
                "quote_as_of": quote_row.get("provider_timestamp") or quote_row.get("source_time") or quote_row.get("timestamp"),
            })
            event = self._latest_event(str(row.get("model_id") or ""))
            if not event:
                skipped += 1
                continue
            model = {
                "model_id": str(row.get("model_id") or ""),
                "mode": str(row.get("mode") or ""),
                "horizon": str(row.get("horizon") or ""),
            }
            prediction = {
                **row,
                "prediction_id": str(row.get("prediction_id") or ""),
                "normalized_score": float(row.get("normalized_score") or 0.0),
            }
            admission = self._maybe_open_evaluation(model, event, candidate, prediction)
            self._persist_admission_result(prediction["prediction_id"], admission)
            rechecked += 1
            state = str(admission.get("state") or "")
            if state in {"QUANT_EVALUATION_PAPER_OPENED", "ALREADY_OPEN"}:
                opened += 1
            else:
                waiting += 1
        return {"rechecked": rechecked, "opened": opened, "waiting": waiting, "skipped": skipped}

    def mark_quotes(self, quotes: Mapping[str, Any]) -> Dict[str, Any]:
        updates = []
        with self.store.write_lock:
            rows = self.store.conn.execute(
                "SELECT * FROM quant_evaluation_positions WHERE status='OPEN' ORDER BY opened_at"
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                try:
                    venue = self.costs.venue_identity(
                        exchange=str(row.get("exchange") or ""),
                        bse_group=str(row.get("bse_group") or "").strip().upper() or None,
                    )
                except ValueError as exc:
                    blocked_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    self.store.conn.execute(
                        """UPDATE quant_evaluation_positions
                              SET data_failure=1,unscorable=1,updated_at=?
                            WHERE position_id=?""",
                        (blocked_at, row["position_id"]),
                    )
                    updates.append({
                        "symbol": row["symbol"], "status": "VENUE_IDENTITY_BLOCKED",
                        "reason": str(exc), "unscorable": True,
                    })
                    continue
                quote = quotes.get(row["symbol"]) or quotes.get(str(row["symbol"]).upper())
                quote_row = dict(quote or {}) if isinstance(quote, Mapping) else {}
                mark_at = (
                    self._timestamp(
                        quote_row.get("provider_timestamp")
                        or quote_row.get("source_time")
                        or quote_row.get("as_of")
                        or quote_row.get("received_at")
                    )
                    or datetime.now(timezone.utc)
                )
                horizon_exit = self._timestamp(row.get("horizon_exit_at"))
                price = self._valid_quote(quote_row) if quote_row else None
                if price is None:
                    if quote_row:
                        self.store.conn.execute(
                            """UPDATE quant_evaluation_positions
                               SET data_failure=1,unscorable=?,updated_at=?
                               WHERE position_id=?""",
                            (
                                int(bool(horizon_exit and mark_at >= horizon_exit)),
                                mark_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
                                row["position_id"],
                            ),
                        )
                    continue
                session_due = False
                if (
                    str(row.get("mode")) == "delivery"
                    and int(row.get("horizon_sessions_required") or 0) > 0
                    and _is_verified_nse_session_close(
                        quote_row, mark_at=mark_at
                    )
                ):
                    session_state = _advance_session_counter(
                        last_session_date=row.get("last_session_date"),
                        sessions_observed=int(
                            row.get("horizon_sessions_observed") or 0
                        ),
                        required_sessions=int(
                            row.get("horizon_sessions_required") or 0
                        ),
                        mark_at=mark_at,
                    )
                    session_due = bool(session_state["due"])
                    if (
                        session_state["last_session_date"]
                        != str(row.get("last_session_date") or "")
                        or int(session_state["sessions_observed"])
                        != int(row.get("horizon_sessions_observed") or 0)
                    ):
                        self.store.conn.execute(
                            """UPDATE quant_evaluation_positions
                               SET horizon_sessions_observed=?,last_session_date=?
                               WHERE position_id=?""",
                            (
                                int(session_state["sessions_observed"]),
                                session_state["last_session_date"],
                                row["position_id"],
                            ),
                        )
                        row.update(session_state)
                side = str(row["side"])
                evaluation_objective = str(
                    row.get("evaluation_objective") or FIXED_HORIZON_OBJECTIVE
                )
                entry = float(row["entry_price"])
                original_stop = float(row["stop_price"])
                managed_stop = float(row.get("managed_stop") or original_stop)
                trailing_state = str(row.get("trailing_state") or "ORIGINAL_STOP")
                risk_per_share = abs(entry - original_stop)
                favorable = (price - entry) if side == "LONG" else (entry - price)
                estimate_for_stop = self.costs.round_trip(
                    row["mode"], side, entry, price, int(row["quantity"]),
                    exchange=str(venue["exchange"]), bse_group=venue["bse_group"],
                )
                cost_buffer = (
                    float(estimate_for_stop["costs"]["total"]) / max(1, int(row["quantity"]))
                )
                secured_profit = int(row.get("secured_profit") or 0)
                if risk_per_share > 0 and favorable >= risk_per_share:
                    breakeven_stop = (
                        entry + cost_buffer if side == "LONG" else entry - cost_buffer
                    )
                    if side == "LONG" and breakeven_stop > managed_stop:
                        managed_stop = breakeven_stop
                        trailing_state = "SECURE_PROFIT"
                        secured_profit = 1
                    elif side == "SHORT" and breakeven_stop < managed_stop:
                        managed_stop = breakeven_stop
                        trailing_state = "SECURE_PROFIT"
                        secured_profit = 1
                if risk_per_share > 0 and favorable >= 1.5 * risk_per_share:
                    trail_candidate = (
                        price - 0.75 * risk_per_share
                        if side == "LONG"
                        else price + 0.75 * risk_per_share
                    )
                    if side == "LONG" and trail_candidate > managed_stop:
                        managed_stop = trail_candidate
                        trailing_state = "TRAILING_ACTIVE"
                        secured_profit = 1
                    elif side == "SHORT" and trail_candidate < managed_stop:
                        managed_stop = trail_candidate
                        trailing_state = "TRAILING_ACTIVE"
                        secured_profit = 1
                reason = None
                if evaluation_objective == TRADE_MAP_OVERLAY_OBJECTIVE:
                    if side == "LONG":
                        reason = "TARGET_HIT" if price >= float(row["target_price"]) else (
                            "TRAILING_STOP_HIT" if price <= managed_stop and trailing_state == "TRAILING_ACTIVE" else
                            "SECURE_PROFIT_STOP" if price <= managed_stop and secured_profit else
                            "STOP_HIT" if price <= managed_stop else None
                        )
                    else:
                        reason = "TARGET_HIT" if price <= float(row["target_price"]) else (
                            "TRAILING_STOP_HIT" if price >= managed_stop and trailing_state == "TRAILING_ACTIVE" else
                            "SECURE_PROFIT_STOP" if price >= managed_stop and secured_profit else
                            "STOP_HIT" if price >= managed_stop else None
                        )
                if reason is None and horizon_exit and mark_at >= horizon_exit:
                    reason = "SESSION_EXIT" if row["mode"] == "intraday" else "HORIZON_EXIT"
                if reason is None and session_due:
                    reason = "HORIZON_EXIT"
                estimate = self.costs.round_trip(
                    row["mode"], side, float(row["entry_price"]), price, int(row["quantity"]),
                    exchange=str(venue["exchange"]), bse_group=venue["bse_group"],
                )
                updated = mark_at.isoformat(timespec="seconds").replace("+00:00", "Z")
                signed_move_bps = (
                    (price - entry) / entry * 10_000.0
                    if side == "LONG"
                    else (entry - price) / entry * 10_000.0
                )
                mfe = max(float(row.get("mfe_bps") or 0.0), signed_move_bps)
                mae = min(float(row.get("mae_bps") or 0.0), signed_move_bps)
                opened_at = self._timestamp(row.get("opened_at")) or mark_at
                holding_seconds = max(0, int((mark_at - opened_at).total_seconds()))
                stresses = {
                    bps: self._stress_pnl(
                        float(estimate["net_pnl"]),
                        entry=entry,
                        exit_price=price,
                        quantity=int(row["quantity"]),
                        extra_bps_each_side=bps,
                    )
                    for bps in (5, 10, 20)
                }
                if reason:
                    outcome = "WIN" if float(estimate["net_pnl"]) > 0 else (
                        "LOSS" if float(estimate["net_pnl"]) < 0 else "BREAKEVEN"
                    )
                    self.store.conn.execute(
                        """UPDATE quant_evaluation_positions
                           SET status='CLOSED',last_price=?,exit_price=?,managed_stop=?,
                               trailing_state=?,secured_profit=?,gross_pnl=?,total_cost=?,net_pnl=?,net_pnl_stress_5bps=?,
                               net_pnl_stress_10bps=?,net_pnl_stress_20bps=?,
                               mfe_bps=?,mae_bps=?,holding_seconds=?,outcome=?,exit_reason=?,
                               updated_at=?,closed_at=?,unscorable=0 WHERE position_id=?""",
                        (
                            price, price, managed_stop, trailing_state, secured_profit,
                            estimate["gross_pnl"], estimate["costs"]["total"], estimate["net_pnl"],
                            stresses[5], stresses[10], stresses[20],
                            mfe, mae, holding_seconds, outcome, reason, updated, updated,
                            row["position_id"],
                        ),
                    )
                    updates.append({"position_id": row["position_id"], "status": "CLOSED", "reason": reason, "net_pnl": estimate["net_pnl"], "managed_stop": round(managed_stop, 4), "trailing_state": trailing_state, "secured_profit": bool(secured_profit)})
                else:
                    self.store.conn.execute(
                        """UPDATE quant_evaluation_positions
                           SET last_price=?,managed_stop=?,trailing_state=?,secured_profit=?,
                               gross_pnl=?,total_cost=?,net_pnl=?,net_pnl_stress_5bps=?,net_pnl_stress_10bps=?,
                               net_pnl_stress_20bps=?,mfe_bps=?,mae_bps=?,
                               holding_seconds=?,updated_at=?
                           WHERE position_id=?""",
                        (
                            price, managed_stop, trailing_state, secured_profit,
                            estimate["gross_pnl"], estimate["costs"]["total"], estimate["net_pnl"],
                            stresses[5], stresses[10], stresses[20],
                            mfe, mae, holding_seconds, updated, row["position_id"],
                        ),
                    )
                    updates.append({"position_id": row["position_id"], "status": "OPEN", "net_pnl": estimate["net_pnl"], "managed_stop": round(managed_stop, 4), "trailing_state": trailing_state, "secured_profit": bool(secured_profit)})
            self.store.conn.commit()
        pending_admissions = self._recheck_pending_bootstrap_admissions(quotes)
        return {"ok": True, "updated": updates, "pending_admissions": pending_admissions, "broker_orders": False}

    def positions(self, *, mode: Optional[str] = None, limit: int = 100) -> list[Dict[str, Any]]:
        """Return the durable automatic-paper book for decision-workstation use."""
        params: list[Any] = []
        where = ""
        if mode and str(mode).lower() in {"intraday", "delivery"}:
            where = "WHERE mode=?"
            params.append(require_production_mode(mode))
        params.append(max(1, min(int(limit), 500)))
        rows = self.store.conn.execute(
            f"""SELECT * FROM quant_evaluation_positions {where}
                ORDER BY CASE status WHEN 'OPEN' THEN 0 ELSE 1 END,
                         opened_at DESC,position_id DESC LIMIT ?""",
            tuple(params),
        ).fetchall()
        output = []
        for raw in rows:
            row = dict(raw)
            payload = _json_object(row.get("payload_json"))
            row["payload"] = payload
            row["prediction_state"] = row.get("model_state")
            row["decision_weight"] = float(payload.get("decision_weight") or 0.0)
            row["broker_execution_weight"] = 0.0
            row["paper_trade"] = True
            output.append(row)
        return output

    def admission_diagnostics(
        self, *, mode: Optional[str] = None, limit: int = 20
        ) -> list[Dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if mode and str(mode).lower() in {"intraday", "delivery"}:
            where = "WHERE mode=?"
            params.append(require_production_mode(mode))
        params.append(max(1, min(int(limit), 100)))
        rows = self.store.conn.execute(
            f"""SELECT prediction_id,symbol,mode,model_id,model_state,
                       normalized_score,observed_at,payload_json
                  FROM quant_paper_predictions {where}
                 ORDER BY observed_at DESC,prediction_id DESC LIMIT ?""",
            tuple(params),
        ).fetchall()
        output = []
        for raw in rows:
            row = dict(raw)
            payload = _json_object(row.pop("payload_json", "{}"))
            admission = payload.get("automatic_paper_admission")
            if not isinstance(admission, Mapping):
                continue
            blockers = admission.get("blockers")
            if not isinstance(blockers, list):
                blockers = [admission.get("reason")] if admission.get("reason") else []
            output.append({
                **row,
                "prediction_state": row.get("model_state"),
                "admission_state": admission.get("state"),
                "blockers": [str(value) for value in blockers if value],
                "broker_execution_weight": 0.0,
            })
        return output

    def status(self, *, reconcile: bool = False) -> Dict[str, Any]:
        virtual_reconciliation = self.reconcile_virtual_outcomes()
        if reconcile:
            self.reconcile_latest()
        events = self.store.conn.execute(
            """SELECT * FROM quant_paper_activation_ledger
               ORDER BY created_at DESC,event_id DESC"""
        ).fetchall()
        latest_by_model: Dict[str, Dict[str, Any]] = {}
        for raw in events:
            row = dict(raw)
            latest_by_model.setdefault(str(row["model_id"]), row)
        models = []
        for row in latest_by_model.values():
            gates = _json_object(row.get("gate_json"))
            canonical_default = str(row["horizon"]) == _default_model_horizon(row["mode"])
            models.append({
                "model_id": row["model_id"],
                "mode": row["mode"],
                "horizon": row["horizon"],
                "state": row["state"],
                "prediction_state": row["state"],
                "decision_weight": float(row["paper_weight"]),
                "broker_execution_weight": 0.0,
                "gates": gates.get("gates") or {},
                "predecessor_model_id": row.get("predecessor_model_id"),
                "event_hash": row["event_hash"],
                "created_at": row["created_at"],
                "canonical_default_horizon": canonical_default,
            })
        canonical_models = [
            model for model in models if model["canonical_default_horizon"]
        ]
        if bool(getattr(self.store, "production_model_governance_required", False)):
            for model in models:
                model["legacy_state"] = model.get("state")
                model["state"] = MODEL_UNAVAILABLE
                model["prediction_state"] = MODEL_UNAVAILABLE
                model["decision_weight"] = 0.0
                model["reason"] = "Legacy SQLite lifecycle has zero production authority in v68."
            canonical_models = [model for model in models if model["canonical_default_horizon"]]
        aggregate = dict(self.store.conn.execute(
            """SELECT
                 COUNT(*) AS positions,
                 SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_positions,
                 SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) AS closed_positions,
                 SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                 SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS losses,
                 COALESCE(SUM(CASE WHEN status='CLOSED' THEN net_pnl ELSE 0 END),0) AS realized_net_pnl,
                 COALESCE(SUM(CASE WHEN status='CLOSED' THEN net_pnl_stress_5bps ELSE 0 END),0) AS realized_net_pnl_stress_5bps,
                 COALESCE(SUM(CASE WHEN status='CLOSED' THEN net_pnl_stress_10bps ELSE 0 END),0) AS realized_net_pnl_stress_10bps,
                 COALESCE(SUM(CASE WHEN status='CLOSED' THEN net_pnl_stress_20bps ELSE 0 END),0) AS realized_net_pnl_stress_20bps,
                 COALESCE(SUM(CASE WHEN status='OPEN' THEN net_pnl ELSE 0 END),0) AS open_mtm_net_pnl,
                 COALESCE(SUM(CASE WHEN status='OPEN' THEN notional+reserved_cost ELSE 0 END),0) AS deployed,
                 AVG(CASE WHEN status='CLOSED' THEN mfe_bps END) AS average_mfe_bps,
                 AVG(CASE WHEN status='CLOSED' THEN mae_bps END) AS average_mae_bps,
                 AVG(CASE WHEN status='CLOSED' THEN holding_seconds END) AS average_holding_seconds,
                 SUM(CASE WHEN data_failure=1 THEN 1 ELSE 0 END) AS data_failures,
                 SUM(CASE WHEN unscorable=1 THEN 1 ELSE 0 END) AS unscorable
               FROM quant_evaluation_positions"""
        ).fetchone())
        scored = int(self.store.conn.execute(
            "SELECT COUNT(*) FROM quant_paper_predictions WHERE prediction_state='SCORED'"
        ).fetchone()[0])
        strategy_books = []
        for raw in self.store.conn.execute(
            """SELECT
                 model_id,mode,horizon,evaluation_objective,
                 COUNT(*) AS positions,
                 SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_positions,
                 SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) AS closed_positions,
                 SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                 SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS losses,
                 COALESCE(SUM(CASE WHEN status='CLOSED' THEN net_pnl ELSE 0 END),0)
                   AS realized_net_pnl,
                 COALESCE(SUM(CASE WHEN status='OPEN' THEN net_pnl ELSE 0 END),0)
                   AS open_mtm_net_pnl,
                 COALESCE(SUM(CASE WHEN status='CLOSED' AND net_pnl>0 THEN net_pnl ELSE 0 END),0)
                   AS gross_profit,
                 COALESCE(-SUM(CASE WHEN status='CLOSED' AND net_pnl<0 THEN net_pnl ELSE 0 END),0)
                   AS gross_loss,
                 COALESCE(SUM(CASE WHEN status='OPEN' THEN notional+reserved_cost ELSE 0 END),0)
                   AS deployed
               FROM quant_evaluation_positions
               GROUP BY model_id,mode,horizon,evaluation_objective
               ORDER BY model_id,horizon,evaluation_objective"""
        ).fetchall():
            row = dict(raw)
            settled = int(row.get("closed_positions") or 0)
            row["win_rate_pct"] = (
                round(int(row.get("wins") or 0) / settled * 100.0, 2)
                if settled else None
            )
            loss = float(row.get("gross_loss") or 0.0)
            row["profit_factor"] = (
                round(float(row.get("gross_profit") or 0.0) / loss, 4)
                if loss > 0 else None
            )
            latest_allocation = self.store.conn.execute(
                """SELECT risk_scale,strategy_cap,allocated_cash,created_at
                   FROM quant_capital_allocation_ledger WHERE model_id=?
                   ORDER BY created_at DESC,allocation_id DESC LIMIT 1""",
                (row["model_id"],),
            ).fetchone()
            row["latest_capital_allocation"] = (
                dict(latest_allocation) if latest_allocation else None
            )
            strategy_books.append(row)
        virtual_aggregate = dict(self.store.conn.execute(
            """SELECT
                 COUNT(*) AS selected,
                 SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) AS pending,
                 SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END) AS settled,
                 SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                 SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS losses,
                 AVG(CASE WHEN status='SETTLED' THEN net_return_bps END)
                   AS mean_net_return_bps,
                 AVG(CASE WHEN status='SETTLED' THEN net_return_plus_20bps END)
                   AS mean_net_return_plus_20bps
               FROM quant_virtual_model_outcomes"""
        ).fetchone())
        virtual_books = []
        for raw in self.store.conn.execute(
            """SELECT model_id,mode,horizon,
                      COUNT(*) AS selected,
                      SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) AS pending,
                      SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END) AS settled,
                      SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS losses,
                      AVG(CASE WHEN status='SETTLED' THEN net_return_bps END)
                        AS mean_net_return_bps,
                      AVG(CASE WHEN status='SETTLED' THEN net_return_plus_20bps END)
                        AS mean_net_return_plus_20bps
                 FROM quant_virtual_model_outcomes
                GROUP BY model_id,mode,horizon
                ORDER BY model_id,horizon"""
        ).fetchall():
            item = dict(raw)
            settled_virtual = int(item.get("settled") or 0)
            item["win_rate_after_20bps_pct"] = (
                round(int(item.get("wins") or 0) / settled_virtual * 100.0, 2)
                if settled_virtual else None
            )
            virtual_books.append(item)
        closed = int(aggregate.get("closed_positions") or 0)
        wins = int(aggregate.get("wins") or 0)
        return {
            "ok": True,
            "version": SERVICE_VERSION,
            "rule_id": RULE_ID,
            "automatic_simulation_activation": True,
            "human_approval_required_for_simulation": False,
            "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
            "forward_edge_claim_gate": {
                "state": "COLLECTING",
                "required": "predeclared independent forward-paper sample plus drift review",
                "prediction_active_is_not_profit_certification": True,
            },
            "prediction_states": [PREDICTION_ACTIVE, MODEL_UNAVAILABLE],
            "prediction_state": (
                PREDICTION_ACTIVE
                if any(model.get("state") == PREDICTION_ACTIVE for model in canonical_models)
                else MODEL_UNAVAILABLE
            ),
            "decision_weight": max(
                (
                    float(model.get("decision_weight") or 0.0)
                    for model in canonical_models
                    if model.get("state") == PREDICTION_ACTIVE
                ),
                default=0.0,
            ),
            "isolated_from_canonical_model_paper": True,
                "prediction_influence": any(
                model.get("state") == PREDICTION_ACTIVE
                and float(model.get("decision_weight") or 0.0) > 0
                for model in canonical_models
            ),
            "broker_execution_weight": 0.0,
            "broker_order_authority": "NONE",
            "automatic_paper_decision_mutation": any(
                model.get("state") == PREDICTION_ACTIVE
                and float(model.get("decision_weight") or 0.0) > 0
                for model in canonical_models
            ),
            "broker_order_mutation": False,
            "models": sorted(models, key=lambda row: (row["mode"], row["horizon"], row["created_at"]), reverse=True),
            "canonical_models": sorted(canonical_models, key=lambda row: (row["mode"], row["created_at"]), reverse=True),
            "strategy_books": strategy_books,
            "virtual_model_evidence": {
                "objective": FIXED_HORIZON_OBJECTIVE,
                "capital_admission_required": False,
                "selection_rule": "WITHIN_SNAPSHOT_TOP_20_PERCENT",
                "selected": int(virtual_aggregate.get("selected") or 0),
                "pending": int(virtual_aggregate.get("pending") or 0),
                "settled": int(virtual_aggregate.get("settled") or 0),
                "wins_after_20bps": int(virtual_aggregate.get("wins") or 0),
                "losses_after_20bps": int(virtual_aggregate.get("losses") or 0),
                "mean_net_return_bps": (
                    round(float(virtual_aggregate["mean_net_return_bps"]), 6)
                    if virtual_aggregate.get("mean_net_return_bps") is not None
                    else None
                ),
                "mean_net_return_plus_20bps": (
                    round(float(virtual_aggregate["mean_net_return_plus_20bps"]), 6)
                    if virtual_aggregate.get("mean_net_return_plus_20bps") is not None
                    else None
                ),
                "models": virtual_books,
                "reconciliation": virtual_reconciliation,
            },
            "historical_capital_replay_gate": {
                "state": "BLOCKED_NOT_IMPLEMENTED",
                "automatic_rank_walk_forward": True,
                "automatic_500k_capital_lifecycle_replay": False,
                "paper_lifecycle_reuse_required": True,
                "blocker": (
                    "Historical fold predictions are not yet persisted with "
                    "point-in-time sector, ADV and admission ordering required "
                    "to replay the same ₹5L allocator without look-ahead."
                ),
            },
            "admission_diagnostics": {
                desk: self.admission_diagnostics(mode=desk, limit=50)
                for desk in ("delivery", "intraday")
            },
            "evaluation_sleeve": {
                "initial_capital": INITIAL_CAPITAL,
                "equity": round(INITIAL_CAPITAL + float(aggregate["realized_net_pnl"] or 0) + float(aggregate["open_mtm_net_pnl"] or 0), 2),
                "free_cash": round(max(0.0, INITIAL_CAPITAL + float(aggregate["realized_net_pnl"] or 0) - float(aggregate["deployed"] or 0)), 2),
                "deployed": round(float(aggregate["deployed"] or 0), 2),
                "predictions_scored": scored,
                "positions": int(aggregate.get("positions") or 0),
                "open_positions": int(aggregate.get("open_positions") or 0),
                "closed_positions": closed,
                "wins": wins,
                "losses": int(aggregate.get("losses") or 0),
                "win_rate_pct": round(wins / closed * 100.0, 2) if closed else None,
                "realized_net_pnl": round(float(aggregate["realized_net_pnl"] or 0), 2),
                "realized_net_pnl_stress_5bps": round(float(aggregate["realized_net_pnl_stress_5bps"] or 0), 2),
                "realized_net_pnl_stress_10bps": round(float(aggregate["realized_net_pnl_stress_10bps"] or 0), 2),
                "realized_net_pnl_stress_20bps": round(float(aggregate["realized_net_pnl_stress_20bps"] or 0), 2),
                "open_mtm_net_pnl": round(float(aggregate["open_mtm_net_pnl"] or 0), 2),
                "average_mfe_bps": (
                    round(float(aggregate["average_mfe_bps"]), 2)
                    if aggregate.get("average_mfe_bps") is not None else None
                ),
                "average_mae_bps": (
                    round(float(aggregate["average_mae_bps"]), 2)
                    if aggregate.get("average_mae_bps") is not None else None
                ),
                "average_holding_seconds": (
                    round(float(aggregate["average_holding_seconds"]), 2)
                    if aggregate.get("average_holding_seconds") is not None else None
                ),
                "data_failure_count": int(aggregate.get("data_failures") or 0),
                "unscorable_count": int(aggregate.get("unscorable") or 0),
                "costs_included": True,
                "stress_scenarios_bps_each_side": [5, 10, 20],
                "mandatory_exit_policy": f"intraday governed mandatory flat at {__import__('core.intraday_session_policy', fromlist=['IntradaySessionPolicy']).IntradaySessionPolicy.mandatory_flat_label()} IST; delivery at verified close of declared NSE-session horizon",
                "isolated_from_canonical_model_paper": True,
                "prediction_influence": any(
                    model.get("state") == PREDICTION_ACTIVE
                    and float(model.get("decision_weight") or 0.0) > 0
                    for model in canonical_models
                ),
            },
            "holdout_consumptions": [
                dict(row) for row in self.store.conn.execute(
                    """SELECT * FROM quant_holdout_consumption_ledger
                       ORDER BY consumed_at DESC"""
                ).fetchall()
            ],
        }
