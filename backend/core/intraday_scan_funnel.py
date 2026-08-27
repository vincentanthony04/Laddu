"""Observable live-scan funnel without coupling counters into LadduRuntime."""
from __future__ import annotations
from typing import Any, Dict

INTRADAY_MIN_NET_RR = 1.30

class IntradayScanFunnel:
    def __init__(self):
        self.counts = {"scanned":0,"fresh":0,"prepared_map":0,"valid_map":0,"rr_passed":0,"ready":0,"promoted":0}
        self.blockers: Dict[str,int] = {}

    def observe(self, decision: Dict[str,Any]) -> None:
        self.counts["scanned"] += 1
        if str(decision.get("freshness_state") or "").lower()=="live" and str(decision.get("candle_state") or "").lower() not in ("stale","pending","invalid"):
            self.counts["fresh"] += 1
        if bool(decision.get("planned_map_valid")): self.counts["prepared_map"] += 1
        if bool(decision.get("trade_map_valid")): self.counts["valid_map"] += 1
        net_rr = decision.get("est_net_rr") if decision.get("est_net_rr") is not None else decision.get("rr")
        if net_rr is not None and float(net_rr or 0) >= INTRADAY_MIN_NET_RR: self.counts["rr_passed"] += 1
        if str(decision.get("rank_readiness") or "").upper()=="READY": self.counts["ready"] += 1
        if str(decision.get("status") or "").upper()=="PROMOTED": self.counts["promoted"] += 1
        conflicts = decision.get("rank_conflicts") or decision.get("promotion_blocked_by") or []
        if isinstance(conflicts, str):
            conflicts = [conflicts]
        for conflict in conflicts:
            self.blockers[str(conflict)] = self.blockers.get(str(conflict),0)+1

    def report(self):
        return self.counts, dict(sorted(self.blockers.items(),key=lambda kv:(-kv[1],kv[0]))[:6])
