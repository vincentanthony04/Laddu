"""Unified fail-closed risk admission authority for actionable candidates.

This service is deliberately separate from alpha/evidence scoring.  A candidate
can be statistically attractive and still be rejected because the portfolio or
operational state cannot safely admit another position.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.india_time import INDIA_TZ
from core.production_mode_policy import is_production_mode, normalise_mode
from core.production_decision_math_service import ProductionDecisionMathService
from core.portfolio_exposure_authority import DEFAULT_PORTFOLIO_EXPOSURE_AUTHORITY
from core.risk_admission_and_sizing_authority import DEFAULT_RISK_ADMISSION_AND_SIZING_AUTHORITY

from config import (
    TRADING_CAPITAL,
    RISK_PER_TRADE_PCT,
    MAX_RISK_OPEN_POSITIONS,
    MAX_SYMBOL_EXPOSURE_PCT,
    MAX_PORTFOLIO_HEAT_PCT,
    MAX_SECTOR_EXPOSURE_PCT,
    MAX_SECTOR_OPEN_POSITIONS,
    MAX_DAILY_LOSS_PCT,
    MAX_PORTFOLIO_DRAWDOWN_PCT,
    MAX_CORRELATION,
    MAX_CORRELATED_POSITIONS,
)

RISK_AUTHORITY_VERSION = "production-risk-authority-5.1.0-no-open-position-quantity-reconstruction"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _pct(value: float, total: float) -> float:
    return (float(value) / float(total) * 100.0) if total > 0 else 0.0


def _loads(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        return json.loads(value or "{}") if value else {}
    except Exception:
        return {}


@dataclass(frozen=True)
class RiskLimits:
    capital: float = TRADING_CAPITAL
    risk_per_trade_pct: float = RISK_PER_TRADE_PCT
    max_open_positions: int = MAX_RISK_OPEN_POSITIONS
    max_symbol_exposure_pct: float = MAX_SYMBOL_EXPOSURE_PCT
    max_portfolio_heat_pct: float = MAX_PORTFOLIO_HEAT_PCT
    max_sector_exposure_pct: float = MAX_SECTOR_EXPOSURE_PCT
    max_sector_open_positions: int = MAX_SECTOR_OPEN_POSITIONS
    max_daily_loss_pct: float = MAX_DAILY_LOSS_PCT
    max_drawdown_pct: float = MAX_PORTFOLIO_DRAWDOWN_PCT
    max_correlation: float = MAX_CORRELATION
    max_correlated_positions: int = MAX_CORRELATED_POSITIONS


class ProductionRiskAuthorityService:
    """Admit or reject one candidate against portfolio and operational limits."""

    def __init__(self, store: Any = None, runtime_status: Optional[Dict[str, Any]] = None, limits: Optional[RiskLimits] = None):
        self.store = store
        self.repository = getattr(store, "production_risk_repository", None) if store is not None else None
        self.runtime_status = runtime_status if isinstance(runtime_status, dict) else {}
        self.limits = limits or RiskLimits()
        self.decision_math = ProductionDecisionMathService()
        if store is not None and self.repository is None:
            if not hasattr(store, "write_lock"):
                store.write_lock = threading.RLock()
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        lock = getattr(self.store, "write_lock", None)
        if lock is None:
            self.store.conn.executescript(self._schema_sql())
            self._ensure_columns()
            self.store.conn.commit()
            return
        with lock:
            self.store.conn.executescript(self._schema_sql())
            self._ensure_columns()
            self.store.conn.commit()

    def _ensure_columns(self) -> None:
        try:
            cols = {str(row[1]) for row in self.store.conn.execute("PRAGMA table_info(production_risk_state)").fetchall()}
            if "account_as_of" not in cols:
                self.store.conn.execute("ALTER TABLE production_risk_state ADD COLUMN account_as_of TEXT")
        except Exception:
            pass

    @staticmethod
    def _schema_sql() -> str:
        return """
        CREATE TABLE IF NOT EXISTS production_risk_state (
          singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
          operator_stop INTEGER NOT NULL DEFAULT 0,
          operator_reason TEXT,
          operator_actor TEXT,
          updated_at TEXT NOT NULL,
          external_daily_pnl REAL,
          external_equity REAL,
          equity_peak REAL,
          account_as_of TEXT
        );
        CREATE TABLE IF NOT EXISTS production_risk_events (
          event_id TEXT PRIMARY KEY,
          occurred_at TEXT NOT NULL,
          event_type TEXT NOT NULL,
          symbol TEXT,
          mode TEXT,
          admission_state TEXT NOT NULL,
          reasons_json TEXT NOT NULL,
          payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_risk_events_time ON production_risk_events(occurred_at DESC);
        INSERT OR IGNORE INTO production_risk_state(singleton_id,operator_stop,updated_at)
        VALUES(1,0,CURRENT_TIMESTAMP);
        """

    def set_operator_stop(self, enabled: bool, reason: str, actor: str = "operator") -> Dict[str, Any]:
        if self.store is None:
            raise RuntimeError("risk authority store unavailable")
        reason = str(reason or "").strip()
        if enabled and not reason:
            raise ValueError("a reason is required to enable the operator stop")
        if self.repository is not None:
            self.repository.set_operator_stop(enabled, reason or None, str(actor or "operator"))
        else:
            with self.store.write_lock:
                self.store.conn.execute(
                    "UPDATE production_risk_state SET operator_stop=?,operator_reason=?,operator_actor=?,updated_at=? WHERE singleton_id=1",
                    (1 if enabled else 0, reason or None, str(actor or "operator"), _now()),
                )
                self.store.conn.commit()
        return self.status()

    def update_account_state(self, *, daily_pnl: Optional[float] = None, equity: Optional[float] = None, actor: str = "operator") -> Dict[str, Any]:
        if self.store is None:
            raise RuntimeError("risk authority store unavailable")
        daily = _f(daily_pnl)
        eq = _f(equity)
        row = self._state_row()
        peak = _f(row.get("equity_peak"))
        if eq is not None:
            peak = max(eq, peak or eq)
        if self.repository is not None:
            self.repository.update_account_state(
                daily_pnl=daily, equity=eq, peak=peak,
                actor=str(actor or "operator"), as_of=_now(),
            )
        else:
            with self.store.write_lock:
                self.store.conn.execute(
                    "UPDATE production_risk_state SET external_daily_pnl=COALESCE(?,external_daily_pnl),external_equity=COALESCE(?,external_equity),equity_peak=COALESCE(?,equity_peak),account_as_of=?,operator_actor=?,updated_at=? WHERE singleton_id=1",
                    (daily, eq, peak, _now(), str(actor or "operator"), _now()),
                )
                self.store.conn.commit()
        return self.status()

    def evaluate(self, candidate: Dict[str, Any], *, persist: bool = True) -> Dict[str, Any]:
        candidate = dict(candidate or {})
        symbol = str(candidate.get("symbol") or "").upper().strip()
        mode = normalise_mode(candidate.get("mode"))
        side = str(candidate.get("side") or "").upper().strip()
        entry = _f(candidate.get("entry") if candidate.get("entry") is not None else candidate.get("planned_entry"))
        stop = _f(candidate.get("sl") or candidate.get("stop") or candidate.get("planned_sl"))
        sector = str(candidate.get("sector") or candidate.get("sector_label") or "").strip()
        state = self._state_row()
        open_rows = self._open_positions()
        hard_blocks: List[str] = []
        capital_blocks: List[str] = []
        warnings: List[str] = []

        if not is_production_mode(mode):
            hard_blocks.append(f"unsupported production mode '{mode or '<missing>'}'")
        if bool(state.get("operator_stop")):
            hard_blocks.append("operator emergency stop is enabled")
        feed = self._feed_health(candidate)
        if not feed["healthy"]:
            hard_blocks.extend(feed["reasons"])
        if feed["quote_state"] == "unknown" or feed["candle_state"] == "unknown":
            capital_blocks.append("decision-time quote/candle freshness is not fully measured")
        if not symbol:
            hard_blocks.append("symbol identity missing")
        if side not in ("LONG", "SHORT"):
            hard_blocks.append("direction is not LONG or SHORT")
        if mode == "delivery" and side == "SHORT":
            hard_blocks.append("Delivery production desk is long-only")
        if mode == "intraday" and candidate.get("market_open_at_decision") is not True:
            hard_blocks.append("Intraday capital admission requires market-open decision time")
        if entry is None or entry <= 0 or stop is None or stop <= 0 or entry == stop:
            hard_blocks.append("valid entry and stop are required for risk sizing")
        if not sector:
            capital_blocks.append("sector metadata missing")
            sector = "Unknown"

        equity_base = _f(state.get("external_equity")) or self.limits.capital
        current = self._portfolio_metrics(open_rows, capital=equity_base)
        if current.get("unknown_quantity_positions", 0) > 0:
            hard_blocks.append(
                f"open-position quantity authority missing for {current['unknown_quantity_positions']} position(s); portfolio risk is unknowable"
            )
        sizing = self._size_candidate(candidate, entry, stop, state, current)
        if sizing["quantity"] < 1:
            hard_blocks.append("risk budget cannot support one share within the stop distance")

        projected = dict(current)
        projected["open_positions"] = current["open_positions"] + (1 if sizing["quantity"] > 0 else 0)
        projected["portfolio_risk_cash"] = current["portfolio_risk_cash"] + sizing["risk_cash"]
        projected["portfolio_heat_pct"] = _pct(projected["portfolio_risk_cash"], equity_base)
        projected["portfolio_notional"] = current["portfolio_notional"] + sizing["notional"]
        projected["symbol_notional"] = current["by_symbol_notional"].get(symbol, 0.0) + sizing["notional"]
        projected["symbol_exposure_pct"] = _pct(projected["symbol_notional"], equity_base)
        projected["sector_notional"] = current["by_sector_notional"].get(sector, 0.0) + sizing["notional"]
        projected["sector_exposure_pct"] = _pct(projected["sector_notional"], equity_base)
        projected["sector_positions"] = current["by_sector_positions"].get(sector, 0) + 1

        if current["open_positions"] >= self.limits.max_open_positions:
            hard_blocks.append(f"maximum open positions reached ({self.limits.max_open_positions})")
        if projected["portfolio_heat_pct"] > self.limits.max_portfolio_heat_pct + 1e-9:
            hard_blocks.append(f"projected portfolio heat {projected['portfolio_heat_pct']:.2f}% exceeds {self.limits.max_portfolio_heat_pct:.2f}%")
        if projected["symbol_exposure_pct"] > self.limits.max_symbol_exposure_pct + 1e-9:
            hard_blocks.append(f"projected symbol exposure {projected['symbol_exposure_pct']:.2f}% exceeds {self.limits.max_symbol_exposure_pct:.2f}%")
        if any(str(row.get("symbol") or "").upper() == symbol for row in open_rows):
            hard_blocks.append("an open thesis already exists for this symbol")

        pnl = self._account_loss_state(state)
        if pnl["daily_loss_breached"]:
            hard_blocks.append(f"daily loss limit breached ({pnl['daily_loss_pct']:.2f}% <= -{self.limits.max_daily_loss_pct:.2f}%)")
        if pnl["drawdown_breached"]:
            hard_blocks.append(f"portfolio drawdown limit breached ({pnl['drawdown_pct']:.2f}% <= -{self.limits.max_drawdown_pct:.2f}%)")
        if not pnl["measured"]:
            capital_blocks.append("account equity/daily P&L reconciliation is not measured")

        correlation = self._correlation_gate(candidate, open_rows)
        exposure = DEFAULT_PORTFOLIO_EXPOSURE_AUTHORITY.evaluate(
            sector=sector if sector != "Unknown" else "",
            capital=equity_base,
            current_sector_notional=current["by_sector_notional"].get(sector, 0.0),
            current_sector_positions=current["by_sector_positions"].get(sector, 0),
            proposed_notional=sizing["notional"],
            correlation_pairs=correlation.get("pairs") or [],
            correlation_measured=bool(correlation.get("measured")),
            max_sector_exposure_pct=self.limits.max_sector_exposure_pct,
            max_sector_open_positions=self.limits.max_sector_open_positions,
            max_correlation=self.limits.max_correlation,
            max_correlated_positions=self.limits.max_correlated_positions,
        )
        hard_blocks.extend(exposure.get("hard_blocks") or [])
        capital_blocks.extend(exposure.get("capital_blocks") or [])
        warnings.extend(correlation.get("warnings") or [])

        governance_required = bool(getattr(self.store, "production_model_governance_required", False))
        if governance_required and sizing["quantity"] > 0:
            decision_math = self.decision_math.evaluate(
                candidate, candidate.get("ai_governance") or {}, quantity=sizing["quantity"]
            )
            if decision_math.get("capital_admissible") is not True:
                capital_blocks.extend(
                    f"decision math: {code}" for code in (decision_math.get("blockers") or ["MODEL_MATH_UNAVAILABLE"])
                )
        else:
            decision_math = {
                "ok": True,
                "state": "LEGACY_COMPATIBILITY_NOT_ENFORCED" if not governance_required else "QUANTITY_UNAVAILABLE",
                "capital_admissible": not governance_required,
                "blockers": [] if not governance_required else ["QUANTITY_ZERO"],
                "heuristic_score_is_probability": False,
                "policy": "v68 production mode enforces frozen champion probability and post-cost lower-bound expectancy.",
            }

        admission = "BLOCKED" if hard_blocks else "APPROVED_RESEARCH_ONLY" if capital_blocks else "APPROVED_CAPITAL"
        report = {
            "ok": not hard_blocks,
            "risk_authority_version": RISK_AUTHORITY_VERSION,
            "as_of": _now(),
            "symbol": symbol,
            "mode": mode,
            "admission_state": admission,
            "capital_eligible": admission == "APPROVED_CAPITAL",
            "research_eligible": admission != "BLOCKED",
            "hard_blocks": hard_blocks,
            "capital_blocks": capital_blocks,
            "warnings": warnings,
            "limits": asdict(self.limits),
            "sizing": sizing,
            "current_portfolio": current,
            "projected_portfolio": projected,
            "feed_health": feed,
            "account_loss_state": pnl,
            "correlation": correlation,
            "portfolio_exposure": exposure,
            "decision_math": decision_math,
            "operator_stop": {
                "enabled": bool(state.get("operator_stop")),
                "reason": state.get("operator_reason"),
                "actor": state.get("operator_actor"),
                "updated_at": state.get("updated_at"),
            },
            "policy": "Risk admission cannot improve evidence score. Hard breaches downgrade actionable promotions; missing capital evidence prevents capital approval.",
        }
        if persist and self.store is not None:
            self._record(candidate, report)
        return report

    def apply(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(candidate or {})
        if str(out.get("status") or "").upper() != "PROMOTED":
            out.setdefault("risk_admission_state", "NOT_APPLICABLE")
            out.setdefault("risk_authority", {
                "risk_authority_version": RISK_AUTHORITY_VERSION,
                "admission_state": "NOT_APPLICABLE",
                "policy": "Risk admission is evaluated only after canonical evidence promotion.",
            })
            return out
        out.setdefault("pre_risk_status", out.get("status"))
        out.setdefault("pre_risk_decision", out.get("decision"))
        report = self.evaluate(out)
        out["risk_authority"] = report
        out["risk_admission_state"] = report["admission_state"]
        out["risk_quantity"] = report["sizing"]["quantity"]  # compatibility alias
        out["risk_ceiling_quantity"] = report["sizing"]["quantity"]
        out["risk_notional"] = report["sizing"]["notional"]
        out["risk_cash"] = report["sizing"]["risk_cash"]
        out["decision_math"] = report.get("decision_math") or {}
        out["economic_rank_utility"] = (out["decision_math"].get("ranking_utility")
                                        if isinstance(out["decision_math"], dict) else None)
        out["expected_net_return"] = (out["decision_math"].get("expected_net_return")
                                      if isinstance(out["decision_math"], dict) else None)
        if report["admission_state"] != "APPROVED_CAPITAL":
            out["status"] = "WATCH"
            out["decision"] = "WATCH"
            reasons = report["hard_blocks"] if report["admission_state"] == "BLOCKED" else report["capital_blocks"]
            out["promotion_blocked_by"] = list(dict.fromkeys(list(out.get("promotion_blocked_by") or []) + list(reasons)))
            label = "blocked" if report["admission_state"] == "BLOCKED" else "withheld capital promotion"
            out["reason"] = (str(out.get("reason") or "") + f"; Risk authority {label}: " + ", ".join(reasons)).strip("; ")
        return out

    def status(self) -> Dict[str, Any]:
        state = self._state_row()
        open_rows = self._open_positions()
        account = self._account_loss_state(state)
        operator = bool(state.get("operator_stop"))
        data_plane = self.runtime_status.get("production_data_plane") or {}
        data_plane_blocked = bool(data_plane and data_plane.get("production_ready") is not True)
        data_plane_reason = ""
        if data_plane_blocked:
            blockers = ",".join(data_plane.get("blockers") or []) or "not ready"
            data_plane_reason = f"production data plane is blocked: {blockers}"
        global_feed = self.runtime_status.get("auth") or {}
        feed_state = str(global_feed.get("state") or "unknown").lower()
        feed_blocked = feed_state in {
            "token_missing", "api_degraded", "data_degraded", "quote_rate_limited",
            "historical_rate_limited", "quote_api_blocked", "historical_api_blocked",
        }
        if operator:
            authority_state = "KILLED"
            new_signals = "BLOCKED"
            state_reason = state.get("operator_reason") or "operator emergency stop"
        elif data_plane_blocked or account.get("daily_loss_breached") or account.get("drawdown_breached") or feed_blocked:
            authority_state = "SUSPENDED"
            new_signals = "BLOCKED"
            if data_plane_blocked:
                state_reason = data_plane_reason
            elif account.get("daily_loss_breached") or account.get("drawdown_breached"):
                state_reason = "daily loss/drawdown limit breached"
            else:
                state_reason = f"market-data health is {feed_state}"
        elif not account.get("measured"):
            authority_state = "DEGRADED"
            new_signals = "RESEARCH_ONLY"
            state_reason = "account P&L/equity reconciliation is unavailable or stale"
        else:
            authority_state = "ACTIVE"
            new_signals = "ALLOWED_BY_RISK_ONLY"
            state_reason = "hard portfolio and operational controls are measured"
        return {
            "ok": authority_state not in ("KILLED", "SUSPENDED"),
            "risk_authority_version": RISK_AUTHORITY_VERSION,
            "as_of": _now(),
            "authority_state": authority_state,
            "new_signals": new_signals,
            "state_reason": state_reason,
            "operator_stop": {
                "enabled": operator,
                "reason": state.get("operator_reason"),
                "actor": state.get("operator_actor"),
                "updated_at": state.get("updated_at"),
            },
            "feed_state": {"state": feed_state, "blocked": feed_blocked},
            "production_data_plane": {
                "ready": not data_plane_blocked,
                "reason": data_plane_reason or "ready or not required by this runtime",
            },
            "limits": asdict(self.limits),
            "portfolio": self._portfolio_metrics(open_rows),
            "account_loss_state": account,
            "capital_authority": "NONE" if not account["measured"] else "MODEL_PAPER_CAPITAL_AUTHORITY" if account.get("source") == "MODEL_PAPER_LEDGER" else "RISK_CONTROLS_MEASURED_ONLY",
            "policy": "The authority gates recommendations; it does not place orders or grant live-capital approval.",
        }

    def _state_row(self) -> Dict[str, Any]:
        if self.store is None:
            return {"operator_stop": 0, "updated_at": None}
        if self.repository is not None:
            return self.repository.state_row() or {"operator_stop": 0, "updated_at": None}
        row = self.store.conn.execute("SELECT * FROM production_risk_state WHERE singleton_id=1").fetchone()
        return dict(row) if row else {"operator_stop": 0, "updated_at": None}

    def _open_positions(self) -> List[Dict[str, Any]]:
        """Return open positions without reconstructing missing risk authority.

        Quantity is an immutable admission/position fact.  Recomputing it later
        from Entry/SL can change exposure after limits or equity change, so a
        missing persisted quantity makes portfolio risk *unknown* and blocks new
        capital admission rather than inventing a replacement quantity.
        """
        if self.store is None:
            return []
        if self.repository is not None:
            try:
                rows = list(self.repository.open_positions() or [])
            except Exception:
                rows = []
        else:
            try:
                rows = [dict(raw) for raw in self.store.conn.execute("SELECT * FROM signal_ledger WHERE status='OPEN'").fetchall()]
            except Exception:
                rows = []

        result: List[Dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else _loads(row.get("payload_json"))
            row["payload"] = payload
            row["sector"] = row.get("sector") or payload.get("sector") or payload.get("sector_label") or "Unknown"
            if row.get("entry") is None and row.get("entry_price") is not None:
                row["entry"] = row.get("entry_price")
            if row.get("sl") is None:
                row["sl"] = row.get("managed_stop") or row.get("original_stop")
            raw_quantity = row.get("quantity")
            if raw_quantity is None:
                raw_quantity = payload.get("risk_quantity") if payload.get("risk_quantity") is not None else payload.get("quantity")
            quantity_value = _f(raw_quantity)
            quantity = int(quantity_value) if quantity_value is not None and quantity_value > 0 and float(quantity_value).is_integer() else 0
            row["quantity"] = quantity
            row["quantity_authority_state"] = "PERSISTED" if quantity > 0 else "MISSING_PERSISTED_AUTHORITY"
            result.append(row)
        return result

    def _size_candidate(
        self,
        candidate: Dict[str, Any],
        entry: Optional[float],
        stop: Optional[float],
        state: Dict[str, Any],
        current: Dict[str, Any],
    ) -> Dict[str, Any]:
        if entry is None or stop is None or entry <= 0 or stop <= 0 or entry == stop:
            return DEFAULT_RISK_ADMISSION_AND_SIZING_AUTHORITY._zero("invalid entry/stop", entry=entry, stop=stop)
        equity = _f(state.get("external_equity")) or self.limits.capital
        available_cash = _f(candidate.get("available_cash"))
        if available_cash is None:
            available_cash = max(0.0, equity - float(current.get("portfolio_notional") or 0.0))
        mode = normalise_mode(candidate.get("mode"))
        intraday_used = float((current.get("by_mode_notional") or {}).get("intraday", 0.0))
        # One risk authority owns quantity. Model/regime/alpha confidence is deliberately
        # not a sizing reducer unless a separately approved de-risk-only policy exists.
        policy = candidate.get("risk_derisk_policy") if isinstance(candidate.get("risk_derisk_policy"), dict) else {}
        return DEFAULT_RISK_ADMISSION_AND_SIZING_AUTHORITY.ceiling(
            mode=mode,
            entry=float(entry),
            stop=float(stop),
            equity=float(equity),
            available_cash=float(available_cash),
            risk_per_trade_pct=self.limits.risk_per_trade_pct,
            max_symbol_pct=self.limits.max_symbol_exposure_pct,
            intraday_cap=min(100_000.0, float(equity)),
            intraday_used=intraday_used,
            lot_size=max(1, int(_f(candidate.get("lot_size")) or 1)),
            approved_derisk_multiplier=float(policy.get("risk_multiplier") or 1.0),
            derisk_policy_approved=policy.get("human_approved") is True,
            derisk_policy_version=str(policy.get("policy_version") or "") or None,
        )

    def _portfolio_metrics(self, rows: Iterable[Dict[str, Any]], *, capital: Optional[float] = None) -> Dict[str, Any]:
        by_symbol: Dict[str, float] = {}
        by_sector: Dict[str, float] = {}
        by_sector_positions: Dict[str, int] = {}
        by_mode_notional: Dict[str, float] = {}
        risk_cash = notional = 0.0
        count = 0
        unknown_quantity_positions = 0
        for row in rows:
            entry = _f(row.get("entry")) or 0.0
            stop = _f(row.get("sl")) or 0.0
            qty = int(row.get("quantity") or 0)
            if qty <= 0:
                unknown_quantity_positions += 1
                continue
            count += 1
            sym = str(row.get("symbol") or "").upper()
            sector = str(row.get("sector") or "Unknown")
            n = entry * qty
            r = abs(entry - stop) * qty if stop > 0 else 0.0
            notional += n
            risk_cash += r
            by_symbol[sym] = by_symbol.get(sym, 0.0) + n
            by_sector[sector] = by_sector.get(sector, 0.0) + n
            by_sector_positions[sector] = by_sector_positions.get(sector, 0) + 1
            mode = normalise_mode(row.get("mode"))
            if mode:
                by_mode_notional[mode] = by_mode_notional.get(mode, 0.0) + n
        capital_base = float(capital or self.limits.capital)
        return {
            "open_positions": count,
            "portfolio_notional": round(notional, 2),
            "portfolio_exposure_pct": round(_pct(notional, capital_base), 4),
            "portfolio_risk_cash": round(risk_cash, 2),
            "portfolio_heat_pct": round(_pct(risk_cash, capital_base), 4),
            "by_symbol_notional": {k: round(v, 2) for k, v in by_symbol.items()},
            "by_sector_notional": {k: round(v, 2) for k, v in by_sector.items()},
            "by_sector_positions": by_sector_positions,
            "by_mode_notional": {k: round(v, 2) for k, v in by_mode_notional.items()},
            "unknown_quantity_positions": unknown_quantity_positions,
            "risk_complete": unknown_quantity_positions == 0,
        }

    def _feed_health(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        quote_state = str(candidate.get("quote_freshness_state") or candidate.get("freshness_state") or candidate.get("price_freshness_state") or "").lower()
        candle_state = str(candidate.get("candle_freshness_state") or candidate.get("historical_freshness_state") or "").lower()
        usable = candidate.get("usable_for_promotion")
        reasons: List[str] = []
        if usable is False:
            reasons.append("quote is explicitly unusable for promotion")
        bad = {"stale", "failed", "missing", "unverified", "lkg", "provider_unavailable", "api_failed"}
        if quote_state in bad:
            reasons.append(f"quote freshness is {quote_state}")
        if candle_state in bad:
            reasons.append(f"candle freshness is {candle_state}")
        data_plane = self.runtime_status.get("production_data_plane") or {}
        data_plane_blocked = bool(data_plane and data_plane.get("production_ready") is not True)
        data_plane_reason = ""
        if data_plane_blocked:
            blockers = ",".join(data_plane.get("blockers") or []) or "not ready"
            data_plane_reason = f"production data plane is blocked: {blockers}"
        global_feed = self.runtime_status.get("auth") or {}
        if str(global_feed.get("state") or "").lower() in {
            "token_missing", "api_degraded", "data_degraded", "quote_rate_limited",
            "historical_rate_limited", "quote_api_blocked", "historical_api_blocked",
        }:
            reasons.append(f"global market-data state is {global_feed.get('state')}")
        return {"healthy": not reasons, "quote_state": quote_state or "unknown", "candle_state": candle_state or "unknown", "reasons": reasons}

    def _model_paper_account_state(self) -> Optional[Dict[str, Any]]:
        """Derive the canonical simulated account state from the Model Paper ledger.

        Project Laddu is a governed paper-production product.  Capital admission
        must therefore be measured from its own canonical portfolio instead of
        relying on a manually refreshed external account endpoint.  The manual
        fields remain visible as an optional reconciliation reference, but they
        no longer make an otherwise healthy Model Paper book research-only.
        """
        if self.store is None:
            return None
        try:
            if self.repository is not None:
                rows = self.repository.model_paper_account_rows()
            else:
                table = self.store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='model_portfolio_positions'"
                ).fetchone()
                if not table:
                    return None
                rows = [dict(row) for row in self.store.conn.execute(
                    "SELECT status,net_pnl,closed_at,updated_at FROM model_portfolio_positions ORDER BY COALESCE(closed_at,updated_at),position_id"
                ).fetchall()]
        except Exception:
            return None

        initial = float(self.limits.capital)
        now = datetime.now(timezone.utc)
        today = now.astimezone(INDIA_TZ).date()
        realised = 0.0
        open_mtm = 0.0
        daily = 0.0
        curve = initial
        peak = initial
        latest = None
        for row in rows:
            pnl = _f(row.get("net_pnl")) or 0.0
            status = str(row.get("status") or "").upper()
            raw_stamp = row.get("closed_at") or row.get("updated_at")
            stamp = None
            try:
                stamp = datetime.fromisoformat(str(raw_stamp).replace("Z", "+00:00")) if raw_stamp else None
                if stamp and stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
            except ValueError:
                stamp = None
            if stamp and (latest is None or stamp > latest):
                latest = stamp
            if status == "CLOSED":
                realised += pnl
                curve += pnl
                peak = max(peak, curve)
                if stamp and stamp.astimezone(INDIA_TZ).date() == today:
                    daily += pnl
            elif status == "OPEN":
                open_mtm += pnl
        equity = initial + realised + open_mtm
        peak = max(peak, initial + realised, equity)
        return {
            "daily_pnl": round(daily, 2),
            "realised_pnl": round(realised, 2),
            "open_mtm": round(open_mtm, 2),
            "equity": round(equity, 2),
            "equity_peak": round(peak, 2),
            "as_of": _now(),
            "source": "MODEL_PAPER_LEDGER",
            "latest_position_event": latest.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if latest else None,
        }

    def _account_loss_state(self, state: Dict[str, Any]) -> Dict[str, Any]:
        model = self._model_paper_account_state()
        if model is not None:
            daily = _f(model.get("daily_pnl"))
            equity = _f(model.get("equity"))
            peak = _f(model.get("equity_peak"))
            as_of_raw = model.get("as_of")
            source = "MODEL_PAPER_LEDGER"
        else:
            daily = _f(state.get("external_daily_pnl"))
            equity = _f(state.get("external_equity"))
            peak = _f(state.get("equity_peak"))
            as_of_raw = state.get("account_as_of")
            source = "MANUAL_EXTERNAL_RECONCILIATION"
        stamp = None
        try:
            stamp = datetime.fromisoformat(str(as_of_raw).replace("Z", "+00:00")) if as_of_raw else None
            if stamp and stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
        except ValueError:
            stamp = None
        now = datetime.now(timezone.utc)
        age_seconds = max(0.0, (now - stamp.astimezone(timezone.utc)).total_seconds()) if stamp else None
        same_trading_day = bool(stamp and stamp.astimezone(INDIA_TZ).date() == now.astimezone(INDIA_TZ).date())
        values_present = daily is not None and equity is not None and peak is not None and peak > 0
        # Model Paper state is calculated transactionally on demand and is
        # therefore current even when no trade has occurred today.  Manual
        # external reconciliation retains the conservative 15-minute TTL.
        measured = bool(values_present and same_trading_day and (
            source == "MODEL_PAPER_LEDGER" or (age_seconds is not None and age_seconds <= 900.0)
        ))
        effective_daily = daily if same_trading_day else None
        daily_pct = _pct(effective_daily or 0.0, self.limits.capital)
        drawdown_pct = ((equity - peak) / peak * 100.0) if values_present else 0.0
        return {
            "measured": measured,
            "fresh_for_capital": measured,
            "source": source,
            "as_of": as_of_raw,
            "age_seconds": round(age_seconds, 2) if age_seconds is not None else None,
            "same_trading_day": same_trading_day,
            "daily_pnl": effective_daily,
            "daily_loss_pct": round(daily_pct, 4),
            "equity": equity,
            "equity_peak": peak,
            "realised_pnl": model.get("realised_pnl") if model else None,
            "open_mtm": model.get("open_mtm") if model else None,
            "drawdown_pct": round(drawdown_pct, 4),
            "daily_loss_breached": bool(values_present and same_trading_day and daily_pct <= -abs(self.limits.max_daily_loss_pct)),
            "drawdown_breached": bool(values_present and same_trading_day and drawdown_pct <= -abs(self.limits.max_drawdown_pct)),
            "state": "measured" if measured else "stale" if values_present and age_seconds is not None and age_seconds <= 86400.0 else "previous_day" if values_present else "unavailable",
        }

    def _correlation_gate(self, candidate: Dict[str, Any], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not rows:
            return {"measured": True, "breached": False, "highly_correlated_count": 0, "pairs": [], "source": "no_open_positions"}
        corr = candidate.get("open_position_correlations")
        source = "candidate_snapshot"
        sample_sizes: Dict[str, int] = {}
        if not isinstance(corr, dict):
            corr, sample_sizes = self._derive_local_correlations(candidate, rows)
            source = "completed_daily_local"
        pairs = []
        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            value = _f((corr or {}).get(sym))
            if value is not None:
                pairs.append({"symbol": sym, "correlation": round(value, 6), "samples": int(sample_sizes.get(sym) or 0) or None})
        high = [p for p in pairs if abs(p["correlation"]) >= self.limits.max_correlation]
        breached = len(high) >= self.limits.max_correlated_positions
        measured = len(pairs) == len(rows)
        result = {
            "measured": measured,
            "breached": breached,
            "highly_correlated_count": len(high),
            "pairs": pairs,
            "source": source,
            "reason": f"{len(high)} open positions have |correlation| >= {self.limits.max_correlation:.2f}" if breached else None,
        }
        if not measured:
            result["warnings"] = ["at least 30 overlapping completed daily returns are required per open position; capital approval withheld"]
        return result

    def _derive_local_correlations(self, candidate: Dict[str, Any], rows: List[Dict[str, Any]]) -> Tuple[Dict[str, float], Dict[str, int]]:
        if self.store is None or not hasattr(self.store, "recent_daily_candles_many"):
            return {}, {}
        candidate_symbol = str(candidate.get("symbol") or "").upper()
        candidate_key = self._instrument_key(candidate_symbol, candidate)
        open_keys: Dict[str, str] = {}
        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            key = self._instrument_key(sym, row.get("payload") or row)
            if key:
                open_keys[sym] = key
        if not candidate_key or len(open_keys) != len(rows):
            return {}, {}
        keys = [candidate_key, *open_keys.values()]
        try:
            candles = self.store.recent_daily_candles_many(keys, limit_per_key=95) or {}
        except Exception:
            return {}, {}
        candidate_returns = self._daily_returns(candles.get(candidate_key) or [])
        correlations: Dict[str, float] = {}
        samples: Dict[str, int] = {}
        for sym, key in open_keys.items():
            other = self._daily_returns(candles.get(key) or [])
            dates = sorted(set(candidate_returns).intersection(other))
            if len(dates) < 30:
                continue
            xs = [candidate_returns[d] for d in dates]
            ys = [other[d] for d in dates]
            corr = self._pearson(xs, ys)
            if corr is not None:
                correlations[sym] = corr
                samples[sym] = len(dates)
        return correlations, samples

    def _instrument_key(self, symbol: str, payload: Dict[str, Any]) -> str:
        key = str((payload or {}).get("instrument_key") or (payload or {}).get("instrument_token") or "").strip()
        if key:
            return key
        if self.store is None or not symbol:
            return ""
        try:
            row = self.store.conn.execute(
                "SELECT instrument_key FROM instruments WHERE UPPER(trading_symbol)=? AND UPPER(COALESCE(exchange,''))='NSE' ORDER BY CASE WHEN UPPER(COALESCE(segment,''))='NSE_EQ' THEN 0 ELSE 1 END LIMIT 1",
                (symbol,),
            ).fetchone()
            return str(row[0] or "") if row else ""
        except Exception:
            return ""

    @staticmethod
    def _daily_returns(rows: List[Dict[str, Any]]) -> Dict[str, float]:
        clean = []
        for raw in rows:
            try:
                ts = str(raw.get("ts") or raw.get("timestamp") or "")[:10]
                close = float(raw.get("close"))
                if ts and close > 0 and math.isfinite(close):
                    clean.append((ts, close))
            except (TypeError, ValueError):
                continue
        clean.sort(key=lambda item: item[0])
        out: Dict[str, float] = {}
        for idx in range(1, len(clean)):
            prior = clean[idx - 1][1]
            if prior > 0:
                out[clean[idx][0]] = clean[idx][1] / prior - 1.0
        return out

    @staticmethod
    def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
        if len(xs) != len(ys) or len(xs) < 2:
            return None
        mx = sum(xs) / len(xs); my = sum(ys) / len(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        dx = sum((x - mx) ** 2 for x in xs); dy = sum((y - my) ** 2 for y in ys)
        if dx <= 0 or dy <= 0:
            return None
        return num / math.sqrt(dx * dy)

    def _record(self, candidate: Dict[str, Any], report: Dict[str, Any]) -> None:
        basis = {
            "symbol": report.get("symbol"), "mode": report.get("mode"),
            "as_of": report.get("as_of"), "state": report.get("admission_state"),
            "reasons": report.get("hard_blocks"),
        }
        eid = hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:24]
        if self.repository is not None:
            self.repository.record_admission(candidate, report)
            return
        with self.store.write_lock:
            self.store.conn.execute(
                "INSERT OR REPLACE INTO production_risk_events(event_id,occurred_at,event_type,symbol,mode,admission_state,reasons_json,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                (eid, report["as_of"], "candidate_admission", report.get("symbol"), report.get("mode"), report.get("admission_state"), json.dumps(report.get("hard_blocks") or []), json.dumps({"candidate": candidate, "report": report}, sort_keys=True, default=str)),
            )
            self.store.conn.commit()
