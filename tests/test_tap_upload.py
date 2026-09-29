"""TAP ``UPLOAD``: a client table that lives for one query (TAP 1.1 §2.5.2)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from tapdrop.api.tap import create_app
from tapdrop.config import Settings
from tapdrop.discovery import discover
from tapdrop.engine import create_connection

DATA_DIR = Path(__file__).parent / "data"
VOTABLE = (DATA_DIR / "sources.vot").read_bytes()


def build_client(**overrides: object) -> Iterator[TestClient]:
    settings = Settings(sources=[str(DATA_DIR / "gaia.parquet")], **overrides)  # type: ignore[arg-type]
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    try:
        with TestClient(create_app(settings, registry, con)) as client:
            yield client
    finally:
        con.close()


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield from build_client(allow_upload=True)


def post(client: TestClient, query: str, *, payload: bytes = VOTABLE) -> object:
    return client.post(
        "/tap/sync",
        data={"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": query, "UPLOAD": "mine,param:t1"},
        files={"t1": ("mine.vot", payload, "application/x-votable+xml")},
    )


def test_an_uploaded_table_is_queryable_as_tap_upload(client: TestClient) -> None:
    response = post(client, "SELECT * FROM tap_upload.mine")

    assert response.status_code == 200  # type: ignore[attr-defined]
    assert b'value="OK"' in response.content  # type: ignore[attr-defined]


def test_an_uploaded_table_can_be_joined_against_a_served_one(client: TestClient) -> None:
    response = post(
        client,
        "SELECT TOP 5 g.ra FROM data.gaia AS g, tap_upload.mine AS m WHERE g.ra = m.ra",
    )

    assert response.status_code == 200  # type: ignore[attr-defined]


def test_the_uploaded_table_is_gone_once_the_query_is_answered(client: TestClient) -> None:
    post(client, "SELECT * FROM tap_upload.mine")

    con: duckdb.DuckDBPyConnection = client.app.state.con  # type: ignore[attr-defined]
    with pytest.raises(duckdb.CatalogException):
        con.execute('SELECT * FROM "tap_upload"."mine"')

    second = client.post(
        "/tap/sync",
        data={"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": "SELECT * FROM tap_upload.mine"},
    )
    assert second.status_code == 400
    assert b"tap_upload.mine" in second.content


def test_upload_is_refused_when_the_server_did_not_enable_it() -> None:
    for client in build_client():
        response = post(client, "SELECT * FROM tap_upload.mine")

        assert response.status_code == 400  # type: ignore[attr-defined]
        assert b"allow-upload" in response.content  # type: ignore[attr-defined]


def test_an_upload_over_the_cap_is_refused() -> None:
    for client in build_client(allow_upload=True, upload_max_mb=1):
        response = post(client, "SELECT * FROM tap_upload.mine", payload=b"x" * 2 * 1024 * 1024)

        assert response.status_code == 413  # type: ignore[attr-defined]
        assert b"1 MB limit" in response.content  # type: ignore[attr-defined]


def test_a_missing_multipart_part_is_a_client_error(client: TestClient) -> None:
    response = client.post(
        "/tap/sync",
        data={
            "REQUEST": "doQuery",
            "LANG": "ADQL",
            "QUERY": "SELECT * FROM tap_upload.mine",
            "UPLOAD": "mine,param:absent",
        },
        files={"t1": ("mine.vot", VOTABLE, "application/x-votable+xml")},
    )

    assert response.status_code == 400
    assert b"absent" in response.content


def test_a_uri_upload_is_refused_with_a_message_naming_what_is_supported(
    client: TestClient,
) -> None:
    response = client.post(
        "/tap/sync",
        data={
            "REQUEST": "doQuery",
            "LANG": "ADQL",
            "QUERY": "SELECT * FROM tap_upload.mine",
            "UPLOAD": "mine,https://example.invalid/t.vot",
        },
    )

    assert response.status_code == 400
    assert b"param:" in response.content


def test_unreadable_upload_content_is_reported_not_registered(client: TestClient) -> None:
    response = post(client, "SELECT * FROM tap_upload.mine", payload=b"not a votable")

    assert response.status_code == 400
    assert b"UPLOAD" in response.content
