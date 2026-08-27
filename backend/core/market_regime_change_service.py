"""Point-in-time market-regime classification and transition detection.

The service converts the already analysed cash-equity population into an
append-only market state.  It does not fetch history, mutate trading decisions,
or grant ML influence.  Regime transitions require persistence and a measured
change score so one noisy scan cannot relabel the market.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import statistics
import threading
from typing import Any, Dict, Iterable, Mapping, Optional

from core.sector_classification_authority import DEFAULT_SECTOR_CLASSIFICATION_AUTHORITY


AUTHORITY_NAME = "MarketRegimeAuthority"
AUTHORITY_VERSION = "1.1.0"
SERVICE_VERSION = "market-regime-change-1.1.0"
REGIMES = ("BULL", "BEAR", "RANGE", "VOLATILE", "SECTOR_ROTATION")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _stdev(values: list[float]) -> float:
    return statistics.pstdev(values) if len(values) >= 2 else 0.0


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class MarketRegimeChangeService:
    """Append-only online regime authority with persistence confirmation.

    This is the cross-sectional regime authority, distinct from the atomic
    index/sector Direction authority.  It consumes an already analysed equity
    population and never owns quote/breadth acquisition.
    """

    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    def __init__(self, store: Any):
        self.store = store
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.RLock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_regime_observations (
                  observation_id TEXT PRIMARY KEY,
                  observed_at TEXT NOT NULL,
                  candidate_regime TEXT NOT NULL,
                  confirmed_regime TEXT NOT NULL,
                  previous_regime TEXT,
                  transition_state TEXT NOT NULL,
                  persistence_count INTEGER NOT NULL,
                  confidence REAL NOT NULL,
                  change_probability REAL NOT NULL,
                  breadth_ratio REAL NOT NULL,
                  mean_change_pct REAL NOT NULL,
                  cross_section_volatility REAL NOT NULL,
                  trend_score REAL NOT NULL,
                  sector_dispersion REAL NOT NULL,
                  observation_count INTEGER NOT NULL,
                  reasons_json TEXT NOT NULL,
                  input_hash TEXT NOT NULL UNIQUE,
                  service_version TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_market_regime_observed
                  ON market_regime_observations(observed_at);
                CREATE INDEX IF NOT EXISTS ix_market_regime_confirmed
                  ON market_regime_observations(confirmed_regime,observed_at);
                """
            )
            self.store.conn.commit()

    @staticmethod
    def population_metrics(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
        changes: list[float] = []
        trends: list[float] = []
        sector_changes: dict[str, list[float]] = {}
        for raw in rows:
            row = dict(raw or {})
            change = _number(
                row.get("change_pct")
                if row.get("change_pct") is not None
                else row.get("percent_change")
            )
            trend = _number(
                row.get("trend_score")
                if row.get("trend_score") is not None
                else row.get("momentum_score")
            )
            if change is not None:
                changes.append(change)
                classified = DEFAULT_SECTOR_CLASSIFICATION_AUTHORITY.classify(
                    row.get("sector") or row.get("sector_name"), row.get("industry")
                )
                sector = str(classified.get("market_sector_key") or classified.get("fundamental_sector") or "UNKNOWN").upper()
                sector_changes.setdefault(sector, []).append(change)
            if trend is not None:
                trends.append(trend)
        advances = sum(value > 0.05 for value in changes)
        declines = sum(value < -0.05 for value in changes)
        unchanged = max(0, len(changes) - advances - declines)
        sector_means = [_mean(values) for key, values in sector_changes.items() if key != "UNKNOWN" and values]
        return {
            "observation_count": len(changes),
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            "breadth_ratio": advances / len(changes) if changes else 0.5,
            "mean_change_pct": _mean(changes),
            "cross_section_volatility": _stdev(changes),
            "trend_score": _mean(trends),
            "sector_dispersion": _stdev(sector_means),
            "sector_count": len(sector_means),
        }

    def _history(self, limit: int = 60) -> list[Dict[str, Any]]:
        rows = self.store.conn.execute(
            """SELECT * FROM market_regime_observations
               ORDER BY observed_at DESC LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        output = []
        for row in rows:
            try:
                output.append(dict(row))
            except Exception:
                names = [item[1] for item in self.store.conn.execute(
                    "PRAGMA table_info(market_regime_observations)"
                ).fetchall()]
                output.append(dict(zip(names, row)))
        return output

    @staticmethod
    def _classify(metrics: Mapping[str, Any], baseline: Mapping[str, float]) -> tuple[str, list[str]]:
        breadth = float(metrics["breadth_ratio"])
        mean_change = float(metrics["mean_change_pct"])
        volatility = float(metrics["cross_section_volatility"])
        trend = float(metrics["trend_score"])
        sector_dispersion = float(metrics["sector_dispersion"])
        base_vol = max(0.35, float(baseline.get("volatility") or 0.0))
        reasons: list[str] = []
        if volatility >= max(1.8, base_vol * 1.55):
            reasons.append("cross-sectional volatility expanded materially")
            return "VOLATILE", reasons
        if (
            int(metrics.get("sector_count") or 0) >= 5
            and 0.34 <= breadth <= 0.66
            and abs(mean_change) <= 0.65
            and sector_dispersion >= max(0.85, base_vol * 0.75)
        ):
            reasons.append("sector dispersion is high while broad direction is mixed")
            return "SECTOR_ROTATION", reasons
        if mean_change >= 0.22 and breadth >= 0.55 and trend >= -2.0:
            reasons.extend(("broad returns are positive", "advance breadth is supportive"))
            return "BULL", reasons
        if mean_change <= -0.22 and breadth <= 0.45 and trend <= 2.0:
            reasons.extend(("broad returns are negative", "decline breadth is dominant"))
            return "BEAR", reasons
        reasons.append("directional evidence is balanced or weak")
        return "RANGE", reasons

    @staticmethod
    def _baseline(history: list[Mapping[str, Any]]) -> Dict[str, float]:
        return {
            "mean_change": _mean([float(row.get("mean_change_pct") or 0.0) for row in history]),
            "breadth": _mean([float(row.get("breadth_ratio") or 0.5) for row in history]),
            "volatility": _mean([float(row.get("cross_section_volatility") or 0.0) for row in history]),
            "trend": _mean([float(row.get("trend_score") or 0.0) for row in history]),
            "sector_dispersion": _mean([float(row.get("sector_dispersion") or 0.0) for row in history]),
        }

    @staticmethod
    def _change_probability(metrics: Mapping[str, Any], history: list[Mapping[str, Any]], candidate: str) -> float:
        if not history:
            return 0.0
        fields = (
            ("mean_change_pct", 0.35),
            ("breadth_ratio", 0.08),
            ("cross_section_volatility", 0.30),
            ("trend_score", 8.0),
            ("sector_dispersion", 0.25),
        )
        score = 0.0
        for field, floor in fields:
            values = [float(row.get(field) or 0.0) for row in history]
            scale = max(floor, _stdev(values))
            score += abs(float(metrics[field]) - _mean(values)) / scale
        if candidate != str(history[0].get("confirmed_regime") or "RANGE"):
            score += 1.25
        return round(_clamp(1.0 - math.exp(-score / 6.0)), 6)

    def observe_population(self, rows: Iterable[Mapping[str, Any]], *, observed_at: str = "") -> Dict[str, Any]:
        metrics = self.population_metrics(rows)
        stamp = str(observed_at or _now())
        if int(metrics["observation_count"]) < 8:
            latest = self.latest()
            return {
                "ok": False,
                "state": "INSUFFICIENT_POPULATION",
                "regime": latest.get("confirmed_regime") or "UNKNOWN",
                "candidate_regime": "UNKNOWN",
                "transition_state": "NOT_OBSERVED",
                "observation_count": metrics["observation_count"],
                "production_influence": False,
                "service_version": SERVICE_VERSION,
                "authority": AUTHORITY_NAME,
                "authority_version": AUTHORITY_VERSION,
            }
        history = self._history(60)
        baseline = self._baseline(history)
        candidate, reasons = self._classify(metrics, baseline)
        previous = str(history[0].get("confirmed_regime") or "") if history else ""
        last_candidate = str(history[0].get("candidate_regime") or "") if history else ""
        persistence = int(history[0].get("persistence_count") or 0) + 1 if last_candidate == candidate else 1
        probability = self._change_probability(metrics, history, candidate)
        # Preserve the strongest measured break while the same candidate is
        # awaiting persistence. Otherwise the pending observations dilute the
        # baseline and make a real transition mathematically impossible to
        # confirm on its third observation.
        if history and last_candidate == candidate:
            probability = max(probability, float(history[0].get("change_probability") or 0.0))
        if not previous:
            confirmed = candidate
            transition = "INITIAL"
        elif candidate == previous:
            confirmed = previous
            transition = "STABLE"
        elif (persistence >= 3 and probability >= 0.55) or (persistence >= 2 and probability >= 0.82):
            confirmed = candidate
            transition = "CONFIRMED_CHANGE"
            reasons.append(f"{candidate} persisted for {persistence} observations")
        else:
            confirmed = previous
            transition = "WATCH"
            reasons.append(f"candidate {candidate} requires persistence before confirmation")
        directional_strength = min(1.0, abs(float(metrics["mean_change_pct"])) / 1.5)
        breadth_strength = min(1.0, abs(float(metrics["breadth_ratio"]) - 0.5) * 2.0)
        confidence = round(_clamp(0.35 + 0.30 * directional_strength + 0.20 * breadth_strength + 0.15 * min(1.0, persistence / 3.0)), 6)
        material = {
            "observed_at": stamp,
            "candidate": candidate,
            "confirmed": confirmed,
            "metrics": metrics,
            "service_version": SERVICE_VERSION,
        }
        input_hash = hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()
        observation_id = input_hash[:32]
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT OR IGNORE INTO market_regime_observations(
                     observation_id,observed_at,candidate_regime,confirmed_regime,previous_regime,
                     transition_state,persistence_count,confidence,change_probability,breadth_ratio,
                     mean_change_pct,cross_section_volatility,trend_score,sector_dispersion,
                     observation_count,reasons_json,input_hash,service_version,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    observation_id, stamp, candidate, confirmed, previous or None, transition,
                    persistence, confidence, probability, float(metrics["breadth_ratio"]),
                    float(metrics["mean_change_pct"]), float(metrics["cross_section_volatility"]),
                    float(metrics["trend_score"]), float(metrics["sector_dispersion"]),
                    int(metrics["observation_count"]), _canonical(reasons), input_hash,
                    SERVICE_VERSION, _now(),
                ),
            )
            self.store.conn.commit()
        return {
            "ok": True,
            "state": "REGIME_OBSERVED",
            "observation_id": observation_id,
            "observed_at": stamp,
            "regime": confirmed,
            "confirmed_regime": confirmed,
            "candidate_regime": candidate,
            "previous_regime": previous or None,
            "transition_state": transition,
            "persistence_count": persistence,
            "confidence": confidence,
            "change_probability": probability,
            "metrics": metrics,
            "reasons": reasons,
            "production_influence": False,
            "service_version": SERVICE_VERSION,
            "authority": AUTHORITY_NAME,
            "authority_version": AUTHORITY_VERSION,
        }

    def latest(self) -> Dict[str, Any]:
        rows = self._history(1)
        if not rows:
            return {
                "ok": True,
                "state": "NO_REGIME_OBSERVATIONS",
                "confirmed_regime": "UNKNOWN",
                "candidate_regime": "UNKNOWN",
                "transition_state": "NOT_OBSERVED",
                "production_influence": False,
                "service_version": SERVICE_VERSION,
                "authority": AUTHORITY_NAME,
                "authority_version": AUTHORITY_VERSION,
            }
        row = rows[0]
        try:
            reasons = json.loads(str(row.get("reasons_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            reasons = []
        return {
            "ok": True,
            "state": "REGIME_AVAILABLE",
            "confirmed_regime": row.get("confirmed_regime"),
            "candidate_regime": row.get("candidate_regime"),
            "previous_regime": row.get("previous_regime"),
            "transition_state": row.get("transition_state"),
            "persistence_count": int(row.get("persistence_count") or 0),
            "confidence": float(row.get("confidence") or 0.0),
            "change_probability": float(row.get("change_probability") or 0.0),
            "observed_at": row.get("observed_at"),
            "observation_count": int(row.get("observation_count") or 0),
            "reasons": reasons,
            "production_influence": False,
            "service_version": SERVICE_VERSION,
            "authority": AUTHORITY_NAME,
            "authority_version": AUTHORITY_VERSION,
        }

    def status(self) -> Dict[str, Any]:
        latest = self.latest()
        total = self.store.conn.execute("SELECT COUNT(*) FROM market_regime_observations").fetchone()[0]
        regimes = self.store.conn.execute(
            "SELECT COUNT(DISTINCT confirmed_regime) FROM market_regime_observations WHERE confirmed_regime<>'UNKNOWN'"
        ).fetchone()[0]
        changes = self.store.conn.execute(
            "SELECT COUNT(*) FROM market_regime_observations WHERE transition_state='CONFIRMED_CHANGE'"
        ).fetchone()[0]
        return {
            **latest,
            "regime_observations": int(total or 0),
            "observed_regimes": int(regimes or 0),
            "confirmed_changes": int(changes or 0),
            "required_confirmation_observations": 3,
            "method": "cross-sectional state classifier plus persistent multivariate change score",
        }
