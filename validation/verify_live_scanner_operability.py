"""Live market-hours scanner operability proof.

This validator is intentionally strict. A healthy API or a rendered shell is not
scanner proof. During market hours both canonical desks must show a real completed
analysis cycle with quote-ready inputs and at least one deep mathematical analysis.
Zero promotions are allowed only when analysed candidates have explicit rejection or
blocking evidence; an empty/warming/no-data surface is a failure.

No mutation, provider write, model promotion or broker action is performed.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

VERSION = "live-scanner-operability-proof-1.0.0"
FAIL_STATES = {
    "FAILED", "FAIL", "ERROR", "WAITING_TOKEN", "WAITING_UNIVERSE",
    "WAITING_FOCUSED_UNIVERSE", "QUOTE_RATE_LIMITED", "DATABASE_UNAVAILABLE",
    "MARKET_DATA_UNAVAILABLE",
}


def _dict(v: Any) -> Dict[str, Any]:
    return dict(v) if isinstance(v, dict) else {}


def _num(*values: Any) -> int:
    for value in values:
        try:
            if value is not None and value != "":
                return int(float(value))
        except (TypeError, ValueError):
            pass
    return 0


def _get_json(base_url: str, path: str, timeout: float = 20.0) -> Dict[str, Any]:
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} did not return a JSON object")
    return payload


def _reason_count(values: Iterable[Any]) -> int:
    count = 0
    for value in values or []:
        if isinstance(value, dict):
            if str(value.get("reason") or value.get("state") or value.get("code") or "").strip():
                count += 1
        elif str(value or "").strip():
            count += 1
    return count


def _mode_evidence(scanner: Dict[str, Any], mode: str) -> Dict[str, Any]:
    root = _dict(scanner.get("scanner")) or scanner
    modes = _dict(root.get("mode_scanners") or root.get("modes"))
    row = _dict(modes.get(mode))
    analysis = _dict(row.get("analysis"))
    contract = _dict(row.get("progress_contract")) or _dict(analysis.get("progress_contract"))
    last = _dict(analysis.get("last_completed")) or _dict(row.get("last_completed"))
    resolution = _dict(last.get("resolution_summary"))
    stage_members = _dict(analysis.get("stage_members")) or _dict(row.get("stage_members"))

    population = _num(
        contract.get("population_count"), analysis.get("universe_size"),
        _dict(row.get("coverage")).get("universe_size"), last.get("candidate_universe"), last.get("attempted"),
    )
    attempted = _num(
        last.get("attempted"), last.get("candidate_universe"), analysis.get("cycle_attempted"),
        contract.get("current_sweep_scanned"), row.get("attempted"),
    )
    quote_ready = _num(
        last.get("quote_ready"), last.get("quote_returned"), analysis.get("cycle_quote_ready"),
        analysis.get("quote_ready"), row.get("quote_scanned"),
    )
    analysed = _num(
        last.get("scanned"), resolution.get("analysed"), analysis.get("cycle_scanned"),
        row.get("deep_scanned"), row.get("scanned"),
    )
    promoted = _num(last.get("promoted"), analysis.get("cycle_promoted"), row.get("promoted"))
    rejected = _num(last.get("rejected"), analysis.get("cycle_rejected"), row.get("rejected"))
    blocked = _num(
        last.get("blocked"), last.get("data_missing"), analysis.get("cycle_blocked"),
        analysis.get("cycle_data_missing"), row.get("blocked"), row.get("data_missing"),
    )
    explicit_reasons = (
        _reason_count(row.get("top_blockers") or [])
        + _reason_count(analysis.get("top_blockers") or [])
        + _reason_count(stage_members.get("blocked") or [])
        + _reason_count(stage_members.get("mathematically_rejected") or [])
        + _reason_count(stage_members.get("data_pending") or [])
    )
    state = str(analysis.get("state") or row.get("state") or contract.get("state") or "UNKNOWN").upper()
    blocker = str(
        analysis.get("blocker_reason") or row.get("blocker_reason") or contract.get("blocker_reason")
        or contract.get("pause_reason") or ""
    ).upper()

    checks = {
        "mode_present": bool(row),
        "population_present": population > 0,
        "attempted_real_work": attempted > 0,
        "quotes_reached_scanner": quote_ready > 0,
        "deep_math_executed": analysed > 0,
        "not_failed_or_waiting": state not in FAIL_STATES and blocker not in FAIL_STATES,
        "zero_promotion_is_explained": promoted > 0 or rejected > 0 or blocked > 0 or explicit_reasons > 0,
    }
    return {
        "mode": mode,
        "state": state,
        "blocker_reason": blocker or None,
        "population": population,
        "attempted": attempted,
        "quote_ready": quote_ready,
        "analysed": analysed,
        "promoted": promoted,
        "rejected": rejected,
        "blocked_or_data_missing": blocked,
        "explicit_reason_count": explicit_reasons,
        "last_completed_at": last.get("completed_at") or analysis.get("last_run") or row.get("last_run"),
        "checks": checks,
        "operational": all(checks.values()),
    }


def evaluate(scanner: Dict[str, Any], pipeline: Dict[str, Any], *, require_market_open: bool) -> Dict[str, Any]:
    market_open = bool(scanner.get("market_open"))
    instruments = _dict(scanner.get("instruments"))
    instrument_count = _num(instruments.get("universe_count"), instruments.get("count"), instruments.get("population_count"))
    # Some installed snapshots expose instrument count only inside the scanner root.
    root = _dict(scanner.get("scanner")) or scanner
    instrument_count = max(instrument_count, _num(_dict(root.get("instruments")).get("count")))
    token = _dict(scanner.get("token"))
    auth = _dict(scanner.get("auth"))
    token_ok = bool(token.get("ok")) if token else not any(str(v).lower() in {"false", "failed", "invalid"} for v in auth.values())

    modes = {mode: _mode_evidence(scanner, mode) for mode in ("intraday", "delivery")}
    fast = _dict(pipeline.get("fast_lane"))
    diagnosis = str(fast.get("diagnosis") or "").lower()
    pipeline_scanned = _num(fast.get("scanned"), fast.get("deep_scanned"))
    pipeline_ok = diagnosis in {"ok", "no_qualifying_setups"} and pipeline_scanned > 0

    global_checks = {
        "market_open": (market_open or not require_market_open),
        "instrument_universe_loaded": instrument_count >= 1000,
        "token_or_auth_available": token_ok,
        "intraday_operational": modes["intraday"]["operational"],
        "delivery_operational": modes["delivery"]["operational"],
        "pipeline_not_blank": pipeline_ok,
    }
    return {
        "version": VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "market_open": market_open,
        "require_market_open": require_market_open,
        "instrument_count": instrument_count,
        "token_ok": token_ok,
        "pipeline": {
            "diagnosis": diagnosis or None,
            "scanned": pipeline_scanned,
            "promoted": _num(fast.get("promoted")),
            "data_missing": _num(fast.get("data_missing")),
            "below_threshold": _num(fast.get("below_threshold")),
        },
        "modes": modes,
        "checks": global_checks,
        "ok": all(global_checks.values()),
        "policy": (
            "During market hours API/UI shell health is insufficient. Both canonical desks must prove "
            "quote-ready work and at least one deep mathematical analysis. Zero promoted candidates is "
            "allowed only when analysed candidates carry explicit governed rejection/blocking evidence."
        ),
        "broker_authority": "NONE",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8086")
    ap.add_argument("--output", required=True)
    ap.add_argument("--wait-seconds", type=int, default=180)
    ap.add_argument("--poll-seconds", type=float, default=5.0)
    ap.add_argument("--require-market-open", action="store_true")
    args = ap.parse_args()

    deadline = time.monotonic() + max(0, args.wait_seconds)
    attempts = []
    final: Dict[str, Any] = {}
    while True:
        try:
            scanner = _get_json(args.base_url, "/api/scanner/status", timeout=25.0)
            pipeline = _get_json(args.base_url, "/api/pipeline-health", timeout=15.0)
            final = evaluate(scanner, pipeline, require_market_open=args.require_market_open)
            attempts.append({
                "captured_at": final.get("captured_at"), "ok": final.get("ok"),
                "market_open": final.get("market_open"), "checks": final.get("checks"),
                "modes": {k: {x: v for x, v in row.items() if x in {"state","population","attempted","quote_ready","analysed","promoted","rejected","blocked_or_data_missing","operational","blocker_reason"}} for k, row in final.get("modes", {}).items()},
                "pipeline": final.get("pipeline"),
            })
            if final.get("ok"):
                break
        except Exception as exc:
            final = {"version": VERSION, "ok": False, "error": f"{type(exc).__name__}: {exc}", "captured_at": datetime.now(timezone.utc).isoformat()}
            attempts.append(dict(final))
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.5, args.poll_seconds))

    final["attempt_count"] = len(attempts)
    final["attempts"] = attempts[-12:]
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(final, indent=2, sort_keys=False), encoding="utf-8")
    print(json.dumps(final, indent=2, sort_keys=False))
    return 0 if final.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
