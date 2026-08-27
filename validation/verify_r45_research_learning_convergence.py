from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PARENT_SHA = "1bb0174b2a1afb56c798aeaccfe1b164e42da9f3167e3d886c7e42a0cb098999"
FROZEN = json.loads((ROOT / "validation/r45_frozen_r44_hashes.json").read_text(encoding="utf-8"))
FUNCTIONAL_ALLOWED = {
    "backend/core/historical_pit_sweep_service.py",
    "backend/core/quant_research_orchestrator_service.py",
    "backend/core/research_lifecycle_reconciliation_service.py",
    "backend/tools/run_operational_learning_cycle.py",
}
METADATA_ALLOWED = {
    "RELEASE_IDENTITY.json", "RELEASE_ATTESTATION.json", "frontend/release-identity.json",
    "validation/package_allowlist.json", "validation/package_manifest.sha256",
    "validation/validate_deployable_candidate.py", "validation/r45_frozen_r44_hashes.json",
    "validation/verify_r45_research_learning_convergence.py",
    "docs/R45_RESEARCH_LEARNING_CONVERGENCE.md",
}
ALLOWED = FUNCTIONAL_ALLOWED | METADATA_ALLOWED


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    failures: list[str] = []
    checks: list[dict] = []
    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"gate": name, "state": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            failures.append(f"{name}:{detail}")

    check("EXACT_R44_PARENT_ARCHIVE", FROZEN.get("parent_archive_sha256") == EXPECTED_PARENT_SHA, "R45 bound to exact R44 archive")
    missing=[]; unexpected=[]; changed=[]
    for rel,digest in dict(FROZEN.get("hashes") or {}).items():
        path=ROOT/rel
        if not path.is_file(): missing.append(rel); continue
        if sha(path) != digest:
            (changed if rel in ALLOWED else unexpected).append(rel)
    check("R44_NON_REGRESSION", not missing and not unexpected, f"missing={len(missing)} unexpected_changed={unexpected}")
    check("DECLARED_FUNCTIONAL_BOUNDARY", set(changed).issubset(ALLOWED), f"changed={sorted(changed)}")

    # Freeze sensitive production/installer/session/selector-policy authorities.
    protected = [
        "INSTALL_UPDATE.cmd", "installer/install.ps1", "installer/register_research_tasks.ps1",
        "train_ai_model.ps1", "run_learning_cycle.ps1",
        "backend/core/intraday_session_policy.py", "backend/core/selection_walk_forward_replay_service.py",
        "backend/core/operations_control_service.py", "backend/tools/train_nse_smart_model.py",
        "backend/tools/quant_duckdb_lightgbm_worker.py", "frontend/app.js", "frontend/index.html", "frontend/ui-system.css",
    ]
    protected_changed=[]
    frozen_hashes=dict(FROZEN.get("hashes") or {})
    for rel in protected:
        if rel in frozen_hashes and (ROOT/rel).is_file() and sha(ROOT/rel) != frozen_hashes[rel]:
            protected_changed.append(rel)
    check("PRODUCTION_AND_INSTALLER_AUTHORITIES_FROZEN", not protected_changed, f"changed={protected_changed}")

    hist=(ROOT/"backend/core/historical_pit_sweep_service.py").read_text(encoding="utf-8")
    op=(ROOT/"backend/tools/run_operational_learning_cycle.py").read_text(encoding="utf-8")
    orch=(ROOT/"backend/core/quant_research_orchestrator_service.py").read_text(encoding="utf-8")
    recon=(ROOT/"backend/core/research_lifecycle_reconciliation_service.py").read_text(encoding="utf-8")
    check("CATALOGUE_BEFORE_DELIVERY_TRAINER", "refresh_research_catalog.py" in hist and hist.find("refresh_research_catalog.py") < hist.find("train_nse_smart_model.py"), "autonomous worker refreshes catalogue before canonical Delivery trainer")
    check("DELIVERY_TRAINER_STAYS_504", 'MIN_DATES = 504' in hist and '"--horizon", "10"' in hist, "existing Delivery 10d/504-date contract retained")
    check("AFTER_CLOSE_CONVERGENCE_LEASE", "current waiters" in hist and "is_india_market_open" in hist and "capacity-only pressure" in hist, "post-close occupancy-only pressure cannot kill deep trainer")
    check("SCHEDULED_SINGLE_SPEC", op.count('"trial_count": 1') == 2 and '"trial_count": 3' not in op, "both post-close/weekend desk cycles use one declared LightGBM specification")
    check("EVIDENCE_DRIVEN_RETRY", "snapshots_advanced" in orch and "labels_advanced" in orch and "NO_NEW_EVIDENCE" in orch, "new labels or snapshots bypass stale cadence suppression")
    check("AUTONOMOUS_SCAN_COMPLETION_HOOK_RETAINED", "maybe_run_cycle" in (ROOT/"backend/core/quant_scan_capture_service.py").read_text(encoding="utf-8"), "scan completion still invokes research convergence without waiting for scheduled task")
    check("REJECTED_MODEL_NOT_AVAILABLE", 'usable_shadow_states = {"SHADOW_MODEL_ELIGIBLE", "ACTIVE_VALIDATION", "ACTIVE_PRODUCTION"}' in recon and 'learned_model_available": False' in recon, "only eligible/validation shadow artifacts can become learned available")
    check("ML_PRODUCTION_INFLUENCE_STAYS_ZERO", '"production_influence": 0.0' in recon and 'production_influence": False' in orch, "research model remains zero production influence")

    # Installer-equivalent backend import: this catches module/import errors before Windows mutation.
    code = "import os,sys; sys.path.insert(0, os.path.join(os.getcwd(),'backend')); import main; print(main.APP_VERSION)"
    env = dict(__import__('os').environ)
    env.update({"PROJECT_LADDU_DATA_PLANE_MODE":"test", "PROJECT_LADDU_HOME":"/tmp/project-laddu-r45-import-home", "PYTHONDONTWRITEBYTECODE":"1"})
    cp=subprocess.run([sys.executable,"-c",code],cwd=ROOT,text=True,capture_output=True,env=env)
    check("INSTALLER_EQUIVALENT_BACKEND_IMPORT", cp.returncode==0 and "v131.0.0" in cp.stdout, f"rc={cp.returncode} stdout={cp.stdout[-100:]} stderr={cp.stderr[-240:]}")

    # Dynamic post-close yield semantics using a fake governor.
    sys.path.insert(0, str(ROOT/"backend"))
    import core.historical_pit_sweep_service as hmod
    class Gov:
        def __init__(self, *, waiting=0, interactive=False): self.waiting=waiting; self.interactive=interactive
        def should_yield(self, tier, record=False): return True, "governance read PostgreSQL pool pressure"
        def snapshot(self):
            base={"usable":True,"recovering":False,"requests_waiting":0,"admission_waiters":0}
            govr=dict(base); govr["requests_waiting"]=self.waiting
            return {"manual_bulk_pause_remaining_sec":0,"interactive_priority_active":self.interactive,"scanner_saturated":False,"database_pressure":{"required_database_recovery":False,"operational":dict(base),"interactive":dict(base),"governance":dict(base),"governance_read":govr}}
    class App: pass
    app=App(); app.status={}; app.workload_governor=Gov()
    svc=hmod.HistoricalPitSweepService(app)
    original=hmod.is_india_market_open
    try:
        hmod.is_india_market_open=lambda: False
        postclose=svc._must_yield()
        app.workload_governor=Gov(waiting=1)
        waiter=svc._must_yield()
        hmod.is_india_market_open=lambda: True
        app.workload_governor=Gov()
        market=svc._must_yield()
    finally:
        hmod.is_india_market_open=original
    check("POSTCLOSE_OCCUPANCY_ONLY_CONTINUES", postclose[0] is False, f"result={postclose}")
    check("POSTCLOSE_CURRENT_WAITER_YIELDS", waiter[0] is True, f"result={waiter}")
    check("MARKET_HOURS_P5_STILL_YIELDS", market[0] is True, f"result={market}")

    # Dynamic evidence-driven retry without training anything.
    from core.quant_research_orchestrator_service import QuantResearchOrchestratorService
    class Data:
        def __init__(self, labels, snapshots): self.labels=labels; self.snapshots=snapshots
        def status(self, mode): return {"labels":self.labels,"snapshots":self.snapshots}
    q=object.__new__(QuantResearchOrchestratorService)
    q.data=Data(10,21)
    q._latest_cycle=lambda mode:{"completed_at":datetime.now(timezone.utc).isoformat(),"label_count":10,"snapshot_count":20,"mode":mode}
    called=[]
    q.run_cycle=lambda **kw: called.append(kw) or {"ok":True,"state":"TEST_RUN","trial_count":kw.get("trial_count")}
    evidence_result=QuantResearchOrchestratorService.maybe_run_cycle(q,mode="intraday",trigger="r45-test")
    check("NEW_SNAPSHOT_TRIGGERS_IMMEDIATE_CYCLE", bool(called) and called[0].get("trial_count")==1 and evidence_result.get("state")=="TEST_RUN", f"called={called}")
    called.clear(); q.data=Data(10,20)
    nochange=QuantResearchOrchestratorService.maybe_run_cycle(q,mode="intraday",trigger="r45-test")
    check("UNCHANGED_EVIDENCE_RESPECTS_CADENCE", not called and nochange.get("state")=="NOT_DUE", f"state={nochange.get('state')}")

    # Dynamic reconciliation truth: rejected row must stay deterministic bootstrap.
    from core.research_lifecycle_reconciliation_service import ResearchLifecycleReconciliationService
    conn=sqlite3.connect(":memory:"); conn.row_factory=sqlite3.Row
    conn.execute("CREATE TABLE shadow_lightgbm_models(model_id TEXT,horizon TEXT,state TEXT,observations INTEGER,trading_days INTEGER,regimes INTEGER,created_at TEXT,mode TEXT)")
    conn.execute("INSERT INTO shadow_lightgbm_models VALUES(?,?,?,?,?,?,?,?)",("bad","15m","REJECTED",500,100,3,"2026-08-18T00:00:00Z","intraday")); conn.commit()
    store=type("Store",(),{"conn":conn})()
    r=ResearchLifecycleReconciliationService(store).paper_model_status("intraday")
    check("REJECTED_ROW_FAILS_CLOSED", r.get("learned_model_available") is False and r.get("admission_authority")=="DETERMINISTIC_BOOTSTRAP" and r.get("training_state")=="REJECTED", f"status={r}")

    report={"ok":not failures,"scope":"R45_RESEARCH_LEARNING_CONVERGENCE","checks":checks,"failures":failures}
    print(json.dumps(report,indent=2,default=str))
    return 0 if not failures else 2

if __name__ == "__main__":
    raise SystemExit(main())
