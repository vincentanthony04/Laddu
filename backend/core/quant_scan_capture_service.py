"""Scan-owned point-in-time capture for the shadow quant research loop.

The dashboard is deliberately not an orchestration surface. Canonical scan
completion calls this service once with every analysed row, including rows the
production policy rejected or left on watch.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping

from core.india_time import INDIA_TZ, india_now
from core.runtime_primitives import is_india_market_open
from core.nse_cross_sectional_selector_service import FEATURE_MANIFEST_HASH
from core.production_mode_policy import require_production_mode
from core.quant_research_orchestrator_service import QuantResearchOrchestratorService
from core.market_regime_change_service import MarketRegimeChangeService
from core.selection_platform_service import SelectionPlatformService


QUANT_SCAN_CAPTURE_VERSION = "quant-scan-capture-3.2.0-event-risk-pit"


def record_quant_scan_cycle(
    host: Any,
    rows: Iterable[Mapping[str, Any]],
    mode: str,
    observed_at: str,
    universe_size: int,
) -> Dict[str, Any]:
    """Keep scan orchestration thin while preserving fail-closed diagnostics."""
    captured = list(rows)
    if not captured:
        return {
            "state": "NO_ANALYSED_CANDIDATES",
            "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
            "production_change_allowed": False,
        }
    try:
        return QuantScanCaptureService(host.store).record(
            captured,
            mode=mode,
            observed_at=observed_at,
            universe_size=universe_size,
        )
    except Exception as exc:
        recorder = getattr(host, "record_error", None)
        if callable(recorder):
            recorder("quant_shadow_cycle", str(exc)[:200])
        return {
            "state": "RECORD_FAILED",
            "error": str(exc)[:200],
            "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
            "production_change_allowed": False,
        }


def _first(row: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def _market_regime(value: Any) -> tuple[str, str]:
    text = str(value or "").strip().lower()
    if any(token in text for token in ("supportive", "bull", "uptrend", "trending_up")):
        return "BULL", "INDEX_CONTEXT"
    if any(token in text for token in ("risk", "bear", "downtrend", "trending_down")):
        return "BEAR", "INDEX_CONTEXT"
    if any(token in text for token in ("neutral", "mixed", "range", "sideways")):
        return "RANGE", "INDEX_CONTEXT"
    return "UNKNOWN", "MISSING"


class QuantScanCaptureService:
    """Capture one canonical scan population and trigger only due shadow work."""

    def __init__(self, store: Any):
        self.store = store

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            number = float(value)
            return number if number > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _signed_number(value: Any) -> float | None:
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _candle_time(row: Mapping[str, Any]) -> datetime | None:
        raw = (
            row.get("timestamp")
            or row.get("ts")
            or row.get("time")
            or row.get("date")
        )
        if raw in (None, ""):
            return None
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return (
                parsed.astimezone(timezone.utc)
                if parsed.tzinfo
                else parsed.replace(tzinfo=INDIA_TZ).astimezone(timezone.utc)
            )
        except (TypeError, ValueError):
            return None

    def _attach_adv_evidence(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        """Attach strictly pre-decision daily features from canonical candles.

        R26 expands the existing ADV lookup into the minimum compact history
        features that can be proved from already-retained daily bars. The
        boundary is the candidate's *decision session*, not today's session, so
        recovering an older Research candidate can never consume future candles.
        No delivery/sector/event value is fabricated when its source is absent.
        """
        item = dict(row or {})
        # PL34: the canonical scanner already evaluates the scheduled-event
        # authority at decision time and retains an explicit event_risk mapping.
        # Project that existing point-in-time result into the shadow selector as
        # a scalar penalty: 1 = known scheduled-event exposure, 0 = canonical
        # event authority explicitly clear. If the authority mapping is absent or
        # malformed, leave the feature missing rather than manufacturing a clear.
        event_risk = item.get("event_risk")
        if item.get("event_risk_score") in (None, "") and isinstance(event_risk, Mapping):
            flag = event_risk.get("flag")
            if isinstance(flag, bool):
                item["event_risk_score"] = 1.0 if flag else 0.0
                item["event_risk_source"] = "CANONICAL_DECISION_EVENT_RISK_AUTHORITY"
                item["event_risk_as_of"] = (
                    item.get("decision_ts") or item.get("decision_as_of")
                    or item.get("observed_at")
                )
                if flag and event_risk.get("nearest_event_date") not in (None, ""):
                    item["event_risk_nearest_date"] = str(event_risk.get("nearest_event_date"))
        # PL32: sector-relative strength is a retained selector feature, but the
        # canonical scanner already carries both stock % change and the mapped
        # sector-index % change at decision time. Preserve that exact PIT
        # relationship even when daily-candle enrichment is unavailable. No
        # sector value is guessed and the 60% completeness gate is unchanged.
        if not any(item.get(name) not in (None, "") for name in ("sector_relative_strength", "sector_relative_return")):
            sector_status = str(item.get("sector_status") or "").strip().lower()
            stock_change = self._signed_number(item.get("change_pct") if item.get("change_pct") is not None else item.get("percent_change"))
            sector_change = self._signed_number(item.get("sector_change_pct"))
            if sector_status != "unavailable" and stock_change is not None and sector_change is not None:
                item["sector_relative_return"] = round(stock_change - sector_change, 6)
                item["sector_relative_source"] = "DECISION_STOCK_CHANGE_MINUS_MAPPED_SECTOR_INDEX_CHANGE"
                item["sector_relative_as_of"] = (item.get("sector_freshness") or item.get("decision_ts") or item.get("decision_as_of") or item.get("observed_at"))
        instrument_key = str(item.get("instrument_key") or "").strip()
        getter = getattr(self.store, "get_candles", None)
        if not instrument_key or not callable(getter):
            item.setdefault("avg_daily_value_freshness_state", "MISSING")
            item.setdefault("avg_daily_value_source", "canonical_daily_candles_unavailable")
            return item
        decision_raw = str(
            item.get("decision_ts") or item.get("decision_as_of")
            or item.get("observed_at") or ""
        ).strip()
        try:
            decision_dt = datetime.fromisoformat(decision_raw.replace("Z", "+00:00"))
            if decision_dt.tzinfo is None:
                decision_dt = decision_dt.replace(tzinfo=INDIA_TZ)
            decision_session = decision_dt.astimezone(INDIA_TZ).date()
        except (TypeError, ValueError):
            decision_session = india_now().date()
        try:
            raw_rows = getter(instrument_key, "day", limit=150) or []
        except TypeError:
            raw_rows = getter(instrument_key, "day", 150) or []
        except Exception:
            raw_rows = []
        completed = []
        for raw in raw_rows:
            candle = dict(raw or {})
            stamp = self._candle_time(candle)
            volume = self._number(candle.get("volume"))
            close = self._number(candle.get("close"))
            if stamp is None or close is None or stamp.astimezone(INDIA_TZ).date() >= decision_session:
                continue
            completed.append((stamp, volume, close))
        completed.sort(key=lambda value: value[0])
        if len(completed) < 20:
            item["avg_daily_value_freshness_state"] = "INSUFFICIENT_HISTORY"
            item["avg_daily_value_source"] = "canonical_daily_candles"
            item["avg_daily_value_sessions"] = len(completed)
            return item

        recent20 = completed[-20:]
        traded_values = [volume * close for _stamp, volume, close in recent20 if volume is not None]
        average_volumes = [volume for _stamp, volume, _close in recent20 if volume is not None]
        if traded_values:
            adv = sum(traded_values) / len(traded_values)
            item.setdefault("avg_daily_value", round(adv, 2))
            # Cross-sectional ranking only needs a monotonic liquidity measure.
            # log10 compresses the rupee-value scale without inventing a score.
            item.setdefault("liquidity_score", round(math.log10(max(1.0, adv)), 6))
        if average_volumes:
            item.setdefault("avg_volume_20d", round(sum(average_volumes) / len(average_volumes), 2))
        item.update({
            "avg_daily_value_sessions": len(recent20),
            "avg_daily_value_as_of": recent20[-1][0].isoformat().replace("+00:00", "Z"),
            "avg_daily_value_freshness_state": "VERIFIED_CLOSE",
            "avg_daily_value_source": "canonical_daily_candles_before_decision",
            "historical_feature_boundary": "STRICTLY_BEFORE_DECISION_SESSION",
        })

        closes = [float(close) for _stamp, _volume, close in completed]
        for days, key in (
            (20, "relative_strength_20d"),
            (60, "relative_strength_60d"),
            (120, "relative_strength_120d"),
        ):
            if len(closes) >= days + 1 and closes[-days-1] > 0:
                item.setdefault(key, round((closes[-1] / closes[-days-1] - 1.0) * 100.0, 6))
        if len(closes) >= 21:
            returns = [
                (closes[index] / closes[index - 1] - 1.0) * 100.0
                for index in range(len(closes) - 19, len(closes))
                if closes[index - 1] > 0
            ]
            if len(returns) >= 10:
                item.setdefault("volatility_pct", round(statistics.pstdev(returns), 6))

        price = self._number(
            item.get("ltp") or item.get("current_price") or item.get("price") or item.get("close")
        )
        vwap = self._number(item.get("vwap") or item.get("average_traded_price"))
        if price is not None and vwap is not None and vwap > 0:
            item.setdefault("vwap_distance_pct", round((price / vwap - 1.0) * 100.0, 6))
        bid = self._number(item.get("bid_price") or item.get("best_bid"))
        ask = self._number(item.get("ask_price") or item.get("best_ask"))
        if bid is not None and ask is not None and ask >= bid and (ask + bid) > 0:
            mid = (ask + bid) / 2.0
            item.setdefault("spread_bps", round((ask - bid) / mid * 10000.0, 6))

        return item

    @staticmethod
    def _prepare(row: Mapping[str, Any], population_at: str) -> Dict[str, Any]:
        item = dict(row or {})
        decision_at = _first(
            item,
            "decision_ts",
            "decision_as_of",
            "observed_at",
            "last_ai_validation",
        ) or population_at
        feature_at = _first(item, "feature_as_of", "decision_as_of") or decision_at
        source_at = _first(
            item,
            "source_as_of",
            "quote_as_of",
            "quote_timestamp",
            "provider_timestamp",
            "provider_ts",
            "provider_time",
            "source_time",
            "quote_time",
            "last_refresh",
            "timestamp",
        )
        received_at = _first(
            item,
            "quote_received_at",
            "received_at",
            "received_time",
            "fetched_at",
            "last_ai_validation",
        ) or decision_at
        existing_freshness = _first(
            item, "freshness_state", "evidence_freshness_state",
            "quote_freshness_state", "price_freshness_state",
        ).upper()
        if source_at:
            try:
                decision_dt = datetime.fromisoformat(str(decision_at).replace("Z", "+00:00"))
                source_dt = datetime.fromisoformat(str(source_at).replace("Z", "+00:00"))
                if decision_dt.tzinfo is None:
                    decision_dt = decision_dt.replace(tzinfo=timezone.utc)
                if source_dt.tzinfo is None:
                    source_dt = source_dt.replace(tzinfo=timezone.utc)
                source_age = (decision_dt.astimezone(timezone.utc) - source_dt.astimezone(timezone.utc)).total_seconds()
                if 0 <= source_age <= 86400 * 8:
                    item.setdefault("quote_age_seconds", round(source_age, 3))
                verified_source = item.get("provider_timestamp_verified") is True
                # Do not manufacture freshness from an arbitrary timestamp.
                # Only a provider-verified source time may be promoted to FRESH,
                # using the same strict live-age ceilings as runtime quote trust.
                max_age = 20.0 if str(item.get("mode") or "").lower() == "intraday" else 60.0
                if not existing_freshness and verified_source and 0 <= source_age <= max_age:
                    existing_freshness = "FRESH"
                    item["freshness_derivation"] = "PROVIDER_TIMESTAMP_VERIFIED_AGE"
                # Delivery training is explicitly allowed to run off the last
                # verified session close (see TRAINING_FRESHNESS["delivery"]
                # in quant_edge_data_service.py). Outside market hours a
                # provider-verified same-session close was previously left
                # unpromoted here and fell through to STALE/UNKNOWN in the
                # training gate even though it is legitimate closed-market
                # evidence. Promote it explicitly, bounded to a recent
                # session (covers weekends/holidays up to ~4 calendar days)
                # so it cannot silently admit genuinely old data.
                elif (
                    str(item.get("mode") or "").lower() == "delivery"
                    and existing_freshness not in ("LIVE", "FRESH", "CLOSED_MARKET", "VERIFIED_CLOSE")
                    and verified_source
                    and not is_india_market_open()
                    and 0 <= source_age <= 86400 * 4
                ):
                    existing_freshness = "CLOSED_MARKET"
                    item["freshness_derivation"] = "PROVIDER_TIMESTAMP_VERIFIED_CLOSED_MARKET"
            except (TypeError, ValueError):
                pass
        if existing_freshness:
            item["freshness_state"] = existing_freshness
        membership_at = _first(
            item,
            "universe_membership_as_of",
            "constituent_as_of",
            "universe_as_of",
        ) or decision_at
        supplied_regime = _first(item, "market_regime", "regime", "regime_tag")
        market_regime, regime_source = (
            (supplied_regime.upper(), "SOURCE_FIELD")
            if supplied_regime
            else _market_regime(item.get("index_context"))
        )
        item.update(
            {
                "decision_ts": decision_at,
                "decision_as_of": decision_at,
                "observed_at": decision_at,
                "feature_as_of": feature_at,
                "source_as_of": source_at,
                "received_at": received_at,
                "universe_membership_as_of": membership_at,
                "market_regime": market_regime,
                "regime_derivation": regime_source,
                "point_in_time_capture_version": QUANT_SCAN_CAPTURE_VERSION,
                "lineage_evidence": {
                    "feature_as_of": (
                        "SOURCE_FIELD"
                        if _first(row, "feature_as_of", "decision_as_of")
                        else "DECISION_COMPUTATION_TIME"
                    ),
                    "source_as_of": "SOURCE_FIELD" if source_at else "MISSING",
                    "received_at": (
                        "SOURCE_FIELD"
                        if _first(row, "quote_received_at", "received_at", "received_time", "fetched_at")
                        else "DECISION_PIPELINE_TIME"
                    ),
                    "universe_membership_as_of": (
                        "SOURCE_FIELD"
                        if _first(row, "universe_membership_as_of", "constituent_as_of", "universe_as_of")
                        else "CANONICAL_SCAN_RESOLUTION_TIME"
                    ),
                },
            }
        )
        return item

    @staticmethod
    def _dataset_fingerprint(rows: Iterable[Mapping[str, Any]]) -> str:
        material = sorted(
            (dict(row or {}) for row in rows),
            key=lambda item: (
                str(item.get("mode") or ""),
                str(item.get("symbol") or ""),
                str(item.get("decision_ts") or ""),
            ),
        )
        encoded = json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _institutional_context(self) -> Dict[str, Any]:
        """Read the latest market-wide FII/FPI and DII regime once per scan.
        This is context evidence only; it is never labelled stock-specific."""
        try:
            rows = [dict(r) for r in self.store.conn.execute(
                """SELECT trade_date,category,net_value_crore,provisional
                   FROM institutional_market_flows
                   ORDER BY trade_date DESC,category LIMIT 50"""
            ).fetchall()]
        except Exception:
            return {"state": "UNAVAILABLE", "regime": "UNKNOWN", "market_scope": "NSE_CASH_MARKET"}
        by_category = {"FII/FPI": [], "DII": []}
        for row in rows:
            category = str(row.get("category") or "")
            if category in by_category and len(by_category[category]) < 20:
                by_category[category].append(float(row.get("net_value_crore") or 0.0))
        fii5 = sum(by_category["FII/FPI"][:5])
        dii5 = sum(by_category["DII"][:5])
        if fii5 > 0 and dii5 > 0:
            regime = "BROAD_INSTITUTIONAL_BUYING"
        elif fii5 > 0:
            regime = "FII_POSITIVE"
        elif fii5 < 0 and dii5 > 0:
            regime = "DII_ABSORPTION"
        elif fii5 < 0 and dii5 <= 0:
            regime = "INSTITUTIONAL_RISK_OFF"
        else:
            regime = "MIXED_OR_INSUFFICIENT"
        latest = max((str(row.get("trade_date") or "") for row in rows), default="") or None
        return {
            "state": "AVAILABLE" if rows else "UNAVAILABLE",
            "regime": regime,
            "fii_net_5d_crore": round(fii5, 2),
            "dii_net_5d_crore": round(dii5, 2),
            "latest_trade_date": latest,
            "provisional": True,
            "market_scope": "NSE cash-market aggregate; not stock-specific identity",
        }

    def record(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        mode: str,
        observed_at: str,
        universe_size: int,
    ) -> Dict[str, Any]:
        desk = require_production_mode(mode)
        flow = self._institutional_context()
        captured = []
        for row in rows:
            item = self._attach_adv_evidence(self._prepare(row, observed_at))
            item.update({
                "institutional_flow_regime": flow.get("regime"),
                "fii_net_5d_crore": flow.get("fii_net_5d_crore"),
                "dii_net_5d_crore": flow.get("dii_net_5d_crore"),
                "institutional_flow_as_of": flow.get("latest_trade_date"),
                "institutional_flow_provisional": flow.get("provisional"),
                "institutional_flow_scope": flow.get("market_scope"),
            })
            captured.append(item)
        if not captured:
            return {
                "state": "NO_ANALYSED_CANDIDATES",
                "prediction_state": "MODEL_UNAVAILABLE",
                "decision_weight": 0.0,
                "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
            }
        regime = MarketRegimeChangeService(self.store).observe_population(captured, observed_at=observed_at)
        for item in captured:
            item.update({
                "market_regime": regime.get("confirmed_regime") or regime.get("regime") or "UNKNOWN",
                "regime_candidate": regime.get("candidate_regime") or "UNKNOWN",
                "regime_transition_state": regime.get("transition_state") or "NOT_OBSERVED",
                "regime_confidence": regime.get("confidence"),
                "regime_change_probability": regime.get("change_probability"),
                "regime_observation_id": regime.get("observation_id"),
            })
        population = SelectionPlatformService(self.store).evaluate_population(
            captured,
            mode=desk,
            observed_at=observed_at,
            universe_id=f"canonical-scan:{desk}:{max(0, int(universe_size))}",
            dataset_fingerprint=self._dataset_fingerprint(captured),
            feature_manifest_hash=FEATURE_MANIFEST_HASH,
        )
        cycle = QuantResearchOrchestratorService(self.store).maybe_run_cycle(
            mode=desk,
            trigger=f"{desk}-scan-completion",
        )
        return {
            "state": "POINT_IN_TIME_RECORDED",
            "capture_version": QUANT_SCAN_CAPTURE_VERSION,
            "population_fingerprint": population.get("population_fingerprint"),
            "candidate_count": population.get("candidate_count"),
            "quant_snapshot_ledger": (
                (population.get("recorded_population") or {}).get("quant_snapshot_ledger")
            ),
            "research_cycle": cycle,
            "prediction_state": population.get("prediction_state", "MODEL_UNAVAILABLE"),
            "decision_weight": population.get("decision_weight", 0.0),
            "automatic_paper": population.get("quant_paper"),
            "market_regime": regime,
            "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
            "broker_execution_weight": 0.0,
        }
