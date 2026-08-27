-- Project Laddu v68.0.0 — QuestDB market time-series authority
CREATE TABLE IF NOT EXISTS market_ticks (
    provider_ts TIMESTAMP,
    instrument_key SYMBOL CAPACITY 16384 CACHE,
    exchange SYMBOL CAPACITY 4 CACHE,
    symbol SYMBOL CAPACITY 16384 CACHE,
    canonical_sequence LONG,
    provider_sequence LONG,
    received_ts TIMESTAMP,
    ltp DOUBLE,
    last_quantity DOUBLE,
    cumulative_volume DOUBLE,
    bid DOUBLE,
    ask DOUBLE,
    open_interest DOUBLE,
    identity_verified BOOLEAN,
    quality_state SYMBOL CAPACITY 16 CACHE,
    replay_state SYMBOL CAPACITY 16 CACHE,
    payload_json VARCHAR
) TIMESTAMP(provider_ts) PARTITION BY DAY WAL
DEDUP UPSERT KEYS(provider_ts, instrument_key, canonical_sequence);

CREATE TABLE IF NOT EXISTS market_bars (
    bar_end_ts TIMESTAMP,
    instrument_key SYMBOL CAPACITY 16384 CACHE,
    interval SYMBOL CAPACITY 16 CACHE,
    bar_start_ts TIMESTAMP,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume DOUBLE,
    open_interest DOUBLE,
    tick_count LONG,
    is_closed BOOLEAN,
    is_partial_session_bar BOOLEAN,
    source SYMBOL CAPACITY 32 CACHE,
    quality_state SYMBOL CAPACITY 16 CACHE,
    universe_revision SYMBOL CAPACITY 64 CACHE
) TIMESTAMP(bar_end_ts) PARTITION BY MONTH WAL
DEDUP UPSERT KEYS(bar_end_ts, instrument_key, interval);

CREATE TABLE IF NOT EXISTS market_data_quality_events (
    event_ts TIMESTAMP,
    instrument_key SYMBOL CAPACITY 16384 CACHE,
    event_type SYMBOL CAPACITY 32 CACHE,
    source_sequence LONG,
    canonical_sequence LONG,
    gap_size LONG,
    detail VARCHAR
) TIMESTAMP(event_ts) PARTITION BY MONTH WAL;
