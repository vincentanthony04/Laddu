"""Static fail-closed proof for multi-phase Windows installer state handoff.

The v105 Windows failure proved that green source tests are insufficient when
Prepare and Apply are separate PowerShell script invocations. This validator
requires every state value used by Apply to be serialized, integrity checked,
bound to the same installer transaction and bound to the same release identity.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_PLANE = ROOT / "installer" / "data_plane.ps1"
INSTALLER = ROOT / "installer" / "install.ps1"
RECOVERY = ROOT / "installer" / "runtime_recovery.ps1"
TRANSACTION = ROOT / "installer" / "install_transaction.py"




def transaction_phase_contract() -> tuple[list[str], list[str], list[str]]:
    tree = ast.parse(TRANSACTION.read_text(encoding="utf-8-sig"))
    phases: tuple[str, ...] | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "PHASES" for target in node.targets):
            value = ast.literal_eval(node.value)
            phases = tuple(str(item) for item in value)
            break
    if not phases or phases[0] != "BEGIN" or phases[-1] != "COMMIT":
        return [], [], ["TRANSACTION_PHASE_AUTHORITY_INVALID"]
    installer_text = INSTALLER.read_text(encoding="utf-8-sig")
    calls = re.findall(r"Set-InstallTransactionPhase\s+-Phase\s+'([^']+)'", installer_text)
    # DATA_AUTHORITY_PROOF is present in both sides of one if/else branch.
    # Collapse consecutive/branch duplicates while preserving first real order.
    unique_calls: list[str] = []
    for phase in calls:
        if phase not in unique_calls:
            unique_calls.append(phase)
    expected = list(phases[1:-1])
    failures: list[str] = []
    unknown = [phase for phase in unique_calls if phase not in phases]
    missing = [phase for phase in expected if phase not in unique_calls]
    if unknown:
        failures.append("INSTALLER_CALLS_UNKNOWN_TRANSACTION_PHASE:" + ",".join(unknown))
    if missing:
        failures.append("TRANSACTION_PHASE_NOT_CALLED_BY_INSTALLER:" + ",".join(missing))
    if unique_calls != expected:
        failures.append("INSTALLER_TRANSACTION_PHASE_ORDER_MISMATCH")
    complete_index = installer_text.find("\n  Complete-InstallTransaction")
    last_phase_index = installer_text.rfind("Set-InstallTransactionPhase -Phase 'OPERATIONAL_PROOF'")
    if complete_index < 0 or last_phase_index < 0 or complete_index < last_phase_index:
        failures.append("TRANSACTION_COMMIT_NOT_AFTER_OPERATIONAL_PROOF")
    return expected, unique_calls, failures

def check(condition: bool, code: str, failures: list[str]) -> None:
    if not condition:
        failures.append(code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transaction-parity-only", action="store_true")
    args = parser.parse_args(argv)
    expected_phases, installer_phases, phase_failures = transaction_phase_contract()
    if args.transaction_parity_only:
        report = {
            "ok": not phase_failures,
            "contract": "installer-transaction-parity-2.0.0",
            "failures": phase_failures,
            "state_machine_phases": expected_phases,
            "installer_phases": installer_phases,
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if not phase_failures else 1
    data = DATA_PLANE.read_text(encoding="utf-8-sig")
    install = INSTALLER.read_text(encoding="utf-8-sig")
    recovery = RECOVERY.read_text(encoding="utf-8-sig")
    failures: list[str] = list(phase_failures)

    check("[string]$TransactionId = ''" in data, "TRANSACTION_ID_PARAMETER_MISSING", failures)
    check("$containerChanged = $false" in data, "APPLY_DEFAULT_STATE_NOT_INITIALIZED", failures)
    check("function Write-PrepareHandoff" in data, "PREPARE_HANDOFF_WRITER_MISSING", failures)
    check("function Read-PrepareHandoff" in data, "PREPARE_HANDOFF_READER_MISSING", failures)
    check("data-plane.prepare-handoff.json" in data and "data-plane.prepare-handoff.sha256" in data,
          "SECURE_HANDOFF_ARTIFACT_MISSING", failures)
    check("Prepared data-plane handoff checksum mismatch." in data, "HANDOFF_HASH_GATE_MISSING", failures)
    check("handoff.transaction_id -ne $TransactionId" in data, "HANDOFF_TRANSACTION_BINDING_MISSING", failures)
    check("handoff.release_identity_sha256 -ne (Get-ReleaseIdentityDigest)" in data,
          "HANDOFF_RELEASE_BINDING_MISSING", failures)
    check("Apply cannot infer state from a previous PowerShell invocation" in data,
          "APPLY_FAIL_CLOSED_MESSAGE_MISSING", failures)

    apply_load = data.find("if($Phase -eq 'Apply'){\n  $handoff = Read-PrepareHandoff")
    first_apply_use = data.find("Write-MutationState -Stage 'SCHEMA_MIGRATION_STARTED'")
    check(apply_load >= 0 and first_apply_use >= 0 and apply_load < first_apply_use,
          "HANDOFF_NOT_LOADED_BEFORE_SCHEMA_APPLY", failures)
    for assignment in (
        "$OperationalAdminDsn = [string]$handoff.operational_admin_dsn",
        "$GovernanceAdminDsn = [string]$handoff.governance_admin_dsn",
        "$QuestDbUrl = [string]$handoff.questdb_url",
        "$containerChanged = [bool]$handoff.container_lifecycle_changed",
    ):
        check(assignment in data, "APPLY_STATE_NOT_REHYDRATED:" + assignment.split("=")[0].strip(), failures)

    prepare_start = data.find("if($Phase -eq 'Prepare'){\n  $handoffSha256 = Write-PrepareHandoff")
    prepare_exit = data.find("exit 0", prepare_start)
    schema_tool = data.find("$tool = Join-Path", prepare_start)
    check(prepare_start >= 0 and prepare_exit >= 0 and schema_tool >= 0 and prepare_exit < schema_tool,
          "PREPARE_CAN_REACH_SCHEMA_MUTATION", failures)
    if prepare_start >= 0 and prepare_exit > prepare_start:
        proof_end = data.find("Set-Content -LiteralPath $PrepareProofPath", prepare_start)
        public_prepare = data[prepare_start:proof_end if proof_end >= 0 else prepare_exit]
        check("operational_admin_dsn =" not in public_prepare and "governance_admin_dsn =" not in public_prepare,
              "PUBLIC_PREPARE_PROOF_LEAKS_ADMIN_DSN", failures)

    prepare_call = "-Phase Prepare -TransactionId $RunId"
    upgrade_apply_call = "-Phase Apply -InPlaceUpgrade -TransactionId $RunId"
    clean_apply_call = "-Phase Apply -TransactionId $RunId"
    check(prepare_call in install, "INSTALLER_PREPARE_TRANSACTION_BINDING_MISSING", failures)
    check(upgrade_apply_call in install, "INSTALLER_UPGRADE_APPLY_TRANSACTION_BINDING_MISSING", failures)
    check(clean_apply_call in install, "INSTALLER_CLEAN_APPLY_TRANSACTION_BINDING_MISSING", failures)
    check(install.count("-TransactionId $RunId") == 3, "INSTALLER_PHASE_TRANSACTION_COUNT_INVALID", failures)

    stop_idx = install.find("Stopping any existing Project Laddu runtime")
    apply_idx = install.find("Applying schema only after runtime stop")
    check(stop_idx >= 0 and apply_idx > stop_idx, "APPLY_NOT_AFTER_RUNTIME_STOP", failures)

    check("Restore-ParentRuntimeOwner" in recovery, "PARENT_RUNTIME_RECOVERY_OWNER_MISSING", failures)
    check("$ready = Wait-Ready" in recovery, "PARENT_RUNTIME_RECOVERY_READINESS_MISSING", failures)
    check("actualVersion -ne $ExpectedVersion" in recovery, "PARENT_RUNTIME_RECOVERY_VERSION_GATE_MISSING", failures)
    check("parent-runtime-restore.json" in install, "PRIOR_RUNTIME_RECOVERY_EVIDENCE_MISSING", failures)
    check("$ParentVersionExpected = $PreviousInstalledVersion" in install, "ROLLBACK_VERSION_NOT_CAPTURED_FROM_PRIOR_RUNTIME", failures)
    catch_idx = install.find("catch {")
    check(catch_idx >= 0 and "Restore-ParentRuntimeOwner -ExpectedVersion $ParentVersionExpected" in install[catch_idx:],
          "FAILED_TRANSACTION_DOES_NOT_PROVE_PARENT_RESTORE", failures)

    report = {
        "ok": not failures,
        "contract": "installer-phase-handoff-1.0.0",
        "failures": failures,
        "facts": {
            "transaction_bound": True if not failures else None,
            "release_bound": True if not failures else None,
            "checksum_protected": True if not failures else None,
            "public_proof_contains_admin_dsn": False if "PUBLIC_PREPARE_PROOF_LEAKS_ADMIN_DSN" not in failures else True,
            "parent_restore_readiness_proved": True if not failures else None,
            "transaction_phase_parity": not phase_failures,
            "transaction_phase_count": len(expected_phases),
        },
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
