-- AC-061: durable/queryable Signal Age attribution.
-- Adds only forward relational evidence. Historical missing clocks remain
-- explicit MISSING/PARTIAL; no timestamp is fabricated.
BEGIN;

ALTER TABLE trading.model_paper_positions
    ADD COLUMN IF NOT EXISTS decision_delay_seconds numeric(20,3);

UPDATE trading.model_paper_positions
   SET decision_delay_seconds=ROUND(EXTRACT(EPOCH FROM (opened_at-generated_at))::numeric,3)
 WHERE decision_delay_seconds IS NULL
   AND generated_at IS NOT NULL
   AND opened_at IS NOT NULL
   AND opened_at >= generated_at;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='trading.model_paper_positions'::regclass
           AND conname='ck_model_paper_decision_delay_nonnegative'
    ) THEN
        ALTER TABLE trading.model_paper_positions
            ADD CONSTRAINT ck_model_paper_decision_delay_nonnegative CHECK (
                decision_delay_seconds IS NULL OR decision_delay_seconds >= 0
            );
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS ix_model_paper_signal_age_delay
    ON trading.model_paper_positions(mode,decision_delay_seconds,opened_at DESC)
    WHERE decision_delay_seconds IS NOT NULL;

CREATE OR REPLACE FUNCTION trading.reject_model_paper_decision_delay_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.decision_delay_seconds IS DISTINCT FROM OLD.decision_delay_seconds THEN
        RAISE EXCEPTION 'MODEL_PAPER_DECISION_DELAY_IS_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid='trading.model_paper_positions'::regclass
           AND tgname='trg_model_paper_decision_delay_immutable'
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_model_paper_decision_delay_immutable
        BEFORE UPDATE OF decision_delay_seconds
        ON trading.model_paper_positions
        FOR EACH ROW EXECUTE FUNCTION trading.reject_model_paper_decision_delay_mutation();
    END IF;
END;
$$;

ALTER TABLE trading.signal_lifecycle_events
    ADD COLUMN IF NOT EXISTS decision_delay_seconds numeric(20,3),
    ADD COLUMN IF NOT EXISTS generation_age_bucket text,
    ADD COLUMN IF NOT EXISTS open_age_bucket text,
    ADD COLUMN IF NOT EXISTS decision_delay_bucket text,
    ADD COLUMN IF NOT EXISTS age_attribution_state text,
    ADD COLUMN IF NOT EXISTS age_bucket_policy_version text;

UPDATE trading.signal_lifecycle_events
   SET generation_age_seconds=COALESCE(
           generation_age_seconds,
           CASE WHEN generated_at IS NOT NULL AND occurred_at >= generated_at
                THEN ROUND(EXTRACT(EPOCH FROM (occurred_at-generated_at))::numeric,3) END
       ),
       open_age_seconds=COALESCE(
           open_age_seconds,
           CASE WHEN opened_at IS NOT NULL AND occurred_at >= opened_at
                THEN ROUND(EXTRACT(EPOCH FROM (occurred_at-opened_at))::numeric,3) END
       ),
       decision_delay_seconds=COALESCE(
           decision_delay_seconds,
           CASE WHEN generated_at IS NOT NULL AND opened_at IS NOT NULL AND opened_at >= generated_at
                THEN ROUND(EXTRACT(EPOCH FROM (opened_at-generated_at))::numeric,3) END
       ),
       age_attribution_state=COALESCE(
           age_attribution_state,
           CASE WHEN generated_at IS NOT NULL AND opened_at IS NOT NULL THEN 'COMPLETE'
                WHEN generated_at IS NOT NULL OR opened_at IS NOT NULL THEN 'PARTIAL'
                ELSE 'MISSING' END
       ),
       age_bucket_policy_version=COALESCE(age_bucket_policy_version,'signal-age-attribution-buckets-1.0.0');

UPDATE trading.signal_lifecycle_events
   SET generation_age_bucket=COALESCE(generation_age_bucket,
           CASE WHEN generation_age_seconds IS NULL THEN 'MISSING'
                WHEN generation_age_seconds < 300 THEN '0_5M'
                WHEN generation_age_seconds < 900 THEN '5_15M'
                WHEN generation_age_seconds < 3600 THEN '15_60M'
                WHEN generation_age_seconds < 14400 THEN '1_4H'
                ELSE '4H_PLUS' END),
       open_age_bucket=COALESCE(open_age_bucket,
           CASE WHEN open_age_seconds IS NULL THEN 'MISSING'
                WHEN open_age_seconds < 300 THEN '0_5M'
                WHEN open_age_seconds < 900 THEN '5_15M'
                WHEN open_age_seconds < 3600 THEN '15_60M'
                WHEN open_age_seconds < 14400 THEN '1_4H'
                ELSE '4H_PLUS' END),
       decision_delay_bucket=COALESCE(decision_delay_bucket,
           CASE WHEN decision_delay_seconds IS NULL THEN 'MISSING'
                WHEN decision_delay_seconds < 300 THEN '0_5M'
                WHEN decision_delay_seconds < 900 THEN '5_15M'
                WHEN decision_delay_seconds < 3600 THEN '15_60M'
                WHEN decision_delay_seconds < 14400 THEN '1_4H'
                ELSE '4H_PLUS' END);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='trading.signal_lifecycle_events'::regclass
           AND conname='ck_signal_lifecycle_age_attribution'
    ) THEN
        ALTER TABLE trading.signal_lifecycle_events
            ADD CONSTRAINT ck_signal_lifecycle_age_attribution CHECK (
                (decision_delay_seconds IS NULL OR decision_delay_seconds >= 0)
                AND generation_age_bucket IN ('MISSING','0_5M','5_15M','15_60M','1_4H','4H_PLUS')
                AND open_age_bucket IN ('MISSING','0_5M','5_15M','15_60M','1_4H','4H_PLUS')
                AND decision_delay_bucket IN ('MISSING','0_5M','5_15M','15_60M','1_4H','4H_PLUS')
                AND age_attribution_state IN ('COMPLETE','PARTIAL','MISSING')
                AND age_bucket_policy_version IS NOT NULL
            );
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS ix_signal_lifecycle_age_attribution
    ON trading.signal_lifecycle_events(event_type,decision_delay_bucket,open_age_bucket,occurred_at DESC);

COMMIT;
