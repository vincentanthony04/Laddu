"""One authoritative installed-product acceptance contract.

The HTTP readiness service owns acceptance semantics.  Installers and verifiers
consume this projection instead of independently re-implementing looser rules.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

CONTRACT_VERSION = "installed-operational-acceptance-2.0.0-single-risk-lifecycle-authority"
REQUIRED_RUNTIME_STATUS_KEYS = (
    "production_data_plane",
    "startup_phases",
)
REQUIRED_INSTALLATION_CHECKS = (
    "production_data_plane",
    "startup_phases",
    "instrument_identity",
    "intraday_lifecycle",
    "delivery_lifecycle",
)


def evaluate_installation_acceptance(
    *,
    product_state: str,
    checks: Iterable[Mapping[str, Any]],
    missing_runtime_status_keys: Iterable[str] = (),
) -> dict[str, Any]:
    """Return a fail-closed, machine-readable installation decision."""
    rows = {str(row.get("key") or ""): dict(row) for row in checks or ()}
    missing_checks = [key for key in REQUIRED_INSTALLATION_CHECKS if key not in rows]
    non_ready = [
        {
            "key": key,
            "state": str(rows[key].get("state") or "MISSING").upper(),
            "detail": str(rows[key].get("detail") or ""),
        }
        for key in REQUIRED_INSTALLATION_CHECKS
        if key in rows and str(rows[key].get("state") or "").upper() != "READY"
    ]
    missing_runtime = sorted({str(key) for key in missing_runtime_status_keys if str(key)})
    # Installation safety is intentionally independent from immediate customer
    # usefulness. A cold/weekend install may have no scanner output yet, while
    # the data plane, identity authority and safety loops are fully valid.
    eligible = not missing_runtime and not missing_checks and not non_ready
    reasons: list[str] = []
    reasons.extend(f"MISSING_RUNTIME_STATUS:{key}" for key in missing_runtime)
    reasons.extend(f"MISSING_READINESS_CHECK:{key}" for key in missing_checks)
    reasons.extend(f"READINESS_CHECK_NOT_READY:{row['key']}:{row['state']}" for row in non_ready)
    return {
        "contract_version": CONTRACT_VERSION,
        "eligible": eligible,
        "state": "ACCEPTED" if eligible else "BLOCKED",
        "required_runtime_status_keys": list(REQUIRED_RUNTIME_STATUS_KEYS),
        "missing_runtime_status_keys": missing_runtime,
        "required_check_keys": list(REQUIRED_INSTALLATION_CHECKS),
        "missing_check_keys": missing_checks,
        "non_ready_required_checks": non_ready,
        "reason_codes": reasons,
        "observed_product_state": str(product_state or "UNKNOWN").upper(),
        "policy": "fail closed on installation-safety authorities; scanner output, research readiness and customer usefulness are reported separately and may warm after install",
    }
