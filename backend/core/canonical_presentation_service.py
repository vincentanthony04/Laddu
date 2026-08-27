from __future__ import annotations

"""Canonical customer-facing instrument presentation.

Provider instrument keys are machine identities only.  Customer read models
must expose a canonical trading symbol and company/index name from the local
instrument authority.  This service never calls a provider.
"""

from dataclasses import dataclass
import re
import threading
from typing import Any, Dict, Iterable, Optional

_PROVIDER_KEY = re.compile(r"^(?:NSE|BSE)_(?:EQ|INDEX)\|", re.IGNORECASE)


@dataclass(frozen=True)
class PresentationIdentity:
    ok: bool
    symbol: str
    display_name: str
    instrument_key: str
    exchange: str
    isin: str
    instrument_type: str
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "symbol": self.symbol,
            "trading_symbol": self.symbol,
            "display_name": self.display_name,
            "name": self.display_name,
            "instrument_key": self.instrument_key,
            "exchange": self.exchange,
            "isin": self.isin,
            "instrument_type": self.instrument_type,
            "reason": self.reason,
        }


class CanonicalPresentationService:
    VERSION = "clean-core-canonical-presentation-1.0.0"

    def __init__(self, store: Any):
        self.store = store
        # Canonical instrument identity is session-stable and read on nearly
        # every customer route.  Re-querying PostgreSQL/search indexes for the
        # same symbol made a supposedly O(1) read model pay hundreds of
        # milliseconds repeatedly.  Cache only positive canonical identities;
        # misses are never cached so a later catalogue/bootstrap repair remains
        # immediately discoverable.
        try:
            if not hasattr(store, "_presentation_identity_cache"):
                setattr(store, "_presentation_identity_cache", {})
            if not hasattr(store, "_presentation_identity_cache_lock"):
                setattr(store, "_presentation_identity_cache_lock", threading.RLock())
            self._cache = store._presentation_identity_cache
            self._cache_lock = store._presentation_identity_cache_lock
        except (AttributeError, TypeError):
            # Narrow immutable test/read facades may not expose __dict__. They
            # still get a correct service-local cache; production stores share
            # the attached session cache above.
            self._cache = {}
            self._cache_lock = threading.RLock()

    @staticmethod
    def is_provider_key(value: Any) -> bool:
        return bool(_PROVIDER_KEY.match(str(value or "").strip()))

    @staticmethod
    def _symbol(row: Dict[str, Any]) -> str:
        return str(row.get("trading_symbol") or row.get("symbol") or "").strip().upper()

    @staticmethod
    def _display_name(row: Dict[str, Any], symbol: str) -> str:
        return str(row.get("display_name") or row.get("name") or row.get("company_name") or symbol).strip()

    @classmethod
    def from_authority_row(
        cls,
        source: Dict[str, Any],
        *,
        reason: str = "projected from canonical local instrument authority",
    ) -> PresentationIdentity:
        """Project a row already returned by the canonical instrument authority.

        This is deliberately query-free. Batch read models must not turn one
        authoritative catalogue query into an N+1 sequence of identity reads.
        """
        row = dict(source or {})
        symbol = cls._symbol(row)
        instrument_key = str(row.get("instrument_key") or "").strip()
        ok = bool(symbol and not cls.is_provider_key(symbol) and instrument_key)
        return PresentationIdentity(
            ok=ok,
            symbol=symbol if not cls.is_provider_key(symbol) else "",
            display_name=cls._display_name(row, symbol) if ok else "",
            instrument_key=instrument_key,
            exchange=str(row.get("exchange") or row.get("segment") or "").strip(),
            isin=str(row.get("isin") or "").strip(),
            instrument_type=str(row.get("instrument_type") or "").strip(),
            reason=reason if ok else "canonical instrument authority row is incomplete or unsafe",
        )

    def _find_local(self, raw: str) -> Optional[Dict[str, Any]]:
        token = str(raw or "").strip()
        if not token:
            return None
        if self.is_provider_key(token):
            finder = getattr(self.store, "find_instrument_by_key", None)
            row = finder(token) if callable(finder) else None
            return dict(row) if row else None

        symbol = token.upper()
        # Governed index aliases are part of Laddu's local identity authority.
        # They must not depend on a fuzzy catalogue search being warm before a
        # chart/report can resolve NIFTY/sector identities (e.g. PHARMA).
        try:
            from core.heatmap_index_catalog import heatmap_index_identity
            index_row = heatmap_index_identity(symbol)
        except Exception:
            index_row = None
        if index_row:
            return dict(index_row)
        candidates = []
        for name in ("quick_symbol_search", "find_instruments", "find_any_instruments"):
            fn = getattr(self.store, name, None)
            if not callable(fn):
                continue
            try:
                rows = fn(symbol, 12) or []
            except TypeError:
                rows = fn(symbol) or []
            candidates.extend(dict(row or {}) for row in rows if isinstance(row, dict))
            exact = [row for row in candidates if self._symbol(row) == symbol]
            if exact:
                nse = [row for row in exact if str(row.get("exchange") or row.get("segment") or "").upper().startswith("NSE")]
                return dict((nse or exact)[0])
        cached = getattr(self.store, "get_cached_instrument", None)
        if callable(cached):
            for key in (symbol, f"EQUITY::{symbol}", f"INDEX::{symbol}"):
                try:
                    row = cached(key)
                except Exception:
                    row = None
                if row and self._symbol(dict(row)) == symbol:
                    return dict(row)
        return None

    def resolve(self, raw: Any) -> PresentationIdentity:
        requested = str(raw or "").strip()
        token = requested if self.is_provider_key(requested) else requested.upper()
        with self._cache_lock:
            cached = self._cache.get(token)
        if isinstance(cached, PresentationIdentity) and cached.ok:
            return cached
        row = self._find_local(requested)
        if not row:
            # A provider key is never allowed to leak as a customer symbol.
            # Negative results are intentionally not cached.
            safe_symbol = "" if self.is_provider_key(requested) else requested.upper()
            return PresentationIdentity(
                ok=False,
                symbol=safe_symbol,
                display_name=safe_symbol,
                instrument_key=requested if self.is_provider_key(requested) else "",
                exchange="",
                isin="",
                instrument_type="",
                reason="canonical instrument identity is absent from the local authority",
            )
        identity = self.from_authority_row(
            row,
            reason="resolved from local canonical instrument authority",
        )
        if identity.ok:
            with self._cache_lock:
                if len(self._cache) >= 8192:
                    self._cache.clear()
                self._cache[token] = identity
                self._cache[identity.symbol] = identity
                if identity.instrument_key:
                    self._cache[identity.instrument_key] = identity
        return identity


    def prime_authority_rows(self, rows: Iterable[Dict[str, Any]]) -> int:
        """Seed the shared session identity cache from an existing authority batch.

        A catalogue/sample/search response has already paid for canonical
        PostgreSQL identity. Repeating one query per subsequent stock click is an
        N+1 anti-pattern, so those exact authority rows are reused in memory.
        """
        count = 0
        with self._cache_lock:
            for raw in rows or []:
                identity = self.from_authority_row(dict(raw or {}))
                if not identity.ok:
                    continue
                self._cache[identity.symbol] = identity
                if identity.instrument_key:
                    self._cache[identity.instrument_key] = identity
                count += 1
            if len(self._cache) > 16384:
                # Session identity is small, but keep the bound explicit.
                items = list(self._cache.items())[-8192:]
                self._cache.clear()
                self._cache.update(items)
        return count
    def decorate_row(self, source: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(source or {})
        raw = row.get("instrument_key") or row.get("symbol") or row.get("trading_symbol") or row.get("stock")
        identity = self.resolve(raw)
        if identity.ok:
            row.update({
                "symbol": identity.symbol,
                "trading_symbol": identity.symbol,
                "display_name": identity.display_name,
                "company_name": identity.display_name,
                "instrument_key": identity.instrument_key or str(row.get("instrument_key") or ""),
                "exchange": identity.exchange or row.get("exchange"),
                "isin": identity.isin or row.get("isin"),
                "identity_verified": True,
                "presentation_identity": identity.as_dict(),
                "customer_visible": True,
            })
        else:
            row.update({
                "identity_verified": False,
                "presentation_identity": identity.as_dict(),
                "customer_visible": not self.is_provider_key(row.get("symbol") or row.get("trading_symbol")),
                "presentation_blocker": identity.reason,
            })
        return row

    def decorate_rows(self, rows: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
        return [self.decorate_row(row) for row in rows or []]
