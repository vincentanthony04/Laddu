from __future__ import annotations

"""Project Laddu backend read-model purity contract.

A browser GET may project already-owned canonical state, but it may not become an
acquisition, scoring, lifecycle or trading authority.  Background producer lanes
remain free to materialize canonical projections from local authorities.
"""

AUTHORITY_NAME = "BackendReadModelPurityContract"
AUTHORITY_VERSION = "1.0.0"


class BackendReadModelPurityContract:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    HTTP_RULES = (
        "NO_SYNCHRONOUS_PROVIDER_IO",
        "NO_DECISION_OR_POSITION_MUTATION",
        "NO_INDEPENDENT_TRADING_MATHEMATICS",
        "LOCAL_CANONICAL_PROJECTION_ONLY",
    )
    PRODUCER_RULES = (
        "BACKGROUND_ONLY",
        "CAN_USE_CANONICAL_PURE_AUTHORITIES",
        "CAN_MATERIALIZE_REPLACEABLE_PROJECTIONS",
        "CANNOT_GRANT_DECISION_OR_CAPITAL_AUTHORITY",
    )

    def describe(self) -> dict:
        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "http_rules": list(self.HTTP_RULES),
            "producer_rules": list(self.PRODUCER_RULES),
        }


DEFAULT_BACKEND_READ_MODEL_PURITY_CONTRACT = BackendReadModelPurityContract()
