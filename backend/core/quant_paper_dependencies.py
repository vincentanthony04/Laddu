"""Shared primitives for production prediction and paper-trading services.

A usable promoted strategy is ``PAPER_ACTIVE`` with an explicit non-zero
decision weight. A scored but unpromoted prediction remains calculation-only
shadow evidence with zero ranking authority. A missing, corrupt or incompatible
artifact is ``MODEL_UNAVAILABLE``. Broker order authority remains zero and this
service never imports an order-placement client.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from core.india_cash_cost_service import IndiaCashCostService
from core.model_portfolio_risk_service import ModelPortfolioRiskService
from core.nse_cross_sectional_selector_service import (
    DELIVERY_FEATURES,
    FEATURE_MANIFEST_HASH,
    INTRADAY_FEATURES,
)
from core.production_mode_policy import require_production_mode
from core.quant_research_dataset_service import feature_value
from core.range_compression_rule_service import (
    RangeCompressionRuleService, RULE_ID as RANGE_COMPRESSION_RULE_ID,
    RULE_VERSION as RANGE_COMPRESSION_RULE_VERSION,
)
from config import DATA_DIR
from core.india_time import INDIA_TZ
from core.forward_horizon_policy import PRIMARY_HORIZON


SERVICE_VERSION = "production-prediction-paper-3.1.0-shadow-score"
RULE_ID = "active-prediction-automatic-paper-v1"
PREDICTION_ACTIVE = "PAPER_ACTIVE"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
EVALUATION_PAPER_WEIGHT = 0.05
PAPER_WEIGHT = 0.20
STATISTICALLY_QUALIFIED_WEIGHT = 0.65
RULE_MODEL_WEIGHT = 1.0
LONG_SCORE_THRESHOLD = 80.0
SHORT_SCORE_THRESHOLD = 20.0
INITIAL_CAPITAL = 500_000.0
INTRADAY_CAPITAL = 100_000.0
DEFAULT_MODEL_HORIZON = dict(PRIMARY_HORIZON)
FIXED_HORIZON_OBJECTIVE = "FIXED_HORIZON_TOP_COHORT_NET_RETURN"
TRADE_MAP_OVERLAY_OBJECTIVE = "TARGET_STOP_TRADE_MAP_OVERLAY"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any, length: int = 64) -> str:
    material = value if isinstance(value, (bytes, bytearray)) else _canonical(value).encode("utf-8")
    if isinstance(material, str):
        material = material.encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:length]


def _number(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _parse_timestamp_utc(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=INDIA_TZ)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _selector_signal_side(
    *,
    requested_side: Any,
    selected_for_top_cohort: bool,
    mode: Any,
) -> str:
    """Return the candidate side only when a ranker selected its top cohort.

    LambdaRank is trained on the return of each candidate's already-declared
    side. A low rank therefore means "do not take this candidate"; it never
    means "reverse the candidate into a short".
    """
    desk = require_production_mode(mode)
    side = str(requested_side or "").upper().strip()
    if not selected_for_top_cohort or side not in {"LONG", "SHORT"}:
        return ""
    if desk == "delivery" and side == "SHORT":
        return ""
    return side


def _entry_crossed(side: Any, *, market_price: float, entry: float) -> bool:
    direction = str(side or "").upper().strip()
    price = float(market_price)
    trigger = float(entry)
    return price >= trigger if direction == "LONG" else (
        price <= trigger if direction == "SHORT" else False
    )


def _default_model_horizon(mode: Any) -> str:
    return DEFAULT_MODEL_HORIZON[require_production_mode(mode)]


def _candidate_avg_daily_value(
    candidate: Mapping[str, Any], *, market_price: float
) -> Optional[float]:
    direct = _number(
        candidate.get("avg_daily_value")
        if candidate.get("avg_daily_value") is not None
        else candidate.get("average_daily_value")
    )
    if direct is not None and direct > 0:
        return direct
    volume = _number(
        candidate.get("avg_volume_20d")
        if candidate.get("avg_volume_20d") is not None
        else candidate.get("average_volume")
        if candidate.get("average_volume") is not None
        else candidate.get("avg_traded_qty")
    )
    if volume is not None and volume > 0 and market_price > 0:
        return volume * market_price
    return None


def _candidate_adv_evidence(
    candidate: Mapping[str, Any], *, market_price: float
) -> Dict[str, Any]:
    value = _candidate_avg_daily_value(candidate, market_price=market_price)
    freshness = str(
        candidate.get("avg_daily_value_freshness_state") or ""
    ).upper().strip()
    source = str(candidate.get("avg_daily_value_source") or "").strip()
    as_of = str(candidate.get("avg_daily_value_as_of") or "").strip()
    try:
        sessions = int(candidate.get("avg_daily_value_sessions") or 0)
    except (TypeError, ValueError):
        sessions = 0
    evidence_at = _parse_timestamp_utc(as_of)
    decision_at = _parse_timestamp_utc(
        candidate.get("decision_ts")
        or candidate.get("decision_as_of")
        or candidate.get("observed_at")
        or _now()
    )
    age_seconds = (
        (decision_at - evidence_at).total_seconds()
        if decision_at is not None and evidence_at is not None
        else None
    )
    # Ten calendar days safely spans ordinary NSE weekends/holidays while
    # rejecting an old cached ADV value and any look-ahead timestamp.
    timestamp_verified = bool(
        age_seconds is not None
        and -300.0 <= age_seconds <= 10 * 24 * 60 * 60
    )
    verified = bool(
        value is not None
        and value > 0
        and sessions >= 20
        and as_of
        and source
        and timestamp_verified
        and freshness in {"VERIFIED_CLOSE", "FRESH", "LIVE_CURRENT"}
    )
    return {
        "value": value,
        "verified": verified,
        "freshness_state": freshness or "MISSING",
        "source": source or "MISSING",
        "as_of": as_of or None,
        "sessions": sessions,
        "timestamp_verified": timestamp_verified,
        "age_days": (
            round(max(0.0, float(age_seconds)) / 86_400.0, 4)
            if age_seconds is not None
            else None
        ),
    }


def _intraday_horizon_exit(opened_at: datetime) -> datetime:
    """Return the single governed Model Paper intraday mandatory-flat clock."""
    from core.intraday_session_policy import IntradaySessionPolicy
    return IntradaySessionPolicy.mandatory_flat_at(opened_at)


def _is_verified_nse_session_close(
    quote: Mapping[str, Any], *, mark_at: datetime
) -> bool:
    from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY
    local = mark_at.astimezone(INDIA_TZ)
    sessions = DEFAULT_TRADING_SESSION_AUTHORITY
    if not sessions.calendar_covered(local.date()):
        return False
    window = sessions.session_window(local.date())
    if window is None:
        return False
    explicit = quote.get("session_close_verified") is True
    state = str(quote.get("freshness_state") or "").lower().strip()
    close_state = state in {"verified_close", "closed_market"}
    after_close = local >= window.close_at()
    return bool(explicit or (close_state and after_close) or (
        state in {"live", "fresh", "live_current"} and after_close
    ))


def _advance_session_counter(
    *,
    last_session_date: Any,
    sessions_observed: int,
    required_sessions: int,
    mark_at: datetime,
) -> Dict[str, Any]:
    """Advance a delivery horizon from distinct verified NSE close dates."""
    required = max(0, int(required_sessions or 0))
    observed = max(0, int(sessions_observed or 0))
    session_date = mark_at.astimezone(INDIA_TZ).date().isoformat()
    last = str(last_session_date or "")
    from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY
    local_day = mark_at.astimezone(INDIA_TZ).date()
    if (
        required > 0
        and DEFAULT_TRADING_SESSION_AUTHORITY.calendar_covered(local_day)
        and DEFAULT_TRADING_SESSION_AUTHORITY.is_trading_day(local_day)
        and session_date != last
    ):
        observed += 1
        last = session_date
    return {
        "last_session_date": last or session_date,
        "sessions_observed": observed,
        "required_sessions": required,
        "due": bool(required > 0 and observed >= required),
    }


def _paper_risk_scale(
    *,
    closed_trades: int,
    realized_net_pnl: float,
    gross_profit: float,
    gross_loss: float,
    portfolio_drawdown_pct: float,
) -> float:
    """Conservative automatic risk throttle for the ₹5 lakh paper sleeve."""
    closed = max(0, int(closed_trades or 0))
    net = float(realized_net_pnl or 0.0)
    profit = max(0.0, float(gross_profit or 0.0))
    loss = max(0.0, float(gross_loss or 0.0))
    drawdown = max(0.0, float(portfolio_drawdown_pct or 0.0))
    profit_factor = profit / loss if loss > 0 else (float("inf") if profit > 0 else 0.0)
    if closed < 10:
        evidence_scale = 0.50
    elif net <= 0.0 or profit_factor < 1.0:
        evidence_scale = 0.25
    elif closed < 30:
        evidence_scale = 0.75
    elif profit_factor >= 1.20:
        evidence_scale = 1.00
    else:
        evidence_scale = 0.75
    if drawdown >= 8.0:
        return 0.0
    if drawdown >= 5.0:
        return min(evidence_scale, 0.25)
    if drawdown >= 3.0:
        return min(evidence_scale, 0.50)
    return evidence_scale


class LightGbmArtifactAdapter:
    """Dependency-free inference for the numeric LightGBM text artifact.

    Training remains isolated in the research virtual environment.  The live
    service evaluates the audited text model directly, avoiding an optional
    LightGBM import and avoiding a subprocess per ranked candidate.
    """

    def __init__(self, artifact: Mapping[str, Any]):
        self.artifact = dict(artifact)
        self.feature_names = [str(value) for value in artifact.get("feature_names") or []]
        self.medians = [float(value) for value in artifact.get("medians") or []]
        self.score_adapter = dict(artifact.get("score_adapter") or {})
        dump = artifact.get("booster_dump_model")
        self.dump_trees = (
            [
                item.get("tree_structure")
                for item in dump.get("tree_info") or []
                if isinstance(item, Mapping) and isinstance(item.get("tree_structure"), Mapping)
            ]
            if isinstance(dump, Mapping) else []
        )
        self.trees = self._parse_trees(str(artifact.get("booster_model") or ""))
        if not self.feature_names or len(self.medians) != len(self.feature_names):
            raise ValueError("artifact feature/median lineage is incomplete")
        if not self.dump_trees and not self.trees:
            raise ValueError("artifact contains no parseable LightGBM trees")

    @staticmethod
    def _values(raw: str, cast):
        return [cast(value) for value in str(raw or "").split() if value != ""]

    @classmethod
    def _parse_trees(cls, model_text: str) -> list[Dict[str, Any]]:
        trees: list[Dict[str, Any]] = []
        current: Dict[str, str] = {}
        for line in model_text.splitlines():
            clean = line.strip()
            if clean.startswith("Tree="):
                if current:
                    trees.append(cls._tree(current))
                current = {}
                continue
            if "=" in clean and current is not None:
                key, value = clean.split("=", 1)
                current[key] = value
        if current and "leaf_value" in current:
            trees.append(cls._tree(current))
        return [tree for tree in trees if tree.get("leaf_value")]

    @classmethod
    def _tree(cls, raw: Mapping[str, str]) -> Dict[str, Any]:
        return {
            "split_feature": cls._values(raw.get("split_feature", ""), int),
            "threshold": cls._values(raw.get("threshold", ""), float),
            "decision_type": cls._values(raw.get("decision_type", ""), int),
            "left_child": cls._values(raw.get("left_child", ""), int),
            "right_child": cls._values(raw.get("right_child", ""), int),
            "leaf_value": cls._values(raw.get("leaf_value", ""), float),
        }

    @staticmethod
    def _tree_score(tree: Mapping[str, Sequence[Any]], values: Sequence[float]) -> float:
        node = 0
        splits = tree["split_feature"]
        while node >= 0:
            if node >= len(splits):
                raise ValueError("invalid LightGBM tree node")
            feature = int(splits[node])
            threshold = float(tree["threshold"][node])
            decision_type = (
                int(tree["decision_type"][node])
                if node < len(tree["decision_type"])
                else 0
            )
            if decision_type & 1:
                raise ValueError("categorical LightGBM split is unsupported by paper adapter")
            go_left = float(values[feature]) <= threshold
            children = tree["left_child"] if go_left else tree["right_child"]
            node = int(children[node])
        leaf_index = -node - 1
        leaves = tree["leaf_value"]
        if leaf_index < 0 or leaf_index >= len(leaves):
            raise ValueError("invalid LightGBM leaf index")
        return float(leaves[leaf_index])

    @classmethod
    def _dump_tree_score(cls, node: Mapping[str, Any], values: Sequence[float]) -> float:
        if node.get("leaf_value") is not None:
            value = _number(node.get("leaf_value"))
            if value is None:
                raise ValueError("invalid LightGBM dump leaf")
            return value
        feature = int(node.get("split_feature"))
        threshold = node.get("threshold")
        decision = str(node.get("decision_type") or "<=")
        if decision != "<=":
            raise ValueError(f"unsupported LightGBM dump decision_type {decision}")
        threshold_value = _number(threshold)
        if threshold_value is None or feature < 0 or feature >= len(values):
            raise ValueError("invalid LightGBM dump numeric split")
        branch = "left_child" if float(values[feature]) <= threshold_value else "right_child"
        child = node.get(branch)
        if not isinstance(child, Mapping):
            raise ValueError("invalid LightGBM dump child")
        return cls._dump_tree_score(child, values)

    def raw_score(self, values: Sequence[float]) -> float:
        if len(values) != len(self.feature_names):
            raise ValueError("feature width does not match artifact")
        if self.dump_trees:
            return sum(self._dump_tree_score(tree, values) for tree in self.dump_trees)
        return sum(self._tree_score(tree, values) for tree in self.trees)

    def normalize(self, raw_score: float) -> float:
        knots = self.score_adapter.get("quantile_knots") or []
        usable = []
        for item in knots:
            if not isinstance(item, Mapping):
                continue
            score = _number(item.get("raw_score"))
            percentile = _number(item.get("percentile"))
            if score is not None and percentile is not None:
                usable.append((score, max(0.0, min(1.0, percentile))))
        usable.sort()
        if usable:
            if raw_score <= usable[0][0]:
                return round(100.0 * usable[0][1], 6)
            if raw_score >= usable[-1][0]:
                return round(100.0 * usable[-1][1], 6)
            for (left_score, left_pct), (right_score, right_pct) in zip(usable, usable[1:]):
                if left_score <= raw_score <= right_score:
                    if abs(right_score - left_score) <= 1e-12:
                        return round(100.0 * max(left_pct, right_pct), 6)
                    fraction = (raw_score - left_score) / (right_score - left_score)
                    return round(100.0 * (left_pct + fraction * (right_pct - left_pct)), 6)
        center = _number(self.score_adapter.get("center")) or 0.0
        scale = abs(_number(self.score_adapter.get("scale")) or 1.0)
        z = max(-8.0, min(8.0, (raw_score - center) / max(scale, 1e-12)))
        return round(100.0 / (1.0 + math.exp(-z)), 6)

# Explicitly export private helper primitives to the focused service mixins.
__all__ = [name for name in globals() if not name.startswith("__")]
