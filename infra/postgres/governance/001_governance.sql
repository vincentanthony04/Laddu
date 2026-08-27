-- Project Laddu v68.0.0 — separate model-governance PostgreSQL authority.
-- This schema must not share a PostgreSQL service/cluster with live trading.
BEGIN;
CREATE SCHEMA IF NOT EXISTS model_registry;
CREATE SCHEMA IF NOT EXISTS research;
CREATE SCHEMA IF NOT EXISTS deployment;
CREATE SCHEMA IF NOT EXISTS runtime_control;

CREATE TABLE IF NOT EXISTS runtime_control.schema_migrations (
    version bigint PRIMARY KEY,
    name text NOT NULL,
    content_sha256 text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS model_registry.models (
    model_id uuid PRIMARY KEY,
    model_key text NOT NULL,
    model_version text NOT NULL,
    desk text NOT NULL CHECK (desk IN ('INTRADAY','DELIVERY')),
    setup_family text NOT NULL,
    horizon_value integer NOT NULL CHECK (horizon_value > 0),
    horizon_unit text NOT NULL CHECK (horizon_unit IN ('MINUTE','SESSION','TRADING_DAY')),
    model_type text NOT NULL,
    artifact_uri text NOT NULL,
    artifact_sha256 text NOT NULL,
    feature_schema_hash text NOT NULL,
    label_definition_version text NOT NULL,
    training_data_manifest_uri text NOT NULL,
    training_window_start timestamptz NOT NULL,
    training_window_end timestamptz NOT NULL,
    code_revision text NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(model_key, model_version)
);

CREATE TABLE IF NOT EXISTS research.regime_observations (
    regime_observation_id uuid PRIMARY KEY,
    as_of timestamptz NOT NULL,
    scope text NOT NULL CHECK (scope IN ('MARKET','SECTOR','INSTRUMENT')),
    scope_key text NOT NULL,
    timeframe text NOT NULL,
    model_id uuid REFERENCES model_registry.models(model_id),
    bull_probability numeric(8,7) NOT NULL CHECK (bull_probability BETWEEN 0 AND 1),
    bear_probability numeric(8,7) NOT NULL CHECK (bear_probability BETWEEN 0 AND 1),
    volatile_probability numeric(8,7) NOT NULL CHECK (volatile_probability BETWEEN 0 AND 1),
    range_probability numeric(8,7) NOT NULL CHECK (range_probability BETWEEN 0 AND 1),
    sector_rotation_probability numeric(8,7) NOT NULL CHECK (sector_rotation_probability BETWEEN 0 AND 1),
    chosen_label text NOT NULL CHECK (chosen_label IN ('BULL','BEAR','VOLATILE','RANGE','SECTOR_ROTATION')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(as_of, scope, scope_key, timeframe, model_id),
    CHECK (abs((bull_probability+bear_probability+volatile_probability+range_probability+sector_rotation_probability)-1.0) <= 0.0001)
);

CREATE TABLE IF NOT EXISTS research.model_paper_observations (
    observation_id text PRIMARY KEY,
    source_signal_id text,
    symbol text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('intraday','delivery')),
    disposition text NOT NULL,
    observed_price numeric(20,6),
    occurred_at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_model_paper_observations_time
    ON research.model_paper_observations(occurred_at DESC);

CREATE TABLE IF NOT EXISTS research.ranking_populations (
    population_id uuid PRIMARY KEY,
    desk text NOT NULL CHECK (desk IN ('INTRADAY','DELIVERY')),
    setup_family text NOT NULL,
    as_of timestamptz NOT NULL,
    universe_revision text NOT NULL,
    population_definition jsonb NOT NULL,
    member_count integer NOT NULL CHECK (member_count > 0),
    population_sha256 text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS research.feature_snapshots (
    feature_snapshot_id uuid PRIMARY KEY,
    population_id uuid NOT NULL REFERENCES research.ranking_populations(population_id),
    as_of timestamptz NOT NULL,
    data_cutoff_at timestamptz NOT NULL,
    feature_schema_hash text NOT NULL,
    source_manifest_uri text NOT NULL,
    source_manifest_sha256 text NOT NULL,
    market_data_quality text NOT NULL CHECK (market_data_quality IN ('VERIFIED','PARTIAL','REJECTED')),
    frozen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (data_cutoff_at <= as_of)
);

CREATE TABLE IF NOT EXISTS research.predictions (
    prediction_id uuid PRIMARY KEY,
    prediction_key text NOT NULL UNIQUE,
    model_id uuid NOT NULL REFERENCES model_registry.models(model_id),
    population_id uuid NOT NULL REFERENCES research.ranking_populations(population_id),
    feature_snapshot_id uuid NOT NULL REFERENCES research.feature_snapshots(feature_snapshot_id),
    instrument_key text NOT NULL,
    as_of timestamptz NOT NULL,
    data_cutoff_at timestamptz NOT NULL,
    cost_model_version text NOT NULL,
    return_basis text NOT NULL CHECK (return_basis IN (
        'GROSS_POSITION_RETURN_BEFORE_COSTS','NET_POSITION_RETURN_AFTER_COSTS'
    )),
    effective_sample_size integer NOT NULL CHECK (effective_sample_size >= 2),
    net_return_standard_error numeric(16,10) NOT NULL CHECK (net_return_standard_error >= 0),
    uncertainty_method text NOT NULL CHECK (uncertainty_method IN (
        'NORMAL_STANDARD_ERROR','CONFORMAL_INTERVAL','EMPIRICAL_BOOTSTRAP_INTERVAL'
    )),
    calibration_model_id uuid REFERENCES model_registry.models(model_id),
    predicted_rank numeric(20,8),
    predicted_percentile numeric(10,9) CHECK (predicted_percentile BETWEEN 0 AND 1),
    target_before_stop_probability numeric(10,9) CHECK (target_before_stop_probability BETWEEN 0 AND 1),
    stop_before_target_probability numeric(10,9) CHECK (stop_before_target_probability BETWEEN 0 AND 1),
    neither_probability numeric(10,9) CHECK (neither_probability BETWEEN 0 AND 1),
    calibrated_confidence numeric(10,9) CHECK (calibrated_confidence BETWEEN 0 AND 1),
    observation_price numeric(20,6) CHECK (observation_price IS NULL OR observation_price > 0),
    target_price numeric(20,6) CHECK (target_price IS NULL OR target_price > 0),
    stop_price numeric(20,6) CHECK (stop_price IS NULL OR stop_price > 0),
    horizon_end_at timestamptz,
    label_parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    return_q05 numeric(16,10),
    return_q50 numeric(16,10),
    return_q95 numeric(16,10),
    mae_q50 numeric(16,10),
    mfe_q50 numeric(16,10),
    expected_time_to_target numeric(16,6),
    expected_time_to_stop numeric(16,6),
    uncertainty_lower numeric(16,10),
    uncertainty_upper numeric(16,10),
    regime_observation_id uuid REFERENCES research.regime_observations(regime_observation_id),
    frozen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (data_cutoff_at <= as_of),
    CHECK (horizon_end_at IS NULL OR horizon_end_at > as_of),
    CHECK (uncertainty_lower IS NULL OR uncertainty_upper IS NULL OR uncertainty_lower <= uncertainty_upper),
    CHECK (
        target_before_stop_probability IS NULL
        OR stop_before_target_probability IS NULL
        OR neither_probability IS NULL
        OR abs(target_before_stop_probability + stop_before_target_probability + neither_probability - 1.0) <= 0.000001
    )
);
CREATE INDEX IF NOT EXISTS ix_predictions_model_asof ON research.predictions(model_id, as_of DESC);
CREATE INDEX IF NOT EXISTS ix_predictions_population ON research.predictions(population_id, predicted_rank);

CREATE TABLE IF NOT EXISTS research.prediction_outcomes (
    outcome_id uuid PRIMARY KEY,
    prediction_id uuid NOT NULL UNIQUE REFERENCES research.predictions(prediction_id),
    outcome_class text NOT NULL CHECK (outcome_class IN ('TARGET_FIRST','STOP_FIRST','NEITHER','DATA_INVALID')),
    realised_return_gross numeric(16,10),
    realised_return_net numeric(16,10),
    mae numeric(16,10),
    mfe numeric(16,10),
    time_to_target_seconds bigint,
    time_to_stop_seconds bigint,
    holding_seconds bigint NOT NULL CHECK (holding_seconds >= 0),
    slippage_bps numeric(16,8),
    costs jsonb NOT NULL,
    exit_reason text NOT NULL,
    outcome_quality text NOT NULL CHECK (outcome_quality IN ('VERIFIED','PARTIAL','REJECTED')),
    label_definition_version text NOT NULL,
    settled_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS research.experiments (
    experiment_id uuid PRIMARY KEY,
    experiment_key text NOT NULL UNIQUE,
    model_id uuid NOT NULL REFERENCES model_registry.models(model_id),
    population_manifest_uri text NOT NULL,
    population_manifest_sha256 text NOT NULL,
    validation_method text NOT NULL CHECK (validation_method IN ('PURGED_WALK_FORWARD','EMBARGOED_KFOLD','FORWARD_PAPER')),
    cost_model_version text NOT NULL,
    multiple_testing_method text NOT NULL,
    periods_per_year numeric(12,4) NOT NULL CHECK (periods_per_year > 0),
    top_fraction numeric(8,7) NOT NULL DEFAULT 0.20 CHECK (top_fraction > 0 AND top_fraction <= 1),
    requested_production_weight numeric(10,9) NOT NULL DEFAULT 0.10 CHECK (requested_production_weight > 0 AND requested_production_weight <= 0.15),
    status text NOT NULL CHECK (status IN ('PLANNED','RUNNING','COMPLETED','FAILED')),
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    lineage_complete boolean NOT NULL DEFAULT false,
    leakage_checks_passed boolean NOT NULL DEFAULT false,
    point_in_time_universe_passed boolean NOT NULL DEFAULT false,
    survivorship_control_passed boolean NOT NULL DEFAULT false,
    corporate_action_control_passed boolean NOT NULL DEFAULT false,
    multiple_testing_passed boolean NOT NULL DEFAULT false,
    baseline_comparison_passed boolean NOT NULL DEFAULT false,
    cost_model_verified boolean NOT NULL DEFAULT false,
    seed_stability_passed boolean NOT NULL DEFAULT false,
    ablation_passed boolean NOT NULL DEFAULT false,
    forward_days integer NOT NULL DEFAULT 0 CHECK (forward_days >= 0),
    forward_samples integer NOT NULL DEFAULT 0 CHECK (forward_samples >= 0),
    evidence_manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (completed_at IS NULL OR completed_at >= started_at)
);

CREATE TABLE IF NOT EXISTS research.experiment_predictions (
    experiment_id uuid NOT NULL REFERENCES research.experiments(experiment_id),
    prediction_id uuid NOT NULL REFERENCES research.predictions(prediction_id),
    assigned_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(experiment_id, prediction_id)
);

CREATE TABLE IF NOT EXISTS research.experiment_folds (
    fold_id uuid PRIMARY KEY,
    experiment_id uuid NOT NULL REFERENCES research.experiments(experiment_id),
    fold_number integer NOT NULL CHECK (fold_number >= 0),
    train_start timestamptz NOT NULL,
    train_end timestamptz NOT NULL,
    embargo_end timestamptz NOT NULL,
    test_start timestamptz NOT NULL,
    test_end timestamptz NOT NULL,
    sample_size integer NOT NULL CHECK (sample_size >= 0),
    UNIQUE(experiment_id, fold_number),
    CHECK (train_start < train_end AND train_end <= embargo_end AND embargo_end <= test_start AND test_start < test_end)
);

CREATE TABLE IF NOT EXISTS research.experiment_metrics (
    metric_id uuid PRIMARY KEY,
    experiment_id uuid NOT NULL REFERENCES research.experiments(experiment_id),
    fold_id uuid REFERENCES research.experiment_folds(fold_id),
    fold_key uuid GENERATED ALWAYS AS (COALESCE(fold_id, '00000000-0000-0000-0000-000000000000'::uuid)) STORED,
    regime_label text NOT NULL CHECK (regime_label IN ('ALL','BULL','BEAR','VOLATILE','RANGE','SECTOR_ROTATION')),
    liquidity_band text NOT NULL CHECK (liquidity_band IN ('ALL','LOW','MEDIUM','HIGH')),
    market_cap_band text NOT NULL CHECK (market_cap_band IN ('ALL','SMALL','MID','LARGE')),
    sample_size integer NOT NULL CHECK (sample_size > 0),
    population_count integer NOT NULL CHECK (population_count > 0),
    rank_ic numeric(16,10),
    ndcg numeric(16,10),
    brier_score numeric(16,10),
    calibration_error numeric(16,10),
    net_expectancy numeric(16,10),
    sharpe numeric(16,10),
    sortino numeric(16,10),
    max_drawdown numeric(16,10),
    cvar_95 numeric(16,10),
    turnover numeric(16,10),
    capacity_inr numeric(24,4),
    lower_confidence_net_expectancy numeric(16,10),
    computed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_experiment_metrics_gate ON research.experiment_metrics(experiment_id, regime_label, liquidity_band, market_cap_band);
CREATE UNIQUE INDEX IF NOT EXISTS uq_experiment_metric_stratum
    ON research.experiment_metrics(experiment_id, fold_key, regime_label, liquidity_band, market_cap_band);

CREATE TABLE IF NOT EXISTS deployment.promotion_decisions (
    promotion_decision_id uuid PRIMARY KEY,
    decision_key text NOT NULL UNIQUE,
    model_id uuid NOT NULL REFERENCES model_registry.models(model_id),
    experiment_id uuid NOT NULL REFERENCES research.experiments(experiment_id),
    decision text NOT NULL CHECK (decision IN ('PROMOTED_CHAMPION','PROMOTED_CHALLENGER','REJECTED','DEMOTED','ROLLED_BACK')),
    promotion_rule_version text NOT NULL,
    gate_results jsonb NOT NULL,
    reason text NOT NULL,
    decided_by text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS deployment.assignments (
    assignment_id uuid PRIMARY KEY,
    model_id uuid NOT NULL REFERENCES model_registry.models(model_id),
    desk text NOT NULL CHECK (desk IN ('INTRADAY','DELIVERY')),
    setup_family text NOT NULL,
    horizon_value integer NOT NULL,
    horizon_unit text NOT NULL CHECK (horizon_unit IN ('MINUTE','SESSION','TRADING_DAY')),
    role text NOT NULL CHECK (role IN ('CHAMPION','CHALLENGER')),
    production_weight numeric(10,9) NOT NULL CHECK (production_weight BETWEEN 0 AND 0.15),
    effective_from timestamptz NOT NULL,
    effective_to timestamptz,
    rollback_assignment_id uuid REFERENCES deployment.assignments(assignment_id),
    promotion_decision_id uuid NOT NULL REFERENCES deployment.promotion_decisions(promotion_decision_id),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_champion
    ON deployment.assignments(desk, setup_family, horizon_value, horizon_unit)
    WHERE role='CHAMPION' AND effective_to IS NULL;

CREATE OR REPLACE FUNCTION deployment.assert_assignment_matches_model() RETURNS trigger AS $$
DECLARE
    registered model_registry.models%ROWTYPE;
BEGIN
    SELECT * INTO STRICT registered
      FROM model_registry.models
     WHERE model_id=NEW.model_id;
    IF NEW.desk <> registered.desk
       OR NEW.setup_family <> registered.setup_family
       OR NEW.horizon_value <> registered.horizon_value
       OR NEW.horizon_unit <> registered.horizon_unit THEN
        RAISE EXCEPTION 'assignment scope does not match registered model scope';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_assignment_scope ON deployment.assignments;
CREATE TRIGGER trg_assignment_scope
BEFORE INSERT OR UPDATE OF model_id, desk, setup_family, horizon_value, horizon_unit
ON deployment.assignments
FOR EACH ROW EXECUTE FUNCTION deployment.assert_assignment_matches_model();

CREATE OR REPLACE FUNCTION research.reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is immutable after insertion', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_predictions_immutable ON research.predictions;
CREATE TRIGGER trg_predictions_immutable BEFORE UPDATE OR DELETE ON research.predictions
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();
DROP TRIGGER IF EXISTS trg_outcomes_immutable ON research.prediction_outcomes;
CREATE TRIGGER trg_outcomes_immutable BEFORE UPDATE OR DELETE ON research.prediction_outcomes
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
COMMIT;
