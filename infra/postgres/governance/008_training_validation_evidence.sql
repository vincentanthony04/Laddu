BEGIN;

CREATE TABLE IF NOT EXISTS research.training_validation_evidence (
    validation_evidence_id text PRIMARY KEY,
    publication_id text NOT NULL REFERENCES research.training_publications(publication_id),
    model_key text NOT NULL,
    validation_profile text NOT NULL CHECK (validation_profile IN ('research','capital')),
    approval_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('APPROVED','REJECTED','INSUFFICIENT_EVIDENCE','NOT_RUN')),
    lifecycle_state text NOT NULL,
    validated_at timestamptz NOT NULL,
    payload_json jsonb NOT NULL,
    payload_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(publication_id, validation_profile, approval_id)
);
CREATE INDEX IF NOT EXISTS ix_training_validation_model_profile_time
    ON research.training_validation_evidence(model_key, validation_profile, validated_at DESC);

DROP TRIGGER IF EXISTS trg_training_validation_evidence_immutable ON research.training_validation_evidence;
CREATE TRIGGER trg_training_validation_evidence_immutable
BEFORE UPDATE OR DELETE ON research.training_validation_evidence
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();

COMMIT;
