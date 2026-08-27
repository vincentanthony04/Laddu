"""Point-in-time corporate-action adjustment authority.

The authority never infers split/bonus factors from price jumps.  It consumes
only independently verified factors plus an explicit full-history coverage
attestation.  Historical OHLC values are adjusted by actions whose ex-date is
strictly after the candle date; volume uses the separately supplied quantity
factor.  Current/future rows therefore remain on the current share basis.
"""
from __future__ import annotations

from datetime import date, datetime
from functools import reduce
import hashlib
import json
import operator
from typing import Any, Iterable, Mapping

AUTHORITY_NAME = "CorporateActionAdjustmentAuthority"
AUTHORITY_VERSION = "1.1.0-pl42-row-scoped-coverage"


def _day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _factor_product(values: Iterable[float]) -> float:
    return float(reduce(operator.mul, values, 1.0))


class CorporateActionAdjustmentAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    def factors_for(
        self,
        *,
        instrument_key: str,
        candle_date: Any,
        actions: Iterable[Mapping[str, Any]],
        coverage: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        key = str(instrument_key or "").strip()
        day = _day(candle_date)
        cover = dict(coverage or {})
        coverage_key = str(cover.get("instrument_key") or "").strip()
        coverage_complete = bool(
            key
            and coverage_key == key
            and cover.get("complete") is True
            and cover.get("verified_at") not in (None, "")
            and _day(cover.get("coverage_start")) <= day <= _day(cover.get("coverage_end"))
        )
        if not coverage_complete:
            return {
                "authority": self.authority,
                "authority_version": self.authority_version,
                "state": "COVERAGE_UNVERIFIED",
                "decision_usable": False,
                "instrument_key": key,
                "candle_date": day.isoformat(),
                "price_factor": None,
                "volume_factor": None,
                "action_count": 0,
                "lineage_hash": None,
            }

        applicable: list[dict[str, Any]] = []
        for raw in actions:
            row = dict(raw or {})
            if str(row.get("instrument_key") or "").strip() != key:
                continue
            if row.get("verified") is not True:
                return {
                    "authority": self.authority,
                    "authority_version": self.authority_version,
                    "state": "UNVERIFIED_ACTION",
                    "decision_usable": False,
                    "instrument_key": key,
                    "candle_date": day.isoformat(),
                    "price_factor": None,
                    "volume_factor": None,
                    "action_count": 0,
                    "lineage_hash": None,
                }
            if _day(row.get("ex_date")) <= day:
                continue
            pf = float(row.get("price_factor") or 0.0)
            vf = float(row.get("volume_factor") or 0.0)
            if pf <= 0 or vf <= 0:
                raise ValueError("verified corporate-action factors must be positive")
            applicable.append({
                "ex_date": _day(row.get("ex_date")).isoformat(),
                "action_type": str(row.get("action_type") or "OTHER").upper(),
                "price_factor": pf,
                "volume_factor": vf,
                "source_hash": str(row.get("source_hash") or ""),
            })
        applicable.sort(key=lambda row: (row["ex_date"], row["source_hash"], row["action_type"]))
        price_factor = _factor_product(row["price_factor"] for row in applicable)
        volume_factor = _factor_product(row["volume_factor"] for row in applicable)
        lineage = {
            "authority_version": self.authority_version,
            "instrument_key": key,
            "candle_date": day.isoformat(),
            "coverage_source_hash": str(cover.get("source_hash") or ""),
            "actions": applicable,
        }
        lineage_hash = hashlib.sha256(
            json.dumps(lineage, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "state": "ADJUSTED" if applicable else "NO_ACTION_REQUIRED",
            "decision_usable": True,
            "instrument_key": key,
            "candle_date": day.isoformat(),
            "price_factor": price_factor,
            "volume_factor": volume_factor,
            "action_count": len(applicable),
            "lineage_hash": lineage_hash,
        }

    def adjust_candle(
        self,
        candle: Mapping[str, Any],
        *,
        instrument_key: str,
        actions: Iterable[Mapping[str, Any]],
        coverage: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        row = dict(candle or {})
        stamp = row.get("timestamp") or row.get("ts") or row.get("date")
        factors = self.factors_for(
            instrument_key=instrument_key,
            candle_date=stamp,
            actions=actions,
            coverage=coverage,
        )
        if not factors["decision_usable"]:
            return {**row, "corporate_action_adjusted": False, "corporate_action_evidence": factors}
        pf = float(factors["price_factor"])
        vf = float(factors["volume_factor"])
        for field in ("open", "high", "low", "close"):
            if row.get(field) is not None:
                row[field] = float(row[field]) * pf
        if row.get("volume") is not None:
            row["volume"] = float(row["volume"]) * vf
        row["corporate_action_adjusted"] = True
        row["corporate_action_revision"] = self.authority_version
        row["corporate_action_evidence"] = factors
        return row

    @staticmethod
    def duckdb_adjusted_candles_sql(
        *, candles_relation: str = "curated_candles", actions_relation: str = "corporate_actions",
        coverage_relation: str = "corporate_action_coverage",
    ) -> str:
        """Return row-scoped adjusted candles with explicit coverage truth.

        A missing/unverified coverage row never receives an adjustment and is marked
        unusable for corporate-action-qualified ML. Coverage is evaluated per stock
        and candle date; one unresolved symbol cannot poison unrelated instruments.
        """
        c, a, v = candles_relation, actions_relation, coverage_relation
        covered = (
            f"COALESCE(v.complete,FALSE) AND v.verified_at IS NOT NULL "
            f"AND CAST(c.ts AS DATE) BETWEEN v.coverage_start AND v.coverage_end"
        )
        pf = f"COALESCE((SELECT exp(sum(ln(a.price_factor))) FROM {a} a WHERE a.instrument_key=c.instrument_key AND a.verified AND a.ex_date > CAST(c.ts AS DATE)),1.0)"
        vf = f"COALESCE((SELECT exp(sum(ln(a.volume_factor))) FROM {a} a WHERE a.instrument_key=c.instrument_key AND a.verified AND a.ex_date > CAST(c.ts AS DATE)),1.0)"
        return f"""
            SELECT c.* REPLACE (
              CASE WHEN {covered} THEN c.open * {pf} ELSE c.open END AS open,
              CASE WHEN {covered} THEN c.high * {pf} ELSE c.high END AS high,
              CASE WHEN {covered} THEN c.low * {pf} ELSE c.low END AS low,
              CASE WHEN {covered} THEN c.close * {pf} ELSE c.close END AS close,
              CASE WHEN {covered} THEN c.volume * {vf} ELSE c.volume END AS volume
            ),
            CAST(({covered}) AS BOOLEAN) AS corporate_action_adjusted,
            CASE WHEN {covered} THEN 'VERIFIED_MARKET_RANGE' ELSE 'COVERAGE_UNVERIFIED' END AS corporate_action_coverage_state,
            CAST(v.coverage_basis AS VARCHAR) AS corporate_action_coverage_basis,
            CAST(v.source_hash AS VARCHAR) AS corporate_action_coverage_hash
            FROM {c} c
            LEFT JOIN {v} v ON v.instrument_key=c.instrument_key
        """.strip()



DEFAULT_CORPORATE_ACTION_ADJUSTMENT_AUTHORITY = CorporateActionAdjustmentAuthority()
