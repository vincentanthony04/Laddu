"""Candidate discovery funnel composition, extracted from LadduRuntime."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, Iterable, List

from core.production_mode_policy import is_production_mode, normalise_mode


class CandidateDiscoveryService:
    def __init__(self, store: Any, status: Dict[str, Any], *, universe_size: int,
                 compact_project: Callable[[Dict[str, Any]], Dict[str, Any]],
                 group_rows: Callable[[Iterable[Dict[str, Any]]], List[Dict[str, Any]]]):
        self.store = store
        self.status = status
        self.universe_size = int(universe_size)
        self.compact_project = compact_project
        self.group_rows = group_rows


    @staticmethod
    def _snapshot_stamp(rows: List[Dict[str, Any]]) -> str:
        values = []
        for row in rows:
            for key in ("observed_at", "decision_as_of", "last_ai_validation", "last_refresh", "last_update", "created_at"):
                value = str(row.get(key) or "").strip()
                if value:
                    values.append(value)
                    break
        return max(values) if values else "1970-01-01T00:00:00Z"

    @staticmethod
    def _dataset_fingerprint(rows: List[Dict[str, Any]]) -> str:
        # Fingerprint the complete captured rows. A selective field list can
        # silently treat materially different feature sets as the same dataset.
        material = [
            dict(row or {})
            for row in sorted(
                rows,
                key=lambda item: (
                    str(item.get("mode") or ""),
                    str(item.get("symbol") or ""),
                    str(item.get("decision_as_of") or item.get("observed_at") or ""),
                ),
            )
        ]
        canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _shadow_selection(self, rows: List[Dict[str, Any]], mode: str) -> Dict[str, Any]:
        if not hasattr(self.store, "conn"):
            return {"state": "UNAVAILABLE", "reason": "store has no database connection"}
        try:
            from core.selection_platform_service import SelectionPlatformService
            service = SelectionPlatformService(self.store)
            requested = ("intraday", "delivery") if mode == "all" else (mode,)
            desks = {}
            for desk in requested:
                result = service.latest_summary(desk)
                arms = result.get("arms") or {}
                desks[desk] = {
                    "state": result.get("state") or "NO_POPULATION_RECORDED",
                    "population_fingerprint": result.get("population_fingerprint"),
                    "candidate_count": max((len(items or []) for items in arms.values()), default=0),
                    "prediction_count": sum(len(items or []) for items in arms.values()),
                    "prediction_state": result.get("prediction_state", "MODEL_UNAVAILABLE"),
                    "decision_weight": result.get("decision_weight", 0.0),
                    "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
                    "arms": {name: list(items or [])[:10] for name, items in arms.items()},
                }
            return {
                "state": ("SHADOW_ACTIVE" if any(v.get("state") != "NO_POPULATION_RECORDED" for v in desks.values()) else "NO_POPULATION_RECORDED"),
                "desks": desks,
                "product_mode": "AUTOMATIC_PAPER_ONLY",
                "prediction_state": "MODEL_UNAVAILABLE",
                "decision_weight": 0.0,
                "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
            }
        except Exception as exc:
            return {
                "state": "BLOCKED", "reason": str(exc)[:240],
                "prediction_state": "MODEL_UNAVAILABLE",
                "decision_weight": 0.0,
            }

    def build(self, rows: List[Dict[str, Any]], mode: str = "all", coverage: Dict[str, Any] | None = None, fairness: Dict[str, Any] | None = None) -> Dict[str, Any]:
        requested_mode = normalise_mode(mode)
        if requested_mode not in {"all", "intraday", "delivery"}:
            requested_mode = "all"
        rows = [dict(d, mode=normalise_mode(d.get("mode"))) for d in rows if is_production_mode(d.get("mode"))]
        mode = requested_mode
        by_stage: Dict[str, int] = {}
        by_sector: Dict[str, int] = {}
        by_theme: Dict[str, int] = {}
        blockers: Dict[str, int] = {}
        institutional_stages: Dict[str, int] = {}
        trade_map_valid = final_ready = 0
        near_qualified: List[Dict[str, Any]] = []
        for d in rows:
            candidate_stage = str(d.get("candidate_stage") or "WATCH")
            by_stage[candidate_stage] = by_stage.get(candidate_stage, 0) + 1
            sector = str(d.get("sector") or "broad")
            by_sector[sector] = by_sector.get(sector, 0) + 1
            for theme in d.get("themes") or []:
                by_theme[str(theme)] = by_theme.get(str(theme), 0) + 1
            if bool(d.get("trade_map_valid")) or str(d.get("level_status") or "").lower() == "valid":
                trade_map_valid += 1
            if str(d.get("rank_readiness") or "").upper() == "READY":
                final_ready += 1
            inst_stage = str(d.get("institutional_stage") or "Unclassified")
            institutional_stages[inst_stage] = institutional_stages.get(inst_stage, 0) + 1
            conflicts = d.get("rank_conflicts") or d.get("promotion_blocked_by") or []
            if isinstance(conflicts, str):
                try: conflicts = json.loads(conflicts)
                except Exception: conflicts = [conflicts]
            conflicts = [str(x) for x in conflicts if x]
            for conflict in conflicts:
                blockers[conflict] = blockers.get(conflict, 0) + 1
            if len(conflicts) <= 1 and str(d.get("rank_readiness") or "").upper() in ("WATCH", "EXTENDED"):
                near_qualified.append(dict(d, qualification_blocker=conflicts[0] if conflicts else "score/readiness threshold"))

        potential = self.store.opportunity_candidates(mode, limit=40)
        scanners = self.status.get("mode_scanners") or {}
        scan_modes = ("intraday",) if mode == "intraday" else ("delivery",) if mode == "delivery" else ("intraday", "delivery")
        scanned = sum(int((scanners.get(m) or {}).get("scanned") or 0) for m in scan_modes)
        compact = [self.compact_project(d) for d in rows]
        shadow_selection = self._shadow_selection(rows, mode)
        funnel_rows = {
            "patterns": compact,
            "watch": [self.compact_project(d) for d in rows if str(d.get("candidate_stage") or d.get("rank_readiness") or "").upper() == "WATCH"],
            "valid_map": [self.compact_project(d) for d in rows if bool(d.get("trade_map_valid")) or str(d.get("level_status") or "").lower() == "valid"],
            "final": [self.compact_project(d) for d in rows if str(d.get("rank_readiness") or "").upper() == "READY"],
        }
        return {
            "state": "active", "mode": mode, "coverage": coverage or {}, "fairness": fairness or {"state":"INSUFFICIENT_DATA"}, "research_count": len(rows),
            "potential_count": len(potential), "armed_count": by_stage.get("ARMED", 0),
            "qualified_count": by_stage.get("QUALIFIED", 0), "watch_count": by_stage.get("WATCH", 0),
            "by_stage": by_stage,
            "by_sector": dict(sorted(by_sector.items(), key=lambda kv: (-kv[1], kv[0]))[:12]),
            "by_theme": dict(sorted(by_theme.items(), key=lambda kv: (-kv[1], kv[0]))[:12]),
            "institutional_stages": dict(sorted(institutional_stages.items(), key=lambda kv: (-kv[1], kv[0]))),
            "top_blockers": dict(sorted(blockers.items(), key=lambda kv: (-kv[1], kv[0]))[:5]),
            "selection_funnel": {"universe": self.universe_size, "scanned_latest_cycles": scanned,
                                 "patterns_found": len(rows), "watch": by_stage.get("WATCH", 0),
                                 "trade_map_valid": trade_map_valid, "final_ready": final_ready},
            "near_qualified": [self.compact_project(d) for d in sorted(near_qualified, key=lambda x: int(x.get("rank_score") or x.get("score") or 0), reverse=True)[:5]],
            "funnel_rows": funnel_rows,
            "top": compact[:6],
            "potential_top": [self.compact_project(d) for d in self.group_rows(potential)[:6]],
            "shadow_selection": shadow_selection,
            # Compatibility alias for consumers introduced during the interrupted
            # v67 edit. This remains research metadata and has no capital authority.
            "active_prediction": shadow_selection,
            "message": "Discovery/Potential are research cases. Selected Candidates stays strict and may be zero.",
        }
