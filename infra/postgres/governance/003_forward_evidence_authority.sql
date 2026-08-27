-- Project Laddu v70.0.0 — immutable three-arm forward-evidence authority.
-- Local runtime SQLite remains a bounded capture/read projection.  This
-- PostgreSQL schema is the durable governance authority for complete candidate
-- populations, point-in-time arm predictions, independently settled outcomes
-- and hash-chained Level 5 maturity checkpoints.
BEGIN;

CREATE TABLE IF NOT EXISTS research.selector_populations (
    population_fingerprint text PRIMARY KEY,
    desk text NOT NULL CHECK (desk IN ('INTRADAY','DELIVERY')),
    observed_at timestamptz NOT NULL,
    universe_id text NOT NULL,
    dataset_fingerprint text NOT NULL,
    feature_manifest_hash text NOT NULL,
    candidate_count integer NOT NULL CHECK (candidate_count > 0),
    policy_version text NOT NULL,
    payload_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_selector_populations_desk_time
    ON research.selector_populations(desk, observed_at DESC);

CREATE TABLE IF NOT EXISTS research.selector_population_members (
    candidate_id text PRIMARY KEY,
    population_fingerprint text NOT NULL REFERENCES research.selector_populations(population_fingerprint),
    instrument_key text NOT NULL,
    symbol text NOT NULL,
    exchange text NOT NULL,
    desk text NOT NULL CHECK (desk IN ('INTRADAY','DELIVERY')),
    side text NOT NULL CHECK (side IN ('LONG','SHORT','UNKNOWN')),
    observed_at timestamptz NOT NULL,
    feature_hash text NOT NULL,
    feature_payload jsonb NOT NULL,
    payload_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(population_fingerprint, candidate_id)
);
CREATE INDEX IF NOT EXISTS ix_selector_members_population
    ON research.selector_population_members(population_fingerprint, symbol);
CREATE INDEX IF NOT EXISTS ix_selector_members_symbol_time
    ON research.selector_population_members(desk, symbol, observed_at DESC);

CREATE TABLE IF NOT EXISTS research.selector_arm_predictions (
    prediction_key text PRIMARY KEY,
    population_fingerprint text NOT NULL REFERENCES research.selector_populations(population_fingerprint),
    candidate_id text NOT NULL REFERENCES research.selector_population_members(candidate_id),
    arm text NOT NULL CHECK (arm IN ('heuristic','quant','hybrid')),
    model_version text NOT NULL,
    score numeric(20,10) NOT NULL,
    predicted_rank integer NOT NULL CHECK (predicted_rank > 0),
    predicted_percentile numeric(12,8) NOT NULL CHECK (predicted_percentile BETWEEN 0 AND 100),
    probability_positive numeric(12,10) CHECK (probability_positive BETWEEN 0 AND 1),
    expected_net_return_bps numeric(20,10),
    prediction_at timestamptz NOT NULL,
    prediction_payload jsonb NOT NULL,
    payload_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(population_fingerprint, candidate_id, arm)
);
CREATE INDEX IF NOT EXISTS ix_selector_predictions_population_arm
    ON research.selector_arm_predictions(population_fingerprint, arm, predicted_rank);
CREATE INDEX IF NOT EXISTS ix_selector_predictions_model_time
    ON research.selector_arm_predictions(arm, model_version, prediction_at DESC);

CREATE TABLE IF NOT EXISTS research.selector_outcomes (
    outcome_key text PRIMARY KEY,
    candidate_id text NOT NULL REFERENCES research.selector_population_members(candidate_id),
    population_fingerprint text NOT NULL REFERENCES research.selector_populations(population_fingerprint),
    horizon text NOT NULL,
    observed_at timestamptz NOT NULL,
    settled_at timestamptz NOT NULL,
    market_regime text NOT NULL,
    result text NOT NULL CHECK (result IN ('SUCCESS','FAIL','BREAKEVEN','EXPIRED','INVALIDATED','TARGET','STOP','TARGET_FIRST','STOP_FIRST','WIN','LOSS')),
    gross_return_bps numeric(20,10),
    net_return_bps numeric(20,10) NOT NULL,
    actual_cost_bps numeric(20,10),
    same_bar_ambiguous boolean NOT NULL DEFAULT false,
    proof_payload jsonb NOT NULL,
    record_hash text NOT NULL,
    payload_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(candidate_id, horizon),
    CHECK (settled_at > observed_at)
);
CREATE INDEX IF NOT EXISTS ix_selector_outcomes_population_horizon
    ON research.selector_outcomes(population_fingerprint, horizon);
CREATE INDEX IF NOT EXISTS ix_selector_outcomes_settled
    ON research.selector_outcomes(settled_at DESC);

CREATE TABLE IF NOT EXISTS research.forward_maturity_checkpoints (
    checkpoint_id text PRIMARY KEY,
    previous_checkpoint_hash text,
    checkpoint_hash text NOT NULL UNIQUE,
    build_version text NOT NULL,
    policy_version text NOT NULL,
    maturity_state text NOT NULL,
    evidence_cutoff_at timestamptz NOT NULL,
    checkpoint_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_forward_maturity_checkpoints_time
    ON research.forward_maturity_checkpoints(created_at DESC);

DROP TRIGGER IF EXISTS trg_selector_populations_immutable ON research.selector_populations;
CREATE TRIGGER trg_selector_populations_immutable
BEFORE UPDATE OR DELETE ON research.selector_populations
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();

DROP TRIGGER IF EXISTS trg_selector_population_members_immutable ON research.selector_population_members;
CREATE TRIGGER trg_selector_population_members_immutable
BEFORE UPDATE OR DELETE ON research.selector_population_members
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();

DROP TRIGGER IF EXISTS trg_selector_arm_predictions_immutable ON research.selector_arm_predictions;
CREATE TRIGGER trg_selector_arm_predictions_immutable
BEFORE UPDATE OR DELETE ON research.selector_arm_predictions
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();

DROP TRIGGER IF EXISTS trg_selector_outcomes_immutable ON research.selector_outcomes;
CREATE TRIGGER trg_selector_outcomes_immutable
BEFORE UPDATE OR DELETE ON research.selector_outcomes
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();

DROP TRIGGER IF EXISTS trg_forward_maturity_checkpoints_immutable ON research.forward_maturity_checkpoints;
CREATE TRIGGER trg_forward_maturity_checkpoints_immutable
BEFORE UPDATE OR DELETE ON research.forward_maturity_checkpoints
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();

COMMIT;
