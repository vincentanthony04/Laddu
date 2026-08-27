from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any, Dict, List, Mapping, Optional

from core.india_time import trading_date_ist
from core.production_mode_policy import require_production_mode
from .postgres import PostgresAuthority


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def _number(value: Any) -> Optional[float]:
    """Strict finite-number decoder for performance/learning arithmetic."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


class ProductionPerformanceJournalRepository:
    """PostgreSQL-backed Accuracy, Performance, learning and manual journal.

    Signal metrics are projections of the one canonical decision authority;
    there is no second production signal-ledger table to drift or duplicate.
    """

    production_authority = True

    def __init__(self, operational: PostgresAuthority):
        self.operational = operational

    @staticmethod
    def _holding_minutes(opened_at: Any, closed_at: Any) -> Optional[float]:
        if not opened_at or not closed_at:
            return None
        try:
            def parse(value: Any) -> datetime:
                if isinstance(value, datetime):
                    return value
                return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            minutes = (parse(closed_at) - parse(opened_at)).total_seconds() / 60.0
            return round(minutes, 1) if math.isfinite(minutes) and minutes >= 0 else None
        except Exception:
            return None

    @staticmethod
    def _json(value: Any) -> Dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                return dict(decoded) if isinstance(decoded, Mapping) else {}
            except Exception:
                return {}
        return {}

    def record_daily_learning(self, payload: Dict[str, Any]) -> None:
        self.operational.execute(
            "INSERT INTO runtime_control.daily_learning(learning_date,payload) VALUES(CURRENT_DATE,%s::jsonb)",
            (json.dumps(dict(payload or {}), default=str),),
        )

    def latest_daily_learning(self, limit: int = 5) -> List[Dict[str, Any]]:
        rows = self.operational.execute(
            "SELECT learning_id AS id,learning_date,payload AS payload_json,created_at FROM runtime_control.daily_learning ORDER BY learning_id DESC LIMIT %s",
            (max(1, min(int(limit), 500)),), fetch="all",
        ) or []
        out = []
        for raw in rows:
            row = dict(raw)
            payload = self._json(row.get("payload_json"))
            out.append({**row, "payload_json": json.dumps(payload, default=str), "payload": payload, "learning_date": str(row.get("learning_date") or "")[:10], "created_at": _iso(row.get("created_at"))})
        return out

    def _decision_rows(self, mode: str, start_date: str = "", end_date: str = "", *, limit: int = 100000) -> List[Dict[str, Any]]:
        canonical = require_production_mode(mode)
        where = ["mode=%s", "publication_authority IN ('CAPITAL','MODEL_PAPER')"]
        params: List[Any] = [canonical]
        if start_date:
            where.append("trading_date >= %s::date")
            params.append(str(start_date)[:10])
        if end_date:
            where.append("trading_date <= %s::date")
            params.append(str(end_date)[:10])
        params.append(max(1, min(int(limit), 200000)))
        rows = self.operational.execute(
            "SELECT * FROM trading.canonical_decisions WHERE " + " AND ".join(where) + " ORDER BY updated_at DESC LIMIT %s",
            tuple(params), fetch="all", statement_timeout_ms=3500,
        ) or []
        return [dict(row) for row in rows]

    def _project(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        row = dict(raw)
        latest = self._json(row.get("latest_payload"))
        entry_plan = self._json(row.get("entry_plan"))
        risk_plan = self._json(row.get("risk_plan"))
        live = self._json(row.get("live_snapshot"))
        outcome = self._json(row.get("outcome"))
        state = str(row.get("state") or "").upper()
        status = str(outcome.get("status") or outcome.get("signal_status") or "").upper()
        result = str(outcome.get("result") or latest.get("result") or "").upper()
        if not status:
            if state == "COMPLETED":
                status = "SUCCESS" if result in {"SUCCESS", "TARGET_HIT", "T1_HIT", "T2_HIT"} else "FAIL" if result in {"FAIL", "STOP_HIT", "SL_HIT"} else "COMPLETED"
            elif state in {"INVALIDATED", "REJECTED"}:
                status = "EXPIRED" if "EXPIRED" in result else "INVALIDATED"
            else:
                status = "OPEN"
        entry = _number(entry_plan.get("entry") if entry_plan.get("entry") is not None else latest.get("entry"))
        pnl = _number(outcome.get("pnl_points") if outcome.get("pnl_points") is not None else outcome.get("pnl"))
        if pnl is None:
            pnl = _number(live.get("pnl") if live.get("pnl") is not None else latest.get("pnl_points"))
        quality_excluded = bool(pnl is not None and entry and abs(pnl) > abs(entry) * 0.5)
        opened = row.get("activated_at") or row.get("created_at")
        closed = row.get("closed_at") or outcome.get("closed_at")
        return {
            "id": row.get("signal_id") or row.get("decision_id"),
            "decision_id": row.get("decision_id"),
            "signal_id": row.get("signal_id") or row.get("decision_id"),
            "trade_date": str(row.get("trading_date") or "")[:10],
            "symbol": row.get("symbol"), "exchange": row.get("exchange"),
            "mode": row.get("mode"), "side": row.get("side"),
            "entry": entry, "t1": _number(entry_plan.get("target_1")), "t2": _number(entry_plan.get("target_2")),
            "sl": _number(risk_plan.get("stop")),
            "exit": _number(outcome.get("exit") if outcome.get("exit") is not None else outcome.get("exit_price")),
            "ltp": _number(live.get("ltp") if live.get("ltp") is not None else latest.get("ltp")),
            "status": status, "result": result or status,
            "pnl": None if quality_excluded else pnl, "pnl_points": None if quality_excluded else pnl,
            "pnl_units": "PRICE_POINTS",
            "net_pnl": None, "gross_pnl": None, "costs": None,
            "currency_pnl_available": False,
            "economic_performance_eligible": False,
            "metric_lane": "SIGNAL_ACCURACY_POINTS",
            "quality_excluded": quality_excluded,
            "mfe": outcome.get("mfe") if outcome.get("mfe") is not None else latest.get("mfe"),
            "mae": outcome.get("mae") if outcome.get("mae") is not None else latest.get("mae"),
            "validation_source": outcome.get("validation_source") or latest.get("validation_source"),
            "proof_ts": outcome.get("proof_ts") or latest.get("proof_ts"),
            "proof_interval": outcome.get("interval") or latest.get("interval"),
            "holding_minutes": self._holding_minutes(opened, closed),
            "notes": latest.get("reason") or outcome.get("reason"),
            "opened_at": _iso(opened), "closed_at": _iso(closed), "created_at": _iso(row.get("created_at")),
            "canonical_state": state,
            "policy": "one canonical decision ledger; Intraday and Delivery only; ambiguous/expired are not wins",
        }

    @staticmethod
    def _metrics(mode: str, rows: List[Dict[str, Any]], *, all_time: bool = False) -> Dict[str, Any]:
        quality_excluded = sum(1 for row in rows if row.get("quality_excluded"))
        clean = [row for row in rows if not row.get("quality_excluded")]
        wins = [row for row in clean if str(row.get("status") or "").upper() == "SUCCESS"]
        losses = [row for row in clean if str(row.get("status") or "").upper() == "FAIL"]
        ambiguous = [row for row in clean if str(row.get("status") or "").upper() == "AMBIGUOUS"]
        expired = [row for row in clean if str(row.get("status") or "").upper() in {"EXPIRED", "INVALIDATED"}]
        open_rows = [row for row in clean if str(row.get("status") or "").upper() == "OPEN"]
        decisive = len(wins) + len(losses)
        settled = decisive + len(ambiguous) + len(expired)
        pnls = [value for row in clean if (value := _number(row.get("pnl"))) is not None]
        win_pnls = [value for row in wins if (value := _number(row.get("pnl"))) is not None]
        loss_pnls = [value for row in losses if (value := _number(row.get("pnl"))) is not None]
        gross_profit = sum(value for value in win_pnls if value > 0)
        gross_loss = abs(sum(value for value in loss_pnls if value < 0))
        holding = [
            value for row in clean
            if str(row.get("status") or "").upper() in {"SUCCESS", "FAIL"}
            and (value := _number(row.get("holding_minutes"))) is not None
        ]
        pnl_excluded = sum(1 for row in clean if row.get("pnl") is not None and _number(row.get("pnl")) is None)
        result = {
            "mode": mode,
            "triggered_trades": len(rows), "trades": len(rows), "total_signals": len(rows),
            "success": len(wins), "fail": len(losses), "ambiguous": len(ambiguous), "expired": len(expired),
            "open": len(open_rows), "closed": settled, "decisive_closed": decisive,
            "win_pct": round(len(wins) / settled * 100, 1) if settled else None,
            "decisive_win_pct": round(len(wins) / decisive * 100, 1) if decisive else None,
            "settled_success_pct": round(len(wins) / settled * 100, 1) if settled else None,
            "failure_pct": round(len(losses) / decisive * 100, 1) if decisive else None,
            "ambiguous_pct": round(len(ambiguous) / settled * 100, 1) if settled else None,
            "expired_pct": round(len(expired) / settled * 100, 1) if settled else None,
            "pnl": round(sum(pnls), 2),
            "pnl_points": round(sum(pnls), 2),
            "pnl_units": "PRICE_POINTS",
            "avg_win": round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else None,
            "avg_loss": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else None,
            "average_win_points": round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else None,
            "average_loss_points": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else None,
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
            "point_profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
            "expectancy_per_trade": round(sum(pnls) / len(pnls), 2) if pnls else None,
            "expectancy_points": round(sum(pnls) / len(pnls), 2) if pnls else None,
            "net_pnl": None, "gross_pnl": None, "costs": None,
            "currency_pnl_available": False,
            "economic_performance_eligible": False,
            "metric_lane": "SIGNAL_ACCURACY_POINTS",
            "avg_holding_minutes": round(sum(holding) / len(holding), 1) if holding else None,
            "quality_excluded": quality_excluded,
            "nonfinite_pnl_excluded": pnl_excluded,
            "authority": "POSTGRESQL_CANONICAL_DECISIONS",
            "policy": ("All-time" if all_time else "Period") + "; canonical signal accuracy in price points only. Governed rupee economics require Model Paper settlement quantity and costs.",
        }
        return result

    def daily_performance(self, start_date: str = "", end_date: str = "") -> List[Dict[str, Any]]:
        if not start_date and not end_date:
            start_date = end_date = trading_date_ist()
        return [
            self._metrics(mode, [self._project(row) for row in self._decision_rows(mode, start_date, end_date)])
            for mode in ("intraday", "delivery")
        ]

    def mode_performance_alltime(self) -> List[Dict[str, Any]]:
        return [
            self._metrics(mode, [self._project(row) for row in self._decision_rows(mode)], all_time=True)
            for mode in ("intraday", "delivery")
        ]

    def log_trade(self, data: Dict[str, Any]) -> int:
        mode = require_production_mode(data.get("mode"))
        row = self.operational.execute(
            """INSERT INTO trading.manual_trade_journal(
                   symbol,exchange,mode,side,entry,exit,quantity,status,pnl,holding_minutes,notes,opened_at,closed_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING trade_id""",
            (
                str(data.get("symbol") or "").upper().strip(), str(data.get("exchange") or "NSE").upper(), mode,
                str(data.get("side") or "").upper(), data.get("entry"), data.get("exit"), data.get("qty"),
                str(data.get("status") or ("CLOSED" if data.get("exit") is not None else "OPEN")).upper(),
                data.get("pnl"), self._holding_minutes(data.get("opened_at"), data.get("closed_at")),
                str(data.get("notes") or ""), data.get("opened_at") or datetime.now(timezone.utc), data.get("closed_at"),
            ), fetch="one",
        )
        return int((row or {}).get("trade_id") if hasattr(row, "get") else row[0])

    def _manual_row(self, trade_id: int) -> Optional[Dict[str, Any]]:
        row = self.operational.execute(
            "SELECT * FROM trading.manual_trade_journal WHERE trade_id=%s", (int(trade_id),), fetch="one"
        )
        return dict(row) if row else None

    def update_trade(self, trade_id: int, data: Dict[str, Any]) -> bool:
        current = self._manual_row(trade_id)
        if not current:
            return False
        merged = dict(current)
        merged.update({key: value for key, value in dict(data or {}).items() if value is not None})
        mode = require_production_mode(merged.get("mode"))
        self.operational.execute(
            """UPDATE trading.manual_trade_journal SET
                   symbol=%s,exchange=%s,mode=%s,side=%s,entry=%s,exit=%s,quantity=%s,status=%s,pnl=%s,
                   holding_minutes=%s,notes=%s,opened_at=%s,closed_at=%s,updated_at=now()
               WHERE trade_id=%s""",
            (
                str(merged.get("symbol") or "").upper(), str(merged.get("exchange") or "NSE").upper(), mode,
                str(merged.get("side") or "").upper(), merged.get("entry"), merged.get("exit"),
                merged.get("qty") if merged.get("qty") is not None else merged.get("quantity"),
                str(merged.get("status") or "OPEN").upper(), merged.get("pnl"),
                self._holding_minutes(merged.get("opened_at"), merged.get("closed_at")), str(merged.get("notes") or ""),
                merged.get("opened_at"), merged.get("closed_at"), int(trade_id),
            ),
        )
        return True

    def delete_trade(self, trade_id: int) -> bool:
        return int(self.operational.execute(
            "DELETE FROM trading.manual_trade_journal WHERE trade_id=%s", (int(trade_id),)
        ) or 0) > 0

    @staticmethod
    def _manual_project(raw: Mapping[str, Any]) -> Dict[str, Any]:
        row = dict(raw)
        row["id"] = row.pop("trade_id", row.get("id"))
        row["qty"] = row.pop("quantity", row.get("qty"))
        for key in ("opened_at", "closed_at", "created_at", "updated_at"):
            row[key] = _iso(row.get(key))
        return row

    def my_trades(self, limit: int = 200, mode: str = "all", start_date: str = "", end_date: str = "") -> List[Dict[str, Any]]:
        where = ["TRUE"]
        params: List[Any] = []
        if mode and str(mode).lower() != "all":
            where.append("mode=%s")
            params.append(require_production_mode(mode))
        if start_date:
            where.append("opened_at::date >= %s::date")
            params.append(start_date[:10])
        if end_date:
            where.append("opened_at::date <= %s::date")
            params.append(end_date[:10])
        params.append(max(1, min(int(limit), 100000)))
        rows = self.operational.execute(
            "SELECT * FROM trading.manual_trade_journal WHERE " + " AND ".join(where) + " ORDER BY opened_at DESC NULLS LAST,trade_id DESC LIMIT %s",
            tuple(params), fetch="all",
        ) or []
        return [self._manual_project(row) for row in rows]

    def my_trades_summary(self, mode: str = "all", start_date: str = "", end_date: str = "") -> Dict[str, Any]:
        rows = self.my_trades(100000, mode, start_date, end_date)
        all_closed = [row for row in rows if str(row.get("status") or "").upper() == "CLOSED"]
        closed = [row for row in all_closed if _number(row.get("pnl")) is not None]
        excluded_unscorable = len(all_closed) - len(closed)
        wins = [row for row in closed if _number(row.get("pnl")) > 0]
        losses = [row for row in closed if _number(row.get("pnl")) <= 0]
        by_mode: Dict[str, Dict[str, Any]] = {}
        for row in closed:
            pnl = _number(row.get("pnl"))
            if pnl is None:
                continue
            desk = str(row.get("mode") or "unspecified")
            item = by_mode.setdefault(desk, {"mode": desk, "trades": 0, "pnl": 0.0, "wins": 0})
            item["trades"] += 1
            item["pnl"] += pnl
            item["wins"] += 1 if pnl > 0 else 0
        for item in by_mode.values():
            item["pnl"] = round(item["pnl"], 2)
            item["win_rate"] = round(item["wins"] / item["trades"] * 100, 1) if item["trades"] else None
        return {
            "total_trades": len(rows), "closed_trades": len(all_closed), "scorable_closed_trades": len(closed),
            "unscorable_closed_trades": excluded_unscorable,
            "open_trades": len(rows) - len(all_closed),
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "total_pnl": round(sum(_number(row.get("pnl")) for row in closed), 2),
            "by_mode": list(by_mode.values()), "authority": "POSTGRESQL_MANUAL_TRADE_JOURNAL",
        }

    def trade_journal(self, limit: int = 50, mode: str = "all", start_date: str = "", end_date: str = "", month: str = "", year: str = "", outcome: str = "") -> List[Dict[str, Any]]:
        start = start_date
        end = end_date
        if month:
            start = month[:7] + "-01"
            try:
                probe = datetime.fromisoformat(start)
                end = (probe.replace(day=28) + timedelta(days=4)).replace(day=1).date().isoformat()
            except Exception:
                end = ""
        elif year:
            start, end = year[:4] + "-01-01", year[:4] + "-12-31"
        default_window = not any((start, end, outcome, month, year))
        modes = ("intraday", "delivery") if str(mode or "all").lower() == "all" else (require_production_mode(mode),)
        out: List[Dict[str, Any]] = []
        for desk in modes:
            rows = [self._project(row) for row in self._decision_rows(desk, start, end, limit=max(int(limit) * 4, 200))]
            if default_window:
                threshold = (datetime.fromisoformat(trading_date_ist()) - timedelta(days=6)).date().isoformat()
                rows = [row for row in rows if row.get("status") == "OPEN" or str(row.get("trade_date") or "") >= threshold]
            if outcome:
                rows = [row for row in rows if str(row.get("status") or "").upper() == str(outcome).upper()]
            out.extend(rows)
        # Stable two-pass ordering: newest first within each state, with open
        # positions always ahead of settled history.
        out.sort(key=lambda row: str(row.get("opened_at") or ""), reverse=True)
        out.sort(key=lambda row: 0 if row.get("status") == "OPEN" else 1)
        return out[: max(1, int(limit))]

    def outcome_learning_rows(self, limit: int = 5000) -> List[Dict[str, Any]]:
        rows = self.operational.execute(
            "SELECT * FROM trading.outcome_learning ORDER BY created_at DESC LIMIT %s",
            (max(1, min(int(limit), 100000)),), fetch="all",
        ) or []
        out = []
        for raw in rows:
            row = dict(raw)
            row["feature_json"] = json.dumps(self._json(row.pop("features", {})), default=str)
            row["proof_json"] = json.dumps(self._json(row.pop("proof", {})), default=str)
            row["closed_at"] = _iso(row.get("closed_at"))
            row["created_at"] = _iso(row.get("created_at"))
            out.append(row)
        return out

    def health_snapshot(self) -> Dict[str, Any]:
        canonical = self.operational.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE active) AS active,
                      MAX(updated_at) AS last_update
                 FROM trading.canonical_decisions""", fetch="one", statement_timeout_ms=1200,
        ) or {}
        manual = self.operational.execute(
            "SELECT COUNT(*) AS total,MAX(COALESCE(closed_at,opened_at,created_at)) AS last_update FROM trading.manual_trade_journal",
            fetch="one", statement_timeout_ms=1200,
        ) or {}
        return {
            "signal_ledger": {"total": int(canonical.get("total") or 0), "open": int(canonical.get("active") or 0), "last": _iso(canonical.get("last_update")), "authority": "POSTGRESQL_CANONICAL_DECISIONS"},
            "trade_journal": {"total": int(manual.get("total") or 0), "last": _iso(manual.get("last_update")), "authority": "POSTGRESQL_MANUAL_TRADE_JOURNAL"},
        }
