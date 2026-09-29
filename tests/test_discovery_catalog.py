"""Per-format readers and RA/Dec + UCD detection (``discovery/catalog.py``)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pytest

from tapdrop.discovery import catalog
from tapdrop.registry import ColumnMeta
from tapdrop.sources import SourceGroup

DATA_DIR = Path(__file__).parent / "data"


def _uri(name: str) -> str:
    return str((DATA_DIR / name).resolve())


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    yield connection
    connection.close()


# --------------------------------------------------------------------------
# detect_format / type mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "fmt"),
    [
        ("x.parquet", "parquet"),
        ("x.fits", "fits"),
        ("x.fit", "fits"),
        ("x.fits.gz", "fits"),
        ("x.csv", "csv"),
        ("x.tsv", "tsv"),
        ("x.ecsv", "ecsv"),
        ("x.vot", "votable"),
        ("x.xml", "votable"),
        ("x.txt", None),
    ],
)
def test_detect_format(path: str, fmt: str | None) -> None:
    assert catalog.detect_format(path) == fmt


def test_duckdb_type_to_tap_covers_common_types() -> None:
    assert catalog.duckdb_type_to_tap("BIGINT") == "long"
    assert catalog.duckdb_type_to_tap("DOUBLE") == "double"
    assert catalog.duckdb_type_to_tap("VARCHAR") == "char"
    assert catalog.duckdb_type_to_tap("DECIMAL(18,4)") == "double"
    assert catalog.duckdb_type_to_tap("SOMETHING_UNKNOWN") == "char"


def test_numpy_dtype_to_tap() -> None:
    assert catalog.numpy_dtype_to_tap(np.dtype("bool")) == "boolean"
    assert catalog.numpy_dtype_to_tap(np.dtype("int16")) == "short"
    assert catalog.numpy_dtype_to_tap(np.dtype("int32")) == "int"
    assert catalog.numpy_dtype_to_tap(np.dtype("int64")) == "long"
    assert catalog.numpy_dtype_to_tap(np.dtype("float32")) == "float"
    assert catalog.numpy_dtype_to_tap(np.dtype("float64")) == "double"
    assert catalog.numpy_dtype_to_tap(np.dtype("U10")) == "char"


# --------------------------------------------------------------------------
# detect_ra_dec: the 3 rules, the failure case, the mislabeled-column guard
# --------------------------------------------------------------------------


def _range_fn(ranges: dict[str, tuple[float | None, float | None]]) -> catalog.RangeFn:
    def fn(col: str) -> tuple[float | None, float | None]:
        return ranges.get(col, (None, None))

    return fn


def test_ra_dec_ucd_rule_wins_over_everything_else() -> None:
    columns = [
        ColumnMeta(name="lon", datatype="double", ucd="pos.eq.ra;meta.main"),
        ColumnMeta(name="lat", datatype="double", ucd="pos.eq.dec;meta.main"),
        ColumnMeta(name="ra", datatype="double"),  # name-rule bait; must lose
        ColumnMeta(name="dec", datatype="double"),
    ]
    ranges = {"lon": (10.0, 200.0), "lat": (-10.0, 10.0), "ra": (0.0, 0.0), "dec": (0.0, 0.0)}
    guess = catalog.detect_ra_dec(columns, _range_fn(ranges))
    assert (guess.ra_column, guess.dec_column) == ("lon", "lat")
    assert guess.rule == "ucd"
    assert guess.confidence == "high"


def test_ra_dec_name_rule_fires_when_no_ucd() -> None:
    columns = [
        ColumnMeta(name="raj2000", datatype="double"),
        ColumnMeta(name="dej2000", datatype="double"),
    ]
    ranges = {"raj2000": (0.0, 359.0), "dej2000": (-89.0, 89.0)}
    guess = catalog.detect_ra_dec(columns, _range_fn(ranges))
    assert (guess.ra_column, guess.dec_column) == ("raj2000", "dej2000")
    assert guess.rule == "name"
    assert guess.confidence == "medium"


def test_ra_dec_unit_sanity_rule_fires_for_unnamed_numeric_pair() -> None:
    columns = [
        ColumnMeta(name="id", datatype="long"),
        ColumnMeta(name="lon", datatype="double"),
        ColumnMeta(name="lat", datatype="double"),
    ]
    ranges = {"lon": (0.0, 360.0), "lat": (-90.0, 90.0)}
    guess = catalog.detect_ra_dec(columns, _range_fn(ranges))
    assert (guess.ra_column, guess.dec_column) == ("lon", "lat")
    assert guess.rule == "unit_sanity"
    assert guess.confidence == "low"


def test_ra_dec_detection_correctly_fails_when_nothing_plausible() -> None:
    columns = [
        ColumnMeta(name="flux_u", datatype="double"),
        ColumnMeta(name="flux_g", datatype="double"),
    ]
    # Both out of both RA and Dec range: no candidate pair can pass the guard.
    ranges = {"flux_u": (1000.0, 2000.0), "flux_g": (500.0, 900.0)}
    guess = catalog.detect_ra_dec(columns, _range_fn(ranges))
    assert guess.ra_column is None
    assert guess.dec_column is None
    assert guess.rule is None
    assert guess.confidence is None


def test_ra_dec_mislabeled_column_is_caught_by_the_sanity_guard() -> None:
    # A column literally named "ra" with values outside [0, 360] is a
    # mislabeled column, not a hit - the name-rule candidate must be rejected
    # rather than blindly trusted, and detection must not report it as found.
    columns = [
        ColumnMeta(name="ra", datatype="double"),
        ColumnMeta(name="dec", datatype="double"),
    ]
    ranges = {"ra": (400.0, 500.0), "dec": (-10.0, 10.0)}
    guess = catalog.detect_ra_dec(columns, _range_fn(ranges))
    assert guess.ra_column is None
    assert guess.dec_column is None
    assert guess.rule is None


# --------------------------------------------------------------------------
# Parquet
# --------------------------------------------------------------------------


def test_discover_parquet_ucd_and_metadata_round_trip(con: duckdb.DuckDBPyConnection) -> None:
    group = SourceGroup("data", "gaia", (_uri("gaia.parquet"),))
    metas, skipped = catalog.discover_parquet(con, group, {})
    assert skipped == []
    assert len(metas) == 1
    meta = metas[0]

    assert (meta.ra_column, meta.dec_column) == ("ra", "dec")
    assert meta.ra_dec_rule == "ucd"
    assert meta.ra_dec_confidence == "high"

    ra_col = next(c for c in meta.columns if c.name == "ra")
    assert ra_col.unit == "deg"
    assert ra_col.ucd == "pos.eq.ra;meta.main"
    assert ra_col.description == "Right ascension"
    assert ra_col.datatype == "double"

    source_id = next(c for c in meta.columns if c.name == "source_id")
    assert source_id.datatype == "long"

    # No hive-partitioning artifact columns leak into the schema.
    assert {c.name for c in meta.columns} == {"source_id", "ra", "dec", "phot_g_mean_mag"}


def test_discover_parquet_sidecar_yaml_round_trip(con: duckdb.DuckDBPyConnection) -> None:
    group = SourceGroup("data", "clusters", (_uri("clusters.parquet"),))
    metas, skipped = catalog.discover_parquet(con, group, {})
    assert skipped == []
    meta = metas[0]

    assert meta.description == "Star cluster catalog"
    assert (meta.ra_column, meta.dec_column) == ("ra_icrs", "dec_icrs")
    assert meta.ra_dec_rule == "name"
    assert meta.ra_dec_confidence == "medium"

    ra_col = next(c for c in meta.columns if c.name == "ra_icrs")
    assert ra_col.unit == "deg"
    assert ra_col.description == "ICRS right ascension"
    assert ra_col.ucd is None  # sidecar in this fixture deliberately omits ucd


def test_discover_parquet_unreadable_file_is_skipped_not_raised(
    con: duckdb.DuckDBPyConnection,
) -> None:
    group = SourceGroup("data", "corrupt", (_uri("corrupt.fits"),))
    metas, skipped = catalog.discover_parquet(con, group, {})
    assert metas == []
    assert len(skipped) == 1
    assert "could not read Parquet schema" in skipped[0].reason


# --------------------------------------------------------------------------
# CSV / TSV
# --------------------------------------------------------------------------


def test_discover_csv_unit_sanity_rule(con: duckdb.DuckDBPyConnection) -> None:
    group = SourceGroup("data", "stars", (_uri("stars.csv"),))
    metas, skipped = catalog.discover_csv(con, group, {}, "csv")
    assert skipped == []
    meta = metas[0]
    assert (meta.ra_column, meta.dec_column) == ("lon", "lat")
    assert meta.ra_dec_rule == "unit_sanity"
    assert meta.ra_dec_confidence == "low"
    assert {c.name for c in meta.columns} == {"id", "lon", "lat", "flux"}


def test_discover_tsv_detection_correctly_fails(con: duckdb.DuckDBPyConnection) -> None:
    group = SourceGroup("data", "phot", (_uri("phot.tsv"),))
    metas, skipped = catalog.discover_csv(con, group, {}, "tsv")
    assert skipped == []
    meta = metas[0]
    assert meta.ra_column is None
    assert meta.dec_column is None
    assert meta.unresolved == ("ra", "dec")


# --------------------------------------------------------------------------
# astropy-backed: ECSV, VOTable, FITS
# --------------------------------------------------------------------------


def test_discover_ecsv_name_rule_and_unit_description_round_trip() -> None:
    group = SourceGroup("data", "spec", (_uri("spec.ecsv"),))
    metas, skipped = catalog.discover_astropy(group, {}, "ecsv")
    assert skipped == []
    meta = metas[0]
    assert (meta.ra_column, meta.dec_column) == ("ra", "dec")
    assert meta.ra_dec_rule == "name"

    teff = next(c for c in meta.columns if c.name == "teff")
    assert teff.unit == "K"
    assert teff.description == "Effective temperature"


def test_discover_votable_ucd_rule() -> None:
    group = SourceGroup("data", "sources", (_uri("sources.vot"),))
    metas, skipped = catalog.discover_astropy(group, {}, "votable")
    assert skipped == []
    meta = metas[0]
    assert (meta.ra_column, meta.dec_column) == ("ra", "dec")
    assert meta.ra_dec_rule == "ucd"
    assert meta.ra_dec_confidence == "high"


def test_discover_fits_single_bintable_no_hdu_split() -> None:
    group = SourceGroup("data", "single", (_uri("single.fits"),))
    metas, skipped = catalog.discover_astropy(group, {}, "fits")
    assert skipped == []
    assert len(metas) == 1
    meta = metas[0]
    assert meta.table_name == "single"
    assert meta.fits_hdu is None
    assert (meta.ra_column, meta.dec_column) == ("ra", "dec")
    assert meta.ra_dec_rule == "ucd"


def test_discover_fits_multi_bintable_splits_into_name_hdu_n() -> None:
    group = SourceGroup("data", "multi", (_uri("multi.fits"),))
    metas, skipped = catalog.discover_astropy(group, {}, "fits")
    assert skipped == []
    names = {m.table_name: m for m in metas}
    assert set(names) == {"multi_hdu1", "multi_hdu2"}
    assert names["multi_hdu1"].fits_hdu == 1
    assert names["multi_hdu2"].fits_hdu == 2
    # HDU 1 has no ra/dec columns at all.
    assert names["multi_hdu1"].ra_column is None
    # HDU 2 has TUCDn-tagged ra/dec.
    assert (names["multi_hdu2"].ra_column, names["multi_hdu2"].dec_column) == ("ra", "dec")
    assert names["multi_hdu2"].ra_dec_rule == "ucd"


def test_discover_fits_with_no_bintable_is_skipped() -> None:
    group = SourceGroup("data", "empty", (_uri("empty.fits"),))
    metas, skipped = catalog.discover_astropy(group, {}, "fits")
    assert metas == []
    assert len(skipped) == 1
    assert "no BINTABLE HDU" in skipped[0].reason


# --------------------------------------------------------------------------
# discover_table: dispatch + failure isolation
# --------------------------------------------------------------------------


def test_discover_table_dispatches_by_format(con: duckdb.DuckDBPyConnection) -> None:
    group = SourceGroup("data", "gaia", (_uri("gaia.parquet"),))
    metas, skipped = catalog.discover_table(con, group, {})
    assert skipped == []
    assert metas[0].table_name == "gaia"


def test_discover_table_corrupt_file_never_raises_and_is_reported_skipped(
    con: duckdb.DuckDBPyConnection,
) -> None:
    group = SourceGroup("data", "corrupt", (_uri("corrupt.fits"),))
    metas, skipped = catalog.discover_table(con, group, {})
    assert metas == []
    assert len(skipped) == 1
    assert skipped[0].uri == _uri("corrupt.fits")


def test_discover_table_mixed_formats_in_one_group_is_skipped(
    con: duckdb.DuckDBPyConnection,
) -> None:
    group = SourceGroup("data", "mixed", (_uri("gaia.parquet"), _uri("stars.csv")))
    metas, skipped = catalog.discover_table(con, group, {})
    assert metas == []
    assert "mixes incompatible file formats" in skipped[0].reason


def test_discover_table_unrecognized_format_is_skipped(con: duckdb.DuckDBPyConnection) -> None:
    group = SourceGroup("data", "readme", (_uri("generate_fixtures.py"),))
    metas, skipped = catalog.discover_table(con, group, {})
    assert metas == []
    assert skipped[0].reason == "unrecognized format"


# --------------------------------------------------------------------------
# tapdrop.yaml overrides
# --------------------------------------------------------------------------


def test_load_overrides_parses_tables_and_ignores_obscore(tmp_path: Path) -> None:
    config = tmp_path / "tapdrop.yaml"
    config.write_text(
        "tables:\n  data.gaia:\n    description: override\nobscore:\n  table: ivoa.obscore\n"
    )
    overrides = catalog.load_overrides(config)
    assert overrides == {"data.gaia": {"description": "override"}}


def test_overrides_win_over_detection(con: duckdb.DuckDBPyConnection) -> None:
    group = SourceGroup("data", "gaia", (_uri("gaia.parquet"),))
    overrides = {
        "description": "Overridden description",
        "ra": "source_id",  # nonsensical, but proves override wins
        "primary_key": "source_id",
        "columns": {"ra": {"description": "Overridden RA description"}},
    }
    metas, _ = catalog.discover_parquet(con, group, overrides)
    meta = metas[0]
    assert meta.description == "Overridden description"
    assert meta.ra_column == "source_id"
    assert meta.primary_key == "source_id"
    assert meta.ra_dec_rule == "override"
    assert meta.ra_dec_confidence == "override"
    ra_col = next(c for c in meta.columns if c.name == "ra")
    assert ra_col.description == "Overridden RA description"
