"""End-to-end tests for ``/sia/query`` (SIA v2, RDD.md M9).

Real app, real DuckDB, the image fixtures under ``tests/data/images``, no
network. ``basic.fits`` sits at (200, -10), ``ra0.fits`` straddles RA=0 and
``pole.fits`` sits near the pole, so the POS tests below cover both seams.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from astropy.io.votable import parse as parse_votable
from fastapi.testclient import TestClient

from tapdrop.api.tap import create_app
from tapdrop.caom_lite import attach_images
from tapdrop.config import Settings
from tapdrop.discovery import discover
from tapdrop.engine import create_connection

DATA_DIR = Path(__file__).parent / "data"
IMAGE_DIR = DATA_DIR / "images"


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(sources=[str(DATA_DIR / "gaia.parquet")], images=[str(IMAGE_DIR)])
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    attach_images(con, registry, settings.images, "http://testserver")
    try:
        yield TestClient(create_app(settings, registry, con))
    finally:
        con.close()


@pytest.fixture
def client_without_images() -> Iterator[TestClient]:
    settings = Settings(sources=[str(DATA_DIR / "gaia.parquet")])
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    try:
        yield TestClient(create_app(settings, registry, con))
    finally:
        con.close()


def obs_ids(response: object) -> set[str]:
    votable = parse_votable(io.BytesIO(response.content)).get_first_table()  # type: ignore[attr-defined]
    table = votable.to_table(use_names_over_ids=True)
    return {str(value) for value in table["obs_id"]}


def _center(client: TestClient, obs_id: str) -> tuple[float, float]:
    con = client.app.state.con  # type: ignore[attr-defined]
    row = con.execute(
        'SELECT s_ra, s_dec FROM "ivoa"."obscore" WHERE obs_id = ?', [obs_id]
    ).fetchone()
    assert row is not None
    return float(row[0]), float(row[1])


# --------------------------------------------------------------------------
# POS
# --------------------------------------------------------------------------


def test_no_constraints_returns_every_image(client: TestClient) -> None:
    response = client.get("/sia/query")
    assert response.status_code == 200
    assert {"basic", "ra0", "pole", "no_wcs"} <= obs_ids(response)


def test_pos_circle_selects_only_the_overlapping_image(client: TestClient) -> None:
    response = client.get("/sia/query", params={"POS": "CIRCLE 200.0 -10.0 0.1"})
    assert response.status_code == 200
    assert obs_ids(response) == {"basic"}


def test_pos_circle_elsewhere_matches_nothing(client: TestClient) -> None:
    response = client.get("/sia/query", params={"POS": "CIRCLE 120.0 40.0 0.1"})
    assert response.status_code == 200
    assert obs_ids(response) == set()


def test_pos_circle_across_ra_zero(client: TestClient) -> None:
    ra, dec = _center(client, "ra0")
    response = client.get("/sia/query", params={"POS": f"CIRCLE {ra} {dec} 0.05"})
    assert obs_ids(response) == {"ra0"}


def test_pos_circle_near_the_pole(client: TestClient) -> None:
    ra, dec = _center(client, "pole")
    response = client.get("/sia/query", params={"POS": f"CIRCLE {ra} {dec} 0.05"})
    assert obs_ids(response) == {"pole"}


def test_pos_polygon_selects_the_enclosed_image(client: TestClient) -> None:
    response = client.get(
        "/sia/query",
        params={"POS": "POLYGON 199.9 -10.1 200.1 -10.1 200.1 -9.9 199.9 -9.9"},
    )
    assert obs_ids(response) == {"basic"}


def test_pos_range_selects_the_enclosed_image(client: TestClient) -> None:
    response = client.get("/sia/query", params={"POS": "RANGE 199.9 200.1 -10.1 -9.9"})
    assert obs_ids(response) == {"basic"}


def test_pos_range_wrapping_through_zero_matches_the_ra0_image(client: TestClient) -> None:
    """lon1 > lon2 means the range wraps through RA=0, not the long way round."""
    response = client.get("/sia/query", params={"POS": "RANGE 359.9 0.1 -0.2 0.2"})
    assert obs_ids(response) == {"ra0"}

    # A non-wrapping range covering RA=200 picks up the other image instead.
    wide = client.get("/sia/query", params={"POS": "RANGE 150 250 -30 10"})
    assert obs_ids(wide) == {"basic"}


def test_pos_range_open_bounds_are_clamped_to_the_sky(client: TestClient) -> None:
    """SIA 2.0 section 2.1.1: RANGE takes -Inf/+Inf as open bounds."""
    everything = client.get("/sia/query", params={"POS": "RANGE -Inf +Inf -90 90"})
    assert everything.status_code == 200, everything.text
    assert obs_ids(everything) == obs_ids(client.get("/sia/query"))

    # A pole-to-pole strip: basic at RA 200; ra0 (RA 360) and pole (RA 106) are out.
    strip = client.get("/sia/query", params={"POS": "RANGE 190 210 -Inf +Inf"})
    assert strip.status_code == 200, strip.text
    assert obs_ids(strip) == {"basic"}

    # A wrapping pole-to-pole strip through RA=0 picks up the ra0 image.
    wrapping = client.get("/sia/query", params={"POS": "RANGE 350 10 -Inf +Inf"})
    assert obs_ids(wrapping) == {"ra0"}

    # An all-longitude declination band: basic (-10) and ra0 (0), not pole (90).
    band = client.get("/sia/query", params={"POS": "RANGE -Inf +Inf -30 10"})
    assert obs_ids(band) == {"basic", "ra0"}

    cap = client.get("/sia/query", params={"POS": "RANGE -Inf +Inf 80 +Inf"})
    assert obs_ids(cap) == {"pole"}


def test_post_is_accepted_with_the_same_result_as_get(client: TestClient) -> None:
    """SIA 2.0 section 2.1 / DALI 1.1 section 2.2: parameters by GET or POST."""
    response = client.post("/sia/query", data={"POS": "CIRCLE 200.0 -10.0 0.1"})
    assert response.status_code == 200, response.text
    assert obs_ids(response) == {"basic"}


def test_repeated_pos_is_ored(client: TestClient) -> None:
    ra0_ra, ra0_dec = _center(client, "ra0")
    response = client.get(
        "/sia/query",
        params=[("POS", "CIRCLE 200.0 -10.0 0.1"), ("POS", f"CIRCLE {ra0_ra} {ra0_dec} 0.05")],
    )
    assert obs_ids(response) == {"basic", "ra0"}


# --------------------------------------------------------------------------
# BAND / TIME / POL / COLLECTION
# --------------------------------------------------------------------------


def test_band_interval_overlapping_the_v_filter(client: TestClient) -> None:
    # generic-fits-wcs puts V at 5.0e-7 - 6.0e-7 m; only basic.fits has FILTER.
    response = client.get("/sia/query", params={"BAND": "5.4e-7 5.6e-7"})
    assert obs_ids(response) == {"basic"}


def test_band_outside_every_filter_matches_nothing(client: TestClient) -> None:
    response = client.get("/sia/query", params={"BAND": "1.0e-3 2.0e-3"})
    assert obs_ids(response) == set()


def test_band_open_upper_bound(client: TestClient) -> None:
    response = client.get("/sia/query", params={"BAND": "5.4e-7 +Inf"})
    assert obs_ids(response) == {"basic"}


def test_time_interval_covering_the_exposure(client: TestClient) -> None:
    # basic.fits: DATE-OBS 2025-06-01T00:00:00, one minute long (MJD 60827).
    response = client.get("/sia/query", params={"TIME": "60827.0 60828.0"})
    assert obs_ids(response) == {"basic"}


def test_time_before_the_exposure_matches_nothing(client: TestClient) -> None:
    response = client.get("/sia/query", params={"TIME": "50000.0 50001.0"})
    assert obs_ids(response) == set()


def test_collection_filter(client: TestClient) -> None:
    response = client.get("/sia/query", params={"COLLECTION": "TAPDROP-TEST"})
    assert obs_ids(response) == {"basic"}

    other = client.get("/sia/query", params={"COLLECTION": "NOT-A-COLLECTION"})
    assert obs_ids(other) == set()


def test_pol_matches_nothing_because_no_image_declares_polarization(client: TestClient) -> None:
    response = client.get("/sia/query", params={"POL": "I"})
    assert response.status_code == 200
    assert obs_ids(response) == set()


def test_constraints_are_anded(client: TestClient) -> None:
    response = client.get(
        "/sia/query", params={"POS": "CIRCLE 200.0 -10.0 0.1", "COLLECTION": "NOT-A-COLLECTION"}
    )
    assert obs_ids(response) == set()


def test_maxrec_limits_the_result(client: TestClient) -> None:
    response = client.get("/sia/query", params={"MAXREC": "1"})
    assert response.status_code == 200
    assert len(obs_ids(response)) == 1


def test_responseformat_csv_is_honoured(client: TestClient) -> None:
    """DALI 1.1 section 3.3: a supported RESPONSEFORMAT is served as asked."""
    response = client.get(
        "/sia/query", params={"POS": "CIRCLE 200.0 -10.0 0.1", "RESPONSEFORMAT": "csv"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    header, row = response.text.splitlines()[:2]
    assert "obs_id" in header.split(",")
    assert "basic" in row


def test_responseformat_unknown_is_a_usage_fault(client: TestClient) -> None:
    """DALI 1.1 section 3.3: an unsupported RESPONSEFORMAT is an error, not a silent VOTable."""
    response = client.get("/sia/query", params={"RESPONSEFORMAT": "bogus"})
    assert response.status_code == 400
    assert b"UsageFault: " in response.content
    assert b"bogus" in response.content


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"POS": "CIRCLE 1 2"},
        {"POS": "SQUARE 1 2 3"},
        {"POS": "CIRCLE 1 2 three"},
        {"POS": "POLYGON 1 2 3 4"},
        {"POS": "RANGE 1 2 3"},
        {"POS": "RANGE 0 10 20 10"},
        {"BAND": "1 2 3"},
        {"BAND": "high low", "TIME": "1 2"},
        {"MAXREC": "-1"},
        {"COLLECTION": "robert'); DROP TABLE plane;--"},
    ],
)
def test_bad_parameters_return_a_votable_error(client: TestClient, params: dict[str, str]) -> None:
    response = client.get("/sia/query", params=params)
    assert response.status_code == 400
    assert b"ERROR" in response.content
    # SIA 2.0 section 4.2: the error text starts with a fault code.
    assert b"UsageFault: " in response.content


@pytest.mark.parametrize("pos", ["CIRCLE 1e400 0 1", "CIRCLE inf 0 1", "POLYGON -Inf 0 10 0 10 10"])
def test_pos_infinity_is_rejected_before_it_reaches_the_query(client: TestClient, pos: str) -> None:
    """A bare ``inf`` token in the ADQL would surface as an unknown column.

    Only RANGE reads -Inf/+Inf as open bounds; CIRCLE and POLYGON keep refusing them.
    """
    response = client.get("/sia/query", params={"POS": pos})
    assert response.status_code == 400
    assert b"finite" in response.content
    assert b"Unknown column" not in response.content


def test_pol_underscore_is_not_a_like_wildcard(client: TestClient) -> None:
    response = client.get("/sia/query", params={"POL": "_"})
    assert response.status_code == 400
    assert b"polarization state" in response.content


def test_band_interval_given_low_above_high_is_an_error(client: TestClient) -> None:
    response = client.get("/sia/query", params={"BAND": "6.0e-7 5.0e-7"})
    assert response.status_code == 400


def test_without_images_the_endpoint_reports_no_obscore(client_without_images: TestClient) -> None:
    response = client_without_images.get("/sia/query")
    assert response.status_code == 400
    assert b"Unknown table 'ivoa.obscore'" in response.content
