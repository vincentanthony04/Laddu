"""Verified, locally cached instrument brand assets for the terminal.

The live UI never scrapes arbitrary logo providers.  An operator or governed
reference-data job imports a small PNG/JPEG/WebP asset, records its source and
content hash, and stores it as a data URI under the persistent data directory.
Only verified, hash-matching assets are returned; otherwise the frontend uses a neutral issuer placeholder; index rows may use packaged exchange artwork.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

SERVICE_VERSION = "instrument-brand-asset-authority-2.0.0-governed-repository-cache"
REGISTRY_RELATIVE_PATH = Path("reference") / "instrument_brand_assets.json"
ALLOWED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/svg+xml"})
ALLOWED_AUTHORITIES = frozenset({
    "NSE_ISSUER_PROFILE",
    "ISSUER_OFFICIAL_SITE",
    "OPERATOR_VERIFIED_ISSUER_ASSET",
    "GOVERNED_OPEN_SOURCE_LOGO_REPOSITORY",
})
MAX_ASSET_BYTES = 256 * 1024


def _normalise_symbol(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper().strip() if ch.isalnum() or ch in {"&", "-"})


def _decode_data_uri(value: str) -> tuple[str, bytes] | None:
    if not isinstance(value, str) or not value.startswith("data:image/") or ";base64," not in value:
        return None
    prefix, encoded = value.split(",", 1)
    mime = prefix[5:].split(";", 1)[0].lower()
    if mime not in ALLOWED_MIME_TYPES:
        return None
    try:
        payload = base64.b64decode(encoded, validate=True)
    except Exception:
        return None
    if not payload or len(payload) > MAX_ASSET_BYTES:
        return None
    return mime, payload


class InstrumentBrandAssetService:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.registry_path = self.data_dir / REGISTRY_RELATIVE_PATH

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _validated_asset(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict) or raw.get("verified") is not True:
            return None
        authority = str(raw.get("source_authority") or "").strip().upper()
        if authority not in ALLOWED_AUTHORITIES:
            return None
        decoded = _decode_data_uri(str(raw.get("data_uri") or ""))
        if not decoded:
            return None
        mime, payload = decoded
        digest = hashlib.sha256(payload).hexdigest()
        if digest.lower() != str(raw.get("content_sha256") or "").lower():
            return None
        symbol = _normalise_symbol(raw.get("symbol"))
        if not symbol:
            return None
        return {
            "symbol": symbol,
            "exchange": str(raw.get("exchange") or "NSE").upper(),
            "verified_logo_url": raw["data_uri"],
            "content_sha256": digest,
            "source_authority": authority,
            "source_url": raw.get("source_url"),
            "verified_at": raw.get("verified_at"),
            "mime_type": mime,
        }

    def status(self, symbols: Iterable[str] | None = None) -> dict[str, Any]:
        requested = {_normalise_symbol(value) for value in (symbols or []) if _normalise_symbol(value)}
        payload = self._load()
        raw_assets = payload.get("assets") if isinstance(payload.get("assets"), dict) else {}
        assets: dict[str, dict[str, Any]] = {}
        rejected = 0
        for key, raw in raw_assets.items():
            candidate = dict(raw) if isinstance(raw, dict) else raw
            if isinstance(candidate, dict) and not candidate.get("symbol"):
                candidate["symbol"] = key
            valid = self._validated_asset(candidate)
            if valid is None:
                rejected += 1
                continue
            if requested and valid["symbol"] not in requested:
                continue
            assets[valid["symbol"]] = valid
        return {
            "ok": True,
            "service_version": SERVICE_VERSION,
            "authority": "LOCAL_VERIFIED_CONTENT_HASH_REGISTRY",
            "registry_present": self.registry_path.is_file(),
            "requested_count": len(requested),
            "asset_count": len(assets),
            "rejected_count": rejected,
            "assets": assets,
            "fallback": "NEUTRAL_ISSUER_PLACEHOLDER_OR_PACKAGED_INDEX_ARTWORK",
            "network_fetch_in_get_route": False,
        }
