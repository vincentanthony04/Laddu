"""Paper admission, portfolio risk and selection-population processing."""
from __future__ import annotations

from core.quant_paper_dependencies import *  # noqa: F401,F403


class QuantPaperPortfolioMixin:
    def reconcile_virtual_outcomes(
        self, *, candidate_id: Optional[str] = None
        ) -> Dict[str, Any]:
        label_table = self.store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quant_label_vectors'"
        ).fetchone()
        if not label_table:
            return {"ok": True, "settled": 0, "state": "NO_LABEL_LEDGER"}
        where = "AND v.candidate_id=?" if candidate_id else ""
        params = (str(candidate_id),) if candidate_id else ()
        rows = self.store.conn.execute(
            f"""SELECT v.virtual_id,l.net_return_bps,l.net_return_plus_20bps,
                       l.settled_at,l.record_hash
                  FROM quant_virtual_model_outcomes v
                  JOIN quant_label_vectors l
                    ON l.candidate_id=v.candidate_id AND l.horizon=v.horizon
                 WHERE v.status='PENDING' {where}
                 ORDER BY v.selected_at,v.virtual_id""",
            params,
        ).fetchall()
        if not rows:
            return {"ok": True, "settled": 0, "state": "NO_DUE_OUTCOMES"}
        updated = _now()
        with self.store.write_lock:
            for raw in rows:
                row = dict(raw)
                stressed = float(row["net_return_plus_20bps"])
                outcome = "WIN" if stressed > 0 else "LOSS" if stressed < 0 else "BREAKEVEN"
                self.store.conn.execute(
                    """UPDATE quant_virtual_model_outcomes
                          SET status='SETTLED',net_return_bps=?,
                              net_return_plus_20bps=?,outcome=?,settled_at=?,
                              label_record_hash=?,updated_at=?
                        WHERE virtual_id=? AND status='PENDING'""",
                    (
                        float(row["net_return_bps"]), stressed, outcome,
                        row["settled_at"], row["record_hash"], updated,
                        row["virtual_id"],
                    ),
                )
            self.store.conn.commit()
        return {"ok": True, "settled": len(rows), "state": "RECONCILED"}

    def _open_metrics(self) -> Dict[str, Any]:
        open_rows = [
            dict(row) for row in self.store.conn.execute(
                "SELECT * FROM quant_evaluation_positions WHERE status='OPEN'"
            ).fetchall()
        ]
        realized = self.store.conn.execute(
            "SELECT COALESCE(SUM(net_pnl),0) FROM quant_evaluation_positions WHERE status='CLOSED'"
        ).fetchone()[0]
        closed_pnl = [
            float(row[0] or 0.0)
            for row in self.store.conn.execute(
                """SELECT net_pnl FROM quant_evaluation_positions
                   WHERE status='CLOSED' ORDER BY closed_at,position_id"""
            ).fetchall()
        ]
        running_equity = INITIAL_CAPITAL
        equity_peak = INITIAL_CAPITAL
        max_drawdown_pct = 0.0
        for pnl in closed_pnl:
            running_equity += pnl
            equity_peak = max(equity_peak, running_equity)
            if equity_peak > 0:
                max_drawdown_pct = max(
                    max_drawdown_pct,
                    (equity_peak - running_equity) / equity_peak * 100.0,
                )
        equity = INITIAL_CAPITAL + float(realized or 0.0)
        deployed = sum(float(row["notional"]) + float(row["reserved_cost"]) for row in open_rows)
        open_mtm = sum(float(row["net_pnl"] or 0.0) for row in open_rows)
        marked_equity = equity + open_mtm
        current_drawdown_pct = (
            max(0.0, (equity_peak - marked_equity) / equity_peak * 100.0)
            if equity_peak > 0 else 100.0
        )
        return {
            "rows": open_rows,
            "equity": equity,
            "marked_equity": marked_equity,
            "equity_peak": equity_peak,
            "current_drawdown_pct": current_drawdown_pct,
            "max_drawdown_pct": max(max_drawdown_pct, current_drawdown_pct),
            "free_cash": max(0.0, equity - deployed),
            "intraday_used": sum(
                float(row["notional"]) + float(row["reserved_cost"])
                for row in open_rows if row["mode"] == "intraday"
            ),
            "open_risk": sum(float(row["open_risk"]) for row in open_rows),
        }

    def _strategy_metrics(self, model_id: str) -> Dict[str, Any]:
        row = dict(self.store.conn.execute(
            """SELECT
                 COUNT(*) AS positions,
                 SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_positions,
                 SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) AS closed_trades,
                 SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                 SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS losses,
                 COALESCE(SUM(CASE WHEN status='CLOSED' THEN net_pnl ELSE 0 END),0)
                   AS realized_net_pnl,
                 COALESCE(SUM(CASE WHEN status='CLOSED' AND net_pnl>0 THEN net_pnl ELSE 0 END),0)
                   AS gross_profit,
                 COALESCE(-SUM(CASE WHEN status='CLOSED' AND net_pnl<0 THEN net_pnl ELSE 0 END),0)
                   AS gross_loss,
                 COALESCE(SUM(CASE WHEN status='OPEN' THEN notional+reserved_cost ELSE 0 END),0)
                   AS open_capital
               FROM quant_evaluation_positions WHERE model_id=?""",
            (str(model_id),),
        ).fetchone())
        loss = float(row.get("gross_loss") or 0.0)
        profit = float(row.get("gross_profit") or 0.0)
        row["profit_factor"] = (
            profit / loss if loss > 0 else (None if profit <= 0 else float("inf"))
        )
        return row

    def _capital_plan(self, model_id: str, metrics: Mapping[str, Any]) -> Dict[str, Any]:
        strategy = self._strategy_metrics(model_id)
        scale = _paper_risk_scale(
            closed_trades=int(strategy.get("closed_trades") or 0),
            realized_net_pnl=float(strategy.get("realized_net_pnl") or 0.0),
            gross_profit=float(strategy.get("gross_profit") or 0.0),
            gross_loss=float(strategy.get("gross_loss") or 0.0),
            portfolio_drawdown_pct=float(metrics.get("current_drawdown_pct") or 0.0),
        )
        equity = max(0.0, float(metrics.get("equity") or 0.0))
        strategy_cap_pct = 0.0 if scale <= 0 else 0.10 + 0.25 * scale
        strategy_cap = equity * strategy_cap_pct
        open_capital = float(strategy.get("open_capital") or 0.0)
        available = min(
            max(0.0, float(metrics.get("free_cash") or 0.0)),
            max(0.0, strategy_cap - open_capital),
        )
        return {
            "model_id": str(model_id),
            "risk_scale": scale,
            "strategy_cap_pct": round(strategy_cap_pct * 100.0, 2),
            "strategy_cap": round(strategy_cap, 2),
            "strategy_open_capital": round(open_capital, 2),
            "available_cash": round(available, 2),
            "portfolio_drawdown_pct": round(
                float(metrics.get("current_drawdown_pct") or 0.0), 4
            ),
            "closed_trades": int(strategy.get("closed_trades") or 0),
            "realized_net_pnl": round(
                float(strategy.get("realized_net_pnl") or 0.0), 2
            ),
            "gross_profit": round(float(strategy.get("gross_profit") or 0.0), 2),
            "gross_loss": round(float(strategy.get("gross_loss") or 0.0), 2),
            "profit_factor": strategy.get("profit_factor"),
            "automatic": True,
            "manual_approval_required": False,
        }

    def _maybe_open_evaluation(
        self,
        model: Mapping[str, Any],
        event: Mapping[str, Any],
        candidate: Mapping[str, Any],
        prediction: Mapping[str, Any],
        ) -> Dict[str, Any]:
        score = float(prediction["normalized_score"])
        mode = str(model["mode"])
        requested_side = str(candidate.get("side") or candidate.get("direction") or "").upper()
        selected_for_top_cohort = bool(
            prediction.get("selected_for_top_cohort")
            if prediction.get("selected_for_top_cohort") is not None
            else score >= LONG_SCORE_THRESHOLD
        )
        model_side = _selector_signal_side(
            requested_side=requested_side,
            selected_for_top_cohort=selected_for_top_cohort,
            mode=mode,
        )
        if not model_side:
            return {
                "state": "NO_MODEL_SIGNAL",
                "reason": (
                    "ranker did not select this candidate's declared side"
                    if requested_side in {"LONG", "SHORT"}
                    else "candidate side is missing or unsupported"
                ),
                "top_cohort_required": True,
                "candidate_side": requested_side or None,
            }
        symbol = str(candidate.get("symbol") or candidate.get("stock") or "").upper().strip()
        try:
            venue = self.costs.venue_identity(
                exchange=str(candidate.get("exchange") or ""),
                bse_group=str(candidate.get("bse_group") or "").strip().upper() or None,
            )
            venue_blocker = None
        except ValueError as exc:
            venue = {"exchange": None, "bse_group": None}
            venue_blocker = str(exc)
        identity = candidate.get("identity_verified") in (True, 1)
        freshness = str(
            candidate.get("quote_freshness_state")
            or candidate.get("price_freshness_state")
            or candidate.get("freshness_state")
            or ""
        ).lower()
        closed_market_reference = bool(
            freshness == "closed_market"
            and candidate.get("execution_price_authority") is False
        )
        stale = bool(
            candidate.get("stale") is True
            or (candidate.get("usable_for_promotion") is False and not closed_market_reference)
        )
        market_price = _number(candidate.get("ltp") if candidate.get("ltp") is not None else candidate.get("current_price"))
        entry = _number(candidate.get("planned_entry") if candidate.get("planned_entry") is not None else candidate.get("entry")) or market_price
        target = _number(
            candidate.get("planned_t1")
            if candidate.get("planned_t1") is not None
            else candidate.get("target") if candidate.get("target") is not None else candidate.get("t1")
        )
        stop = _number(
            candidate.get("planned_sl")
            if candidate.get("planned_sl") is not None
            else candidate.get("sl") if candidate.get("sl") is not None else candidate.get("stop")
        )
        blockers = []
        if not symbol or not identity:
            blockers.append("verified cash-equity identity required")
        if venue_blocker:
            blockers.append(f"venue cost identity blocked: {venue_blocker}")
        if freshness not in {"live", "fresh", "live_current"} or stale:
            if freshness == "closed_market" and not stale:
                blockers.append("market closed; fresh executable quote required at next session")
            else:
                blockers.append("fresh executable quote required")
        if not all(value is not None and value > 0 for value in (market_price, entry, target, stop)):
            blockers.append("positive entry/target/stop required")
        elif model_side == "LONG" and not (stop < entry < target):
            blockers.append("invalid LONG trade map")
        elif model_side == "SHORT" and not (target < entry < stop):
            blockers.append("invalid SHORT trade map")
        if mode == "delivery" and model_side == "SHORT":
            blockers.append("delivery short unsupported")
        if market_price and entry:
            if not _entry_crossed(
                model_side, market_price=float(market_price), entry=float(entry)
            ):
                blockers.append("waiting for directional entry trigger")
            elif abs(market_price - entry) / entry > 0.0075:
                blockers.append("entry chase exceeds 0.75%")
        if blockers:
            return {"state": "SIGNAL_RECORDED_NOT_OPENED", "blockers": blockers}
        opened_dt = datetime.now(timezone.utc)
        opened = opened_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        horizon = str(model.get("horizon") or _default_model_horizon(mode))
        evaluation_objective = str(
            candidate.get("_evaluation_objective") or FIXED_HORIZON_OBJECTIVE
        )
        horizon_sessions = 0
        last_session_date = opened_dt.astimezone(INDIA_TZ).date().isoformat()
        if mode == "intraday" or horizon in {"eod", "session"}:
            local = opened_dt.astimezone(INDIA_TZ)
            horizon_exit = _intraday_horizon_exit(opened_dt)
            if horizon_exit <= local:
                # A new intraday evaluation signal after the cutoff is not
                # opened; this avoids creating a position whose mandatory
                # end-of-day exit is already in the past.
                return {
                    "state": "SIGNAL_RECORDED_NOT_OPENED",
                    "blockers": ["intraday model-paper entry cutoff passed"],
                }
            horizon_exit = horizon_exit.astimezone(timezone.utc)
            horizon_exit_text = horizon_exit.isoformat(timespec="seconds").replace("+00:00", "Z")
        else:
            try:
                horizon_sessions = max(1, int(horizon.rstrip("d")))
            except (TypeError, ValueError):
                horizon_sessions = 20
            # Delivery horizons are counted from verified NSE session dates
            # observed by mark_quotes. A calendar timestamp would close early
            # across weekends and exchange holidays.
            horizon_exit_text = ""
        position_id = _sha({
            "prediction_id": prediction["prediction_id"],
            "model_id": model["model_id"],
            "symbol": symbol,
        }, 40)
        paper_active = bool(
            event.get("state") == PREDICTION_ACTIVE
            and float(event.get("paper_weight") or 0.0) > 0.0
        )
        payload = {
            "candidate_id": candidate.get("candidate_id"),
            "exchange": venue["exchange"],
            "bse_group": venue["bse_group"],
            "canonical_rank_score": candidate.get("rank_score") or candidate.get("score"),
            "model_score": score,
            "paper_authority": event["state"],
            "paper_weight": float(event.get("paper_weight") or 0.0),
            "prediction_active": paper_active,
            "decision_weight": float(event.get("paper_weight") or 0.0),
            "broker_orders": False,
            "selector_rule": "DECLARED_SIDE_TOP_COHORT",
            "selected_for_top_cohort": selected_for_top_cohort,
            "horizon_unit": "NSE_SESSIONS" if horizon_sessions else "INTRADAY_SESSION",
            "evaluation_objective": evaluation_objective,
            "trade_map_levels_are_exit_authority": (
                evaluation_objective == TRADE_MAP_OVERLAY_OBJECTIVE
            ),
        }
        with self.store.write_lock:
            metrics = self._open_metrics()
            if len(metrics["rows"]) >= 10:
                return {
                    "state": "SIGNAL_RECORDED_NOT_OPENED",
                    "blockers": ["evaluation sleeve maximum open positions"],
                }
            duplicate = self.store.conn.execute(
                """SELECT position_id FROM quant_evaluation_positions
                   WHERE model_id=? AND symbol=? AND status='OPEN'""",
                (model["model_id"], symbol),
            ).fetchone()
            if duplicate:
                return {"state": "ALREADY_OPEN", "position_id": duplicate[0]}
            try:
                risk_table = self.store.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='production_risk_state'"
                ).fetchone()
                if risk_table:
                    stopped = self.store.conn.execute(
                        "SELECT operator_stop FROM production_risk_state WHERE singleton_id=1"
                    ).fetchone()
                    if stopped and bool(stopped[0]):
                        return {
                            "state": "SIGNAL_RECORDED_NOT_OPENED",
                            "blockers": ["operator kill switch"],
                        }
            except Exception:
                return {
                    "state": "SIGNAL_RECORDED_NOT_OPENED",
                    "blockers": ["risk authority unavailable"],
                }
            capital_plan = self._capital_plan(str(model["model_id"]), metrics)
            if float(capital_plan["risk_scale"]) <= 0.0:
                return {
                    "state": "SIGNAL_RECORDED_NOT_OPENED",
                    "blockers": ["dynamic capital allocator suspended new risk"],
                    "capital_plan": capital_plan,
                }
            symbol_used = sum(
                float(row["notional"])
                for row in metrics["rows"] if row["symbol"] == symbol
            )
            sector = str(
                candidate.get("sector") or candidate.get("sector_label") or ""
            ).strip()
            sector_key = sector.casefold()
            adv_evidence = _candidate_adv_evidence(
                candidate, market_price=float(market_price)
            )
            if not sector:
                return {
                    "state": "SIGNAL_RECORDED_NOT_OPENED",
                    "blockers": ["verified sector required for concentration cap"],
                    "capital_plan": capital_plan,
                }
            if not adv_evidence["verified"]:
                return {
                    "state": "SIGNAL_RECORDED_NOT_OPENED",
                    "blockers": [
                        "fresh verified 20-session average daily traded value required"
                    ],
                    "capital_plan": capital_plan,
                    "adv_evidence": adv_evidence,
                }
            avg_daily_value = float(adv_evidence["value"])
            sector_used = 0.0
            for row in metrics["rows"]:
                row_payload = _json_object(row.get("payload_json"))
                row_sector = str(
                    row_payload.get("sector")
                    or row_payload.get("sector_label")
                    or ""
                ).strip()
                if row_sector.casefold() == sector_key:
                    sector_used += float(row["notional"])
            sizing = self.risk.size(
                mode=mode,
                side=model_side,
                exchange=str(venue["exchange"]),
                bse_group=venue["bse_group"],
                entry=float(market_price),
                stop=float(stop),
                free_cash=float(capital_plan["available_cash"]),
                intraday_used=metrics["intraday_used"],
                symbol_used=symbol_used,
                sector_used=sector_used,
                open_risk=metrics["open_risk"],
                avg_daily_value=avg_daily_value,
                equity=metrics["equity"],
                risk_scale=float(capital_plan["risk_scale"]),
            )
            if int(sizing.get("quantity") or 0) < 1:
                return {
                    "state": "SIGNAL_RECORDED_NOT_OPENED",
                    "blockers": ["capital/risk/liquidity sizing returned zero"],
                    "sizing": sizing,
                    "capital_plan": capital_plan,
                }
            allocation_id = _sha({
                "prediction_id": prediction["prediction_id"],
                "model_id": model["model_id"],
                "opened_at": opened,
                "sizing": sizing,
            }, 40)
            payload["sizing"] = sizing
            payload["capital_plan"] = capital_plan
            payload["allocation_id"] = allocation_id
            payload["sector"] = sector
            payload["avg_daily_value"] = avg_daily_value
            payload["avg_daily_value_evidence"] = adv_evidence
            try:
                self.store.conn.execute("SAVEPOINT quant_evaluation_admission")
                self.store.conn.execute(
                """INSERT INTO quant_capital_allocation_ledger(
                   allocation_id,prediction_id,model_id,mode,equity,
                   portfolio_drawdown_pct,closed_trades,realized_net_pnl,
                   profit_factor,risk_scale,strategy_cap,strategy_open_capital,
                   allocated_cash,sizing_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    allocation_id, prediction["prediction_id"], model["model_id"],
                    mode, float(metrics["equity"]),
                    float(capital_plan["portfolio_drawdown_pct"]),
                    int(capital_plan["closed_trades"]),
                    float(capital_plan["realized_net_pnl"]),
                    _number(capital_plan.get("profit_factor")),
                    float(capital_plan["risk_scale"]),
                    float(capital_plan["strategy_cap"]),
                    float(capital_plan["strategy_open_capital"]),
                    float(sizing["required_cash"]), _canonical(sizing), opened,
                ),
                )
                self.store.conn.execute(
                """INSERT INTO quant_evaluation_positions(
                   position_id,prediction_id,model_id,model_state,symbol,exchange,bse_group,mode,side,
                   status,quantity,entry_price,target_price,stop_price,managed_stop,
                   trailing_state,secured_profit,last_price,notional,reserved_cost,open_risk,
                   opened_at,updated_at,cost_version,
                   evaluation_objective,horizon,horizon_exit_at,horizon_sessions_required,
                   horizon_sessions_observed,last_session_date,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    position_id, prediction["prediction_id"], model["model_id"],
                    event["state"], symbol, venue["exchange"], venue["bse_group"], mode, model_side, "OPEN",
                    int(sizing["quantity"]), float(market_price), float(target), float(stop),
                    float(stop), "ORIGINAL_STOP", 0, float(market_price), float(sizing["notional"]),
                    float(sizing["cost_reserve"]), float(sizing["risk_cash"]),
                    opened, opened, self.costs.schedule.version,
                    evaluation_objective, horizon,
                    horizon_exit_text, horizon_sessions, 0, last_session_date,
                    _canonical(payload),
                ),
                )
                self.store.conn.execute("RELEASE SAVEPOINT quant_evaluation_admission")
            except Exception as exc:
                self.store.conn.execute(
                    "ROLLBACK TO SAVEPOINT quant_evaluation_admission"
                )
                self.store.conn.execute(
                    "RELEASE SAVEPOINT quant_evaluation_admission"
                )
                return {
                    "state": "SIGNAL_RECORDED_NOT_OPENED",
                    "blockers": ["atomic capital admission failed"],
                    "reason": str(exc)[:160],
                    "capital_plan": capital_plan,
                }
            self.store.conn.commit()
        return {
            "state": "QUANT_EVALUATION_PAPER_OPENED",
            "position_id": position_id,
            "model_state": event["state"],
            "sizing": sizing,
            "capital_plan": capital_plan,
            "broker_orders": False,
        }

    def evaluate_candidate(self, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        mode = require_production_mode(candidate.get("mode"))
        routed_horizon = _default_model_horizon(mode)
        # Automatic reconciliation is idempotent and is intentionally part of
        # the simulation path; it is not a production promotion endpoint.
        self.reconcile_latest(mode)
        models = [
            row for row in self._latest_models(mode)
            if str(row.get("horizon")) == routed_horizon
        ]
        active_event = self._current_active(mode, routed_horizon)
        selected: list[tuple[Dict[str, Any], Dict[str, Any]]] = []
        if active_event:
            active_model = next(
                (row for row in models if row["model_id"] == active_event["model_id"]),
                None,
            )
            if active_model:
                selected.append((active_model, active_event))
        if models:
            latest = models[0]
            if all(latest["model_id"] != row[0]["model_id"] for row in selected):
                latest_event = self._latest_event(str(latest["model_id"]))
                if latest_event:
                    selected.append((latest, latest_event))
        scores = [
            self._score(model, event, candidate, open_evaluation=False)
            for model, event in selected
        ]
        for score in scores:
            if score.get("state") == "SCORED":
                score["evaluation_trade"] = {
                    "state": "AWAITING_IMMUTABLE_POPULATION_RANK",
                    "reason": (
                        "LambdaRank evaluation opens only after the complete "
                        "same-snapshot population identifies its top quintile"
                    ),
                }
        active_score = next(
            (
                score for score in scores
                if active_event and score.get("model_id") == active_event.get("model_id")
                and score.get("state") == "SCORED"
            ),
            None,
        )
        evidence = _number(candidate.get("evidence_score"))
        if evidence is None:
            evidence = _number(candidate.get("rank_score"))
        if evidence is None:
            evidence = _number(candidate.get("score")) or 0.0
        paper_weight = float(active_event.get("paper_weight") or 0.0) if active_score else 0.0
        paper_rank = (
            (1.0 - paper_weight) * evidence
            + paper_weight * float(active_score["normalized_score"])
            if active_score else evidence
        )
        return {
            "ok": True,
            "state": PREDICTION_ACTIVE if active_score else MODEL_UNAVAILABLE,
            "prediction_state": PREDICTION_ACTIVE if active_score else MODEL_UNAVAILABLE,
            "paper_rank_score": round(max(0.0, min(100.0, paper_rank)), 4),
            "decision_weight": paper_weight,
            "active_model_id": active_score.get("model_id") if active_score else None,
            "scores": scores,
            "broker_execution_weight": 0.0,
            "automatic_paper_decision_mutated": bool(active_score),
            "canonical_final_score_unchanged": not bool(active_score),
            "broker_order_authority": "NONE",
            "routed_horizon": routed_horizon,
        }

    def process_selection_population(
        self,
        *,
        mode: str,
        population_fingerprint: str,
        candidates: Sequence[Mapping[str, Any]],
        quant_predictions: Sequence[Mapping[str, Any]],
        range_predictions: Sequence[Mapping[str, Any]] = (),
        ) -> Dict[str, Any]:
        """Batch the automatic evaluation once after immutable population capture."""
        desk = require_production_mode(mode)
        routed_horizon = _default_model_horizon(desk)
        virtual_reconciliation = self.reconcile_virtual_outcomes()
        reconciled = self.reconcile_latest(desk)
        models = [
            row for row in self._latest_models(desk)
            if str(row.get("horizon")) == routed_horizon
        ]
        active_event = self._current_active(desk, routed_horizon)
        active_model = next(
            (
                row for row in models
                if active_event and row["model_id"] == active_event.get("model_id")
            ),
            None,
        )
        rank_models: list[tuple[Dict[str, Any], Dict[str, Any]]] = []
        if active_model is not None and active_event is not None:
            rank_models.append((active_model, active_event))
        if models:
            latest_model = models[0]
            if all(
                str(item[0]["model_id"]) != str(latest_model["model_id"])
                for item in rank_models
            ):
                latest_event = self._latest_event(str(latest_model["model_id"]))
                if latest_event:
                    rank_models.append((latest_model, latest_event))
        candidates_by_id = {
            str(row.get("candidate_id") or ""): dict(row) for row in candidates
        }
        results = []
        rank_entries: Dict[str, list[tuple[Any, ...]]] = {
            str(model["model_id"]): [] for model, _event in rank_models
        }
        for raw_prediction in quant_predictions:
            prediction = dict(raw_prediction)
            candidate_id = str(prediction.get("candidate_id") or "")
            candidate = candidates_by_id.get(candidate_id)
            if candidate is None:
                continue
            candidate["population_fingerprint"] = str(population_fingerprint)
            bootstrap = self._score_bootstrap(candidate, prediction)
            model_scores = []
            for rank_model, rank_event in rank_models:
                score = self._score(
                    rank_model,
                    rank_event,
                    candidate,
                    open_evaluation=False,
                )
                model_scores.append(score)
            active = next(
                (
                    score for score in model_scores
                    if active_event
                    and score.get("model_id") == active_event.get("model_id")
                ),
                None,
            )
            evidence = _number(candidate.get("evidence_score"))
            if evidence is None:
                evidence = _number(candidate.get("rank_score"))
            if evidence is None:
                evidence = _number(candidate.get("score")) or 0.0
            paper_weight = (
                float(active_event.get("paper_weight") or 0.0)
                if active and active.get("state") == "SCORED" else 0.0
            )
            paper_rank = (
                (1.0 - paper_weight) * evidence
                + paper_weight * float(active["normalized_score"])
                if paper_weight else evidence
            )
            result_row = {
                "candidate_id": candidate_id,
                "symbol": candidate.get("symbol"),
                "bootstrap": bootstrap,
                "active_model": active,
                "model_scores": model_scores,
                "paper_rank_score": round(max(0.0, min(100.0, paper_rank)), 4),
                "prediction_state": PREDICTION_ACTIVE if paper_weight else MODEL_UNAVAILABLE,
                "decision_weight": paper_weight,
                "broker_execution_weight": 0.0,
                "automatic_paper_decision_mutated": bool(paper_weight),
            }
            results.append(result_row)
            for rank_model, rank_event in rank_models:
                score = next(
                    (
                        item for item in model_scores
                        if item.get("model_id") == rank_model.get("model_id")
                    ),
                    None,
                )
                if score is not None:
                    rank_entries[str(rank_model["model_id"])].append(
                        (result_row, candidate, rank_model, rank_event, score)
                    )
        cohort_summary = {}
        for model_id, entries in rank_entries.items():
            scored_entries = [
                item for item in entries if item[4].get("state") == "SCORED"
            ]
            scored_entries.sort(
                key=lambda item: (
                    -float(item[4].get("raw_score") or 0.0),
                    str(item[1].get("symbol") or ""),
                )
            )
            top_count = max(1, math.ceil(len(scored_entries) * 0.20)) if scored_entries else 0
            for index, (_result, candidate, rank_model, rank_event, score) in enumerate(
                scored_entries
            ):
                selected_for_top = index < top_count
                score["selected_for_top_cohort"] = selected_for_top
                score["population_rank"] = index + 1
                score["population_size"] = len(scored_entries)
                score["model_evaluation"] = (
                    self._record_virtual_evaluation(
                        rank_model,
                        rank_event,
                        candidate,
                        {**score, "selected_for_top_cohort": True},
                    )
                    if selected_for_top
                    else {
                        "state": "NOT_SELECTED",
                        "objective": FIXED_HORIZON_OBJECTIVE,
                    }
                )
                score["evaluation_trade"] = (
                    self._maybe_open_evaluation(
                        rank_model,
                        rank_event,
                        candidate,
                        {**score, "selected_for_top_cohort": True},
                    )
                    if selected_for_top
                    else {
                        "state": "NO_MODEL_SIGNAL",
                        "reason": "outside same-snapshot top quintile",
                    }
                )
                if score.get("prediction_id"):
                    self._persist_admission_result(
                        str(score["prediction_id"]), score["evaluation_trade"]
                    )
            cohort_summary[model_id] = {
                "population_scored": len(scored_entries),
                "top_quintile_count": top_count,
                "selection_rule": "WITHIN_SNAPSHOT_TOP_20_PERCENT",
                "automatic_paper_open": True,
            }
        range_results = []
        if desk == "delivery":
            for raw_prediction in range_predictions or ():
                prediction = dict(raw_prediction or {})
                candidate = candidates_by_id.get(str(prediction.get("candidate_id") or ""))
                if candidate is None:
                    continue
                candidate["population_fingerprint"] = str(population_fingerprint)
                range_results.append({
                    "candidate_id": prediction.get("candidate_id"),
                    "symbol": candidate.get("symbol"),
                    "cohort": prediction.get("cohort"),
                    "evaluation": self._score_range_rule(candidate, prediction),
                    "prediction_state": PREDICTION_ACTIVE,
                    "decision_weight": RULE_MODEL_WEIGHT,
                    "broker_execution_weight": 0.0,
                })
        return {
            "ok": True,
            "state": PREDICTION_ACTIVE if results else MODEL_UNAVAILABLE,
            "prediction_state": PREDICTION_ACTIVE if results else MODEL_UNAVAILABLE,
            "decision_weight": RULE_MODEL_WEIGHT if results else 0.0,
            "mode": desk,
            "population_fingerprint": str(population_fingerprint),
            "routed_horizon": routed_horizon,
            "processed": len(results),
            "results": results,
            "rank_model_cohorts": cohort_summary,
            "range_compression": {
                "rule_id": RANGE_COMPRESSION_RULE_ID,
                "processed": len(range_results),
                "results": range_results,
                "prediction_state": PREDICTION_ACTIVE if range_results else MODEL_UNAVAILABLE,
                "decision_weight": RULE_MODEL_WEIGHT if range_results else 0.0,
                "broker_execution_weight": 0.0,
            },
            "model_reconciliation": reconciled,
            "virtual_outcome_reconciliation": virtual_reconciliation,
            "automatic": True,
            "broker_execution_weight": 0.0,
            "broker_order_authority": "NONE",
            "live_production_weight": 0.0,
        }
