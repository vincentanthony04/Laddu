"""Project Laddu GET handlers: help."""
from __future__ import annotations
from routes_get_dependencies import *

def _help_category(path: str) -> str:
    value = str(path or "").lower()
    if any(token in value for token in ("ready", "health", "status", "architecture", "engineering", "storage")):
        return "Operations"
    if any(token in value for token in ("historical", "coverage", "candles", "mtf", "market-depth", "live-market", "quote")):
        return "Market Data"
    if any(token in value for token in ("scanner", "radar", "heatmap", "indices", "market-breadth", "selection")):
        return "Scanning & Selection"
    if any(token in value for token in ("stock-intelligence", "market-intelligence", "evidence", "decision", "action-object", "fundamental", "market-layers")):
        return "Intelligence & Decisions"
    if any(token in value for token in ("performance", "journal", "portfolio", "positions", "risk", "capital", "settings")):
        return "Portfolio & Performance"
    if any(token in value for token in ("model", "quant", "research", "simulation", "walk-forward", "ai/", "factor", "counterfactual", "winrate", "cost-model")):
        return "Quant, ML & Research"
    if any(token in value for token in ("search", "suggest", "instruments")):
        return "Identity & Search"
    return "Other"

def _help_purpose(path: str, handler: Any) -> str:
    purpose = {
        "/api/ready": "Process readiness and deployed build identity.",
        "/api/health": "Bounded live health snapshot and authoritative runtime state.",
        "/api/product-readiness": "Installation acceptance, usefulness boundaries and blockers.",
        "/api/product-state": "Canonical cache-only product state envelope shared by every operator surface.",
        "/api/scanner/status": "Intraday and Delivery scanner progress contracts.",
        "/api/dashboard-cards": "Canonical Workspace read model.",
        "/api/stock-intelligence": "Selected-stock identity, quote, MTF, evidence, verdict and Trade Map.",
        "/api/historical": "Verified candle history for one symbol and timeframe.",
        "/api/data-coverage": "Historical coverage catalogue and exact timeframe availability.",
        "/api/mtf-trend": "Ten-timeframe composite trend, momentum, structure and quality.",
        "/api/market-depth": "Selected-stock bid/ask depth evidence; never standalone trade authority.",
        "/api/performance/summary": "Settled post-cost performance and accuracy.",
        "/api/model-tournament": "Governed heuristic, Quant and Hybrid model evidence.",
        "/api/nse-data-authority": "Official NSE cash-market source catalogue, incremental history policy, target coverage and model-admission gates.",
        "/api/instrument-brand-assets": "Verified locally cached issuer/index logo assets with content-hash validation and deterministic fallback.",
        "/api/selection-walk-forward-replay": "Purged out-of-sample walk-forward alpha evaluation.",
        "/api/calibrated-challenger/status": "Chronological ML challenger and untouched holdout status.",
        "/api/evidence-pipeline/status": "Historical PIT, trained-model and persisted capital-WFA evidence closure status.",
        "/api/quant-edge/paper-status": "Forward Model Paper edge claim and production-weight boundary.",
        "/api/market-cycle-maturity": "Evidence-based market-cycle and sector-rotation maturity level and missing gates.",
        "/api/product-maturity": "Level-4/Level-5 product maturity gates from scanner, ranking, model and browser evidence.",
        "/api/decision-surface-reconciliation": "Canonical Today Entries, Signal Ledger and Model Paper reconciliation.",
        "/api/model-learning-audit": "Shadow-model observation linkage and ranking-authority leakage audit.",
        "/api/operational-evidence-integrity": "Build-bound hash-chain validation for scanner, browser and market-soak proofs.",
        "/api/system-help": "Endpoint catalogue, diagnostic commands and product test modes.",
        "/api/operator-settings": "Read governed Model Paper wallet and Intraday exposure settings.",
    }
    if path in purpose:
        return purpose[path]
    name = str(getattr(handler, "__name__", "endpoint")).replace("r_", "").replace("_", " ")
    return name[:1].upper() + name[1:] + "."

def r_system_help(app, qs, q, mode):
    """Self-documenting operator help; generated from the active route tables."""
    from routes_get_registry import ROUTES
    try:
        from routes_post import ROUTES as POST_ROUTES
    except Exception:
        POST_ROUTES = {}
    rows = []
    for method, mapping in (("GET", ROUTES), ("POST", POST_ROUTES)):
        for path, handler in sorted(mapping.items()):
            mutating = method == "POST" or path in {
                "/api/delivery-sync", "/api/reload-delivery", "/api/refresh", "/api/deep-scan"
            }
            rows.append({
                "method": method,
                "path": path,
                "category": _help_category(path),
                "purpose": _help_purpose(path, handler),
                "safety": "MUTATING_OR_QUEUED" if mutating else "READ_ONLY",
                "handler": getattr(handler, "__name__", "unknown"),
            })
    commands = [
        {
            "name": "Operational acceptance",
            "purpose": "Verify service, databases, scanner contracts and installed build.",
            "command": "cd C:\\ProgramData\\ProjectLaddu; .\\STATUS.cmd; powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\VERIFY_OPERATIONAL_PRODUCT.ps1 -FailOnBlocked",
            "output": "C:\\ProgramData\\ProjectLaddu\\logs\\validation\\operational-proof-<timestamp>.json",
        },
        {
            "name": "Complete operational evidence",
            "purpose": "Collect service, database, scanner, chart and retained-data evidence.",
            "command": "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\ProgramData\\ProjectLaddu\\tools\\COLLECT_OPERATIONAL_EVIDENCE.ps1 -Minutes 120",
            "output": "C:\\Temp\\ProjectLaddu\\ProjectLaddu-Operational-Evidence-<timestamp>.zip",
        },
        {
            "name": "Stock and chart diagnostics",
            "purpose": "Validate canonical price, coverage, MTF, chart history, verdict and Trade Map for one stock.",
            "command": "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\ProgramData\\ProjectLaddu\\tools\\COLLECT_STOCK_CHART_DIAGNOSTICS.ps1 -Symbol INFY",
            "output": "C:\\Temp\\ProjectLaddu\\ProjectLaddu-Stock-Chart-Diagnostics-<timestamp>.zip",
        },
        {
            "name": "Quant, ML and alpha audit",
            "purpose": "Measure operational latency, settled performance, walk-forward alpha, ML holdout and robustness.",
            "command": "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\ProgramData\\ProjectLaddu\\tools\\AUDIT_QUANT_ML_ALPHA.ps1 -Symbol INFY -PerformanceSamples 3 -SimulationPaths 5000",
            "output": "C:\\Temp\\ProjectLaddu\\ProjectLaddu-Quant-Success-Audit-<timestamp>.zip",
        },
        {
            "name": "Level-4 market-hours soak",
            "purpose": "Collect market-hours scanner progression, canonical ranking traces, restart recovery, risk/lifecycle readiness and post-close Intraday flatten evidence.",
            "command": "C:\\ProgramData\\ProjectLaddu\\RUN_LEVEL4_MARKET_SOAK.cmd",
            "output": "C:\\ProgramData\\ProjectLaddu\\logs\\validation\\level4-market-soak-<timestamp>.json",
        },
    ]
    test_modes = [
        {"name": "Operational", "proves": "The installed product is running and authoritative.", "does_not_prove": "Trading profitability."},
        {"name": "Browser render", "proves": "Current page, chart instances, candle count, MTF cells, overlays and selected-stock identity render coherently.", "does_not_prove": "Historical or forward alpha."},
        {"name": "Purged walk-forward", "proves": "Historical out-of-sample post-cost edge against baselines.", "does_not_prove": "Future persistence."},
        {"name": "ML challenger", "proves": "Calibration, chronological holdout and regime stability.", "does_not_prove": "Broker-authorised performance."},
        {"name": "Forward Model Paper", "proves": "Independent future post-cost outcomes and drift stability.", "does_not_prove": "Live broker execution because broker authority remains NONE."},
    ]
    return {
        "ok": True,
        "version": "system-help-1.2.0-level4-evidence",
        "endpoint_count": len(rows),
        "read_only_count": sum(1 for row in rows if row["safety"] == "READ_ONLY"),
        "mutating_or_queued_count": sum(1 for row in rows if row["safety"] != "READ_ONLY"),
        "endpoints": rows,
        "diagnostic_commands": commands,
        "test_modes": test_modes,
        "installed_root": "C:\\ProgramData\\ProjectLaddu",
        "evidence_root": "C:\\Temp\\ProjectLaddu",
        "broker_authority": "NONE",
        "time": now_iso(),
    }

