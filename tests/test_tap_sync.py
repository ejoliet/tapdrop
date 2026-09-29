"""End-to-end tests for ``/tap/sync`` against the committed fixtures.

These go through the real app: parameters, translator, DuckDB, serialisation.
No network — the sources are local files under ``tests/data``.
"""

from __future__ import annotations

import io
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
def client() -> Iterator[TestClient]:
    yield from build_client()


def query(client: TestClient, adql: str, **extra: str) -> object:
    params = {"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": adql, **extra}
    return client.get("/tap/sync", params=params)


def first_table(response: object):
    return parse_votable(io.BytesIO(response.content)).get_first_table()  # type: ignore[attr-defined]


def test_sync_returns_a_votable(client: TestClient) -> None:
    response = query(client, "SELECT source_id, ra, dec FROM data.gaia ORDER BY source_id")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-votable+xml")
    table = first_table(response).to_table(use_names_over_ids=True)
    assert list(table.colnames) == ["source_id", "ra", "dec"]
    assert len(table) > 0


def test_sync_carries_unit_and_ucd_from_discovery(client: TestClient) -> None:
    fields = {f.name: f for f in first_table(query(client, "SELECT ra, dec FROM data.gaia")).fields}

    assert fields["ra"].unit == "deg"
    assert fields["ra"].ucd == "pos.eq.ra;meta.main"


def test_post_works_like_get(client: TestClient) -> None:
    response = client.post(
        "/tap/sync",
        data={"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": "SELECT ra FROM data.gaia"},
    )

    assert response.status_code == 200
    assert b"<FIELD" in response.content


def test_cone_search_runs(client: TestClient) -> None:
    response = query(
        client,
        "SELECT ra, dec FROM data.gaia "
        "WHERE CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 10.0, 41.0, 180.0)) = 1",
    )

    assert response.status_code == 200
    assert len(first_table(response).to_table()) > 0


def test_top_is_honoured(client: TestClient) -> None:
    table = first_table(query(client, "SELECT TOP 2 ra FROM data.gaia")).to_table()

    assert len(table) == 2


def test_maxrec_truncates_and_flags_overflow(client: TestClient) -> None:
    response = query(client, "SELECT ra FROM data.gaia", MAXREC="1")

    assert len(first_table(response).to_table()) == 1
    assert b'value="OVERFLOW"' in response.content


def test_maxrec_zero_returns_metadata_only(client: TestClient) -> None:
    response = query(client, "SELECT ra, dec FROM data.gaia", MAXREC="0")

    table = first_table(response)
    assert len(table.to_table()) == 0
    assert [f.name for f in table.fields] == ["ra", "dec"]
    assert b'value="OVERFLOW"' in response.content


def test_no_overflow_flag_when_the_result_fits(client: TestClient) -> None:
    response = query(client, "SELECT TOP 1 ra FROM data.gaia", MAXREC="1000")

    assert b"OVERFLOW" not in response.content


@pytest.mark.parametrize(
    ("fmt", "media_type"),
    [
        ("csv", "text/csv"),
        ("tsv", "text/tab-separated-values"),
        ("parquet", "application/vnd.apache.parquet"),
        ("json", "application/json"),
        ("votable/td", "application/x-votable+xml"),
    ],
)
def test_response_formats(client: TestClient, fmt: str, media_type: str) -> None:
    response = query(client, "SELECT TOP 3 ra FROM data.gaia", RESPONSEFORMAT=fmt)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media_type)


def test_csv_body_has_a_bare_header(client: TestClient) -> None:
    body = query(client, "SELECT TOP 2 ra, dec FROM data.gaia", RESPONSEFORMAT="csv").text

    assert body.splitlines()[0] == "ra,dec"


def test_unknown_table_is_a_400_votable_error(client: TestClient) -> None:
    response = query(client, "SELECT ra FROM data.gaya")

    assert response.status_code == 400
    info = parse_votable(io.BytesIO(response.content)).resources[0].infos[0]
    assert info.value == "ERROR"
    assert "gaya" in info.content


def test_syntax_error_is_a_400(client: TestClient) -> None:
    response = query(client, "SELECT FROM WHERE")

    assert response.status_code == 400
    assert b'value="ERROR"' in response.content


def test_missing_query_parameter_is_a_400(client: TestClient) -> None:
    response = client.get("/tap/sync", params={"REQUEST": "doQuery", "LANG": "ADQL"})

    assert response.status_code == 400
    assert b"QUERY" in response.content


def test_unsupported_format_is_a_400(client: TestClient) -> None:
    response = query(client, "SELECT ra FROM data.gaia", RESPONSEFORMAT="fits")

    assert response.status_code == 400
    assert b'value="ERROR"' in response.content


@pytest.mark.parametrize(
    "adql",
    [
        "SELECT ra FROM data.gaia; DROP TABLE data.gaia",
        "COPY (SELECT 1) TO '/tmp/pwned.csv'",
        "SELECT * FROM read_parquet('/etc/passwd')",
        "ATTACH '/tmp/evil.db' AS evil",
    ],
)
def test_injection_attempts_are_rejected(client: TestClient, adql: str) -> None:
    response = query(client, adql)

    assert response.status_code == 400
    assert b'value="ERROR"' in response.content


def test_upload_is_refused_when_not_enabled(client: TestClient) -> None:
    response = query(client, "SELECT ra FROM data.gaia", UPLOAD="t,http://example.org/t.vot")

    assert response.status_code == 400
    assert b"allow-upload" in response.content


def test_token_moves_every_route_under_the_secret_prefix() -> None:
    for client in build_client(token="s3cr3t"):
        assert client.get("/tap/sync", params={"REQUEST": "doQuery"}).status_code == 401

        response = client.get(
            "/t/s3cr3t/tap/sync",
            params={
                "REQUEST": "doQuery",
                "LANG": "ADQL",
                "QUERY": "SELECT TOP 1 ra FROM data.gaia",
            },
        )
        assert response.status_code == 200


def test_maxrec_zero_flags_overflow_even_when_nothing_matches(client: TestClient) -> None:
    """DALI 1.1 §4.4.1: zero rows back must not be readable as "no rows matched"."""
    response = query(client, "SELECT ra FROM data.gaia WHERE ra > 1e6", MAXREC="0")

    assert len(first_table(response).to_table()) == 0
    assert b'value="OVERFLOW"' in response.content


def test_syntax_errors_report_where_the_query_failed(client: TestClient) -> None:
    response = query(client, "SELECT * FROM data.gaia WHERE (")

    assert response.status_code == 400
    assert b"at character" in response.content


def test_an_unexpected_failure_is_still_a_votable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """TAP 1.1 §2.6: a VO client cannot read Starlette's JSON 500."""

    def boom(*args: object, **kwargs: object) -> bytes:
        raise RuntimeError("serialiser exploded")

    monkeypatch.setattr("tapdrop.api.tap.serialize", boom)
    settings = Settings(sources=[str(DATA_DIR / "gaia.parquet")])
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    try:
        client = TestClient(create_app(settings, registry, con), raise_server_exceptions=False)
        response = client.get(
            "/tap/sync",
            params={"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": "SELECT ra FROM data.gaia"},
        )
    finally:
        con.close()

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/x-votable+xml")
    assert b'value="ERROR"' in response.content
    assert b"exploded" not in response.content  # internals stay server-side


# --------------------------------------------------------------------------
# TAP_SCHEMA is queryable through ADQL (TAP 1.1 §4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("schema", ["TAP_SCHEMA", "tap_schema"])
def test_tap_schema_tables_are_queryable_case_insensitively(
    client: TestClient, schema: str
) -> None:
    for table in ("schemas", "tables", "columns", "keys", "key_columns"):
        response = query(client, f"SELECT * FROM {schema}.{table}")
        assert response.status_code == 200, (table, response.text)

    names = first_table(
        query(client, f"SELECT table_name FROM {schema}.tables ORDER BY table_name")
    ).to_table(use_names_over_ids=True)
    assert "data.gaia" in list(names["table_name"])
    # TAP_SCHEMA describes itself, so a client can discover its own columns.
    assert "TAP_SCHEMA.columns" in list(names["table_name"])

    columns = first_table(
        query(client, f"SELECT column_name FROM {schema}.columns WHERE table_name = 'data.gaia'")
    ).to_table(use_names_over_ids=True)
    assert {"ra", "dec"} <= set(columns["column_name"])


def test_tap_schema_is_read_only_like_everything_else(client: TestClient) -> None:
    assert query(client, "DROP TABLE TAP_SCHEMA.tables").status_code == 400
    assert query(client, "DELETE FROM tap_schema.columns").status_code == 400


# --------------------------------------------------------------------------
# ivoa.obscore column metadata survives the shared-name lookup
# --------------------------------------------------------------------------


@pytest.fixture
def obscore_client() -> Iterator[TestClient]:
    from tapdrop.caom_lite import attach_images

    settings = Settings(sources=[str(DATA_DIR / "gaia.parquet")], images=[str(DATA_DIR / "images")])
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    attach_images(con, registry, settings.images, "http://testserver")
    try:
        yield TestClient(create_app(settings, registry, con))
    finally:
        con.close()


def test_obscore_fields_carry_ucd_and_unit_despite_bare_caom_columns(
    obscore_client: TestClient,
) -> None:
    """caom.plane declares s_ra/s_fov with no UCD or unit and is registered first."""
    fields = {
        f.name: f for f in first_table(query(obscore_client, "SELECT * FROM ivoa.obscore")).fields
    }

    assert fields["s_ra"].ucd == "pos.eq.ra"
    assert fields["s_ra"].unit == "deg"
    assert fields["s_fov"].unit == "deg"
    assert fields["t_exptime"].unit == "s"
    assert fields["dataproduct_type"].ucd == "meta.code.class"


def test_obscore_s_xel_are_delivered_as_long(obscore_client: TestClient) -> None:
    """ObsCore 1.1 Table 6: s_xel1/s_xel2 are BIGINT; TAP_SCHEMA says long, so must the FIELD."""
    fields = {
        f.name: f
        for f in first_table(
            query(obscore_client, "SELECT s_xel1, s_xel2 FROM ivoa.obscore")
        ).fields
    }
    assert fields["s_xel1"].datatype == "long"
    assert fields["s_xel2"].datatype == "long"
