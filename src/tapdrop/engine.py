"""DuckDB connection management: limits, UDFs, timeouts.

One connection per server process. Setup (installing ``httpfs``, registering
the geometry macros, attaching the ``Registry``'s views) happens once in
``create_connection``; every query after that is SQL already produced by
``adql.translate.translate``, never raw client input.

AIDEV-NOTE: "Grant SELECT only" (RDD.md "Security") is enforced by
``adql/translate.py``'s AST allowlist, not by a DuckDB-level permission --
DuckDB's embedded catalog has no per-statement GRANT model to restrict a
single connection to read-only after it has already been used to create
schemas/views/macros. The invariant this module relies on: nothing except
``translate()`` output is ever passed to ``execute``/``run_with_timeout``.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import duckdb
import pyarrow as pa

from tapdrop.adql.udfs import register_geometry_udfs
from tapdrop.errors import QueryTimeoutError

if TYPE_CHECKING:
    from tapdrop.config import Settings
    from tapdrop.registry import Registry


def create_connection(settings: Settings, registry: Registry) -> duckdb.DuckDBPyConnection:
    """Build the one DuckDB connection the service queries against.

    Installs ``httpfs`` for remote (``s3://``/``https://``) scans, applies
    ``settings.memory_limit``, registers the geometry macros, then attaches
    the registry (creates one schema per TAP schema and one view per table)
    and populates ``TAP_SCHEMA``.
    """
    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    con.execute("SET memory_limit = ?", [_memory_limit(settings.memory_limit)])
    register_geometry_udfs(con)
    registry.attach(con)
    registry.populate_tap_schema(con)
    return con


def _memory_limit(value: str) -> str:
    """Turn the RDD's ``75%`` default into a byte size DuckDB accepts.

    DuckDB's ``memory_limit`` takes KB/MB/GB/TB only; a percentage is rejected
    with "Unknown unit for memory". The total is read from the POSIX page count,
    which is the physical machine even inside a container, so a cgroup-limited
    container should be given an absolute ``--memory-limit``.
    """
    text = value.strip()
    if not text.endswith("%"):
        return text
    fraction = float(text[:-1]) / 100.0
    total_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    return f"{max(int(total_bytes * fraction) // (1024 * 1024), 128)}MiB"


@dataclass(frozen=True)
class QueryResult:
    """Result rows as Arrow, which every output format is written from.

    Arrow rather than Python tuples: it keeps DuckDB's types (a nullable int
    stays an int), and the Parquet and CSV writers consume it without a second
    pass over the rows.
    """

    table: pa.Table
    elapsed_seconds: float

    @property
    def columns(self) -> list[str]:
        return list(self.table.column_names)

    @property
    def rows(self) -> list[tuple[Any, ...]]:
        """Row tuples. Convenience for tests and small results only."""
        return [tuple(row.values()) for row in self.table.to_pylist()]


def run_with_timeout(
    con: duckdb.DuckDBPyConnection, sql: str, timeout_seconds: float
) -> QueryResult:
    """Run already-translated *sql* and enforce a wall-clock timeout.

    AIDEV-NOTE: DuckDB has no native per-query timeout. A watchdog thread
    calls ``con.interrupt()`` after ``timeout_seconds`` if the query has not
    finished; ``execute`` releases the GIL while running, so the watchdog
    actually gets to run concurrently. The resulting ``InterruptException``
    becomes ``QueryTimeoutError`` with the elapsed time, per RDD.md's error
    table.
    """
    done = threading.Event()

    def _watchdog() -> None:
        if not done.wait(timeout_seconds):
            con.interrupt()

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    start = time.monotonic()
    watchdog.start()
    try:
        # fetch_arrow_table, not arrow(): the latter returns a RecordBatchReader
        # that would still be streaming after the watchdog has been stood down.
        table = con.execute(sql).fetch_arrow_table()
    except duckdb.InterruptException as exc:
        elapsed = time.monotonic() - start
        raise QueryTimeoutError(elapsed, timeout_seconds) from exc
    finally:
        done.set()
        watchdog.join(timeout=1.0)
    return QueryResult(table=table, elapsed_seconds=time.monotonic() - start)
