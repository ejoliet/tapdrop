"""HATS directory detection and reading (``discovery/hats.py``)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import fsspec
import pytest

from tapdrop.discovery import hats
from tapdrop.sources import SourceGroup, to_uri

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    yield connection
    connection.close()


def test_is_hats_dir_true_for_the_mini_fixture() -> None:
    fs, path = fsspec.core.url_to_fs(str(DATA_DIR / "hats_catalog"))
    assert hats.is_hats_dir(fs, path) is True


def test_is_hats_dir_false_for_a_plain_directory() -> None:
    fs, path = fsspec.core.url_to_fs(str(DATA_DIR))
    assert hats.is_hats_dir(fs, path) is False


def test_is_hats_dir_false_for_a_missing_directory(tmp_path: Path) -> None:
    fs, path = fsspec.core.url_to_fs(str(tmp_path / "nope"))
    assert hats.is_hats_dir(fs, path) is False


def test_read_hats_order_from_partition_info_csv() -> None:
    fs, path = fsspec.core.url_to_fs(str(DATA_DIR / "hats_catalog"))
    assert hats._read_hats_order(fs, path) == 1


def test_read_hats_order_falls_back_to_directory_names(tmp_path: Path) -> None:
    root = tmp_path / "hats_no_csv"
    (root / "Norder=0").mkdir(parents=True)
    (root / "Norder=0" / "Npix=0.parquet").write_bytes(b"")
    (root / "Norder=2").mkdir()
    (root / "Norder=2" / "Npix=1.parquet").write_bytes(b"")
    (root / "_common_metadata").write_bytes(b"")

    fs, path = fsspec.core.url_to_fs(str(root))
    assert hats.is_hats_dir(fs, path) is True
    assert hats._read_hats_order(fs, path) == 2


def test_discover_hats_table_reuses_the_parquet_reader(con: duckdb.DuckDBPyConnection) -> None:
    root_uri = str((DATA_DIR / "hats_catalog").resolve())
    fs, _ = fsspec.core.url_to_fs(root_uri)
    group = SourceGroup("data", "hats_catalog", (to_uri(fs, root_uri),), is_hats=True)

    metas, skipped = hats.discover_hats_table(con, group, {})
    assert skipped == []
    assert len(metas) == 1
    meta = metas[0]

    assert meta.hats_order == 1
    assert (meta.ra_column, meta.dec_column) == ("ra", "dec")
    assert meta.ra_dec_rule == "ucd"
    # All 3 shards contribute rows; no hive-partitioning "Norder" column leaks in.
    assert {c.name for c in meta.columns} == {"id", "ra", "dec"}
    con.execute(f"SELECT count(*) FROM read_parquet({list(meta.source_uris)!r})")
    (count,) = con.fetchone()
    assert count == 6


def test_discover_hats_table_with_no_parquet_shards_is_skipped(
    con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    root = tmp_path / "empty_hats"
    root.mkdir()
    (root / "partition_info.csv").write_text("Norder,Npix\n0,0\n")

    fs, _ = fsspec.core.url_to_fs(str(root))
    group = SourceGroup("data", "empty_hats", (to_uri(fs, str(root)),), is_hats=True)
    metas, skipped = hats.discover_hats_table(con, group, {})
    assert metas == []
    assert "no Parquet shard files" in skipped[0].reason
