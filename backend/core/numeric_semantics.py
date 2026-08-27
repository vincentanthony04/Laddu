"""Single fail-closed numeric semantics for production mathematics.

Project Laddu treats absence/non-finite values as *missing evidence*, never as
zero or a valid market number.  Domain-specific authorities may impose tighter
bounds (positive price, non-negative volume, percentage range) on top of this
primitive, but they must not redefine NaN/inf/bool handling.
"""
from __future__ import annotations

import math
from typing import Any

AUTHORITY = "FiniteNumericSemantics"
AUTHORITY_VERSION = "1.0.0"


def finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def positive_number(value: Any) -> float | None:
    out = finite_number(value)
    return out if out is not None and out > 0 else None


def nonnegative_number(value: Any) -> float | None:
    out = finite_number(value)
    return out if out is not None and out >= 0 else None


def require_finite(name: str, value: Any, *, nonnegative: bool = False, positive: bool = False) -> float:
    out = finite_number(value)
    if out is None:
        raise ValueError(f"{name} must be finite")
    if positive and out <= 0:
        raise ValueError(f"{name} must be positive")
    if nonnegative and out < 0:
        raise ValueError(f"{name} must be non-negative")
    return out
