-- Project Laddu v68.0.0 — dedicated operational PostgreSQL authority
-- This database must run in its own PostgreSQL service/cluster. No research or
-- bulk market-data tables are permitted here.
BEGIN;

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS trading;
CREATE SCHEMA IF NOT EXISTS risk;
CREATE SCHEMA IF NOT EXISTS accounting;
CREATE SCHEMA IF NOT EXISTS integration;
CREATE SCHEMA IF NOT EXISTS runtime_control;

-- Immutable evidence for retained rows that cannot become v68 operational
-- authority.  Quarantine is not a silent discard: every row keeps its exact
-- source payload and SHA-256, with idempotent identity across retries.
CREATE TABLE IF NOT EXISTS integration.legacy_state_quarantine (
    quarantine_id text PRIMARY KEY,
    migration_run_id text NOT NULL,
    source_database text NOT NULL,
    source_table text NOT NULL,
    source_primary_key text NOT NULL,
    reason_code text NOT NULL,
    reason_detail jsonb NOT NULL,
    source_payload jsonb NOT NULL,
    source_payload_sha256 text NOT NULL,
    decision_id text,
    position_id text,
    event_id text,
    mode text,
    active_hint boolean NOT NULL DEFAULT false,
    trading_date date,
    quarantined_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(source_database, source_table, source_primary_key, source_payload_sha256, reason_code)
);
CREATE INDEX IF NOT EXISTS ix_legacy_state_quarantine_reason
    ON integration.legacy_state_quarantine(reason_code, source_table, quarantined_at DESC);
CREATE INDEX IF NOT EXISTS ix_legacy_state_quarantine_decision
    ON integration.legacy_state_quarantine(decision_id) WHERE decision_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS runtime_control.schema_migrations (
    version bigint PRIMARY KEY,
    name text NOT NULL,
    content_sha256 text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS core.instruments (
    instrument_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider_instrument_key text NOT NULL,
    exchange text NOT NULL CHECK (exchange IN ('NSE','BSE')),
    trading_symbol text NOT NULL,
    display_name text NOT NULL,
    isin text,
    asset_class text NOT NULL CHECK (asset_class IN ('CASH_EQUITY','INDEX')),
    exchange_series text NOT NULL,
    lot_size integer NOT NULL DEFAULT 1 CHECK (lot_size > 0),
    tick_size numeric(12,6) NOT NULL DEFAULT 0.05 CHECK (tick_size > 0),
    universe_revision text NOT NULL,
    classification_reason text NOT NULL,
    validation_status text NOT NULL CHECK (validation_status IN ('ACCEPTED','REJECTED','QUARANTINED')),
    active_from timestamptz NOT NULL DEFAULT clock_timestamp(),
    active_to timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (active_to IS NULL OR active_to > active_from),
    UNIQUE (provider_instrument_key, universe_revision),
    UNIQUE (exchange, trading_symbol, universe_revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_core_instruments_active_provider_key
    ON core.instruments(provider_instrument_key) WHERE active_to IS NULL AND validation_status='ACCEPTED';
CREATE UNIQUE INDEX IF NOT EXISTS uq_core_instruments_active_isin
    ON core.instruments(isin) WHERE isin IS NOT NULL AND active_to IS NULL AND validation_status='ACCEPTED';
CREATE INDEX IF NOT EXISTS ix_core_instruments_active_symbol
    ON core.instruments(exchange, trading_symbol) WHERE active_to IS NULL AND validation_status='ACCEPTED';

CREATE TABLE IF NOT EXISTS trading.portfolios (
    portfolio_id uuid PRIMARY KEY,
    portfolio_code text NOT NULL UNIQUE,
    portfolio_type text NOT NULL CHECK (portfolio_type IN ('MODEL_PAPER','MANUAL_CAPTURE')),
    base_currency char(3) NOT NULL DEFAULT 'INR',
    initial_equity numeric(20,4) NOT NULL CHECK (initial_equity >= 0),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS trading.trade_intents (
    intent_id uuid PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    portfolio_id uuid NOT NULL REFERENCES trading.portfolios(portfolio_id),
    instrument_id bigint NOT NULL REFERENCES core.instruments(instrument_id),
    desk text NOT NULL CHECK (desk IN ('INTRADAY','DELIVERY')),
    strategy_id text NOT NULL,
    setup_family text NOT NULL,
    side text NOT NULL CHECK (side IN ('BUY','SELL')),
    requested_quantity bigint NOT NULL CHECK (requested_quantity > 0),
    approved_quantity bigint CHECK (approved_quantity >= 0 AND approved_quantity <= requested_quantity),
    entry_type text NOT NULL CHECK (entry_type IN ('MARKET_REFERENCE','LIMIT_REFERENCE','BREAKOUT_REFERENCE')),
    entry_price numeric(20,6),
    stop_price numeric(20,6) NOT NULL,
    target_price numeric(20,6),
    status text NOT NULL DEFAULT 'PROPOSED' CHECK (status IN (
        'PROPOSED','RISK_APPROVED','RISK_REJECTED','ACTIVE','PARTIAL','COMPLETED','CANCELLED','EXPIRED'
    )),
    decision_record_id text NOT NULL,
    model_assignment_id uuid,
    source_event_time timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    CHECK (target_price IS NULL OR target_price > 0),
    CHECK (entry_price IS NULL OR entry_price > 0),
    CHECK (stop_price > 0)
);
CREATE INDEX IF NOT EXISTS ix_trade_intents_status_created ON trading.trade_intents(status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_trade_intents_portfolio_instrument ON trading.trade_intents(portfolio_id, instrument_id, created_at DESC);

CREATE TABLE IF NOT EXISTS trading.intent_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_key text NOT NULL UNIQUE,
    intent_id uuid NOT NULL REFERENCES trading.trade_intents(intent_id),
    event_type text NOT NULL CHECK (event_type IN (
        'PROPOSED','RISK_APPROVED','RISK_REJECTED','ACTIVATED','PARTIAL_FILL','FILLED','STOP_UPDATED',
        'TARGET_UPDATED','CANCELLED','EXPIRED','CLOSED','RECONCILED'
    )),
    event_time timestamptz NOT NULL,
    canonical_sequence bigint NOT NULL CHECK (canonical_sequence > 0),
    actor text NOT NULL,
    payload jsonb NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (intent_id, canonical_sequence)
);
CREATE INDEX IF NOT EXISTS ix_intent_events_intent_time ON trading.intent_events(intent_id, event_time, event_id);

CREATE TABLE IF NOT EXISTS trading.execution_fills (
    fill_id uuid PRIMARY KEY,
    fill_key text NOT NULL UNIQUE,
    intent_id uuid NOT NULL REFERENCES trading.trade_intents(intent_id),
    execution_channel text NOT NULL CHECK (execution_channel IN ('MODEL_PAPER','MANUAL_CAPTURE')),
    side text NOT NULL CHECK (side IN ('BUY','SELL')),
    quantity bigint NOT NULL CHECK (quantity > 0),
    price numeric(20,6) NOT NULL CHECK (price > 0),
    gross_value numeric(24,6) GENERATED ALWAYS AS (quantity * price) STORED,
    event_time timestamptz NOT NULL,
    source_reference text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_execution_fills_intent_time ON trading.execution_fills(intent_id, event_time);

CREATE TABLE IF NOT EXISTS trading.positions (
    position_id uuid PRIMARY KEY,
    portfolio_id uuid NOT NULL REFERENCES trading.portfolios(portfolio_id),
    strategy_id text NOT NULL,
    instrument_id bigint NOT NULL REFERENCES core.instruments(instrument_id),
    desk text NOT NULL CHECK (desk IN ('INTRADAY','DELIVERY')),
    signed_quantity bigint NOT NULL,
    average_price numeric(20,6) NOT NULL CHECK (average_price > 0),
    original_stop numeric(20,6) NOT NULL CHECK (original_stop > 0),
    managed_stop numeric(20,6) NOT NULL CHECK (managed_stop > 0),
    target_price numeric(20,6),
    status text NOT NULL CHECK (status IN ('OPEN','CLOSED')),
    opened_at timestamptz NOT NULL,
    closed_at timestamptz,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((status='OPEN' AND closed_at IS NULL AND signed_quantity <> 0)
        OR (status='CLOSED' AND closed_at IS NOT NULL AND signed_quantity = 0))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_open_position
    ON trading.positions(portfolio_id, strategy_id, instrument_id, desk)
    WHERE status='OPEN';
CREATE INDEX IF NOT EXISTS ix_positions_open_portfolio ON trading.positions(portfolio_id, instrument_id) WHERE status='OPEN';

CREATE TABLE IF NOT EXISTS trading.model_paper_positions (
    position_id text PRIMARY KEY,
    source_signal_id text NOT NULL UNIQUE,
    symbol text NOT NULL,
    exchange text NOT NULL CHECK (exchange IN ('NSE','BSE')),
    mode text NOT NULL CHECK (mode IN ('intraday','delivery')),
    side text NOT NULL CHECK (side IN ('LONG','SHORT')),
    status text NOT NULL CHECK (status IN ('OPEN','CLOSED')),
    quantity bigint NOT NULL CHECK (quantity >= 0),
    original_entry numeric(20,6) NOT NULL CHECK (original_entry > 0),
    original_target numeric(20,6) NOT NULL CHECK (original_target > 0),
    original_stop numeric(20,6) NOT NULL CHECK (original_stop > 0),
    managed_stop numeric(20,6) NOT NULL CHECK (managed_stop > 0),
    entry_price numeric(20,6) NOT NULL CHECK (entry_price > 0),
    last_price numeric(20,6) NOT NULL CHECK (last_price > 0),
    exit_price numeric(20,6),
    notional numeric(24,6) NOT NULL CHECK (notional >= 0),
    reserved_cost numeric(20,6) NOT NULL CHECK (reserved_cost >= 0),
    gross_pnl numeric(20,6) NOT NULL DEFAULT 0,
    total_cost numeric(20,6) NOT NULL DEFAULT 0,
    net_pnl numeric(20,6) NOT NULL DEFAULT 0,
    open_risk numeric(20,6) NOT NULL CHECK (open_risk >= 0),
    high_watermark numeric(20,6),
    low_watermark numeric(20,6),
    hit_status text NOT NULL DEFAULT 'NONE',
    action text NOT NULL,
    exit_reason text,
    economic_outcome text,
    signal_outcome text,
    data_failure boolean NOT NULL DEFAULT false,
    opened_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    closed_at timestamptz,
    cost_version text NOT NULL,
    payload jsonb NOT NULL,
    row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
    CHECK ((status='OPEN' AND closed_at IS NULL AND quantity > 0) OR (status='CLOSED' AND closed_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_paper_open_symbol
    ON trading.model_paper_positions(symbol) WHERE status='OPEN';
CREATE INDEX IF NOT EXISTS ix_model_paper_status_mode
    ON trading.model_paper_positions(status, mode, opened_at DESC);


CREATE TABLE IF NOT EXISTS trading.canonical_decisions (
    decision_id text PRIMARY KEY,
    thesis_id text NOT NULL,
    thesis_key text NOT NULL,
    signal_id text NOT NULL,
    symbol text NOT NULL,
    exchange text NOT NULL CHECK (exchange IN ('NSE','BSE')),
    mode text NOT NULL CHECK (mode IN ('intraday','delivery')),
    side text NOT NULL CHECK (side IN ('LONG','SHORT')),
    setup_family text NOT NULL,
    activation_window text NOT NULL,
    trading_date date NOT NULL,
    state text NOT NULL CHECK (state IN ('WATCHING','PREPARED','TRIGGERED','CONFIRMED','WEAKENING','INVALIDATED','COMPLETED','REJECTED')),
    decision_action text,
    publication_authority text NOT NULL CHECK (publication_authority IN ('NOT_PUBLISHABLE','MODEL_PAPER','CAPITAL')),
    execution_authority text NOT NULL CHECK (execution_authority IN ('BLOCKED','CAPITAL_ALLOWED')),
    entry_plan jsonb NOT NULL,
    risk_plan jsonb NOT NULL,
    candidate_snapshot jsonb NOT NULL,
    frozen_evidence jsonb,
    frozen_evidence_hash text,
    live_snapshot jsonb NOT NULL,
    confidence jsonb NOT NULL,
    data_lineage jsonb NOT NULL,
    rejection_reasons jsonb NOT NULL,
    latest_payload jsonb NOT NULL,
    outcome jsonb,
    model_version text,
    policy_version text,
    pipeline_version text,
    record_version bigint NOT NULL DEFAULT 1 CHECK (record_version > 0),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    activated_at timestamptz,
    closed_at timestamptz,
    contract_version text NOT NULL,
    CHECK ((active AND state NOT IN ('INVALIDATED','COMPLETED','REJECTED')) OR (NOT active AND state IN ('INVALIDATED','COMPLETED','REJECTED')))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_canonical_active_thesis
    ON trading.canonical_decisions(thesis_key) WHERE active;
CREATE UNIQUE INDEX IF NOT EXISTS uq_canonical_active_delivery_symbol_side
    ON trading.canonical_decisions(symbol, side) WHERE active AND mode='delivery';
CREATE INDEX IF NOT EXISTS ix_canonical_today
    ON trading.canonical_decisions(trading_date, mode, state, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_canonical_symbol
    ON trading.canonical_decisions(symbol, mode, side, active, updated_at DESC);

CREATE TABLE IF NOT EXISTS trading.canonical_decision_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_key text NOT NULL UNIQUE,
    decision_id text NOT NULL REFERENCES trading.canonical_decisions(decision_id),
    thesis_id text NOT NULL,
    event_type text NOT NULL,
    from_state text,
    to_state text,
    reason text,
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL,
    contract_version text NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_canonical_events_decision
    ON trading.canonical_decision_events(decision_id, event_id);

CREATE TABLE IF NOT EXISTS risk.control_state (
    singleton_id smallint PRIMARY KEY CHECK (singleton_id=1),
    operator_stop boolean NOT NULL DEFAULT false,
    reason text,
    updated_by text NOT NULL,
    external_daily_pnl numeric(20,4),
    external_equity numeric(20,4),
    equity_peak numeric(20,4),
    account_as_of timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO risk.control_state(singleton_id, operator_stop, updated_by)
VALUES(1,false,'schema-bootstrap') ON CONFLICT(singleton_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS risk.candidate_admissions (
    admission_id text PRIMARY KEY,
    occurred_at timestamptz NOT NULL,
    symbol text,
    mode text CHECK (mode IS NULL OR mode IN ('intraday','delivery')),
    admission_state text NOT NULL,
    reason_codes jsonb NOT NULL,
    input_snapshot jsonb NOT NULL,
    report jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_candidate_admissions_time
    ON risk.candidate_admissions(occurred_at DESC);

CREATE TABLE IF NOT EXISTS trading.position_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_key text NOT NULL UNIQUE,
    position_id uuid NOT NULL REFERENCES trading.positions(position_id),
    event_type text NOT NULL CHECK (event_type IN (
        'OPENED','INCREASED','REDUCED','STOP_MOVED','TARGET_MOVED','MARKED','CLOSED','RECONCILED'
    )),
    event_time timestamptz NOT NULL,
    canonical_sequence bigint NOT NULL CHECK (canonical_sequence > 0),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (position_id, canonical_sequence)
);

CREATE TABLE IF NOT EXISTS risk.policy_versions (
    policy_version text PRIMARY KEY,
    effective_from timestamptz NOT NULL,
    effective_to timestamptz,
    policy jsonb NOT NULL,
    policy_sha256 text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE IF NOT EXISTS risk.decisions (
    risk_decision_id uuid PRIMARY KEY,
    decision_key text NOT NULL UNIQUE,
    intent_id uuid NOT NULL REFERENCES trading.trade_intents(intent_id),
    policy_version text NOT NULL REFERENCES risk.policy_versions(policy_version),
    decision text NOT NULL CHECK (decision IN ('APPROVED','REJECTED','SIZED_DOWN')),
    requested_quantity bigint NOT NULL CHECK (requested_quantity > 0),
    approved_quantity bigint NOT NULL CHECK (approved_quantity >= 0 AND approved_quantity <= requested_quantity),
    reason_codes text[] NOT NULL DEFAULT '{}',
    account_equity numeric(20,4) NOT NULL,
    available_cash numeric(20,4) NOT NULL,
    realised_pnl_day numeric(20,4) NOT NULL,
    unrealised_pnl numeric(20,4) NOT NULL,
    existing_gross_exposure numeric(20,4) NOT NULL,
    proposed_gross_exposure numeric(20,4) NOT NULL,
    portfolio_heat numeric(12,6) NOT NULL,
    sector_exposure numeric(20,4) NOT NULL,
    open_position_count integer NOT NULL,
    market_data_age_ms bigint NOT NULL CHECK (market_data_age_ms >= 0),
    input_snapshot jsonb NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_intent ON risk.decisions(intent_id, decided_at DESC);

CREATE TABLE IF NOT EXISTS accounting.accounts (
    account_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_code text NOT NULL UNIQUE,
    account_name text NOT NULL,
    account_type text NOT NULL CHECK (account_type IN ('ASSET','LIABILITY','EQUITY','REVENUE','EXPENSE')),
    currency char(3) NOT NULL DEFAULT 'INR',
    is_active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS accounting.journal_entries (
    journal_entry_id uuid PRIMARY KEY,
    entry_key text NOT NULL UNIQUE,
    portfolio_id uuid REFERENCES trading.portfolios(portfolio_id),
    event_time timestamptz NOT NULL,
    description text NOT NULL,
    source_type text NOT NULL,
    source_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS accounting.journal_postings (
    posting_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    journal_entry_id uuid NOT NULL REFERENCES accounting.journal_entries(journal_entry_id) ON DELETE RESTRICT,
    account_id bigint NOT NULL REFERENCES accounting.accounts(account_id),
    debit numeric(20,4) NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit numeric(20,4) NOT NULL DEFAULT 0 CHECK (credit >= 0),
    instrument_id bigint REFERENCES core.instruments(instrument_id),
    memo text,
    CHECK ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0))
);
CREATE INDEX IF NOT EXISTS ix_journal_postings_entry ON accounting.journal_postings(journal_entry_id);

CREATE OR REPLACE FUNCTION accounting.assert_balanced_journal() RETURNS trigger AS $$
DECLARE
    target_id uuid;
    total_debit numeric(20,4);
    total_credit numeric(20,4);
BEGIN
    target_id := COALESCE(NEW.journal_entry_id, OLD.journal_entry_id);
    SELECT COALESCE(sum(debit),0), COALESCE(sum(credit),0)
      INTO total_debit, total_credit
      FROM accounting.journal_postings WHERE journal_entry_id=target_id;
    IF total_debit <> total_credit THEN
        RAISE EXCEPTION 'journal entry % is unbalanced: debit %, credit %', target_id, total_debit, total_credit;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_balanced_journal ON accounting.journal_postings;
CREATE CONSTRAINT TRIGGER trg_balanced_journal
AFTER INSERT OR UPDATE OR DELETE ON accounting.journal_postings
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION accounting.assert_balanced_journal();

CREATE TABLE IF NOT EXISTS integration.event_inbox (
    inbox_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_system text NOT NULL,
    source_event_key text NOT NULL,
    event_type text NOT NULL,
    event_time timestamptz NOT NULL,
    payload jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    processed_at timestamptz,
    processing_error text,
    UNIQUE (source_system, source_event_key)
);
CREATE INDEX IF NOT EXISTS ix_event_inbox_pending ON integration.event_inbox(inbox_id) WHERE processed_at IS NULL;

CREATE TABLE IF NOT EXISTS integration.transactional_outbox (
    outbox_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_key text NOT NULL UNIQUE,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    claimed_at timestamptz,
    claimed_by text,
    published_at timestamptz,
    projection_part text,
    publish_attempts integer NOT NULL DEFAULT 0,
    last_error text
);
ALTER TABLE integration.transactional_outbox
    ADD COLUMN IF NOT EXISTS projection_part text;
CREATE INDEX IF NOT EXISTS ix_transactional_outbox_pending
    ON integration.transactional_outbox(outbox_id) WHERE published_at IS NULL;

CREATE OR REPLACE FUNCTION integration.claim_outbox(p_worker text, p_limit integer)
RETURNS SETOF integration.transactional_outbox AS $$
BEGIN
    RETURN QUERY
    WITH picked AS (
        SELECT outbox_id FROM integration.transactional_outbox
        WHERE published_at IS NULL
          AND (claimed_at IS NULL OR claimed_at < clock_timestamp() - interval '2 minutes')
        ORDER BY outbox_id
        FOR UPDATE SKIP LOCKED
        LIMIT GREATEST(1, LEAST(p_limit, 1000))
    )
    UPDATE integration.transactional_outbox o
       SET claimed_at=clock_timestamp(), claimed_by=p_worker, publish_attempts=publish_attempts+1
      FROM picked
     WHERE o.outbox_id=picked.outbox_id
    RETURNING o.*;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION core.set_updated_at_and_version() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    NEW.version := OLD.version + 1;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_trade_intents_version ON trading.trade_intents;
CREATE TRIGGER trg_trade_intents_version BEFORE UPDATE ON trading.trade_intents
FOR EACH ROW EXECUTE FUNCTION core.set_updated_at_and_version();
DROP TRIGGER IF EXISTS trg_positions_version ON trading.positions;
CREATE TRIGGER trg_positions_version BEFORE UPDATE ON trading.positions
FOR EACH ROW EXECUTE FUNCTION core.set_updated_at_and_version();

-- Application roles are created by the provisioning script. The runtime role
-- receives no CREATE/ALTER/DROP privileges and research roles never connect to
-- this database.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
COMMIT;
