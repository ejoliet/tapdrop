"""End-to-end tests for ``/tap/async`` (UWS 1.1) against the committed fixtures.

Mirrors ``test_tap_sync.py``'s shape: real app, real DuckDB, local fixtures,
no network. Jobs run for real in the app's thread pool, so tests poll
``.../phase`` in a short bounded loop rather than sleeping a fixed amount.
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from astropy.io.votable import parse as parse_votable
from fastapi.testclient import TestClient

from tapdrop.api.tap import create_app
from tapdrop.config import Settings
from tapdrop.discovery import discover
from tapdrop.engine import create_connection

DATA_DIR = Path(__file__).parent / "data"


def build_client(**overrides: object) -> Iterator[TestClient]:
    settings = Settings(sources=[str(DATA_DIR / "gaia.parquet")], **overrides)  # type: ignore[arg-type]
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    try:
        yield TestClient(create_app(settings, registry, con))
    finally:
        con.close()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from build_client(result_store=str(tmp_path))


def create_job(client: TestClient, adql: str, **extra: str) -> str:
    """POST a job, return its absolute job URL without following the redirect."""
    params = {"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": adql, **extra}
    response = client.post("/tap/async", data=params, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"]


def wait_for_phase(client: TestClient, job_url: str, timeout: float = 2.0) -> str:
    deadline = time.monotonic() + timeout
    phase = client.get(f"{job_url}/phase").text
    while phase not in ("COMPLETED", "ERROR", "ABORTED"):
        if time.monotonic() > deadline:
            raise AssertionError(f"job stuck in {phase}")
        time.sleep(0.02)
        phase = client.get(f"{job_url}/phase").text
    return phase


def first_table(response: object):
    return parse_votable(io.BytesIO(response.content)).get_first_table()  # type: ignore[attr-defined]


# --- creation and the job document --------------------------------------


def test_create_job_returns_303_with_a_location(client: TestClient) -> None:
    job_url = create_job(client, "SELECT TOP 1 ra FROM data.gaia")

    assert job_url.startswith("http://testserver/tap/async/")


def test_new_job_document_is_uws_xml_in_pending(client: TestClient) -> None:
    job_url = create_job(client, "SELECT TOP 1 ra FROM data.gaia")

    response = client.get(job_url)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    assert b'xmlns:uws="http://www.ivoa.net/xml/UWS/v1.0"' in response.content
    assert b"<uws:phase>PENDING</uws:phase>" in response.content


def test_job_list_includes_a_jobref(client: TestClient) -> None:
    job_url = create_job(client, "SELECT TOP 1 ra FROM data.gaia")
    job_id = job_url.rsplit("/", 1)[-1]

    response = client.get("/tap/async")

    assert response.status_code == 200
    assert f'id="{job_id}"'.encode() in response.content


# --- running to completion ------------------------------------------------


def test_run_reaches_completed_and_results_are_fetchable(client: TestClient) -> None:
    job_url = create_job(client, "SELECT source_id, ra, dec FROM data.gaia ORDER BY source_id")

    run_response = client.post(f"{job_url}/phase", data={"PHASE": "RUN"}, follow_redirects=False)
    assert run_response.status_code == 303

    phase = wait_for_phase(client, job_url)
    assert phase == "COMPLETED"

    results = client.get(f"{job_url}/results")
    assert b"<uws:result " in results.content

    result_body = client.get(f"{job_url}/results/result")
    assert result_body.status_code == 200
    assert result_body.headers["content-type"].startswith("application/x-votable+xml")
    table = first_table(result_body).to_table(use_names_over_ids=True)
    assert list(table.colnames) == ["source_id", "ra", "dec"]


def test_phase_run_at_creation_time(client: TestClient) -> None:
    params = {
        "REQUEST": "doQuery",
        "LANG": "ADQL",
        "QUERY": "SELECT TOP 1 ra FROM data.gaia",
        "PHASE": "RUN",
    }
    response = client.post("/tap/async", data=params, follow_redirects=False)
    job_url = response.headers["location"]

    assert wait_for_phase(client, job_url) == "COMPLETED"


def test_maxrec_and_responseformat_match_sync(client: TestClient) -> None:
    job_url = create_job(client, "SELECT ra FROM data.gaia", MAXREC="1", RESPONSEFORMAT="csv")
    client.post(f"{job_url}/phase", data={"PHASE": "RUN"}, follow_redirects=False)

    assert wait_for_phase(client, job_url) == "COMPLETED"
    result = client.get(f"{job_url}/results/result")
    assert result.headers["content-type"].startswith("text/csv")
    assert result.text.splitlines() == ["ra", result.text.splitlines()[1]]


# --- errors -------------------------------------------------------------


def test_unknown_table_lands_in_error_with_uws_and_votable_bodies(client: TestClient) -> None:
    job_url = create_job(client, "SELECT ra FROM data.gaya")
    client.post(f"{job_url}/phase", data={"PHASE": "RUN"}, follow_redirects=False)

    assert wait_for_phase(client, job_url) == "ERROR"

    job_doc = client.get(job_url)
    assert b"<uws:errorSummary" in job_doc.content

    error_doc = client.get(f"{job_url}/error")
    assert error_doc.status_code == 200
    info = parse_votable(io.BytesIO(error_doc.content)).resources[0].infos[0]
    assert info.value == "ERROR"
    assert "gaya" in info.content


# --- abort ----------------------------------------------------------------


def test_abort_a_pending_job(client: TestClient) -> None:
    job_url = create_job(client, "SELECT TOP 1 ra FROM data.gaia")

    response = client.post(f"{job_url}/phase", data={"PHASE": "ABORT"}, follow_redirects=False)

    assert response.status_code == 303
    assert client.get(f"{job_url}/phase").text == "ABORTED"


def test_phase_post_rejects_an_unknown_action(client: TestClient) -> None:
    job_url = create_job(client, "SELECT TOP 1 ra FROM data.gaia")

    response = client.post(f"{job_url}/phase", data={"PHASE": "BOGUS"})

    assert response.status_code == 400
    assert b'value="ERROR"' in response.content


# --- destruction and 404s -------------------------------------------------


def test_delete_removes_the_job(client: TestClient) -> None:
    job_url = create_job(client, "SELECT TOP 1 ra FROM data.gaia")

    response = client.delete(job_url, follow_redirects=False)
    assert response.status_code == 303

    assert client.get(job_url).status_code == 404


def test_unknown_job_id_is_404_on_every_subresource(client: TestClient) -> None:
    missing = "/tap/async/does-not-exist"

    assert client.get(missing).status_code == 404
    assert client.get(f"{missing}/phase").status_code == 404
    assert client.get(f"{missing}/quote").status_code == 404
    assert client.get(f"{missing}/executionduration").status_code == 404
    assert client.get(f"{missing}/destruction").status_code == 404
    assert client.get(f"{missing}/error").status_code == 404
    assert client.get(f"{missing}/parameters").status_code == 404
    assert client.get(f"{missing}/results").status_code == 404
    assert client.get(f"{missing}/results/result").status_code == 404
    assert client.delete(missing).status_code == 404
    assert client.post(f"{missing}/phase", data={"PHASE": "RUN"}).status_code == 404
