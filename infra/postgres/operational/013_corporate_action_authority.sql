-- Project Laddu v81: point-in-time corporate-action adjustment authority.
-- Factors are supplied by an independently verified source. The platform does
-- not infer split/bonus ratios from price jumps and never marks coverage
-- complete without an explicit source attestation.
BEGIN;

CREATE SCHEMA IF NOT EXISTS reference;

CREATE TABLE IF NOT EXISTS reference.corporate_actions (
    action_id text PRIMARY KEY,
    instrument_key text NOT NULL,
    exchange text NOT NULL,
    trading_symbol text NOT NULL,
    isin text,
    ex_date date NOT NULL,
    action_type text NOT NULL CHECK (action_type IN ('SPLIT','BONUS','CONSOLIDATION','RIGHTS','OTHER')),
    price_factor double precision NOT NULL CHECK (price_factor > 0),
    volume_factor double precision NOT NULL CHECK (volume_factor > 0),
    source_name text NOT NULL,
    source_record_id text,
    source_hash char(64) NOT NULL UNIQUE,
    published_at timestamptz,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    verified boolean NOT NULL DEFAULT false,
    verification_note text
);
CREATE INDEX IF NOT EXISTS corporate_actions_instrument_ex_date_idx
    ON reference.corporate_actions (instrument_key, ex_date DESC);
CREATE INDEX IF NOT EXISTS corporate_actions_symbol_ex_date_idx
    ON reference.corporate_actions (exchange, trading_symbol, ex_date DESC);

CREATE TABLE IF NOT EXISTS reference.corporate_action_coverage (
    instrument_key text PRIMARY KEY,
    exchange text NOT NULL,
    trading_symbol text NOT NULL,
    coverage_start date NOT NULL,
    coverage_end date NOT NULL,
    coverage_basis text NOT NULL DEFAULT 'FULL_LISTING_HISTORY',
    source_name text NOT NULL,
    source_hash char(64) NOT NULL,
    complete boolean NOT NULL DEFAULT false,
    verified_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (coverage_end >= coverage_start)
);
CREATE INDEX IF NOT EXISTS corporate_action_coverage_complete_idx
    ON reference.corporate_action_coverage (complete, exchange, trading_symbol);

COMMIT;
