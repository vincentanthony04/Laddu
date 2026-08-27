"""Governed stock-level broad/sector session-heat analysis.

This authority preserves Laddu's established heat-strip arithmetic (weighted
same-session percentage change) but removes its ability to operate on arbitrary
or partial rows.  It is intentionally *not* a second market-direction model:
IndexDirectionEvidenceAuthority owns Direction/Conviction.  This layer answers
only whether the current governed broad + selected-sector session heat supports
or conflicts with a stock candidate.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping
from core.numeric_semantics import finite_number

from core.market_clock import parse_timestamp

AUTHORITY_NAME = "MarketSectorContextAnalysisAuthority"
AUTHORITY_VERSION = "1.2.0-canonical-finite-same-session"


def _number(value: Any) -> float | None:
    return finite_number(value)


def _row_name(row: Mapping[str, Any]) -> str:
    return str(
        row.get("trading_symbol") or row.get("symbol") or
        row.get("canonical_display_name") or row.get("display_name") or row.get("name") or ""
    ).upper().strip()


class MarketSectorContextAnalysisAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    def analyze(self, rows: Iterable[Mapping[str, Any]], sector_key: str = "") -> dict[str, Any]:
        selected = str(sector_key or "").upper().strip()
        score = 0.0
        evidence: list[str] = []
        broad_used = 0
        sector_used = 0
        rejected = 0
        source_times: set[str] = set()
        parsed_source_times = []

        for raw in rows or ():
            row = dict(raw or {})
            # Only the atomic market-context authority can make a row usable by
            # this analysis. Diagnostic/fallback breadth remains visible but has
            # zero stock-candidate influence.
            if row.get("direction_authority_ready") is not True:
                rejected += 1
                continue
            if str(row.get("market_context_authority") or "") != "MarketContextSnapshotAuthority":
                rejected += 1
                continue
            name = _row_name(row)
            display = str(row.get("canonical_display_name") or row.get("display_name") or row.get("name") or "").upper().strip()
            change = _number(row.get("change_pct"))
            if change is None:
                rejected += 1
                continue

            is_nifty = name in {"NIFTY", "NIFTY50"} or display == "NIFTY 50"
            is_sensex = name == "SENSEX" or display == "SENSEX"
            is_bank = name in {"BANK", "NIFTYBANK"} or display == "NIFTY BANK"
            is_broad = is_nifty or is_sensex or is_bank
            is_selected = bool(selected) and (
                name == selected or selected in name or selected in display.replace("NIFTY ", "")
            )
            if not is_broad and not is_selected:
                continue

            weight = 1.2 if (is_nifty or is_sensex) else 1.0
            if is_selected:
                weight = 1.8
                sector_used += 1
            elif is_broad:
                broad_used += 1
            score += change * weight
            if is_selected or abs(change) >= 0.20:
                direction = str(row.get("direction") or "").upper()
                evidence.append(f"{display or name} {change:+.2f}% {direction}".strip())
            stamp = str(row.get("direction_source_time") or row.get("source_time") or "").strip()
            parsed_stamp = parse_timestamp(stamp) if stamp else None
            if parsed_stamp is None:
                rejected += 1
                # The contribution cannot remain in an atomic same-session
                # context when its source clock is unknown. Undo it.
                score -= change * weight
                if is_selected:
                    sector_used -= 1
                elif is_broad:
                    broad_used -= 1
                if evidence and (is_selected or abs(change) >= 0.20):
                    evidence.pop()
                continue
            source_times.add(parsed_stamp.isoformat(timespec="seconds"))
            parsed_source_times.append(parsed_stamp)

        source_dates = sorted({stamp.date().isoformat() for stamp in parsed_source_times})
        same_session = bool(parsed_source_times) and len(source_dates) == 1
        max_source_skew_seconds = 120.0
        source_skew_seconds = (
            (max(parsed_source_times) - min(parsed_source_times)).total_seconds()
            if len(parsed_source_times) >= 2 else 0.0 if parsed_source_times else None
        )
        atomic_time = bool(same_session and source_skew_seconds is not None and source_skew_seconds <= max_source_skew_seconds)

        state = "neutral"
        if score > 0.35:
            state = "supportive"
        elif score < -0.35:
            state = "weak"
        usable = bool(broad_used and (not selected or sector_used) and atomic_time)
        if not usable:
            # Preserve partial evidence for diagnostics but do not present it as
            # a complete broad+sector analysis to the candidate engine.
            state = "unavailable"
        return {
            "ok": usable,
            "state": state,
            "score": round(score, 2) if usable else None,
            "stock_sector": selected or None,
            "summary": ", ".join(evidence[:4]) if evidence else "governed broad/sector session heat unavailable",
            "evidence": evidence,
            "broad_rows_used": broad_used,
            "sector_rows_used": sector_used,
            "rejected_non_governed_rows": rejected,
            "source_times": sorted(source_times),
            "source_session_dates": source_dates,
            "same_session_evidence": same_session,
            "source_skew_seconds": round(source_skew_seconds, 3) if source_skew_seconds is not None else None,
            "max_source_skew_seconds": max_source_skew_seconds,
            "authority": self.authority,
            "authority_version": self.authority_version,
            "direction_authority": "IndexDirectionEvidenceAuthority",
            "meaning": "same-session stock support/conflict heat; not market Direction/Conviction",
        }


DEFAULT_MARKET_SECTOR_CONTEXT_ANALYSIS_AUTHORITY = MarketSectorContextAnalysisAuthority()
