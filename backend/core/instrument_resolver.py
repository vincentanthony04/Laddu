"""
InstrumentResolver — v37.4, Cluster B decoupling.

Consolidates three previously-duplicated symbol -> instrument resolution
paths that lived inline inside LadduRuntime (main.py):
  1. quote_delta's per-symbol resolution loop
  2. _first_instrument (cash-equity search)
  3. _index_instrument_for_chart (index search + any-instrument fallback)

Two real bugs lived in the old duplicated code, both fixed here once instead
of three times:

  BUG 1 (perf): storage.find_index_instruments() and find_any_instruments()
  ran an unconditional leading-wildcard `LIKE '%q%'` query -- a full scan of
  the legacy provider-wide instruments table -- on every single call. find_instruments()
  had already been fixed to try a sargable prefix match first (v36.6), but
  that fix was never applied to the index/any-instrument paths. This module
  applies the same prefix-first strategy to both.

  BUG 2 (correctness + perf compounding): when a symbol resolved to zero
  matches (e.g. an index name that doesn't line up with the local
  instruments dump), nothing was ever cached, so the same expensive query
  re-ran on *every* subsequent quote-delta poll or chart load for that
  symbol, forever. This module adds a negative-result TTL cache (same
  pattern as LadduRuntime._bad_historical_keys) so an unresolved symbol is
  retried at most once per `negative_ttl_sec`, not once per poll.

Caching layers (in resolution order):
  1. in-process positive cache (fast path, expires after `positive_ttl_sec`)
  2. persistent DB cache (store.get_cached_instrument) -- survives restarts
  3. in-process negative cache (skip network/DB scan entirely while fresh)
  4. live search against the local instruments table (prefix-first)
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional, Tuple

from reference_catalog import canonical_fallback_equity_symbol, final_fallback_instrument
from core.instrument_identity_contract import INDEX_SYMBOL_ALIASES


class InstrumentResolver:
    def __init__(self, store, client, logger=None,
                 positive_ttl_sec: float = 3600.0,
                 negative_ttl_sec: float = 180.0):
        self.store = store
        self.client = client
        self.logger = logger
        self.positive_ttl_sec = positive_ttl_sec
        self.negative_ttl_sec = negative_ttl_sec

        self._positive_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
        self._negative_cache: Dict[str, float] = {}  # symbol -> expiry ts

        self._index_alias_map = dict(INDEX_SYMBOL_ALIASES)
        self._index_aliases = set(self._index_alias_map) | set(self._index_alias_map.values())
        # Deterministic identities for the core indices. These are identity
        # fallbacks only; they never provide a price. They prevent a fuzzy local
        # instrument search from rebinding an index display name to an equity,
        # ETF, debt security, or another NIFTY-family instrument.
        self._index_identity_fallbacks = {
            "NIFTY 50": {"instrument_key": "NSE_INDEX|Nifty 50", "trading_symbol": "NIFTY 50", "symbol": "NIFTY 50", "name": "Nifty 50", "exchange": "NSE_INDEX", "segment": "NSE_INDEX", "instrument_type": "INDEX"},
            "NIFTY BANK": {"instrument_key": "NSE_INDEX|Nifty Bank", "trading_symbol": "NIFTY BANK", "symbol": "NIFTY BANK", "name": "Nifty Bank", "exchange": "NSE_INDEX", "segment": "NSE_INDEX", "instrument_type": "INDEX"},
            "SENSEX": {"instrument_key": "BSE_INDEX|SENSEX", "trading_symbol": "SENSEX", "symbol": "SENSEX", "name": "SENSEX", "exchange": "BSE_INDEX", "segment": "BSE_INDEX", "instrument_type": "INDEX"},
            "INDIA VIX": {"instrument_key": "NSE_INDEX|India VIX", "trading_symbol": "INDIA VIX", "symbol": "INDIA VIX", "name": "India VIX", "exchange": "NSE_INDEX", "segment": "NSE_INDEX", "instrument_type": "INDEX"},
        }
        # Identity-only fallback for NSE cash indices. Search results remain
        # authoritative when available; this prevents a known index name from
        # falling through to fuzzy equity matches or a permanent negative loop.
        provider_names = {
            "NIFTY FINANCIAL SERVICES": "Nifty Fin Service",
            "NIFTY OIL & GAS": "Nifty Oil & Gas",
            "NIFTY HEALTHCARE": "Nifty Healthcare Index",
            "NIFTY CONSUMER DURABLES": "Nifty Consumer Durables",
            "NIFTY PRIVATE BANK": "Nifty Private Bank",
            "NIFTY PSU BANK": "Nifty PSU Bank",
        }
        for alias in sorted(self._index_aliases):
            canonical = self._canonical_index_symbol(alias)
            if canonical in self._index_identity_fallbacks or not canonical.startswith("NIFTY "):
                continue
            provider_name = provider_names.get(canonical, "Nifty " + canonical.removeprefix("NIFTY ").title())
            self._index_identity_fallbacks[canonical] = {
                "instrument_key": f"NSE_INDEX|{provider_name}",
                "trading_symbol": canonical,
                "symbol": canonical,
                "name": provider_name,
                "exchange": "NSE_INDEX",
                "segment": "NSE_INDEX",
                "instrument_type": "INDEX",
            }

    # --------------------------------------------------------------- log
    def _log(self, level: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        if self.logger is not None:
            self.logger.event(level, message, detail)

    # ---------------------------------------------------------- negative
    def _is_negative(self, symbol: str) -> bool:
        exp = self._negative_cache.get(symbol)
        if exp is None:
            return False
        if time.time() >= exp:
            self._negative_cache.pop(symbol, None)
            return False
        return True

    def _mark_negative(self, symbol: str) -> None:
        self._negative_cache[symbol] = time.time() + self.negative_ttl_sec

    def _canonical_index_symbol(self, symbol: str) -> str:
        s = str(symbol or "").strip().upper().replace("_", " ")
        return self._index_alias_map.get(s, s)

    @staticmethod
    def _is_index_identity(row: Optional[Dict[str, Any]]) -> bool:
        if not row:
            return False
        key = str(row.get("instrument_key") or "").upper()
        kind = str(row.get("instrument_type") or "").upper()
        segment = str(row.get("segment") or row.get("exchange") or "").upper()
        return key.startswith(("NSE_INDEX|", "BSE_INDEX|")) or kind == "INDEX" or "INDEX" in segment

    def _cache_key(self, symbol: str, *, looks_like_index: bool) -> str:
        if looks_like_index:
            return f"INDEX::{self._canonical_index_symbol(symbol)}"
        return f"EQUITY::{symbol}"

    def _trusted_equity_fallback(self, symbol: str) -> Optional[Dict[str, Any]]:
        row = final_fallback_instrument(symbol)
        if not row:
            return None
        row = dict(row)
        row.setdefault("segment", row.get("exchange") or "NSE_EQ")
        row.setdefault("exchange", "NSE_EQ")
        row.setdefault("symbol", symbol)
        row.setdefault("trading_symbol", symbol)
        resolved = str(row.get("trading_symbol") or "").strip().upper()
        key = str(row.get("instrument_key") or "").strip().upper()
        isin = str(row.get("isin") or "").strip().upper()
        if resolved != symbol or not (re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", isin) or re.search(r"\bIN[A-Z0-9]{10}\b", key)):
            return None
        return row

    # ---------------------------------------------------------- resolve
    def resolve(self, symbol: str, *, prefer_index: Optional[bool] = None, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        """Resolve a display symbol to a cash-equity or cash-index identity.

        ``prefer_index=True`` selects the index search path; ``None``
        auto-detects known index names. ``False`` keeps a non-index symbol on
        the cash-equity path. The active product exposes no derivative
        resolution path.
        """
        raw = str(symbol or "").strip()
        if not raw:
            return None

        # Some runtime lanes already carry the authoritative provider token.
        # Never turn ``NSE_EQ|...`` into the display string ``NSE EQ|...`` and
        # send it through fuzzy symbol search: that creates negative-cache storms
        # and can never produce an exact trading-symbol match. Resolve the token
        # directly from the binding local instrument projection.
        token = raw
        token_upper = token.upper()
        match = re.match(r"^(NSE|BSE)[ _](EQ|INDEX)\|(.*)$", token, flags=re.IGNORECASE)
        if match:
            token = f"{match.group(1).upper()}_{match.group(2).upper()}|{match.group(3)}"
            finder = getattr(self.store, "find_instrument_by_key", None)
            inst = finder(token) if callable(finder) else None
            if inst:
                resolved_symbol = str(inst.get("trading_symbol") or inst.get("symbol") or "").strip().upper()
                if resolved_symbol:
                    looks_index = self._is_index_identity(inst)
                    cache_key = self._cache_key(
                        self._canonical_index_symbol(resolved_symbol) if looks_index else resolved_symbol,
                        looks_like_index=looks_index,
                    )
                    self._positive_cache[cache_key] = (dict(inst), time.time())
                return dict(inst)
            self._log("WARN", "Authoritative instrument token absent from active catalogue",
                      {"instrument_key": token, "identity_class": "INDEX" if "_INDEX|" in token.upper() else "EQUITY"})
            return None

        s = raw.upper().replace("_", " ")

        auto_index = s in self._index_aliases or s.startswith("NIFTY ") or s in ("SENSEX", "BSE SENSEX", "INDIA VIX")
        if not auto_index:
            s = canonical_fallback_equity_symbol(s)
        # Known cash-index identities may never be demoted into the equity
        # resolver merely because a caller passed prefer_index=False.
        looks_like_index = bool(auto_index or (prefer_index is True))
        if looks_like_index:
            s = self._canonical_index_symbol(s)
        cache_key = self._cache_key(s, looks_like_index=looks_like_index)

        def usable_identity(row: Optional[Dict[str, Any]]) -> bool:
            if not row:
                return False
            if looks_like_index:
                return self._is_index_identity(row)
            # A cache slot is namespaced by the requested trading symbol. It
            # must never be considered usable merely because it contains an
            # equity ISIN. Earlier fuzzy resolutions could persist IDEAFORGE
            # under EQUITY::IDEA and bind chart data to the wrong security.
            resolved_symbol = str(row.get("trading_symbol") or row.get("symbol") or "").strip().upper()
            if resolved_symbol != s or self._is_index_identity(row):
                return False
            isin = str(row.get("isin") or "").strip().upper()
            key = str(row.get("instrument_key") or "").strip().upper()
            return bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", isin) or re.search(r"\bIN[A-Z0-9]{10}\b", key))

        cached = self._positive_cache.get(cache_key)
        if not force_refresh and cached and (time.time() - cached[1]) < self.positive_ttl_sec:
            if usable_identity(cached[0]):
                return cached[0]
            self._positive_cache.pop(cache_key, None)

        persisted = None if force_refresh else self.store.get_cached_instrument(cache_key)
        if persisted and usable_identity(persisted):
            self._positive_cache[cache_key] = (persisted, time.time())
            return persisted
        if persisted and not looks_like_index:
            self._log("WARN", "Persisted equity identity is incomplete; refreshing from instrument master",
                      {"symbol": s, "instrument_key": persisted.get("instrument_key")})

        if not force_refresh and self._is_negative(cache_key):
            return None
        if force_refresh:
            self._positive_cache.pop(cache_key, None)
            self._negative_cache.pop(cache_key, None)

        matches = []
        try:
            if looks_like_index:
                matches = self.client.search_index_instruments(s, limit=10)
                matches = [row for row in matches if self._is_index_identity(row)]
                if not matches:
                    any_matches = self.client.search_any_instruments(s, limit=10)
                    matches = [row for row in any_matches if self._is_index_identity(row)]
            else:
                matches = self.client.search_instruments(s, limit=10)
                if not matches:
                    matches = self.client.search_any_instruments(s, limit=10)
        except Exception as exc:
            self._log("WARN", "Instrument resolution search failed", {"symbol": s, "error": str(exc)[:180]})
            matches = []

        if not matches and looks_like_index:
            fallback = self._index_identity_fallbacks.get(s)
            if fallback:
                matches = [dict(fallback)]

        if not matches:
            fallback = None if looks_like_index else self._trusted_equity_fallback(s)
            if fallback:
                matches = [fallback]
                self._log("WARN", "Instrument master exact row unavailable; trusted recovery identity used",
                          {"symbol": s, "instrument_key": fallback.get("instrument_key")})
            else:
                self._mark_negative(cache_key)
                self._log("WARN", "Instrument unresolved; caching negative result",
                          {"symbol": s, "identity_class": cache_key.split("::", 1)[0], "retry_after_sec": self.negative_ttl_sec})
                return None

        inst = dict(matches[0])
        if looks_like_index:
            def index_label(row):
                return str(row.get("trading_symbol") or row.get("symbol") or row.get("name") or "").upper().replace("_", " ").strip()
            exact = [m for m in matches if self._canonical_index_symbol(index_label(m)) == s and self._is_index_identity(m)]
            if exact:
                nse = [m for m in exact if str(m.get("exchange") or m.get("segment") or "").upper().startswith("NSE")]
                inst = dict((nse or exact)[0])
        else:
            exact = [m for m in matches if str(m.get("trading_symbol") or "").strip().upper() == s and not self._is_index_identity(m)]
            if exact:
                nse = [m for m in exact if str(m.get("exchange") or m.get("segment") or "").upper().startswith("NSE")]
                inst = dict((nse or exact)[0])
            else:
                # Resolution is an identity operation, not a suggestion
                # operation. Fuzzy rows are valid in /api/suggest, but a
                # direct request must never bind to another symbol.
                fallback = self._trusted_equity_fallback(s)
                if fallback:
                    inst = fallback
                    self._log("WARN", "Fuzzy instrument rows rejected; trusted exact recovery identity used",
                              {"symbol": s, "candidate_symbols": [str(m.get("trading_symbol") or "") for m in matches[:5]], "instrument_key": fallback.get("instrument_key")})
                else:
                    self._mark_negative(cache_key)
                    self._log("ERROR", "Equity resolution had no exact trading-symbol match",
                              {"symbol": s, "candidate_symbols": [str(m.get("trading_symbol") or "") for m in matches[:5]]})
                    return None

        if looks_like_index and not self._is_index_identity(inst):
            self._mark_negative(cache_key)
            self._log("ERROR", "Resolved row failed index identity contract", {"symbol": s, "instrument_key": inst.get("instrument_key")})
            return None

        self._positive_cache[cache_key] = (inst, time.time())
        try:
            self.store.set_cached_instrument(cache_key, inst)
        except Exception:
            pass
        return inst

    def resolve_many(self, symbols: list[str]) -> Dict[str, Dict[str, Any]]:
        """Resolve a cash-equity/index symbol list and return successful identities."""
        out: Dict[str, Dict[str, Any]] = {}
        for symbol in symbols:
            inst = self.resolve(symbol)
            if inst:
                out[str(symbol or "").upper()] = inst
        return out

    def clear_negative_cache(self) -> int:
        """Release prior misses immediately after a new instrument master lands."""
        count = len(self._negative_cache)
        self._negative_cache.clear()
        return count

    def invalidate(self, symbol: str) -> None:
        s = (symbol or "").strip().upper().replace("_", " ")
        equity_s = canonical_fallback_equity_symbol(s)
        candidates = {
            self._cache_key(equity_s, looks_like_index=False),
            self._cache_key(self._canonical_index_symbol(s), looks_like_index=True),
        }
        for key in candidates:
            self._positive_cache.pop(key, None)
            self._negative_cache.pop(key, None)
