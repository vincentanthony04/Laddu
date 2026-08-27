"""Project Laddu GET handlers: Model Paper, accuracy and performance evidence."""
from __future__ import annotations

from routes_get_dependencies import *


def r_model_portfolio(app, qs, q, mode):
    """Read-only projection of the governed Model Paper / Research books.

    R49 contract: canonical Final positions and persisted Research publications
    must remain independently visible.  Auxiliary performance/diagnostic paths
    are opt-in and can never turn the whole book into UNAVAILABLE.
    """
    detail = str(qs.get("detail", ["core"])[0] or "core").lower().strip()
    include_aux = detail in {"extended", "diagnostic", "all"}
    try:
        settings_state = {"state": "READY", "error": None}
        try:
            settings = app.operator_capital_settings.read() if hasattr(app, "operator_capital_settings") else {
                "model_wallet": TRADING_CAPITAL,
                "intraday_exposure_ceiling": 100_000.0,
            }
        except Exception as exc:
            settings_state = {"state": "FALLBACK", "error": str(exc)[:240]}
            settings = {
                "model_wallet": TRADING_CAPITAL,
                "intraday_exposure_ceiling": 100_000.0,
            }
        payload = PortfolioWorkspaceService(
            app.store,
            equity=float(settings.get("model_wallet") or TRADING_CAPITAL),
            intraday_cap=float(settings.get("intraday_exposure_ceiling") or 100_000.0),
            portfolio_service=getattr(app, "model_portfolio", None),
            repository=getattr(app, "model_portfolio_repository", None),
        ).build(include_aux=include_aux, research_limit=1000)
        payload.setdefault("sections", {})["operator_settings"] = settings_state
        payload["requested_detail"] = "EXTENDED" if include_aux else "CORE"
        if payload.get("state") == "UNAVAILABLE":
            return (payload, 503)
        return payload
    except Exception as exc:
        return ({
            "ok": False,
            "state": "UNAVAILABLE",
            "error": str(exc)[:240],
            "read_contract": "INDEPENDENT_CANONICAL_BOOKS_AUXILIARY_FAIL_ISOLATED",
            "execution_boundary": "AUTOMATIC_PAPER_SIMULATION_NO_BROKER_ORDERS",
        }, 503)


def r_operator_settings(app, qs, q, mode):
    service = getattr(app, "operator_capital_settings", None) or OperatorCapitalSettingsService(
        app.store, default_wallet=TRADING_CAPITAL, default_intraday_cap=100_000.0
    )
    settings = service.read()
    return {
        "ok": True,
        "settings": settings,
        "locked": {
            "operating_mode": "AUTOMATIC_MODEL_PAPER_ONLY",
            "broker_authority": "NONE",
            "universe": "NSE-first · BSE-only fallback",
        },
        "message": "Capital edits apply only to future Model Paper admissions; open positions are never resized.",
        "time": now_iso(),
    }


def r_daily_performance(app, qs, q, mode):
    from core.india_time import trading_date_ist
    start = str(qs.get("start", [""])[0] or "")
    end = str(qs.get("end", [""])[0] or "")
    rows = app.store.daily_performance(start, end)
    return {
        "ok": True,
        "authoritative": True,
        "authority_scope": "SIGNAL_ACCURACY_POINTS_ONLY",
        "currency_performance_authoritative": False,
        "units": "PRICE_POINTS",
        "trading_date": trading_date_ist(),
        "start": start,
        "end": end,
        "daily_performance": rows,
        "policy": "This compatibility endpoint is signal-outcome/price-point evidence only. Governed rupee performance comes only from Model Paper settlement economics.",
    }


def r_trade_journal(app, qs, q, mode):
    rows = app.store.trade_journal(
        limit=_qint(qs, "limit", 300, min_val=1, max_val=5000), mode=mode,
        start_date=str(qs.get("start", [""])[0] or ""),
        end_date=str(qs.get("end", [""])[0] or ""),
        month=str(qs.get("month", [""])[0] or ""),
        year=str(qs.get("year", [""])[0] or ""),
        outcome=str(qs.get("outcome", [""])[0] or ""),
    )
    from core.india_time import trading_date_ist
    return {
        "ok": True,
        "authoritative": True,
        "authority_scope": "CANONICAL_SIGNAL_HISTORY",
        "currency_performance_authoritative": False,
        "trading_date": trading_date_ist(),
        "trade_journal": rows,
    }


def r_signal_accuracy_export(app, qs, q, mode):
    """Export reproducible Final Signal Accuracy / Performance lineage (AC-073).

    Production prefers the same canonical PostgreSQL settled-attribution rows
    consumed by Performance and governed Learning.  Research/counterfactual
    economics are never merged into this realized Final export.
    """
    import json
    from core.decision_lifecycle_read_model_service import DecisionLifecycleReadModelService
    selected_mode = mode if mode in {"delivery", "intraday"} else "all"
    limit = _qint(qs, "limit", 5000, min_val=1, max_val=10000)
    lifecycle_service = DecisionLifecycleReadModelService(app)
    repository = getattr(app, "model_portfolio_repository", None)

    def _mapping(value):
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
                return dict(parsed) if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    def _candidate_id(row):
        for source in (row, _mapping(row.get("candidate_snapshot")), _mapping(row.get("latest_payload"))):
            value = source.get("candidate_id") or source.get("research_candidate_id") or source.get("origin_candidate_id")
            if value:
                return str(value)
        return None

    def _path_summary(path, event_type):
        if not isinstance(path, list):
            return None
        values = []
        for event in path:
            if not isinstance(event, dict) or str(event.get("event_type") or "").upper() != event_type:
                continue
            action = str(event.get("action") or event.get("thesis_state") or "").strip()
            reason = str(event.get("reason") or "").strip()
            stamp = str(event.get("occurred_at") or "").strip()
            token = " | ".join(part for part in (stamp, action, reason) if part)
            if token:
                values.append(token)
        return " || ".join(values) if values else None

    rows = []
    source_authority = "POSTGRESQL_CANONICAL_DECISIONS"
    if repository is not None and callable(getattr(repository, "settled_learning_rows", None)):
        source_authority = "POSTGRESQL_MODEL_PAPER_SETTLED_ATTRIBUTION"
        try:
            raw_rows = repository.settled_learning_rows(limit=limit)
        except Exception as exc:
            return ({
                "ok": False, "state": "UNAVAILABLE", "authority": source_authority,
                "error": str(exc)[:300], "fallback_used": False,
                "policy": "Final performance export cannot substitute empty rows or another authority when PostgreSQL settled attribution is unavailable",
            }, 503)
        for raw in raw_rows:
            source = dict(raw or {})
            source.setdefault("entry", source.get("original_entry") if source.get("original_entry") is not None else source.get("entry_price"))
            source.setdefault("target", source.get("original_target"))
            source.setdefault("stop", source.get("original_stop"))
            source.setdefault("costs", source.get("total_cost"))
            source.setdefault("settlement_id", source.get("position_id"))
            row = lifecycle_service._classify(source)
            if selected_mode != "all" and str(row.get("mode") or "").lower() != selected_mode:
                continue
            if row.get("accuracy_eligible") is not True:
                continue
            path = row.get("lifecycle_action_path") if isinstance(row.get("lifecycle_action_path"), list) else []
            row.update({
                "candidate_id": _candidate_id(row),
                "reassessment_summary": _path_summary(path, "REASSESSED"),
                "management_summary": _path_summary(path, "MANAGED"),
                "market_regime": next((event.get("regime") for event in reversed(path) if isinstance(event, dict) and event.get("regime")), None),
                "publication_authority": "MODEL_PAPER",
                "entry_unit": "INR_PER_SHARE",
                "target_unit": "INR_PER_SHARE",
                "stop_unit": "INR_PER_SHARE",
                "exit_unit": "INR_PER_SHARE",
                "gross_pnl_unit": "INR_REALIZED",
                "costs_unit": "INR_EXECUTION_COSTS",
                "net_pnl_unit": "INR_REALIZED_NET_OF_EXECUTION_COSTS",
                "initial_risk_unit": "INR",
                "mfe_unit": "R_MULTIPLE",
                "mae_unit": "R_MULTIPLE",
                "realized_r_unit": "R_MULTIPLE_NET_PNL_OVER_INITIAL_RISK",
                "holding_duration_unit": "MINUTES",
                "signal_age_unit": "SECONDS",
                "export_lineage_contract": "signal-accuracy-export-2.0.0-ac073",
            })
            rows.append(row)
    else:
        lifecycle = lifecycle_service.status(mode=selected_mode, limit=limit)
        if lifecycle.get("ok") is not True:
            return ({
                "ok": False, "state": "UNAVAILABLE",
                "authority": lifecycle.get("authority") or "POSTGRESQL_CANONICAL_DECISIONS",
                "error": lifecycle.get("error") or lifecycle.get("state"),
                "fallback_used": False,
                "policy": "Signal Accuracy export fails closed when canonical lifecycle authority is unavailable",
            }, 503)
        rows = [row for row in list(lifecycle.get("records") or []) if row.get("accuracy_eligible") is True]

    columns = [
        "settlement_id", "decision_id", "source_signal_id", "candidate_id", "trading_date", "symbol", "exchange", "mode", "side",
        "entry", "entry_unit", "target", "target_unit", "stop", "stop_unit", "exit", "exit_unit", "exit_reason",
        "signal_outcome", "economic_outcome", "outcome_taxonomy_version", "lifecycle_status", "gross_pnl", "gross_pnl_unit",
        "costs", "costs_unit", "net_pnl", "net_pnl_unit", "initial_risk", "initial_risk_unit", "mfe_r", "mfe_unit", "mae_r", "mae_unit",
        "realized_r", "realized_r_unit", "holding_minutes", "holding_duration_unit", "generated_at", "opened_at", "closed_at",
        "generation_age_seconds", "open_age_seconds", "decision_delay_seconds", "signal_age_unit", "generation_age_bucket", "open_age_bucket",
        "decision_delay_bucket", "age_bucket_policy_version", "exit_reason", "reassessment_summary", "management_summary", "market_regime",
        "model_version", "policy_version", "feature_manifest_hash", "evidence_snapshot_id", "evidence_hash", "publication_authority",
        "export_lineage_contract",
    ]
    # Keep column order unique while retaining the explicit contract above.
    columns = list(dict.fromkeys(columns))
    return {
        "_response": "csv", "rows": rows, "columns": columns,
        "filename": "project-laddu-canonical-signal-accuracy.csv",
        "authority": source_authority,
        "accuracy_policy": "SUCCESS+FAILURE only; NEUTRAL/UNSCORABLE excluded; exact Model Paper settlement lineage and complete geometry (entry+target+stop+exit) required",
        "export_contract": "signal-accuracy-export-2.0.0-ac073",
        "broker_authority": "NONE",
    }


def r_journal_summary(app, qs, q, mode):
    """Cache-only Performance/Accuracy detail projection.

    Heavy lifecycle, journal and settlement joins are materialized on the
    bounded background repair lane; an HTTP request never owns that work.
    """
    from core.materialized_performance_snapshot_service import MaterializedPerformanceSnapshotService
    payload = MaterializedPerformanceSnapshotService(app).read(
        mode=mode if mode in {"delivery", "intraday"} else "all",
        start=str(qs.get("start", [""])[0] or ""),
        end=str(qs.get("end", [""])[0] or ""),
    )
    return (payload, 503) if payload.get("state") == "UNAVAILABLE" else payload
