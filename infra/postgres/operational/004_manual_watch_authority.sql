-- Project Laddu v69.8.3 stabilisation: manual watchlist authority migration.
-- Additive and idempotent. Replaces ManualWatchRepository's unconditional
-- SQLite table with a PostgreSQL operational authority. schema_migrations
-- bookkeeping is owned by provision_production_data_plane.py, not this file.
BEGIN;

CREATE TABLE IF NOT EXISTS trading.manual_watch (
    symbol text NOT NULL,
    exchange text NOT NULL DEFAULT 'NSE',
    mode text NOT NULL,
    side text NOT NULL DEFAULT 'WAIT',
    state text NOT NULL DEFAULT 'WATCH',
    waiting_for text,
    trigger text,
    invalidation text,
    reason text,
    pinned boolean NOT NULL DEFAULT false,
    source text NOT NULL DEFAULT 'manual_search',
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, mode)
);

CREATE INDEX IF NOT EXISTS manual_watch_pinned_updated_idx
    ON trading.manual_watch (pinned DESC, updated_at DESC);

COMMIT;
