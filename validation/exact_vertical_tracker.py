"""Persistent same-decision acceptance tracker.

The tracker is test evidence, never a trading authority.  It follows one exact
canonical decision through Actionable -> Model Paper -> Settlement -> After and
will not substitute a different symbol/decision merely to obtain a green test.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Mapping

STAGES = (
    "WAITING_FOR_ACTIONABLE",
    "ACTIONABLE_OBSERVED",
    "MODEL_OPEN_OBSERVED",
    "SETTLED_OBSERVED",
    "AFTER_OBSERVED",
    "RESTART_VERIFIED",
)
AFTER_STATES = {"CONTINUED", "REVERSED", "RECOVERED", "FLAT"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def ids(row: Mapping[str, Any]) -> set[str]:
    return {
        str(value).strip()
        for value in (
            row.get("decision_id"), row.get("position_decision_id"), row.get("source_decision_id"),
            row.get("signal_id"), row.get("source_signal_id"), row.get("id"),
        ) if str(value or "").strip()
    }


def geometry(row: Mapping[str, Any]) -> dict[str, float | None]:
    def first(*keys: str) -> float | None:
        for key in keys:
            value = finite(row.get(key))
            if value is not None:
                return value
        return None
    return {
        "entry": first("display_entry", "original_entry", "entry", "entry_price"),
        "target": first("display_target", "original_target", "target", "t1"),
        "stop": first("display_stop", "original_stop", "stop", "sl", "stop_price"),
    }


def geometry_complete(g: Mapping[str, Any]) -> bool:
    return all(finite(g.get(k)) is not None and float(g[k]) > 0 for k in ("entry", "target", "stop"))


def same_geometry(a: Mapping[str, Any], b: Mapping[str, Any], tol: float = 0.011) -> bool:
    if not geometry_complete(a) or not geometry_complete(b):
        return False
    return all(abs(float(a[k])-float(b[k])) <= tol for k in ("entry", "target", "stop"))


def extract_model_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    found: list[dict[str, Any]]=[]; seen:set[int]=set()
    def walk(value: Any, depth: int=0) -> None:
        if depth > 7: return
        if isinstance(value, Mapping):
            oid=id(value)
            if oid in seen: return
            seen.add(oid)
            row=dict(value)
            if ids(row) and any(k in row for k in ("entry","entry_price","original_entry","target","original_target","stop","original_stop","stop_price","position_id")):
                found.append(row)
            for v in value.values(): walk(v,depth+1)
        elif isinstance(value,list):
            for v in value[:5000]: walk(v,depth+1)
    walk(payload)
    return found


def lifecycle_records(performance: Mapping[str, Any]) -> list[dict[str, Any]]:
    lifecycle=performance.get("canonical_lifecycle") or (performance.get("performance_evidence") or {}).get("signal_accuracy") or {}
    return [dict(x or {}) for x in (lifecycle.get("records") or []) if isinstance(x,Mapping)]


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version":"exact-vertical-tracker-1.0.0","stage":"WAITING_FOR_ACTIONABLE","events":[],"created_at":utcnow(),"updated_at":utcnow()}
    try:
        data=json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data,dict) else {}
    except Exception:
        return {}


def save(path: Path, state: Mapping[str, Any]) -> None:
    payload=dict(state); payload["updated_at"]=utcnow()
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    temp.replace(path)


def _advance(state: dict[str, Any], stage: str, detail: Mapping[str, Any]) -> None:
    current=str(state.get("stage") or "WAITING_FOR_ACTIONABLE")
    if STAGES.index(stage) < STAGES.index(current):
        return
    changed=stage != current
    state["stage"]=stage
    if changed:
        state.setdefault("events",[]).append({"stage":stage,"at":utcnow(),"detail":dict(detail)})


def _result_name(row: Mapping[str, Any]) -> str:
    return str(row.get("exit_reason") or row.get("result") or row.get("display_result") or "").strip().upper()


def _outcome_name(row: Mapping[str, Any]) -> str:
    return str(row.get("signal_outcome") or row.get("accuracy_state") or row.get("economic_outcome") or "").strip().upper()


def update(
    state: Mapping[str, Any] | None,
    *, live: Mapping[str, Any], workspace: Mapping[str, Any], model: Mapping[str, Any], performance: Mapping[str, Any],
    expected_version: str, expected_build: str, preferred_mode: str="intraday", require_full_sweep: bool=True,
    restart_proof: Mapping[str, Any] | None=None,
) -> tuple[dict[str, Any], list[str]]:
    out=dict(state or {})
    out.setdefault("version","exact-vertical-tracker-1.0.0"); out.setdefault("stage","WAITING_FOR_ACTIONABLE"); out.setdefault("events",[])
    errors: list[str]=[]
    if out.get("expected_version") not in (None,"",expected_version): errors.append("TRACKER_VERSION_CHANGED")
    if out.get("expected_build") not in (None,"",expected_build): errors.append("TRACKER_BUILD_CHANGED")
    out["expected_version"]=expected_version; out["expected_build"]=expected_build

    tracked=str(out.get("decision_id") or "")
    finals=[dict(x or {}) for x in (workspace.get("final_signals") or []) if isinstance(x,Mapping)]
    if not tracked:
        trust=dict(live.get("trust") or {})
        if live.get("market_open") is True and trust.get("state")=="TRUSTED" and trust.get("decision_admission_allowed") is True:
            candidates=[]
            for row in finals:
                desk=str(row.get("mode") or "").lower()
                decision_id=str(row.get("decision_id") or "").strip()
                g=geometry(row)
                cov=dict((workspace.get("coverage") or {}).get(desk) or {})
                if not decision_id or not geometry_complete(g): continue
                if require_full_sweep and cov.get("complete") is not True: continue
                score=finite(row.get("rank_score") if row.get("rank_score") is not None else row.get("evidence_score")) or -1
                preferred_rank=0 if preferred_mode and desk == preferred_mode else 1
                candidates.append((preferred_rank,-score,row))
            if candidates:
                candidates.sort(key=lambda item:(item[0],item[1]))
                row=candidates[0][2]; tracked=str(row.get("decision_id") or "").strip(); g=geometry(row)
                out.update({
                    "decision_id":tracked,"signal_id":row.get("signal_id"),"symbol":str(row.get("symbol") or row.get("trading_symbol") or "").upper(),
                    "mode":str(row.get("mode") or "").lower(),"instrument_key":row.get("instrument_key") or row.get("provider_instrument_key"),
                    "geometry":g,"generated_at":row.get("generated_at") or row.get("decision_generated_at"),
                    "model_version":row.get("model_version"),"policy_version":row.get("policy_version"),"evidence_hash":row.get("evidence_hash"),
                    "preferred_mode":preferred_mode,"selected_mode_fallback":bool(preferred_mode and str(row.get("mode") or "").lower()!=preferred_mode),
                    "actionable_observed_at":utcnow(),
                })
                _advance(out,"ACTIONABLE_OBSERVED",{"decision_id":tracked,"symbol":out["symbol"],"mode":out["mode"],"geometry":g})
    else:
        # If the tracked decision is still Final, its frozen geometry may not drift.
        current=next((r for r in finals if tracked in ids(r)),None)
        if current is not None and not same_geometry(out.get("geometry") or {},geometry(current)):
            errors.append("CANONICAL_GEOMETRY_DRIFT")

    if tracked:
        matches=[r for r in extract_model_rows(model) if tracked in ids(r)]
        if matches:
            row=max(matches,key=lambda r:str(r.get("updated_at") or r.get("closed_at") or r.get("opened_at") or ""))
            mg=geometry(row)
            if geometry_complete(mg) and not same_geometry(out.get("geometry") or {},mg):
                errors.append("MODEL_PAPER_GEOMETRY_DRIFT")
            position_id=str(row.get("position_id") or row.get("settlement_id") or "").strip()
            status=str(row.get("status") or row.get("canonical_state") or row.get("state") or "").upper()
            if position_id: out["position_id"]=position_id
            if row.get("opened_at"): out["opened_at"]=row.get("opened_at")
            if position_id and (row.get("opened_at") or any(token in status for token in ("OPEN","ACTIVE","CLOSED","SETTLED"))):
                _advance(out,"MODEL_OPEN_OBSERVED",{"position_id":position_id,"opened_at":row.get("opened_at"),"status":status})

        settlements=[r for r in lifecycle_records(performance) if tracked in ids(r) and (r.get("accuracy_eligible") is True or r.get("performance_eligible") is True)]
        if settlements:
            row=max(settlements,key=lambda r:str(r.get("closed_at") or r.get("settled_at") or r.get("updated_at") or ""))
            sg=geometry(row)
            if geometry_complete(sg) and not same_geometry(out.get("geometry") or {},sg):
                errors.append("SETTLEMENT_GEOMETRY_DRIFT")
            settlement_id=str(row.get("settlement_id") or row.get("position_id") or "").strip()
            result=_result_name(row); outcome=_outcome_name(row); net=finite(row.get("net_pnl"))
            if not settlement_id: errors.append("SETTLEMENT_ID_MISSING")
            if not result: errors.append("SETTLEMENT_RESULT_MISSING")
            if not outcome: errors.append("SETTLEMENT_OUTCOME_MISSING")
            if net is None: errors.append("SETTLEMENT_NET_PNL_INVALID")
            if settlement_id and result and outcome and net is not None:
                out.update({"settlement_id":settlement_id,"closed_at":row.get("closed_at") or row.get("settled_at"),"result":result,"outcome":outcome,"net_pnl":net,"realized_r":finite(row.get("realized_r"))})
                _advance(out,"SETTLED_OBSERVED",{"settlement_id":settlement_id,"result":result,"outcome":outcome,"net_pnl":net})
                after=str(row.get("after") or row.get("after_state") or row.get("follow_through_state") or "").strip().upper()
                horizon=str(row.get("after_horizon") or (row.get("follow_through") or {}).get("after_horizon") or "").strip()
                if after in AFTER_STATES and horizon:
                    out["after"]=after; out["after_horizon"]=horizon
                    _advance(out,"AFTER_OBSERVED",{"after":after,"after_horizon":horizon,"result_immutable":row.get("result_is_immutable") is True})

    if restart_proof and str(out.get("stage")) == "AFTER_OBSERVED":
        before=str(restart_proof.get("before_boot_id") or ""); after=str(restart_proof.get("after_boot_id") or "")
        persisted=restart_proof.get("same_settlement_persisted") is True
        if not before or not after or before == after: errors.append("PROCESS_RESTART_NOT_PROVEN")
        if not persisted: errors.append("SETTLEMENT_NOT_PERSISTED_AFTER_RESTART")
        if not errors:
            out["restart_proof"]={"before_boot_id":before,"after_boot_id":after,"verified_at":utcnow()}
            _advance(out,"RESTART_VERIFIED",out["restart_proof"])

    out["complete"]=str(out.get("stage"))=="RESTART_VERIFIED"
    out["errors"]=errors
    out["updated_at"]=utcnow()
    return out, errors
