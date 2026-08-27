-- Project Laddu v69.8.6 complete-alignment authority migration.
-- Canonical decisions remain the single signal ledger.  This migration adds
-- only mutable operator priority, manual-trade, daily-learning and immutable
-- outcome-attribution tables that were still using compatibility SQLite.
BEGIN;

CREATE TABLE IF NOT EXISTS trading.priority_symbols (
    symbol text NOT NULL,
    exchange text NOT NULL DEFAULT 'NSE' CHECK (exchange IN ('NSE','BSE')),
    mode text NOT NULL CHECK (mode IN ('intraday','delivery','all')),
    source text NOT NULL DEFAULT 'search',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, exchange, mode)
);
CREATE INDEX IF NOT EXISTS priority_symbols_created_idx
    ON trading.priority_symbols (created_at DESC);

CREATE TABLE IF NOT EXISTS trading.manual_trade_journal (
    trade_id bigserial PRIMARY KEY,
    symbol text NOT NULL,
    exchange text NOT NULL DEFAULT 'NSE' CHECK (exchange IN ('NSE','BSE')),
    mode text NOT NULL CHECK (mode IN ('intraday','delivery')),
    side text NOT NULL CHECK (side IN ('LONG','SHORT')),
    entry numeric(20,6),
    exit numeric(20,6),
    quantity numeric(20,6),
    status text NOT NULL,
    pnl numeric(20,6),
    holding_minutes numeric(20,4),
    notes text,
    opened_at timestamptz,
    closed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    legacy_source_key text UNIQUE
);
CREATE INDEX IF NOT EXISTS manual_trade_journal_mode_opened_idx
    ON trading.manual_trade_journal (mode, opened_at DESC, trade_id DESC);

CREATE TABLE IF NOT EXISTS runtime_control.daily_learning (
    learning_id bigserial PRIMARY KEY,
    learning_date date NOT NULL DEFAULT CURRENT_DATE,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    legacy_source_key text UNIQUE
);
CREATE INDEX IF NOT EXISTS daily_learning_created_idx
    ON runtime_control.daily_learning (created_at DESC);

CREATE TABLE IF NOT EXISTS trading.outcome_learning (
    signal_id text PRIMARY KEY,
    decision_id text,
    symbol text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('intraday','delivery')),
    side text NOT NULL CHECK (side IN ('LONG','SHORT')),
    result text NOT NULL,
    pnl_points numeric(20,6),
    holding_minutes numeric(20,4),
    attribution text,
    features jsonb NOT NULL DEFAULT '{}'::jsonb,
    proof jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_version text,
    closed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS outcome_learning_mode_closed_idx
    ON trading.outcome_learning (mode, closed_at DESC, created_at DESC);

COMMIT;
