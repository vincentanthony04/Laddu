"""
Request validation/schema layer for Project Laddu's hand-rolled HTTP routes.

v59 (HTTP layer hardening): previously every route handler did its own
ad-hoc checks (`if not str(data.get("symbol") or "").strip(): return
({"error": ...}, 400)`), inconsistently, and most routes did none at all --
a route could crash on `int(data.get("id"))` with a TypeError instead of
returning a clean 400, or silently accept a wrong-typed field into an
sqlite write.

This module gives routes_get.py / routes_post.py a declarative schema
they can attach to a handler with a decorator, no new dependency (no
pydantic) since this ships as a Windows background service with no
guaranteed internet access at install time -- stdlib only.

Usage:
    from validation import Field, validate

    @validate(symbol=Field(str, required=True, strip=True, min_len=1),
              mode=Field(str, default="intraday", choices=MODES))
    def r_search(app, data):
        ...

Inside the handler, `data` is replaced with a *cleaned* dict: required
fields are guaranteed present, defaults are filled in, types are coerced
(or the request never reaches the handler), and strings are stripped.
"""
from __future__ import annotations
from dataclasses import dataclass, field as _dc_field
from typing import Any, Callable, Dict, Optional, Tuple, Type


class ValidationError(ValueError):
    def __init__(self, errors: Dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))


@dataclass
class Field:
    type: Type = str
    required: bool = False
    default: Any = None
    strip: bool = False           # strings: .strip()
    min_len: Optional[int] = None  # strings/lists
    max_len: Optional[int] = None  # strings/lists
    min_val: Optional[float] = None  # numbers
    max_val: Optional[float] = None  # numbers
    choices: Optional[tuple] = None
    upper: bool = False
    lower: bool = False

    def coerce(self, name: str, raw: Any) -> Any:
        if raw is None:
            if self.required:
                raise ValidationError({name: "required"})
            return self.default() if callable(self.default) else self.default

        if self.type is str:
            val = str(raw)
            if self.strip:
                val = val.strip()
            if self.upper:
                val = val.upper()
            if self.lower:
                val = val.lower()
            if self.required and not val:
                raise ValidationError({name: "required"})
            if self.min_len is not None and len(val) < self.min_len:
                raise ValidationError({name: f"must be at least {self.min_len} chars"})
            if self.max_len is not None and len(val) > self.max_len:
                raise ValidationError({name: f"must be at most {self.max_len} chars"})
            if self.choices is not None and val not in self.choices:
                raise ValidationError({name: f"must be one of {self.choices}"})
            return val

        if self.type is bool:
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")

        if self.type in (int, float):
            try:
                val = self.type(raw)
            except (TypeError, ValueError):
                raise ValidationError({name: f"must be a {self.type.__name__}"})
            if self.min_val is not None and val < self.min_val:
                raise ValidationError({name: f"must be >= {self.min_val}"})
            if self.max_val is not None and val > self.max_val:
                raise ValidationError({name: f"must be <= {self.max_val}"})
            if self.choices is not None and val not in self.choices:
                raise ValidationError({name: f"must be one of {self.choices}"})
            return val

        if self.type is dict:
            if not isinstance(raw, dict):
                raise ValidationError({name: "must be an object"})
            return raw

        if self.type is list:
            if not isinstance(raw, list):
                raise ValidationError({name: "must be an array"})
            if self.min_len is not None and len(raw) < self.min_len:
                raise ValidationError({name: f"must have at least {self.min_len} items"})
            if self.max_len is not None and len(raw) > self.max_len:
                raise ValidationError({name: f"must have at most {self.max_len} items"})
            return raw

        # Fallback: no coercion, pass through as-is.
        return raw


def validate(**schema: Field) -> Callable:
    """Decorator for POST route handlers of shape (app, data: dict) -> Any.

    On failure, short-circuits with a (body, 400) tuple in the same shape
    routes already return on their own validation errors, so Handler's
    do_POST needs zero changes.
    """
    def decorator(fn: Callable) -> Callable:
        def wrapped(app, data: dict, *args, **kwargs):
            if not isinstance(data, dict):
                return ({"ok": False, "error": "request body must be a JSON object"}, 400)
            cleaned: Dict[str, Any] = {}
            errors: Dict[str, str] = {}
            for name, spec in schema.items():
                try:
                    cleaned[name] = spec.coerce(name, data.get(name))
                except ValidationError as exc:
                    errors.update(exc.errors)
            if errors:
                return ({"ok": False, "error": "validation failed", "fields": errors}, 400)
            # Pass through any extra keys the schema didn't declare, so
            # handlers that read data.get("something_undeclared") still work
            # during the migration -- schema coverage can be tightened route
            # by route without a flag day.
            for k, v in data.items():
                cleaned.setdefault(k, v)
            return fn(app, cleaned, *args, **kwargs)
        wrapped.__name__ = getattr(fn, "__name__", "wrapped")
        wrapped.__wrapped__ = fn
        return wrapped
    return decorator


def validate_query(**schema: Field) -> Callable:
    """Same idea for GET route handlers of shape (app, qs, q, mode).

    qs is the raw dict[str, list[str]] from parse_qs; each declared field is
    read as qs.get(name, [None])[0] and coerced the same way as POST bodies.
    Unlike POST body validation this never blocks the request (GET routes
    already default missing/bad query params rather than erroring, matching
    existing behavior) -- it just returns the cleaned dict for handlers that
    opt in to reading `clean` instead of re-parsing `qs` themselves.
    """
    def decorator(fn: Callable) -> Callable:
        def wrapped(app, qs, q, mode, *args, **kwargs):
            cleaned: Dict[str, Any] = {}
            for name, spec in schema.items():
                raw_list = qs.get(name)
                raw = raw_list[0] if raw_list else None
                try:
                    cleaned[name] = spec.coerce(name, raw)
                except ValidationError:
                    cleaned[name] = spec.default() if callable(spec.default) else spec.default
            return fn(app, qs, q, mode, clean=cleaned, *args, **kwargs)
        wrapped.__name__ = getattr(fn, "__name__", "wrapped")
        wrapped.__wrapped__ = fn
        return wrapped
    return decorator
