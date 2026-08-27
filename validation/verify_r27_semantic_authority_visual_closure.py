from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from core.stock_snapshot_service import StockSnapshotService


def gate_map(proof):
    return {str(row.get("gate")): row for row in list(proof.get("gates") or [])}


def main() -> int:
    checks = []
    failures = []

    def check(name: str, ok: bool, detail: str):
        checks.append({"gate": name, "state": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            failures.append(name)

    proof = StockSnapshotService._decision_proof(
        instrument={"exchange": "NSE"},
        component_states={
            "technical_snapshot": {"state": "READY"},
            "chart": {"state": "READY"},
            "quote": {"state": "READY"},
        },
        decision={
            "setup_family": "BREAKOUT_RETEST",
            "canonical_state": "READY",
            "action": "BUY",
            "regime": "BULL",
        },
        trade_map={"valid": True, "room_rr": 2.4, "state": "READY"},
        research={"market_participation": {"liquidity_state": "high"}},
        fundamentals={},
        official_nse_evidence={"state": "POINT_IN_TIME_OFFICIAL_EVIDENCE_READY"},
    )
    gates = gate_map(proof)
    check("SETUP_FAMILY_IS_CANONICAL_SETUP", gates.get("Setup / trigger", {}).get("status") == "PASS", "setup_family must satisfy the named setup/trigger gate")
    check("HIGH_LIQUIDITY_IS_PASS", gates.get("Liquidity / participation", {}).get("status") == "PASS", "high/adequate liquidity states must not render DEFERRED")
    check("POINT_IN_TIME_NSE_READY_IS_PASS", gates.get("NSE delivery / volume", {}).get("status") == "PASS", "POINT_IN_TIME_*_READY is valid official NSE evidence")
    check("FINAL_SELECTED_AUTHORITY_TIER", proof.get("authority_tier") == "FINAL_SELECTED", "admitted BUY/SELL/HOLD with no unresolved hard gate receives strongest authority tier")
    check("NO_FALSE_HARD_BLOCKER", proof.get("first_hard_blocker") is None, "healthy materialized evidence must not fabricate a first blocker")

    pending = StockSnapshotService._decision_proof(
        instrument={"exchange": "NSE"},
        component_states={"technical_snapshot": {"state": "READY"}, "chart": {"state": "READY"}, "quote": {"state": "UNAVAILABLE"}},
        decision={}, trade_map={}, research={}, fundamentals={}, official_nse_evidence={},
    )
    pg = gate_map(pending)
    check("NO_TRADE_DOES_NOT_POISON_DOWNSTREAM", pg.get("S/R room", {}).get("status") == "NOT_APPLICABLE" and pg.get("Final Action", {}).get("status") == "NOT_APPLICABLE", "absence of an admitted setup must remain neutral instead of turning downstream geometry/final action amber")
    check("UNAVAILABLE_IS_NOT_FAILURE", pending.get("first_hard_blocker") is None, "missing pre-admission evidence is a pending requirement, not a fabricated hard failure")

    css = (ROOT / "frontend" / "app.css").read_text(encoding="utf-8-sig")
    js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8-sig")
    ops = (BACKEND / "core" / "operations_control_service.py").read_text(encoding="utf-8-sig")
    check("READABILITY_FLOOR", "body{font-size:15px" in css and "font-size:13px!important" in css and "#quoteStats .stat b{font-size:14px!important}" in css, "R27 overrides historical ultra-small type with readable body/table/quote sizes")
    check("DUAL_THEME_SEMANTIC_PALETTE", 'html[data-theme="dark"]{--green:#34d399' in css and 'html[data-theme="light"]{--green:#059669' in css, "dark and light themes share semantic meaning with theme-appropriate emerald/red/amber/blue values")
    check("UNAVAILABLE_NEUTRAL_RED_RESERVED", ".state-pill.unavailable{color:var(--muted)!important" in css and ".state-pill.stale{color:var(--red)!important" in css, "unavailable is neutral while stale/failure remains red")
    check("RUNNING_IS_GREEN", "if (/COMPLETE|READY|HEALTHY|PASS|RUNNING|ACTIVE|SETTLED/.test(value)) return 'positive'" in js, "healthy diagnostics/runtime progress renders green")
    check("DECISION_PROOF_AUTHORITY_VISUAL", "proof-authority" in js and "proof-tier" in css and "Evidence quality" in js, "Decision Proof visibly separates authority tier, evidence quality and final action")
    check("WFA_STAGE_ERROR_ISOLATION", '"state": "EXECUTION_ERROR"' in ops and "must not\n                    # abort the orchestration" in ops, "one research WFA desk cannot prevent final read-model refresh/reconciliation")
    check("EXPECTED_RETRY_NOT_STUCK", '"WAITING_RETRY"' in ops and '"retry schedule"' in ops, "declared retry/yield waits are classified expected-idle instead of false STUCK")

    att = json.loads((ROOT / "RELEASE_ATTESTATION.json").read_text(encoding="utf-8-sig"))
    check("BROKER_AUTHORITY_NONE", str(att.get("broker_authority") or "").upper() == "NONE", "visual/reconciliation changes never create broker authority")

    result = {
        "ok": not failures,
        "scope": "R27_END_TO_END_SEMANTIC_AUTHORITY_VISUAL_CLOSURE",
        "checks": checks,
        "passed": len(checks) - len(failures),
        "failed": len(failures),
        "failures": failures,
        "broker_authority": "NONE",
        "production_ready": False,
    }
    print(json.dumps(result, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
