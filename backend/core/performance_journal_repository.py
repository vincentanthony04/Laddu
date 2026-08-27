"""Learning/performance/trade journal: daily learning notes, per-mode
signal-accuracy performance rollups (today and all-time), and the manual
trade journal (log/update/delete/list/summarize actual trades) plus the
triggered-signal trade_journal view. Extracted verbatim from storage.py's
Store class (v51 storage split, cluster 9 -- the last of the storage.py
clusters; core connection/kv/migrations/sticky-selected/priority-delegate
plumbing stays in storage.py by design).

Constructed fresh per call (same pattern as ManualWatchRepository /
MarketDataRepository / ReferenceDataRepository / InstrumentSearchRepository),
against self.conn (the per-thread connection property).

_parse_ts and _payload are duplicated here from storage.py's Store (which
also carried a duplicate of _parse_ts for cluster 3's expire_fast_desk_signals,
and _payload for cluster 3's evaluate_signal_from_candles/settle path) --
same small pure helpers, no behavior change. storage.py's Store no longer
needs its own copies once this cluster's methods are the only remaining
callers; see BUILD_NOTES for this version.
"""
from __future__ import annotations

from core.production_mode_policy import require_production_mode

import json
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from models import now_iso
from core.india_time import trading_date_ist


def _desk_modes(mode): return (require_production_mode(mode),)


class PerformanceJournalRepository:
    def __init__(self, connection, write_lock=None):
        self.conn = connection
        # v60.7: same gap as SignalLedgerRepository had -- update_trade does
        # a SELECT-then-UPDATE (read-modify-write) with no lock at all. Trade
        # journal edits are user-triggered rather than scanner-loop-frequency,
        # so the exposure window is smaller, but the same lost-update race is
        # possible if two edits to the same trade land close together.
        # v61: record_daily_learning/log_trade/delete_trade were single-statement
        # writes and safe unlocked under WAL+busy_timeout on their own, but they
        # bypassed the write_lock convention every other repo follows. Now all
        # write paths in this repo (including update_trade above) take the lock,
        # for consistency and so a future multi-statement change here doesn't
        # silently reopen the gap.
        # Optional param + private fallback so this repo is never
        # unsynchronized even if constructed without Store's shared lock.
        self._write_lock = write_lock or threading.Lock()

    def record_daily_learning(self, payload: Dict[str, Any]) -> None:
        with self._write_lock:
            self.conn.execute("INSERT INTO daily_learning(learning_date,payload_json) VALUES(?,?)", (trading_date_ist(), json.dumps(payload)))
            self.conn.commit()

    def latest_daily_learning(self, limit: int = 5) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM daily_learning ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out=[]
        for r in rows:
            d=json.loads(r["payload_json"] or "{}")
            d.update({"learning_date":r["learning_date"],"created_at":r["created_at"]})
            out.append(d)
        return out

    def _parse_ts(self, s: Optional[str]):
        if not s:
            return None
        s = str(s).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s[:19] if "%z" not in fmt else s, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None

    def _holding_minutes(self, opened_at: Optional[str], closed_at: Optional[str]) -> Optional[float]:
        a = self._parse_ts(opened_at); b = self._parse_ts(closed_at)
        if not a or not b:
            return None
        try:
            delta = (b - a).total_seconds() / 60.0
            return round(delta, 1) if delta >= 0 else None
        except Exception:
            return None

    def _payload(self, row: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return json.loads(row.get("payload_json") or "{}") if isinstance(row, dict) else json.loads(row["payload_json"] or "{}")
        except Exception:
            return {}

    def daily_performance(self, start_date: str = "", end_date: str = "") -> List[Dict[str, Any]]:
        modes = ["intraday", "delivery"]
        out = []
        for m in modes:
            params: List[Any] = [m]
            where = "mode=?"
            if start_date:
                where += " AND trade_date>=?"; params.append(start_date)
            if end_date:
                where += " AND trade_date<=?"; params.append(end_date)
            if not start_date and not end_date:
                where += " AND trade_date=?"; params.append(trading_date_ist())
            rows = self.conn.execute(f"SELECT status,pnl_points,entry,opened_at,closed_at FROM signal_ledger WHERE {where}", tuple(params)).fetchall()
            raw_total = len(rows)
            quality_excluded = len([r for r in rows if r["pnl_points"] is not None and r["entry"] and abs(float(r["pnl_points"])) > abs(float(r["entry"])) * 0.5])
            rows = [r for r in rows if not (r["pnl_points"] is not None and r["entry"] and abs(float(r["pnl_points"])) > abs(float(r["entry"])) * 0.5)]
            total = raw_total
            win_rows = [r for r in rows if str(r["status"]).upper() == "SUCCESS"]
            loss_rows = [r for r in rows if str(r["status"]).upper() == "FAIL"]
            ambiguous_rows = [r for r in rows if str(r["status"]).upper() == "AMBIGUOUS"]
            expired_rows = [r for r in rows if str(r["status"]).upper() == "EXPIRED"]
            open_count = len([r for r in rows if str(r["status"]).upper() == "OPEN"])
            wins = len(win_rows); losses = len(loss_rows)
            ambiguous = len(ambiguous_rows); expired = len(expired_rows)
            decisive_closed = wins + losses
            closed = decisive_closed + ambiguous + expired
            pnl_vals = [(r["pnl_points"] or 0) for r in rows]
            pnl = round(sum(pnl_vals), 2) if total else 0
            win_pnls = [(r["pnl_points"] or 0) for r in win_rows]
            loss_pnls = [(r["pnl_points"] or 0) for r in loss_rows]
            gross_profit = sum(p for p in win_pnls if p > 0)
            gross_loss = abs(sum(p for p in loss_pnls if p < 0))
            avg_win = round(sum(win_pnls) / wins, 2) if wins else None
            avg_loss = round(sum(loss_pnls) / losses, 2) if losses else None
            # profit_factor: gross profit / gross loss. None if there's nothing to divide by yet;
            # capped display value when there are wins but zero losses (can't divide by zero).
            if gross_loss > 0:
                profit_factor = round(gross_profit / gross_loss, 2)
            elif gross_profit > 0:
                profit_factor = None  # undefined (no losing trades yet) rather than a misleading number
            else:
                profit_factor = None
            decisive_win_rate = (wins / decisive_closed) if decisive_closed else None
            settled_success_rate = (wins / closed) if closed else None
            settled_pnls = [(r["pnl_points"] or 0) for r in rows if str(r["status"]).upper() in ("SUCCESS", "FAIL", "AMBIGUOUS", "EXPIRED")]
            expectancy = round(sum(settled_pnls) / len(settled_pnls), 2) if settled_pnls else None
            holding_list = [self._holding_minutes(r["opened_at"], r["closed_at"]) for r in rows if str(r["status"]).upper() in ("SUCCESS", "FAIL")]
            holding_list = [h for h in holding_list if h is not None]
            avg_holding_minutes = round(sum(holding_list) / len(holding_list), 1) if holding_list else None
            out.append({
                "mode": m, "triggered_trades": total, "trades": total, "success": wins, "fail": losses, "ambiguous": ambiguous, "expired": expired, "open": open_count, "closed": closed, "decisive_closed": decisive_closed,
                "win_pct": round((settled_success_rate * 100), 1) if settled_success_rate is not None else None,
                "decisive_win_pct": round((decisive_win_rate * 100), 1) if decisive_win_rate is not None else None,
                "settled_success_pct": round((settled_success_rate * 100), 1) if settled_success_rate is not None else None,
                "failure_pct": round((losses / decisive_closed * 100), 1) if decisive_closed else None,
                "ambiguous_pct": round((ambiguous / closed * 100), 1) if closed else None, "expired_pct": round((expired / closed * 100), 1) if closed else None,
                # Legacy aliases remain for compatibility, but the metric lane
                # is explicitly price points -- never rupee P&L.
                "pnl": pnl, "pnl_points": pnl, "pnl_units": "PRICE_POINTS",
                "avg_win": avg_win, "avg_loss": avg_loss,
                "average_win_points": avg_win, "average_loss_points": avg_loss,
                "profit_factor": profit_factor, "point_profit_factor": profit_factor,
                "expectancy_per_trade": expectancy, "expectancy_points": expectancy,
                "net_pnl": None, "gross_pnl": None, "costs": None,
                "currency_pnl_available": False,
                "economic_performance_eligible": False,
                "metric_lane": "SIGNAL_ACCURACY_POINTS",
                "avg_holding_minutes": avg_holding_minutes,
                "quality_excluded": quality_excluded,
                "policy": "Triggered signal accuracy only. pnl/expectancy aliases are PRICE POINTS, never currency; governed rupee economics come only from Model Paper settlement."
            })
        return out

    def mode_performance_alltime(self) -> List[Dict[str, Any]]:
        """v36.9.8: long-term signal accuracy per mode.

        daily_performance() defaults to today only when no dates are given --
        useful for the live desk, but there was no view anywhere of "how has
        each mode actually performed since inception," which is the whole
        point of keeping a permanent ledger. Same metrics, no date filter.
        """
        modes = ["intraday", "delivery"]
        out = []
        for m in modes:
            rows = self.conn.execute("SELECT status,pnl_points,entry,opened_at,closed_at FROM signal_ledger WHERE mode=?", (m,)).fetchall()
            raw_total = len(rows)
            quality_excluded = len([r for r in rows if r["pnl_points"] is not None and r["entry"] and abs(float(r["pnl_points"])) > abs(float(r["entry"])) * 0.5])
            rows = [r for r in rows if not (r["pnl_points"] is not None and r["entry"] and abs(float(r["pnl_points"])) > abs(float(r["entry"])) * 0.5)]
            total = raw_total
            win_rows = [r for r in rows if str(r["status"]).upper() == "SUCCESS"]
            loss_rows = [r for r in rows if str(r["status"]).upper() == "FAIL"]
            ambiguous_rows = [r for r in rows if str(r["status"]).upper() == "AMBIGUOUS"]
            expired_rows = [r for r in rows if str(r["status"]).upper() == "EXPIRED"]
            open_count = len([r for r in rows if str(r["status"]).upper() == "OPEN"])
            wins = len(win_rows); losses = len(loss_rows)
            ambiguous = len(ambiguous_rows); expired = len(expired_rows)
            decisive_closed = wins + losses
            closed = decisive_closed + ambiguous + expired
            pnl_vals = [(r["pnl_points"] or 0) for r in rows]
            pnl = round(sum(pnl_vals), 2) if total else 0
            win_pnls = [(r["pnl_points"] or 0) for r in win_rows]
            loss_pnls = [(r["pnl_points"] or 0) for r in loss_rows]
            gross_profit = sum(p for p in win_pnls if p > 0)
            gross_loss = abs(sum(p for p in loss_pnls if p < 0))
            avg_win = round(sum(win_pnls) / wins, 2) if wins else None
            avg_loss = round(sum(loss_pnls) / losses, 2) if losses else None
            profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None
            decisive_win_rate = (wins / decisive_closed) if decisive_closed else None
            settled_success_rate = (wins / closed) if closed else None
            out.append({
                "mode": m, "total_signals": total, "success": wins, "fail": losses, "ambiguous": ambiguous, "expired": expired, "open": open_count, "closed": closed, "decisive_closed": decisive_closed,
                "win_pct": round(settled_success_rate * 100, 1) if settled_success_rate is not None else None,
                "decisive_win_pct": round(decisive_win_rate * 100, 1) if decisive_win_rate is not None else None,
                "settled_success_pct": round(settled_success_rate * 100, 1) if settled_success_rate is not None else None,
                "pnl": pnl, "pnl_points": pnl, "pnl_units": "PRICE_POINTS",
                "avg_win": avg_win, "avg_loss": avg_loss,
                "average_win_points": avg_win, "average_loss_points": avg_loss,
                "profit_factor": profit_factor, "point_profit_factor": profit_factor,
                "net_pnl": None, "gross_pnl": None, "costs": None,
                "currency_pnl_available": False,
                "economic_performance_eligible": False,
                "metric_lane": "SIGNAL_ACCURACY_POINTS",
                "quality_excluded": quality_excluded,
                "policy": "All-time triggered signal accuracy. Price-point statistics are continuity/accuracy evidence only, not currency performance."
            })
        return out

    # ---- v36.5: real manual trade log (what you actually did), kept fully
    # separate from signal_ledger (what the system suggested). The
    # trade_journal table already existed with the right shape; it was never
    # inserted into. This wires it up rather than repurposing signal_ledger,
    # so "my P&L" and "signal accuracy" can't get silently conflated again.
    def log_trade(self, data: Dict[str, Any]) -> int:
        with self._write_lock:
            cur = self.conn.execute(
                "INSERT INTO trade_journal(symbol,exchange,mode,side,entry,exit,qty,status,pnl,holding_minutes,notes,opened_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(data.get("symbol") or "").upper(), str(data.get("exchange") or "NSE"), str(data.get("mode") or ""),
                    str(data.get("side") or ""), data.get("entry"), data.get("exit"), data.get("qty"),
                    str(data.get("status") or ("CLOSED" if data.get("exit") is not None else "OPEN")),
                    data.get("pnl"), self._holding_minutes(data.get("opened_at"), data.get("closed_at")),
                    str(data.get("notes") or ""), data.get("opened_at") or now_iso(), data.get("closed_at"),
                ),
            )
            self.conn.commit()
            return cur.lastrowid

    def update_trade(self, trade_id: int, data: Dict[str, Any]) -> bool:
        with self._write_lock:
            row = self.conn.execute("SELECT * FROM trade_journal WHERE id=?", (trade_id,)).fetchone()
            if not row:
                return False
            merged = dict(row)
            merged.update({k: v for k, v in data.items() if v is not None})
            merged["holding_minutes"] = self._holding_minutes(merged.get("opened_at"), merged.get("closed_at"))
            self.conn.execute(
                "UPDATE trade_journal SET symbol=?,exchange=?,mode=?,side=?,entry=?,exit=?,qty=?,status=?,pnl=?,holding_minutes=?,notes=?,opened_at=?,closed_at=? WHERE id=?",
                (merged.get("symbol"), merged.get("exchange"), merged.get("mode"), merged.get("side"), merged.get("entry"),
                 merged.get("exit"), merged.get("qty"), merged.get("status"), merged.get("pnl"), merged.get("holding_minutes"),
                 merged.get("notes"), merged.get("opened_at"), merged.get("closed_at"), trade_id),
            )
            self.conn.commit()
            return True

    def delete_trade(self, trade_id: int) -> bool:
        with self._write_lock:
            self.conn.execute("DELETE FROM trade_journal WHERE id=?", (trade_id,))
            self.conn.commit()
            return True

    def my_trades(self, limit: int = 200, mode: str = "all", start_date: str = "", end_date: str = "") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM trade_journal WHERE 1=1"; params: List[Any] = []
        if mode and mode != "all":
            sql += " AND mode=?"; params.append(mode)
        if start_date:
            sql += " AND substr(opened_at,1,10)>=?"; params.append(start_date)
        if end_date:
            sql += " AND substr(opened_at,1,10)<=?"; params.append(end_date)
        sql += " ORDER BY opened_at DESC LIMIT ?"; params.append(limit)
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def my_trades_summary(self, mode: str = "all", start_date: str = "", end_date: str = "") -> Dict[str, Any]:
        rows = self.my_trades(limit=100000, mode=mode, start_date=start_date, end_date=end_date)
        closed = [r for r in rows if r.get("status") == "CLOSED" and r.get("pnl") is not None]
        wins = [r for r in closed if float(r["pnl"]) > 0]
        losses = [r for r in closed if float(r["pnl"]) <= 0]
        total_pnl = round(sum(float(r["pnl"]) for r in closed), 2) if closed else 0
        win_rate = round(len(wins) / len(closed) * 100, 1) if closed else None
        by_mode: Dict[str, Dict[str, Any]] = {}
        for r in closed:
            m = r.get("mode") or "unspecified"
            by_mode.setdefault(m, {"mode": m, "trades": 0, "pnl": 0.0, "wins": 0})
            by_mode[m]["trades"] += 1
            by_mode[m]["pnl"] += float(r["pnl"])
            if float(r["pnl"]) > 0:
                by_mode[m]["wins"] += 1
        for m in by_mode.values():
            m["pnl"] = round(m["pnl"], 2)
            m["win_rate"] = round(m["wins"] / m["trades"] * 100, 1) if m["trades"] else None
        return {
            "total_trades": len(rows), "closed_trades": len(closed), "open_trades": len(rows) - len(closed),
            "wins": len(wins), "losses": len(losses), "win_rate": win_rate, "total_pnl": total_pnl,
            "by_mode": list(by_mode.values()),
        }

    def trade_journal(self, limit: int = 50, mode: str = "all", start_date: str = "", end_date: str = "", month: str = "", year: str = "", outcome: str = "") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM signal_ledger WHERE 1=1"; params: List[Any] = []
        default_open_plus_7d = not any([start_date, end_date, month, year, outcome])
        if mode and mode != "all":
            modes=_desk_modes(mode); marks=",".join("?" for _ in modes); sql += f" AND mode IN ({marks})"; params.extend(modes)
        if month:
            sql += " AND substr(trade_date,1,7)=?"; params.append(month[:7])
        if year and not month:
            sql += " AND substr(trade_date,1,4)=?"; params.append(year[:4])
        if start_date:
            sql += " AND trade_date>=?"; params.append(start_date)
        if end_date:
            sql += " AND trade_date<=?"; params.append(end_date)
        if default_open_plus_7d:
            sql += " AND (status='OPEN' OR trade_date>=?)"; params.append((datetime.fromisoformat(trading_date_ist()) - timedelta(days=6)).date().isoformat())
        if outcome:
            sql += " AND UPPER(status)=?"; params.append(outcome.upper())
        sql += " ORDER BY CASE WHEN status='OPEN' THEN 0 ELSE 1 END, opened_at DESC LIMIT ?"; params.append(limit)
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        out = []
        for r in rows:
            payload = self._payload(dict(r))
            pnl = r["pnl_points"]
            quality_excluded = bool(pnl is not None and r["entry"] and abs(float(pnl)) > abs(float(r["entry"])) * 0.5)
            out.append({"id": r["signal_id"], "trade_date": r["trade_date"], "symbol": r["symbol"], "exchange": r["exchange"], "mode": r["mode"], "side": r["side"], "entry": r["entry"], "t1": r["t1"], "t2": r["t2"], "sl": r["sl"], "exit": r["exit"], "ltp": r["ltp"], "status": r["status"], "result": r["result"], "pnl": None if quality_excluded else pnl, "quality_excluded": quality_excluded, "mfe": payload.get("mfe"), "mae": payload.get("mae"), "validation_source": payload.get("validation_source"), "proof_ts": payload.get("proof_ts"), "proof_interval": payload.get("interval"), "holding_minutes": self._holding_minutes(r["opened_at"], r["closed_at"]), "notes": r["reason"], "opened_at": r["opened_at"], "closed_at": r["closed_at"], "created_at": r["opened_at"], "policy":"triggered trades only; ambiguous rows are not counted as success; price-scale outliers are excluded from P&L"})
        return out
