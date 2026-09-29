"""Spherical polygon / ObsCore ``s_region`` support (M9).

Two kinds of tests:

- Translation-level: which ``POLYGON``/``INTERSECTS``/``CONTAINS`` shapes are
  accepted vs. rejected, and that a malicious region operand cannot reach SQL
  as an interpolated string (mirrors ``tests/test_adql_injection.py``'s
  pattern).
- Numeric correctness: the point-in-spherical-polygon / circle-polygon /
  polygon-polygon UDFs in ``adql/udfs.py``, checked against an independent
  oracle (``mocpy``'s HEALPix-based ``MOC``, already a project dependency)
  rather than re-deriving the same geometry the code under test uses.
"""

from __future__ import annotations

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from mocpy import MOC

from adql_fixtures import make_engine_connection, make_registry
from tapdrop.adql.translate import translate
from tapdrop.errors import AdqlSyntaxError, UnsupportedAdqlError

# ---------------------------------------------------------------------------
# Translation-level: accepted shapes
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    return make_registry()


def test_contains_point_polygon_translates(registry):
    adql = (
        "SELECT * FROM cats.stars WHERE "
        "CONTAINS(POINT('ICRS', ra, dec), POLYGON('ICRS', 0, 0, 1, 0, 1, 1)) = 1"
    )
    result = translate(adql, registry)
    assert "tapdrop_point_in_polygon" in result.sql


def test_contains_point_region_column_translates(registry):
    adql = "SELECT * FROM cats.footprints WHERE CONTAINS(POINT('ICRS', ra, dec), s_region) = 1"
    result = translate(adql, registry)
    assert "tapdrop_point_in_polygon" in result.sql
    assert "tapdrop_parse_region_ra" in result.sql
    assert "tapdrop_parse_region_dec" in result.sql


def test_contains_point_region_string_literal_translates(registry):
    adql = (
        "SELECT * FROM cats.stars WHERE "
        "CONTAINS(POINT('ICRS', ra, dec), 'POLYGON ICRS 0 0 1 0 1 1') = 1"
    )
    result = translate(adql, registry)
    assert "tapdrop_point_in_polygon" in result.sql
    assert "tapdrop_parse_region_ra" in result.sql


@pytest.mark.parametrize(
    "geoms",
    [
        "CIRCLE('ICRS', 0, 0, 1), POLYGON('ICRS', 0, 0, 1, 0, 1, 1)",
        "POLYGON('ICRS', 0, 0, 1, 0, 1, 1), CIRCLE('ICRS', 0, 0, 1)",
    ],
)
def test_intersects_circle_polygon_translates(registry, geoms):
    adql = f"SELECT * FROM cats.stars WHERE INTERSECTS({geoms}) = 1"
    result = translate(adql, registry)
    assert "tapdrop_circle_polygon_intersects" in result.sql


def test_intersects_polygon_polygon_translates(registry):
    adql = (
        "SELECT * FROM cats.stars WHERE "
        "INTERSECTS(POLYGON('ICRS', 0, 0, 1, 0, 1, 1), "
        "POLYGON('ICRS', 0.5, 0.5, 1.5, 0.5, 1.5, 1.5)) = 1"
    )
    result = translate(adql, registry)
    assert "tapdrop_polygon_polygon_intersects" in result.sql


def test_intersects_polygon_region_column_translates(registry):
    adql = (
        "SELECT * FROM cats.footprints WHERE "
        "INTERSECTS(POLYGON('ICRS', 0, 0, 1, 0, 1, 1), s_region) = 1"
    )
    result = translate(adql, registry)
    assert "tapdrop_polygon_polygon_intersects" in result.sql


def test_intersects_region_region_translates(registry):
    adql = (
        "SELECT a.id FROM cats.footprints a, cats.footprints b WHERE "
        "INTERSECTS(a.s_region, b.s_region) = 1"
    )
    result = translate(adql, registry)
    assert "tapdrop_polygon_polygon_intersects" in result.sql


def test_intersects_circle_region_column_translates(registry):
    adql = "SELECT * FROM cats.footprints WHERE INTERSECTS(CIRCLE('ICRS', 0, 0, 1), s_region) = 1"
    result = translate(adql, registry)
    assert "tapdrop_circle_polygon_intersects" in result.sql


# ---------------------------------------------------------------------------
# Translation-level: rejected shapes
# ---------------------------------------------------------------------------


def test_intersects_circle_circle_still_rejected(registry):
    adql = (
        "SELECT * FROM cats.stars WHERE "
        "INTERSECTS(CIRCLE('ICRS', 0, 0, 1), CIRCLE('ICRS', 1, 1, 1)) = 1"
    )
    with pytest.raises(UnsupportedAdqlError, match="INTERSECTS"):
        translate(adql, registry)


def test_intersects_point_operand_rejected(registry):
    adql = (
        "SELECT * FROM cats.stars WHERE "
        "INTERSECTS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 0, 0, 1)) = 1"
    )
    with pytest.raises(UnsupportedAdqlError, match="INTERSECTS"):
        translate(adql, registry)


def test_polygon_too_few_vertices_rejected(registry):
    adql = (
        "SELECT * FROM cats.stars WHERE "
        "CONTAINS(POINT('ICRS', ra, dec), POLYGON('ICRS', 0, 0, 1, 0)) = 1"
    )
    with pytest.raises(AdqlSyntaxError, match="POLYGON"):
        translate(adql, registry)


def test_polygon_odd_coordinate_count_rejected(registry):
    adql = (
        "SELECT * FROM cats.stars WHERE "
        "CONTAINS(POINT('ICRS', ra, dec), POLYGON('ICRS', 0, 0, 1, 0, 1)) = 1"
    )
    with pytest.raises(AdqlSyntaxError, match="POLYGON"):
        translate(adql, registry)


def test_contains_point_subquery_region_rejected(registry):
    # A malicious/invalid region operand: neither a POLYGON literal, a
    # column, nor a string literal must not silently reach SQL generation.
    adql = "SELECT * FROM cats.stars WHERE CONTAINS(POINT('ICRS', ra, dec), (SELECT 1)) = 1"
    with pytest.raises(UnsupportedAdqlError, match="CONTAINS"):
        translate(adql, registry)


# ---------------------------------------------------------------------------
# Injection: a malicious region string cannot escape into SQL text
# ---------------------------------------------------------------------------


def test_malicious_region_literal_cannot_escape_sql():
    """A string region value with SQL metacharacters never becomes SQL text.

    It reaches ``tapdrop_parse_region_ra``/``_dec`` as an ordinary function
    *argument* (a value), not as text spliced into the query -- so it cannot
    smuggle a second statement in, and a value that isn't a valid STC-S
    ``POLYGON`` simply matches nothing (mirrors the negative-radius-from-a-
    column convention in ``tapdrop_cone_contains``).
    """
    con = make_engine_connection()
    con.execute("INSERT INTO cats.stars VALUES (1, 0.0, 0.0, 10.0, 'x')")

    payload = "'); DROP TABLE cats.stars; --"
    escaped = payload.replace("'", "''")
    sql = (
        "SELECT * FROM cats.stars WHERE "
        f"tapdrop_point_in_polygon(ra, dec, tapdrop_parse_region_ra('{escaped}'), "
        f"tapdrop_parse_region_dec('{escaped}'))"
    )
    rows = con.execute(sql).fetchall()
    assert rows == []  # malformed region -> NULL vertex lists -> no match, no crash

    # The table survives: nothing executed as a second statement.
    (count,) = con.execute("SELECT count(*) FROM cats.stars").fetchone()
    assert count == 1


def test_malicious_region_string_literal_end_to_end(registry):
    """The same payload, going through the full ``translate()`` -> DuckDB path."""
    con = make_engine_connection()
    con.execute("INSERT INTO cats.footprints VALUES (1, 0.0, 0.0, NULL)")

    payload = "'); DROP TABLE cats.footprints; --"
    escaped = payload.replace("'", "''")
    adql = f"SELECT * FROM cats.footprints WHERE CONTAINS(POINT('ICRS', ra, dec), '{escaped}') = 1"
    result = translate(adql, registry)
    rows = con.execute(result.sql).fetchall()
    assert rows == []

    (count,) = con.execute("SELECT count(*) FROM cats.footprints").fetchone()
    assert count == 1


# ---------------------------------------------------------------------------
# Numeric correctness, against mocpy (independent HEALPix-based oracle)
# ---------------------------------------------------------------------------


@pytest.fixture
def con():
    return make_engine_connection()


def _point_in_polygon(con, ra, dec, poly_ra, poly_dec):
    (result,) = con.execute(
        "SELECT tapdrop_point_in_polygon(?, ?, ?, ?)",
        [ra, dec, list(poly_ra), list(poly_dec)],
    ).fetchone()
    return bool(result)


def _circle_polygon_intersects(con, ra0, dec0, radius, poly_ra, poly_dec):
    (result,) = con.execute(
        "SELECT tapdrop_circle_polygon_intersects(?, ?, ?, ?, ?)",
        [ra0, dec0, radius, list(poly_ra), list(poly_dec)],
    ).fetchone()
    return bool(result)


def _polygon_polygon_intersects(con, ra1, dec1, ra2, dec2):
    (result,) = con.execute(
        "SELECT tapdrop_polygon_polygon_intersects(?, ?, ?, ?)",
        [list(ra1), list(dec1), list(ra2), list(dec2)],
    ).fetchone()
    return bool(result)


# A tiny square straddling the RA=0 seam.
_RA0_SQUARE = ([359.0, 1.0, 1.0, 359.0], [-1.0, -1.0, 1.0, 1.0])
# A "square" ring around the north pole (great-circle edges bulge poleward
# between vertices -- see implementation-notes.md).
_POLE_RING = ([0.0, 90.0, 180.0, 270.0], [80.0, 80.0, 80.0, 80.0])
# Three collinear points: a degenerate polygon with zero area.
_DEGENERATE = ([0.0, 10.0, 20.0], [0.0, 0.0, 0.0])


def test_polygon_crossing_ra_zero(con):
    ra, dec = _RA0_SQUARE
    assert _point_in_polygon(con, 0.0, 0.0, ra, dec) is True
    assert _point_in_polygon(con, 0.5, 0.5, ra, dec) is True
    assert _point_in_polygon(con, 180.0, 0.0, ra, dec) is False
    assert _point_in_polygon(con, 90.0, 0.0, ra, dec) is False


def test_polygon_enclosing_pole(con):
    ra, dec = _POLE_RING
    assert _point_in_polygon(con, 0.0, 90.0, ra, dec) is True  # the pole itself
    assert _point_in_polygon(con, 45.0, 85.0, ra, dec) is True
    assert _point_in_polygon(con, 45.0, 70.0, ra, dec) is False
    assert _point_in_polygon(con, 0.0, -80.0, ra, dec) is False  # antipodal-ish


def test_point_just_inside_and_outside_an_edge(con):
    ra, dec = _RA0_SQUARE
    # 0.99/0.99 sits inside the diagonal edge (1,-1)->(1,1) at ra<1; 1.01 sits
    # just past it.
    assert _point_in_polygon(con, 0.99, 0.99, ra, dec) is True
    assert _point_in_polygon(con, 1.01, 0.99, ra, dec) is False


def test_degenerate_collinear_polygon_contains_nothing(con):
    ra, dec = _DEGENERATE
    for test_ra, test_dec in [(5.0, 5.0), (5.0, -5.0), (0.0, 90.0), (10.0, 0.0)]:
        assert _point_in_polygon(con, test_ra, test_dec, ra, dec) is False


@pytest.mark.parametrize(
    "ra,dec",
    [_RA0_SQUARE, _POLE_RING, ([10.0, 11.0, 11.0, 10.0], [10.0, 10.0, 11.0, 11.0])],
)
def test_winding_order_does_not_flip_containment(con, ra, dec):
    reversed_ra, reversed_dec = list(reversed(ra)), list(reversed(dec))
    rng = np.random.default_rng(99)
    test_ra = rng.uniform(0, 360, 200)
    test_dec = np.degrees(np.arcsin(rng.uniform(-1, 1, 200)))
    for tr, td in zip(test_ra, test_dec, strict=True):
        forward = _point_in_polygon(con, tr, td, ra, dec)
        backward = _point_in_polygon(con, tr, td, reversed_ra, reversed_dec)
        assert forward == backward, f"winding order flipped containment at ({tr}, {td})"


@pytest.mark.parametrize(
    "ra,dec",
    [
        _RA0_SQUARE,
        _POLE_RING,
        ([10.0, 60.0, 60.0, 10.0], [-40.0, -40.0, 40.0, 40.0]),
        ([0.0, 120.0, 240.0], [80.0, 80.0, 80.0]),
        ([0.0, 72.0, 144.0, 216.0, 288.0], [30.0, 30.0, 30.0, 30.0, 30.0]),
    ],
)
def test_point_in_polygon_matches_mocpy(con, ra, dec):
    sc = SkyCoord(ra, dec, unit="deg", frame="icrs")
    moc = MOC.from_polygon_skycoord(sc, max_depth=16)

    rng = np.random.default_rng(1)
    n = 1500
    test_ra = rng.uniform(0, 360, n)
    test_dec = np.degrees(np.arcsin(rng.uniform(-1, 1, n)))
    want = moc.contains_skycoords(SkyCoord(test_ra, test_dec, unit="deg", frame="icrs"))

    got = np.array(
        [_point_in_polygon(con, r, d, ra, dec) for r, d in zip(test_ra, test_dec, strict=True)]
    )
    np.testing.assert_array_equal(got, want)


@pytest.mark.parametrize(
    "ra,dec,c_ra,c_dec,c_radius,label",
    [
        (*_RA0_SQUARE, 0.0, 0.0, 0.05, "circle inside polygon"),
        (*_RA0_SQUARE, 20.0, 20.0, 1.0, "circle far outside"),
        (*_RA0_SQUARE, 1.0, 0.0, 0.3, "circle straddling an edge"),
        (*_POLE_RING, 0.0, 90.0, 1.0, "circle at the pole"),
        (*_POLE_RING, 45.0, 70.0, 1.0, "circle outside the ring"),
    ],
)
def test_circle_polygon_intersects_matches_mocpy(con, ra, dec, c_ra, c_dec, c_radius, label):
    sc = SkyCoord(ra, dec, unit="deg", frame="icrs")
    poly_moc = MOC.from_polygon_skycoord(sc, max_depth=14)
    circle_moc = MOC.from_cone(
        lon=c_ra * u.deg, lat=c_dec * u.deg, radius=c_radius * u.deg, max_depth=14
    )
    want = not (poly_moc & circle_moc).empty()

    got = _circle_polygon_intersects(con, c_ra, c_dec, c_radius, ra, dec)
    assert got == want, label


@pytest.mark.parametrize(
    "ra1,dec1,ra2,dec2,label",
    [
        (
            [10.0, 11.0, 11.0, 10.0],
            [10.0, 10.0, 11.0, 11.0],
            [10.5, 11.5, 11.5, 10.5],
            [10.5, 10.5, 11.5, 11.5],
            "overlapping squares",
        ),
        (
            [10.0, 11.0, 11.0, 10.0],
            [10.0, 10.0, 11.0, 11.0],
            [20.0, 21.0, 21.0, 20.0],
            [20.0, 20.0, 21.0, 21.0],
            "disjoint squares",
        ),
        (
            *_RA0_SQUARE,
            [0.5, 1.5, 1.5, 0.5],
            [-0.5, -0.5, 0.5, 0.5],
            "RA0 straddle overlap",
        ),
    ],
)
def test_polygon_polygon_intersects_matches_mocpy(con, ra1, dec1, ra2, dec2, label):
    m1 = MOC.from_polygon_skycoord(SkyCoord(ra1, dec1, unit="deg", frame="icrs"), max_depth=14)
    m2 = MOC.from_polygon_skycoord(SkyCoord(ra2, dec2, unit="deg", frame="icrs"), max_depth=14)
    want = not (m1 & m2).empty()

    got = _polygon_polygon_intersects(con, ra1, dec1, ra2, dec2)
    assert got == want, label


# ---------------------------------------------------------------------------
# STC-S ``s_region`` string parsing
# ---------------------------------------------------------------------------


def test_parse_region_valid_polygon(con):
    (ra,) = con.execute("SELECT tapdrop_parse_region_ra('POLYGON ICRS 0 0 1 0 1 1')").fetchone()
    (dec,) = con.execute("SELECT tapdrop_parse_region_dec('POLYGON ICRS 0 0 1 0 1 1')").fetchone()
    assert ra == [0.0, 1.0, 1.0]
    assert dec == [0.0, 0.0, 1.0]


@pytest.mark.parametrize(
    "value",
    [
        "POLYGON GALACTIC 0 0 1 0 1 1",  # non-ICRS frame
        "CIRCLE ICRS 0 0 1",  # not a polygon
        "POLYGON ICRS 0 0 1 0",  # too few vertices
        "POLYGON ICRS 0 0 1 0 1",  # odd coordinate count
        "POLYGON ICRS a b c d e f",  # non-numeric
        "",
    ],
)
def test_parse_region_degrades_to_null_on_bad_input(con, value):
    (ra,) = con.execute("SELECT tapdrop_parse_region_ra(?)", [value]).fetchone()
    assert ra is None


def test_contains_point_region_column_end_to_end(registry):
    con = make_engine_connection()
    con.execute(
        "INSERT INTO cats.footprints VALUES "
        "(1, 0.5, 0.5, 'POLYGON ICRS 0 0 1 0 1 1 0 1'), "
        "(2, 5.0, 5.0, 'POLYGON ICRS 0 0 1 0 1 1 0 1'), "
        "(3, 0.5, 0.5, NULL)"
    )
    adql = "SELECT id FROM cats.footprints WHERE CONTAINS(POINT('ICRS', ra, dec), s_region) = 1"
    result = translate(adql, registry)
    rows = con.execute(result.sql).fetchall()
    assert rows == [(1,)]
