"""Verified-candle settlement for shadow selector candidates.

Every selector arm shares a candidate_id, therefore one immutable outcome is
settled per candidate/horizon and joined to all arms by the validation service.
No production decision or manual position is changed.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from dataclasses import fields
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from core.india_cost_model import IndiaCashCostConfig, IndiaCashCostModel
from core.india_time import INDIA_TZ
from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY
from core.quant_edge_data_service import QuantEdgeDataService
from core.selection_research_validation_service import SelectionResearchValidationService

SETTLEMENT_VERSION = "selection-outcome-settlement-1.3.0-session-authority"


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stamp(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _candle(raw: Mapping[str, Any]) -> Optional[Tuple[datetime, float, float, float]]:
    stamp = _stamp(raw.get("timestamp") or raw.get("ts") or raw.get("time"))
    high, low, close = _num(raw.get("high")), _num(raw.get("low")), _num(raw.get("close"))
    if stamp is None or high is None or low is None or close is None or min(high, low, close) <= 0:
        return None
    return stamp.astimezone(timezone.utc), high, low, close


class SelectionOutcomeSettlementService:
    HORIZONS = {
        # Canonical Intraday settlement assumes the supplied path is 5-minute
        # bars. The same model contract is available at five horizons, equal
        # in governance to the five Delivery horizons below.
        "intraday": {"5m": 1, "15m": 3, "30m": 6, "60m": 12, "eod": 75},
        "delivery": {"1d": 1, "3d": 3, "5d": 5, "10d": 10, "20d": 20},
    }

    def __init__(self, store: Any):
        self.store = store
        self.production_governance_required = bool(
            getattr(store, "production_model_governance_required", False)
        )
        self.governance_repository = getattr(
            store, "production_model_governance_repository", None
        )
        if self.production_governance_required:
            required = ("selector_candidates_for_settlement", "selector_outcome")
            if (
                self.governance_repository is None
                or getattr(self.governance_repository, "authority", None) is None
                or any(not callable(getattr(self.governance_repository, name, None)) for name in required)
            ):
                raise RuntimeError("PRODUCTION_SELECTION_SETTLEMENT_REQUIRES_POSTGRES_GOVERNANCE_REPOSITORY")
        self.validation = SelectionResearchValidationService(store)
        self.quant_data = QuantEdgeDataService(store)

    @staticmethod
    def _levels(features: Mapping[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        entry = _num(features.get("entry") if features.get("entry") is not None else features.get("planned_entry"))
        target = _num(
            features.get("target") if features.get("target") is not None
            else features.get("t1") if features.get("t1") is not None
            else features.get("planned_target")
        )
        stop = _num(
            features.get("stop") if features.get("stop") is not None
            else features.get("sl") if features.get("sl") is not None
            else features.get("planned_stop")
        )
        return entry, target, stop

    @staticmethod
    def _returns(
        *,
        mode: str,
        side: str,
        entry: float,
        exit_price: float,
        quantity: int,
        cost_assumptions: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, float]:
        direction = str(side or "LONG").upper()
        gross_bps = ((entry - exit_price) / entry if direction == "SHORT" else (exit_price - entry) / entry) * 10000.0
        if not isinstance(cost_assumptions, Mapping) or not cost_assumptions:
            raise ValueError("settlement requires snapshot-frozen India cost assumptions")
        allowed = {item.name for item in fields(IndiaCashCostConfig)}
        values = {key: value for key, value in cost_assumptions.items() if key in allowed}
        config = IndiaCashCostConfig(**values)
        model = IndiaCashCostModel(config=config, mode=mode)
        estimate = model.round_trip(exit_price, entry, quantity) if direction == "SHORT" else model.round_trip(entry, exit_price, quantity)
        net_bps = float(estimate["net_return_pct"]) * 100.0
        return {
            "gross_return_bps": round(gross_bps, 6),
            "net_return_bps": round(net_bps, 6),
            "actual_cost_bps": round(gross_bps - net_bps, 6),
            "gross_pnl": float(estimate["gross_pnl"]),
            "net_pnl": float(estimate["net_pnl"]),
            "cost_total": float(estimate["costs"]["total"]),
            "cost_version": str(estimate["config"]["version"]),
            "cost_authority": str(estimate.get("cost_authority") or "IndiaCashCostAuthority"),
            "cost_authority_version": str(estimate.get("cost_authority_version") or ""),
            "tariff_schedule_version": estimate.get("tariff_schedule_version"),
            "execution_assumption_version": estimate.get("execution_assumption_version"),
        }

    @staticmethod
    def _session_complete(path: List[Tuple[datetime, float, float, float]], expected_bars: int) -> bool:
        if not path:
            return False
        if len(path) >= expected_bars:
            return True
        last_india = path[-1][0].astimezone(INDIA_TZ)
        sessions = DEFAULT_TRADING_SESSION_AUTHORITY
        # A shortened-path end-of-session shortcut is allowed only when the
        # release calendar positively identifies the trading session.  Older
        # historical dates outside calendar coverage must provide the full
        # expected bar count; weekday/15:25 guesses are not accepted.
        if not sessions.calendar_covered(last_india.date()):
            return False
        window = sessions.session_window(last_india.date())
        if window is None:
            return False
        final_completed_bar = window.close_at() - timedelta(minutes=5)
        return last_india >= final_completed_bar

    def settle_symbol(self, symbol: str, mode: str, candles: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
        desk = str(mode or "").lower().strip()
        if desk not in self.HORIZONS:
            raise ValueError("mode must be intraday or delivery")
        reconciliation = self.quant_data.backfill_existing(limit=5000)
        incoming = [item for item in (_candle(raw) for raw in candles or []) if item is not None]
        by_period: Dict[Any, Tuple[datetime, float, float, float]] = {}
        conflicting_periods = 0
        for item in incoming:
            key = item[0].astimezone(INDIA_TZ).date() if desk == "delivery" else item[0]
            prior = by_period.get(key)
            if prior is not None and prior[1:] != item[1:]:
                conflicting_periods += 1
                continue
            by_period[key] = item
        if conflicting_periods:
            return {
                "ok": False,
                "updated": 0,
                "state": "CONFLICTING_CANDLE_PERIODS",
                "conflicting_periods": conflicting_periods,
                "version": SETTLEMENT_VERSION,
                "production_authority": "UNCHANGED_HEURISTIC_BASELINE",
            }
        clean = sorted(by_period.values(), key=lambda item: item[0])
        if not clean:
            return {"ok": True, "updated": 0, "state": "NO_VALID_CANDLES", "version": SETTLEMENT_VERSION}
        if self.production_governance_required:
            candidates = self.governance_repository.selector_candidates_for_settlement(
                symbol=str(symbol or "").upper().strip(), mode=desk
            )
        else:
            candidates = self.store.conn.execute(
                """SELECT DISTINCT o.candidate_id,o.population_fingerprint,o.symbol,o.mode,o.side,
                          o.observed_at,o.feature_json,q.cost_assumption_json
                   FROM candidate_population_observations o
                   JOIN shadow_selector_predictions p ON p.candidate_id=o.candidate_id
                   JOIN quant_feature_snapshots q ON q.candidate_id=o.candidate_id
                   WHERE UPPER(o.symbol)=? AND o.mode=?
                   ORDER BY o.observed_at""",
                (str(symbol or "").upper().strip(), desk),
            ).fetchall()
        updated = pending = skipped_incomplete = ambiguous = 0
        settlements: List[Dict[str, Any]] = []
        for raw in candidates:
            row = dict(raw)
            observed = _stamp(row.get("observed_at"))
            if observed is None:
                continue
            try:
                features = json.loads(row.get("feature_json") or "{}")
                features = dict(features) if isinstance(features, dict) else {}
            except Exception:
                features = {}
            entry, target, stop = self._levels(features)
            if entry is None:
                entry = _num(
                    features.get("ltp")
                    if features.get("ltp") is not None
                    else features.get("price")
                    if features.get("price") is not None
                    else features.get("current_price")
                )
            if entry is None or entry <= 0:
                skipped_incomplete += 1
                continue
            has_trade_map = (
                target is not None and stop is not None and target > 0 and stop > 0
            )
            side = str(row.get("side") or features.get("side") or "LONG").upper()
            if side not in {"LONG", "SHORT"}:
                skipped_incomplete += 1
                continue
            future = [item for item in clean if item[0] > observed.astimezone(timezone.utc)]
            if desk == "intraday":
                decision_day = observed.astimezone(INDIA_TZ).date()
                future = [item for item in future if item[0].astimezone(INDIA_TZ).date() == decision_day]
            for horizon, bars in self.HORIZONS[desk].items():
                if self.production_governance_required:
                    exists = self.governance_repository.selector_outcome(row["candidate_id"], horizon)
                else:
                    exists = self.store.conn.execute(
                        "SELECT 1 FROM selector_candidate_outcomes WHERE candidate_id=? AND horizon=?",
                        (row["candidate_id"], horizon),
                    ).fetchone()
                if exists:
                    continue
                path = future[:bars]
                complete = self._session_complete(path, bars) if desk == "intraday" else len(path) >= bars
                if not path or not complete:
                    pending += 1
                    continue
                outcome_price = None
                outcome_at = None
                outcome_reason = None
                outcome_index = None
                same_bar = False
                for bar_index, (stamp, high, low, close) in enumerate(path):
                    if not has_trade_map:
                        break
                    target_hit = low <= target if side == "SHORT" else high >= target
                    stop_hit = high >= stop if side == "SHORT" else low <= stop
                    if target_hit and stop_hit:
                        outcome_price, outcome_at, outcome_reason, outcome_index, same_bar = stop, stamp, "SAME_BAR_STOP_FIRST", bar_index, True
                        ambiguous += 1
                        break
                    if target_hit:
                        outcome_price, outcome_at, outcome_reason, outcome_index = target, stamp, "TARGET_FIRST", bar_index
                        break
                    if stop_hit:
                        outcome_price, outcome_at, outcome_reason, outcome_index = stop, stamp, "STOP_FIRST", bar_index
                        break
                if outcome_price is None:
                    outcome_price, outcome_at, outcome_reason, outcome_index = path[-1][3], path[-1][0], "HORIZON_CLOSE", len(path) - 1
                observed_path = path
                if side == "SHORT":
                    mfe_bps = max((entry - low) / entry * 10000.0 for _stamp, _high, low, _close in observed_path)
                    mae_bps = max((high - entry) / entry * 10000.0 for _stamp, high, _low, _close in observed_path)
                else:
                    mfe_bps = max((high - entry) / entry * 10000.0 for _stamp, high, _low, _close in observed_path)
                    mae_bps = max((entry - low) / entry * 10000.0 for _stamp, _high, low, _close in observed_path)
                try:
                    cost_assumptions = json.loads(row.get("cost_assumption_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    cost_assumptions = {}
                quantity = int(
                    _num(features.get("quantity") or features.get("qty") or features.get("model_quantity"))
                    or max(1, 100000 // entry)
                )
                returns = self._returns(
                    mode=desk,
                    side=side,
                    entry=entry,
                    exit_price=float(outcome_price),
                    quantity=quantity,
                    cost_assumptions=cost_assumptions,
                )
                fixed_horizon = self._returns(
                    mode=desk,
                    side=side,
                    entry=entry,
                    exit_price=float(path[-1][3]),
                    quantity=quantity,
                    cost_assumptions=cost_assumptions,
                )
                result = "SUCCESS" if returns["net_return_bps"] > 0 else "FAIL" if returns["net_return_bps"] < 0 else "BREAKEVEN"
                if same_bar:
                    result = "FAIL"
                proof = {
                    "settlement_version": SETTLEMENT_VERSION,
                    "outcome_reason": outcome_reason,
                    "outcome_price": outcome_price,
                    "entry": entry,
                    "target": target,
                    "stop": stop,
                    "bars_observed": len(path),
                    "horizon_bars": bars,
                    "time_to_outcome_bars": int(outcome_index or 0) + 1,
                    "target_before_stop": (
                        outcome_reason == "TARGET_FIRST" if has_trade_map else None
                    ),
                    "mfe_bps": round(max(0.0, mfe_bps), 6),
                    "mae_bps": round(max(0.0, mae_bps), 6),
                    "identity_verified": True,
                    "cost_version": returns["cost_version"],
                    "gross_pnl_100": returns["gross_pnl"],
                    "net_pnl_100": returns["net_pnl"],
                    "estimated_cost_100": returns["cost_total"],
                    "assumed_quantity": quantity,
                    "fixed_horizon_exit": path[-1][3],
                    "fixed_horizon_settled_at": path[-1][0].isoformat().replace("+00:00", "Z"),
                    "fixed_horizon_gross_return_bps": fixed_horizon["gross_return_bps"],
                    "fixed_horizon_net_return_bps": fixed_horizon["net_return_bps"],
                    "fixed_horizon_cost_bps": fixed_horizon["actual_cost_bps"],
                    "fixed_horizon_result": (
                        "SUCCESS"
                        if fixed_horizon["net_return_bps"] > 0
                        else "FAIL"
                        if fixed_horizon["net_return_bps"] < 0
                        else "BREAKEVEN"
                    ),
                }
                saved = self.validation.record_outcome(
                    candidate_id=row["candidate_id"], horizon=horizon, result=result,
                    net_return_bps=returns["net_return_bps"], gross_return_bps=returns["gross_return_bps"],
                    settled_at=path[-1][0].isoformat().replace("+00:00", "Z"),
                    market_regime=str(features.get("market_regime") or features.get("regime") or "UNKNOWN"),
                    same_bar_ambiguous=same_bar, actual_cost_bps=returns["actual_cost_bps"],
                    proof=proof,
                )
                label = self.quant_data.record_label(
                    candidate_id=row["candidate_id"],
                    horizon=horizon,
                    result=saved["result"],
                    gross_return_bps=returns["gross_return_bps"],
                    net_return_bps=returns["net_return_bps"],
                    settled_at=path[-1][0].isoformat().replace("+00:00", "Z"),
                    market_regime=str(features.get("market_regime") or features.get("regime") or "UNKNOWN"),
                    same_bar_stop_first=same_bar,
                    proof=proof,
                )
                updated += int(bool(saved.get("inserted")))
                settlements.append({
                    "candidate_id": row["candidate_id"], "horizon": horizon,
                    "result": saved["result"], "net_return_bps": saved["net_return_bps"],
                    "outcome_reason": outcome_reason,
                    "quant_label_hash": label["record_hash"],
                })
        return {
            "ok": True, "version": SETTLEMENT_VERSION, "mode": desk,
            "symbol": str(symbol or "").upper().strip(), "updated": updated,
            "pending": pending, "skipped_incomplete_plan": skipped_incomplete,
            "same_bar_stop_first": ambiguous, "settlements": settlements,
            "reconciliation": reconciliation,
            "production_authority": "UNCHANGED_HEURISTIC_BASELINE",
        }
