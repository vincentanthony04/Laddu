"""Hash-chained operational proof integrity for the active Project Laddu build.

The installed product already emits scanner, browser and market-soak evidence.
This module makes those records build-bound, content-addressed and chained so
an overwritten/stale KV value cannot silently satisfy a maturity gate.

The ledger is observational only. It cannot move scanner cursors, promote a
model, create a decision or enable broker execution.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, Iterable, Mapping

from config import APP_VERSION

BUILD_ID = APP_VERSION
SERVICE_VERSION = "operational-evidence-integrity-1.0.0"
CONTRACT_VERSION = "hash-chain-evidence-1.0.0"
LEDGER_KEY = f"operational_evidence_ledger:{BUILD_ID}"
MAX_LEDGER_ENTRIES = 2048


def _map(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _without_integrity(payload: Mapping[str, Any]) -> Dict[str, Any]:
    clean = dict(payload or {})
    clean.pop("evidence_integrity", None)
    return clean


def _get(store: Any, key: str, default: Any) -> Any:
    getter = getattr(store, "get_kv", None)
    if not callable(getter):
        return default
    try:
        return getter(key, default)
    except TypeError:
        try:
            value = getter(key)
            return default if value is None else value
        except Exception:
            return default
    except Exception:
        return default


def _set(store: Any, key: str, value: Any) -> None:
    setter = getattr(store, "set_kv", None)
    if not callable(setter):
        raise RuntimeError("operational KV authority unavailable")
    setter(key, value)


def attach_evidence_integrity(
    store: Any,
    kind: str,
    payload: Mapping[str, Any],
    *,
    source_key: str,
    build: str = BUILD_ID,
) -> Dict[str, Any]:
    """Return *payload* with a persisted content/chain proof attached.

    A ledger write is performed before the caller stores the source record.
    If the later source write fails, validation reports an orphan ledger row;
    it never treats that as a pass.
    """
    clean = _without_integrity(payload)
    payload_hash = _sha(clean)
    ledger = _map(_get(store, LEDGER_KEY, {}))
    entries = [dict(row) for row in (ledger.get("entries") or []) if isinstance(row, Mapping)]
    previous_hash = str(ledger.get("head_hash") or "")
    sequence = int(ledger.get("next_sequence") or (len(entries) + 1))
    envelope = {
        "sequence": sequence,
        "kind": str(kind or "UNKNOWN").upper(),
        "build": str(build or BUILD_ID),
        "source_key": str(source_key or ""),
        "payload_hash": payload_hash,
        "previous_hash": previous_hash,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "contract_version": CONTRACT_VERSION,
    }
    record_hash = _sha(envelope)
    record = dict(envelope, record_hash=record_hash)
    entries.append(record)
    # The current release will not normally reach this bound. If it does, keep
    # a verifiable anchor for the first retained record instead of silently
    # restarting the chain.
    anchor_hash = str(ledger.get("anchor_hash") or "")
    if len(entries) > MAX_LEDGER_ENTRIES:
        removed = entries[:-MAX_LEDGER_ENTRIES]
        entries = entries[-MAX_LEDGER_ENTRIES:]
        if removed:
            anchor_hash = str(removed[-1].get("record_hash") or anchor_hash)
    ledger_record = {
        "build": BUILD_ID,
        "contract_version": CONTRACT_VERSION,
        "anchor_hash": anchor_hash,
        "head_hash": record_hash,
        "next_sequence": sequence + 1,
        "entry_count": len(entries),
        "entries": entries,
        "updated_at": envelope["recorded_at"],
    }
    _set(store, LEDGER_KEY, ledger_record)
    result = dict(clean)
    result["evidence_integrity"] = {
        "ok": True,
        "build": build,
        "kind": envelope["kind"],
        "source_key": envelope["source_key"],
        "sequence": sequence,
        "payload_hash": payload_hash,
        "previous_hash": previous_hash,
        "record_hash": record_hash,
        "contract_version": CONTRACT_VERSION,
        "ledger_key": LEDGER_KEY,
    }
    return result


class OperationalEvidenceIntegrityService:
    """Validate current Level-4 evidence and the append-only hash chain."""

    def __init__(self, app: Any):
        self.app = app
        self.store = getattr(app, "store", None)

    def _ledger(self) -> Dict[str, Any]:
        return _map(_get(self.store, LEDGER_KEY, {}))

    @staticmethod
    def _validate_chain(ledger: Mapping[str, Any]) -> Dict[str, Any]:
        entries = [dict(row) for row in (ledger.get("entries") or []) if isinstance(row, Mapping)]
        errors: list[str] = []
        previous = str(ledger.get("anchor_hash") or "")
        last_sequence: int | None = None
        hashes: set[str] = set()
        for index, row in enumerate(entries):
            record_hash = str(row.get("record_hash") or "")
            envelope = dict(row)
            envelope.pop("record_hash", None)
            expected = _sha(envelope)
            sequence = int(row.get("sequence") or 0)
            if record_hash != expected:
                errors.append(f"entry {index} record hash mismatch")
            if str(row.get("previous_hash") or "") != previous:
                errors.append(f"entry {index} previous hash mismatch")
            if last_sequence is not None and sequence != last_sequence + 1:
                errors.append(f"entry {index} sequence discontinuity")
            if str(row.get("build") or "") != BUILD_ID:
                errors.append(f"entry {index} belongs to another build")
            previous = record_hash
            last_sequence = sequence
            if record_hash:
                hashes.add(record_hash)
        if entries and str(ledger.get("head_hash") or "") != previous:
            errors.append("ledger head hash mismatch")
        return {
            "passed": bool(entries) and not errors,
            "state": "PASS" if entries and not errors else "PENDING_EVIDENCE" if not entries else "FAILED",
            "entry_count": len(entries),
            "head_hash": str(ledger.get("head_hash") or ""),
            "record_hashes": hashes,
            "errors": errors,
        }

    @staticmethod
    def _validate_payload(
        payload: Mapping[str, Any],
        *,
        expected_kind: str,
        expected_source: str,
        ledger_hashes: set[str],
    ) -> Dict[str, Any]:
        row = dict(payload or {})
        integrity = _map(row.get("evidence_integrity"))
        actual_hash = _sha(_without_integrity(row)) if row else ""
        checks = {
            "record_present": bool(row),
            "integrity_present": bool(integrity),
            "build_bound": integrity.get("build") == BUILD_ID,
            "kind_bound": str(integrity.get("kind") or "").upper() == expected_kind.upper(),
            "source_bound": str(integrity.get("source_key") or "") == expected_source,
            "payload_hash_matches": bool(actual_hash) and integrity.get("payload_hash") == actual_hash,
            "ledger_record_present": str(integrity.get("record_hash") or "") in ledger_hashes,
        }
        passed = all(checks.values())
        # Retained evidence from an older/unbound source is absence of current
        # build proof, not proof of corruption.  It must never satisfy the gate,
        # but it also must not poison the current hash-chain as FAILED.  Once a
        # record claims the current build-bound integrity contract, any mismatch
        # is a real source failure/tamper condition.
        if passed:
            state = "PASS"
            reason = "CURRENT_BUILD_BOUND_EVIDENCE"
        elif not row:
            state = "PENDING_EVIDENCE"
            reason = "EVIDENCE_NOT_YET_CAPTURED"
        elif not integrity:
            state = "PENDING_EVIDENCE"
            reason = "RETAINED_UNBOUND_EVIDENCE"
        elif integrity.get("build") != BUILD_ID:
            state = "PENDING_EVIDENCE"
            reason = "RETAINED_OTHER_BUILD_EVIDENCE"
        else:
            state = "FAILED"
            reason = "CURRENT_BUILD_INTEGRITY_MISMATCH"
        return {
            "passed": passed,
            "state": state,
            "reason": reason,
            "checks": checks,
            "record_hash": integrity.get("record_hash"),
            "payload_hash": integrity.get("payload_hash"),
        }

    def status(self) -> Dict[str, Any]:
        ledger = self._ledger()
        chain = self._validate_chain(ledger)
        hashes = set(chain.pop("record_hashes", set()))
        intraday = _map(_get(self.store, "scanner_cycle_evidence:intraday", {}))
        delivery = _map(_get(self.store, "scanner_cycle_evidence:delivery", {}))
        browser = _map(_get(self.store, "level4_browser_proof:last", {}))
        soak = _map(_get(self.store, "level4_market_soak:last", {}))
        sources = {
            "intraday_full_sweep": self._validate_payload(
                _map(intraday.get("full_sweep")), expected_kind="SCANNER_FULL_SWEEP",
                expected_source="scanner_cycle_evidence:intraday/full_sweep", ledger_hashes=hashes,
            ),
            "intraday_market_hours": self._validate_payload(
                _map(intraday.get("market_hours_analysis")), expected_kind="SCANNER_MARKET_HOURS_ANALYSIS",
                expected_source="scanner_cycle_evidence:intraday/market_hours_analysis", ledger_hashes=hashes,
            ),
            "delivery_full_sweep": self._validate_payload(
                _map(delivery.get("full_sweep")), expected_kind="SCANNER_FULL_SWEEP",
                expected_source="scanner_cycle_evidence:delivery/full_sweep", ledger_hashes=hashes,
            ),
            "delivery_market_hours": self._validate_payload(
                _map(delivery.get("market_hours_analysis")), expected_kind="SCANNER_MARKET_HOURS_ANALYSIS",
                expected_source="scanner_cycle_evidence:delivery/market_hours_analysis", ledger_hashes=hashes,
            ),
            "browser_proof": self._validate_payload(
                browser, expected_kind="BROWSER_SELF_CHECK", expected_source="level4_browser_proof:last",
                ledger_hashes=hashes,
            ),
            "market_soak": self._validate_payload(
                soak, expected_kind="MARKET_HOURS_SOAK", expected_source="level4_market_soak:last",
                ledger_hashes=hashes,
            ),
        }
        passed = bool(chain.get("passed") and all(row.get("passed") for row in sources.values()))
        missing = [name for name, row in sources.items() if not row.get("passed")]
        source_failures = [name for name, row in sources.items() if row.get("state") == "FAILED"]
        if not chain.get("passed"):
            missing.insert(0, "valid build-bound evidence hash chain")
        failed = bool(chain.get("errors") or source_failures)
        return {
            "ok": passed,
            "version": SERVICE_VERSION,
            "contract_version": CONTRACT_VERSION,
            "build": BUILD_ID,
            "state": "PASS" if passed else "FAILED" if failed else "PENDING_EVIDENCE",
            "passed": passed,
            "chain": chain,
            "sources": sources,
            "source_failures": source_failures,
            "source_failure_count": len(source_failures),
            "missing_gates": missing,
            "authority": "OBSERVATIONAL_POSTGRESQL_KV_HASH_CHAIN",
            "production_change_allowed": False,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }
