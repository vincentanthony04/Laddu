"""Fail-closed production authority for factor-derived model inputs.

A factor is production-eligible only when four independent evidence families
agree:

* the local NSE IC/IR registry says the factor is alive and sufficiently strong;
* that IC/IR validation is fresh;
* predictive-decay evidence is healthy and fresh; and
* redundancy governance proves the factor is canonical rather than an alias.

The ML challenger may continue to calculate isolated Model Paper predictions
when this authority blocks production influence.  This service only controls
governed influence and therefore always fails closed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from typing import Any, Dict, Iterable, Mapping, Optional

from core.numeric_semantics import finite_number

from core.factors.factor_store import ensure_factor_tables, latest_decay_reports
from core.factors.factor_thresholds import DEFAULT_ALIVE_IC_THRESHOLD
from core.factor_dedup_service import load_static_manifest, static_redundancy_for


AUTHORITY_VERSION = "factor-authority-2.0.0"
DEFAULT_MIN_ABS_IR = 0.10


def _parse(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finite(value: Any) -> Optional[float]:
    return finite_number(value)


def _age_days(value: Any, now: datetime) -> float:
    measured = _parse(value)
    if measured is None:
        return float("inf")
    return (now - measured).total_seconds() / 86_400.0


def _row_dict(row: Any, columns: Iterable[str]) -> Dict[str, Any]:
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {name: row[index] for index, name in enumerate(columns)}


class FactorAuthorityService:
    def __init__(
        self,
        store: Any,
        *,
        max_age_days: int = 35,
        min_abs_ic: float = DEFAULT_ALIVE_IC_THRESHOLD,
        min_abs_ir: float = DEFAULT_MIN_ABS_IR,
        require_redundancy_evidence: bool = True,
    ):
        self.store = store
        self.max_age_days = int(max_age_days)
        self.min_abs_ic = float(min_abs_ic)
        self.min_abs_ir = float(min_abs_ir)
        self.require_redundancy_evidence = bool(require_redundancy_evidence)
        ensure_factor_tables(store.conn)

    def authorize(self, factor_names: Iterable[str]) -> Dict[str, Any]:
        names = sorted({str(name).strip() for name in factor_names if str(name).strip()})
        if not names:
            return self._blocked(
                "FACTOR_LINEAGE_MISSING",
                "Approved model does not declare its factor inputs",
                names,
                {},
            )

        latest_decay = {row["factor_name"]: row for row in latest_decay_reports(self.store.conn)}
        columns = (
            "factor_name", "family", "ic_score", "ir_score", "status", "last_validated",
            "redundancy_status", "canonical_factor_name", "redundancy_correlation",
            "dedup_version", "dedup_measured_at",
        )
        registry_rows = self.store.conn.execute(
            """SELECT factor_name,family,ic_score,ir_score,status,last_validated,
                      redundancy_status,canonical_factor_name,redundancy_correlation,
                      dedup_version,dedup_measured_at
               FROM factor_registry WHERE factor_name IN ({})""".format(
                ",".join("?" for _ in names)
            ),
            names,
        ).fetchall()
        registry = {
            str(item.get("factor_name")): item
            for item in (_row_dict(row, columns) for row in registry_rows)
        }

        manifest = load_static_manifest()
        static_canonical = set(manifest.get("canonical_factors") or [])
        manifest_version = manifest.get("version")
        now = datetime.now(timezone.utc)
        details: Dict[str, Dict[str, Any]] = {}
        blockers = []

        for name in names:
            factor_detail: Dict[str, Any] = {
                "eligible": False,
                "weight": 0.0,
                "registry": {},
                "decay": {},
                "redundancy": {},
            }
            details[name] = factor_detail
            row = registry.get(name)
            if not row:
                factor_detail.update(state="registry_missing", reason="no local NSE IC/IR registry row")
                blockers.append(f"{name}: no local NSE IC/IR registry row")
                continue

            registry_status = str(row.get("status") or "missing").lower()
            ic_score = _finite(row.get("ic_score"))
            ir_score = _finite(row.get("ir_score"))
            registry_age = _age_days(row.get("last_validated"), now)
            registry_fresh = 0.0 <= registry_age <= self.max_age_days
            status_eligible = registry_status == "alive"
            ic_eligible = ic_score is not None and ic_score >= self.min_abs_ic
            ir_eligible = ir_score is not None and ir_score >= self.min_abs_ir
            factor_detail["registry"] = {
                "status": registry_status,
                "ic_score": ic_score,
                "ir_score": ir_score,
                "last_validated": row.get("last_validated"),
                "age_days": round(registry_age, 2) if math.isfinite(registry_age) else None,
                "fresh": registry_fresh,
                "minimum_ic": self.min_abs_ic,
                "minimum_ir": self.min_abs_ir,
            }
            registry_reasons = []
            if not status_eligible:
                if registry_status == "reversed":
                    registry_reasons.append(
                        "reversed factor must be explicitly negated, versioned and revalidated"
                    )
                else:
                    registry_reasons.append(f"registry status is {registry_status}")
            if not registry_fresh:
                registry_reasons.append(
                    f"IC/IR validation stale ({registry_age:.1f}d)"
                    if math.isfinite(registry_age)
                    else "IC/IR validation timestamp missing"
                )
            if not ic_eligible:
                registry_reasons.append(
                    f"IC below {self.min_abs_ic:.4f}"
                    if ic_score is not None else "IC missing"
                )
            if not ir_eligible:
                registry_reasons.append(
                    f"IR below {self.min_abs_ir:.4f}"
                    if ir_score is not None else "IR missing"
                )
            if registry_reasons:
                factor_detail.update(state="ic_ir_blocked", reason="; ".join(registry_reasons))
                blockers.append(f"{name}: {factor_detail['reason']}")
                continue

            static_redundancy = static_redundancy_for(name)
            local_redundancy = str(row.get("redundancy_status") or "UNMEASURED").upper()
            if static_redundancy or local_redundancy == "REDUNDANT":
                canonical = (
                    (static_redundancy or {}).get("canonical")
                    or row.get("canonical_factor_name")
                )
                correlation = (static_redundancy or {}).get("correlation")
                if correlation is None:
                    correlation = row.get("redundancy_correlation")
                factor_detail["redundancy"] = {
                    "status": "REDUNDANT",
                    "canonical_factor_name": canonical,
                    "correlation": correlation,
                    "version": (static_redundancy or {}).get("version") or row.get("dedup_version"),
                }
                factor_detail.update(
                    state="redundant",
                    reason=f"redundant with {canonical or 'another factor'}",
                )
                blockers.append(f"{name}: {factor_detail['reason']}")
                continue

            static_is_canonical = name in static_canonical
            dedup_age = _age_days(row.get("dedup_measured_at"), now)
            live_canonical = (
                local_redundancy == "CANONICAL"
                and 0.0 <= dedup_age <= self.max_age_days
                and bool(row.get("dedup_version"))
            )
            redundancy_eligible = static_is_canonical or live_canonical
            factor_detail["redundancy"] = {
                "status": "CANONICAL" if redundancy_eligible else local_redundancy,
                "source": "static_manifest" if static_is_canonical else "local_audit",
                "version": manifest_version if static_is_canonical else row.get("dedup_version"),
                "measured_at": row.get("dedup_measured_at"),
                "age_days": round(dedup_age, 2) if math.isfinite(dedup_age) else None,
                "fresh": static_is_canonical or live_canonical,
            }
            if self.require_redundancy_evidence and not redundancy_eligible:
                factor_detail.update(
                    state="redundancy_unmeasured",
                    reason="fresh canonical/redundancy evidence missing",
                )
                blockers.append(f"{name}: {factor_detail['reason']}")
                continue

            decay = latest_decay.get(name)
            if not decay:
                factor_detail.update(state="decay_missing", reason="no predictive-decay report")
                blockers.append(f"{name}: no predictive-decay report")
                continue
            decay_age = _age_days(decay.get("measured_at"), now)
            decay_state = str(decay.get("status") or "missing").lower()
            decay_eligible = (
                decay_state == "healthy"
                and 0.0 <= decay_age <= self.max_age_days
            )
            decay_reason = (
                "healthy and fresh"
                if decay_eligible
                else (
                    f"stale ({decay_age:.1f}d)"
                    if decay_age > self.max_age_days
                    else decay_state
                )
            )
            factor_detail["decay"] = {
                "status": decay_state,
                "eligible": decay_eligible,
                "measured_at": decay.get("measured_at"),
                "age_days": round(decay_age, 2) if math.isfinite(decay_age) else None,
                "recent_ic": decay.get("recent_ic"),
                "baseline_ic": decay.get("baseline_ic"),
                "reason": decay_reason,
            }
            if not decay_eligible:
                factor_detail.update(state="decay_blocked", reason=decay_reason)
                blockers.append(f"{name}: {decay_reason}")
                continue

            factor_detail.update(
                state="authorized",
                eligible=True,
                weight=1.0,
                reason="fresh NSE IC/IR, canonical redundancy evidence and healthy decay",
            )

        if blockers:
            states = {detail.get("state") for detail in details.values()}
            if "redundant" in states or "redundancy_unmeasured" in states:
                state = "FACTOR_REDUNDANCY_BLOCKED"
            elif "ic_ir_blocked" in states or "registry_missing" in states:
                state = "FACTOR_IC_IR_BLOCKED"
            elif "decay_blocked" in states or "decay_missing" in states:
                state = "FACTOR_DECAY_BLOCKED"
            else:
                state = "FACTOR_AUTHORITY_INCOMPLETE"
            return self._blocked(state, "; ".join(blockers), names, details)

        return {
            "eligible": True,
            "state": "FACTORS_AUTHORIZED",
            "weight_multiplier": 1.0,
            "factor_names": names,
            "details": details,
            "blockers": [],
            "authority_version": AUTHORITY_VERSION,
            "policy": {
                "max_age_days": self.max_age_days,
                "minimum_ic": self.min_abs_ic,
                "minimum_ir": self.min_abs_ir,
                "require_redundancy_evidence": self.require_redundancy_evidence,
                "accepted_registry_status": "alive",
                "reversed_factor_policy": "explicitly negate, version and revalidate",
            },
        }

    def authorize_model(self, model_row: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            metadata = json.loads(model_row.get("metadata_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        return self.authorize(metadata.get("factor_names") or metadata.get("features") or [])

    def status(self) -> Dict[str, Any]:
        reports = latest_decay_reports(self.store.conn)
        rows = self.store.conn.execute(
            """SELECT status,COUNT(*) AS count FROM factor_registry
               GROUP BY status ORDER BY status"""
        ).fetchall()
        registry_counts = {}
        for row in rows:
            item = _row_dict(row, ("status", "count"))
            registry_counts[str(item.get("status"))] = int(item.get("count") or 0)
        return {
            "ok": True,
            "authority_version": AUTHORITY_VERSION,
            "max_age_days": self.max_age_days,
            "minimum_ic": self.min_abs_ic,
            "minimum_ir": self.min_abs_ir,
            "require_redundancy_evidence": self.require_redundancy_evidence,
            "registry_counts": registry_counts,
            "decay_counts": {
                state: sum(1 for row in reports if str(row.get("status")) == state)
                for state in ("healthy", "degraded", "insufficient_data")
            },
            "reports": reports,
        }

    @staticmethod
    def _blocked(state: str, reason: str, names, details):
        return {
            "eligible": False,
            "state": state,
            "reason": reason,
            "weight_multiplier": 0.0,
            "factor_names": names,
            "details": details,
            "blockers": [reason],
            "authority_version": AUTHORITY_VERSION,
        }
