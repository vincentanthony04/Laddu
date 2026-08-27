from __future__ import annotations

"""Clean Core selected-stock read model.

This is deliberately a read path, not a workflow. It resolves identity from the
local catalogue and combines independently available local quote, candle/MTF,
levels, fundamentals, retained Research and canonical decision evidence. No
scanner, priority pipeline, controller, model training or provider request is
required for the response.
"""

from datetime import datetime, timezone
import hashlib
import json
import threading
import time
from typing import Any, Dict

from core.canonical_presentation_service import CanonicalPresentationService
from core.materialized_research_snapshot_service import MaterializedResearchSnapshotService
from core.materialized_fundamental_snapshot_service import MaterializedFundamentalSnapshotService
from core.technical_snapshot_service import TechnicalSnapshotService
from core.price_performance_service import PricePerformanceService
from core.market_level_service import reconcile_level_snapshot
from core.trade_map_projection_service import TradeMapProjectionService
from core.market_clock import india_now, is_india_market_open
from core.quote_integrity_service import revalidate_cached_quote
from core.local_projection_dispatcher import for_app as local_projection_dispatcher_for_app, PRIORITY_PERSISTENCE


class StockSnapshotService:
    VERSION = "clean-core-stock-snapshot-1.13.0-explicit-multiscope-levels"

    def __init__(self, app: Any):
        self.app = app
        self.store = app.store
        self.presentation = CanonicalPresentationService(self.store)
        self.technical = TechnicalSnapshotService(app)
        self.fundamental_snapshot = MaterializedFundamentalSnapshotService(app)
        self.research = MaterializedResearchSnapshotService(app)
        if not hasattr(app, "_selected_quote_cache"):
            setattr(app, "_selected_quote_cache", {})
        if not hasattr(app, "_selected_quote_cache_lock"):
            setattr(app, "_selected_quote_cache_lock", threading.RLock())
        self._quote_cache = app._selected_quote_cache
        self._quote_lock = app._selected_quote_cache_lock
        if not hasattr(app, "_selected_structural_snapshot_cache"):
            setattr(app, "_selected_structural_snapshot_cache", {})
        if not hasattr(app, "_selected_structural_snapshot_cache_lock"):
            setattr(app, "_selected_structural_snapshot_cache_lock", threading.RLock())
        self._structural_cache = app._selected_structural_snapshot_cache
        self._structural_lock = app._selected_structural_snapshot_cache_lock


    @staticmethod
    def _quote_has_price(quote: Dict[str, Any]) -> bool:
        try:
            return float((quote or {}).get("ltp") or (quote or {}).get("last_price") or 0.0) > 0
        except Exception:
            return False

    @staticmethod
    def _display_quote(quote: Dict[str, Any], performance: Dict[str, Any]) -> Dict[str, Any]:
        """Customer display price without weakening execution-price authority.

        A live/executable quote remains the only execution authority. After market
        close (or on a cold quote cache) the latest verified completed daily close
        may be displayed so Stock Intelligence never renders a misleading ₹0.00
        when retained history is already authoritative.
        """
        raw = dict(quote or {})
        try:
            live_price = float(raw.get("ltp") or raw.get("last_price") or 0.0)
        except Exception:
            live_price = 0.0
        if live_price > 0:
            return {**raw, "display_only": False, "display_price_authority": "SELECTED_QUOTE"}
        try:
            close_price = float((performance or {}).get("current_price") or 0.0)
        except Exception:
            close_price = 0.0
        if close_price <= 0:
            return raw
        stamp = (performance or {}).get("source_last_completed_candle") or (performance or {}).get("as_of")
        return {
            "ltp": close_price, "last_price": close_price, "close": close_price,
            "timestamp": stamp, "source_time": stamp,
            "freshness_state": "completed_session_close",
            "source": "COMPLETED_DAILY_CANDLE_DISPLAY_ONLY",
            "display_only": True,
            "execution_price_authority": False,
            "display_price_authority": "COMPLETED_DAILY_CANDLES",
        }

    @staticmethod
    def _decision_proof(*, instrument: Dict[str, Any], component_states: Dict[str, Any], decision: Dict[str, Any], trade_map: Dict[str, Any], research: Dict[str, Any], fundamentals: Dict[str, Any], official_nse_evidence: Dict[str, Any]) -> Dict[str, Any]:
        """Project the customer-visible fail-closed decision gate chain.

        This is a projection of already-materialized evidence only. It never
        invents a PASS when a source value is absent; unavailable evidence is
        WARN/DEFERRED and the first hard blocker is explicit.
        """
        def state_of(name: str) -> str:
            return str((component_states.get(name) or {}).get("state") or "UNAVAILABLE").upper()
        def gate(name: str, status: str, actual: Any, rule: str, *, age: Any = None, hard: bool = False, reason: str = "") -> Dict[str, Any]:
            return {"gate": name, "status": status, "actual": actual, "rule": rule, "evidence_age": age, "hard_gate": bool(hard), "reason": reason}

        venue = str(instrument.get("exchange") or "").upper()
        venue_ok = venue in {"NSE", "BSE"}
        tech_ready = state_of("technical_snapshot") == "READY"
        chart_ready = state_of("chart") == "READY"
        quote_ready = state_of("quote") == "READY"
        decision_present = bool(decision)
        map_valid = bool(trade_map.get("valid"))
        map_state = str(trade_map.get("state") or decision.get("lifecycle_state") or decision.get("canonical_state") or decision.get("status") or "UNAVAILABLE").upper()
        raw_action = str(decision.get("display_action") or decision.get("current_action") or decision.get("management_action") or decision.get("decision") or decision.get("action") or "").upper()
        if any(x in map_state for x in ("REJECT","INVALID","BLOCK")):
            action = "REJECT"
        elif not decision_present:
            action = "NO-TRADE"
        elif any(x in raw_action for x in ("SELL","EXIT")):
            action = "SELL"
        elif any(x in raw_action for x in ("BUY","ENTER")):
            action = "BUY"
        elif any(x in raw_action for x in ("HOLD","CONTINUE")) or map_state in {"OPEN","OPENED"}:
            action = "HOLD"
        else:
            action = "NO-TRADE"

        rr = trade_map.get("room_rr") if trade_map.get("room_rr") is not None else trade_map.get("rr")
        setup = decision.get("setup") or decision.get("setup_type") or decision.get("pattern") or decision.get("setup_family")

        # Decision Proof is an evidence projection, not a mirror of whichever
        # fields happened to be copied into the canonical decision row.  Pull
        # independent materialized evidence from the research snapshot first
        # and only fall back to decision-local aliases.
        participation = {}
        for key in ("market_participation", "participation"):
            value = research.get(key)
            if isinstance(value, dict):
                participation.update(value)
        for key in ("behavioural_pattern", "behavioral_pattern", "pattern_evidence", "behavioural_evidence"):
            value = research.get(key)
            if isinstance(value, dict) and isinstance(value.get("market_participation"), dict):
                participation.update(value.get("market_participation") or {})
        liquidity = (
            decision.get("liquidity_state") or decision.get("participation_state") or
            participation.get("liquidity_state") or participation.get("participation_state")
        )
        market_context = decision.get("market_context_state") or decision.get("regime")
        if not market_context:
            for key in ("market_sector_context", "market_context", "regime_context", "sector_context"):
                value = research.get(key)
                if isinstance(value, dict):
                    market_context = value.get("state") or value.get("regime") or value.get("market_regime") or value.get("sector_regime")
                elif value:
                    market_context = value
                if market_context:
                    break
        nse_state = official_nse_evidence.get("state") or official_nse_evidence.get("status")
        ml_influence = decision.get("model_influence_applied")
        def normalized_status(value: Any, *, domain: str = "") -> str:
            v = str(value or "").upper().strip()
            if not v:
                return "UNAVAILABLE"
            if any(token in v for token in ("FAILED", "FAIL", "REJECT", "BLOCKED", "INELIGIBLE", "INVALID", "ERROR", "STUCK")):
                return "FAIL"
            if any(token in v for token in ("PARTIAL", "DEFERRED", "WAITING", "PENDING", "WARMING", "RECOVERING", "NO_PROGRESS")):
                return "WAITING"
            if v in {"LOW", "WEAK"} and domain == "liquidity":
                return "WAITING"
            if v in {"HIGH", "ADEQUATE", "PASS", "READY", "ELIGIBLE", "QUALIFIED", "CURRENT", "COMPLETE", "VERIFIED", "RUNNING", "ACTIVE"} or ("READY" in v and "NOT_READY" not in v):
                return "PASS"
            if v in {"NOT_REQUIRED", "NOT_APPLICABLE", "N/A", "NONE"}:
                return "NOT_REQUIRED"
            return "PASS" if domain == "market_context" else "UNAVAILABLE"

        liquidity_status = normalized_status(liquidity, domain="liquidity")
        market_status = normalized_status(market_context, domain="market_context")
        nse_status = normalized_status(nse_state, domain="nse")
        setup_present = bool(setup and str(setup).upper() not in {"UNSPECIFIED", "NONE", "NO_VALID_SETUP", "UNAVAILABLE"})

        gates = [
            gate("Venue", "PASS" if venue_ok else "FAIL", venue or "UNAVAILABLE", "NSE/BSE cash identity required", hard=True, reason="Canonical venue identity"),
            gate("Data completeness / freshness", "PASS" if tech_ready and chart_ready else "WAITING", f"technical={state_of('technical_snapshot')}; chart={state_of('chart')}; quote={state_of('quote')}", "Technical + chart evidence must be materialized; quote may be unavailable off-market", hard=True, reason="Local materialized evidence readiness"),
            gate("Liquidity / participation", liquidity_status, liquidity or "UNAVAILABLE", "Desk liquidity/participation authority", hard=True, reason="Independent participation evidence; no implicit pass"),
            gate("Market / index / sector regime", market_status, market_context or "UNAVAILABLE", "Regime/context evidence required when material to setup", reason="Independent market/sector context projection"),
            gate("Technical trend / structure / pattern", "PASS" if tech_ready else "WAITING", setup or state_of("technical_snapshot"), "Materialized technical evidence required", hard=True),
            gate("NSE delivery / volume", nse_status, nse_state or "UNAVAILABLE", "Official NSE evidence when applicable", reason="POINT_IN_TIME_*_READY is a valid ready state"),
            gate("Setup / trigger", "PASS" if setup_present else ("FAIL" if decision_present and action == "REJECT" else "WAITING"), setup or "NO_VALID_SETUP", "A named valid setup/trigger is required for entry admission", hard=True),
            gate("S/R room", "PASS" if map_valid else ("WAITING" if setup_present else "NOT_APPLICABLE"), "valid" if map_valid else trade_map.get("block_reason") or "NOT_APPLICABLE", "Positive entry/target/stop geometry and sufficient room", hard=bool(setup_present)),
            gate("Transaction-cost adjusted R:R / risk", "PASS" if map_valid and rr not in (None, "") else ("WAITING" if setup_present else "NOT_APPLICABLE"), rr if rr not in (None, "") else "NOT_APPLICABLE", "Canonical risk authority must authorize geometry/R:R", hard=bool(setup_present)),
            gate("Canonical admission", "PASS" if decision_present and map_state in {"FINAL","OPEN","OPENED","READY","CANONICAL_DECISION_READY"} else ("FAIL" if action == "REJECT" else ("WAITING" if setup_present else "NOT_APPLICABLE")), map_state, "Single canonical decision authority", hard=bool(setup_present or decision_present)),
            gate("ML influence", "PASS" if ml_influence is True else "NOT_REQUIRED", "APPLIED" if ml_influence is True else "0% / shadow unless qualified", "ML cannot be silently promoted", reason="Mathematical authority remains valid without production ML influence"),
            gate("Final Action", "PASS" if action in {"BUY","SELL","HOLD"} else ("FAIL" if action == "REJECT" else "NOT_APPLICABLE"), action, "BUY / SELL / HOLD only after hard-gate admission; otherwise NO-TRADE / REJECT", hard=bool(decision_present)),
        ]
        first_blocker = next((g for g in gates if g["hard_gate"] and g["status"] == "FAIL"), None)
        first_pending = next((g for g in gates if g["hard_gate"] and g["status"] in {"WAITING", "UNAVAILABLE"}), None)
        applicable = [g for g in gates if g["status"] not in {"NOT_APPLICABLE", "NOT_REQUIRED"}]
        passed = [g for g in applicable if g["status"] == "PASS"]
        quality_score = round((100.0 * len(passed) / len(applicable)), 1) if applicable else 0.0
        authority_tier = (
            "FINAL_SELECTED" if action in {"BUY", "SELL", "HOLD"} and first_blocker is None and first_pending is None
            else "REJECTED" if action == "REJECT"
            else "EVIDENCE_READY" if quality_score >= 80 and first_blocker is None
            else "EVIDENCE_BUILDING" if quality_score >= 50
            else "EVIDENCE_PENDING"
        )
        return {
            "state": "CANONICAL_DECISION_READY" if authority_tier == "FINAL_SELECTED" else ("REJECTED" if action == "REJECT" else "EVIDENCE_PENDING"),
            "final_action": action,
            "authority_tier": authority_tier,
            "evidence_quality_score": quality_score,
            "first_hard_blocker": first_blocker,
            "first_pending_gate": first_pending,
            "what_made_eligible": [g["gate"] for g in gates if g["hard_gate"] and g["status"] == "PASS"],
            "what_blocked": [g["gate"] for g in gates if g["hard_gate"] and g["status"] == "FAIL"],
            "what_is_pending": [g["gate"] for g in gates if g["hard_gate"] and g["status"] in {"WAITING", "UNAVAILABLE"}],
            "gates": gates,
        }

    def _quote(self, symbol: str, instrument_key: str) -> Dict[str, Any]:
        """Return only a quote compatible with the canonical instrument identity."""
        expected_key = str(instrument_key or "").strip()

        def accepted(raw: Any) -> Dict[str, Any]:
            row = dict(raw or {})
            if str(row.get("symbol") or symbol).upper() != symbol:
                return {}
            returned_key = str(row.get("instrument_key") or "").strip()
            if expected_key and returned_key and returned_key != expected_key:
                return {}
            # This remains a pure local read.  Re-evaluate freshness at request
            # time so a retained quote can never lose/retain a stale freshness
            # label merely because the source cache omitted/kept one.  Provider
            # receipt time is never promoted to exchange time.
            return revalidate_cached_quote(
                row,
                now=india_now(),
                market_open=is_india_market_open(),
            )

        try:
            rows = self.app.runtime_market_state.latest_quotes([symbol]) or []
            for raw in rows:
                row = accepted(raw)
                if row:
                    return row
        except Exception:
            pass
        with self._quote_lock:
            cached = dict(self._quote_cache.get(expected_key or symbol) or {})
        row = accepted(cached)
        if row:
            return row

        def hydrate_retained_quote() -> None:
            try:
                retained = accepted((self.store.latest_quotes_by_symbol([symbol]) or {}).get(symbol, {}))
            except Exception:
                retained = {}
            if retained:
                with self._quote_lock:
                    self._quote_cache[expected_key or symbol] = dict(retained)

        local_projection_dispatcher_for_app(self.app).submit(
            f"selected-quote:{expected_key or symbol}",
            hydrate_retained_quote,
            priority=PRIORITY_PERSISTENCE,
        )
        return {}

    def _fundamentals(self, instrument: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = self.fundamental_snapshot.peek(instrument)
        evidence = dict(snapshot.get("fundamentals") or {})
        if evidence:
            return {
                **evidence,
                "materialized_snapshot_id": snapshot.get("snapshot_id"),
                "materialized_snapshot_version": snapshot.get("version"),
                "materialized_read_source": snapshot.get("read_source"),
            }
        return {
            "ok": False,
            "state": str(snapshot.get("state") or "WARMING").upper(),
            "source": "materialized_fundamental_snapshot",
            "refreshing": bool(snapshot.get("refreshing")),
            "reason": "Fundamental projection is warming; foreground Stock Report performs no scoring/provider I/O.",
            "materialized_snapshot_version": snapshot.get("version"),
        }

    def _schedule_selected_enrichment(self, instrument: Dict[str, Any], mode: str) -> bool:
        """Debounce enrichment so a 40-symbol selection wave does not fan out.

        Every foreground selection updates one app-level pending record. One
        coalesced low-priority worker waits for 300ms of selection stability and
        then schedules Research/Fundamental materialization only for the latest
        actually selected stock. Mathematical/technical readiness is unaffected.
        """
        symbol = str(instrument.get("trading_symbol") or instrument.get("symbol") or "").upper()
        instrument_key = str(instrument.get("instrument_key") or "")
        if not instrument_key:
            return False
        setattr(self.app, "_selected_enrichment_pending", {
            "instrument": dict(instrument), "mode": str(mode or "delivery").lower(),
            "symbol": symbol, "updated": time.monotonic(),
        })
        dispatcher = local_projection_dispatcher_for_app(self.app)

        def settle_latest() -> None:
            # Keep one coalesced token alive while rapid selections arrive.
            deadline = time.monotonic() + 2.0
            pending = {}
            while time.monotonic() < deadline:
                pending = dict(getattr(self.app, "_selected_enrichment_pending", {}) or {})
                updated = float(pending.get("updated") or 0.0)
                age = max(0.0, time.monotonic() - updated)
                if pending and age >= 0.30:
                    break
                time.sleep(min(0.05, max(0.01, 0.30 - age)))
            latest = dict(getattr(self.app, "_selected_enrichment_pending", {}) or pending or {})
            latest_instrument = dict(latest.get("instrument") or {})
            latest_key = str(latest_instrument.get("instrument_key") or "")
            if not latest_key:
                return
            # Do not let enrichment steal capacity while interactive technical or
            # chart materialization is still converging. A later stock poll will
            # schedule the same coalesced dwell again.
            if dispatcher.high_priority_pending():
                return
            self.fundamental_snapshot.request_projection(latest_instrument)
            # One selected stock has two legitimate trader decisions. Materialize
            # both desk snapshots in the same coalesced background dwell so the
            # side-by-side Stock Report does not leave the unselected desk warming
            # indefinitely. This remains bounded to one selected instrument.
            for desk_mode in ("delivery", "intraday"):
                self.research.read(
                    symbol=str(latest.get("symbol") or ""),
                    instrument_key=latest_key,
                    mode=desk_mode,
                )

        result = dispatcher.submit(
            "selected-enrichment-dwell", settle_latest, priority=PRIORITY_PERSISTENCE + 30
        )
        return bool(result.accepted or result.state == "COALESCED")

    def _structural_key(
        self, instrument_key: str, mode: str, technical: Dict[str, Any],
        *, research_token: tuple[str, str], fundamental_token: str, priority_state: str,
    ) -> tuple[str, str, str, str, str, str, str]:
        research_id, research_freshness = research_token
        return (
            str(instrument_key), str(mode or "delivery").lower(),
            str(technical.get("snapshot_id") or ""), str(research_id or ""),
            str(research_freshness or ""), str(fundamental_token or ""),
            str(priority_state or "IDLE"),
        )

    def _cached_structural(self, key: tuple[str, ...]) -> Dict[str, Any]:
        with self._structural_lock:
            row = self._structural_cache.get(key)
            return row if isinstance(row, dict) else {}

    def _store_structural(self, key: tuple[str, ...], payload: Dict[str, Any]) -> None:
        with self._structural_lock:
            # Complete immutable structural response components are reused across
            # warm clicks. Live quote/performance/snapshot timestamps are overlaid
            # per request below, so no trading identity can be changed by cache.
            if len(self._structural_cache) >= 1024:
                self._structural_cache.clear()
            self._structural_cache[key] = payload

    def read(self, symbol_or_key: str, mode: str = "delivery") -> Dict[str, Any]:
        identity = self.presentation.resolve(symbol_or_key)
        if not identity.ok or not identity.instrument_key:
            return {
                "ok": False,
                "service_version": self.VERSION,
                "state": "IDENTITY_UNAVAILABLE",
                "symbol": identity.symbol,
                "instrument": identity.as_dict(),
                "message": identity.reason,
            }
        instrument = identity.as_dict()
        quote = self._quote(identity.symbol, identity.instrument_key)
        # One materialized technical read model owns MTF/SR for the Stock Report.
        # The separate chart endpoint owns chart rows; neither depends on scanner,
        # controller, Research refresh or provider I/O.
        technical = self.technical.read(instrument)
        dispatcher = local_projection_dispatcher_for_app(self.app)
        high_priority_busy = dispatcher.high_priority_pending()
        # Structural cache identity includes the materialized enrichment generations
        # as cheap in-memory tokens.  A newly materialized Research decision or
        # fundamental snapshot therefore invalidates the warm response immediately
        # even when the technical generation itself has not changed.
        delivery_token = self.research.cache_token(instrument_key=identity.instrument_key, mode="delivery")
        intraday_token = self.research.cache_token(instrument_key=identity.instrument_key, mode="intraday")
        research_token = (
            f"delivery:{delivery_token[0]}|intraday:{intraday_token[0]}",
            f"delivery:{delivery_token[1]}|intraday:{intraday_token[1]}",
        )
        fundamental_token = self.fundamental_snapshot.cache_token(identity.instrument_key)
        structural_key = self._structural_key(
            identity.instrument_key, mode, technical, research_token=research_token,
            fundamental_token=fundamental_token,
            priority_state="BUSY" if high_priority_busy else "IDLE",
        )
        cached_structural = self._cached_structural(structural_key) if technical.get("ok") else {}
        if cached_structural:
            # Only quote-dependent fields are refreshed. The cached object contains
            # no mutable live quote so structural decision/desk/level identity is
            # immutable across requests.
            performance = PricePerformanceService.reprice(
                cached_structural.get("_price_performance_base"),
                current_price=quote.get("ltp"),
                current_as_of=quote.get("provider_timestamp") or quote.get("source_time") or quote.get("timestamp"),
            )
            display_quote = self._display_quote(quote, performance)
            current_price = quote.get("ltp") or quote.get("last_price") or display_quote.get("ltp") or display_quote.get("close")
            level_snapshot = reconcile_level_snapshot(cached_structural.get("_level_snapshot_base"), current_price)
            levels = dict(level_snapshot.get("structural") or {})
            now = datetime.now(timezone.utc).isoformat()
            out = dict(cached_structural.get("payload") or {})
            out["selected_quote"] = quote; out["quote"] = quote; out["display_quote"] = display_quote
            out["level_snapshot"] = level_snapshot
            out["levels_by_timeframe"] = dict(level_snapshot.get("by_timeframe") or {})
            out["market_levels"] = levels; out["levels"] = levels
            out["structural_support"] = levels.get("support"); out["structural_resistance"] = levels.get("resistance")
            out["support"] = levels.get("support"); out["resistance"] = levels.get("resistance")
            out["support_resistance_scope"] = "STRUCTURAL_1D_COMPATIBILITY"
            out["price_performance"] = performance; out["period_returns"] = performance
            out["range_52_week"] = performance.get("range_52_week") or {}
            component_states = dict(out.get("component_states") or {})
            component_states["quote"] = {"state": "READY" if self._quote_has_price(quote) else "UNAVAILABLE", "as_of": quote.get("provider_timestamp") or quote.get("source_time") or quote.get("timestamp")}
            out["component_states"] = component_states
            snap = dict(out.get("selected_stock_snapshot") or {})
            snap["as_of"] = now; snap["component_states"] = component_states
            out["selected_stock_snapshot"] = snap
            analysis = dict(out.get("analysis") or {})
            analysis["quote"] = quote; analysis["price_performance"] = performance; analysis["range_52_week"] = performance.get("range_52_week") or {}; analysis["quality"] = component_states
            out["analysis"] = analysis
            try:
                out["trust"] = self.app.trust_state_service.snapshot()
            except Exception:
                out["trust"] = {"state": "DO_NOT_TRUST", "decision_admission_allowed": False, "reason": "trust projection unavailable"}
            return out
        mtf = list(technical.get("mtf") or [])
        raw_level_snapshot = dict(technical.get("level_snapshot") or {})
        # Current-price reconciliation is projection-only: completed candles own
        # role flips, while the live quote only suppresses a crossed level from
        # being mislabeled as current support/resistance until confirmation.
        levels = dict(technical.get("levels") or {})
        # Foreground is strictly memory-only. High-water comes from the already
        # materialized technical snapshot; there is no synchronous catalogue or
        # PostgreSQL fallback on a stock click.
        chart_high_water = technical.get("daily_high_water")
        chart_storage_high_water = technical.get("storage_high_water")
        # Both desk decisions are retained in memory.  Confirming Delivery and
        # Intraday side-by-side therefore adds no database/provider/workflow fan-out.
        delivery_research = self.research.peek(
            symbol=identity.symbol, instrument_key=identity.instrument_key, mode="delivery"
        )
        intraday_research = self.research.peek(
            symbol=identity.symbol, instrument_key=identity.instrument_key, mode="intraday"
        )
        research_by_desk = {"delivery": delivery_research, "intraday": intraday_research}
        research = research_by_desk.get(str(mode or "delivery").lower(), delivery_research)
        if technical.get("ok") and not high_priority_busy:
            fundamentals = self._fundamentals(instrument)
            self._schedule_selected_enrichment(instrument, mode)
        elif technical.get("ok"):
            fundamentals = {
                "ok": False, "state": "DEFERRED", "source": "deferred_for_interactive_convergence",
                "reason": "Fundamental enrichment waits until technical/chart priority work drains.",
            }
        else:
            fundamentals = {
                "ok": False, "state": "WARMING", "source": "deferred_until_technical_ready",
                "reason": "Fundamental enrichment starts after the technical read model is available.",
            }
        official_nse_evidence = dict(research.get("official_nse_evidence") or {})
        decision = dict(research.get("canonical_decision") or {})
        trade_map = TradeMapProjectionService.project(decision)
        desk_decisions: Dict[str, Any] = {}
        for desk_name, desk_research in research_by_desk.items():
            desk_decision = dict(desk_research.get("canonical_decision") or {})
            desk_decisions[desk_name] = {
                "desk": desk_name,
                "research_state": desk_research.get("state") or ("READY" if desk_decision else "WARMING"),
                "freshness": desk_research.get("freshness"),
                "as_of": desk_research.get("as_of") or desk_research.get("materialized_at"),
                "decision": desk_decision,
                "trade_map": TradeMapProjectionService.project(desk_decision),
            }
        performance = PricePerformanceService.reprice(
            technical.get("price_performance"),
            current_price=quote.get("ltp"),
            current_as_of=quote.get("provider_timestamp") or quote.get("source_time") or quote.get("timestamp"),
        )
        if performance.get("state") != "READY":
            # Cold retained anchors are a background-materialization concern.
            # Foreground Stock Snapshot must never open the Parquet lake merely
            # to synthesize a temporary short-history performance view.
            performance = {
                **dict(performance or {}),
                "authority": "MATERIALIZED_TECHNICAL_SNAPSHOT",
                "state": "WARMING",
                "pricing_state": "MATERIALIZED_ANCHORS_PENDING",
                "horizons": dict((performance or {}).get("horizons") or {}),
                "range_52_week": dict((performance or {}).get("range_52_week") or {}),
                "policy": "Foreground performance is projection-only; historical anchors materialize in background.",
            }
        display_quote = self._display_quote(quote, performance)
        current_price = quote.get("ltp") or quote.get("last_price") or display_quote.get("ltp") or display_quote.get("close")
        level_snapshot = reconcile_level_snapshot(raw_level_snapshot, current_price)
        levels = dict(level_snapshot.get("structural") or levels or {})
        levels_by_timeframe = dict(level_snapshot.get("by_timeframe") or {})
        now = datetime.now(timezone.utc).isoformat()
        component_states = {
            "identity": {"state": "READY", "as_of": now},
            "quote": {"state": "READY" if self._quote_has_price(quote) else "UNAVAILABLE", "as_of": quote.get("provider_timestamp") or quote.get("source_time") or quote.get("timestamp")},
            "chart": {"state": "READY" if chart_high_water else str((technical.get("component_states") or {}).get("chart") or "UNAVAILABLE"), "as_of": chart_high_water},
            "mtf": {"state": "READY" if mtf else "UNAVAILABLE", "as_of": max([str(row.get("last_candle") or row.get("as_of") or "") for row in mtf] or [""]) or None},
            "levels": {"state": "READY" if level_snapshot.get("ok") else "UNAVAILABLE", "as_of": chart_high_water},
            "technical_snapshot": {"state": "READY" if technical.get("ok") else "UNAVAILABLE", "as_of": technical.get("as_of")},
            "fundamentals": {"state": "READY" if fundamentals.get("ok") else str(fundamentals.get("state") or "UNAVAILABLE").upper(), "as_of": fundamentals.get("as_of") or fundamentals.get("effective_date")},
            "research": {"state": research.get("state") or "UNAVAILABLE", "as_of": research.get("as_of")},
            "decision": {"state": "READY" if decision else "UNAVAILABLE", "as_of": decision.get("updated_at") or decision.get("created_at")},
        }
        material = {
            "instrument_key": identity.instrument_key,
            "mode": mode,
            "quote_time": component_states["quote"]["as_of"],
            "chart_time": component_states["chart"]["as_of"],
            "technical_snapshot_id": technical.get("snapshot_id"),
            "mtf": [(row.get("tf") or row.get("timeframe"), row.get("composite_score"), row.get("state")) for row in mtf],
            "decision_id": decision.get("decision_id") or decision.get("signal_id"),
            "research_time": research.get("as_of"),
        }
        snapshot_id = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
        selected_truth = {
            "data_status": "ready" if technical.get("ok") else "partial",
            "identity_verified": True,
            "quote_status": "available" if self._quote_has_price(quote) else "unavailable",
            "display_price_status": "verified_close" if display_quote.get("display_only") else ("quote" if display_quote else "unavailable"),
            "valid_trade_map": bool(trade_map.get("valid")),
            "trade_map_state": trade_map.get("state"),
            "reason": "Independent Clean Core components; unavailable sections do not gate the rest of the report.",
        }
        decision_proof = self._decision_proof(
            instrument=instrument, component_states=component_states, decision=decision,
            trade_map=trade_map, research=research, fundamentals=fundamentals,
            official_nse_evidence=official_nse_evidence,
        )
        try:
            trust = self.app.trust_state_service.snapshot()
        except Exception:
            trust = {"state": "DO_NOT_TRUST", "decision_admission_allowed": False, "reason": "trust projection unavailable"}
        payload = {
            "ok": True,
            "service_version": self.VERSION,
            "state": "READY" if technical.get("ok") else "PARTIAL",
            "symbol": identity.symbol,
            "instrument": instrument,
            "selected_quote": quote,
            "quote": quote,
            "display_quote": display_quote,
            "mtf_trend": mtf,
            "market_levels": levels,
            "levels": levels,
            "level_snapshot": level_snapshot,
            "levels_by_timeframe": levels_by_timeframe,
            "structural_support": levels.get("support"),
            "structural_resistance": levels.get("resistance"),
            # Deprecated compatibility fields are explicitly 1D structural.
            "support_resistance_scope": "STRUCTURAL_1D_COMPATIBILITY",
            "indicator_metrics": dict(technical.get("indicator_metrics") or {}),
            "indicator_authority": technical.get("indicator_authority"),
            "indicator_authority_version": technical.get("indicator_authority_version"),
            "support": levels.get("support"),
            "resistance": levels.get("resistance"),
            "fundamentals": fundamentals,
            "research_snapshot": research,
            "official_nse_evidence": official_nse_evidence,
            "decision": decision,
            "trade_map": trade_map,
            "decision_proof": decision_proof,
            "desk_decisions": desk_decisions,
            "price_performance": performance,
            "period_returns": performance,
            "range_52_week": performance.get("range_52_week") or {},
            "selected_stock_truth": selected_truth,
            "component_states": component_states,
            "trust": trust,
            "selected_stock_snapshot": {
                "snapshot_id": snapshot_id,
                "as_of": now,
                "quality_state": "READY" if technical.get("ok") else "PARTIAL",
                "symbol": identity.symbol,
                "instrument_key": identity.instrument_key,
                "component_states": component_states,
                "chart_high_water": chart_high_water,
                "chart_storage_high_water": chart_storage_high_water,
                "technical_snapshot_id": technical.get("snapshot_id"),
                "technical_snapshot_source": technical.get("source"),
            },
            "analysis": {
                # Compact compatibility projection: authoritative material is
                # already present at the response top level. Do not duplicate the
                # complete technical/research/fundamental payload a second time.
                "instrument": instrument,
                "quote": quote,
                "mtf": mtf,
                "market_levels": levels,
                "technical_snapshot": {
                    "ok": technical.get("ok"), "snapshot_id": technical.get("snapshot_id"),
                    "as_of": technical.get("as_of"), "source": technical.get("source"),
                    "daily_high_water": technical.get("daily_high_water"),
                },
                "indicator_metrics": dict(technical.get("indicator_metrics") or {}),
                "mode": mode,
                "decision": decision,
                "trade_map": trade_map,
                "decision_proof": decision_proof,
                "desk_decisions": desk_decisions,
                "price_performance": performance,
                "range_52_week": performance.get("range_52_week") or {},
                "selected_stock_truth": selected_truth,
                "narrative": "Clean Core memory read model; enrichment remains independent and lazy.",
                "quality": component_states,
                "missing_evidence": [key for key, row in component_states.items() if row.get("state") not in {"READY", "CURRENT"}],
            },
            "message": "Local-first Stock Report snapshot; no scanner/controller/provider dependency on this request.",
        }
        if technical.get("ok"):
            # Store one structural response per exact technical generation. Quote and
            # price-performance are deliberately excluded from the cached authority.
            cached_payload = dict(payload)
            cached_payload["selected_quote"] = {}; cached_payload["quote"] = {}
            cached_payload["price_performance"] = {}; cached_payload["period_returns"] = {}; cached_payload["range_52_week"] = {}
            cached_analysis = dict(cached_payload.get("analysis") or {})
            cached_analysis["quote"] = {}; cached_analysis["price_performance"] = {}; cached_analysis["range_52_week"] = {}
            cached_payload["analysis"] = cached_analysis
            self._store_structural(structural_key, {
                "payload": cached_payload,
                "_price_performance_base": technical.get("price_performance"),
                "_level_snapshot_base": raw_level_snapshot,
            })
        return payload
