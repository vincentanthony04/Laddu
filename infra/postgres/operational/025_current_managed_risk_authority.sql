-- AC-076: immutable initial risk plus separately queryable current managed risk.
-- Admission heat remains based on open_risk (the immutable accepted risk). Moving
-- a stop can reduce current downside or secure profit, but can never automatically
-- release portfolio admission capacity.
BEGIN;

ALTER TABLE trading.model_paper_positions
    ADD COLUMN IF NOT EXISTS current_managed_risk numeric(20,6),
    ADD COLUMN IF NOT EXISTS secured_profit numeric(20,6) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS managed_risk_state text;

UPDATE trading.model_paper_positions
   SET current_managed_risk=CASE
           WHEN status='CLOSED' THEN 0
           WHEN side='LONG' THEN ROUND(GREATEST(0::numeric,(entry_price-managed_stop)*quantity),2)
           ELSE ROUND(GREATEST(0::numeric,(managed_stop-entry_price)*quantity),2)
       END,
       secured_profit=CASE
           WHEN side='LONG' THEN ROUND(GREATEST(0::numeric,(managed_stop-entry_price)*quantity),2)
           ELSE ROUND(GREATEST(0::numeric,(entry_price-managed_stop)*quantity),2)
       END,
       managed_risk_state=CASE
           WHEN status='CLOSED' THEN 'CLOSED'
           WHEN side='LONG' AND managed_stop>entry_price THEN 'PROFIT_SECURED'
           WHEN side='SHORT' AND managed_stop<entry_price THEN 'PROFIT_SECURED'
           WHEN managed_stop=entry_price THEN 'BREAKEVEN_PROTECTED'
           WHEN (side='LONG' AND managed_stop>original_stop) OR (side='SHORT' AND managed_stop<original_stop) THEN 'RISK_REDUCED'
           ELSE 'ORIGINAL_RISK'
       END
 WHERE current_managed_risk IS NULL OR managed_risk_state IS NULL;

ALTER TABLE trading.model_paper_positions
    ALTER COLUMN current_managed_risk SET NOT NULL,
    ALTER COLUMN managed_risk_state SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='trading.model_paper_positions'::regclass
           AND conname='ck_model_paper_managed_risk_values'
    ) THEN
        ALTER TABLE trading.model_paper_positions
            ADD CONSTRAINT ck_model_paper_managed_risk_values CHECK (
                current_managed_risk >= 0 AND secured_profit >= 0
                AND managed_risk_state IN ('ORIGINAL_RISK','RISK_REDUCED','BREAKEVEN_PROTECTED','PROFIT_SECURED','CLOSED')
            );
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION trading.enforce_model_paper_managed_risk()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    expected_current numeric(20,6);
    expected_secured numeric(20,6);
    expected_state text;
BEGIN
    IF TG_OP='UPDATE' AND NEW.open_risk IS DISTINCT FROM OLD.open_risk THEN
        RAISE EXCEPTION 'MODEL_PAPER_INITIAL_RISK_IS_IMMUTABLE';
    END IF;
    IF TG_OP='UPDATE' AND NEW.managed_stop IS DISTINCT FROM OLD.managed_stop THEN
        IF (OLD.side='LONG' AND NEW.managed_stop < OLD.managed_stop)
           OR (OLD.side='SHORT' AND NEW.managed_stop > OLD.managed_stop) THEN
            RAISE EXCEPTION 'MODEL_PAPER_MANAGED_STOP_CANNOT_WIDEN_RISK';
        END IF;
    END IF;

    IF NEW.status='CLOSED' THEN
        expected_current := 0;
        expected_state := 'CLOSED';
    ELSE
        IF NEW.side='LONG' AND NEW.managed_stop < NEW.original_stop THEN
            RAISE EXCEPTION 'MODEL_PAPER_MANAGED_STOP_BELOW_ORIGINAL_RISK';
        ELSIF NEW.side='SHORT' AND NEW.managed_stop > NEW.original_stop THEN
            RAISE EXCEPTION 'MODEL_PAPER_MANAGED_STOP_ABOVE_ORIGINAL_RISK';
        END IF;
        expected_current := CASE WHEN NEW.side='LONG'
            THEN ROUND(GREATEST(0::numeric,(NEW.entry_price-NEW.managed_stop)*NEW.quantity),2)
            ELSE ROUND(GREATEST(0::numeric,(NEW.managed_stop-NEW.entry_price)*NEW.quantity),2) END;
        IF expected_current > NEW.open_risk + 0.01 THEN
            RAISE EXCEPTION 'MODEL_PAPER_MANAGED_RISK_EXCEEDS_INITIAL_RISK';
        END IF;
        expected_state := CASE
            WHEN NEW.side='LONG' AND NEW.managed_stop>NEW.entry_price THEN 'PROFIT_SECURED'
            WHEN NEW.side='SHORT' AND NEW.managed_stop<NEW.entry_price THEN 'PROFIT_SECURED'
            WHEN NEW.managed_stop=NEW.entry_price THEN 'BREAKEVEN_PROTECTED'
            WHEN (NEW.side='LONG' AND NEW.managed_stop>NEW.original_stop)
              OR (NEW.side='SHORT' AND NEW.managed_stop<NEW.original_stop) THEN 'RISK_REDUCED'
            ELSE 'ORIGINAL_RISK' END;
    END IF;
    expected_secured := CASE WHEN NEW.side='LONG'
        THEN ROUND(GREATEST(0::numeric,(NEW.managed_stop-NEW.entry_price)*NEW.quantity),2)
        ELSE ROUND(GREATEST(0::numeric,(NEW.entry_price-NEW.managed_stop)*NEW.quantity),2) END;

    IF ABS(NEW.current_managed_risk-expected_current)>0.01
       OR ABS(NEW.secured_profit-expected_secured)>0.01
       OR NEW.managed_risk_state IS DISTINCT FROM expected_state THEN
        RAISE EXCEPTION 'MODEL_PAPER_MANAGED_RISK_ATTRIBUTION_MISMATCH';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid='trading.model_paper_positions'::regclass
           AND tgname='trg_model_paper_managed_risk'
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_model_paper_managed_risk
        BEFORE INSERT OR UPDATE OF managed_stop,open_risk,current_managed_risk,secured_profit,managed_risk_state,status
        ON trading.model_paper_positions
        FOR EACH ROW EXECUTE FUNCTION trading.enforce_model_paper_managed_risk();
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS ix_model_paper_current_managed_risk
    ON trading.model_paper_positions(status,mode,current_managed_risk,opened_at DESC);

COMMIT;
