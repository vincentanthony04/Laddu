"""
POST route table for Project Laddu. Same extraction rules as routes_get.py:
zero logic changes to the underlying store/service calls, just moved out of
main.py's do_POST if/elif chain.

v59: each handler now declares its schema via @validate(...) (see
validation.py) instead of hand-rolled `if not data.get(...)` checks. This
is a superset of what each handler checked before -- e.g. r_trade_update
previously did `int(data.get("id") or 0)` which silently coerced a bad
string to 0 and then failed the truthiness check; it now returns a clear
400 with field-level errors instead.

Each handler: (app, data: dict) -> dict | tuple[dict, int]
"""
from __future__ import annotations
from uuid import uuid4
from models import now_iso
from core.walk_forward_validation_service import WalkForwardValidationService
from core.ai_governance_service import AIGovernanceService
from core.ai_training_publication_service import AITrainingPublicationService
from core.production_replay_service import ProductionReplayService
from core.production_risk_authority_service import ProductionRiskAuthorityService
from core.production_ranking_service import RANKING_VERSION, RANKING_CONTRACT_VERSION
from core.operational_evidence_integrity_service import attach_evidence_integrity
from core.nse_calibrated_challenger_service import NseCalibratedChallengerService, DEFAULT_HORIZON
from core.portfolio_candidate_assessment_service import PortfolioCandidateAssessmentService
from core.quant_research_orchestrator_service import QuantResearchOrchestratorService
from core.operator_capital_settings_service import OperatorCapitalSettingsService
from core.improvement_proposal_service import ImprovementProposalService
from core.priority_pipeline_service import PriorityPipelineService
from core.level5_resilience_drill_service import Level5ResilienceDrillService
from core.research_lifecycle_advance_service import ResearchLifecycleAdvanceService
from config import TRADING_CAPITAL, APP_VERSION, BUILD_MARKER
from validation import Field, validate


@validate(q=Field(str, required=True, strip=True, min_len=1),
          mode=Field(str, default="intraday", strip=True, lower=True, choices=("intraday", "delivery")))
def r_search(app, data):
    result = app.search(data["q"], data["mode"])
    try:
        from core.canonical_presentation_service import CanonicalPresentationService
        rows = (result or {}).get("matches") if isinstance(result, dict) else []
        CanonicalPresentationService(app.store).prime_authority_rows(rows or [])
    except Exception:
        pass
    return result


def r_refresh(app, data):
    request = app.scan_orchestration.request_scan("intraday")
    return {"ok": True, "message": "Intraday refresh requested", "request": request, "time": now_iso()}


def r_deep_scan(app, data):
    request = app.scan_orchestration.request_scan("delivery")
    return {"ok": True, "message": "Delivery scan requested", "request": request, "time": now_iso()}


@validate(cohort_size=Field(int, default=96, min_val=24, max_val=150),
          batch_size=Field(int, default=12, min_val=4, max_val=24))
def r_first_useful_mode_run(app, data):
    return app.first_useful_mode.activate(
        cohort_size=data["cohort_size"], batch_size=data["batch_size"]
    )


@validate(symbol=Field(str, required=True, strip=True, min_len=1),
          mode=Field(str, default="all", strip=True, lower=True))
def r_watch_remove(app, data):
    removed = app.store.remove_manual_watch(data["symbol"], data["mode"])
    return {"ok": True, "removed": removed, "symbol": data["symbol"], "mode": data["mode"], "time": now_iso()}


@validate(symbol=Field(str, required=True, strip=True, min_len=1),
          mode=Field(str, default="delivery", strip=True, lower=True, choices=("intraday", "delivery")),
          pinned=Field(bool, default=True))
def r_watch_pin(app, data):
    changed = app.store.pin_manual_watch(data["symbol"], data["mode"], data["pinned"])
    return {"ok": True, "changed": changed, "symbol": data["symbol"], "mode": data["mode"], "pinned": data["pinned"], "time": now_iso()}


@validate(keep_pinned=Field(bool, default=True))
def r_watch_clear(app, data):
    keep_pinned = data["keep_pinned"]
    removed = app.store.clear_manual_watch(keep_pinned=keep_pinned)
    app.event("INFO", "manual_watch", "Manual watch cleared", {"removed": removed, "keep_pinned": keep_pinned})
    msg = "Manual watch cleared" + ("; pinned rows preserved" if keep_pinned else "; all manual rows cleared")
    return {"ok": True, "removed": removed, "keep_pinned": keep_pinned, "message": msg, "time": now_iso()}


def r_priority_reset(app, data):
    removed = app.store.clear_priority_symbols()
    app.event("INFO", "priority", "Searched/priority queue reset", {"removed": removed})
    return {"ok": True, "removed": removed, "message": "Searched / priority queue reset; selected signals and manual watch preserved", "time": now_iso()}


def r_auth_test(app, data):
    return app.auth_test(force=True)


def r_institutional_flow_refresh(app, data):
    trade_date = str(data.get("trade_date") or "").strip() or None
    return app.reference_data.fetch_fii_dii_activity(trade_date)


@validate(symbol=Field(str, required=True, strip=True, min_len=1))
def r_trade_log(app, data):
    trade_id = app.store.log_trade(data)
    return {"ok": True, "id": trade_id, "time": now_iso()}


@validate(id=Field(int, required=True, min_val=1))
def r_trade_update(app, data):
    ok = app.store.update_trade(data["id"], data)
    return {"ok": ok, "id": data["id"], "time": now_iso()}


@validate(id=Field(int, required=True, min_val=1))
def r_trade_delete(app, data):
    ok = app.store.delete_trade(data["id"])
    return {"ok": ok, "id": data["id"], "time": now_iso()}


@validate(model_id=Field(str, required=True, strip=True, min_len=1),
          observations=Field(list, required=True, min_len=0),
          horizon_days=Field(int, default=10, min_val=1),
          min_train_days=Field(int, default=252, min_val=1),
          test_days=Field(int, default=63, min_val=1),
          max_folds=Field(int, default=8, min_val=1),
          min_samples=Field(int, default=100, min_val=1),
          profile=Field(str, default="research", strip=True, lower=True, choices=("research", "capital")),
          trial_count=Field(int, default=1, min_val=1),
          embargo_days=Field(int, default=0, min_val=0))
def r_validation_run(app, data):
    result = WalkForwardValidationService(app.store).validate(
        model_id=data["model_id"], observations=data["observations"],
        horizon_days=data["horizon_days"], min_train_days=data["min_train_days"],
        test_days=data["test_days"], purge_days=data.get("purge_days"),
        max_folds=data["max_folds"], min_samples=data["min_samples"],
        profile=data["profile"], trial_count=data["trial_count"], embargo_days=data["embargo_days"],
    )
    return {"ok": True, "validation": result}


@validate(cases=Field(list, required=True, min_len=1),
          mode=Field(str, required=True, strip=True, lower=True, choices=("intraday", "delivery")),
          trial_count=Field(int, default=1, min_val=1),
          horizon_days=Field(int, default=10, min_val=1),
          min_train_days=Field(int, default=252, min_val=1),
          test_days=Field(int, default=63, min_val=1),
          max_folds=Field(int, default=8, min_val=1),
          min_samples=Field(int, default=300, min_val=1),
          embargo_days=Field(int, default=0, min_val=0))
def r_production_replay(app, data):
    service = ProductionReplayService()
    replay = service.replay(data["cases"], mode=data["mode"])
    validation = WalkForwardValidationService(app.store).validate_capital(
        replay["model_id"], replay["observations"], trial_count=data["trial_count"],
        horizon_days=data["horizon_days"], min_train_days=data["min_train_days"],
        test_days=data["test_days"], max_folds=data["max_folds"],
        min_samples=data["min_samples"], embargo_days=data["embargo_days"],
    )
    return {"ok": True, "replay": replay, "validation": validation}





@validate(enabled=Field(bool, required=True),
          reason=Field(str, default="", strip=True, max_len=500),
          actor=Field(str, default="operator", strip=True, min_len=1, max_len=120))
def r_risk_operator_stop(app, data):
    try:
        status = ProductionRiskAuthorityService(app.store, runtime_status=app.status).set_operator_stop(
            data["enabled"], data["reason"], data["actor"]
        )
        app.event("WARN" if data["enabled"] else "INFO", "risk_authority", "Operator emergency stop changed", {
            "enabled": data["enabled"], "reason": data["reason"], "actor": data["actor"]
        })
        return {"ok": True, "risk_authority": status}
    except ValueError as exc:
        return ({"ok": False, "error": str(exc)}, 400)


@validate(daily_pnl=Field(float, required=False),
          equity=Field(float, required=False),
          actor=Field(str, default="operator", strip=True, min_len=1, max_len=120))
def r_risk_account_state(app, data):
    if data.get("daily_pnl") is None and data.get("equity") is None:
        return ({"ok": False, "error": "daily_pnl or equity is required"}, 400)
    status = ProductionRiskAuthorityService(app.store, runtime_status=app.status).update_account_state(
        daily_pnl=data.get("daily_pnl"), equity=data.get("equity"), actor=data["actor"]
    )
    return {"ok": True, "risk_authority": status}







@validate(candidate=Field(dict, required=True))
def r_portfolio_candidate_assessment(app, data):
    try:
        return PortfolioCandidateAssessmentService(app.store, runtime_status=getattr(app, "status", {})).assess(data["candidate"])
    except ValueError as exc:
        return ({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        return ({"ok": False, "state": "UNAVAILABLE", "error": str(exc)}, 503)


@validate(mode=Field(str, required=True, strip=True, lower=True, choices=("intraday", "delivery")),
          horizon=Field(str, required=False, strip=True, lower=True),
          min_train_days=Field(int, default=126, min_val=20),
          test_days=Field(int, default=21, min_val=5),
          purge_days=Field(int, required=False, min_val=0),
          embargo_days=Field(int, default=1, min_val=0),
          max_folds=Field(int, default=8, min_val=1),
          trial_count=Field(int, default=1, min_val=1))
def r_calibrated_challenger_train(app, data):
    mode = data["mode"]
    try:
        return NseCalibratedChallengerService(app.store).train(
            mode=mode, horizon=data.get("horizon") or DEFAULT_HORIZON[mode],
            min_train_days=data["min_train_days"], test_days=data["test_days"],
            purge_days=data.get("purge_days"), embargo_days=data["embargo_days"],
            max_folds=data["max_folds"], trial_count=data["trial_count"],
        )
    except ValueError as exc:
        return ({"ok": False, "error": str(exc)}, 400)


@validate(mode=Field(str, required=True, strip=True, lower=True, choices=("intraday", "delivery")),
          trigger=Field(str, default="manual-model-review", strip=True, min_len=1),
          min_train_days=Field(int, default=126, min_val=20),
          test_days=Field(int, default=21, min_val=5),
          embargo_days=Field(int, default=1, min_val=0),
          max_folds=Field(int, default=8, min_val=1),
          trial_count=Field(int, default=1, min_val=1))
def r_quant_shadow_cycle(app, data):
    try:
        return QuantResearchOrchestratorService(app.store).run_cycle(
            mode=data["mode"],
            trigger=data["trigger"],
            min_train_days=data["min_train_days"],
            test_days=data["test_days"],
            embargo_days=data["embargo_days"],
            max_folds=data["max_folds"],
            trial_count=data["trial_count"],
        )
    except ValueError as exc:
        return ({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        return ({
            "ok": False,
            "state": "UNAVAILABLE",
            "error": str(exc),
            "production_change_allowed": False,
        }, 503)


def r_ai_model_register(app, data):
    try:
        return {"ok": True, "model": AIGovernanceService(app.store).register_model(data)}
    except (TypeError, ValueError) as exc:
        return ({"ok": False, "error": str(exc)}, 400)



def r_ai_training_publication(app, data):
    try:
        return AITrainingPublicationService(app.store).publish(data)
    except (TypeError, ValueError) as exc:
        return ({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        return ({"ok": False, "state": "PUBLICATION_FAILED", "error": str(exc)}, 503)

def r_ai_prediction(app, data):
    try:
        return {"ok": True, "prediction": AIGovernanceService(app.store).record_prediction(data)}
    except (TypeError, ValueError) as exc:
        return ({"ok": False, "error": str(exc)}, 400)


@validate(model_wallet=Field(float, required=True, min_val=10000, max_val=1000000000),
          intraday_exposure_ceiling=Field(float, required=True, min_val=0, max_val=1000000000),
          actor=Field(str, default="operator", strip=True, min_len=1, max_len=120))
def r_operator_settings_update(app, data):
    service = getattr(app, "operator_capital_settings", None) or OperatorCapitalSettingsService(
        app.store, default_wallet=TRADING_CAPITAL, default_intraday_cap=100_000.0
    )
    try:
        settings = service.update(
            model_wallet=data["model_wallet"],
            intraday_exposure_ceiling=data["intraday_exposure_ceiling"],
            actor=data["actor"],
        )
        if hasattr(app, "apply_operator_capital_settings"):
            app.apply_operator_capital_settings(settings)
        return {
            "ok": True,
            "settings": settings,
            "message": "Saved. New values apply to future Model Paper admissions; open positions were not resized.",
            "time": now_iso(),
        }
    except ValueError as exc:
        return ({"ok": False, "error": str(exc)}, 400)


@validate(symbol=Field(str, required=True, strip=True, min_len=1),
          interval=Field(str, default="day", strip=True, min_len=1),
          action=Field(str, default="priority_sync", strip=True, lower=True, choices=("refresh_view", "priority_sync", "repair_gaps", "recalculate", "reassess")),
          mode=Field(str, default="delivery", strip=True, lower=True, choices=("intraday", "delivery")),
          days=Field(int, required=False, min_val=1, max_val=5000))
def r_priority_sync(app, data):
    """Bounded operator priority request using the normal authoritative queues."""
    symbol = data["symbol"].upper()
    interval = data["interval"]
    action = data["action"]
    mode = data["mode"]
    result = {
        "ok": True, "symbol": symbol, "interval": interval, "action": action,
        "mode": mode, "time": now_iso(), "bypasses_safety_gates": False,
        "history_preserved": True, "queue": "AUTHORITATIVE_DEDUPLICATED_PRIORITY",
    }
    if action == "refresh_view":
        result.update({"state": "VIEW_REFRESH", "scheduled": False, "message": "Current server projection should be reread; no provider request was made."})
        return result

    try:
        inst = app._index_instrument_for_chart(symbol) or app._first_instrument(symbol, force_refresh=False)
    except Exception:
        inst = None
    if not inst or not inst.get("instrument_key"):
        return ({"ok": False, "state": "IDENTITY_UNRESOLVED", "symbol": symbol, "error": "Verified instrument identity is required."}, 404)

    pipeline_authority = getattr(app, "priority_pipeline", None) or PriorityPipelineService(app)
    pipeline_accepted = False
    try:
        result["pipeline"] = pipeline_authority.queue(
            symbol=symbol, instrument_key=str(inst.get("instrument_key") or ""),
            mode=mode, action=action,
        )
        pipeline_accepted = bool((result.get("pipeline") or {}).get("ok") is not False)
    except Exception as exc:
        result["pipeline"] = {"state": "PIPELINE_AUTHORITY_UNAVAILABLE", "error": str(exc)[:240]}

    try:
        priority_reason = "operator_reassessment" if action == "reassess" else "operator_priority_sync"
        app.store.add_priority(symbol, str(inst.get("exchange") or "NSE"), mode, priority_reason)
    except Exception:
        pass

    scheduled = None
    if action in {"priority_sync", "repair_gaps", "recalculate", "reassess"}:
        # The durable PriorityPipeline job above is the HTTP acceptance boundary.
        # Candidate-10 proved that synchronously inspecting/scheduling every base
        # interval can exceed the 30-second operator request timeout even though
        # exact-gap workers are healthy. Dispatch that bounded inspection in the
        # background; pipeline/status endpoints expose subsequent progress.
        if not pipeline_accepted:
            scheduled = {
                "ok": False, "accepted": False, "state": "DURABLE_PIPELINE_NOT_ACCEPTED",
                "error": "Exact-gap dispatch withheld because the durable priority job was not accepted.",
            }
        else:
            try:
                scheduled = pipeline_authority.dispatch_history_schedule(
                    symbol=symbol, mode=mode, selected_interval=interval, action=action,
                )
            except Exception as exc:
                scheduled = {"ok": False, "accepted": False, "state": "DISPATCH_FAILED", "error": str(exc)[:180]}
        result["historical"] = scheduled

    if action in {"recalculate", "reassess"}:
        try:
            app.market_data._invalidate_mtf_cache(str(inst.get("instrument_key")))
            result["mtf_cache_invalidated"] = True
        except Exception:
            result["mtf_cache_invalidated"] = False

    if action == "reassess":
        reassessment_id = f"reassess:{mode}:{symbol}:{uuid4().hex[:12]}"
        result["reassessment_id"] = reassessment_id
        result["model_ranking_contract"] = {
            "consumer": "REASSESSMENT_SCANNER",
            "same_canonical_ranker_as_today_entries": True,
            "shadow_calculation_recorded": True,
            "influence_policy": "GOVERNED_ACTIVE_CAP_15_WITH_DETERMINISTIC_FALLBACK",
            "ranking_version": RANKING_VERSION,
            "ranking_contract_version": RANKING_CONTRACT_VERSION,
            "expected_trace": "ranking_input_hash + ranking_result_hash + ranking_trace_id",
        }
        request_record = {
            "reassessment_id": reassessment_id,
            "symbol": symbol,
            "instrument_key": str(inst.get("instrument_key") or ""),
            "mode": mode,
            "requested_at": result["time"],
            "ranking_version": RANKING_VERSION,
            "ranking_contract_version": RANKING_CONTRACT_VERSION,
            "state": "QUEUED",
        }
        try:
            app.store.set_kv(f"reassessment_request:{mode}:{symbol}", request_record)
            app.store.set_kv("reassessment_request:last", request_record)
            result["reassessment_evidence_persisted"] = True
        except Exception as exc:
            result["reassessment_evidence_persisted"] = False
            result["reassessment_evidence_error"] = str(exc)[:160]
        try:
            result["scan_request"] = app.scan_orchestration.request_scan(mode)
        except Exception as exc:
            result["scan_request"] = {"accepted": False, "error": str(exc)[:180]}

    accepted = bool((scheduled or {}).get("accepted") or (scheduled or {}).get("state") in {"QUEUED", "COALESCED"})
    result["state"] = "QUEUED" if accepted else "BLOCKED"
    result["scheduled"] = accepted
    result["message"] = "Priority request was durably queued; exact-gap inspection continues in the normal background pipeline and stored history was not cleared."
    return result


def r_market_soak_proof(app, data):
    """Persist target-machine market-hours/restart evidence without granting authority.

    The collector submits bounded observations. This endpoint is deliberately
    fail-closed: source validation, a closed-market run or a partial soak cannot
    self-certify Level 4. No trade, model assignment or scanner cursor is changed.
    """
    payload = dict(data or {}) if isinstance(data, dict) else {}
    checks = dict(payload.get("checks") or {}) if isinstance(payload.get("checks"), dict) else {}
    try:
        duration_seconds = int(payload.get("duration_seconds") or 0)
        sample_count = int(payload.get("sample_count") or 0)
        market_open_samples = int(payload.get("market_open_samples") or 0)
    except (TypeError, ValueError):
        duration_seconds = sample_count = market_open_samples = 0
    required_checks = (
        "service_continuity",
        "scanner_progress_observed",
        "canonical_ranking_trace_observed",
        "restart_recovery_passed",
        "risk_monitor_ready",
        "intraday_lifecycle_ready",
        "post_close_intraday_flatten_verified",
        "decision_surface_reconciliation_passed",
        "model_learning_audit_passed",
    )
    passed = bool(
        payload.get("build") == APP_VERSION
        and duration_seconds >= 900
        and sample_count >= 10
        and market_open_samples >= 5
        and all(checks.get(name) is True for name in required_checks)
    )
    record = {
        "build": payload.get("build"),
        "started_at": payload.get("started_at"),
        "completed_at": payload.get("completed_at") or now_iso(),
        "received_at": now_iso(),
        "duration_seconds": duration_seconds,
        "sample_count": sample_count,
        "market_open_samples": market_open_samples,
        "checks": checks,
        "required_checks": list(required_checks),
        "passed": passed,
        "samples_digest": str(payload.get("samples_digest") or "")[:128],
        "evidence_path": str(payload.get("evidence_path") or "")[:500],
        "authority": "TARGET_MACHINE_LEVEL4_SOAK",
        "production_change_allowed": False,
    }
    try:
        record = attach_evidence_integrity(
            app.store, "MARKET_HOURS_SOAK", record,
            source_key="level4_market_soak:last",
        )
        app.store.set_kv("level4_market_soak:last", record)
    except Exception as exc:
        return ({"ok": False, "state": "PERSIST_FAILED", "error": str(exc)[:180]}, 503)
    return {
        "ok": passed,
        "state": "RECORDED" if passed else "PENDING_REQUIRED_EVIDENCE",
        "passed": passed,
        "missing_checks": [name for name in required_checks if checks.get(name) is not True],
        "minimums": {"duration_seconds": 900, "sample_count": 10, "market_open_samples": 5},
        "time": record["received_at"],
    }


def r_browser_proof(app, data):
    """Persist a local browser self-check as Level-4 evidence only.

    The proof cannot promote a trade or a model. It is build-bound and is
    rejected unless every submitted self-check passed.
    """
    proof = dict(data.get("proof") or {}) if isinstance(data, dict) else {}
    checks = list(data.get("checks") or []) if isinstance(data, dict) else []
    passed = bool(
        proof.get("build") == APP_VERSION
        and proof.get("build_marker") == BUILD_MARKER
        and checks
        and all(isinstance(row, dict) and row.get("ok") is True for row in checks)
    )
    record = {
        "build": proof.get("build"),
        "build_marker": proof.get("build_marker"),
        "captured_at": proof.get("captured_at") or now_iso(),
        "received_at": now_iso(),
        "passed": passed,
        "checks": checks,
        "proof": proof,
        "authority": "LOCAL_BROWSER_SELF_CHECK",
        "production_change_allowed": False,
    }
    try:
        record = attach_evidence_integrity(
            app.store, "BROWSER_SELF_CHECK", record,
            source_key="level4_browser_proof:last",
        )
        app.store.set_kv("level4_browser_proof:last", record)
    except Exception as exc:
        return ({"ok": False, "state": "PERSIST_FAILED", "error": str(exc)[:180]}, 503)
    return {
        "ok": passed,
        "state": "RECORDED" if passed else "REVIEW_REQUIRED",
        "passed": passed,
        "build": proof.get("build"),
        "build_marker": proof.get("build_marker"),
        "expected_build_marker": BUILD_MARKER,
        "time": record["received_at"],
    }


@validate(mode=Field(str, required=True, strip=True, lower=True, choices=("intraday", "delivery")),
          horizon=Field(str, required=True, strip=True, lower=True, min_len=1),
          actor=Field(str, default="local_operator", strip=True, min_len=1))
def r_improvement_proposal_create(app, data):
    try:
        return ImprovementProposalService(app.store).create(
            mode=data["mode"], horizon=data["horizon"], actor=data["actor"]
        )
    except ValueError as exc:
        return ({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        return ({"ok": False, "state": "CREATE_FAILED", "error": str(exc),
                 "production_influence": 0.0, "broker_authority": "NONE"}, 503)


@validate(proposal_id=Field(str, required=True, strip=True, min_len=1),
          action=Field(str, required=True, strip=True, upper=True,
                       choices=("APPROVE_RESEARCH", "APPROVE_CHALLENGER", "REJECT", "QUARANTINE")),
          actor=Field(str, required=True, strip=True, min_len=1),
          reason=Field(str, required=True, strip=True, min_len=3))
def r_improvement_proposal_decision(app, data):
    try:
        return ImprovementProposalService(app.store).decide(
            proposal_id=data["proposal_id"], action=data["action"],
            actor=data["actor"], reason=data["reason"],
        )
    except KeyError as exc:
        return ({"ok": False, "error": str(exc)}, 404)
    except ValueError as exc:
        return ({"ok": False, "error": str(exc)}, 409)
    except Exception as exc:
        return ({"ok": False, "state": "DECISION_FAILED", "error": str(exc),
                 "production_influence": 0.0, "broker_authority": "NONE"}, 503)


def r_improvement_proposal_reconcile(app, data):
    try:
        return ImprovementProposalService(app.store).reconcile()
    except Exception as exc:
        return ({"ok": False, "state": "RECONCILE_FAILED", "error": str(exc),
                 "production_influence": 0.0, "broker_authority": "NONE"}, 503)


@validate(settlement_limit=Field(int, default=80, min_val=1, max_val=250))
def r_research_lifecycle_advance(app, data):
    try:
        result = ResearchLifecycleAdvanceService(app).run(
            settlement_limit=data["settlement_limit"]
        )
        return result if result.get("ok") else (result, 409)
    except Exception as exc:
        return ({
            "ok": False,
            "state": "RESEARCH_LIFECYCLE_ADVANCE_FAILED",
            "error": str(exc)[:500],
            "production_influence": 0.0,
            "broker_authority": "NONE",
        }, 503)


def r_level5_resilience_drill(app, data):
    try:
        return Level5ResilienceDrillService(app).run()
    except Exception as exc:
        return ({"ok": False, "state": "DRILL_FAILED", "error": str(exc)[:300], "production_change_allowed": False}, 503)


@validate(symbol=Field(str, required=True, strip=True, min_len=1),
          mode=Field(str, default="full", strip=True, lower=True, choices=("ltpc", "full", "full_d30")),
          ttl_seconds=Field(int, default=900, min_val=30, max_val=3600))
def r_live_subscription(app, data):
    try:
        return app.set_interactive_live_subscription(data["symbol"], data["mode"], data["ttl_seconds"])
    except ValueError as exc:
        return ({"ok": False, "error": str(exc)}, 400)



@validate(action=Field(str, required=True, strip=True, lower=True, min_len=1),
          action_id=Field(str, default="", strip=True, max_len=120),
          component=Field(str, default="", strip=True, max_len=120),
          symbol=Field(str, default="", strip=True, upper=True, max_len=80),
          mode=Field(str, default="delivery", strip=True, lower=True, choices=("intraday", "delivery")),
          interval=Field(str, default="", strip=True, max_len=40),
          reason=Field(str, default="operator_requested", strip=True, max_len=300),
          seconds=Field(int, default=120, min_val=5, max_val=900))
def r_operations_action(app, data):
    service = getattr(app, "operations_control", None)
    if service is None:
        return ({"ok": False, "state": "OPERATIONS_CONTROL_UNAVAILABLE"}, 503)
    result = service.execute(data)
    if result.get("ok"):
        return result
    state = str(result.get("state") or "")
    return (result, 400 if state in {"ACTION_NOT_ALLOWED", "ACTION_FAILED"} else 409)


def r_clean_core_browser_proof(app, data):
    """Persist exact-build Clean Core Gate-1 browser evidence only.

    This proof is intentionally separate from Level-4/Level-5 maturity evidence.
    It proves the core customer path: canonical stock identity, chart, MTF,
    support/resistance and selected StockSnapshot rendering.
    """
    proof = dict(data.get("proof") or {}) if isinstance(data, dict) else {}
    checks = list(data.get("checks") or []) if isinstance(data, dict) else []
    passed = bool(
        proof.get("build") == APP_VERSION
        and proof.get("build_marker") == BUILD_MARKER
        and checks
        and all(isinstance(row, dict) and row.get("ok") is True for row in checks)
    )
    record = {
        "build": proof.get("build"),
        "captured_at": proof.get("captured_at") or now_iso(),
        "received_at": now_iso(),
        "passed": passed,
        "checks": checks,
        "proof": proof,
        "authority": "CLEAN_CORE_GATE1_BROWSER_SELF_CHECK",
        "production_change_allowed": False,
        "broker_authority": "NONE",
    }
    try:
        app.store.set_kv("clean_core_browser_proof:last", record)
    except Exception as exc:
        return ({"ok": False, "state": "PERSIST_FAILED", "error": str(exc)[:180]}, 503)
    return {
        "ok": passed,
        "state": "RECORDED" if passed else "REVIEW_REQUIRED",
        "passed": passed,
        "build": proof.get("build"),
        "time": record["received_at"],
    }

ROUTES = {
    "/api/search": r_search,
    "/api/control-plane/action": r_operations_action,
    "/api/operations/action": r_operations_action,
    "/api/operator-settings": r_operator_settings_update, "/api/settings": r_operator_settings_update,
    "/api/live-market/subscriptions": r_live_subscription,
    "/api/improvement-proposals/create": r_improvement_proposal_create,
    "/api/improvement-proposals/decision": r_improvement_proposal_decision,
    "/api/improvement-proposals/reconcile": r_improvement_proposal_reconcile,
    "/api/research-lifecycle/advance": r_research_lifecycle_advance,
    "/api/priority-sync": r_priority_sync, "/api/sync": r_priority_sync, "/api/reassess": r_priority_sync,
    "/api/refresh": r_refresh,
    "/api/deep-scan": r_deep_scan,
    "/api/first-useful-mode/run": r_first_useful_mode_run,
    "/api/watch-queue/remove": r_watch_remove, "/api/manual-watch/remove": r_watch_remove,
    "/api/watch-queue/pin": r_watch_pin, "/api/manual-watch/pin": r_watch_pin,
    "/api/watch-queue/clear": r_watch_clear, "/api/manual-watch/clear": r_watch_clear,
    "/api/search-queue/reset": r_priority_reset, "/api/priority/reset": r_priority_reset,
    "/api/auth-test": r_auth_test,
    "/api/institutional-flow/refresh": r_institutional_flow_refresh,
    "/api/my-trades/log": r_trade_log,
    "/api/my-trades/update": r_trade_update,
    "/api/my-trades/delete": r_trade_delete,
    "/api/validation/run": r_validation_run,
    "/api/validation/production-replay": r_production_replay,
    "/api/validation/browser-proof": r_browser_proof,
    "/api/clean-core/browser-proof": r_clean_core_browser_proof,
    "/api/validation/market-soak-proof": r_market_soak_proof,
    "/api/validation/level5-resilience-drill": r_level5_resilience_drill,
    "/api/risk-authority/operator-stop": r_risk_operator_stop,
    "/api/risk-authority/account-state": r_risk_account_state,
    "/api/portfolio/candidate-assessment": r_portfolio_candidate_assessment,
    "/api/calibrated-challenger/train": r_calibrated_challenger_train,
    "/api/quant-edge/run-shadow-cycle": r_quant_shadow_cycle,
    "/api/ai/models/register": r_ai_model_register,
    "/api/ai/predictions": r_ai_prediction,
    "/api/ai/training-publication": r_ai_training_publication,
}
