from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class LibraryCapability:
    name: str
    import_name: str
    role: str
    status: str
    version: str | None = None
    reason: str | None = None
    runtime: str = "app_python"
    package_name: str | None = None
    lifecycle_state: str = "ACTIVE_RUNTIME"
    responsibility: str | None = None
    modes: tuple[str, ...] = ("intraday", "delivery")
    production_influence: bool = False


class ResearchLibraryRegistry:
    """Research dependency registry with explicit, non-decorative ownership.

    A library is either an active runtime dependency, an active finite
    validation dependency, or removed.  Package availability never contributes
    score and no capability row is treated as evidence.
    """

    LIBRARIES = [
        ("NumPy", "numpy", "factor and numerical kernels", "numpy", "ACTIVE_RUNTIME", "vectorised point-in-time factor calculations", ("intraday", "delivery")),
        ("pandas", "pandas", "point-in-time feature and outcome tables", "pandas", "ACTIVE_RUNTIME", "canonical tabular transformations and Parquet projections", ("intraday", "delivery")),
        ("SciPy", "scipy", "scikit-learn numerical dependency and statistical primitives", "scipy", "ACTIVE_RUNTIME", "bounded numerical routines used by governed challengers", ("intraday", "delivery")),
        ("scikit-learn", "sklearn", "governed baseline challenger", "scikit-learn", "ACTIVE_VALIDATION", "walk-forward HistGradientBoosting challenger and calibration", ("intraday", "delivery")),
        ("ta", "ta", "technical feature implementation", "ta", "ACTIVE_RUNTIME", "isolated RSI, MACD, ADX, ATR and Bollinger feature projection", ("intraday", "delivery")),
        ("DuckDB", "duckdb", "Parquet analytical and training query plane", "duckdb", "ACTIVE_RUNTIME", "read-only catalogue and point-in-time model datasets", ("intraday", "delivery")),
        ("LightGBM", "lightgbm", "cross-sectional ranking challenger", "lightgbm", "ACTIVE_VALIDATION", "finite LambdaRank tournament with zero automatic production authority", ("intraday", "delivery")),
        ("psycopg", "psycopg", "research authority connectivity", "psycopg", "ACTIVE_RUNTIME", "read-only instrument catalogue and governance publication connectivity", ("intraday", "delivery")),
    ]

    REMOVED = [
        {
            "name": name,
            "package_name": package,
            "lifecycle_state": "REMOVED",
            "reason": "No executable production, research or finite validation owner remains in the current architecture.",
            "production_influence": False,
        }
        for name, package in (
            ("pandas-ta-classic", "pandas-ta-classic"),
            ("TA-Lib", "TA-Lib"),
            ("backtesting.py", "backtesting"),
            ("statsmodels", "statsmodels"),
            ("arch", "arch"),
            ("skfolio", "skfolio"),
            ("smartmoneyconcepts", "smartmoneyconcepts"),
            ("Microsoft Qlib", "pyqlib"),
            ("HKUDS Vibe-Trading", "vibe-trading-ai"),
        )
    ]

    FACTOR_FAMILIES = [
        "price_trend",
        "mtf_alignment",
        "candlestick_pattern",
        "volume_expansion",
        "delivery_accumulation",
        "support_resistance_distance",
        "risk_reward",
        "index_relative_strength",
        "sector_relative_strength",
        "freshness_penalty",
        "contradiction_penalty",
        "technical_equilibrium",
        "session_phase",
        "microstructure_liquidity",
        "volatility_tail",
        "regime_state",
        "fundamental_quality_value_growth",
        "alpha101_style_research_local_82",
        "qlib158_style_research_local_154",
        "gtja191_style_research_local_191",
    ]

    def __init__(self) -> None:
        self._cache: Dict[str, Any] | None = None

    def _programdata_research_python(self) -> Optional[str]:
        env_py = os.environ.get("PROJECT_LADDU_RESEARCH_PYTHON")
        if env_py and Path(env_py).exists():
            return env_py
        programdata = Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "ProjectLaddu"
        pointer = programdata / "runtime" / "research_python.txt"
        try:
            candidate = Path(pointer.read_text(encoding="utf-8").strip())
            if candidate.is_file():
                return str(candidate)
        except OSError:
            pass
        return None

    def _package_version(self, package_name: str | None) -> str | None:
        if not package_name:
            return None
        try:
            return importlib.metadata.version(package_name)
        except Exception:
            return None

    @staticmethod
    def _normalise_spec(spec: Sequence[Any]) -> tuple[str, str, str, str | None, str, str | None, tuple[str, ...]]:
        values = list(spec)
        if len(values) < 4:
            raise ValueError("library spec requires name, import_name, role and package_name")
        name, import_name, role, package_name = values[:4]
        lifecycle_state = str(values[4]) if len(values) > 4 else "ACTIVE_RUNTIME"
        responsibility = str(values[5]) if len(values) > 5 and values[5] is not None else str(role)
        modes = tuple(values[6]) if len(values) > 6 else ("intraday", "delivery")
        return str(name), str(import_name), str(role), package_name, lifecycle_state, responsibility, modes

    def _probe_one(self, *spec: Any) -> LibraryCapability:
        name, import_name, role, package_name, lifecycle_state, responsibility, modes = self._normalise_spec(spec)
        try:
            mod = importlib.import_module(import_name)
            ver = getattr(mod, "__version__", None) or self._package_version(package_name)
            return LibraryCapability(
                name=name, import_name=import_name, role=role, status="installed",
                version=str(ver) if ver else None, package_name=package_name,
                lifecycle_state=lifecycle_state, responsibility=responsibility, modes=modes,
            )
        except Exception as exc:
            ver = self._package_version(package_name)
            if ver:
                return LibraryCapability(
                    name=name, import_name=import_name, role=role, status="installed",
                    version=str(ver), reason="distribution installed; direct import module not used",
                    package_name=package_name, lifecycle_state=lifecycle_state,
                    responsibility=responsibility, modes=modes,
                )
            return LibraryCapability(
                name=name, import_name=import_name, role=role, status="missing",
                reason=str(exc).splitlines()[0][:220], package_name=package_name,
                lifecycle_state=lifecycle_state, responsibility=responsibility, modes=modes,
            )

    def _probe_research_python(self, python_exe: str, libs: List[Sequence[Any]]) -> List[LibraryCapability]:
        normalised = [self._normalise_spec(spec) for spec in libs]
        probe = r'''
import contextlib, importlib, importlib.metadata, io, json, sys
libs = json.loads(sys.argv[1])
out=[]
for name, import_name, role, package_name, lifecycle_state, responsibility, modes in libs:
    try:
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            mod = importlib.import_module(import_name)
        ver = getattr(mod, '__version__', None)
        if not ver and package_name:
            try: ver = importlib.metadata.version(package_name)
            except Exception: ver = None
        out.append({'name':name,'import_name':import_name,'role':role,'status':'installed','version':str(ver) if ver else None,'reason':None,'runtime':'research_venv','package_name':package_name,'lifecycle_state':lifecycle_state,'responsibility':responsibility,'modes':modes,'production_influence':False})
    except Exception as exc:
        ver = None
        if package_name:
            try: ver = importlib.metadata.version(package_name)
            except Exception: ver = None
        if ver:
            out.append({'name':name,'import_name':import_name,'role':role,'status':'installed','version':str(ver),'reason':'distribution installed; direct import module not used','runtime':'research_venv','package_name':package_name,'lifecycle_state':lifecycle_state,'responsibility':responsibility,'modes':modes,'production_influence':False})
        else:
            out.append({'name':name,'import_name':import_name,'role':role,'status':'missing','version':None,'reason':str(exc).splitlines()[0][:220],'runtime':'research_venv','package_name':package_name,'lifecycle_state':lifecycle_state,'responsibility':responsibility,'modes':modes,'production_influence':False})
print(json.dumps(out))
'''
        try:
            env = dict(os.environ)
            env.setdefault("PYTHONUTF8", "1")
            env.setdefault("PYTHONIOENCODING", "utf-8")
            raw = subprocess.check_output(
                [python_exe, "-c", probe, json.dumps(normalised)],
                text=True, timeout=20, env=env,
            )
            rows = []
            for payload in json.loads(raw):
                payload["modes"] = tuple(payload.get("modes") or ())
                rows.append(LibraryCapability(**payload))
            return rows
        except Exception as exc:
            return [LibraryCapability(
                name="Research venv", import_name=python_exe, role="persistent research runtime",
                status="unavailable", reason=str(exc).splitlines()[0][:220], runtime="research_venv",
                lifecycle_state="ACTIVE_RUNTIME", responsibility="isolated research execution",
            )]

    def capabilities(self, refresh: bool = False) -> Dict[str, Any]:
        if self._cache is not None and not refresh:
            return self._cache
        app_caps: List[LibraryCapability] = [self._probe_one(*spec) for spec in self.LIBRARIES]
        research_python = self._programdata_research_python()
        research_caps: List[LibraryCapability] = []
        if research_python:
            research_caps = self._probe_research_python(research_python, self.LIBRARIES)

        by_name: Dict[str, LibraryCapability] = {cap.name: cap for cap in app_caps}
        for cap in research_caps:
            existing = by_name.get(cap.name)
            if cap.status == "installed" or existing is None or existing.status != "installed":
                by_name[cap.name] = cap
        effective = list(by_name.values())
        installed = [cap.name for cap in effective if cap.status == "installed"]
        missing = [cap.name for cap in effective if cap.status != "installed"]
        active_runtime = [cap.name for cap in effective if cap.lifecycle_state == "ACTIVE_RUNTIME"]
        active_validation = [cap.name for cap in effective if cap.lifecycle_state == "ACTIVE_VALIDATION"]
        self._cache = {
            "ok": True,
            "method": "Laddu Dual-Desk Active Edge Research Engine",
            "policy": "Availability is never evidence. Each dependency owns an active runtime or finite validation responsibility; tournament winners are promoted with positive weight and losers are rejected.",
            "app_python": sys.executable,
            "research_python": research_python,
            "libraries": [asdict(cap) for cap in effective],
            "removed_libraries": list(self.REMOVED),
            "app_python_libraries": [asdict(cap) for cap in app_caps],
            "research_venv_libraries": [asdict(cap) for cap in research_caps],
            "installed": installed,
            "available": installed,  # compatibility alias; never means scoring authority
            "missing": missing,
            "active_runtime": active_runtime,
            "active_validation": active_validation,
            "factor_families": list(self.FACTOR_FAMILIES),
            "live_scoring_gate": "ACTIVE_PRODUCTION_MODEL_OR_VALIDATED_FEATURE_ONLY",
            "no_shadow_rule": "Finite SHADOW/ACTIVE_VALIDATION candidates may receive bounded evaluation-paper weight; production weight is enabled only by a healthy governed champion assignment and remains capped at 15%; broker authority remains zero.",
            "duckdb_policy": "DuckDB/Parquet is the isolated point-in-time training plane; PostgreSQL owns operational and model-governance authority, QuestDB owns market time series, and SQLite is rebuildable compatibility only.",
            "lightgbm_policy": "LightGBM competes for each Intraday and Delivery horizon, enters bounded evaluation paper after backtest validation, and can receive production weight only after forward-paper governance.",
                                                            "local_factor_zoo": {
                "alpha101": 82, "qlib158": 154, "gtja191": 191,
                "columns": "point-in-time OHLCV + Laddu desk context",
                "requires_sector_tags": False,
                "admission": "finite feature ablation tournament with post-cost incremental lower confidence bound",
            },
            "install_hint": "The single INSTALL_UPDATE.cmd transaction installs the isolated research runtime. Packages without an executable owner are removed rather than advertised.",
            "ui_contract": "Show model tournament responsibility, sample, evidence, decision and expiry; never show package availability as alpha.",
        }
        return self._cache
