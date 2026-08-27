-- Project Laddu v76.0.0 — destructive cleanup of forbidden active capabilities.
-- Rollback evidence is the installer-created external JSONL evidence snapshot, never an active table.
BEGIN;
DROP TABLE IF EXISTS reference.option_chain_snapshot CASCADE;
DROP TABLE IF EXISTS reference.fno_ban_list CASCADE;
DROP TABLE IF EXISTS integration.legacy_state_quarantine CASCADE;
DELETE FROM trading.manual_watch WHERE lower(mode) NOT IN ('intraday','delivery');
DELETE FROM trading.opportunity_memory WHERE lower(mode) NOT IN ('intraday','delivery');
DELETE FROM trading.manual_trade_journal WHERE lower(mode) NOT IN ('intraday','delivery');
DELETE FROM trading.outcome_learning WHERE lower(mode) NOT IN ('intraday','delivery');
DELETE FROM trading.model_paper_positions WHERE lower(mode) NOT IN ('intraday','delivery');
DELETE FROM trading.canonical_decisions WHERE lower(mode) NOT IN ('intraday','delivery');
ALTER TABLE trading.manual_watch DROP CONSTRAINT IF EXISTS manual_watch_mode_check;
ALTER TABLE trading.manual_watch ADD CONSTRAINT manual_watch_mode_check CHECK (mode IN ('intraday','delivery')) NOT VALID;
ALTER TABLE trading.manual_watch VALIDATE CONSTRAINT manual_watch_mode_check;
ALTER TABLE trading.opportunity_memory DROP CONSTRAINT IF EXISTS opportunity_memory_mode_check;
ALTER TABLE trading.opportunity_memory ADD CONSTRAINT opportunity_memory_mode_check CHECK (mode IN ('intraday','delivery')) NOT VALID;
ALTER TABLE trading.opportunity_memory VALIDATE CONSTRAINT opportunity_memory_mode_check;
COMMIT;
