-- Project Laddu v70.0.0 — retained-data tombstone for the retired Options capability.
-- Existing rows are preserved for audit/migration evidence.  The current
-- Intraday/Delivery runtime has no API, repository method or write authority.
BEGIN;
COMMENT ON TABLE reference.option_chain_snapshot IS
  'RETIRED: historical Options-chain evidence only; no active Project Laddu production capability.';
REVOKE ALL ON TABLE reference.option_chain_snapshot FROM PUBLIC;
COMMIT;
