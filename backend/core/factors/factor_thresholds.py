"""Lightweight factor-governance thresholds shared by runtime and research.

This module deliberately imports no scientific stack. Production startup may
consume governance thresholds without importing NumPy/Pandas research runners.
"""
from __future__ import annotations

DEFAULT_MIN_NAMES_PER_DATE = 5
DEFAULT_ALIVE_IC_THRESHOLD = 0.02
