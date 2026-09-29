"""CAOM-lite tables and the ObsCore 1.1 view (RDD.md M9)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from tapdrop import caom_lite
from tapdrop.discovery.scan import scan_images
from tapdrop.registry import Registry

DATA_DIR = Path(__file__).parent / "data" / "images"

# ObsCore 1.1 Table 1: every column a service claiming the data model must expose.
OBSCORE_MANDATORY = (
    "dataproduct_type",
    "calib_level",
    "obs_collection",
    "obs_id",
    "obs_publisher_did",
    "access_url",
    "access_format",
    "access_estsize",
    "target_name",
    "s_ra",
    "s_dec",
    "s_fov",
    "s_region",
    "s_resolution",
    "s_xel1",
    "s_xel2",
    "t_min",
    "t_max",
    "t_exptime",
    "t_resolution",
    "t_xel",
    "em_min",
    "em_max",
    "em_res_power",
    "em_xel",
    "o_ucd",
    "pol_states",
    "pol_xel",
    "facility_name",
    "instrument_name",
)


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    catalog = scan_images(str(DATA_DIR))
    caom_lite.build_caom(connection, catalog, "http://127.0.0.1:8000")
    return connection


def _row(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[object, ...]:
    result = con.execute(sql).fetchone()
    assert result is not None
    return result


def test_build_caom_fills_every_table(con: duckdb.DuckDBPyConnection) -> None:
    catalog = scan_images(str(DATA_DIR))
    expected = len(catalog.observations)
    assert expected >= 4  # basic, ra0, pole, no_wcs; corrupt.fits is skipped

    for table in ("observation", "plane", "artifact", "chunk"):
        count = _row(con, f'SELECT count(*) FROM "caom"."{table}"')[0]
        assert count == expected, table


def test_observation_row_carries_header_metadata(con: duckdb.DuckDBPyConnection) -> None:
    obs_uri, collection, instrument, target, facility = _row(
        con,
        "SELECT obs_uri, collection, instrument, target_name, facility "
        'FROM "caom"."observation" WHERE obs_id = \'basic\'',
    )
    assert obs_uri == "caom:TAPDROP-TEST/basic"
    assert collection == "TAPDROP-TEST"
    assert instrument == "TAPDROP-CAM"
    assert target == "M42"
    assert facility == "TAPDROP-TEST"


def test_plane_row_carries_spatial_and_time_coverage(con: duckdb.DuckDBPyConnection) -> None:
    plane_uri, calib_level, dataproduct_type, s_region, s_fov, s_resolution, t_exptime = _row(
        con,
        "SELECT plane_uri, calib_level, dataproduct_type, s_region, s_fov, s_resolution, "
        't_exptime FROM "caom"."plane" WHERE obs_uri = \'caom:TAPDROP-TEST/basic\'',
    )
    assert plane_uri == "caom:TAPDROP-TEST/basic/2"
    assert calib_level == 2
    assert dataproduct_type == "image"
    assert isinstance(s_region, str)
    assert s_region.startswith("POLYGON ICRS ")
    assert s_fov > 0.0
    assert s_resolution == pytest.approx(0.0005 * 3600.0)
    assert t_exptime == 60.0


def test_artifact_access_url_points_at_datalink(con: duckdb.DuckDBPyConnection) -> None:
    access_url, content_type, product_type = _row(
        con,
        'SELECT access_url, content_type, product_type FROM "caom"."artifact" '
        "WHERE plane_uri = 'caom:TAPDROP-TEST/basic/2'",
    )
    assert access_url == ("http://127.0.0.1:8000/datalink/links?ID=caom%3ATAPDROP-TEST%2Fbasic%2F2")
    assert content_type == caom_lite.DATALINK_CONTENT_TYPE
    assert product_type == "science"


def test_chunk_carries_hdu_index_and_rebuildable_wcs(con: duckdb.DuckDBPyConnection) -> None:
    import json

    from astropy.io import fits
    from astropy.wcs import WCS

    artifact_uri = str((DATA_DIR / "basic.fits").resolve())
    chunk_id, extension, naxis1, naxis2, wcs_json = _row(
        con,
        'SELECT chunk_id, extension, naxis1, naxis2, wcs_json FROM "caom"."chunk" '
        f"WHERE artifact_uri = '{artifact_uri}'",
    )
    assert chunk_id == f"{artifact_uri}#{extension}"
    assert (naxis1, naxis2) == (100, 100)

    assert isinstance(wcs_json, str)
    wcs = WCS(fits.Header(json.loads(wcs_json)))
    assert wcs.has_celestial
    ra, dec = wcs.all_pix2world([50.0], [50.0], 0)
    assert float(ra[0]) == pytest.approx(200.0, abs=0.1)
    assert float(dec[0]) == pytest.approx(-10.0, abs=0.1)


def test_chunk_wcs_json_is_null_without_a_wcs(con: duckdb.DuckDBPyConnection) -> None:
    artifact_uri = str((DATA_DIR / "no_wcs.fits").resolve())
    (wcs_json,) = _row(
        con, f'SELECT wcs_json FROM "caom"."chunk" WHERE artifact_uri = \'{artifact_uri}\''
    )
    assert wcs_json is None


# --------------------------------------------------------------------------
# ivoa.obscore
# --------------------------------------------------------------------------


def test_obscore_view_has_every_mandatory_column(con: duckdb.DuckDBPyConnection) -> None:
    columns = [row[0] for row in con.execute('DESCRIBE SELECT * FROM "ivoa"."obscore"').fetchall()]
    assert columns == list(OBSCORE_MANDATORY)


def test_obscore_table_meta_matches_the_view(con: duckdb.DuckDBPyConnection) -> None:
    """TAP_SCHEMA must describe the view that exists, column for column."""
    declared = [column.name for column in caom_lite.table_metas()["ivoa.obscore"].columns]
    actual = [row[0] for row in con.execute('DESCRIBE SELECT * FROM "ivoa"."obscore"').fetchall()]
    assert declared == actual


def test_obscore_row_joins_observation_plane_and_artifact(con: duckdb.DuckDBPyConnection) -> None:
    row = con.execute("""
        SELECT obs_collection, obs_id, target_name, facility_name, instrument_name,
               dataproduct_type, calib_level, access_format, s_xel1, s_xel2,
               o_ucd, t_xel, em_xel, pol_xel
        FROM "ivoa"."obscore" WHERE obs_id = 'basic'
    """).fetchone()
    assert row == (
        "TAPDROP-TEST",
        "basic",
        "M42",
        "TAPDROP-TEST",
        "TAPDROP-CAM",
        "image",
        2,
        caom_lite.DATALINK_CONTENT_TYPE,
        100,
        100,
        "phot.flux",
        1,
        1,
        0,
    )


def test_obscore_keeps_an_image_with_no_wcs_but_nulls_its_coverage(
    con: duckdb.DuckDBPyConnection,
) -> None:
    s_ra, s_dec, s_region, s_resolution = _row(
        con,
        'SELECT s_ra, s_dec, s_region, s_resolution FROM "ivoa"."obscore" '
        "WHERE obs_id = 'no_wcs'",
    )
    assert (s_ra, s_dec, s_region, s_resolution) == (None, None, None, None)


def test_obscore_unknown_fields_are_null_not_missing(con: duckdb.DuckDBPyConnection) -> None:
    t_resolution, em_res_power, pol_states, access_estsize = _row(
        con,
        'SELECT t_resolution, em_res_power, pol_states, access_estsize FROM "ivoa"."obscore" '
        "WHERE obs_id = 'basic'",
    )
    assert (t_resolution, em_res_power, pol_states, access_estsize) == (None, None, None, None)


def test_build_caom_is_repeatable(con: duckdb.DuckDBPyConnection) -> None:
    """Rebuilding replaces the rows rather than doubling them."""
    before = _row(con, 'SELECT count(*) FROM "caom"."plane"')[0]
    caom_lite.build_caom(con, scan_images(str(DATA_DIR)), "http://127.0.0.1:8000")
    assert _row(con, 'SELECT count(*) FROM "caom"."plane"')[0] == before


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------


def test_table_metas_carry_no_source_uris() -> None:
    """Registry.attach must leave these alone: DuckDB already holds them."""
    metas = caom_lite.table_metas()
    assert set(metas) == {
        "caom.observation",
        "caom.plane",
        "caom.artifact",
        "caom.chunk",
        "ivoa.obscore",
    }
    for meta in metas.values():
        assert meta.source_uris == ()


def test_obscore_meta_declares_ra_dec_for_cone_search() -> None:
    obscore = caom_lite.table_metas()["ivoa.obscore"]
    assert (obscore.ra_column, obscore.dec_column) == ("s_ra", "s_dec")
    assert all(column.std for column in obscore.columns)


def test_attach_images_registers_tables_and_fills_tap_schema() -> None:
    connection = duckdb.connect(":memory:")
    registry = Registry({})
    skipped = caom_lite.attach_images(
        connection, registry, [str(DATA_DIR)], "http://127.0.0.1:8000"
    )

    assert "ivoa.obscore" in registry.tables
    assert {Path(s.uri).name for s in skipped} == {"corrupt.fits"}

    described = connection.execute(
        'SELECT DISTINCT table_name FROM "TAP_SCHEMA"."tables"'
    ).fetchall()
    assert {row[0] for row in described} >= {"ivoa.obscore", "caom.plane"}

    ucd = connection.execute(
        'SELECT ucd FROM "TAP_SCHEMA"."columns" '
        "WHERE table_name = 'ivoa.obscore' AND column_name = 's_ra'"
    ).fetchone()
    assert ucd is not None and ucd[0] == "pos.eq.ra"


def test_attach_images_registry_stays_queryable_after_attach() -> None:
    """A registry holding both file-backed and service-provided tables attaches cleanly."""
    connection = duckdb.connect(":memory:")
    registry = Registry({})
    caom_lite.attach_images(connection, registry, [str(DATA_DIR)], "http://127.0.0.1:8000")
    registry.attach(connection)  # must not try to build a view over a missing file

    count = connection.execute('SELECT count(*) FROM "ivoa"."obscore"').fetchone()
    assert count is not None and count[0] >= 4


# --------------------------------------------------------------------------
# declared vs delivered (taplint MDQ compares the two)
# --------------------------------------------------------------------------

_DUCKDB_TO_TAP = {"VARCHAR": "char", "INTEGER": "int", "BIGINT": "long", "DOUBLE": "double"}


def test_obscore_declared_datatypes_match_what_the_view_delivers(
    con: duckdb.DuckDBPyConnection,
) -> None:
    declared = {c.name: c.datatype for c in caom_lite.table_metas()["ivoa.obscore"].columns}
    delivered = {
        row[0]: _DUCKDB_TO_TAP[row[1]]
        for row in con.execute('DESCRIBE SELECT * FROM "ivoa"."obscore"').fetchall()
    }
    assert declared == delivered


def test_obscore_columns_carry_their_obscore_utype() -> None:
    """ObsCore 1.1 Table 6 gives every mandatory column a utype."""
    columns = {c.name: c for c in caom_lite.table_metas()["ivoa.obscore"].columns}

    assert all(c.utype and c.utype.startswith("obscore:") for c in columns.values())
    assert columns["dataproduct_type"].utype == "obscore:ObsDataset.dataProductType"
    assert columns["obs_publisher_did"].utype == "obscore:Curation.publisherDID"
    assert (
        columns["s_ra"].utype
        == "obscore:Char.SpatialAxis.Coverage.Location.Coord.Position2D.Value2.C1"
    )
    assert columns["s_region"].xtype == "adql:REGION"
    assert all(c.xtype is None for name, c in columns.items() if name != "s_region")
