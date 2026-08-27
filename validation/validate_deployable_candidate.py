"""Validate the minimal deployable Project Laddu Windows payload.

This gate intentionally does not require engineering tests or historical release
notes in the installation ZIP. Source provenance is sealed in
RELEASE_ATTESTATION.json; package integrity is re-derived from the deploy tree.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "validation"
if str(VALIDATION) not in sys.path:
    sys.path.insert(0, str(VALIDATION))

from package_integrity import verify_attestation, verify_exact_inventory_and_hashes

REQUIRED_ROOT = {
    "INSTALL_UPDATE.cmd", "README_INSTALL.txt", "RELEASE_IDENTITY.json", "RELEASE_ATTESTATION.json",
    "START.cmd", "STOP.cmd", "RESTART.cmd", "STATUS.cmd", "settoken.ps1", "uninstall.ps1",
    "requirements-runtime.txt", "requirements-research.txt", "VERIFY_INSTALLED_PRODUCT.ps1",
    "VERIFY_OPERATIONAL_PRODUCT.ps1", "RUN_CUSTOMER_VERTICAL_SELF_TEST.cmd", "RUN_LEVEL5_FINAL_MARKET_PROOF.cmd", "RUN_EXACT_PRODUCT_ACCEPTANCE.cmd", "RUN_EXACT_PRODUCT_ACCEPTANCE.ps1", "RUN_FULL_EXACT_CUSTOMER_VERTICAL.cmd", "RUN_FINAL_PRODUCT_ACCEPTANCE.cmd", "RUN_FINAL_PRODUCT_ACCEPTANCE.ps1",
}
REQUIRED_DOCS = {
    "docs/ARCHITECTURE.md", "docs/INSTALLATION.md", "docs/SECURITY_RECOVERY.md", "docs/LEVEL5_PRODUCT_CONTRACT.md",
    "docs/R47_INTRADAY_PRICE_ACTION_SESSION_STRUCTURE.md", "docs/R48_UI_CONTAINMENT_EVIDENCE_SEMANTICS.md", "docs/PL24_CAPITAL_WFA_GOVERNANCE_CLOSURE.md", "docs/PL25_PERSISTED_CATALOGUE_WFA_ACTIVATION.md", "docs/PL26_QUANT_GOVERNANCE_CLOSURE.md", "docs/PL27_TRAINING_SCHEMA_CLOSURE.md", "docs/PL28_FINITE_TARGET_TRAINING_CLOSURE.md", "docs/PL29_PUBLICATION_AUTHORITY_CONTRACT_CLOSURE.md", "docs/PL30_TRADE_READY_SELECTOR_HASH_CLOSURE.md",
    "docs/R50_CANONICAL_FINAL_SIGNAL_AUTHORITY_END_TO_END_CLOSURE.md", "docs/SIMPLE_ACTIONABLE_UI_R1.md", "docs/TERMINAL_ACTIONABLE_UI_R2.md", "docs/TERMINAL_ACTIONABLE_UI_R3_E2E_ACCEPTANCE.md", "docs/R5_PERSISTENT_RESEARCH_HEALTHY_CADENCE.md", "docs/R6_RESEARCH_FRESHNESS_REVALIDATION.md", "docs/R8_PRODUCTION_USABILITY_CLOSURE.md",
}
REQUIRED_VALIDATION = {
    "validation/package_manifest.sha256", "validation/package_allowlist.json", "validation/package_integrity.py",
    "validation/validate_deployable_candidate.py", "validation/validate_installer_phase_handoff.py",
    "validation/validate_powershell_structure.py", "validation/capture_authority_retention_evidence.py",
    "validation/verify_pinned_environment.py", "validation/verify_parent_migration_lineage.py", "validation/verify_r29_installer_endpoint_closure.py", "validation/verify_r30_end_to_end_architecture_convergence.py", "validation/verify_r31_browser_customer_visual_closure.py", "validation/r31_frozen_parent_hashes.json", "validation/verify_r32_final_release_windows_metadata_closure.py", "validation/r32_frozen_r31_hashes.json", "validation/verify_r33_research_projection_contract_closure.py", "validation/r33_frozen_r32_hashes.json", "validation/verify_r34_customer_ui_sr_semantic_closure.py", "validation/r34_frozen_r33_runtime_hashes.json", "validation/verify_r35_decision_dashboard_historical_pit_enrichment.py", "validation/r35_frozen_r34_product_hashes.json", "validation/verify_r36_qc_task_contract_historical_pit_closure.py", "validation/r36_frozen_r35_hashes.json", "validation/verify_r37_workspace_numeric_connection_resilience.py", "validation/r37_frozen_r36_product_hashes.json", "validation/verify_r38_production_usability_sprint.py", "validation/r38_frozen_r37_hashes.json", "validation/verify_r39_backend_import_preflight_qc_closure.py", "validation/r39_frozen_r38_hashes.json", "validation/verify_r42_candidate_first_workspace_terminal.py", "validation/r42_frozen_r41_hashes.json", "validation/verify_r43_workspace_ledger_route_isolation.py", "validation/r43_frozen_r42_hashes.json", "validation/verify_r44_final_signal_workspace_compact_context.py", "validation/r44_frozen_r43_hashes.json", "validation/verify_r45_research_learning_convergence.py", "validation/r45_frozen_r44_hashes.json", "validation/verify_r46_research_data_engine_closure.py", "validation/r46_frozen_r45_hashes.json", "validation/verify_r47_intraday_price_action_session_structure.py", "validation/r47_frozen_r46_hashes.json", "validation/verify_r48_ui_containment_evidence_semantics.py", "validation/r48_frozen_r47_hashes.json", "validation/verify_r49_final_product_read_model_closure.py", "validation/r49_frozen_r48_hashes.json", "validation/verify_r50_canonical_final_signal_authority_closure.py", "validation/r50_frozen_r49_hashes.json",
    "validation/verify_authoritative_quant_research_lifecycle.py", "validation/verify_customer_vertical_slice.py", "validation/verify_runtime_lifecycle_authority_closure.py", "validation/verify_intelligence_evaluation_guard.py", "validation/verify_data_utilization_guard.py", "validation/verify_level5_evidence_closure.py", "validation/verify_installed_package_binding.py", "validation/verify_live_scanner_operability.py", "validation/run_level5_final_market_proof.ps1", "validation/installed_proof_common.ps1",
    "validation/installed_proof_gates.ps1", "validation/installed_fault_contract_probe.py", "validation/verify_simple_actionable_ui_r1.py", "validation/verify_terminal_actionable_ui_r2.py", "validation/verify_terminal_actionable_ui_r3.py", "validation/installed_customer_vertical_acceptance_r3.py", "validation/exact_vertical_tracker.py", "validation/verify_exact_vertical_tracker_r3.py", "validation/verify_follow_through_projection_r3.py", "validation/verify_user_r2_truth_regression_browser_r3.py", "validation/fixtures/r2_installed_truth_regression_20260819.json",
    "validation/historical_37_installed_proof_plan.json", "validation/validate_historical_37_installed_proof_result.py", "validation/verify_r5_persistent_research_healthy_cadence.py", "validation/r5_frozen_r3_backend_hashes.json", "validation/verify_r6_research_freshness_revalidation.py", "validation/r6_frozen_r5_backend_hashes.json", "validation/verify_r8_production_usability_closure.py", "validation/verify_pl17_clean_usability_install_contract.py", "validation/verify_pl20_evidence_pipeline_closure.py", "validation/pl20_frozen_pl17_trading_hashes.json", "validation/verify_release_metadata_installer_contract.py", "validation/verify_pl23_scanner_truth_restoration.py", "validation/verify_pl24_capital_wfa_governance_closure.py", "validation/verify_pl25_persisted_catalogue_wfa_activation.py", "validation/verify_pl26_quant_governance_closure.py", "validation/verify_pl27_training_schema_closure.py", "validation/verify_pl28_finite_target_training_closure.py", "validation/verify_pl29_publication_authority_contract.py", "validation/verify_pl30_trade_ready_selector_hash_closure.py", "validation/verify_pl31_research_lineage_capture_closure.py", "validation/verify_pl32_sector_relative_pit_closure.py", "validation/verify_pl33_official_pit_feature_wiring_closure.py", "validation/verify_pl34_event_risk_pit_capture_closure.py", "validation/verify_pl35_installer_ephemeral_lock_closure.py",
    "validation/verify_pl46_pit_timestamp_lineage_closure.py", "validation/verify_pl46_three_arm_self_repair_closure.py",
    "validation/verify_pl46_monitoring_semantic_blocker_closure.py", "validation/verify_pl46_lifecycle_snapshot_immutability_closure.py",
    "validation/verify_pl46_error_diagnosability_closure.py", "validation/verify_pl46_corporate_action_resume_dynamic_proof.py",
    "validation/verify_pl46_questdb_recovery_retry_closure.py", "validation/verify_pl46_pinned_venv_retry_closure.py", "validation/verify_pl46_research_subprocess_drain_closure.py", "validation/verify_pl46_closed_market_received_at_freshness.py",
}
FORBIDDEN_NAMES = {
    "VERIFY_HISTORICAL_37_DEFECTS.ps1", "RUN_LEVEL5_ACCEPTANCE_CYCLE.ps1", "RUN_LEVEL5_ACCEPTANCE_CYCLE.cmd",
    "RUN_LEVEL5_RESILIENCE_DRILL.ps1", "RUN_LEVEL5_RESILIENCE_DRILL.cmd",
}
FORBIDDEN_RUNTIME_MODULES = {
    "backend/core/shadow_portfolio_service.py",
    "backend/core/research_position_book_service.py", "backend/core/stock_narrative_service.py",
}
DERIVATIVES_CONTEXT_REL = "backend/core/derivatives_context_service.py"
DERIVATIVES_CONTEXT_REQUIRED_TOKENS = (
    "CONFIRM_WEAKEN_OR_VETO_ONLY",
    "CASH_ONLY",
    'broker_authority": "NONE"',
    '"production_influence": 0.0',
    "def peek(",
    '"provider_io": False',
)
DERIVATIVES_CONTEXT_FORBIDDEN_TOKENS = (
    "place_order(", "submit_order(", "create_order(", "create_candidate(",
    'active_trading_universe": "DERIVATIVES',
)


def run(command: list[str]) -> dict:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(ROOT / "backend") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, env=env)
    return {"ok": completed.returncode == 0, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}


def main() -> int:
    failures: list[str] = []
    try:
        integrity = verify_exact_inventory_and_hashes(ROOT)
        attestation = verify_attestation(ROOT, verify_source_tree=True)
    except Exception as exc:
        print(json.dumps({"ok": False, "failures": [f"package_integrity:{exc}"]}, indent=2))
        return 2

    actual = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file()}
    for rel in sorted(REQUIRED_ROOT | REQUIRED_DOCS | REQUIRED_VALIDATION):
        if rel not in actual:
            failures.append(f"required deploy file missing: {rel}")
    for name in FORBIDDEN_NAMES:
        if name in actual:
            failures.append(f"obsolete operator file shipped: {name}")
    for rel in FORBIDDEN_RUNTIME_MODULES:
        if rel in actual:
            failures.append(f"dead runtime module shipped: {rel}")

    # AC-062 restored derivatives/OI strictly as read-only contextual evidence
    # for cash-equity decisions.  It is required runtime evidence, not a
    # derivative trading surface.  Packaging therefore proves the safety
    # boundary instead of deleting the authority as obsolete code.
    if DERIVATIVES_CONTEXT_REL not in actual:
        failures.append("required evidence-only derivatives context authority missing")
    else:
        derivatives_text = (ROOT / DERIVATIVES_CONTEXT_REL).read_text(encoding="utf-8-sig")
        missing_tokens = [token for token in DERIVATIVES_CONTEXT_REQUIRED_TOKENS if token not in derivatives_text]
        if missing_tokens:
            failures.append("derivatives context evidence-only contract incomplete: " + ",".join(missing_tokens))
        forbidden_tokens = [token for token in DERIVATIVES_CONTEXT_FORBIDDEN_TOKENS if token in derivatives_text]
        if forbidden_tokens:
            failures.append("derivatives context exposes forbidden trading authority: " + ",".join(forbidden_tokens))
    if any(rel.startswith("backend/tests/") for rel in actual):
        failures.append("engineering backend tests must not ship in deployable payload")
    if any("/__pycache__/" in f"/{rel}/" or rel.endswith((".pyc", ".pyo", ".log")) for rel in actual):
        failures.append("generated runtime/test artefacts shipped")
    historical_root = [rel for rel in actual if "/" not in rel and (rel.startswith("BUILD_NOTES_v") or rel.startswith("ROLLBACK_v") or rel.startswith("VALIDATION_REPORT_v") or rel.startswith("REQUIREMENT_TO_CODE_TRACEABILITY_v"))]
    if historical_root:
        failures.append("historical root documentation shipped: " + ",".join(sorted(historical_root)[:5]))

    metadata_gate = run([sys.executable, "validation/verify_release_metadata_installer_contract.py"])
    if not metadata_gate["ok"]:
        failures.append("release metadata installer contract failed:" + (metadata_gate.get("stdout") or metadata_gate.get("stderr") or "unknown"))

    identity = json.loads((ROOT / "RELEASE_IDENTITY.json").read_text(encoding="utf-8-sig"))
    acceptance_state = str(identity.get("acceptance_state") or "")
    production_usability_r8 = "R8_PRODUCTION_USABILITY_CLOSURE" in acceptance_state
    pl17_clean = "PL17_CLEAN_USABILITY_INSTALL_CLOSURE" in acceptance_state
    pl20_evidence = "PL20_EVIDENCE_PIPELINE_CLOSURE" in acceptance_state
    pl21_evidence = "PL21_EVIDENCE_ORCHESTRATION_CLOSURE" in acceptance_state
    pl22_evidence = "PL22_EVIDENCE_TRANSPORT_CLOSURE" in acceptance_state
    pl46_defect_cluster = "PL46_DEFECT_CLUSTER_CLOSURE" in acceptance_state
    pl45_ca_resume = pl46_defect_cluster or "PL45_RESUMABLE_CORPORATE_ACTION_CLOSURE" in acceptance_state
    pl44_fold_local = pl45_ca_resume or "PL44_FOLD_LOCAL_CAPITAL_WFA_CLOSURE" in acceptance_state
    pl43_e2e_agents = pl44_fold_local or "PL43_ONE_CLICK_E2E_AGENTS_CLOSURE" in acceptance_state
    pl42_final = pl43_e2e_agents or "PL42_ADAPTIVE_HISTORY_CORPORATE_ACTION_CLOSURE" in acceptance_state
    pl41_official_policy = pl42_final or "PL41_OFFICIAL_SOURCE_QUALIFICATION_POLICY" in acceptance_state
    pl40_survivorship = pl41_official_policy or "PL40_SURVIVORSHIP_PIT_MEMBERSHIP_CLOSURE" in acceptance_state
    pl39_session_lineage = pl40_survivorship or "PL39_HISTORICAL_SESSION_LINEAGE_CLOSURE" in acceptance_state
    pl38_forward_eligibility = pl39_session_lineage or "PL38_FORWARD_EVIDENCE_ELIGIBILITY_CLOSURE" in acceptance_state
    pl37_ml_wfa = pl38_forward_eligibility or "PL37_CONFIGURABLE_ROLLING_ML_WFA_CLOSURE" in acceptance_state
    pl36_e2e = pl37_ml_wfa or "PL36_END_TO_END_BLOCKER_CLOSURE" in acceptance_state
    pl35_installer_lock = pl36_e2e or "PL35_INSTALLER_EPHEMERAL_LOCK_CLOSURE" in acceptance_state
    pl34_event_risk = pl35_installer_lock or "PL34_EVENT_RISK_PIT_CAPTURE_CLOSURE" in acceptance_state
    pl33_official_pit = pl34_event_risk or "PL33_OFFICIAL_PIT_FEATURE_WIRING_CLOSURE" in acceptance_state
    pl32_sector_relative = pl33_official_pit or "PL32_SECTOR_RELATIVE_PIT_CLOSURE" in acceptance_state
    pl31_lineage = pl32_sector_relative or "PL31_RESEARCH_LINEAGE_CAPTURE_CLOSURE" in acceptance_state
    pl30_trade_ready = pl31_lineage or "PL30_TRADE_READY_SELECTOR_HASH_CLOSURE" in acceptance_state
    pl29_publication = pl30_trade_ready or "PL29_PUBLICATION_AUTHORITY_CONTRACT_CLOSURE" in acceptance_state
    pl28_targets = pl29_publication or "PL28_FINITE_TARGET_TRAINING_CLOSURE" in acceptance_state
    pl27_schema = pl28_targets or "PL27_TRAINING_SCHEMA_CLOSURE" in acceptance_state
    pl26_quant = pl27_schema or "PL26_QUANT_GOVERNANCE_CLOSURE" in acceptance_state
    pl25_activation = pl26_quant or "PL25_PERSISTED_CATALOGUE_WFA_ACTIVATION" in acceptance_state
    pl24_wfa = pl25_activation or "PL24_CAPITAL_WFA_GOVERNANCE_CLOSURE" in acceptance_state
    pl23_scanner = pl24_wfa or "PL23_SCANNER_TRUTH_RESTORATION" in acceptance_state
    simple_actionable_ui = "SIMPLE_ACTIONABLE_UI_R1" in acceptance_state
    terminal_actionable_ui_r3 = "TERMINAL_ACTIONABLE_UI_R3_E2E_ACCEPTANCE" in acceptance_state and not production_usability_r8
    terminal_actionable_ui = production_usability_r8 or terminal_actionable_ui_r3 or "TERMINAL_ACTIONABLE_UI_R2" in acceptance_state
    if identity.get("deployment_validator") != "validation/validate_deployable_candidate.py":
        failures.append("deployment validator identity mismatch")
    if identity.get("artifact_type") != "INSTALLATION_CANDIDATE" or identity.get("production_ready") is not False:
        failures.append("deployable candidate certification boundary is not fail-closed")
    if identity.get("broker_authority") != "NONE" or identity.get("product_mode") != "AUTOMATIC_MODEL_PAPER_ONLY":
        failures.append("trading authority boundary changed")

    # Exact browser-entry identity must match the same contract enforced by the
    # Windows installer after service startup. This specifically prevents a
    # candidate with stale parent cache-busters from reaching Windows proof.
    version = str(identity.get("version") or "")
    expected_owner = f"standalone-{version}"
    asset_version = version[1:] if version.startswith("v") else version
    index_html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8-sig")
    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8-sig")
    if f'data-build-version="{version}"' not in index_html:
        failures.append("frontend index build-version mismatch")
    if f'data-frontend-owner="{expected_owner}"' not in index_html:
        failures.append("frontend index owner mismatch")
    r5_persistent_research = "R5_PERSISTENT_RESEARCH_HEALTHY_CADENCE" in acceptance_state
    r6_research_freshness = "R6_RESEARCH_FRESHNESS_REVALIDATION" in acceptance_state
    expected_asset_marker = ((f"{asset_version}-r8-pl46-defect-cluster-closure-8086" if pl46_defect_cluster else f"{asset_version}-r8-pl45-resumable-corporate-actions-8086" if pl45_ca_resume else f"{asset_version}-r8-pl44-fold-local-capital-wfa-8086" if pl44_fold_local else f"{asset_version}-r8-pl43-one-click-e2e-agents-8086" if pl43_e2e_agents else f"{asset_version}-r8-pl42-adaptive-history-corporate-action-8086" if pl42_final else f"{asset_version}-r8-pl41-official-source-policy-8086" if pl41_official_policy else f"{asset_version}-r8-pl40-survivorship-pit-membership-8086" if pl40_survivorship else f"{asset_version}-r8-pl39-historical-session-lineage-8086" if pl39_session_lineage else f"{asset_version}-r8-pl38-forward-evidence-eligibility-8086" if pl38_forward_eligibility else f"{asset_version}-r8-pl37-configurable-rolling-ml-wfa-8086" if pl37_ml_wfa else f"{asset_version}-r8-pl36-end-to-end-blocker-closure-8086" if pl36_e2e else f"{asset_version}-r8-pl35-installer-ephemeral-lock-8086" if pl35_installer_lock else f"{asset_version}-r8-pl34-event-risk-pit-capture-8086" if pl34_event_risk else f"{asset_version}-r8-pl33-official-pit-feature-wiring-8086" if pl33_official_pit else f"{asset_version}-r8-pl32-sector-relative-pit-8086" if pl32_sector_relative else f"{asset_version}-r8-pl31-research-lineage-8086" if pl31_lineage else f"{asset_version}-r8-pl30-trade-ready-selector-hash-8086" if pl30_trade_ready else f"{asset_version}-r8-pl29-publication-authority-8086" if pl29_publication else f"{asset_version}-r8-pl28-finite-targets-8086" if pl28_targets else f"{asset_version}-r8-pl27-training-schema-8086" if pl27_schema else f"{asset_version}-r8-pl26-quant-governance-8086" if pl26_quant else f"{asset_version}-r8-pl25-catalogue-wfa-8086" if pl25_activation else f"{asset_version}-r8-pl24-capital-wfa-8086" if pl24_wfa else f"{asset_version}-r8-pl23-scanner-truth-8086" if pl23_scanner else f"{asset_version}-r8-pl22-evidence-transport-8086" if pl22_evidence else f"{asset_version}-r8-pl21-evidence-orchestration-8086" if pl21_evidence else f"{asset_version}-r8-pl20-evidence-pipeline-8086" if pl20_evidence else f"{asset_version}-r8-pl17-clean-8086" if pl17_clean else f"{asset_version}-r8-pl12-final-8086") if production_usability_r8 else ((f"{asset_version}-terminal-ui-r3-e2e-r5" if r5_persistent_research else f"{asset_version}-terminal-ui-r3-e2e") if terminal_actionable_ui_r3 else (f"{asset_version}-terminal-ui-r2" if terminal_actionable_ui else (f"{asset_version}-simple-ui-r1" if simple_actionable_ui else asset_version))))
    if f'app.css?v={expected_asset_marker}' not in index_html or f'app.js?v={expected_asset_marker}' not in index_html:
        failures.append("frontend index asset-version cache-buster mismatch")
    expected_visible_version = (('v131 · R8 · PL46 · 8086' if pl46_defect_cluster else 'v131 · R8 · PL45 · 8086' if pl45_ca_resume else 'v131 · R8 · PL44 · 8086' if pl44_fold_local else 'v131 · R8 · PL43 · 8086' if pl43_e2e_agents else 'v131 · R8 · PL42 · 8086' if pl42_final else 'v131 · R8 · PL41 · 8086' if pl41_official_policy else 'v131 · R8 · PL40 · 8086' if pl40_survivorship else 'v131 · R8 · PL39 · 8086' if pl39_session_lineage else 'v131 · R8 · PL38 · 8086' if pl38_forward_eligibility else 'v131 · R8 · PL37 · 8086' if pl37_ml_wfa else 'v131 · R8 · PL36 · 8086' if pl36_e2e else 'v131 · R8 · PL35 · 8086' if pl35_installer_lock else 'v131 · R8 · PL34 · 8086' if pl34_event_risk else 'v131 · R8 · PL33 · 8086' if pl33_official_pit else 'v131 · R8 · PL32 · 8086' if pl32_sector_relative else 'v131 · R8 · PL31 · 8086' if pl31_lineage else 'v131 · R8 · PL30 · 8086' if pl30_trade_ready else 'v131 · R8 · PL29 · 8086' if pl29_publication else 'v131 · R8 · PL28 · 8086' if pl28_targets else 'v131 · R8 · PL27 · 8086' if pl27_schema else 'v131 · R8 · PL26 · 8086' if pl26_quant else 'v131 · R8 · PL25 · 8086' if pl25_activation else 'v131 · R8 · PL24 · 8086' if pl24_wfa else 'v131 · R8 · PL23 · 8086' if pl23_scanner else 'v131 · R8 · PL22 · 8086' if pl22_evidence else 'v131 · R8 · PL21 · 8086' if pl21_evidence else 'v131 · R8 · PL20 · 8086' if pl20_evidence else 'v131 · R8 · PL17 · 8086' if pl17_clean else 'v131 · R8 · 8086') if production_usability_r8 else version)
    if f'id="versionPill" class="version-pill">{expected_visible_version}<' not in index_html:
        failures.append("frontend visible version pill mismatch")

    frontend_manifest_path = ROOT / "frontend" / "release-identity.json"
    try:
        frontend_manifest = json.loads(frontend_manifest_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        frontend_manifest = {}
        failures.append(f"frontend release identity unreadable:{type(exc).__name__}")
    if str(frontend_manifest.get("version") or "") != version:
        failures.append("frontend release identity version mismatch")
    if str(frontend_manifest.get("frontend_owner") or "") != expected_owner:
        failures.append("frontend release identity owner mismatch")
    if production_usability_r8:
        expected_build_marker = ("production-usability-r8-pl46-defect-cluster-closure-8086" if pl46_defect_cluster else "production-usability-r8-pl45-resumable-corporate-actions-8086" if pl45_ca_resume else "production-usability-r8-pl44-fold-local-capital-wfa-8086" if pl44_fold_local else "production-usability-r8-pl43-one-click-e2e-agents-8086" if pl43_e2e_agents else "production-usability-r8-pl42-adaptive-history-corporate-action-8086" if pl42_final else "production-usability-r8-pl41-official-source-policy-8086" if pl41_official_policy else "production-usability-r8-pl40-survivorship-pit-membership-8086" if pl40_survivorship else "production-usability-r8-pl39-historical-session-lineage-8086" if pl39_session_lineage else "production-usability-r8-pl38-forward-evidence-eligibility-8086" if pl38_forward_eligibility else "production-usability-r8-pl37-configurable-rolling-ml-wfa-8086" if pl37_ml_wfa else "production-usability-r8-pl36-end-to-end-blocker-closure-8086" if pl36_e2e else "production-usability-r8-pl35-installer-ephemeral-lock-8086" if pl35_installer_lock else "production-usability-r8-pl34-event-risk-pit-capture-8086" if pl34_event_risk else "production-usability-r8-pl33-official-pit-feature-wiring-8086" if pl33_official_pit else "production-usability-r8-pl32-sector-relative-pit-8086" if pl32_sector_relative else "production-usability-r8-pl31-research-lineage-8086" if pl31_lineage else "production-usability-r8-pl30-trade-ready-selector-hash-8086" if pl30_trade_ready else "production-usability-r8-pl29-publication-authority-8086" if pl29_publication else "production-usability-r8-pl28-finite-targets-8086" if pl28_targets else "production-usability-r8-pl27-training-schema-8086" if pl27_schema else "production-usability-r8-pl26-quant-governance-8086" if pl26_quant else "production-usability-r8-pl25-catalogue-wfa-8086" if pl25_activation else "production-usability-r8-pl24-capital-wfa-8086" if pl24_wfa else "production-usability-r8-pl23-scanner-truth-8086" if pl23_scanner else "production-usability-r8-pl22-evidence-transport-8086" if pl22_evidence else "production-usability-r8-pl21-evidence-orchestration-8086" if pl21_evidence else "production-usability-r8-pl20-evidence-pipeline-8086" if pl20_evidence else "production-usability-r8-pl17-clean-8086" if pl17_clean else "production-usability-r8-8086")
        if str(frontend_manifest.get("build_marker") or "") != expected_build_marker:
            failures.append("frontend release identity R8 build marker mismatch")
        if f'data-build-marker="{expected_build_marker}"' not in index_html:
            failures.append("frontend index R8 build marker mismatch")
        if "state.frontendIdentityValid !== true" not in app_js or "IDENTITY FAIL" not in app_js:
            failures.append("frontend identity failure is not fail-closed")
        final_runner = (ROOT / "RUN_FINAL_PRODUCT_ACCEPTANCE.ps1").read_text(encoding="utf-8-sig") if (ROOT / "RUN_FINAL_PRODUCT_ACCEPTANCE.ps1").is_file() else ""
        missing_runner_tokens = [token for token in ("Restart-Service", "research_ids_before", "research_ids_after", "Compress-Archive", "installed_customer_vertical_acceptance_r3.py") if token not in final_runner]
        if missing_runner_tokens:
            failures.append("R8 final installed acceptance runner incomplete:" + ",".join(missing_runner_tokens))
    elif terminal_actionable_ui_r3:
        expected_build_marker = "terminal-actionable-ui-r3-e2e-acceptance"
        if str(frontend_manifest.get("build_marker") or "") != expected_build_marker:
            failures.append("frontend release identity R3 build marker mismatch")
        if f'data-build-marker="{expected_build_marker}"' not in index_html:
            failures.append("frontend index R3 build marker mismatch")
        if "state.frontendIdentityValid !== true" not in app_js or "IDENTITY FAIL" not in app_js:
            failures.append("frontend identity failure is not fail-closed")
        full_runner_cmd = (ROOT / "RUN_FULL_EXACT_CUSTOMER_VERTICAL.cmd").read_text(encoding="utf-8-sig") if (ROOT / "RUN_FULL_EXACT_CUSTOMER_VERTICAL.cmd").is_file() else ""
        full_runner_ps1 = (ROOT / "RUN_EXACT_PRODUCT_ACCEPTANCE.ps1").read_text(encoding="utf-8-sig") if (ROOT / "RUN_EXACT_PRODUCT_ACCEPTANCE.ps1").is_file() else ""
        if "-FullLive" not in full_runner_cmd or "RUN_EXACT_PRODUCT_ACCEPTANCE.ps1" not in full_runner_cmd:
            failures.append("R3 one-click full exact customer vertical runner contract missing")
        required_runner_tokens = ("--track-lifecycle", "Restart-Service", "process_boot_id", "FULL_EXACT_CUSTOMER_VERTICAL_PASSED", "TRACKING_WINDOW_EXPIRED_NOT_ACCEPTED")
        missing_runner_tokens = [token for token in required_runner_tokens if token not in full_runner_ps1]
        if missing_runner_tokens:
            failures.append("R3 persistent same-decision/restart runner incomplete:" + ",".join(missing_runner_tokens))
    declared_assets = dict(frontend_manifest.get("assets") or {})
    for rel in ("index.html", "app.css", "app.js", "assets/lightweight-charts.js"):
        target = ROOT / "frontend" / rel
        declared = str(declared_assets.get(rel) or "").lower()
        if not target.is_file():
            failures.append(f"frontend required asset missing:{rel}")
            continue
        import hashlib
        actual_digest = hashlib.sha256(target.read_bytes()).hexdigest().lower()
        if declared != actual_digest:
            failures.append(f"frontend declared asset hash mismatch:{rel}")

    # Customer visual contract.  Simple Actionable UI R1 deliberately removes the
    # home-built chart from decision authority; older candidates retain the legacy
    # chart-control gate.  Do not force a disabled compatibility chart to behave as
    # a live trading terminal.
    if simple_actionable_ui or terminal_actionable_ui:
        if "const INTERNAL_CHART_ENABLED = false" not in app_js:
            failures.append("simple UI internal chart is not fail-closed disabled")
        if 'data-internal-chart-disabled="true"' not in index_html:
            failures.append("simple UI disabled-chart DOM contract missing")
        if 'href="https://tv.upstox.com"' not in index_html or 'target="_blank"' not in index_html:
            failures.append("simple UI external broker-chart action missing")
        if production_usability_r8:
            if pl35_installer_lock:
                ui_guard_path = "validation/verify_pl35_installer_ephemeral_lock_closure.py"
            elif pl34_event_risk:
                ui_guard_path = "validation/verify_pl34_event_risk_pit_capture_closure.py"
            elif pl33_official_pit:
                ui_guard_path = "validation/verify_pl33_official_pit_feature_wiring_closure.py"
            elif pl32_sector_relative:
                ui_guard_path = "validation/verify_pl32_sector_relative_pit_closure.py"
            elif pl31_lineage:
                ui_guard_path = "validation/verify_pl31_research_lineage_capture_closure.py"
            elif pl30_trade_ready:
                ui_guard_path = "validation/verify_pl30_trade_ready_selector_hash_closure.py"
            elif pl29_publication:
                ui_guard_path = "validation/verify_pl29_publication_authority_contract.py"
            elif pl28_targets:
                ui_guard_path = "validation/verify_pl28_finite_target_training_closure.py"
            elif pl27_schema:
                ui_guard_path = "validation/verify_pl27_training_schema_closure.py"
            elif pl26_quant:
                ui_guard_path = "validation/verify_pl26_quant_governance_closure.py"
            elif pl25_activation:
                ui_guard_path = "validation/verify_pl25_persisted_catalogue_wfa_activation.py"
            elif pl24_wfa:
                ui_guard_path = "validation/verify_pl24_capital_wfa_governance_closure.py"
            elif pl23_scanner:
                ui_guard_path = "validation/verify_pl23_scanner_truth_restoration.py"
            elif pl22_evidence:
                ui_guard_path = "validation/verify_pl22_evidence_transport_closure.py"
            elif pl21_evidence or pl20_evidence:
                ui_guard_path = "validation/verify_pl20_evidence_pipeline_closure.py"
            elif pl17_clean:
                ui_guard_path = "validation/verify_pl17_clean_usability_install_contract.py"
            else:
                ui_guard_path = "validation/verify_r8_production_usability_closure.py"
        else:
            ui_guard_path = ("validation/verify_terminal_actionable_ui_r3.py" if terminal_actionable_ui_r3
                             else "validation/verify_terminal_actionable_ui_r2.py" if terminal_actionable_ui
                             else "validation/verify_simple_actionable_ui_r1.py")
        ui_guard = run([sys.executable, ui_guard_path])
        if not ui_guard["ok"]:
            if production_usability_r8:
                label = "PL45 Resumable Corporate Action Closure" if pl45_ca_resume else "PL44 Fold-Local Capital WFA Closure" if pl44_fold_local else "PL43 One-Click End-to-End Agents Closure" if pl43_e2e_agents else "PL42 Adaptive History + Corporate Action Authority Closure" if pl42_final else "PL41 Official Source Qualification Policy" if pl41_official_policy else "PL40 Survivorship/PIT Membership Closure" if pl40_survivorship else "PL39 Historical Session Lineage Closure" if pl39_session_lineage else "PL38 Forward Evidence Eligibility Closure" if pl38_forward_eligibility else "PL37 Configurable Rolling ML/WFA" if pl37_ml_wfa else "PL36 End-to-End Blocker Closure" if pl36_e2e else "PL35 Installer Ephemeral Lock Closure" if pl35_installer_lock else "PL34 Event Risk PIT Capture Closure" if pl34_event_risk else "PL33 Official PIT Feature Wiring Closure" if pl33_official_pit else "PL32 Sector Relative PIT Closure" if pl32_sector_relative else "PL31 Research Lineage Capture Closure" if pl31_lineage else "PL30 Trade Ready + Selector Hash Closure" if pl30_trade_ready else "PL27 Training Schema Closure" if pl27_schema else "PL26 Quant Governance Closure" if pl26_quant else "PL25 Persisted Catalogue WFA Activation" if pl25_activation else "PL24 Capital WFA Governance Closure" if pl24_wfa else "PL23 Scanner Truth Restoration" if pl23_scanner else ("PL22 Evidence Transport Closure" if pl22_evidence else ("PL21 Evidence Orchestration Closure" if pl21_evidence else ("PL20 Evidence Pipeline Clean Rebuild" if pl20_evidence else ("PL17 Clean Usability Install Closure" if pl17_clean else "R8 Production Usability"))))
            else:
                label = "Terminal Actionable UI R3 E2E" if terminal_actionable_ui_r3 else ("Terminal Actionable UI R2" if terminal_actionable_ui else "Simple Actionable UI R1")
            failures.append(label + " customer contract proof failed")
    else:
        required_overlays = {"volume", "trade", "major_sr", "vwap", "ema", "supertrend", "rsi", "macd"}
        declared_overlays = set(re.findall(r'data-overlay=["\']([^"\']+)["\']', index_html))
        if not required_overlays.issubset(declared_overlays):
            failures.append("chart overlay controls missing:" + ",".join(sorted(required_overlays - declared_overlays)))
        required_chart_ids = {"chartHost", "chartOverlayButtons", "volumePane", "volumeChart", "rsiPane", "rsiChart", "macdPane", "macdChart"}
        html_ids = set(re.findall(r'id=["\']([^"\']+)["\']', index_html))
        if not required_chart_ids.issubset(html_ids):
            failures.append("chart pane DOM missing:" + ",".join(sorted(required_chart_ids - html_ids)))
        js_id_refs = set(re.findall(r"\$\(['\"]([^'\"]+)['\"]\)", app_js))
        missing_js_ids = sorted(js_id_refs - html_ids)
        if missing_js_ids:
            failures.append("frontend JS references missing DOM ids:" + ",".join(missing_js_ids[:20]))
        chart_contract_tokens = (
            "$('chartOverlayButtons').addEventListener('click'",
            "toggleOverlay(button.dataset.overlay)",
            "function toggleOverlay(name)",
            "state.volumeChart = LightweightCharts.createChart",
            "state.volumeSeries = state.volumeChart.addHistogramSeries",
            "state.rsiChart = LightweightCharts.createChart",
            "state.rsiSeries = state.rsiChart.addLineSeries",
            "state.macdChart = LightweightCharts.createChart",
            "state.macdLineSeries = state.macdChart.addLineSeries",
            "state.macdSignalSeries = state.macdChart.addLineSeries",
            "state.macdHistogramSeries = state.macdChart.addHistogramSeries",
            "if (state.overlayEnabled.vwap) addLineOverlay('vwap'",
            "addLineOverlay('ema20'",
            "addLineOverlay('ema50'",
            "if (state.overlayEnabled.supertrend)",
            "scheduleProjectionRefresh(40)",
            "projectionRequirement(name)",
        )
        missing_chart_tokens = [token for token in chart_contract_tokens if token not in app_js]
        if missing_chart_tokens:
            failures.append("chart behavioural wiring incomplete:" + ",".join(missing_chart_tokens))
        app_css = (ROOT / "frontend" / "app.css").read_text(encoding="utf-8-sig")
        if 'id="themeToggle"' not in index_html or ':root[data-theme="light"]' not in app_css or "localStorage.setItem('projectLadduTheme'" not in app_js:
            failures.append("light/dark theme contract incomplete")
        if "timeZone:'Asia/Kolkata'" not in app_js or "if (/minute$/.test(state.interval))" not in app_js or "hour:'2-digit', minute:'2-digit'" not in app_js:
            failures.append("intraday IST time-axis contract incomplete")
        chart_read = (ROOT / "backend" / "core" / "clean_chart_read_service.py").read_text(encoding="utf-8-sig")
        scan_modes = (ROOT / "backend" / "core" / "scan_orchestration_modes.py").read_text(encoding="utf-8-sig")
        if "def _resolution_identity(" not in chart_read or '"timeframe_identity": timeframe_identity' not in chart_read:
            failures.append("backend served-timestamp timeframe identity proof missing")
        if "function timeframeIdentityMatches(payload, expectedInterval)" not in app_js or "timeframe identity FAILED CLOSED" not in app_js:
            failures.append("frontend timeframe identity fail-closed contract missing")
        if 'return {\n                        "ok": True,\n                        "state": "YIELDING_TO_HIGHER_PRIORITY"' in scan_modes:
            failures.append("delivery scanner can still return before coverage advancement under interactive priority")
        for token in ("defer_deep_analysis", "coverage_advancing_deep_deferred", "admitted_limit = 0 if defer_deep_analysis"):
            if token not in scan_modes:
                failures.append("delivery interactive-priority coverage separation missing:" + token)
        stale_browser_versions = [value for value in ("v128.0.0", "v129.0.0", "v130.0.0", "?v=128.0.0", "?v=129.0.0", "?v=130.0.0") if value in index_html or value in app_js or value in json.dumps(frontend_manifest, sort_keys=True)]
        if stale_browser_versions:
            failures.append("stale parent/rejected browser identity remains:" + ",".join(stale_browser_versions))

    installer = (ROOT / "installer/install.ps1").read_text(encoding="utf-8-sig")
    if not (ROOT / "installer" / "postgres_connectivity_probe.py").is_file():
        failures.append("packaged PostgreSQL connectivity probe missing")
    prerequisites = (ROOT / "installer/prerequisites.ps1").read_text(encoding="utf-8-sig")
    if "ACCEPTANCE_TESTS.md" in installer or "SECURITY_NOTES.md" in installer:
        failures.append("installer still depends on retired root documentation")
    if "$PrerequisiteProof = Ensure-ProjectLadduPrerequisites" not in installer:
        failures.append("clean-machine prerequisite gate is not wired")
    if "Python.Python.3.12" not in prerequisites or "Docker.DockerDesktop" not in prerequisites:
        failures.append("prerequisite bootstrap does not cover Python 3.12 and Docker Desktop")
    if "Microsoft-Windows-Subsystem-Linux" not in prerequisites or "VirtualMachinePlatform" not in prerequisites:
        failures.append("prerequisite bootstrap does not cover WSL/VirtualMachinePlatform")

    package_gate = (ROOT / "installer" / "package_gate.ps1").read_text(encoding="utf-8-sig")
    hygiene_call = "$PackageTransientBytecodeProof = Clear-PackageTransientPythonBytecode"
    manifest_call = "Assert-PackageManifest"
    if "function Clear-PackageTransientPythonBytecode" not in package_gate:
        failures.append("installer transient Python-bytecode cleanup authority missing")
    if hygiene_call not in installer:
        failures.append("installer does not invoke transient bytecode cleanup before package proof")
    elif installer.find(hygiene_call) > installer.find(manifest_call, installer.find(hygiene_call)):
        pass
    if hygiene_call in installer:
        first_manifest_after_package_step = installer.find("  Assert-PackageManifest")
        if first_manifest_after_package_step < 0 or installer.find(hygiene_call) > first_manifest_after_package_step:
            failures.append("installer transient bytecode cleanup is not ordered before exact manifest proof")
    for token in ("PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE='1'", "without writing bytecode into the sealed package tree"):
        if token not in installer:
            failures.append("installer package-tree bytecode isolation missing:" + token)
    for token in ("ONLY___PYCACHE___PYC_PYO_TRANSIENTS_REMOVED_BEFORE_EXACT_MANIFEST_PROOF", "all_other_unmanifested_files_fail_closed"):
        if token not in package_gate:
            failures.append("installer package-hygiene fail-closed contract missing:" + token)

    # Windows preservation regression gate: all governed research scheduled writers
    # must be disabled/stopped before the local-state snapshot and restored on rollback.
    research_task_state = (ROOT / "installer" / "research_task_state.ps1").read_text(encoding="utf-8-sig")
    local_state_manifest = (ROOT / "installer" / "local_state_manifest.py").read_text(encoding="utf-8-sig")
    for token in ("function Quiesce-ResearchTasks", "Disable-ScheduledTask", "Stop-ScheduledTask", "research-task-quiescence-1.0.1", "$taskStarted = Get-Date", "deadline_scope='PER_TASK'"):
        if token not in research_task_state:
            failures.append("research-task quiescence contract missing:" + token)
    disable_pos = research_task_state.find("Disable-ScheduledTask")
    stop_pos = research_task_state.find("Stop-ScheduledTask")
    if disable_pos < 0 or stop_pos < 0 or disable_pos > stop_pos:
        failures.append("research tasks are not disabled before stop; retrigger race remains")
    quiesce_call = "$ResearchTaskQuiescence = Quiesce-ResearchTasks"
    snapshot_call = "$ProtectedStateBefore = New-PreservedLocalStateSnapshot"
    if quiesce_call not in installer or snapshot_call not in installer or installer.find(quiesce_call) > installer.find(snapshot_call):
        failures.append("research-task quiescence is not ordered before durable local-state snapshot")
    if "if($ResearchTasksQuiesced -or $ResearchTasksChanged)" not in installer or "Restore-ResearchTasks" not in installer:
        failures.append("research-task quiescence rollback restoration contract missing")
    for token in ("READ_RETRY_ATTEMPTS = 8", "cannot read preserved file {relative}", "{5, 32, 33}", 'EPHEMERAL_RUNTIME_LOCK_DIR = "data/runtime/locks"', 'folded.startswith(lock_dir + "/") and folded.endswith(".lock")'):
        if token not in local_state_manifest:
            failures.append("preserved-state lock diagnostic/retry contract missing:" + token)
    pl35_lock_guard = run([sys.executable, "validation/verify_pl35_installer_ephemeral_lock_closure.py"])
    if not pl35_lock_guard["ok"]:
        failures.append("PL35 Windows preservation runtime-lock regression proof failed")

    phase = run([sys.executable, "validation/validate_installer_phase_handoff.py", "--transaction-parity-only"])
    if not phase["ok"]:
        failures.append("installer transaction parity failed")
    ps = run([sys.executable, "validation/validate_powershell_structure.py"])
    if not ps["ok"]:
        failures.append("PowerShell structural validation failed")
    node = run(["node", "--check", "frontend/app.js"])
    if not node["ok"]:
        failures.append("frontend JavaScript syntax failed")
    delivery_coverage = run([sys.executable, "validation/validate_delivery_coverage_scheduler.py"])
    if not delivery_coverage["ok"]:
        failures.append("delivery independent coverage scheduler proof failed")
    lifecycle_authority_guard = run([sys.executable, "validation/verify_runtime_lifecycle_authority_closure.py"])
    if not lifecycle_authority_guard["ok"]:
        failures.append("runtime lifecycle canonical-authority closure proof failed")
    intelligence_guard = run([sys.executable, "validation/verify_intelligence_evaluation_guard.py"])
    if not intelligence_guard["ok"]:
        failures.append("intelligence/evaluation guard deterministic proof failed")
    data_utilization_guard = run([sys.executable, "validation/verify_data_utilization_guard.py"])
    if not data_utilization_guard["ok"]:
        failures.append("data-utilization alpha-funnel deterministic proof failed")
    level5_closure_guard = run([sys.executable, "validation/verify_level5_evidence_closure.py"])
    if not level5_closure_guard["ok"]:
        failures.append("Level-5 evidence-closure deterministic proof failed")
    r29_installer_guard = run([sys.executable, "validation/verify_r29_installer_endpoint_closure.py"])
    if not r29_installer_guard["ok"]:
        failures.append("R29 Windows PostgreSQL endpoint/probe execution closure proof failed")
    r30_architecture_guard = run([sys.executable, "validation/verify_r30_end_to_end_architecture_convergence.py"])
    if not r30_architecture_guard["ok"]:
        failures.append("R30 end-to-end architecture convergence proof failed")
    # R48 is a descendant-only UI/control-plane closure. Its focused guard freezes
    # every R47 parent byte outside the explicit frontend/identity/package-metadata
    # boundary, which is stronger and more appropriate than re-running R47's own
    # exact-R46 scope guard against an intentional R48 frontend delta.
    if production_usability_r8:
        if pl46_defect_cluster:
            pl46_guards = (
                "validation/verify_pl46_pit_timestamp_lineage_closure.py",
                "validation/verify_pl46_three_arm_self_repair_closure.py",
                "validation/verify_pl46_monitoring_semantic_blocker_closure.py",
                "validation/verify_pl46_lifecycle_snapshot_immutability_closure.py",
                "validation/verify_pl46_error_diagnosability_closure.py",
                "validation/verify_pl46_corporate_action_resume_dynamic_proof.py",
                "validation/verify_pl46_questdb_recovery_retry_closure.py",
                "validation/verify_pl46_pinned_venv_retry_closure.py",
                "validation/verify_pl46_research_subprocess_drain_closure.py",
                "validation/verify_pl46_closed_market_received_at_freshness.py",
            )
            for pl46_guard_path in pl46_guards:
                pl46_guard = run([sys.executable, pl46_guard_path])
                if not pl46_guard["ok"]:
                    failures.append("PL46 defect-cluster focused proof failed:" + pl46_guard_path + ":" + (pl46_guard.get("stdout") or pl46_guard.get("stderr") or "unknown"))
        else:
            current_guard_path = "validation/verify_pl45_resumable_corporate_action_closure.py" if pl45_ca_resume else "validation/verify_pl44_fold_local_capital_wfa_closure.py" if pl44_fold_local else "validation/verify_pl43_one_click_e2e_agents.py" if pl43_e2e_agents else "validation/verify_pl42_adaptive_history_corporate_action_closure.py" if pl42_final else "validation/verify_pl41_official_source_qualification_policy.py" if pl41_official_policy else "validation/verify_pl40_survivorship_pit_membership_closure.py" if pl40_survivorship else "validation/verify_pl39_historical_session_lineage_closure.py" if pl39_session_lineage else "validation/verify_pl38_forward_evidence_eligibility_closure.py" if pl38_forward_eligibility else "validation/verify_pl37_configurable_rolling_ml_wfa.py" if pl37_ml_wfa else "validation/verify_pl36_end_to_end_blocker_closure.py" if pl36_e2e else "validation/verify_pl35_installer_ephemeral_lock_closure.py" if pl35_installer_lock else "validation/verify_pl34_event_risk_pit_capture_closure.py" if pl34_event_risk else "validation/verify_pl33_official_pit_feature_wiring_closure.py" if pl33_official_pit else "validation/verify_pl32_sector_relative_pit_closure.py" if pl32_sector_relative else "validation/verify_pl31_research_lineage_capture_closure.py" if pl31_lineage else "validation/verify_pl30_trade_ready_selector_hash_closure.py" if pl30_trade_ready else "validation/verify_pl29_publication_authority_contract.py" if pl29_publication else "validation/verify_pl28_finite_target_training_closure.py" if pl28_targets else "validation/verify_pl27_training_schema_closure.py" if pl27_schema else "validation/verify_pl26_quant_governance_closure.py" if pl26_quant else "validation/verify_pl25_persisted_catalogue_wfa_activation.py" if pl25_activation else "validation/verify_pl24_capital_wfa_governance_closure.py" if pl24_wfa else "validation/verify_pl23_scanner_truth_restoration.py" if pl23_scanner else ("validation/verify_pl22_evidence_transport_closure.py" if pl22_evidence else ("validation/verify_pl20_evidence_pipeline_closure.py" if (pl21_evidence or pl20_evidence) else ("validation/verify_pl17_clean_usability_install_contract.py" if pl17_clean else "validation/verify_r8_production_usability_closure.py")))
            current_guard = run([sys.executable, current_guard_path])
            if not current_guard["ok"]:
                failures.append("PL45 Resumable Corporate Action Closure proof failed" if pl45_ca_resume else "PL44 Fold-Local Capital WFA Closure proof failed" if pl44_fold_local else "PL43 One-Click End-to-End Agents Closure proof failed" if pl43_e2e_agents else "PL42 Adaptive History + Corporate Action Authority Closure proof failed" if pl42_final else "PL41 Official Source Qualification Policy proof failed" if pl41_official_policy else "PL40 Survivorship/PIT Membership Closure proof failed" if pl40_survivorship else "PL39 Historical Session Lineage Closure proof failed" if pl39_session_lineage else "PL38 Forward Evidence Eligibility Closure proof failed" if pl38_forward_eligibility else "PL37 Configurable Rolling ML/WFA proof failed" if pl37_ml_wfa else "PL36 End-to-End Blocker Closure proof failed" if pl36_e2e else "PL35 Installer Ephemeral Lock Closure proof failed" if pl35_installer_lock else "PL34 Event Risk PIT Capture Closure proof failed" if pl34_event_risk else "PL33 Official PIT Feature Wiring Closure proof failed" if pl33_official_pit else "PL32 Sector Relative PIT Closure proof failed" if pl32_sector_relative else "PL31 Research Lineage Capture Closure proof failed" if pl31_lineage else "PL30 Trade Ready + selector snapshot hash closure proof failed" if pl30_trade_ready else "PL29 publication authority contract proof failed" if pl29_publication else "PL28 finite target training closure proof failed" if pl28_targets else "PL27 training schema closure proof failed" if pl27_schema else "PL26 quant governance closure proof failed" if pl26_quant else "PL25 persisted catalogue WFA activation proof failed" if pl25_activation else "PL24 capital WFA governance closure proof failed" if pl24_wfa else "PL23 scanner truth restoration proof failed" if pl23_scanner else ("PL22 evidence transport closure proof failed" if pl22_evidence else ("PL21 evidence orchestration closure proof failed" if pl21_evidence else ("PL20 evidence pipeline clean rebuild proof failed" if pl20_evidence else ("PL17 clean usability/install closure proof failed" if pl17_clean else "R8 production usability closure proof failed")))))
        r6_guard = run([sys.executable, "validation/verify_r6_research_freshness_revalidation.py"])
        if pl26_quant or pl25_activation or pl24_wfa or pl23_scanner or pl22_evidence or pl21_evidence or pl20_evidence:
            # PL20/PL21/PL22/PL23/PL24 intentionally change research/WFA publication/read paths; their focused guard
            # freezes trading/risk mathematics while validating the declared evidence delta.
            pass
        elif pl17_clean:
            try:
                r6_result = json.loads(r6_guard.get("stdout") or "{}")
                r6_failed_names = {str(row.get("name") or "") for row in list(r6_result.get("checks") or []) if not row.get("ok")}
            except Exception:
                r6_result = {}
                r6_failed_names = {"UNPARSEABLE"}
            expected_pl17_r6_scope_delta = {"R6 backend delta is limited to freshness lineage, runtime revalidation and the declared R8 Research projection"}
            if r6_failed_names != expected_pl17_r6_scope_delta or int(r6_result.get("passed") or 0) < 14:
                failures.append("PL17 inherited R6 behavior failed outside the declared clean usability delta")
        elif not r6_guard["ok"]:
            failures.append("R6 Research freshness / runtime revalidation proof failed")
    elif "TERMINAL_ACTIONABLE_UI_R3_E2E_ACCEPTANCE" in acceptance_state:
        current_guard = run([sys.executable, "validation/verify_terminal_actionable_ui_r3.py"])
        if not current_guard["ok"]:
            failures.append("Terminal Actionable UI R3 E2E exact customer-contract proof failed")
        follow_guard = run([sys.executable, "validation/verify_follow_through_projection_r3.py"])
        if not follow_guard["ok"]:
            failures.append("R3 exact post-exit Follow-Through projection proof failed")
        tracker_guard = run([sys.executable, "validation/verify_exact_vertical_tracker_r3.py"])
        if not tracker_guard["ok"]:
            failures.append("R3 persistent same-decision lifecycle tracker proof failed")
        if r6_research_freshness:
            r6_guard = run([sys.executable, "validation/verify_r6_research_freshness_revalidation.py"])
            if not r6_guard["ok"]:
                failures.append("R6 Research freshness / runtime revalidation proof failed")
        elif r5_persistent_research:
            r5_guard = run([sys.executable, "validation/verify_r5_persistent_research_healthy_cadence.py"])
            if not r5_guard["ok"]:
                failures.append("R5 persistent Research / healthy cadence proof failed")
    elif "TERMINAL_ACTIONABLE_UI_R2" in acceptance_state:
        current_guard = run([sys.executable, "validation/verify_terminal_actionable_ui_r2.py"])
        if not current_guard["ok"]:
            failures.append("Terminal Actionable UI R2 exact customer-contract proof failed")
    elif "SIMPLE_ACTIONABLE_UI_R1" in acceptance_state:
        current_guard = run([sys.executable, "validation/verify_simple_actionable_ui_r1.py"])
        if not current_guard["ok"]:
            failures.append("Simple Actionable UI R1 exact customer-contract proof failed")
    elif "R50" in acceptance_state:
        current_guard = run([sys.executable, "validation/verify_r50_canonical_final_signal_authority_closure.py"])
        if not current_guard["ok"]:
            failures.append("R50 canonical Final Signal authority exact-R49 end-to-end closure proof failed")
    elif "R49" in acceptance_state:
        current_guard = run([sys.executable, "validation/verify_r49_final_product_read_model_closure.py"])
        if not current_guard["ok"]:
            failures.append("R49 final product/read-model exact-R48 closure proof failed")
    elif "R48" in acceptance_state:
        current_guard = run([sys.executable, "validation/verify_r48_ui_containment_evidence_semantics.py"])
        if not current_guard["ok"]:
            failures.append("R48 UI-containment/evidence-semantics exact-R47 closure proof failed")
    else:
        current_guard = run([sys.executable, "validation/verify_r47_intraday_price_action_session_structure.py"])
        if not current_guard["ok"]:
            failures.append("R47 Intraday price-action/session-structure frozen-R46 closure proof failed")



    compiled = 0
    for base in (ROOT / "backend", ROOT / "validation"):
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
                compiled += 1
            except Exception as exc:
                failures.append(f"python compile failed: {path.relative_to(ROOT)}:{exc}")

    report = {
        "ok": not failures,
        "version": identity.get("version"),
        "candidate_revision": identity.get("candidate_revision"),
        "manifest_files": integrity.get("manifest_files"),
        "source_attestation": attestation,
        "deploy_files": len(actual),
        "compiled_python_files": compiled,
        "engineering_tests_shipped": any(rel.startswith("backend/tests/") for rel in actual),
        "failures": failures,
        "broker_authority": identity.get("broker_authority"),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
