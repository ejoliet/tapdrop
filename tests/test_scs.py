"""End-to-end tests for ``/scs/{schema}.{table}`` (SCS 1.03).

Same style as ``test_tap_sync.py``: real app, real DuckDB, fixtures under
``tests/data``, no network.
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


@pytest.fixture
def client() -> Iterator[TestClient]:
    # Two tables in the same "data" schema: "gaia" has RA/Dec, "phot" does
    # not, which is what the "table without RA/Dec" tests need.
    settings = Settings(sources=[str(DATA_DIR / "gaia.parquet"), str(DATA_DIR / "phot.tsv")])
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    try:
        yield TestClient(create_app(settings, registry, con))
    finally:
        con.close()


def scs(client: TestClient, path: str = "/scs/data.gaia", **params: str) -> object:
    return client.get(path, params=params)


def first_table(response: object):
    return parse_votable(io.BytesIO(response.content)).get_first_table()  # type: ignore[attr-defined]


def test_cone_hit_returns_the_matching_row(client: TestClient) -> None:
    response = scs(client, RA="10.0", DEC="-60.0", SR="1.0", VERB="3")

    assert response.status_code == 200
    table = first_table(response).to_table(use_names_over_ids=True)
    assert list(table["source_id"]) == [1]


def test_post_is_accepted_with_the_same_result_as_get(client: TestClient) -> None:
    """DALI 1.1 section 2.2: a sync resource takes its parameters by GET or POST."""
    response = client.post(
        "/scs/data.gaia", data={"RA": "10.0", "DEC": "-60.0", "SR": "1.0", "VERB": "3"}
    )

    assert response.status_code == 200, response.text
    table = first_table(response).to_table(use_names_over_ids=True)
    assert list(table["source_id"]) == [1]


def test_cone_miss_returns_no_rows(client: TestClient) -> None:
    response = scs(client, RA="0.0", DEC="0.0", SR="0.01")

    assert response.status_code == 200
    assert len(first_table(response).to_table()) == 0


@pytest.mark.parametrize("missing", ["RA", "DEC", "SR"])
def test_missing_parameter_is_a_400(client: TestClient, missing: str) -> None:
    params = {"RA": "10.0", "DEC": "-60.0", "SR": "1.0"}
    del params[missing]

    response = scs(client, **params)

    assert response.status_code == 400
    info = parse_votable(io.BytesIO(response.content)).resources[0].infos[0]
    assert info.value == "ERROR"
    assert missing in info.content


@pytest.mark.parametrize("bad", ["RA", "DEC", "SR"])
def test_malformed_parameter_is_a_400(client: TestClient, bad: str) -> None:
    params = {"RA": "10.0", "DEC": "-60.0", "SR": "1.0", bad: "not-a-number"}

    response = scs(client, **params)

    assert response.status_code == 400
    assert b'value="ERROR"' in response.content


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RA", "400"),
        ("RA", "-1"),
        ("DEC", "100"),
        ("DEC", "-100"),
        ("SR", "-0.1"),
        ("SR", "200"),
    ],
)
def test_out_of_range_parameter_is_a_400(client: TestClient, name: str, value: str) -> None:
    params = {"RA": "10.0", "DEC": "-60.0", "SR": "1.0", name: value}

    response = scs(client, **params)

    assert response.status_code == 400
    assert b'value="ERROR"' in response.content


def test_sr_zero_is_a_valid_point_search(client: TestClient) -> None:
    response = scs(client, RA="10.0", DEC="-60.0", SR="0")

    assert response.status_code == 200


def test_verb_1_returns_the_minimum_column_set(client: TestClient) -> None:
    table = first_table(scs(client, RA="10.0", DEC="-60.0", SR="180.0", VERB="1")).to_table()

    assert set(table.colnames) == {"ra", "dec"}


def test_verb_3_returns_every_column(client: TestClient) -> None:
    table = first_table(scs(client, RA="10.0", DEC="-60.0", SR="180.0", VERB="3")).to_table()

    assert set(table.colnames) == {"source_id", "ra", "dec", "phot_g_mean_mag"}


def test_default_verb_is_2_and_matches_verb_1_without_configured_principals(
    client: TestClient,
) -> None:
    default_table = first_table(scs(client, RA="10.0", DEC="-60.0", SR="180.0")).to_table()
    verb1_table = first_table(scs(client, RA="10.0", DEC="-60.0", SR="180.0", VERB="1")).to_table()

    assert set(default_table.colnames) == set(verb1_table.colnames)


def test_invalid_verb_is_a_400(client: TestClient) -> None:
    response = scs(client, RA="10.0", DEC="-60.0", SR="1.0", VERB="4")

    assert response.status_code == 400
    assert b"VERB" in response.content


def test_unknown_table_is_a_400(client: TestClient) -> None:
    response = scs(client, "/scs/data.nope", RA="10.0", DEC="-60.0", SR="1.0")

    assert response.status_code == 400
    assert b'value="ERROR"' in response.content


def test_table_without_ra_dec_is_a_400(client: TestClient) -> None:
    response = scs(client, "/scs/data.phot", RA="10.0", DEC="-60.0", SR="1.0")

    assert response.status_code == 400
    assert b'value="ERROR"' in response.content


def test_response_is_votable_tabledata_with_the_right_content_type(client: TestClient) -> None:
    response = scs(client, RA="10.0", DEC="-60.0", SR="180.0")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-votable+xml")
    assert b"<TABLEDATA>" in response.content
    assert b"<BINARY2>" not in response.content
    # Parseable, and carries the discovered unit/UCD like /tap/sync does.
    fields = {f.name: f for f in first_table(response).fields}
    assert fields["ra"].unit == "deg"
