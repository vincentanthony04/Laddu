-- Project Laddu v85: immutable evidence snapshots and resumable pipeline leases.
BEGIN;
CREATE SCHEMA IF NOT EXISTS runtime_control;

ALTER TABLE runtime_control.priority_pipeline_jobs
  ADD COLUMN IF NOT EXISTS lease_owner text,
  ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_progress_at timestamptz,
  ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS recovery_count integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_error text;

ALTER TABLE runtime_control.priority_pipeline_stages
  ADD COLUMN IF NOT EXISTS lease_owner text,
  ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_progress_at timestamptz,
  ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS recovery_count integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_error text;

CREATE TABLE IF NOT EXISTS runtime_control.priority_pipeline_recovery_events (
  event_id bigserial PRIMARY KEY,
  job_id uuid NOT NULL REFERENCES runtime_control.priority_pipeline_jobs(job_id) ON DELETE CASCADE,
  stage_key text,
  prior_state text,
  new_state text NOT NULL,
  reason text NOT NULL,
  lease_owner text,
  recovery_count integer NOT NULL DEFAULT 0,
  occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS priority_pipeline_recovery_events_job_idx
  ON runtime_control.priority_pipeline_recovery_events(job_id,occurred_at DESC);

CREATE TABLE IF NOT EXISTS runtime_control.canonical_evidence_snapshots (
  snapshot_id uuid PRIMARY KEY,
  symbol text NOT NULL,
  instrument_key text NOT NULL,
  mode text NOT NULL CHECK (mode IN ('intraday','delivery')),
  state text NOT NULL,
  completeness_pct double precision NOT NULL CHECK (completeness_pct >= 0 AND completeness_pct <= 100),
  component_states jsonb NOT NULL,
  components jsonb NOT NULL,
  blockers jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_revisions jsonb NOT NULL DEFAULT '{}'::jsonb,
  payload_hash text NOT NULL,
  build_version text NOT NULL,
  captured_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(symbol,mode,payload_hash)
);
CREATE INDEX IF NOT EXISTS canonical_evidence_snapshots_symbol_idx
  ON runtime_control.canonical_evidence_snapshots(symbol,mode,captured_at DESC);

CREATE TABLE IF NOT EXISTS runtime_control.cross_plane_reconciliation_runs (
  run_id uuid PRIMARY KEY,
  symbol text NOT NULL,
  instrument_key text NOT NULL,
  interval text NOT NULL,
  state text NOT NULL,
  canonical_count bigint NOT NULL DEFAULT 0,
  planes jsonb NOT NULL,
  mismatches jsonb NOT NULL DEFAULT '[]'::jsonb,
  repair_plan jsonb NOT NULL DEFAULT '[]'::jsonb,
  evidence_hash text NOT NULL,
  build_version text NOT NULL,
  captured_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cross_plane_reconciliation_symbol_idx
  ON runtime_control.cross_plane_reconciliation_runs(symbol,interval,captured_at DESC);
COMMIT;
