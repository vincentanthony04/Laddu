BEGIN;

-- Append-only Level-5 signal lifecycle: generation/open/reassessment/management/settlement.
-- Model-Paper remains the only execution simulation authority; these rows are evidence only.
CREATE TABLE IF NOT EXISTS trading.signal_lifecycle_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_key text NOT NULL UNIQUE,
    signal_id text NOT NULL,
    position_id text,
    decision_id text,
    event_type text NOT NULL CHECK (event_type IN ('GENERATED','OPENED','REASSESSED','MANAGED','SETTLED')),
    thesis_state text CHECK (thesis_state IS NULL OR thesis_state IN ('VALID','WEAKENING','INVALIDATED')),
    occurred_at timestamptz NOT NULL,
    authority_version text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_signal_lifecycle_signal_time
    ON trading.signal_lifecycle_events(signal_id, occurred_at, event_id);
CREATE INDEX IF NOT EXISTS ix_signal_lifecycle_position_time
    ON trading.signal_lifecycle_events(position_id, occurred_at, event_id)
    WHERE position_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_signal_lifecycle_type_time
    ON trading.signal_lifecycle_events(event_type, occurred_at DESC);

GRANT SELECT, INSERT ON TABLE trading.signal_lifecycle_events TO laddu_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA trading TO laddu_runtime;

COMMIT;
