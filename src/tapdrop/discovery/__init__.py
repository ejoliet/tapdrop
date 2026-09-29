"""File discovery: sources -> registered tables.

``catalog.py`` holds the per-format readers and RA/Dec detection, ``hats.py``
holds HATS partition detection. This module wires them together into a
:class:`~tapdrop.registry.Registry`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from tapdrop.registry import Registry

__all__ = ["discover"]


def discover(source_uris: list[str], config_path: Path | None = None) -> Registry:
    """Resolve ``source_uris`` and read them into a populated :class:`Registry`.

    A file that fails to read never aborts discovery (RDD.md "Error handling");
    it is recorded in :attr:`Registry.skipped` instead.

    Imports are local to break the natural ``sources -> discovery.hats ->
    discovery (this package) -> catalog -> registry -> sources`` import cycle:
    Python initializes ``discovery/__init__.py`` before any submodule, so a
    module-level import here of ``catalog``/``registry`` would run while
    ``sources`` is still mid-import.
    """
    from tapdrop.discovery import catalog, hats
    from tapdrop.registry import Registry, TableMeta
    from tapdrop.sources import SkippedFile, resolve_sources

    overrides = catalog.load_overrides(config_path) if config_path else {}

    groups, skipped_from_sources = resolve_sources(source_uris)
    skipped: list[SkippedFile] = list(skipped_from_sources)
    tables: dict[str, TableMeta] = {}

    con = duckdb.connect()
    try:
        for group in groups:
            table_overrides = overrides.get(f"{group.schema_name}.{group.table_name}", {})
            if group.is_hats:
                metas, group_skipped = hats.discover_hats_table(con, group, table_overrides)
            else:
                metas, group_skipped = catalog.discover_table(con, group, table_overrides)
            skipped.extend(group_skipped)
            for meta in metas:
                key = meta.qualified_name.lower()
                if key in tables:
                    skipped.append(
                        SkippedFile(
                            ", ".join(meta.source_uris),
                            f"table name collision on '{meta.qualified_name}'",
                        )
                    )
                    continue
                tables[key] = meta
    finally:
        con.close()

    return Registry(tables, tuple(skipped))
