"""Query log: what it records, what it must never record, and how it is exposed."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from tapdrop.api.tap import create_app
from tapdrop.config import Settings
from tapdrop.discovery import discover
from tapdrop.engine import create_connection
from tapdrop.querylog import (
    JsonFormatter,
    QueryLog,
    configure_logging,
    redact_token_in_access_log,
    token_hash,
)

DATA_DIR = Path(__file__).parent / "data"
QUERY = {"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": "SELECT TOP 2 ra FROM data.gaia"}


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs"


@pytest.fixture
def client(log_dir: Path) -> Iterator[TestClient]:
    settings = Settings(sources=[str(DATA_DIR / "gaia.parquet")], log_dir=log_dir, token="s3cr3t")
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    try:
        with TestClient(create_app(settings, registry, con)) as client:
            yield client
    finally:
        con.close()


def _rows(client: TestClient) -> list[tuple[object, ...]]:
    con: duckdb.DuckDBPyConnection = client.app.state.con  # type: ignore[attr-defined]
    return con.execute(
        'SELECT endpoint, query, row_count, status, error, token_hash FROM "tapdrop"."query_log"'
    ).fetchall()


def test_a_successful_sync_query_is_recorded(client: TestClient) -> None:
    client.get("/t/s3cr3t/tap/sync", params=QUERY)

    rows = _rows(client)
    assert len(rows) == 1
    endpoint, query, row_count, status, error, _ = rows[0]
    assert (endpoint, status, error) == ("sync", "ok", None)
    assert query == QUERY["QUERY"]
    assert row_count == 2


def test_a_failed_query_is_recorded_with_its_error(client: TestClient) -> None:
    client.get("/t/s3cr3t/tap/sync", params={**QUERY, "QUERY": "SELECT * FROM data.nope"})

    endpoint, _, _, status, error, _ = _rows(client)[0]
    assert (endpoint, status) == ("sync", "error")
    assert "nope" in str(error)


def test_an_async_job_is_recorded(client: TestClient) -> None:
    created = client.post("/t/s3cr3t/tap/async", data=QUERY, follow_redirects=False)
    job_url = created.headers["location"]
    client.post(f"{job_url}/phase", data={"PHASE": "RUN"})

    async_rows = [row for row in _rows(client) if row[0] == "async"]
    assert len(async_rows) == 1
    assert async_rows[0][2] == 2  # row_count
    assert async_rows[0][3] == "ok"


def test_the_token_is_never_written_to_the_log(client: TestClient) -> None:
    client.get("/t/s3cr3t/tap/sync", params=QUERY)

    recorded_hash = _rows(client)[0][5]
    assert recorded_hash == token_hash("s3cr3t")
    assert "s3cr3t" not in str(recorded_hash)
    assert len(str(recorded_hash)) == 16


def test_the_log_is_queryable_through_the_service_itself(client: TestClient) -> None:
    client.get("/t/s3cr3t/tap/sync", params=QUERY)

    response = client.get(
        "/t/s3cr3t/tap/sync",
        params={**QUERY, "QUERY": "SELECT endpoint, status FROM tapdrop.query_log"},
    )

    assert response.status_code == 200
    assert b"sync" in response.content
    assert b"tapdrop.query_log" in client.get("/t/s3cr3t/tap/tables").content


def test_the_log_is_flushed_to_parquet_on_shutdown(log_dir: Path) -> None:
    settings = Settings(sources=[str(DATA_DIR / "gaia.parquet")], log_dir=log_dir)
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    try:
        with TestClient(create_app(settings, registry, con)) as client:
            client.get("/tap/sync", params=QUERY)
    finally:
        con.close()

    written = duckdb.sql(f"SELECT query FROM read_parquet('{log_dir / 'query_log.parquet'}')")
    assert written.fetchall() == [(QUERY["QUERY"],)]


def test_nothing_is_recorded_without_a_log_dir() -> None:
    settings = Settings(sources=[str(DATA_DIR / "gaia.parquet")])
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    try:
        with TestClient(create_app(settings, registry, con)) as client:
            assert client.get("/tap/sync", params=QUERY).status_code == 200
        with pytest.raises(duckdb.CatalogException):
            con.execute('SELECT * FROM "tapdrop"."query_log"')
        assert "tapdrop.query_log" not in registry.tables
    finally:
        con.close()


def test_a_broken_log_does_not_break_the_query(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The log is observability, not the service; it fails alone."""
    con = duckdb.connect()
    query_log = QueryLog(con, tmp_path)
    con.execute('DROP TABLE "tapdrop"."query_log"')  # whatever breaks it, it must not raise

    from tapdrop.querylog import QueryRecord

    with caplog.at_level(logging.ERROR, logger="tapdrop"):
        query_log.record(
            QueryRecord(
                endpoint="sync",
                query="SELECT 1",
                response_format="votable",
                maxrec=1,
                rows=1,
                elapsed_seconds=0.1,
                status="ok",
                error=None,
                token_hash=None,
                started_at=datetime.now(UTC),
            )
        )

    assert "query log" in caplog.text
    con.close()


def test_logs_are_emitted_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    logging.getLogger("tapdrop").info("hello %s", "world")

    line = json.loads(capsys.readouterr().out.strip())
    assert line["message"] == "hello world"
    assert line["level"] == "INFO"
    assert line["time"].endswith("Z")


def test_the_token_never_reaches_uvicorns_access_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """uvicorn logs the request line, and for a tokenised service that is the secret."""
    token = "s3cr3t-token-value"
    access = logging.getLogger("uvicorn.access")
    filters_before = list(access.filters)
    redact_token_in_access_log(token)
    try:
        with caplog.at_level(logging.INFO, logger="uvicorn.access"):
            # The shape uvicorn itself logs with: message plus five arguments.
            access.info(
                '%s - "%s %s HTTP/%s" %d',
                "127.0.0.1:50000",
                "GET",
                f"/t/{token}/tap/sync?QUERY=SELECT+1",
                "1.1",
                200,
            )
    finally:
        access.filters = filters_before

    assert token not in caplog.text
    assert "/t/<redacted>/tap/sync" in caplog.text


def test_a_traceback_goes_into_the_json_line_not_across_lines() -> None:
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "tapdrop",
            logging.ERROR,
            __file__,
            1,
            "failed",
            None,
            exc_info=__import__("sys").exc_info(),
        )

    payload = json.loads(formatter.format(record))
    assert "boom" in payload["error"]
