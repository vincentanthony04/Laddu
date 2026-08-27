-- AC-075: immutable execution-model identity and calibration lineage for Model Paper.
-- The execution contract is frozen at admission and reused for marks/settlement.
-- CALIBRATION_PENDING is permitted as an honest conservative source state; only
-- sufficient fresh installed market evidence may produce CALIBRATED.
BEGIN;

ALTER TABLE trading.model_paper_positions
    ADD COLUMN IF NOT EXISTS execution_model_version text,
    ADD COLUMN IF NOT EXISTS execution_model_contract_hash text,
    ADD COLUMN IF NOT EXISTS execution_calibration_state text,
    ADD COLUMN IF NOT EXISTS execution_calibration_snapshot_hash text,
    ADD COLUMN IF NOT EXISTS execution_model jsonb;

UPDATE trading.model_paper_positions
   SET execution_model_version=COALESCE(NULLIF(execution_model_version,''),NULLIF(payload->'execution_model'->>'execution_model_version','')),
       execution_model_contract_hash=COALESCE(NULLIF(execution_model_contract_hash,''),NULLIF(payload->'execution_model'->>'contract_hash','')),
       execution_calibration_state=COALESCE(NULLIF(execution_calibration_state,''),NULLIF(payload->'execution_model'->>'calibration_state','')),
       execution_calibration_snapshot_hash=COALESCE(NULLIF(execution_calibration_snapshot_hash,''),NULLIF(payload->'execution_model'->>'calibration_snapshot_hash','')),
       execution_model=COALESCE(execution_model,payload->'execution_model')
 WHERE execution_model_version IS NULL
    OR execution_model_contract_hash IS NULL
    OR execution_calibration_state IS NULL
    OR execution_model IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='trading.model_paper_positions'::regclass
           AND conname='ck_model_paper_execution_calibration_state'
    ) THEN
        ALTER TABLE trading.model_paper_positions
            ADD CONSTRAINT ck_model_paper_execution_calibration_state CHECK (
                execution_calibration_state IS NULL OR execution_calibration_state IN ('CALIBRATED','CALIBRATION_PENDING')
            );
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION trading.reject_model_paper_execution_model_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.execution_model_version IS DISTINCT FROM OLD.execution_model_version
       OR NEW.execution_model_contract_hash IS DISTINCT FROM OLD.execution_model_contract_hash
       OR NEW.execution_calibration_state IS DISTINCT FROM OLD.execution_calibration_state
       OR NEW.execution_calibration_snapshot_hash IS DISTINCT FROM OLD.execution_calibration_snapshot_hash
       OR NEW.execution_model IS DISTINCT FROM OLD.execution_model THEN
        RAISE EXCEPTION 'MODEL_PAPER_EXECUTION_MODEL_IS_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid='trading.model_paper_positions'::regclass
           AND tgname='trg_model_paper_execution_model_immutable'
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_model_paper_execution_model_immutable
        BEFORE UPDATE OF execution_model_version,execution_model_contract_hash,execution_calibration_state,
                         execution_calibration_snapshot_hash,execution_model
        ON trading.model_paper_positions
        FOR EACH ROW EXECUTE FUNCTION trading.reject_model_paper_execution_model_mutation();
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS ix_model_paper_execution_model
    ON trading.model_paper_positions(execution_model_version,execution_calibration_state,opened_at DESC);

COMMIT;
