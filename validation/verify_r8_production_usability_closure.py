from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from datetime import timezone

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from core.persistent_research_history_service import PersistentResearchHistoryService


checks: list[dict] = []


def check(name: str, ok: object, detail: object = None) -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


class FakeStore:
    def __init__(self) -> None:
        self.quotes: dict[str, dict] = {}

    def latest_quotes_by_symbol(self, symbols):
        return {symbol: self.quotes[symbol] for symbol in symbols if symbol in self.quotes}


class FakePortfolio:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def research_rows(self, limit=1000):
        return sorted(self.rows.values(), key=lambda row: row["occurred_at"], reverse=True)[:limit]

    def _research(self, candidate, disposition, at, price=None):
        signal = str(candidate.get("signal_id") or candidate.get("source_signal_id") or "")
        symbol = str(candidate.get("symbol") or candidate.get("stock") or "").upper()
        mode = str(candidate.get("mode") or "").lower()
        rid = hashlib.sha256(f"{signal}|{symbol}|{mode}|{disposition}".encode()).hexdigest()[:28]
        self.rows[rid] = {
            "research_id": rid,
            "source_signal_id": signal or None,
            "symbol": symbol,
            "mode": mode,
            "disposition": disposition,
            "observed_price": price,
            "occurred_at": at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "payload": dict(candidate),
        }
        return {"state": "RESEARCH", "disposition": disposition, "symbol": symbol, "mode": mode}


store = FakeStore()
portfolio = FakePortfolio()
service = PersistentResearchHistoryService(store, portfolio)
kaynes = {
    "symbol": "KAYNES",
    "exchange": "NSE",
    "mode": "intraday",
    "side": "LONG",
    "source_signal_id": "kaynes-r8-durable",
    "entry": 5200.0,
    "target": 5300.0,
    "sl": 5150.0,
    "ltp": 5225.0,
    "setup": "BREAKOUT_RETEST",
    "research_score": 78.5,
    "holding_period": "same session",
    "latest_reason": "Awaiting volume confirmation",
}
other = {
    "symbol": "RELIANCE",
    "exchange": "NSE",
    "mode": "intraday",
    "side": "LONG",
    "source_signal_id": "reliance-r8",
    "entry": 1400.0,
    "target": 1420.0,
    "sl": 1390.0,
    "ltp": 1405.0,
}
service.publish_many([kaynes], scope_mode="intraday")
first = next(row for row in service.history() if row["symbol"] == "KAYNES")
service.publish_many([other], scope_mode="intraday")
history = service.history()
reranked = next(row for row in history if row["symbol"] == "KAYNES")
check("published Research identity remains after rerank", reranked["research_candidate_id"] == "kaynes-r8-durable" and reranked["result"] == "RERANKED_OUT", reranked)
check("Research first/latest timestamps and customer fields persist", all(reranked.get(key) not in (None, "") for key in ("first_seen_at", "latest_seen_at", "setup", "holding_period", "latest_reason")) and reranked.get("research_score") == 78.5, reranked)
service.publish_many([kaynes], scope_mode="intraday")
resumed = next(row for row in service.history() if row["symbol"] == "KAYNES")
check("same durable Research lifecycle resumes", resumed["first_seen_at"] == first["first_seen_at"] and resumed["research_lifecycle"] == "RESEARCH_ACTIVE", resumed)
store.quotes["KAYNES"] = {"ltp": 5310.0}
service.mark_quotes({"KAYNES": {"ltp": 5310.0, "verified": True, "fresh": True, "executable": True}})
settled = next(row for row in service.history() if row["symbol"] == "KAYNES")
performance = service.performance(service.history())
check("Research Target/SL outcome remains counterfactual", settled["result"] == "TARGET_HIT" and settled["signal_outcome"] == "SUCCESS" and settled["included_in_final_performance"] is False, settled)
check(
    "Research performance exposes complete separate metrics",
    performance.get("authority") == "PERSISTENT_RESEARCH_COUNTERFACTUAL_ONLY"
    and performance.get("included_in_final_performance") is False
    and all(key in performance for key in ("total", "open", "settled", "successful", "failed", "expired_rejected", "success_pct", "average_return_pct", "average_r", "latest_outcomes")),
    performance,
)


def backend_inventory() -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted((ROOT / "backend").rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        result[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


inventory = backend_inventory()
material = "".join(f"{inventory[rel]}  {rel}\n" for rel in sorted(inventory) if rel != "backend/core/persistent_research_history_service.py")
tree_digest = hashlib.sha256(material.encode()).hexdigest()
check("R7 backend is byte-frozen outside persistent Research projection", tree_digest == "0f78a91fadf1449d22af40cbbec9106a06d9c7e6d4614c36a56e13d09fb11b4d" and len(inventory) == 863, {"tree": tree_digest, "files": len(inventory)})
protected = {
    "backend/core/decision_engine_service.py": "e035a0e3c36521ed2150a2ed9fcc18ef8e352b328a1b4a8f99be1a22e7c4cc69",
    "backend/core/trade_geometry_authority.py": "517c265231fd0da0142329780d93e1895e1350e917e8c5f8d7f9b254853e3525",
    "backend/core/exact_broker_cash_cost_authority.py": "70f463a37021fc2422b3d3195b11df13b1a8e59ca88f10d000459d99f723a727",
    "backend/core/model_paper_lifecycle_authority.py": "2d0a69453d7568aab420191df675a4677cd5a6407eb045c4eab3e7ad7b1aac8c",
    "backend/core/outcome_accuracy_taxonomy.py": "2c000a60c2570c341416ae7bf6e5fbf53d72b618a19da05fcd1e2ae0a04eb470",
    "backend/core/intraday_session_structure_authority.py": "b296c8a3972ab7eacee85da2c2242fcb27e781180267cb9b87741aa6a97f06eb",
    "backend/core/structural_trade_map_service.py": "f3d0b139947ba79ec7a3ccb8a65f29fb5ba76bc98e57393c62199f4aa694d0c9",
    "backend/core/evidence_engine_service.py": "f0c33451e1293ce4c53ed13ae0375b9caa00ca15e2915d5f5b1cb60126333c9e",
}
mismatches = [rel for rel, expected in protected.items() if inventory.get(rel) != expected]
check("decision/geometry/cost/settlement/outcome/intraday mathematics are byte-identical to R7", not mismatches, mismatches)

html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8-sig")
js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8-sig")
css = (ROOT / "frontend" / "ui-system.css").read_text(encoding="utf-8-sig")
check("exact visible build and cache identity is R8/PL12 on 8086", all(token in html for token in ('data-build-marker="production-usability-r8-8086"', 'v131 · R8 · 8086', 'app.js?v=131.0.0-r8-pl12-final-8086', 'ui-system.css?v=131.0.0-r8-pl12-final-8086')) and "v131 · R8 · 8086" in js)
check("one coherent six-destination customer navigation remains", all(token in html for token in (">Actionable<", ">Stock Intelligence<", ">Model Paper<", ">Accuracy<", ">Research<", ">Diagnostics<")))
check("Actionable row exposes all required independent fields", all(token in html for token in ("₹ Chg", "% Chg", "Entry", "Target", "SL", "R:R", "Signal Age", "Holding Period", "Outcome / Hit", "Net P&amp;L")) and "emptyRow(16" in js and "rows(payload?.final_signals).filter(workspaceFinalSignal)" in js)
check("Research table is durable, filterable and paginated", all(token in html for token in ("Research Price", "Current R", "First Seen", "Waiting For / Reason", "data-research-scope", "data-research-mode", "data-research-outcome", "researchPrev", "researchNext")) and "researchPageSize: 50" in js)
check("Research, Final and Model Paper performance are visibly separate", all(token in html for token in ("RESEARCH PERFORMANCE + DURABLE HISTORY", "Final Decision Performance", "Model Paper Performance")) and all(token in js for token in ("Final / Paper impact", "Total Final", "Open positions", "Gross P&L", "Charges")))
check("both scanner cadences show lifecycle timestamps/countdown/heartbeat", all(token in html for token in ("trustCadenceIntraday", "trustCadenceDelivery")) and all(token in js for token in ("Last ${compactTime", "Next ${compactTime", "HB ${formatNumber")))
check("Stock Intelligence retains all ten canonical timeframes", all(token in js for token in ("['1m', '1minute']", "['3m', '3minute']", "['5m', '5minute']", "['15m', '15minute']", "['30m', '30minute']", "['1H', '60minute']", "['4H', '240minute']", "['1D', 'day']", "['1W', 'week']", "['1M', 'month']")))
check("direct decision and Research routes are encoded", all(token in js for token in ("function parseHashRoute", "function routeHash", "decision:decisionId", "research:researchCandidate", "researchFocusCandidate")))
check("wide tables are contained inside their own scroll surfaces", "min-width:1540px" in css and "min-width:1840px" in css and ".research-history-wrap" in css)
check("active runtime and acceptance defaults use port 8086", 'DEFAULT_PORT = int(os.environ.get("PROJECT_LADDU_PORT", "8086"))' in (ROOT / "backend" / "config.py").read_text(encoding="utf-8") and "http://127.0.0.1:8086" in (ROOT / "RUN_FINAL_PRODUCT_ACCEPTANCE.ps1").read_text(encoding="utf-8-sig"))
check("one-shot installed acceptance produces evidence ZIP", all((ROOT / name).is_file() for name in ("RUN_FINAL_PRODUCT_ACCEPTANCE.cmd", "RUN_FINAL_PRODUCT_ACCEPTANCE.ps1")) and all(token in (ROOT / "RUN_FINAL_PRODUCT_ACCEPTANCE.ps1").read_text(encoding="utf-8-sig") for token in ("Restart-Service", "research_ids_before", "research_ids_after", "Compress-Archive")))

passed = sum(item["ok"] for item in checks)
failed = len(checks) - passed
payload = {"ok": failed == 0, "contract": "R8_PRODUCTION_USABILITY_CLOSURE", "passed": passed, "failed": failed, "checks": checks}
print(json.dumps(payload, indent=2, default=str))
raise SystemExit(0 if payload["ok"] else 2)
