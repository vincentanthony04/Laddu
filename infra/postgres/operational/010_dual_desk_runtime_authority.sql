BEGIN;

CREATE TABLE IF NOT EXISTS trading.desk_candidates (
  candidate_id TEXT PRIMARY KEY,
  instrument_key TEXT,
  symbol TEXT NOT NULL,
  exchange TEXT NOT NULL DEFAULT 'NSE',
  desk TEXT NOT NULL CHECK (desk IN ('intraday','delivery')),
  strategy TEXT NOT NULL DEFAULT 'unspecified',
  direction TEXT NOT NULL DEFAULT 'LONG',
  state TEXT NOT NULL CHECK (state IN ('DISCOVERED','PREQUALIFIED','ENRICHING','READY_FOR_GATE','GATE_EVALUATION','RESEARCH','PROMOTED','REJECTED','EXPIRED')),
  priority DOUBLE PRECISION NOT NULL DEFAULT 0,
  entry_price NUMERIC,
  target_price NUMERIC,
  stop_price NUMERIC,
  score DOUBLE PRECISION,
  model_probability DOUBLE PRECISION,
  risk_reward DOUBLE PRECISION,
  data_freshness JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  missing_requirements JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_methods JSONB NOT NULL DEFAULT '[]'::jsonb,
  next_evaluation_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  decision_id TEXT,
  row_version BIGINT NOT NULL DEFAULT 1,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_desk_candidates_active_identity
ON trading.desk_candidates(desk, symbol, strategy)
WHERE active;
CREATE INDEX IF NOT EXISTS ix_desk_candidates_intraday_queue
ON trading.desk_candidates(state, priority DESC, next_evaluation_at, updated_at)
WHERE active AND desk='intraday';
CREATE INDEX IF NOT EXISTS ix_desk_candidates_delivery_queue
ON trading.desk_candidates(state, next_evaluation_at, priority DESC, updated_at)
WHERE active AND desk='delivery';
CREATE INDEX IF NOT EXISTS ix_desk_candidates_decision
ON trading.desk_candidates(decision_id) WHERE decision_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS trading.desk_candidate_events (
  event_id BIGSERIAL PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES trading.desk_candidates(candidate_id) ON DELETE CASCADE,
  event_sequence BIGINT NOT NULL,
  event_type TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT,
  reason_code TEXT,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(candidate_id,event_sequence)
);
CREATE INDEX IF NOT EXISTS ix_desk_candidate_events_candidate_time
ON trading.desk_candidate_events(candidate_id,event_sequence DESC);

CREATE TABLE IF NOT EXISTS trading.desk_runtime_checkpoints (
  worker_name TEXT PRIMARY KEY,
  desk TEXT NOT NULL CHECK (desk IN ('intraday','delivery')),
  worker_kind TEXT NOT NULL CHECK (worker_kind IN ('candidate','lifecycle')),
  state TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_desk_runtime_checkpoints_desk_kind
ON trading.desk_runtime_checkpoints(desk,worker_kind,heartbeat_at DESC);

GRANT SELECT,INSERT,UPDATE,DELETE ON trading.desk_candidates TO laddu_runtime;
GRANT SELECT,INSERT ON trading.desk_candidate_events TO laddu_runtime;
GRANT USAGE,SELECT ON SEQUENCE trading.desk_candidate_events_event_id_seq TO laddu_runtime;
GRANT SELECT,INSERT,UPDATE,DELETE ON trading.desk_runtime_checkpoints TO laddu_runtime;

COMMIT;
