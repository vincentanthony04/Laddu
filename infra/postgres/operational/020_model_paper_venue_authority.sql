-- Immutable venue identity for cost-correct Model Paper positions.
BEGIN;

ALTER TABLE trading.model_paper_positions
    ADD COLUMN IF NOT EXISTS bse_group text;

UPDATE trading.model_paper_positions
   SET bse_group=upper(nullif(payload->>'bse_group',''))
 WHERE exchange='BSE' AND coalesce(bse_group,'')='';

UPDATE trading.model_paper_positions
   SET bse_group=NULL
 WHERE exchange='NSE';

DO $$
DECLARE
    blocked_position text;
BEGIN
    SELECT position_id INTO blocked_position
      FROM trading.model_paper_positions
     WHERE exchange IS NULL
        OR exchange NOT IN ('NSE','BSE')
        OR (
            exchange='BSE'
            AND coalesce(bse_group,'') NOT IN
                ('A','B','X','XT','Z','ZP','XC','XD','T','SS','ST')
        )
        OR (
            nullif(payload->>'exchange','') IS NOT NULL
            AND upper(payload->>'exchange') <> exchange
        )
        OR (
            exchange='BSE'
            AND nullif(payload->>'bse_group','') IS NOT NULL
            AND upper(payload->>'bse_group') <> bse_group
        )
     ORDER BY opened_at,position_id
     LIMIT 1;
    IF blocked_position IS NOT NULL THEN
        RAISE EXCEPTION 'MODEL_PAPER_VENUE_IDENTITY_UNRESOLVED:%', blocked_position;
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='trading.model_paper_positions'::regclass
           AND conname='ck_model_paper_venue_identity'
    ) THEN
        ALTER TABLE trading.model_paper_positions
            ADD CONSTRAINT ck_model_paper_venue_identity CHECK (
                (exchange='NSE' AND bse_group IS NULL)
                OR (exchange='BSE' AND bse_group IN
                    ('A','B','X','XT','Z','ZP','XC','XD','T','SS','ST'))
            );
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION trading.reject_model_paper_venue_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.exchange IS DISTINCT FROM OLD.exchange
       OR NEW.bse_group IS DISTINCT FROM OLD.bse_group THEN
        RAISE EXCEPTION 'MODEL_PAPER_VENUE_IDENTITY_IS_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_model_paper_venue_immutable
    ON trading.model_paper_positions;
CREATE TRIGGER trg_model_paper_venue_immutable
BEFORE UPDATE OF exchange,bse_group ON trading.model_paper_positions
FOR EACH ROW EXECUTE FUNCTION trading.reject_model_paper_venue_mutation();

COMMIT;
