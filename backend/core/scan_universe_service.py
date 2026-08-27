from __future__ import annotations
from typing import Any, Callable, List

from core.production_mode_policy import normalise_mode, require_production_mode


class ScanUniverseService:
    """Resolves which symbols a mode is allowed to scan.

    Full tier: every clean NSE/BSE equity in the canonical instrument cache.
    Intraday and Delivery can both inspect this population cheaply; expensive
    MTF/model analysis is bounded later by their governed screening/shortlist
    policies. The historical curated tier is retained only as a cold-start
    fallback before the instrument authority is populated.
    """

    def __init__(self, fast_tier_symbols: List[str], all_eligible_equity_keys: Callable[[int], List[dict]]):
        self._fast_tier = list(dict.fromkeys(fast_tier_symbols))
        self._all_eligible_equity_keys = all_eligible_equity_keys

    def fast_tier(self) -> List[str]:
        return list(self._fast_tier)

    def full_tier(self, limit: int = 5000) -> List[str]:
        try:
            rows = self._all_eligible_equity_keys(limit) or []
        except Exception:
            rows = []
        symbols = [str(r.get("trading_symbol") or "").upper().strip() for r in rows]
        symbols = [s for s in symbols if s]
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            return self.fast_tier()
        return symbols

    def universe_for_mode(self, mode: str, limit: int = 5000) -> List[str]:
        raw = str(mode or "").lower().strip()
        if raw == "all":
            return self.full_tier(limit)
        require_production_mode(normalise_mode(raw))
        return self.full_tier(limit)
