-- v122 Candidate-12 clean-design installed read-path isolation.
-- Transactionally maintained narrow lifecycle projection for bounded foreground metrics.
-- Canonical decisions remain the source of truth; this migration is forward-only/additive.
BEGIN;

CREATE TABLE IF NOT EXISTS trading.canonical_decision_lifecycle (
    decision_id text PRIMARY KEY REFERENCES trading.canonical_decisions(decision_id) ON DELETE RESTRICT,
    thesis_id text NOT NULL,
    signal_id text NOT NULL,
    symbol text NOT NULL,
    exchange text NOT NULL CHECK (exchange IN ('NSE','BSE')),
    mode text NOT NULL CHECK (mode IN ('intraday','delivery')),
    side text NOT NULL CHECK (side IN ('LONG','SHORT')),
    setup_family text NOT NULL,
    canonical_state text NOT NULL,
    publication_authority text NOT NULL,
    execution_authority text NOT NULL,
    entry text,
    target text,
    t2 text,
    stop text,
    rr text,
    ltp text,
    exit_price text,
    net_pnl text,
    gross_pnl text,
    quantity text,
    settlement_id text,
    position_id text,
    signal_outcome text,
    economic_outcome text,
    result text,
    costs jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    opened_at timestamptz,
    closed_at timestamptz,
    active boolean NOT NULL,
    record_version bigint NOT NULL,
    frozen_evidence_hash text
);

CREATE INDEX IF NOT EXISTS ix_canonical_decision_lifecycle_updated_at
    ON trading.canonical_decision_lifecycle(updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_canonical_decision_lifecycle_mode_updated_at
    ON trading.canonical_decision_lifecycle(mode, updated_at DESC);

CREATE OR REPLACE FUNCTION trading.project_canonical_decision_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, trading
AS $$
BEGIN
    INSERT INTO trading.canonical_decision_lifecycle (
        decision_id,thesis_id,signal_id,symbol,exchange,mode,side,setup_family,
        canonical_state,publication_authority,execution_authority,
        entry,target,t2,stop,rr,ltp,exit_price,net_pnl,gross_pnl,quantity,
        settlement_id,position_id,signal_outcome,economic_outcome,result,costs,
        created_at,updated_at,opened_at,closed_at,active,record_version,frozen_evidence_hash
    ) VALUES (
        NEW.decision_id,NEW.thesis_id,NEW.signal_id,NEW.symbol,NEW.exchange,NEW.mode,NEW.side,NEW.setup_family,
        NEW.state,NEW.publication_authority,NEW.execution_authority,
        NULLIF(NEW.entry_plan->>'entry',''),
        NULLIF(NEW.entry_plan->>'target_1',''),
        NULLIF(NEW.entry_plan->>'target_2',''),
        NULLIF(NEW.risk_plan->>'stop',''),
        NULLIF(NEW.risk_plan->>'risk_reward',''),
        NULLIF(NEW.live_snapshot->>'ltp',''),
        NULLIF(COALESCE(NEW.outcome->>'exit_price',NEW.outcome->>'exit',NEW.latest_payload->>'exit_price',NEW.latest_payload->>'exit'),''),
        NULLIF(COALESCE(NEW.outcome->>'net_pnl',NEW.latest_payload->>'net_pnl'),''),
        NULLIF(COALESCE(NEW.outcome->>'gross_pnl',NEW.latest_payload->>'gross_pnl'),''),
        NULLIF(COALESCE(NEW.outcome->>'quantity',NEW.latest_payload->>'quantity'),''),
        COALESCE(NEW.outcome->>'settlement_id',NEW.latest_payload->>'settlement_id'),
        COALESCE(NEW.outcome->>'position_id',NEW.latest_payload->>'position_id'),
        COALESCE(NEW.outcome->>'signal_outcome',NEW.latest_payload->>'signal_outcome'),
        COALESCE(NEW.outcome->>'economic_outcome',NEW.latest_payload->>'economic_outcome'),
        COALESCE(NEW.outcome->>'result',NEW.outcome->>'status',NEW.latest_payload->>'result',NEW.latest_payload->>'status'),
        COALESCE(NEW.outcome->'costs',NEW.outcome->'charges',NEW.latest_payload->'costs',NEW.latest_payload->'charges'),
        NEW.created_at,NEW.updated_at,NEW.activated_at,NEW.closed_at,NEW.active,NEW.record_version,NEW.frozen_evidence_hash
    )
    ON CONFLICT (decision_id) DO UPDATE SET
        thesis_id=EXCLUDED.thesis_id,signal_id=EXCLUDED.signal_id,symbol=EXCLUDED.symbol,
        exchange=EXCLUDED.exchange,mode=EXCLUDED.mode,side=EXCLUDED.side,setup_family=EXCLUDED.setup_family,
        canonical_state=EXCLUDED.canonical_state,publication_authority=EXCLUDED.publication_authority,
        execution_authority=EXCLUDED.execution_authority,entry=EXCLUDED.entry,target=EXCLUDED.target,t2=EXCLUDED.t2,
        stop=EXCLUDED.stop,rr=EXCLUDED.rr,ltp=EXCLUDED.ltp,exit_price=EXCLUDED.exit_price,
        net_pnl=EXCLUDED.net_pnl,gross_pnl=EXCLUDED.gross_pnl,quantity=EXCLUDED.quantity,
        settlement_id=EXCLUDED.settlement_id,position_id=EXCLUDED.position_id,
        signal_outcome=EXCLUDED.signal_outcome,economic_outcome=EXCLUDED.economic_outcome,result=EXCLUDED.result,costs=EXCLUDED.costs,
        created_at=EXCLUDED.created_at,updated_at=EXCLUDED.updated_at,opened_at=EXCLUDED.opened_at,
        closed_at=EXCLUDED.closed_at,active=EXCLUDED.active,record_version=EXCLUDED.record_version,
        frozen_evidence_hash=EXCLUDED.frozen_evidence_hash;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION trading.project_canonical_decision_lifecycle() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION trading.project_canonical_decision_lifecycle() TO laddu_runtime;
GRANT SELECT ON trading.canonical_decision_lifecycle TO laddu_runtime;

DROP TRIGGER IF EXISTS trg_project_canonical_decision_lifecycle ON trading.canonical_decisions;
CREATE TRIGGER trg_project_canonical_decision_lifecycle
AFTER INSERT OR UPDATE ON trading.canonical_decisions
FOR EACH ROW EXECUTE FUNCTION trading.project_canonical_decision_lifecycle();

-- One-time bounded migration projection. Source rows are read only; no canonical row is rewritten.
INSERT INTO trading.canonical_decision_lifecycle (
    decision_id,thesis_id,signal_id,symbol,exchange,mode,side,setup_family,
    canonical_state,publication_authority,execution_authority,
    entry,target,t2,stop,rr,ltp,exit_price,net_pnl,gross_pnl,quantity,
    settlement_id,position_id,signal_outcome,economic_outcome,result,costs,
    created_at,updated_at,opened_at,closed_at,active,record_version,frozen_evidence_hash
)
SELECT
    decision_id,thesis_id,signal_id,symbol,exchange,mode,side,setup_family,
    state,publication_authority,execution_authority,
    NULLIF(entry_plan->>'entry',''),
    NULLIF(entry_plan->>'target_1',''),
    NULLIF(entry_plan->>'target_2',''),
    NULLIF(risk_plan->>'stop',''),
    NULLIF(risk_plan->>'risk_reward',''),
    NULLIF(live_snapshot->>'ltp',''),
    NULLIF(COALESCE(outcome->>'exit_price',outcome->>'exit',latest_payload->>'exit_price',latest_payload->>'exit'),''),
    NULLIF(COALESCE(outcome->>'net_pnl',latest_payload->>'net_pnl'),''),
    NULLIF(COALESCE(outcome->>'gross_pnl',latest_payload->>'gross_pnl'),''),
    NULLIF(COALESCE(outcome->>'quantity',latest_payload->>'quantity'),''),
    COALESCE(outcome->>'settlement_id',latest_payload->>'settlement_id'),
    COALESCE(outcome->>'position_id',latest_payload->>'position_id'),
    COALESCE(outcome->>'signal_outcome',latest_payload->>'signal_outcome'),
    COALESCE(outcome->>'economic_outcome',latest_payload->>'economic_outcome'),
    COALESCE(outcome->>'result',outcome->>'status',latest_payload->>'result',latest_payload->>'status'),
    COALESCE(outcome->'costs',outcome->'charges',latest_payload->'costs',latest_payload->'charges'),
    created_at,updated_at,activated_at,closed_at,active,record_version,frozen_evidence_hash
FROM trading.canonical_decisions
ON CONFLICT (decision_id) DO UPDATE SET
    thesis_id=EXCLUDED.thesis_id,signal_id=EXCLUDED.signal_id,symbol=EXCLUDED.symbol,
    exchange=EXCLUDED.exchange,mode=EXCLUDED.mode,side=EXCLUDED.side,setup_family=EXCLUDED.setup_family,
    canonical_state=EXCLUDED.canonical_state,publication_authority=EXCLUDED.publication_authority,
    execution_authority=EXCLUDED.execution_authority,entry=EXCLUDED.entry,target=EXCLUDED.target,t2=EXCLUDED.t2,
    stop=EXCLUDED.stop,rr=EXCLUDED.rr,ltp=EXCLUDED.ltp,exit_price=EXCLUDED.exit_price,
    net_pnl=EXCLUDED.net_pnl,gross_pnl=EXCLUDED.gross_pnl,quantity=EXCLUDED.quantity,
    settlement_id=EXCLUDED.settlement_id,position_id=EXCLUDED.position_id,
    signal_outcome=EXCLUDED.signal_outcome,economic_outcome=EXCLUDED.economic_outcome,result=EXCLUDED.result,costs=EXCLUDED.costs,
    created_at=EXCLUDED.created_at,updated_at=EXCLUDED.updated_at,opened_at=EXCLUDED.opened_at,
    closed_at=EXCLUDED.closed_at,active=EXCLUDED.active,record_version=EXCLUDED.record_version,
    frozen_evidence_hash=EXCLUDED.frozen_evidence_hash;

COMMIT;
