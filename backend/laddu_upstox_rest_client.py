"""Project Laddu REST/candle client.

The distinct module name is intentional: ``upstox_client`` belongs to the
official Upstox Python SDK used by the canonical WebSocket gateway.
"""
from __future__ import annotations
import csv
import gzip
import io
import json
import os
import re
import subprocess
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import DATA_DIR, INSTRUMENT_URLS, LINUX_TOKEN_FILE, TOKEN_FILE, TOKEN_HELPER, UPSTOX_BASE_URL
from models import now_iso
from core.fundamental_scoring_authority import DEFAULT_FUNDAMENTAL_SCORING_AUTHORITY
from core.fundamental_dimension_authority import DEFAULT_FUNDAMENTAL_DIMENSION_AUTHORITY
from core.sector_classification_authority import DEFAULT_SECTOR_CLASSIFICATION_AUTHORITY
from core.sector_fundamental_checklist_authority import DEFAULT_SECTOR_FUNDAMENTAL_CHECKLIST_AUTHORITY
from core.historical_data_service import HistoricalDataReadinessService, PREFERRED_RESEARCH_YEARS
from core.instrument_universe_policy import ACTIVE_UNIVERSE_REVISION, build_active_universe
from core.universe_authority import CanonicalUniverse, build_canonical_universe, lifecycle_diff
from core.broker_charge_snapshot_authority import DEFAULT_BROKER_CHARGE_SNAPSHOT_AUTHORITY


def _safe_float(v):
    try:
        if v in (None, "", "null"):
            return None
        return float(v)
    except Exception:
        return None


def _pick(d: Dict[str, Any], *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _full_quote_day_change(raw: Dict[str, Any], ohlc: Dict[str, Any], ltp: float | None) -> Dict[str, Any]:
    """Normalise the day-change contract for Upstox full-market quotes.

    The V2 full-quote response documents ``net_change`` as the absolute move
    from the previous trading day's close.  ``ohlc.close`` is the most recent
    session close and can equal the current LTP, so using it as previous close
    can manufacture a false 0.00 / 0.00% ticker.  Prefer the authoritative
    absolute change, derive previous close from it, and leave the direction
    unavailable rather than inventing a zero when no authoritative reference
    exists.
    """
    last = _safe_float(ltp)
    net_change = _safe_float(_pick(raw, "net_change", "netChange", "day_change_abs", "point_change"))
    cp = _safe_float(_pick(raw, "cp", "prev_close", "previous_close"))
    source = None

    if last is not None and net_change is not None:
        previous_close = last - net_change
        source = "net_change"
    elif cp not in (None, 0):
        previous_close = cp
        net_change = round(last - cp, 8) if last is not None else None
        source = "previous_close"
    else:
        # Legacy/test payloads may omit net_change but expose a distinct OHLC
        # close.  Use it only when it is materially different from LTP.  Equality
        # is ambiguous (often the current session close) and must not manufacture
        # a neutral 0.00 / 0.00% day signal.
        session_close = _safe_float(_pick(ohlc or {}, "close"))
        if last is not None and session_close not in (None, 0) and abs(last - session_close) > 1e-9:
            previous_close = session_close
            net_change = last - session_close
            source = "distinct_ohlc_close_fallback"
        else:
            previous_close = None

    change_pct = None
    if previous_close not in (None, 0) and net_change is not None:
        change_pct = round((net_change / previous_close) * 100.0, 4)

    return {
        "previous_close": round(previous_close, 8) if previous_close is not None else None,
        "rupee_change": round(net_change, 8) if net_change is not None else None,
        "change_pct": change_pct,
        "change_source": source,
        "session_close": _safe_float(_pick(ohlc or {}, "close")),
    }


def _normalise_expiry(v):
    if v in (None, ""):
        return ""
    return str(v)


def _merge_authoritative_instrument_meta(
    meta: Dict[str, Any] | None,
    stats: Dict[str, Any] | None,
    reason: str = "cache",
) -> Dict[str, Any]:
    """Merge PostgreSQL instrument proof into the runtime metadata contract.

    PostgreSQL exposes the accepted catalogue revision as ``revision`` while
    the historical cache metadata uses ``universe_revision``.  Treating those
    as unrelated made a fully reconciled 4,365-row authority appear legacy and
    forced an unnecessary provider refresh during installation.
    """
    current = dict(meta or {})
    proof = dict(stats or {})
    count = int(proof.get("active_total") or proof.get("count") or current.get("count") or 0)
    revision = str(
        proof.get("revision")
        or proof.get("universe_revision")
        or current.get("universe_revision")
        or ""
    ).strip()
    if count <= 0:
        return current or {"loaded": False, "count": 0, "source": reason, "cache_usable": False}

    universe_stats = dict(current.get("universe_stats") or {})
    universe_stats.update(proof)
    if revision:
        universe_stats["revision"] = revision
        universe_stats["universe_revision"] = revision

    source = current.get("source") or reason
    if proof.get("active_total") is not None and revision:
        source = "postgresql-instrument-authority"

    current.update({
        "loaded": True,
        "count": count,
        "source": source,
        "cache_usable": True,
        "message": "Using focused NSE/BSE cash-equity catalogue",
        "universe_revision": revision,
        "target_universe_revision": ACTIVE_UNIVERSE_REVISION,
        "universe_stats": universe_stats,
        "authority_engine": "postgresql" if source == "postgresql-instrument-authority" else current.get("authority_engine"),
    })
    return current


class UpstoxApiError(RuntimeError):
    def __init__(self, message: str, url: str = "", status: int | None = None, body: str = ""):
        super().__init__(message)
        self.url = url
        self.status = status
        self.body = body


class UpstoxClient:
    """Small, dependency-free Upstox HTTP client.

    v10 market-aware fixes:
    - Uses Upstox v3 historical candle path matching the user's successful direct test:
      /v3/historical-candle/{encoded_instrument_key}/{unit}/{interval}/{to}/{from}
    - Encodes the pipe in instrument keys (NSE_EQ|INE009A01021 -> NSE_EQ%7CINE009A01021).
    - Uses v3 LTP quote path for lightweight quote checks.
    - Never prints/stores plaintext token.
    """

    def __init__(self, store, logger=None):
        self.store = store
        self.logger = logger or (lambda level, module, msg, detail=None: None)
        self._token_cache: Optional[str] = None
        self._token_cache_ts = 0.0
        self._token_lock = threading.Lock()
        self._token_fail_until = 0.0
        self._preflight_cache: Dict[str, Any] = {}
        self._preflight_ts = 0.0
        # v15: prevent instrument download storms. Dashboard/search must use local cache first.
        self._instrument_lock = threading.Lock()
        self._instrument_refreshing = False
        self._instrument_last_attempt = 0.0
        self._instrument_cooldown_sec = 900

    def token_status(self) -> Dict[str, Any]:
        token = self.get_token()
        if not token:
            return {"ok": False, "state": "Login required", "message": "No secure Upstox token found"}
        return {"ok": True, "state": "Token configured", "message": "Encrypted token present; auth test available under /api/auth-test"}

    def get_token(self) -> Optional[str]:
        now = time.time()
        if self._token_cache and now - self._token_cache_ts < 600:
            return self._token_cache
        if os.name != "nt":
            # No Windows DPAPI available. Same operating model (a script writes
            # the token, the runtime reads it) but backed by a plain file with
            # owner-only permissions instead of DPAPI encryption.
            linux_token_file = Path(LINUX_TOKEN_FILE)
            if not linux_token_file.exists():
                return None
            try:
                token = linux_token_file.read_text(encoding="utf-8").strip()
            except Exception as exc:
                self.logger("WARN", "security", "Unable to read Linux token file", {"error": str(exc)})
                return None
            if token:
                self._token_cache = token
                self._token_cache_ts = now
            return token or None
        if not Path(TOKEN_FILE).exists():
            return None
        # One stuck PowerShell helper should not fan out into 10 concurrent
        # helper processes from quote/MTF/chart requests. Serialize decrypt and
        # apply a short negative cache after timeout/failure.
        if now < self._token_fail_until:
            return None
        with self._token_lock:
            now = time.time()
            if self._token_cache and now - self._token_cache_ts < 600:
                return self._token_cache
            if now < self._token_fail_until:
                return None
            try:
                cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(TOKEN_HELPER), "read"]
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
                if p.returncode != 0:
                    self._token_fail_until = time.time() + 30
                    self.logger("WARN", "security", "Unable to decrypt Upstox token", {"stderr": p.stderr[-300:]})
                    return None
                token = (p.stdout or "").strip()
                if token:
                    self._token_cache = token
                    self._token_cache_ts = time.time()
                    self._token_fail_until = 0.0
                    return token
                self._token_fail_until = time.time() + 30
            except Exception as exc:
                self._token_fail_until = time.time() + 30
                self.logger("WARN", "security", "Token helper failed", {"error": str(exc)})
        return None

    def clear_token_cache(self) -> None:
        with self._token_lock:
            self._token_cache = None
            self._token_cache_ts = 0.0
            self._token_fail_until = 0.0
        self._preflight_cache = {}
        self._preflight_ts = 0.0

    @staticmethod
    def _encode_key(instrument_key: str) -> str:
        return urllib.parse.quote(str(instrument_key or ""), safe="")

    _TRANSIENT_ERRNOS = (10053, 10054, 10060, 104, 32)

    def _is_transient(self, exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, ConnectionError, ConnectionResetError, ConnectionAbortedError)):
            return True
        if isinstance(exc, OSError) and getattr(exc, "winerror", getattr(exc, "errno", None)) in self._TRANSIENT_ERRNOS:
            return True
        if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
            return True
        return False

    def _request_json(self, path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 8, _retry: bool = True) -> Dict[str, Any]:
        token = self.get_token()
        if not token:
            raise RuntimeError("UPSTOX_TOKEN_MISSING")
        qs = ""
        if params:
            # Keep comma only for multiple instrument_key lists. Encode pipe. v6 kept pipe raw and caused 403.
            qs = "?" + urllib.parse.urlencode(params, doseq=True, safe=",")
        url = UPSTOX_BASE_URL.rstrip("/") + path + qs
        req = urllib.request.Request(url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}", "User-Agent": "ProjectLaddu/18.0"})
        try:
            with urllib.request.urlopen(req, timeout=max(3, min(int(timeout or 8), 10))) as res:
                body = res.read().decode("utf-8", errors="replace")
                return json.loads(body or "{}")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:1200]
            except Exception:
                pass
            raise UpstoxApiError(f"HTTP Error {exc.code}: {exc.reason}", url=url, status=exc.code, body=body)
        except Exception as exc:
            if _retry and self._is_transient(exc):
                try:
                    self.logger("WARN", "http", "Transient network error; retrying once", {"path": path, "error": str(exc)})
                except Exception:
                    pass
                time.sleep(0.4)
                return self._request_json(path, params=params, timeout=timeout, _retry=False)
            if self._is_transient(exc):
                raise UpstoxApiError(f"Transient network error: {exc}", url=url, status=None, body="")
            raise


    def brokerage_charge_snapshot(
        self,
        *,
        instrument_token: str,
        quantity: int,
        product: str,
        transaction_type: str,
        price: float,
        account_id_hash: str | None = None,
    ) -> Dict[str, Any]:
        """Fetch and freeze exact authenticated Upstox Brokerage Details evidence.

        DP-plan minimum expense is retained separately from the per-order total
        because the provider documents it as a per-scrip/day sale charge.
        """
        request = DEFAULT_BROKER_CHARGE_SNAPSHOT_AUTHORITY.normalize_request({
            "instrument_token": instrument_token,
            "quantity": quantity,
            "product": product,
            "transaction_type": transaction_type,
            "price": price,
        })
        payload = self._request_json(
            "/v2/charges/brokerage",
            request,
            timeout=7,
            _retry=False,
        )
        return DEFAULT_BROKER_CHARGE_SNAPSHOT_AUTHORITY.normalize(
            request=request,
            response=payload,
            observed_at=now_iso(),
            account_id_hash=account_id_hash,
        )

    def _iter_instrument_rows(self, text: str, url: str):
        stripped = text.lstrip()
        if url.endswith(".json.gz") or stripped.startswith("[") or stripped.startswith("{"):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                rows = parsed.get("data") or parsed.get("instruments") or parsed.get("records") or []
            else:
                rows = parsed
            for row in rows:
                if isinstance(row, dict):
                    yield row
            return
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            yield row

    def _normalise_instrument(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        instrument_key = _pick(row, "instrument_key", "instrumentKey", "instrument_token", "exchange_token")
        trading_symbol = _pick(row, "trading_symbol", "tradingsymbol", "tradingSymbol", "symbol")
        if not instrument_key or not trading_symbol:
            return None
        segment = str(_pick(row, "segment", "exchange_segment") or "").upper()
        exchange = str(_pick(row, "exchange") or "").upper()
        if not exchange:
            if segment.startswith("NSE") or str(instrument_key).startswith("NSE"):
                exchange = "NSE"
            elif segment.startswith("BSE") or str(instrument_key).startswith("BSE"):
                exchange = "BSE"
            else:
                exchange = segment.split("_")[0] if "_" in segment else segment
        if exchange.startswith("NSE"):
            exchange = "NSE"
        elif exchange.startswith("BSE"):
            exchange = "BSE"
        if not segment:
            segment = exchange
        inst_type = str(_pick(row, "instrument_type", "instrumentType", "type") or "").upper()
        option_type = str(_pick(row, "option_type", "optionType") or "").upper()
        if inst_type in ("CE", "PE") and not option_type:
            option_type = inst_type
        name = str(_pick(row, "name", "company_name", "short_name", "underlying_symbol") or "")
        strike = _safe_float(_pick(row, "strike", "strike_price"))
        return {
            "instrument_key": str(instrument_key),
            "exchange": exchange,
            "segment": segment,
            "trading_symbol": str(trading_symbol).upper(),
            "name": name,
            "instrument_type": inst_type,
            "isin": str(_pick(row, "isin") or ""),
            "expiry": _normalise_expiry(_pick(row, "expiry", "expiry_date")),
            "strike": strike,
            "option_type": option_type,
            "lot_size": int(_safe_float(_pick(row, "lot_size", "lotSize", "minimum_lot")) or 0) or None,
        }

    def _cached_instrument_meta(self, reason: str = "cache") -> Dict[str, Any]:
        meta = self.store.get_kv("instruments_meta", {}) or {}
        try:
            stats = self.store.instrument_universe_stats()
        except Exception:
            stats = {}
        merged = _merge_authoritative_instrument_meta(meta, stats, reason)
        if merged.get("loaded") and merged != meta:
            # Persist the canonical PostgreSQL revision so every readiness
            # reader sees the same authority after restart. This is bounded to
            # an actual metadata change, not a write on every readiness poll.
            self.store.set_kv("instruments_meta", merged)
        return merged

    def load_instruments(self, force: bool = False, background: bool = False) -> Dict[str, Any]:
        """Refresh the binding NSE-first/BSE-only cash catalogue atomically.

        Both NSE and BSE exchange files must be downloaded successfully before
        the previous usable catalogue is replaced.  Provider-wide complete
        masters and derivatives are never admitted into the active table.
        """
        meta = self._cached_instrument_meta("sqlite-cache")
        count = int(meta.get("count") or 0)
        revision_current = meta.get("universe_revision") == ACTIVE_UNIVERSE_REVISION
        if not force and count > 0 and revision_current:
            return meta
        now = time.time()
        if not force and self._instrument_last_attempt and now - self._instrument_last_attempt < self._instrument_cooldown_sec:
            stale = dict(meta)
            stale.update({
                "loaded": count > 0,
                "refresh_state": "cooldown",
                "next_retry_seconds": int(self._instrument_cooldown_sec - (now - self._instrument_last_attempt)),
                "universe_revision": meta.get("universe_revision"),
                "target_universe_revision": ACTIVE_UNIVERSE_REVISION,
            })
            return stale
        if not self._instrument_lock.acquire(blocking=False):
            active = dict(meta)
            active.update({"loaded": count > 0, "refresh_state": "already_running", "message": "Instrument refresh already running; using local cache"})
            return active
        try:
            self._instrument_last_attempt = time.time()
            self._instrument_refreshing = True
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            errors: List[Dict[str, Any]] = []
            provider_rows: List[Dict[str, Any]] = []
            successful_sources: List[str] = []

            for url in INSTRUMENT_URLS:
                try:
                    self.logger("INFO", "instruments", "Downloading focused exchange instrument master", {"url": url})
                    with urllib.request.urlopen(url, timeout=45) as res:
                        raw = res.read()
                    if url.endswith(".gz"):
                        raw = gzip.decompress(raw)
                    text = raw.decode("utf-8", errors="replace")
                    source_rows = 0
                    for row in self._iter_instrument_rows(text, url):
                        inst = self._normalise_instrument(row)
                        if inst:
                            provider_rows.append(inst)
                            source_rows += 1
                    if source_rows <= 0:
                        raise RuntimeError("exchange instrument file contained no usable rows")
                    successful_sources.append(url)
                except Exception as exc:
                    errors.append({"url": url, "error": str(exc)})
                    self.logger("WARN", "instruments", "Focused exchange instrument download failed", {"url": url, "error": str(exc)})

            all_sources_ready = len(successful_sources) == len(INSTRUMENT_URLS)
            active_rows: List[Dict[str, Any]] = []
            policy_stats: Dict[str, int] = {}
            if all_sources_ready:
                active_rows, policy_stats = build_active_universe(provider_rows)
                if not active_rows or not policy_stats.get("nse_equities"):
                    errors.append({"error": "focused universe policy produced no NSE equities"})
                    all_sources_ready = False

            if all_sources_ready and getattr(self.store, "universe_authority_repository", None) is not None:
                try:
                    canonical = build_canonical_universe(provider_rows, effective_date=date.today())
                    previous = getattr(self.store, "canonical_universe", None)
                    events = lifecycle_diff(previous, canonical) if isinstance(previous, CanonicalUniverse) else ()
                    self.store.universe_authority_repository.reconcile_universe(canonical, events)
                    self.store.canonical_universe = canonical
                    policy_stats.update({
                        "canonical_securities": len(canonical.securities),
                        "listing_aliases": len(canonical.listing_aliases),
                        "explicit_exclusions": len(canonical.exclusions),
                        "lifecycle_events": len(events),
                    })
                except Exception as exc:
                    errors.append({"error": f"authoritative universe reconciliation failed: {str(exc)[:240]}"})
                    all_sources_ready = False

            if all_sources_ready:
                self.store.replace_active_instruments(active_rows)
                final_stats = self.store.instrument_universe_stats()
                final_count = int(final_stats.get("active_total") or 0)
                meta = {
                    "loaded": final_count > 0,
                    "count": final_count,
                    "loaded_this_run": final_count,
                    "last_refresh": now_iso(),
                    "last_attempt": now_iso(),
                    "format": "focused-exchange-json",
                    "refresh_state": "ok",
                    "cache_usable": final_count > 0,
                    "universe_revision": ACTIVE_UNIVERSE_REVISION,
                    "universe_stats": final_stats,
                    "policy_stats": policy_stats,
                    "source_files": successful_sources,
                    "derivatives_active": False,
                    "errors": errors[-5:],
                }
            else:
                preserved = self._cached_instrument_meta("preserved-cache-after-incomplete-refresh")
                final_count = int(preserved.get("count") or 0)
                meta = dict(preserved)
                meta.update({
                    "loaded": final_count > 0,
                    "count": final_count,
                    "last_attempt": now_iso(),
                    "refresh_state": "using_existing_cache_after_incomplete_exchange_refresh" if final_count > 0 else "failed_no_complete_exchange_catalogue",
                    "cache_usable": final_count > 0,
                    "target_universe_revision": ACTIVE_UNIVERSE_REVISION,
                    "source_files": successful_sources,
                    "errors": errors[-8:],
                })
            self.store.set_kv("instruments_meta", meta)
            return meta
        finally:
            self._instrument_refreshing = False
            try:
                self._instrument_lock.release()
            except RuntimeError:
                pass

    def refresh_instruments_background(self, force: bool = False) -> Dict[str, Any]:
        meta = self._cached_instrument_meta("sqlite-cache")
        if self._instrument_refreshing:
            meta["refresh_state"] = "already_running"
            return meta
        def _run():
            try:
                self.load_instruments(force=force)
            except Exception as exc:
                self.logger("WARN", "instruments", "Background instrument refresh failed", {"error": str(exc)})
        threading.Thread(target=_run, name="ProjectLadduInstrumentRefresh", daemon=True).start()
        meta["refresh_state"] = "background_started"
        return meta

    def _ensure_instruments_nonblocking(self) -> Dict[str, Any]:
        meta = self._cached_instrument_meta("sqlite-cache")
        revision_current = meta.get("universe_revision") == ACTIVE_UNIVERSE_REVISION
        if (not meta.get("loaded") or not revision_current) and not self._instrument_refreshing:
            self.refresh_instruments_background(force=not revision_current)
        return meta

    def search_instruments(self, q: str, limit: int = 20) -> List[Dict[str, Any]]:
        self._ensure_instruments_nonblocking()
        return self.store.find_instruments(q, limit=limit)

    def search_any_instruments(self, q: str, limit: int = 20) -> List[Dict[str, Any]]:
        self._ensure_instruments_nonblocking()
        return self.store.find_any_instruments(q, limit=limit)

    def search_index_instruments(self, q: str, limit: int = 5) -> List[Dict[str, Any]]:
        self._ensure_instruments_nonblocking()
        return self.store.find_index_instruments(q, limit=limit)

    # Upstox's /v3/market-quote/ltp accepts at most this many comma-joined
    # instrument_keys per request. Widening INTELLIGENCE_SCAN_SYMBOLS beyond
    # this without chunking causes truncated or failing responses -- see
    # VALIDATION_FINDINGS_2026-07-18.md section 5.
    LTP_BATCH_LIMIT = 500

    def ltp_quotes(self, instruments: List[Dict[str, Any]], *, persist: bool = True) -> List[Dict[str, Any]]:
        if not instruments:
            return []
        keys = [i["instrument_key"] for i in instruments if i.get("instrument_key")]
        if not keys:
            return []
        data: Dict[str, Any] = {}
        for start in range(0, len(keys), self.LTP_BATCH_LIMIT):
            chunk = keys[start:start + self.LTP_BATCH_LIMIT]
            payload = self._request_json("/v3/market-quote/ltp", {"instrument_key": ",".join(chunk)}, timeout=6)
            chunk_data = payload.get("data") or {}
            if isinstance(chunk_data, dict):
                data.update(chunk_data)
        # Map using the exchange-provided instrument token.  Symbol suffix
        # matching is unsafe for similarly named securities and NIFTY-family
        # indices and must never be used for a tradable price.
        by_token: Dict[str, Dict[str, Any]] = {}
        for raw in data.values() if isinstance(data, dict) else []:
            if not isinstance(raw, dict):
                continue
            token = str(_pick(raw, "instrument_token", "instrument_key") or "").strip()
            if token:
                by_token[token] = raw
        received_at = now_iso()
        out: List[Dict[str, Any]] = []
        for inst in instruments:
            key = inst["instrument_key"]
            raw = by_token.get(str(key)) or data.get(key) or data.get(self._encode_key(key)) or {}
            if not isinstance(raw, dict) or not raw:
                continue
            returned_token = str(_pick(raw, "instrument_token", "instrument_key") or key).strip()
            if returned_token != str(key):
                continue
            ltp = _safe_float(_pick(raw, "last_price", "ltp", "lastPrice", "last_traded_price"))
            # v37.5: was ("close","prev_close","previous_close","cp") -- "close" was picked
            # BEFORE the explicit previous-close fields, so if Upstox's LTP snapshot ever
            # carried an unrelated/stale "close" value, chg used the wrong reference and
            # flipped sign (e.g. Nifty +200 pts rendering red). "cp" is Upstox's documented
            # previous-close field; prefer it, only fall back to generic "close" last.
            close = _safe_float(_pick(raw, "cp", "prev_close", "previous_close", "close"))
            chg = None
            if ltp is not None and close:
                chg = round(((ltp - close) / close) * 100, 2)
            provider_ts = _pick(
                raw, "timestamp", "last_trade_time", "last_trade_timestamp",
                "last_traded_at", "ltt",
            )
            q = {
                "instrument_key": key,
                "symbol": inst.get("trading_symbol") or raw.get("symbol") or key,
                "exchange": inst.get("exchange") or "",
                "ltp": ltp,
                "open": _safe_float(_pick(raw, "open")),
                "high": _safe_float(_pick(raw, "high")),
                "low": _safe_float(_pick(raw, "low")),
                "close": close,
                "volume": _safe_float(_pick(raw, "volume", "volume_traded", "total_buy_quantity")),
                "oi": _safe_float(_pick(raw, "oi", "open_interest")),
                "iv": _safe_float(_pick(raw, "iv", "implied_volatility")),
                "change_pct": chg,
                # V3 LTP does not guarantee a provider market timestamp. Keep
                # local receipt time separate and leave provider time empty
                # when the response omits it. The coverage lane may use this
                # observation for discovery, but only timestamped full quotes
                # may be labelled live on a decision surface.
                "timestamp": str(provider_ts or ""),
                "provider_timestamp": str(provider_ts or ""),
                "received_at": received_at,
                "source": "upstox_ltp_v3",
                "identity_verified": True,
                "raw_json": json.dumps(raw),
                "raw": raw,
            }
            if q["ltp"] is not None:
                out.append(q)
        if out and persist:
            self.store.save_quotes(out)
        return out

    def quotes(self, instruments: List[Dict[str, Any]], *, persist: bool = True) -> List[Dict[str, Any]]:
        # v65.26.12: broad coverage is quote-only and must never block on SQLite.
        return self.ltp_quotes(instruments, persist=persist)


    def option_open_interest(self, underlying_instrument_key: str, *, expiry: str = "current_month", trade_date: str | None = None) -> Dict[str, Any]:
        """Fetch aggregate and strike-level option OI for a cash/index underlying.

        This is evidence-only.  It never inserts derivatives into the active
        scanner universe and never grants broker authority.
        """
        params = {"instrument_key": str(underlying_instrument_key or "").strip(), "expiry": expiry}
        if trade_date:
            params["date"] = trade_date
        if not params["instrument_key"]:
            raise ValueError("underlying instrument_key is required")
        return self._request_json("/v2/market/oi", params, timeout=7, _retry=False)

    def option_change_open_interest(self, underlying_instrument_key: str, *, expiry: str = "current_month", trade_date: str, interval_days: int = 1) -> Dict[str, Any]:
        """Fetch strike-level change in OI for contextual cash-stock evidence."""
        key = str(underlying_instrument_key or "").strip()
        if not key:
            raise ValueError("underlying instrument_key is required")
        return self._request_json(
            "/v2/market/change-oi",
            {"instrument_key": key, "expiry": expiry, "date": trade_date, "interval": max(1, int(interval_days))},
            timeout=7,
            _retry=False,
        )

    def option_chain(self, underlying_instrument_key: str, *, expiry: str = "current_month") -> Dict[str, Any]:
        """Fetch the provider option-chain snapshot for Greeks/IV/liquidity context."""
        key = str(underlying_instrument_key or "").strip()
        if not key:
            raise ValueError("underlying instrument_key is required")
        return self._request_json(
            "/v2/option/chain",
            {"instrument_key": key, "expiry_date": expiry},
            timeout=7,
            _retry=False,
        )

    def full_quotes(self, instruments: List[Dict[str, Any]], *, persist: bool = False) -> List[Dict[str, Any]]:
        """Fetch verified exchange snapshots for the small visible-quote set.

        The broad universe stays on V3 LTP for throughput.  Visible dashboard
        prices use the full-quote endpoint because it returns instrument_token,
        feed timestamp and last_trade_time, allowing identity and freshness to be
        verified before a value is labelled live.
        """
        if not instruments:
            return []
        keys = [str(i.get("instrument_key") or "").strip() for i in instruments]
        keys = [k for k in keys if k]
        if not keys:
            return []
        payload = self._request_json("/v2/market-quote/quotes", {"instrument_key": ",".join(keys)}, timeout=5, _retry=False)
        data = (payload or {}).get("data") or {}
        if not isinstance(data, dict):
            return []

        # Resolve by the exchange-provided instrument token only.  A suffix
        # match can silently bind NIFTY 50 to another NIFTY family instrument or
        # one equity to a similarly named security; live prices must never use it.
        by_token: Dict[str, Dict[str, Any]] = {}
        for raw in data.values():
            if not isinstance(raw, dict):
                continue
            token = str(_pick(raw, "instrument_token", "instrument_key") or "").strip()
            if token:
                by_token[token] = raw

        received_at = now_iso()
        out: List[Dict[str, Any]] = []
        for inst in instruments:
            key = str(inst.get("instrument_key") or "").strip()
            raw = by_token.get(key)
            if not raw:
                continue
            ohlc = raw.get("ohlc") or {}
            ltp = _safe_float(_pick(raw, "last_price", "ltp", "last_traded_price"))
            day_change = _full_quote_day_change(raw, ohlc, ltp)
            previous_close = day_change["previous_close"]
            provider_ts = _pick(raw, "timestamp", "last_trade_time")
            q = {
                "instrument_key": key,
                "symbol": str(inst.get("trading_symbol") or raw.get("symbol") or key).upper(),
                "exchange": inst.get("exchange") or "",
                "ltp": ltp,
                "open": _safe_float(_pick(ohlc, "open")),
                "high": _safe_float(_pick(ohlc, "high")),
                "low": _safe_float(_pick(ohlc, "low")),
                "close": previous_close,
                "previous_close": previous_close,
                "session_close": day_change["session_close"],
                "volume": _safe_float(_pick(raw, "volume", "volume_traded")),
                "oi": _safe_float(_pick(raw, "oi", "open_interest")),
                "change_pct": day_change["change_pct"],
                "rupee_change": day_change["rupee_change"],
                "change_source": day_change["change_source"],
                "timestamp": str(provider_ts or ""),
                "provider_timestamp": str(provider_ts or ""),
                "received_at": received_at,
                "source": "upstox_full_quote_v2",
                "identity_verified": True,
                "raw_json": json.dumps(raw),
                "raw": raw,
            }
            if q["ltp"] is not None:
                out.append(q)
        if out and persist:
            self.store.save_quotes(out)
        return out

    def selected_full_quote(self, instrument: Dict[str, Any]) -> Dict[str, Any]:
        """Verified full quote for one explicitly selected instrument.

        The response is accepted only when Upstox returns the exact requested
        instrument token.  Display-name/suffix matching is prohibited because
        it can bind an equity or index to a similarly named instrument.  The
        provider timestamp is preserved so the runtime can decide whether the
        snapshot is live, closed-market, or stale.
        """
        key = str((instrument or {}).get("instrument_key") or "").strip()
        if not key:
            return {"ok": False, "state": "instrument_key_missing", "identity_verified": False}
        payload = self._request_json(
            "/v2/market-quote/quotes",
            {"instrument_key": key},
            timeout=5,
            _retry=False,
        )
        data = (payload or {}).get("data") or {}
        raw = None
        if isinstance(data, dict):
            for value in data.values():
                if not isinstance(value, dict):
                    continue
                returned_token = str(_pick(value, "instrument_token", "instrument_key") or "").strip()
                if returned_token == key:
                    raw = value
                    break
        if not isinstance(raw, dict):
            return {"ok": False, "state": "quote_unavailable_or_identity_mismatch", "identity_verified": False}
        ohlc = raw.get("ohlc") or {}
        depth = raw.get("depth") or {}
        buys = [row for row in (depth.get("buy") or []) if isinstance(row, dict) and _safe_float(row.get("price"))]
        sells = [row for row in (depth.get("sell") or []) if isinstance(row, dict) and _safe_float(row.get("price"))]
        buy_qty = sum(_safe_float(row.get("quantity")) or 0 for row in buys)
        sell_qty = sum(_safe_float(row.get("quantity")) or 0 for row in sells)
        total_depth = buy_qty + sell_qty
        imbalance = round((buy_qty - sell_qty) * 100.0 / total_depth, 1) if total_depth else None
        best_bid = _safe_float(buys[0].get("price")) if buys else None
        best_ask = _safe_float(sells[0].get("price")) if sells else None
        spread = round(best_ask - best_bid, 2) if best_bid is not None and best_ask is not None else None
        ltp = _safe_float(_pick(raw, "last_price", "ltp", "last_traded_price"))
        day_change = _full_quote_day_change(raw, ohlc, ltp)
        provider_ts = _pick(raw, "timestamp", "last_trade_time")
        received_at = now_iso()
        return {
            "ok": ltp is not None,
            "state": "verified_snapshot_received" if ltp is not None else "price_missing",
            "instrument_key": key,
            "symbol": str((instrument or {}).get("trading_symbol") or raw.get("symbol") or key).upper(),
            "ltp": ltp,
            "open": _safe_float(ohlc.get("open")),
            "high": _safe_float(ohlc.get("high")),
            "low": _safe_float(ohlc.get("low")),
            "previous_close": day_change["previous_close"],
            "session_close": day_change["session_close"],
            "rupee_change": day_change["rupee_change"],
            "change_pct": day_change["change_pct"],
            "change_source": day_change["change_source"],
            "volume": _safe_float(_pick(raw, "volume", "volume_traded")),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "buy_quantity": buy_qty or None,
            "sell_quantity": sell_qty or None,
            "depth_imbalance_pct": imbalance,
            "depth": {"buy": buys[:5], "sell": sells[:5]},
            "timestamp": str(provider_ts or ""),
            "provider_timestamp": str(provider_ts or ""),
            "received_at": received_at,
            "identity_verified": True,
            "source": "upstox_full_market_quote_v2",
        }

    @staticmethod
    def _historical_unit_interval(interval: str) -> Tuple[str, str]:
        from core.timeframe import Timeframe, parse_timeframe_strict, spec
        tf = parse_timeframe_strict(interval)
        if tf == Timeframe.D1:
            return ("days", "1")
        if tf == Timeframe.W1:
            return ("weeks", "1")
        if tf == Timeframe.MN1:
            return ("months", "1")
        minutes = spec(tf).minutes
        return ("minutes", str(int(minutes or 1)))

    # v35.5: server-side candle cache. Avoids re-fetching full history from
    # Upstox on every scan; each (instrument, interval, days) bucket is cached
    # with a TTL scaled to bar size (minute bars go stale fast, daily bars don't).
    _candle_cache: Dict[Tuple[str, str, int], Tuple[float, List[Dict[str, Any]]]] = {}
    _candle_cache_lock = threading.Lock()

    @staticmethod
    def _candle_cache_ttl(interval: str) -> float:
        unit, _ = UpstoxClient._historical_unit_interval(interval)
        if unit == "minutes":
            return 60.0        # live intraday bars: 1 minute
        if unit == "weeks":
            return 3600.0 * 6  # 6 hours
        if unit == "months":
            return 3600.0 * 12
        return 1800.0          # daily bars: 30 minutes

    @classmethod
    def historical_max_window_days(cls, interval: str) -> int:
        """Provider-valid maximum date span for one V3 history request.

        Upstox V3 permits at most one month for minute intervals up to 15m,
        one quarter for larger minute/hour intervals, and one decade for daily
        rows.  Keep a small calendar safety margin so month length and inclusive
        date boundaries never turn an otherwise valid request into UDAPI1148.
        """
        unit, value = cls._historical_unit_interval(interval)
        try:
            n = int(value or 1)
        except Exception:
            n = 1
        if unit == "minutes":
            return 28 if n <= 15 else 85
        if unit == "hours":
            return 85
        if unit == "days":
            return 3650
        return 36500

    @classmethod
    def effective_historical_days(cls, interval: str, requested_days: int | None) -> int:
        requested = max(1, int(requested_days or 20))
        return min(requested, cls.historical_max_window_days(interval))

    def historical_candles(self, instrument_key: str, interval: str, days: int = 20) -> List[Dict[str, Any]]:
        effective_days = self.effective_historical_days(interval, days)
        cache_key = (instrument_key, interval, effective_days)
        ttl = self._candle_cache_ttl(interval)
        now = time.time()
        with self._candle_cache_lock:
            hit = self._candle_cache.get(cache_key)
            if hit and (now - hit[0]) < ttl:
                return hit[1]
        result = self._historical_candles_fetch(instrument_key, interval, days)
        with self._candle_cache_lock:
            self._candle_cache[cache_key] = (now, result)
        return result

    def historical_candles_range(self, instrument_key: str, interval: str, *, before_date: str, days: int | None = None) -> List[Dict[str, Any]]:
        """Fetch one provider-valid historical window strictly before a date.

        Used by interactive chart left-pan backfill.  It never walks multiple
        windows on the request thread; callers schedule one bounded chunk in a
        background worker and may request another chunk only after it lands.
        """
        try:
            boundary = date.fromisoformat(str(before_date or "")[:10])
        except Exception as exc:
            raise ValueError("before_date must be YYYY-MM-DD") from exc
        to_date = boundary - timedelta(days=1)
        requested_days = max(1, int(days or self.historical_max_window_days(interval)))
        effective_days = self.effective_historical_days(interval, requested_days)
        from_date = to_date - timedelta(days=effective_days)
        safe_key = self._encode_key(instrument_key)
        unit, interval_value = self._historical_unit_interval(interval)
        path = f"/v3/historical-candle/{safe_key}/{unit}/{interval_value}/{to_date.isoformat()}/{from_date.isoformat()}"
        payload = self._request_json(path, None, timeout=9)
        raw_candles = (((payload or {}).get("data") or {}).get("candles") or [])
        out = []
        for c in raw_candles:
            if isinstance(c, list) and len(c) >= 6:
                out.append({
                    "timestamp": c[0], "open": _safe_float(c[1]), "high": _safe_float(c[2]),
                    "low": _safe_float(c[3]), "close": _safe_float(c[4]), "volume": _safe_float(c[5]),
                    "oi": _safe_float(c[6]) if len(c) > 6 else None,
                    "source": "upstox_chart_backfill", "requested_days": requested_days,
                    "provider_window_days": effective_days,
                })
            elif isinstance(c, dict):
                row = dict(c)
                row.update({"source": "upstox_chart_backfill", "requested_days": requested_days, "provider_window_days": effective_days})
                out.append(row)
        out.reverse()
        return out

    def historical_candles_exact_range(
        self,
        instrument_key: str,
        interval: str,
        *,
        from_date: str,
        to_date: str,
    ) -> List[Dict[str, Any]]:
        """Fetch one governed missing range without failing on closed sessions.

        Upstox can reject a dated request when ``to_date`` is a weekend,
        exchange holiday, or an unsettled current session.  The older generic
        historical method already retried the previous date, but the exact-gap
        pipeline did not.  That asymmetry left cold-cache index charts such as
        NIFTY PHARMA permanently pending after a weekend install.

        We preserve the requested start and walk the end boundary backwards for
        at most seven calendar days, skipping weekends.  The returned bars keep
        both requested and provider date boundaries so coverage/audit code never
        mistakes a provider fallback for a different request.
        """
        try:
            start = date.fromisoformat(str(from_date or "")[:10])
            requested_end = date.fromisoformat(str(to_date or "")[:10])
        except Exception as exc:
            raise ValueError("from_date and to_date must be YYYY-MM-DD") from exc
        if requested_end < start:
            raise ValueError("to_date must not precede from_date")
        maximum = self.historical_max_window_days(interval)
        if (requested_end - start).days > maximum:
            raise ValueError(f"exact range exceeds provider maximum of {maximum} days")

        safe_key = self._encode_key(instrument_key)
        unit, interval_value = self._historical_unit_interval(interval)
        last_provider_error = None
        raw_candles = []
        provider_end = requested_end

        # Seven days covers weekends plus ordinary exchange holidays while
        # remaining strictly bounded.  A true identity/provider failure is
        # re-raised after the bounded fallback set is exhausted.
        for shift in range(0, 8):
            candidate_end = requested_end - timedelta(days=shift)
            if candidate_end < start:
                break
            if candidate_end.weekday() >= 5:
                continue
            path = (
                f"/v3/historical-candle/{safe_key}/{unit}/{interval_value}/"
                f"{candidate_end.isoformat()}/{start.isoformat()}"
            )
            try:
                payload = self._request_json(path, None, timeout=9)
                candidate_rows = (((payload or {}).get("data") or {}).get("candles") or [])
                if candidate_rows:
                    raw_candles = candidate_rows
                    provider_end = candidate_end
                    break
            except UpstoxApiError as exc:
                last_provider_error = exc
                if exc.status not in (400, 403, 404):
                    raise
                continue

        if not raw_candles and last_provider_error is not None:
            raise last_provider_error

        out = []
        metadata = {
            "source": "upstox_exact_gap_v3",
            "requested_from": start.isoformat(),
            "requested_to": requested_end.isoformat(),
            "provider_from": start.isoformat(),
            "provider_to": provider_end.isoformat(),
            "closed_session_fallback_days": max(0, (requested_end - provider_end).days),
        }
        for candle in raw_candles:
            if isinstance(candle, list) and len(candle) >= 6:
                out.append({
                    "timestamp": candle[0], "open": _safe_float(candle[1]),
                    "high": _safe_float(candle[2]), "low": _safe_float(candle[3]),
                    "close": _safe_float(candle[4]), "volume": _safe_float(candle[5]),
                    "oi": _safe_float(candle[6]) if len(candle) > 6 else None,
                    **metadata,
                })
            elif isinstance(candle, dict):
                row = dict(candle)
                row.update(metadata)
                out.append(row)
        out.reverse()
        return out

    def intraday_candles(self, instrument_key: str, interval: str = "5minute") -> List[Dict[str, Any]]:
        """Current-session OHLC from the Upstox V3 intraday endpoint.

        The dated historical endpoint commonly ends at the previous session
        during market hours.  It must not be used as the live candle source.
        """
        safe_key = self._encode_key(instrument_key)
        unit, interval_value = self._historical_unit_interval(interval)
        if unit not in ("minutes", "hours", "days"):
            unit, interval_value = "minutes", "5"
        path = f"/v3/historical-candle/intraday/{safe_key}/{unit}/{interval_value}"
        payload = self._request_json(path, None, timeout=7)
        raw_candles = (((payload or {}).get("data") or {}).get("candles") or [])
        out = []
        for c in raw_candles:
            if isinstance(c, list) and len(c) >= 6:
                out.append({"timestamp": c[0], "open": _safe_float(c[1]), "high": _safe_float(c[2]), "low": _safe_float(c[3]), "close": _safe_float(c[4]), "volume": _safe_float(c[5]), "oi": _safe_float(c[6]) if len(c) > 6 else None, "source": "upstox_intraday_v3"})
            elif isinstance(c, dict):
                row = dict(c); row["source"] = "upstox_intraday_v3"; out.append(row)
        out.reverse()
        return out

    def deep_backfill_daily_candles(self, instrument_key: str, years: int = PREFERRED_RESEARCH_YEARS, window_days: int = 380,
                                    max_seconds: float = 20.0, request_guard: Callable[[], Any] | None = None) -> int:
        """Incrementally extend local daily history towards a 10-15 year target.

        Each cycle resumes *before the earliest locally persisted candle*.  The
        previous implementation restarted from today on every invocation, so
        repeated background cycles re-downloaded the same recent windows and
        never reliably extended the store backwards.  This method is bounded by
        ``max_seconds`` and records exact coverage/backfill progress.
        """
        target_years = max(1, int(years or PREFERRED_RESEARCH_YEARS))
        chunk_days = max(30, int(window_days or 380))
        target_start = date.today() - timedelta(days=int(target_years * 365.2425))
        to_date = date.today()
        readiness_service = None
        if self.store is not None:
            try:
                readiness_service = HistoricalDataReadinessService(self.store.conn)
                coverage = self.store.candle_coverage(instrument_key, "1d") or {}
                first_raw = str(coverage.get("first") or "")[:10]
                if first_raw:
                    earliest = date.fromisoformat(first_raw)
                    if earliest <= target_start:
                        readiness_service.mark_backfill(
                            instrument_key=instrument_key, interval="1d", target_years=target_years,
                            target_start_date=target_start.isoformat(), next_to_date=None, state="COMPLETE",
                        )
                        return 0
                    to_date = earliest - timedelta(days=1)
            except Exception:
                readiness_service = None

        if to_date < target_start:
            return 0
        safe_key = self._encode_key(instrument_key)
        unit, interval_value = self._historical_unit_interval("day")
        total_saved = 0
        windows_completed = 0
        started = time.monotonic()
        remaining_days = max(1, (to_date - target_start).days)
        max_windows = max(1, int((remaining_days + chunk_days - 1) / chunk_days))
        last_error = ""
        state = "RUNNING"
        for _ in range(max_windows):
            if time.monotonic() - started > max_seconds:
                state = "PAUSED_BUDGET"
                break
            from_date = max(target_start, to_date - timedelta(days=chunk_days))
            path = f"/v3/historical-candle/{safe_key}/{unit}/{interval_value}/{to_date.isoformat()}/{from_date.isoformat()}"
            try:
                if request_guard is None:
                    payload = self._request_json(path, None, timeout=10, _retry=False)
                else:
                    with request_guard():
                        payload = self._request_json(path, None, timeout=10, _retry=False)
            except Exception as exc:
                last_error = str(exc)
                state = "PAUSED_ERROR"
                break
            raw_candles = (((payload or {}).get("data") or {}).get("candles") or [])
            if not raw_candles:
                # Empty windows can occur before listing.  Stop rather than
                # manufacturing coverage or hammering the provider.
                state = "COMPLETE_PROVIDER_DEPTH"
                break
            parsed = []
            for c in raw_candles:
                if isinstance(c, list) and len(c) >= 6:
                    parsed.append({"timestamp": c[0], "open": _safe_float(c[1]), "high": _safe_float(c[2]),
                                    "low": _safe_float(c[3]), "close": _safe_float(c[4]), "volume": _safe_float(c[5]),
                                    "oi": _safe_float(c[6]) if len(c) > 6 else None, "source": "upstox_deep_backfill"})
                elif isinstance(c, dict):
                    row = dict(c)
                    row["source"] = "upstox_deep_backfill"
                    parsed.append(row)
            if parsed and self.store is not None:
                total_saved += self.store.save_candles(instrument_key, "day", parsed, source="upstox_deep_backfill")
            windows_completed += 1
            if from_date <= target_start:
                state = "COMPLETE"
                to_date = target_start - timedelta(days=1)
                break
            to_date = from_date - timedelta(days=1)
            time.sleep(0.3)
        else:
            state = "COMPLETE" if to_date < target_start else "PAUSED_BUDGET"

        if readiness_service is not None:
            try:
                readiness_service.mark_backfill(
                    instrument_key=instrument_key, interval="1d", target_years=target_years,
                    target_start_date=target_start.isoformat(),
                    next_to_date=to_date.isoformat() if to_date >= target_start else None,
                    state=state, rows_saved_delta=total_saved, windows_delta=windows_completed, error=last_error,
                )
            except Exception:
                pass
        return total_saved

    def _historical_candles_fetch(self, instrument_key: str, interval: str, days: int = 20) -> List[Dict[str, Any]]:
        requested_days = max(int(days or 20), 1)
        effective_days = self.effective_historical_days(interval, requested_days)
        to_date = date.today()
        from_date = to_date - timedelta(days=effective_days)
        safe_key = self._encode_key(instrument_key)
        unit, interval_value = self._historical_unit_interval(interval)
        path = f"/v3/historical-candle/{safe_key}/{unit}/{interval_value}/{to_date.isoformat()}/{from_date.isoformat()}"
        try:
            payload = self._request_json(path, None, timeout=9)
        except UpstoxApiError as exc:
            # Some non-trading days/pre-EOD-settlement windows reject today as to_date.
            # Retry with previous date before failing. v32.4: this used to exclude
            # unit=="minutes", so 1-minute/Intraday 5-minute fetches had zero
            # fallback and just raised straight through — market_structure()/
            # volume_profile() then saw candles=[] and reported "pending"/"missing"
            # forever for those modes, even on symbols the fast lane did reach.
            # Minute-unit gets the same one-shot previous-date retry now.
            if exc.status in (400, 403, 404):
                retry_to = to_date - timedelta(days=1)
                retry_from = retry_to - timedelta(days=effective_days)
                retry_path = f"/v3/historical-candle/{safe_key}/{unit}/{interval_value}/{retry_to.isoformat()}/{retry_from.isoformat()}"
                payload = self._request_json(retry_path, None, timeout=9)
            else:
                raise
        raw_candles = (((payload or {}).get("data") or {}).get("candles") or [])
        out = []
        # Upstox returns newest-first. Normalize oldest-first for indicators.
        for c in raw_candles:
            if isinstance(c, list) and len(c) >= 6:
                out.append({"timestamp": c[0], "open": _safe_float(c[1]), "high": _safe_float(c[2]), "low": _safe_float(c[3]), "close": _safe_float(c[4]), "volume": _safe_float(c[5]), "oi": _safe_float(c[6]) if len(c) > 6 else None,
                            "requested_days": requested_days, "provider_window_days": effective_days,
                            "provider_window_clamped": effective_days < requested_days})
            elif isinstance(c, dict):
                row = dict(c)
                row.setdefault("requested_days", requested_days)
                row.setdefault("provider_window_days", effective_days)
                row.setdefault("provider_window_clamped", effective_days < requested_days)
                out.append(row)
        out.reverse()
        return out


    @staticmethod
    def _ratio_num(v):
        try:
            if v in (None, "", "NA", "N/A"):
                return None
            return float(str(v).replace("%", "").replace(",", ""))
        except Exception:
            return None

    @staticmethod
    def _range_score(v, good, bad, higher=True):
        if v is None:
            return None
        v = float(v)
        if higher:
            if v >= good: return 100
            if v <= bad: return 0
            return int((v - bad) / (good - bad) * 100)
        if v <= good: return 100
        if v >= bad: return 0
        return int((bad - v) / (bad - good) * 100)

    def fundamentals_snapshot(self, isin: str, *, request_guard: Callable[[], Any] | None = None,
                              max_workers: int = 2, request_retry: bool = False) -> Dict[str, Any]:
        """Use current Upstox Fundamentals APIs by ISIN where permitted.

        Every endpoint request can be wrapped in ``request_guard`` so the real
        socket count participates in the shared RateController.  Older builds
        acquired one outer slot and then opened up to eight sockets inside it,
        starving chart/MTF requests and defeating the concurrency cap.
        """
        isin = str(isin or "").strip().upper()
        if not isin:
            return {"ok": False, "state": "missing", "reason": "ISIN missing", "source": "upstox_fundamentals_api"}
        errors = []
        profile = {}; ratios_raw = []; holdings_raw = []; income = {}; balance = {}; cashflow = {}
        q = urllib.parse.quote(isin, safe="")

        def guarded_request(path: str, params: Optional[Dict[str, Any]], timeout: int):
            if request_guard is None:
                return self._request_json(path, params, timeout, _retry=request_retry)
            with request_guard():
                return self._request_json(path, params, timeout, _retry=request_retry)

        # v65.8: these 6 calls (profile, key-ratios, share-holdings, and the
        # consolidated try for each of the 3 statements) used to run one at a
        # time -- up to 9 sequential HTTP round trips on a cold cache, each
        # paying its own connect+TLS+response latency, which is what made
        # Refresh Stock feel slow. They don't depend on each other (each is
        # its own ISIN-keyed endpoint), so fire them together and only
        # serialize the standalone retry for whichever statements came back
        # empty under "consolidated". Same endpoints, same fallback
        # semantics, same downstream scoring -- only the fetch phase is
        # parallel now.
        statement_type: Dict[str, str] = {}
        simple_specs = {
            "profile": (f"/v2/fundamentals/{q}/profile", None, 6),
            "key-ratios": (f"/v2/fundamentals/{q}/key-ratios", None, 6),
            "share-holdings": (f"/v2/fundamentals/{q}/share-holdings", None, 6),
        }
        stmt_specs = {
            "income": ("income-statement", "income", 7),
            "balance": ("balance-sheet", "balance", 7),
            "cashflow": ("cash-flow", "cashflow", 7),
        }
        def statement_params(target: str, statement_type_value: str) -> Dict[str, Any]:
            params: Dict[str, Any] = {"type": statement_type_value, "fs": "false"}
            if target == "income":
                params["time_period"] = "yearly"
            return params
        with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers or 2), 3)), thread_name_prefix="LadduFundFetch") as pool:
            futures = {}
            for name, (path, params, timeout) in simple_specs.items():
                futures[pool.submit(guarded_request, path, params, timeout)] = ("simple", name)
            for target, (endpoint, _target, timeout) in stmt_specs.items():
                path = f"/v2/fundamentals/{q}/{endpoint}"
                futures[pool.submit(guarded_request, path, statement_params(target, "consolidated"), timeout)] = ("stmt", target, endpoint, "consolidated")

            standalone_retries = []  # (target, endpoint, timeout)
            for fut in as_completed(futures):
                spec = futures[fut]
                try:
                    data = fut.result().get("data")
                except Exception as exc:
                    if spec[0] == "simple":
                        errors.append({"endpoint": spec[1], "error": str(exc)})
                    else:
                        _, target, endpoint, _ftype = spec
                        errors.append({"endpoint": endpoint, "type": "consolidated", "error": str(exc)})
                        standalone_retries.append((target, endpoint, stmt_specs[target][2]))
                    continue
                if spec[0] == "simple":
                    name = spec[1]
                    if name == "profile": profile = data or {}
                    elif name == "key-ratios": ratios_raw = data or []
                    else: holdings_raw = data or []
                else:
                    _, target, endpoint, _ftype = spec
                    if data:
                        statement_type[target] = "consolidated"
                        if target == "income": income = data
                        elif target == "balance": balance = data
                        else: cashflow = data
                    else:
                        standalone_retries.append((target, endpoint, stmt_specs[target][2]))

            if standalone_retries:
                retry_futures = {}
                for target, endpoint, timeout in standalone_retries:
                    path = f"/v2/fundamentals/{q}/{endpoint}"
                    retry_futures[pool.submit(guarded_request, path, statement_params(target, "standalone"), timeout)] = (target, endpoint)
                for fut in as_completed(retry_futures):
                    target, endpoint = retry_futures[fut]
                    try:
                        data = fut.result().get("data") or {}
                    except Exception as exc:
                        errors.append({"endpoint": endpoint, "type": "standalone", "error": str(exc)})
                        continue
                    if data:
                        statement_type[target] = "standalone"
                        if target == "income": income = data
                        elif target == "balance": balance = data
                        else: cashflow = data
        ratios = {}
        sector_ratios = {}
        for r in ratios_raw if isinstance(ratios_raw, list) else []:
            name = str(r.get("name") or "").upper().replace("/", "_").replace(" ", "_")
            ratios[name] = self._ratio_num(r.get("company_value"))
            sector_ratios[name] = self._ratio_num(r.get("sector_value"))
        roe = ratios.get("ROE"); roce = ratios.get("ROCE"); roa = ratios.get("ROA")
        pe = ratios.get("P_E"); pb = ratios.get("P_B"); ev = ratios.get("EV_EBITDA")
        parts_quality = [self._range_score(roe,18,6), self._range_score(roce,20,7), self._range_score(roa,10,2)]
        sector_classification = DEFAULT_SECTOR_CLASSIFICATION_AUTHORITY.classify(
            profile.get("sector"), profile.get("industry")
        )
        sector_policy = sector_classification["fundamental_policy"]
        sector_pe, sector_pb, sector_ev = sector_ratios.get("P_E"), sector_ratios.get("P_B"), sector_ratios.get("EV_EBITDA")
        def relative_value(company, sector, absolute_good, absolute_bad):
            if company is None: return None
            if sector and sector > 0:
                return self._range_score(company / sector, 0.85, 1.55, higher=False)
            return self._range_score(company, absolute_good, absolute_bad, higher=False)
        if sector_policy in ("banking", "nbfc"):
            parts_quality = [self._range_score(roe,16,6), self._range_score(roa,1.5,0.35)]
            parts_val = [relative_value(pe, sector_pe, 18, 45), relative_value(pb, sector_pb, 2.5, 8)]
        elif sector_policy in ("insurance", "asset_light_financial_services"):
            parts_quality = [self._range_score(roe,18,7), self._range_score(roce,20,8), self._range_score(roa,10,3)]
            parts_val = [relative_value(pe, sector_pe, 24, 65), relative_value(pb, sector_pb, 5, 15)]
        elif sector_policy == "asset_light_services":
            parts_quality = [self._range_score(roe,20,8), self._range_score(roce,22,9), self._range_score(roa,12,4)]
            parts_val = [relative_value(pe, sector_pe, 24, 65), relative_value(ev, sector_ev, 14, 38)]
        elif sector_policy == "utilities_infrastructure":
            parts_quality = [self._range_score(roe,14,5), self._range_score(roce,13,5), self._range_score(roa,6,1.5)]
            parts_val = [relative_value(pe, sector_pe, 16,45), relative_value(pb, sector_pb,2.5,8), relative_value(ev, sector_ev,10,28)]
        elif sector_policy == "consumer":
            parts_quality = [self._range_score(roe,20,8), self._range_score(roce,22,9), self._range_score(roa,10,3)]
            parts_val = [relative_value(pe, sector_pe, 28,70), relative_value(ev, sector_ev,16,42)]
        else:
            parts_val = [relative_value(pe, sector_pe,18,60), relative_value(pb, sector_pb,3,12), relative_value(ev, sector_ev,8,30)]
        def avg(xs):
            xs=[x for x in xs if x is not None]
            return round(sum(xs)/len(xs),1) if xs else None
        quality=avg(parts_quality); valuation=avg(parts_val)
        holding_summary = {}
        institutional_delta = None
        holding_aliases = {
            "foreign_institutional_investors": "fii", "foreign_investors": "fii", "fpi": "fii", "fiis": "fii",
            "domestic_institutional_investors": "other_dii", "dii": "other_dii", "diis": "other_dii",
            "mutual_fund": "mutual_funds", "mutual_funds": "mutual_funds", "mf": "mutual_funds",
        }
        for h in holdings_raw if isinstance(holdings_raw, list) else []:
            cat = re.sub(r"[^a-z0-9]+", "_", str(h.get("category") or "").strip().lower()).strip("_")
            cat = holding_aliases.get(cat, cat)
            hist = h.get("history") or []
            if hist:
                latest = self._ratio_num(hist[0].get("value"))
                prev = self._ratio_num(hist[1].get("value")) if len(hist)>1 else None
                holding_summary[cat] = {"latest": latest, "previous": prev, "delta": round(latest-prev,2) if latest is not None and prev is not None else None, "period": hist[0].get("period")}
        institutional_delta = sum([holding_summary.get(k,{}).get("delta") or 0 for k in ("fii","other_dii","mutual_funds")]) if holding_summary else None
        inst_score = None if institutional_delta is None else max(0, min(100, int(50 + institutional_delta*10)))
        def category_history(payload, key, category):
            rows = payload.get(key) or []
            hit = next((x for x in rows if str(x.get("category") or "").lower().replace(" ", "_") == category), {})
            return hit.get("history") or []
        def change_score(history):
            changes = []
            for row in history[:4]:
                try: changes.append(float(str(row.get("change") or "").replace("%", "").replace("+", "")))
                except Exception: pass
            return None if not changes else round(max(0, min(100, 50 + sum(changes) / len(changes) * 2)), 1)
        def mean_change(history):
            changes = []
            for row in history[:4]:
                try: changes.append(float(str(row.get("change") or "").replace("%", "").replace("+", "")))
                except Exception: pass
            return None if not changes else round(sum(changes) / len(changes), 4)
        revenue_hist = category_history(income, "income_statement", "revenue")
        profit_hist = category_history(income, "income_statement", "net_profit")
        growth_parts = [x for x in (change_score(revenue_hist), change_score(profit_hist)) if x is not None]
        growth = round(sum(growth_parts) / len(growth_parts), 1) if growth_parts else None
        balance_hist = balance.get("history") or []
        latest_balance = balance_hist[0] if balance_hist else {}
        assets, liabilities = self._ratio_num(latest_balance.get("total_asset")), self._ratio_num(latest_balance.get("total_liability"))
        leverage_score = None if not assets or liabilities is None else max(0, min(100, round((1 - liabilities / assets) * 140, 1)))
        operating_hist = category_history(cashflow, "cash_flow", "operating")
        positive_cfo = None if not operating_hist else round(sum(1 for x in operating_hist[:4] if (self._ratio_num(x.get("value")) or 0) > 0) / min(4, len(operating_hist)) * 100, 1)
        if sector_policy in ("banking", "nbfc"):
            # Deposits and borrowings are operating inputs for financial companies;
            # generic liabilities/assets and industrial CFO tests are misleading.
            safety_parts = [x for x in (self._range_score(roa, 1.5, 0.35), self._range_score(roe, 15, 5)) if x is not None]
        elif sector_policy == "utilities_infrastructure":
            safety_parts = [x for x in (leverage_score, positive_cfo) if x is not None]
            if leverage_score is not None: safety_parts.append(min(100, leverage_score + 12))
        else:
            safety_parts = [x for x in (leverage_score, positive_cfo) if x is not None]
        safety = round(sum(safety_parts) / len(safety_parts), 1) if safety_parts else None
        provider_dimensions = {"quality": quality, "growth": growth, "safety": safety, "valuation": valuation}
        normalized = DEFAULT_FUNDAMENTAL_DIMENSION_AUTHORITY.normalize(
            {
                "roe": roe, "roce": roce, "roa": roa,
                "pe": pe, "pb": pb, "ev_ebitda": ev,
                "sales_growth": mean_change(revenue_hist),
                "profit_growth": mean_change(profit_hist),
                "total_assets": assets, "total_liabilities": liabilities,
                "positive_cfo_ratio": positive_cfo,
            },
            sector=profile.get("sector") or profile.get("industry"),
            sector_benchmarks={"pe": sector_pe, "pb": sector_pb, "ev_ebitda": sector_ev},
        )
        quality = normalized["dimensions"].get("quality")
        growth = normalized["dimensions"].get("growth")
        safety = normalized["dimensions"].get("safety")
        valuation = normalized["dimensions"].get("valuation")
        coverage = {"profile": bool(profile), "key_ratios": len(ratios), "quality_metrics": normalized["dimension_counts"].get("quality", 0), "valuation_metrics": normalized["dimension_counts"].get("valuation", 0), "shareholding_categories": len(holding_summary), "income_periods": max(len(revenue_hist), len(profit_hist)), "balance_periods": len(balance_hist), "cashflow_periods": len(operating_hist), "dimension_counts": normalized["dimension_counts"], "minimum_counts": normalized["minimum_counts"], "dimension_authority": normalized["authority"], "dimension_authority_version": normalized["authority_version"]}
        if not profile and not ratios and not holding_summary:
            return {"ok": False, "state": "source_unavailable", "score": None, "coverage": coverage, "reason": "Live fundamental source returned no company data; Delivery promotion remains blocked", "source": "upstox_fundamentals_api", "fetched_at": now_iso(), "errors": errors[-5:]}
        if not normalized["ok"]:
            dimensions = dict(normalized["dimensions"])
            resolved_dimensions = [name for name, value in dimensions.items() if value is not None]
            missing_dimensions = list(normalized["insufficient_dimensions"])
            return {
                "ok": False, "state": "incomplete", "score": None,
                "quality": quality, "valuation": valuation, "growth": growth, "safety": safety,
                "pe": pe, "pb": pb, "roe": roe, "roce": roce, "roa": roa,
                "sector": profile.get("sector"), "profile": profile.get("company_profile"),
                "shareholding": holding_summary, "institutional_delta": institutional_delta,
                "coverage": coverage, "resolved_dimensions": resolved_dimensions,
                "missing_dimensions": missing_dimensions, "sector_policy": sector_policy,
                "statement_type": statement_type,
                "reason": "Verified partial fundamentals loaded; final score withheld until " + ", ".join(missing_dimensions) + " evidence is available",
                "source": "upstox_fundamentals_api", "fetched_at": now_iso(), "errors": errors[-8:],
            }
        # Provider-specific policy remains diagnostic evidence only.  The final
        # Laddu score is produced by one source-independent authority.  FII/DII
        # ownership change is intentionally *not* folded into fundamentals; it
        # belongs to Participation and otherwise gets double-counted.
        provider_score_weights = {"quality": 0.30, "growth": 0.25, "safety": 0.20, "valuation": 0.15}
        if inst_score is not None:
            provider_score_weights["institutional"] = 0.10
        provider_weight_total = sum(provider_score_weights.values())
        provider_diagnostic_score = None
        if all(provider_dimensions.get(name) is not None for name in ("quality", "growth", "safety", "valuation")):
            provider_diagnostic_score = round((provider_dimensions["quality"]*provider_score_weights["quality"] + provider_dimensions["growth"]*provider_score_weights["growth"] + provider_dimensions["safety"]*provider_score_weights["safety"] + provider_dimensions["valuation"]*provider_score_weights["valuation"] + (inst_score or 0)*provider_score_weights.get("institutional", 0)) / provider_weight_total, 1)
        sector_checklist = DEFAULT_SECTOR_FUNDAMENTAL_CHECKLIST_AUTHORITY.evaluate(
            {
                "roe": roe, "roce": roce, "roa": roa,
                "pe": pe, "pb": pb, "ev_ebitda": ev,
                "sales_growth": mean_change(revenue_hist),
                "profit_growth": mean_change(profit_hist),
                "total_assets": assets, "total_liabilities": liabilities,
                "positive_cfo_ratio": positive_cfo,
            },
            sector_classification.get("fundamental_sector"),
        )
        canonical = DEFAULT_FUNDAMENTAL_SCORING_AUTHORITY.score_dimensions(
            {"quality": quality, "growth": growth, "safety": safety, "valuation": valuation},
            sector_score=sector_checklist.get("score"),
            sector=sector_classification.get("fundamental_sector") or profile.get("sector"),
        )
        score = canonical["score"]
        state = canonical["state"]
        return {"ok": True, "state": state, "score": score, "quality": quality, "valuation": valuation, "growth": growth, "safety": safety, "pe": pe, "pb": pb, "roe": roe, "roce": roce, "roa": roa, "sector": profile.get("sector"), "sector_policy": sector_policy, "sector_classification_authority": sector_classification["authority"], "sector_classification_authority_version": sector_classification["authority_version"], "market_sector_key": sector_classification.get("market_sector_key"), "sector_checklist": sector_checklist, "sector_checklist_authority": sector_checklist["authority"], "sector_checklist_authority_version": sector_checklist["authority_version"], "profile": profile.get("company_profile"), "shareholding": holding_summary, "institutional_delta": institutional_delta, "coverage": coverage, "score_method": {**canonical["score_method"], "sector_pe": sector_pe, "sector_pb": sector_pb, "sector_ev_ebitda": sector_ev}, "fundamental_dimension_authority": normalized["authority"], "fundamental_dimension_authority_version": normalized["authority_version"], "fundamental_scoring_authority": canonical["authority"], "fundamental_scoring_authority_version": canonical["authority_version"], "provider_dimension_diagnostic": provider_dimensions, "provider_diagnostic_score": provider_diagnostic_score, "provider_diagnostic_score_method": {"weights": provider_score_weights}, "statement_type": statement_type, "source": "upstox_fundamentals_api", "fetched_at": now_iso(), "errors": errors[-8:], "reason": f"Canonical fundamental score over verified {sector_policy} normalized dimensions: quality {quality}, growth {growth}, safety {safety}, valuation {valuation}; institutional evidence remains separate"}

    def preflight(self, sample_instrument: Optional[Dict[str, Any]] = None, force: bool = False) -> Dict[str, Any]:
        if not force and self._preflight_cache and time.time() - self._preflight_ts < 60:
            return self._preflight_cache
        token_state = self.token_status()
        result = {"time": now_iso(), "token": token_state, "quote": {"ok": False}, "historical": {"ok": False}, "ok": False}
        if not token_state.get("ok"):
            result["message"] = "Token missing"
            self._preflight_cache = result
            self._preflight_ts = time.time()
            return result
        inst = sample_instrument
        if not inst:
            found = self.search_instruments("INFY", limit=1)
            inst = found[0] if found else None
        if not inst:
            result["message"] = "Sample instrument missing"
            self._preflight_cache = result
            self._preflight_ts = time.time()
            return result
        result["instrument"] = {"symbol": inst.get("trading_symbol"), "instrument_key": inst.get("instrument_key")}
        try:
            q = self.quotes([inst])
            result["quote"] = {"ok": bool(q and q[0].get("ltp") is not None), "ltp": q[0].get("ltp") if q else None}
        except Exception as exc:
            result["quote"] = {"ok": False, "error": str(exc), "endpoint": "/v3/market-quote/ltp"}
        try:
            c = self.historical_candles(inst["instrument_key"], "day", 30)
            result["historical"] = {"ok": bool(c), "count": len(c), "last": c[-1] if c else None, "endpoint": "/v3/historical-candle"}
        except Exception as exc:
            result["historical"] = {"ok": False, "error": str(exc), "endpoint": "/v3/historical-candle"}
        result["ok"] = bool(result["historical"].get("ok"))
        result["message"] = "Historical OK; quote may be unavailable outside permissions/session" if result["ok"] and not result["quote"].get("ok") else "Quote and historical OK" if result["ok"] and result["quote"].get("ok") else "Upstox auth/API blocked"
        self._preflight_cache = result
        self._preflight_ts = time.time()
        return result
