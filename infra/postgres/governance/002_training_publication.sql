BEGIN;

CREATE TABLE IF NOT EXISTS research.training_publications (
    publication_id text PRIMARY KEY,
    model_key text NOT NULL,
    model_version text NOT NULL,
    desk text NOT NULL CHECK (desk IN ('INTRADAY','DELIVERY')),
    horizon_value integer NOT NULL CHECK (horizon_value > 0),
    horizon_unit text NOT NULL CHECK (horizon_unit IN ('MINUTE','SESSION','TRADING_DAY')),
    lifecycle_state text NOT NULL CHECK (lifecycle_state IN ('SHADOW','REJECTED','RETIRED')),
    evaluation_paper_weight numeric(10,9) NOT NULL DEFAULT 0 CHECK (evaluation_paper_weight BETWEEN 0 AND 0.15),
    production_weight numeric(10,9) NOT NULL DEFAULT 0 CHECK (production_weight = 0),
    feature_schema_hash text NOT NULL,
    dataset_fingerprint text NOT NULL,
    training_data_source text NOT NULL CHECK (training_data_source IN ('PARQUET_DUCKDB','SQLITE_EXPLICIT_RECOVERY')),
    validation_state text NOT NULL,
    validation_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_payload jsonb NOT NULL,
    payload_sha256 text NOT NULL,
    trained_through date,
    artifact_uri text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(model_key, model_version, dataset_fingerprint)
);

CREATE TABLE IF NOT EXISTS research.shadow_predictions (
    prediction_id text PRIMARY KEY,
    publication_id text NOT NULL REFERENCES research.training_publications(publication_id),
    model_key text NOT NULL,
    instrument_key text,
    symbol text NOT NULL,
    desk text NOT NULL CHECK (desk IN ('INTRADAY','DELIVERY')),
    as_of timestamptz NOT NULL,
    horizon_value integer NOT NULL CHECK (horizon_value > 0),
    horizon_unit text NOT NULL CHECK (horizon_unit IN ('MINUTE','SESSION','TRADING_DAY')),
    predicted_rank numeric(12,8) NOT NULL CHECK (predicted_rank BETWEEN 0 AND 100),
    expected_excess_return numeric(16,10),
    calibrated_confidence numeric(12,10) NOT NULL CHECK (calibrated_confidence BETWEEN 0 AND 1),
    feature_schema_hash text NOT NULL,
    dataset_fingerprint text NOT NULL,
    payload_json jsonb NOT NULL,
    settled_outcome_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(publication_id, symbol, as_of)
);
CREATE INDEX IF NOT EXISTS ix_shadow_predictions_model_asof
    ON research.shadow_predictions(model_key, as_of DESC);
CREATE INDEX IF NOT EXISTS ix_shadow_predictions_unsettled
    ON research.shadow_predictions(as_of)
    WHERE settled_outcome_id IS NULL;

CREATE TABLE IF NOT EXISTS research.factor_decay_observations (
    observation_id uuid PRIMARY KEY,
    publication_id text NOT NULL REFERENCES research.training_publications(publication_id),
    factor_name text NOT NULL,
    measured_at timestamptz NOT NULL,
    status text NOT NULL,
    payload_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(publication_id, factor_name, measured_at)
);

CREATE TABLE IF NOT EXISTS research.training_publication_events (
    event_id uuid PRIMARY KEY,
    publication_id text NOT NULL REFERENCES research.training_publications(publication_id),
    event_type text NOT NULL,
    payload_json jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

DROP TRIGGER IF EXISTS trg_training_publications_immutable ON research.training_publications;
CREATE TRIGGER trg_training_publications_immutable
BEFORE UPDATE OR DELETE ON research.training_publications
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();
DROP TRIGGER IF EXISTS trg_shadow_predictions_immutable ON research.shadow_predictions;
CREATE TRIGGER trg_shadow_predictions_immutable
BEFORE UPDATE OR DELETE ON research.shadow_predictions
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();
DROP TRIGGER IF EXISTS trg_factor_decay_observations_immutable ON research.factor_decay_observations;
CREATE TRIGGER trg_factor_decay_observations_immutable
BEFORE UPDATE OR DELETE ON research.factor_decay_observations
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();
DROP TRIGGER IF EXISTS trg_training_publication_events_immutable ON research.training_publication_events;
CREATE TRIGGER trg_training_publication_events_immutable
BEFORE UPDATE OR DELETE ON research.training_publication_events
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();

COMMIT;
