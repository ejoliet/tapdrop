"""Unit tests for ``tapdrop.uws.JobManager``: the UWS 1.1 state machine and
runner, exercised directly (no HTTP layer -- see ``test_tap_async.py`` for
that).

The default ``run_query`` talks to a real DuckDB connection over the
``gaia.parquet`` fixture, which is fast enough to poll to completion in a
bounded loop. Tests that need to observe an in-flight job (QUEUED while
saturated, an EXECUTING job being aborted) inject a fake ``run_query`` gated
by a ``threading.Event`` instead of sleeping and hoping -- see RDD.md's
instruction to make the runner injectable rather than poll a wall clock.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from tapdrop.api.params import TapRequest, parse_tap_request
from tapdrop.config import Settings
from tapdrop.discovery import discover
from tapdrop.engine import QueryResult, create_connection
from tapdrop.errors import InvalidParameterError, QueryTimeoutError
from tapdrop.uws import Job, JobManager, Phase

DATA_DIR = Path(__file__).parent / "data"


def _tap_request(query: str, **extra: str) -> TapRequest:
    params = {"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": query, **extra}
    return parse_tap_request(params)


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[JobManager]:
    settings = Settings(
        sources=[str(DATA_DIR / "gaia.parquet")], result_store=str(tmp_path), max_jobs=2
    )
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    try:
        yield JobManager(settings, registry, con)
    finally:
        con.close()


def _wait_for_terminal(manager: JobManager, job_id: str, timeout: float = 2.0) -> Job:
    deadline = time.monotonic() + timeout
    job = manager.get_job(job_id)
    while job.phase not in (Phase.COMPLETED, Phase.ERROR, Phase.ABORTED):
        if time.monotonic() > deadline:
            raise AssertionError(f"job stuck in {job.phase}")
        time.sleep(0.02)
        job = manager.get_job(job_id)
    return job


# --- phase transitions -------------------------------------------------


def _new_job(now: datetime | None = None) -> Job:
    now = now or datetime.now(UTC)
    return Job(
        job_id="j",
        request=_tap_request("SELECT ra FROM data.gaia"),
        creation_time=now,
        execution_duration=60.0,
        destruction=now + timedelta(hours=1),
    )


def test_legal_transition_sequence() -> None:
    job = _new_job()
    job.transition(Phase.QUEUED)
    job.transition(Phase.EXECUTING)
    job.transition(Phase.COMPLETED)
    assert job.phase is Phase.COMPLETED


def test_illegal_transition_pending_to_executing_is_rejected() -> None:
    job = _new_job()
    with pytest.raises(InvalidParameterError):
        job.transition(Phase.EXECUTING)


def test_illegal_transition_from_a_terminal_phase_is_rejected() -> None:
    job = _new_job()
    job.transition(Phase.QUEUED)
    job.transition(Phase.EXECUTING)
    job.transition(Phase.COMPLETED)
    with pytest.raises(InvalidParameterError):
        job.transition(Phase.ABORTED)


def test_abort_from_pending_is_legal() -> None:
    job = _new_job()
    job.transition(Phase.ABORTED)
    assert job.phase is Phase.ABORTED


# --- JobManager: happy path and errors ----------------------------------


def test_run_job_completes(manager: JobManager) -> None:
    job = manager.create_job(_tap_request("SELECT ra FROM data.gaia"))
    assert job.phase is Phase.PENDING

    manager.run_job(job.job_id)
    finished = _wait_for_terminal(manager, job.job_id)

    assert finished.phase is Phase.COMPLETED
    assert finished.result_path is not None
    assert finished.result_path.exists()
    assert finished.result_size == len(finished.result_path.read_bytes())


def test_run_job_on_unknown_table_lands_in_error(manager: JobManager) -> None:
    job = manager.create_job(_tap_request("SELECT ra FROM data.nope"))

    manager.run_job(job.job_id)
    finished = _wait_for_terminal(manager, job.job_id)

    assert finished.phase is Phase.ERROR
    assert finished.error_message is not None
    assert "nope" in finished.error_message


def test_maxrec_and_format_are_honoured(manager: JobManager) -> None:
    from tapdrop.output import CSV

    job = manager.create_job(_tap_request("SELECT ra FROM data.gaia", MAXREC="1", FORMAT=CSV))

    manager.run_job(job.job_id)
    finished = _wait_for_terminal(manager, job.job_id)

    assert finished.phase is Phase.COMPLETED
    assert finished.result_path is not None
    body = finished.result_path.read_text()
    assert body.splitlines() == ["ra", body.splitlines()[1]]  # header + exactly one row


def test_run_is_idempotent_once_queued_or_executing(manager: JobManager) -> None:
    job = manager.create_job(_tap_request("SELECT ra FROM data.gaia"))
    manager.run_job(job.job_id)
    manager.run_job(job.job_id)  # must not raise
    _wait_for_terminal(manager, job.job_id)


def test_run_on_a_completed_job_is_illegal(manager: JobManager) -> None:
    job = manager.create_job(_tap_request("SELECT ra FROM data.gaia"))
    manager.run_job(job.job_id)
    _wait_for_terminal(manager, job.job_id)

    with pytest.raises(InvalidParameterError):
        manager.run_job(job.job_id)


# --- destruction ----------------------------------------------------------


def test_destroy_removes_the_job_and_its_result(manager: JobManager) -> None:
    job = manager.create_job(_tap_request("SELECT ra FROM data.gaia"))
    manager.run_job(job.job_id)
    finished = _wait_for_terminal(manager, job.job_id)
    result_path = finished.result_path
    assert result_path is not None and result_path.exists()

    manager.destroy_job(job.job_id)

    assert not result_path.exists()
    with pytest.raises(KeyError):
        manager.get_job(job.job_id)


def test_destroy_unknown_job_raises_key_error(manager: JobManager) -> None:
    with pytest.raises(KeyError):
        manager.destroy_job("does-not-exist")


def test_expired_destruction_purges_on_next_access(manager: JobManager) -> None:
    job = manager.create_job(_tap_request("SELECT ra FROM data.gaia"))
    job.destruction = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(KeyError):
        manager.get_job(job.job_id)


# --- abort ------------------------------------------------------------


def test_abort_a_pending_job(manager: JobManager) -> None:
    job = manager.create_job(_tap_request("SELECT ra FROM data.gaia"))

    aborted = manager.abort_job(job.job_id)

    assert aborted.phase is Phase.ABORTED


def test_abort_a_completed_job_is_illegal(manager: JobManager) -> None:
    job = manager.create_job(_tap_request("SELECT ra FROM data.gaia"))
    manager.run_job(job.job_id)
    _wait_for_terminal(manager, job.job_id)

    with pytest.raises(InvalidParameterError):
        manager.abort_job(job.job_id)


def _blocking_run_query(started: threading.Event, release: threading.Event) -> object:
    def run_query(manager: JobManager, job: Job) -> QueryResult:
        started.set()
        release.wait(2.0)
        return QueryResult(table=pa.table({"x": [1]}), elapsed_seconds=0.0)

    return run_query


def _cancellable_run_query() -> object:
    def run_query(manager: JobManager, job: Job) -> QueryResult:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if job.cancel_requested:
                raise QueryTimeoutError(0.1, 0.1)
            time.sleep(0.02)
        raise QueryTimeoutError(2.0, 2.0)  # pragma: no cover - safety net only

    return run_query


def test_max_jobs_saturation_and_abort_paths(tmp_path: Path) -> None:
    settings = Settings(
        sources=[str(DATA_DIR / "gaia.parquet")], result_store=str(tmp_path), max_jobs=1
    )
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    started = threading.Event()
    release = threading.Event()
    manager = JobManager(settings, registry, con, run_query=_blocking_run_query(started, release))
    try:
        job1 = manager.create_job(_tap_request("SELECT ra FROM data.gaia"))
        job2 = manager.create_job(_tap_request("SELECT ra FROM data.gaia"))

        manager.run_job(job1.job_id)
        assert started.wait(2.0), "job1's worker never started"
        assert manager.get_job(job1.job_id).phase is Phase.EXECUTING

        manager.run_job(job2.job_id)
        assert manager.get_job(job2.job_id).phase is Phase.QUEUED

        # QUEUED job2's future is still sitting in the pool's queue, so cancel()
        # succeeds and the transition is immediate -- no polling needed.
        aborted = manager.abort_job(job2.job_id)
        assert aborted.phase is Phase.ABORTED

        release.set()
        finished1 = _wait_for_terminal(manager, job1.job_id)
        assert finished1.phase is Phase.COMPLETED
    finally:
        release.set()
        con.close()


def test_abort_an_executing_job(tmp_path: Path) -> None:
    settings = Settings(
        sources=[str(DATA_DIR / "gaia.parquet")], result_store=str(tmp_path), max_jobs=1
    )
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    manager = JobManager(settings, registry, con, run_query=_cancellable_run_query())
    try:
        job = manager.create_job(_tap_request("SELECT ra FROM data.gaia"))
        manager.run_job(job.job_id)

        deadline = time.monotonic() + 2.0
        while manager.get_job(job.job_id).phase is not Phase.EXECUTING:
            if time.monotonic() > deadline:
                raise AssertionError("job never reached EXECUTING")
            time.sleep(0.02)

        manager.abort_job(job.job_id)
        finished = _wait_for_terminal(manager, job.job_id)
        assert finished.phase is Phase.ABORTED
    finally:
        con.close()
