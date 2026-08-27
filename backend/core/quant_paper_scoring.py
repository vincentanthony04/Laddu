"""Frozen selector scoring and immutable prediction recording."""
from __future__ import annotations

from core.quant_paper_dependencies import *  # noqa: F401,F403


class QuantPaperScoringMixin:
    def _bootstrap_event(self, mode: str, model_version: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
        desk = require_production_mode(mode)
        horizon = _default_model_horizon(desk)
        specification = {
            "rule_id": "frozen-compact-cross-sectional-bootstrap-1.0.0",
            "source_model_version": str(model_version or "unknown-selector"),
            "mode": desk,
            "horizon": horizon,
            "feature_manifest_hash": FEATURE_MANIFEST_HASH,
            "long_percentile_threshold": LONG_SCORE_THRESHOLD,
            "intraday_short_percentile_threshold": SHORT_SCORE_THRESHOLD,
            "delivery_short_enabled": False,
            "risk_policy": "model-paper-risk-service-current",
            "cost_version": self.costs.schedule.version,
            "paper_weight": RULE_MODEL_WEIGHT,
            "broker_orders": False,
        }
        spec_hash = _sha(specification)
        model_id = "bootstrap-" + _sha({"specification_hash": spec_hash}, 30)
        prior = self._latest_event(model_id)
        if prior:
            return (
                {
                    "model_id": model_id,
                    "model_family": "FROZEN_COMPACT_CROSS_SECTIONAL_SELECTOR",
                    "mode": desk,
                    "horizon": horizon,
                    "dataset_fingerprint": "FORWARD_IMMUTABLE_POPULATION_STREAM",
                    "feature_manifest_hash": FEATURE_MANIFEST_HASH,
                    "trial_count": 1,
                },
                prior,
            )
        created = _now()
        event_payload = {
            "model_id": model_id,
            "mode": desk,
            "horizon": horizon,
            "state": PREDICTION_ACTIVE,
            "paper_weight": RULE_MODEL_WEIGHT,
            "live_production_weight": 0.0,
            "specification_hash": spec_hash,
            "automatic": True,
        }
        event_hash = _sha(event_payload)
        event_id = _sha({"event_hash": event_hash, "created_at": created}, 40)
        gate_report = {
            "forward_evaluation_source": True,
            "immutable_cross_sectional_population_required": True,
            "feature_manifest_locked": True,
            "prediction_active": True,
            "decision_weight": RULE_MODEL_WEIGHT,
            "reason": "deterministic baseline is an active paper strategy; statistical edge remains separately unvalidated",
        }
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT OR IGNORE INTO quant_model_trial_ledger(
                   model_id,model_family,mode,horizon,dataset_fingerprint,
                   feature_manifest_hash,trial_count,rule_id,model_specification_hash,
                   immutable_spec_hash,observed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    model_id, "FROZEN_COMPACT_CROSS_SECTIONAL_SELECTOR", desk, horizon,
                    "FORWARD_IMMUTABLE_POPULATION_STREAM", FEATURE_MANIFEST_HASH, 1,
                    RULE_ID, spec_hash, _sha({"specification": specification}), created,
                ),
            )
            self.store.conn.execute(
                """INSERT INTO quant_paper_activation_ledger(
                   event_id,model_id,mode,horizon,state,paper_weight,
                   live_production_weight,artifact_sha256,dataset_fingerprint,
                   feature_manifest_hash,validation_hash,dependency_hash,
                   projection_id,holdout_start,holdout_end,gate_json,
                   predecessor_model_id,predecessor_event_hash,event_hash,
                   automatic,created_at,service_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, model_id, desk, horizon, PREDICTION_ACTIVE, RULE_MODEL_WEIGHT,
                    0.0, None, "FORWARD_IMMUTABLE_POPULATION_STREAM",
                    FEATURE_MANIFEST_HASH, _sha({"state": "FORWARD_ONLY"}),
                    _sha({"runtime": "stdlib", "cost_version": self.costs.schedule.version}),
                    None, None, None, _canonical({"gates": gate_report, "specification": specification}),
                    None, None, event_hash, 1, created, SERVICE_VERSION,
                ),
            )
            self.store.conn.commit()
        model = {
            "model_id": model_id,
            "model_family": "FROZEN_COMPACT_CROSS_SECTIONAL_SELECTOR",
            "mode": desk,
            "horizon": horizon,
            "dataset_fingerprint": "FORWARD_IMMUTABLE_POPULATION_STREAM",
            "feature_manifest_hash": FEATURE_MANIFEST_HASH,
            "trial_count": 1,
        }
        return model, dict(self._latest_event(model_id) or {})

    def _score_bootstrap(
        self,
        candidate: Mapping[str, Any],
        prediction: Mapping[str, Any],
        ) -> Dict[str, Any]:
        model, event = self._bootstrap_event(
            str(candidate.get("mode") or prediction.get("mode") or ""),
            str(prediction.get("model_version") or ""),
        )
        percentile = _number(prediction.get("percentile"))
        score = _number(prediction.get("score"))
        coverage = _number(prediction.get("feature_coverage")) or 0.0
        if percentile is None or score is None:
            reason = "selection-platform percentile and score are required"
            result = {
                "ok": True,
                "state": "BOOTSTRAP_RANK_UNAVAILABLE",
                "model_id": model["model_id"],
                "feature_coverage": coverage,
                "paper_weight": 0.0,
                "reason": reason,
            }
            # Every immutable candidate must leave an admission record.  The
            # previous early return persisted a prediction but no admission
            # outcome, leaving Operations in PAPER_ADMISSION_PENDING forever.
            output = self._record_prediction(
                model, event, candidate, result, open_evaluation=False,
            )
            output["evaluation_trade"] = {
                "state": "SIGNAL_RECORDED_NOT_OPENED",
                "blockers": [reason],
            }
            if output.get("prediction_id"):
                self._persist_admission_result(
                    str(output["prediction_id"]), output["evaluation_trade"]
                )
            return output
        result = {
            "ok": True,
            "state": "SCORED",
            "model_id": model["model_id"],
            "model_state": "QUANT_EVALUATION_PAPER",
            "raw_score": score,
            "normalized_score": max(0.0, min(100.0, percentile)),
            "feature_coverage": coverage,
            "missing_features": [],
            "paper_weight": RULE_MODEL_WEIGHT,
            "live_production_weight": 0.0,
            "source": "SelectionPlatformService.quant",
            "population_fingerprint": prediction.get("population_fingerprint"),
            "population_rank": prediction.get("rank"),
            "population_size": prediction.get("population_size"),
        }
        # Persist score, percentile and immutable population feature hash in
        # prediction lineage; no per-candidate model fitting occurs here.
        output = self._record_prediction(
            model,
            event,
            candidate,
            result,
            values=[
                score,
                percentile,
                coverage,
                int(_sha(str(candidate.get("feature_hash") or ""), 8), 16),
            ],
            open_evaluation=False,
        )
        selected = percentile >= LONG_SCORE_THRESHOLD
        output["selected_for_top_cohort"] = selected
        if selected:
            output["model_evaluation"] = self._record_virtual_evaluation(
                model, event, candidate, output
            )
            output["evaluation_trade"] = self._maybe_open_evaluation(
                model, event, candidate, output
            )
        else:
            output["model_evaluation"] = {
                "state": "NOT_SELECTED",
                "objective": FIXED_HORIZON_OBJECTIVE,
            }
            output["evaluation_trade"] = {
                "state": "NO_MODEL_SIGNAL",
                "reason": "outside same-snapshot top quintile",
            }
        # Bootstrap is the deterministic Model-Paper authority before a trained
        # challenger exists. Persist its admission result exactly like trained
        # rankers so Operations/Research can distinguish an open position from
        # a governed wait or a concrete missing-evidence blocker.
        if output.get("prediction_id"):
            self._persist_admission_result(
                str(output["prediction_id"]), output.get("evaluation_trade") or {}
            )
        return output

    def _range_rule_event(self) -> tuple[Dict[str, Any], Dict[str, Any]]:
        card = RangeCompressionRuleService.rule_card()
        model_id = "rule-" + _sha({
            "rule_id": RANGE_COMPRESSION_RULE_ID,
            "immutable_spec_hash": card["immutable_spec_hash"],
        }, 30)
        prior = self._latest_event(model_id)
        model = {
            "model_id": model_id,
            "model_family": "FROZEN_RANGE_COMPRESSION_RULE",
            "mode": "delivery",
            "horizon": "20d",
            "dataset_fingerprint": "FORWARD_IMMUTABLE_POPULATION_STREAM",
            "feature_manifest_hash": card["immutable_spec_hash"],
            "trial_count": 1,
        }
        if prior:
            return model, prior
        created = _now()
        event_payload = {
            "model_id": model_id,
            "mode": "delivery",
            "horizon": "20d",
            "state": PREDICTION_ACTIVE,
            "paper_weight": RULE_MODEL_WEIGHT,
            "live_production_weight": 0.0,
            "rule_id": RANGE_COMPRESSION_RULE_ID,
            "immutable_spec_hash": card["immutable_spec_hash"],
            "automatic": True,
        }
        event_hash = _sha(event_payload)
        event_id = _sha({"event_hash": event_hash, "created_at": created}, 40)
        gate_report = {
            "frozen_rule_card": True,
            "completed_daily_bars_only": True,
            "top_1_percent_primary_cohort": True,
            "top_5_percent_secondary_benchmark": True,
            "prediction_active": True,
            "decision_weight": RULE_MODEL_WEIGHT,
            "live_production_weight": 0.0,
            "reason": "forward evaluation only; no statistical edge claim",
        }
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT OR IGNORE INTO quant_model_trial_ledger(
                   model_id,model_family,mode,horizon,dataset_fingerprint,
                   feature_manifest_hash,trial_count,rule_id,model_specification_hash,
                   immutable_spec_hash,observed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    model_id, model["model_family"], "delivery", "20d",
                    model["dataset_fingerprint"], card["immutable_spec_hash"], 1,
                    RANGE_COMPRESSION_RULE_ID, card["immutable_spec_hash"],
                    card["immutable_spec_hash"], created,
                ),
            )
            self.store.conn.execute(
                """INSERT INTO quant_paper_activation_ledger(
                   event_id,model_id,mode,horizon,state,paper_weight,
                   live_production_weight,artifact_sha256,dataset_fingerprint,
                   feature_manifest_hash,validation_hash,dependency_hash,
                   projection_id,holdout_start,holdout_end,gate_json,
                   predecessor_model_id,predecessor_event_hash,event_hash,
                   automatic,created_at,service_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, model_id, "delivery", "20d", PREDICTION_ACTIVE,
                    RULE_MODEL_WEIGHT, 0.0, None, model["dataset_fingerprint"],
                    card["immutable_spec_hash"], _sha({"state": "FORWARD_ONLY"}),
                    _sha({"runtime": "stdlib", "cost_version": self.costs.schedule.version}),
                    None, None, None, _canonical({"gates": gate_report, "rule_card": card}),
                    None, None, event_hash, 1, created, SERVICE_VERSION,
                ),
            )
            self.store.conn.commit()
        return model, dict(self._latest_event(model_id) or {})

    def _score_range_rule(
        self,
        candidate: Mapping[str, Any],
        prediction: Mapping[str, Any],
        ) -> Dict[str, Any]:
        model, event = self._range_rule_event()
        score = _number(prediction.get("score"))
        percentile = _number(prediction.get("percentile"))
        cohort = str(prediction.get("cohort") or "QUALIFIED_OUTSIDE_TOP_5")
        if prediction.get("qualified") is not True or score is None or percentile is None:
            result = {
                "ok": True,
                "state": "RULE_NOT_TRIGGERED",
                "model_id": model["model_id"],
                "feature_coverage": 1.0,
                "paper_weight": 0.0,
                "reason": "frozen range predicate not satisfied",
            }
            return self._record_prediction(
                model, event, candidate, result,
                rule_id=RANGE_COMPRESSION_RULE_ID, open_evaluation=False,
                payload_extra={"cohort": cohort, "rule_version": RANGE_COMPRESSION_RULE_VERSION},
            )
        result = {
            "ok": True,
            "state": "SCORED",
            "model_id": model["model_id"],
            "model_state": "QUANT_EVALUATION_PAPER",
            "raw_score": score,
            "normalized_score": percentile,
            "feature_coverage": 1.0,
            "missing_features": [],
            "paper_weight": RULE_MODEL_WEIGHT,
            "live_production_weight": 0.0,
            "source": "RangeCompressionRuleService",
            "cohort": cohort,
        }
        output = self._record_prediction(
            model, event, candidate, result,
            values=[score, percentile, _number(prediction.get("compression_ratio")) or 0.0],
            rule_id=RANGE_COMPRESSION_RULE_ID, open_evaluation=False,
            payload_extra={
                "cohort": cohort,
                "rule_version": RANGE_COMPRESSION_RULE_VERSION,
                "range_compression_rule": prediction.get("range_compression_rule"),
            },
        )
        if cohort == "TOP_1_PERCENT":
            rule = prediction.get("range_compression_rule")
            evidence = dict(rule.get("evidence") or {}) if isinstance(rule, Mapping) else {}
            enriched = dict(candidate)
            enriched["_evaluation_objective"] = TRADE_MAP_OVERLAY_OBJECTIVE
            entry = _number(enriched.get("ltp")) or _number(enriched.get("current_price")) or _number(enriched.get("planned_entry"))
            stop = _number(evidence.get("latest_low"))
            if entry is not None and stop is not None and entry > stop:
                enriched.update({
                    "side": "LONG",
                    "planned_entry": entry,
                    "planned_sl": stop,
                    "planned_t1": entry + 2.0 * (entry - stop),
                })
            output["evaluation_trade"] = self._maybe_open_evaluation(
                model, event, enriched, output
            )
        else:
            output["evaluation_trade"] = {
                "state": "BENCHMARK_RECORDED_NOT_OPENED",
                "cohort": cohort,
                "reason": "only the predeclared top 1% cohort opens forward evaluation positions",
            }
        return output

    def _current_active(
        self, mode: str, horizon: Optional[str] = None
        ) -> Optional[Dict[str, Any]]:
        desk = require_production_mode(mode)
        routed_horizon = str(horizon or _default_model_horizon(desk))
        rows = self.store.conn.execute(
            """SELECT * FROM quant_paper_activation_ledger
               WHERE mode=? AND horizon=? AND state=? AND artifact_sha256 IS NOT NULL
               ORDER BY created_at DESC,event_id DESC""",
            (desk, routed_horizon, PREDICTION_ACTIVE),
        ).fetchall()
        for raw in rows:
            row = dict(raw)
            latest = self._latest_event(str(row["model_id"]))
            if latest and latest["event_id"] == row["event_id"]:
                return row
        return None

    def _candidate_features(candidate: Mapping[str, Any], mode: str) -> tuple[list[float], float, list[str]]:
        merged = dict(candidate)
        nested = candidate.get("features")
        if isinstance(nested, Mapping):
            merged.update(nested)
        specs = DELIVERY_FEATURES if mode == "delivery" else INTRADAY_FEATURES
        values = []
        missing = []
        for name, aliases, _weight, _higher in specs:
            value = feature_value(merged, aliases)
            values.append(value)
            if value is None:
                missing.append(name)
        coverage = sum(value is not None for value in values) / len(values) if values else 0.0
        return values, coverage, missing

    def _score(
        self,
        model: Mapping[str, Any],
        event: Mapping[str, Any],
        candidate: Mapping[str, Any],
        *,
        open_evaluation: bool = True,
        ) -> Dict[str, Any]:
        artifact = self._artifact(model)
        if not artifact.get("ok"):
            return {
                "ok": False,
                "state": artifact.get("state") or "ARTIFACT_FAILED",
                "model_id": model["model_id"],
                "paper_weight": 0.0,
            }
        mode = str(model["mode"])
        values, coverage, missing = self._candidate_features(candidate, mode)
        adapter: LightGbmArtifactAdapter = artifact["adapter"]
        if [str(value) for value in adapter.feature_names] != [
            item[0] for item in (DELIVERY_FEATURES if mode == "delivery" else INTRADAY_FEATURES)
        ]:
            return {
                "ok": False,
                "state": "LIVE_FEATURE_MANIFEST_ORDER_MISMATCH",
                "model_id": model["model_id"],
                "paper_weight": 0.0,
            }
        if coverage < 0.60:
            result = {
                "ok": True,
                "state": "FEATURE_COVERAGE_INSUFFICIENT",
                "model_id": model["model_id"],
                "feature_coverage": round(coverage, 6),
                "missing_features": missing,
                "paper_weight": float(event.get("paper_weight") or 0.0),
            }
            return self._record_prediction(model, event, candidate, result)
        imputed = [
            float(value) if value is not None else float(adapter.medians[index])
            for index, value in enumerate(values)
        ]
        try:
            raw_score = adapter.raw_score(imputed)
            normalized = adapter.normalize(raw_score)
            result = {
                "ok": True,
                "state": "SCORED",
                "model_id": model["model_id"],
                "model_state": event["state"],
                "raw_score": round(raw_score, 10),
                "normalized_score": normalized,
                "feature_coverage": round(coverage, 6),
                "missing_features": missing,
                "paper_weight": float(event.get("paper_weight") or 0.0),
                "live_production_weight": 0.0,
            }
        except Exception as exc:
            result = {
                "ok": False,
                "state": "INFERENCE_FAILED",
                "model_id": model["model_id"],
                "reason": str(exc)[:200],
                "feature_coverage": round(coverage, 6),
                "paper_weight": 0.0,
            }
        return self._record_prediction(
            model,
            event,
            candidate,
            result,
            values=imputed,
            open_evaluation=open_evaluation,
        )

    def _record_prediction(
        self,
        model: Mapping[str, Any],
        event: Mapping[str, Any],
        candidate: Mapping[str, Any],
        result: Dict[str, Any],
        *,
        values: Optional[Sequence[float]] = None,
        rule_id: str = RULE_ID,
        open_evaluation: bool = True,
        payload_extra: Optional[Mapping[str, Any]] = None,
        ) -> Dict[str, Any]:
        symbol = str(candidate.get("symbol") or candidate.get("stock") or "").upper().strip()
        candidate_id = str(candidate.get("candidate_id") or candidate.get("signal_id") or "")
        observed = str(
            candidate.get("decision_ts")
            or candidate.get("observed_at")
            or candidate.get("quote_as_of")
            or _now()
        )
        feature_hash = _sha({
            "model_id": model["model_id"],
            "values": list(values or []),
            "coverage": result.get("feature_coverage"),
        })
        prediction_id = _sha({
            "model_id": model["model_id"],
            "candidate_id": candidate_id,
            "symbol": symbol,
            "observed_at": observed,
            "feature_hash": feature_hash,
        }, 40)
        payload = {
            "missing_features": result.get("missing_features") or [],
            "prediction_state": event["state"],
            "decision_weight": float(event.get("paper_weight") or 0.0),
            "rule_id": str(rule_id or RULE_ID),
            "canonical_paper_decision_mutation": (
                event.get("state") == PREDICTION_ACTIVE
                and float(event.get("paper_weight") or 0.0) > 0
            ),
            "broker_orders": False,
        }
        if isinstance(payload_extra, Mapping):
            payload.update(dict(payload_extra))
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT OR IGNORE INTO quant_paper_predictions(
                   prediction_id,model_id,mode,horizon,symbol,candidate_id,
                   model_state,raw_score,normalized_score,paper_weight,
                   feature_coverage,prediction_state,reason,feature_hash,
                   observed_at,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    prediction_id, model["model_id"], model["mode"], model["horizon"],
                    symbol, candidate_id or None, event["state"], result.get("raw_score"),
                    result.get("normalized_score"), float(event.get("paper_weight") or 0.0),
                    float(result.get("feature_coverage") or 0.0), result["state"],
                    result.get("reason"), feature_hash, observed, _canonical(payload),
                ),
            )
            self.store.conn.commit()
        output = {**result, "prediction_id": prediction_id}
        if result.get("state") == "SCORED" and open_evaluation:
            output["evaluation_trade"] = self._maybe_open_evaluation(
                model, event, candidate, output
            )
            self._persist_admission_result(
                prediction_id, output["evaluation_trade"]
            )
        return output

    def _persist_admission_result(
        self, prediction_id: str, admission: Mapping[str, Any]
        ) -> None:
        row = self.store.conn.execute(
            "SELECT payload_json FROM quant_paper_predictions WHERE prediction_id=?",
            (str(prediction_id),),
        ).fetchone()
        if not row:
            return
        payload = _json_object(row[0])
        payload["automatic_paper_admission"] = dict(admission or {})
        with self.store.write_lock:
            self.store.conn.execute(
                """UPDATE quant_paper_predictions SET payload_json=?
                   WHERE prediction_id=?""",
                (_canonical(payload), str(prediction_id)),
            )
            self.store.conn.commit()

    def _record_virtual_evaluation(
        self,
        model: Mapping[str, Any],
        event: Mapping[str, Any],
        candidate: Mapping[str, Any],
        prediction: Mapping[str, Any],
        ) -> Dict[str, Any]:
        """Record every selected top-cohort outcome before capital admission.

        This ledger is the forward equivalent of the model's fixed-horizon
        training objective. It deliberately does not require a trade map,
        trigger fill, ADV, sector or available cash; those belong to the
        separate executable ₹5L portfolio ledger.
        """
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        symbol = str(candidate.get("symbol") or candidate.get("stock") or "").upper().strip()
        mode = require_production_mode(model.get("mode"))
        horizon = str(model.get("horizon") or _default_model_horizon(mode))
        side = _selector_signal_side(
            requested_side=candidate.get("side") or candidate.get("direction"),
            selected_for_top_cohort=True,
            mode=mode,
        )
        prediction_id = str(prediction.get("prediction_id") or "").strip()
        if not candidate_id or not symbol or not side or not prediction_id:
            return {
                "state": "VIRTUAL_OUTCOME_NOT_RECORDED",
                "reason": "candidate identity, side and prediction lineage are required",
            }
        try:
            rank = max(1, int(
                prediction.get("population_rank")
                or prediction.get("rank")
                or 1
            ))
        except (TypeError, ValueError):
            rank = 1
        try:
            population_size = max(1, int(
                prediction.get("population_size")
                or candidate.get("population_size")
                or 1
            ))
        except (TypeError, ValueError):
            population_size = 1
        selected_at = str(
            candidate.get("decision_ts")
            or candidate.get("observed_at")
            or prediction.get("observed_at")
            or _now()
        )
        virtual_id = _sha({
            "model_id": model["model_id"],
            "candidate_id": candidate_id,
            "horizon": horizon,
            "objective": FIXED_HORIZON_OBJECTIVE,
        }, 40)
        created = _now()
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT OR IGNORE INTO quant_virtual_model_outcomes(
                   virtual_id,prediction_id,model_id,model_state,candidate_id,
                   population_fingerprint,symbol,mode,horizon,side,
                   population_rank,population_size,status,selected_at,
                   created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    virtual_id, prediction_id, model["model_id"], event["state"],
                    candidate_id, str(candidate.get("population_fingerprint") or ""),
                    symbol, mode, horizon, side, rank, population_size, "PENDING",
                    selected_at, created, created,
                ),
            )
            self.store.conn.commit()
        self.reconcile_virtual_outcomes(candidate_id=candidate_id)
        row = self.store.conn.execute(
            "SELECT * FROM quant_virtual_model_outcomes WHERE virtual_id=?",
            (virtual_id,),
        ).fetchone()
        return {
            "state": str((dict(row) if row else {}).get("status") or "PENDING"),
            "virtual_id": virtual_id,
            "objective": FIXED_HORIZON_OBJECTIVE,
            "capital_admission_required": False,
            "broker_orders": False,
        }
