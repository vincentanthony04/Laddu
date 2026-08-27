"""Point-in-time institutional ranking evaluation over real NSE archives."""
from __future__ import annotations
import argparse, csv, json, sqlite3, statistics
from collections import defaultdict
from pathlib import Path

from core.india_cost_model import IndiaCashCostModel
from core.institutional_signal_service import analyze, MODEL_VERSION
from core.walk_forward_validation_service import WalkForwardValidationService


def load_delivery(folder: Path):
    out = defaultdict(list)
    for path in sorted(folder.glob("sec_bhavdata_full_*.csv")):
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for source in csv.DictReader(fh):
                raw = {str(k or "").strip(): str(v or "").strip() for k,v in source.items()}
                if raw.get("SERIES") != "EQ": continue
                sym = str(raw.get("SYMBOL") or "").strip().upper()
                day = str(raw.get("DATE1") or "").strip()
                try:
                    from datetime import datetime
                    day = datetime.strptime(day, "%d-%b-%Y").date().isoformat()
                    out[sym].append({"trade_date": day, "traded_qty": float(raw.get("TTL_TRD_QNTY") or 0),
                                     "deliverable_qty": float(raw.get("DELIV_QTY") or 0), "delivery_pct": float(raw.get("DELIV_PER") or 0)})
                except (TypeError, ValueError): pass
    for rows in out.values(): rows.sort(key=lambda r: r["trade_date"])
    return out


def load_candles(db: Path):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True); conn.row_factory = sqlite3.Row
    symbols = {}
    for row in conn.execute("SELECT trading_symbol,instrument_key FROM instruments WHERE exchange LIKE 'NSE%' AND trading_symbol IS NOT NULL"):
        symbols.setdefault(str(row["instrument_key"]), str(row["trading_symbol"]).upper())
    out = defaultdict(list)
    for r in conn.execute("SELECT instrument_key,ts,open,high,low,close,volume FROM candles WHERE interval='1d' ORDER BY instrument_key,ts"):
        sym = symbols.get(str(r["instrument_key"]))
        if sym: out[sym].append({"timestamp":r["ts"],"open":r["open"],"high":r["high"],"low":r["low"],"close":r["close"],"volume":r["volume"]})
    conn.close(); return out


def load_delivery_csv(path: Path):
    out = defaultdict(list)
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for raw in csv.DictReader(fh):
            try:
                symbol = str(raw.get("symbol") or "").strip().upper()
                if symbol:
                    out[symbol].append({
                        "trade_date": str(raw.get("trade_date") or "")[:10],
                        "traded_qty": float(raw.get("traded_qty") or 0),
                        "deliverable_qty": float(raw.get("deliverable_qty") or 0),
                        "delivery_pct": float(raw.get("delivery_pct") or 0),
                    })
            except (TypeError, ValueError):
                continue
    for rows in out.values():
        rows.sort(key=lambda row: row["trade_date"])
    return out


def load_candles_csv(candles_path: Path, instruments_path: Path):
    instrument_symbols = {}
    with instruments_path.open("r", encoding="utf-8-sig", newline="") as fh:
        for raw in csv.DictReader(fh):
            key = str(raw.get("instrument_key") or "")
            symbol = str(raw.get("trading_symbol") or "").strip().upper()
            if key and symbol:
                instrument_symbols[key] = symbol
    out = defaultdict(list)
    with candles_path.open("r", encoding="utf-8-sig", newline="") as fh:
        for raw in csv.DictReader(fh):
            symbol = instrument_symbols.get(str(raw.get("instrument_key") or ""))
            if not symbol:
                continue
            try:
                out[symbol].append({
                    "timestamp": str(raw.get("ts") or ""),
                    "open": float(raw.get("open") or 0),
                    "high": float(raw.get("high") or 0),
                    "low": float(raw.get("low") or 0),
                    "close": float(raw.get("close") or 0),
                    "volume": float(raw.get("volume") or 0),
                })
            except (TypeError, ValueError):
                continue
    for rows in out.values():
        rows.sort(key=lambda row: row["timestamp"])
    return out


def evaluate(delivery, candles, delivery_files=0):
    scored = defaultdict(list); signal_counts = defaultdict(int)
    matched = sorted(set(delivery) & set(candles))
    for symbol_index, sym in enumerate(matched, 1):
        if symbol_index == 1 or symbol_index % 25 == 0 or symbol_index == len(matched):
            print(f"PROGRESS {symbol_index}/{len(matched)} symbols", flush=True)
        drows, crows = delivery[sym], candles[sym]
        c_by_day = {str(c["timestamp"])[:10]: i for i,c in enumerate(crows)}
        for di in range(20, len(drows)):
            day = drows[di]["trade_date"]; ci = c_by_day.get(day)
            if ci is None or ci < 34: continue
            # The frozen model consumes at most 70 daily candles. Passing the
            # entire expanding history made the universe replay quadratic and
            # caused multi-gigabyte production datasets to exceed ten minutes.
            result = analyze(
                sym,
                list(reversed(drows[max(0, di - 64):di + 1])),
                crows[max(0, ci - 79):ci + 1],
            )
            if not result.get("ok"): continue
            scored[day].append({"symbol":sym,"score":result["score"],"result":result,"ci":ci,"candles":crows})
            for k,v in (result.get("signals") or {}).items():
                if v: signal_counts[k] += 1
    costs = IndiaCashCostModel.for_mode("delivery"); output = {"model_version":MODEL_VERSION,"universe_symbols":len(matched),
        "delivery_files":delivery_files,"scored_dates":len(scored),"signal_occurrences":dict(signal_counts),
        "method":"Precomputed signal observation validation: daily point-in-time top-decile institutional score; median same-day universe benchmark; 5/10/20 trading-day closes; India cash costs.",
        "limitations":["Installed-history universe only (97 matched symbols), not the complete NSE.","Overlapping observations inflate sample counts.","No fold-local trainer or immutable per-fold model artifact is supplied, so this cannot prove future-fold leakage is absent.","The equal-weight same-day signal drawdown is not an investable portfolio drawdown; no capital or concurrency simulator is supplied.","This observes institutional-nse-1.0.0 signals only; it does not approve Vibe/Qlib or the complete composite decision engine."],"horizons":{}}
    for horizon in (5,10,20):
        observations=[]
        for day, rows in sorted(scored.items()):
            forwards=[]
            for row in rows:
                cs=row["candles"];i=row["ci"]
                if i+horizon < len(cs) and cs[i]["close"] and cs[i+horizon]["close"]:
                    row=dict(row);row["forward"]=(float(cs[i+horizon]["close"])/float(cs[i]["close"]))-1;forwards.append(row)
            if len(forwards)<10: continue
            benchmark=statistics.median(r["forward"] for r in forwards)
            top=sorted(forwards,key=lambda r:(-r["score"],r["symbol"]))[:max(1,len(forwards)//10)]
            for r in top:
                buy=float(r["candles"][r["ci"]]["close"]);sell=float(r["candles"][r["ci"]+horizon]["close"])
                quantity=max(1, int(100000 / buy))
                cost=costs.round_trip(buy,sell,quantity)["costs"]["total"]/(buy*quantity)
                observations.append({"date":day,"symbol":r["symbol"],"forward_return":r["forward"],"benchmark_return":benchmark,"cost_return":cost})
        validation=WalkForwardValidationService().validate(f"{MODEL_VERSION}-top-decile-{horizon}d", observations,
            horizon_days=horizon,min_train_days=126,test_days=42,purge_days=horizon,max_folds=8,min_samples=80,persist=False)
        output["horizons"][str(horizon)]=validation
    return output


def run(delivery_dir: Path, db: Path):
    return evaluate(load_delivery(delivery_dir), load_candles(db), len(list(delivery_dir.glob("*.csv"))))


if __name__ == "__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--delivery-dir",type=Path)
    ap.add_argument("--db",type=Path)
    ap.add_argument("--delivery-csv",type=Path)
    ap.add_argument("--candles-csv",type=Path)
    ap.add_argument("--instruments-csv",type=Path)
    ap.add_argument("--out",type=Path,required=True)
    a=ap.parse_args()
    if a.delivery_csv and a.candles_csv and a.instruments_csv:
        result=evaluate(load_delivery_csv(a.delivery_csv),load_candles_csv(a.candles_csv,a.instruments_csv),1)
    elif a.delivery_dir and a.db:
        result=run(a.delivery_dir,a.db)
    else:
        ap.error("provide export CSV inputs or --delivery-dir plus --db")
    a.out.write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result,indent=2))
