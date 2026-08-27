from __future__ import annotations

"""Authoritative focused instrument catalogue in operational PostgreSQL.

PostgreSQL owns the accepted universe revision. The existing SQLite instrument
store is retained only as a local RAM/search projection so legacy search code
cannot put bulk or live-trade writes on the operational authority.
"""

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .postgres import PostgresAuthority


_ALLOWED_NSE = {"EQ", "BE", "SM", "ST", "BZ"}
_ALLOWED_BSE = {"A", "B", "X", "XT", "T", "M", "MT", "TS", "MS", "Z", "ZP"}
_INDEX_SEGMENTS = {"NSE_INDEX", "BSE_INDEX"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def canonical_instrument_row(row: Mapping[str, Any], revision: str) -> dict[str, Any]:
    segment = _text(row.get("segment")).upper()
    series = _text(row.get("instrument_type")).upper()
    exchange = _text(row.get("exchange")).upper()
    if not exchange:
        exchange = "NSE" if segment.startswith("NSE") else "BSE" if segment.startswith("BSE") else ""
    if exchange not in {"NSE", "BSE"}:
        raise ValueError(f"unsupported exchange for active instrument: {exchange or '<empty>'}")
    if segment in _INDEX_SEGMENTS:
        asset_class = "INDEX"
        reason = "binding-index-context"
    elif segment == "NSE_EQ" and series in _ALLOWED_NSE:
        asset_class = "CASH_EQUITY"
        reason = f"accepted-nse-series:{series}"
    elif segment == "BSE_EQ" and series in _ALLOWED_BSE:
        asset_class = "CASH_EQUITY"
        reason = f"accepted-bse-only-series:{series}"
    else:
        raise ValueError(f"out-of-policy active instrument: segment={segment} series={series}")
    isin = _text(row.get("isin")) or None
    if isin and isin.upper().startswith("INF"):
        raise ValueError(f"mutual-fund ISIN cannot enter active equity universe: {isin}")
    key = _text(row.get("instrument_key") or row.get("provider_instrument_key"))
    symbol = _text(row.get("trading_symbol") or row.get("symbol")).upper()
    name = _text(row.get("name") or row.get("display_name") or symbol)
    if not key or not symbol:
        raise ValueError("instrument_key and trading_symbol are required")
    return {
        "provider_instrument_key": key,
        "exchange": exchange,
        "trading_symbol": symbol,
        "display_name": name,
        "isin": isin,
        "asset_class": asset_class,
        "exchange_series": series or ("INDEX" if asset_class == "INDEX" else ""),
        "lot_size": max(1, int(row.get("lot_size") or 1)),
        "tick_size": float(row.get("tick_size") or 0.05),
        "universe_revision": revision,
        "classification_reason": reason,
    }


# Backward-compatible private alias retained for existing contract tests.
_normalise = canonical_instrument_row


@dataclass(frozen=True)
class InstrumentAuthorityProof:
    revision: str
    active_total: int
    nse_equities: int
    bse_only_equities: int
    indices: int
    derivatives: int
    out_of_policy_rows: int

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class ProductionInstrumentRepository:
    SERVICE_VERSION = "postgres-instrument-authority-1.1.0-bounded-interactive-sample"

    def __init__(self, authority: PostgresAuthority):
        self.authority = authority

    def replace_active(self, rows: Iterable[Mapping[str, Any]], *, revision: str) -> InstrumentAuthorityProof:
        revision = _text(revision)
        if not revision:
            raise ValueError("universe revision is required")
        normalised = [canonical_instrument_row(row, revision) for row in rows]
        if not normalised:
            raise ValueError("active instrument catalogue cannot be empty")
        keys = [row["provider_instrument_key"] for row in normalised]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate provider instrument key in staged catalogue")
        isins = [row["isin"].upper() for row in normalised if row.get("isin")]
        if len(isins) != len(set(isins)):
            raise ValueError("duplicate ISIN in staged NSE-first catalogue")

        insert_sql = """
            INSERT INTO core.instruments(
                provider_instrument_key, exchange, trading_symbol, display_name, isin,
                asset_class, exchange_series, lot_size, tick_size, universe_revision,
                classification_reason, validation_status, active_from, active_to
            ) VALUES(
                %(provider_instrument_key)s, %(exchange)s, %(trading_symbol)s, %(display_name)s, %(isin)s,
                %(asset_class)s, %(exchange_series)s, %(lot_size)s, %(tick_size)s, %(universe_revision)s,
                %(classification_reason)s, 'ACCEPTED', clock_timestamp(), NULL
            )
            ON CONFLICT(provider_instrument_key, universe_revision) DO UPDATE SET
                exchange=EXCLUDED.exchange,
                trading_symbol=EXCLUDED.trading_symbol,
                display_name=EXCLUDED.display_name,
                isin=EXCLUDED.isin,
                asset_class=EXCLUDED.asset_class,
                exchange_series=EXCLUDED.exchange_series,
                lot_size=EXCLUDED.lot_size,
                tick_size=EXCLUDED.tick_size,
                classification_reason=EXCLUDED.classification_reason,
                validation_status='ACCEPTED',
                -- Preserve the original effective start for an already-known
                -- (instrument, universe revision). Refreshing the catalogue is
                -- not a new listing/effective-date event. Resetting active_from
                -- on every refresh destroys the point-in-time history used by
                -- Research joins and can make millions of older candles appear
                -- outside the authoritative universe.
                active_to=NULL
        """
        with self.authority.transaction(
            isolation_level="serializable",
            lock_timeout_ms=1500,
            statement_timeout_ms=30000,
            idle_timeout_ms=35000,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('project-laddu-instrument-catalogue'))")
                # MVCC keeps the prior committed catalogue visible to other
                # sessions until this entire replacement commits.
                cur.execute(
                    "UPDATE core.instruments SET active_to=clock_timestamp() "
                    "WHERE active_to IS NULL AND universe_revision<>%s",
                    (revision,),
                )
                cur.executemany(insert_sql, normalised)
                cur.execute(
                    "UPDATE core.instruments SET active_to=clock_timestamp() "
                    "WHERE active_to IS NULL AND universe_revision=%s "
                    "AND NOT (provider_instrument_key = ANY(%s))",
                    (revision, keys),
                )
        proof = self.proof(revision=revision)
        if proof.active_total != len(normalised):
            raise RuntimeError(
                f"PostgreSQL instrument replacement mismatch: staged={len(normalised)} active={proof.active_total}"
            )
        if proof.nse_equities <= 0 or proof.bse_only_equities <= 0 or proof.indices <= 0:
            raise RuntimeError(f"incomplete focused universe committed: {proof.as_dict()}")
        if proof.derivatives or proof.out_of_policy_rows:
            raise RuntimeError(f"out-of-policy universe committed: {proof.as_dict()}")
        return proof

    def active_rows(self, *, revision: str | None = None) -> list[dict[str, Any]]:
        where = "active_to IS NULL AND validation_status='ACCEPTED'"
        params: tuple[Any, ...] = ()
        if revision:
            where += " AND universe_revision=%s"
            params = (revision,)
        sql = f"""
            SELECT provider_instrument_key AS instrument_key,
                   exchange,
                   CASE WHEN asset_class='INDEX' THEN exchange || '_INDEX' ELSE exchange || '_EQ' END AS segment,
                   trading_symbol,
                   display_name AS name,
                   exchange_series AS instrument_type,
                   isin,
                   NULL::text AS expiry,
                   NULL::double precision AS strike,
                   NULL::text AS option_type,
                   lot_size,
                   tick_size,
                   universe_revision
              FROM core.instruments
             WHERE {where}
             ORDER BY exchange, trading_symbol, provider_instrument_key
        """
        return [dict(row) for row in self.authority.execute(sql, params, fetch="all", statement_timeout_ms=5000)]

    def equity_sample(self, limit: int = 100, *, revision: str | None = None) -> list[dict[str, Any]]:
        """Return a bounded deterministic NSE/BSE-only cash-equity sample.

        This is an interactive proof/read-model primitive, not a universe load.
        It never materialises the full catalogue and it deliberately includes a
        small BSE-only slice plus common symbol-parser edge cases when present.
        """
        cap = max(20, min(150, int(limit or 100)))
        bse_target = max(5, min(cap // 3, max(5, cap // 10)))
        nse_target = max(0, cap - bse_target)
        pinned = ["M&M", "BAJAJ-AUTO", "TCS", "TECHM", "RELIANCE"]
        revision_sql = " AND i.universe_revision=%s" if revision else ""
        base_params: list[Any] = [revision] if revision else []
        nse_sql = f"""
            SELECT i.provider_instrument_key AS instrument_key, i.exchange,
                   i.exchange || '_EQ' AS segment, i.trading_symbol,
                   i.display_name AS name, i.exchange_series AS instrument_type,
                   i.isin, i.lot_size, i.tick_size, i.universe_revision
              FROM core.instruments i
             WHERE i.active_to IS NULL AND i.validation_status='ACCEPTED'
               AND i.asset_class='CASH_EQUITY' AND i.exchange='NSE'
               {revision_sql}
             ORDER BY CASE WHEN i.trading_symbol = ANY(%s) THEN 0 ELSE 1 END,
                      i.trading_symbol, i.provider_instrument_key
             LIMIT %s
        """
        nse_params = [*base_params, pinned, nse_target]
        nse = [dict(row) for row in self.authority.execute(
            nse_sql, tuple(nse_params), fetch="all", statement_timeout_ms=1200
        )]
        bse_sql = f"""
            SELECT i.provider_instrument_key AS instrument_key, i.exchange,
                   i.exchange || '_EQ' AS segment, i.trading_symbol,
                   i.display_name AS name, i.exchange_series AS instrument_type,
                   i.isin, i.lot_size, i.tick_size, i.universe_revision
              FROM core.instruments i
             WHERE i.active_to IS NULL AND i.validation_status='ACCEPTED'
               AND i.asset_class='CASH_EQUITY' AND i.exchange='BSE'
               {revision_sql}
               AND NOT EXISTS (
                   SELECT 1 FROM core.instruments n
                    WHERE n.active_to IS NULL AND n.validation_status='ACCEPTED'
                      AND n.asset_class='CASH_EQUITY' AND n.exchange='NSE'
                      AND upper(n.trading_symbol)=upper(i.trading_symbol)
               )
             ORDER BY i.trading_symbol, i.provider_instrument_key
             LIMIT %s
        """
        bse_params = [*base_params, bse_target]
        bse = [dict(row) for row in self.authority.execute(
            bse_sql, tuple(bse_params), fetch="all", statement_timeout_ms=1200
        )]
        rows = nse + bse
        # Exact provider keys are unique in the canonical authority. Keep the
        # defensive de-duplication local and bounded in case a compatibility
        # test facade returns duplicates.
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            key = str(row.get("instrument_key") or "")
            if key and key not in seen:
                seen.add(key)
                out.append(row)
            if len(out) >= cap:
                break
        return out

    def proof(self, *, revision: str | None = None) -> InstrumentAuthorityProof:
        where = "active_to IS NULL AND validation_status='ACCEPTED'"
        params: tuple[Any, ...] = ()
        if revision:
            where += " AND universe_revision=%s"
            params = (revision,)
        row = self.authority.execute(
            f"""
            SELECT COALESCE(max(universe_revision),'') AS revision,
                   count(*)::bigint AS active_total,
                   count(*) FILTER (WHERE exchange='NSE' AND asset_class='CASH_EQUITY')::bigint AS nse_equities,
                   count(*) FILTER (WHERE exchange='BSE' AND asset_class='CASH_EQUITY')::bigint AS bse_only_equities,
                   count(*) FILTER (WHERE asset_class='INDEX')::bigint AS indices,
                   0::bigint AS derivatives,
                   count(*) FILTER (
                       WHERE asset_class NOT IN ('CASH_EQUITY','INDEX')
                          OR (isin IS NOT NULL AND left(upper(isin), 3) = 'INF')
                   )::bigint AS out_of_policy_rows
              FROM core.instruments WHERE {where}
            """,
            params,
            fetch="one",
            statement_timeout_ms=5000,
        ) or {}
        return InstrumentAuthorityProof(
            revision=_text(row.get("revision")),
            active_total=int(row.get("active_total") or 0),
            nse_equities=int(row.get("nse_equities") or 0),
            bse_only_equities=int(row.get("bse_only_equities") or 0),
            indices=int(row.get("indices") or 0),
            derivatives=int(row.get("derivatives") or 0),
            out_of_policy_rows=int(row.get("out_of_policy_rows") or 0),
        )
