"""
ReferenceDataService -- v37.5, Phase 2/3 combined.

Owns everything that is NOT live tick data: NSE's daily public reports
(security-wise delivery % and bulk/block deals) and the
market-breadth rollup computed from the existing live-quote universe.

Deliberately isolated from MarketDataService (live candles/quotes) and
from engines.py (decision logic). This runs on its own daily cadence and
writes to its own tables (see storage.py SCHEMA: delivery_data,
bulk_block_deals and market_breadth_daily). A parsing failure
here must never be able to raise into the scanner loop or touch
signal_ledger/candles -- every public entry point catches its own
exceptions and records the outcome via Store.record_reference_run so
/api/system-health can show pass/fail per job per day, not silence.

Data sources (NSE's own public archives -- no login/paid feed required):
  - Delivery %:      NSE "security-wise delivery" bhavcopy (daily CSV)
  - Bulk deals:       NSE bulk deal report (daily CSV)
  - Block deals:      NSE block deal report (daily CSV)

NOTE ON NETWORK ACCESS: NSE's archive endpoints require a browser-like
User-Agent and (for some reports) an initial cookie-fetch against
nseindia.com's homepage before the CSV endpoints will respond -- calling
the CSV URL cold often gets a 403. This module fetches the homepage once
per session to pick up cookies before requesting the actual reports. If
NSE changes these endpoints/columns (they do this periodically), this is
the one file to update -- nothing else in the codebase depends on the
specific URL/column shape, only on the normalized rows this module hands
back to storage.py's save_* methods.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from core.rate_controller import SlotBusy
from core.official_report_publication_policy import DEFAULT_OFFICIAL_REPORT_PUBLICATION_POLICY

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

NSE_HOME = "https://www.nseindia.com"
NSE_DELIVERY_URL_TMPL = "https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
NSE_BULK_DEALS_URL = "https://archives.nseindia.com/content/equities/bulk.csv"
NSE_BLOCK_DEALS_URL = "https://archives.nseindia.com/content/equities/block.csv"
NSE_FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/csv,application/csv,*/*",
    "Referer": "https://www.nseindia.com/",
}



class _UrllibResponse:
    def __init__(self, data: bytes, url: str):
        self.content = data
        self.text = data.decode("utf-8", errors="replace")
        self.url = url
    def raise_for_status(self):
        return None
    def json(self):
        import json
        return json.loads(self.text or "{}")

class _UrllibSession:
    def __init__(self):
        self.headers = dict(_HEADERS)
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    def get(self, url: str, params=None, timeout=15):
        if params:
            qs = urllib.parse.urlencode(params)
            sep = '&' if '?' in url else '?'
            url = url + sep + qs
        req = urllib.request.Request(url, headers=self.headers)
        with self._opener.open(req, timeout=timeout) as res:
            return _UrllibResponse(res.read(), url)

class ReferenceDataService:
    def __init__(self, store, event: Callable[..., None], record_error: Callable[..., None],
                 client=None, rate=None, fundamentals=None, host: Any = None):
        self.store = store
        self.event = event
        self.record_error = record_error
        self._session = None
        self._session_ts = 0.0
        # v51 (Cluster 7): fundamentals + sector context merged in here.
        # client/rate/fundamentals are the same objects LadduRuntime already
        # owns (Upstox client, RateController, FundamentalStore) -- not
        # duplicated, just referenced. host covers what's genuinely someone
        # else's concern: instrument identity (_first_instrument,
        # _enrich_instrument_identity, _isin_from_instrument) and
        # heatmap_snapshot/.status -- same host-reference pattern used by
        # ScanOrchestrationService and MarketDataService.
        self.client = client
        self.rate = rate
        self.fundamentals = fundamentals
        self.host = host
        self._fund_api_cache: dict[str, tuple] = {}
        self._fund_api_pending: set = set()
        self._fund_attempt_lock = threading.RLock()
        self._fund_attempts: dict[str, Dict[str, Any]] = {}
        self._fund_identity_retry_at: dict[str, float] = {}
        self._fund_identity_cache: dict[str, Dict[str, Any]] = {}
        self._fund_prefetch_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="LadduFundPrefetch")
        # v65.7.1: hydrate from the persistent fundamentals_cache table ONCE
        # here at construction, not per-lookup. v65.7.0 made _fund_cache_get()
        # fall back to a DB query on every miss, which was correct for the
        # occasional user-facing lookup but wrong for the position/delivery
        # scanner's prefetch check (scan_orchestration_service.py), which
        # calls this for every symbol in every batch of a continuously
        # running background loop across a ~2000-symbol universe -- that
        # turned a free dict lookup into hundreds of synchronous SQLite
        # reads per cycle and made the whole backend slower than before the
        # DB persistence was added. Bulk-loading once here keeps every
        # hot-path lookup pure in-memory again, while still surviving
        # restarts because this table is populated on startup.
        try:
            for isin, row in store.get_all_fundamentals_cache().items():
                payload = row.get("payload") if isinstance(row, dict) else None
                # v65.26.20 could persist/retain a generic pending/source=none
                # shell. It contains no provider attempt and must not survive a
                # restart as though it were evidence.
                if not isinstance(payload, dict):
                    continue
                legacy_pending = str(payload.get("state") or "").lower() == "pending" and str(payload.get("source") or "").lower() in ("", "none")
                error_text = " ".join(
                    [str(payload.get("reason") or ""), str(payload.get("api_error") or "")]
                    + [str(item.get("error") or "") for item in (payload.get("errors") or []) if isinstance(item, dict)]
                ).lower()
                known_broken_normalizer = "name 're' is not defined" in error_text or 'name "re" is not defined' in error_text
                if legacy_pending or known_broken_normalizer:
                    continue
                self._fund_api_cache[str(isin or "").upper()] = (self._parse_fetched_at(row.get("fetched_at")), payload)
        except Exception as exc:
            event("WARN", "fundamentals_cache", "Startup hydration from fundamentals_cache table failed; starting with empty cache", {"error": str(exc)[:200]})
        self._ensure_institutional_flow_schema()

    @staticmethod
    def _parse_fetched_at(fetched_at: Optional[str]) -> float:
        """Convert a stored ISO timestamp back to a time.time()-comparable
        epoch float, so hydrated rows use the same TTL arithmetic as
        freshly-written ones. Unparsable/missing -> treated as already
        expired (0.0) rather than raising, so one bad row can't break startup."""
        if not fetched_at:
            return 0.0
        try:
            dt = datetime.fromisoformat(fetched_at)
            if dt.tzinfo:
                return dt.timestamp()
            return dt.replace(tzinfo=datetime.now().astimezone().tzinfo).timestamp()
        except Exception:
            return 0.0

    # ------------------------------------------------------------ session
    def _get_session(self):
        """NSE archives reject cold requests without cookies from a prior
        homepage hit. Reuse one session for ~10 min, refresh after that.
        If requests is not installed in the backend runtime, fall back to urllib
        so reference-data jobs do not fail with 'requests library not available'.
        """
        if self._session is not None and (time.time() - self._session_ts) < 600:
            return self._session
        if requests is not None:
            s = requests.Session()
            s.headers.update(_HEADERS)
        else:
            s = _UrllibSession()
        try:
            s.get(NSE_HOME, timeout=8)
        except Exception as exc:
            self.event("WARN", "reference_data", "NSE homepage warm-up failed (cookies may be missing)", {"error": str(exc)[:160]})
        self._session = s
        self._session_ts = time.time()
        return s

    def _fetch_csv_rows(self, url: str) -> List[Dict[str, str]]:
        sess = self._get_session()
        resp = sess.get(url, timeout=15)
        resp.raise_for_status()
        text = resp.content.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows: List[Dict[str, str]] = []
        for row in reader:
            clean: Dict[str, str] = {}
            for k, v in (row or {}).items():
                # csv.DictReader stores overflow columns under key None with a
                # list value. NSE occasionally adds stray delimiters; ignore
                # those instead of recording a scanner API error.
                if k is None:
                    continue
                key = str(k or "").strip()
                if isinstance(v, list):
                    val = ",".join(str(x or "") for x in v).strip()
                else:
                    val = str(v or "").strip()
                clean[key] = val
            if clean:
                rows.append(clean)
        return rows


    # ------------------------------------------------------ institutional flow
    def _ensure_institutional_flow_schema(self) -> None:
        """Own market-wide FII/FPI and DII history independently from
        stock-level delivery data. Minimal test doubles without a SQLite
        connection intentionally skip this optional repository."""
        conn = getattr(self.store, "conn", None)
        if conn is None:
            return
        lock = getattr(self.store, "write_lock", None)
        if lock is None:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS institutional_market_flows(
                   trade_date TEXT NOT NULL,
                   market_scope TEXT NOT NULL DEFAULT 'NSE_CASH_MARKET',
                   category TEXT NOT NULL,
                   buy_value_crore REAL,
                   sell_value_crore REAL,
                   net_value_crore REAL,
                   provisional INTEGER NOT NULL DEFAULT 1,
                   source TEXT NOT NULL DEFAULT 'NSE_FII_DII',
                   content_hash TEXT NOT NULL,
                   fetched_at TEXT NOT NULL,
                   raw_json TEXT,
                   PRIMARY KEY(trade_date,market_scope,category)
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_institutional_flows_date
                   ON institutional_market_flows(trade_date DESC,category)"""
            )
            conn.commit()
            return
        with lock:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS institutional_market_flows(
                   trade_date TEXT NOT NULL,
                   market_scope TEXT NOT NULL DEFAULT 'NSE_CASH_MARKET',
                   category TEXT NOT NULL,
                   buy_value_crore REAL,
                   sell_value_crore REAL,
                   net_value_crore REAL,
                   provisional INTEGER NOT NULL DEFAULT 1,
                   source TEXT NOT NULL DEFAULT 'NSE_FII_DII',
                   content_hash TEXT NOT NULL,
                   fetched_at TEXT NOT NULL,
                   raw_json TEXT,
                   PRIMARY KEY(trade_date,market_scope,category)
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_institutional_flows_date
                   ON institutional_market_flows(trade_date DESC,category)"""
            )
            conn.commit()

    @staticmethod
    def _flow_number(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _flow_date(value: Any, fallback: str) -> str:
        text = str(value or "").strip()
        if not text:
            return fallback
        for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        return fallback

    @staticmethod
    def _flow_category(value: Any) -> str:
        text = str(value or "").upper().replace("FOREIGN INSTITUTIONAL INVESTORS", "FII/FPI")
        if "FII" in text or "FPI" in text:
            return "FII/FPI"
        if "DII" in text or "DOMESTIC" in text:
            return "DII"
        return text.strip() or "UNKNOWN"

    def fetch_fii_dii_activity(self, trade_date: Optional[str] = None) -> Dict[str, Any]:
        """Fetch NSE's market-wide provisional FII/FPI and DII cash-market
        activity once per publication cycle.  Upserts are content-hash driven,
        so unchanged source data performs no analytical rewrite."""
        requested_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        try:
            sess = self._get_session()
            resp = sess.get(NSE_FII_DII_URL, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
            raw_rows = payload if isinstance(payload, list) else (
                payload.get("data") if isinstance(payload, dict) else []
            )
            if not isinstance(raw_rows, list):
                raw_rows = []
            now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            normalized: List[Dict[str, Any]] = []
            for raw in raw_rows:
                if not isinstance(raw, dict):
                    continue
                category = self._flow_category(raw.get("category") or raw.get("clientType") or raw.get("type"))
                if category not in ("FII/FPI", "DII"):
                    continue
                row = {
                    "trade_date": self._flow_date(raw.get("date") or raw.get("tradeDate"), requested_date),
                    "market_scope": str(raw.get("market") or raw.get("marketType") or "NSE_CASH_MARKET").strip().upper(),
                    "category": category,
                    "buy_value_crore": self._flow_number(raw.get("buyValue") or raw.get("buy_value") or raw.get("buy")),
                    "sell_value_crore": self._flow_number(raw.get("sellValue") or raw.get("sell_value") or raw.get("sell")),
                    "net_value_crore": self._flow_number(raw.get("netValue") or raw.get("net_value") or raw.get("net")),
                    "provisional": True,
                    "source": "NSE_FII_DII",
                    "raw": raw,
                }
                if row["net_value_crore"] is None and row["buy_value_crore"] is not None and row["sell_value_crore"] is not None:
                    row["net_value_crore"] = row["buy_value_crore"] - row["sell_value_crore"]
                material = {k: row[k] for k in ("trade_date", "market_scope", "category", "buy_value_crore", "sell_value_crore", "net_value_crore", "provisional", "source")}
                row["content_hash"] = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
                normalized.append(row)
            changed = 0
            unchanged = 0
            with self.store.write_lock:
                for row in normalized:
                    prior = self.store.conn.execute(
                        """SELECT content_hash FROM institutional_market_flows
                           WHERE trade_date=? AND market_scope=? AND category=?""",
                        (row["trade_date"], row["market_scope"], row["category"]),
                    ).fetchone()
                    prior_hash = (dict(prior).get("content_hash") if prior else None)
                    if prior_hash == row["content_hash"]:
                        unchanged += 1
                        continue
                    self.store.conn.execute(
                        """INSERT INTO institutional_market_flows(
                           trade_date,market_scope,category,buy_value_crore,sell_value_crore,
                           net_value_crore,provisional,source,content_hash,fetched_at,raw_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(trade_date,market_scope,category) DO UPDATE SET
                           buy_value_crore=excluded.buy_value_crore,
                           sell_value_crore=excluded.sell_value_crore,
                           net_value_crore=excluded.net_value_crore,
                           provisional=excluded.provisional,
                           source=excluded.source,
                           content_hash=excluded.content_hash,
                           fetched_at=excluded.fetched_at,
                           raw_json=excluded.raw_json""",
                        (row["trade_date"], row["market_scope"], row["category"], row["buy_value_crore"],
                         row["sell_value_crore"], row["net_value_crore"], 1, row["source"],
                         row["content_hash"], now, json.dumps(row["raw"], sort_keys=True, default=str)),
                    )
                    changed += 1
                self.store.conn.commit()
            state = "OK" if normalized else "PARTIAL"
            self.store.record_reference_run("fii_dii_market_flow", requested_date, state, changed)
            context = self.institutional_flow_context()
            self.event("INFO", "reference_data", "FII/DII market flow ingested", {"trade_date": requested_date, "changed": changed, "unchanged": unchanged})
            return {"ok": bool(normalized), "changed": changed, "unchanged": unchanged, "rows": len(normalized), "context": context}
        except Exception as exc:
            self.store.record_reference_run("fii_dii_market_flow", requested_date, "FAILED", 0, str(exc))
            self.record_error("reference_data_fii_dii", str(exc), NSE_FII_DII_URL)
            return {"ok": False, "error": str(exc)[:200], "trade_date": td, "publication": publication}

    def institutional_flow_context(self, days: int = 20) -> Dict[str, Any]:
        limit = max(1, min(int(days or 20), 120))
        rows = [dict(r) for r in self.store.conn.execute(
            """SELECT * FROM institutional_market_flows
               ORDER BY trade_date DESC, category LIMIT ?""", (limit * 2 + 10,)
        ).fetchall()]
        grouped: Dict[str, List[Dict[str, Any]]] = {"FII/FPI": [], "DII": []}
        for row in rows:
            category = str(row.get("category") or "")
            if category in grouped and len(grouped[category]) < limit:
                grouped[category].append(row)
        def summary(category: str) -> Dict[str, Any]:
            items = grouped[category]
            values = [float(r.get("net_value_crore") or 0.0) for r in items]
            return {
                "latest": items[0] if items else None,
                "net_5d_crore": round(sum(values[:5]), 2),
                "net_20d_crore": round(sum(values[:20]), 2),
                "positive_days_20": sum(1 for value in values[:20] if value > 0),
                "observations": len(items),
            }
        fii = summary("FII/FPI")
        dii = summary("DII")
        fii5 = float(fii["net_5d_crore"] or 0.0)
        dii5 = float(dii["net_5d_crore"] or 0.0)
        if fii5 > 0 and dii5 > 0:
            regime = "BROAD_INSTITUTIONAL_BUYING"
        elif fii5 > 0:
            regime = "FII_POSITIVE"
        elif fii5 < 0 and dii5 > 0:
            regime = "DII_ABSORPTION"
        elif fii5 < 0 and dii5 <= 0:
            regime = "INSTITUTIONAL_RISK_OFF"
        else:
            regime = "MIXED_OR_INSUFFICIENT"
        latest_dates = [x.get("latest", {}).get("trade_date") for x in (fii, dii) if x.get("latest")]
        return {
            "state": "AVAILABLE" if latest_dates else "UNAVAILABLE",
            "market_scope": "NSE cash-market aggregate; not stock-specific identity",
            "provisional": True,
            "latest_trade_date": max(latest_dates) if latest_dates else None,
            "regime": regime,
            "fii_fpi": fii,
            "dii": dii,
        }

    # ------------------------------------------------------------ delivery %
    def fetch_delivery_data(self, trade_date: Optional[str] = None) -> Dict[str, Any]:
        """NSE's 'sec_bhavdata_full' CSV carries columns including
        SYMBOL, SERIES, TTL_TRD_QNTY, DELIV_QTY, DELIV_PER. Filter to
        SERIES=='EQ' (cash equity) and normalize into our schema."""
        if trade_date:
            td = str(trade_date)[:10]
            publication = {"state": "EXPLICIT", "trade_date": td}
        else:
            publication = DEFAULT_OFFICIAL_REPORT_PUBLICATION_POLICY.latest_eligible_trade_date()
            td = str(publication.get("trade_date") or "")
            if not td:
                detail = f"official report date unavailable: {publication.get('state') or 'UNKNOWN'}"
                self.store.record_reference_run("delivery_data", datetime.now().strftime("%Y-%m-%d"), "SKIPPED_CALENDAR_UNVERIFIED", 0, detail)
                return {"ok": False, "state": "CALENDAR_UNVERIFIED", "error": detail, "publication": publication}
        ddmmyyyy = datetime.strptime(td, "%Y-%m-%d").strftime("%d%m%Y")
        url = NSE_DELIVERY_URL_TMPL.format(ddmmyyyy=ddmmyyyy)
        rows_out: List[Dict[str, Any]] = []
        try:
            raw_rows = self._fetch_csv_rows(url)
            for r in raw_rows:
                series = (r.get("SERIES") or r.get(" SERIES") or "").strip().upper()
                if series and series != "EQ":
                    continue
                symbol = r.get("SYMBOL") or r.get(" SYMBOL")
                traded = r.get("TTL_TRD_QNTY") or r.get(" TTL_TRD_QNTY")
                deliv = r.get("DELIV_QTY") or r.get(" DELIV_QTY")
                pct = r.get("DELIV_PER") or r.get(" DELIV_PER")
                if not symbol:
                    continue
                rows_out.append({
                    "symbol": symbol, "traded_qty": traded, "deliverable_qty": deliv, "delivery_pct": pct,
                })
            n = self.store.save_delivery_data(td, rows_out)
            self.store.record_reference_run("delivery_data", td, "OK" if n else "PARTIAL", n)
            self.event("INFO", "reference_data", "Delivery data ingested", {"trade_date": td, "rows": n})
            return {"ok": True, "rows": n, "trade_date": td, "publication": publication}
        except Exception as exc:
            self.store.record_reference_run("delivery_data", td, "FAILED", 0, str(exc))
            self.record_error("reference_data_delivery", str(exc), url)
            return {"ok": False, "error": str(exc)[:200]}

    # ------------------------------------------------------------ bulk/block deals
    def fetch_bulk_block_deals(self, trade_date: Optional[str] = None) -> Dict[str, Any]:
        td = trade_date or datetime.now().strftime("%Y-%m-%d")
        total = 0
        results = {}
        for deal_type, url in (("BULK", NSE_BULK_DEALS_URL), ("BLOCK", NSE_BLOCK_DEALS_URL)):
            try:
                raw_rows = self._fetch_csv_rows(url)
                norm = []
                for r in raw_rows:
                    date_field = r.get("Date") or r.get("DATE") or ""
                    if date_field and td not in date_field and not date_field.strip():
                        pass  # NSE's own CSV is already "today's" report; keep permissive
                    norm.append({
                        "symbol": r.get("Symbol") or r.get("SYMBOL"),
                        "client_name": r.get("Client Name") or r.get("CLIENT NAME"),
                        "buy_sell": r.get("Buy/Sell") or r.get("BUY / SELL"),
                        "qty": r.get("Quantity Traded") or r.get("QUANTITY TRADED") or r.get("Trade Qty"),
                        "price": r.get("Trade Price / Wght. Avg. Price") or r.get("TRADE PRICE / WGHT. AVG. PRICE") or r.get("Price"),
                    })
                n = self.store.save_bulk_block_deals(td, deal_type, norm)
                total += n
                results[deal_type] = n
                self.store.record_reference_run(f"{deal_type.lower()}_deals", td, "OK" if n else "PARTIAL", n)
            except Exception as exc:
                self.store.record_reference_run(f"{deal_type.lower()}_deals", td, "FAILED", 0, str(exc))
                self.record_error(f"reference_data_{deal_type.lower()}", str(exc), url)
                results[deal_type] = f"error: {str(exc)[:120]}"
        self.event("INFO", "reference_data", "Bulk/block deals ingested", {"trade_date": td, **results})
        return {"ok": True, "rows": total, "detail": results}

    # ------------------------------------------------------------ market breadth
    def compute_market_breadth(self, quotes_by_symbol: Dict[str, Dict[str, Any]], universe: str = "NIFTY250_CORE") -> Dict[str, Any]:
        """Pure rollup of quotes the scanner already fetched this cycle --
        zero new API calls. Call this once per scan cycle with whatever
        quote dict you already have in hand."""
        adv = decl = unch = 0
        for q in (quotes_by_symbol or {}).values():
            chg = q.get("change_pct")
            if chg is None:
                continue
            try:
                chg = float(chg)
            except (TypeError, ValueError):
                continue
            if chg > 0:
                adv += 1
            elif chg < 0:
                decl += 1
            else:
                unch += 1
        self.store.save_market_breadth(universe, adv, decl, unch)
        return {"advances": adv, "declines": decl, "unchanged": unch, "universe": universe}

    # ------------------------------------------------------------ daily job entrypoint
    def run_daily_job(self, trade_date: Optional[str] = None) -> Dict[str, Any]:
        """Single entrypoint the scheduler calls once/day after market
        close. Each sub-fetch is independently caught -- one NSE endpoint
        being down/changed must not block the other two."""
        if trade_date:
            td = str(trade_date)[:10]
            publication = {"state": "EXPLICIT", "trade_date": td}
        else:
            publication = DEFAULT_OFFICIAL_REPORT_PUBLICATION_POLICY.latest_eligible_trade_date()
            td = str(publication.get("trade_date") or "")
            if not td:
                detail = f"official report date unavailable: {publication.get('state') or 'UNKNOWN'}"
                return {"trade_date": None, "state": "CALENDAR_UNVERIFIED", "publication": publication, "error": detail}
        results = {
            "trade_date": td,
            "publication": publication,
            "delivery": self.fetch_delivery_data(td),
            "bulk_block": self.fetch_bulk_block_deals(td),
            "institutional_flow": self.fetch_fii_dii_activity(td),
        }
        self.event("INFO", "reference_data", "Daily reference data job complete", {"trade_date": td})
        return results

    # ------------------------------------------------------------ fundamentals

    # Positive fundamentals survive indefinitely in the versioned store. This
    # is only the revalidation cadence, not a deletion/validity deadline.
    FUND_API_OK_TTL_SEC = 24 * 3600
    FUND_API_FAIL_TTL_SEC = 300
    FUND_API_TRANSIENT_TTL_SEC = 25
    FUND_IDENTITY_RETRY_SEC = 25

    @classmethod
    def _fund_payload_ttl(cls, payload: Dict[str, Any] | None) -> float:
        payload = payload or {}
        if payload.get("ok"):
            return cls.FUND_API_OK_TTL_SEC
        state = str(payload.get("state") or "").lower()
        errors = payload.get("errors") or []
        if errors or state in ("pending", "scheduled", "requesting", "api_failed", "source_unavailable", "network_busy", "identity_refreshing"):
            return cls.FUND_API_TRANSIENT_TTL_SEC
        return cls.FUND_API_FAIL_TTL_SEC

    @staticmethod
    def _fund_symbol(instrument: Dict[str, Any] | None, fallback: str = "") -> str:
        row = instrument or {}
        return str(row.get("trading_symbol") or row.get("symbol") or fallback or "").strip().upper()

    @staticmethod
    def _fund_attempt_key(symbol: str = "", isin: str = "") -> str:
        return str(isin or symbol or "UNKNOWN").strip().upper()

    def _record_fund_attempt(self, symbol: str, isin: str, state: str, *,
                             source: str = "upstox_fundamentals_api",
                             reason: str = "", errors: Optional[List[Dict[str, Any]]] = None,
                             attempt: Optional[int] = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from models import now_iso
        symbol = str(symbol or "").strip().upper()
        isin = str(isin or "").strip().upper()
        key = self._fund_attempt_key(symbol, isin)
        with self._fund_attempt_lock:
            prior = dict(self._fund_attempts.get(key) or {})
            if attempt is None:
                attempt = int(prior.get("attempt") or 0)
                if state == "requesting":
                    attempt += 1
            payload = {
                "state": str(state or "unknown"), "source": source,
                "symbol": symbol or prior.get("symbol"), "isin": isin or prior.get("isin"),
                "attempt": int(attempt or 0), "updated_at": now_iso(),
                "reason": reason or prior.get("reason") or "",
                "errors": list(errors or []),
            }
            if prior.get("scheduled_at"):
                payload["scheduled_at"] = prior["scheduled_at"]
            if state == "scheduled" and not payload.get("scheduled_at"):
                payload["scheduled_at"] = payload["updated_at"]
            if state == "requesting" and not prior.get("started_at"):
                payload["started_at"] = payload["updated_at"]
            elif prior.get("started_at"):
                payload["started_at"] = prior["started_at"]
            if state in ("completed", "incomplete", "source_unavailable", "network_busy", "api_failed", "identity_missing", "token_required"):
                payload["finished_at"] = payload["updated_at"]
            if extra:
                payload.update(extra)
            self._fund_attempts[key] = payload
            if symbol:
                self._fund_attempts[symbol] = payload
            if isin:
                self._fund_attempts[isin] = payload
            return dict(payload)

    def _fund_attempt_snapshot(self, symbol: str = "", isin: str = "") -> Dict[str, Any]:
        with self._fund_attempt_lock:
            row = self._fund_attempts.get(self._fund_attempt_key(symbol, isin)) or self._fund_attempts.get(str(symbol or "").upper()) or {}
            return dict(row)

    def _repair_fundamental_identity(self, instrument: Dict[str, Any] | None, symbol: str = "") -> Dict[str, Any] | None:
        """Repair stale persisted equity identity without blocking on downloads.

        A cached row with no ISIN cannot be sent to a fundamentals provider.
        Re-resolve it once per short cooldown from the already-local instrument
        master; if the local master is also incomplete, schedule a background
        master refresh and expose identity_missing instead of generic pending.
        """
        inst = self.host._enrich_instrument_identity(instrument) if instrument else None
        sym = self._fund_symbol(inst, symbol)
        cached_identity = self._fund_identity_cache.get(sym) if sym else None
        if cached_identity and self.host._isin_from_instrument(cached_identity):
            return dict(cached_identity)
        isin = self.host._isin_from_instrument(inst) if inst else ""
        if isin or not sym:
            if isin and sym:
                self._fund_identity_cache[sym] = dict(inst)
            return inst
        now = time.time()
        retry_at = float(self._fund_identity_retry_at.get(sym) or 0.0)
        if now >= retry_at:
            self._fund_identity_retry_at[sym] = now + self.FUND_IDENTITY_RETRY_SEC
            resolver = getattr(self.host, "instrument_resolver", None)
            try:
                repaired = resolver.resolve(sym, prefer_index=False, force_refresh=True) if resolver else self.host._first_instrument(sym, force_refresh=True)
                repaired = self.host._enrich_instrument_identity(repaired) if repaired else None
                if repaired and self.host._isin_from_instrument(repaired):
                    self._fund_identity_cache[sym] = dict(repaired)
                    self.event("INFO", "fundamentals_identity", "Repaired missing ISIN from local instrument master", {"symbol": sym, "isin": self.host._isin_from_instrument(repaired)})
                    return repaired
            except Exception as exc:
                self.record_error("fundamentals_identity", str(exc), "instrument_master")
            try:
                self.client.refresh_instruments_background(force=False)
            except Exception:
                pass
        return inst

    def fundamental_provider_status(self) -> Dict[str, Any]:
        """Report each provider layer and active attempt separately."""
        status = dict(self.fundamentals.status())
        local_loaded = bool(status.get("loaded"))
        local_count = int(status.get("count") or status.get("symbols") or 0)
        successful = sum(1 for _, payload in self._fund_api_cache.values() if isinstance(payload, dict) and payload.get("ok"))
        pending = len(self._fund_api_pending)
        ready = bool(local_loaded or successful)
        state = "ready_local_and_live" if local_loaded and successful else "ready_local" if local_loaded else "ready_live_cache" if successful else "not_loaded"
        with self._fund_attempt_lock:
            attempt_states: Dict[str, int] = {}
            seen = set()
            for row in self._fund_attempts.values():
                marker = id(row)
                if marker in seen:
                    continue
                seen.add(marker)
                key = str(row.get("state") or "unknown")
                attempt_states[key] = attempt_states.get(key, 0) + 1
        available_count = max(local_count, successful)
        status.update({
            "loaded": ready, "ready": ready, "state": state,
            # `count` is the public readiness contract consumed by health, the
            # installer and ProductReadinessService.  It represents verified
            # point-in-time rows across the governed provider chain, not only
            # the optional local file.
            "count": available_count, "symbols": available_count,
            "local_file_loaded": local_loaded, "local_symbol_count": local_count,
            "live_cache_count": successful, "pending_count": pending,
            "available_symbol_count": available_count,
            "provider_chain": ["user_or_authorized_import", "upstox_fundamentals_api", "official_exchange_filing_import"],
            "attempt_states": attempt_states,
            "source": "local_import+upstox_cache" if local_loaded and successful else "upstox_fundamentals_api_cache" if successful else status.get("source"),
            "message": "Verified fundamentals available" if ready else "No verified local import or successful live-cache row yet",
        })
        return status

    def _fund_cache_get(self, isin: str) -> Optional[tuple]:
        return self._fund_api_cache.get(str(isin or "").strip().upper())

    def _fund_cache_set(self, isin: str, payload: Dict[str, Any]) -> None:
        isin = str(isin or "").strip().upper()
        if not isin:
            return
        existing = self._fund_api_cache.get(isin)
        # A transient provider failure must never erase the last verified
        # filing snapshot. Attempt/error state remains observable separately.
        if existing and isinstance(existing[1], dict) and existing[1].get("ok") and not payload.get("ok"):
            return
        self._fund_api_cache[isin] = (time.time(), payload)
        try:
            self.store.save_fundamentals_cache(isin, bool(payload.get("ok")), payload)
        except Exception as exc:
            self.record_error("fundamentals_cache", str(exc), "fundamentals_cache")

    def fundamental_context(self, instrument: Dict[str, Any], use_api: bool = False) -> Dict[str, Any]:
        from models import now_iso
        instrument = self._repair_fundamental_identity(instrument, self._fund_symbol(instrument)) or instrument
        local = self.fundamentals.score(instrument)
        symbol = self._fund_symbol(instrument)
        isin = self.host._isin_from_instrument(instrument)
        cached_payload = self._fund_cache_lookup(instrument)
        if cached_payload:
            if cached_payload.get("revalidation_required"):
                self._schedule_fundamental_prefetch(instrument)
            return cached_payload
        if local.get("ok") or not use_api:
            return local
        if not isin:
            attempt = self._record_fund_attempt(symbol, "", "identity_missing", source="instrument_master", reason="Instrument ISIN is unavailable; local master refresh scheduled")
            return dict(local, state="identity_missing", source="instrument_master", reason=attempt["reason"], provider_attempt=attempt)
        if not self.client.token_status().get("ok"):
            attempt = self._record_fund_attempt(symbol, isin, "token_required", reason="A valid Upstox token is required for live fundamentals")
            return dict(local, state="source_unavailable", source="upstox_fundamentals_api", api_state="token_required", reason=attempt["reason"], provider_attempt=attempt)
        try:
            self._record_fund_attempt(symbol, isin, "requesting", reason="Synchronous refresh requested")
            api_fund = self.client.fundamentals_snapshot(
                isin, request_guard=lambda: self.rate.net_slot(priority="interactive", timeout=6.0),
                max_workers=2, request_retry=False,
            )
            if not isinstance(api_fund, dict):
                api_fund = {"ok": False, "state": "api_failed", "reason": "Provider returned an invalid payload", "source": "upstox_fundamentals_api"}
            final_state = "completed" if api_fund.get("ok") else str(api_fund.get("state") or "incomplete")
            attempt = self._record_fund_attempt(symbol, isin, final_state, reason=api_fund.get("reason") or "", errors=api_fund.get("errors") or [])
            api_fund = dict(api_fund, symbol=symbol, isin=isin, source=api_fund.get("source") or "upstox_fundamentals_api", provider_attempt=attempt)
            self._fund_cache_set(isin, api_fund)
            if api_fund.get("ok"):
                with self.host.lock:
                    self.host.status["last_fundamental_refresh"] = now_iso()
            return api_fund
        except Exception as exc:
            self.record_error("fundamentals", str(exc), "/v2/fundamentals")
            attempt = self._record_fund_attempt(symbol, isin, "api_failed", reason=str(exc), errors=[{"error": str(exc)}])
            payload = dict(local, state="api_failed", source="upstox_fundamentals_api", api_error=str(exc), provider_attempt=attempt, errors=attempt["errors"])
            self._fund_cache_set(isin, payload)
            return payload

    def fundamentals_for_symbol(self, symbol: str) -> Dict[str, Any]:
        symbol = str(symbol or "").strip().upper()
        inst = self.host._first_instrument(symbol)
        if not inst:
            return {"ok": False, "symbol": symbol, "error": "instrument not found", "fundamentals": {"ok": False, "state": "identity_missing", "source": "instrument_master", "reason": "Instrument not found"}, "provider": self.fundamental_provider_status()}
        inst = self._repair_fundamental_identity(inst, symbol) or inst
        self.fundamentals.load(force=False)
        local = self.fundamentals.score(inst)
        if local.get("ok"):
            return {"ok": True, "symbol": symbol, "instrument": inst, "fundamentals": local, "provider": self.fundamental_provider_status()}
        cached = self._fund_cache_lookup(inst)
        if cached:
            if cached.get("revalidation_required"):
                self._schedule_fundamental_prefetch(inst)
            return {"ok": True, "symbol": symbol, "instrument": inst, "fundamentals": cached, "provider": self.fundamental_provider_status()}

        schedule = self._schedule_fundamental_prefetch(inst)
        state = str(schedule.get("state") or "pending")
        if state in ("scheduled", "requesting", "already_pending"):
            display_state = "pending"
        elif state == "token_required":
            display_state = "source_unavailable"
        else:
            display_state = state
        partial = {k: v for k, v in local.items() if k not in ("state", "source", "reason")}
        fundamentals = dict(partial)
        fundamentals.update({
            "ok": False, "score": None, "state": display_state,
            "source": schedule.get("source") or "fundamental_provider_chain",
            "reason": schedule.get("reason") or "Fundamentals provider chain is running",
            "isin": self.host._isin_from_instrument(inst) or None,
            "provider_attempt": schedule,
            "local_evidence": {"state": local.get("state"), "source": local.get("source"), "reason": local.get("reason")},
            "errors": schedule.get("errors") or [],
        })
        return {"ok": True, "symbol": symbol, "instrument": inst, "fundamentals": fundamentals, "provider": self.fundamental_provider_status()}

    def _fund_cache_lookup(self, instrument: Dict[str, Any] | None) -> Dict[str, Any] | None:
        isin = self.host._isin_from_instrument(instrument)
        if not isin:
            return None
        cached = self._fund_cache_get(isin)
        if cached and isinstance(cached[1], dict):
            payload = cached[1]
            legacy_pending = str(payload.get("state") or "").lower() == "pending" and str(payload.get("source") or "").lower() in ("", "none")
            if legacy_pending:
                return None
            age = max(0.0, time.time() - float(cached[0] or 0.0))
            fresh = age < self._fund_payload_ttl(payload)
            if fresh or payload.get("ok"):
                return dict(
                    payload,
                    cache_state="fresh" if fresh else "stale_while_revalidate",
                    cached_at=datetime.fromtimestamp(float(cached[0] or 0.0), tz=timezone.utc).isoformat(),
                    cache_age_seconds=round(age, 1),
                    revalidation_required=not fresh,
                    point_in_time_versioned=bool(payload.get("ok")),
                )
        return None

    def _schedule_fundamental_prefetch(self, instrument: Dict[str, Any] | None) -> Dict[str, Any]:
        from models import now_iso
        inst = self._repair_fundamental_identity(instrument, self._fund_symbol(instrument)) if instrument else None
        symbol = self._fund_symbol(inst, self._fund_symbol(instrument))
        isin = self.host._isin_from_instrument(inst) if inst else ""
        if not isin:
            return self._record_fund_attempt(symbol, "", "identity_missing", source="instrument_master", reason="Instrument ISIN is unavailable; local instrument-master refresh was requested")
        existing = self._fund_cache_get(isin)
        if existing and time.time() - existing[0] < self._fund_payload_ttl(existing[1]):
            return dict(self._fund_attempt_snapshot(symbol, isin) or {"state": "cache_fresh", "source": existing[1].get("source") or "fundamental_cache", "symbol": symbol, "isin": isin, "reason": "A fresh fundamental payload is already cached"})
        if isin in self._fund_api_pending:
            return dict(self._fund_attempt_snapshot(symbol, isin) or {"state": "already_pending", "source": "upstox_fundamentals_api", "symbol": symbol, "isin": isin, "reason": "A fundamentals request is already running"})
        if not self.client.token_status().get("ok"):
            return self._record_fund_attempt(symbol, isin, "token_required", reason="A valid Upstox token is required for live fundamentals")

        self._fund_api_pending.add(isin)
        scheduled = self._record_fund_attempt(symbol, isin, "scheduled", reason="Background fundamentals request accepted")

        def worker():
            attempts = 3
            backoff = (0.5, 1.5, 3.0)
            try:
                self.rate.prioritize_interactive(25.0)
                for attempt_no in range(1, attempts + 1):
                    self._record_fund_attempt(symbol, isin, "requesting", reason=f"Provider attempt {attempt_no}/{attempts}", attempt=attempt_no)
                    try:
                        res = self.client.fundamentals_snapshot(
                            isin, request_guard=lambda: self.rate.net_slot(priority="interactive", timeout=6.0),
                            max_workers=2, request_retry=False,
                        )
                        if not isinstance(res, dict):
                            res = {"ok": False, "state": "api_failed", "reason": "Provider returned an invalid payload", "errors": [{"error": "invalid payload"}]}
                        state = "completed" if res.get("ok") else str(res.get("state") or "incomplete").lower()
                        attempt_row = self._record_fund_attempt(symbol, isin, state, reason=res.get("reason") or "", errors=res.get("errors") or [], attempt=attempt_no)
                        res = dict(res, symbol=symbol, isin=isin, source=res.get("source") or "upstox_fundamentals_api", provider_attempt=attempt_row)
                        self._fund_cache_set(isin, res)
                        if res.get("ok"):
                            with self.host.lock:
                                self.host.status["last_fundamental_refresh"] = now_iso()
                            return
                        transient = bool(res.get("errors")) or state in ("source_unavailable", "api_failed", "network_busy")
                        if not transient:
                            return
                        if attempt_no < attempts:
                            time.sleep(backoff[attempt_no - 1])
                    except SlotBusy as exc:
                        if attempt_no < attempts:
                            time.sleep(backoff[attempt_no - 1])
                            continue
                        errors = [{"error": str(exc), "type": "network_busy"}]
                        attempt_row = self._record_fund_attempt(symbol, isin, "network_busy", reason="No shared network slot became available", errors=errors, attempt=attempt_no)
                        self._fund_cache_set(isin, {"ok": False, "state": "network_busy", "score": None, "source": "upstox_fundamentals_api", "symbol": symbol, "isin": isin, "reason": attempt_row["reason"], "errors": errors, "provider_attempt": attempt_row, "retry_after_sec": self.FUND_API_TRANSIENT_TTL_SEC})
                        self.record_error("fundamentals_prefetch", str(exc), "/v2/fundamentals")
                        return
                    except Exception as exc:
                        if attempt_no < attempts:
                            time.sleep(backoff[attempt_no - 1])
                            continue
                        errors = [{"error": str(exc), "type": "api_failed"}]
                        attempt_row = self._record_fund_attempt(symbol, isin, "api_failed", reason=str(exc), errors=errors, attempt=attempt_no)
                        self._fund_cache_set(isin, {"ok": False, "state": "api_failed", "score": None, "source": "upstox_fundamentals_api", "symbol": symbol, "isin": isin, "reason": str(exc), "errors": errors, "provider_attempt": attempt_row, "retry_after_sec": self.FUND_API_TRANSIENT_TTL_SEC})
                        self.record_error("fundamentals_prefetch", str(exc), "/v2/fundamentals")
                        return
            finally:
                self._fund_api_pending.discard(isin)

        try:
            self._fund_prefetch_pool.submit(worker)
        except Exception as exc:
            self._fund_api_pending.discard(isin)
            errors = [{"error": str(exc), "type": "scheduler_failed"}]
            failed = self._record_fund_attempt(symbol, isin, "api_failed", reason="Fundamentals worker could not be scheduled", errors=errors)
            self._fund_cache_set(isin, {"ok": False, "state": "api_failed", "score": None, "source": "upstox_fundamentals_api", "symbol": symbol, "isin": isin, "reason": failed["reason"], "errors": errors, "provider_attempt": failed})
            return failed
        return scheduled

    # ------------------------------------------------------------ sector context

    def _resolve_sector_key(self, d: Dict[str, Any]) -> str:
        """Return a stock-specific sector key only when we have explicit mapping evidence.

        This deliberately refuses to create a generic sector guess. If no stock-specific
        mapping exists, the frontend must show sector unavailable rather than unrelated
        sector heat.
        """
        from reference_catalog import normalize_sector_key, final_fallback_instrument
        from market_layers import sector_hint_from_symbol
        raw = d.get("sector") or d.get("industry")
        key = normalize_sector_key(raw)
        if key:
            return key
        sym = str(d.get("symbol") or d.get("trading_symbol") or "").upper().strip()
        inst = final_fallback_instrument(sym) or {}
        key = normalize_sector_key(inst.get("sector"))
        if key:
            return key
        # sector_hint_from_symbol is an explicit symbol/name whitelist in market_layers,
        # not a broad classifier. If it returns blank, keep unavailable.
        hint = sector_hint_from_symbol(sym, str(d.get("name") or ""))
        return normalize_sector_key(hint)

    def _sector_context_for_row(self, d: Dict[str, Any], heatmap: list[Dict[str, Any]] | None = None) -> Dict[str, Any]:
        from reference_catalog import FINAL_INDEX_ALIAS, SECTOR_INDEX_LABEL
        key = self._resolve_sector_key(d)
        if not key:
            return {
                "sector_key": None, "sector_label": "Sector unavailable", "sector_index": None,
                "sector_status": "unavailable", "sector_change_pct": None, "sector_freshness": None,
                "sector_reason": "No reliable stock-specific sector mapping; not using unrelated sector heat."
            }
        rows = heatmap if heatmap is not None else self.host.heatmap_snapshot()
        match = None
        for h in rows or []:
            nm = str(h.get("name") or h.get("index") or "").upper().strip()
            if nm == key or FINAL_INDEX_ALIAS.get(SECTOR_INDEX_LABEL.get(key, ""), "").upper() == nm:
                match = h; break
        chg = None if not match else match.get("change_pct")
        label = SECTOR_INDEX_LABEL.get(key, key)
        if chg is None:
            return {
                "sector_key": key, "sector_label": label, "sector_index": label,
                "sector_status": "unavailable", "sector_change_pct": None,
                "sector_freshness": (match or {}).get("freshness") or (match or {}).get("last_refresh"),
                "sector_reason": f"{label} mapped for this stock, but live sector quote is unavailable."
            }
        side = str(d.get("side") or "").upper()
        try:
            chgf = float(chg)
        except Exception:
            chgf = 0.0
        if abs(chgf) < 0.10:
            status = "neutral"
        elif side in ("LONG", "BUY"):
            status = "supportive" if chgf > 0 else "conflicting"
        elif side in ("SHORT", "SELL"):
            status = "supportive" if chgf < 0 else "conflicting"
        else:
            status = "supportive" if chgf > 0 else "weak"
        return {
            "sector_key": key, "sector_label": label, "sector_index": label,
            "sector_status": status, "sector_change_pct": round(chgf, 2),
            "sector_freshness": (match or {}).get("freshness") or (match or {}).get("last_refresh"),
            "sector_reason": f"{label} {round(chgf,2)}% · {status}"
        }
