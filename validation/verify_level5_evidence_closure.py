"""Deterministic R21 Level-5 evidence-closure contract proof.

This is a source/package guard only.  It deliberately does not claim installed,
live-market, historical-alpha, or elapsed-forward evidence.  Those belong to
RUN_LEVEL5_FINAL_MARKET_PROOF.cmd on the target Windows host.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.forward_horizon_policy import FORWARD_DURABILITY_TIERS, durability_status
from core.walk_forward_validation_service import WalkForwardValidationService

VERSION = "level5-evidence-closure-1.4.0-live-scanner-runtime-closure"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    checks: dict[str, bool] = {}
    evidence: dict[str, object] = {}

    tiers = [dict(row) for row in FORWARD_DURABILITY_TIERS]
    checks["forward_tiers_126_252_504_756"] = [int(x["minimum_trading_days"]) for x in tiers] == [126, 252, 504, 756]
    checks["forward_sample_depth_increases"] = all(
        int(b["minimum_settled_candidates"]) > int(a["minimum_settled_candidates"])
        for a, b in zip(tiers, tiers[1:])
    )
    deep = durability_status({
        "trading_days": 756, "settled_candidates": 2000,
        "complete_populations": 120, "regimes": ["BULL", "BEAR", "RANGE", "HIGH_VOL"],
    })
    checks["forward_evidence_never_stops_or_relabels_history"] = bool(
        deep.get("continuous_collection_required") is True
        and deep.get("historical_replay_never_counts_as_forward_time") is True
        and deep.get("achieved_tier") == "DEEP_CYCLE"
    )
    evidence["durability"] = deep

    base = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    valid_oof = {
        "oof_model_hash": hashlib.sha256(b"fold-model").hexdigest(),
        "oof_train_start": _iso(base),
        "oof_train_end": _iso(base + timedelta(days=90)),
        "oof_feature_cutoff": _iso(base + timedelta(days=90)),
        "oof_prediction_timestamp": _iso(base + timedelta(days=111)),
        "decision_as_of": _iso(base + timedelta(days=111, minutes=1)),
        "outcome_as_of": _iso(base + timedelta(days=121)),
        "oof_artifact_kind": "HISTORICAL_FOLD_MODEL_BINARY_SHA256",
    }
    oof = WalkForwardValidationService._oof_prediction_lineage([valid_oof], purge_days=10)
    checks["historical_oof_model_lineage_proven"] = oof.get("proven") is True
    contaminated_oof = dict(valid_oof)
    contaminated_oof["oof_train_end"] = _iso(base + timedelta(days=110))
    contaminated = WalkForwardValidationService._oof_prediction_lineage([contaminated_oof], purge_days=10)
    checks["historical_oof_purge_violation_rejected"] = contaminated.get("proven") is False
    evidence["oof"] = {"valid": oof, "contaminated": contaminated}

    prospective = {
        "prospective_prediction_hash": hashlib.sha256(b"immutable-prediction").hexdigest(),
        "prospective_prediction_key": "pred-1",
        "prospective_model_version": "hybrid-v1",
        "prospective_prediction_at": _iso(base + timedelta(days=1, minutes=1)),
        "decision_as_of": _iso(base + timedelta(days=1, minutes=2)),
        "feature_as_of": _iso(base + timedelta(days=1)),
        "outcome_as_of": _iso(base + timedelta(days=2)),
        "prospective_evidence_authority": "GOVERNANCE_POSTGRESQL_SELECTOR_EVIDENCE",
    }
    forward = WalkForwardValidationService._prospective_prediction_lineage([prospective])
    checks["prospective_immutable_prediction_lineage_proven"] = forward.get("proven") is True
    leaked = dict(prospective)
    leaked["prospective_prediction_at"] = _iso(base + timedelta(days=3))
    forward_bad = WalkForwardValidationService._prospective_prediction_lineage([leaked])
    checks["prospective_post_outcome_prediction_rejected"] = forward_bad.get("proven") is False
    evidence["prospective"] = {"valid": forward, "leaked": forward_bad}

    def weak_simulator(_):
        return {
            "initial_capital": 500000, "max_concurrent_positions": 10,
            "capital_constraints_enforced": True, "concurrency_constraints_enforced": True,
            "position_sizing_enforced": True, "mark_to_market_enforced": False, "no_leverage": True,
            "equity_curve": [
                {"timestamp": _iso(base), "equity": 500000},
                {"timestamp": _iso(base + timedelta(days=1)), "equity": 501000},
            ],
        }
    def strong_simulator(_):
        return {
            "initial_capital": 500000, "max_concurrent_positions": 10,
            "capital_constraints_enforced": True, "concurrency_constraints_enforced": True,
            "position_sizing_enforced": True, "mark_to_market_enforced": True, "no_leverage": True,
            "equity_curve": [
                {"timestamp": _iso(base), "equity": 500000},
                {"timestamp": _iso(base + timedelta(days=1)), "equity": 501000},
                {"timestamp": _iso(base + timedelta(days=2)), "equity": 502000},
            ],
        }
    weak_capital = WalkForwardValidationService._capital_simulation(weak_simulator, model_id="x", observations=[])
    strong_capital = WalkForwardValidationService._capital_simulation(strong_simulator, model_id="x", observations=[])
    checks["capital_gate_requires_mark_to_market"] = weak_capital.get("proven") is False and "MARK_TO_MARKET_ENFORCED_NOT_PROVEN" in (weak_capital.get("blockers") or [])
    checks["capital_gate_accepts_mtm_no_leverage_proof"] = strong_capital.get("proven") is True
    evidence["capital"] = {"weak": weak_capital, "strong": strong_capital}

    replay_source = (BACKEND / "core" / "selection_walk_forward_replay_service.py").read_text(encoding="utf-8")
    repo_source = (BACKEND / "core" / "data_plane" / "model_governance_repository.py").read_text(encoding="utf-8")
    corp_source = (BACKEND / "tools" / "reconcile_corporate_action_authority.py").read_text(encoding="utf-8")
    maturity_source = (BACKEND / "core" / "level5_forward_maturity_service.py").read_text(encoding="utf-8")
    trainer_source = (BACKEND / "tools" / "train_nse_smart_model.py").read_text(encoding="utf-8")
    installer_source = (ROOT / "installer" / "install.ps1").read_text(encoding="utf-8")

    checks["governance_selector_replay_is_primary"] = all(token in replay_source for token in (
        "selector_replay_rows", "GOVERNANCE_POSTGRESQL_SELECTOR_EVIDENCE", "CANONICAL_SELECTOR_EVIDENCE_NOT_MIGRATED"
    )) and "payload_sha256 AS prediction_payload_sha256" in repo_source
    checks["level5_service_exposes_durability"] = "forward_durability" in maturity_source and "durability_status" in maturity_source
    checks["corporate_action_reconciliation_is_fail_closed"] = all(token in corp_source for token in (
        "unresolved_structural_actions", "unresolved_symbols", "complete = symbol not in unresolved_symbols",
        "NSE_OFFICIAL_CONTENT_ADDRESSED_RANGE", "inference_from_price_jump_allowed\": False",
        "price_factor", "volume_factor"
    ))
    checks["historical_capital_wfa_uses_real_oof_and_mtm_simulator"] = all(token in trainer_source for token in (
        "HISTORICAL_FOLD_MODEL_BINARY_SHA256", "delivery_capital_portfolio_simulator", "mark_to_market_enforced", "no_leverage"
    ))
    checks["clean_install_ships_training_entrypoint"] = "'train_ai_model.ps1'" in installer_source.split("$DiscoveredOperatorFiles", 1)[0]
    checks["exact_installed_package_binding_shipped"] = (ROOT / "validation" / "verify_installed_package_binding.py").is_file()
    final_market_source = (ROOT / "validation" / "run_level5_final_market_proof.ps1").read_text(encoding="utf-8")
    checks["final_market_proof_hard_gates_both_scanner_and_intraday_capital_wfa"] = all(token in final_market_source for token in (
        "LIVE_MARKET_SCANNER_OPERABILITY", "--require-market-open",
        "INTRADAY_HISTORICAL_CAPITAL_WFA", "profile=capital",
        "same_candidate_population_across_arms", "REAL_FORWARD_LEVEL5_MATURITY",
    ))
    package_integrity_source = (ROOT / "validation" / "package_integrity.py").read_text(encoding="utf-8")
    deploy_validator_source = (ROOT / "validation" / "validate_deployable_candidate.py").read_text(encoding="utf-8")
    checks["source_attestation_recomputed_inside_sealed_package"] = all(token in package_integrity_source for token in (
        "def eligible_source_files(root: Path)", "def digest_material(files: Iterable[Path], root: Path)",
        "SOURCE_METADATA_EXCLUSIONS",
    )) and "verify_source_tree=True" in deploy_validator_source

    runtime_lifecycle_source = (BACKEND / "runtime_lifecycle.py").read_text(encoding="utf-8")
    desk_runtime_source = (BACKEND / "core" / "desk_runtime_authority.py").read_text(encoding="utf-8")
    checks["full_canonical_universe_can_reach_operational_startup"] = (
        "0 < len(delivery) <= canonical_count" in runtime_lifecycle_source
        and "0 < len(intraday) <= canonical_count" in runtime_lifecycle_source
        and "0 < len(delivery) <= 1500" not in runtime_lifecycle_source
    )
    checks["scanner_cadence_uses_canonical_market_clock"] = (
        "from core.market_clock import is_india_market_open" in desk_runtime_source
        and "market_open = bool(is_india_market_open())" in desk_runtime_source
        and 'getattr(self.host, "market_open", None)' not in desk_runtime_source
    )

    # Market-hours acceptance must reject the exact class of failure where the UI/API
    # shell is healthy but scanners have no quote-ready/deep-math progression.
    validation_dir = ROOT / "validation"
    if str(validation_dir) not in sys.path:
        sys.path.insert(0, str(validation_dir))
    from verify_live_scanner_operability import evaluate as evaluate_live_scanner
    good_mode = {
        "state": "waiting_next_cycle",
        "analysis": {
            "state": "waiting_next_cycle",
            "last_completed": {
                "attempted": 24, "quote_ready": 22, "scanned": 8,
                "promoted": 0, "rejected": 8,
                "resolution_summary": {"analysed": 8, "mathematically_rejected": 8},
                "completed_at": _iso(base + timedelta(minutes=1)),
            },
            "stage_members": {
                "mathematically_rejected": [{"state": "MATHEMATICALLY_REJECTED", "reason": "NO_QUALIFIED_DECISION"}]
            },
        },
        "progress_contract": {"population_count": 4095, "current_sweep_scanned": 24},
    }
    good_scanner = {
        "market_open": True, "token": {"ok": True}, "instruments": {"universe_count": 4365},
        "scanner": {"mode_scanners": {"intraday": dict(good_mode), "delivery": dict(good_mode)}},
    }
    good_pipeline = {"fast_lane": {"diagnosis": "no_qualifying_setups", "scanned": 8, "promoted": 0, "below_threshold": 8}}
    blank_mode = {
        "state": "waiting_next_cycle",
        "analysis": {"state": "waiting_next_cycle", "cycle_attempted": 0, "cycle_quote_ready": 0, "cycle_scanned": 0},
        "progress_contract": {"population_count": 4095, "current_sweep_scanned": 0},
    }
    blank_scanner = {
        "market_open": True, "token": {"ok": True}, "instruments": {"universe_count": 4365},
        "scanner": {"mode_scanners": {"intraday": dict(blank_mode), "delivery": dict(blank_mode)}},
    }
    blank_pipeline = {"fast_lane": {"diagnosis": "warming", "scanned": 0, "promoted": 0, "data_missing": 0}}
    scanner_good = evaluate_live_scanner(good_scanner, good_pipeline, require_market_open=True)
    scanner_blank = evaluate_live_scanner(blank_scanner, blank_pipeline, require_market_open=True)
    checks["market_scanner_operability_accepts_real_analysis_with_zero_promotions"] = scanner_good.get("ok") is True
    checks["market_scanner_operability_rejects_blank_live_shell"] = scanner_blank.get("ok") is False
    evidence["market_scanner_contract"] = {"operational_fixture": scanner_good, "blank_fixture": scanner_blank}

    failed = [name for name, ok in checks.items() if not ok]
    result = {
        "ok": not failed,
        "version": VERSION,
        "checks": checks,
        "passed": len(checks) - len(failed),
        "total": len(checks),
        "failed": failed,
        "evidence": evidence,
        "production_ready_claimed": False,
        "historical_alpha_claimed": False,
        "elapsed_forward_evidence_claimed": False,
        "broker_authority": "NONE",
    }
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
