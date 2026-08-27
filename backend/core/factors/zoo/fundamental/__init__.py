"""Fundamental alpha zoo — factors over `fund:*` panel columns.

These require point-in-time (PIT) fundamental data supplied by our own
FundamentalStore, keyed to NSE reporting periods (via
earnings_calendar_service.py), not the source repo's fund:* schema.
Formulas below are generic (ROE, gross profitability, asset growth,
earnings yield) and are family-agnostic; the panel-column plumbing that
feeds them is ours to build, not ported.
"""
