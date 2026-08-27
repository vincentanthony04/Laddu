-- Project Laddu v69.8.3 stabilisation: reference-data authority migration.
-- Additive and idempotent. Covers the remaining core.reference_data_repository
-- (ReferenceDataRepository) surface not already cut over by
-- DeliveryLakeRepository (save_delivery_rows/latest_delivery/
-- save_delivery_data/get_delivery_data stay on that repository -- untouched
-- here): bulk/block deals, F&O ban list, market breadth, reference-run
-- status, fundamentals cache, option chain snapshots, earnings calendar.
-- schema_migrations bookkeeping is owned by provision_production_data_plane.py,
-- not this file.
BEGIN;

CREATE SCHEMA IF NOT EXISTS reference;

CREATE TABLE IF NOT EXISTS reference.bulk_block_deals (
    id bigserial PRIMARY KEY,
    trade_date date NOT NULL,
    symbol text NOT NULL,
    deal_type text NOT NULL,
    client_name text,
    buy_sell text,
    qty double precision,
    price double precision,
    source_hash char(64) UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bulk_block_deals_symbol_date_idx
    ON reference.bulk_block_deals (symbol, trade_date DESC);
CREATE INDEX IF NOT EXISTS bulk_block_deals_date_idx
    ON reference.bulk_block_deals (trade_date DESC);

CREATE TABLE IF NOT EXISTS reference.fno_ban_list (
    trade_date date NOT NULL,
    symbol text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS reference.market_breadth_daily (
    ts timestamptz NOT NULL,
    universe text NOT NULL,
    advances integer NOT NULL,
    declines integer NOT NULL,
    unchanged integer NOT NULL,
    PRIMARY KEY (ts, universe)
);
CREATE INDEX IF NOT EXISTS market_breadth_daily_universe_ts_idx
    ON reference.market_breadth_daily (universe, ts DESC);

CREATE TABLE IF NOT EXISTS reference.reference_data_runs (
    job_name text NOT NULL,
    run_date date NOT NULL,
    status text NOT NULL,
    rows_written integer NOT NULL DEFAULT 0,
    error text,
    finished_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_name, run_date)
);
CREATE INDEX IF NOT EXISTS reference_data_runs_finished_idx
    ON reference.reference_data_runs (finished_at DESC);

CREATE TABLE IF NOT EXISTS reference.fundamentals_cache (
    isin text PRIMARY KEY,
    ok boolean NOT NULL,
    payload_json jsonb NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reference.option_chain_snapshot (
    id bigserial PRIMARY KEY,
    underlying text NOT NULL,
    expiry text NOT NULL,
    ts timestamptz NOT NULL DEFAULT now(),
    pcr_oi double precision,
    pcr_volume double precision,
    max_pain double precision,
    total_call_oi double precision,
    total_put_oi double precision,
    top_strikes_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_hash char(64) UNIQUE
);
CREATE INDEX IF NOT EXISTS option_chain_snapshot_underlying_expiry_ts_idx
    ON reference.option_chain_snapshot (underlying, expiry, ts DESC);
CREATE INDEX IF NOT EXISTS option_chain_snapshot_underlying_ts_idx
    ON reference.option_chain_snapshot (underlying, ts DESC);

CREATE TABLE IF NOT EXISTS reference.earnings_calendar (
    symbol text NOT NULL,
    event_date date NOT NULL,
    event_type text NOT NULL DEFAULT 'board_meeting',
    purpose text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, event_date, event_type)
);
CREATE INDEX IF NOT EXISTS earnings_calendar_event_date_idx
    ON reference.earnings_calendar (event_date);

COMMIT;
