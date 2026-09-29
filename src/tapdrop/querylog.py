"""The query log: what ran, how long it took, and how much came back.

Enabled by ``--log-dir``. Rows live in a DuckDB table so they are queryable
through the service itself as ``tapdrop.query_log`` (RDD.md), and are written
out as Parquet so they outlive a service that is, by design, temporary.

The token never appears here. A request authenticated with the secret is
recorded by the first 16 hex characters of its SHA-256, which is enough to tell
two callers apart and useless for impersonating either.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tapdrop.registry import ColumnMeta, TableMeta

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    import duckdb

__all__ = [
    "QueryLog",
    "QueryRecord",
    "configure_logging",
    "redact_token_in_access_log",
    "table_meta",
    "token_hash",
]

logger = logging.getLogger("tapdrop")

#: ponytail: the whole table is rewritten on every flush, which is fine for a
#: service meant to run for hours; switch to appending Parquet files if a
#: tapdrop instance ever logs enough queries for the rewrite to show up.
FLUSH_EVERY = 50


class JsonFormatter(logging.Formatter):
    """One JSON object per log line, for a service that usually runs in a container.

    Cloud Run and HF Spaces both read stdout and parse JSON lines; a human
    reading them locally loses nothing, because the message is still in there.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: int = logging.INFO) -> None:
    """Send tapdrop's own logs to stdout as JSON. Called once, from ``serve``."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(level)
    logger.propagate = False  # uvicorn configures the root logger for its own lines


class _TokenRedactingFilter(logging.Filter):
    """Replace the secret token wherever it appears in a log record.

    uvicorn's access logger prints the request line, and for a tokenised
    service that line is ``GET /t/<token>/tap/sync?... HTTP/1.1``. The token is
    substituted in both the format arguments (where uvicorn puts the path) and
    the message itself, so it never reaches a terminal or a log file.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        self._prefix = f"/t/{token}"
        self._replacement = "/t/<redacted>"

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                argument.replace(self._prefix, self._replacement)
                if isinstance(argument, str)
                else argument
                for argument in record.args
            )
        if isinstance(record.msg, str):
            record.msg = record.msg.replace(self._prefix, self._replacement)
        return True


def redact_token_in_access_log(token: str) -> None:
    """Keep the ``--token`` secret out of uvicorn's own log lines.

    ``configure_logging`` only owns the ``tapdrop`` logger; uvicorn writes its
    access lines through loggers of its own, which is where a secret path would
    otherwise be recorded verbatim.
    """
    redact = _TokenRedactingFilter(token)
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(redact)


@dataclass(frozen=True)
class QueryRecord:
    """One request worth logging. Everything here is safe to write down."""

    endpoint: str  # "sync" or "async"
    query: str
    response_format: str
    maxrec: int
    rows: int
    elapsed_seconds: float
    status: str  # "ok" or "error"
    error: str | None
    token_hash: str | None
    started_at: datetime


def token_hash(token: str | None) -> str | None:
    """A short, stable fingerprint of a token. Never the token itself."""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class QueryLog:
    """Records queries into ``tapdrop.query_log`` and flushes them to Parquet.

    A no-op when no log directory is configured, so callers never branch.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection, log_dir: Path | None) -> None:
        self.log_dir = log_dir
        self._con = con
        self._lock = threading.Lock()
        self._unflushed = 0
        self._path = None if log_dir is None else log_dir / "query_log.parquet"
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            self._create_table()

    @property
    def enabled(self) -> bool:
        return self.log_dir is not None

    def _create_table(self) -> None:
        self._con.execute('CREATE SCHEMA IF NOT EXISTS "tapdrop"')
        self._con.execute("""
            CREATE TABLE IF NOT EXISTS "tapdrop"."query_log" (
                started_at TIMESTAMPTZ, endpoint VARCHAR, query VARCHAR,
                response_format VARCHAR, maxrec INTEGER, row_count BIGINT,
                elapsed_seconds DOUBLE, status VARCHAR, error VARCHAR, token_hash VARCHAR
            )
        """)

    def record(self, record: QueryRecord) -> None:
        """Append one row. Logging must never break the request it describes."""
        if not self.enabled:
            return
        try:
            with self._lock:
                self._con.execute(
                    'INSERT INTO "tapdrop"."query_log" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    [
                        record.started_at,
                        record.endpoint,
                        record.query,
                        record.response_format,
                        record.maxrec,
                        record.rows,
                        record.elapsed_seconds,
                        record.status,
                        record.error,
                        record.token_hash,
                    ],
                )
                self._unflushed += 1
                if self._unflushed >= FLUSH_EVERY:
                    self._flush_locked()
        except Exception:
            logger.exception("could not record a query in the query log")

    def flush(self) -> None:
        """Write the log out as Parquet. Called on shutdown and every FLUSH_EVERY rows."""
        if not self.enabled:
            return
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        assert self._path is not None  # enabled implies a path
        # COPY is forbidden in a client query by the AST allowlist; this is the
        # server's own statement, over the server's own table.
        self._con.execute(f'COPY "tapdrop"."query_log" TO \'{self._path}\' (FORMAT PARQUET)')
        self._unflushed = 0


#: Column types as TAP_SCHEMA datatypes, in the order the DuckDB table declares
#: them, so the table can be described to clients without a second source of truth.
_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("started_at", "char", "When the request arrived, UTC"),
    ("endpoint", "char", "sync or async"),
    ("query", "char", "The ADQL as submitted"),
    ("response_format", "char", "Requested RESPONSEFORMAT"),
    ("maxrec", "int", "Effective MAXREC after clamping"),
    ("row_count", "long", "Rows returned to the client"),
    ("elapsed_seconds", "double", "Wall-clock time to serve the request"),
    ("status", "char", "ok or error"),
    ("error", "char", "Error message when the request failed"),
    ("token_hash", "char", "Truncated SHA-256 of the token, never the token"),
)


def table_meta() -> TableMeta:
    """Describe ``tapdrop.query_log`` so it is queryable through the service itself.

    It has no ``source_uris``: DuckDB already holds the table, so ``Registry.attach``
    leaves it alone rather than building a view over a file that does not exist.
    """
    return TableMeta(
        schema_name="tapdrop",
        table_name="query_log",
        description="Queries this service has run.",
        columns=tuple(
            ColumnMeta(
                name=name,
                datatype=datatype,
                arraysize="*" if datatype == "char" else None,
                description=description,
            )
            for name, datatype, description in _COLUMNS
        ),
    )
