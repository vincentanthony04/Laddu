from __future__ import annotations

"""Build and commit the focused reference authority in one process.

The installer calls this tool once. It owns the complete chain:
focused SQLite staging projection -> source proof -> PostgreSQL replacement ->
target proof -> one durable JSON authority report. PowerShell no longer passes a
path between two independent migration programs.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")



def _catalogue_digest(rows: list[dict[str, Any]], revision: str) -> str:
    from core.data_plane.instrument_repository import canonical_instrument_row

    canonical = []
    for row in rows:
        item = canonical_instrument_row(row, revision)
        canonical.append({
            "provider_instrument_key": item["provider_instrument_key"],
            "exchange": item["exchange"],
            "trading_symbol": item["trading_symbol"],
            "display_name": item["display_name"],
            "isin": item["isin"] or "",
            "asset_class": item["asset_class"],
            "exchange_series": item["exchange_series"],
            "lot_size": int(item["lot_size"]),
            "tick_size": format(float(item["tick_size"]), ".12g"),
            "universe_revision": item["universe_revision"],
            "classification_reason": item["classification_reason"],
        })
    canonical.sort(key=lambda item: (item["provider_instrument_key"], item["exchange"], item["trading_symbol"]))
    material = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_reference_authority(*, install_dir: Path, dsn: str) -> dict[str, Any]:
    install_dir = install_dir.resolve()
    os.environ["PROJECT_LADDU_HOME"] = str(install_dir)

    from config import DB_PATH
    from core.focused_instrument_migration import migrate_database
    from core.instrument_universe_policy import ACTIVE_UNIVERSE_REVISION
    from core.data_plane.instrument_repository import ProductionInstrumentRepository
    from core.data_plane.postgres import PostgresAuthority
    from tools.migrate_focused_instrument_catalog import _fresh_bootstrap, _row_count
    from tools.migrate_reference_catalog_to_postgres import _load_projection

    staging_path = Path(DB_PATH).resolve()
    if _row_count(staging_path) > 0:
        staging = migrate_database(staging_path)
    else:
        staging = _fresh_bootstrap()
    staging = dict(staging)
    staging["database"] = str(staging_path)

    rows, revision, meta = _load_projection(staging_path)
    if revision != ACTIVE_UNIVERSE_REVISION:
        raise RuntimeError(
            f"reference bootstrap revision mismatch: expected={ACTIVE_UNIVERSE_REVISION} actual={revision}"
        )

    source_digest = _catalogue_digest(rows, revision)
    authority = PostgresAuthority(
        dsn,
        role="operational-reference-bootstrap",
        min_size=1,
        max_size=2,
    )
    try:
        authority.open()
        repository = ProductionInstrumentRepository(authority)
        existing = repository.proof(revision=revision)
        existing_ready = (
            existing.active_total > 0
            and existing.nse_equities > 0
            and existing.bse_only_equities > 0
            and existing.indices > 0
            and existing.derivatives == 0
            and existing.out_of_policy_rows == 0
        )
        if existing_ready:
            target = existing
            target_rows = repository.active_rows(revision=revision)
            reused_existing_postgres = True
        else:
            target = repository.replace_active(rows, revision=revision)
            target_rows = repository.active_rows(revision=revision)
            reused_existing_postgres = False
    finally:
        authority.close()

    target_digest = _catalogue_digest(target_rows, revision)
    target_dict = target.as_dict()
    if target.active_total != len(rows):
        raise RuntimeError(
            f"reference bootstrap count mismatch: source={len(rows)} target={target.active_total}"
        )
    if target.revision != revision:
        raise RuntimeError(
            f"reference bootstrap target revision mismatch: source={revision} target={target.revision}"
        )
    if source_digest != target_digest:
        raise RuntimeError(
            f"reference bootstrap content mismatch: source_sha256={source_digest} target_sha256={target_digest}"
        )

    return {
        "ok": True,
        "state": "REFERENCE_AUTHORITY_READY",
        "service_version": "reference-authority-bootstrap-1.0.0",
        "universe_revision": revision,
        "source": {
            "sqlite": str(staging_path),
            "count": len(rows),
            "format": str(meta.get("format") or ""),
            "content_revision": revision,
            "content_sha256": source_digest,
            "staging_proof": staging,
        },
        "target": {
            "engine": "postgresql",
            "content_sha256": target_digest,
            "proof": target_dict,
        },
        "reconciled": True,
        "reused_existing_postgres": reused_existing_postgres,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    dsn = os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", "").strip()
    if not dsn:
        raise RuntimeError("PROJECT_LADDU_OPERATIONAL_DSN is required")

    try:
        report = build_reference_authority(install_dir=args.install_dir, dsn=dsn)
        _write_report(args.report, report)
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        failure = {
            "ok": False,
            "state": "REFERENCE_AUTHORITY_FAILED",
            "service_version": "reference-authority-bootstrap-1.0.0",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "install_dir": str(args.install_dir.resolve()),
        }
        _write_report(args.report, failure)
        print(json.dumps(failure, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
