"""Governed issuer-logo refresh with bounded repository population.

Runtime GET routes never fetch images. The scheduled job first admits explicit
issuer-owned assets, then resolves a bounded batch from the configured NSE/BSE
logo catalogue. SVG files are sanitised by the import service, content-hashed and
stored as local data URIs. A durable cursor makes the full universe converge over
successive runs without thousands of requests in one cycle.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
import tempfile
from urllib.parse import urljoin, urlparse

import requests

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.instrument_brand_asset_service import MAX_ASSET_BYTES, REGISTRY_RELATIVE_PATH
from core.storage_layout import atomic_write_json
from tools.import_instrument_brand_assets import import_asset

SERVICE_VERSION = "instrument-brand-asset-refresh-2.0.0-bounded-repository-population"
USER_AGENT = "Project-Laddu-Brand-Asset-Collector/2.0"
ALLOWED_CONTENT_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/svg+xml": ".svg"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _public_host(host: str) -> bool:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError:
        return False
    for address in addresses:
        if address.startswith(("10.", "127.", "169.254.", "192.168.")):
            return False
        if address.startswith("172."):
            try:
                second = int(address.split(".")[1])
            except (ValueError, IndexError):
                second = -1
            if 16 <= second <= 31:
                return False
        if address in {"::1", "0.0.0.0"} or address.lower().startswith(("fc", "fd", "fe80")):
            return False
    return bool(addresses)


def _existing_symbols(data_dir: Path) -> set[str]:
    path = Path(data_dir) / REGISTRY_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {str(key).upper() for key in (payload.get("assets") or {})}
    except (OSError, ValueError, TypeError):
        return set()


def _download(session: requests.Session, url: str, allowed_host: str, timeout_seconds: float) -> tuple[bytes, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname is None or not (parsed.hostname.lower() == allowed_host or parsed.hostname.lower().endswith("." + allowed_host)):
        raise ValueError("source URL left governed host")
    response = session.get(url, headers={"User-Agent": USER_AGENT, "Accept": "image/svg+xml,image/png,image/jpeg,image/webp,application/json"}, timeout=timeout_seconds, stream=True, allow_redirects=True)
    response.raise_for_status()
    final = urlparse(response.url)
    if final.scheme != "https" or final.hostname is None or not (final.hostname.lower() == allowed_host or final.hostname.lower().endswith("." + allowed_host)):
        raise ValueError("redirect left governed host")
    content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
    payload = bytearray()
    for chunk in response.iter_content(32 * 1024):
        payload.extend(chunk)
        if len(payload) > MAX_ASSET_BYTES:
            raise ValueError("asset exceeds maximum size")
    if not payload:
        raise ValueError("empty asset")
    if content_type not in ALLOWED_CONTENT_TYPES:
        # GitHub Pages occasionally serves SVG as octet-stream; extension is safe
        # only because import_asset performs content-level type detection/sanitising.
        suffix = Path(final.path).suffix.lower()
        if suffix != ".svg":
            raise ValueError(f"unsupported content type {content_type or 'missing'}")
        content_type = "image/svg+xml"
    return bytes(payload), ALLOWED_CONTENT_TYPES[content_type]


def _import_downloaded(*, session: requests.Session, data_dir: Path, symbol: str, exchange: str, source_url: str, allowed_host: str, authority: str, timeout_seconds: float) -> dict:
    payload, suffix = _download(session, source_url, allowed_host, timeout_seconds)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    try:
        return import_asset(data_dir=data_dir, symbol=symbol, exchange=exchange, image_file=temp_path, source_authority=authority, source_url=source_url)
    finally:
        temp_path.unlink(missing_ok=True)


def _normalise_catalogue_rows(document: object, base_url: str) -> list[dict]:
    if isinstance(document, dict):
        raw = document.get("logos") or document.get("data") or document.get("rows") or []
    else:
        raw = document if isinstance(document, list) else []
    rows = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("ticker") or item.get("symbol") or "").upper().strip()
        exchange = str(item.get("exchange") or "NSE").upper().strip()
        file_value = str(item.get("file") or item.get("path") or "").strip()
        if not symbol or exchange not in {"NSE", "BSE"}:
            continue
        if not file_value:
            safe_symbol = symbol.replace("&", "%26")
            file_value = f"{exchange.lower()}/{exchange}_{safe_symbol}.svg"
        rows.append({"symbol": symbol, "exchange": exchange, "url": urljoin(base_url, file_value)})
    return rows


def refresh(*, data_dir: Path, plan: Path, timeout_seconds: float = 15.0, limit_override: int | None = None) -> dict:
    document = json.loads(Path(plan).read_text(encoding="utf-8"))
    explicit = document.get("sources") if isinstance(document, dict) else []
    catalogues = document.get("catalogues") if isinstance(document, dict) else []
    data_dir = Path(data_dir)
    session = requests.Session()
    results: list[dict] = []
    required_failures = 0
    for row in explicit if isinstance(explicit, list) else []:
        symbol = str(row.get("symbol") or "").upper().strip()
        source_url = str(row.get("source_url") or "").strip()
        allowed_host = str(row.get("allowed_host") or "").lower().strip()
        required = row.get("required") is True
        try:
            if not symbol or not source_url or not allowed_host or not _public_host(allowed_host):
                raise ValueError("explicit source policy rejected")
            imported = _import_downloaded(session=session, data_dir=data_dir, symbol=symbol, exchange=str(row.get("exchange") or "NSE"), source_url=source_url, allowed_host=allowed_host, authority=str(row.get("source_authority") or "ISSUER_OFFICIAL_SITE"), timeout_seconds=timeout_seconds)
            results.append({**imported, "state": "READY", "source": "EXPLICIT"})
        except Exception as exc:
            required_failures += int(required)
            results.append({"symbol": symbol, "ok": False, "state": "FETCH_FAILED", "required": required, "error": str(exc), "source": "EXPLICIT"})

    state_path = data_dir / "reference" / "instrument_brand_refresh_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        state = {}
    existing = _existing_symbols(data_dir)
    catalogue_summaries = []
    for catalogue in catalogues if isinstance(catalogues, list) else []:
        if catalogue.get("enabled") is False:
            continue
        key = str(catalogue.get("catalogue_key") or "catalogue")
        required = catalogue.get("required") is True
        manifest_url = str(catalogue.get("manifest_url") or "").strip()
        base_url = str(catalogue.get("asset_base_url") or "").strip()
        allowed_host = str(catalogue.get("allowed_host") or "").lower().strip()
        authority = str(catalogue.get("source_authority") or "GOVERNED_OPEN_SOURCE_LOGO_REPOSITORY")
        limit = max(1, min(1500, int(limit_override if limit_override is not None else (catalogue.get("max_assets_per_run") or 300))))
        summary = {"catalogue_key": key, "required": required, "attempted": 0, "imported": 0, "failed": 0}
        try:
            if not manifest_url or not base_url or not allowed_host or not _public_host(allowed_host):
                raise ValueError("catalogue source policy rejected")
            response = session.get(manifest_url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=timeout_seconds)
            response.raise_for_status()
            rows = _normalise_catalogue_rows(response.json(), base_url)
            if not rows:
                raise ValueError("catalogue manifest contains no logo rows")
            # Preserve manifest order (normally market-cap/relevance order), skip cached
            # symbols, and cycle deterministically through the remaining universe.
            pending = [row for row in rows if row["symbol"] not in existing]
            cursor = int((state.get("catalogues") or {}).get(key, {}).get("cursor") or 0)
            if pending:
                cursor %= len(pending)
                batch = (pending[cursor:] + pending[:cursor])[:limit]
            else:
                batch = []
            summary.update({"manifest_rows": len(rows), "already_cached": len(existing), "pending": len(pending), "attempted": len(batch)})
            def worker(item: dict) -> dict:
                try:
                    imported = _import_downloaded(session=requests.Session(), data_dir=data_dir, symbol=item["symbol"], exchange=item["exchange"], source_url=item["url"], allowed_host=allowed_host, authority=authority, timeout_seconds=timeout_seconds)
                    return {**imported, "state": "READY", "source": key}
                except Exception as exc:
                    return {"symbol": item["symbol"], "ok": False, "state": "FETCH_FAILED", "error": str(exc), "source": key}
            if batch:
                with ThreadPoolExecutor(max_workers=min(8, len(batch)), thread_name_prefix="brand-assets") as pool:
                    futures = [pool.submit(worker, item) for item in batch]
                    for future in as_completed(futures):
                        result = future.result()
                        results.append(result)
                        if result.get("ok"):
                            summary["imported"] += 1
                            existing.add(str(result.get("symbol") or "").upper())
                        else:
                            summary["failed"] += 1
            state.setdefault("catalogues", {})[key] = {
                "cursor": (cursor + len(batch)) % max(1, len(pending)),
                "manifest_rows": len(rows),
                "last_run_at": now_iso(),
                "last_imported": summary["imported"],
                "last_failed": summary["failed"],
            }
        except Exception as exc:
            summary.update({"state": "CATALOGUE_FAILED", "error": str(exc)})
            required_failures += int(required)
        catalogue_summaries.append(summary)
    state.update({"version": SERVICE_VERSION, "updated_at": now_iso()})
    atomic_write_json(state_path, state)
    imported_count = sum(1 for item in results if item.get("ok"))
    return {
        "ok": required_failures == 0,
        "service_version": SERVICE_VERSION,
        "generated_at": now_iso(),
        "plan": str(plan),
        "registry_asset_count": len(_existing_symbols(data_dir)),
        "imported_this_run": imported_count,
        "required_failures": required_failures,
        "catalogues": catalogue_summaries,
        "results": results[:100],
        "result_count": len(results),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--plan", type=Path, default=BACKEND / "resources" / "instrument_brand_sources.json")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    result = refresh(data_dir=args.data_dir, plan=args.plan, limit_override=args.limit)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
