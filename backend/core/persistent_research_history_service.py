"""Durable customer Research publication/history authority.

This service deliberately reuses the retained Research observation authority.
It does not create a second trading/decision database.  A candidate becomes a
customer-history commitment when the Research read model publishes it.  Later
scanner reranks may remove it from the *active shortlist*, but cannot erase the
published observation/history.

Research performance is counterfactual and is never mixed into canonical Final
Model Paper performance or realised P&L.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Dict, Iterable, Mapping


class PersistentResearchHistoryService:
    VERSION = "persistent-research-history-r8-1.1.0"
    PUBLISHED = "SCANNER_RESEARCH_PUBLISHED"
    TARGET = "RESEARCH_TARGET_HIT"
    STOP = "RESEARCH_SL_HIT"
    RERANKED = "RESEARCH_RERANKED_OUT"
    PROMOTED = "RESEARCH_PROMOTED_TO_FINAL"
    REJECTED = "RESEARCH_REJECTED"
    EXPIRED = "RESEARCH_EXPIRED"
    TIME_EXIT = "RESEARCH_TIME_EXIT"
    CLOSED = "RESEARCH_CLOSED"

    def __init__(self, store: Any, portfolio: Any):
        self.store = store
        self.portfolio = portfolio

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _next_event_time(cls, rows: Iterable[Mapping[str, Any]]) -> datetime:
        """Return a timestamp strictly newer than every persisted event.

        Research publication and rerank can happen inside one scheduler tick.
        Strict monotonicity makes the last event deterministic even on clocks
        whose effective timestamp resolution is coarser than Python's timer.
        """
        value = cls._now()
        for row in rows:
            stamp = cls._iso(row.get("occurred_at"))
            if not stamp:
                continue
            try:
                prior = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except Exception:
                continue
            if prior.tzinfo is None:
                prior = prior.replace(tzinfo=timezone.utc)
            prior = prior.astimezone(timezone.utc)
            if prior >= value:
                value = prior + timedelta(microseconds=1)
        return value

    @staticmethod
    def _iso(value: Any) -> str:
        if isinstance(value, datetime):
            dt = value
        else:
            text = str(value or "").strip()
            if not text:
                return ""
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except Exception:
                return text
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _payload(row: Mapping[str, Any]) -> Dict[str, Any]:
        raw = row.get("payload")
        if isinstance(raw, dict):
            return dict(raw)
        raw = row.get("payload_json")
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                return dict(parsed) if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _num(value: Any) -> float | None:
        try:
            out = float(value)
            return out if out == out else None
        except Exception:
            return None

    @classmethod
    def candidate_id(cls, row: Mapping[str, Any]) -> str:
        for key in ("research_candidate_id", "source_signal_id", "signal_id", "decision_id", "candidate_id"):
            value = str(row.get(key) or "").strip()
            if value:
                return value
        body = "|".join(str(row.get(key) or "") for key in (
            "symbol", "stock", "exchange", "mode", "side", "generated_at", "decision_ts", "created_at",
            "entry", "planned_entry", "target", "t1", "planned_t1", "sl", "stop", "planned_sl",
        ))
        return "research:" + hashlib.sha256(body.encode()).hexdigest()[:28]

    def _raw_rows(self, limit: int = 1000) -> list[Dict[str, Any]]:
        try:
            return [dict(row or {}) for row in (self.portfolio.research_rows(limit=max(1, min(int(limit), 1000))) or [])]
        except Exception:
            return []

    def _first_seen_map(self, raw_rows: Iterable[Mapping[str, Any]]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for raw in raw_rows:
            payload = self._payload(raw)
            rid = str(payload.get("research_candidate_id") or raw.get("source_signal_id") or raw.get("research_id") or "").strip()
            if not rid:
                continue
            stamp = self._iso(payload.get("research_first_seen_at") or raw.get("occurred_at"))
            if stamp and (rid not in out or stamp < out[rid]):
                out[rid] = stamp
        return out

    def publish_many(self, candidates: Iterable[Mapping[str, Any]], *, scope_mode: str = "all") -> Dict[str, Any]:
        rows = [dict(row or {}) for row in candidates or []]
        if not rows:
            return {"ok": True, "published": 0, "version": self.VERSION}
        existing = self._raw_rows(1000)
        first_seen = self._first_seen_map(existing)
        now = self._next_event_time(existing)
        now_iso = self._iso(now)
        published = 0
        reranked = 0
        errors: list[str] = []
        visible_ids: set[str] = set()
        for candidate in rows:
            try:
                symbol = str(candidate.get("symbol") or candidate.get("stock") or "").upper().strip()
                mode = str(candidate.get("mode") or "").lower().strip()
                if not symbol or mode not in {"intraday", "delivery"}:
                    continue
                rid = self.candidate_id(candidate)
                visible_ids.add(rid)
                payload = dict(candidate)
                payload.setdefault("source_signal_id", rid)
                payload["research_candidate_id"] = rid
                payload["research_first_seen_at"] = first_seen.get(rid) or now_iso
                payload["research_last_seen_at"] = now_iso
                payload["research_lifecycle"] = "ACTIVE"
                payload["research_event"] = "PUBLISHED"
                payload["research_origin"] = "CUSTOMER_RESEARCH_PUBLICATION"
                price = self._num(candidate.get("ltp") or candidate.get("price") or candidate.get("current_price"))
                self.portfolio._research(payload, self.PUBLISHED, now, price=price)
                first_seen.setdefault(rid, payload["research_first_seen_at"])
                published += 1
            except Exception as exc:
                errors.append(f"{type(exc).__name__}:{exc}"[:240])

        # A shortlist is transient; the history is not.  Explicitly mark a
        # previously customer-published active candidate as RERANKED_OUT when it
        # leaves this publication scope.  The counterfactual remains monitored
        # and can still settle Target/SL later.  Re-publication resumes the same
        # stable candidate identity.
        scope = str(scope_mode or "all").lower().strip()
        for rid, events in self._groups(existing).items():
            if rid in visible_ids or not events:
                continue
            latest = events[-1]
            lp = dict(latest.get("_payload") or {})
            if str(lp.get("research_origin") or "").upper() != "CUSTOMER_RESEARCH_PUBLICATION":
                continue
            last_disp = str(latest.get("disposition") or "").upper()
            if last_disp != self.PUBLISHED:
                continue
            mode = str(latest.get("mode") or lp.get("mode") or "").lower()
            if scope in {"intraday", "delivery"} and mode != scope:
                continue
            payload = dict(lp)
            payload["research_candidate_id"] = rid
            payload["research_first_seen_at"] = first_seen.get(rid) or self._iso(latest.get("occurred_at"))
            payload["research_last_seen_at"] = now_iso
            payload["research_lifecycle"] = "RERANKED_OUT"
            payload["research_event"] = "RERANKED_OUT"
            try:
                self.portfolio._research(payload, self.RERANKED, now, price=self._num(latest.get("observed_price")))
                reranked += 1
            except Exception as exc:
                errors.append(f"rerank:{type(exc).__name__}:{exc}"[:240])
        return {"ok": not errors, "published": published, "reranked": reranked, "errors": errors[:10], "version": self.VERSION}

    def _groups(self, raw_rows: Iterable[Mapping[str, Any]]) -> Dict[str, list[Dict[str, Any]]]:
        groups: Dict[str, list[Dict[str, Any]]] = {}
        for raw0 in raw_rows:
            raw = dict(raw0 or {})
            payload = self._payload(raw)
            rid = str(payload.get("research_candidate_id") or raw.get("source_signal_id") or "").strip()
            if not rid:
                # Older persisted Research evidence is still retained rather than hidden.
                rid = str(raw.get("research_id") or "").strip()
            if not rid:
                continue
            raw["_payload"] = payload
            groups.setdefault(rid, []).append(raw)
        for events in groups.values():
            events.sort(key=lambda x: self._iso(x.get("occurred_at")))
        return groups

    @staticmethod
    def _geometry(source: Mapping[str, Any]) -> tuple[str, float | None, float | None, float | None]:
        side = str(source.get("side") or "").upper().strip()
        def num(v):
            try: return float(v)
            except Exception: return None
        entry = num(source.get("entry") or source.get("planned_entry"))
        target = num(source.get("target") or source.get("t1") or source.get("planned_t1"))
        stop = num(source.get("sl") or source.get("stop") or source.get("planned_sl"))
        return side, entry, target, stop

    @staticmethod
    def _perf(side: str, entry: float | None, stop: float | None, price: float | None) -> tuple[float | None, float | None]:
        if not entry or price is None:
            return None, None
        raw_pct = 100.0 * (price - entry) / entry
        if side == "SHORT": raw_pct = -raw_pct
        risk = abs(entry - stop) if stop else 0.0
        raw_r = ((price - entry) / risk if risk else None)
        if raw_r is not None and side == "SHORT": raw_r = -raw_r
        return (round(raw_r, 4) if raw_r is not None else None, round(raw_pct, 4))

    def history(self, limit: int = 500) -> list[Dict[str, Any]]:
        groups = self._groups(self._raw_rows(1000))
        symbols = sorted({str(events[-1].get("symbol") or "").upper() for events in groups.values() if events})
        try:
            quotes = dict(self.store.latest_quotes_by_symbol(symbols) or {}) if symbols else {}
        except Exception:
            quotes = {}
        out: list[Dict[str, Any]] = []
        for rid, events in groups.items():
            if not events:
                continue
            # Merge payloads so later lifecycle metadata can enrich, but first geometry remains available.
            merged: Dict[str, Any] = {}
            for event in events:
                merged.update(event.get("_payload") or {})
            latest = events[-1]
            first_seen = min((self._iso((e.get("_payload") or {}).get("research_first_seen_at") or e.get("occurred_at")) for e in events), default="")
            symbol = str(latest.get("symbol") or merged.get("symbol") or merged.get("stock") or "").upper()
            mode = str(latest.get("mode") or merged.get("mode") or "").lower()
            quote = dict(quotes.get(symbol) or {})
            terminal_dispositions = {self.TARGET, self.STOP, self.REJECTED, self.EXPIRED, self.TIME_EXIT, self.CLOSED}
            terminal = next((e for e in reversed(events) if str(e.get("disposition") or "").upper() in terminal_dispositions), None)
            disposition = str((terminal or latest).get("disposition") or "").upper()
            price = self._num((terminal or {}).get("observed_price")) if terminal else self._num(quote.get("ltp") or latest.get("observed_price"))
            side, entry, target, stop = self._geometry(merged)
            realized_r, perf_pct = self._perf(side, entry, stop, price)
            if disposition == self.TARGET:
                outcome, result, lifecycle = "SUCCESS", "TARGET_HIT", "RESEARCH_SETTLED"
            elif disposition == self.STOP:
                outcome, result, lifecycle = "FAILURE", "SL_HIT", "RESEARCH_SETTLED"
            elif disposition == self.RERANKED:
                outcome, result, lifecycle = "PENDING", "RERANKED_OUT", "RESEARCH_HISTORY"
            elif disposition == self.PROMOTED:
                outcome, result, lifecycle = "PENDING", "PROMOTED_TO_FINAL", "RESEARCH_PROMOTED_TO_FINAL"
            elif disposition == self.REJECTED:
                outcome, result, lifecycle = "REJECTED", "REJECTED", "RESEARCH_REJECTED"
            elif disposition == self.EXPIRED:
                outcome, result, lifecycle = "EXPIRED", "EXPIRED", "RESEARCH_EXPIRED"
            elif disposition == self.TIME_EXIT:
                outcome, result, lifecycle = "TIME_EXIT", "TIME_EXIT", "RESEARCH_SETTLED"
            elif disposition == self.CLOSED:
                outcome, result, lifecycle = "CLOSED", "CLOSED", "RESEARCH_CLOSED"
            else:
                outcome, result, lifecycle = "PENDING", "OPEN", "RESEARCH_ACTIVE"
            setup = next((merged.get(key) for key in ("setup", "setup_type", "setup_name", "pattern", "strategy_name", "thesis_type") if merged.get(key) not in (None, "")), None)
            score = next((self._num(merged.get(key)) for key in ("research_score", "rank_score", "evidence_score", "priority_score", "score") if self._num(merged.get(key)) is not None), None)
            holding = next((merged.get(key) for key in ("holding_period", "target_window", "horizon", "expected_horizon") if merged.get(key) not in (None, "")), None)
            reason = next((merged.get(key) for key in ("latest_reason", "waiting_for", "blocker", "block_reason", "reason", "gate_reason") if merged.get(key) not in (None, "")), None)
            latest_seen = self._iso(merged.get("research_last_seen_at") or latest.get("occurred_at"))
            out.append({
                "row_id": f"research:{rid}", "research_candidate_id": rid,
                "source_signal_id": latest.get("source_signal_id") or merged.get("source_signal_id") or rid,
                "book": "RESEARCH_COUNTERFACTUAL", "segment": "research",
                "symbol": symbol, "exchange": merged.get("exchange") or "NSE", "mode": mode,
                "side": side or None, "status": lifecycle, "research_lifecycle": lifecycle,
                "research_stage": result, "setup": setup, "research_score": score,
                "ltp": price, "entry": entry, "target": target, "original_stop": stop, "active_stop": stop,
                "trade_map_valid": bool(side == "LONG" and None not in (stop, entry, target) and stop < entry < target or side == "SHORT" and None not in (target, entry, stop) and target < entry < stop),
                "trade_map_state": "RESEARCH_MAP_READY" if None not in (entry, target, stop) else "NON_ACTIONABLE_MAP_INCOMPLETE",
                "quantity": 0, "capital": 0.0, "net_pnl": None,
                "action": "RESEARCH SETTLED" if terminal else ("RESEARCH · HISTORY / MONITOR" if lifecycle == "RESEARCH_HISTORY" else "RESEARCH · MONITOR"),
                "outcome": outcome, "signal_outcome": outcome, "result": result,
                "research_realized_r": realized_r if terminal else None,
                "research_current_r": realized_r,
                "research_performance_pct": perf_pct,
                "first_seen_at": first_seen,
                "latest_seen_at": latest_seen,
                "holding_period": holding,
                "latest_reason": reason,
                "occurred_at": first_seen,
                "updated_at": self._iso(latest.get("occurred_at")),
                "closed_at": self._iso(terminal.get("occurred_at")) if terminal else None,
                "accuracy_state": "RESEARCH_SCORED" if terminal else "RESEARCH_PENDING",
                "performance_state": "RESEARCH_COUNTERFACTUAL_ONLY",
                "included_in_final_performance": False,
                "research_event_count": len(events),
                "research_terminal_disposition": disposition if terminal else None,
                "chart_binding": {"symbol": symbol, "mode": mode},
            })
        out.sort(key=lambda x: str(x.get("first_seen_at") or ""), reverse=True)
        return out[:max(1, min(int(limit), 1000))]

    def mark_quotes(self, quotes: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
        accepted = {str(k).upper(): dict(v or {}) for k,v in (quotes or {}).items()}
        if not accepted:
            return {"ok": True, "settled": 0, "version": self.VERSION}
        rows = self.history(limit=1000)
        settled = 0; errors: list[str] = []
        now = self._next_event_time(self._raw_rows(1000))
        for row in rows:
            if row.get("research_lifecycle") == "RESEARCH_SETTLED":
                continue
            quote = accepted.get(str(row.get("symbol") or "").upper())
            if not quote:
                continue
            price = self._num(quote.get("ltp") or quote.get("price"))
            if not price:
                continue
            side = str(row.get("side") or "").upper(); target=self._num(row.get("target")); stop=self._num(row.get("original_stop"))
            disposition = None
            if side == "LONG" and target is not None and price >= target: disposition = self.TARGET
            elif side == "LONG" and stop is not None and price <= stop: disposition = self.STOP
            elif side == "SHORT" and target is not None and price <= target: disposition = self.TARGET
            elif side == "SHORT" and stop is not None and price >= stop: disposition = self.STOP
            if not disposition:
                continue
            payload = {
                "symbol": row.get("symbol"), "exchange": row.get("exchange") or "NSE", "mode": row.get("mode"),
                "side": side, "entry": row.get("entry"), "target": row.get("target"), "stop": row.get("original_stop"),
                "source_signal_id": row.get("source_signal_id") or row.get("research_candidate_id"),
                "research_candidate_id": row.get("research_candidate_id"),
                "research_first_seen_at": row.get("first_seen_at"),
                "research_event": disposition, "research_origin": "CUSTOMER_RESEARCH_PUBLICATION",
            }
            try:
                self.portfolio._research(payload, disposition, now, price=price); settled += 1
            except Exception as exc:
                errors.append(f"{row.get('symbol')}:{type(exc).__name__}:{exc}"[:240])
        return {"ok": not errors, "settled": settled, "errors": errors[:10], "version": self.VERSION}

    @staticmethod
    def performance(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
        data = [dict(r or {}) for r in rows or []]
        success = sum(str(r.get("signal_outcome") or "").upper() == "SUCCESS" for r in data)
        failure = sum(str(r.get("signal_outcome") or "").upper() == "FAILURE" for r in data)
        scored = success + failure
        active = sum(str(r.get("research_lifecycle") or "").upper() == "RESEARCH_ACTIVE" for r in data)
        history = sum(str(r.get("research_lifecycle") or "").upper() == "RESEARCH_HISTORY" for r in data)
        promoted = sum("PROMOTED" in str(r.get("research_stage") or r.get("research_lifecycle") or "").upper() for r in data)
        rejected = sum("REJECTED" in str(r.get("research_stage") or r.get("signal_outcome") or "").upper() for r in data)
        expired = sum("EXPIRED" in str(r.get("research_stage") or r.get("signal_outcome") or "").upper() for r in data)
        open_count = sum(str(r.get("signal_outcome") or "").upper() == "PENDING" for r in data)
        positive = sum((r.get("research_performance_pct") or 0) > 0 for r in data if r.get("research_performance_pct") is not None)
        negative = sum((r.get("research_performance_pct") or 0) < 0 for r in data if r.get("research_performance_pct") is not None)
        returns = [float(r["research_performance_pct"]) for r in data if r.get("research_performance_pct") is not None]
        r_values = [float(r["research_current_r"]) for r in data if r.get("research_current_r") is not None]
        latest = sorted(data, key=lambda r: str(r.get("updated_at") or r.get("first_seen_at") or ""), reverse=True)[:10]
        return {
            "authority": "PERSISTENT_RESEARCH_COUNTERFACTUAL_ONLY",
            "published": len(data), "total": len(data), "open": open_count, "active": active, "history": history, "settled": scored,
            "success": success, "failure": failure,
            "successful": success, "failed": failure, "promoted": promoted,
            "rejected": rejected, "expired": expired, "expired_rejected": rejected + expired,
            "accuracy_pct": round(success * 100.0 / scored, 2) if scored else None,
            "success_pct": round(success * 100.0 / scored, 2) if scored else None,
            "average_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
            "average_r": round(sum(r_values) / len(r_values), 4) if r_values else None,
            "latest_outcomes": [{
                "research_candidate_id": r.get("research_candidate_id"), "symbol": r.get("symbol"), "mode": r.get("mode"),
                "stage": r.get("research_stage") or r.get("research_lifecycle"), "outcome": r.get("signal_outcome"),
                "return_pct": r.get("research_performance_pct"), "r": r.get("research_current_r"), "updated_at": r.get("updated_at"),
            } for r in latest],
            "positive_mtm": positive, "negative_mtm": negative,
            "included_in_final_performance": False,
            "policy": "Every customer-published Research candidate remains visible; Research results never enter Final/Model Paper accuracy or realised P&L.",
            "version": PersistentResearchHistoryService.VERSION,
        }
