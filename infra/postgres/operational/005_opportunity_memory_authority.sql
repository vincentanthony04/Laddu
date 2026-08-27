-- Project Laddu v69.8.3 stabilisation: opportunity memory authority migration.
-- Additive and idempotent. Replaces OpportunityMemoryRepository's
-- unconditional SQLite table with a PostgreSQL operational authority.
-- schema_migrations bookkeeping is owned by provision_production_data_plane.py.
BEGIN;

CREATE TABLE IF NOT EXISTS trading.opportunity_memory (
    symbol text NOT NULL,
    exchange text NOT NULL DEFAULT 'NSE',
    mode text NOT NULL,
    stage text NOT NULL DEFAULT 'Potential',
    priority_score integer NOT NULL DEFAULT 0,
    sector text,
    themes_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    priority_reason text,
    trigger text,
    invalidation text,
    target_window text,
    next_scan_at timestamptz,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, mode)
);

CREATE INDEX IF NOT EXISTS opportunity_memory_stage_score_idx
    ON trading.opportunity_memory (mode, stage, priority_score DESC, updated_at DESC);

COMMIT;
