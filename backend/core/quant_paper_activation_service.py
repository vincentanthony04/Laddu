"""Composition root for the production prediction and automatic paper authority.

Implementation is decomposed by lifecycle, scoring, portfolio and read-model
responsibility. This module preserves the stable import contract only.
"""
from __future__ import annotations

from core.quant_paper_dependencies import *  # noqa: F401,F403
from core.quant_paper_model_lifecycle import QuantPaperModelLifecycleMixin
from core.quant_paper_scoring import QuantPaperScoringMixin
from core.quant_paper_portfolio import QuantPaperPortfolioMixin
from core.quant_paper_read_model import QuantPaperReadModelMixin


class QuantPaperActivationService(
    QuantPaperModelLifecycleMixin,
    QuantPaperScoringMixin,
    QuantPaperPortfolioMixin,
    QuantPaperReadModelMixin,
):
    def __init__(self, store: Any):
        self.store = store
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.RLock()
        if not hasattr(store, "_quant_paper_artifact_cache"):
            store._quant_paper_artifact_cache = {}
        self._artifact_cache = store._quant_paper_artifact_cache
        self.costs = IndiaCashCostService()
        self.risk = ModelPortfolioRiskService(
            equity=INITIAL_CAPITAL,
            intraday_cap=INTRADAY_CAPITAL,
            cost_service=self.costs,
        )
        self._ensure_schema()
