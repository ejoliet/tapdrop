"""Landing page, drag-drop ingest, liveness and expiry.

The page itself is checked only for what the service promises about it (it is
served, it is HTML, it is behind the token prefix); its behaviour in a browser
is a manual check.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
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
        with TestClient(create_app(settings, registry, con)) as client:
            yield client
    finally:
        con.close()


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield from build_client()


@pytest.fixture
def upload_client() -> Iterator[TestClient]:
    yield from build_client(allow_upload=True)


def test_the_landing_page_is_served_as_html(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<html" in response.text.lower()


def test_the_landing_page_is_behind_the_token_prefix() -> None:
    for client in build_client(token="s3cr3t"):
        assert client.get("/").status_code == 401
        assert client.get("/t/s3cr3t/").status_code == 200


def test_healthz_needs_no_token() -> None:
    for client in build_client(token="s3cr3t"):
        response = client.get("/healthz")

        assert response.status_code == 200
        assert response.text == "ok"
        assert "s3cr3t" not in response.text


def test_upload_is_refused_when_not_enabled(client: TestClient) -> None:
    response = client.post("/upload", files={"file": ("stars.csv", b"ra,dec\n1,2\n", "text/csv")})

    assert response.status_code == 400
    assert b"allow-upload" in response.content


def test_an_uploaded_file_becomes_a_queryable_table(upload_client: TestClient) -> None:
    payload = (DATA_DIR / "stars.csv").read_bytes()

    response = upload_client.post("/upload", files={"file": ("stars.csv", payload, "text/csv")})

    assert response.status_code == 200
    assert response.json()["tables"] == ["uploads.stars"]

    rows = upload_client.get(
        "/tap/sync",
        params={"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": "SELECT * FROM uploads.stars"},
    )
    assert rows.status_code == 200
    assert b"<TABLE" in rows.content


def test_an_uploaded_table_shows_up_in_vosi_tables(upload_client: TestClient) -> None:
    payload = (DATA_DIR / "stars.csv").read_bytes()
    upload_client.post("/upload", files={"file": ("stars.csv", payload, "text/csv")})

    assert b"uploads.stars" in upload_client.get("/tap/tables").content


def test_an_upload_over_the_cap_is_refused() -> None:
    for client in build_client(allow_upload=True, upload_max_mb=1):
        oversized = b"ra,dec\n" + b"1.0,2.0\n" * 200_000  # ~1.6 MB

        response = client.post("/upload", files={"file": ("big.csv", oversized, "text/csv")})

        assert response.status_code == 413
        assert b"1 MB limit" in response.content


def test_an_unreadable_upload_is_reported_not_registered(upload_client: TestClient) -> None:
    response = upload_client.post(
        "/upload", files={"file": ("junk.fits", b"not a fits file", "application/fits")}
    )

    assert response.status_code == 400


def test_a_client_path_in_the_filename_cannot_escape_the_upload_directory(
    upload_client: TestClient,
) -> None:
    payload = (DATA_DIR / "stars.csv").read_bytes()

    response = upload_client.post(
        "/upload", files={"file": ("../../evil.csv", payload, "text/csv")}
    )

    assert response.status_code == 200
    assert response.json()["tables"] == ["uploads.evil"]


def test_requests_are_refused_once_the_service_has_expired() -> None:
    for client in build_client(ttl="1h"):
        client.app.state.settings.down_at = datetime.now(UTC) - timedelta(minutes=1)  # type: ignore[attr-defined]

        response = client.get(
            "/tap/sync",
            params={
                "REQUEST": "doQuery",
                "LANG": "ADQL",
                "QUERY": "SELECT TOP 1 ra FROM data.gaia",
            },
        )

        assert response.status_code == 503
        assert b'value="ERROR"' in response.content
        assert client.get("/healthz").status_code == 200  # liveness still answers


def test_a_bearer_token_works_where_a_path_prefix_is_impractical() -> None:
    """A script can send a header; TOPCAT cannot, which is why both exist."""
    params = {"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": "SELECT TOP 1 ra FROM data.gaia"}
    for client in build_client(token="s3cr3t"):
        ok = client.get("/tap/sync", params=params, headers={"Authorization": "Bearer s3cr3t"})
        wrong = client.get("/tap/sync", params=params, headers={"Authorization": "Bearer nope"})
        basic = client.get("/tap/sync", params=params, headers={"Authorization": "Basic s3cr3t"})

        assert ok.status_code == 200
        assert wrong.status_code == 401
        assert basic.status_code == 401


def test_the_token_never_appears_in_an_error_body() -> None:
    for client in build_client(token="s3cr3t"):
        assert b"s3cr3t" not in client.get("/tap/sync").content
