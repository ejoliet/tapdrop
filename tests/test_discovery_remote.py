"""s3:// and https:// sources. Never a real network: moto for S3, a local
``http.server`` standing in for HTTPS (see ``tests/conftest.py``).
"""

from __future__ import annotations

import duckdb

from tapdrop.discovery import catalog, discover
from tapdrop.sources import resolve_sources


def test_resolve_sources_over_s3(s3_endpoint: str) -> None:
    groups, skipped = resolve_sources(["s3://tapdrop-test/"])
    assert skipped == []
    by_name = {g.table_name: g for g in groups}
    assert set(by_name) == {"gaia", "stars"}
    assert by_name["gaia"].files == ("s3://tapdrop-test/gaia.parquet",)


def test_discover_parquet_over_s3(s3_endpoint: str) -> None:
    # DuckDB reads the parquet content itself (not through fsspec), so it needs
    # its own S3 endpoint config - that wiring belongs to engine.py in a later
    # milestone. Here we configure it directly on a throwaway connection to
    # prove discover_parquet's generated SQL is s3-path-compatible.
    host_port = s3_endpoint.removeprefix("http://")
    con = duckdb.connect()
    con.execute(f"SET s3_endpoint='{host_port}'")
    con.execute("SET s3_use_ssl=false")
    con.execute("SET s3_url_style='path'")
    con.execute("SET s3_access_key_id='testing'")
    con.execute("SET s3_secret_access_key='testing'")

    groups, _ = resolve_sources(["s3://tapdrop-test/gaia.parquet"])
    metas, skipped = catalog.discover_parquet(con, groups[0], {})
    con.close()

    assert skipped == []
    meta = metas[0]
    assert (meta.ra_column, meta.dec_column) == ("ra", "dec")
    assert meta.ra_dec_rule == "ucd"


def test_resolve_sources_over_http(http_server: str) -> None:
    groups, skipped = resolve_sources([f"{http_server}/gaia.parquet"])
    assert skipped == []
    assert len(groups) == 1
    assert groups[0].table_name == "gaia"
    assert groups[0].files == (f"{http_server}/gaia.parquet",)


def test_discover_single_parquet_file_over_http(http_server: str) -> None:
    # Plain HTTP needs no credentials, so the full discover() pipeline (which
    # owns its own DuckDB connection) works end-to-end here, unlike s3://.
    # A bare file URL has no schema-shaped parent directory, so the schema is
    # derived from the host:port authority (see sources.py's single-file case).
    registry = discover([f"{http_server}/gaia.parquet"])
    assert len(registry.tables) == 1
    meta = next(iter(registry.tables.values()))
    assert meta.table_name == "gaia"
    assert (meta.ra_column, meta.dec_column) == ("ra", "dec")
    assert meta.ra_dec_rule == "ucd"


def test_discover_votable_over_http(http_server: str) -> None:
    registry = discover([f"{http_server}/sources.vot"])
    assert len(registry.tables) == 1
    meta = next(iter(registry.tables.values()))
    assert meta.table_name == "sources"
    assert (meta.ra_column, meta.dec_column) == ("ra", "dec")
