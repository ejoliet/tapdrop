"""In-process UWS 1.1 job manager.

No Celery, no Redis: RDD.md's "Recommended stack" table is explicit that async
jobs run in-process with job state kept in memory and results written to
``settings.result_store``. A bounded thread pool (``settings.max_jobs``) runs
queued jobs; jobs submitted past the limit stay ``QUEUED`` in the pool's own
work queue rather than being rejected, which is what gives the RDD's "over the
limit, jobs stay QUEUED" behaviour for free.

AIDEV-NOTE: every job runs its query against the one shared DuckDB connection
built by ``engine.create_connection`` (see that module's docstring: "DuckDB
serialises concurrent use of a single connection itself"). ``max_jobs`` bounds
how many jobs may be *in flight* (queued or executing) at once; it does not
give them independent DuckDB connections, so true parallel execution is
whatever DuckDB's own connection-level serialisation allows. That is an
existing property of ``engine.py``, not something this module changes.
"""

from __future__ import annotations

import contextlib
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from tapdrop.adql import translate as translate_adql
from tapdrop.engine import QueryResult, run_with_timeout
from tapdrop.errors import InvalidParameterError, QueryTimeoutError, TapdropError
from tapdrop.output import serialize
from tapdrop.querylog import QueryLog, QueryRecord, token_hash

if TYPE_CHECKING:
    import duckdb

    from tapdrop.api.params import TapRequest
    from tapdrop.config import Settings
    from tapdrop.registry import Registry

# DEVIATION: RDD.md is silent on a default job destruction interval. 24 h
# mirrors the default TTL a shared service gets in config.py's
# `resolve_down_at`; a client that cares can set its own via job creation.
_DEFAULT_DESTRUCTION_HOURS = 24


class Phase(StrEnum):
    """UWS 1.1 sec 4.1 execution phases."""

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    ABORTED = "ABORTED"
    HELD = "HELD"
    SUSPENDED = "SUSPENDED"
    UNKNOWN = "UNKNOWN"


#: Legal next phases per current phase (UWS 1.1 sec 4.1 state diagram, trimmed
#: to the states tapdrop actually produces). Every terminal phase maps to an
#: empty set, so any further transition attempt is rejected.
_LEGAL_TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    Phase.PENDING: frozenset({Phase.QUEUED, Phase.HELD, Phase.ABORTED}),
    Phase.HELD: frozenset({Phase.QUEUED, Phase.ABORTED}),
    Phase.QUEUED: frozenset({Phase.EXECUTING, Phase.SUSPENDED, Phase.ABORTED}),
    Phase.SUSPENDED: frozenset({Phase.QUEUED, Phase.ABORTED}),
    Phase.EXECUTING: frozenset({Phase.COMPLETED, Phase.ERROR, Phase.SUSPENDED, Phase.ABORTED}),
    Phase.COMPLETED: frozenset(),
    Phase.ERROR: frozenset(),
    Phase.ABORTED: frozenset(),
    Phase.UNKNOWN: frozenset(),
}


@dataclass
class Job:
    """One UWS job. Owner-less: tapdrop has no authentication beyond the token."""

    job_id: str
    request: TapRequest
    creation_time: datetime
    execution_duration: float
    destruction: datetime
    phase: Phase = Phase.PENDING
    start_time: datetime | None = None
    end_time: datetime | None = None
    quote: datetime | None = None
    error_message: str | None = None
    result_path: Path | None = None
    result_size: int | None = None
    result_rows: int | None = None
    cancel_requested: bool = False

    def transition(self, new_phase: Phase) -> None:
        """Move to *new_phase*, raising if the UWS state diagram forbids it."""
        if new_phase not in _LEGAL_TRANSITIONS[self.phase]:
            raise InvalidParameterError(
                "PHASE", f"cannot go from {self.phase.value} to {new_phase.value}"
            )
        self.phase = new_phase


#: Injection seam for tests: a callable that runs *job*'s query and returns a
#: result, given the manager it belongs to. The default talks to DuckDB
#: through the engine; tests substitute one that blocks/aborts on demand so
#: phase-transition tests never need to poll a wall clock.
RunQuery = Callable[["JobManager", Job], QueryResult]


def _run_query_via_engine(manager: JobManager, job: Job) -> QueryResult:
    # Local import: avoids a cycle (api.tap imports uws to mount the router)
    # and reuses the sync endpoint's exact MAXREC-wrapping helper, per the
    # task's instruction not to reinvent it.
    from tapdrop.api.tap import _with_maxrec

    translation = translate_adql(job.request.query, manager.registry)
    sql = _with_maxrec(translation.sql, job.request.effective_maxrec(manager.settings))
    timeout = min(job.execution_duration, float(manager.settings.async_query_timeout))
    return run_with_timeout(manager.con, sql, timeout)


class _AbortedError(Exception):
    """Internal signal: the query finished, but the job was cancelled meanwhile."""


class JobManager:
    """Owns every UWS job for one server process."""

    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        con: duckdb.DuckDBPyConnection,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        run_query: RunQuery = _run_query_via_engine,
        query_log: QueryLog | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.con = con
        self.query_log = query_log or QueryLog(con, None)  # disabled unless one is passed in
        self._clock = clock
        self._run_query = run_query
        self._jobs: dict[str, Job] = {}
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, settings.max_jobs), thread_name_prefix="tapdrop-uws"
        )

    def create_job(self, tap_request: TapRequest) -> Job:
        now = self._clock()
        job = Job(
            job_id=secrets.token_urlsafe(16),
            request=tap_request,
            creation_time=now,
            execution_duration=float(self.settings.async_query_timeout),
            destruction=now + timedelta(hours=_DEFAULT_DESTRUCTION_HOURS),
        )
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get_job(self, job_id: str) -> Job:
        self.purge_expired()
        with self._lock:
            return self._jobs[job_id]

    def list_jobs(self) -> list[Job]:
        self.purge_expired()
        with self._lock:
            return list(self._jobs.values())

    def run_job(self, job_id: str) -> Job:
        """Handle ``PHASE=RUN``: queue the job for the thread pool.

        UWS 1.1 sec 2.1.3: requesting RUN on a job that is already QUEUED or
        EXECUTING has no effect; requesting it on a terminal phase is illegal
        and raises via ``Job.transition``.
        """
        self.purge_expired()
        with self._lock:
            job = self._jobs[job_id]
            if job.phase in (Phase.QUEUED, Phase.EXECUTING):
                return job
            job.transition(Phase.QUEUED)
        future = self._executor.submit(self._execute, job_id)
        with self._lock:
            self._futures[job_id] = future
        return job

    def abort_job(self, job_id: str) -> Job:
        """Handle ``PHASE=ABORT``."""
        self.purge_expired()
        with self._lock:
            job = self._jobs[job_id]
            if job.phase is Phase.PENDING:
                job.transition(Phase.ABORTED)
                job.end_time = self._clock()
                return job
            if job.phase in (Phase.COMPLETED, Phase.ERROR, Phase.ABORTED):
                raise InvalidParameterError("PHASE", f"job is already {job.phase.value}")
            job.cancel_requested = True
            future = self._futures.get(job_id)

        if future is not None and future.cancel():
            # Removed from the pool's work queue before a worker touched it.
            with self._lock:
                job.transition(Phase.ABORTED)
                job.end_time = self._clock()
            return job

        # Already EXECUTING, or a worker is about to pick it up: interrupt the
        # shared connection. `_execute` notices `cancel_requested` either from
        # the resulting `QueryTimeoutError` or, if the interrupt lands before
        # the query starts, after it finishes anyway, and completes the
        # ABORTED transition itself.
        self.con.interrupt()
        return job

    def destroy_job(self, job_id: str) -> None:
        """Remove a job and its result file. Raises ``KeyError`` if unknown."""
        with self._lock:
            job = self._jobs.pop(job_id)
            future = self._futures.pop(job_id, None)
        if future is not None:
            future.cancel()
        if job.result_path is not None:
            job.result_path.unlink(missing_ok=True)

    def purge_expired(self) -> None:
        """Destroy every job whose ``destruction`` time has passed.

        Checked lazily on every read/write entry point instead of a background
        timer, so tests control expiry deterministically through the injected
        ``clock`` rather than sleeping.
        """
        now = self._clock()
        with self._lock:
            expired = [job_id for job_id, job in self._jobs.items() if job.destruction <= now]
        for job_id in expired:
            # defensive against a racing purge
            with contextlib.suppress(KeyError):
                self.destroy_job(job_id)

    def _execute(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if job.cancel_requested:
                job.transition(Phase.ABORTED)
                job.end_time = self._clock()
                return
            job.transition(Phase.EXECUTING)
            job.start_time = self._clock()

        try:
            self._run_and_finish(job)
        finally:
            # Logged here rather than in each terminal branch: every one of them
            # leaves the job in its final phase, which is what the log records.
            self._log(job)

    def _log(self, job: Job) -> None:
        if not self.query_log.enabled:
            return
        started = job.start_time or job.creation_time
        ended = job.end_time or self._clock()
        self.query_log.record(
            QueryRecord(
                endpoint="async",
                query=job.request.query,
                response_format=job.request.fmt,
                maxrec=job.request.effective_maxrec(self.settings),
                rows=job.result_rows or 0,
                elapsed_seconds=(ended - started).total_seconds(),
                status="ok" if job.phase is Phase.COMPLETED else "error",
                error=job.error_message or (None if job.phase is Phase.COMPLETED else job.phase),
                token_hash=token_hash(self.settings.token),
                started_at=started,
            )
        )

    def _run_and_finish(self, job: Job) -> None:
        try:
            result = self._run_query(self, job)
            if job.cancel_requested:
                raise _AbortedError
            maxrec = job.request.effective_maxrec(self.settings)
            # DALI 1.1 §4.4.1: MAXREC=0 always reports OVERFLOW (see api/tap.py).
            overflow = maxrec == 0 or result.table.num_rows > maxrec
            table = result.table.slice(0, maxrec) if overflow else result.table
            from tapdrop.api.tap import _column_meta  # local import: avoid a cycle

            body = serialize(table, job.request.fmt, _column_meta(self.registry), overflow=overflow)
            self._write_result(job, body)
        except _AbortedError:
            with self._lock:
                job.transition(Phase.ABORTED)
                job.end_time = self._clock()
            return
        except QueryTimeoutError as exc:
            with self._lock:
                if job.cancel_requested:
                    job.transition(Phase.ABORTED)
                else:
                    job.error_message = exc.message
                    job.transition(Phase.ERROR)
                job.end_time = self._clock()
            return
        except TapdropError as exc:
            with self._lock:
                job.error_message = exc.message
                job.transition(Phase.ERROR)
                job.end_time = self._clock()
            return
        except Exception as exc:
            with self._lock:
                job.error_message = str(exc)
                job.transition(Phase.ERROR)
                job.end_time = self._clock()
            return

        with self._lock:
            job.result_size = len(body)
            job.result_rows = table.num_rows
            job.end_time = self._clock()
            job.transition(Phase.COMPLETED)

    def _write_result(self, job: Job, body: bytes) -> None:
        store_path = self.settings.result_store_path
        if store_path is None:
            # ponytail: S3 result store not implemented. Add an s3fs write
            # here (mirroring `discovery`'s S3 read path) when a scale-to-zero
            # deployment actually needs it; local disk covers every milestone
            # test today.
            raise NotImplementedError(
                "S3 result store (TAPDROP_RESULT_STORE=s3://...) is not "
                "implemented for async jobs yet."
            )
        store_path.mkdir(parents=True, exist_ok=True)
        result_path = store_path / f"{job.job_id}.result"
        result_path.write_bytes(body)
        job.result_path = result_path
