from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    failures: list[str] = []
    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"gate": name, "state": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            failures.append(name)

    compose = (ROOT / "infra/compose/docker-compose.yml").read_text(encoding="utf-8-sig")
    data_plane = (ROOT / "installer/data_plane.ps1").read_text(encoding="utf-8-sig")
    probe = (ROOT / "installer/postgres_connectivity_probe.py").read_text(encoding="utf-8-sig")
    authority = (ROOT / "installer/authority_retention_gate.ps1").read_text(encoding="utf-8-sig")
    lineage = (ROOT / "validation/verify_parent_migration_lineage.py").read_text(encoding="utf-8-sig")
    local_state = (ROOT / "installer/local_state_manifest.py").read_text(encoding="utf-8-sig")

    check("SAFE_DEFAULT_PORTS", "${LADDU_OPERATIONAL_PORT:-15432}:5432" in compose and "${LADDU_GOVERNANCE_PORT:-15433}:5432" in compose, "Windows defaults avoid the observed excluded 55421-55520 range")
    check("NO_STALE_HARDCODED_DSN_PORTS", "@127.0.0.1:55432/" not in data_plane and "@127.0.0.1:55433/" not in data_plane, "DSNs cannot diverge from Docker-selected ports")
    check("EXCLUDED_RANGE_AND_BIND_PROOF", "Get-LadduWindowsExcludedTcpRanges" in data_plane and "Test-LadduPortBindable" in data_plane and "Resolve-LadduPostgresHostPort" in data_plane, "PostgreSQL host ports use Windows excluded-range and bind proof")
    check("ACTUAL_DOCKER_PORT_DSN", "Get-LadduPostgresPublishedPort" in data_plane and "$actualOperationalPort" in data_plane and "$actualGovernancePort" in data_plane and "@127.0.0.1:$actualOperationalPort/laddu_operational" in data_plane and "@127.0.0.1:$actualGovernancePort/laddu_governance" in data_plane, "Admin DSNs come from effective Docker bindings")
    check("PACKAGED_PROBE_PRESENT", (ROOT / "installer/postgres_connectivity_probe.py").is_file(), "Connectivity logic ships as a Python file instead of PowerShell inline source")
    check("NO_INLINE_PYTHON_C", "$PythonExe -c" not in data_plane and "& $PythonExe -c" not in data_plane and "$pythonCode" not in data_plane, "Windows native argument parsing cannot corrupt embedded Python source")
    check("PROBE_FILE_INVOCATION", "$PythonExe $probePath $requestPath --retry-seconds 35" in data_plane, "PowerShell invokes the packaged probe by path")
    check("AUTHENTICATED_CONNECTIVITY_GATE", "SELECT 1,current_database(),current_user" in probe and "AUTHENTICATED_SELECT_1_PASS" in data_plane, "Packaged probe authenticates and verifies exact database/user identity")
    check("CONNECTIVITY_RETRY", "deadline = time.monotonic() + total_seconds" in probe and "connect_timeout=3" in probe, "Probe performs bounded readiness retry")
    check("PRIOR_RUNTIME_DSN_REBOUND", "if($hadPriorRuntimeEnv)" in data_plane and "Write-LadduRuntimeDataPlaneEnv" in data_plane, "Existing runtime DSNs are rebound before lineage/retention checks")
    check("PREPARE_HANDOFF_USES_EFFECTIVE_DSN", "Write-PrepareHandoff -ResolvedMode $Mode -OperationalDsn $OperationalAdminDsn -GovernanceDsn $GovernanceAdminDsn" in data_plane, "Apply receives the exact DSNs proven during Prepare")
    check("CONNECTIVITY_FAILURE_CLASSIFICATION", "DATABASE_UNREACHABLE" in lineage and "AUTHENTICATION_FAILED" in lineage and "DATABASE_NOT_FOUND" in lineage and "MIGRATION_LEDGER_OR_IMMUTABILITY" in lineage, "Connectivity/authentication and ledger-integrity failures are distinct")
    check("NO_FALSE_LEDGER_MESSAGE", "Authoritative parent migration ledger is incomplete or immutable migration hashes differ" not in authority and "failure_class" in authority, "PowerShell gate reports verifier failure class instead of relabeling timeout as ledger corruption")
    check("TRANSIENT_SECRET_REQUEST_MANAGED", "secure/data-plane.connectivity.request.json" in local_state, "Crash-left transient connectivity request cannot break durable-state equality")
    check("PROBE_NO_SECRET_OUTPUT", '"secrets_emitted": False' in probe, "Probe output explicitly declares that secrets are not emitted")

    # Compile the exact probe to catch the class of defect that escaped R28.
    try:
        compile(probe, str(ROOT / "installer/postgres_connectivity_probe.py"), "exec")
        probe_compile = True
    except Exception:
        probe_compile = False
    check("PROBE_PYTHON_COMPILES", probe_compile, "Packaged connectivity probe compiles as standalone Python")

    result = {
        "ok": not failures,
        "scope": "R29_WINDOWS_POSTGRES_PROBE_EXECUTION_CLOSURE",
        "checks": checks,
        "passed": len(checks) - len(failures),
        "failed": len(failures),
        "failures": failures,
        "production_ready": False,
        "broker_authority": "NONE",
    }
    print(json.dumps(result, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
