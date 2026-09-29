"""``engine.py``: connection setup, UDF registration, query timeout."""

from __future__ import annotations

import time

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tapdrop.config import Settings
from tapdrop.engine import create_connection, run_with_timeout
from tapdrop.errors import QueryTimeoutError
from tapdrop.registry import ColumnMeta, Registry, TableMeta


def _parquet_registry(tmp_path):
    # Registry.attach() builds a view over real files, so this needs an
    # actual parquet file on disk -- unlike adql_fixtures.make_registry(),
    # which only backs translate()'s schema lookups.
    path = tmp_path / "stars.parquet"
    table = pa.table({"id": [1, 2], "ra": [10.0, 20.0], "dec": [1.0, 2.0]})
    pq.write_table(table, path)
    stars = TableMeta(
        schema_name="cats",
        table_name="stars",
        columns=(
            ColumnMeta(name="id", datatype="long"),
            ColumnMeta(name="ra", datatype="double"),
            ColumnMeta(name="dec", datatype="double"),
        ),
        source_uris=(str(path),),
    )
    return Registry({"cats.stars": stars})


def test_create_connection_registers_geometry_macros_and_registry(tmp_path):
    settings = Settings(memory_limit="500MB")
    registry = _parquet_registry(tmp_path)
    con = create_connection(settings, registry)
    try:
        assert con.execute("SELECT tapdrop_hav_deg(0, 0, 1, 0)").fetchone()[0] == pytest.approx(1.0)
        # Registry.attach() created the views; TAP_SCHEMA.tables lists them.
        rows = con.execute('SELECT table_name FROM "TAP_SCHEMA"."tables"').fetchall()
        assert ("cats.stars",) in rows
        assert con.execute('SELECT COUNT(*) FROM "cats"."stars"').fetchone()[0] == 2
    finally:
        con.close()


def test_run_with_timeout_returns_rows():
    con = duckdb.connect(":memory:")
    result = run_with_timeout(con, "SELECT 1 AS a, 2 AS b", timeout_seconds=5.0)
    assert result.columns == ["a", "b"]
    assert result.rows == [(1, 2)]
    assert result.elapsed_seconds >= 0.0


def test_run_with_timeout_raises_on_slow_query():
    con = duckdb.connect(":memory:")
    start = time.monotonic()
    with pytest.raises(QueryTimeoutError) as excinfo:
        run_with_timeout(
            con,
            # A filtered scan over a huge lazy range: DuckDB streams and
            # checks for an interrupt between chunks, so this responds to
            # con.interrupt() in well under a second -- unlike a large join
            # build phase, which can run for minutes before checking.
            "SELECT count(*) FROM range(9223372036854775000) WHERE range % 7 = 0",
            timeout_seconds=0.2,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 5.0  # the watchdog actually interrupted it, not a hang
    assert excinfo.value.limit_seconds == 0.2
