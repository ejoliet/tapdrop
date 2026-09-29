"""Cone/haversine correctness, checked against astropy's ``SkyCoord``.

These tests execute the DuckDB macros directly (not through ``translate()``)
since they are testing numeric correctness of the geometry, not translation.
"""

from __future__ import annotations

import numpy as np
import pytest
from astropy import units as u
from astropy.coordinates import SkyCoord

from adql_fixtures import make_engine_connection


@pytest.fixture
def con():
    return make_engine_connection()


def _hav_deg_batch(con, ra1, dec1, ra2, dec2):
    rows = con.execute(
        "SELECT tapdrop_hav_deg(a.ra1, a.dec1, a.ra2, a.dec2) "
        "FROM (SELECT unnest($ra1) AS ra1, unnest($dec1) AS dec1, "
        "unnest($ra2) AS ra2, unnest($dec2) AS dec2) AS a",
        {"ra1": list(ra1), "dec1": list(dec1), "ra2": list(ra2), "dec2": list(dec2)},
    ).fetchall()
    return np.array([r[0] for r in rows])


def _astropy_sep_deg(ra1, dec1, ra2, dec2):
    c1 = SkyCoord(ra=ra1 * u.deg, dec=dec1 * u.deg, frame="icrs")
    c2 = SkyCoord(ra=ra2 * u.deg, dec=dec2 * u.deg, frame="icrs")
    return c1.separation(c2).degree


def test_haversine_matches_astropy_random_points(con):
    rng = np.random.default_rng(42)
    n = 500
    ra1 = rng.uniform(0, 360, n)
    dec1 = rng.uniform(-90, 90, n)
    ra2 = rng.uniform(0, 360, n)
    dec2 = rng.uniform(-90, 90, n)

    got = _hav_deg_batch(con, ra1, dec1, ra2, dec2)
    want = _astropy_sep_deg(ra1, dec1, ra2, dec2)
    np.testing.assert_allclose(got, want, atol=1e-6)


def test_haversine_matches_astropy_edge_cases(con):
    # RA=0 wraparound, both poles, coincident points, antipodal-ish points.
    ra1 = np.array([0.0, 359.0, 10.0, 0.0, 0.0, 45.0])
    dec1 = np.array([0.0, 0.0, 90.0, -90.0, 45.0, 45.0])
    ra2 = np.array([1.0, 1.0, 200.0, 10.0, 0.0, 225.0])
    dec2 = np.array([0.0, 0.0, 90.0, -90.0, 45.0, -45.0])

    got = _hav_deg_batch(con, ra1, dec1, ra2, dec2)
    want = _astropy_sep_deg(ra1, dec1, ra2, dec2)
    np.testing.assert_allclose(got, want, atol=1e-6)


def _prefilter_and_exact(con, ra, dec, ra0, dec0, radius):
    rows = con.execute(
        "SELECT unnest($ra) AS ra, unnest($dec) AS dec",
        {"ra": list(ra), "dec": list(dec)},
    ).fetchall()
    exact = []
    full = []
    for r, d in rows:
        exact_val = con.execute(
            "SELECT tapdrop_hav_deg(?, ?, ?, ?) <= ?", [r, d, ra0, dec0, radius]
        ).fetchone()[0]
        full_val = con.execute(
            "SELECT tapdrop_cone_contains(?, ?, ?, ?, ?)", [r, d, ra0, dec0, radius]
        ).fetchone()[0]
        exact.append(bool(exact_val))
        full.append(bool(full_val))
    return exact, full


@pytest.mark.parametrize(
    "ra0,dec0,radius",
    [
        (0.0, 0.0, 5.0),  # equator, RA=0 seam
        (180.0, 0.0, 30.0),
        (10.0, 89.0, 5.0),  # near north pole
        (10.0, -89.0, 5.0),  # near south pole
        (0.0, 90.0, 90.0),  # radius breaking the naive dec-band (full-sky cap)
        (0.0, 0.0, 179.0),  # huge radius, RA half-width must degenerate to 180
    ],
)
def test_prefilter_never_changes_result_set(con, ra0, dec0, radius):
    rng = np.random.default_rng(7)
    n = 300
    ra = rng.uniform(0, 360, n)
    dec = rng.uniform(-90, 90, n)
    # Densify near the pole and the RA=0 seam, where the prefilter is riskiest.
    ra = np.concatenate([ra, rng.uniform(-2, 2, 50) % 360, np.full(20, 0.0), np.full(20, 359.9)])
    dec = np.concatenate(
        [dec, rng.uniform(85, 90, 50), rng.uniform(-90, -85, 50), np.full(20, 90.0)]
    )

    exact, full = _prefilter_and_exact(con, ra, dec, ra0, dec0, radius)
    assert full == exact, (
        "prefiltered predicate must select exactly the same rows as haversine alone"
    )


def test_cone_contains_matches_astropy_membership(con):
    rng = np.random.default_rng(123)
    n = 1000
    ra = rng.uniform(0, 360, n)
    dec = rng.uniform(-90, 90, n)
    ra0, dec0, radius = 15.0, 10.0, 3.0

    sep = _astropy_sep_deg(ra, dec, np.full(n, ra0), np.full(n, dec0))
    want_contains = sep <= radius

    got_contains = [
        bool(
            con.execute(
                "SELECT tapdrop_cone_contains(?, ?, ?, ?, ?)", [r, d, ra0, dec0, radius]
            ).fetchone()[0]
        )
        for r, d in zip(ra, dec, strict=True)
    ]
    np.testing.assert_array_equal(got_contains, want_contains)
