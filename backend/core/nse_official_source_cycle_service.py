"""Governed active acquisition cycle for official NSE cash-market reports.

Project Laddu ships a conservative set of verified NSE archive transports and merges
those defaults into retained operator configuration during upgrades. The collector:

* accepts only NSE-owned HTTPS hosts;
* tries bounded host/date fallbacks for trading-day reports;
* preserves conditional HTTP state and prior admitted authority;
* rejects empty, oversized and HTML/error payloads;
* sends every accepted payload through schema, PostgreSQL and Parquet admission;
* never changes model influence or broker authority.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import Message
from http.cookiejar import CookieJar
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Dict, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from core.nse_cash_data_authority_service import SOURCES
from core.nse_official_report_ingestion_service import NseOfficialReportIngestionService
from core.storage_layout import StorageLayout, atomic_write_json, interprocess_lock

SERVICE_VERSION = "nse-official-source-cycle-3.1.0-admission-aware-conditional-fetch"
ALLOWED_HOST_SUFFIXES = (".nseindia.com", "nseindia.com", ".niftyindices.com", "niftyindices.com")
SOURCE_KEYS = frozenset(str(row["key"]) for row in SOURCES)
SOURCE_DEFINITIONS = {str(row["key"]): dict(row) for row in SOURCES}
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_WORKERS = 2
MAX_ACQUISITION_WORKERS = 6
REQUEST_TIMEOUT_SECONDS = 10
REQUIRED_ARTIFACT_BUDGET_SECONDS = 28
OPTIONAL_ARTIFACT_BUDGET_SECONDS = 16
MAX_REQUIRED_ATTEMPTS = 6
MAX_OPTIONAL_ATTEMPTS = 3
MAX_LOOKBACK_DATES = 4
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Project-Laddu-v98"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_name(value: str, fallback: str) -> str:
    value = Path(str(value or "")).name
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return value or fallback


def _content_disposition_name(headers: Message, fallback: str) -> str:
    value = str(headers.get("Content-Disposition") or "")
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", value, flags=re.I)
    return _safe_name(match.group(1).strip() if match else fallback, fallback)


def _approved_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(host == suffix or host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def _render(value: str, trade_date: str) -> str:
    day = datetime.strptime(trade_date, "%Y-%m-%d")
    return str(value or "").format(
        trade_date=trade_date,
        yyyymmdd=day.strftime("%Y%m%d"),
        ddmmyyyy=day.strftime("%d%m%Y"),
        ddmmyy=day.strftime("%d%m%y"),
        yymmdd=day.strftime("%y%m%d"),
    )


def _looks_like_html(payload: bytes) -> bool:
    head = payload[:2048].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<title>access denied" in head


@dataclass(frozen=True)
class SourceArtifactPlan:
    artifact_key: str
    source_key: str
    enabled: bool
    url_templates: tuple[str, ...]
    inbox_glob: str | None
    filename_template: str | None
    required: bool
    lookback_days: int
    index_name: str | None = None


class NseOfficialSourceCycleService:
    def __init__(self, data_dir: Path, *, plan_path: Path | None = None):
        self.data_dir = Path(data_dir)
        self.layout = StorageLayout.from_data_dir(self.data_dir)
        self.layout.ensure()
        self.plan_path = Path(plan_path) if plan_path else self.data_dir / "config" / "nse_official_sources.json"
        self.state_path = self.layout.manifests_dir / "nse_official" / "collector-state.json"
        self.cycle_path = self.layout.manifests_dir / "nse_official" / "last-cycle.json"
        self.lock_path = self.layout.locks_dir / "nse-official-source-cycle.lock"
        self.ingestion = NseOfficialReportIngestionService(self.layout)
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self._bootstrapped = False
        self._state_lock = threading.RLock()

    def _load_plan(self) -> tuple[list[SourceArtifactPlan], Dict[str, Any]]:
        if not self.plan_path.is_file():
            return [], {"state": "SOURCE_PLAN_REQUIRED", "path": str(self.plan_path)}
        payload = json.loads(self.plan_path.read_text(encoding="utf-8"))
        if str(payload.get("version") or "") not in {"1", "1.0", "v1", "2", "2.0", "v2", "3", "3.0", "v3"}:
            raise ValueError("NSE source plan version must be 1, 2 or 3")
        rows: list[SourceArtifactPlan] = []
        seen: set[str] = set()
        for raw in payload.get("artifacts") or []:
            source_key = str(raw.get("source_key") or "").strip()
            artifact_key = str(raw.get("artifact_key") or source_key).strip()
            if source_key not in SOURCE_KEYS:
                raise ValueError(f"Unsupported source_key in NSE source plan: {source_key}")
            if not artifact_key or artifact_key in seen:
                raise ValueError(f"Duplicate/empty artifact_key in NSE source plan: {artifact_key}")
            seen.add(artifact_key)
            urls = [str(value).strip() for value in (raw.get("url_templates") or []) if str(value).strip()]
            legacy = str(raw.get("url_template") or "").strip()
            if legacy and legacy not in urls:
                urls.insert(0, legacy)
            for template in urls:
                if not _approved_url(_render(template, date.today().isoformat())):
                    raise ValueError(f"NSE source plan URL is not an approved NSE HTTPS host: {artifact_key}")
            inbox_glob = str(raw.get("inbox_glob") or "").strip() or None
            if not urls and not inbox_glob:
                raise ValueError(f"NSE source artifact has no transport: {artifact_key}")
            rows.append(SourceArtifactPlan(
                artifact_key=artifact_key,
                source_key=source_key,
                enabled=raw.get("enabled") is not False,
                url_templates=tuple(urls),
                inbox_glob=inbox_glob,
                filename_template=str(raw.get("filename_template") or "").strip() or None,
                required=raw.get("required") is True,
                lookback_days=max(0, min(14, int(raw.get("lookback_days") or 0))),
                index_name=str(raw.get("index_name") or "").strip() or None,
            ))
        active_urls = sum(1 for row in rows if row.enabled and row.url_templates)
        return rows, {
            "state": "PLAN_READY",
            "path": str(self.plan_path),
            "version": str(payload.get("version") or ""),
            "artifact_count": len(rows),
            "active_url_artifacts": active_urls,
        }

    def _load_state(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _bootstrap(self) -> None:
        if self._bootstrapped:
            return
        request = Request(
            "https://www.nseindia.com/all-reports",
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*", "Connection": "close"},
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                response.read(1024)
        except Exception:
            # Direct archive hosts normally do not require the website cookie. A failed
            # bootstrap is evidence in an individual attempt, not permission to stop
            # archive fallback attempts.
            pass
        self._bootstrapped = True

    def _candidate_dates(self, trade_date: str, lookback_days: int) -> list[str]:
        from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY

        sessions = DEFAULT_TRADING_SESSION_AUTHORITY
        start = datetime.strptime(trade_date, "%Y-%m-%d").date()
        # Never infer historical trading days from weekdays.  The active release
        # calendar may only cover the forward/live horizon; outside that proven
        # horizon the collector attempts exactly the requested date and leaves
        # any historical backfill to a provenance-backed session index.
        if not sessions.calendar_covered(start):
            return [trade_date]
        result: list[str] = []
        cursor = start
        max_sessions = max(1, int(lookback_days or 0) + 1)
        for _ in range(max_sessions):
            if sessions.is_trading_day(cursor):
                result.append(cursor.isoformat())
            try:
                cursor = sessions.previous_trading_day(cursor)
            except RuntimeError:
                break
        return result or [trade_date]

    def _admitted_manifest_for(
        self, plan: SourceArtifactPlan, resolved_date: str, url: str, prior: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Return a matching admitted manifest only when its required wiring is complete."""
        root = self.layout.manifests_dir / "nse_official" / plan.source_key / f"trade_date={resolved_date}"
        if not root.is_dir():
            return None
        expected_name = str(prior.get("filename") or "").strip()
        matches: list[dict[str, Any]] = []
        for path in root.glob("*.json"):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if row.get("ok") is not True or not row.get("content_hash"):
                continue
            metadata = dict(row.get("source_metadata") or {})
            if plan.index_name and str(metadata.get("index_name") or "").strip() != str(plan.index_name).strip():
                continue
            source_url = str(row.get("source_url") or "").strip()
            source_name = str(row.get("source_filename") or "").strip()
            if source_url == url or (expected_name and source_name == expected_name) or (not source_url and not expected_name):
                matches.append(row)
        if not matches:
            return None
        matches.sort(key=lambda row: str(row.get("ingested_at") or ""), reverse=True)
        row = matches[0]
        target = str((SOURCE_DEFINITIONS.get(plan.source_key) or {}).get("target") or "").upper()
        if "POSTGRESQL" in target:
            projection = dict(row.get("postgres_projection") or {})
            if str(projection.get("state") or "").upper() != "PROJECTED":
                return None
        if str((row.get("schema_status") or {}).get("state") or "").upper() != "VALID":
            return None
        if not row.get("curated_path"):
            return None
        return row

    def _download_one(
        self, plan: SourceArtifactPlan, template: str, resolved_date: str,
        state: Dict[str, Any], *, deadline: float,
    ) -> tuple[bytes, str, str, Dict[str, Any]] | None:
        url = _render(template, resolved_date)
        if not _approved_url(url):
            raise ValueError(f"Resolved URL is not an approved NSE HTTPS host: {plan.artifact_key}")
        with self._state_lock:
            prior = dict((state.get("artifacts") or {}).get(plan.artifact_key) or {})
        headers = {
            "Accept": "text/csv,text/plain,application/zip,application/gzip,application/octet-stream,*/*;q=0.5",
            "User-Agent": USER_AGENT,
            "Referer": "https://www.nseindia.com/all-reports",
            "Connection": "close",
        }
        admitted = self._admitted_manifest_for(plan, resolved_date, url, prior)
        # Conditional transport is safe only after the downloaded bytes have an
        # admitted, schema-valid and (when required) PostgreSQL-projected manifest.
        # Collector metadata by itself is never authority: otherwise a prior 304
        # can strand bytes outside the governed lake forever.
        if admitted is not None and prior.get("url") == url and prior.get("etag"):
            headers["If-None-Match"] = str(prior["etag"])
        if admitted is not None and prior.get("url") == url and prior.get("last_modified"):
            headers["If-Modified-Since"] = str(prior["last_modified"])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Acquisition budget exhausted: {plan.artifact_key}")
        timeout = max(2, min(REQUEST_TIMEOUT_SECONDS, int(remaining)))
        request = Request(url, headers=headers, method="GET")
        with self.opener.open(request, timeout=timeout) as response:
            length = int(response.headers.get("Content-Length") or 0)
            if length > MAX_ARTIFACT_BYTES:
                raise ValueError(f"NSE artifact exceeds {MAX_ARTIFACT_BYTES} bytes: {plan.artifact_key}")
            payload = response.read(MAX_ARTIFACT_BYTES + 1)
            if len(payload) > MAX_ARTIFACT_BYTES:
                raise ValueError(f"NSE artifact exceeds {MAX_ARTIFACT_BYTES} bytes: {plan.artifact_key}")
            if not payload:
                raise ValueError(f"NSE artifact is empty: {plan.artifact_key}")
            if _looks_like_html(payload):
                raise ValueError(f"NSE endpoint returned HTML instead of a report: {plan.artifact_key}")
            fallback = _render(plan.filename_template or Path(urlparse(url).path).name or f"{plan.artifact_key}.dat", resolved_date)
            filename = _content_disposition_name(response.headers, fallback)
            metadata = {
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "url": url,
                "downloaded_at": _now(),
                "filename": filename,
                "resolved_trade_date": resolved_date,
            }
            with self._state_lock:
                (state.setdefault("artifacts", {}))[plan.artifact_key] = metadata
            return payload, filename, url, metadata

    def _download(self, plan: SourceArtifactPlan, trade_date: str, state: Dict[str, Any]) -> tuple[bytes, str, str, str, list[dict[str, Any]]] | None:
        self._bootstrap()
        attempts: list[dict[str, Any]] = []
        budget = REQUIRED_ARTIFACT_BUDGET_SECONDS if plan.required else OPTIONAL_ARTIFACT_BUDGET_SECONDS
        max_attempts = MAX_REQUIRED_ATTEMPTS if plan.required else MAX_OPTIONAL_ATTEMPTS
        deadline = time.monotonic() + budget
        dates = self._candidate_dates(trade_date, plan.lookback_days)[:MAX_LOOKBACK_DATES]
        candidates = [(resolved_date, template) for resolved_date in dates for template in plan.url_templates]
        for resolved_date, template in candidates[:max_attempts]:
            if time.monotonic() >= deadline:
                attempts.append({"trade_date": resolved_date, "state": "BUDGET_EXHAUSTED"})
                break
            url = _render(template, resolved_date)
            try:
                downloaded = self._download_one(plan, template, resolved_date, state, deadline=deadline)
                if downloaded is None:
                    return None
                payload, filename, source_url, _metadata = downloaded
                attempts.append({"url": source_url, "trade_date": resolved_date, "state": "DOWNLOADED"})
                return payload, filename, source_url, resolved_date, attempts
            except HTTPError as exc:
                if exc.code == 304:
                    return None
                attempts.append({"url": url, "trade_date": resolved_date, "state": f"HTTP_{exc.code}"})
                if exc.code in {401, 403}:
                    self._bootstrapped = False
                continue
            except URLError as exc:
                attempts.append({"url": url, "trade_date": resolved_date, "state": "NETWORK_FAILURE", "error": str(exc.reason)})
                continue
            except TimeoutError as exc:
                attempts.append({"url": url, "trade_date": resolved_date, "state": "TIME_BUDGET_EXHAUSTED", "error": str(exc)})
                break
            except Exception as exc:
                attempts.append({"url": url, "trade_date": resolved_date, "state": "INVALID_PAYLOAD", "error": f"{type(exc).__name__}: {exc}"})
                continue
        error = attempts[-1] if attempts else {"state": "NO_ATTEMPT"}
        raise RuntimeError(
            f"All governed transports failed for {plan.artifact_key}: {error.get('state')} "
            f"after {len(attempts)} bounded attempts"
        )

    def _inbox(self, plan: SourceArtifactPlan, trade_date: str) -> list[tuple[bytes, str, str, str]]:
        if not plan.inbox_glob:
            return []
        pattern = _render(plan.inbox_glob, trade_date)
        root = self.data_dir / "inbox" / "nse_official"
        results = []
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            if path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise ValueError(f"Inbox artifact exceeds {MAX_ARTIFACT_BYTES} bytes: {path.name}")
            results.append((path.read_bytes(), path.name, path.resolve().as_uri(), trade_date))
        return results

    def run(self, *, trade_date: str, inbox_only: bool = False) -> Dict[str, Any]:
        datetime.strptime(trade_date, "%Y-%m-%d")
        started = time.monotonic()
        with interprocess_lock(self.lock_path, timeout_seconds=5.0):
            plans, plan_status = self._load_plan()
            if not plans:
                result = {
                    "ok": False, "version": SERVICE_VERSION, "state": plan_status["state"],
                    "trade_date": trade_date, "plan": plan_status, "results": [],
                    "catalog_refresh_required": False, "finished_at": _now(),
                }
                atomic_write_json(self.cycle_path, result)
                return result
            state = self._load_state()
            enabled = [row for row in plans if row.enabled]
            work: list[tuple[SourceArtifactPlan, bytes, str, str, str]] = []
            results: list[dict[str, Any]] = []
            for plan in enabled:
                for payload, filename, source_url, resolved_date in self._inbox(plan, trade_date):
                    work.append((plan, payload, filename, source_url, resolved_date))

            def acquire(plan: SourceArtifactPlan) -> tuple[dict[str, Any], tuple[SourceArtifactPlan, bytes, str, str, str] | None]:
                try:
                    downloaded = self._download(plan, trade_date, state)
                    if downloaded is None:
                        return ({"artifact_key": plan.artifact_key, "source_key": plan.source_key, "state": "HTTP_NOT_MODIFIED", "ok": True, "required": plan.required}, None)
                    payload, filename, source_url, resolved_date, attempts = downloaded
                    row = {"artifact_key": plan.artifact_key, "source_key": plan.source_key, "state": "ACQUIRED", "ok": True, "required": plan.required, "resolved_trade_date": resolved_date, "attempts": attempts}
                    return row, (plan, payload, filename, source_url, resolved_date)
                except Exception as exc:
                    return ({"artifact_key": plan.artifact_key, "source_key": plan.source_key, "state": "ACQUISITION_FAILED", "ok": False, "required": plan.required, "error": f"{type(exc).__name__}: {exc}"}, None)

            network_plans = [plan for plan in enabled if plan.url_templates and not inbox_only]
            if network_plans:
                ordered = sorted(network_plans, key=lambda row: (not row.required, row.artifact_key))
                with ThreadPoolExecutor(max_workers=min(MAX_ACQUISITION_WORKERS, len(ordered)), thread_name_prefix="nse-official-fetch") as pool:
                    futures = {pool.submit(acquire, plan): plan for plan in ordered}
                    for future in as_completed(futures):
                        row, item = future.result()
                        results.append(row)
                        if item is not None:
                            work.append(item)

            represented = {str(row.get("artifact_key") or "") for row in results if row.get("ok") is True}
            represented.update(item[0].artifact_key for item in work)
            for plan in enabled:
                if not plan.required or plan.artifact_key in represented:
                    continue
                existing = list((self.layout.manifests_dir / "nse_official" / plan.source_key).glob("trade_date=*/*.json"))
                if existing:
                    results.append({"artifact_key": plan.artifact_key, "source_key": plan.source_key, "state": "PRIOR_AUTHORITY_RETAINED", "ok": True, "required": True, "note": "Current acquisition failed; last schema-valid content-hashed authority remains available."})
                else:
                    results.append({"artifact_key": plan.artifact_key, "source_key": plan.source_key, "state": "MISSING_REQUIRED_ARTIFACT", "ok": False, "required": True, "error": "No admitted report exists and all configured transports failed."})

            def admit(item: tuple[SourceArtifactPlan, bytes, str, str, str]) -> dict[str, Any]:
                plan, payload, filename, source_url, resolved_date = item
                admitted = self.ingestion.ingest_bytes(
                    source_key=plan.source_key,
                    trade_date=resolved_date,
                    payload=payload,
                    filename=filename,
                    source_url=source_url,
                    source_metadata={"index_name": plan.index_name, "exchange": "NSE"} if plan.index_name else None,
                )
                return {"artifact_key": plan.artifact_key, "required": plan.required, "requested_trade_date": trade_date, "resolved_trade_date": resolved_date, **admitted}

            if work:
                with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(work)), thread_name_prefix="nse-official-cycle") as pool:
                    futures = {pool.submit(admit, item): item[0] for item in work}
                    for future in as_completed(futures):
                        plan = futures[future]
                        try:
                            results.append(future.result())
                        except Exception as exc:
                            results.append({"artifact_key": plan.artifact_key, "source_key": plan.source_key, "state": "ADMISSION_FAILED", "ok": False, "required": plan.required, "error": f"{type(exc).__name__}: {exc}"})
            state.update({"version": SERVICE_VERSION, "updated_at": _now()})
            atomic_write_json(self.state_path, state)
            required_failure_keys = sorted({
                str(row.get("artifact_key") or row.get("source_key") or "unknown")
                for row in results if row.get("required") and row.get("ok") is not True
            })
            admitted_states = {"INGESTED", "UNCHANGED_CONTENT_SKIPPED", "UNCHANGED_CONTENT_REPROJECTED", "CURRENT_FROM_MANIFEST", "PRIOR_AUTHORITY_RETAINED", "HTTP_NOT_MODIFIED"}
            admitted_keys = sorted({
                str(row.get("artifact_key") or row.get("source_key") or "unknown")
                for row in results if row.get("state") in admitted_states and row.get("ok") is True
            })
            result = {
                "ok": not required_failure_keys,
                "version": SERVICE_VERSION,
                "state": "CYCLE_COMPLETE" if not required_failure_keys else "REQUIRED_SOURCE_FAILED",
                "trade_date": trade_date,
                "plan": plan_status,
                "enabled_artifacts": len(enabled),
                "results": sorted(results, key=lambda row: (str(row.get("artifact_key") or ""), str(row.get("state") or ""))),
                "ingested_or_current": len(admitted_keys),
                "ingested_or_current_keys": admitted_keys,
                "required_failures": len(required_failure_keys),
                "required_failure_keys": required_failure_keys,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "acquisition_policy": {"parallel_workers": MAX_ACQUISITION_WORKERS, "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS, "required_budget_seconds": REQUIRED_ARTIFACT_BUDGET_SECONDS, "optional_budget_seconds": OPTIONAL_ARTIFACT_BUDGET_SECONDS, "max_lookback_dates": MAX_LOOKBACK_DATES},
                "catalog_refresh_required": bool(work),
                "finished_at": _now(),
                "production_influence": 0.0,
                "broker_authority": "NONE",
            }
            atomic_write_json(self.cycle_path, result)
            return result
