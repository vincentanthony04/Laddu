"""Point-in-time 5-minute ORB/VWAP/EMA/RVOL long-short replay."""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

from core.india_cost_model import IndiaCashCostModel


def ema(values, period):
    alpha = 2.0 / (period + 1)
    result = []
    current = None
    for value in values:
        current = value if current is None else value * alpha + current * (1 - alpha)
        result.append(current)
    return result


def true_ranges(rows):
    result, previous = [], None
    for row in rows:
        result.append(max(row["high"] - row["low"], abs(row["high"] - previous), abs(row["low"] - previous)) if previous is not None else row["high"] - row["low"])
        previous = row["close"]
    return result


def mean(values):
    return sum(values) / len(values) if values else 0.0


def load_rows(candles_path: Path, instruments_path: Path):
    symbols = {}
    with instruments_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            if raw.get("instrument_key") and raw.get("trading_symbol"):
                symbols[raw["instrument_key"]] = raw["trading_symbol"].strip().upper()
    grouped = defaultdict(list)
    with candles_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            symbol = symbols.get(raw.get("instrument_key"))
            if not symbol:
                continue
            try:
                grouped[symbol].append({
                    "timestamp": raw["ts"], "day": raw["ts"][:10],
                    "open": float(raw["open"]), "high": float(raw["high"]),
                    "low": float(raw["low"]), "close": float(raw["close"]),
                    "volume": float(raw.get("volume") or 0),
                })
            except (TypeError, ValueError, KeyError):
                continue
    for rows in grouped.values():
        rows.sort(key=lambda row: row["timestamp"])
    return grouped


def replay_symbol(symbol, rows, costs):
    sessions = defaultdict(list)
    for row in rows:
        sessions[row["day"]].append(row)
    trades = []
    for day, bars in sorted(sessions.items()):
        if len(bars) < 35:
            continue
        closes = [bar["close"] for bar in bars]
        volumes = [bar["volume"] for bar in bars]
        ema20, ema50 = ema(closes, 20), ema(closes, 50)
        ranges = true_ranges(bars)
        orb_high = max(bar["high"] for bar in bars[:3])
        orb_low = min(bar["low"] for bar in bars[:3])
        cumulative_value = cumulative_volume = 0.0
        vwap = []
        for bar in bars:
            typical = (bar["high"] + bar["low"] + bar["close"]) / 3
            cumulative_value += typical * bar["volume"]
            cumulative_volume += bar["volume"]
            vwap.append(cumulative_value / cumulative_volume if cumulative_volume else bar["close"])
        signal = None
        for index in range(20, min(len(bars) - 1, 66)):
            average_volume = mean(volumes[max(0, index - 20):index])
            rvol = volumes[index] / average_volume if average_volume else 0
            atr14 = mean(ranges[index - 13:index + 1])
            long_gate = closes[index] > orb_high and closes[index] > vwap[index] and ema20[index] > ema50[index] and rvol >= 1.2
            short_gate = closes[index] < orb_low and closes[index] < vwap[index] and ema20[index] < ema50[index] and rvol >= 1.2
            if long_gate or short_gate:
                signal = {"index": index + 1, "side": "LONG" if long_gate else "SHORT", "atr": atr14, "rvol": rvol}
                break
        if not signal:
            continue
        entry_index = signal["index"]
        entry = bars[entry_index]["open"]
        risk = signal["atr"]
        if not entry or not risk:
            continue
        if signal["side"] == "LONG":
            stop, target = entry - risk, entry + 1.5 * risk
        else:
            stop, target = entry + risk, entry - 1.5 * risk
        exit_price, outcome = bars[-1]["close"], "SESSION_EXIT"
        for bar in bars[entry_index:]:
            stop_hit = bar["low"] <= stop if signal["side"] == "LONG" else bar["high"] >= stop
            target_hit = bar["high"] >= target if signal["side"] == "LONG" else bar["low"] <= target
            if stop_hit:
                exit_price, outcome = stop, "STOP"
                break
            if target_hit:
                exit_price, outcome = target, "TARGET"
                break
        gross = (exit_price / entry - 1) * (1 if signal["side"] == "LONG" else -1)
        quantity = max(1, int(100000 / entry))
        cost = costs.round_trip(entry, exit_price, quantity)["costs"]["total"] / (entry * quantity)
        trades.append({
            "date": day, "symbol": symbol, "side": signal["side"], "entry": entry,
            "exit": exit_price, "gross_return": gross, "cost_return": cost,
            "net_return": gross - cost, "outcome": outcome, "rvol": signal["rvol"],
        })
    return trades


def metrics(rows):
    returns = [row["net_return"] for row in rows]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    equity = peak = 1.0
    drawdown = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    return {
        "trades": len(rows),
        "win_rate": len(wins) / len(rows) if rows else 0,
        "mean_net_return": mean(returns),
        "median_net_return": sorted(returns)[len(returns) // 2] if returns else 0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses and sum(losses) else None,
        "max_drawdown_serialized": drawdown,
        "target_hits": sum(row["outcome"] == "TARGET" for row in rows),
        "stop_hits": sum(row["outcome"] == "STOP" for row in rows),
        "session_exits": sum(row["outcome"] == "SESSION_EXIT" for row in rows),
    }


def run(candles_path, instruments_path):
    grouped = load_rows(candles_path, instruments_path)
    costs = IndiaCashCostModel.for_mode("intraday")
    trades = []
    for index, (symbol, rows) in enumerate(sorted(grouped.items()), 1):
        if index == 1 or index % 25 == 0 or index == len(grouped):
            print(f"PROGRESS {index}/{len(grouped)} symbols", flush=True)
        trades.extend(replay_symbol(symbol, rows, costs))
    dates = sorted({row["day"] for rows in grouped.values() for row in rows})
    return {
        "model_id": "intraday-orb-vwap-ema-rvol-1.0.0",
        "status": "EXPERIMENTAL",
        "approved": False,
        "universe_symbols": len(grouped),
        "sessions": len(dates),
        "date_range": [dates[0], dates[-1]] if dates else [],
        "method": "5m point-in-time; 15m ORB; cumulative typical-price VWAP; EMA20/50; RVOL>=1.2; next-bar entry; ATR14 stop; 1.5R target; forced session exit; conservative stop-first ambiguous bars; India cash costs on approximately INR 100,000 notional.",
        "all": metrics(trades),
        "long": metrics([row for row in trades if row["side"] == "LONG"]),
        "short": metrics([row for row in trades if row["side"] == "SHORT"]),
        "limitations": [
            "Only 30 sessions are available; insufficient for production approval or regime robustness.",
            "No tick-level spread, queue position, partial fills, or historical depth.",
            "This tests the intraday core trigger, not the complete live MTF/sector/market-regime composite.",
            "Serialized trade drawdown is not a capital-weighted concurrent portfolio drawdown.",
        ],
        "trades": trades,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candles-csv", type=Path, required=True)
    parser.add_argument("--instruments-csv", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.candles_csv, args.instruments_csv)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "trades"}, indent=2))
