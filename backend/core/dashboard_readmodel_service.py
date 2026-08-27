"""
DashboardReadModelService -- Cluster 3 of the v51 LadduRuntime extraction.

Owns the presentation layer that the old LadduRuntime fused into its scan/
decision code: building, caching, curating and projecting the data the
frontend actually renders (dashboard cards, selected-signal table, watch
seeds, heatmap-pending payloads). Nothing outside this file should touch
_cards_cache / _cards_cache_mode / _cards_cache_ts / _cards_refreshing /
_last_cards_error directly -- go through the methods below instead.

This is the cluster responsible for the "bare 12 with no reasoning" and
"disappearing New Entries" bug class: those happened because scoring logic
and card-serialization logic lived in the same class with no contract
between them. This service is the read-model half of that contract; the
decision/scan clusters (not yet extracted) are the write half.

FACADE SEAM (temporary, tracked for removal):
  This service is constructed with `runtime_facade` -- a reference to the
  still-not-fully-decomposed LadduRuntime -- for calls into clusters that
  have not been extracted yet:
    - runtime_facade.mode_intelligence_foundation  (decision engine cluster)
    - runtime_facade.candidate_discovery           (scan orchestration cluster)
    - runtime_facade.potential_candidates          (scan orchestration cluster)
    - runtime_facade._research_candidates          (scan orchestration cluster)
    - runtime_facade.health_light                  (system health cluster)
    - runtime_facade._sector_context_for_row       (reference data cluster)
    - runtime_facade._coverage_snapshot            (scan orchestration cluster)
    - runtime_facade._is_actionable_selected       (scan orchestration cluster)
    - runtime_facade._best_available_watch_seeds   (scan orchestration cluster)
    - runtime_facade.heatmap_snapshot              (scan orchestration cluster)
  (RUNNING was removed from this list in v60.2 -- it is a main.py
  module-level global, never a LadduRuntime attribute, so
  `runtime_facade.RUNNING` always raised AttributeError and crashed
  card_cache_loop on its first iteration. Now passed in directly as
  `running_fn`, the same pattern main.py already uses for every other
  background loop -- not a facade seam, a real constructor dependency.)
  Every one of these should be removed as its owning cluster is extracted --
  at that point this service should take a direct reference to the new
  service instead of reaching through the facade. Do not add new facade
  calls here; if a new cross-cluster need shows up, extract that cluster
  first.

Input/output contract (unchanged from old LadduRuntime methods of the same name):
  - dashboard_cards_data(mode) -> dict
  - dashboard_data(mode) -> dict
  - refresh_cards_cache(mode) -> dict, also updates the cache dashboard_cards_data reads
  - card_cache_loop(sup) -> background loop, run on its own thread same as before

Depends on: Store, and the facade above. Does NOT depend on engines.py,
UpstoxClient, or RateController directly.
"""
from __future__ import annotations

import threading
import time
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict

from storage import INTELLIGENCE_SCAN_SYMBOLS
from core.production_mode_policy import POLICY_VERSION, PRODUCTION_MODES, normalise_mode

STICKY_TTL_SECONDS = 90

DEFAULT_CARDS_CACHE = {
    "cache_state": "cold", "cards": [], "time": None,
}


from models import now_iso
from core.runtime_primitives import is_india_market_open
from reference_catalog import final_heatmap_payload


class DashboardReadModelService:
    def __init__(self, store, status: dict, event, record_error, runtime_facade,
                 app_version: str, running_fn=None):
        self.store = store
        self.status = status
        self.event = event
        self.record_error = record_error
        self.runtime = runtime_facade  # FACADE SEAM -- see module docstring
        self.APP_VERSION = app_version
        # v60.2: RUNNING is a main.py *module-level* global, never an
        # attribute of LadduRuntime. `self.runtime.RUNNING` in
        # card_cache_loop always raised AttributeError, crashing the loop
        # on its first iteration; Supervisor silently restarted it every
        # 60s forever, so the dashboard-cards cache never populated. Fixed
        # the same way every other loop in main.py gets RUNNING: a
        # callable passed in, not an attribute read off the facade.
        self._running_fn = running_fn or (lambda: True)

        # v61.4.1: this used to be ONE cache slot shared by every mode,
        # tagged with whichever mode last wrote it (_cards_cache_mode).
        # The background loop always refreshes mode="all" every 12-45s,
        # while dashboard_cards_data() does a synchronous rebuild-and-
        # overwrite of that SAME slot whenever a request's mode didn't
        # match the tag (e.g. the instant a user clicked a mode tab).
        # Those two writers stomped on each other's data continuously,
        # which is the actual mechanism behind the "cards appearing then
        # disappearing" report: a mode-specific fetch would populate the
        # single slot, then the next background "all" tick (or the next
        # differently-moded request) would overwrite it, and the
        # frontend's structural-diff render (see app_data_actions.js
        # structSignature()) would faithfully re-render to match --
        # flipping the dashboard between two different datasets.
        # Fix: key the cache by mode so "all", "intraday" and "delivery"
        # each own their own slot and never overwrite one another.
        self._cards_cache: Dict[str, Dict[str, Any]] = {}       # mode -> payload
        self._cards_cache_ts: Dict[str, float] = {}             # mode -> last build ts
        self._cards_refreshing: Dict[str, bool] = {}            # mode -> in-flight flag
        self._last_cards_error = None

    def _safe_section(self, name, fn, default):
        # shared utility (33 call sites repo-wide) -- kept as a plain method
        # here rather than duplicated; TODO: promote to a module-level util
        # once all callers are migrated off the LadduRuntime copy.
        try:
            return fn()
        except Exception as exc:
            self.record_error(name, str(exc)[:200])
            return default

    # v65.9.12: card_cache_loop only ever refreshed mode="all". Before v61.4.1,
    # every other mode accidentally rode along on "all"'s warm cache slot (they
    # were all one shared slot back then), so this gap was invisible. Splitting
    # per-mode slots (see the v61.4.1 note above) fixed the data-stomping bug
    # but silently orphaned every mode except "all" from ever being pre-warmed
    # in the background -- Intraday and Delivery both need warm independent
    # slots so an actual HTTP request never pays for a synchronous
    # build inline in dashboard_cards_data(), which is slow (real Upstox calls
    # across the mode's whole symbol universe) and, under contention, can hang
    # the request indefinitely. This is the same root cause reported for
    # Stock Intelligence/fundamentals: any cold, never-pre-warmed cache path
    # blocks the calling request instead of hydrating in the background.
    _OTHER_MODES = ("intraday", "delivery")

    @staticmethod
    def _cards_business_token(payload: Dict[str, Any]) -> str:
        row = dict(payload or {})
        discovery = dict(row.get("candidate_discovery") or {})
        material = {
            "cache_state": row.get("cache_state"),
            "selected": len(row.get("selected") or []),
            "final_signals": len(row.get("final_signals") or []),
            "active_positions": len(row.get("active_positions") or []),
            "decision_list": len(row.get("decision_list") or []),
            "watch_queue": len(row.get("watch_queue") or []),
            "research_candidates": len(row.get("research_candidates") or []),
            "discovery": {key: discovery.get(key) for key in (
                "state", "research_count", "potential_count", "armed_count",
                "qualified_count", "watch_count", "selection_funnel",
            )},
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()[:24]

    def card_cache_loop(self, sup=None):
        """v26: dashboard-cards is served from memory. This background worker may fail or be slow,
        but it never blocks /api/health, /api/dashboard-state, or /api/dashboard-cards.
        v37.0: sup.beat() proves liveness to Supervisor each iteration; an
        unhandled exception now propagates up to Supervisor for restart-with-
        backoff instead of being caught-and-slept here forever.
        v65.9.12: also round-robins through every other trade mode (one per
        tick, alongside "all" every tick) so no mode's cache is ever cold by
        the time a real request for it arrives -- see _OTHER_MODES note above.
        """
        time.sleep(1.0)
        other_idx = 0
        while self._running_fn() and (sup is None or sup.running):
            if sup: sup.beat("card_cache")
            try:
                if sup:
                    with sup.heartbeat_guard("card_cache"):
                        self.refresh_cards_cache("all")
                else:
                    self.refresh_cards_cache("all")
                other_mode = self._OTHER_MODES[other_idx % len(self._OTHER_MODES)]
                other_idx += 1
                try:
                    if sup:
                        with sup.heartbeat_guard("card_cache"):
                            other_payload = self.refresh_cards_cache(other_mode)
                    else:
                        other_payload = self.refresh_cards_cache(other_mode)
                except Exception as exc:
                    other_payload = {}
                    # Don't let one mode's failure take down "all"'s refresh cadence.
                    self.record_error("cards_cache", f"{other_mode}: {str(exc)[:160]}")
                if sup:
                    all_payload = self._cards_cache.get("all") or {}
                    token = f"{self._cards_business_token(all_payload)}:{other_mode}:{self._cards_business_token(other_payload)}"
                    previous = getattr(self, "_last_cards_business_token", None)
                    unchanged = previous == token
                    self._last_cards_business_token = token
                    sup.progress(
                        "card_cache",
                        token=token,
                        stage="projection_refresh",
                        current_item=other_mode,
                        completed_units=2 if other_payload else 1,
                        total_units=2,
                        expected_idle=unchanged,
                        waiting_on="projection unchanged; next governed cache refresh" if unchanged else None,
                    )
                time.sleep(12 if is_india_market_open() else 45)
            except Exception as exc:
                self._last_cards_error = str(exc)[:180]
                self.record_error("cards_cache", self._last_cards_error)
                self._cards_cache["all"] = dict(self._cards_cache.get("all") or DEFAULT_CARDS_CACHE, cache_state="stale_error", cards_error=self._last_cards_error, time=now_iso())
                time.sleep(15)


    def refresh_cards_cache(self, mode: str = "all") -> Dict[str, Any]:
        mode = normalise_mode(mode or "all")
        if mode not in ("all", "intraday", "delivery"):
            raise ValueError(f"unsupported dashboard mode '{mode}'; allowed modes are intraday and delivery")
        if self._cards_refreshing.get(mode):
            return self._cards_cache.get(mode) or dict(DEFAULT_CARDS_CACHE)
        self._cards_refreshing[mode] = True
        started = time.time()
        try:
            data = self._build_dashboard_cards_data(mode)
            data["cache_state"] = "fresh"
            data["cache_mode"] = mode
            data["cache_age_ms"] = 0
            data["build_elapsed_ms"] = int((time.time() - started) * 1000)
            data["payload_policy"] = dict(data.get("payload_policy") or {}, version=self.APP_VERSION, served_from="memory_cache")
            self._cards_cache[mode] = data
            self._cards_cache_ts[mode] = time.time()
            self._last_cards_error = None
            return data
        except Exception as exc:
            self._last_cards_error = str(exc)[:180]
            self.record_error("cards_cache", self._last_cards_error)
            stale = dict(self._cards_cache.get(mode) or DEFAULT_CARDS_CACHE, cache_state="stale_error", cards_error=self._last_cards_error, time=now_iso())
            self._cards_cache[mode] = stale
            return stale
        finally:
            self._cards_refreshing[mode] = False


    def _curate_dashboard_decisions(self, rows: list[Dict[str, Any]], limit: int = 80) -> list[Dict[str, Any]]:
        """Prevent A-list/random broad-scan rows from dominating the customer desk."""
        keep = self._priority_symbol_set()
        curated = []
        seen = set()
        for d in rows:
            sym = str(d.get("symbol") or "").upper()
            mode = str(d.get("mode") or "").lower()
            score = int(d.get("score") or 0)
            if mode not in PRODUCTION_MODES:
                continue
            decision = str(d.get("decision") or "").upper()
            status = str(d.get("status") or "").upper()
            manual_or_core = sym in keep
            if not sym or (sym, mode) in seen:
                continue
            # Always keep searched/manual/core rows; otherwise require real quality, not low-score alphabetic filler.
            if not manual_or_core and score < 70:
                continue
            if mode == "delivery" and str(d.get("fundamental_state") or "").lower() in ("missing", "pending", "incomplete", "source_unavailable", "identity_missing") and not manual_or_core:
                # v61.7: don't silently drop -- surface as a non-actionable research-queue
                # row so the desk shows *why* it isn't a selected candidate instead of the
                # row vanishing entirely. See bug12.txt / fundamental.txt findings.
                d = dict(d)
                d["fundamental_state"] = "fundamental_pending"
                d["decision"] = "WATCH"
                d["status"] = "WATCH"
                d["candidate_stage"] = "Research Queue"
                d["qualification_blocker"] = "fundamentals_pending"
                d["reason"] = (str(d.get("reason") or "").rstrip(". ") + "; waiting for fundamental intelligence before selection.").lstrip("; ")
            if mode == "delivery" and str(d.get("side") or "").upper() == "SHORT":
                d = self._sanitize_mode_action(d)
            seen.add((sym, mode)); curated.append(d)
        curated.sort(key=lambda x: (0 if str(x.get("symbol") or "").upper() in self.store.priority_symbols_set() else 1, -(int(x.get("score") or 0)), str(x.get("symbol") or "")))
        return curated[:limit]


    def _curate_selected_signals(self, rows: list[Dict[str, Any]], limit: int = 30) -> list[Dict[str, Any]]:
        keep = self._priority_symbol_set()
        out = []
        seen = set()
        for d in rows:
            sym = str(d.get("symbol") or "").upper(); mode = str(d.get("mode") or "").lower(); score = int(d.get("score") or 0)
            identity = str(d.get("signal_id") or "").strip() or (
                sym,
                mode,
                str(d.get("opened_at") or d.get("triggered_at") or ""),
            )
            if not sym or identity in seen or mode not in PRODUCTION_MODES:
                continue
            signal_status = str(d.get("signal_status") or d.get("status") or "").upper()
            is_closed = signal_status in ("SUCCESS", "FAIL") or str(d.get("status") or "").upper() in ("SIGNAL_SUCCESS", "SIGNAL_FAIL")
            if is_closed:
                # v36.3.2: closed trades belong to Day Performance + Trade Journal, not the live selected card.
                continue
            if signal_status in ("EXPIRED", "AMBIGUOUS", "CANCELLED", "SIGNAL_EXPIRED", "SIGNAL_AMBIGUOUS"):
                continue
            is_open_position = signal_status in ("OPEN", "SIGNAL_OPEN")
            # OPEN ledger rows are lifecycle records. They must remain visible
            # until settlement/expiry/cancellation, even if today's scanner score
            # falls or the fresh-entry gate no longer qualifies the stock.
            if not is_open_position:
                if sym not in keep and score < 84:
                    continue
                if not self.runtime._is_actionable_selected(d):
                    continue
            d["card_state"] = "OPEN"
            if mode == "delivery":
                d["selected_lifecycle"] = "persistent_until_success_fail_exit_or_invalidation"
                d["candidate_stage"] = d.get("candidate_stage") or "Persistent Selected"
            else:
                d["selected_lifecycle"] = "same_session_only"
            seen.add(identity); out.append(d)
        out.sort(key=lambda x: (
            1 if x.get("card_state") == "EXIT" else 0,
            0 if str(x.get("symbol") or "").upper() in keep else 1,
            -(int(x.get("score") or 0)),
            str(x.get("symbol") or "")
        ))
        return out[:limit]


    def _card_project(self, d: Dict[str, Any]) -> Dict[str, Any]:
        signal_status = str((d or {}).get("signal_status") or (d or {}).get("status") or "").upper()
        is_open_lifecycle = signal_status in ("OPEN", "SIGNAL_OPEN")
        # Fresh carry recommendations remain long-only and may be sanitized from
        # SHORT to AVOID_LONG. An already-open ledger position is immutable
        # lifecycle history: preserve its opened side and risk map until closure.
        projected = dict(d or {}) if is_open_lifecycle else self._sanitize_mode_action(d)
        d = self._apply_card_fundamentals(projected)
        keys = ["symbol","exchange","mode","side","decision","status","ltp","open","high","low","close","previous_close","rupee_change","day_change_abs","point_change","change_pct","entry","t1","t2","sl","rr","rr_live","room_to_t1","room_to_t2","risk_to_sl","score",
                "fundamental_score","technical_score","fundamental_weight_pct","fundamental_state","quality_score","valuation_score","fundamental_source","fundamental_note",
                "market_structure","volume_profile","orb_state","rsi","adx","volume_state","support","resistance","support_kind","resistance_kind",
                "risk","reason","price_freshness","last_ai_validation","last_refresh","setup","result","signal_status","signal_id","opened_at","triggered_at","last_update","closed_at","pnl_points","mfe","mae","mfe_r","target_stage","stage_remarks","lifecycle_state","lifecycle_reason","managed_sl","original_sl","secured_fraction","secured_price","breakeven_price","obstacle_touched","obstacle_touch_price","mfe_retrace_fraction","add_allowed","fomo_guard","reentry_policy","validation_source","validation_policy","proof_ts","watch_type","quantity","gross_pnl","net_pnl","total_cost","managed_stop","management_action","mtm_pnl_points","mtm_pnl","mtm_qty","mtm_status","position_state","active_stop","stop_state","position_age_days",
                "waiting_for","trigger","invalidation","target_window","max_holding_period","thesis_expiry","review_cadence","time_window_reason","trade_budget_note","entry_cutoff","candidate_stage","opportunity_bucket","discovery_buckets","discovery_evidence","candidate_model","sector","sector_key","sector_label","sector_index","sector_status","sector_change_pct","sector_freshness","sector_reason","themes","coverage_bucket","institutional_delta","institutional_score","institutional_stage","institutional_model_version","ema_compression_pct","recent_volume_vs_base","range_contraction_pct","range_compression_rule","range_compression_rule_id","range_compression_score","range_compression_qualified","opportunity_stage","priority_score","priority_reason","source_engine_score","evidence_score","evidence_model_id","rank_score","rank_readiness","rank_components","rank_conflicts","rank_raw_score","rank_effective_max_score","rank_normalized_score","rank_scoring_state","rank_degraded_components","rank_missing_inputs","rank_gate_failures","rank_veto_reasons","ranking_version","ranking_explanation","ranking_trace_id","research_factor_state","research_factor_points","factor_authority","model_score","model_confidence","model_calibrated_score","model_state","model_id","model_channel","model_ranking_weight","model_ranking_authority_pct","model_influence_applied","model_rank_contribution","model_ranking_stage","model_ranking_contract","risk_admission_state","risk_quantity","risk_notional","risk_cash","risk_authority","calibrated_edge","execution_quality","event_risk_policy","performance_drift_guard","governed_edge_gates","counterfactual_observation","selection_validation_observation","final_promotion_invariants","promotion_blocked_by","qualification_blocker","rejection_reasons","generated_at","finalized_at","observed_at","research_only","execution_quote_required","execution_price_authority","analysis_price_authority","quote_as_of","current_price","last_seen_at","next_scan_at","pinned","pnl","exit","planned_entry","planned_sl","planned_t1","planned_t2","planned_rr","planned_map_valid","prepared_state","signal_status","signal_outcome","economic_outcome","result","opened_at","closed_at","exit_reason","net_pnl","gross_pnl","total_cost","management_action","current_action","lifecycle_state","lifecycle_reason","position_state","thesis_state","reassessment_state","thesis_reassessment"]
        out = {k: d.get(k) for k in keys if k in d}
        out.update(self.runtime._sector_context_for_row(out))
        if "reason" in out:
            out["reason"] = self._trim_reason(out.get("reason"), 190)
        if "risk" in out:
            out["risk"] = self._trim_reason(out.get("risk"), 120)
        self._apply_mtm(out)
        return out

    def _apply_mtm(self, out, default_qty=None):
        """Projection-only compatibility hook.

        Older dashboard code calculated mark-to-market, assumed quantity=100,
        inferred stop state and emitted trade-management actions inside the read
        model.  Those are lifecycle/risk authorities and may not be recreated by
        a browser projection.  Canonical fields are copied by ``_card_project``;
        missing economics remain explicitly unavailable.
        """
        status = str(out.get("signal_status") or out.get("status") or "").upper()
        if status not in ("OPEN", "SIGNAL_OPEN"):
            return out
        # Preserve canonical aliases only; never invent quantity or management.
        if out.get("managed_stop") is None and out.get("managed_sl") is not None:
            out["managed_stop"] = out.get("managed_sl")
        if out.get("quantity") is not None and out.get("mtm_qty") is None:
            out["mtm_qty"] = out.get("quantity")
        out.setdefault("read_model_economics_state", "CANONICAL" if out.get("quantity") is not None else "UNAVAILABLE")
        out.setdefault("read_model_policy", "projection_only_no_mtm_or_trade_management_math")
        return out



    def _compact_card_project(self, d: Dict[str, Any]) -> Dict[str, Any]:
        # Dashboard compact row: do not ship full case evidence here.
        p = self._card_project(d)
        keep = ["symbol","exchange","mode","side","decision","status","ltp","current_price","previous_close","rupee_change","day_change_abs","point_change","change_pct","score","fundamental_score","technical_score","fundamental_weight_pct","fundamental_state","support","resistance","setup","reason","waiting_for","trigger","invalidation","target_window","candidate_stage","opportunity_stage","opportunity_bucket","sector","sector_label","sector_status","sector_change_pct","sector_reason","priority_score","price_freshness","evidence_score","rank_score","rank_readiness","rank_components","rank_conflicts","rank_scoring_state","rank_missing_inputs","rank_gate_failures","rank_veto_reasons","ranking_explanation","ranking_trace_id","promotion_blocked_by","qualification_blocker","rejection_reasons","model_score","model_confidence","model_state","model_id","model_ranking_authority_pct","model_influence_applied","model_rank_contribution","model_ranking_stage","research_factor_state","research_factor_points","generated_at","finalized_at","observed_at","research_only","execution_quote_required","execution_price_authority","analysis_price_authority","quote_as_of","planned_entry","planned_sl","planned_t1","planned_t2","planned_rr","planned_map_valid","prepared_state"]
        out = {k: p.get(k) for k in keep if k in p}
        if "setup" in out:
            out["setup"] = self._trim_reason(out.get("setup"), 64)
        if "opportunity_bucket" in out:
            out["opportunity_bucket"] = self._trim_reason(out.get("opportunity_bucket"), 72)
        return out



    def dashboard_cards_data(self, mode: str = "all") -> Dict[str, Any]:
        """Return cards from cache, but never return an unusable empty shell if the cache has not hydrated yet.

        v61.4.1: reads/writes self._cards_cache[mode] -- its own slot --
        instead of a single shared slot tagged with the last-seen mode.
        A request for one mode can no longer overwrite what the background
        loop (or a request for a different mode) just built.

        v65.9.12: a cold/never-warmed mode used to run _build_dashboard_cards_data(mode)
        SYNCHRONOUSLY, inline, inside this call -- i.e. inside the HTTP request
        thread. That's a real Upstox fetch chain across the mode's whole symbol
        universe with no timeout, so the very first request for a mode the
        card_cache_loop hadn't gotten to yet could hang for a long time (this is
        the confirmed cause of `/api/dashboard-cards?mode=intraday` hanging).
        Now a cold cache spawns the SAME guarded build (refresh_cards_cache,
        which already has the _cards_refreshing in-flight guard so concurrent
        cold requests for the same mode don't pile up duplicate builds) on a
        background thread and returns immediately with a "starting" shell --
        the request never blocks. card_cache_loop pre-warming every mode (see
        _OTHER_MODES) means this cold path should now be rare in practice.
        """
        mode = mode or "all"
        cache = dict(self._cards_cache.get(mode) or {})
        has_built_cache = mode in self._cards_cache_ts
        needs_refresh = str(cache.get("cache_state") or "") in ("stale_quote_update", "stale_error")
        if not has_built_cache or needs_refresh:
            if not self._cards_refreshing.get(mode):
                threading.Thread(target=self.refresh_cards_cache, args=(mode,), name=f"cards-cold-{mode}", daemon=True).start()
            # Read again rather than writing our own "starting" shell into
            # self._cards_cache: the background thread may have already
            # finished (it can be near-instant, e.g. in tests) and written the
            # real result by the time we get here -- overwriting the slot
            # unconditionally would clobber a completed build with a stale
            # "starting" placeholder.
            cache = dict(self._cards_cache.get(mode) or DEFAULT_CARDS_CACHE, cache_state=(self._cards_cache.get(mode) or {}).get("cache_state") or "starting")
        cache = dict(cache or {})
        cache["cache_state"] = cache.get("cache_state") or "empty"
        ts = self._cards_cache_ts.get(mode)
        cache["cache_age_ms"] = int((time.time() - ts) * 1000) if ts else None
        cache["cache_mode"] = mode
        cache["cards_error"] = self._last_cards_error
        cache["time"] = now_iso()
        return cache


    @staticmethod
    def _final_identity_tokens(row: Dict[str, Any]) -> set[str]:
        """Exact immutable IDs that are allowed to join Final surfaces.

        Symbol/time proximity is deliberately excluded.  A Model Paper position
        belongs to a canonical decision only when one of its persisted decision /
        source-signal identifiers exactly matches the canonical decision/signal ID.
        """
        tokens: set[str] = set()
        decision_id = str(row.get("decision_id") or "").strip()
        if decision_id:
            tokens.add(f"decision:{decision_id}")
        for key in ("signal_id", "source_signal_id"):
            signal_id = str(row.get(key) or "").strip()
            if signal_id:
                tokens.add(f"signal:{signal_id}")
        return tokens

    @staticmethod
    def _final_signal_age_seconds(row: Dict[str, Any]) -> int | None:
        raw = row.get("generated_at") or row.get("decision_generated_at") or row.get("created_at")
        if not raw:
            return None
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0, int((datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()))
        except Exception:
            return None

    @staticmethod
    def _final_position_age_seconds(row: Dict[str, Any]) -> int | None:
        raw = row.get("opened_at")
        if not raw:
            return None
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0, int((datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()))
        except Exception:
            return None

    @staticmethod
    def _final_holding_period(row: Dict[str, Any]) -> str | None:
        """Return only a strategy-declared horizon; never invent a Delivery default."""
        for key in ("holding_period", "target_window", "horizon", "expected_horizon"):
            value = str(row.get(key) or "").strip()
            if value:
                return value
        return None

    def _final_signal_rows(self, mode: str = "all") -> list[Dict[str, Any]]:
        """Build the single authoritative Final Signals projection.

        Canonical decisions own signal identity and frozen trade geometry.  Open
        PostgreSQL Model Paper positions own fill/position lifecycle and economics.
        These two authorities may join by exact persisted ID only.  The legacy
        selected-signal projection is intentionally not an authority here.
        """
        requested = str(mode or "all").lower()
        repo = getattr(self.store, "production_canonical_decision_repository", None)
        active_getter = getattr(repo, "active_decisions", None)
        canonical_rows = self._safe_section(
            "final_signal_canonical_active",
            lambda: list(active_getter(requested, limit=120) or []) if callable(active_getter) else [],
            [],
        )
        portfolio = getattr(self.runtime, "model_portfolio", None)
        position_getter = getattr(portfolio, "open_positions", None)
        open_positions = self._safe_section(
            "final_signal_model_paper_open",
            lambda: list(position_getter() or []) if callable(position_getter) else [],
            [],
        )
        if requested in PRODUCTION_MODES:
            open_positions = [row for row in open_positions if str(row.get("mode") or "").lower() == requested]

        # Index positions by every immutable persisted linkage token.
        positions_by_token: Dict[str, list[Dict[str, Any]]] = {}
        for raw in open_positions:
            position = dict(raw or {})
            for token in self._final_identity_tokens(position):
                positions_by_token.setdefault(token, []).append(position)

        final_rows: list[Dict[str, Any]] = []
        matched_position_ids: set[str] = set()
        seen_decisions: set[str] = set()
        for raw in canonical_rows:
            canonical = dict(raw or {})
            desk = str(canonical.get("mode") or "").lower()
            if desk not in PRODUCTION_MODES:
                continue
            decision_id = str(canonical.get("decision_id") or "").strip()
            signal_id = str(canonical.get("signal_id") or "").strip()
            if not decision_id or decision_id in seen_decisions:
                continue
            seen_decisions.add(decision_id)
            candidates: list[Dict[str, Any]] = []
            for token in self._final_identity_tokens(canonical):
                candidates.extend(positions_by_token.get(token) or [])
            # Deduplicate exact-position candidates; newest persisted position wins
            # only when duplicate IDs exist due to historical migration evidence.
            unique = {}
            for position in candidates:
                pid = str(position.get("position_id") or "").strip()
                if pid:
                    unique[pid] = position
            position = sorted(
                unique.values(),
                key=lambda row: str(row.get("updated_at") or row.get("opened_at") or ""),
                reverse=True,
            )[0] if unique else None

            row = dict(canonical)
            row.update({
                "decision_id": decision_id,
                "signal_id": signal_id or canonical.get("signal_id"),
                "final_signal_authority": "POSTGRESQL_CANONICAL_DECISION",
                "final_signal_join_state": "CANONICAL_SIGNAL_ONLY",
                "research_only": False,
                "entry": canonical.get("entry"),
                "target": canonical.get("t1") if canonical.get("t1") is not None else canonical.get("target"),
                "stop": canonical.get("sl") if canonical.get("sl") is not None else canonical.get("stop"),
                "original_stop": canonical.get("sl") if canonical.get("sl") is not None else canonical.get("stop"),
                "active_stop": canonical.get("sl") if canonical.get("sl") is not None else canonical.get("stop"),
                "holding_period": self._final_holding_period(canonical),
                "signal_age_seconds": self._final_signal_age_seconds(canonical),
                "position_age_seconds": None,
                "position_opened_at": None,
                "display_stage": "FINAL",
                "status": "FINAL",
                "active": True,
                "hit_status": canonical.get("hit_status") or "NONE",
                "management_action": canonical.get("management_action") or canonical.get("current_action") or canonical.get("decision") or "AWAIT ENTRY",
            })
            if position is not None:
                pid = str(position.get("position_id") or "").strip()
                if pid:
                    matched_position_ids.add(pid)
                entry = position.get("entry_price") if position.get("entry_price") is not None else position.get("original_entry")
                target = position.get("original_target") if position.get("original_target") is not None else row.get("target")
                original_stop = position.get("original_stop") if position.get("original_stop") is not None else row.get("original_stop")
                active_stop = position.get("managed_stop") if position.get("managed_stop") is not None else original_stop
                row.update({
                    "position_id": position.get("position_id"),
                    "final_signal_authority": "POSTGRESQL_CANONICAL_DECISION+MODEL_PAPER_POSITION",
                    "final_signal_join_state": "EXACT_POSITION_LINKED",
                    "entry": entry,
                    "target": target,
                    "stop": active_stop,
                    "original_stop": original_stop,
                    "active_stop": active_stop,
                    "quantity": position.get("quantity"),
                    "net_pnl": position.get("net_pnl"),
                    "gross_pnl": position.get("gross_pnl"),
                    "total_cost": position.get("total_cost"),
                    "hit_status": position.get("hit_status") or "MONITORING",
                    "management_action": position.get("action") or "HOLD / MONITOR",
                    "opened_at": position.get("opened_at"),
                    "position_opened_at": position.get("opened_at"),
                    "position_age_seconds": self._final_position_age_seconds(position),
                    "last_price": position.get("last_price"),
                    "display_stage": "OPEN",
                    "status": "OPEN",
                    "active": True,
                })
            final_rows.append(row)

        # Risk-critical fail-safe: an open Model Paper position must remain visible
        # even if canonical lineage is damaged.  It is explicitly labelled as a
        # reconciliation fault and never silently masquerades as a clean Final row.
        for raw in open_positions:
            position = dict(raw or {})
            pid = str(position.get("position_id") or "").strip()
            if pid and pid in matched_position_ids:
                continue
            desk = str(position.get("mode") or "").lower()
            if desk not in PRODUCTION_MODES:
                continue
            final_rows.append({
                **position,
                "decision_id": position.get("decision_id") or position.get("source_signal_id"),
                "signal_id": position.get("source_signal_id"),
                "final_signal_authority": "POSTGRESQL_MODEL_PAPER_OPEN_POSITION",
                "final_signal_join_state": "ORPHAN_OPEN_POSITION_RECONCILIATION_REQUIRED",
                "reconciliation_required": True,
                "research_only": False,
                "entry": position.get("entry_price") if position.get("entry_price") is not None else position.get("original_entry"),
                "target": position.get("original_target"),
                "stop": position.get("managed_stop") if position.get("managed_stop") is not None else position.get("original_stop"),
                "original_stop": position.get("original_stop"),
                "active_stop": position.get("managed_stop") if position.get("managed_stop") is not None else position.get("original_stop"),
                "holding_period": self._final_holding_period(position),
                "signal_age_seconds": self._final_signal_age_seconds(position),
                "position_age_seconds": self._final_position_age_seconds(position),
                "position_opened_at": position.get("opened_at"),
                "display_stage": "RECONCILIATION_REQUIRED",
                "status": "RECONCILIATION_REQUIRED",
                "active": True,
                "management_action": "RECONCILE LINEAGE",
            })

        final_rows.sort(key=lambda row: (
            0 if str(row.get("display_stage") or "").upper() == "OPEN" else 1,
            -float(row.get("rank_score") or row.get("evidence_score") or row.get("score") or 0.0),
            str(row.get("symbol") or ""),
        ))
        return final_rows[:80]

    def _build_dashboard_cards_data(self, mode: str = "all") -> Dict[str, Any]:
        """v26.2: background-only card payload builder. Dashboard gets compact rows; full cases stay in detail endpoints."""
        # v103 read-model boundary: dashboard construction is a pure read.
        # It must never settle/expire/cancel a decision merely because a browser
        # card was refreshed. DeskPositionLifecycleAuthority is the sole Model
        # Paper risk/settlement owner; policy cleanup belongs to governed startup
        # migrations/reconciliation, never an HTTP/read-model path.
        raw_decisions = [self._sanitize_mode_action(d) for d in self._safe_section("decision_list", lambda: self.store.latest_decisions(mode, limit=240), [])]
        decisions = self._curate_dashboard_decisions(raw_decisions, limit=10)
        # R50: Final/active customer surfaces no longer read the legacy selected-
        # signal projection.  Final Signals are canonical-decision rows optionally
        # joined to exact PostgreSQL Model Paper positions; active_positions are
        # therefore the OPEN subset of the same authority projection.
        final_signals = self._final_signal_rows(mode)
        active_positions = [dict(row) for row in final_signals if str(row.get("display_stage") or row.get("status") or "").upper() in {"OPEN", "RECONCILIATION_REQUIRED"}][:20]
        fresh_ranked = [d for d in raw_decisions if str(d.get("rank_readiness") or "").upper() == "READY" and str(d.get("status") or "").upper() in ("PROMOTED", "SIGNAL_OPEN")]
        fresh_ranked.sort(key=lambda d: (-int(d.get("rank_score") or d.get("score") or 0), str(d.get("symbol") or "")))
        promoted = fresh_ranked[:8]
        manual_watch = [self._sanitize_mode_action(d) for d in self._safe_section("manual_watch", lambda: self.store.manual_watch_rows(mode, limit=6), [])]
        auto_watch = []
        for d in decisions:
            try:
                if self._is_curated_watch(d):
                    auto_watch.append(self._watch_projection(d))
            except Exception:
                continue
        research_candidates = self._safe_section("research_candidates", lambda: self.runtime._research_candidates(mode, limit=10), [])
        selected_floor_source = list(promoted) + list(decisions) + list(research_candidates) + list(manual_watch)
        # v51: don't hand the raw per-cycle top-N slice straight to the
        # dashboard — a symbol outranked by one point used to vanish
        # entirely on the next poll. Merge through the sticky ledger so an
        # entry stays visible (flagged _sticky_stale) for STICKY_TTL_SECONDS
        # after it drops out of `promoted`, instead of disappearing instantly.
        try:
            active_keys = {
                f"{str(a.get('symbol') or '').upper().strip()}|{str(a.get('mode') or '').lower().strip()}"
                for a in active_positions
            }
            selected_for_dashboard = self.store.sticky_selected_merge(
                promoted, ttl_seconds=STICKY_TTL_SECONDS, dismiss_keys=active_keys
            )
        except Exception as exc:
            self.record_error("sticky_selected_merge", str(exc))
            selected_for_dashboard = promoted
        selected_memory = []
        if selected_for_dashboard:
            try:
                memory_payload = [self._card_project(d) for d in selected_for_dashboard[:12]]
                saved_at = now_iso()
                for r in memory_payload:
                    r["memory_state"] = "live_selected_snapshot"
                    r["memory_saved_at"] = saved_at
                self.store.set_kv("selected_memory:last", memory_payload)
                selected_memory = memory_payload
            except Exception as exc:
                self.record_error("selected_memory_save", str(exc))
        else:
            try:
                selected_memory = self.store.get_kv("selected_memory:last", []) or []
                for r in selected_memory:
                    r["memory_state"] = "reference_only"
                    r["status"] = r.get("status") or "MEMORY"
                    r["signal_status"] = "REFERENCE_ONLY"
                    r["price_freshness"] = "reference only"
                    r["reason"] = "Last selected snapshot retained for continuity; not actionable unless refreshed and promoted again."
            except Exception as exc:
                self.record_error("selected_memory_load", str(exc))
                selected_memory = []
        for d in research_candidates:
            try:
                auto_watch.append(self._watch_projection(d))
            except Exception:
                continue
        seen = set(); watch = []
        for d in manual_watch + auto_watch:
            key = (d.get("symbol"), d.get("mode"))
            if key in seen:
                continue
            seen.add(key); watch.append(d)
        # Supplement sparse Watch/Potential sections with the governed fair-
        # analysis queue.  These rows remain UNDER_REVIEW and never enter
        # Today's Entries unless the production Evidence Engine later promotes
        # a fresh canonical READY decision.
        present_modes = {str(x.get("mode") or "").lower() for x in list(watch) + list(decisions) + list(research_candidates)}
        for seed in self.runtime._best_available_watch_seeds(mode):
            m = str(seed.get("mode") or "").lower()
            key = (seed.get("symbol"), m)
            if key in seen:
                continue
            watch.append(seed); seen.add(key); present_modes.add(m)
            if len(watch) >= 8:
                break
        discovery = self.runtime.candidate_discovery(mode)
        return {
            "selected": [self._card_project(d) for d in selected_for_dashboard],
            "final_signals": [dict(d) for d in final_signals[:80]],
            "active_positions": [dict(d) for d in active_positions[:20]],
            "selected_memory": selected_memory[:12],
            "selected_policy": {"strict": True, "message": "Final Signals is sourced only from canonical PostgreSQL decisions plus exact-ID Model Paper positions. Research/selected-memory rows cannot enter it."},
            "decision_list": [self._compact_card_project(d) for d in decisions],
            "watch_queue": [self._compact_card_project(d) for d in watch[:8]],
            "research_candidates": [self._compact_card_project(d) for d in research_candidates[:5]],
            "potential_candidates": self.runtime.potential_candidates(mode, limit=5, compact=True),
            "sector_cycle": {"state":"removed", "message":"Standalone sector board removed; sector proof appears only on stock-specific rows/details when live mapping is reliable."},
            "daily_learning": {"state":"compact", "endpoint":"/api/daily-learning"},
            "candidate_discovery": {k: discovery.get(k) for k in ("state","mode","coverage","fairness","research_count","potential_count","armed_count","qualified_count","watch_count","by_stage","by_sector","by_theme","institutional_stages","top_blockers","selection_funnel","near_qualified","funnel_rows","shadow_selection","message")},
            "model_governance": {
                "production_model": "governed_evidence_plus_approved_ai",
                "ranking_version": "production-ranker-2.0.0",
                "weights": {"institutional":30,"technical":25,"participation":20,"tradeability":15,"market_regime":10},
                "institutional_detail": {"delivery_model":22,"lifecycle_stage":6,"reported_fii_mf_dii_context":2},
                "oi_role": "supporting technical evidence only when actual current/previous OI exists",
                "vibe_qlib_production_weight": "active for a healthy governed champion; effective weight is reversible and capped at 15%",
                "vibe_qlib_state": "governed shadow-to-production pipeline; popularity and runtime installation never count as evidence",
                "governance_endpoint": "/api/ai/governance"
            },
            # v32.2: dashboard_data() (the fast shell) hard-codes market_intelligence.state to
            # "shell-ready" and nothing downstream ever overwrote it, so the Stock Intelligence
            # panel's "Research state" showed "shell-ready" forever regardless of real scan
            # progress. Cards is the layer that actually knows the discovery funnel, so it now
            # reports real state here once it has loaded.
            "market_intelligence": {
                "state": "hydrated",
                "message": f"{discovery.get('research_count', 0)} research · {discovery.get('potential_count', 0)} potential · {discovery.get('armed_count', 0)} armed · {discovery.get('qualified_count', 0)} qualified. Open/click a stock for full Stock Intelligence."
            },
            "daily_performance": self._safe_section("daily_performance", lambda: self.store.daily_performance("2000-01-01", now_iso()[:10]), []),
            "mode_performance_alltime": self._safe_section("mode_performance_alltime", self.store.mode_performance_alltime, []),
            "trade_journal": [self._compact_card_project(j) for j in self._safe_section("trade_journal", lambda: self.store.trade_journal(limit=20), [])],
            "payload_policy": {"version":self.APP_VERSION, "detail":"v36.9.4 chart/journal trust fix: suppress off-session fake S/R in intraday chart, compact selected-style journal dashboard", "decision_limit":10, "watch_limit":6, "payload_target":"<45KB"},
            "time": now_iso(),
        }


    def dashboard_data(self, mode: str = "all") -> Dict[str, Any]:
        """v22: shell-only dashboard endpoint. Must return fast even if DB/API/scanner is slow."""
        heat_payload = self._safe_section("final_heatmap", lambda: final_heatmap_payload(self.runtime), {"items": self._pending_heatmap("section failed")})
        heat = heat_payload.get("items") if isinstance(heat_payload, dict) else heat_payload
        # v60.8: health_light/_coverage_snapshot/mode_intelligence_foundation
        # were the only 3 facade calls in this method NOT wrapped in
        # _safe_section, despite the method's own docstring promising it
        # returns fast even under DB/API/scanner trouble. An exception from
        # any of these three would have broken the whole shell response
        # instead of degrading gracefully like every other section here.
        coverage_val = self._safe_section("coverage_snapshot", lambda: self.status.get("deep_scan", {}).get("coverage") or self.runtime._coverage_snapshot(), None)
        return {
            "health": self._safe_section("health_light", self.runtime.health_light, {"state": "unknown", "message": "health check failed"}),
            "selected": [],
            "decision_list": [],
            "watch_queue": [],
            "daily_performance": [],
            "trade_journal": [],
            "scanner_events": [],
            "heatmap": heat,
            "cards_async": True,
            "cards_endpoint": "/api/dashboard-cards",
            "market_intelligence": {
                "state":"shell-ready",
                "message":"Dashboard shell loaded. Candidate, journal, discovery and scanner details hydrate asynchronously. Open/click a stock for full Stock Intelligence."
            },
            "candidate_discovery": {"state":"cards_async", "coverage": coverage_val, "message":"Discovery hydrates from /api/dashboard-cards"},
            "mode_intelligence_foundation": self._safe_section("mode_intelligence_foundation", self.runtime.mode_intelligence_foundation, {}),
            "time": now_iso(),
            "message": f"{self.APP_VERSION}: shell-only; selected lifecycle is mode-aware; open trades + 7D journal hydrate asynchronously; chart defaults to local Upstox candles.",
        }


    def _pending_heatmap(self, reason: str = "pending"):
        names = ["NIFTY","SENSEX","BANK","MIDCAP","SMALLCAP","IT","PHARMA","AUTO","METAL","FMCG","PSUBANK"]
        return [{"name": n, "state": "pending", "change_pct": None, "last_refresh": self.status.get("last_price_refresh"), "reason": reason} for n in names]


    def _watch_projection(self, d: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(d)
        side = str(d.get("side") or "WAIT").upper()
        support = d.get("support")
        resistance = d.get("resistance")
        if side == "LONG":
            out["waiting_for"] = "breakout/reclaim confirmation"
            out["trigger"] = resistance or d.get("entry") or "above setup level"
            out["invalidation"] = support or d.get("sl") or "below support"
        elif side == "SHORT":
            out["waiting_for"] = "breakdown/retest confirmation"
            out["trigger"] = support or d.get("entry") or "below setup level"
            out["invalidation"] = resistance or d.get("sl") or "above resistance"
        else:
            out["waiting_for"] = "clear directional trigger"
            out["trigger"] = d.get("entry") or "pending"
            out["invalidation"] = d.get("sl") or "pending"
        return out


    def _priority_symbol_set(self) -> set:
        return {s.upper() for s in INTELLIGENCE_SCAN_SYMBOLS} | self.store.priority_symbols_set()


    def _sanitize_mode_action(self, d: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(d or {})
        mode = str(out.get("mode") or "").lower()
        side = str(out.get("side") or "").upper()
        if side == "SHORT" and mode == "delivery":
            out["side"] = "BEARISH"
            out["decision"] = "AVOID_LONG"
            out["status"] = "WATCH"
            out["entry"] = None; out["t1"] = None; out["t2"] = None; out["sl"] = None; out["rr"] = None
            out["risk"] = "Stock short blocked for this mode"
            out["reason"] = (out.get("reason") or "") + "; Delivery is long-only. Bearish evidence is expressed as avoid-long/reduce/watch, never as a short entry."
        return out



    def _trim_reason(self, text: str, limit: int = 190) -> str:
        text = " ".join(str(text or "").split())
        return text if len(text) <= limit else text[:limit-1].rstrip() + "…"


    def _apply_card_fundamentals(self, d: Dict[str, Any]) -> Dict[str, Any]:
        """v26: card projection is zero-network and zero-symbol-search.
        It preserves already-known fundamentals and otherwise uses load_on_open, never false missing.
        """
        out = dict(d or {})
        mode = str(out.get("mode") or "").lower()
        state = str(out.get("fundamental_state") or "").lower()
        has_score = out.get("fundamental_score") not in (None, "", "—")
        if has_score:
            if not state or state in ("missing", "pending", "none"):
                out["fundamental_state"] = "loaded"
            return out
        if mode == "delivery" and state in ("", "missing", "pending", "none"):
            out["fundamental_state"] = "load_on_open"
            out["fundamental_source"] = "stock_intelligence_cache_or_api"
            out["fundamental_note"] = "Open Stock Intelligence for fundamentals; table will not claim false missing"
            if "missing" in str(out.get("risk") or "").lower():
                out["risk"] = "Fundamentals load on Stock Intelligence; not marked missing in table"
        return out


    def _is_curated_watch(self, d: Dict[str, Any]) -> bool:
        if d.get("status") != "WATCH" or d.get("decision") != "WATCH":
            return False
        if not d.get("symbol") or any(str(d.get("symbol")).upper().startswith(x) for x in ("9IIFL", "8IIFL")):
            return False
        if str(d.get("fundamental_state") or "").lower() == "missing" and str(d.get("mode") or "").lower() == "delivery":
            return False
        if (d.get("score") or 0) < 48:
            return False
        return True
