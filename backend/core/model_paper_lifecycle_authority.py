"""Compatibility export for the single canonical production lifecycle authority."""
from core.canonical_trade_lifecycle_authority import (
    CanonicalTradeLifecycleAuthority,
    DEFAULT_CANONICAL_TRADE_LIFECYCLE_AUTHORITY,
)

# Existing callers keep their import name, but no second lifecycle mathematics exists.
ModelPaperLifecycleAuthority = CanonicalTradeLifecycleAuthority
DEFAULT_MODEL_PAPER_LIFECYCLE_AUTHORITY = DEFAULT_CANONICAL_TRADE_LIFECYCLE_AUTHORITY
