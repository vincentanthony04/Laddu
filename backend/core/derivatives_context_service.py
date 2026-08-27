"""Evidence-only derivatives context for NSE cash equities and indices.

Derivatives never enter the active scanner/trading universe.  This service
uses option OI, change in OI, IV and liquidity only to confirm, weaken or veto
cash-price interpretations.  It is cache-first and refreshes in bounded
background work so Stock Intelligence never blocks on provider I/O.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading
import time
from typing import Any, Dict, Iterable, Optional

SERVICE_VERSION = "cash-underlying-derivatives-context-1.1.0"
AUTHORITY_NAME = "DerivativesContextEvidenceAuthority"
IST = timezone(timedelta(hours=5, minutes=30))


def _num(value: Any) -> Optional[float]:
    try:
        return None if value in (None, "", "null") else float(value)
    except Exception:
        return None


def _rows(payload: Any) -> list[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [dict(row) for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("call_put_oi_data_list", "rows", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [dict(row) for row in value if isinstance(row, dict)]
    return []


class DerivativesContextService:
    """Read-only contextual evidence authority; never owns instruments, candidates or orders."""

    authority = AUTHORITY_NAME
    authority_version = SERVICE_VERSION

    def __init__(self, store: Any, client: Any, event=None, *, ttl_seconds: int = 600):
        self.store = store
        self.client = client
        self.event = event or (lambda *args, **kwargs: None)
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="laddu-derivatives-context")
        self._lock = threading.Lock()
        self._pending: set[str] = set()

    @staticmethod
    def _cache_key(instrument_key: str) -> str:
        return f"derivatives_context:{instrument_key}:current_month"

    def _get(self, key: str) -> Optional[Dict[str, Any]]:
        getter = getattr(self.store, "get_kv", None)
        if not callable(getter):
            return None
        try:
            value = getter(key, None)
            return dict(value) if isinstance(value, dict) else None
        except Exception:
            return None

    def _set(self, key: str, value: Dict[str, Any]) -> None:
        setter = getattr(self.store, "set_kv", None)
        if callable(setter):
            try:
                setter(key, value)
            except Exception:
                pass

    @staticmethod
    def _chain_rows(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
        data = payload.get("data") if isinstance(payload, dict) else None
        return [dict(row) for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    @staticmethod
    def _option_side(row: Dict[str, Any], side: str) -> Dict[str, Any]:
        block = row.get(f"{side}_options") or row.get(side) or {}
        market = block.get("market_data") or block.get("marketData") or block
        greeks = block.get("option_greeks") or block.get("greeks") or {}
        return {
            "oi": _num(market.get("oi") or market.get("open_interest")),
            "previous_oi": _num(market.get("prev_oi") or market.get("previous_oi")),
            "volume": _num(market.get("volume") or market.get("volume_traded")),
            "ltp": _num(market.get("ltp") or market.get("last_price")),
            "iv": _num(greeks.get("iv") or greeks.get("implied_volatility")),
        }

    @staticmethod
    def _nearest_wall(rows: Iterable[Dict[str, Any]], *, spot: Optional[float], side: str) -> Optional[Dict[str, Any]]:
        candidates = []
        for row in rows:
            strike = _num(row.get("strike_price") or row.get("strike"))
            oi = _num(row.get(f"{side}_oi"))
            change = _num(row.get(f"{side}_change_oi"))
            if strike is None or oi is None:
                continue
            if spot is not None and ((side == "call" and strike < spot) or (side == "put" and strike > spot)):
                continue
            candidates.append({"strike": strike, "oi": oi, "change_oi": change})
        if not candidates:
            return None
        ranked = sorted(candidates, key=lambda row: (row["oi"], -(abs((spot or row["strike"]) - row["strike"]))), reverse=True)
        return ranked[0]

    def _build(self, instrument: Dict[str, Any], spot: Optional[float]) -> Dict[str, Any]:
        key = str(instrument.get("instrument_key") or "").strip()
        symbol = str(instrument.get("trading_symbol") or instrument.get("symbol") or "").upper()
        if not key or not key.startswith("NSE_"):
            return {"ok": True, "version": SERVICE_VERSION, "state": "NOT_APPLICABLE", "symbol": symbol, "reason": "NSE underlying identity required", "production_influence": 0.0}
        if not self.client.token_status().get("ok"):
            return {"ok": False, "version": SERVICE_VERSION, "state": "TOKEN_REQUIRED", "symbol": symbol, "reason": "Upstox token is required for live OI context", "production_influence": 0.0}

        now = datetime.now(IST)
        trade_date = now.date().isoformat()
        oi_payload = self.client.option_open_interest(key, expiry="current_month", trade_date=trade_date)
        change_payload = self.client.option_change_open_interest(key, expiry="current_month", trade_date=trade_date, interval_days=1)
        try:
            chain_payload = self.client.option_chain(key, expiry="current_month")
        except Exception:
            chain_payload = {}

        oi_data = oi_payload.get("data") if isinstance(oi_payload, dict) else {}
        change_data = change_payload.get("data") if isinstance(change_payload, dict) else {}
        oi_rows = _rows(oi_payload)
        change_rows = _rows(change_payload)
        changes = {round(float(row.get("strike_price")), 6): row for row in change_rows if _num(row.get("strike_price")) is not None}
        merged = []
        for row in oi_rows:
            strike = _num(row.get("strike_price"))
            if strike is None:
                continue
            ch = changes.get(round(strike, 6), {})
            merged.append({
                "strike_price": strike,
                "call_oi": _num(row.get("call_oi")),
                "put_oi": _num(row.get("put_oi")),
                "call_change_oi": _num(ch.get("call_change_oi")),
                "put_change_oi": _num(ch.get("put_change_oi")),
            })

        iv_values, option_volume = [], 0.0
        for row in self._chain_rows(chain_payload):
            for side in ("call", "put"):
                item = self._option_side(row, side)
                if item["iv"] is not None:
                    iv_values.append(item["iv"])
                option_volume += item["volume"] or 0.0
        total_call = _num((oi_data or {}).get("total_call_oi")) or sum((row.get("call_oi") or 0.0) for row in merged)
        total_put = _num((oi_data or {}).get("total_put_oi")) or sum((row.get("put_oi") or 0.0) for row in merged)
        pcr = (total_put / total_call) if total_call else None
        call_wall = self._nearest_wall(merged, spot=spot, side="call")
        put_wall = self._nearest_wall(merged, spot=spot, side="put")
        total_call_change = _num((change_data or {}).get("total_call_change_oi"))
        total_put_change = _num((change_data or {}).get("total_put_change_oi"))
        if total_call_change is None:
            total_call_change = sum((row.get("call_change_oi") or 0.0) for row in merged)
        if total_put_change is None:
            total_put_change = sum((row.get("put_change_oi") or 0.0) for row in merged)

        if total_call <= 0 and total_put <= 0:
            state = "NOT_FNO_ELIGIBLE_OR_NO_CHAIN"
            interpretation = "No governed derivative chain was returned for this underlying."
        else:
            state = "READY"
            if total_put_change > 0 and total_call_change < 0:
                interpretation = "Put positioning is building while calls unwind; derivative context supports downside protection, subject to cash confirmation."
            elif total_call_change > 0 and total_put_change < 0:
                interpretation = "Call positioning is building while puts unwind; derivative context increases overhead pressure, subject to cash confirmation."
            elif total_call_change > 0 and total_put_change > 0:
                interpretation = "Both sides are adding OI; positioning implies a contested range or event-volatility regime."
            else:
                interpretation = "Derivative positioning is mixed or unwinding; it should not dominate the cash-price decision."

        captured = time.time()
        return {
            "ok": state == "READY",
            "version": SERVICE_VERSION,
            "state": state,
            "symbol": symbol,
            "underlying_instrument_key": key,
            "expiry": (oi_data or {}).get("expiry") or (change_data or {}).get("expiry") or "current_month",
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "captured_at_epoch": captured,
            "spot": spot,
            "pcr": round(pcr, 4) if pcr is not None else None,
            "total_call_oi": int(total_call or 0),
            "total_put_oi": int(total_put or 0),
            "total_call_change_oi": int(total_call_change or 0),
            "total_put_change_oi": int(total_put_change or 0),
            "call_wall": call_wall,
            "put_wall": put_wall,
            "mean_iv": round(sum(iv_values) / len(iv_values), 2) if iv_values else None,
            "option_volume": int(option_volume),
            "interpretation": interpretation,
            "cash_decision_role": "CONFIRM_WEAKEN_OR_VETO_ONLY",
            "canonical_level_authority": "CASH_PRICE_STRUCTURE",
            "active_trading_universe": "CASH_ONLY",
            "production_influence": 0.0,
            "broker_authority": "NONE",
            "source": "UPSTOX_OI_CHANGE_OI_OPTION_CHAIN",
        }

    def _refresh(self, instrument: Dict[str, Any], spot: Optional[float]) -> None:
        key = str(instrument.get("instrument_key") or "").strip()
        cache_key = self._cache_key(key)
        try:
            payload = self._build(instrument, spot)
        except Exception as exc:
            payload = {
                "ok": False, "version": SERVICE_VERSION, "state": "FETCH_FAILED",
                "symbol": str(instrument.get("trading_symbol") or "").upper(),
                "underlying_instrument_key": key, "reason": str(exc)[:240],
                "captured_at_epoch": time.time(), "production_influence": 0.0,
                "broker_authority": "NONE",
            }
            self.event("WARN", "derivatives_context", "Derivative context refresh failed", {"instrument_key": key, "error": str(exc)[:240]})
        self._set(cache_key, payload)
        with self._lock:
            self._pending.discard(key)

    def peek(self, instrument: Dict[str, Any]) -> Dict[str, Any]:
        """Return cached context only; never schedules provider I/O.

        Open-position risk/lifecycle evaluation uses this method so a critical
        mark/stop path cannot be blocked by an option-chain request.
        """
        key = str((instrument or {}).get("instrument_key") or "").strip()
        symbol = str((instrument or {}).get("trading_symbol") or (instrument or {}).get("symbol") or "").upper()
        if not key:
            return {
                "ok": False, "version": SERVICE_VERSION, "authority": self.authority,
                "authority_version": self.authority_version, "state": "IDENTITY_REQUIRED",
                "symbol": symbol, "production_influence": 0.0, "broker_authority": "NONE",
                "active_trading_universe": "CASH_ONLY", "provider_io": False,
            }
        if not key.startswith("NSE_"):
            return {
                "ok": True, "version": SERVICE_VERSION, "authority": self.authority,
                "authority_version": self.authority_version, "state": "NOT_APPLICABLE",
                "symbol": symbol, "underlying_instrument_key": key,
                "reason": "read-only NSE F&O context is not applicable to this cash identity",
                "cash_decision_role": "CONFIRM_WEAKEN_OR_VETO_ONLY",
                "production_influence": 0.0, "broker_authority": "NONE",
                "active_trading_universe": "CASH_ONLY", "provider_io": False,
            }
        cached = self._get(self._cache_key(key))
        if not cached:
            return {
                "ok": False, "version": SERVICE_VERSION, "authority": self.authority,
                "authority_version": self.authority_version, "state": "NOT_CAPTURED",
                "symbol": symbol, "underlying_instrument_key": key,
                "cash_decision_role": "CONFIRM_WEAKEN_OR_VETO_ONLY",
                "production_influence": 0.0, "broker_authority": "NONE",
                "active_trading_universe": "CASH_ONLY", "provider_io": False,
            }
        age = time.time() - float(cached.get("captured_at_epoch") or 0.0)
        return {
            **cached, "authority": self.authority, "authority_version": self.authority_version,
            "cache_age_seconds": round(max(0.0, age), 1), "stale": age >= self.ttl_seconds,
            "provider_io": False, "active_trading_universe": "CASH_ONLY", "broker_authority": "NONE",
        }

    def status(self, instrument: Dict[str, Any], *, spot: Optional[float] = None, refresh: bool = False) -> Dict[str, Any]:
        key = str((instrument or {}).get("instrument_key") or "").strip()
        if not key:
            return {"ok": False, "version": SERVICE_VERSION, "authority": self.authority, "authority_version": self.authority_version, "state": "IDENTITY_REQUIRED", "production_influence": 0.0, "broker_authority": "NONE", "active_trading_universe": "CASH_ONLY"}
        cache_key = self._cache_key(key)
        cached = self._get(cache_key)
        age = time.time() - float((cached or {}).get("captured_at_epoch") or 0.0)
        stale = not cached or age >= self.ttl_seconds
        should_schedule = refresh or stale
        if should_schedule:
            with self._lock:
                if key not in self._pending:
                    self._pending.add(key)
                    self._executor.submit(self._refresh, dict(instrument), spot)
        if cached:
            return {**cached, "authority": self.authority, "authority_version": self.authority_version, "refreshing": key in self._pending, "cache_age_seconds": round(max(0.0, age), 1), "stale": stale, "active_trading_universe": "CASH_ONLY", "broker_authority": "NONE"}
        return {
            "ok": False, "version": SERVICE_VERSION, "state": "REFRESH_SCHEDULED" if should_schedule else "UNAVAILABLE",
            "symbol": str(instrument.get("trading_symbol") or "").upper(),
            "underlying_instrument_key": key, "refreshing": should_schedule,
            "cash_decision_role": "CONFIRM_WEAKEN_OR_VETO_ONLY", "production_influence": 0.0, "broker_authority": "NONE",
        }
