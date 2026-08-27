-- AC-074: durable exact market identity + observation watermark for open-position gap recovery.
-- These fields record the last canonical market observation that actually
-- advanced Model Paper. Recovery is local-only; no provider resolution/fetch.
BEGIN;

ALTER TABLE trading.model_paper_positions
    ADD COLUMN IF NOT EXISTS instrument_key text,
    ADD COLUMN IF NOT EXISTS last_market_observation_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_market_observation_sequence bigint,
    ADD COLUMN IF NOT EXISTS gap_recovery_state text;

UPDATE trading.model_paper_positions
   SET instrument_key=COALESCE(NULLIF(instrument_key,''),NULLIF(payload->>'instrument_key',''),NULLIF(payload->>'provider_instrument_key','')),
       last_market_observation_at=COALESCE(last_market_observation_at,opened_at),
       gap_recovery_state=COALESCE(gap_recovery_state,'HISTORICAL_WATERMARK_FROM_OPEN')
 WHERE instrument_key IS NULL OR instrument_key=''
    OR last_market_observation_at IS NULL
    OR gap_recovery_state IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='trading.model_paper_positions'::regclass
           AND conname='ck_model_paper_market_observation_order'
    ) THEN
        ALTER TABLE trading.model_paper_positions
            ADD CONSTRAINT ck_model_paper_market_observation_order CHECK (
                last_market_observation_at IS NULL OR last_market_observation_at >= opened_at
            );
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION trading.reject_model_paper_instrument_key_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.instrument_key IS DISTINCT FROM OLD.instrument_key THEN
        RAISE EXCEPTION 'MODEL_PAPER_INSTRUMENT_KEY_IS_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid='trading.model_paper_positions'::regclass
           AND tgname='trg_model_paper_instrument_key_immutable'
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_model_paper_instrument_key_immutable
        BEFORE UPDATE OF instrument_key
        ON trading.model_paper_positions
        FOR EACH ROW EXECUTE FUNCTION trading.reject_model_paper_instrument_key_mutation();
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS ix_model_paper_open_market_watermark
    ON trading.model_paper_positions(mode,last_market_observation_at)
    WHERE status='OPEN';
CREATE INDEX IF NOT EXISTS ix_model_paper_instrument_key
    ON trading.model_paper_positions(instrument_key)
    WHERE instrument_key IS NOT NULL;

COMMIT;
