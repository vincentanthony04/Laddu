-- Project Laddu v69.8.0 fix-forward authority migration.
-- Additive and idempotent: retained v69.7 decisions, positions, risk state,
-- credentials, QuestDB data and Parquet data are not rewritten or deleted.
BEGIN;

CREATE SCHEMA IF NOT EXISTS market_data;
CREATE SCHEMA IF NOT EXISTS scanner;

CREATE TABLE IF NOT EXISTS core.securities (
    security_id uuid PRIMARY KEY,
    isin char(12) NOT NULL UNIQUE,
    company_id uuid NOT NULL,
    security_type text NOT NULL CHECK (security_type='ORDINARY_EQUITY'),
    share_class text NOT NULL DEFAULT 'ORDINARY',
    face_value numeric(12,4),
    lifecycle_state text NOT NULL CHECK (lifecycle_state IN (
        'ANNOUNCED','PRELISTED','ACTIVE_UNVERIFIED','DATA_ACCUMULATING',
        'DELIVERY_ELIGIBLE','INTRADAY_ELIGIBLE','SUSPENDED','DELISTED'
    )),
    effective_from date NOT NULL,
    effective_to date,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE TABLE IF NOT EXISTS core.listings (
    listing_id uuid PRIMARY KEY,
    security_id uuid NOT NULL REFERENCES core.securities(security_id),
    exchange text NOT NULL CHECK (exchange IN ('NSE','BSE')),
    segment text NOT NULL CHECK (segment IN ('NSE_EQ','BSE_EQ')),
    symbol text NOT NULL,
    series_group text NOT NULL,
    provider_instrument_key text NOT NULL,
    display_name text NOT NULL,
    listing_state text NOT NULL CHECK (listing_state IN ('ACTIVE','SUSPENDED','DELISTED','PRELISTED')),
    is_canonical boolean NOT NULL DEFAULT false,
    effective_from date NOT NULL,
    effective_to date,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    UNIQUE(exchange, symbol, effective_from),
    UNIQUE(provider_instrument_key, effective_from)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_listings_one_current_canonical
    ON core.listings(security_id) WHERE is_canonical AND effective_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_listings_symbol_alias
    ON core.listings(symbol, exchange, effective_from DESC);

CREATE TABLE IF NOT EXISTS core.security_lifecycle_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_key text NOT NULL UNIQUE,
    event_type text NOT NULL CHECK (event_type IN (
        'NEW_LISTING','SUSPENDED','RESUMED','DELISTED','SYMBOL_CHANGED',
        'COMPANY_NAME_CHANGED','SERIES_CHANGED','PRIMARY_LISTING_CHANGED','INSTRUMENT_KEY_CHANGED'
    )),
    security_id uuid NOT NULL REFERENCES core.securities(security_id),
    listing_id uuid REFERENCES core.listings(listing_id),
    effective_date date NOT NULL,
    previous_state jsonb,
    current_state jsonb,
    source_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS core.universe_snapshots (
    snapshot_id text PRIMARY KEY,
    effective_date date NOT NULL,
    desk text NOT NULL CHECK (desk IN ('DELIVERY','INTRADAY')),
    rule_version text NOT NULL,
    content_hash char(64) NOT NULL,
    population_count integer NOT NULL CHECK (population_count >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_universe_snapshots_latest
    ON core.universe_snapshots(desk, effective_date DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_universe_snapshots_rule
    ON core.universe_snapshots(effective_date, desk, rule_version);

CREATE TABLE IF NOT EXISTS core.universe_snapshot_members (
    snapshot_id text NOT NULL REFERENCES core.universe_snapshots(snapshot_id) ON DELETE RESTRICT,
    security_id uuid NOT NULL REFERENCES core.securities(security_id),
    listing_id uuid NOT NULL REFERENCES core.listings(listing_id),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    inclusion_reasons jsonb NOT NULL,
    PRIMARY KEY(snapshot_id, security_id),
    UNIQUE(snapshot_id, listing_id),
    UNIQUE(snapshot_id, ordinal)
);

CREATE TABLE IF NOT EXISTS core.universe_snapshot_exclusions (
    snapshot_id text NOT NULL REFERENCES core.universe_snapshots(snapshot_id) ON DELETE RESTRICT,
    security_id uuid,
    listing_id uuid,
    provider_instrument_key text,
    reason_code text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(snapshot_id, reason_code, provider_instrument_key)
);

CREATE TABLE IF NOT EXISTS market_data.coverage (
    security_id uuid NOT NULL REFERENCES core.securities(security_id),
    interval text NOT NULL,
    earliest_stored_ts timestamptz,
    latest_stored_ts timestamptz,
    expected_latest_completed_ts timestamptz NOT NULL,
    verified_ranges jsonb NOT NULL DEFAULT '[]'::jsonb,
    missing_ranges jsonb NOT NULL DEFAULT '[]'::jsonb,
    adjustment_version text NOT NULL,
    data_version text NOT NULL,
    quality_state text NOT NULL CHECK (quality_state IN (
        'ACCEPTED','REPAIRED','QUARANTINED_IDENTITY','QUARANTINED_CANDLE',
        'DEFERRED_PROVIDER','EXCLUDED_INSTRUMENT','UNSCORABLE_DATA_GAP'
    )),
    last_verified_at timestamptz NOT NULL,
    PRIMARY KEY(security_id, interval, data_version)
);

CREATE TABLE IF NOT EXISTS market_data.hydration_jobs (
    job_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    security_id uuid NOT NULL REFERENCES core.securities(security_id),
    interval text NOT NULL,
    range_from timestamptz NOT NULL,
    range_to timestamptz NOT NULL,
    data_version text NOT NULL,
    priority smallint NOT NULL CHECK (priority BETWEEN 0 AND 5),
    reason_code text NOT NULL,
    state text NOT NULL CHECK (state IN ('QUEUED','CLAIMED','COMPLETE','DEFERRED_RATE_LIMIT','FAILED')),
    attempts integer NOT NULL DEFAULT 0,
    claimed_by text,
    claimed_at timestamptz,
    completed_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (range_to > range_from),
    UNIQUE(security_id, interval, range_from, range_to, data_version)
);
CREATE INDEX IF NOT EXISTS ix_hydration_jobs_ready
    ON market_data.hydration_jobs(priority, job_id) WHERE state IN ('QUEUED','DEFERRED_RATE_LIMIT');

CREATE TABLE IF NOT EXISTS market_data.request_audit (
    request_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    security_id uuid NOT NULL REFERENCES core.securities(security_id),
    interval text NOT NULL,
    requested_from timestamptz NOT NULL,
    requested_to timestamptz NOT NULL,
    outcome text NOT NULL CHECK (outcome IN (
        'CACHE_HIT','CACHE_PARTIAL_DELTA','PROVIDER_GAP_FETCH','DEFERRED_RATE_LIMIT',
        'GOVERNED_FULL_REBUILD','UNSCORABLE_DATA_GAP'
    )),
    missing_ranges jsonb NOT NULL,
    data_version text NOT NULL,
    governed_reason text,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS scanner.scan_runs (
    run_id text PRIMARY KEY,
    snapshot_id text NOT NULL REFERENCES core.universe_snapshots(snapshot_id),
    desk text NOT NULL CHECK (desk IN ('DELIVERY','INTRADAY')),
    population_count integer NOT NULL CHECK (population_count >= 0),
    terminal_count integer NOT NULL DEFAULT 0 CHECK (terminal_count >= 0),
    candidate_count integer NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    scanner_version text NOT NULL,
    market_state text NOT NULL,
    state text NOT NULL CHECK (state IN ('RUNNING','COMPLETE','FAILED')),
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    CHECK (terminal_count <= population_count),
    CHECK (state <> 'COMPLETE' OR terminal_count = population_count)
);

CREATE TABLE IF NOT EXISTS scanner.scanner_evaluations (
    run_id text NOT NULL REFERENCES scanner.scan_runs(run_id) ON DELETE RESTRICT,
    security_id uuid NOT NULL REFERENCES core.securities(security_id),
    listing_id uuid NOT NULL REFERENCES core.listings(listing_id),
    terminal_state text NOT NULL CHECK (terminal_state IN (
        'ANALYSED','CANDIDATE','REJECTED_PRICE','REJECTED_LIQUIDITY','REJECTED_DATA',
        'DEFERRED_HISTORY','DEFERRED_RATE_LIMIT','IDENTITY_ERROR'
    )),
    priority_tier text NOT NULL CHECK (priority_tier IN ('P0','P1','P2','P3','P4')),
    priority_score numeric(12,6) NOT NULL,
    research_state text NOT NULL CHECK (research_state IN (
        'RESEARCH','WATCH','PREPARING','WAITING_FOR_CONFIRMATION','REJECTED'
    )),
    canonical_decision_allowed boolean NOT NULL DEFAULT false,
    evidence jsonb NOT NULL,
    evaluated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(run_id, security_id)
);

CREATE TABLE IF NOT EXISTS scanner.candidate_rejections (
    run_id text NOT NULL,
    security_id uuid NOT NULL,
    reason_code text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(run_id, security_id, reason_code),
    FOREIGN KEY(run_id, security_id) REFERENCES scanner.scanner_evaluations(run_id, security_id)
);

CREATE TABLE IF NOT EXISTS scanner.candidate_evidence (
    run_id text NOT NULL,
    security_id uuid NOT NULL,
    evidence_key text NOT NULL,
    evidence_value jsonb NOT NULL,
    source_version text NOT NULL,
    PRIMARY KEY(run_id, security_id, evidence_key),
    FOREIGN KEY(run_id, security_id) REFERENCES scanner.scanner_evaluations(run_id, security_id)
);

COMMIT;
