"""Acquire official NSE corporate-action range evidence for the research horizon.

PL45 changes the acquisition contract from all-or-nothing to resumable proof:
- every date chunk has an atomic durable manifest;
- successful and valid-empty chunks survive later failures;
- retries skip already proven chunks;
- only a complete set of proven chunks can create full-range coverage;
- transient NSE failures remain explicit and never become false empty evidence.

No adjustment factor is inferred from price jumps and no statistical/WFA gate is
weakened by this transport/reconciliation change.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
from http.cookiejar import CookieJar
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from core.corporate_action_chunk_manifest import CorporateActionChunkManifest, TERMINAL_SUCCESS
from core.nse_official_report_ingestion_service import (
    ALIASES,
    NseOfficialReportIngestionService,
    canonicalise_rows,
)
from core.storage_layout import StorageLayout, atomic_write_json

VERSION = "nse-corporate-action-range-sync-1.1.0-pl45-resumable-chunks"
REQUEST_VERSION = "nse-corporate-action-request-2.0.0"
SOURCE_FAMILY = "corporate_actions"
EXCHANGE = "NSE"
ENDPOINT = "https://www.nseindia.com/api/corporates-corporateActions"
HOME = "https://www.nseindia.com/"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Project-Laddu-PL45"
MAX_BYTES = 32 * 1024 * 1024


class FetchFailure(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, http_status: int | None = None,
                 error_code: str = "FETCH_FAILED", content_type: str | None = None) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)
        self.http_status = http_status
        self.error_code = str(error_code)
        self.content_type = content_type


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retry_at(minutes: int = 15) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=max(1, int(minutes)))).isoformat()


def _scope(data_dir: Path) -> tuple[str, str] | None:
    layout = StorageLayout.from_data_dir(data_dir)
    if not layout.analytics_db.is_file():
        return None
    try:
        import duckdb
        db = duckdb.connect(str(layout.analytics_db), read_only=True)
        try:
            relations = {str(r[0]) for r in db.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()}
            if "research_delivery_training_panel" not in relations:
                return None
            row = db.execute(
                "SELECT CAST(min(date) AS VARCHAR),CAST(max(date) AS VARCHAR) FROM research_delivery_training_panel"
            ).fetchone()
            if row and row[0] and row[1]:
                return str(row[0])[:10], str(row[1])[:10]
        finally:
            db.close()
    except Exception:
        return None
    return None


def _chunks(start: str, end: str, days: int = 365):
    cursor, finish = date.fromisoformat(start), date.fromisoformat(end)
    step = max(30, min(365, int(days)))
    while cursor <= finish:
        chunk_end = min(finish, cursor + timedelta(days=step - 1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def _normalise_header(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return text


def _to_csv_if_json(payload: bytes) -> tuple[bytes, int]:
    stripped = payload.lstrip()
    if not stripped:
        raise FetchFailure("NSE corporate-action endpoint returned an empty payload", retryable=True, error_code="EMPTY_PAYLOAD")
    if stripped[:1] not in (b"{", b"["):
        text = payload.decode("utf-8-sig", errors="replace")
        try:
            rows = list(csv.reader(io.StringIO(text)))
            return payload, max(0, len(rows) - 1)
        except Exception as exc:
            raise FetchFailure(f"CSV decode failed: {type(exc).__name__}: {exc}", retryable=False, error_code="INVALID_CSV") from exc
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except Exception as exc:
        raise FetchFailure(f"JSON decode failed: {type(exc).__name__}: {exc}", retryable=False, error_code="INVALID_JSON") from exc
    if isinstance(value, dict):
        candidates = value.get("data")
        if candidates is None:
            candidates = value.get("records")
        if candidates is None:
            candidates = value.get("rows")
        if candidates is None:
            # NSE sometimes wraps a valid empty result in an object with no data key.
            # Do not call that a valid empty chunk: schema/version must be explicit.
            raise FetchFailure("JSON payload lacks data/records/rows collection", retryable=False, error_code="INVALID_SCHEMA")
    else:
        candidates = value
    if not isinstance(candidates, list):
        raise FetchFailure("JSON row collection is not a list", retryable=False, error_code="INVALID_SCHEMA")
    rows = [dict(row) for row in candidates if isinstance(row, dict)]
    if len(rows) != len(candidates):
        raise FetchFailure("JSON row collection contains non-object members", retryable=False, error_code="INVALID_SCHEMA")
    if not rows:
        # Canonical empty schema: absence is valid only after the transport and
        # response container itself were validated for this exact range.
        return b"symbol,exDate,subject\n", 0
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=keys, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8"), len(rows)


def _header_has_group(fieldnames: list[str], canonical: str) -> bool:
    present = {_normalise_header(value) for value in fieldnames}
    aliases = {_normalise_header(value) for value in ALIASES.get(canonical, (canonical,))}
    return bool(present.intersection(aliases))


def _validate_payload(*, payload: bytes, content_type: str, chunk_start: date,
                      chunk_end: date) -> dict[str, Any]:
    head = payload[:4096].lstrip().lower()
    lower_ctype = str(content_type or "").lower()
    if not payload:
        raise FetchFailure("NSE corporate-action endpoint returned an empty payload", retryable=True, error_code="EMPTY_PAYLOAD")
    if "text/html" in lower_ctype or head.startswith(b"<html") or head.startswith(b"<!doctype html"):
        raise FetchFailure("NSE returned an HTML/login/bot page", retryable=True, error_code="HTML_BLOCK_PAGE", content_type=content_type)

    normalized, row_count = _to_csv_if_json(payload)
    text = normalized.decode("utf-8-sig", errors="replace")
    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = list(reader.fieldnames or [])
    except Exception as exc:
        raise FetchFailure(f"Canonical CSV parse failed: {type(exc).__name__}: {exc}", retryable=False, error_code="INVALID_SCHEMA") from exc
    if not fieldnames:
        raise FetchFailure("Corporate-action response has no header", retryable=False, error_code="INVALID_SCHEMA")
    if not _header_has_group(fieldnames, "symbol") or not _header_has_group(fieldnames, "ex_date"):
        raise FetchFailure("Corporate-action response lacks symbol/ex-date schema", retryable=False, error_code="INVALID_SCHEMA")
    if not (_header_has_group(fieldnames, "action_type") or _header_has_group(fieldnames, "purpose")):
        raise FetchFailure("Corporate-action response lacks action/purpose schema", retryable=False, error_code="INVALID_SCHEMA")

    content_hash = hashlib.sha256(payload).hexdigest()
    canonical = canonicalise_rows(
        SOURCE_FAMILY,
        chunk_end.isoformat(),
        normalized,
        f"nse-corporate-actions-{chunk_start.isoformat()}-{chunk_end.isoformat()}.csv",
        content_hash,
        ENDPOINT,
        source_metadata={
            "range_start": chunk_start.isoformat(),
            "range_end": chunk_end.isoformat(),
            "market_wide": True,
            "exchange": EXCHANGE,
            "transport_version": VERSION,
        },
    )
    if row_count != len(canonical):
        raise FetchFailure(
            f"Canonical row count mismatch: transport={row_count}, canonical={len(canonical)}",
            retryable=False,
            error_code="INVALID_SCHEMA",
        )
    for row in canonical:
        symbol = str(row.get("symbol") or "").strip().upper()
        ex_date = str(row.get("ex_date") or "")[:10]
        if not symbol or not ex_date:
            raise FetchFailure("Corporate-action row lacks symbol/ex_date", retryable=False, error_code="INVALID_SCHEMA")
        try:
            ex = date.fromisoformat(ex_date)
        except ValueError as exc:
            raise FetchFailure(f"Invalid ex_date {ex_date!r}", retryable=False, error_code="INVALID_DATE") from exc
        if ex < chunk_start or ex > chunk_end:
            raise FetchFailure(
                f"Corporate-action ex_date {ex_date} outside requested chunk {chunk_start}..{chunk_end}",
                retryable=False,
                error_code="OUT_OF_RANGE_DATA",
            )
        if not (str(row.get("action_type") or "").strip() or str(row.get("purpose") or "").strip()):
            raise FetchFailure("Corporate-action row lacks action terms", retryable=False, error_code="INVALID_SCHEMA")
        exchange = str(row.get("exchange") or EXCHANGE).strip().upper()
        if exchange not in {"", EXCHANGE}:
            raise FetchFailure(f"Unexpected exchange {exchange}", retryable=False, error_code="INVALID_EXCHANGE")
    return {
        "normalized": normalized,
        "canonical_rows": canonical,
        "row_count": len(canonical),
        "payload_sha256": content_hash,
        "schema_valid": True,
    }


def _new_opener(user_agent: str):
    return build_opener(HTTPCookieProcessor(CookieJar()))


def _warm(opener, user_agent: str) -> dict[str, Any]:
    request = Request(HOME, headers={"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.5"})
    try:
        with opener.open(request, timeout=15) as response:
            response.read(1024)
            return {"ok": True, "http_status": int(getattr(response, "status", 200) or 200)}
    except HTTPError as exc:
        return {"ok": False, "http_status": int(exc.code), "error": f"HTTPError: {exc}"}
    except Exception as exc:
        return {"ok": False, "http_status": None, "error": f"{type(exc).__name__}: {exc}"}


def _fetch(opener, start: date, end: date, *, user_agent: str) -> dict[str, Any]:
    query = urlencode({
        "index": "equities",
        "from_date": start.strftime("%d-%m-%Y"),
        "to_date": end.strftime("%d-%m-%Y"),
        "csv": "true",
    })
    url = ENDPOINT + "?" + query
    request = Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "text/csv,application/json,*/*;q=0.5",
        "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions",
        "Connection": "close",
    })
    try:
        with opener.open(request, timeout=25) as response:
            status = int(getattr(response, "status", 200) or 200)
            ctype = str(response.headers.get("Content-Type") or "")
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_BYTES:
                raise FetchFailure("corporate-action response exceeds size policy", retryable=False, http_status=status, error_code="RESPONSE_TOO_LARGE", content_type=ctype)
            payload = response.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES:
                raise FetchFailure("corporate-action response exceeds size policy", retryable=False, http_status=status, error_code="RESPONSE_TOO_LARGE", content_type=ctype)
            if status != 200:
                raise FetchFailure(f"NSE corporate-action HTTP {status}", retryable=status in {401, 403, 408, 425, 429} or status >= 500, http_status=status, error_code=f"HTTP_{status}", content_type=ctype)
            return {"payload": payload, "content_type": ctype, "http_status": status, "url": url}
    except HTTPError as exc:
        code = int(exc.code)
        raise FetchFailure(
            f"NSE corporate-action HTTP {code}",
            retryable=code in {401, 403, 408, 425, 429} or code >= 500,
            http_status=code,
            error_code=f"HTTP_{code}",
            content_type=str(exc.headers.get("Content-Type") or "") if exc.headers else None,
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise FetchFailure(f"{type(exc).__name__}: {exc}", retryable=True, error_code="NETWORK_OR_TIMEOUT") from exc


def _raw_evidence_path(layout: StorageLayout, chunk_start: date, chunk_end: date,
                       payload_sha256: str, content_type: str) -> Path:
    suffix = ".json" if "json" in str(content_type or "").lower() else ".csv"
    root = layout.raw_lake_dir / "nse_official" / "corporate_actions_range_chunks" / f"range_start={chunk_start.isoformat()}"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{chunk_end.isoformat()}-{payload_sha256}{suffix}"


def _write_raw_evidence(path: Path, payload: bytes) -> None:
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(payload)
    os.replace(temp, path)


def _stable_chunk_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_family": row.get("source_family"),
        "exchange": row.get("exchange"),
        "range_start": row.get("range_start"),
        "range_end": row.get("range_end"),
        "request_version": row.get("request_version"),
        "status": row.get("status"),
        "payload_sha256": row.get("payload_sha256"),
        "row_count": int(row.get("row_count") or 0),
        "ingestion_content_hash": row.get("ingestion_content_hash"),
    }


def sync(*, data_dir: Path, operational_dsn: str, start: str | None = None,
         end: str | None = None, chunk_days: int = 365,
         fetcher: Callable[..., dict[str, Any]] | None = None,
         manifest: CorporateActionChunkManifest | None = None,
         ingestion_service: Any | None = None,
         sleep_fn: Callable[[float], None] = time.sleep,
         random_fn: Callable[[], float] = random.random) -> dict:
    layout = StorageLayout.from_data_dir(data_dir)
    layout.ensure()
    inferred = _scope(data_dir)
    start = start or (inferred[0] if inferred else None)
    end = end or (inferred[1] if inferred else None)
    if not start or not end:
        return {
            "ok": True,
            "state": "WAITING_FOR_RESEARCH_PANEL_SCOPE",
            "version": VERSION,
            "broker_authority": "NONE",
        }
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("coverage start is after end")
    if not str(operational_dsn or "").strip():
        raise ValueError("PROJECT_LADDU_OPERATIONAL_DSN is required")

    # The transport-neutral ingestion service reads the DSN from process env.
    # Ensure the exact CLI/runtime authority is visible without overriding an
    # already established environment binding.
    os.environ.setdefault("PROJECT_LADDU_OPERATIONAL_DSN", str(operational_dsn).strip())

    manifest = manifest or CorporateActionChunkManifest(layout)
    ingestion = ingestion_service or NseOfficialReportIngestionService(layout)
    fetcher = fetcher or _fetch
    requested = [(a.isoformat(), b.isoformat()) for a, b in _chunks(start, end, chunk_days)]
    attestation_path = layout.manifests_dir / "corporate_actions" / "market-range-attestation.json"
    progress_path = layout.manifests_dir / "corporate_actions" / "market-range-progress.json"

    try:
        prior = json.loads(attestation_path.read_text(encoding="utf-8-sig"))
    except Exception:
        prior = {}
    if (
        prior.get("ok") is True
        and str(prior.get("coverage_start") or "") <= start
        and str(prior.get("coverage_end") or "") >= end
        and prior.get("range_source_hash")
    ):
        from tools.reconcile_corporate_action_authority import reconcile
        recon = reconcile(
            operational_dsn,
            data_dir=data_dir,
            coverage_start=start,
            coverage_end=end,
            range_source_hash=str(prior["range_source_hash"]),
        )
        return {
            "ok": bool(recon.get("ok")),
            "state": "ALREADY_COVERED_RECONCILED" if recon.get("ok") else "RECONCILIATION_FAILED",
            "version": VERSION,
            "coverage_start": start,
            "coverage_end": end,
            "attestation": prior,
            "reconciliation": recon,
            "broker_authority": "NONE",
        }

    rows = manifest.list_for_range(
        source_family=SOURCE_FAMILY,
        exchange=EXCHANGE,
        request_version=REQUEST_VERSION,
        requested=requested,
    )
    already_complete = {
        (str(row.get("range_start")), str(row.get("range_end"))): row
        for row in rows if manifest.completed(row)
    }

    user_agent = str(os.environ.get("PROJECT_LADDU_NSE_USER_AGENT") or DEFAULT_USER_AGENT).strip()
    # Lazy session creation means a run containing only proven/cooldown/permanent
    # chunks performs zero NSE requests, including zero homepage warm-up calls.
    opener = None
    warm: dict[str, Any] = {"ok": None, "state": "NOT_NEEDED"}
    min_delay = max(0.0, float(os.environ.get("PROJECT_LADDU_NSE_CA_MIN_REQUEST_DELAY_SEC", "1.25") or 1.25))
    configured_budget = int(os.environ.get("PROJECT_LADDU_NSE_CA_REQUEST_BUDGET", "0") or 0)
    request_budget = configured_budget if configured_budget > 0 else max(24, len(requested) + 6)
    requests_used = 0
    progress_made = False
    resumed_without_network = 0
    cooldown_chunks = 0
    permanent_failure_chunks = 0
    last_request_at = 0.0

    for chunk_start_s, chunk_end_s in requested:
        key = (chunk_start_s, chunk_end_s)
        if key in already_complete:
            continue
        chunk_start = date.fromisoformat(chunk_start_s)
        chunk_end = date.fromisoformat(chunk_end_s)
        identity = {
            "source_family": SOURCE_FAMILY,
            "exchange": EXCHANGE,
            "range_start": chunk_start_s,
            "range_end": chunk_end_s,
            "request_version": REQUEST_VERSION,
        }
        current = manifest.load(**identity)
        current_status = str(current.get("status") or "MISSING").upper()
        if current_status == "FAILED_PERMANENT":
            permanent_failure_chunks += 1
            continue
        if current_status == "FAILED_RETRYABLE" and not manifest.retry_due(current):
            cooldown_chunks += 1
            continue

        attempt_count = int(current.get("attempt_count") or 0)
        last_failure: FetchFailure | None = None
        fetched: dict[str, Any] | None = None
        validation: dict[str, Any] | None = None

        # If transport already succeeded and raw evidence was durably checkpointed,
        # resume validation/projection locally. A PostgreSQL outage must not cause
        # another NSE download.
        if current_status in {"FETCHED", "VALIDATED"}:
            raw_path = Path(str(current.get("raw_evidence_path") or ""))
            if raw_path.is_file():
                try:
                    raw_payload = raw_path.read_bytes()
                    raw_hash = hashlib.sha256(raw_payload).hexdigest()
                    expected_hash = str(current.get("payload_sha256") or "")
                    if expected_hash and raw_hash != expected_hash:
                        raise FetchFailure("durable raw chunk hash mismatch", retryable=False, error_code="RAW_EVIDENCE_HASH_MISMATCH")
                    validation = _validate_payload(
                        payload=raw_payload, content_type=str(current.get("content_type") or ""),
                        chunk_start=chunk_start, chunk_end=chunk_end,
                    )
                    fetched = {
                        "payload": raw_payload,
                        "content_type": str(current.get("content_type") or ""),
                        "http_status": int(current.get("last_http_status") or 200),
                        "url": str(current.get("source_url") or ENDPOINT),
                    }
                    resumed_without_network += 1
                except FetchFailure as exc:
                    current = manifest.write({
                        **current, **identity,
                        "status": "FAILED_RETRYABLE" if exc.retryable else "FAILED_PERMANENT",
                        "last_error_code": exc.error_code,
                        "last_error_message": str(exc)[:1000],
                        "next_retry_at": _retry_at(15) if exc.retryable else None,
                    })
                    if not exc.retryable:
                        permanent_failure_chunks += 1
                    continue
                except Exception as exc:
                    manifest.write({
                        **current, **identity, "status": "FAILED_RETRYABLE",
                        "last_error_code": "RAW_EVIDENCE_READ_FAILED",
                        "last_error_message": f"{type(exc).__name__}: {exc}"[:1000],
                        "next_retry_at": _retry_at(15),
                    })
                    continue
            else:
                manifest.write({
                    **current, **identity, "status": "FAILED_RETRYABLE",
                    "last_error_code": "RAW_EVIDENCE_MISSING",
                    "last_error_message": "validated/fetched checkpoint has no readable raw evidence",
                    "next_retry_at": _retry_at(15),
                })
                continue

        for local_attempt in range(3) if fetched is None else range(0):
            if requests_used >= request_budget:
                last_failure = FetchFailure(
                    f"Per-run NSE request budget exhausted ({request_budget})",
                    retryable=True,
                    error_code="REQUEST_BUDGET_EXHAUSTED",
                )
                break
            since = time.monotonic() - last_request_at if last_request_at else min_delay
            if since < min_delay:
                sleep_fn(min_delay - since)
            requests_used += 1
            attempt_count += 1
            last_request_at = time.monotonic()
            try:
                if opener is None:
                    opener = _new_opener(user_agent)
                    warm = _warm(opener, user_agent)
                fetched = fetcher(opener, chunk_start, chunk_end, user_agent=user_agent)
                payload = bytes(fetched.get("payload") or b"")
                content_type = str(fetched.get("content_type") or "")
                validation = _validate_payload(
                    payload=payload,
                    content_type=content_type,
                    chunk_start=chunk_start,
                    chunk_end=chunk_end,
                )
                break
            except FetchFailure as exc:
                last_failure = exc
                current = manifest.write({
                    **current,
                    **identity,
                    "status": "FAILED_RETRYABLE" if exc.retryable else "FAILED_PERMANENT",
                    "attempt_count": attempt_count,
                    "last_http_status": exc.http_status,
                    "last_error_code": exc.error_code,
                    "last_error_message": str(exc)[:1000],
                    "content_type": exc.content_type,
                    "last_attempt_at": _now(),
                    "next_retry_at": _retry_at(15) if exc.retryable else None,
                })
                if not exc.retryable:
                    break
                # 401/403 means the NSE session/cookies may have expired. Recreate
                # the session and warm the homepage before the next bounded retry.
                if exc.http_status in {401, 403}:
                    opener = _new_opener(user_agent)
                    warm = _warm(opener, user_agent)
                if local_attempt < 2:
                    backoff = min(30.0, (2.0 ** local_attempt) * 2.0 + random_fn())
                    sleep_fn(backoff)
            except Exception as exc:
                last_failure = FetchFailure(
                    f"{type(exc).__name__}: {exc}",
                    retryable=True,
                    error_code="UNEXPECTED_FETCH_FAILURE",
                )
                current = manifest.write({
                    **current,
                    **identity,
                    "status": "FAILED_RETRYABLE",
                    "attempt_count": attempt_count,
                    "last_http_status": None,
                    "last_error_code": last_failure.error_code,
                    "last_error_message": str(last_failure)[:1000],
                    "last_attempt_at": _now(),
                    "next_retry_at": _retry_at(15),
                })
                if local_attempt < 2:
                    sleep_fn(min(30.0, (2.0 ** local_attempt) * 2.0 + random_fn()))

        if fetched is None or validation is None:
            continue

        payload = bytes(fetched.get("payload") or b"")
        content_type = str(fetched.get("content_type") or "")
        payload_sha256 = str(validation["payload_sha256"])
        raw_path = _raw_evidence_path(layout, chunk_start, chunk_end, payload_sha256, content_type)
        _write_raw_evidence(raw_path, payload)
        base = {
            **current,
            **identity,
            "attempt_count": attempt_count,
            "last_http_status": int(fetched.get("http_status") or 200),
            "last_error_code": None,
            "last_error_message": None,
            "next_retry_at": None,
            "payload_sha256": payload_sha256,
            "row_count": int(validation["row_count"]),
            "response_bytes": len(payload),
            "content_type": content_type,
            "source_url": str(fetched.get("url") or ENDPOINT),
            "raw_evidence_path": str(raw_path),
            "fetched_at": _now(),
            "validated_at": _now(),
            "schema_valid": True,
        }
        if int(validation["row_count"]) == 0:
            manifest.write({
                **base,
                "status": "EMPTY_VALID",
                "published_at": _now(),
                "ingestion_state": "ZERO_ACTION_RANGE_ATTESTED",
                "rows_projected": 0,
            })
            progress_made = True
            continue

        # Persist a validated checkpoint before projection so a crash is visible
        # as VALIDATED rather than looking as if the chunk was never fetched.
        manifest.write({**base, "status": "VALIDATED", "published_at": None})
        try:
            normalized = bytes(validation["normalized"])
            result = ingestion.ingest_bytes(
                source_key=SOURCE_FAMILY,
                trade_date=chunk_end.isoformat(),
                payload=normalized,
                filename=f"nse-corporate-actions-{chunk_start.isoformat()}-{chunk_end.isoformat()}.csv",
                source_url=ENDPOINT,
                source_metadata={
                    "range_start": chunk_start.isoformat(),
                    "range_end": chunk_end.isoformat(),
                    "market_wide": True,
                    "exchange": EXCHANGE,
                    "transport_version": VERSION,
                    "request_version": REQUEST_VERSION,
                    "source_payload_sha256": payload_sha256,
                },
            )
            projection = dict(result.get("postgres_projection") or {})
            projected = str(projection.get("state") or "").upper() == "PROJECTED"
            if not result.get("ok") or not projected:
                raise FetchFailure(
                    f"Corporate-action chunk did not reach PostgreSQL projection: state={result.get('state')} projection={projection.get('state')}",
                    retryable=True,
                    error_code="POSTGRES_PROJECTION_INCOMPLETE",
                )
            manifest.write({
                **base,
                "status": "PUBLISHED",
                "published_at": _now(),
                "ingestion_state": result.get("state"),
                "ingestion_content_hash": result.get("content_hash"),
                "rows_projected": int(projection.get("rows_projected") or 0),
                "curated_path": result.get("curated_path"),
            })
            progress_made = True
        except FetchFailure as exc:
            manifest.write({
                **base,
                "status": "FAILED_RETRYABLE" if exc.retryable else "FAILED_PERMANENT",
                "published_at": None,
                "last_error_code": exc.error_code,
                "last_error_message": str(exc)[:1000],
                "next_retry_at": _retry_at(10) if exc.retryable else None,
            })
        except Exception as exc:
            manifest.write({
                **base,
                "status": "FAILED_RETRYABLE",
                "published_at": None,
                "last_error_code": "INGESTION_FAILED",
                "last_error_message": f"{type(exc).__name__}: {exc}"[:1000],
                "next_retry_at": _retry_at(10),
            })

    final_rows = manifest.list_for_range(
        source_family=SOURCE_FAMILY,
        exchange=EXCHANGE,
        request_version=REQUEST_VERSION,
        requested=requested,
    )
    summary = manifest.summary(final_rows)
    complete_rows = [row for row in final_rows if manifest.completed(row)]
    complete = len(complete_rows) == len(final_rows) and all(str(row.get("status") or "") in TERMINAL_SUCCESS for row in complete_rows)
    failed = [
        {
            "start": row.get("range_start"),
            "end": row.get("range_end"),
            "status": row.get("status"),
            "http_status": row.get("last_http_status"),
            "error_code": row.get("last_error_code"),
            "error": row.get("last_error_message"),
            "next_retry_at": row.get("next_retry_at"),
        }
        for row in final_rows
        if str(row.get("status") or "").upper() not in TERMINAL_SUCCESS
    ]
    progress = {
        "ok": complete,
        "state": "RANGE_ACQUISITION_COMPLETE" if complete else "RANGE_ACQUISITION_PARTIAL",
        "version": VERSION,
        "request_version": REQUEST_VERSION,
        "coverage_start": start,
        "coverage_end": end,
        **summary,
        "failed_chunks": len(failed),
        "retryable_chunks": sum(1 for row in failed if str(row.get("status") or "").upper() == "FAILED_RETRYABLE"),
        "progress_made": bool(progress_made),
        "chunk_manifest_written": True,
        "coverage_written": False,
        "complete_market_range": False,
        "complete_coverage": False,
        "requests_used": requests_used,
        "request_budget": request_budget,
        "resumed_without_network": resumed_without_network,
        "cooldown_chunks": cooldown_chunks,
        "permanent_failure_chunks": permanent_failure_chunks,
        "session_warmup": warm,
        "failures": failed,
        "chunks": [
            {
                "start": row.get("range_start"),
                "end": row.get("range_end"),
                "status": row.get("status"),
                "payload_sha256": row.get("payload_sha256"),
                "row_count": row.get("row_count"),
                "attempt_count": row.get("attempt_count"),
                "last_http_status": row.get("last_http_status"),
                "last_error_code": row.get("last_error_code"),
                "next_retry_at": row.get("next_retry_at"),
            }
            for row in final_rows
        ],
        "broker_authority": "NONE",
        "captured_at": _now(),
    }
    atomic_write_json(progress_path, progress)
    if not complete:
        return progress

    stable = [_stable_chunk_evidence(row) for row in final_rows]
    range_hash = hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    attestation = {
        "ok": True,
        "version": VERSION,
        "request_version": REQUEST_VERSION,
        "authority": "NSE_OFFICIAL_MARKET_WIDE_CORPORATE_ACTION_RANGE",
        "coverage_start": start,
        "coverage_end": end,
        "range_source_hash": range_hash,
        "chunks": stable,
        "captured_at": _now(),
        "zero_action_absence_policy": "ONLY_EXACT_SUCCESSFUL_MARKET_WIDE_RANGE_REQUESTS_CERTIFY_ABSENCE",
        "resume_policy": "PUBLISHED_OR_EMPTY_VALID_CHUNKS_ARE_DURABLY_REUSED",
    }
    atomic_write_json(attestation_path, attestation)

    from tools.reconcile_corporate_action_authority import reconcile
    recon = reconcile(
        operational_dsn,
        data_dir=data_dir,
        coverage_start=start,
        coverage_end=end,
        range_source_hash=range_hash,
    )
    panel_symbols = int(recon.get("panel_symbols") or 0)
    complete_rows = int(recon.get("complete_coverage_rows") or 0)
    result = {
        **progress,
        "ok": bool(recon.get("ok")),
        "state": "RANGE_SYNC_AND_RECONCILIATION_COMPLETE" if recon.get("ok") else "RECONCILIATION_FAILED",
        "range_source_hash": range_hash,
        "coverage_written": bool(recon.get("coverage_rows_written")),
        "complete_market_range": True,
        "complete_coverage": bool(panel_symbols and complete_rows >= panel_symbols),
        "reconciliation": recon,
    }
    atomic_write_json(progress_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--operational-dsn", default=os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", ""))
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--chunk-days", type=int, default=365)
    args = parser.parse_args()
    if not str(args.operational_dsn or "").strip():
        print(json.dumps({
            "ok": False,
            "state": "BLOCKED",
            "reason": "PROJECT_LADDU_OPERATIONAL_DSN is required",
        }, indent=2))
        return 2
    try:
        result = sync(
            data_dir=Path(args.data_dir),
            operational_dsn=str(args.operational_dsn).strip(),
            start=args.start or None,
            end=args.end or None,
            chunk_days=args.chunk_days,
        )
    except Exception as exc:
        result = {"ok": False, "state": "BLOCKED", "reason": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
