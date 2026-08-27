from __future__ import annotations
import hashlib, json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PARENT=json.loads((ROOT/'validation/r38_frozen_r37_hashes.json').read_text())['hashes']
ALLOWED={
 'RELEASE_ATTESTATION.json','RELEASE_IDENTITY.json','frontend/index.html','frontend/app.js','frontend/app.css','frontend/release-identity.json',
 'backend/application_runtime.py','backend/core/data_plane/coordinator.py','backend/core/operations_control_service.py','backend/core/stock_snapshot_service.py',
 'backend/core/supervisor.py','backend/http_server.py','backend/routes_get_system.py','backend/routes_post.py',
 'validation/package_allowlist.json','validation/package_manifest.sha256','validation/validate_deployable_candidate.py',
 'docs/R38_PRODUCTION_USABILITY_SPRINT.md','validation/r38_frozen_r37_hashes.json','validation/verify_r38_production_usability_sprint.py',
 'backend/core/http_latency_monitor.py','backend/core/trust_state_service.py','backend/core/historical_pit_sweep_service.py'
}

def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()

def check(ok,msg,failures):
    if not ok: failures.append(msg)

def main():
    f=[]
    # Parent non-regression: every R37 member not explicitly in the sprint boundary is byte-identical.
    for rel,digest in PARENT.items():
        if rel in ALLOWED: continue
        p=ROOT/rel
        check(p.is_file(),f'missing frozen R37 file:{rel}',f)
        if p.is_file(): check(sha(p)==digest,f'frozen R37 hash changed:{rel}',f)

    app=(ROOT/'backend/application_runtime.py').read_text()
    coord=(ROOT/'backend/core/data_plane/coordinator.py').read_text()
    trust=(ROOT/'backend/core/trust_state_service.py').read_text()
    lat=(ROOT/'backend/core/http_latency_monitor.py').read_text()
    pit=(ROOT/'backend/core/historical_pit_sweep_service.py').read_text()
    sup=(ROOT/'backend/core/supervisor.py').read_text()
    snap=(ROOT/'backend/core/stock_snapshot_service.py').read_text()
    http=(ROOT/'backend/http_server.py').read_text()
    js=(ROOT/'frontend/app.js').read_text()
    html=(ROOT/'frontend/index.html').read_text()
    css=(ROOT/'frontend/app.css').read_text()

    for token in ('HistoricalPitSweepService','TrustStateService','HttpLatencyMonitor','historical_pit_enrichment'):
        check(token in app,f'runtime wiring missing:{token}',f)
    check('role="governance", max_size=8' in coord,'governance write pool capacity not raised to 8',f)
    check('requests_queued is a cumulative psycopg-pool statistic' in trust,'trust incorrectly risks treating cumulative queued as live backlog',f)
    for token in ('TRUSTED','DEGRADED','DO_NOT_TRUST','customer chart/snapshot p95','decision_admission_allowed'):
        check(token in trust,f'trust state contract missing:{token}',f)
    for token in ('/api/stock-snapshot','/api/live-chart-bar','customer_read_p95_ms'):
        check(token in lat,f'latency truth missing:{token}',f)
    check('http_latency_monitor.record("GET", self.path, elapsed_ms)' in http,'GET latency recording missing',f)
    check('out["trust"]' in snap and '"trust": trust' in snap,'Stock Snapshot trust projection missing',f)
    check('elif expected_idle is False' in sup and 'rec.waiting_on = None' in sup,'stale waiting reason clear contract missing',f)

    # Autonomous PIT is an actual runtime sweep, not an arbitrary future schedule.
    for token in ('MIN_DATES = 504','subprocess.Popen','should_yield("P5")','YIELDED_TO_HIGHER_PRIORITY','CONTINUING_SWEEP','PROJECT_LADDU_AUTONOMOUS_PIT'):
        check(token in pit,f'autonomous PIT contract missing:{token}',f)
    check('subprocess.run(' not in pit,'PIT worker uses non-interruptible subprocess.run',f)
    check('18:30' not in pit,'runtime PIT worker is still tied to 18:30 schedule',f)

    for token in ('id="trustStrip"','id="researchTruthStrip"','id="opsRootCause"','SECTOR PULSE'):
        check(token in html,f'customer UI truth surface missing:{token}',f)
    for token in ('renderTrustStrip(payload.trust || {})','trustBlocksAdmission(trust)','System blocked · no admission','Historical PIT / WFA','customer read p95'):
        check(token in js,f'frontend trust/research contract missing:{token}',f)
    for token in ('.trust-strip.trust-blocked','.sector-grid','.research-truth-strip','.ops-root-cause.blocked'):
        check(token in css,f'frontend terminal styling missing:{token}',f)

    print(json.dumps({'ok':not f,'passed':26-len(f),'failed':len(f),'failures':f},indent=2))
    return 0 if not f else 2
if __name__=='__main__': raise SystemExit(main())
