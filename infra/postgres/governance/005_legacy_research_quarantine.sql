-- Project Laddu v122 candidate-3 — retained quarantine authority for legacy
-- research rows that cannot satisfy the stricter canonical selector schema.
BEGIN;

CREATE TABLE IF NOT EXISTS research.legacy_research_quarantine (
    quarantine_key text PRIMARY KEY,
    entity_type text NOT NULL CHECK (entity_type IN ('POPULATION','OUTCOME')),
    legacy_key text NOT NULL,
    reason text NOT NULL,
    payload jsonb NOT NULL,
    payload_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(entity_type, legacy_key)
);
CREATE INDEX IF NOT EXISTS ix_legacy_research_quarantine_type_time
    ON research.legacy_research_quarantine(entity_type, created_at DESC);

DROP TRIGGER IF EXISTS trg_legacy_research_quarantine_immutable ON research.legacy_research_quarantine;
CREATE TRIGGER trg_legacy_research_quarantine_immutable
BEFORE UPDATE OR DELETE ON research.legacy_research_quarantine
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();

GRANT SELECT, INSERT ON TABLE research.legacy_research_quarantine TO laddu_governance_writer;

COMMIT;
