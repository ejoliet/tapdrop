"""Table/column model, ``attach()``, and ``TAP_SCHEMA`` population (``registry.py``)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from tapdrop.discovery import discover
from tapdrop.errors import UnknownTableError
from tapdrop.registry import ColumnMeta, Registry, TableMeta, tap_schema_metas
from tapdrop.sources import SkippedFile

DATA_DIR = Path(__file__).parent / "data"


def _uri(name: str) -> str:
    return str((DATA_DIR / name).resolve())


def _sample_registry() -> Registry:
    parquet_meta = TableMeta(
        schema_name="cats",
        table_name="gaia",
        columns=(
            ColumnMeta(name="id", datatype="long"),
            ColumnMeta(name="ra", datatype="double", unit="deg", ucd="pos.eq.ra;meta.main"),
            ColumnMeta(name="dec", datatype="double", unit="deg", ucd="pos.eq.dec;meta.main"),
        ),
        ra_column="ra",
        dec_column="dec",
        source_uris=(_uri("gaia.parquet"),),
        ra_dec_rule="ucd",
        ra_dec_confidence="high",
    )
    csv_meta = TableMeta(
        schema_name="cats",
        table_name="stars",
        columns=(
            ColumnMeta(name="id", datatype="long"),
            ColumnMeta(name="lon", datatype="double"),
            ColumnMeta(name="lat", datatype="double"),
            ColumnMeta(name="flux", datatype="double"),
        ),
        ra_column="lon",
        dec_column="lat",
        source_uris=(_uri("stars.csv"),),
        ra_dec_rule="unit_sanity",
        ra_dec_confidence="low",
    )
    fits_meta = TableMeta(
        schema_name="cats",
        table_name="single",
        columns=(
            ColumnMeta(name="id", datatype="long"),
            ColumnMeta(name="ra", datatype="double", ucd="pos.eq.ra;meta.main", principal=True),
            ColumnMeta(name="dec", datatype="double", ucd="pos.eq.dec;meta.main", principal=True),
        ),
        ra_column="ra",
        dec_column="dec",
        source_uris=(_uri("single.fits"),),
        ra_dec_rule="ucd",
        ra_dec_confidence="high",
    )
    tables = {
        parquet_meta.qualified_name.lower(): parquet_meta,
        csv_meta.qualified_name.lower(): csv_meta,
        fits_meta.qualified_name.lower(): fits_meta,
    }
    return Registry(tables, (SkippedFile(_uri("corrupt.fits"), "discovery failed: bad file"),))


def test_get_returns_the_table_meta() -> None:
    registry = _sample_registry()
    meta = registry.get("cats.gaia")
    assert meta.table_name == "gaia"


def test_get_is_case_insensitive() -> None:
    registry = _sample_registry()
    assert registry.get("CATS.GAIA").table_name == "gaia"


def test_get_unknown_table_raises_with_close_matches() -> None:
    registry = _sample_registry()
    with pytest.raises(UnknownTableError) as exc_info:
        registry.get("cats.gai")
    assert "cats.gaia" in exc_info.value.close_matches


def test_close_matches_empty_for_nothing_similar() -> None:
    registry = _sample_registry()
    assert registry.close_matches("zzzzzzzzz.nope") == []


def test_attach_creates_queryable_views_for_every_format() -> None:
    registry = _sample_registry()
    con = duckdb.connect()
    registry.attach(con)

    ra, dec = con.execute('SELECT ra, dec FROM "cats"."gaia" ORDER BY ra LIMIT 1').fetchone()
    assert ra == 10.0
    assert dec == -60.0

    (count,) = con.execute('SELECT count(*) FROM "cats"."stars"').fetchone()
    assert count == 4

    (count,) = con.execute('SELECT count(*) FROM "cats"."single"').fetchone()
    assert count == 3
    con.close()


def test_attach_creates_one_schema_per_table_schema() -> None:
    registry = _sample_registry()
    con = duckdb.connect()
    registry.attach(con)
    rows = con.execute("SELECT schema_name FROM information_schema.schemata").fetchall()
    schemas = {row[0] for row in rows}
    assert "cats" in schemas
    con.close()


def test_populate_tap_schema_fills_schemas_tables_columns() -> None:
    registry = _sample_registry()
    con = duckdb.connect()
    registry.populate_tap_schema(con)

    schemas = {
        r[0] for r in con.execute('SELECT schema_name FROM "TAP_SCHEMA"."schemas"').fetchall()
    }
    assert schemas == {"cats", "TAP_SCHEMA"}

    tables = {r[0] for r in con.execute('SELECT table_name FROM "TAP_SCHEMA"."tables"').fetchall()}
    assert tables == {
        "cats.gaia",
        "cats.stars",
        "cats.single",
        "TAP_SCHEMA.schemas",
        "TAP_SCHEMA.tables",
        "TAP_SCHEMA.columns",
        "TAP_SCHEMA.keys",
        "TAP_SCHEMA.key_columns",
    }

    columns = con.execute(
        'SELECT column_name, unit, ucd, principal FROM "TAP_SCHEMA"."columns" '
        "WHERE table_name = 'cats.gaia' ORDER BY column_name"
    ).fetchall()
    by_name = {row[0]: row for row in columns}
    assert by_name["ra"][1] == "deg"
    assert by_name["ra"][2] == "pos.eq.ra;meta.main"
    assert by_name["ra"][3] == 1  # principal, because it is the RA column

    (key_table,) = con.execute('SELECT count(*) FROM "TAP_SCHEMA"."keys"').fetchone()
    assert key_table == 0  # no foreign keys discovered in M1
    con.close()


def test_populate_tap_schema_marks_explicit_principal_columns() -> None:
    registry = _sample_registry()
    con = duckdb.connect()
    registry.populate_tap_schema(con)
    (principal,) = con.execute(
        'SELECT principal FROM "TAP_SCHEMA"."columns" '
        "WHERE table_name = 'cats.single' AND column_name = 'ra'"
    ).fetchone()
    assert principal == 1
    con.close()


def test_end_to_end_discover_attach_and_query_a_view() -> None:
    """discover() -> attach() -> a plain SELECT works, per the fixed contract."""
    registry = discover([str(DATA_DIR / "golden")])
    con = duckdb.connect()
    registry.attach(con)
    (count,) = con.execute('SELECT count(*) FROM "golden"."t1"').fetchone()
    assert count == 2
    con.close()


# --------------------------------------------------------------------------
# TAP_SCHEMA describes itself (TAP 1.1 §4)
# --------------------------------------------------------------------------

_DUCKDB_TO_TAP = {"VARCHAR": "char", "INTEGER": "int", "BIGINT": "long", "DOUBLE": "double"}


def test_tap_schema_registers_its_own_five_tables_as_service_provided() -> None:
    registry = _sample_registry()
    con = duckdb.connect()
    registry.populate_tap_schema(con)

    tap_schema = {name for name in registry.tables if name.startswith("tap_schema.")}
    assert tap_schema == {
        "tap_schema.schemas",
        "tap_schema.tables",
        "tap_schema.columns",
        "tap_schema.keys",
        "tap_schema.key_columns",
    }
    for name in tap_schema:
        assert registry.tables[name].source_uris == ()  # attach() must leave them alone
    registry.attach(con)  # and it does: no view over a missing file
    assert registry.get("TAP_SCHEMA.Tables").qualified_name == "TAP_SCHEMA.tables"
    con.close()


def test_tap_schema_metas_match_the_duckdb_tables_column_for_column() -> None:
    """The declared columns must be the delivered ones: taplint compares them."""
    registry = _sample_registry()
    con = duckdb.connect()
    registry.populate_tap_schema(con)

    for meta in tap_schema_metas().values():
        described = con.execute(f'DESCRIBE "TAP_SCHEMA"."{meta.table_name}"').fetchall()
        delivered = [(row[0], _DUCKDB_TO_TAP[row[1]]) for row in described]
        declared = [(column.name, column.datatype) for column in meta.columns]
        assert declared == delivered, meta.qualified_name
    con.close()


def test_populate_tap_schema_writes_utype_and_xtype() -> None:
    meta = TableMeta(
        schema_name="cats",
        table_name="regions",
        columns=(
            ColumnMeta(
                name="s_region",
                datatype="char",
                arraysize="*",
                utype="obscore:Char.SpatialAxis.Coverage.Support.Area",
                xtype="adql:REGION",
            ),
        ),
    )
    con = duckdb.connect()
    Registry({"cats.regions": meta}).populate_tap_schema(con)
    row = con.execute(
        'SELECT utype, xtype FROM "TAP_SCHEMA"."columns" WHERE table_name = \'cats.regions\''
    ).fetchone()
    assert row == ("obscore:Char.SpatialAxis.Coverage.Support.Area", "adql:REGION")
    con.close()
