"""Install-time proof for the isolated, authoritative Quant/AI research plane."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from urllib import parse, request

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.research_plane_contract import CONTRACT_VERSION, REQUIRED_POLICY, REQUIRED_TASKS
from core.storage_layout import atomic_write_json
from tools.train_nse_smart_model import (
    PUBLICATION_AUTHORITY, PRODUCTION_WEIGHT_POLICY, TRAINING_SOURCE_POLICY,
)

MODULES = {
    "numpy": "numpy",
    "pandas": "pandas",
    "scipy": "scipy",
    "scikit-learn": "sklearn",
    "ta": "ta",
    "duckdb": "duckdb",
    "lightgbm": "lightgbm",
    "psycopg": "psycopg",
}

TASK_SCRIPT_BY_NAME = {
    "ProjectLaddu-First-Useful-Mode": "run_first_useful_mode.ps1",
    "ProjectLaddu-Premarket-Learning": "run_learning_cycle.ps1",
    "ProjectLaddu-PostClose-Settlement": "run_learning_cycle.ps1",
    "ProjectLaddu-NSE-Official-Data": "run_nse_official_data_cycle.ps1",
    "ProjectLaddu-AI-Training": "train_ai_model.ps1",
    "ProjectLaddu-Model-Governance": "run_model_governance_cycle.ps1",
    "ProjectLaddu-Brand-Assets": "run_brand_asset_refresh.ps1",
    "ProjectLaddu-Weekend-Research": "run_learning_cycle.ps1",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _version(package: str, module) -> str | None:
    value = getattr(module, "__version__", None)
    if value:
        return str(value)
    try:
        return importlib.metadata.version(package)
    except Exception:
        return None


def _tcp_probe(dsn: str, timeout: float = 3.0) -> dict:
    parsed = parse.urlsplit(dsn)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5432
    started = datetime.now(timezone.utc)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return {"ok": True, "host": host, "port": port, "latency_ms": round((datetime.now(timezone.utc)-started).total_seconds()*1000, 3)}
    except Exception as exc:
        return {"ok": False, "host": host, "port": port, "error": f"{type(exc).__name__}: {exc}"[:240]}


def _quest_probe(base_url: str) -> dict:
    try:
        url = base_url.rstrip("/") + "/exec?" + parse.urlencode({"query": "select 1"})
        with request.urlopen(url, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        return {"ok": response.status == 200 and not payload.get("error"), "status": response.status}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:240]}



def _decode_native_xml(raw: bytes) -> tuple[str, str]:
    """Decode schtasks /XML output without trusting the active Windows code page.

    Windows Task Scheduler commonly emits UTF-16 XML even when the parent
    process uses an ANSI console.  text=True therefore produced NUL-separated
    text and the old exact-string test falsely rejected enabled tasks.
    """
    if not raw:
        return "", "empty"
    candidates: list[tuple[str, str]] = []
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.append(("utf-16", "utf-16-bom"))
    if raw.startswith(b"\xef\xbb\xbf"):
        candidates.append(("utf-8-sig", "utf-8-bom"))
    if b"\x00" in raw[:256]:
        candidates.extend((("utf-16-le", "utf-16-le-nul"), ("utf-16-be", "utf-16-be-nul")))
    candidates.extend((("utf-8-sig", "utf-8"), ("cp1252", "cp1252")))
    seen = set()
    for encoding, label in candidates:
        if encoding in seen:
            continue
        seen.add(encoding)
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "<" in text and ">" in text:
            return text.lstrip("\ufeff"), label
    return raw.decode("utf-8", errors="replace").replace("\x00", ""), "utf-8-replace"


def _xml_local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _scheduled_task_xml_status(
    stdout: bytes,
    stderr: bytes,
    returncode: int,
    *,
    expected_script: str | None = None,
) -> dict:
    """Validate a Task Scheduler XML definition using schema semantics.

    Settings/Enabled is optional in the Task Scheduler XSD and defaults to
    true. Trigger Enabled values are evaluated separately so a trigger-level
    flag can never be mistaken for the task-level setting.
    """
    text, encoding = _decode_native_xml(stdout or stderr or b"")
    result = {"ok": False, "returncode": int(returncode), "encoding": encoding}
    if returncode != 0:
        result["error"] = (text.strip() or "SCHTASKS_QUERY_FAILED")[:400]
        return result
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        result["error"] = f"TASK_XML_PARSE_FAILED:{exc}"[:400]
        return result

    settings_enabled_value: str | None = None
    command = ""
    arguments = ""
    trigger_count = 0
    active_trigger_count = 0

    for child in root:
        local = _xml_local_name(child)
        if local == "Settings":
            for setting in child:
                if _xml_local_name(setting) == "Enabled":
                    settings_enabled_value = (setting.text or "").strip().lower()
                    break
        elif local == "Triggers":
            for trigger in child:
                trigger_local = _xml_local_name(trigger)
                if not trigger_local.endswith("Trigger"):
                    continue
                trigger_count += 1
                trigger_enabled_value: str | None = None
                for trigger_child in trigger:
                    if _xml_local_name(trigger_child) == "Enabled":
                        trigger_enabled_value = (trigger_child.text or "").strip().lower()
                        break
                if trigger_enabled_value in (None, "true"):
                    active_trigger_count += 1

    for element in root.iter():
        local = _xml_local_name(element)
        value = (element.text or "").strip()
        if local == "Command" and not command:
            command = value
        elif local == "Arguments" and not arguments:
            arguments = value

    if settings_enabled_value is None:
        enabled = True
        enabled_source = "TASK_SCHEMA_DEFAULT_TRUE"
    else:
        enabled = settings_enabled_value == "true"
        enabled_source = "EXPLICIT_XML"

    result.update(
        {
            "enabled": enabled,
            "enabled_source": enabled_source,
            "command": command,
            "arguments": arguments,
            "trigger_count": trigger_count,
            "active_trigger_count": active_trigger_count,
        }
    )

    if not enabled:
        result["error"] = f"TASK_DISABLED:{settings_enabled_value or 'blank'}"
        return result
    if expected_script:
        normalized = (command + " " + arguments).replace("/", "\\").lower()
        expected = expected_script.replace("/", "\\").lower()
        if expected not in normalized:
            result["error"] = f"TASK_ACTION_MISMATCH:{expected_script}"
            return result
    if trigger_count < 1:
        result["error"] = "TASK_TRIGGER_MISSING"
        return result
    if active_trigger_count < 1:
        result["error"] = "TASK_TRIGGERS_DISABLED"
        return result

    result["ok"] = True
    return result

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-tasks", action="store_true")
    args = parser.parse_args()
    install_dir = args.install_dir.resolve()
    blockers = []
    modules = {}
    for package, import_name in MODULES.items():
        try:
            module = importlib.import_module(import_name)
            modules[package] = {"ok": True, "version": _version(package, module)}
        except Exception as exc:
            modules[package] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:240]}
            blockers.append(f"MODULE_UNAVAILABLE:{package}")

    policies = {
        "data_plane_mode": os.environ.get("PROJECT_LADDU_DATA_PLANE_MODE", ""),
        "training_source_policy": TRAINING_SOURCE_POLICY,
        "operational_authority": "POSTGRESQL",
        "governance_authority": "POSTGRESQL",
        "market_time_series_authority": "QUESTDB",
        "publication_authority": PUBLICATION_AUTHORITY,
        "production_weight_policy": PRODUCTION_WEIGHT_POLICY,
        "broker_authority": "NONE",
    }
    for key, expected in REQUIRED_POLICY.items():
        if policies.get(key) != expected:
            blockers.append(f"POLICY_MISMATCH:{key}")

    operational_dsn = os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", "").strip()
    governance_dsn = os.environ.get("PROJECT_LADDU_GOVERNANCE_DSN", "").strip()
    quest_url = os.environ.get("PROJECT_LADDU_QUESTDB_HTTP_URL", "").strip()
    connectivity = {
        "operational_postgres": _tcp_probe(operational_dsn) if operational_dsn else {"ok": False, "error": "DSN_MISSING"},
        "governance_postgres": _tcp_probe(governance_dsn) if governance_dsn else {"ok": False, "error": "DSN_MISSING"},
        "questdb": _quest_probe(quest_url) if quest_url else {"ok": False, "error": "URL_MISSING"},
    }
    for name, row in connectivity.items():
        if not row.get("ok"):
            blockers.append(f"AUTHORITY_UNREACHABLE:{name}")

    trainer = (install_dir / "backend" / "tools" / "train_nse_smart_model.py").read_text(encoding="utf-8")
    training_script = (install_dir / "train_ai_model.ps1").read_text(encoding="utf-8")
    learning_script = (install_dir / "run_learning_cycle.ps1").read_text(encoding="utf-8")
    governance_script = (install_dir / "run_model_governance_cycle.ps1").read_text(encoding="utf-8")
    source_contracts = {
        "trainer_accepts_only_parquet_duckdb": '--allow-sqlite-fallback' not in trainer and 'SQLITE_EXPLICIT_RECOVERY' not in trainer and 'PARQUET_DUCKDB_ONLY' in trainer,
        "trainer_refreshes_production_authority_catalog": 'refresh_research_catalog.py' in training_script,
        "trainer_publication_authority": 'GOVERNANCE_POSTGRESQL_VIA_LIVE_SERVICE' in trainer,
        "trainer_zero_production_weight": '"production_weight": 0.0' in trainer,
        "training_launcher_loads_data_plane": 'secure\\data-plane.env.ps1' in training_script,
        "learning_launcher_loads_data_plane": 'secure\\data-plane.env.ps1' in learning_script,
        "governance_launcher_loads_data_plane": 'secure\\data-plane.env.ps1' in governance_script,
        "learning_cycle_uses_server_authority": '/api/learning-health' in (install_dir / 'backend' / 'tools' / 'run_operational_learning_cycle.py').read_text(encoding='utf-8'),
    }
    for name, ok in source_contracts.items():
        if not ok:
            blockers.append(f"SOURCE_CONTRACT_FAILED:{name}")

    task_status = {}
    if args.require_tasks:
        for task_name in REQUIRED_TASKS:
            try:
                result = subprocess.run(
                    ["schtasks.exe", "/Query", "/TN", task_name, "/XML"],
                    capture_output=True, timeout=10, check=False,
                )
                proof = _scheduled_task_xml_status(
                    result.stdout,
                    result.stderr,
                    result.returncode,
                    expected_script=TASK_SCRIPT_BY_NAME.get(task_name),
                )
                task_status[task_name] = proof
                if not proof.get("ok"):
                    blockers.append(f"TASK_UNAVAILABLE:{task_name}")
            except Exception as exc:
                task_status[task_name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:240]}
                blockers.append(f"TASK_UNAVAILABLE:{task_name}")
    else:
        task_status = {name: {"ok": None, "state": "PENDING_FINAL_REGISTRATION"} for name in REQUIRED_TASKS}

    payload = {
        "ok": not blockers,
        "contract_version": CONTRACT_VERSION,
        "state": "READY" if not blockers else "BLOCKED",
        "verified_at": _now(),
        "research_python": sys.executable,
        "python_version": sys.version.split()[0],
        "modules": modules,
        "policies": policies,
        "connectivity": connectivity,
        "source_contracts": source_contracts,
        "required_tasks": list(REQUIRED_TASKS),
        "task_status": task_status,
        "task_proof_required": bool(args.require_tasks),
        "blockers": blockers,
        "evidence_boundary": "runtime, authority wiring and lifecycle safety proven; market alpha is not implied",
    }
    atomic_write_json(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
