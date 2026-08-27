"""DuckDB projection and LightGBM finite-tournament orchestration.

PostgreSQL remains the operational and governance authority while QuestDB owns
market time series. Required research dependencies run in an isolated Python worker,
so importing the live service never imports DuckDB, LightGBM, NumPy or pandas.
Training registers finite validation candidates; promotion is owned only by ModelTournamentService.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Dict, Iterable, Mapping, Optional

from config import DATA_DIR
from core.production_mode_policy import require_production_mode
from core.model_tournament_service import ModelTournamentService


ANALYTICS_SERVICE_VERSION = "quant-analytics-1.1.0-r6-runtime-revalidation"
WORKER_VERSION = "duckdb-lightgbm-worker-1.1.1-r6-revalidated"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _identifier(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8", errors="replace")).hexdigest()[:40]


class QuantAnalyticsService:
    """Own the analytical projection and nonlinear validation artefacts."""

    def __init__(
        self,
        store: Any,
        *,
        analytics_path: Optional[Path | str] = None,
        artifact_dir: Optional[Path | str] = None,
        research_python: Optional[Path | str] = None,
    ):
        self.store = store
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.RLock()
        configured = os.environ.get("PROJECT_LADDU_QUANT_DUCKDB")
        self.analytics_path = Path(
            analytics_path or configured or (Path(DATA_DIR) / "analytics" / "project_laddu_quant.duckdb")
        ).resolve()
        self.artifact_dir = Path(
            artifact_dir or (self.analytics_path.parent / "models")
        ).resolve()
        if not hasattr(store, "_quant_model_artifact_roots"):
            store._quant_model_artifact_roots = set()
        store._quant_model_artifact_roots.add(str(self.artifact_dir))
        self.parquet_dir = (self.analytics_path.parent / "parquet").resolve()
        self.research_cache_dir = (self.analytics_path.parent / "runtime-cache").resolve()
        self.explicit_research_python = str(research_python or "").strip()
        self.worker = (
            Path(__file__).resolve().parents[1] / "tools" / "quant_duckdb_lightgbm_worker.py"
        ).resolve()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS quant_analytics_projections (
                  projection_id TEXT PRIMARY KEY,
                  mode TEXT NOT NULL,
                  state TEXT NOT NULL,
                  sqlite_snapshot_count INTEGER NOT NULL,
                  sqlite_label_count INTEGER NOT NULL,
                  duckdb_snapshot_count INTEGER NOT NULL,
                  duckdb_label_count INTEGER NOT NULL,
                  source_content_hash TEXT NOT NULL,
                  projected_content_hash TEXT NOT NULL,
                  reconciled INTEGER NOT NULL,
                  analytics_path TEXT NOT NULL,
                  parquet_paths_json TEXT NOT NULL,
                  dependency_json TEXT NOT NULL,
                  evidence_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  service_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_quant_analytics_projection_latest
                  ON quant_analytics_projections(mode,created_at);

                CREATE TABLE IF NOT EXISTS shadow_lightgbm_models (
                  model_id TEXT PRIMARY KEY,
                  mode TEXT NOT NULL,
                  horizon TEXT NOT NULL,
                  model_family TEXT NOT NULL,
                  state TEXT NOT NULL,
                  artifact_path TEXT,
                  dataset_fingerprint TEXT NOT NULL,
                  feature_manifest_hash TEXT NOT NULL,
                  observations INTEGER NOT NULL,
                  trading_days INTEGER NOT NULL,
                  regimes INTEGER NOT NULL,
                  trial_count INTEGER NOT NULL,
                  validation_json TEXT NOT NULL,
                  dependency_json TEXT NOT NULL,
                  authority TEXT NOT NULL,
                  production_weight REAL NOT NULL,
                  created_at TEXT NOT NULL,
                  service_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_shadow_lightgbm_latest
                  ON shadow_lightgbm_models(mode,horizon,created_at);
                """
            )
            self.store.conn.commit()

    def _sqlite_path(self) -> Optional[Path]:
        rows = self.store.conn.execute("PRAGMA database_list").fetchall()
        for raw in rows:
            row = dict(raw) if hasattr(raw, "keys") else {
                "name": raw[1] if len(raw) > 1 else "",
                "file": raw[2] if len(raw) > 2 else "",
            }
            if str(row.get("name") or "") != "main":
                continue
            value = str(row.get("file") or "").strip()
            if value:
                return Path(value).resolve()
        return None

    @staticmethod
    def _programdata_python() -> Path:
        programdata = Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "ProjectLaddu"
        pointer = programdata / "runtime" / "research_python.txt"
        try:
            return Path(pointer.read_text(encoding="utf-8").strip())
        except OSError:
            return Path()

    def _python_candidates(self) -> Iterable[Path]:
        values = [
            self.explicit_research_python,
            os.environ.get("PROJECT_LADDU_RESEARCH_PYTHON"),
            str(self._programdata_python()),
            sys.executable,
        ]
        seen = set()
        for value in values:
            if not value:
                continue
            path = Path(str(value)).resolve()
            key = str(path).lower()
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            yield path

    def _research_python(self, required: Iterable[str]) -> tuple[Optional[Path], Dict[str, Any]]:
        modules = sorted({str(name) for name in required if name})
        probe = "import " + ",".join(modules)
        failures = []
        for candidate in self._python_candidates():
            try:
                env = dict(os.environ)
                env.setdefault("PYTHONUTF8", "1")
                env.setdefault("PYTHONIOENCODING", "utf-8")
                completed = subprocess.run(
                    [str(candidate), "-c", probe],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                    env=env,
                )
            except Exception as exc:
                failures.append({"python": str(candidate), "error": str(exc)[:200]})
                continue
            if completed.returncode == 0:
                return candidate, {
                    "state": "AVAILABLE",
                    "python": str(candidate),
                    "required_modules": modules,
                }
            failures.append({
                "python": str(candidate),
                "error": (completed.stderr or completed.stdout or "dependency probe failed")[-300:],
            })
        return None, {
            "state": "DEPENDENCY_UNAVAILABLE",
            "required_modules": modules,
            "probes": failures,
            "install": "Re-run the single INSTALL_UPDATE.cmd complete-build transaction; the installed research runtime is incomplete.",
        }

    def _run_worker(self, action: str, arguments: list[str], required: Iterable[str]) -> Dict[str, Any]:
        sqlite_path = self._sqlite_path()
        if sqlite_path is None or not sqlite_path.is_file():
            return {
                "ok": True,
                "state": "SQLITE_FILE_REQUIRED",
                "reason": "DuckDB projection requires the canonical file-backed SQLite database.",
                "prediction_state": "MODEL_UNAVAILABLE",
                "authority": "VALIDATION_BLOCKED",
                "production_influence": False,
                "broker_execution_weight": 0.0,
            }
        python, dependency = self._research_python(required)
        if python is None:
            return {
                "ok": True,
                "state": "DEPENDENCY_UNAVAILABLE",
                "dependency": dependency,
                "prediction_state": "MODEL_UNAVAILABLE",
                "authority": "VALIDATION_BLOCKED",
                "production_influence": False,
                "broker_execution_weight": 0.0,
            }
        if not self.worker.is_file():
            return {
                "ok": False,
                "state": "WORKER_MISSING",
                "reason": str(self.worker),
                "dependency": dependency,
                "prediction_state": "MODEL_UNAVAILABLE",
                "authority": "VALIDATION_BLOCKED",
                "production_influence": False,
                "broker_execution_weight": 0.0,
            }
        self.analytics_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.parquet_dir.mkdir(parents=True, exist_ok=True)
        self.research_cache_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(python),
            str(self.worker),
            action,
            "--sqlite",
            str(sqlite_path),
            "--duckdb",
            str(self.analytics_path),
            "--parquet-dir",
            str(self.parquet_dir),
            *arguments,
        ]
        completed = subprocess.run(
            command,
            cwd=str(self.worker.parents[2]),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "MPLCONFIGDIR": str(self.research_cache_dir),
            },
        )
        lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        payload: Dict[str, Any] = {}
        for line in reversed(lines):
            try:
                candidate = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
        if completed.returncode != 0 or not payload:
            return {
                "ok": False,
                "state": "WORKER_FAILED",
                "return_code": completed.returncode,
                "error": (completed.stderr or completed.stdout or "worker returned no JSON")[-1000:],
                "dependency": dependency,
                "prediction_state": "MODEL_UNAVAILABLE",
                "authority": "VALIDATION_BLOCKED",
                "production_influence": False,
                "broker_execution_weight": 0.0,
            }
        payload["dependency"] = dependency
        payload.setdefault("prediction_state", "MODEL_UNAVAILABLE")
        payload.setdefault("production_influence", False)
        payload["broker_execution_weight"] = 0.0
        return payload

    def project(self, *, mode: str) -> Dict[str, Any]:
        desk = require_production_mode(mode)
        result = self._run_worker("project", ["--mode", desk], ("duckdb",))
        if result.get("state") not in {"RECONCILED", "MISMATCH"}:
            return {**result, "authority": "ANALYTICAL_RUNTIME_BLOCKED", "production_influence": False}
        created = _now()
        projection_id = str(result.get("projection_id") or _identifier({
            "mode": desk,
            "source_content_hash": result.get("source_content_hash"),
            "created_at": created,
        }))
        evidence = {**result, "projection_id": projection_id, "created_at": created}
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT OR IGNORE INTO quant_analytics_projections(
                    projection_id,mode,state,sqlite_snapshot_count,sqlite_label_count,
                    duckdb_snapshot_count,duckdb_label_count,source_content_hash,
                    projected_content_hash,reconciled,analytics_path,parquet_paths_json,
                    dependency_json,evidence_json,created_at,service_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    projection_id,
                    desk,
                    str(result.get("state")),
                    int(result.get("sqlite_snapshot_count") or 0),
                    int(result.get("sqlite_label_count") or 0),
                    int(result.get("duckdb_snapshot_count") or 0),
                    int(result.get("duckdb_label_count") or 0),
                    str(result.get("source_content_hash") or ""),
                    str(result.get("projected_content_hash") or ""),
                    int(result.get("state") == "RECONCILED"),
                    str(self.analytics_path),
                    _canonical(result.get("parquet_paths") or {}),
                    _canonical(result.get("dependency") or {}),
                    _canonical(evidence),
                    created,
                    ANALYTICS_SERVICE_VERSION,
                ),
            )
            self.store.conn.commit()
        return {"ok": True, **evidence, "authority": "ANALYTICAL_RUNTIME", "production_influence": False}

    def train_lightgbm(
        self,
        *,
        mode: str,
        horizon: str,
        trial_count: int = 1,
        min_train_days: int = 126,
        test_days: int = 21,
        max_folds: int = 8,
        embargo_days: int = 1,
        projection_result: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        desk = require_production_mode(mode)
        horizon_key = str(horizon or "").lower().strip()
        projection = (
            dict(projection_result)
            if isinstance(projection_result, Mapping)
            else self.project(mode=desk)
        )
        if projection.get("state") != "RECONCILED":
            return {
                "ok": True,
                "state": "ANALYTICS_NOT_RECONCILED",
                "projection": projection,
                "prediction_state": "MODEL_UNAVAILABLE",
                "authority": "VALIDATION_BLOCKED",
                "production_influence": False,
                "broker_execution_weight": 0.0,
            }
        artifact_hint = self.artifact_dir / f"lightgbm_{desk}_{horizon_key}.json"
        result = self._run_worker(
            "train-lightgbm",
            [
                "--mode", desk,
                "--horizon", horizon_key,
                "--artifact", str(artifact_hint),
                "--trial-count", str(max(1, int(trial_count))),
                "--min-train-days", str(max(20, int(min_train_days))),
                "--test-days", str(max(5, int(test_days))),
                "--max-folds", str(max(1, int(max_folds))),
                "--embargo-days", str(max(0, int(embargo_days))),
            ],
            ("duckdb", "lightgbm"),
        )
        if not result.get("model_id"):
            return result
        created = _now()
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT OR IGNORE INTO shadow_lightgbm_models(
                    model_id,mode,horizon,model_family,state,artifact_path,
                    dataset_fingerprint,feature_manifest_hash,observations,trading_days,
                    regimes,trial_count,validation_json,dependency_json,authority,
                    production_weight,created_at,service_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(result["model_id"]),
                    desk,
                    horizon_key,
                    "LIGHTGBM_LAMBDARANK",
                    str(result.get("state") or "UNAVAILABLE"),
                    str(result.get("artifact_path") or ""),
                    str(result.get("dataset_fingerprint") or ""),
                    str(result.get("feature_manifest_hash") or ""),
                    int(result.get("observations") or 0),
                    int(result.get("trading_days") or 0),
                    int(result.get("regimes") or 0),
                    int(result.get("trial_count") or 1),
                    _canonical(result.get("validation") or {}),
                    _canonical(result.get("dependency") or {}),
                    "ACTIVE_VALIDATION",
                    0.0,
                    created,
                    ANALYTICS_SERVICE_VERSION,
                ),
            )
            self.store.conn.commit()
        # A trained candidate now enters the finite model tournament. It is
        # absent from production scoring until the tournament records forward
        # predictions, multiple-testing-adjusted evaluation and a promotion
        # decision with positive weight.
        try:
            tournament = ModelTournamentService(self.store)
            experiment = tournament.register_candidate(
                model_key=str(result["model_id"]),
                library_key="lightgbm",
                mode=desk,
                horizon=horizon_key,
                benchmark_model_key="laddu_current_production",
                dataset_fingerprint=str(result.get("dataset_fingerprint") or ""),
                feature_manifest_hash=str(result.get("feature_manifest_hash") or ""),
                target_name="net_return_plus_20bps",
                validation_deadline=(datetime.now(timezone.utc) + timedelta(days=180)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                required_observations=max(300, int(result.get("observations") or 0)),
                required_regimes=max(3, int(result.get("regimes") or 0)),
                config={
                    "model_family": "LIGHTGBM_LAMBDARANK",
                    "artifact_path": result.get("artifact_path"),
                    "historical_validation": result.get("validation") or {},
                },
            )
            if experiment.get("lifecycle_state") == "EXPERIMENT":
                experiment = tournament.start_validation(experiment["experiment_id"])
            result["model_tournament"] = experiment
            result["paper_activation"] = {
                "ok": True,
                "state": "ACTIVE_VALIDATION",
                "production_influence": False,
                "broker_execution_weight": 0.0,
            }
        except Exception as exc:
            result["model_tournament"] = {
                "ok": False,
                "state": "TOURNAMENT_REGISTRATION_FAILED",
                "reason": str(exc)[:240],
                "production_influence": False,
            }
        return result

    def status(self, mode: Optional[str] = None) -> Dict[str, Any]:
        desk = require_production_mode(mode) if mode else None
        params = (desk,) if desk else ()
        projection_where = "WHERE mode=?" if desk else ""
        model_where = "WHERE mode=?" if desk else ""
        projection = self.store.conn.execute(
            f"""SELECT * FROM quant_analytics_projections {projection_where}
                ORDER BY created_at DESC LIMIT 1""",
            params,
        ).fetchone()
        models = self.store.conn.execute(
            f"""SELECT * FROM shadow_lightgbm_models {model_where}
                ORDER BY created_at DESC""",
            params,
        ).fetchall()
        by_horizon: Dict[str, Dict[str, Any]] = {}
        for raw in models:
            row = dict(raw)
            horizon = str(row.get("horizon") or "")
            if horizon in by_horizon:
                continue
            try:
                validation = json.loads(row.get("validation_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                validation = {}
            by_horizon[horizon] = {
                "state": row.get("state"),
                "model_id": row.get("model_id"),
                "observations": int(row.get("observations") or 0),
                "trading_days": int(row.get("trading_days") or 0),
                "regimes": int(row.get("regimes") or 0),
                "validation": validation,
                "prediction_state": row.get("authority") or "MODEL_UNAVAILABLE",
                "authority": row.get("authority") or "ACTIVE_VALIDATION",
                "lifecycle_state": row.get("authority") or "ACTIVE_VALIDATION",
                "production_influence": False,
            }
        projection_item = dict(projection) if projection else None
        response = {
            "ok": True,
            "version": ANALYTICS_SERVICE_VERSION,
            "mode": desk or "all",
            "duckdb_projection": {
                "state": projection_item.get("state"),
                "reconciled": bool(projection_item.get("reconciled")),
                "snapshot_count": int(projection_item.get("duckdb_snapshot_count") or 0),
                "label_count": int(projection_item.get("duckdb_label_count") or 0),
                "created_at": projection_item.get("created_at"),
            } if projection_item else {
                "state": "NOT_PROJECTED",
                "reconciled": False,
                "snapshot_count": 0,
                "label_count": 0,
                "created_at": None,
            },
            "lightgbm_models": by_horizon,
            "prediction_state": (
                "ACTIVE_VALIDATION" if any(
                    item.get("lifecycle_state") == "ACTIVE_VALIDATION"
                    for item in by_horizon.values()
                ) else "MODEL_UNAVAILABLE"
            ),
            "automatic_activation": False,
            "automatic_promotion": False,
            "broker_execution_weight": 0.0,
        }
        try:
            response["model_tournament"] = ModelTournamentService(self.store).status(mode=desk)
        except Exception as exc:
            response["model_tournament"] = {
                "ok": False, "state": "UNAVAILABLE", "reason": str(exc)[:240]
            }
        return response
