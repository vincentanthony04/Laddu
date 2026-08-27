from __future__ import annotations
import bisect
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from actionability import is_actionable_signal
from typing import Any, Dict, List, Optional
from config import DB_PATH, DATA_DIR, LOG_DIR, RUNTIME_DB_PATH
from models import now_iso
from core.manual_watch_repository import ManualWatchRepository
from core.market_data_repository import MarketDataRepository
from core.priority_repository import PriorityRepository
from core.system_health_repository import SystemHealthRepository
from core.opportunity_memory_repository import OpportunityMemoryRepository
from core.reference_data_repository import ReferenceDataRepository, ensure_fundamentals_history_schema
from core.instrument_search_repository import InstrumentSearchRepository
from core.scan_universe_service import ScanUniverseService
from core.signal_ledger_repository import SignalLedgerRepository
from core.performance_journal_repository import PerformanceJournalRepository
from core.decision_write_policy import DecisionWritePolicy
from core.quant_edge_data_service import ensure_quant_edge_tables
from core.storage_decision_pipeline_mixin import StoreDecisionPipelineMixin
from core.decision_pipeline_storage_schema import DECISION_PIPELINE_MIGRATIONS, apply_decision_pipeline_migration, repair_decision_pipeline_schema
from core.model_tournament_service import ensure_model_tournament_schema
# v51 storage split: shared perf logging/canonicalization lives in core/db_utils.py;
# old private aliases preserve every existing call without a circular import.
from core.storage_layout import StorageLayout, prepare_operational_database
from core.runtime_market_state_store import RuntimeMarketStateStore
from core.curated_market_data_repository import CuratedMarketDataRepository
from core.db_utils import (
    perf_log as _perf_log, timed_write as _timed_write, canonical_interval,
    canonical_timestamp, utc_now_iso, to_float,
)
def _desk_modes(mode):
    from core.production_mode_policy import require_production_mode
    return (require_production_mode(mode),)
def _column_names(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
    except Exception:
        return False

def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if _table_exists(conn, table) and column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Ordered, idempotent upgrades for existing ProgramData DBs.

    This directly fixes the observed runtime failure `no such column: exchange`
    after upgrading older installs: CREATE TABLE IF NOT EXISTS never alters
    existing tables, so new code was selecting exchange columns that old local
    DBs did not have.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
    applied = {int(r[0]) for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    migrations = [
        (1, "legacy table exchange/mode columns"),
        (2, "candle provenance and price snapshots"),
        (3, "delivery evidence extended columns"),
        (4, "canonical candle interval keys/timestamps"),
        (5, "repair legacy migration-version collisions"),
        (6, "versioned point-in-time fundamentals history"),
        (7, "immutable quant feature snapshots and linked label vectors"),
        *DECISION_PIPELINE_MIGRATIONS,
        (10, "dual-desk finite model tournament"),
    ]
    for version, name in migrations:
        if version in applied:
            continue
        if version == 1:
            _add_column_if_missing(conn, "priority_symbols", "exchange", "TEXT DEFAULT 'NSE'")
            _add_column_if_missing(conn, "priority_symbols", "mode", "TEXT DEFAULT 'all'")
            _add_column_if_missing(conn, "priority_symbols", "source", "TEXT DEFAULT 'search'")
            _add_column_if_missing(conn, "priority_symbols", "created_at", "TEXT DEFAULT CURRENT_TIMESTAMP")
            for col, decl in (("exchange", "TEXT DEFAULT 'NSE'"), ("side", "TEXT"), ("state", "TEXT DEFAULT 'WATCH'"),
                              ("waiting_for", "TEXT"), ("trigger", "TEXT"), ("invalidation", "TEXT"),
                              ("reason", "TEXT"), ("pinned", "INTEGER DEFAULT 0"), ("source", "TEXT DEFAULT 'manual_search'"),
                              ("payload_json", "TEXT"), ("created_at", "TEXT DEFAULT CURRENT_TIMESTAMP"),
                              ("updated_at", "TEXT DEFAULT CURRENT_TIMESTAMP")):
                _add_column_if_missing(conn, "manual_watch", col, decl)
            for col, decl in (("exchange", "TEXT DEFAULT 'NSE'"), ("mode", "TEXT DEFAULT 'delivery'"), ("stage", "TEXT DEFAULT 'Potential'"),
                              ("priority_score", "INTEGER DEFAULT 0"), ("sector", "TEXT"), ("themes_json", "TEXT"),
                              ("priority_reason", "TEXT"), ("trigger", "TEXT"), ("invalidation", "TEXT"),
                              ("target_window", "TEXT"), ("next_scan_at", "TEXT"), ("last_seen_at", "TEXT DEFAULT CURRENT_TIMESTAMP"),
                              ("payload_json", "TEXT"), ("created_at", "TEXT DEFAULT CURRENT_TIMESTAMP"),
                              ("updated_at", "TEXT DEFAULT CURRENT_TIMESTAMP")):
                _add_column_if_missing(conn, "opportunity_memory", col, decl)
        elif version == 2:
            for col, decl in (("source", "TEXT DEFAULT 'upstox_historical'"), ("provider_ts", "TEXT"),
                              ("received_at", "TEXT"), ("raw_json", "TEXT")):
                _add_column_if_missing(conn, "candles", col, decl)
            conn.executescript("""CREATE TABLE IF NOT EXISTS price_snapshots (
              instrument_key TEXT NOT NULL, captured_at TEXT NOT NULL, symbol TEXT, exchange TEXT,
              ltp REAL, change_pct REAL, provider_ts TEXT, received_at TEXT NOT NULL,
              source TEXT DEFAULT 'upstox_ltp', raw_json TEXT,
              PRIMARY KEY(instrument_key, captured_at));
              CREATE INDEX IF NOT EXISTS ix_price_snapshots_symbol_ts ON price_snapshots(symbol, captured_at);""")
        elif version == 3:
            for col, decl in (("exchange", "TEXT DEFAULT 'NSE'"), ("close", "REAL"), ("source", "TEXT DEFAULT 'nse_delivery'"),
                              ("raw_json", "TEXT"), ("updated_at", "TEXT DEFAULT CURRENT_TIMESTAMP")):
                _add_column_if_missing(conn, "delivery_data", col, decl)
        elif version == 4:
            # Keep old rows, but copy common alias intervals into canonical keys so
            # future analysis doesn't split day/1d or 60minute/60m histories.
            aliases = {"day":"1d", "1day":"1d", "week":"1w", "1week":"1w", "month":"1mo", "1month":"1mo",
                       "1minute":"1m", "minute":"1m", "3minute":"3m", "5minute":"5m", "10minute":"10m",
                       "15minute":"15m", "30minute":"30m", "60minute":"60m", "1hour":"60m"}
            if _table_exists(conn, "candles"):
                cols = _column_names(conn, "candles")
                if {"source", "provider_ts", "received_at", "raw_json"}.issubset(cols):
                    for old, new in aliases.items():
                        conn.execute("""INSERT OR REPLACE INTO candles(instrument_key,interval,ts,open,high,low,close,volume,oi,source,provider_ts,received_at,raw_json)
                            SELECT instrument_key,?,ts,open,high,low,close,volume,oi,source,provider_ts,received_at,raw_json FROM candles WHERE interval=?""", (new, old))
                        if old != new:
                            conn.execute("DELETE FROM candles WHERE interval=?", (old,))
        elif version == 5:
            # Older releases reused migration numbers 1-4 for different work.
            # Re-check the physical schema instead of trusting those old names.
            for col, decl in (("exchange", "TEXT DEFAULT 'NSE'"), ("close", "REAL"),
                              ("source", "TEXT DEFAULT 'nse_delivery'"), ("raw_json", "TEXT"),
                              # SQLite rejects a non-constant default when ALTERing
                              # a populated table. Add nullable, then backfill below.
                              ("updated_at", "TEXT")):
                _add_column_if_missing(conn, "delivery_data", col, decl)
            for col, decl in (("exchange", "TEXT DEFAULT 'NSE'"), ("mode", "TEXT DEFAULT 'all'"),
                              ("source", "TEXT DEFAULT 'search'"), ("created_at", "TEXT")):
                _add_column_if_missing(conn, "priority_symbols", col, decl)
            if _table_exists(conn, "delivery_data") and "updated_at" in _column_names(conn, "delivery_data"):
                delivery_cols = _column_names(conn, "delivery_data")
                if "fetched_at" in delivery_cols:
                    conn.execute("UPDATE delivery_data SET updated_at=COALESCE(updated_at,fetched_at,CURRENT_TIMESTAMP)")
                else:
                    conn.execute("UPDATE delivery_data SET updated_at=COALESCE(updated_at,CURRENT_TIMESTAMP)")
            if _table_exists(conn, "priority_symbols") and "created_at" in _column_names(conn, "priority_symbols"):
                conn.execute("UPDATE priority_symbols SET created_at=COALESCE(created_at,CURRENT_TIMESTAMP)")
        elif version == 6:
            ensure_fundamentals_history_schema(conn)
        elif version == 7:
            ensure_quant_edge_tables(conn)
        elif version in (8, 9):
            apply_decision_pipeline_migration(conn, version)
        elif version == 10:
            ensure_model_tournament_schema(conn)
        conn.execute("INSERT OR REPLACE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)", (version, name, utc_now_iso()))
        conn.commit()

    repair_decision_pipeline_schema(conn)
    ensure_model_tournament_schema(conn)
    conn.commit()
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT,
  applied_at TEXT
);
CREATE TABLE IF NOT EXISTS instruments (
  instrument_key TEXT PRIMARY KEY,
  exchange TEXT,
  segment TEXT,
  trading_symbol TEXT,
  name TEXT,
  instrument_type TEXT,
  isin TEXT,
  expiry TEXT,
  strike REAL,
  option_type TEXT,
  lot_size INTEGER
);
CREATE INDEX IF NOT EXISTS ix_instruments_symbol ON instruments(trading_symbol, exchange, instrument_type);
CREATE INDEX IF NOT EXISTS ix_instruments_symbol_upper ON instruments(UPPER(trading_symbol), exchange, instrument_type);
CREATE INDEX IF NOT EXISTS ix_instruments_name_upper ON instruments(UPPER(name));
CREATE TABLE IF NOT EXISTS quotes (
  instrument_key TEXT PRIMARY KEY,
  symbol TEXT,
  exchange TEXT,
  ltp REAL,
  open REAL,
  high REAL,
  low REAL,
  close REAL,
  volume REAL,
  oi REAL,
  iv REAL,
  change_pct REAL,
  timestamp TEXT,
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_quotes_symbol_upper ON quotes(UPPER(symbol), timestamp);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT,
  exchange TEXT,
  mode TEXT,
  side TEXT,
  decision TEXT,
  status TEXT,
  score INTEGER,
  payload_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_decisions_mode_created ON decisions(mode, created_at);
CREATE TABLE IF NOT EXISTS signal_ledger (
  signal_id TEXT PRIMARY KEY,
  trade_date TEXT DEFAULT (date('now')),
  symbol TEXT,
  exchange TEXT,
  mode TEXT,
  side TEXT,
  decision TEXT,
  entry REAL,
  t1 REAL,
  t2 REAL,
  sl REAL,
  ltp REAL,
  exit REAL,
  score INTEGER,
  rr REAL,
  result TEXT DEFAULT 'OPEN',
  status TEXT DEFAULT 'OPEN',
  reason TEXT,
  payload_json TEXT,
  opened_at TEXT DEFAULT CURRENT_TIMESTAMP,
  last_update TEXT DEFAULT CURRENT_TIMESTAMP,
  closed_at TEXT,
  pnl_points REAL,
  source TEXT DEFAULT 'selected_candidate'
);
CREATE INDEX IF NOT EXISTS ix_signal_ledger_date_mode ON signal_ledger(trade_date, mode, opened_at);
CREATE TABLE IF NOT EXISTS outcome_learning (
  signal_id TEXT PRIMARY KEY,
  symbol TEXT,
  mode TEXT,
  side TEXT,
  result TEXT,
  pnl_points REAL,
  holding_minutes REAL,
  attribution TEXT,
  feature_json TEXT,
  proof_json TEXT,
  model_version TEXT,
  closed_at TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_outcome_learning_mode_side ON outcome_learning(mode, side, created_at);
CREATE TABLE IF NOT EXISTS manual_watch (
  symbol TEXT,
  exchange TEXT,
  mode TEXT,
  side TEXT,
  state TEXT DEFAULT 'WATCH',
  waiting_for TEXT,
  trigger TEXT,
  invalidation TEXT,
  reason TEXT,
  pinned INTEGER DEFAULT 0,
  source TEXT DEFAULT 'manual_search',
  payload_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(symbol, mode)
);
CREATE TABLE IF NOT EXISTS opportunity_memory (
  symbol TEXT,
  exchange TEXT DEFAULT 'NSE',
  mode TEXT DEFAULT 'delivery',
  stage TEXT DEFAULT 'Potential',
  priority_score INTEGER DEFAULT 0,
  sector TEXT,
  themes_json TEXT,
  priority_reason TEXT,
  trigger TEXT,
  invalidation TEXT,
  target_window TEXT,
  next_scan_at TEXT,
  last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
  payload_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(symbol, mode)
);
CREATE INDEX IF NOT EXISTS ix_opportunity_stage_priority ON opportunity_memory(stage, priority_score, updated_at);
CREATE TABLE IF NOT EXISTS daily_learning (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  learning_date TEXT DEFAULT (date('now')),
  payload_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS scanner_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  level TEXT,
  module TEXT,
  message TEXT,
  detail_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS priority_symbols (
  symbol TEXT,
  exchange TEXT DEFAULT 'NSE',
  mode TEXT DEFAULT 'all',
  source TEXT DEFAULT 'search',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(symbol, exchange, mode)
);
CREATE TABLE IF NOT EXISTS trade_journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT,
  exchange TEXT,
  mode TEXT,
  side TEXT,
  entry REAL,
  exit REAL,
  qty REAL,
  status TEXT,
  pnl REAL,
  holding_minutes REAL,
  notes TEXT,
  opened_at TEXT,
  closed_at TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS candles (
  instrument_key TEXT NOT NULL,
  interval TEXT NOT NULL,
  ts TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL, oi REAL,
  source TEXT DEFAULT 'upstox_historical',
  provider_ts TEXT,
  received_at TEXT,
  raw_json TEXT,
  PRIMARY KEY(instrument_key, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_lookup ON candles(instrument_key, interval, ts);
CREATE TABLE IF NOT EXISTS price_snapshots (
  instrument_key TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  symbol TEXT,
  exchange TEXT,
  ltp REAL,
  change_pct REAL,
  provider_ts TEXT,
  received_at TEXT NOT NULL,
  source TEXT DEFAULT 'upstox_ltp',
  raw_json TEXT,
  PRIMARY KEY(instrument_key, captured_at)
);
CREATE INDEX IF NOT EXISTS ix_price_snapshots_symbol_ts ON price_snapshots(symbol, captured_at);

-- v37.5 Phase 2/3: reference data. Separate tables, separate ingestion
-- cadence (daily EOD batch) from live tick data above -- a bad reference
-- data fetch must never be able to touch candles/signal_ledger.
CREATE TABLE IF NOT EXISTS delivery_data (
  trade_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  exchange TEXT DEFAULT 'NSE',
  traded_qty REAL,
  deliverable_qty REAL,
  delivery_pct REAL,
  close REAL,
  source TEXT DEFAULT 'nse_delivery',
  raw_json TEXT,
  fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(trade_date, symbol)
);
CREATE TABLE IF NOT EXISTS bulk_block_deals (
  trade_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  deal_type TEXT NOT NULL,
  client_name TEXT,
  buy_sell TEXT,
  qty REAL,
  price REAL,
  fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bulk_block_symbol_date ON bulk_block_deals(symbol, trade_date);
CREATE TABLE IF NOT EXISTS market_breadth_daily (
  ts TEXT NOT NULL,
  universe TEXT NOT NULL,
  advances INTEGER, declines INTEGER, unchanged INTEGER,
  PRIMARY KEY(ts, universe)
);
CREATE TABLE IF NOT EXISTS reference_data_runs (
  job_name TEXT NOT NULL,
  run_date TEXT NOT NULL,
  status TEXT,
  rows_written INTEGER,
  error TEXT,
  finished_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(job_name, run_date)
);

-- v65.7: persistent cache for live Upstox Company Fundamentals API results
-- (laddu_upstox_rest_client.py::fundamentals_snapshot). Was previously an in-memory-only
-- dict on ReferenceDataService (_fund_api_cache) that was lost on every
-- restart, forcing a fresh multi-endpoint live fetch (profile, key-ratios,
-- share-holdings, income/balance/cashflow statements) for every symbol the
-- first time it was opened after each restart -- the dominant cause of slow
-- first-load Stock Intelligence. This table makes that result survive
-- restarts; ok=0 rows use a short retry window (see FUND_API_FAIL_TTL_SEC in
-- reference_data_service.py) since a failed/incomplete fetch may recover on
-- next attempt, while ok=1 rows use a long TTL matching the data's real
-- refresh cadence (fundamentals don't change intraday).
CREATE TABLE IF NOT EXISTS fundamentals_cache (
  isin TEXT PRIMARY KEY,
  ok INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- v37.5 Phase 6: earnings / corporate-action calendar (event-risk input only).
CREATE TABLE IF NOT EXISTS earnings_calendar (
  symbol TEXT NOT NULL,
  event_date TEXT NOT NULL,
  event_type TEXT,
  purpose TEXT,
  fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(symbol, event_date, event_type)
);
CREATE INDEX IF NOT EXISTS idx_earnings_symbol ON earnings_calendar(symbol, event_date);
"""

# v18 intelligence-first universe. This is not a recommendation list; it is a scan-order map
# to prevent alphabetic/A-list pollution. Manual searched/pinned rows still outrank this list.
NIFTY50_CORE = [
 'RELIANCE','TCS','HDFCBANK','ICICIBANK','INFY','LT','ITC','SBIN','BHARTIARTL','AXISBANK','KOTAKBANK','HINDUNILVR','BAJFINANCE','ASIANPAINT','MARUTI','SUNPHARMA','TITAN','ULTRACEMCO','WIPRO','NTPC','POWERGRID','ONGC','TMPV','M&M','TECHM','HCLTECH','JSWSTEEL','TATASTEEL','COALINDIA','ADANIENT','ADANIPORTS','BAJAJFINSV','BAJAJ-AUTO','BPCL','BRITANNIA','CIPLA','DIVISLAB','DRREDDY','EICHERMOT','GRASIM','HEROMOTOCO','HINDALCO','INDUSINDBK','NESTLEIND','SBILIFE','SHRIRAMFIN','TATACONSUM','APOLLOHOSP','UPL','LTM'
]
NEXT50_CORE = [
 'ABB','ADANIENSOL','ADANIGREEN','ADANIPOWER','AMBUJACEM','ATGL','BANKBARODA','BHEL','BOSCHLTD','CANBK','CHOLAFIN','DABUR','DLF','DMART','GAIL','GODREJCP','HAL','HAVELLS','ICICIGI','ICICIPRULI','IOC','IRFC','JINDALSTEL','JIOFIN','LICI','LODHA','MOTHERSON','NAUKRI','PFC','PIDILITIND','PNB','RECLTD','SIEMENS','TATAPOWER','TORNTPHARM','TRENT','TVSMOTOR','VEDL','VBL','ZYDUSLIFE','BEL','INDIGO','POLYCAB','SHREECEM','MANKIND','UNIONBANK','MARICO','COLPAL','BERGEPAINT','PAGEIND'
]
NIFTY250_EXTRA = [
 'AARTIIND','ABCAPITAL','ABFRL','ALKEM','ASHOKLEY','ASTRAL','AUROPHARMA','BANDHANBNK','BANKINDIA','BIOCON','BSOFT','CAMS','COFORGE','CONCOR','CUMMINSIND','DIXON','ESCORTS','FEDERALBNK','GMRINFRA','HDFCAMC','HINDPETRO','IDEA','IDFCFIRSTB','IGL','INDHOTEL','INDUSTOWER','IPCALAB','IRCTC','JSWENERGY','KPITTECH','LUPIN','M&MFIN','MANAPPURAM','MAXHEALTH','MPHASIS','MRF','NMDC','OBEROIRLTY','OFSS','PETRONET','PHOENIXLTD','PRESTIGE','SAIL','SUNTV','SYNGENE','TATACHEM','TATAELXSI','TATATECH','TORNTPOWER','VOLTAS','YESBANK','ZEEL','PAYTM','NYKAA','POLICYBZR','ZOMATO','DELHIVERY','SONACOMS','SUPREMEIND','APLAPOLLO','CGPOWER','FACT','MAZDOCK','COCHINSHIP','BSE','CDSL','MCX','IEX','SUZLON','KAYNES','HAL','BHEL'
]
NIFTY100_CORE = list(dict.fromkeys(NIFTY50_CORE + NEXT50_CORE))
NIFTY250_CORE = list(dict.fromkeys(NIFTY50_CORE + NEXT50_CORE + NIFTY250_EXTRA))
INTELLIGENCE_SCAN_SYMBOLS = NIFTY250_CORE
# Full-universe cap for Delivery scanning (see ScanUniverseService).
FULL_UNIVERSE_LIMIT = 5000


def _is_clean_stock_symbol(row: Dict[str, Any]) -> bool:
    """Keep stock search/universe to actual equity-like symbols, not bonds/debt/NCD/security codes.

    Allows real NSE stocks like 3MINDIA, 5PAISA, 360ONE and 3PLAND.
    Excludes 1039UCL26, 9IIFL31 and similar NCD/bond/debt symbols.
    """
    sym = str(row.get("trading_symbol") or "").upper().strip()
    name = str(row.get("name") or "").upper().strip()
    if not sym:
        return False
    inst_type = str(row.get("instrument_type") or "").upper().strip()
    opt = str(row.get("option_type") or "").upper().strip()
    seg = str(row.get("segment") or row.get("exchange") or "").upper().strip()
    if opt in ("CE", "PE") or inst_type in ("CE", "PE", "FUT", "INDEX", "BOND", "NCD"):
        return False
    if not (seg.startswith("NSE") or seg.startswith("BSE")):
        return False
    letters = len(re.findall(r"[A-Z]", sym))
    digits = len(re.findall(r"\d", sym))
    if letters < 2:
        return False
    if digits >= 4:
        return False
    if re.match(r"^\d{2,}", sym):
        return False
    # one-digit coupon/rate prefix + maturity suffix, e.g. 9IIFL31, 8.5XYZ28 style simplified symbols
    if re.match(r"^\d+[A-Z]{2,}\d{2,}$", sym):
        return False
    debt_words = ("NCD", "DEBENTURE", "BOND", "SECURED", "UNSECURED", "TAX FREE", "SDL", "G-SEC", "TREASURY", "TBILL")
    if any(w in name for w in debt_words):
        return False
    if re.search(r"(NCD|GB|GS|SDL|TBILL|SG|UCL|SFL|SIFL|PFC|REC)\d*$", sym) and digits >= 2:
        return False
    return True


def connect() -> sqlite3.Connection:
    layout = StorageLayout.from_data_dir(DATA_DIR)
    if Path(DB_PATH) == layout.operational_db:
        prepare_operational_database(layout)
    else:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    # WAL keeps readers concurrent with the single operational writer; the
    # longer timeout is a final safety net, not the primary coordination mechanism.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class Store(StoreDecisionPipelineMixin):
    def __init__(
        self,
        *,
        runtime_market_state: Any | None = None,
        production_instrument_repository: Any | None = None,
        runtime_event_buffer: Any | None = None,
    ):
        self._local = threading.local()
        self.production_instrument_repository = production_instrument_repository
        self.production_instrument_read_repository: Any | None = None
        self.runtime_event_buffer = runtime_event_buffer
        # Injected post-construction by main.py when the production data
        # plane is active (see LadduRuntime.__init__); stays None in
        # compat/test mode, where set_kv/get_kv fall back to local SQLite.
        self.production_kv_repository: Any | None = None
        self.production_manual_watch_repository: Any | None = None
        self.production_opportunity_memory_repository: Any | None = None
        self.production_reference_data_repository: Any | None = None
        self.production_priority_repository: Any | None = None
        self.production_signal_ledger_repository: Any | None = None
        self.production_performance_repository: Any | None = None
        # v51 storage split, cluster 1: instrument/symbol search & cache moved
        # to core/instrument_search_repository.py. Unlike the other cluster
        # repos this one is NOT built fresh per call -- it owns the in-memory
        # symbol typeahead index, which must persist across calls to be worth
        # having. One instance, built once here, same lifetime the index had
        # as Store instance state before.
        # v60.14 P0 fix: write_lock must exist before InstrumentSearchRepository
        # is constructed below, since that repo now takes it too (previously
        # it wrote set_cached_instrument/upsert_instruments unlocked). Moved
        # up from its old position right after this block.
        self.write_lock = threading.RLock()
        # One shared runtime owner and one lake reader are exposed through Store
        # so every candle consumer sees the same merge order: curated history,
        # recent operational corrections, then canonical live bars.
        self.runtime_market_state = runtime_market_state or RuntimeMarketStateStore(RUNTIME_DB_PATH)
        self.curated_market_data = CuratedMarketDataRepository(DATA_DIR)
        self._instrument_search_repo_instance = InstrumentSearchRepository(
            lambda: self.conn, _is_clean_stock_symbol, INTELLIGENCE_SCAN_SYMBOLS, self.write_lock)
        # v62: tiered scan universe -- fast tier (NIFTY250_CORE) unchanged for
        # Intraday; full tier pulls the entire clean NSE/BSE equity
        # list from the instruments table for Delivery, which
        # were previously capped to the same 250-name list with no freshness
        # reason to be. See core/scan_universe_service.py.
        self._scan_universe_service = ScanUniverseService(
            INTELLIGENCE_SCAN_SYMBOLS, self._instrument_search_repo_instance.all_eligible_equity_keys)
        # v43.1: general-purpose lock for multi-statement write sequences on
        # the shared connection (self.conn is one sqlite3.Connection used
        # across threads with check_same_thread=False -- that flag disables
        # Python's guard, it does not itself make concurrent multi-statement
        # writes from different threads safe). Found via code audit: decision
        # ledger writes 4 statements + commit with no serialization -- see
        # VALIDATION_FINDINGS_2026-07-18.md section 9. Any caller doing more
        # than one write statement that must land atomically together should
        # acquire this. (Assigned above, before InstrumentSearchRepository
        # construction -- not reassigned here.)
        # Init connection + schema once on the main thread.
        bootstrap = connect()
        # Existing installs may have old tables without columns now used by
        # SCHEMA indexes (for example opportunity_memory.stage). Run the small
        # ALTER-only migration first, then create any missing modern objects.
        _apply_migrations(bootstrap)
        bootstrap.executescript(SCHEMA)
        _apply_migrations(bootstrap)
        bootstrap.commit()
        bootstrap.close()
        if self.production_instrument_repository is not None:
            self.sync_instrument_cache_from_authority()

    # v36.6.1: persistent symbol -> instrument resolution cache (kv-backed).
    # Previously this only lived in an in-process dict, so every backend
    # restart wiped it and the next quote poll for every visible symbol had
    # to re-run a live instrument search inline on the request thread --
    # that's what produced the "Quotes timeout Nx" pileup right after restart.
    # v51 storage split, cluster 1: instrument/symbol search & cache moved to
    # core/instrument_search_repository.py. This repo instance is built once
    # in __init__ (not fresh per call) because it owns the in-memory symbol
    # typeahead index, which must persist across calls to be worth having.
    def _instrument_search_repo(self) -> "InstrumentSearchRepository":
        return self._instrument_search_repo_instance

    def get_cached_instrument(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self._instrument_search_repo().get_cached_instrument(symbol)

    def find_instrument_by_key(self, instrument_key: str) -> Optional[Dict[str, Any]]:
        return self._instrument_search_repo().find_instrument_by_key(instrument_key)

    def set_cached_instrument(self, symbol: str, inst: Dict[str, Any]) -> None:
        return self._instrument_search_repo().set_cached_instrument(symbol, inst)

    def upsert_instruments(self, rows: List[Dict[str, Any]]) -> None:
        return self._instrument_search_repo().upsert_instruments(rows)

    def replace_active_instruments(self, rows: List[Dict[str, Any]]) -> None:
        # Production PostgreSQL is committed first. SQLite is a bounded local
        # search projection only; a cache failure cannot roll back the canonical
        # authority and will fail readiness until rebuilt from PostgreSQL.
        if self.production_instrument_repository is not None:
            from core.instrument_universe_policy import ACTIVE_UNIVERSE_REVISION
            self.production_instrument_repository.replace_active(rows, revision=ACTIVE_UNIVERSE_REVISION)
        return self._instrument_search_repo().replace_active_instruments(rows)

    def sync_instrument_cache_from_authority(self) -> int:
        if self.production_instrument_repository is None:
            return 0
        from core.instrument_universe_policy import ACTIVE_UNIVERSE_REVISION
        rows = self.production_instrument_repository.active_rows(revision=ACTIVE_UNIVERSE_REVISION)
        if not rows:
            raise RuntimeError("POSTGRES_INSTRUMENT_AUTHORITY_EMPTY")
        self._instrument_search_repo().replace_active_instruments(rows)
        return len(rows)

    def instrument_universe_stats(self) -> Dict[str, Any]:
        if self.production_instrument_repository is not None:
            from core.instrument_universe_policy import ACTIVE_UNIVERSE_REVISION
            proof = self.production_instrument_repository.proof(revision=ACTIVE_UNIVERSE_REVISION).as_dict()
            # PostgreSQL is count/revision authority. The bounded local search
            # projection publishes a few concrete BSE-only identities so the
            # installed verifier can prove inclusion through the public search
            # contract without issuing a second PostgreSQL catalogue scan.
            try:
                local = self._instrument_search_repo().instrument_universe_stats()
                proof["bse_only_sample"] = list(local.get("bse_only_sample") or [])[:5]
            except Exception:
                proof["bse_only_sample"] = []
            return proof
        return self._instrument_search_repo().instrument_universe_stats()
    def warm_symbol_index(self) -> int:
        return self._instrument_search_repo().warm_symbol_index()

    def quick_symbol_search(self, q: str, limit: int = 8) -> List[Dict[str, Any]]:
        return self._instrument_search_repo().quick_symbol_search(q, limit)

    def find_instruments(self, q: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self._instrument_search_repo().find_instruments(q, limit=limit)

    def find_any_instruments(self, q: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self._instrument_search_repo().find_any_instruments(q, limit)

    def find_index_instruments(self, q: str, limit: int = 5) -> List[Dict[str, Any]]:
        return self._instrument_search_repo().find_index_instruments(q, limit)

    def instrument_count(self) -> int:
        return self._instrument_search_repo().instrument_count()

    def canonical_equity_sample(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Bounded customer/proof sample from the canonical instrument authority.

        Production foreground reads use the dedicated interactive PostgreSQL
        pool. Compatibility/test mode may use the bounded local projection, but
        the production path never loads thousands of local rows just to return
        a 20-150 row sample.
        """
        reader = self.production_instrument_read_repository or self.production_instrument_repository
        if reader is not None and callable(getattr(reader, "equity_sample", None)):
            from core.instrument_universe_policy import ACTIVE_UNIVERSE_REVISION
            return list(reader.equity_sample(limit, revision=ACTIVE_UNIVERSE_REVISION) or [])
        return list(self._instrument_search_repo().all_eligible_equity_keys(max(20, min(150, int(limit or 100)))) or [])

    def all_eligible_equity_keys(self, limit: int = 5000) -> List[Dict[str, Any]]:
        return self._instrument_search_repo().all_eligible_equity_keys(limit)

    def all_authoritative_reference_rows(self, limit: int = 10000) -> List[Dict[str, Any]]:
        return self._instrument_search_repo().all_authoritative_reference_rows(limit)

    def tradable_nse_equity_universe(self, limit: int = 5000) -> List[Dict[str, Any]]:
        return self._instrument_search_repo().tradable_nse_equity_universe(limit)

    def symbols_to_equity_rows(self, symbols: List[str], limit: int = 500) -> List[Dict[str, Any]]:
        return self._instrument_search_repo().symbols_to_equity_rows(symbols, limit)

    def scan_universe(self, mode: str, limit: int = FULL_UNIVERSE_LIMIT) -> List[str]:
        """Canonical cash-equity symbols available to scanner screening.

        Both production desks may inspect the complete clean NSE/BSE cash
        catalogue cheaply. Expensive analysis remains separately bounded by
        each desk's immutable snapshot and shortlist policy.
        """
        return self._scan_universe_service.universe_for_mode(mode, limit)

    def intelligence_universe(self, limit: int = 500, include_rest: bool = False, offset: int = 0) -> List[Dict[str, Any]]:
        return self._instrument_search_repo().intelligence_universe(limit, include_rest, offset)

    def liquid_wide_universe(self, limit: int = 5000, lookback_days: int = 60, min_avg_traded_qty: float = 50000.0) -> List[Dict[str, Any]]:
        return self._instrument_search_repo().liquid_wide_universe(limit, lookback_days, min_avg_traded_qty)

    def liquidity_ranked_symbols(self, limit: int = 1500, lookback_days: int = 60, min_avg_turnover: float = 50_000_000.0) -> List[str]:
        return self._instrument_search_repo().liquidity_ranked_symbols(limit, lookback_days, min_avg_turnover)

    def priority_symbols_set(self) -> set:
        return self._instrument_search_repo().priority_symbols_set()

    def cleanup_scanner_artifacts(self, core_symbols: List[str]) -> Dict[str, int]:
        # Canonical production decisions and ledgers live in PostgreSQL. An
        # identity-cache cleanup must never inspect or mutate compatibility
        # SQLite decision tables when that authority is active.
        production = getattr(self, "production_canonical_decision_repository", None)
        return self._instrument_search_repo().cleanup_scanner_artifacts(
            core_symbols, include_legacy_decisions=production is None
        )

    @property
    def conn(self) -> sqlite3.Connection:
        """v35.9-fix: one sqlite3.Connection per thread. The previous code shared
        a single connection across the scanner thread, card-cache thread, and a
        5-6 worker ThreadPoolExecutor prefetch pool with zero synchronization --
        sqlite3.Connection objects are not safe for concurrent use from multiple
        threads, which caused intermittent 'bad parameter or other API misuse'
        errors (candle_store WARN spam, /api/health 500s)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect()
            self._local.conn = conn
        return conn

    def set_kv(self, key: str, value: Any) -> None:
        # KV is shared by scanner, dashboard, instrument bootstrap and operator
        # state. In production mode this delegates to ProductionKVRepository
        # (PostgreSQL, runtime_control.kv) so normal production operation
        # never writes this table to SQLite -- see data_plane/kv_repository.py.
        # v65.26.23 left this write outside the common writer lock, allowing
        # it to race repository transactions and consume the entire SQLite
        # busy_timeout. RLock permits repository helpers to call set_kv while
        # already inside the same atomic writer section (compat/test path only).
        if self.production_kv_repository is not None:
            self.production_kv_repository.set_kv(key, value)
            return
        with self.write_lock:
            self.conn.execute("INSERT INTO kv(k,v,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=CURRENT_TIMESTAMP", (key, json.dumps(value)))
            self.conn.commit()

    def delete_kv(self, key: str) -> None:
        """Delete obsolete operational KV state from the active authority."""
        if self.production_kv_repository is not None:
            deleter = getattr(self.production_kv_repository, "delete_kv", None)
            if callable(deleter):
                deleter(key)
            return
        with self.write_lock:
            self.conn.execute("DELETE FROM kv WHERE k=?", (key,))
            self.conn.commit()

    def get_kv(self, key: str, default: Any = None) -> Any:
        if self.production_kv_repository is not None:
            return self.production_kv_repository.get_kv(key, default)
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["v"])
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
        """Foreground-safe lookup of an already-materialized read model only.

        Production delegates to the isolated PostgreSQL interactive-read pool
        with hard pool/statement budgets.  SQLite is compatibility/test-only and
        remains an indexed primary-key lookup.
        """
        if self.production_kv_repository is not None:
            reader = getattr(self.production_kv_repository, "get_kv_bounded", None)
            if callable(reader):
                return reader(
                    key, default,
                    statement_timeout_ms=statement_timeout_ms,
                    pool_timeout_seconds=pool_timeout_seconds,
                )
            return default
        return self.get_kv(key, default)

    def get_kv_prefix(self, prefix: str, limit: int = 10000) -> Dict[str, Any]:
        """Background-only bounded prefix hydration for materialized read models."""
        token = str(prefix or "")
        if not token:
            return {}
        cap = max(1, min(20000, int(limit)))
        if self.production_kv_repository is not None:
            reader = getattr(self.production_kv_repository, "get_prefix", None)
            return dict(reader(token, cap) or {}) if callable(reader) else {}
        rows = self.conn.execute(
            "SELECT k,v FROM kv WHERE k LIKE ? ORDER BY k LIMIT ?",
            (token + "%", cap),
        ).fetchall()
        out: Dict[str, Any] = {}
        for row in rows:
            try:
                out[str(row["k"])] = json.loads(row["v"])
            except Exception:
                continue
        return out

    # -- sticky selected (New Entries) lifecycle -----------------------
    # Problem this fixes: the scanner recomputes "selected/new entries"
    # fresh every cycle from a top-N-by-score slice. A symbol that scored
    # #6 this cycle (cap=5) simply vanishes from the dashboard next poll,
    # even though nothing about it actually disqualified it. Traders see
    # rows disappear mid-review. This gives each (symbol, mode) a stable
    # identity that survives being briefly outranked, and only drops it
    # once it's been genuinely absent for `ttl_seconds`, or is explicitly
    # dismissed (promoted to active, invalidated, etc).
    _STICKY_KEY = "sticky_selected:v1"

    # v51 storage split, cluster 4: priority-queue methods moved to
    # core/priority_repository.py. Constructed fresh per call (same pattern
    # as ManualWatchRepository/MarketDataRepository) with the exact deps it
    # needs -- self.conn, self.write_lock, get_kv/set_kv -- rather than the
    # whole Store, so it isn't coupled back to the God object.
    def _priority_repo(self):
        if self.production_priority_repository is not None:
            return self.production_priority_repository
        return PriorityRepository(self.conn, self.write_lock, self.get_kv, self.set_kv)

    def sticky_selected_merge(self, promoted: list, ttl_seconds: int = 90, dismiss_keys: set | None = None) -> list:
        return self._priority_repo().sticky_selected_merge(promoted, ttl_seconds, dismiss_keys)

    def sticky_selected_dismiss(self, symbol: str, mode: str) -> None:
        return self._priority_repo().sticky_selected_dismiss(symbol, mode)

    # v51 storage split, cluster 2: candles/quotes/price_snapshots moved to
    # core/market_data_repository.py. Delegated the same way manual_watch
    # methods delegate to ManualWatchRepository below -- constructed fresh
    # per call against self.conn (the per-thread connection property), so
    # no change to threading/connection behavior.
    def save_candles(self, instrument_key: str, interval: str, candles: List[Dict[str, Any]], source: str = "upstox_historical") -> int:
        production = getattr(self, "production_candle_repository", None)
        if production is not None:
            return production.save_candles(instrument_key, interval, candles, source)
        return MarketDataRepository(self.conn, self.write_lock).save_candles(instrument_key, interval, candles, source)

    @staticmethod
    def _merge_candle_rows(*collections: List[Dict[str, Any]], limit: int = 2000, interval: str = "1d") -> List[Dict[str, Any]]:
        """Merge all local candle planes on one canonical UTC timestamp key.

        Parquet/DuckDB may return ``datetime`` objects while QuestDB/runtime
        rows use ISO strings.  Comparing their raw string forms can create two
        identities for the same candle and can mis-order the newest local tail.
        The visible chart, MTF and S/R read path therefore normalises every row
        through the single timeframe/timestamp authority before de-duplication.
        Later collections intentionally win for the same canonical timestamp,
        so runtime/QuestDB corrections supersede older durable copies without
        mutating the historical lake.
        """
        merged: Dict[str, Dict[str, Any]] = {}
        for rows in collections:
            for raw in rows or []:
                row = dict(raw or {})
                source_ts = row.get("timestamp") or row.get("ts") or row.get("time") or row.get("date")
                ts = canonical_timestamp(source_ts, interval)
                if not ts:
                    continue
                row["timestamp"] = ts
                merged[ts] = row
        ordered = [merged[key] for key in sorted(merged)]
        return ordered[-max(1, int(limit)):]

    def get_recent_candles(self, instrument_key: str, interval: str, limit: int = 2000) -> List[Dict[str, Any]]:
        """Read only recent-session authorities; never open the Parquet lake.

        Used by freshness/readiness probes that need the newest canonical bars,
        not retained deep history. QuestDB and the hot runtime are authoritative
        for this recent tail. Missing rows remain missing; this method never
        fabricates or silently substitutes old lake data.
        """
        cap = max(1, min(5000, int(limit)))
        market_authority = getattr(self, "production_market_time_series_repository", None)
        durable_session = []
        if market_authority is not None:
            try:
                durable_session = list(market_authority.recent_bars(instrument_key, str(interval), cap) or [])
            except Exception:
                durable_session = []
        try:
            runtime = self.runtime_market_state.canonical_bars(
                instrument_key, interval, limit=cap, include_forming=True
            )
        except Exception:
            runtime = []
        return self._merge_candle_rows(
            durable_session, runtime, limit=cap, interval=interval
        )

    def get_candles(self, instrument_key: str, interval: str, limit: int = 2000) -> List[Dict[str, Any]]:
        cap = max(1, int(limit))
        production = getattr(self, "production_candle_repository", None)
        if production is not None:
            lake = production.get_candles(instrument_key, interval, cap)
            operational = []
        else:
            lake = self.curated_market_data.get_candles(instrument_key, interval, cap)
            operational = MarketDataRepository(self.conn, self.write_lock).get_candles(instrument_key, interval, cap)
            for row in operational:
                row.setdefault("storage_plane", "operational_sqlite_recent")
        market_authority = getattr(self, "production_market_time_series_repository", None)
        durable_session = market_authority.recent_bars(instrument_key, str(interval), cap) if market_authority is not None else []
        runtime = self.runtime_market_state.canonical_bars(instrument_key, interval, limit=cap, include_forming=True)
        return self._merge_candle_rows(lake, operational, durable_session, runtime, limit=cap, interval=interval)

    def get_candles_window(
        self, instrument_key: str, interval: str, *, since: Any = None,
        before: Any = None, limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        """Read a bounded recent/deep window without making cold history foreground-wide.

        Candidate 13 routes the immutable Parquet authority through its offline
        file-window index, then merges QuestDB/runtime corrections. The economic
        candle identity is unchanged; only file discovery/planning is removed
        from the request path.
        """
        cap = max(1, int(limit))
        norm = canonical_interval(interval)
        production = getattr(self, "production_candle_repository", None)
        if production is not None and callable(getattr(production, "get_candles_window", None)):
            lake = production.get_candles_window(
                instrument_key, norm, since=since, before=before, limit=cap
            )
            operational = []
        else:
            lake = self.curated_market_data.get_candles(instrument_key, norm, cap)
            operational = MarketDataRepository(self.conn, self.write_lock).get_candles(instrument_key, norm, cap)
        market_authority = getattr(self, "production_market_time_series_repository", None)
        durable_session = []
        if market_authority is not None:
            try:
                durable_session = list(market_authority.recent_bars(instrument_key, norm, min(5000, cap)) or [])
            except Exception:
                durable_session = []
        try:
            runtime = self.runtime_market_state.canonical_bars(
                instrument_key, norm, limit=min(5000, cap), include_forming=True
            )
        except Exception:
            runtime = []
        merged = self._merge_candle_rows(lake, operational, durable_session, runtime, limit=cap, interval=norm)
        if before not in (None, ""):
            before_ts = canonical_timestamp(before, norm)
            merged = [row for row in merged if canonical_timestamp(row.get("timestamp") or row.get("ts"), norm) and canonical_timestamp(row.get("timestamp") or row.get("ts"), norm) < before_ts]
        return merged[-cap:]

    def get_candles_before(self, instrument_key: str, interval: str, before: Any, limit: int = 2000) -> List[Dict[str, Any]]:
        """Merge a bounded local page strictly older than ``before``.

        This is the browser horizontal-navigation primitive. It never performs
        provider I/O; every source is an already-local authority/projection.
        """
        cap = max(1, int(limit))
        norm = canonical_interval(interval)
        before_ts = canonical_timestamp(before, norm)
        if not before_ts:
            return []
        production = getattr(self, "production_candle_repository", None)
        if production is not None:
            reader = getattr(production, "get_candles_before", None)
            lake = reader(instrument_key, norm, before_ts, cap) if callable(reader) else [row for row in production.get_candles(instrument_key, norm, max(cap * 4, cap + 256)) if canonical_timestamp(row.get("timestamp") or row.get("ts"), norm) and canonical_timestamp(row.get("timestamp") or row.get("ts"), norm) < before_ts][-cap:]
            operational = []
        else:
            curated_reader = getattr(self.curated_market_data, "get_candles_before", None)
            lake = curated_reader(instrument_key, norm, before_ts, cap) if callable(curated_reader) else []
            operational = MarketDataRepository(self.conn, self.write_lock).get_candles_before(instrument_key, norm, before_ts, cap)
            for row in operational:
                row.setdefault("storage_plane", "operational_sqlite_recent")
        market_authority = getattr(self, "production_market_time_series_repository", None)
        durable_session = []
        if market_authority is not None:
            # QuestDB is intentionally treated as a recent-session authority. A
            # bounded recent read is filtered locally; historical pagination is
            # owned by the Parquet lake.
            try:
                durable_session = [row for row in market_authority.recent_bars(instrument_key, norm, min(5000, max(cap * 2, 256))) if canonical_timestamp(row.get("timestamp") or row.get("bar_start_ts"), norm) and canonical_timestamp(row.get("timestamp") or row.get("bar_start_ts"), norm) < before_ts][-cap:]
            except Exception:
                durable_session = []
        try:
            runtime = [row for row in self.runtime_market_state.canonical_bars(instrument_key, norm, limit=min(5000, max(cap * 2, 256)), include_forming=False) if canonical_timestamp(row.get("timestamp") or row.get("ts"), norm) and canonical_timestamp(row.get("timestamp") or row.get("ts"), norm) < before_ts][-cap:]
        except Exception:
            runtime = []
        return self._merge_candle_rows(lake, operational, durable_session, runtime, limit=cap, interval=norm)

    def get_candles_many(
        self,
        instrument_key: str,
        intervals: List[str],
        limit: int = 2000,
        *,
        expand_sparse: bool = True,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Read multiple timeframes without reopening each storage plane.

        Keys are canonical intervals (1m, 3m, 5m, 15m, 30m, 60m, 240m, 1d).
        """
        cap = max(1, int(limit))
        norms = list(dict.fromkeys(canonical_interval(value) for value in intervals or [] if value))
        production = getattr(self, "production_candle_repository", None)
        if production is not None and callable(getattr(production, "get_candles_many", None)):
            try:
                lake_many = production.get_candles_many(
                    instrument_key, norms, cap, expand_sparse=bool(expand_sparse)
                )
            except TypeError:
                # Compatibility for narrow test adapters/older retained facades.
                lake_many = production.get_candles_many(instrument_key, norms, cap)
            operational_many: Dict[str, List[Dict[str, Any]]] = {}
        else:
            lake_many = {norm: self.curated_market_data.get_candles(instrument_key, norm, cap) for norm in norms}
            operational_many = {
                norm: MarketDataRepository(self.conn, self.write_lock).get_candles(instrument_key, norm, cap)
                for norm in norms
            }
        market_authority = getattr(self, "production_market_time_series_repository", None)
        durable_many = (
            market_authority.recent_bars_many(instrument_key, norms, cap)
            if market_authority is not None and callable(getattr(market_authority, "recent_bars_many", None))
            else {norm: (market_authority.recent_bars(instrument_key, norm, cap) if market_authority is not None else []) for norm in norms}
        )
        out: Dict[str, List[Dict[str, Any]]] = {}
        for norm in norms:
            runtime = self.runtime_market_state.canonical_bars(
                instrument_key, norm, limit=cap, include_forming=True
            )
            out[norm] = self._merge_candle_rows(
                lake_many.get(norm, []),
                operational_many.get(norm, []),
                durable_many.get(norm, []),
                runtime,
                limit=cap,
                interval=norm,
            )
        return out

    def candle_coverage(self, instrument_key: str, interval: str) -> Dict[str, Any]:
        """Return cheap canonical coverage without materialising bar payloads.

        Historical qualification is owned by the indexed Parquet catalogue.
        Runtime rows may extend the latest timestamp, but counts from duplicate
        storage planes are never summed.  This keeps scanner/status endpoints
        bounded and prevents hundreds of 5,000-row QuestDB reads.
        """
        production = getattr(self, "production_candle_repository", None)
        if production is not None:
            lake = dict(production.candle_coverage(instrument_key, interval) or {})
            operational = {"count": 0, "first": None, "last": None, "last_received_at": None}
        else:
            operational = dict(MarketDataRepository(self.conn, self.write_lock).candle_coverage(instrument_key, interval) or {})
            lake = dict(self.curated_market_data.candle_coverage(instrument_key, interval) or {})
        runtime_rows = self.runtime_market_state.canonical_bars(
            instrument_key, interval, limit=2, include_forming=True
        )
        base = lake if int(lake.get("count") or 0) >= int(operational.get("count") or 0) else operational
        runtime_first = runtime_rows[0].get("timestamp") if runtime_rows else None
        runtime_last = runtime_rows[-1].get("timestamp") if runtime_rows else None
        firsts = [value for value in (base.get("first"), runtime_first) if value]
        lasts = [value for value in (base.get("last"), runtime_last) if value]
        return {
            "count": max(int(base.get("count") or 0), len(runtime_rows)),
            "first": min(firsts) if firsts else None,
            "last": max(lasts) if lasts else None,
            "last_received_at": max([value for value in (base.get("last_received_at"), (runtime_rows[-1].get("received_at") if runtime_rows else None)) if value], default=None),
            "source": "parquet_catalog+runtime_tail" if production is not None else "curated_catalog+operational_catalog+runtime_tail",
            "planes": {
                "lake": int(lake.get("count") or 0),
                "operational": int(operational.get("count") or 0),
                "questdb": "not_materialised",
                "runtime_tail": len(runtime_rows),
            },
            "catalog_state": base.get("catalog_state"),
            "file_count": int(base.get("file_count") or 0),
            "indexed": bool(base.get("indexed", int(base.get("count") or 0) > 0)),
        }

    def recent_daily_candles_many(self, instrument_keys: List[str], limit_per_key: int = 25) -> Dict[str, List[Dict[str, Any]]]:
        production = getattr(self, "production_candle_repository", None)
        if production is not None:
            operational = {}
            lake = production.recent_daily_candles_many(instrument_keys, limit_per_key)
        else:
            operational = MarketDataRepository(self.conn, self.write_lock).recent_daily_candles_many(instrument_keys, limit_per_key)
            lake = self.curated_market_data.recent_daily_candles_many(instrument_keys, limit_per_key)
        out: Dict[str, List[Dict[str, Any]]] = {}
        for key in list(dict.fromkeys(str(value or "").strip() for value in instrument_keys or [] if str(value or "").strip())):
            rows = self._merge_candle_rows(lake.get(key, []), operational.get(key, []), limit=limit_per_key)
            out[key] = rows
        return out

    def save_quote(self, q: Dict[str, Any]) -> None:
        self.save_quotes([q])
        return None

    def save_quotes(self, quotes: List[Dict[str, Any]]) -> int:
        if self.production_instrument_repository is not None:
            # REST snapshots are a rebuildable hot read model.  They never
            # write SQLite and never become tick chronology unless accepted by
            # the canonical live stream with a verified provider timestamp.
            return int(self.runtime_market_state.save_latest_quotes(quotes or []) or 0)
        return MarketDataRepository(self.conn, self.write_lock).save_quotes(quotes)

    def price_snapshots(self, symbol: str = "", instrument_key: str = "", limit: int = 1000,
                        start: str = "", end: str = "") -> List[Dict[str, Any]]:
        cap = max(1, int(limit))
        lake = self.curated_market_data.price_snapshots(
            symbol=symbol, instrument_key=instrument_key, limit=cap, start=start, end=end
        )
        operational = [] if self.production_instrument_repository is not None else MarketDataRepository(self.conn, self.write_lock).price_snapshots(
            symbol, instrument_key, cap, start, end
        )
        # Newest operational corrections override the same lake identity.
        merged: Dict[tuple[str, str], Dict[str, Any]] = {}
        for rows in (lake, operational):
            for raw in rows or []:
                row = dict(raw or {})
                key = (str(row.get("instrument_key") or ""), str(row.get("captured_at") or ""))
                if not key[1]:
                    continue
                merged[key] = row
        return sorted(merged.values(), key=lambda row: str(row.get("captured_at") or ""), reverse=True)[:cap]

    def latest_quotes_by_symbol(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        if self.production_instrument_repository is not None:
            return {str(row.get("symbol") or "").upper(): row for row in self.runtime_market_state.latest_quotes(symbols) if row.get("symbol")}
        return MarketDataRepository(self.conn, self.write_lock).latest_quotes_by_symbol(symbols)

    def recent_nse_equity_quotes(self, limit: int = 250) -> List[Dict[str, Any]]:
        if self.production_instrument_repository is not None:
            rows = [row for row in self.runtime_market_state.latest_quotes(()) if str(row.get("exchange") or "").upper() == "NSE"]
            return rows[-max(1, int(limit)):]
        return MarketDataRepository(self.conn, self.write_lock).recent_nse_equity_quotes(limit)

    def storage_stats(self) -> Dict[str, Any]:
        if self.production_instrument_repository is not None:
            return {
                "authority": "POSTGRESQL_QUESTDB_PARQUET",
                "compatibility_projection_authority": False,
                "policy": "production status never scans compatibility SQLite",
            }
        return MarketDataRepository(self.conn, self.write_lock).storage_stats()

    def prune_runtime_data(self, *, now_iso: str, chunk_size: int = 5000, max_chunks_per_table: int = 8) -> Dict[str, Any]:
        if self.production_instrument_repository is not None:
            # This SQLite file is a non-authoritative compatibility/search
            # projection in v68.  Bounded cleanup may remove obsolete market
            # cache rows, but never canonical decision/signal/position state.
            result = MarketDataRepository(self.conn, self.write_lock).prune_runtime_data(
                now_iso=now_iso,
                chunk_size=chunk_size,
                max_chunks_per_table=max_chunks_per_table,
                include_decisions=False,
            )
            result["state"] = "BOUNDED_COMPATIBILITY_PROJECTION_CLEANUP"
            result["authoritative_state_pruned"] = False
            return result
        return MarketDataRepository(self.conn, self.write_lock).prune_runtime_data(
            now_iso=now_iso, chunk_size=chunk_size, max_chunks_per_table=max_chunks_per_table
        )

    # v51 storage split, cluster 3: decisions/signals engine moved to
    # core/signal_ledger_repository.py. Constructed fresh per call, same
    # pattern as ManualWatchRepository/MarketDataRepository, against
    # self.conn and self.event (event() already lives in
    # SystemHealthRepository; passed through rather than duplicated here).
    def _signal_repo(self):
        if self.production_signal_ledger_repository is not None:
            return self.production_signal_ledger_repository
        # Compatibility/test mode only. Production projects this API from the
        # one PostgreSQL canonical-decision authority.
        return SignalLedgerRepository(self.conn, self.event, self.write_lock)

    # Kept as a Store-level shim (module-level is_actionable_signal, no self
    # access) because tests call it unbound as Store._decision_is_signal(None, d).
    def _decision_is_signal(self, d: Dict[str, Any]) -> bool:
        return is_actionable_signal(d)

    def evaluate_signal_from_candles(self, row: Dict[str, Any], candles: List[Dict[str, Any]], interval: str = "1minute") -> Dict[str, Any]:
        return self._signal_repo().evaluate_signal_from_candles(row, candles, interval)

    def refresh_open_signals_from_quotes(self, quotes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        return self._signal_repo().refresh_open_signals_from_quotes(quotes)

    def lifecycle_decisions(self, mode: str = "all", limit: int = 5000) -> List[Dict[str, Any]]:
        production = getattr(self, "production_canonical_decision_repository", None)
        if production is not None and callable(getattr(production, "lifecycle_rows", None)):
            return production.lifecycle_rows(mode, limit)
        # Test/compatibility mode retains the same source data but does not gain
        # a second production authority.
        return self.latest_decisions(mode, limit)

    def latest_decisions(self, mode: str = "all", limit: int = 50) -> List[Dict[str, Any]]:
        production = getattr(self, "production_canonical_decision_repository", None)
        if production is not None:
            return production.latest_decisions(mode, limit)
        return self._signal_repo().latest_decisions(mode, limit)

    def add_priority(self, symbol: str, exchange: str, mode: str, source: str) -> None:
        return self._priority_repo().add_priority(symbol, exchange, mode, source)

    def auto_live_priority(self, limit: int = 6, cursor: int = 0) -> List[Dict[str, Any]]:
        return self._priority_repo().auto_live_priority(NIFTY250_CORE, limit, cursor)

    def priority_list(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self._priority_repo().priority_list(NIFTY250_CORE, limit)

    # ------------------------------------------------------------------
    # v37.5 Phase 2/3: reference data (delivery %, bulk/block deals,
    # market breadth). Separate write paths from the live
    # tick pipeline on purpose -- a malformed NSE CSV must never be able
    # to raise into the scanner loop or touch signal_ledger/candles.
    # ------------------------------------------------------------------
    # v51 storage split, cluster 5: reference data (delivery %, bulk/block
    # deals, market breadth, reference-job status
    # chain, earnings calendar) moved to core/reference_data_repository.py.
    # _normalize_trade_date kept as a thin classmethod wrapper since callers
    # elsewhere may reference Store._normalize_trade_date directly.
    def _ref_repo(self) -> "ReferenceDataRepository":
        return ReferenceDataRepository(self.conn, self.write_lock)

    @classmethod
    def _normalize_trade_date(cls, raw: str) -> str:
        return ReferenceDataRepository._normalize_trade_date(raw)

    def migrate_delivery_data_dates(self) -> Dict[str, int]:
        return self._ref_repo().migrate_delivery_data_dates()

    def save_delivery_rows(self, rows: List[Dict[str, Any]], source: str = "nse_delivery") -> int:
        production = getattr(self, "production_delivery_repository", None)
        if production is not None:
            return production.save_delivery_rows(rows, source)
        return self._ref_repo().save_delivery_rows(rows, source)

    def latest_delivery(self, symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
        production = getattr(self, "production_delivery_repository", None)
        if production is not None:
            return production.latest_delivery(symbol, limit)
        return self._ref_repo().latest_delivery(symbol, limit)

    def save_delivery_data(self, trade_date: str, rows: List[Dict[str, Any]]) -> int:
        production = getattr(self, "production_delivery_repository", None)
        if production is not None:
            return production.save_delivery_data(trade_date, rows)
        return self._ref_repo().save_delivery_data(trade_date, rows)

    def get_delivery_data(self, symbol: str, days: int = 10) -> List[Dict[str, Any]]:
        production = getattr(self, "production_delivery_repository", None)
        if production is not None:
            return production.get_delivery_data(symbol, days)
        return self._ref_repo().get_delivery_data(symbol, days)

    # v69.8.3: reference_data_repository.py cluster 5 methods below (bulk/
    # block deals through earnings calendar) delegate to
    # ProductionReferenceDataRepository (PostgreSQL `reference` schema) when
    # the production data plane is active -- see
    # data_plane/reference_data_repository.py and
    # infra/postgres/operational/006_reference_data_authority.sql. Delivery
    # methods above are untouched; they already delegate separately to
    # production_delivery_repository (DeliveryLakeRepository).
    def save_bulk_block_deals(self, trade_date: str, deal_type: str, rows: List[Dict[str, Any]]) -> int:
        if self.production_reference_data_repository is not None:
            return self.production_reference_data_repository.save_bulk_block_deals(trade_date, deal_type, rows)
        return self._ref_repo().save_bulk_block_deals(trade_date, deal_type, rows)

    def get_bulk_block_deals(self, symbol: str = "", days: int = 5) -> List[Dict[str, Any]]:
        if self.production_reference_data_repository is not None:
            return self.production_reference_data_repository.get_bulk_block_deals(symbol, days)
        return self._ref_repo().get_bulk_block_deals(symbol, days)

    def save_market_breadth(self, universe: str, advances: int, declines: int, unchanged: int) -> None:
        if self.production_reference_data_repository is not None:
            return self.production_reference_data_repository.save_market_breadth(universe, advances, declines, unchanged)
        return self._ref_repo().save_market_breadth(universe, advances, declines, unchanged)

    def get_latest_market_breadth(self, universe: str = "NIFTY250_CORE") -> Optional[Dict[str, Any]]:
        if self.production_reference_data_repository is not None:
            return self.production_reference_data_repository.get_latest_market_breadth(universe)
        return self._ref_repo().get_latest_market_breadth(universe)

    def record_reference_run(self, job_name: str, run_date: str, status: str, rows_written: int, error: str = "") -> None:
        if self.production_reference_data_repository is not None:
            return self.production_reference_data_repository.record_reference_run(job_name, run_date, status, rows_written, error)
        return self._ref_repo().record_reference_run(job_name, run_date, status, rows_written, error)

    def save_fundamentals_cache(self, isin: str, ok: bool, payload: Dict[str, Any]) -> None:
        if self.production_reference_data_repository is not None:
            return self.production_reference_data_repository.save_fundamentals_cache(isin, ok, payload)
        return self._ref_repo().save_fundamentals_cache(isin, ok, payload)

    def get_fundamentals_cache(self, isin: str) -> Optional[Dict[str, Any]]:
        if self.production_reference_data_repository is not None:
            return self.production_reference_data_repository.get_fundamentals_cache(isin)
        return self._ref_repo().get_fundamentals_cache(isin)

    def get_all_fundamentals_cache(self) -> Dict[str, Dict[str, Any]]:
        if self.production_reference_data_repository is not None:
            return self.production_reference_data_repository.get_all_fundamentals_cache()
        return self._ref_repo().get_all_fundamentals_cache()

    def reference_run_status(self) -> List[Dict[str, Any]]:
        if self.production_reference_data_repository is not None:
            return self.production_reference_data_repository.reference_run_status()
        return self._ref_repo().reference_run_status()



    def save_earnings_calendar(self, rows: List[Dict[str, Any]]) -> int:
        if self.production_reference_data_repository is not None:
            return self.production_reference_data_repository.save_earnings_calendar(rows)
        return self._ref_repo().save_earnings_calendar(rows)

    def get_upcoming_earnings(self, symbol: str, within_days: int = 3) -> List[Dict[str, Any]]:
        if self.production_reference_data_repository is not None:
            return self.production_reference_data_repository.get_upcoming_earnings(symbol, within_days)
        return self._ref_repo().get_upcoming_earnings(symbol, within_days)

    def event_risk_symbols(self, within_days: int = 3) -> Dict[str, str]:
        if self.production_reference_data_repository is not None:
            return self.production_reference_data_repository.event_risk_symbols(within_days)
        return self._ref_repo().event_risk_symbols(within_days)

    def system_health_snapshot(self) -> Dict[str, Any]:
        if self.production_performance_repository is not None:
            return self.production_performance_repository.health_snapshot()
        return SystemHealthRepository(self.conn, self.write_lock).system_health_snapshot()

    def clear_priority_symbols(self, source_like: str = "") -> int:
        return self._priority_repo().clear_priority_symbols(source_like)

    def event(self, level: str, module: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        if self.runtime_event_buffer is not None:
            self.runtime_event_buffer.append(level, module, message, detail)
            return None
        return SystemHealthRepository(self.conn, self.write_lock).event(level, module, message, detail)

    def events(self, limit: int = 80) -> List[Dict[str, Any]]:
        if self.runtime_event_buffer is not None:
            return self.runtime_event_buffer.recent(limit)
        return SystemHealthRepository(self.conn, self.write_lock).events(limit)

    def open_signal_rows(self, limit: int = 500) -> List[Dict[str, Any]]:
        return self._signal_repo().open_signal_rows(limit)

    def settle_signal_by_id(self, signal_id: str, status: str, result: str, exit_price, pnl, proof: Optional[Dict[str, Any]] = None) -> None:
        return self._signal_repo().settle_signal_by_id(signal_id, status, result, exit_price, pnl, proof)

    def outcome_learning_rows(self, limit: int = 5000) -> List[Dict[str, Any]]:
        if self.production_performance_repository is not None:
            return self.production_performance_repository.outcome_learning_rows(limit)
        rows = self.conn.execute("SELECT * FROM outcome_learning ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def selected_signals(self, mode: str = "all", limit: int = 50) -> List[Dict[str, Any]]:
        return self._signal_repo().selected_signals(mode, limit)

    def cancel_invalid_carry_shorts(self) -> int:
        return self._signal_repo().cancel_invalid_carry_shorts()

    def expire_fast_desk_signals(self, reason: str = "trade_window_expired") -> int:
        return self._signal_repo().expire_fast_desk_signals(reason)

    def _manual_watch_repo(self):
        return self.production_manual_watch_repository or ManualWatchRepository(self.conn, self.write_lock)

    def upsert_manual_watch(self, d: Dict[str, Any], source: str = "manual_search") -> None:
        return self._manual_watch_repo().upsert(d, source)

    def remove_manual_watch(self, symbol: str, mode: str = "all") -> int:
        return self._manual_watch_repo().remove(symbol, mode)

    def clear_manual_watch(self, keep_pinned: bool = True) -> int:
        return self._manual_watch_repo().clear(keep_pinned)

    def pin_manual_watch(self, symbol: str, mode: str, pinned: bool = True) -> int:
        return self._manual_watch_repo().pin(symbol, mode, pinned)

    def manual_watch_rows(self, mode: str = "all", limit: int = 60) -> List[Dict[str, Any]]:
        rows=self._manual_watch_repo().rows("all" if mode in ("intraday","delivery") else mode, limit*3)
        return [r for r in rows if mode=="all" or str(r.get("mode") or "") in _desk_modes(mode)][:limit]


    def _opportunity_memory_repo(self):
        return self.production_opportunity_memory_repository or OpportunityMemoryRepository(self.conn, _desk_modes, self.write_lock)

    def upsert_opportunity_memory(self, d: Dict[str, Any], source: str = "auto_discovery") -> None:
        return self._opportunity_memory_repo().upsert(d, source)

    def opportunity_candidates(self, mode: str = "all", limit: int = 60) -> List[Dict[str, Any]]:
        return self._opportunity_memory_repo().candidates(mode, limit)

    def retire_scanner_candidate(self, symbol: str, mode: str) -> Dict[str, int]:
        """Move a failed/gated scanner candidate off active surfaces without deleting audit evidence.

        The immutable scanner/event/research evidence remains elsewhere. Only
        replaceable opportunity/manual-watch projections are retired. Explicit
        user-pinned/manual rows are preserved.
        """
        removed_memory = int(self._opportunity_memory_repo().remove(symbol, mode) or 0)
        remove_generated = getattr(self._manual_watch_repo(), "remove_generated", None)
        removed_watch = int(remove_generated(symbol, mode) or 0) if callable(remove_generated) else 0
        return {"opportunity_memory": removed_memory, "generated_watch": removed_watch}

    def opportunity_summary(self) -> Dict[str, Any]:
        return self._opportunity_memory_repo().summary()

    # v51 storage split, cluster 9: learning/performance/trade journal moved
    # to core/performance_journal_repository.py. This finishes the storage.py
    # split -- constructed fresh per call, same pattern as every other
    # cluster repo, against self.conn.
    def _perf_repo(self):
        if self.production_performance_repository is not None:
            return self.production_performance_repository
        return PerformanceJournalRepository(self.conn, self.write_lock)

    def record_daily_learning(self, payload: Dict[str, Any]) -> None:
        return self._perf_repo().record_daily_learning(payload)

    def latest_daily_learning(self, limit: int = 5) -> List[Dict[str, Any]]:
        return self._perf_repo().latest_daily_learning(limit)

    def daily_performance(self, start_date: str = "", end_date: str = "") -> List[Dict[str, Any]]:
        return self._perf_repo().daily_performance(start_date, end_date)

    def mode_performance_alltime(self) -> List[Dict[str, Any]]:
        return self._perf_repo().mode_performance_alltime()

    def log_trade(self, data: Dict[str, Any]) -> int:
        return self._perf_repo().log_trade(data)

    def update_trade(self, trade_id: int, data: Dict[str, Any]) -> bool:
        return self._perf_repo().update_trade(trade_id, data)

    def delete_trade(self, trade_id: int) -> bool:
        return self._perf_repo().delete_trade(trade_id)

    def my_trades(self, limit: int = 200, mode: str = "all", start_date: str = "", end_date: str = "") -> List[Dict[str, Any]]:
        return self._perf_repo().my_trades(limit, mode, start_date, end_date)

    def my_trades_summary(self, mode: str = "all", start_date: str = "", end_date: str = "") -> Dict[str, Any]:
        return self._perf_repo().my_trades_summary(mode, start_date, end_date)

    def trade_journal(self, limit: int = 50, mode: str = "all", start_date: str = "", end_date: str = "", month: str = "", year: str = "", outcome: str = "") -> List[Dict[str, Any]]:
        return self._perf_repo().trade_journal(limit, mode, start_date, end_date, month, year, outcome)
