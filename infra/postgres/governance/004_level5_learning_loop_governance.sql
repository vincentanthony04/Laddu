BEGIN;

CREATE TABLE IF NOT EXISTS research.learning_findings (
    finding_id text PRIMARY KEY,
    finding_type text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('intraday','delivery')),
    evidence_hash text NOT NULL,
    finding jsonb NOT NULL,
    authority_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_learning_findings_mode_time
    ON research.learning_findings(mode, created_at DESC);

CREATE TABLE IF NOT EXISTS research.rule_change_proposals (
    proposal_id text PRIMARY KEY,
    finding_id text NOT NULL REFERENCES research.learning_findings(finding_id),
    proposal_type text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('intraday','delivery')),
    proposal jsonb NOT NULL,
    evidence_hash text NOT NULL,
    authority_version text NOT NULL,
    human_approval_required boolean NOT NULL DEFAULT true CHECK (human_approval_required),
    approval_state text NOT NULL DEFAULT 'PENDING' CHECK (approval_state IN ('PENDING','APPROVED_FOR_RESEARCH','APPROVED_FOR_CHALLENGER','REJECTED','QUARANTINED')),
    production_applied boolean NOT NULL DEFAULT false CHECK (NOT production_applied),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_rule_change_proposals_mode_time
    ON research.rule_change_proposals(mode, created_at DESC);

GRANT SELECT, INSERT ON TABLE research.learning_findings, research.rule_change_proposals TO laddu_governance_writer;

COMMIT;
