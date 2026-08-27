"""Canonical point-in-time candidate population ledger.

The ledger is additive and observation-only.  It gives every selector arm the
same immutable candidates, timestamps and fingerprints without changing any
production decision field.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional

from core.production_mode_policy import is_production_mode, normalise_mode
from core.quant_edge_data_service import QuantEdgeDataService

POPULATION_VERSION = "candidate-population-2.0.0"
CANDIDATE_IDENTITY_VERSION = "candidate-identity-v2"
LEGACY_CANDIDATE_IDENTITY_VERSION = "candidate-identity-v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(material: str, length: int = 32) -> str:
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:length]


class CandidatePopulationService:
    """Persist one immutable candidate population for all selector arms."""

    def __init__(self, store: Any):
        self.store = store
        self.production_governance_required = bool(
            getattr(store, "production_model_governance_required", False)
        )
        self.governance_repository = getattr(
            store, "production_model_governance_repository", None
        )
        if self.production_governance_required:
            required = ("record_selector_population", "selector_population_members")
            if (
                self.governance_repository is None
                or getattr(self.governance_repository, "authority", None) is None
                or any(not callable(getattr(self.governance_repository, name, None)) for name in required)
            ):
                raise RuntimeError("PRODUCTION_CANDIDATE_POPULATION_REQUIRES_POSTGRES_GOVERNANCE_REPOSITORY")
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.RLock()
        if not self.production_governance_required:
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS candidate_populations (
                  population_fingerprint TEXT PRIMARY KEY,
                  mode TEXT NOT NULL,
                  observed_at TEXT NOT NULL,
                  universe_id TEXT NOT NULL,
                  dataset_fingerprint TEXT NOT NULL,
                  feature_manifest_hash TEXT NOT NULL,
                  candidate_count INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  policy_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidate_population_observations (
                  candidate_id TEXT PRIMARY KEY,
                  population_fingerprint TEXT NOT NULL,
                  symbol TEXT NOT NULL,
                  exchange TEXT NOT NULL,
                  instrument_key TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  side TEXT,
                  observed_at TEXT NOT NULL,
                  identity_verified INTEGER NOT NULL,
                  production_status TEXT,
                  production_decision TEXT,
                  feature_json TEXT NOT NULL,
                  feature_hash TEXT NOT NULL,
                  candidate_identity_version TEXT NOT NULL DEFAULT 'candidate-identity-v1',
                  cost_authority_id TEXT NOT NULL DEFAULT '',
                  policy_version TEXT NOT NULL,
                  FOREIGN KEY(population_fingerprint) REFERENCES candidate_populations(population_fingerprint)
                );
                CREATE INDEX IF NOT EXISTS ix_candidate_population_observations_population
                  ON candidate_population_observations(population_fingerprint, symbol);
                CREATE INDEX IF NOT EXISTS ix_candidate_population_observations_symbol
                  ON candidate_population_observations(symbol, mode, observed_at);
                """
            )
            columns = {
                str(row[1]) for row in self.store.conn.execute(
                    "PRAGMA table_info(candidate_population_observations)"
                ).fetchall()
            }
            if "candidate_identity_version" not in columns:
                self.store.conn.execute(
                    "ALTER TABLE candidate_population_observations "
                    "ADD COLUMN candidate_identity_version TEXT NOT NULL "
                    "DEFAULT 'candidate-identity-v1'"
                )
            if "cost_authority_id" not in columns:
                self.store.conn.execute(
                    "ALTER TABLE candidate_population_observations "
                    "ADD COLUMN cost_authority_id TEXT NOT NULL DEFAULT ''"
                )
            # Preserve every legacy identifier and its downstream references.
            # The migration attaches immutable content/cost authority metadata;
            # all new observations receive v2 identities, so changed content is
            # appended instead of rewriting an existing v1 row in place.
            legacy_rows = self.store.conn.execute(
                """SELECT candidate_id,mode,feature_json
                   FROM candidate_population_observations
                   WHERE COALESCE(cost_authority_id,'')=''"""
            ).fetchall()
            for legacy in legacy_rows:
                try:
                    features = json.loads(legacy[2] or "{}")
                    cost_authority_id = QuantEdgeDataService.cost_authority_identity(
                        str(legacy[1] or ""), features if isinstance(features, dict) else {}
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    cost_authority_id = "UNRESOLVED_LEGACY_COST_AUTHORITY"
                self.store.conn.execute(
                    """UPDATE candidate_population_observations
                       SET candidate_identity_version=?,cost_authority_id=?
                       WHERE candidate_id=?""",
                    (LEGACY_CANDIDATE_IDENTITY_VERSION, cost_authority_id, legacy[0]),
                )
            self.store.conn.commit()

    @staticmethod
    def _candidate_id(row: Mapping[str, Any], *, mode: str, observed_at: str,
                      universe_id: str, dataset_fingerprint: str,
                      feature_manifest_hash: str, feature_hash: str,
                      cost_authority_id: str) -> str:
        return _sha(_json({
            "identity_version": CANDIDATE_IDENTITY_VERSION,
            "instrument_key": row.get("instrument_key"),
            "symbol": row.get("symbol"),
            "exchange": row.get("exchange"),
            "mode": mode,
            "side": row.get("side"),
            "observed_at": observed_at,
            "universe_id": universe_id,
            "dataset_fingerprint": dataset_fingerprint,
            "feature_manifest_hash": feature_manifest_hash,
            "feature_hash": feature_hash,
            "cost_authority_id": cost_authority_id,
        }))

    @staticmethod
    def _population_fingerprint(*, mode: str, observed_at: str, universe_id: str,
                                dataset_fingerprint: str, feature_manifest_hash: str,
                                candidate_ids: Iterable[str]) -> str:
        material = _json({
            "mode": mode,
            "observed_at": observed_at,
            "universe_id": universe_id,
            "dataset_fingerprint": dataset_fingerprint,
            "feature_manifest_hash": feature_manifest_hash,
            "candidate_ids": sorted(candidate_ids),
            "policy_version": POPULATION_VERSION,
        })
        return _sha(material)

    def record_population(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        mode: str,
        observed_at: Optional[str],
        universe_id: str,
        dataset_fingerprint: str,
        feature_manifest_hash: str,
    ) -> Dict[str, Any]:
        desk = normalise_mode(mode)
        if not is_production_mode(desk):
            raise ValueError("mode must be intraday or delivery")
        stamp = str(observed_at or _now())
        universe = str(universe_id or "").strip()
        dataset = str(dataset_fingerprint or "").strip()
        manifest = str(feature_manifest_hash or "").strip()
        if not universe or not dataset or not manifest:
            raise ValueError("universe_id, dataset_fingerprint and feature_manifest_hash are required")

        prepared: List[Dict[str, Any]] = []
        rejected: List[Dict[str, str]] = []
        for raw in rows:
            item = dict(raw or {})
            item_mode = normalise_mode(item.get("mode"))
            symbol = str(item.get("symbol") or "").upper().strip()
            exchange = str(item.get("exchange") or "NSE").upper().strip()
            instrument_key = str(item.get("instrument_key") or "").strip()
            if item_mode != desk:
                rejected.append({"symbol": symbol, "reason": "mode_mismatch"})
                continue
            if not symbol or not instrument_key or item.get("identity_verified") is not True:
                rejected.append({"symbol": symbol, "reason": "identity_not_verified"})
                continue
            item["symbol"] = symbol
            item["exchange"] = exchange
            item["instrument_key"] = instrument_key
            item["mode"] = desk
            # A population has one capture boundary, but its members may have
            # been analysed seconds/minutes apart. Preserve that decision time
            # in both the candidate identity and the immutable observation.
            item_stamp = str(
                item.get("decision_ts")
                or item.get("decision_as_of")
                or item.get("observed_at")
                or stamp
            ).strip()
            item["observed_at"] = item_stamp
            feature_json = _json(item)
            feature_hash = _sha(feature_json, 64)
            cost_authority_id = QuantEdgeDataService.cost_authority_identity(desk, item)
            candidate_id = self._candidate_id(
                item, mode=desk, observed_at=item_stamp, universe_id=universe,
                dataset_fingerprint=dataset, feature_manifest_hash=manifest,
                feature_hash=feature_hash, cost_authority_id=cost_authority_id,
            )
            prepared.append({
                "candidate_id": candidate_id,
                "symbol": symbol,
                "exchange": exchange,
                "instrument_key": instrument_key,
                "mode": desk,
                "side": str(item.get("side") or "").upper(),
                "observed_at": item_stamp,
                "identity_verified": 1,
                "production_status": str(item.get("status") or ""),
                "production_decision": str(item.get("decision") or ""),
                "feature_json": feature_json,
                "feature_hash": feature_hash,
                "candidate_identity_version": CANDIDATE_IDENTITY_VERSION,
                "cost_authority_id": cost_authority_id,
                "feature_snapshot": item,
            })

        candidate_ids = [item["candidate_id"] for item in prepared]
        population = self._population_fingerprint(
            mode=desk, observed_at=stamp, universe_id=universe,
            dataset_fingerprint=dataset, feature_manifest_hash=manifest,
            candidate_ids=candidate_ids,
        )
        created = _now()
        quant_data = QuantEdgeDataService(self.store)
        if self.production_governance_required:
            for item in prepared:
                snapshot = quant_data.record_snapshot(
                    candidate_id=item["candidate_id"],
                    population_fingerprint=population,
                    symbol=item["symbol"],
                    instrument_key=item["instrument_key"],
                    mode=item["mode"],
                    side=item["side"],
                    decision_ts=item["observed_at"],
                    universe_id=universe,
                    dataset_fingerprint=dataset,
                    feature_manifest_hash=manifest,
                    feature_hash=item["feature_hash"],
                    features=item["feature_snapshot"],
                    candidate_identity_version=item["candidate_identity_version"],
                    cost_authority_id=item["cost_authority_id"],
                    _persist=False,
                )
                item["governance_feature_snapshot"] = {
                    key: value for key, value in snapshot.items() if key not in {"ok", "inserted"}
                }
            self.governance_repository.record_selector_population(
                {
                    "population_fingerprint": population, "mode": desk,
                    "observed_at": stamp, "universe_id": universe,
                    "dataset_fingerprint": dataset, "feature_manifest_hash": manifest,
                    "candidate_count": len(prepared), "policy_version": POPULATION_VERSION,
                },
                prepared,
            )
        else:
            with self.store.write_lock, self.store.conn:
                existing_population = self.store.conn.execute(
                    """SELECT mode,observed_at,universe_id,dataset_fingerprint,
                              feature_manifest_hash,candidate_count,policy_version
                       FROM candidate_populations WHERE population_fingerprint=?""",
                    (population,),
                ).fetchone()
                expected_population = (
                    desk, stamp, universe, dataset, manifest, len(prepared), POPULATION_VERSION,
                )
                if existing_population is None:
                    self.store.conn.execute(
                        """INSERT INTO candidate_populations(
                            population_fingerprint,mode,observed_at,universe_id,dataset_fingerprint,
                            feature_manifest_hash,candidate_count,created_at,policy_version
                        ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (population, *expected_population[:-1], created, expected_population[-1]),
                    )
                elif tuple(existing_population) != expected_population:
                    raise ValueError("population fingerprint collision with different immutable content")

                immutable_columns = (
                    "population_fingerprint", "symbol", "exchange", "instrument_key", "mode", "side",
                    "observed_at", "identity_verified", "production_status", "production_decision",
                    "feature_json", "feature_hash", "candidate_identity_version", "cost_authority_id",
                    "policy_version",
                )
                for item in prepared:
                    expected = (
                        population, item["symbol"], item["exchange"], item["instrument_key"],
                        item["mode"], item["side"], item["observed_at"], item["identity_verified"],
                        item["production_status"], item["production_decision"], item["feature_json"],
                        item["feature_hash"], item["candidate_identity_version"],
                        item["cost_authority_id"], POPULATION_VERSION,
                    )
                    existing = self.store.conn.execute(
                        f"SELECT {','.join(immutable_columns)} "
                        "FROM candidate_population_observations WHERE candidate_id=?",
                        (item["candidate_id"],),
                    ).fetchone()
                    if existing is None:
                        self.store.conn.execute(
                            f"""INSERT INTO candidate_population_observations(
                                candidate_id,{','.join(immutable_columns)}
                            ) VALUES({','.join('?' for _ in range(len(immutable_columns) + 1))})""",
                            (item["candidate_id"], *expected),
                        )
                    elif tuple(existing) != expected:
                        raise ValueError(
                            "candidate identity collision with different immutable content"
                        )
        # Snapshot the canonical stored rows. A repeated v2 identity is accepted
        # only after every immutable field has matched transactionally above.
        canonical_rows = self.rows(population)
        quant_snapshots = quant_data.record_population_snapshots(
            canonical_rows,
            population_fingerprint=population,
            universe_id=universe,
            dataset_fingerprint=dataset,
            feature_manifest_hash=manifest,
        )
        return {
            "ok": True,
            "version": POPULATION_VERSION,
            "population_fingerprint": population,
            "candidate_ids": candidate_ids,
            "recorded": len(prepared),
            "rejected": len(rejected),
            "rejected_details": rejected,
            "mode": desk,
            "observed_at": stamp,
            "universe_id": universe,
            "dataset_fingerprint": dataset,
            "feature_manifest_hash": manifest,
            "quant_snapshot_ledger": quant_snapshots,
        }

    def rows(self, population_fingerprint: str) -> List[Dict[str, Any]]:
        if self.production_governance_required:
            return self.governance_repository.selector_population_members(
                str(population_fingerprint)
            )
        raw_rows = self.store.conn.execute(
            """SELECT * FROM candidate_population_observations
               WHERE population_fingerprint=? ORDER BY symbol""",
            (str(population_fingerprint),),
        ).fetchall()
        result: List[Dict[str, Any]] = []
        for raw in raw_rows:
            row = dict(raw)
            try:
                features = json.loads(row.get("feature_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                features = {}
            item = dict(features if isinstance(features, dict) else {})
            item.update({
                "candidate_id": row.get("candidate_id"),
                "population_fingerprint": row.get("population_fingerprint"),
                "symbol": row.get("symbol"),
                "exchange": row.get("exchange"),
                "instrument_key": row.get("instrument_key"),
                "mode": row.get("mode"),
                "side": row.get("side"),
                "observed_at": row.get("observed_at"),
                # Downstream admission uses a strict boolean identity contract.
                # SQLite returns INTEGER for this column; leaking ``1`` here
                # makes ``identity_verified is True`` fail and silently blocks
                # every automatic paper candidate restored from the ledger.
                "identity_verified": bool(int(row.get("identity_verified") or 0)),
                "production_status": row.get("production_status"),
                "production_decision": row.get("production_decision"),
                "feature_snapshot": features,
                "feature_hash": row.get("feature_hash"),
                "candidate_identity_version": row.get("candidate_identity_version"),
                "cost_authority_id": row.get("cost_authority_id"),
            })
            result.append(item)
        return result
