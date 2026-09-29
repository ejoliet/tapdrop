"""TAP ``UPLOAD``: a client table that exists for exactly one query.

TAP 1.1 §2.5.2 lets a client send its own table alongside the query and join
against it as ``tap_upload.<name>``. Unlike the drag-drop ingest in ``ui.py``,
which adds a table to the service for everyone, an uploaded table is torn down
as soon as the query that used it has been answered.

Sync only, per RDD.md's ADQL support matrix ("TAP UPLOAD (``tap_upload.*``):
sync, size-capped").
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from tapdrop.discovery import discover
from tapdrop.errors import InvalidParameterError, UploadTooLargeError
from tapdrop.registry import Registry, TableMeta

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator, Mapping

    import duckdb

    from tapdrop.config import Settings

__all__ = ["UPLOAD_SCHEMA", "uploaded_tables"]

#: The schema name TAP fixes for uploaded tables.
UPLOAD_SCHEMA = "tap_upload"

#: TAP 1.1 §2.5.2 sends the table itself as a multipart part named by the
#: ``param:`` half of the UPLOAD value.
_INLINE_PREFIX = "param:"


@contextmanager
def uploaded_tables(
    specs: tuple[tuple[str, str], ...],
    parts: Mapping[str, bytes],
    con: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
) -> Iterator[Registry]:
    """Make *specs* queryable as ``tap_upload.<name>`` for the duration of the block.

    Yields a registry that is the served one plus the uploaded tables, so the
    translator validates a query against both without the uploads ever becoming
    part of the service's own metadata.
    """
    if not specs:
        yield registry
        return

    directory = Path(tempfile.mkdtemp(prefix="tapdrop-upload-"))
    uploaded = Registry({})
    try:
        tables = {}
        for name, uri in specs:
            meta = _read_one(name, uri, parts, directory, settings)
            tables[meta.qualified_name] = meta
        uploaded = Registry(tables)
        uploaded.attach(con)
        yield Registry({**registry.tables, **tables}, registry.skipped)
    finally:
        for meta in uploaded.tables.values():
            con.execute(f'DROP VIEW IF EXISTS "{meta.schema_name}"."{meta.table_name}"')
        shutil.rmtree(directory, ignore_errors=True)


def _read_one(
    name: str,
    uri: str,
    parts: Mapping[str, bytes],
    directory: Path,
    settings: Settings,
) -> TableMeta:
    if not uri.startswith(_INLINE_PREFIX):
        # ponytail: a client may also name an http(s) URI here. Nobody has asked
        # for it, and fetching a caller-supplied URL from inside a query is a
        # request-forgery surface that would need its own allowlist; add it with
        # one when a real client needs it.
        raise InvalidParameterError(
            "UPLOAD", f"{uri!r} is not supported; send the table inline as param:<part-name>"
        )

    part_name = uri[len(_INLINE_PREFIX) :]
    if part_name not in parts:
        raise InvalidParameterError("UPLOAD", f"no multipart part named {part_name!r}")

    payload = parts[part_name]
    if len(payload) > settings.upload_max_mb * 1024 * 1024:
        raise UploadTooLargeError(settings.upload_max_mb)

    # TAP uploads are VOTable, and the part carries no filename to infer from.
    path = directory / f"{name}.vot"
    path.write_bytes(payload)

    discovered = discover([str(path)])
    if not discovered.tables:
        reasons = "; ".join(skipped.reason for skipped in discovered.skipped)
        raise InvalidParameterError("UPLOAD", f"cannot read table {name!r}: {reasons}")

    meta = next(iter(discovered.tables.values()))
    return replace(meta, schema_name=UPLOAD_SCHEMA, table_name=name.lower())
