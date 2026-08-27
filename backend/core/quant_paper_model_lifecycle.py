"""Model registry, artifact validation and promotion-state reconciliation."""
from __future__ import annotations

from core.quant_paper_dependencies import *  # noqa: F401,F403


class QuantPaperModelLifecycleMixin:
    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS quant_model_trial_ledger (
                  model_id TEXT PRIMARY KEY,
                  model_family TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  horizon TEXT NOT NULL,
                  dataset_fingerprint TEXT NOT NULL,
                  feature_manifest_hash TEXT NOT NULL,
                  trial_count INTEGER NOT NULL,
                  rule_id TEXT NOT NULL,
                  model_specification_hash TEXT NOT NULL,
                  immutable_spec_hash TEXT NOT NULL,
                  observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quant_paper_activation_ledger (
                  event_id TEXT PRIMARY KEY,
                  model_id TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  horizon TEXT NOT NULL,
                  state TEXT NOT NULL,
                  paper_weight REAL NOT NULL,
                  live_production_weight REAL NOT NULL,
                  artifact_sha256 TEXT,
                  dataset_fingerprint TEXT NOT NULL,
                  feature_manifest_hash TEXT NOT NULL,
                  validation_hash TEXT NOT NULL,
                  dependency_hash TEXT NOT NULL,
                  projection_id TEXT,
                  holdout_start TEXT,
                  holdout_end TEXT,
                  gate_json TEXT NOT NULL,
                  predecessor_model_id TEXT,
                  predecessor_event_hash TEXT,
                  event_hash TEXT NOT NULL UNIQUE,
                  automatic INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  service_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_quant_paper_activation_latest
                  ON quant_paper_activation_ledger(mode,horizon,created_at);
                CREATE TABLE IF NOT EXISTS quant_holdout_consumption_ledger (
                  consumption_id TEXT PRIMARY KEY,
                  model_id TEXT NOT NULL UNIQUE,
                  mode TEXT NOT NULL,
                  horizon TEXT NOT NULL,
                  holdout_start TEXT NOT NULL,
                  holdout_end TEXT NOT NULL,
                  validation_hash TEXT NOT NULL,
                  dataset_fingerprint TEXT NOT NULL,
                  consumed_at TEXT NOT NULL,
                  consumer_state TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_quant_holdout_consumption_range
                  ON quant_holdout_consumption_ledger(mode,horizon,holdout_start,holdout_end);
                CREATE TABLE IF NOT EXISTS quant_paper_predictions (
                  prediction_id TEXT PRIMARY KEY,
                  model_id TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  horizon TEXT NOT NULL,
                  symbol TEXT NOT NULL,
                  candidate_id TEXT,
                  model_state TEXT NOT NULL,
                  raw_score REAL,
                  normalized_score REAL,
                  paper_weight REAL NOT NULL,
                  feature_coverage REAL NOT NULL,
                  prediction_state TEXT NOT NULL,
                  reason TEXT,
                  feature_hash TEXT NOT NULL,
                  observed_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_quant_paper_prediction_model
                  ON quant_paper_predictions(model_id,observed_at);
                CREATE TABLE IF NOT EXISTS quant_virtual_model_outcomes (
                  virtual_id TEXT PRIMARY KEY,
                  prediction_id TEXT NOT NULL,
                  model_id TEXT NOT NULL,
                  model_state TEXT NOT NULL,
                  candidate_id TEXT NOT NULL,
                  population_fingerprint TEXT NOT NULL,
                  symbol TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  horizon TEXT NOT NULL,
                  side TEXT NOT NULL,
                  population_rank INTEGER NOT NULL,
                  population_size INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  net_return_bps REAL,
                  net_return_plus_20bps REAL,
                  outcome TEXT,
                  selected_at TEXT NOT NULL,
                  settled_at TEXT,
                  label_record_hash TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(model_id,candidate_id,horizon)
                );
                CREATE INDEX IF NOT EXISTS ix_quant_virtual_outcome_status
                  ON quant_virtual_model_outcomes(status,mode,horizon,selected_at);
                CREATE TABLE IF NOT EXISTS quant_evaluation_positions (
                  position_id TEXT PRIMARY KEY,
                  prediction_id TEXT NOT NULL UNIQUE,
                  model_id TEXT NOT NULL,
                  model_state TEXT NOT NULL,
                  symbol TEXT NOT NULL,
                  exchange TEXT NOT NULL,
                  bse_group TEXT,
                  mode TEXT NOT NULL,
                  side TEXT NOT NULL,
                  status TEXT NOT NULL,
                  quantity INTEGER NOT NULL,
                  entry_price REAL NOT NULL,
                  target_price REAL NOT NULL,
                  stop_price REAL NOT NULL,
                  managed_stop REAL NOT NULL,
                  trailing_state TEXT NOT NULL DEFAULT 'ORIGINAL_STOP',
                  secured_profit INTEGER NOT NULL DEFAULT 0,
                  last_price REAL NOT NULL,
                  exit_price REAL,
                  notional REAL NOT NULL,
                  reserved_cost REAL NOT NULL,
                  open_risk REAL NOT NULL,
                  gross_pnl REAL NOT NULL DEFAULT 0,
                  total_cost REAL NOT NULL DEFAULT 0,
                  net_pnl REAL NOT NULL DEFAULT 0,
                  net_pnl_stress_5bps REAL NOT NULL DEFAULT 0,
                  net_pnl_stress_10bps REAL NOT NULL DEFAULT 0,
                  net_pnl_stress_20bps REAL NOT NULL DEFAULT 0,
                  mfe_bps REAL NOT NULL DEFAULT 0,
                  mae_bps REAL NOT NULL DEFAULT 0,
                  holding_seconds INTEGER NOT NULL DEFAULT 0,
                  evaluation_objective TEXT NOT NULL DEFAULT 'FIXED_HORIZON_TOP_COHORT_NET_RETURN',
                  horizon TEXT NOT NULL,
                  horizon_exit_at TEXT NOT NULL,
                  horizon_sessions_required INTEGER NOT NULL DEFAULT 0,
                  horizon_sessions_observed INTEGER NOT NULL DEFAULT 0,
                  last_session_date TEXT,
                  data_failure INTEGER NOT NULL DEFAULT 0,
                  unscorable INTEGER NOT NULL DEFAULT 0,
                  outcome TEXT,
                  exit_reason TEXT,
                  opened_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  closed_at TEXT,
                  cost_version TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  CHECK(
                    (exchange='NSE' AND bse_group IS NULL)
                    OR (exchange='BSE' AND bse_group IN
                        ('A','B','X','XT','Z','ZP','XC','XD','T','SS','ST'))
                  )
                );
                CREATE INDEX IF NOT EXISTS ix_quant_eval_position_status
                  ON quant_evaluation_positions(status,mode,opened_at);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_quant_eval_open_model_symbol
                  ON quant_evaluation_positions(model_id,symbol) WHERE status='OPEN';
                CREATE TABLE IF NOT EXISTS quant_capital_allocation_ledger (
                  allocation_id TEXT PRIMARY KEY,
                  prediction_id TEXT NOT NULL,
                  model_id TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  equity REAL NOT NULL,
                  portfolio_drawdown_pct REAL NOT NULL,
                  closed_trades INTEGER NOT NULL,
                  realized_net_pnl REAL NOT NULL,
                  profit_factor REAL,
                  risk_scale REAL NOT NULL,
                  strategy_cap REAL NOT NULL,
                  strategy_open_capital REAL NOT NULL,
                  allocated_cash REAL NOT NULL,
                  sizing_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_quant_capital_allocation_model
                  ON quant_capital_allocation_ledger(model_id,created_at);
                """
            )
            migrations = {
                "quant_model_trial_ledger": {
                    "model_specification_hash": "TEXT NOT NULL DEFAULT ''",
                },
                "quant_evaluation_positions": {
                    "exchange": "TEXT",
                    "bse_group": "TEXT",
                    "net_pnl_stress_5bps": "REAL NOT NULL DEFAULT 0",
                    "net_pnl_stress_10bps": "REAL NOT NULL DEFAULT 0",
                    "net_pnl_stress_20bps": "REAL NOT NULL DEFAULT 0",
                    "mfe_bps": "REAL NOT NULL DEFAULT 0",
                    "mae_bps": "REAL NOT NULL DEFAULT 0",
                    "holding_seconds": "INTEGER NOT NULL DEFAULT 0",
                    "evaluation_objective": (
                        "TEXT NOT NULL DEFAULT "
                        "'FIXED_HORIZON_TOP_COHORT_NET_RETURN'"
                    ),
                    "horizon": "TEXT NOT NULL DEFAULT '20d'",
                    "horizon_exit_at": "TEXT NOT NULL DEFAULT ''",
                    "horizon_sessions_required": "INTEGER NOT NULL DEFAULT 0",
                    "horizon_sessions_observed": "INTEGER NOT NULL DEFAULT 0",
                    "last_session_date": "TEXT",
                    "data_failure": "INTEGER NOT NULL DEFAULT 0",
                    "unscorable": "INTEGER NOT NULL DEFAULT 0",
                    "managed_stop": "REAL NOT NULL DEFAULT 0",
                    "trailing_state": "TEXT NOT NULL DEFAULT 'ORIGINAL_STOP'",
                    "secured_profit": "INTEGER NOT NULL DEFAULT 0",
                },
            }
            for table, columns in migrations.items():
                existing = {
                    str(row[1])
                    for row in self.store.conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for name, declaration in columns.items():
                    if name not in existing:
                        self.store.conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                        )
            self.store.conn.execute(
                """UPDATE quant_evaluation_positions
                      SET exchange=UPPER(NULLIF(json_extract(payload_json,'$.exchange'),''))
                    WHERE COALESCE(exchange,'')=''"""
            )
            self.store.conn.execute(
                """UPDATE quant_evaluation_positions
                      SET bse_group=UPPER(NULLIF(json_extract(payload_json,'$.bse_group'),''))
                    WHERE exchange='BSE' AND COALESCE(bse_group,'')=''"""
            )
            candidate_table = self.store.conn.execute(
                """SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='candidate_population_observations'"""
            ).fetchone()
            if candidate_table:
                self.store.conn.execute(
                    """UPDATE quant_evaluation_positions
                          SET exchange=UPPER((
                              SELECT c.exchange FROM candidate_population_observations c
                               WHERE c.candidate_id=json_extract(
                                   quant_evaluation_positions.payload_json,'$.candidate_id'
                               ) LIMIT 1
                          ))
                        WHERE COALESCE(exchange,'')=''"""
                )
                self.store.conn.execute(
                    """UPDATE quant_evaluation_positions
                          SET bse_group=UPPER((
                              SELECT NULLIF(json_extract(c.feature_json,'$.bse_group'),'')
                                FROM candidate_population_observations c
                               WHERE c.candidate_id=json_extract(
                                   quant_evaluation_positions.payload_json,'$.candidate_id'
                               ) LIMIT 1
                          ))
                        WHERE exchange='BSE' AND COALESCE(bse_group,'')=''"""
                )
            self.store.conn.execute(
                "UPDATE quant_evaluation_positions SET bse_group=NULL WHERE exchange='NSE'"
            )
            unresolved_venue = self.store.conn.execute(
                """SELECT position_id FROM quant_evaluation_positions
                    WHERE exchange IS NULL
                       OR exchange NOT IN ('NSE','BSE')
                       OR (exchange='BSE' AND COALESCE(bse_group,'') NOT IN
                           ('A','B','X','XT','Z','ZP','XC','XD','T','SS','ST'))
                    LIMIT 1"""
            ).fetchone()
            if unresolved_venue:
                raise RuntimeError(
                    f"QUANT_PAPER_VENUE_IDENTITY_UNRESOLVED:{unresolved_venue[0]}"
                )
            self.store.conn.executescript(
                """DROP TRIGGER IF EXISTS trg_quant_evaluation_venue_required;
                   CREATE TRIGGER trg_quant_evaluation_venue_required
                   BEFORE INSERT ON quant_evaluation_positions
                   FOR EACH ROW WHEN
                       NEW.exchange IS NULL
                       OR NEW.exchange NOT IN ('NSE','BSE')
                       OR (NEW.exchange='NSE' AND NEW.bse_group IS NOT NULL)
                       OR (NEW.exchange='BSE' AND COALESCE(NEW.bse_group,'') NOT IN
                           ('A','B','X','XT','Z','ZP','XC','XD','T','SS','ST'))
                   BEGIN
                     SELECT RAISE(ABORT,'QUANT_PAPER_VENUE_IDENTITY_REQUIRED');
                   END;
                   DROP TRIGGER IF EXISTS trg_quant_evaluation_venue_immutable;
                   CREATE TRIGGER trg_quant_evaluation_venue_immutable
                   BEFORE UPDATE OF exchange,bse_group ON quant_evaluation_positions
                   FOR EACH ROW WHEN
                       NEW.exchange IS NOT OLD.exchange
                       OR NEW.bse_group IS NOT OLD.bse_group
                   BEGIN
                     SELECT RAISE(ABORT,'QUANT_PAPER_VENUE_IDENTITY_IS_IMMUTABLE');
                   END;
                """
            )
            # Preserve prior paper history while migrating the authority vocabulary.
            # This changes labels only; quantities, prices, outcomes and P&L stay intact.
            self.store.conn.execute(
                """UPDATE quant_paper_activation_ledger
                   SET state=?
                   WHERE state IN ('PAPER_ACTIVE','QUANT_EVALUATION_PAPER')
                     AND paper_weight>0""",
                (PREDICTION_ACTIVE,),
            )
            self.store.conn.execute(
                """UPDATE quant_paper_activation_ledger
                   SET state=?,paper_weight=0
                   WHERE state IN ('SHADOW_ONLY','PAPER_REJECTED','QUANT_EVALUATION_PAPER')
                     AND paper_weight<=0""",
                (MODEL_UNAVAILABLE,),
            )
            self.store.conn.execute(
                """UPDATE quant_paper_predictions
                   SET model_state=?
                   WHERE model_state IN ('PAPER_ACTIVE','QUANT_EVALUATION_PAPER')
                     AND paper_weight>0""",
                (PREDICTION_ACTIVE,),
            )
            self.store.conn.commit()

    def _model_row(raw: Any) -> Dict[str, Any]:
        row = dict(raw)
        row["validation"] = _json_object(row.get("validation_json"))
        row["dependency"] = _json_object(row.get("dependency_json"))
        return row

    def _latest_models(self, mode: Optional[str] = None) -> list[Dict[str, Any]]:
        where = "WHERE mode=?" if mode else ""
        params = (require_production_mode(mode),) if mode else ()
        exists = self.store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shadow_lightgbm_models'"
        ).fetchone()
        if not exists:
            return []
        rows = self.store.conn.execute(
            f"""SELECT * FROM shadow_lightgbm_models {where}
                ORDER BY created_at DESC,model_id DESC""",
            params,
        ).fetchall()
        seen = set()
        output = []
        for raw in rows:
            row = self._model_row(raw)
            key = (row["mode"], row["horizon"])
            if key in seen:
                continue
            seen.add(key)
            output.append(row)
        return output

    def _artifact(self, model: Mapping[str, Any]) -> Dict[str, Any]:
        path = Path(str(model.get("artifact_path") or "")).resolve()
        configured_roots = {
            Path(str(value)).resolve()
            for value in getattr(self.store, "_quant_model_artifact_roots", set())
            if value
        }
        configured_roots.add((Path(DATA_DIR) / "analytics" / "models").resolve())
        explicit_root = os.environ.get("PROJECT_LADDU_QUANT_MODEL_DIR")
        if explicit_root:
            configured_roots.add(Path(explicit_root).resolve())
        if not any(path == root or path.is_relative_to(root) for root in configured_roots):
            return {
                "ok": False,
                "state": "ARTIFACT_PATH_OUTSIDE_TRUSTED_ROOT",
                "path": str(path),
                "trusted_roots": sorted(str(root) for root in configured_roots),
            }
        if not path.is_file():
            return {"ok": False, "state": "ARTIFACT_MISSING", "path": str(path)}
        stat = path.stat()
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = self._artifact_cache.get(cache_key)
        if cached:
            return dict(cached)
        try:
            raw = path.read_bytes()
            artifact = json.loads(raw.decode("utf-8"))
            if not isinstance(artifact, Mapping):
                raise ValueError("artifact root must be an object")
            adapter = LightGbmArtifactAdapter(artifact)
        except Exception as exc:
            return {
                "ok": False,
                "state": "ARTIFACT_UNREADABLE",
                "path": str(path),
                "reason": str(exc)[:240],
            }
        checks = {
            "model_id": str(artifact.get("model_id") or "") == str(model.get("model_id") or ""),
            "mode": str(artifact.get("mode") or "") == str(model.get("mode") or ""),
            "horizon": str(artifact.get("horizon") or "") == str(model.get("horizon") or ""),
            "dataset_fingerprint": (
                str(artifact.get("dataset_fingerprint") or "")
                == str(model.get("dataset_fingerprint") or "")
            ),
            "feature_manifest_hash": (
                str(artifact.get("feature_manifest_hash") or "")
                == str(model.get("feature_manifest_hash") or "")
                == FEATURE_MANIFEST_HASH
            ),
            "dependency_versions": bool(
                (artifact.get("dependency_versions") or {}).get("lightgbm")
                and (artifact.get("dependency_versions") or {}).get("duckdb")
            ),
            "score_adapter": bool((artifact.get("score_adapter") or {}).get("quantile_knots")),
            "safe_json_tree_dump": bool(
                isinstance(artifact.get("booster_dump_model"), Mapping)
                and (artifact.get("booster_dump_model") or {}).get("tree_info")
            ),
            "immutable_model_specification": bool(
                artifact.get("specification_hash")
                and artifact.get("specification")
                and str(artifact.get("specification_hash"))
                == _sha(artifact.get("specification"))
            ),
        }
        result = {
            "ok": all(checks.values()),
            "state": "ARTIFACT_VERIFIED" if all(checks.values()) else "ARTIFACT_LINEAGE_FAILED",
            "path": str(path),
            "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            "checks": checks,
            "artifact": dict(artifact),
            "adapter": adapter,
        }
        self._artifact_cache[cache_key] = result
        return dict(result)

    def _latest_projection(self, mode: str, model_created_at: str) -> Dict[str, Any]:
        exists = self.store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quant_analytics_projections'"
        ).fetchone()
        if not exists:
            return {}
        row = self.store.conn.execute(
            """SELECT * FROM quant_analytics_projections
               WHERE mode=? AND created_at<=?
               ORDER BY created_at DESC,projection_id DESC LIMIT 1""",
            (mode, model_created_at),
        ).fetchone()
        return dict(row) if row else {}

    def _latest_event(self, model_id: str) -> Optional[Dict[str, Any]]:
        row = self.store.conn.execute(
            """SELECT * FROM quant_paper_activation_ledger
               WHERE model_id=? ORDER BY created_at DESC,event_id DESC LIMIT 1""",
            (model_id,),
        ).fetchone()
        return dict(row) if row else None

    def _active_predecessor(self, mode: str, horizon: str, excluding: str) -> Optional[Dict[str, Any]]:
        rows = self.store.conn.execute(
            """SELECT * FROM quant_paper_activation_ledger
               WHERE mode=? AND horizon=? AND state=? AND model_id<>?
               ORDER BY created_at DESC,event_id DESC""",
            (mode, horizon, PREDICTION_ACTIVE, excluding),
        ).fetchall()
        for raw in rows:
            row = dict(raw)
            latest = self._latest_event(str(row["model_id"]))
            if latest and latest["event_id"] == row["event_id"]:
                return row
        return None

    def _holdout_reused(
        self, *, model_id: str, mode: str, horizon: str, start: str, end: str
        ) -> list[Dict[str, Any]]:
        if not start or not end:
            return [{"state": "HOLDOUT_RANGE_MISSING"}]
        rows = self.store.conn.execute(
            """SELECT model_id,holdout_start,holdout_end,consumed_at
               FROM quant_holdout_consumption_ledger
               WHERE mode=? AND horizon=? AND model_id<>?
                 AND holdout_start<=? AND holdout_end>=?
               ORDER BY consumed_at""",
            (mode, horizon, model_id, end, start),
        ).fetchall()
        return [dict(row) for row in rows]

    def reconcile_model(self, model_id: str) -> Dict[str, Any]:
        raw = self.store.conn.execute(
            "SELECT * FROM shadow_lightgbm_models WHERE model_id=?",
            (str(model_id),),
        ).fetchone()
        if not raw:
            return {"ok": False, "state": "MODEL_NOT_FOUND", "model_id": str(model_id)}
        model = self._model_row(raw)
        validation = model["validation"]
        gates = validation.get("gates") if isinstance(validation.get("gates"), Mapping) else {}
        holdout = validation.get("holdout_dates") if isinstance(validation.get("holdout_dates"), Mapping) else {}
        artifact = self._artifact(model)
        projection = self._latest_projection(str(model["mode"]), str(model["created_at"]))
        dependency = model["dependency"]
        dependency_available = str(dependency.get("state") or "").upper() == "AVAILABLE"
        dependency_versions = (
            artifact.get("artifact", {}).get("dependency_versions") or {}
            if artifact.get("ok")
            else {}
        )
        holdout_start = str(holdout.get("start") or "")
        holdout_end = str(holdout.get("end") or "")
        reused = self._holdout_reused(
            model_id=str(model["model_id"]),
            mode=str(model["mode"]),
            horizon=str(model["horizon"]),
            start=holdout_start,
            end=holdout_end,
        )
        gate_report = {
            "artifact_integrity_and_identity": artifact.get("ok") is True,
            "trained_model_eligible": str(model.get("state") or "") in {
                "PREDICTION_MODEL_ELIGIBLE", "SHADOW_MODEL_ELIGIBLE"
            },
            "validation_all_gates_passed": validation.get("all_gates_passed") is True,
            "every_declared_validation_gate_passed": bool(gates) and all(value is True for value in gates.values()),
            "single_declared_trial": int(model.get("trial_count") or 0) == 1,
            "minimum_observations_340": int(model.get("observations") or 0) >= 340,
            "minimum_trading_days_126": int(model.get("trading_days") or 0) >= 126,
            "minimum_regimes_3": int(model.get("regimes") or 0) >= 3,
            "current_feature_manifest": str(model.get("feature_manifest_hash") or "") == FEATURE_MANIFEST_HASH,
            "research_dependency_lineage": bool(
                dependency_available
                and dependency_versions.get("duckdb")
                and dependency_versions.get("lightgbm")
            ),
            "duckdb_projection_reconciled": bool(
                projection
                and int(projection.get("reconciled") or 0) == 1
                and str(projection.get("source_content_hash") or "")
                == str(projection.get("projected_content_hash") or "")
            ),
            "untouched_holdout_identified": bool(holdout_start and holdout_end),
            "holdout_not_reused_across_cycles": not reused,
            "cost_stressed_label": validation.get("label") == "net_return_plus_20bps",
            "same_population_baselines_passed": bool(
                (validation.get("baseline_comparison") or {}).get("all_required_baselines_implemented")
                and (validation.get("baseline_comparison") or {}).get("model_beats_strongest_baseline_by_20bps")
            ),
            "no_probability_claim": validation.get("probability_claim") == "NONE",
        }
        statistically_qualified = all(gate_report.values())
        # Evaluation-paper eligibility is deliberately separate from statistical
        # promotion. A successfully trained, identity-verified model must be able
        # to generate governed paper predictions and accumulate forward evidence
        # even when it has not yet beaten the declared baselines.
        operational_ready = all(
            gate_report[name]
            for name in (
                "artifact_integrity_and_identity",
                "current_feature_manifest",
                "research_dependency_lineage",
                "duckdb_projection_reconciled",
            )
        ) and int(model.get("observations") or 0) >= 100
        gate_report["prediction_operational_ready"] = operational_ready
        gate_report["statistically_qualified"] = statistically_qualified
        prior = self._latest_event(str(model["model_id"]))
        state = PREDICTION_ACTIVE if operational_ready and statistically_qualified else "QUANT_EVALUATION_PAPER"
        decision_weight = (
            PAPER_WEIGHT if state == PREDICTION_ACTIVE
            else EVALUATION_PAPER_WEIGHT if operational_ready
            else 0.0
        )
        predecessor = self._active_predecessor(
            str(model["mode"]), str(model["horizon"]), str(model["model_id"])
        )
        validation_hash = _sha(validation)
        dependency_hash = _sha({
            "service_probe": dependency,
            "artifact_versions": dependency_versions,
        })
        event_payload = {
            "model_id": model["model_id"],
            "mode": model["mode"],
            "horizon": model["horizon"],
            "state": state,
            "paper_weight": decision_weight,
            "live_production_weight": 0.0,
            "artifact_sha256": artifact.get("artifact_sha256"),
            "dataset_fingerprint": model["dataset_fingerprint"],
            "feature_manifest_hash": model["feature_manifest_hash"],
            "validation_hash": validation_hash,
            "dependency_hash": dependency_hash,
            "projection_id": projection.get("projection_id"),
            "holdout_start": holdout_start or None,
            "holdout_end": holdout_end or None,
            "gates": gate_report,
            "reused_holdouts": reused,
            "predecessor_model_id": predecessor.get("model_id") if predecessor else None,
            "predecessor_event_hash": predecessor.get("event_hash") if predecessor else None,
            "automatic": True,
            "rule_id": RULE_ID,
        }
        event_hash = _sha(event_payload)
        created = _now()
        event_id = _sha({"event_hash": event_hash, "created_at": created}, 40)
        immutable_spec = {
            "model_id": model["model_id"],
            "family": model["model_family"],
            "mode": model["mode"],
            "horizon": model["horizon"],
            "dataset_fingerprint": model["dataset_fingerprint"],
            "feature_manifest_hash": model["feature_manifest_hash"],
            "trial_count": int(model["trial_count"]),
            "rule_id": RULE_ID,
            "model_specification_hash": (
                artifact.get("artifact", {}).get("specification_hash")
                if artifact.get("ok") else ""
            ),
        }
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT OR IGNORE INTO quant_model_trial_ledger(
                   model_id,model_family,mode,horizon,dataset_fingerprint,
                   feature_manifest_hash,trial_count,rule_id,model_specification_hash,
                   immutable_spec_hash,observed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    model["model_id"], model["model_family"], model["mode"], model["horizon"],
                    model["dataset_fingerprint"], model["feature_manifest_hash"],
                    int(model["trial_count"]), RULE_ID,
                    immutable_spec["model_specification_hash"], _sha(immutable_spec), created,
                ),
            )
            if not prior or str(prior.get("event_hash")) != event_hash:
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
                        event_id, model["model_id"], model["mode"], model["horizon"], state,
                        decision_weight, 0.0,
                        artifact.get("artifact_sha256"), model["dataset_fingerprint"],
                        model["feature_manifest_hash"], validation_hash, dependency_hash,
                        projection.get("projection_id"), holdout_start or None, holdout_end or None,
                        _canonical({"gates": gate_report, "reused_holdouts": reused}),
                        predecessor.get("model_id") if predecessor else None,
                        predecessor.get("event_hash") if predecessor else None,
                        event_hash, 1, created, SERVICE_VERSION,
                    ),
                )
            # A declared final holdout is burned on first completed evaluation,
            # whether it passes or fails.  Otherwise a rejected specification
            # could silently tune and retry against the same "holdout".
            completed_holdout_evaluation = (
                isinstance(validation.get("all_gates_passed"), bool)
                and bool(holdout_start and holdout_end)
            )
            if completed_holdout_evaluation:
                consumption_id = _sha({
                    "model_id": model["model_id"],
                    "validation_hash": validation_hash,
                    "holdout_start": holdout_start,
                    "holdout_end": holdout_end,
                }, 40)
                self.store.conn.execute(
                    """INSERT OR IGNORE INTO quant_holdout_consumption_ledger(
                       consumption_id,model_id,mode,horizon,holdout_start,holdout_end,
                       validation_hash,dataset_fingerprint,consumed_at,consumer_state
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        consumption_id, model["model_id"], model["mode"], model["horizon"],
                        holdout_start, holdout_end, validation_hash,
                        model["dataset_fingerprint"], created,
                        (
                            "PASS"
                            if validation.get("all_gates_passed") is True and not reused
                            else "FAIL_REUSED"
                            if reused else "FAIL"
                        ),
                    ),
                )
            self.store.conn.commit()
        latest = self._latest_event(str(model["model_id"]))
        return {
            "ok": True,
            "model_id": model["model_id"],
            "state": state,
            "paper_authority": state,
            "paper_weight": decision_weight,
            "live_production_weight": 0.0,
            "broker_execution_weight": 0.0,
            "broker_order_authority": "NONE",
            "gates": gate_report,
            "reused_holdouts": reused,
            "event_id": latest.get("event_id") if latest else event_id,
            "artifact": {key: value for key, value in artifact.items() if key not in {"artifact", "adapter"}},
        }

    def reconcile_latest(self, mode: Optional[str] = None) -> list[Dict[str, Any]]:
        return [self.reconcile_model(str(row["model_id"])) for row in self._latest_models(mode)]
