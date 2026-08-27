"""PostgreSQL repository for v69.8 universe, coverage, and scanner evidence."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from core.incremental_data_pipeline import CoverageRecord, FetchPlan, TimeRange
from core.intelligent_scanner import ScanRun
from core.universe_authority import CanonicalUniverse, LifecycleEvent, UniverseSnapshot


class OperationalUniverseRepository:
    """Production-only repository; intentionally has no SQLite fallback."""
    def __init__(self, authority: Any, read_authority: Any | None = None):
        if authority is None or not hasattr(authority, "transaction"):
            raise ValueError("operational PostgreSQL authority is required")
        self.authority = authority
        self.read_authority = read_authority or authority

    def reconcile_universe(
        self,
        universe: CanonicalUniverse,
        events: tuple[LifecycleEvent, ...] = (),
    ) -> dict[str, int]:
        with self.authority.transaction(statement_timeout_ms=15_000) as conn:
            with conn.cursor() as cur:
                for security in universe.securities:
                    cur.execute(
                        """INSERT INTO core.securities(
                             security_id,isin,company_id,security_type,share_class,face_value,
                             lifecycle_state,effective_from,effective_to
                           ) VALUES(%s,%s,%s,%s,%s,%s,%s,CURRENT_DATE,NULL)
                           ON CONFLICT(security_id) DO UPDATE SET
                             company_id=EXCLUDED.company_id,security_type=EXCLUDED.security_type,
                             share_class=EXCLUDED.share_class,face_value=EXCLUDED.face_value,
                             lifecycle_state=EXCLUDED.lifecycle_state,effective_to=NULL,
                             updated_at=clock_timestamp()""",
                        (
                            security.security_id, security.isin, security.company_id,
                            security.security_type, security.share_class, security.face_value,
                            security.lifecycle_state,
                        ),
                    )
                listings = universe.canonical_listings + universe.listing_aliases
                for listing in listings:
                    if listing.canonical:
                        cur.execute(
                            """UPDATE core.listings SET is_canonical=false,effective_to=CURRENT_DATE
                               WHERE security_id=%s AND is_canonical AND effective_to IS NULL
                                 AND listing_id<>%s""",
                            (listing.security_id, listing.listing_id),
                        )
                    cur.execute(
                        """INSERT INTO core.listings(
                             listing_id,security_id,exchange,segment,symbol,series_group,
                             provider_instrument_key,display_name,listing_state,is_canonical,
                             effective_from,effective_to
                           ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(listing_id) DO UPDATE SET
                             provider_instrument_key=EXCLUDED.provider_instrument_key,
                             display_name=EXCLUDED.display_name,listing_state=EXCLUDED.listing_state,
                             is_canonical=EXCLUDED.is_canonical,effective_to=EXCLUDED.effective_to""",
                        (
                            listing.listing_id, listing.security_id, listing.exchange,
                            listing.segment, listing.symbol, listing.series,
                            listing.provider_instrument_key, listing.display_name,
                            listing.listing_state, listing.canonical,
                            listing.effective_from, listing.effective_to,
                        ),
                    )
                for event in events:
                    source_payload = json.dumps(
                        {"previous": event.previous, "current": event.current}, sort_keys=True
                    )
                    source_hash = hashlib.sha256(source_payload.encode("utf-8")).hexdigest()
                    event_key = f"{event.event_type}:{event.security_id}:{event.listing_id}:" + str(
                        (event.current or event.previous or {}).get("effective_from") or ""
                    )
                    cur.execute(
                        """INSERT INTO core.security_lifecycle_events(
                             event_key,event_type,security_id,listing_id,effective_date,
                             previous_state,current_state,source_hash
                           ) VALUES(%s,%s,%s,%s,CURRENT_DATE,%s::jsonb,%s::jsonb,%s)
                           ON CONFLICT(event_key) DO NOTHING""",
                        (
                            event_key, event.event_type, event.security_id, event.listing_id,
                            json.dumps(event.previous) if event.previous else None,
                            json.dumps(event.current) if event.current else None,
                            source_hash,
                        ),
                    )
        return {
            "securities": len(universe.securities),
            "canonical_listings": len(universe.canonical_listings),
            "aliases": len(universe.listing_aliases),
            "lifecycle_events": len(events),
        }

    def persist_snapshot(self, snapshot: UniverseSnapshot) -> None:
        with self.authority.transaction(statement_timeout_ms=10_000) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO core.universe_snapshots(
                         snapshot_id,effective_date,desk,rule_version,content_hash,population_count,created_at
                       ) VALUES(%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(snapshot_id) DO NOTHING""",
                    (
                        snapshot.snapshot_id, snapshot.effective_date, snapshot.desk,
                        snapshot.rule_version, snapshot.content_hash,
                        snapshot.population_count, snapshot.created_at,
                    ),
                )
                for ordinal, (security_id, listing_id) in enumerate(zip(snapshot.security_ids, snapshot.listing_ids)):
                    cur.execute(
                        """INSERT INTO core.universe_snapshot_members(
                             snapshot_id,security_id,listing_id,ordinal,inclusion_reasons
                           ) VALUES(%s,%s,%s,%s,%s::jsonb)
                           ON CONFLICT(snapshot_id,security_id) DO NOTHING""",
                        (
                            snapshot.snapshot_id, security_id, listing_id, ordinal,
                            json.dumps(snapshot.inclusion_reasons.get(security_id) or ()),
                        ),
                    )
                for security_id, reasons in snapshot.exclusion_reasons.items():
                    for reason in reasons:
                        cur.execute(
                            """INSERT INTO core.universe_snapshot_exclusions(
                                 snapshot_id,security_id,provider_instrument_key,reason_code,detail
                               ) VALUES(%s,%s,%s,%s,'{}'::jsonb)
                               ON CONFLICT DO NOTHING""",
                            (snapshot.snapshot_id, security_id, security_id, reason),
                        )

    def latest_snapshot(self, desk: str) -> dict[str, Any] | None:
        return self.read_authority.execute(
            """SELECT s.snapshot_id,s.effective_date,s.desk,s.rule_version,s.content_hash,
                      s.population_count,s.created_at,
                      COALESCE(jsonb_agg(jsonb_build_object(
                        'security_id',m.security_id,'listing_id',m.listing_id,'ordinal',m.ordinal,
                        'inclusion_reasons',m.inclusion_reasons
                      ) ORDER BY m.ordinal) FILTER (WHERE m.security_id IS NOT NULL),'[]'::jsonb) AS members
                 FROM core.universe_snapshots s
                 LEFT JOIN core.universe_snapshot_members m ON m.snapshot_id=s.snapshot_id
                WHERE s.desk=%s GROUP BY s.snapshot_id
                ORDER BY s.effective_date DESC,s.created_at DESC LIMIT 1""",
            (desk.upper(),), fetch="one", statement_timeout_ms=2500,
        )

    def security_id_for_provider_key(self, provider_instrument_key: str) -> str | None:
        row = self.read_authority.execute(
            """SELECT security_id FROM core.listings
                WHERE provider_instrument_key=%s AND effective_to IS NULL
                ORDER BY is_canonical DESC,effective_from DESC LIMIT 1""",
            (provider_instrument_key,), fetch="one", statement_timeout_ms=1200,
        )
        return str(row["security_id"]) if row else None

    def coverage_summary(self) -> list[dict[str, Any]]:
        """Compact aggregate for visual progress; never scans Parquet parts."""
        rows = self.read_authority.execute(
            """SELECT interval,
                      count(*)::integer AS instruments,
                      count(*) FILTER (WHERE quality_state IN ('ACCEPTED','REPAIRED'))::integer AS verified,
                      min(earliest_stored_ts) AS earliest_stored_ts,
                      max(latest_stored_ts) AS latest_stored_ts,
                      sum(COALESCE(jsonb_array_length(missing_ranges),0))::integer AS missing_ranges
                 FROM market_data.coverage
                GROUP BY interval ORDER BY interval""",
            fetch="all", statement_timeout_ms=1800,
        ) or []
        return [dict(row) for row in rows]

    def persist_coverage(self, coverage: CoverageRecord) -> None:
        self.authority.execute(
            """INSERT INTO market_data.coverage(
                 security_id,interval,earliest_stored_ts,latest_stored_ts,
                 expected_latest_completed_ts,verified_ranges,missing_ranges,
                 adjustment_version,data_version,quality_state,last_verified_at
               ) VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s)
               ON CONFLICT(security_id,interval,data_version) DO UPDATE SET
                 earliest_stored_ts=EXCLUDED.earliest_stored_ts,
                 latest_stored_ts=EXCLUDED.latest_stored_ts,
                 expected_latest_completed_ts=EXCLUDED.expected_latest_completed_ts,
                 verified_ranges=EXCLUDED.verified_ranges,missing_ranges=EXCLUDED.missing_ranges,
                 adjustment_version=EXCLUDED.adjustment_version,
                 quality_state=EXCLUDED.quality_state,last_verified_at=EXCLUDED.last_verified_at""",
            (
                coverage.security_id, coverage.interval, coverage.earliest_stored,
                coverage.latest_stored, coverage.expected_latest_completed,
                json.dumps([item.as_dict() for item in coverage.verified_ranges]),
                json.dumps([item.as_dict() for item in coverage.missing_ranges]),
                coverage.adjustment_version, coverage.data_version, coverage.quality_state,
                coverage.last_verified_at,
            ),
        )

    def load_coverage(self, security_id: str, interval: str, data_version: str) -> CoverageRecord | None:
        """Read only the versioned PostgreSQL coverage manifest used for planning."""
        row = self.read_authority.execute(
            """SELECT security_id,interval,earliest_stored_ts,latest_stored_ts,
                      expected_latest_completed_ts,verified_ranges,missing_ranges,
                      adjustment_version,data_version,quality_state,last_verified_at
                 FROM market_data.coverage
                WHERE security_id=%s AND interval=%s AND data_version=%s""",
            (security_id, interval, data_version), fetch="one", statement_timeout_ms=1500,
        )
        if not row:
            return None

        def ranges(value: Any) -> tuple[TimeRange, ...]:
            raw = json.loads(value) if isinstance(value, str) else (value or [])
            return tuple(TimeRange(item["from"], item["to"]) for item in raw)

        return CoverageRecord(
            security_id=str(row["security_id"]), interval=str(row["interval"]),
            earliest_stored=row.get("earliest_stored_ts"),
            latest_stored=row.get("latest_stored_ts"),
            expected_latest_completed=row["expected_latest_completed_ts"],
            verified_ranges=ranges(row.get("verified_ranges")),
            missing_ranges=ranges(row.get("missing_ranges")),
            adjustment_version=str(row["adjustment_version"]),
            data_version=str(row["data_version"]),
            quality_state=str(row["quality_state"]),
            last_verified_at=row.get("last_verified_at"),
        )

    def enqueue_hydration_jobs(self, plan: FetchPlan, *, priority: int, reason_code: str) -> int:
        """Idempotently register every exact missing range before provider work."""
        created = 0
        for item in plan.missing:
            created += max(0, int(self.authority.execute(
                """INSERT INTO market_data.hydration_jobs(
                     security_id,interval,range_from,range_to,data_version,priority,reason_code,state
                   ) VALUES(%s,%s,%s,%s,%s,%s,%s,'QUEUED')
                   ON CONFLICT(security_id,interval,range_from,range_to,data_version) DO NOTHING""",
                (
                    plan.security_id, plan.interval, item.start, item.end,
                    plan.data_version, max(0, min(5, int(priority))), str(reason_code or "CACHE_GAP"),
                ),
            ) or 0))
        return created

    def complete_hydration_job(
        self,
        *,
        security_id: str,
        interval: str,
        item: TimeRange,
        data_version: str,
        state: str,
        error: str | None = None,
    ) -> None:
        normalized = str(state or "FAILED").upper()
        if normalized not in {"COMPLETE", "DEFERRED_RATE_LIMIT", "FAILED"}:
            raise ValueError("invalid terminal hydration state")
        self.authority.execute(
            """UPDATE market_data.hydration_jobs
                  SET state=%s,attempts=attempts+1,completed_at=CASE WHEN %s='COMPLETE' THEN clock_timestamp() ELSE completed_at END,
                      last_error=%s
                WHERE security_id=%s AND interval=%s AND range_from=%s AND range_to=%s AND data_version=%s""",
            (
                normalized, normalized, (str(error)[:500] if error else None), security_id,
                interval, item.start, item.end, data_version,
            ),
        )

    def audit_fetch_plan(self, plan: FetchPlan) -> None:
        self.authority.execute(
            """INSERT INTO market_data.request_audit(
                 security_id,interval,requested_from,requested_to,outcome,
                 missing_ranges,data_version,governed_reason
               ) VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,%s)""",
            (
                plan.security_id, plan.interval, plan.requested.start, plan.requested.end,
                plan.outcome, json.dumps([item.as_dict() for item in plan.missing]),
                plan.data_version, plan.governed_reason,
            ),
        )

    def persist_scan_run(self, run: ScanRun, *, market_state: str) -> None:
        proof = run.proof()
        with self.authority.transaction(statement_timeout_ms=15_000) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO scanner.scan_runs(
                         run_id,snapshot_id,desk,population_count,terminal_count,candidate_count,
                         scanner_version,market_state,state,started_at,completed_at
                       ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'COMPLETE',%s,%s)
                       ON CONFLICT(run_id) DO NOTHING""",
                    (
                        run.run_id, run.snapshot_id, run.desk, run.population_count,
                        run.terminal_count, run.candidate_count, "pl-scanner-69.8.0",
                        market_state, run.completed_at, run.completed_at,
                    ),
                )
                for evaluation in run.evaluations:
                    cur.execute(
                        """INSERT INTO scanner.scanner_evaluations(
                             run_id,security_id,listing_id,terminal_state,priority_tier,
                             priority_score,research_state,canonical_decision_allowed,evidence
                           ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                           ON CONFLICT(run_id,security_id) DO NOTHING""",
                        (
                            run.run_id, evaluation.security_id, evaluation.listing_id,
                            evaluation.terminal_state, evaluation.priority_tier,
                            evaluation.priority_score, evaluation.research_state,
                            evaluation.canonical_decision_allowed,
                            json.dumps(dict(evaluation.evidence), default=str),
                        ),
                    )
                    for reason in evaluation.rejection_reasons:
                        cur.execute(
                            """INSERT INTO scanner.candidate_rejections(run_id,security_id,reason_code)
                               VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (run.run_id, evaluation.security_id, reason),
                        )
        if not proof["complete"]:
            raise AssertionError("persisted scanner run must have a terminal state per snapshot member")

    def authority_status(self) -> dict[str, Any]:
        snapshots = self.read_authority.execute(
            """SELECT DISTINCT ON (desk)
                      desk,snapshot_id,effective_date,population_count,content_hash
                 FROM core.universe_snapshots
                ORDER BY desk,effective_date DESC,created_at DESC""",
            fetch="all", statement_timeout_ms=1500,
        ) or []
        scans = self.read_authority.execute(
            """SELECT DISTINCT ON (desk) desk,run_id,snapshot_id,population_count,
                      terminal_count,candidate_count,state,completed_at
                 FROM scanner.scan_runs ORDER BY desk,completed_at DESC NULLS LAST""",
            fetch="all", statement_timeout_ms=1500,
        ) or []
        return {
            "ok": True,
            "authority": "POSTGRESQL",
            "snapshots": snapshots,
            "scanner_runs": scans,
            "compatibility_runtime_dependency": False,
        }
