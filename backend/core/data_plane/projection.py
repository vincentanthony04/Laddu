from __future__ import annotations

"""PostgreSQL outbox to immutable Parquet projection.

The operational transaction commits business truth and its outbox event in the
same PostgreSQL transaction. This worker claims committed events with SKIP
LOCKED, writes one deterministic immutable Parquet part, then marks those events
published. A projector failure cannot roll back or block trade/risk authority.
"""

from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, Mapping

from config import DATA_DIR
from .postgres import PostgresAuthority


class PostgresParquetProjectionService:
    SERVICE_VERSION = "postgres-outbox-parquet-projection-1.0.0"

    def __init__(
        self,
        operational: PostgresAuthority,
        *,
        data_dir: Path | None = None,
        worker_id: str = "project-laddu-projector",
        event_fn: Callable[..., Any] | None = None,
    ):
        self.operational = operational
        self.root = Path(data_dir or DATA_DIR) / "parquet" / "operational_events"
        self.worker_id = worker_id
        self.event_fn = event_fn or (lambda *args, **kwargs: None)
        self._lock = threading.RLock()
        self._status: dict[str, Any] = {
            "service_version": self.SERVICE_VERSION,
            "state": "starting",
            "source": "operational_postgres_transactional_outbox",
            "tick_authority": "questdb",
            "pending": None,
            "last_projection": None,
            "last_error": None,
            "parts_written": 0,
            "rows_written": 0,
        }

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    def _duckdb(self):
        try:
            return importlib.import_module("duckdb")
        except Exception as exc:
            raise RuntimeError("DUCKDB_UNAVAILABLE") from exc

    def _claim(self, limit: int) -> list[dict[str, Any]]:
        # ``integration.claim_outbox`` is invoked with SELECT syntax but is a
        # mutating lease-acquisition function (FOR UPDATE / claimed_at update).
        # Do not route it through PostgresAuthority.execute(), whose SELECT/WITH
        # classifier intentionally opens a read-only, retry-eligible transaction.
        # Claiming is writer semantics and must never be implicitly replayed.
        with self.operational.transaction(
            read_only=False,
            statement_timeout_ms=5000,
            pool_timeout_seconds=5.0,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM integration.claim_outbox(%s,%s)",
                    (self.worker_id, max(1, min(int(limit), 1000))),
                )
                rows = cur.fetchall()
        return [dict(row) for row in rows]

    def _write_part(self, rows: list[Mapping[str, Any]]) -> Path:
        first_id = int(rows[0]["outbox_id"])
        last_id = int(rows[-1]["outbox_id"])
        created = rows[0].get("created_at") or datetime.now(timezone.utc)
        if isinstance(created, str):
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        day = created.astimezone(timezone.utc).date().isoformat()
        digest = hashlib.sha256(self._canonical(rows).encode("utf-8")).hexdigest()[:20]
        target = self.root / f"event_date={day}" / f"outbox_{first_id:020d}_{last_id:020d}_{digest}.parquet"
        if target.exists():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="laddu_projection_") as temp_dir:
            temp = Path(temp_dir)
            jsonl = temp / "rows.jsonl"
            staging = temp / "part.parquet"
            with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
                for raw in rows:
                    row = dict(raw)
                    payload = row.get("payload")
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except Exception:
                            pass
                    row["payload"] = payload
                    handle.write(self._canonical(row) + "\n")
            duckdb = self._duckdb()
            conn = duckdb.connect(database=":memory:")
            try:
                source = str(jsonl.resolve()).replace("'", "''").replace("\\", "/")
                destination = str(staging.resolve()).replace("'", "''").replace("\\", "/")
                conn.execute(
                    f"COPY (SELECT * FROM read_json_auto('{source}', format='newline_delimited')) "
                    f"TO '{destination}' (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
            finally:
                conn.close()
            staging.replace(target)
        return target

    def _mark_published(self, rows: list[Mapping[str, Any]], part: Path) -> int:
        ids = [int(row["outbox_id"]) for row in rows]
        return int(
            self.operational.execute(
                """UPDATE integration.transactional_outbox
                      SET published_at=clock_timestamp(), last_error=NULL,
                          projection_part=%s, claimed_at=NULL, claimed_by=NULL
                    WHERE outbox_id = ANY(%s) AND published_at IS NULL""",
                (str(part), ids),
                statement_timeout_ms=5000,
            )
            or 0
        )

    def _release_claims(self, rows: list[Mapping[str, Any]], error: str) -> None:
        ids = [int(row["outbox_id"]) for row in rows if row.get("outbox_id") is not None]
        if not ids:
            return
        try:
            self.operational.execute(
                """UPDATE integration.transactional_outbox
                      SET claimed_at=NULL, claimed_by=NULL, last_error=%s
                    WHERE outbox_id = ANY(%s)
                      AND published_at IS NULL
                      AND claimed_by=%s""",
                (str(error)[:400], ids, self.worker_id),
                statement_timeout_ms=5000,
            )
        except Exception:
            # The lease expiry in integration.claim_outbox remains the final
            # recovery path when PostgreSQL itself is unavailable.
            pass

    def run_once(self, *, limit: int = 500) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        try:
            rows = self._claim(limit)
            if not rows:
                pending = self.operational.execute(
                    "SELECT count(*) AS count FROM integration.transactional_outbox WHERE published_at IS NULL",
                    fetch="one",
                )
                result = {"state": "idle", "claimed": 0, "published": 0, "pending": int((pending or {}).get("count") or 0)}
            else:
                part = self._write_part(rows)
                published = self._mark_published(rows, part)
                result = {
                    "state": "projected",
                    "claimed": len(rows),
                    "published": published,
                    "part": str(part),
                    "first_outbox_id": int(rows[0]["outbox_id"]),
                    "last_outbox_id": int(rows[-1]["outbox_id"]),
                }
            with self._lock:
                self._status["state"] = "ready"
                self._status["pending"] = result.get("pending")
                self._status["last_projection"] = {**result, "at": datetime.now(timezone.utc).isoformat()}
                self._status["last_error"] = None
                if result.get("published"):
                    self._status["parts_written"] += 1
                    self._status["rows_written"] += int(result["published"])
            return result
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:400]
            self._release_claims(rows, error)
            with self._lock:
                self._status["state"] = "degraded"
                self._status["last_error"] = error
            try:
                self.event_fn("WARN", "analytical_projection", "PostgreSQL outbox projection failed", {"error": error})
            except Exception:
                pass
            return {"state": "degraded", "error": error, "claimed": 0, "published": 0}

    def record_tick(self, row: Mapping[str, Any]) -> None:
        # Durable market observations are owned by QuestDB. Duplicating them
        # into the operational outbox would reintroduce the workload coupling
        # that v68 removes.
        return None

    def run(self, supervisor: Any | None = None, *, running_fn=lambda: True) -> None:
        while running_fn() and (supervisor is None or getattr(supervisor, "running", True)):
            if supervisor is not None:
                beat = getattr(supervisor, "beat", None)
                if callable(beat):
                    beat("analytical_projection")
            result = self.run_once(limit=500)
            if supervisor is not None:
                published = int((result or {}).get("published") or 0)
                pending = (result or {}).get("pending")
                supervisor.progress(
                    "analytical_projection", token=f"{(result or {}).get('state')}:{(result or {}).get('last_outbox_id')}:{published}:{pending}",
                    stage=str((result or {}).get("state") or "projection"), completed_units=published, total_units=None,
                    waiting_on="transactional outbox" if str((result or {}).get("state")) == "idle" else None,
                    expected_idle=str((result or {}).get("state")) == "idle",
                )
            time.sleep(2.0)

    def refresh_status_counts(self) -> dict[str, Any]:
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)
