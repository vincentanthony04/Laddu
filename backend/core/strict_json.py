"""Strict JSON semantics for persisted/governed payloads.

Python's stdlib encoder emits NaN/Infinity by default; PostgreSQL json/jsonb
rejects those tokens. Governed persistence therefore normalises every
non-finite real number to JSON null before encoding.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from numbers import Real
from typing import Any


def json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, Real) and not isinstance(value, bool):
        try:
            if not math.isfinite(float(value)):
                return None
        except (TypeError, ValueError, OverflowError):
            pass
    return value


def strict_json_dumps(value: Any, **kwargs: Any) -> str:
    options = dict(kwargs)
    options["allow_nan"] = False
    options.setdefault("default", str)
    return json.dumps(json_safe(value), **options)
