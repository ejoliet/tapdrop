"""M4 HATS HEALPix pruning: ``discovery/hats.py:prune_files`` and its use by
``adql/translate.py`` to narrow a cone query's FROM source.

Fixture: ``tests/data/hats_pruning`` (built by
``tests/data/generate_fixtures.py:make_hats_pruning_catalog``). Unlike
``tests/data/hats_catalog`` (directory-shape-only), every row here truly sits
inside the nested HEALPix cell its filename claims, so a geometrically wrong
prune would be caught by these tests, not just a directory-listing mismatch.

Layout (order, pixel) -> rows:
  (1, 0)  Pleiades-ish patch:  (56.75, 24.12), (56.80, 24.14)
  (1, 25) a distant patch:     (200.0, -10.0), (200.05, -10.02)
  (0, 9)  near the south pole: (100.0, -87.0), (95.0, -88.0)
  (0, 4)  straddles RA=0/360: (0.3, -40.0), (359.7, -40.0)
"""

from __future__ import annotations

import os
from pathlib import Path

import astropy.units as u
import duckdb
import fsspec
import pytest
from astropy.coordinates import SkyCoord

from tapdrop.adql.translate import translate
from tapdrop.config import Settings
from tapdrop.discovery.hats import discover_hats_table, prune_files
from tapdrop.engine import create_connection
from tapdrop.registry import Registry
from tapdrop.sources import SourceGroup, to_uri

DATA_DIR = Path(__file__).parent / "data" / "hats_pruning"

ALL_ROWS = [
    (1, 56.75, 24.12),
    (2, 56.80, 24.14),
    (3, 200.0, -10.0),
    (4, 200.05, -10.02),
    (5, 100.0, -87.0),
    (6, 95.0, -88.0),
    (7, 0.3, -40.0),
    (8, 359.7, -40.0),
]


def _discover():
    fs, path = fsspec.core.url_to_fs(str(DATA_DIR))
    uri = to_uri(fs, path)
    con = duckdb.connect()
    group = SourceGroup("data", "hats_pruning", (uri,), is_hats=True)
    metas, skipped = discover_hats_table(con, group, {})
    assert skipped == []
    return metas[0]


def _basenames(files: tuple[str, ...]) -> set[str]:
    return {os.path.basename(f) for f in files}


def _brute_force_ids(ra0: float, dec0: float, radius_deg: float) -> set[int]:
    center = SkyCoord(ra=ra0 * u.deg, dec=dec0 * u.deg, frame="icrs")
    return {
        row_id
        for row_id, ra, dec in ALL_ROWS
        if SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs").separation(center).degree
        <= radius_deg
    }


@pytest.fixture
def meta():
    return _discover()


# -- unit tests: prune_files() directly -------------------------------------


def test_prune_files_pleiades_cone_keeps_only_its_shard(meta):
    files = prune_files(meta, 56.77, 24.13, 0.1)
    assert _basenames(files) == {"Npix=0.parquet"}


def test_prune_files_ra_zero_crossing_cone(meta):
    files = prune_files(meta, 0.0, -40.0, 1.0)
    assert _basenames(files) == {"Npix=4.parquet"}


def test_prune_files_pole_cone(meta):
    files = prune_files(meta, 97.0, -87.5, 2.0)
    assert _basenames(files) == {"Npix=9.parquet"}


def test_prune_files_cone_matching_nothing(meta):
    files = prune_files(meta, 150.0, 40.0, 0.05)
    assert files == ()


def test_prune_files_cone_above_safety_threshold_falls_back_to_every_file(meta):
    # RDD M4 edge case: "a cone larger than the whole catalog". Above
    # _MAX_PRUNE_RADIUS_DEG, prune_files() takes the always-correct,
    # unconditional fallback rather than trusting cdshealpix's non-monotonic
    # large-radius behaviour (see hats.py's AIDEV-NOTE).
    files = prune_files(meta, 10.0, 10.0, 100.0)
    assert set(files) == set(meta.source_uris)


def test_prune_files_non_hats_table_returns_every_file():
    from dataclasses import replace

    meta = _discover()
    unpruned = replace(meta, hats_order=None, hats_pixels=None)
    assert prune_files(unpruned, 56.77, 24.13, 0.1) == unpruned.source_uris


# -- integration: translate() + execute against a real connection -----------


def _registry_and_connection():
    meta = _discover()
    registry = Registry({"data.hats_pruning": meta})
    con = create_connection(Settings(memory_limit="500MB"), registry)
    return registry, con


def _cone_adql(ra0: float, dec0: float, radius_deg: float) -> str:
    return (
        "SELECT id, ra, dec FROM data.hats_pruning WHERE 1=CONTAINS("
        f"POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra0}, {dec0}, {radius_deg}))"
    )


@pytest.mark.parametrize(
    "ra0,dec0,radius_deg",
    [
        (56.77, 24.13, 0.1),  # A: Pleiades patch
        (0.0, -40.0, 1.0),  # B: RA=0 crossing
        (97.0, -87.5, 2.0),  # C: pole
        (150.0, 40.0, 0.05),  # D: matches nothing
        (10.0, 10.0, 100.0),  # E: larger than the whole catalog
    ],
)
def test_pruned_query_matches_astropy_brute_force(ra0, dec0, radius_deg):
    registry, con = _registry_and_connection()
    try:
        adql = _cone_adql(ra0, dec0, radius_deg)
        result = translate(adql, registry)
        rows = con.execute(result.sql).fetchall()
        got_ids = {row[0] for row in rows}
        assert got_ids == _brute_force_ids(ra0, dec0, radius_deg)
    finally:
        con.close()


def test_pruned_query_touches_fewer_files_than_the_full_view(tmp_path):
    # RDD M4 acceptance check: the cone actually causes fewer files to be
    # opened, not just fewer rows returned - assert on the generated SQL's
    # file list, not on row counts.
    registry, con = _registry_and_connection()
    try:
        result = translate(_cone_adql(56.77, 24.13, 0.1), registry)
        assert result.sql.lower().count(".parquet") == 1
    finally:
        con.close()


def test_pruned_query_matches_unpruned_view_exactly():
    # Same predicate, run once through the (pruned) translator and once
    # directly against the unpruned view Registry.attach() built - pruning
    # must never change the result set, only which files get opened.
    registry, con = _registry_and_connection()
    try:
        ra0, dec0, radius_deg = 56.77, 24.13, 0.1
        pruned_sql = translate(_cone_adql(ra0, dec0, radius_deg), registry).sql
        pruned_rows = sorted(con.execute(pruned_sql).fetchall())

        unpruned_sql = (
            'SELECT id, ra, dec FROM "data"."hats_pruning" WHERE 1=tapdrop_cone_contains('
            f"ra, dec, {ra0}, {dec0}, {radius_deg})"
        )
        unpruned_rows = sorted(con.execute(unpruned_sql).fetchall())

        assert pruned_rows == unpruned_rows
        assert pruned_rows  # sanity: the scenario actually matches something
    finally:
        con.close()


def test_non_cone_query_over_a_hats_table_is_unpruned_and_still_correct():
    registry, con = _registry_and_connection()
    try:
        result = translate("SELECT COUNT(*) AS n FROM data.hats_pruning WHERE ra > 100", registry)
        # No cone in scope: _prune_hats_source must not fire, so the query
        # still goes through the qualified view name, not a rewritten
        # read_parquet() subquery.
        assert "read_parquet" not in result.sql.lower()
        rows = con.execute(result.sql).fetchall()
        expected = sum(1 for _id, ra, _dec in ALL_ROWS if ra > 100)
        assert rows == [(expected,)]
    finally:
        con.close()
