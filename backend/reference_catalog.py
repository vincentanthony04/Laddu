"""
Static reference data + small pure lookups for the dashboard/cockpit:
card contracts, instrument/index universes, sector mappings, and the
handful of payload builders that read directly from this data.

Extracted out of main.py (was inlined there as ~220 lines of module-level
data). the runtime entrypoint re-exports these names for compatibility
call sites elsewhere in the backend keep working unchanged.
"""
from __future__ import annotations
from typing import Any, Dict

CARD_CONTRACTS = {
    "stock_intelligence": {
        "purpose": "Selected/default market intelligence. Answers: what should I do now for this symbol and mode?",
        "storage": ["quote_cache", "price_snapshots", "candle_cache", "delivery_data", "decision_ledger", "fundamentals_store"],
        "intelligence": ["selected_stock_truth", "mode_gates", "MTF", "history_coverage", "valid_trade_map", "calculation_ledger", "replay_proof"],
        "display": "Compact top only: price/support/resistance, decision/MTF, entry/target/SL. Verbose reason and calculation ledger are bottom/expandable only.",
        "empty_state": "Default to NIFTY 50 Market Desk, never blank/loading-only.",
        "failure_state": "Show Data Partial/Pending and the exact missing proof; do not invent actionability."
    },
    "chart_desk": {
        "purpose": "Visual proof of candles and validated levels.",
        "storage": ["local_candle_cache", "chart_level_memory"],
        "intelligence": ["history_coverage", "SR validation", "role-aware levels"],
        "display": "Candles first; fallback map only when candles unavailable; diagnostics collapsed.",
        "empty_state": "NIFTY 50 default chart.",
        "failure_state": "No fake candles; mark thin history as reference only."
    },
    "signal_performance": {
        "purpose": "Mode-wise performance of triggered system signals.",
        "storage": ["signal_ledger"],
        "intelligence": ["open_vs_closed", "success_fail_ambiguous_expired", "accuracy_pending"],
        "display": "Intraday and Delivery grouped rows with open/closed/success/failure/P&L.",
        "empty_state": "Show no triggered signals or open-only pending, not blank zeros.",
        "failure_state": "If ledger unavailable, show proof unavailable and do not calculate win rate."
    },
    "signal_accuracy": {
        "purpose": "Audit trail for suggested signals and outcomes.",
        "storage": ["signal_ledger"],
        "intelligence": ["settlement_audit", "open_age", "target_SL_progress"],
        "display": "Grouped mode/day/month/year accuracy; open-only state must say Accuracy pending.",
        "empty_state": "No settled signals yet or open signals pending.",
        "failure_state": "No silent zero tables; show ledger/proof reason."
    },
    "historical_proof": {
        "purpose": "Proves whether candle history is enough for each mode.",
        "storage": ["candle_cache", "coverage_index"],
        "intelligence": ["required_candles_by_mode", "coverage_status", "stale_state"],
        "display": "Required/available candle count, interval, last candle, coverage status.",
        "empty_state": "History pending with retry state.",
        "failure_state": "If requested history returns thin data, mark PARTIAL/FAIL and block strong S/R."
    },
    "decision_ledger": {
        "purpose": "Auditable calculation trail for every Stock Intelligence decision.",
        "storage": ["decision_runs", "decision_factors", "decision_evidence", "decision_contradictions", "price_snapshots", "candles", "delivery_data"],
        "intelligence": ["research_library_registry", "factor_registry", "score_contribution", "contradiction_penalty", "replayability", "library_policy"],
        "display": "Expandable Calculation Log under Stock Intelligence: evidence, factors, contradictions, replay proof.",
        "empty_state": "No ledger run yet; show missing evidence instead of hiding the proof gap.",
        "failure_state": "If ledger cannot persist, card must say persist_error and must not claim final proof."
    },
    "market_context": {
        "purpose": "Market/index/sector bias used as scoring input.",
        "storage": ["index_quotes", "sector_map", "heatmap_cache"],
        "intelligence": ["index_bias", "sector_support", "resolver_aliases"],
        "display": "Compact supportive/mixed/weak context, never unrelated sector guess.",
        "empty_state": "Market context pending.",
        "failure_state": "Sector unavailable, not inferred."
    }
}


# Final integrated UX/product layer constants. These are not fake market values; they
# provide complete index/constituent structure and fallback instrument identity only.
FINAL_EQUITY_ALIASES = {
    # Current NSE identities. Legacy symbols remain accepted as search/direct
    # aliases but are never presented as active tradable identities.
    "LTIM": "LTM",
    "LTIMINDTREE": "LTM",
    "TATAMOTORS": "TMPV",
    "TATA MOTORS": "TMPV",
}

FINAL_FALLBACK_INSTRUMENTS = {
    "TMPV": {"trading_symbol":"TMPV","symbol":"TMPV","exchange":"NSE_EQ","segment":"NSE_EQ","instrument_key":"NSE_EQ|INE155A01022","isin":"INE155A01022","name":"Tata Motors Passenger Vehicles","instrument_type":"EQ","sector":"Auto","segment_index":"NIFTY AUTO","legacy_symbols":["TATAMOTORS"]},
    "LTM": {"trading_symbol":"LTM","symbol":"LTM","exchange":"NSE_EQ","segment":"NSE_EQ","instrument_key":"NSE_EQ|INE214T01019","isin":"INE214T01019","name":"LTIMindtree","instrument_type":"EQ","sector":"IT","segment_index":"NIFTY IT","legacy_symbols":["LTIM"]},
    "RELIANCE": {"trading_symbol":"RELIANCE","symbol":"RELIANCE","exchange":"NSE_EQ","instrument_key":"NSE_EQ|INE002A01018","name":"Reliance Industries","instrument_type":"EQ","sector":"Energy","segment_index":"NIFTY 50"},
    "TCS": {"trading_symbol":"TCS","symbol":"TCS","exchange":"NSE_EQ","instrument_key":"NSE_EQ|INE467B01029","name":"Tata Consultancy Services","instrument_type":"EQ","sector":"IT","segment_index":"NIFTY 50"},
    "HDFCBANK": {"trading_symbol":"HDFCBANK","symbol":"HDFCBANK","exchange":"NSE_EQ","instrument_key":"NSE_EQ|INE040A01034","name":"HDFC Bank","instrument_type":"EQ","sector":"Private Bank","segment_index":"NIFTY 50"},
    "ICICIBANK": {"trading_symbol":"ICICIBANK","symbol":"ICICIBANK","exchange":"NSE_EQ","instrument_key":"NSE_EQ|INE090A01021","name":"ICICI Bank","instrument_type":"EQ","sector":"Private Bank","segment_index":"NIFTY 50"},
    "MOTHERSON": {"trading_symbol":"MOTHERSON","symbol":"MOTHERSON","exchange":"NSE_EQ","instrument_key":"NSE_EQ|INE775A01035","name":"Samvardhana Motherson International","instrument_type":"EQ","sector":"Auto Components","segment_index":"NIFTY MIDCAP 100"},
    "KAYNES": {"trading_symbol":"KAYNES","symbol":"KAYNES","exchange":"NSE_EQ","instrument_key":"NSE_EQ|INE918Z01012","name":"Kaynes Technology India","instrument_type":"EQ","sector":"Electronics Manufacturing","segment_index":"NIFTY SMALLCAP 250"},
    "COALINDIA": {"trading_symbol":"COALINDIA","symbol":"COALINDIA","exchange":"NSE_EQ","instrument_key":"NSE_EQ|INE522F01014","name":"Coal India","instrument_type":"EQ","sector":"Energy","segment_index":"NIFTY 50"},
    "AXISBANK": {"trading_symbol":"AXISBANK","symbol":"AXISBANK","exchange":"NSE_EQ","instrument_key":"NSE_EQ|INE238A01034","name":"Axis Bank","instrument_type":"EQ","sector":"Private Bank","segment_index":"NIFTY 50"},
    "HCLTECH": {"trading_symbol":"HCLTECH","symbol":"HCLTECH","exchange":"NSE_EQ","instrument_key":"NSE_EQ|INE860A01027","name":"HCL Technologies","instrument_type":"EQ","sector":"IT","segment_index":"NIFTY 50"},
    "HAL": {"trading_symbol":"HAL","symbol":"HAL","exchange":"NSE_EQ","instrument_key":"NSE_EQ|INE066F01020","name":"Hindustan Aeronautics","instrument_type":"EQ","sector":"Defence","segment_index":"NIFTY 100"},
    "BHEL": {"trading_symbol":"BHEL","symbol":"BHEL","exchange":"NSE_EQ","instrument_key":"NSE_EQ|INE257A01026","name":"Bharat Heavy Electricals","instrument_type":"EQ","sector":"Capital Goods","segment_index":"NIFTY MIDCAP 100"},
    "SUZLON": {"trading_symbol":"SUZLON","symbol":"SUZLON","exchange":"NSE_EQ","instrument_key":"NSE_EQ|INE040H01021","name":"Suzlon Energy","instrument_type":"EQ","sector":"Renewable Energy","segment_index":"NIFTY MIDCAP 150"},
}

FINAL_INDEX_UNIVERSE = [
    # v31.1: trimmed to broad-core (used for overall market gating in heat_strip_context)
    # plus genuine sector indices (used for the stock's own sector tilt). NIFTY 100/200/500/
    # NEXT 50/MIDCAP-SMALLCAP-broad/thematic/BSE rows were display-only noise that never fed
    # any decision logic - removed rather than left dangling as fake "live" chips.
    ("NIFTY 50","Broad"),("NIFTY NEXT 50","Broad"),("NIFTY 100","Broad"),("NIFTY 200","Broad"),("NIFTY 500","Broad"),
    ("NIFTY MIDCAP 100","Broad"),("NIFTY SMALLCAP 100","Broad"),("SENSEX","Broad"),("NIFTY BANK","Broad"),
    # v37.5 Phase 3: VIX added to broad universe. Not a signal input yet --
    # engines.py doesn't read it. It's tracked/displayed so the volatility-
    # regime gate (planned) has data flowing before the gating logic lands.
    ("INDIA VIX","Broad"),
    ("NIFTY AUTO","Sector"),("NIFTY IT","Sector"),("NIFTY PHARMA","Sector"),("NIFTY FMCG","Sector"),
    ("NIFTY METAL","Sector"),("NIFTY REALTY","Sector"),("NIFTY ENERGY","Sector"),("NIFTY OIL & GAS","Sector"),
    ("NIFTY HEALTHCARE","Sector"),("NIFTY CONSUMER DURABLES","Sector"),("NIFTY MEDIA","Sector"),
    ("NIFTY PSU BANK","Sector"),("NIFTY PRIVATE BANK","Sector"),
]

# v32.2: previously this held ~10 stub symbols total and most sector indices in
# FINAL_INDEX_UNIVERSE (Media, FMCG, Realty, Energy, Oil & Gas, Healthcare, PSU Bank,
# Private Bank ...) had NO entry at all, so clicking those index cards returned an
# empty row set from final_index_stocks_payload() with no error shown. Filled out with
# each index's real constituent list (NSE trading symbols). NSE/NIFTY Indices rebalances
# these lists semi-annually (cutoff Jan 31 / Jul 31) -- treat this as a best-effort
# snapshot, not a live feed. Long-term fix: source this from the Upstox instrument
# master (which carries ISIN/sector) or NSE Indices' published constituent files
# instead of a hardcoded dict, so it never goes stale.
FINAL_INDEX_CONSTITUENTS = {
    "NIFTY 50": ["RELIANCE","HDFCBANK","ICICIBANK","INFY","TCS","BHARTIARTL","LT","ITC","KOTAKBANK","AXISBANK",
                 "SBIN","HINDUNILVR","BAJFINANCE","M&M","MARUTI","SUNPHARMA","TITAN","ULTRACEMCO","ASIANPAINT","NTPC",
                 "TMPV","POWERGRID","NESTLEIND","BAJAJFINSV","HCLTECH","ADANIENT","ADANIPORTS","ONGC","COALINDIA","JSWSTEEL",
                 "TATASTEEL","WIPRO","GRASIM","TECHM","CIPLA","DRREDDY","EICHERMOT","APOLLOHOSP","HEROMOTOCO","BAJAJ-AUTO",
                 "BRITANNIA","INDUSINDBK","DIVISLAB","SBILIFE","HDFCLIFE","SHRIRAMFIN","TATACONSUM","TRENT","BEL","HINDALCO"],
    "SENSEX": ["RELIANCE","HDFCBANK","ICICIBANK","INFY","TCS","BHARTIARTL","LT","ITC","KOTAKBANK","AXISBANK",
               "SBIN","HINDUNILVR","BAJFINANCE","M&M","MARUTI","SUNPHARMA","TITAN","ULTRACEMCO","ASIANPAINT","NTPC",
               "TMPV","POWERGRID","NESTLEIND","TECHM","HCLTECH","ADANIPORTS","JSWSTEEL","INDUSINDBK","TATASTEEL","ETERNAL"],
    "NIFTY BANK": ["HDFCBANK","ICICIBANK","SBIN","KOTAKBANK","AXISBANK","INDUSINDBK","BANKBARODA","PNB","FEDERALBNK","IDFCFIRSTB","AUBANK","CANBK"],
    "NIFTY IT": ["TCS","INFY","HCLTECH","WIPRO","TECHM","LTM","PERSISTENT","COFORGE","MPHASIS","LTTS"],
    "NIFTY AUTO": ["MARUTI","M&M","TMPV","BAJAJ-AUTO","EICHERMOT","HEROMOTOCO","TVSMOTOR","ASHOKLEY","BHARATFORG","MOTHERSON","BOSCHLTD","MRF","BALKRISIND","EXIDEIND","TIINDIA"],
    "NIFTY PHARMA": ["SUNPHARMA","DRREDDY","CIPLA","DIVISLAB","TORNTPHARM","LUPIN","AUROPHARMA","ZYDUSLIFE","ALKEM","MANKIND","GLENMARK","LAURUSLABS","IPCALAB","ABBOTINDIA","GLAND"],
    "NIFTY METAL": ["TATASTEEL","JSWSTEEL","HINDALCO","VEDL","JINDALSTEL","SAIL","NMDC","NATIONALUM","HINDZINC","APLAPOLLO","JSL","WELCORP"],
    "NIFTY FMCG": ["ITC","HINDUNILVR","NESTLEIND","TATACONSUM","VBL","BRITANNIA","MARICO","GODREJCP","UNITDSPR","DABUR","COLPAL","PATANJALI","RADICO","EMAMILTD"],
    "NIFTY REALTY": ["DLF","LODHA","PHOENIXLTD","OBEROIRLTY","GODREJPROP","PRESTIGE","BRIGADE","ANANTRAJ","SOBHA","ABREL"],
    "NIFTY ENERGY": ["RELIANCE","NTPC","POWERGRID","ONGC","COALINDIA","TATAPOWER","ADANIGREEN","ADANIENSOL","IOC","BPCL","GAIL","JSWENERGY"],
    "NIFTY OIL & GAS": ["RELIANCE","ONGC","IOC","BPCL","HINDPETRO","GAIL","OIL","PETRONET","MGL","IGL","GUJGASLTD","AEGISLOG"],
    "NIFTY HEALTHCARE": ["SUNPHARMA","DIVISLAB","TORNTPHARM","APOLLOHOSP","CIPLA","ZYDUSLIFE","DRREDDY","LUPIN","MAXHEALTH","MANKIND","SYNGENE"],
    "NIFTY MEDIA": ["ZEEL","PVRINOX","NAZARA","SUNTV","TIPSMUSIC","NETWORK18","PFOCUS","DBCORP","HATHWAY","SAREGAMA"],
    "NIFTY PSU BANK": ["SBIN","BANKBARODA","PNB","CANBK","UNIONBANK","INDIANB","BANKINDIA","IOB","UCOBANK","CENTRALBK","MAHABANK","PSB"],
    "NIFTY PRIVATE BANK": ["HDFCBANK","ICICIBANK","AXISBANK","KOTAKBANK","FEDERALBNK","INDUSINDBK","YESBANK","IDFCFIRSTB","BANDHANBNK","RBLBANK"],
    "NIFTY CONSUMER DURABLES": ["TITAN","HAVELLS","VOLTAS","DIXON","CROMPTON","WHIRLPOOL","BLUESTARCO","KAJARIACER","CERA","VGUARD","BATAINDIA","RAJESHEXPO"],
    "NIFTY MIDCAP 100": ["MOTHERSON","BHEL"],
    "NIFTY MIDCAP 150": ["SUZLON","BHEL"],
    "NIFTY SMALLCAP 250": ["KAYNES"],
    "NIFTY INDIA DEFENCE": ["HAL","BHEL"],
    "BSE SENSEX": ["RELIANCE","TCS","HDFCBANK","ICICIBANK"],
}

def canonical_fallback_equity_symbol(symbol: str) -> str:
    raw = str(symbol or '').strip().upper().replace('_', ' ')
    return FINAL_EQUITY_ALIASES.get(raw, raw)


def final_fallback_instrument(symbol: str) -> Dict[str, Any] | None:
    canonical = canonical_fallback_equity_symbol(symbol)
    return dict(FINAL_FALLBACK_INSTRUMENTS.get(canonical) or {}) or None

def fallback_instrument_matches(query: str, limit: int = 8) -> list[Dict[str, Any]]:
    """Small trusted recovery index used only while the Upstox instrument master warms.

    It is deliberately prefix/name based and never replaces the full instrument
    master.  Selection still resolves to an exact trading symbol and verified
    instrument key.
    """
    q = str(query or "").strip().upper()
    if not q:
        return []
    rows = []
    for symbol, raw in FINAL_FALLBACK_INSTRUMENTS.items():
        row = dict(raw)
        name = str(row.get("name") or "").upper()
        aliases = [alias for alias, canonical in FINAL_EQUITY_ALIASES.items() if canonical == symbol]
        matched_alias = next((alias for alias in aliases if alias.startswith(q) or q in alias), None)
        if symbol.startswith(q) or q in name or matched_alias:
            if matched_alias:
                row["matched_alias"] = matched_alias
            rows.append(row)
    rows.sort(key=lambda row: (0 if str(row.get("trading_symbol") or "").upper() == q else 1, str(row.get("trading_symbol") or "")))
    return rows[: max(1, int(limit or 8))]

# v31: explicit alias map from the full index names shown in FINAL_INDEX_UNIVERSE to the
# short row keys produced by App.heatmap()/heatmap_snapshot(). The previous matching logic
# (`existing.get(name) or existing.get(name.replace('NIFTY ',''))`) never actually matched
# anything, since heatmap rows use short keys like "NIFTY"/"BANK"/"MIDCAP", not the full
# index names or a "NIFTY " stripped variant of them. Indices with no alias here genuinely
# have no live source wired up yet and correctly stay "mapping pending".
FINAL_INDEX_ALIAS = {
    "NIFTY 50": "NIFTY", "NIFTY NEXT 50": "NXT50", "NIFTY 100": "N100", "NIFTY 200": "N200", "NIFTY 500": "N500",
    "NIFTY MIDCAP 100": "MIDCAP", "NIFTY SMALLCAP 100": "SMALLCAP", "SENSEX": "SENSEX", "NIFTY BANK": "BANK",
    "NIFTY AUTO": "AUTO", "NIFTY IT": "IT", "NIFTY PHARMA": "PHARMA", "NIFTY FMCG": "FMCG",
    "NIFTY METAL": "METAL", "NIFTY REALTY": "REALTY", "NIFTY ENERGY": "ENERGY",
    "NIFTY OIL & GAS": "OILGAS", "NIFTY HEALTHCARE": "HEALTHCARE",
    "NIFTY CONSUMER DURABLES": "CONSUMDUR", "NIFTY MEDIA": "MEDIA",
    "NIFTY PSU BANK": "PSUBANK", "NIFTY PRIVATE BANK": "PVTBANK",
}

SECTOR_INDEX_KEY_MAP = {
    "AUTO": "AUTO", "AUTO COMPONENTS": "AUTO", "AUTO ANCILLARY": "AUTO", "EV": "AUTO",
    "IT": "IT", "INFORMATION TECHNOLOGY": "IT", "TECHNOLOGY": "IT", "SOFTWARE": "IT",
    "PHARMA": "PHARMA", "PHARMACEUTICALS": "PHARMA",
    "FMCG": "FMCG", "CONSUMER": "FMCG",
    "METAL": "METAL", "METALS": "METAL", "STEEL": "METAL",
    "REALTY": "REALTY", "REAL ESTATE": "REALTY",
    "ENERGY": "ENERGY", "POWER": "ENERGY", "RENEWABLE ENERGY": "ENERGY",
    "OILGAS": "OILGAS", "OIL & GAS": "OILGAS", "OIL AND GAS": "OILGAS",
    "HEALTHCARE": "HEALTHCARE", "HOSPITALS": "HEALTHCARE",
    "CONSUMDUR": "CONSUMDUR", "CONSUMER DURABLES": "CONSUMDUR",
    "MEDIA": "MEDIA",
    "PSUBANK": "PSUBANK", "PSU BANK": "PSUBANK",
    "PVTBANK": "PVTBANK", "PRIVATE BANK": "PVTBANK", "PRIVATE BANKS": "PVTBANK",
    "BANK": "BANK", "BANKING": "BANK", "FINANCIAL SERVICES": "BANK",
}
SECTOR_INDEX_LABEL = {
    "AUTO": "NIFTY Auto", "IT": "NIFTY IT", "PHARMA": "NIFTY Pharma", "FMCG": "NIFTY FMCG",
    "METAL": "NIFTY Metal", "REALTY": "NIFTY Realty", "ENERGY": "NIFTY Energy", "OILGAS": "NIFTY Oil & Gas",
    "HEALTHCARE": "NIFTY Healthcare", "CONSUMDUR": "NIFTY Consumer Durables", "MEDIA": "NIFTY Media",
    "PSUBANK": "NIFTY PSU Bank", "PVTBANK": "NIFTY Private Bank", "BANK": "NIFTY Bank",
}

def normalize_sector_key(value: Any) -> str:
    # Compatibility facade: one authority now owns provider-label -> market
    # sector classification. The catalog maps above remain display/alias data.
    from core.sector_classification_authority import DEFAULT_SECTOR_CLASSIFICATION_AUTHORITY
    return DEFAULT_SECTOR_CLASSIFICATION_AUTHORITY.market_sector_key(value)

def final_heatmap_payload(app) -> Dict[str, Any]:
    from core.market_view_service import build_heatmap_items
    try:
        existing_rows = app.heatmap_snapshot() or []
    except Exception:
        existing_rows = []
    return build_heatmap_items(existing_rows, FINAL_INDEX_UNIVERSE)

def final_index_stocks_payload(app, index_name: str) -> Dict[str, Any]:
    from core.market_view_service import build_index_stocks_rows, _norm_index_name
    name = _norm_index_name(index_name)
    syms = FINAL_INDEX_CONSTITUENTS.get(name) or FINAL_INDEX_CONSTITUENTS.get(name.replace('NIFTY NXT', 'NIFTY NEXT')) or []
    try:
        latest = app.store.latest_decisions('all', limit=300) or []
    except Exception:
        latest = []
    # Decision LTP is an analysis snapshot, never the live-price authority.
    # Fetch the visible tranche regardless of whether an old decision row has
    # a price; build_index_stocks_rows will label any non-live remainder LKG.
    live_quotes: Dict[str, Any] = {}
    # The drawer must reconcile to the complete eligible list, not a 30-row
    # tranche. Fetch in bounded chunks so one large index does not exceed the
    # provider/request budget and merge only verified quote rows.
    for offset in range(0, len(syms), 50):
        chunk = list(syms)[offset:offset + 50]
        if not chunk:
            continue
        try:
            payload = app.live_quotes(",".join(chunk)) or {}
            live_quotes.update(payload.get('quotes') or {})
        except Exception:
            continue
    result = build_index_stocks_rows(name, syms, latest, live_quotes, final_fallback_instrument)
    result["eligible_population"] = len(syms)
    result["breadth_complete"] = bool(syms) and sum(1 for row in result.get("rows", []) if row.get("change_pct") is not None) == len(syms)
    return result

def final_journal_summary_payload(app, mode: str = 'all', start_date: str = '', end_date: str = '') -> Dict[str, Any]:
    from core.journal_service import summarize_trade_journal
    try:
        rows = app.store.trade_journal(limit=10000, mode=mode, start_date=start_date, end_date=end_date)
    except Exception:
        rows = []
    quality_excluded = [r for r in rows if r.get("quality_excluded")]
    result = summarize_trade_journal([r for r in rows if not r.get("quality_excluded")], start_date, end_date)
    result["quality_excluded"] = len(quality_excluded)
    result["observed_signals"] = len(rows)
    if quality_excluded:
        result["accuracy_message"] += f" {len(quality_excluded)} price-scale outlier(s) excluded pending re-audit."
    return result
