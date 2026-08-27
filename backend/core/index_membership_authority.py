"""Point-in-time index-membership authority.

Official PostgreSQL history is the governed decision source.  Legacy static
constituent lists are retained only as an explicit continuity fallback; they
may support diagnostics/UI identity but can never make breadth decision-usable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import os
from typing import Any, Callable, Iterable, Mapping, Sequence

AUTHORITY_NAME = "IndexMembershipAuthority"
AUTHORITY_VERSION = "1.0.0"


def _day(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)[:10]


@dataclass(frozen=True)
class IndexMembershipSnapshot:
    index_name: str
    as_of: str
    membership_date: str | None
    symbols: tuple[str, ...]
    source: str
    state: str
    decision_usable: bool
    content_hashes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "authority": AUTHORITY_NAME,
            "authority_version": AUTHORITY_VERSION,
            "index_name": self.index_name,
            "as_of": self.as_of,
            "membership_date": self.membership_date,
            "symbols": list(self.symbols),
            "eligible_population": len(self.symbols),
            "source": self.source,
            "state": self.state,
            "decision_usable": self.decision_usable,
            "content_hashes": list(self.content_hashes),
        }


class IndexMembershipAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    def __init__(self, dsn: str | None = None, *, loader: Callable[[str, str], Sequence[Mapping[str, Any]]] | None = None):
        self.dsn = (dsn if dsn is not None else os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", "")).strip()
        self.loader = loader

    def _load_postgres(self, index_name: str, as_of: str) -> list[dict[str, Any]]:
        if self.loader is not None:
            return [dict(row) for row in self.loader(index_name, as_of)]
        if not self.dsn:
            return []
        import psycopg
        sql = """
        WITH chosen AS (
          SELECT max(trade_date) AS trade_date
          FROM reference.nse_index_membership_history
          WHERE upper(index_name)=upper(%s) AND trade_date <= %s::date
        )
        SELECT trade_date,index_name,symbol,isin,index_weight,sector_name,content_hash
        FROM reference.nse_index_membership_history
        WHERE upper(index_name)=upper(%s)
          AND trade_date=(SELECT trade_date FROM chosen)
        ORDER BY symbol
        """
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (index_name, as_of, index_name))
                columns = [item.name for item in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def snapshot(self, index_name: str, as_of: date | str, *, fallback_symbols: Iterable[str] = ()) -> IndexMembershipSnapshot:
        as_of_text = _day(as_of)
        rows = self._load_postgres(str(index_name), as_of_text)
        if rows:
            membership_date = str(rows[0].get("trade_date") or "")[:10] or None
            symbols = tuple(sorted({str(row.get("symbol") or "").upper().strip() for row in rows if row.get("symbol")}))
            hashes = tuple(sorted({str(row.get("content_hash") or "") for row in rows if row.get("content_hash")}))
            return IndexMembershipSnapshot(
                index_name=str(index_name), as_of=as_of_text, membership_date=membership_date,
                symbols=symbols, source="POSTGRESQL_OFFICIAL_NSE_MEMBERSHIP_HISTORY",
                state="READY", decision_usable=bool(symbols), content_hashes=hashes,
            )
        fallback = tuple(sorted({str(symbol).upper().strip() for symbol in fallback_symbols if str(symbol).strip()}))
        if fallback:
            return IndexMembershipSnapshot(
                index_name=str(index_name), as_of=as_of_text, membership_date=None,
                symbols=fallback, source="LEGACY_STATIC_FALLBACK", state="FALLBACK",
                decision_usable=False,
            )
        return IndexMembershipSnapshot(
            index_name=str(index_name), as_of=as_of_text, membership_date=None,
            symbols=(), source="NONE", state="UNAVAILABLE", decision_usable=False,
        )
