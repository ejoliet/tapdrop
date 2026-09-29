"""End-to-end tests through a real uvicorn server and a real VO client.

Marked ``live`` and skipped by default (``-m "not live"``): they bind a local
port and drive the service with ``pyvo``, which is the closest thing in CI to
the manual TOPCAT check. Still no external network — the server is this
process's own, on 127.0.0.1, over the committed fixtures.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from tapdrop.api.tap import create_app
from tapdrop.config import Settings
from tapdrop.discovery import discover
from tapdrop.engine import create_connection

pytestmark = pytest.mark.live

DATA_DIR = Path(__file__).parent / "data"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def service() -> Iterator[str]:
    """A real uvicorn server over ``tests/data/gaia.parquet``; yields its base URL."""
    port = free_port()
    settings = Settings(sources=[str(DATA_DIR / "gaia.parquet")], port=port, allow_upload=True)
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, registry, con), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("uvicorn did not start within 30s")

    try:
        yield f"http://127.0.0.1:{port}/tap"
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        con.close()


@pytest.fixture(scope="module")
def tap(service: str) -> object:
    import pyvo

    return pyvo.dal.TAPService(service)


def test_pyvo_reads_the_capabilities(tap: object) -> None:
    assert any("TAP" in str(capability.standardid) for capability in tap.capabilities)  # type: ignore[attr-defined]


def test_pyvo_lists_the_tables_with_their_columns(tap: object) -> None:
    tables = tap.tables  # type: ignore[attr-defined]

    assert "data.gaia" in list(tables.keys())
    assert {"ra", "dec"} <= {column.name for column in tables["data.gaia"].columns}


def test_pyvo_runs_a_sync_cone_search(tap: object) -> None:
    result = tap.search(  # type: ignore[attr-defined]
        "SELECT ra, dec FROM data.gaia "
        "WHERE CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 10.0, 41.0, 180.0)) = 1"
    )
    table = result.to_table()

    assert len(table) > 0
    assert list(table.colnames) == ["ra", "dec"]


def test_pyvo_honours_maxrec(tap: object) -> None:
    assert len(tap.search("SELECT ra FROM data.gaia", maxrec=1).to_table()) == 1  # type: ignore[attr-defined]


def test_pyvo_reports_a_query_error(tap: object) -> None:
    import pyvo

    with pytest.raises(pyvo.dal.DALQueryError):
        tap.search("SELECT nope FROM data.gaia")  # type: ignore[attr-defined]


def test_pyvo_submits_and_runs_an_async_job(tap: object) -> None:
    job = tap.submit_job("SELECT TOP 3 ra, dec FROM data.gaia")  # type: ignore[attr-defined]
    try:
        job.run()
        job.wait(phases={"COMPLETED", "ERROR", "ABORTED"}, timeout=120)

        assert job.phase == "COMPLETED"
        assert len(job.fetch_result().to_table()) == 3
    finally:
        job.delete()


def test_pyvo_uploads_a_table_and_joins_against_it(tap: object) -> None:
    """The v1.0 acceptance check for UPLOAD, driven by a real client."""
    from astropy.table import Table

    mine = Table({"ra": [10.0, 11.0], "dec": [41.0, 42.0]})

    result = tap.run_sync(  # type: ignore[attr-defined]
        "SELECT ra, dec FROM tap_upload.mine", uploads={"mine": mine}
    ).to_table()

    assert len(result) == 2
    assert sorted(result["ra"]) == [10.0, 11.0]


def test_a_deleted_job_is_gone(tap: object) -> None:
    import pyvo

    job = tap.submit_job("SELECT TOP 1 ra FROM data.gaia")  # type: ignore[attr-defined]
    url = job.url
    job.delete()

    with pytest.raises(pyvo.dal.DALServiceError):
        _ = pyvo.dal.AsyncTAPJob(url).phase
