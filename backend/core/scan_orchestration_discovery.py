"""Candidate discovery, fairness and scanner read models."""
from __future__ import annotations

from core.scan_orchestration_dependencies import *  # noqa: F401,F403
from core.persistent_research_history_service import PersistentResearchHistoryService


class ScanDiscoveryMixin:
    def _coverage_snapshot(self, cursor: int | None = None) -> Dict[str, Any]:
            mode_state = self.host.status.get("mode_scanners", {}).get("delivery", {}) or {}
            analysis = mode_state.get("analysis") or {}
            cur = int(cursor if cursor is not None else (analysis.get("sweep_scanned") or mode_state.get("cursor") or 0))
            universe_size = int(analysis.get("universe_size") or (mode_state.get("coverage") or {}).get("universe_size") or 0)
            return {
                "universe_size": universe_size,
                "approx_scanned_cursor": cur,
                "covered": min(universe_size, cur) if universe_size else cur,
                "coverage_pct": round(min(universe_size, cur) * 100.0 / universe_size, 1) if universe_size else 0.0,
                "sweep_number": int(analysis.get("sweep_number") or 1),
                "sweep_complete": bool(analysis.get("sweep_complete")),
                "note": "Coverage is measured against the actual canonical Delivery universe. Priority insertions do not advance the base-universe cursor."
            }

    def _best_available_watch_seeds(self, mode: str = "all") -> list[Dict[str, Any]]:
            """Return honest Intraday/Delivery analysis rows only.

            Non-production desk labels are intentionally excluded
            from active runtime and customer-facing fallbacks.
            """
            requested_mode = normalise_mode(mode or "all")
            requested = ["intraday", "delivery"] if requested_mode in ("all", "") else [requested_mode]
            try:
                queue = self.host.store.get_kv("fair_analysis_queue:last", []) or []
            except Exception:
                queue = []
            out = []
            seen = set()
            for desk in requested:
                if desk not in ("intraday", "delivery"):
                    continue
                for source in queue:
                    symbol = str(source.get("symbol") or "").upper().strip()
                    if not symbol or (symbol, desk) in seen:
                        continue
                    row = dict(source)
                    row.update({
                        "symbol": symbol, "mode": desk, "side": "NEUTRAL",
                        "decision": "ANALYSIS_PENDING", "status": "WATCH", "score": 0,
                        "candidate_stage": "UNDER_REVIEW", "opportunity_stage": "Under Review",
                        "price_freshness": ("verified analysis-queue quote" if row.get("freshness_state") in ("live", "closed_market") and not row.get("stale") else "quote verification pending"),
                        "reason": ("Intraday analysis queue: completed-candle Technical/MTF, liquidity, regime and entry-map gates are pending."
                                   if desk == "intraday" else
                                   "Delivery analysis queue: point-in-time fundamentals, Technical/MTF, regime and entry-map gates are pending."),
                        "priority_reason": "Fair analysis opportunity only; not trade confidence or a selected entry.",
                    })
                    out.append(row); seen.add((symbol, desk))
                    if sum(1 for x in out if x.get("mode") == desk) >= 4:
                        break
            if out:
                return out
            # Empty queue is explicit; one core symbol per supported desk keeps the
            # UI contract visible without inventing a candidate or a price.
            fallbacks = {
                "intraday": {"symbol":"RELIANCE","exchange":"NSE","mode":"intraday","side":"NEUTRAL","decision":"ANALYSIS_PENDING","status":"WATCH","score":0,"reason":"Intraday analysis queue is warming; verified quote and completed-candle gates are pending.","price_freshness":"verified quote pending","candidate_stage":"UNDER_REVIEW","opportunity_stage":"Under Review"},
                "delivery": {"symbol":"RELIANCE","exchange":"NSE","mode":"delivery","side":"NEUTRAL","decision":"ANALYSIS_PENDING","status":"WATCH","score":0,"reason":"Delivery analysis queue is warming; point-in-time fundamentals and completed-candle gates are pending.","price_freshness":"verified quote pending","candidate_stage":"UNDER_REVIEW","opportunity_stage":"Under Review"},
            }
            return [dict(fallbacks[d]) for d in requested if d in fallbacks]

    def potential_candidates(self, mode: str = "all", limit: int = 60, compact: bool = False) -> Dict[str, Any]:
            try:
                raw = [self.host._normalize_opportunity_case(d) for d in self.host.store.opportunity_candidates(mode, limit=max(limit, 80))]
                rows = self.host._group_opportunity_rows(raw)[:limit]
            except Exception as exc:
                self.host.record_error("potential_candidates", str(exc))
                rows = []
            # Supplement sparse promotion funnels with the governed fair-analysis
            # queue. These rows are explicitly UNDER_REVIEW and never promoted by
            # this read model.
            seen = {(str(x.get("symbol") or "").upper(), str(x.get("mode") or "").lower()) for x in rows}
            for seed in self._best_available_watch_seeds(mode):
                key = (str(seed.get("symbol") or "").upper(), str(seed.get("mode") or "").lower())
                if key in seen:
                    continue
                rows.append(dict(seed, priority_score=seed.get("analysis_priority_score") or 0, priority_reason=seed.get("priority_reason") or seed.get("reason")))
                seen.add(key)
                if len(rows) >= limit:
                    break
            projector = self.host._compact_card_project if compact else self.host._card_project
            return {
                "state": "active", "mode": mode, "count": len(rows),
                "summary": self.host._opportunity_summary_from_rows(rows),
                "candidates": [projector(d) for d in rows[:limit]],
                "policy": "Potential/Watch/Under Review rows are analysis priorities only. Only fresh canonical READY decisions can enter Today's Entries."
            }

    def _research_candidates(self, mode: str = "all", limit: int = 40) -> list[Dict[str, Any]]:
            rows = self.host.store.latest_decisions(mode, limit=260)
            out = []
            seen = set()
            for d in rows:
                sym = str(d.get("symbol") or "").upper(); m = str(d.get("mode") or "").lower()
                if not sym or (sym, m) in seen:
                    continue
                stage = str(d.get("candidate_stage") or "").upper()
                score = int(d.get("score") or 0)
                buckets = d.get("discovery_buckets") or []
                if stage in ("ARMED", "QUALIFIED", "WATCH") or (buckets and score >= 50):
                    requested = normalise_mode(mode)
                    if requested not in ("all", "intraday", "delivery") or (requested != "all" and m != requested):
                        continue
                    seen.add((sym, m)); out.append(d)
            out.sort(key=lambda x: (0 if str(x.get("candidate_stage") or "").upper()=="ARMED" else 1 if str(x.get("candidate_stage") or "").upper()=="QUALIFIED" else 2, -(int(x.get("score") or 0)), str(x.get("symbol") or "")))
            visible = out[:limit]
            # R5: publication is the customer-history commitment boundary. The
            # active shortlist remains transient, but a published Research row is
            # persisted before it is returned and can never vanish on rerank.
            try:
                PersistentResearchHistoryService(self.host.store, self.host.model_portfolio).publish_many(visible, scope_mode=mode)
            except Exception as exc:
                self.host.record_error("persistent_research_publication", str(exc))
            return visible

    def selection_fairness_snapshot(self, mode: str = "all", *, persist: bool = False) -> Dict[str, Any]:
            """Measure fair *analysis opportunity* for the two supported desks.

            Legacy desk labels are excluded from the audit instead of being silently
            merged into customer-facing Intraday/Delivery statistics.
            """
            desk = str(mode or "all").lower()
            canonical = desk if desk in ("intraday", "delivery") else "all"
            try:
                universe = self.host.store.tradable_nse_equity_universe(limit=5000)
            except Exception:
                universe = []
            try:
                decisions = self.host.store.latest_decisions(canonical, limit=5000)
            except Exception:
                decisions = []
            if canonical in ("intraday", "delivery"):
                decisions = [row for row in decisions if str(row.get("mode") or "").lower() == canonical]
            else:
                decisions = [row for row in decisions if str(row.get("mode") or "").lower() in ("intraday", "delivery")]
            decisions_before_window = len(decisions)
            window_days = 1 if canonical == "intraday" else 30 if canonical == "delivery" else 7
            cutoff = india_now() - timedelta(days=window_days)
            def _decision_time(row):
                for key in ("last_refresh", "last_update", "observed_at", "created_at", "timestamp"):
                    value = row.get(key)
                    if not value:
                        continue
                    try:
                        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                        return stamp if stamp.tzinfo else stamp.replace(tzinfo=IST)
                    except ValueError:
                        continue
                return None
            decisions = [row for row in decisions if (_decision_time(row) is not None and _decision_time(row).astimezone(IST) >= cutoff)]
            latest_by_symbol = {}
            for row in decisions:
                symbol = str(row.get("symbol") or "").upper().strip()
                if symbol and symbol not in latest_by_symbol:
                    latest_by_symbol[symbol] = row
            coverage_quotes = getattr(self.host, "_coverage_quote_cache", {}) or {}
            eligible = []
            for raw in universe:
                row = dict(raw)
                symbol = str(row.get("trading_symbol") or row.get("symbol") or "").upper().strip()
                row.update(dict(coverage_quotes.get(symbol) or {}))
                # Sector/index metadata from the latest point-in-time decision is
                # descriptive only; it never changes eligibility or confidence.
                prior = latest_by_symbol.get(symbol) or {}
                for key in ("sector", "sector_label", "segment_index", "market_cap_bucket"):
                    if not row.get(key) and prior.get(key):
                        row[key] = prior.get(key)
                eligible.append(row)
            analyzed = list(latest_by_symbol.values())
            promoted = [row for row in analyzed if str(row.get("rank_readiness") or "").upper() == "READY" or str(row.get("status") or "").upper() == "PROMOTED"]
            report = SelectionFairnessService(self.host.store).audit(eligible, analyzed, promoted, persist=persist, desk=canonical)
            report["analysis_window_days"] = window_days
            report["analysis_rows_before_window"] = decisions_before_window
            report["analysis_rows_in_window"] = len(decisions)
            report["universe_scope"] = "clean tradable NSE EQ/BE instruments; pre-listing and non-equity exclusions are outside this fairness audit"
            return report

    def candidate_discovery(self, mode: str = "all") -> Dict[str, Any]:
            rows = self._research_candidates(mode, limit=60)
            fairness = self.selection_fairness_snapshot(mode, persist=False)
            return CandidateDiscoveryService(
                self.host.store, self.host.status, universe_size=len(INTELLIGENCE_SCAN_SYMBOLS),
                compact_project=self.host._compact_card_project, group_rows=self.host._group_opportunity_rows,
            ).build(rows, mode=mode, coverage=self.host.status.get("deep_scan", {}).get("coverage") or self._coverage_snapshot(), fairness=fairness)

    def _is_actionable_selected(self, d: Dict[str, Any]) -> bool:
            return is_actionable_signal(d, market_open=is_india_market_open())

    def sector_cycle_board(self, mode: str = "all") -> Dict[str, Any]:
            rows = self.host.store.opportunity_candidates(mode, limit=240) + self._research_candidates(mode, limit=120)
            sectors: Dict[str, Dict[str, Any]] = {}
            for d in rows:
                sec = str(d.get("sector") or "broad")
                item = sectors.setdefault(sec, {"sector": sec, "candidates": [], "score": 0, "themes": {}, "evidence": []})
                ps = int(d.get("priority_score") or d.get("score") or 0)
                with self.host.lock:
                    st = str(d.get("candidate_stage") or d.get("opportunity_stage") or "Potential").upper()
                item["score"] += ps + (14 if st == "ARMED" else 8 if st == "QUALIFIED" else 3)
                item["candidates"].append(self.host._compact_card_project(d))
                for t in (d.get("themes") or []):
                    item["themes"][str(t)] = item["themes"].get(str(t), 0) + 1
                for e in (d.get("discovery_evidence") or [])[:2]:
                    if e not in item["evidence"]:
                        item["evidence"].append(e)
            out = []
            for sec, item in sectors.items():
                n = max(1, len(item["candidates"]))
                avg = item["score"] / n
                if avg >= 90 or len([x for x in item["candidates"] if str(x.get("candidate_stage") or "").upper() == "ARMED"]) >= 2:
                    state = "Leading"
                elif avg >= 72:
                    state = "Improving"
                elif avg >= 55:
                    state = "Bottoming / Watch"
                elif avg >= 35:
                    state = "Neutral"
                else:
                    state = "Weakening / Avoid"
                item["state"] = state
                item["score"] = round(avg, 1)
                item["themes"] = dict(sorted(item["themes"].items(), key=lambda kv: (-kv[1], kv[0]))[:5])
                item["candidates"] = item["candidates"][:3]
                item["evidence"] = item["evidence"][:6]
                out.append(item)
            out.sort(key=lambda x: (0 if x["state"] == "Leading" else 1 if x["state"] == "Improving" else 2 if "Bottoming" in x["state"] else 3, -x["score"], x["sector"]))
            return {"state": "active", "mode": mode, "sectors": out[:10], "policy": "Sector cycle prioritizes sectors/themes where evidence is improving, then picks best candidates inside each sector."}
