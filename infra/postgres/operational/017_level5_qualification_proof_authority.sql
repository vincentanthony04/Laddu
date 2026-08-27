-- Project Laddu v86: ML population qualification and current-build operational proof history.
BEGIN;
CREATE SCHEMA IF NOT EXISTS runtime_control;

CREATE TABLE IF NOT EXISTS runtime_control.ml_population_qualification_runs (
  run_id bigserial PRIMARY KEY,
  build_version text NOT NULL,
  state text NOT NULL,
  official_source_current integer NOT NULL DEFAULT 0,
  official_source_total integer NOT NULL DEFAULT 0,
  delivery jsonb NOT NULL,
  intraday jsonb NOT NULL,
  payload_hash text NOT NULL,
  captured_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(build_version,payload_hash)
);
CREATE INDEX IF NOT EXISTS ml_population_qualification_runs_build_idx
  ON runtime_control.ml_population_qualification_runs(build_version,captured_at DESC);

CREATE TABLE IF NOT EXISTS runtime_control.level5_operational_proof_runs (
  run_id bigserial PRIMARY KEY,
  build_version text NOT NULL,
  state text NOT NULL,
  passed boolean NOT NULL DEFAULT false,
  gates jsonb NOT NULL,
  missing_gates jsonb NOT NULL DEFAULT '[]'::jsonb,
  evidence_hash text NOT NULL,
  captured_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(build_version,evidence_hash)
);
CREATE INDEX IF NOT EXISTS level5_operational_proof_runs_build_idx
  ON runtime_control.level5_operational_proof_runs(build_version,captured_at DESC);
COMMIT;
