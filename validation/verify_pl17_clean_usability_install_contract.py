from __future__ import annotations
import hashlib, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
checks=[]
def check(name, ok, detail=None): checks.append({'name':name,'ok':bool(ok),'detail':detail})

# Exact clean-line identity.
config=(ROOT/'backend/config.py').read_text(encoding='utf-8-sig')
index=(ROOT/'frontend/index.html').read_text(encoding='utf-8-sig')
front=json.loads((ROOT/'frontend/release-identity.json').read_text(encoding='utf-8-sig'))
installer=(ROOT/'installer/install.ps1').read_text(encoding='utf-8-sig')
final_runner=(ROOT/'RUN_FINAL_PRODUCT_ACCEPTANCE.ps1').read_text(encoding='utf-8-sig')
readiness=(ROOT/'backend/core/product_readiness_service.py').read_text(encoding='utf-8-sig')
market_data=(ROOT/'backend/core/market_data_service.py').read_text(encoding='utf-8-sig')
scanner=(ROOT/'backend/core/scan_orchestration_modes.py').read_text(encoding='utf-8-sig')
frontend=(ROOT/'frontend/app.js').read_text(encoding='utf-8-sig')
research=(ROOT/'backend/tools/refresh_research_catalog.py').read_text(encoding='utf-8-sig')

marker='production-usability-r8-pl17-clean-8086'
check('exact PL17 clean marker', marker in config and front.get('build_marker')==marker and f'data-build-marker="{marker}"' in index)
check('PL17 cache and visible identity', '131.0.0-r8-pl17-clean-8086' in index and 'v131 · R8 · PL17 · 8086' in index)

# Core architectural correction: browser is not part of atomic install commit.
check('installer commit gate is browser-independent',
      'Browser/customer acceptance is post-install evidence and is not an atomic installation commit gate' in installer
      and 'browser_acceptance=\'POST_INSTALL_REQUIRED_NOT_INSTALL_COMMIT_GATE\'' in installer
      and 'installed_customer_vertical_acceptance_r3.py' not in installer
      and 'installed_browser_identity_smoke.py' not in installer
      and "Set-InstallTransactionPhase -Phase 'FRONTEND_IDENTITY'" in installer)
check('post-install browser and lifecycle proof remains mandatory',
      'installed_customer_vertical_acceptance_r3.py' in final_runner
      and 'Restart-Service' in final_runner
      and '--require-actionable' in final_runner
      and '--require-settlement' in final_runner
      and '--track-lifecycle' in final_runner)

# PL13 usability closure retained, but without inheriting rejected installer gates.
check('product readiness binds browser proof to exact current build',
      'str(browser.get("build_marker") or "") == BUILD_MARKER' in readiness)
check('intraday analysis uses bounded recent-cache path',
      'recent_reader = getattr(self.store, "get_recent_candles", None)' in market_data
      and 'SCAN_BUDGET_DEFERRED' in scanner
      and 'time.monotonic() >= deadline' in scanner)
check('truthful Actionable empty state and no synthesized R:R',
      'must never synthesize missing trade truth' in frontend
      and 'Admission blocked:' in frontend
      and 'Set at final admission' in frontend)
check('research mixed-schema normalization retained',
      'normalized_nse_official_union_sql' in research and 'DESCRIBE SELECT * FROM read_parquet' in research)

# Functional delta must equal the previously-reviewed PL13/PL16 usability implementation.
expected_functional={
'RUN_FINAL_PRODUCT_ACCEPTANCE.ps1':'a5b4560b74a244415fb1eff1b201899e8f3fad80956a9c0d0d5de744f09671b3',
'backend/core/desk_runtime_authority.py':'729d10ba075202b7f494ebf27a707ed66fcbcc3e6612fd28b1d3c3831f850b0f',
'backend/core/market_data_service.py':'7ad9af11c3bb1cb13a407b3b2b71e014438056801760402233270c1e90f3098d',
'backend/core/nse_official_report_ingestion_service.py':'3c42712f79bc4ed56720dabf78a9b1708cc8618a952aab9dd8689781de63d125',
'backend/core/product_readiness_service.py':'2364864a85f54e3959ee483989a27258f62839b42c17b39bb0542a86d0d7db84',
'backend/core/scan_orchestration_modes.py':'0afd0d00a577c4edda94b79724f72787526c93fbf1ed9d0bd76c70e7ade9d07f',
'backend/routes_get_dependencies.py':'44cd2ce2cbe20516961edf1da91d3e3fd476e2af6aec15bbae90c56bd15d0de8',
'backend/routes_get_system.py':'7e98cb6d209ad7ee32ed5817edb58a254a969173bfc47c2dda1c458c3600a34c',
'backend/routes_post.py':'c37082cf0be9b03763e6885eb6b30e42f15d44d2f7924d60e246dd008b58b865',
'backend/tools/refresh_research_catalog.py':'986da819edb0cbf5409945e6b706461d1c9d509059b5df10334d5e6227fb8384',
'frontend/app.js':'99bff70de875b81f231120240059db43ada7fd56e867dbaa32c17fb7b329805e',
'validation/exact_vertical_tracker.py':'8fe55717da57b04e85f33d45949228291d7fb5d7d53a7f90629d049d655d720a',
'validation/installed_customer_vertical_acceptance_r3.py':'5a4e98e995d6a56bff73f63b5b7c7205d1ad89a2a9c955d94d0bb88471c08500',
}
mm=[]
for rel,exp in expected_functional.items():
    act=hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()
    if act!=exp: mm.append({'file':rel,'expected':exp,'actual':act})
check('usability functional delta is exact', not mm, mm)

# Trading maths stays exactly PL12.
protected={
'backend/core/decision_engine_service.py':'e035a0e3c36521ed2150a2ed9fcc18ef8e352b328a1b4a8f99be1a22e7c4cc69',
'backend/core/trade_geometry_authority.py':'517c265231fd0da0142329780d93e1895e1350e917e8c5f8d7f9b254853e3525',
'backend/core/exact_broker_cash_cost_authority.py':'70f463a37021fc2422b3d3195b11df13b1a8e59ca88f10d000459d99f723a727',
'backend/core/model_paper_lifecycle_authority.py':'2d0a69453d7568aab420191df675a4677cd5a6407eb045c4eab3e7ad7b1aac8c',
'backend/core/outcome_accuracy_taxonomy.py':'2c000a60c2570c341416ae7bf6e5fbf53d72b618a19da05fcd1e2ae0a04eb470',
'backend/core/intraday_session_structure_authority.py':'b296c8a3972ab7eacee85da2c2242fcb27e781180267cb9b87741aa6a97f06eb',
'backend/core/structural_trade_map_service.py':'f3d0b139947ba79ec7a3ccb8a65f29fb5ba76bc98e57393c62199f4aa694d0c9',
'backend/core/evidence_engine_service.py':'f0c33451e1293ce4c53ed13ae0375b9caa00ca15e2915d5f5b1cb60126333c9e',
}
mm=[]
for rel,exp in protected.items():
    act=hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()
    if act!=exp: mm.append({'file':rel,'expected':exp,'actual':act})
check('PL12 protected trading mathematics frozen', not mm, mm)

passed=sum(1 for x in checks if x['ok']); failed=len(checks)-passed
print(json.dumps({'ok':failed==0,'contract':'PL17_CLEAN_USABILITY_INSTALL_CLOSURE','passed':passed,'failed':failed,'checks':checks},indent=2))
raise SystemExit(0 if failed==0 else 2)
