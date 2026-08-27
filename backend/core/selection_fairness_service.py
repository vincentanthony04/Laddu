"""Governed candidate-discovery fairness and selection-audit authority.

This module deliberately does *not* change trade confidence.  It controls only
which eligible symbols receive scarce analysis capacity first, and measures
whether the discovery funnel systematically starves sectors, capitalization,
liquidity, price, volatility, or data-quality cohorts.

The service is deterministic, standard-library only, and safe to use in live
scanner loops.  A fairness adjustment is capped and fully explained on every
row; manual/user priorities remain protected.  Promotion remains the sole
responsibility of the production Evidence Engine.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import statistics
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


FAIRNESS_VERSION = "selection-fairness-1.0.0"
DEFAULT_DIMENSIONS = (
    "sector_bucket",
    "market_cap_bucket",
    "liquidity_bucket",
    "price_bucket",
    "volatility_bucket",
    "data_quality_bucket",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _num(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "—"):
            return None
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _symbol(row: Mapping[str, Any]) -> str:
    return _text(row.get("symbol") or row.get("trading_symbol")).upper()


def _source_is_protected(row: Mapping[str, Any]) -> bool:
    source = _text(row.get("source")).lower()
    return bool(row.get("pinned")) or any(token in source for token in ("manual", "search", "user", "pinned"))


@dataclass(frozen=True)
class FairnessPolicy:
    max_adjustment_points: float = 18.0
    max_starvation_bonus: float = 10.0
    max_representation_bonus: float = 8.0
    max_sector_share: float = 0.40
    minimum_group_size: int = 5
    adverse_impact_floor: float = 0.80
    unknown_metadata_soft_limit: float = 0.20


class SelectionFairnessService:
    """Fair analysis scheduling plus cohort-level selection auditing."""

    def __init__(self, store: Any = None, policy: FairnessPolicy | None = None):
        self.store = store
        self.policy = policy or FairnessPolicy()
        if store is not None and not hasattr(store, "write_lock"):
            store.write_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Cohort classification
    # ------------------------------------------------------------------
    @staticmethod
    def classify(row: Mapping[str, Any]) -> Dict[str, str]:
        sector = _text(row.get("sector") or row.get("industry") or row.get("sector_label")) or "Unknown"

        explicit_cap = _text(row.get("market_cap_bucket")).lower()
        index_name = _text(row.get("segment_index") or row.get("index_name") or row.get("index_membership")).lower()
        market_cap = _num(row.get("market_cap") or row.get("market_cap_cr"))
        if explicit_cap:
            cap = explicit_cap.replace(" ", "_")
        elif market_cap is not None:
            # INR crore bands; intentionally broad and descriptive rather than
            # pretending one exchange-wide market-cap taxonomy is universal.
            cap = "large" if market_cap >= 50000 else "mid" if market_cap >= 10000 else "small" if market_cap >= 1000 else "micro"
        elif "nifty 50" in index_name or "sensex" in index_name or "nifty 100" in index_name:
            cap = "large"
        elif "midcap" in index_name:
            cap = "mid"
        elif "smallcap" in index_name:
            cap = "small"
        else:
            cap = "unknown"

        volume = _num(row.get("avg_traded_qty") or row.get("average_volume") or row.get("volume"))
        turnover = _num(row.get("avg_turnover") or row.get("turnover"))
        liquidity_value = turnover if turnover is not None else volume
        if liquidity_value is None:
            liquidity = "unknown"
        elif turnover is not None:
            liquidity = "high" if turnover >= 50_000_000 else "medium" if turnover >= 10_000_000 else "low" if turnover >= 2_000_000 else "very_low"
        else:
            liquidity = "high" if liquidity_value >= 1_000_000 else "medium" if liquidity_value >= 200_000 else "low" if liquidity_value >= 50_000 else "very_low"

        price = _num(row.get("ltp") or row.get("last_price") or row.get("close"))
        if price is None:
            price_bucket = "unknown"
        elif price < 20:
            price_bucket = "sub_20"
        elif price < 100:
            price_bucket = "20_100"
        elif price < 500:
            price_bucket = "100_500"
        elif price < 2000:
            price_bucket = "500_2000"
        else:
            price_bucket = "above_2000"

        atr_pct = _num(row.get("atr_pct") or row.get("realized_volatility_pct"))
        movement = abs(_num(row.get("change_pct") or row.get("pChange")) or 0.0)
        vol_value = atr_pct if atr_pct is not None else movement
        volatility = "calm" if vol_value < 1.0 else "normal" if vol_value < 2.5 else "active" if vol_value < 5.0 else "extreme"

        # Data quality is intentionally about availability/provenance only.  It
        # must never add trade-conviction points.
        fields = (
            row.get("instrument_key"),
            row.get("ltp") or row.get("last_price") or row.get("close"),
            row.get("volume") or row.get("avg_traded_qty"),
            row.get("sector") or row.get("industry") or row.get("sector_label"),
        )
        present = sum(value not in (None, "", "—", 0) for value in fields)
        quality = "complete" if present == len(fields) else "usable" if present >= 3 else "partial" if present >= 2 else "sparse"

        return {
            "sector_bucket": sector,
            "market_cap_bucket": cap,
            "liquidity_bucket": liquidity,
            "price_bucket": price_bucket,
            "volatility_bucket": volatility,
            "data_quality_bucket": quality,
        }

    @staticmethod
    def _base_priority(row: Mapping[str, Any]) -> float:
        explicit = _num(row.get("priority_score") or row.get("analysis_priority_score"))
        if explicit is not None:
            return max(0.0, min(100.0, explicit))
        movement = abs(_num(row.get("change_pct") or row.get("pChange")) or 0.0)
        volume = max(0.0, _num(row.get("volume") or row.get("avg_traded_qty")) or 0.0)
        # Same broad intent as the legacy activity rank, but normalized and
        # bounded so a single extreme print cannot monopolize the queue.
        activity = min(45.0, movement * 7.5) + min(35.0, math.log10(volume + 1.0) * 6.0)
        return max(0.0, min(100.0, activity))

    def rank_for_analysis(self, rows: Iterable[Mapping[str, Any]], cap: int, *,
                          max_sector_share: Optional[float] = None) -> List[Dict[str, Any]]:
        """Return a deterministic, fairness-aware analysis queue.

        The function only schedules analysis.  It never modifies evidence,
        confidence, readiness, entry maps, or promotion outcomes.
        """
        cap = max(0, int(cap))
        if cap == 0:
            return []

        deduped: Dict[str, Dict[str, Any]] = {}
        for source in rows:
            row = dict(source or {})
            symbol = _symbol(row)
            if not symbol or not row.get("instrument_key"):
                continue
            row["symbol"] = symbol
            row.update(self.classify(row))
            row["base_analysis_priority"] = round(self._base_priority(row), 4)
            current = deduped.get(symbol)
            if current is None or row["base_analysis_priority"] > current["base_analysis_priority"] or _source_is_protected(row):
                deduped[symbol] = row

        candidates = list(deduped.values())
        if not candidates:
            return []

        sector_counts: Dict[str, int] = {}
        for row in candidates:
            sector_counts[row["sector_bucket"]] = sector_counts.get(row["sector_bucket"], 0) + 1
        median_sector_count = statistics.median(sector_counts.values()) if sector_counts else 1.0

        for row in candidates:
            group_count = sector_counts.get(row["sector_bucket"], 1)
            representation_bonus = 0.0
            if median_sector_count > 0 and group_count < median_sector_count:
                representation_bonus = min(
                    self.policy.max_representation_bonus,
                    (median_sector_count - group_count) / median_sector_count * self.policy.max_representation_bonus,
                )
            starvation_cycles = max(0.0, _num(row.get("cycles_since_analysis") or row.get("scan_age_cycles")) or 0.0)
            starvation_bonus = min(self.policy.max_starvation_bonus, starvation_cycles * 1.25)
            protected_bonus = self.policy.max_adjustment_points if _source_is_protected(row) else 0.0
            fairness_adjustment = min(
                self.policy.max_adjustment_points,
                representation_bonus + starvation_bonus + protected_bonus,
            )
            row["fairness_adjustment"] = round(fairness_adjustment, 4)
            row["analysis_priority_score"] = round(min(118.0, row["base_analysis_priority"] + fairness_adjustment), 4)
            row["analysis_priority_breakdown"] = {
                "base_activity_or_priority": round(row["base_analysis_priority"], 4),
                "underrepresented_sector_bonus": round(representation_bonus, 4),
                "starvation_bonus": round(starvation_bonus, 4),
                "manual_priority_protected": _source_is_protected(row),
                "trade_confidence_affected": False,
            }
            row["fairness_version"] = FAIRNESS_VERSION

        candidates.sort(
            key=lambda row: (
                0 if _source_is_protected(row) else 1,
                -float(row["analysis_priority_score"]),
                -float(row["base_analysis_priority"]),
                row["symbol"],
            )
        )

        share = self.policy.max_sector_share if max_sector_share is None else float(max_sector_share)
        share = max(0.10, min(1.0, share))
        max_per_sector = max(1, int(math.ceil(cap * share)))
        selected: List[Dict[str, Any]] = []
        deferred: List[Dict[str, Any]] = []
        selected_sector_counts: Dict[str, int] = {}
        for row in candidates:
            sector = row["sector_bucket"]
            if _source_is_protected(row) or selected_sector_counts.get(sector, 0) < max_per_sector:
                selected.append(row)
                selected_sector_counts[sector] = selected_sector_counts.get(sector, 0) + 1
            else:
                deferred.append(row)
            if len(selected) >= cap:
                break
        if len(selected) < cap:
            used = {row["symbol"] for row in selected}
            for row in deferred + candidates:
                if row["symbol"] in used:
                    continue
                selected.append(row)
                used.add(row["symbol"])
                if len(selected) >= cap:
                    break
        return selected[:cap]

    # ------------------------------------------------------------------
    # Fairness audit
    # ------------------------------------------------------------------
    def audit(self, eligible_rows: Iterable[Mapping[str, Any]], analyzed_rows: Iterable[Mapping[str, Any]],
              promoted_rows: Optional[Iterable[Mapping[str, Any]]] = None, *, persist: bool = False,
              desk: str = "all") -> Dict[str, Any]:
        eligible = self._dedupe_and_classify(eligible_rows)
        analyzed = self._dedupe_and_classify(analyzed_rows)
        promoted = self._dedupe_and_classify(promoted_rows or [])
        eligible_symbols = set(eligible)
        analyzed = {k: v for k, v in analyzed.items() if k in eligible_symbols}
        promoted = {k: v for k, v in promoted.items() if k in eligible_symbols}

        dimensions: Dict[str, Any] = {}
        dimension_scores: List[float] = []
        for dimension in DEFAULT_DIMENSIONS:
            result = self._dimension_audit(list(eligible.values()), list(analyzed.values()), list(promoted.values()), dimension)
            dimensions[dimension] = result
            if result["state"] == "MEASURED":
                dimension_scores.append(float(result["score"]))

        fairness_score = round(statistics.fmean(dimension_scores), 2) if dimension_scores else None
        state = "MEASURED" if dimension_scores else "INSUFFICIENT_DATA"
        result = {
            "ok": True,
            "fairness_version": FAIRNESS_VERSION,
            "as_of": _now(),
            "desk": str(desk or "all").lower(),
            "policy": "Fair analysis opportunity, not forced equal promotions. Trade confidence is never adjusted by fairness scheduling.",
            "eligible_count": len(eligible),
            "analyzed_count": len(analyzed),
            "promoted_count": len(promoted),
            "analysis_coverage_rate": round(len(analyzed) / len(eligible), 6) if eligible else 0.0,
            "promotion_rate": round(len(promoted) / len(eligible), 6) if eligible else 0.0,
            "state": state,
            "fairness_score": fairness_score,
            "dimensions": dimensions,
            "gates": {
                "minimum_eligible_universe": len(eligible) >= 50,
                "minimum_analyzed_sample": len(analyzed) >= 20,
                "all_measured_dimensions_above_80": bool(dimension_scores) and all(score >= 80.0 for score in dimension_scores),
                "unknown_metadata_below_20pct": all(
                    dim.get("unknown_share", 0.0) <= self.policy.unknown_metadata_soft_limit
                    for dim in dimensions.values()
                    if dim.get("state") == "MEASURED"
                ),
            },
        }
        result["passes_fairness_gate"] = all(result["gates"].values())
        if persist and self.store is not None:
            self._persist(result)
        return result

    def _dimension_audit(self, eligible: Sequence[Mapping[str, Any]], analyzed: Sequence[Mapping[str, Any]],
                         promoted: Sequence[Mapping[str, Any]], dimension: str) -> Dict[str, Any]:
        eligible_counts = self._counts(eligible, dimension)
        analyzed_counts = self._counts(analyzed, dimension)
        promoted_counts = self._counts(promoted, dimension)
        total_e = sum(eligible_counts.values())
        total_a = sum(analyzed_counts.values())
        total_p = sum(promoted_counts.values())
        groups = []
        rates = []
        representation_gaps = []
        for group in sorted(eligible_counts):
            e = eligible_counts[group]
            a = analyzed_counts.get(group, 0)
            p = promoted_counts.get(group, 0)
            selection_rate = a / e if e else 0.0
            eligible_share = e / total_e if total_e else 0.0
            analyzed_share = a / total_a if total_a else 0.0
            representation_ratio = analyzed_share / eligible_share if eligible_share and total_a else 0.0
            groups.append({
                "group": group,
                "eligible": e,
                "analyzed": a,
                "promoted": p,
                "analysis_rate": round(selection_rate, 6),
                "promotion_rate": round(p / e, 6) if e else 0.0,
                "eligible_share": round(eligible_share, 6),
                "analyzed_share": round(analyzed_share, 6),
                "representation_ratio": round(representation_ratio, 6),
            })
            if e >= self.policy.minimum_group_size:
                rates.append(selection_rate)
                representation_gaps.append(abs(analyzed_share - eligible_share))

        if len(rates) < 2 or total_a < self.policy.minimum_group_size:
            return {
                "state": "INSUFFICIENT_DATA",
                "score": None,
                "adverse_impact_ratio": None,
                "max_representation_gap": None,
                "unknown_share": round(eligible_counts.get("unknown", eligible_counts.get("Unknown", 0)) / total_e, 6) if total_e else 0.0,
                "groups": groups,
            }
        max_rate = max(rates)
        adverse = min(rates) / max_rate if max_rate > 0 else 1.0
        max_gap = max(representation_gaps, default=0.0)
        score = max(0.0, min(100.0, adverse * 70.0 + (1.0 - min(1.0, max_gap * 4.0)) * 30.0))
        unknown = eligible_counts.get("unknown", 0) + eligible_counts.get("Unknown", 0)
        return {
            "state": "MEASURED",
            "score": round(score, 2),
            "adverse_impact_ratio": round(adverse, 6),
            "passes_80pct_rule": adverse >= self.policy.adverse_impact_floor,
            "max_representation_gap": round(max_gap, 6),
            "unknown_share": round(unknown / total_e, 6) if total_e else 0.0,
            "groups": groups,
        }

    @staticmethod
    def _counts(rows: Sequence[Mapping[str, Any]], dimension: str) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for row in rows:
            value = _text(row.get(dimension)) or "unknown"
            out[value] = out.get(value, 0) + 1
        return out

    def _dedupe_and_classify(self, rows: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for source in rows:
            row = dict(source or {})
            symbol = _symbol(row)
            if not symbol:
                continue
            row["symbol"] = symbol
            row.update(self.classify(row))
            out[symbol] = row
        return out

    @staticmethod
    def _ensure_schema(conn: Any) -> None:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS selection_fairness_audits (
          audit_id TEXT PRIMARY KEY,
          desk TEXT NOT NULL,
          as_of TEXT NOT NULL,
          fairness_version TEXT NOT NULL,
          fairness_score REAL,
          passes_gate INTEGER NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_selection_fairness_time ON selection_fairness_audits(desk,as_of);
        """)
        conn.commit()

    def _persist(self, result: Dict[str, Any]) -> None:
        canonical = json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)
        audit_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        result["audit_id"] = audit_id
        with self.store.write_lock:
            self._ensure_schema(self.store.conn)
            self.store.conn.execute(
                "INSERT OR REPLACE INTO selection_fairness_audits(audit_id,desk,as_of,fairness_version,fairness_score,passes_gate,payload_json) VALUES(?,?,?,?,?,?,?)",
                (audit_id, result["desk"], result["as_of"], FAIRNESS_VERSION, result.get("fairness_score"), int(bool(result.get("passes_fairness_gate"))), canonical),
            )
            self.store.conn.commit()

    def status(self, desk: str = "") -> Dict[str, Any]:
        if self.store is None:
            return {"ok": True, "fairness_version": FAIRNESS_VERSION, "audits": []}
        self._ensure_schema(self.store.conn)
        if desk:
            rows = self.store.conn.execute(
                "SELECT payload_json FROM selection_fairness_audits WHERE desk=? ORDER BY as_of DESC LIMIT 20",
                (str(desk).lower(),),
            ).fetchall()
        else:
            rows = self.store.conn.execute(
                "SELECT payload_json FROM selection_fairness_audits ORDER BY as_of DESC LIMIT 50"
            ).fetchall()
        return {"ok": True, "fairness_version": FAIRNESS_VERSION, "audits": [json.loads(row[0]) for row in rows]}
