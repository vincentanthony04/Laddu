"""Bounded API-driven learning/readiness cycles for Project Laddu.

Scheduled cycles never open compatibility SQLite or instantiate a second Store.
The running service owns PostgreSQL/QuestDB authority and exposes bounded,
read-only learning/governance summaries. Full model fitting remains isolated in
the Quant/AI trainer. Post-close and weekend cycles explicitly invoke the
independent Intraday and Delivery shadow challengers through the running API;
they remain zero-broker-authority and cannot self-promote.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from urllib import request as urlrequest


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def api_json(base: str, path: str, timeout: int = 8) -> dict:
    try:
        with urlrequest.urlopen(base.rstrip("/") + path, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
            return payload if isinstance(payload, dict) else {"ok": False, "error": "non-object response"}
    except Exception as exc:
        return {"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:240]}


def post_json(base: str, path: str, payload: dict | None = None, timeout: int = 10) -> dict:
    body = json.dumps(payload or {}).encode("utf-8")
    req = urlrequest.Request(
        base.rstrip("/") + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8", "replace"))
            return result if isinstance(result, dict) else {"ok": False, "error": "non-object response"}
    except Exception as exc:
        return {"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:240]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycle", choices=["premarket", "market", "settlement", "weekend"], required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8086")
    args = parser.parse_args()

    data = Path(args.data_dir)
    os.environ["PROJECT_LADDU_DATA_DIR"] = str(data)
    report: dict = {
        "ok": False,
        "cycle": args.cycle,
        "started_at": now(),
        "authority": "RUNNING_SERVICE_API_ONLY",
        "policy": "scheduled cycles never open compatibility SQLite or create a second operational writer; post-close/weekend invoke isolated dual-desk shadow research through the running API",
    }

    report["ready"] = api_json(args.api_url, "/api/ready")
    report["health"] = api_json(args.api_url, "/api/health")
    report["learning_health"] = api_json(args.api_url, "/api/learning-health")
    report["counterfactual_learning"] = api_json(args.api_url, "/api/counterfactual-learning")
    report["quant_edge"] = api_json(args.api_url, "/api/quant-edge/status")
    report["paper_status"] = api_json(args.api_url, "/api/quant-edge/paper-status")
    report["model_governance"] = api_json(args.api_url, "/api/quant-model-governance")
    report["quant_research_plane"] = api_json(args.api_url, "/api/quant-research-plane")

    if args.cycle == "premarket":
        report["dual_desk"] = api_json(args.api_url, "/api/dual-desk-architecture")
        report["intraday_tournament"] = api_json(args.api_url, "/api/model-tournament?desk=intraday")
        report["delivery_tournament"] = api_json(args.api_url, "/api/model-tournament?desk=delivery")
    elif args.cycle in {"settlement", "weekend"}:
        report["refresh"] = post_json(args.api_url, "/api/refresh", timeout=120)
        cycle_trigger = f"scheduled-{args.cycle}-dual-desk-shadow"
        report["intraday_shadow_cycle"] = post_json(
            args.api_url,
            "/api/quant-edge/run-shadow-cycle",
            {
                "mode": "intraday",
                "trigger": cycle_trigger,
                "min_train_days": 126,
                "test_days": 21,
                "embargo_days": 1,
                "max_folds": 8,
                "trial_count": 1,
            },
            timeout=3600,
        )
        report["delivery_shadow_cycle"] = post_json(
            args.api_url,
            "/api/quant-edge/run-shadow-cycle",
            {
                "mode": "delivery",
                "trigger": cycle_trigger,
                "min_train_days": 126,
                "test_days": 21,
                "embargo_days": 10,
                "max_folds": 8,
                "trial_count": 1,
            },
            timeout=3600,
        )
        report["intraday_tournament"] = api_json(args.api_url, "/api/model-tournament?desk=intraday")
        report["delivery_tournament"] = api_json(args.api_url, "/api/model-tournament?desk=delivery")

    required = ["ready", "health", "learning_health", "counterfactual_learning", "quant_edge", "paper_status", "model_governance", "quant_research_plane"]
    if args.cycle in {"settlement", "weekend"}:
        required.extend(["intraday_shadow_cycle", "delivery_shadow_cycle"])
    failed = [name for name in required if report.get(name, {}).get("ok") is not True]
    report["failed_contracts"] = failed
    report["ok"] = not failed and report["ready"].get("ready") is True
    report["finished_at"] = now()

    outdir = data / "manifests" / "learning_cycles"
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"{args.cycle}-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "cycle": args.cycle, "authority": report["authority"], "failed_contracts": failed, "report": str(path)}))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
