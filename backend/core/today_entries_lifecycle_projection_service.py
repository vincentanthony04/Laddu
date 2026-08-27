"""Canonical Today Entries lifecycle attribution projection.

The service joins a published decision to Model Paper only by its immutable
source signal ID.  It never infers thesis state from symbol, price, chart or
browser state.  Missing lifecycle evidence stays explicit.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from core.signal_age_authority import DEFAULT_SIGNAL_AGE_AUTHORITY


class TodayEntriesLifecycleProjectionService:
    authority = "TodayEntriesLifecycleProjectionService"
    authority_version = "1.0.0-ac072"
    lifecycle_authority = "POSTGRESQL_MODEL_PAPER_AND_SIGNAL_LIFECYCLE"

    def __init__(self, repository: Any | None):
        self.repository = repository

    @staticmethod
    def _signal_id(row: Mapping[str, Any]) -> str:
        # ModelPortfolioService admission is keyed by signal_id/source_signal_id.
        # Do not substitute symbol/time or a different decision ID.
        return str(row.get("signal_id") or row.get("source_signal_id") or "").strip()

    @staticmethod
    def _reason(values: Any) -> str | None:
        if not isinstance(values, (list, tuple)):
            return None
        cleaned = [str(value).strip() for value in values if str(value).strip()]
        return " · ".join(cleaned[:3]) or None

    def _project_one(
        self,
        row: Mapping[str, Any],
        attribution: Mapping[str, Any] | None,
        *,
        query_state: str,
    ) -> dict[str, Any]:
        out = dict(row or {})
        signal_id = self._signal_id(out)
        attr = dict(attribution or {})
        opened_at = attr.get("opened_at")
        at = attr.get("closed_at") or attr.get("updated_at") or out.get("updated_at") or out.get("created_at")
        age = DEFAULT_SIGNAL_AGE_AUTHORITY.measure(
            generated_at=out.get("generated_at") or out.get("decision_generated_at") or out.get("created_at"),
            opened_at=opened_at,
            at=at,
            mode=out.get("mode"),
            approved_policy=(out.get("approved_age_risk_policy") if isinstance(out.get("approved_age_risk_policy"), Mapping) else None),
        )
        if query_state != "READY":
            lifecycle_state = "UNAVAILABLE"
        elif not signal_id:
            lifecycle_state = "UNLINKED_DECISION"
        elif not attr:
            lifecycle_state = "NOT_YET_OPENED"
        elif attr.get("latest_reassessment_at"):
            lifecycle_state = "CURRENT"
        else:
            lifecycle_state = "POSITION_OPEN_NOT_REASSESSED"
        thesis = str(attr.get("current_thesis_state") or "").upper().strip() or (
            "NOT_REASSESSED" if attr else "NOT_OPENED"
        )
        latest_action = attr.get("latest_management_action") or attr.get("position_action")
        latest_reason = self._reason(attr.get("management_reasons")) or self._reason(attr.get("reassessment_reasons"))
        out.update({
            "model_paper_position_id": attr.get("position_id"),
            "model_paper_status": attr.get("status"),
            "current_thesis_state": thesis,
            "latest_reassessment_at": attr.get("latest_reassessment_at"),
            "latest_reassessment_reason": self._reason(attr.get("reassessment_reasons")),
            "reassessment_validation_scope": attr.get("reassessment_validation_scope"),
            "latest_management_action": latest_action,
            "latest_management_reason": latest_reason,
            "latest_management_at": attr.get("latest_management_at"),
            "latest_management_hit_status": attr.get("latest_management_hit_status"),
            "lifecycle_attribution_state": lifecycle_state,
            "lifecycle_attribution_authority": self.lifecycle_authority,
            "lifecycle_projection_authority_version": self.authority_version,
            "signal_age": age,
            "generation_age_seconds": age.get("generation_age_seconds"),
            "open_age_seconds": age.get("open_age_seconds"),
            "decision_delay_seconds": age.get("decision_delay_seconds"),
            "generation_age_bucket": age.get("generation_age_bucket"),
            "open_age_bucket": age.get("open_age_bucket"),
            "decision_delay_bucket": age.get("decision_delay_bucket"),
            "age_attribution_state": age.get("age_attribution_state"),
            "age_bucket_policy_version": age.get("age_bucket_policy_version"),
        })
        return out

    def project(self, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        source = [dict(row or {}) for row in (rows or ())]
        signal_ids = [self._signal_id(row) for row in source if self._signal_id(row)]
        if self.repository is None or not callable(getattr(self.repository, "current_lifecycle_attribution", None)):
            return [self._project_one(row, None, query_state="UNAVAILABLE") for row in source]
        try:
            attribution = self.repository.current_lifecycle_attribution(signal_ids)
        except Exception:
            return [self._project_one(row, None, query_state="UNAVAILABLE") for row in source]
        return [self._project_one(row, attribution.get(self._signal_id(row)), query_state="READY") for row in source]


DEFAULT_TODAY_ENTRIES_LIFECYCLE_PROJECTION = TodayEntriesLifecycleProjectionService
