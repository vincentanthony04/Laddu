-- Allow corrected bounded universe snapshots to supersede stale same-day snapshots.
-- Snapshot IDs remain immutable; latest created_at is authoritative.
BEGIN;

ALTER TABLE core.universe_snapshots
    DROP CONSTRAINT IF EXISTS universe_snapshots_effective_date_desk_rule_version_key;
ALTER TABLE core.universe_snapshots
    DROP CONSTRAINT IF EXISTS universe_snapshots_desk_content_hash_key;

CREATE INDEX IF NOT EXISTS ix_universe_snapshots_latest
    ON core.universe_snapshots(desk, effective_date DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_universe_snapshots_rule
    ON core.universe_snapshots(effective_date, desk, rule_version);

COMMIT;
