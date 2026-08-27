"""Executable architecture fitness checks for Project Laddu production desks.

This module turns the two-desk architecture into a startup invariant instead of
leaving it as a convention spread across config, engines and worker wiring.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

TOPOLOGY_VERSION = "production-topology-1.0.0"


class ProductionTopologyError(RuntimeError):
    """Raised when a declared production desk has incomplete or duplicate wiring."""


@dataclass(frozen=True)
class DeskTopology:
    mode: str
    engine_required: bool
    policy_required: bool
    refresh_required: bool
    worker_names: tuple[str, ...]


DESK_TOPOLOGY: Mapping[str, DeskTopology] = {
    "intraday": DeskTopology(
        mode="intraday",
        engine_required=True,
        policy_required=True,
        refresh_required=True,
        worker_names=("intraday_scanner", "intraday_coverage", "intraday_lifecycle"),
    ),
    "delivery": DeskTopology(
        mode="delivery",
        engine_required=True,
        policy_required=True,
        refresh_required=True,
        worker_names=("delivery_scanner", "delivery_coverage", "delivery_lifecycle"),
    ),
}

NON_EXECUTABLE_REFRESH_KEYS = frozenset({"all"})


def _normalise(values: Iterable[Any]) -> set[str]:
    return {str(value or "").strip().lower() for value in values if str(value or "").strip()}


def validate_production_topology(
    *,
    production_modes: Iterable[str],
    policy_modes: Iterable[str],
    engine_modes: Iterable[str],
    refresh_modes: Iterable[str],
    worker_names: Iterable[str],
) -> dict[str, Any]:
    """Validate all executable desk surfaces and return a serialisable report.

    The validator deliberately accepts plain collections so it can be used at
    startup without importing runtime modules here (avoids circular imports),
    and can be exercised independently in architecture fitness tests.
    """
    declared = _normalise(production_modes)
    policies = _normalise(policy_modes)
    engines = _normalise(engine_modes)
    refresh = _normalise(refresh_modes) - NON_EXECUTABLE_REFRESH_KEYS
    workers = {str(value or "").strip() for value in worker_names if str(value or "").strip()}
    expected = set(DESK_TOPOLOGY)

    errors: list[str] = []
    for label, actual in (
        ("production modes", declared),
        ("mode policies", policies),
        ("engine registry", engines),
        ("refresh registry", refresh),
    ):
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing:
            errors.append(f"{label} missing: {', '.join(missing)}")
        if extra:
            errors.append(f"{label} has unsupported executable modes: {', '.join(extra)}")

    expected_workers = {worker for desk in DESK_TOPOLOGY.values() for worker in desk.worker_names}
    missing_workers = sorted(expected_workers - workers)
    if missing_workers:
        errors.append(f"supervisor missing production workers: {', '.join(missing_workers)}")

    # A second Delivery analysis authority was a real regression. Any worker
    # containing both delivery/deep semantics beyond the canonical worker is a
    # release-stopping duplicate.
    delivery_authorities = sorted(
        worker for worker in workers
        if worker == "delivery_scanner" or ("delivery" in worker.lower() and "scan" in worker.lower())
    )
    if delivery_authorities != ["delivery_scanner"]:
        errors.append(
            "Delivery must have exactly one scan authority; found: "
            + (", ".join(delivery_authorities) or "none")
        )

    report = {
        "ok": not errors,
        "version": TOPOLOGY_VERSION,
        "desks": {name: asdict(spec) for name, spec in DESK_TOPOLOGY.items()},
        "declared_modes": sorted(declared),
        "policy_modes": sorted(policies),
        "engine_modes": sorted(engines),
        "refresh_modes": sorted(refresh),
        "production_workers": sorted(expected_workers & workers),
        "errors": errors,
    }
    if errors:
        raise ProductionTopologyError("; ".join(errors))
    return report
