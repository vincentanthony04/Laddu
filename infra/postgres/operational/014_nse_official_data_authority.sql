-- Project Laddu v83: source-specific, point-in-time NSE official data authority.
BEGIN;
CREATE SCHEMA IF NOT EXISTS reference;

CREATE TABLE IF NOT EXISTS reference.nse_official_ingestion_runs (
  source_key text NOT NULL, trade_date date NOT NULL, content_hash char(64) NOT NULL,
  source_url text, source_filename text NOT NULL, row_count integer NOT NULL CHECK (row_count >= 0),
  rows_projected integer NOT NULL DEFAULT 0 CHECK (rows_projected >= 0),
  state text NOT NULL, projected_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_key,trade_date,content_hash)
);

CREATE TABLE IF NOT EXISTS reference.nse_daily_security_facts (
  source_key text NOT NULL, trade_date date NOT NULL, source_record_id text NOT NULL,
  symbol text NOT NULL, series text, isin text, open double precision, high double precision,
  low double precision, close double precision, volume double precision, turnover double precision,
  number_of_trades double precision, traded_qty double precision, deliverable_qty double precision,
  delivery_pct double precision, daily_volatility double precision, var_margin double precision,
  impact_cost double precision, price_band_low double precision, price_band_high double precision,
  published_at timestamptz, content_hash char(64) NOT NULL, raw_payload jsonb NOT NULL,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_key,trade_date,source_record_id)
);
CREATE INDEX IF NOT EXISTS nse_daily_security_symbol_date_idx ON reference.nse_daily_security_facts(symbol,trade_date DESC);

CREATE TABLE IF NOT EXISTS reference.nse_security_master_history (
  trade_date date NOT NULL, source_record_id text NOT NULL, symbol text NOT NULL, series text,
  isin text, instrument_name text, listing_status text, eligible_universe text,
  instrument_status text, listing_date date, published_at timestamptz,
  content_hash char(64) NOT NULL, raw_payload jsonb NOT NULL, ingested_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (trade_date,source_record_id)
);
CREATE INDEX IF NOT EXISTS nse_security_master_symbol_date_idx ON reference.nse_security_master_history(symbol,trade_date DESC);
CREATE INDEX IF NOT EXISTS nse_security_master_isin_date_idx ON reference.nse_security_master_history(isin,trade_date DESC);

CREATE TABLE IF NOT EXISTS reference.nse_index_membership_history (
  trade_date date NOT NULL, source_record_id text NOT NULL, index_name text NOT NULL,
  symbol text NOT NULL, isin text, index_weight double precision, index_return double precision,
  market_cap double precision, free_float_market_cap double precision, beta double precision,
  sector_name text, published_at timestamptz, content_hash char(64) NOT NULL,
  raw_payload jsonb NOT NULL, ingested_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (trade_date,source_record_id)
);
CREATE INDEX IF NOT EXISTS nse_index_membership_index_date_idx ON reference.nse_index_membership_history(index_name,trade_date DESC);
CREATE INDEX IF NOT EXISTS nse_index_membership_symbol_date_idx ON reference.nse_index_membership_history(symbol,trade_date DESC);

CREATE TABLE IF NOT EXISTS reference.nse_market_events (
  source_key text NOT NULL, trade_date date NOT NULL, source_record_id text NOT NULL,
  symbol text NOT NULL, isin text, event_type text NOT NULL, participant text,
  participant_category text, deal_side text, deal_type text, deal_price double precision,
  counterparty text, bulk_qty double precision, block_qty double precision, short_qty double precision,
  margin_qty double precision, ex_date date, record_date date, action_type text, purpose text,
  price_factor double precision, volume_factor double precision, surveillance_flag text,
  surveillance_category text, high_52w double precision, low_52w double precision,
  price_band_change_pct double precision, published_at timestamptz, content_hash char(64) NOT NULL,
  raw_payload jsonb NOT NULL, ingested_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_key,trade_date,source_record_id)
);
CREATE INDEX IF NOT EXISTS nse_market_events_symbol_date_idx ON reference.nse_market_events(symbol,trade_date DESC);
CREATE INDEX IF NOT EXISTS nse_market_events_type_date_idx ON reference.nse_market_events(event_type,trade_date DESC);

CREATE TABLE IF NOT EXISTS reference.nse_filing_events (
  trade_date date NOT NULL, source_record_id text NOT NULL, symbol text NOT NULL, isin text,
  filing_type text, filing_period text, filing_timestamp timestamptz, announcement_category text,
  announcement_text text, revenue double precision, ebitda double precision, net_profit double precision,
  eps double precision, promoter_holding_pct double precision, fii_holding_pct double precision,
  dii_holding_pct double precision, ownership_change_pct double precision, published_at timestamptz,
  content_hash char(64) NOT NULL, raw_payload jsonb NOT NULL, ingested_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (trade_date,source_record_id)
);
CREATE INDEX IF NOT EXISTS nse_filing_events_symbol_date_idx ON reference.nse_filing_events(symbol,trade_date DESC);
COMMIT;
