-- Additive relational lineage for canonical Model-Paper lifecycle evidence.
-- Historical rows may legitimately retain NULL where the original source did
-- not persist a field; this migration never fabricates timestamps.
BEGIN;

ALTER TABLE trading.model_paper_positions
    ADD COLUMN IF NOT EXISTS decision_id text,
    ADD COLUMN IF NOT EXISTS generated_at timestamptz,
    ADD COLUMN IF NOT EXISTS model_version text,
    ADD COLUMN IF NOT EXISTS policy_version text,
    ADD COLUMN IF NOT EXISTS evidence_snapshot_id text,
    ADD COLUMN IF NOT EXISTS evidence_hash text,
    ADD COLUMN IF NOT EXISTS feature_manifest_hash text;

UPDATE trading.model_paper_positions
   SET decision_id=COALESCE(decision_id,NULLIF(payload->>'decision_id','')),
       model_version=COALESCE(model_version,NULLIF(payload->>'model_version','')),
       policy_version=COALESCE(policy_version,NULLIF(payload->>'policy_version',''),NULLIF(payload->>'model_policy','')),
       evidence_snapshot_id=COALESCE(evidence_snapshot_id,NULLIF(payload->>'evidence_snapshot_id',''),NULLIF(payload->>'canonical_snapshot_id','')),
       evidence_hash=COALESCE(evidence_hash,NULLIF(payload->>'evidence_hash',''),NULLIF(payload->>'evidence_snapshot_hash','')),
       feature_manifest_hash=COALESCE(feature_manifest_hash,NULLIF(payload->>'feature_manifest_hash',''))
 WHERE decision_id IS NULL
    OR model_version IS NULL
    OR policy_version IS NULL
    OR evidence_snapshot_id IS NULL
    OR evidence_hash IS NULL
    OR feature_manifest_hash IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='trading.model_paper_positions'::regclass
           AND conname='ck_model_paper_relational_temporal_order'
    ) THEN
        ALTER TABLE trading.model_paper_positions
            ADD CONSTRAINT ck_model_paper_relational_temporal_order CHECK (
                (generated_at IS NULL OR generated_at <= opened_at)
                AND (closed_at IS NULL OR closed_at >= opened_at)
                AND updated_at >= opened_at
            );
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS ix_model_paper_decision_lineage
    ON trading.model_paper_positions(decision_id)
    WHERE decision_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_model_paper_model_policy_lineage
    ON trading.model_paper_positions(model_version,policy_version,opened_at DESC);
CREATE INDEX IF NOT EXISTS ix_model_paper_evidence_lineage
    ON trading.model_paper_positions(evidence_hash)
    WHERE evidence_hash IS NOT NULL;

CREATE OR REPLACE FUNCTION trading.reject_model_paper_lineage_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source_signal_id IS DISTINCT FROM OLD.source_signal_id
       OR NEW.decision_id IS DISTINCT FROM OLD.decision_id
       OR NEW.generated_at IS DISTINCT FROM OLD.generated_at
       OR NEW.opened_at IS DISTINCT FROM OLD.opened_at
       OR NEW.model_version IS DISTINCT FROM OLD.model_version
       OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
       OR NEW.evidence_snapshot_id IS DISTINCT FROM OLD.evidence_snapshot_id
       OR NEW.evidence_hash IS DISTINCT FROM OLD.evidence_hash
       OR NEW.feature_manifest_hash IS DISTINCT FROM OLD.feature_manifest_hash THEN
        RAISE EXCEPTION 'MODEL_PAPER_RELATIONAL_LINEAGE_IS_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid='trading.model_paper_positions'::regclass
           AND tgname='trg_model_paper_relational_lineage_immutable'
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_model_paper_relational_lineage_immutable
        BEFORE UPDATE OF source_signal_id,decision_id,generated_at,opened_at,model_version,policy_version,evidence_snapshot_id,evidence_hash,feature_manifest_hash
        ON trading.model_paper_positions
        FOR EACH ROW EXECUTE FUNCTION trading.reject_model_paper_lineage_mutation();
    END IF;
END;
$$;

ALTER TABLE trading.signal_lifecycle_events
    ADD COLUMN IF NOT EXISTS generated_at timestamptz,
    ADD COLUMN IF NOT EXISTS opened_at timestamptz,
    ADD COLUMN IF NOT EXISTS model_version text,
    ADD COLUMN IF NOT EXISTS policy_version text,
    ADD COLUMN IF NOT EXISTS evidence_hash text,
    ADD COLUMN IF NOT EXISTS generation_age_seconds numeric(20,3),
    ADD COLUMN IF NOT EXISTS open_age_seconds numeric(20,3);

UPDATE trading.signal_lifecycle_events
   SET model_version=COALESCE(model_version,NULLIF(payload->>'model_version','')),
       policy_version=COALESCE(policy_version,NULLIF(payload->>'policy_version','')),
       evidence_hash=COALESCE(evidence_hash,NULLIF(payload->>'evidence_hash',''))
 WHERE model_version IS NULL OR policy_version IS NULL OR evidence_hash IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='trading.signal_lifecycle_events'::regclass
           AND conname='ck_signal_lifecycle_relational_temporal_order'
    ) THEN
        ALTER TABLE trading.signal_lifecycle_events
            ADD CONSTRAINT ck_signal_lifecycle_relational_temporal_order CHECK (
                (generated_at IS NULL OR opened_at IS NULL OR generated_at <= opened_at)
                AND (opened_at IS NULL OR opened_at <= occurred_at)
                AND (generation_age_seconds IS NULL OR generation_age_seconds >= 0)
                AND (open_age_seconds IS NULL OR open_age_seconds >= 0)
            );
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS ix_signal_lifecycle_relational_lineage
    ON trading.signal_lifecycle_events(position_id,model_version,policy_version,occurred_at)
    WHERE position_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_signal_lifecycle_evidence_hash
    ON trading.signal_lifecycle_events(evidence_hash)
    WHERE evidence_hash IS NOT NULL;

COMMIT;
