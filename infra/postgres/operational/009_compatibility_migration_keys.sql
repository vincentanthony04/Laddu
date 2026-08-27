-- Idempotence keys for one-time retained SQLite compatibility projection migration.
BEGIN;
ALTER TABLE reference.bulk_block_deals
    ADD COLUMN IF NOT EXISTS source_hash char(64);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bulk_block_deals_source_hash
    ON reference.bulk_block_deals(source_hash) WHERE source_hash IS NOT NULL;

ALTER TABLE reference.option_chain_snapshot
    ADD COLUMN IF NOT EXISTS source_hash char(64);
CREATE UNIQUE INDEX IF NOT EXISTS uq_option_chain_snapshot_source_hash
    ON reference.option_chain_snapshot(source_hash) WHERE source_hash IS NOT NULL;

ALTER TABLE trading.manual_trade_journal
    ADD COLUMN IF NOT EXISTS legacy_source_key text;
CREATE UNIQUE INDEX IF NOT EXISTS uq_manual_trade_journal_legacy_source_key
    ON trading.manual_trade_journal(legacy_source_key) WHERE legacy_source_key IS NOT NULL;

ALTER TABLE runtime_control.daily_learning
    ADD COLUMN IF NOT EXISTS legacy_source_key text;
CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_learning_legacy_source_key
    ON runtime_control.daily_learning(legacy_source_key) WHERE legacy_source_key IS NOT NULL;
COMMIT;
