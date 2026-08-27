-- Project Laddu v84: durable priority-stock pipeline authority.
BEGIN;
CREATE SCHEMA IF NOT EXISTS runtime_control;

CREATE TABLE IF NOT EXISTS runtime_control.priority_pipeline_jobs (
  job_id uuid PRIMARY KEY,
  job_key text NOT NULL UNIQUE,
  symbol text NOT NULL,
  instrument_key text NOT NULL,
  mode text NOT NULL CHECK (mode IN ('intraday','delivery')),
  action text NOT NULL,
  priority integer NOT NULL DEFAULT 100,
  state text NOT NULL,
  current_stage text,
  completed_stages integer NOT NULL DEFAULT 0 CHECK (completed_stages >= 0),
  total_stages integer NOT NULL DEFAULT 10 CHECK (total_stages > 0),
  progress_pct double precision NOT NULL DEFAULT 0 CHECK (progress_pct >= 0 AND progress_pct <= 100),
  eta_low_seconds integer,
  eta_high_seconds integer,
  blocker text,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS priority_pipeline_jobs_symbol_idx
  ON runtime_control.priority_pipeline_jobs(symbol,mode,updated_at DESC);

CREATE TABLE IF NOT EXISTS runtime_control.priority_pipeline_stages (
  job_id uuid NOT NULL REFERENCES runtime_control.priority_pipeline_jobs(job_id) ON DELETE CASCADE,
  stage_order smallint NOT NULL,
  stage_key text NOT NULL,
  state text NOT NULL,
  completed_units bigint,
  total_units bigint,
  throughput_per_sec double precision,
  eta_low_seconds integer,
  eta_high_seconds integer,
  detail text,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  PRIMARY KEY(job_id,stage_key),
  UNIQUE(job_id,stage_order)
);
CREATE INDEX IF NOT EXISTS priority_pipeline_stage_state_idx
  ON runtime_control.priority_pipeline_stages(state,updated_at DESC);
COMMIT;
