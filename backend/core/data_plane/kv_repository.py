from __future__ import annotations

"""Operational key-value authority for production (PostgreSQL-backed).

storage.py's ``Store.set_kv``/``get_kv`` previously wrote directly to a local
SQLite ``kv`` table with no production delegate at all -- unlike every other
operational surface in ``Store``, which already injects a ``production_*``
repository when the production data plane is active. This repository closes
that gap using the same ``runtime_control`` schema already provisioned by
``ProductionDataPlane`` (see ``coordinator.py``'s required_schemas list).

Scope is deliberately narrow: this is config/state key-value storage used by
the scanner, dashboard, instrument bootstrap and operator controls -- not a
general cache, not time-series, not decision authority. Those already have
their own dedicated repositories.
"""

import json
from typing import Any

from .postgres import PostgresAuthority


class ProductionKVRepository:
    """Operational PostgreSQL persistence for scalar runtime key-value state."""

    production_authority = True

    def __init__(self, operational: PostgresAuthority, read_authority: PostgresAuthority | None = None):
        self.operational = operational
        # Foreground reads are isolated from scanner/backfill writers.  The
        # write authority remains the operational pool so this repository
        # cannot accidentally turn a read pool into another writer pool.
        self.read_authority = read_authority or operational

    def set_kv(self, key: str, value: Any) -> None:
        self.operational.execute(
            """
            INSERT INTO runtime_control.kv (k, v, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (k) DO UPDATE SET v = excluded.v, updated_at = now()
            """,
            (str(key), json.dumps(value)),
        )

    def delete_kv(self, key: str) -> None:
        self.operational.execute(
            "DELETE FROM runtime_control.kv WHERE k = %s",
            (str(key),),
        )

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self.read_authority.execute(
            "SELECT v FROM runtime_control.kv WHERE k = %s",
            (str(key),),
            fetch="one",
        )
        if not row:
            return default
        raw = row[0] if not hasattr(row, "get") else row.get("v")
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return default

    def get_kv_bounded(
        self,
        key: str,
        default: Any = None,
        *,
        statement_timeout_ms: int = 180,
        pool_timeout_seconds: float = 0.18,
    ) -> Any:
        """One indexed, fail-soft read-model lookup for interactive cold misses.

        This is intentionally *not* a general foreground database escape hatch.
        Callers may use it only for already-materialized KV projections.  The
        timeout covers both pool acquisition and the SQL statement so scanner or
        research pressure can never turn a cold browser request into an unbounded
        wait.
        """
        try:
            row = self.read_authority.execute(
                "SELECT v FROM runtime_control.kv WHERE k = %s",
                (str(key),),
                fetch="one",
                statement_timeout_ms=max(25, int(statement_timeout_ms)),
                pool_timeout_seconds=max(0.01, float(pool_timeout_seconds)),
            )
        except Exception:
            return default
        if not row:
            return default
        raw = row[0] if not hasattr(row, "get") else row.get("v")
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return default

    def get_prefix(self, prefix: str, limit: int = 10000) -> dict[str, Any]:
        """Read a bounded materialized-read-model prefix in one PostgreSQL query.

        This is a background hydration primitive. Customer HTTP routes must not
        call it directly; they read in-memory projections only.
        """
        token = str(prefix or "")
        if not token:
            return {}
        cap = max(1, min(20000, int(limit)))
        rows = self.read_authority.execute(
            "SELECT k, v FROM runtime_control.kv WHERE k LIKE %s ORDER BY k LIMIT %s",
            (token + "%", cap),
            fetch="all",
        ) or []
        out: dict[str, Any] = {}
        for row in rows:
            key = row[0] if not hasattr(row, "get") else row.get("k")
            raw = row[1] if not hasattr(row, "get") else row.get("v")
            try:
                out[str(key)] = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
        return out
