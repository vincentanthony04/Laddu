BEGIN;

-- Runtime-control authorities were introduced after the original application
-- role grants.  Existing installations therefore created the tables but left
-- the bounded runtime role unable to read or advance their state.  Keep this
-- repair as an immutable forward migration rather than editing 015-017.
GRANT USAGE ON SCHEMA runtime_control TO laddu_runtime;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    runtime_control.priority_pipeline_jobs,
    runtime_control.priority_pipeline_stages,
    runtime_control.priority_pipeline_recovery_events,
    runtime_control.canonical_evidence_snapshots,
    runtime_control.cross_plane_reconciliation_runs,
    runtime_control.ml_population_qualification_runs,
    runtime_control.level5_operational_proof_runs
TO laddu_runtime;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA runtime_control TO laddu_runtime;

COMMIT;
