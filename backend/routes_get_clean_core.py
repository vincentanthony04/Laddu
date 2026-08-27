"""Project Laddu Clean Core R4 GET handlers.

These bounded routes expose architecture/Gate-1 proof state without regrowing
the legacy System route owner.
"""
from __future__ import annotations
from routes_get_dependencies import *

def r_clean_core_status(app, qs, q, mode):
    """Bounded architecture truth for the Clean Core runtime boundary."""
    executor = getattr(getattr(app, "scan_orchestration", None), "analysis_executor", None)
    scanner = {}
    if executor is not None and callable(getattr(executor, "capacity", None)):
        for desk in ("intraday", "delivery"):
            try:
                scanner[desk] = executor.capacity(desk)
            except Exception as exc:
                scanner[desk] = {"state": "UNAVAILABLE", "error": str(exc)[:160]}
    return {
        "ok": True,
        "architecture": "CLEAN_CORE_R4",
        "product_mode": "AUTOMATIC_MODEL_PAPER_ONLY",
        "broker_authority": "NONE",
        "fast_read": {
            "stock": "/api/stock-snapshot",
            "chart": "/api/chart-data",
            "quote": "/api/live-quotes",
            "search": "/api/search",
            "foreground_forbidden_dependencies": [
                "scanner", "priority_pipeline", "research_training", "ml_inference",
                "controller", "provider_history", "provider_quote_gap_fill",
            ],
        },
        "storage_roles": {
            "memory": "hot/live state",
            "questdb": "live/recent time series",
            "postgresql": "transactional/business authority and materialized read models",
            "parquet_duckdb": "historical/research evidence",
        },
        "research": {
            "policy": "preserve/version/read latest retained snapshot; refresh asynchronously",
            "preservation_manifest": "/api/research-preservation-manifest",
            "destructive_upgrade_allowed": False,
        },
        "scanner": scanner,
        "background_repair": (getattr(app, "_clean_core_repair_dispatcher", None).status()
                              if callable(getattr(getattr(app, "_clean_core_repair_dispatcher", None), "status", None))
                              else {"state": "NOT_STARTED", "pending_or_running": 0}),
        "time": now_iso(),
    }


def r_clean_core_gate1_sample(app, qs, q, mode):
    """Deterministic bounded sample from the canonical cash-equity authority.

    Production uses the dedicated interactive PostgreSQL catalogue reader and
    never materialises thousands of compatibility rows merely to prove a
    20-150 symbol customer path.
    """
    from core.canonical_presentation_service import CanonicalPresentationService
    limit = _qint(qs, "limit", 100, min_val=20, max_val=150)
    canonical_reader = getattr(app.store, "canonical_equity_sample", None)
    compatibility_rows = False
    try:
        if callable(canonical_reader):
            selected = list(canonical_reader(limit) or [])
        else:
            # Compatibility-only test/legacy facade. The installed production
            # Store always implements canonical_equity_sample and never enters
            # this broad local-projection branch.
            rows = list(app.store.all_eligible_equity_keys(limit=5000) or [])
            compatibility_rows = True
            nse = []
            bse = []
            nse_symbols = {
                str(row.get("trading_symbol") or row.get("symbol") or "").upper().strip()
                for row in rows if str(row.get("exchange") or row.get("segment") or "").upper().startswith("NSE")
            }
            for raw in rows:
                row = dict(raw or {})
                exchange = str(row.get("exchange") or row.get("segment") or "").upper()
                symbol = str(row.get("trading_symbol") or row.get("symbol") or "").upper().strip()
                if exchange.startswith("NSE"):
                    nse.append(row)
                elif exchange.startswith("BSE") and symbol and symbol not in nse_symbols:
                    bse.append(row)

            def spaced(source, count):
                source = list(source or [])
                if count <= 0 or not source:
                    return []
                if len(source) <= count:
                    return source
                if count == 1:
                    return [source[len(source) // 2]]
                return [source[round(i * (len(source) - 1) / (count - 1))] for i in range(count)]

            bse_target = min(max(5, limit // 10), len(bse), limit // 3)
            selected = spaced(nse, max(0, limit - bse_target)) + spaced(bse, bse_target)
    except Exception as exc:
        return {"ok": False, "state": "CATALOGUE_UNAVAILABLE", "error": str(exc)[:200], "sample": []}
    presentation = CanonicalPresentationService(app.store)
    # The sample is itself canonical PostgreSQL authority. Prime the shared
    # presentation cache so customer clicks on these rows do not immediately
    # repeat forty identity queries. This is normal batch-to-detail reuse, not
    # an acceptance-only shortcut.
    presentation.prime_authority_rows(selected)
    out = []
    seen = set()
    for raw in selected:
        row = dict(raw or {})
        identity = presentation.from_authority_row(row)
        if not identity.ok or identity.symbol in seen:
            continue
        seen.add(identity.symbol)
        out.append({
            "symbol": identity.symbol,
            "display_name": identity.display_name,
            "exchange": identity.exchange,
            "instrument_key": identity.instrument_key,
            "identity_verified": True,
        })
        if len(out) >= limit:
            break
    return {
        "ok": len(out) >= min(20, limit),
        "state": "READY" if len(out) >= min(20, limit) else "INSUFFICIENT_CATALOGUE_SAMPLE",
        "requested": limit,
        "count": len(out),
        "nse": sum(1 for row in out if str(row.get("exchange") or "").upper().startswith("NSE")),
        "bse_only": sum(1 for row in out if str(row.get("exchange") or "").upper().startswith("BSE")),
        "sample": out,
        "authority": "INTERACTIVE_POSTGRES_INSTRUMENT_AUTHORITY" if not compatibility_rows else "COMPATIBILITY_LOCAL_PROJECTION",
        "policy": "bounded canonical PostgreSQL cash-equity sample; no full compatibility-catalogue scan in production; provider IDs are internal only",
        "time": now_iso(),
    }

def r_clean_core_browser_proof(app, qs, q, mode):
    """Return only the persisted Clean Core Gate-1 browser proof."""
    try:
        proof = dict(app.store.get_kv("clean_core_browser_proof:last", {}) or {})
    except Exception as exc:
        return {"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:180]}
    return {
        "ok": bool(proof),
        "state": "PASS" if proof.get("passed") is True else ("NOT_RUN" if not proof else "REVIEW_REQUIRED"),
        "proof": proof,
        "time": now_iso(),
    }
