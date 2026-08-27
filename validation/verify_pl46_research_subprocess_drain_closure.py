from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core import historical_pit_sweep_service as sweep_module


class _Governor:
    def should_yield(self, *_args, **_kwargs):
        return False, ""

    def snapshot(self):
        return {}


class _App:
    def __init__(self):
        self.status = {}
        self.workload_governor = _Governor()


def main() -> int:
    service_source = (BACKEND / "core" / "historical_pit_sweep_service.py").read_text(encoding="utf-8")
    trainer_source = (BACKEND / "tools" / "train_nse_smart_model.py").read_text(encoding="utf-8")
    checks = {
        "pipe_backpressure_removed": "stdout=subprocess.PIPE" not in service_source and "stderr=subprocess.PIPE" not in service_source,
        "file_spool_contract_present": "FILE_SPOOL_NONBLOCKING" in service_source and "research-subprocess" in service_source,
        "legacy_sqlite_training_disabled": "LEGACY_SQLITE_TRAINING_AUTHORITY_DISABLED" in trainer_source and "FROM candles c JOIN instruments" not in trainer_source,
        "lake_loader_wired": "load_panel=lambda start_date: load_panel_from_lake(layout, start_date)" in trainer_source,
        "lake_fail_closed": "if not lake_training_available(layout):" in trainer_source and 'TRAINING_SOURCE_POLICY = "PARQUET_DUCKDB_ONLY"' in trainer_source,
    }

    dynamic = {}
    with tempfile.TemporaryDirectory(prefix="laddu-pl46-research-drain-") as tmp:
        original_data_dir = sweep_module.DATA_DIR
        sweep_module.DATA_DIR = Path(tmp)
        try:
            service = sweep_module.HistoricalPitSweepService(_App())
            payload = {"ok": True, "state": "COMPLETE", "model_id": "drain-proof"}
            child = (
                "import json,sys;"
                "sys.stdout.write('O'*2000000);sys.stdout.flush();"
                "sys.stderr.write('E'*2000000);sys.stderr.flush();"
                f"print(json.dumps({payload!r}))"
            )
            started = time.monotonic()
            result = service._run_command(
                "large_output_terminality_proof",
                [sys.executable, "-c", child],
                running_fn=lambda: True,
                sup=None,
            )
            elapsed = time.monotonic() - started
            stdout_log = Path(str(result.get("stdout_log") or ""))
            stderr_log = Path(str(result.get("stderr_log") or ""))
            dynamic = {
                "completed": result.get("ok") is True and str(result.get("state") or "").upper() == "COMPLETE",
                "elapsed_seconds": round(elapsed, 3),
                "bounded_completion": elapsed < 20.0,
                "stdout_durable": stdout_log.is_file() and stdout_log.stat().st_size >= 2_000_000,
                "stderr_durable": stderr_log.is_file() and stderr_log.stat().st_size >= 2_000_000,
                "output_mode": result.get("subprocess_output_mode"),
            }
            checks.update({
                "dynamic_large_output_completed": dynamic["completed"],
                "dynamic_large_output_bounded": dynamic["bounded_completion"],
                "dynamic_stdout_durable": dynamic["stdout_durable"],
                "dynamic_stderr_durable": dynamic["stderr_durable"],
                "dynamic_output_mode": dynamic["output_mode"] == "FILE_SPOOL_NONBLOCKING",
            })
        finally:
            sweep_module.DATA_DIR = original_data_dir

    failures = [name for name, ok in checks.items() if not ok]
    report = {
        "ok": not failures,
        "checks": checks,
        "dynamic": dynamic,
        "failures": failures,
        "broker_authority": "NONE",
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
