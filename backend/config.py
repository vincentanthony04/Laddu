from __future__ import annotations
import os
from pathlib import Path

APP_NAME = "Project Laddu"
SERVICE_NAME = "ProjectLaddu"
# Single source of truth for the build/version string. Every surface that
# shows a version to the user (API /api/health, server header, frontend
# pill) must read this value -- do not hardcode a version string anywhere
# else. Update this line, and only this line, on each release.
APP_VERSION = "v131.1.6"
BUILD_MARKER = "production-usability-r8-pl46-defect-cluster-closure-8086"
PRODUCT_MODE = "AUTOMATIC_MODEL_PAPER_ONLY"
BROKER_ORDER_EXECUTION_ENABLED = False

# PL42 governed adaptive historical ML/WFA policy. 500 sessions is the default
# Delivery *reference* depth, never a hard cap. Each trade mode owns its own
# configurable safety floor/reference/optional resource ceiling/recency decay.
# A maximum of 0 means use all eligible history available before each fold.
def _bounded_positive_int_env(name: str, default: int, *, floor: int = 1) -> int:
    try:
        return max(int(floor), int(os.environ.get(name, str(default))))
    except Exception:
        return max(int(floor), int(default))

def _nonnegative_int_env(name: str, default: int = 0) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except Exception:
        return max(0, int(default))

# Backward-compatible names now describe the Delivery reference policy only.
ML_HISTORICAL_TRAIN_MIN_DAYS = _bounded_positive_int_env(
    "PROJECT_LADDU_ML_TRAIN_MIN_DAYS", 252, floor=126
)
ML_HISTORICAL_TRAIN_TARGET_DAYS = _bounded_positive_int_env(
    "PROJECT_LADDU_ML_TRAIN_TARGET_DAYS", 500, floor=ML_HISTORICAL_TRAIN_MIN_DAYS
)
ML_HISTORICAL_TRAIN_MAX_DAYS = _nonnegative_int_env("PROJECT_LADDU_ML_TRAIN_MAX_DAYS", 0)
if ML_HISTORICAL_TRAIN_MAX_DAYS and ML_HISTORICAL_TRAIN_MAX_DAYS < ML_HISTORICAL_TRAIN_MIN_DAYS:
    ML_HISTORICAL_TRAIN_MAX_DAYS = ML_HISTORICAL_TRAIN_MIN_DAYS

ML_DELIVERY_TRAIN_MIN_DAYS = _bounded_positive_int_env(
    "PROJECT_LADDU_DELIVERY_ML_TRAIN_MIN_DAYS", ML_HISTORICAL_TRAIN_MIN_DAYS, floor=126
)
ML_DELIVERY_TRAIN_REFERENCE_DAYS = _bounded_positive_int_env(
    "PROJECT_LADDU_DELIVERY_ML_TRAIN_REFERENCE_DAYS", ML_HISTORICAL_TRAIN_TARGET_DAYS,
    floor=ML_DELIVERY_TRAIN_MIN_DAYS
)
ML_DELIVERY_TRAIN_MAX_DAYS = _nonnegative_int_env(
    "PROJECT_LADDU_DELIVERY_ML_TRAIN_MAX_DAYS", ML_HISTORICAL_TRAIN_MAX_DAYS
)
if ML_DELIVERY_TRAIN_MAX_DAYS and ML_DELIVERY_TRAIN_MAX_DAYS < ML_DELIVERY_TRAIN_MIN_DAYS:
    ML_DELIVERY_TRAIN_MAX_DAYS = ML_DELIVERY_TRAIN_MIN_DAYS
ML_DELIVERY_SYMBOL_MIN_DAYS = _bounded_positive_int_env(
    "PROJECT_LADDU_DELIVERY_ML_SYMBOL_MIN_DAYS", 126, floor=60
)
ML_DELIVERY_RECENCY_HALF_LIFE_DAYS = _bounded_positive_int_env(
    "PROJECT_LADDU_DELIVERY_ML_RECENCY_HALF_LIFE_DAYS", 504, floor=63
)

ML_INTRADAY_TRAIN_MIN_DAYS = _bounded_positive_int_env(
    "PROJECT_LADDU_INTRADAY_ML_TRAIN_MIN_DAYS", 60, floor=20
)
ML_INTRADAY_TRAIN_REFERENCE_DAYS = _bounded_positive_int_env(
    "PROJECT_LADDU_INTRADAY_ML_TRAIN_REFERENCE_DAYS", 126, floor=ML_INTRADAY_TRAIN_MIN_DAYS
)
ML_INTRADAY_TRAIN_MAX_DAYS = _nonnegative_int_env("PROJECT_LADDU_INTRADAY_ML_TRAIN_MAX_DAYS", 0)
if ML_INTRADAY_TRAIN_MAX_DAYS and ML_INTRADAY_TRAIN_MAX_DAYS < ML_INTRADAY_TRAIN_MIN_DAYS:
    ML_INTRADAY_TRAIN_MAX_DAYS = ML_INTRADAY_TRAIN_MIN_DAYS
ML_INTRADAY_SYMBOL_MIN_DAYS = _bounded_positive_int_env(
    "PROJECT_LADDU_INTRADAY_ML_SYMBOL_MIN_DAYS", 40, floor=10
)
ML_INTRADAY_RECENCY_HALF_LIFE_DAYS = _bounded_positive_int_env(
    "PROJECT_LADDU_INTRADAY_ML_RECENCY_HALF_LIFE_DAYS", 63, floor=20
)


# v68 production data-plane authority. The installed production launcher writes
# these values from secure/data-plane.env.ps1. Compatibility mode is explicit
# and must never be reported as production operational.
DATA_PLANE_MODE = os.environ.get("PROJECT_LADDU_DATA_PLANE_MODE", "test").strip().lower()
OPERATIONAL_POSTGRES_DSN = os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", "").strip()
GOVERNANCE_POSTGRES_DSN = os.environ.get("PROJECT_LADDU_GOVERNANCE_DSN", "").strip()
QUESTDB_HTTP_URL = os.environ.get("PROJECT_LADDU_QUESTDB_HTTP_URL", "http://127.0.0.1:59000").rstrip("/")
DEFAULT_PORT = int(os.environ.get("PROJECT_LADDU_PORT", "8086"))
# Localhost-first security boundary.  LAN exposure must be an explicit operator
# decision because the application contains scan, ledger and model-governance
# mutation endpoints.
BIND_HOST = os.environ.get("PROJECT_LADDU_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
MAX_REQUEST_BODY_BYTES = max(4096, int(os.environ.get("PROJECT_LADDU_MAX_REQUEST_BODY", "1048576")))
INSTALL_DIR = Path(os.environ.get("PROJECT_LADDU_HOME", r"C:\ProgramData\ProjectLaddu" if os.name == "nt" else "/tmp/ProjectLaddu"))
DATA_DIR = INSTALL_DIR / "data"
LOG_DIR = INSTALL_DIR / "logs"
SECURE_DIR = INSTALL_DIR / "secure"
FRONTEND_DIR = INSTALL_DIR / "frontend"
ASSET_DIR = INSTALL_DIR / "frontend" / "assets"
LEGACY_DB_PATH = DATA_DIR / "project_laddu.sqlite3"
OPERATIONAL_DIR = Path(os.environ.get("PROJECT_LADDU_OPERATIONAL_DIR", str(DATA_DIR / "operational")))
RUNTIME_DIR = Path(os.environ.get("PROJECT_LADDU_RUNTIME_DIR", str(DATA_DIR / "runtime")))
LAKE_DIR = Path(os.environ.get("PROJECT_LADDU_LAKE_DIR", str(DATA_DIR / "lake")))
ANALYTICS_DIR = Path(os.environ.get("PROJECT_LADDU_ANALYTICS_DIR", str(DATA_DIR / "analytics")))
MANIFESTS_DIR = Path(os.environ.get("PROJECT_LADDU_MANIFESTS_DIR", str(DATA_DIR / "manifests")))
SNAPSHOTS_DIR = Path(os.environ.get("PROJECT_LADDU_SNAPSHOTS_DIR", str(DATA_DIR / "snapshots")))
MODEL_DIR = Path(os.environ.get("PROJECT_LADDU_MODEL_DIR", str(DATA_DIR / "models")))
LEGACY_OPERATIONAL_SQLITE_PATH = OPERATIONAL_DIR / "project_laddu_ops.sqlite3"
COMPATIBILITY_PROJECTION_DB_PATH = Path(os.environ.get(
    "PROJECT_LADDU_COMPATIBILITY_DB",
    str(RUNTIME_DIR / "compatibility" / "compatibility_projection.sqlite3"),
))
# In v68 production, PostgreSQL is operational authority.  The remaining
# SQLite surface is a bounded, rebuildable cache/read-model projection and
# may never select the migrated multi-GB operational database. Compatibility
# and tests use an isolated disposable store explicitly. There is no installed
# compatibility runtime mode in the current production architecture.
DB_PATH = (
    COMPATIBILITY_PROJECTION_DB_PATH
    if DATA_PLANE_MODE == "production"
    else Path(os.environ.get("PROJECT_LADDU_OPERATIONAL_DB", str(LEGACY_OPERATIONAL_SQLITE_PATH)))
)
RUNTIME_DB_PATH = Path(os.environ.get("PROJECT_LADDU_RUNTIME_DB", str(RUNTIME_DIR / "market_session.sqlite3")))
ANALYTICS_DB_PATH = Path(os.environ.get("PROJECT_LADDU_ANALYTICS_DB", str(ANALYTICS_DIR / "project_laddu_quant.duckdb")))
TOKEN_FILE = SECURE_DIR / "upstox_token.dpapi"
# Linux/Docker equivalent: no DPAPI available, so a plain owner-only file is
# used instead. Same "run a script to write the token" model as Windows.
LINUX_TOKEN_FILE = Path(os.environ.get("PROJECT_LADDU_LINUX_TOKEN_FILE", str(SECURE_DIR / "upstox_token.txt")))
TOKEN_HELPER = INSTALL_DIR / "backend" / "security" / "token_helper.ps1"

UPSTOX_BASE_URL = os.environ.get("UPSTOX_BASE_URL", "https://api.upstox.com")
# Binding active-universe authority.  Do not download the provider-wide
# complete master during normal startup: it contains derivatives, debt,
# commodities and other contracts that are outside the current Laddu scope.
# Both exchange files are required so BSE-only cash equities are retained.
INSTRUMENT_URLS = [
    "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
    "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz",
]

# Production dispatch is deliberately limited to two desks.  Read/query
# aggregations may still use ``all`` but it is not an executable mode.
MODE_REFRESH_SECONDS = {
    "intraday": 10,
    "delivery": 1800,
    "all": 10,
}

# v35.5: financial-logic settings. TRADING_CAPITAL/RISK_PER_TRADE_PCT drive
# position sizing; MIN_RR_* are hard promotion gates (a setup below this R:R
# cannot become TRADE/ACCUMULATE regardless of score). User-editable via env
# vars so this can be tuned per account without a code change.
TRADING_CAPITAL = float(os.environ.get("PROJECT_LADDU_CAPITAL", "500000"))
RISK_PER_TRADE_PCT = float(os.environ.get("PROJECT_LADDU_RISK_PCT", "1.0"))  # % of capital risked per trade

MIN_RR_INTRADAY = 1.3
MIN_RR_DELIVERY = 1.8
# Approx round-trip cost (brokerage + STT + slippage) as a fraction of trade value.
EST_ROUNDTRIP_COST_PCT = 0.15

MAX_FASTLANE = 50
MAX_INTRADAY = 80
DEEP_SCAN_BATCH = 120

# Scanner budgets. The immutable Intraday snapshot is the complete canonical
# cash-equity population for supported intraday series/groups. Cheap quote
# coverage screens that whole population in bounded batches; only the ranked
# shortlist receives expensive analysis. MAX_INTRADAY is retained for legacy
# compatibility and is not a universe ceiling.
INTRADAY_PRIORITY_LANE = int(os.environ.get("PROJECT_LADDU_INTRADAY_PRIORITY_LANE", "18"))
INTRADAY_COVERAGE_LANE = int(os.environ.get("PROJECT_LADDU_INTRADAY_COVERAGE_LANE", "182"))
# Quote-only universe coverage is intentionally independent from deep analysis.
# Keep it frequent enough that a full 1,963-symbol sweep does not look frozen,
# while remaining far below the broker quote budget. Both values are operator-
# configurable and the exact next-batch time is exposed to the UI.
INTRADAY_COVERAGE_OPEN_SECONDS = max(15, int(os.environ.get("PROJECT_LADDU_COVERAGE_OPEN_SECONDS", "30")))
INTRADAY_COVERAGE_CLOSED_SECONDS = max(30, int(os.environ.get("PROJECT_LADDU_COVERAGE_CLOSED_SECONDS", "60")))
INTRADAY_QUOTE_BATCH = int(os.environ.get("PROJECT_LADDU_INTRADAY_QUOTE_BATCH", "200"))
INTRADAY_SCREEN_SHORTLIST = max(12, int(os.environ.get("PROJECT_LADDU_INTRADAY_SCREEN_SHORTLIST", "48")))
INTRADAY_DEEP_ANALYSIS = int(os.environ.get("PROJECT_LADDU_INTRADAY_DEEP_ANALYSIS", "12"))
INTRADAY_SCAN_BUDGET_SEC = float(os.environ.get("PROJECT_LADDU_INTRADAY_SCAN_BUDGET_SEC", "30"))

# v35.6: candidate-selection quality gates.
MIN_AVG_TURNOVER_INR = float(os.environ.get("PROJECT_LADDU_MIN_TURNOVER", "50000000"))  # 5 crore/day avg
MIN_ELIGIBLE_PRICE_INR = float(os.environ.get("PROJECT_LADDU_MIN_ELIGIBLE_PRICE", "20"))  # configurable penny-stock exclusion
MAX_PROMOTED_PER_SECTOR = int(os.environ.get("PROJECT_LADDU_MAX_PER_SECTOR", "3"))

# v38.2.1: NSE security-wise delivery/EOD auto-sync.
NSE_DELIVERY_AUTO_DOWNLOAD = os.environ.get("PROJECT_LADDU_NSE_DELIVERY_AUTO_DOWNLOAD", "1").strip().lower() not in ("0", "false", "no")
# SmartAI needs roughly one trading year for institutional features and several
# purged test windows. Downloads remain bounded per maintenance pass.
NSE_DELIVERY_LOOKBACK_DAYS = max(365, int(os.environ.get("PROJECT_LADDU_NSE_DELIVERY_LOOKBACK_DAYS", "420")))
NSE_DELIVERY_REFRESH_SECONDS = max(300, int(os.environ.get("PROJECT_LADDU_NSE_DELIVERY_REFRESH_SECONDS", "1800")))
NSE_DELIVERY_HTTP_TIMEOUT = max(5, int(os.environ.get("PROJECT_LADDU_NSE_DELIVERY_HTTP_TIMEOUT", "12")))

# v65.26.27 unified production-risk authority. These are hard admission
# ceilings, not alpha tuning knobs. Values are percentages of configured
# trading capital unless otherwise stated.
MAX_RISK_OPEN_POSITIONS = max(1, int(os.environ.get("PROJECT_LADDU_MAX_OPEN_POSITIONS", "10")))
MAX_SYMBOL_EXPOSURE_PCT = max(1.0, float(os.environ.get("PROJECT_LADDU_MAX_SYMBOL_EXPOSURE_PCT", "15")))
MAX_PORTFOLIO_HEAT_PCT = max(0.5, float(os.environ.get("PROJECT_LADDU_MAX_PORTFOLIO_HEAT_PCT", "4")))
MAX_SECTOR_EXPOSURE_PCT = max(1.0, float(os.environ.get("PROJECT_LADDU_MAX_SECTOR_EXPOSURE_PCT", "30")))
MAX_SECTOR_OPEN_POSITIONS = max(1, int(os.environ.get("PROJECT_LADDU_MAX_SECTOR_POSITIONS", "3")))
MAX_DAILY_LOSS_PCT = max(0.1, float(os.environ.get("PROJECT_LADDU_MAX_DAILY_LOSS_PCT", "2")))
MAX_PORTFOLIO_DRAWDOWN_PCT = max(0.5, float(os.environ.get("PROJECT_LADDU_MAX_DRAWDOWN_PCT", "8")))
MAX_CORRELATION = min(0.99, max(0.1, float(os.environ.get("PROJECT_LADDU_MAX_CORRELATION", "0.80"))))
MAX_CORRELATED_POSITIONS = max(1, int(os.environ.get("PROJECT_LADDU_MAX_CORRELATED_POSITIONS", "2")))
