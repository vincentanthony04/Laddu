"""Import a verified issuer/index logo into the persistent brand-asset registry."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.instrument_brand_asset_service import (
    ALLOWED_AUTHORITIES,
    ALLOWED_MIME_TYPES,
    MAX_ASSET_BYTES,
    REGISTRY_RELATIVE_PATH,
    _normalise_symbol,
)
from core.storage_layout import atomic_write_json, interprocess_lock


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_svg(payload: bytes) -> None:
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered or b"javascript:" in lowered:
        raise ValueError("unsafe SVG declaration")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError("invalid SVG") from exc
    if not str(root.tag).lower().endswith("svg"):
        raise ValueError("SVG root element required")
    for node in root.iter():
        tag = str(node.tag).lower().split("}")[-1]
        if tag in {"script", "foreignobject", "iframe", "object", "embed"}:
            raise ValueError(f"unsafe SVG element: {tag}")
        for name, value in node.attrib.items():
            key = str(name).lower().split("}")[-1]
            text = str(value or "").strip().lower()
            if key.startswith("on") or (key in {"href", "src"} and text and not text.startswith("#")):
                raise ValueError("unsafe SVG external/event attribute")


def detect_mime(path: Path, payload: bytes) -> str:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    stripped = payload.lstrip()
    if stripped.startswith(b"<svg") or (stripped.startswith(b"<?xml") and b"<svg" in stripped[:1024]):
        _validate_svg(payload)
        return "image/svg+xml"
    guessed = mimetypes.guess_type(path.name)[0] or ""
    if guessed == "image/svg+xml":
        _validate_svg(payload)
    return guessed.lower()


def import_asset(*, data_dir: Path, symbol: str, exchange: str, image_file: Path,
                 source_authority: str, source_url: str | None) -> dict:
    symbol = _normalise_symbol(symbol)
    authority = str(source_authority or "").upper().strip()
    if not symbol:
        raise ValueError("symbol is required")
    if authority not in ALLOWED_AUTHORITIES:
        raise ValueError(f"source_authority must be one of {sorted(ALLOWED_AUTHORITIES)}")
    payload = Path(image_file).read_bytes()
    if not payload or len(payload) > MAX_ASSET_BYTES:
        raise ValueError(f"asset must be between 1 and {MAX_ASSET_BYTES} bytes")
    mime = detect_mime(Path(image_file), payload)
    if mime not in ALLOWED_MIME_TYPES:
        raise ValueError(f"unsupported logo type: {mime or 'unknown'}")
    digest = hashlib.sha256(payload).hexdigest()
    registry = Path(data_dir) / REGISTRY_RELATIVE_PATH
    lock = Path(data_dir) / "runtime" / "locks" / "instrument-brand-assets.lock"
    with interprocess_lock(lock, timeout_seconds=5):
        try:
            document = json.loads(registry.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            document = {"version": "instrument-brand-assets-1.0.0", "assets": {}}
        assets = document.setdefault("assets", {})
        assets[symbol] = {
            "symbol": symbol,
            "exchange": str(exchange or "NSE").upper(),
            "mime_type": mime,
            "content_sha256": digest,
            "data_uri": f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}",
            "source_authority": authority,
            "source_url": source_url,
            "verified": True,
            "verified_at": now_iso(),
        }
        document["updated_at"] = now_iso()
        atomic_write_json(registry, document)
    return {"ok": True, "symbol": symbol, "exchange": str(exchange or "NSE").upper(),
            "mime_type": mime, "content_sha256": digest, "registry": str(registry)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--exchange", default="NSE")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--source-authority", required=True, choices=sorted(ALLOWED_AUTHORITIES))
    parser.add_argument("--source-url")
    args = parser.parse_args()
    print(json.dumps(import_asset(data_dir=args.data_dir, symbol=args.symbol, exchange=args.exchange,
                                  image_file=args.file, source_authority=args.source_authority,
                                  source_url=args.source_url), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
