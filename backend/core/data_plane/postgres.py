from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import random
import re
import threading
import time
from typing import Any, Iterator, Mapping, Sequence


_PYFORMAT_TOKEN = re.compile(r"%(?:%|s|b|t|\([^)]+\)[sbt])")


def validate_psycopg_pyformat(sql: str, params: Sequence[Any] | Mapping[str, Any] | None) -> None:
    """Fail before psycopg when parameterised SQL contains a raw percent sign."""

    if params is None or "%" not in sql:
        return
    index = 0
    while index < len(sql):
        if sql[index] != "%":
            index += 1
            continue
        match = _PYFORMAT_TOKEN.match(sql, index)
        if match is None:
            fragment = sql[max(0, index - 24): index + 24].replace("\n", " ")
            raise ValueError(f"INVALID_PSYCOPG_PYFORMAT_PERCENT near: {fragment!r}")
        index = match.end()


class PostgresUnavailable(RuntimeError):
    pass


class PostgresCommitOutcomeUnknown(PostgresUnavailable):
    """A write body completed but the COMMIT acknowledgement was lost.

    Callers must reconcile by durable business identity before retrying. The
    authority deliberately cannot decide whether PostgreSQL committed the work.
    """

    def __init__(self, role: str, generation_id: int, cause: BaseException):
        self.role = str(role)
        self.generation_id = int(generation_id)
        self.cause = cause
        super().__init__(
            f"{self.role.upper()}_COMMIT_OUTCOME_UNKNOWN:"
            f"generation={self.generation_id}:{type(cause).__name__}"
        )


@dataclass(frozen=True)
class PostgresProbe:
    ok: bool
    role: str
    latency_ms: float
    server_version: str | None
    database: str | None
    error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "role": self.role,
            "latency_ms": round(self.latency_ms, 3),
            "server_version": self.server_version,
            "database": self.database,
            "error": self.error,
        }


@dataclass
class _PoolGeneration:
    generation_id: int
    pool: Any
    created_at: float
    active_leases: int = 0
    retired_at: float | None = None
    closed_at: float | None = None

    @property
    def retired(self) -> bool:
        return self.retired_at is not None


class PostgresAuthority:
    """Stable logical authority backed by replaceable verified pool generations.

    Repositories keep one stable ``PostgresAuthority`` reference. Physical pools
    are disposable generations: one persistent supervisor detects current-
    generation connection loss, constructs a fresh candidate off-path, verifies
    checked SQL, and atomically swaps authority. Transactions stay pinned to the
    generation that admitted them and never migrate mid-transaction.

    Capacity errors never become liveness evidence. Recovery admission is bounded,
    writes are never retried implicitly, and an uncertain COMMIT is surfaced as a
    distinct fail-closed exception.
    """

    RECOVERY_VERSION = "postgres-authority-supervisor-4.0.0-verified-generation-swap"
    POOL_REPLACEMENT_POLICY = "VERIFIED_GENERATION_ATOMIC_SWAP"

    CLOSED = "CLOSED"
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"
    STOPPING = "STOPPING"

    POOL_CONNECT_TIMEOUT_SECONDS = 3
    MAX_POOL_WAITERS = 8
    STARTUP_WAIT_SECONDS = 10.0
    CANDIDATE_WAIT_SECONDS = 6.0
    VERIFY_TIMEOUT_SECONDS = 2.0
    HEARTBEAT_INTERVAL_SECONDS = 5.0
    HEARTBEAT_POOL_TIMEOUT_SECONDS = 0.5
    RECOVERY_ADMISSION_WAIT_SECONDS = 1.5
    MAX_RECOVERY_WAITERS = 8
    RECOVERY_BACKOFF_INITIAL_SECONDS = 0.25
    RECOVERY_BACKOFF_MAX_SECONDS = 5.0
    RECOVERY_BACKOFF_JITTER = 0.20
    RETIRED_GENERATION_GRACE_SECONDS = 5.0
    POOL_CLOSE_TIMEOUT_SECONDS = 5.0

    def __init__(self, dsn: str, *, role: str, min_size: int = 1, max_size: int = 8):
        self.dsn = str(dsn or "").strip()
        self.role = str(role or "postgres").strip()
        self.min_size = max(1, int(min_size))
        self.max_size = max(self.min_size, int(max_size))

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._pool: Any | None = None  # compatibility alias for current generation
        self._current: _PoolGeneration | None = None
        self._retired: dict[int, _PoolGeneration] = {}
        self._candidate_pool: Any | None = None
        self._candidate_generation_id: int | None = None
        self._candidate_reconnect_failed = False
        self._opening = False

        self._state = self.CLOSED
        self._state_reason = "not opened"
        self._supervisor_thread: threading.Thread | None = None
        self._supervisor_stop = threading.Event()
        self._recovery_ready = threading.Event()
        self._recovery_ready.set()

        self._pool_generation = 0
        self._recovery_epoch = 0
        self._recovery_attempts = 0
        self._recovery_failures = 0
        self._recovery_successes = 0
        self._consecutive_recovery_failures = 0
        self._pool_reconnect_failures = 0
        self._capacity_failures = 0
        self._connection_failures = 0
        self._stale_generation_failures = 0
        self._heartbeat_attempts = 0
        self._heartbeat_failures = 0
        self._heartbeat_capacity_skips = 0
        self._retired_pool_closes = 0
        self._retired_pool_forced_closes = 0
        self._retired_pool_close_failures = 0
        self._recovery_waiters = 0
        self._recovery_waiter_max = 0
        self._recovery_waiter_rejections = 0
        self._recovery_waiter_timeouts = 0
        self._commit_outcome_unknown = 0

        self._last_pool_error: str | None = None
        self._last_failure_classification: str | None = None
        self._last_failure_sqlstate: str | None = None
        self._last_failure_at: float | None = None
        self._last_success_at: float | None = None
        self._last_verified_at: float | None = None
        self._last_recovery_started_at: float | None = None
        self._last_recovery_completed_at: float | None = None
        self._last_heartbeat_at: float | None = None
        self._last_heartbeat_success_at: float | None = None

    def _load(self):
        try:
            import psycopg  # noqa: F401
            from psycopg_pool import ConnectionPool
            from psycopg.rows import dict_row
        except Exception as exc:  # pragma: no cover - installed dependency gate
            raise PostgresUnavailable("PSYCOPG_UNAVAILABLE") from exc
        return ConnectionPool, dict_row

    @staticmethod
    def _epoch_seconds(value: float | None) -> float | None:
        return round(float(value), 6) if value is not None else None

    @staticmethod
    def _exception_chain(exc: BaseException) -> list[BaseException]:
        chain: list[BaseException] = []
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            chain.append(current)
            seen.add(id(current))
            current = current.__cause__ or current.__context__
        return chain

    @classmethod
    def _classify_failure(cls, exc: BaseException) -> tuple[str, str | None]:
        """Separate liveness evidence from pressure and SQL/transaction errors."""

        chain = cls._exception_chain(exc)
        names = " ".join(type(item).__name__.lower() for item in chain)
        messages = " ".join(str(item).lower() for item in chain)
        sqlstate = next(
            (
                str(value)
                for item in chain
                for value in (getattr(item, "sqlstate", None), getattr(item, "pgcode", None))
                if value
            ),
            None,
        )

        if any(token in names for token in ("pooltimeout", "toomanyrequests")) or any(
            token in messages for token in (
                "max_waiting",
                "too many requests",
                "couldn't get a connection after",
                "could not get a connection after",
            )
        ):
            return "capacity", sqlstate
        if sqlstate == "53300":
            return "capacity", sqlstate
        if sqlstate == "57014" or "querycanceled" in names or "statement timeout" in messages:
            return "statement_timeout", sqlstate
        if sqlstate == "55P03" or "lock timeout" in messages:
            return "lock_timeout", sqlstate
        if sqlstate in {"40001", "40P01"}:
            return "transaction_retryable", sqlstate
        if sqlstate and (sqlstate.startswith("08") or sqlstate in {"57P01", "57P02", "57P03", "58030"}):
            return "connection", sqlstate
        if any(token in names for token in ("interfaceerror", "connectiontimeout")):
            return "connection", sqlstate
        if "operationalerror" in names and any(
            token in messages
            for token in (
                "connection",
                "server closed",
                "server terminated",
                "network",
                "socket",
                "broken pipe",
                "connection refused",
            )
        ):
            return "connection", sqlstate
        if any(
            token in messages
            for token in (
                "candidate_generation_reconnect_failed",
                "server closed the connection",
                "connection is closed",
                "connection not open",
                "connection refused",
                "could not connect to server",
                "terminating connection due to administrator command",
                "the connection is lost",
                "broken pipe",
            )
        ):
            return "connection", sqlstate
        return "other", sqlstate

    def _new_pool(self, generation_id: int) -> Any:
        pool_cls, dict_row = self._load()

        def reconnect_failed(pool: Any) -> None:
            self._on_reconnect_failed(generation_id, pool)

        return pool_cls(
            conninfo=self.dsn,
            min_size=self.min_size,
            max_size=self.max_size,
            timeout=5,
            max_waiting=self.MAX_POOL_WAITERS,
            kwargs={
                "autocommit": True,
                "application_name": f"project-laddu-{self.role}-g{generation_id}",
                "row_factory": dict_row,
                "connect_timeout": self.POOL_CONNECT_TIMEOUT_SECONDS,
            },
            max_idle=60,
            max_lifetime=900,
            reconnect_timeout=20,
            reconnect_failed=reconnect_failed,
            check=pool_cls.check_connection,
            open=True,
        )

    @staticmethod
    def _pool_stats_for(pool: Any | None) -> dict[str, Any]:
        if pool is None:
            return {}
        try:
            return dict(pool.get_stats() or {})
        except Exception:
            return {}

    def _verify_pool(self, pool: Any, *, timeout: float) -> None:
        with pool.connection(timeout=max(0.05, float(timeout))) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS project_laddu_database_usability_probe")
                cur.fetchone()

    def _close_pool_quietly(self, pool: Any | None, *, retired: bool = False, forced: bool = False) -> None:
        if pool is None:
            return
        try:
            pool.close(timeout=self.POOL_CLOSE_TIMEOUT_SECONDS)
        except Exception:
            if retired:
                with self._condition:
                    self._retired_pool_close_failures += 1
            return
        if retired:
            with self._condition:
                self._retired_pool_closes += 1
                if forced:
                    self._retired_pool_forced_closes += 1

    def _build_verified_generation(self, generation_id: int, *, wait_seconds: float) -> _PoolGeneration:
        pool = self._new_pool(generation_id)
        with self._condition:
            if self._supervisor_stop.is_set() or self._state in {self.CLOSED, self.STOPPING}:
                cancelled = True
            else:
                cancelled = False
                self._candidate_pool = pool
                self._candidate_generation_id = generation_id
                self._candidate_reconnect_failed = False
        if cancelled:
            self._close_pool_quietly(pool)
            raise PostgresUnavailable(f"{self.role.upper()}_CANDIDATE_BUILD_CANCELLED")
        try:
            pool.wait(timeout=max(0.1, float(wait_seconds)))
            self._verify_pool(pool, timeout=self.VERIFY_TIMEOUT_SECONDS)
            with self._condition:
                if self._candidate_reconnect_failed:
                    raise PostgresUnavailable(
                        f"{self.role.upper()}_CANDIDATE_GENERATION_RECONNECT_FAILED:"
                        f"generation={generation_id}"
                    )
            # Leave the candidate registered.  The caller must publish or reject
            # it while holding _condition, closing the callback-after-verify race.
            return _PoolGeneration(generation_id, pool, time.time())
        except Exception:
            with self._condition:
                if self._candidate_pool is pool:
                    self._candidate_pool = None
                    self._candidate_generation_id = None
                    self._candidate_reconnect_failed = False
            self._close_pool_quietly(pool)
            raise

    def _record_verified_success_locked(self, *, recovery: bool) -> None:
        now = time.time()
        self._last_success_at = now
        self._last_verified_at = now
        self._last_pool_error = None
        self._last_failure_classification = None
        self._last_failure_sqlstate = None
        self._consecutive_recovery_failures = 0
        self._state = self.HEALTHY
        self._state_reason = "current generation verified by checked SQL"
        self._recovery_ready.set()
        if recovery:
            self._recovery_successes += 1
            self._last_recovery_completed_at = now

    def _ensure_supervisor_locked(self) -> None:
        current = self._supervisor_thread
        if current is not None and current.is_alive():
            return
        self._supervisor_stop.clear()
        worker = threading.Thread(
            target=self._supervisor_loop,
            name=f"ProjectLaddu-PostgresAuthoritySupervisor-{self.role}",
            daemon=True,
        )
        self._supervisor_thread = worker
        worker.start()

    def _await_healthy_locked(self, deadline: float) -> None:
        if self._recovery_waiters >= self.MAX_RECOVERY_WAITERS:
            self._recovery_waiter_rejections += 1
            raise PostgresUnavailable(f"{self.role.upper()}_DATABASE_RECOVERY_ADMISSION_FULL")
        self._recovery_waiters += 1
        self._recovery_waiter_max = max(self._recovery_waiter_max, self._recovery_waiters)
        try:
            while True:
                if self._state == self.HEALTHY and self._current is not None:
                    return
                if self._state in {self.CLOSED, self.STOPPING}:
                    raise PostgresUnavailable(f"{self.role.upper()}_DATABASE_CLOSED")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._recovery_waiter_timeouts += 1
                    raise PostgresUnavailable(f"{self.role.upper()}_DATABASE_RECOVERING")
                self._condition.wait(remaining)
        finally:
            self._recovery_waiters -= 1

    def open(self, *, timeout_seconds: float | None = None) -> None:
        if not self.dsn:
            raise PostgresUnavailable(f"{self.role.upper()}_DSN_MISSING")
        timeout = self.STARTUP_WAIT_SECONDS if timeout_seconds is None else max(0.01, float(timeout_seconds))
        deadline = time.monotonic() + timeout

        with self._condition:
            if self._state == self.HEALTHY and self._current is not None:
                self._ensure_supervisor_locked()
                return
            if self._state == self.STOPPING:
                raise PostgresUnavailable(f"{self.role.upper()}_DATABASE_STOPPING")
            if self._current is not None or self._opening:
                self._await_healthy_locked(deadline)
                return
            self._opening = True
            self._state = self.STARTING
            self._state_reason = "constructing initial verified pool generation"
            self._recovery_ready.clear()
            self._supervisor_stop.clear()
            generation_id = self._pool_generation + 1

        try:
            generation = self._build_verified_generation(generation_id, wait_seconds=timeout)
        except Exception as exc:
            classification, sqlstate = self._classify_failure(exc)
            with self._condition:
                self._opening = False
                cancelled = self._supervisor_stop.is_set() or self._state in {self.CLOSED, self.STOPPING}
                if not cancelled:
                    self._state = self.DEGRADED
                    self._state_reason = "initial pool generation failed verification"
                    self._last_pool_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                    self._last_failure_classification = classification
                    self._last_failure_sqlstate = sqlstate
                    self._last_failure_at = time.time()
                self._recovery_ready.set()
                self._condition.notify_all()
            if cancelled:
                raise PostgresUnavailable(f"{self.role.upper()}_POOL_OPEN_CANCELLED") from exc
            raise PostgresUnavailable(
                f"{self.role.upper()}_DATABASE_UNAVAILABLE:{type(exc).__name__}"
            ) from exc

        cancel = False
        candidate_failed = False
        with self._condition:
            registered = (
                self._candidate_pool is generation.pool
                and self._candidate_generation_id == generation.generation_id
            )
            candidate_failed = registered and self._candidate_reconnect_failed
            if self._candidate_pool is generation.pool:
                self._candidate_pool = None
                self._candidate_generation_id = None
                self._candidate_reconnect_failed = False
            if (
                self._state == self.STOPPING
                or self._supervisor_stop.is_set()
                or not registered
                or candidate_failed
            ):
                cancel = True
                if candidate_failed and self._state not in {self.CLOSED, self.STOPPING}:
                    self._state = self.DEGRADED
                    self._state_reason = "initial candidate reconnect failed before publication"
                    self._last_pool_error = (
                        f"{self.role.upper()}_CANDIDATE_GENERATION_RECONNECT_FAILED:"
                        f"generation={generation.generation_id}"
                    )
                    self._last_failure_classification = "connection"
                    self._last_failure_at = time.time()
            else:
                self._current = generation
                self._pool = generation.pool
                self._pool_generation = generation.generation_id
                self._opening = False
                self._record_verified_success_locked(recovery=False)
                self._ensure_supervisor_locked()
            self._condition.notify_all()
        if cancel:
            self._close_pool_quietly(generation.pool)
            with self._condition:
                self._opening = False
                self._condition.notify_all()
            if candidate_failed:
                raise PostgresUnavailable(
                    f"{self.role.upper()}_CANDIDATE_GENERATION_RECONNECT_FAILED:"
                    f"generation={generation.generation_id}"
                )
            raise PostgresUnavailable(f"{self.role.upper()}_POOL_OPEN_CANCELLED")

    def _request_recovery_locked(
        self,
        exc: BaseException | str,
        *,
        reason: str,
        generation_id: int,
        classification: str,
        sqlstate: str | None,
    ) -> None:
        current = self._current
        if current is None or generation_id != current.generation_id:
            self._stale_generation_failures += 1
            return
        if classification != "connection":
            if classification == "capacity":
                self._capacity_failures += 1
            return

        error = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc)
        self._last_pool_error = error[:300]
        self._last_failure_classification = classification
        self._last_failure_sqlstate = sqlstate
        self._last_failure_at = time.time()
        self._connection_failures += 1
        if self._state == self.HEALTHY:
            self._recovery_epoch += 1
            self._last_recovery_started_at = time.time()
        self._state = self.DEGRADED
        self._state_reason = str(reason or "current generation connection failure")[:240]
        self._recovery_ready.clear()
        self._ensure_supervisor_locked()
        self._condition.notify_all()

    def _mark_database_failure(
        self,
        exc: BaseException,
        *,
        reason: str,
        generation_id: int,
    ) -> tuple[str, str | None]:
        classification, sqlstate = self._classify_failure(exc)
        with self._condition:
            self._request_recovery_locked(
                exc,
                reason=reason,
                generation_id=generation_id,
                classification=classification,
                sqlstate=sqlstate,
            )
        return classification, sqlstate

    def _on_reconnect_failed(self, generation_id: int, pool: Any) -> None:
        with self._condition:
            current = self._current
            if pool is self._candidate_pool and generation_id == self._candidate_generation_id:
                self._candidate_reconnect_failed = True
                self._pool_reconnect_failures += 1
                self._condition.notify_all()
                return
            if current is None or generation_id != current.generation_id or pool is not current.pool:
                self._stale_generation_failures += 1
                return
            self._pool_reconnect_failures += 1
            self._request_recovery_locked(
                f"reconnect_failed:{self.role}:generation={generation_id}",
                reason=f"psycopg reconnect timeout for current {self.role} generation",
                generation_id=generation_id,
                classification="connection",
                sqlstate=None,
            )

    def _retire_current_locked(self, generation: _PoolGeneration) -> tuple[Any | None, bool]:
        generation.retired_at = time.time()
        self._retired[generation.generation_id] = generation
        if generation.active_leases == 0:
            self._retired.pop(generation.generation_id, None)
            generation.closed_at = time.time()
            return generation.pool, False
        return None, False

    def _release_generation(self, generation: _PoolGeneration) -> None:
        close_pool = None
        with self._condition:
            generation.active_leases = max(0, generation.active_leases - 1)
            if generation.retired and generation.active_leases == 0:
                self._retired.pop(generation.generation_id, None)
                if generation.closed_at is None:
                    generation.closed_at = time.time()
                    close_pool = generation.pool
            self._condition.notify_all()
        if close_pool is not None:
            self._close_pool_quietly(close_pool, retired=True)

    def _reap_retired(self, *, force: bool = False) -> None:
        now = time.time()
        closing: list[tuple[Any, bool]] = []
        with self._condition:
            for generation_id, generation in list(self._retired.items()):
                age = now - float(generation.retired_at or now)
                forced = bool(generation.active_leases > 0)
                if force or generation.active_leases == 0 or age >= self.RETIRED_GENERATION_GRACE_SECONDS:
                    self._retired.pop(generation_id, None)
                    if generation.closed_at is None:
                        generation.closed_at = now
                        closing.append((generation.pool, forced))
        for pool, forced in closing:
            self._close_pool_quietly(pool, retired=True, forced=forced)

    def _heartbeat(self, generation: _PoolGeneration) -> None:
        with self._condition:
            if self._current is not generation or self._state != self.HEALTHY:
                return
            self._heartbeat_attempts += 1
            self._last_heartbeat_at = time.time()
        try:
            self._verify_pool(generation.pool, timeout=self.HEARTBEAT_POOL_TIMEOUT_SECONDS)
        except Exception as exc:
            classification, _ = self._classify_failure(exc)
            if classification == "capacity":
                with self._condition:
                    stats = self._pool_stats_for(generation.pool)
                    real_demand = bool(
                        generation.active_leases > 0
                        or int(stats.get("requests_waiting") or 0) > 0
                        or int(stats.get("requests_queued") or 0) > 0
                    )
                    if real_demand:
                        self._capacity_failures += 1
                        self._heartbeat_capacity_skips += 1
                        return
                    # A verified min-size>=1 generation with no borrowers and no
                    # available checked connection is not capacity pressure. It
                    # is idle liveness loss and must rotate without waiting for a
                    # foreground request or reconnect-timeout callback.
                    self._request_recovery_locked(
                        exc,
                        reason=f"idle heartbeat found no usable {self.role} connection",
                        generation_id=generation.generation_id,
                        classification="connection",
                        sqlstate=None,
                    )
                    self._heartbeat_failures += 1
                return
            with self._condition:
                self._heartbeat_failures += 1
            self._mark_database_failure(
                exc,
                reason=f"heartbeat lost current {self.role} generation",
                generation_id=generation.generation_id,
            )
            return
        with self._condition:
            if self._current is generation and self._state == self.HEALTHY:
                now = time.time()
                self._last_success_at = now
                self._last_verified_at = now
                self._last_heartbeat_success_at = now

    def _recovery_backoff(self, attempt: int) -> float:
        base = min(
            self.RECOVERY_BACKOFF_MAX_SECONDS,
            self.RECOVERY_BACKOFF_INITIAL_SECONDS * (2 ** min(max(0, attempt - 1), 6)),
        )
        return base * random.uniform(1.0 - self.RECOVERY_BACKOFF_JITTER, 1.0 + self.RECOVERY_BACKOFF_JITTER)

    def _recover_epoch(self, epoch: int) -> None:
        attempt = 0
        while not self._supervisor_stop.is_set():
            with self._condition:
                if epoch != self._recovery_epoch or self._state not in {self.DEGRADED, self.RECOVERING}:
                    return
                old = self._current
                if old is None:
                    self._state = self.DEGRADED
                    self._state_reason = "no current generation available for runtime recovery"
                    return
                self._state = self.RECOVERING
                self._state_reason = f"building verified replacement for generation {old.generation_id}"
                self._recovery_attempts += 1
                attempt += 1
                target_generation_id = self._pool_generation + 1
                self._condition.notify_all()

            try:
                candidate = self._build_verified_generation(
                    target_generation_id,
                    wait_seconds=self.CANDIDATE_WAIT_SECONDS,
                )
            except Exception as exc:
                classification, sqlstate = self._classify_failure(exc)
                with self._condition:
                    if epoch != self._recovery_epoch or self._supervisor_stop.is_set():
                        return
                    self._recovery_failures += 1
                    self._consecutive_recovery_failures += 1
                    self._last_pool_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                    self._last_failure_classification = classification
                    self._last_failure_sqlstate = sqlstate
                    self._last_failure_at = time.time()
                    self._state = self.RECOVERING
                    self._state_reason = "replacement candidate failed checked SQL verification"
                if self._supervisor_stop.wait(self._recovery_backoff(attempt)):
                    return
                continue

            close_old = None
            publish = False
            candidate_failed = False
            with self._condition:
                registered = (
                    self._candidate_pool is candidate.pool
                    and self._candidate_generation_id == candidate.generation_id
                )
                candidate_failed = registered and self._candidate_reconnect_failed
                if self._candidate_pool is candidate.pool:
                    self._candidate_pool = None
                    self._candidate_generation_id = None
                    self._candidate_reconnect_failed = False
                if (
                    not self._supervisor_stop.is_set()
                    and epoch == self._recovery_epoch
                    and self._current is old
                    and self._state in {self.DEGRADED, self.RECOVERING}
                    and registered
                    and not candidate_failed
                ):
                    self._current = candidate
                    self._pool = candidate.pool
                    self._pool_generation = candidate.generation_id
                    close_old, _ = self._retire_current_locked(old)
                    self._record_verified_success_locked(recovery=True)
                    self._condition.notify_all()
                    publish = True
            if not publish:
                self._close_pool_quietly(candidate.pool)
                if candidate_failed:
                    with self._condition:
                        if epoch != self._recovery_epoch or self._supervisor_stop.is_set():
                            return
                        self._recovery_failures += 1
                        self._consecutive_recovery_failures += 1
                        self._last_pool_error = (
                            f"{self.role.upper()}_CANDIDATE_GENERATION_RECONNECT_FAILED:"
                            f"generation={candidate.generation_id}"
                        )
                        self._last_failure_classification = "connection"
                        self._last_failure_sqlstate = None
                        self._last_failure_at = time.time()
                        self._state = self.RECOVERING
                        self._state_reason = "replacement candidate reconnect failed before publication"
                    if self._supervisor_stop.wait(self._recovery_backoff(attempt)):
                        return
                    continue
                return
            if close_old is not None:
                self._close_pool_quietly(close_old, retired=True)
            return

    def _supervisor_loop(self) -> None:
        next_heartbeat = time.monotonic() + self.HEARTBEAT_INTERVAL_SECONDS
        try:
            while not self._supervisor_stop.is_set():
                self._reap_retired()
                action = "wait"
                generation = None
                epoch = 0
                with self._condition:
                    if self._supervisor_stop.is_set():
                        break
                    if self._state in {self.DEGRADED, self.RECOVERING} and self._current is not None:
                        action = "recover"
                        epoch = self._recovery_epoch
                    elif self._state == self.HEALTHY and self._current is not None:
                        remaining = next_heartbeat - time.monotonic()
                        if remaining <= 0:
                            action = "heartbeat"
                            generation = self._current
                            next_heartbeat = time.monotonic() + self.HEARTBEAT_INTERVAL_SECONDS
                        else:
                            self._condition.wait(min(remaining, 0.5))
                    else:
                        self._condition.wait(0.5)
                if action == "recover":
                    self._recover_epoch(epoch)
                    next_heartbeat = time.monotonic() + self.HEARTBEAT_INTERVAL_SECONDS
                elif action == "heartbeat" and generation is not None:
                    self._heartbeat(generation)
        finally:
            self._reap_retired(force=True)
            with self._condition:
                if self._supervisor_thread is threading.current_thread():
                    self._supervisor_thread = None
                self._condition.notify_all()

    def _acquire_generation(self, *, timeout_seconds: float) -> _PoolGeneration:
        self.open(timeout_seconds=min(timeout_seconds, self.RECOVERY_ADMISSION_WAIT_SECONDS))
        with self._condition:
            current = self._current
            if self._state != self.HEALTHY or current is None or current.retired:
                raise PostgresUnavailable(f"{self.role.upper()}_DATABASE_RECOVERING")
            current.active_leases += 1
            return current

    def _wait_for_new_generation(self, failed_generation: int, *, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.01, float(timeout_seconds))
        with self._condition:
            if self._recovery_waiters >= self.MAX_RECOVERY_WAITERS:
                self._recovery_waiter_rejections += 1
                return False
            self._recovery_waiters += 1
            self._recovery_waiter_max = max(self._recovery_waiter_max, self._recovery_waiters)
            try:
                while True:
                    if (
                        self._state == self.HEALTHY
                        and self._current is not None
                        and self._current.generation_id > failed_generation
                    ):
                        return True
                    if self._state in {self.CLOSED, self.STOPPING}:
                        return False
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._recovery_waiter_timeouts += 1
                        return False
                    self._condition.wait(remaining)
            finally:
                self._recovery_waiters -= 1

    def close(self) -> None:
        self._supervisor_stop.set()
        with self._condition:
            if self._state == self.CLOSED and self._current is None and not self._opening:
                return
            self._state = self.STOPPING
            self._state_reason = "application close"
            self._recovery_epoch += 1  # invalidate any in-flight recovery publish
            self._recovery_ready.set()
            self._condition.notify_all()
            supervisor = self._supervisor_thread
            candidate = self._candidate_pool
            current = self._current
            retired = list(self._retired.values())
            self._candidate_pool = None
            self._candidate_generation_id = None
            self._candidate_reconnect_failed = False
            self._current = None
            self._pool = None
            self._retired.clear()

        pools: list[Any] = []
        for pool in [candidate, current.pool if current is not None else None, *[item.pool for item in retired]]:
            if pool is not None and all(pool is not known for known in pools):
                pools.append(pool)
        for pool in pools:
            self._close_pool_quietly(pool)

        if supervisor is not None and supervisor is not threading.current_thread():
            supervisor.join(timeout=self.POOL_CONNECT_TIMEOUT_SECONDS + self.CANDIDATE_WAIT_SECONDS + 1.0)
        with self._condition:
            self._opening = False
            self._state = self.CLOSED
            self._state_reason = "application close"
            self._condition.notify_all()

    def pool_health(self) -> dict[str, Any]:
        """Return non-invasive telemetry; never perform database I/O here."""

        with self._condition:
            current = self._current
            pool = current.pool if current is not None else None
            supervisor_active = bool(self._supervisor_thread is not None and self._supervisor_thread.is_alive())
            retired_leases = sum(item.active_leases for item in self._retired.values())
            recovery = {
                "version": self.RECOVERY_VERSION,
                "state": self._state,
                "reason": self._state_reason or None,
                "pool_generation": self._pool_generation,
                "recovery_epoch": self._recovery_epoch,
                "attempts": self._recovery_attempts,
                "failures": self._recovery_failures,
                "successes": self._recovery_successes,
                "consecutive_failures": self._consecutive_recovery_failures,
                "worker_active": bool(supervisor_active and self._state in {self.DEGRADED, self.RECOVERING}),
                "supervisor_active": supervisor_active,
                "current_generation_leases": current.active_leases if current is not None else 0,
                "retired_generations": len(self._retired),
                "retired_generation_leases": retired_leases,
                "stale_generation_failures": self._stale_generation_failures,
                "capacity_failures": self._capacity_failures,
                "connection_failures": self._connection_failures,
                "heartbeat_attempts": self._heartbeat_attempts,
                "heartbeat_failures": self._heartbeat_failures,
                "heartbeat_capacity_skips": self._heartbeat_capacity_skips,
                "retired_pool_closes": self._retired_pool_closes,
                "retired_pool_forced_closes": self._retired_pool_forced_closes,
                "retired_pool_close_failures": self._retired_pool_close_failures,
                "admission_waiters": self._recovery_waiters,
                "admission_waiter_max": self._recovery_waiter_max,
                "admission_rejections": self._recovery_waiter_rejections,
                "admission_timeouts": self._recovery_waiter_timeouts,
                "commit_outcome_unknown": self._commit_outcome_unknown,
                "last_failure_classification": self._last_failure_classification,
                "last_failure_sqlstate": self._last_failure_sqlstate,
                "last_started_at_epoch": self._epoch_seconds(self._last_recovery_started_at),
                "last_completed_at_epoch": self._epoch_seconds(self._last_recovery_completed_at),
                "last_failure_at_epoch": self._epoch_seconds(self._last_failure_at),
                "last_success_at_epoch": self._epoch_seconds(self._last_success_at),
                "last_verified_at_epoch": self._epoch_seconds(self._last_verified_at),
                "last_heartbeat_at_epoch": self._epoch_seconds(self._last_heartbeat_at),
                "last_heartbeat_success_at_epoch": self._epoch_seconds(self._last_heartbeat_success_at),
                "pool_replacement_policy": self.POOL_REPLACEMENT_POLICY,
                "pool_connect_timeout_seconds": self.POOL_CONNECT_TIMEOUT_SECONDS,
                "pool_max_waiting": self.MAX_POOL_WAITERS,
                "recovery_design": "FRESH_POOL_VERIFICATION_THEN_ATOMIC_GENERATION_SWAP",
                # Compatibility telemetry retained for operator clients that read
                # C27 fields; a generation swap replaces the old drain contract.
                "pool_refresh_epoch": self._recovery_epoch,
                "pool_drain_count": self._retired_pool_closes,
                "pool_drain_failures": self._retired_pool_close_failures,
                "server_reachable": self._state == self.HEALTHY and current is not None,
            }
            state = self._state
            last_error = self._last_pool_error
            reconnect_failures = self._pool_reconnect_failures
            usable = bool(state == self.HEALTHY and current is not None and not current.retired)
        return {
            "role": self.role,
            "state": state,
            "open": pool is not None,
            "usable": usable,
            "reconnect_failures": reconnect_failures,
            "last_pool_error": last_error,
            "recovery": recovery,
            "stats": self._pool_stats_for(pool),
        }

    @contextmanager
    def transaction(
        self,
        *,
        lock_timeout_ms: int = 750,
        statement_timeout_ms: int = 2500,
        idle_timeout_ms: int = 15000,
        isolation_level: str = "read committed",
        pool_timeout_seconds: float = 5.0,
        read_only: bool = False,
    ) -> Iterator[Any]:
        isolation = str(isolation_level or "read committed").strip().lower()
        allowed = {"read committed", "repeatable read", "serializable"}
        if isolation not in allowed:
            raise ValueError(f"unsupported PostgreSQL isolation level: {isolation_level!r}")

        generation = self._acquire_generation(timeout_seconds=max(0.01, float(pool_timeout_seconds)))
        body_completed = False
        try:
            try:
                with generation.pool.connection(timeout=max(0.01, float(pool_timeout_seconds))) as conn:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation.upper()}")
                            if read_only:
                                cur.execute("SET TRANSACTION READ ONLY")
                            cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{max(1, lock_timeout_ms)}ms",))
                            cur.execute("SELECT set_config('statement_timeout', %s, true)", (f"{max(1, statement_timeout_ms)}ms",))
                            cur.execute("SELECT set_config('idle_in_transaction_session_timeout', %s, true)", (f"{max(1, idle_timeout_ms)}ms",))
                        yield conn
                        body_completed = True
                with self._condition:
                    if self._current is generation:
                        self._last_success_at = time.time()
            except Exception as exc:
                classification, _ = self._mark_database_failure(
                    exc,
                    reason=f"database failure during {self.role} transaction generation {generation.generation_id}",
                    generation_id=generation.generation_id,
                )
                try:
                    setattr(exc, "project_laddu_generation_id", generation.generation_id)
                    setattr(exc, "project_laddu_failure_classification", classification)
                except Exception:
                    pass
                if body_completed and classification == "connection" and not read_only:
                    with self._condition:
                        self._commit_outcome_unknown += 1
                    raise PostgresCommitOutcomeUnknown(self.role, generation.generation_id, exc) from exc
                raise
        finally:
            self._release_generation(generation)

    @staticmethod
    def _read_retry_allowed(sql: str, fetch: str) -> bool:
        statement = str(sql or "").lstrip().upper()
        return fetch in {"one", "all"} and statement.startswith(("SELECT", "WITH"))

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
        *,
        fetch: str = "none",
        statement_timeout_ms: int = 2500,
        pool_timeout_seconds: float = 5.0,
    ) -> Any:
        validate_psycopg_pyformat(sql, params)
        read_only = self._read_retry_allowed(sql, fetch)
        attempts = 2 if read_only else 1
        for attempt in range(attempts):
            with self._condition:
                attempted_generation = self._pool_generation
            try:
                with self.transaction(
                    statement_timeout_ms=statement_timeout_ms,
                    pool_timeout_seconds=pool_timeout_seconds,
                    read_only=read_only,
                ) as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql, params)
                        if fetch == "one":
                            return cur.fetchone()
                        if fetch == "all":
                            return cur.fetchall()
                        return cur.rowcount
            except PostgresCommitOutcomeUnknown:
                raise
            except Exception as exc:
                classification, _ = self._classify_failure(exc)
                if attempt + 1 >= attempts or classification != "connection":
                    raise
                failed_generation = int(
                    getattr(exc, "project_laddu_generation_id", attempted_generation)
                    or attempted_generation
                )
                if not self._wait_for_new_generation(
                    failed_generation,
                    timeout_seconds=min(2.0, max(0.1, float(pool_timeout_seconds))),
                ):
                    raise
        raise RuntimeError("unreachable PostgreSQL execute state")

    def probe(
        self,
        *,
        required_schemas: Sequence[str] = (),
        required_relations: Sequence[str] = (),
    ) -> PostgresProbe:
        start = time.perf_counter()
        try:
            with self.transaction(statement_timeout_ms=5000, pool_timeout_seconds=5.0) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT current_database() AS database_name, "
                        "current_setting('server_version') AS server_version"
                    )
                    row = cur.fetchone()
                    database, version = row["database_name"], row["server_version"]
                    for schema in required_schemas:
                        cur.execute("SELECT to_regnamespace(%s)", (schema,))
                        if cur.fetchone()["to_regnamespace"] is None:
                            raise RuntimeError(f"REQUIRED_SCHEMA_MISSING:{schema}")
                    for relation in required_relations:
                        cur.execute("SELECT to_regclass(%s)", (relation,))
                        if cur.fetchone()["to_regclass"] is None:
                            raise RuntimeError(f"REQUIRED_RELATION_MISSING:{relation}")
            return PostgresProbe(
                True,
                self.role,
                (time.perf_counter() - start) * 1000,
                str(version),
                str(database),
                None,
            )
        except Exception as exc:
            return PostgresProbe(
                False,
                self.role,
                (time.perf_counter() - start) * 1000,
                None,
                None,
                f"{type(exc).__name__}: {exc}"[:300],
            )
