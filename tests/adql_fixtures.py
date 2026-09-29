"""Shared synthetic fixtures for the ADQL translator/engine test suite.

Builds a ``Registry`` directly from ``TableMeta``/``ColumnMeta`` (never from
``tests/data/``, which belongs to the discovery/registry agent) and a tiny
in-memory ``cats.stars`` table for engine-level query execution tests.
"""

from __future__ import annotations

import duckdb

from tapdrop.registry import ColumnMeta, Registry, TableMeta

STARS_COLUMNS = (
    ColumnMeta(name="id", datatype="long"),
    ColumnMeta(name="ra", datatype="double", ucd="pos.eq.ra;meta.main"),
    ColumnMeta(name="dec", datatype="double", ucd="pos.eq.dec;meta.main"),
    ColumnMeta(name="mag", datatype="float"),
    ColumnMeta(name="name", datatype="char", arraysize="32"),
)

GALAXIES_COLUMNS = (
    ColumnMeta(name="galaxy_id", datatype="long"),
    ColumnMeta(name="ra", datatype="double"),
    ColumnMeta(name="dec", datatype="double"),
    ColumnMeta(name="redshift", datatype="double"),
)

# A separate table (rather than adding a column to STARS_COLUMNS) so the M9
# polygon/s_region tests can't perturb the many existing tests that assume
# cats.stars' exact column set.
FOOTPRINTS_COLUMNS = (
    ColumnMeta(name="id", datatype="long"),
    ColumnMeta(name="ra", datatype="double", ucd="pos.eq.ra;meta.main"),
    ColumnMeta(name="dec", datatype="double", ucd="pos.eq.dec;meta.main"),
    ColumnMeta(name="s_region", datatype="char", arraysize="*"),
)


def make_registry() -> Registry:
    """A ``cats.stars`` / ``cats.galaxies`` registry, no files on disk.

    ``source_uris`` is left empty; these tables are only ever used to build
    ``TableMeta``/``ColumnMeta`` shapes for ``translate()``, never attached
    to a real DuckDB connection (see ``make_engine_connection`` for that).
    """
    stars = TableMeta(
        schema_name="cats",
        table_name="stars",
        columns=STARS_COLUMNS,
        ra_column="ra",
        dec_column="dec",
    )
    galaxies = TableMeta(
        schema_name="cats",
        table_name="galaxies",
        columns=GALAXIES_COLUMNS,
        ra_column="ra",
        dec_column="dec",
    )
    footprints = TableMeta(
        schema_name="cats",
        table_name="footprints",
        columns=FOOTPRINTS_COLUMNS,
        ra_column="ra",
        dec_column="dec",
    )
    return Registry({"cats.stars": stars, "cats.galaxies": galaxies, "cats.footprints": footprints})


def make_engine_connection() -> duckdb.DuckDBPyConnection:
    """A real DuckDB connection with geometry macros and a populated table.

    Used by tests that need to *execute* translated SQL (engine tests, the
    cone-correctness oracle), as opposed to translator tests that only check
    the generated SQL text.
    """
    from tapdrop.adql.udfs import register_geometry_udfs

    con = duckdb.connect(":memory:")
    register_geometry_udfs(con)
    con.execute('CREATE SCHEMA IF NOT EXISTS "cats"')
    con.execute(
        'CREATE TABLE "cats"."stars" (id BIGINT, ra DOUBLE, dec DOUBLE, mag REAL, name VARCHAR)'
    )
    con.execute(
        'CREATE TABLE "cats"."footprints" (id BIGINT, ra DOUBLE, dec DOUBLE, s_region VARCHAR)'
    )
    return con
