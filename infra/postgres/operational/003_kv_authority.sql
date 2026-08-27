-- Project Laddu v69.8.3 stabilisation: runtime KV authority migration.
-- Additive and idempotent. Replaces storage.py Store.set_kv/get_kv's
-- unconditional SQLite table with a PostgreSQL operational authority so no
-- normal production startup writes scanner/dashboard/operator config state
-- to SQLite. Does not touch any existing table.
BEGIN;

CREATE TABLE IF NOT EXISTS runtime_control.kv (
    k text PRIMARY KEY,
    v jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- schema_migrations bookkeeping is owned exclusively by
-- provision_production_data_plane.py's apply_postgres(), which computes the
-- file digest and inserts the row itself. This file must not write that row.

COMMIT;
