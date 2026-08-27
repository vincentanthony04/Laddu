"""Evidence-based market-cycle and sector-rotation maturity authority.

This service reports what the installed product can *prove*.  It does not turn
ordinary direction scores into a self-awarded institutional maturity rating and
it never changes a candidate score, decision, paper position or model weight.
"""
from __future__ import annotations

from typing import Any, Dict


SERVICE_VERSION = "market-cycle-sector-rotation-maturity-2.0.0-regime-change-authority"


class MarketCycleMaturityService:
    LEVELS = {
        0: "DATA_FOUNDATION_INCOMPLETE",
        1: "DESCRIPTIVE_CONTEXT",
        2: "EXPLICIT_REGIME_CLASSIFICATION",
        3: "REGIME_BACKTEST_VALIDATED",
        4: "FORWARD_PAPER_VALIDATED",
    }

    def __init__(self, app: Any):
        self.app = app
        self.store = getattr(app, "store", None)

    def _count(self, sql: str, params=()) -> int:
        try:
            row = self.store.conn.execute(sql, tuple(params)).fetchone()
            return int(row[0] if row else 0)
        except Exception:
            return 0

    def _distinct(self, sql: str, params=()) -> int:
        return self._count(sql, params)

    def _context(self) -> Dict[str, int]:
        try:
            rows = list(self.app.heatmap_snapshot() or [])
        except Exception:
            rows = []
        usable = [
            row for row in rows
            if isinstance(row, dict)
            and (row.get("ltp") is not None or row.get("change_pct") is not None)
        ]
        sectors = [
            row for row in usable
            if str(row.get("name") or "").upper() not in {"NIFTY", "SENSEX", "BANK"}
        ]
        scored = [
            row for row in sectors
            if row.get("trend_score") is not None or row.get("momentum_score") is not None
        ]
        return {
            "configured": len(rows),
            "usable": len(usable),
            "sectors": len(sectors),
            "scored_sectors": len(scored),
        }

    def status(self) -> Dict[str, Any]:
        context = self._context()
        breadth_rows = self._count("SELECT COUNT(*) FROM market_breadth_daily")
        explicit_regime_observations = self._count(
            """SELECT COUNT(*) FROM market_regime_observations
               WHERE confirmed_regime IS NOT NULL AND TRIM(confirmed_regime)<>''
                 AND UPPER(confirmed_regime)<>'UNKNOWN'"""
        )
        explicit_regimes = self._distinct(
            """SELECT COUNT(DISTINCT UPPER(confirmed_regime)) FROM market_regime_observations
               WHERE confirmed_regime IS NOT NULL AND TRIM(confirmed_regime)<>''
                 AND UPPER(confirmed_regime)<>'UNKNOWN'"""
        )
        legacy_regime_snapshots = self._count(
            """SELECT COUNT(*) FROM quant_feature_snapshots
               WHERE regime_tag IS NOT NULL AND TRIM(regime_tag)<>''
                 AND UPPER(regime_tag)<>'UNKNOWN'"""
        )
        legacy_snapshot_regimes = self._distinct(
            """SELECT COUNT(DISTINCT UPPER(regime_tag)) FROM quant_feature_snapshots
               WHERE regime_tag IS NOT NULL AND TRIM(regime_tag)<>''
                 AND UPPER(regime_tag)<>'UNKNOWN'"""
        )
        regime_snapshots = max(explicit_regime_observations, legacy_regime_snapshots)
        snapshot_regimes = max(explicit_regimes, legacy_snapshot_regimes)
        labelled_regimes = self._distinct(
            """SELECT COUNT(DISTINCT UPPER(market_regime)) FROM quant_label_vectors
               WHERE market_regime IS NOT NULL AND TRIM(market_regime)<>''
                 AND UPPER(market_regime)<>'UNKNOWN'"""
        )
        rotation_observations = max(
            self._count("""SELECT COUNT(*) FROM market_regime_observations
                           WHERE UPPER(COALESCE(confirmed_regime,''))='SECTOR_ROTATION'"""),
            self._count("""SELECT COUNT(*) FROM quant_feature_snapshots
                           WHERE UPPER(COALESCE(regime_tag,''))='SECTOR_ROTATION'"""),
        )
        confirmed_regime_changes = self._count(
            "SELECT COUNT(*) FROM market_regime_observations WHERE transition_state='CONFIRMED_CHANGE'"
        )
        active_models = self._count(
            "SELECT COUNT(*) FROM model_experiments WHERE lifecycle_state='ACTIVE_PRODUCTION'"
        )
        settled_learning = self._count("SELECT COUNT(*) FROM position_learning_ledger")

        foundation_ready = context["usable"] >= 12 and breadth_rows > 0
        explicit_regime_ready = (
            foundation_ready
            and max(snapshot_regimes, labelled_regimes) >= 3
            and regime_snapshots >= 300
        )
        backtest_ready = explicit_regime_ready and active_models > 0
        forward_ready = backtest_ready and settled_learning >= 100

        level = 0
        if foundation_ready:
            level = 1
        if explicit_regime_ready:
            level = 2
        if backtest_ready:
            level = 3
        if forward_ready:
            level = 4

        missing_cycle = []
        if not foundation_ready:
            missing_cycle.append("complete verified market/sector context and breadth history")
        if max(snapshot_regimes, labelled_regimes) < 3:
            missing_cycle.append("at least three explicit observed regimes")
        if regime_snapshots < 300:
            missing_cycle.append("at least 300 point-in-time regime observations")
        if active_models < 1:
            missing_cycle.append("regime-conditioned model/backtest promotion")
        if settled_learning < 100:
            missing_cycle.append("at least 100 settled forward-paper outcomes")

        rotation_descriptive = context["sectors"] >= 8 and context["scored_sectors"] >= 8
        rotation_classifier = rotation_observations >= 300 and explicit_regime_ready
        rotation_backtest = rotation_classifier and active_models > 0
        rotation_forward = rotation_backtest and settled_learning >= 100
        rotation_level = 0
        if rotation_descriptive:
            rotation_level = 1
        if rotation_classifier:
            rotation_level = 2
        if rotation_backtest:
            rotation_level = 3
        if rotation_forward:
            rotation_level = 4

        return {
            "ok": True,
            "version": SERVICE_VERSION,
            "maturity_level": level,
            "maturity_max": 4,
            "maturity_state": self.LEVELS[level],
            "market_cycle": {
                "level": level,
                "state": self.LEVELS[level],
                "context": context,
                "breadth_observations": breadth_rows,
                "regime_observations": regime_snapshots,
                "observed_regimes": max(snapshot_regimes, labelled_regimes),
                "confirmed_regime_changes": confirmed_regime_changes,
                "regime_authority": "market_regime_observations" if explicit_regime_observations else "legacy_feature_labels",
                "backtest_promoted_models": active_models,
                "settled_forward_records": settled_learning,
                "missing_gates": missing_cycle,
            },
            "sector_rotation": {
                "level": rotation_level,
                "state": self.LEVELS[rotation_level],
                "usable_sector_rows": context["sectors"],
                "scored_sector_rows": context["scored_sectors"],
                "explicit_rotation_observations": rotation_observations,
                "backtest_promoted_models": active_models if rotation_backtest else 0,
                "settled_forward_records": settled_learning if rotation_forward else 0,
                "missing_gates": [
                    item for item, passed in (
                        ("multi-horizon relative-strength and breadth persistence", rotation_descriptive),
                        ("explicit sector-rotation classifier with 300+ point-in-time observations", rotation_classifier),
                        ("purged regime-conditioned backtest approval", rotation_backtest),
                        ("100+ settled forward-paper outcomes", rotation_forward),
                    ) if not passed
                ],
            },
            "decision_boundary": {
                "heuristic_market_context_may_contribute": True,
                "maximum_declared_context_weight_pct": 10,
                "maturity_status_production_influence": False,
                "ml_production_influence": active_models > 0,
                "broker_authority": "NONE",
            },
            "policy": (
                "Level 1 is descriptive context only. Level 2 requires explicit point-in-time regime labels; "
                "Level 3 requires purged regime-conditioned backtest promotion; Level 4 requires forward-paper proof."
            ),
        }
