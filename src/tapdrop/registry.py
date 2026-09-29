"""Table/column model and ``TAP_SCHEMA`` population.

This is the fixed interface between discovery (this milestone) and the ADQL
translator/engine (built in parallel against this shape): ``ColumnMeta``,
``TableMeta``, and ``Registry`` are load-bearing and must not be renamed.

``Registry.attach`` is the part that matters most: it creates one DuckDB
schema per TAP schema and one VIEW per table, wrapping ``read_parquet``/
``read_csv``/a registered Arrow scan. After ``attach()``, ``SELECT * FROM
cats.gaia`` works and no file path appears anywhere in a query the ADQL
translator emits - the AST allowlist forbids that.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

import duckdb

from tapdrop.errors import UnknownTableError
from tapdrop.sources import SkippedFile

__all__ = [
    "ColumnMeta",
    "Registry",
    "SkippedFile",
    "TableMeta",
    "tap_schema_metas",
]


@dataclass(frozen=True)
class ColumnMeta:
    name: str
    datatype: str  # TAP_SCHEMA datatype: "char","short","int","long","float","double","boolean"
    unit: str | None = None
    ucd: str | None = None
    description: str | None = None
    arraysize: str | None = None
    principal: bool = False
    indexed: bool = False
    std: bool = False
    utype: str | None = None  # data-model path, e.g. "obscore:Char.SpatialAxis.numBins1"
    xtype: str | None = None  # DALI/ADQL extended type, e.g. "adql:REGION"


@dataclass(frozen=True)
class TableMeta:
    schema_name: str
    table_name: str  # bare name, no schema prefix
    columns: tuple[ColumnMeta, ...]
    ra_column: str | None = None
    dec_column: str | None = None
    description: str | None = None
    source_uris: tuple[str, ...] = ()
    hats_order: int | None = None  # HATS partition order; None when not HATS
    # Per-shard (order, pixel) HEALPix nested address, aligned index-for-index
    # with `source_uris`; None when not HATS or the scheme could not be parsed
    # from every shard's path (M4 pruning; see discovery/hats.py:prune_files).
    hats_pixels: tuple[tuple[int, int], ...] | None = None
    fits_hdu: int | None = None  # explicit HDU index; set only for a multi-BINTABLE split
    primary_key: str | None = None
    # Provenance for the RA/Dec guess (RDD.md: "never upgrade a guess to a fact").
    ra_dec_rule: str | None = None  # "ucd" | "name" | "unit_sanity" | None
    ra_dec_confidence: str | None = None  # "high" | "medium" | "low" | None
    unresolved: tuple[str, ...] = field(default_factory=tuple)

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.table_name}"


# TAP_SCHEMA datatypes that carry a length/precision in TAP_SCHEMA.columns.size.
_SIZED_DATATYPES = {"char"}

_TAP_SCHEMA = "TAP_SCHEMA"

# AIDEV-NOTE: TAP 1.1 §4 requires TAP_SCHEMA itself to be queryable through
# ADQL and described in TAP_SCHEMA.tables/columns, so these five tables are
# registered like any service-provided table (no source_uris; DuckDB holds the
# rows). The translator's allowlist is untouched: it still only admits names
# in ``Registry.tables``, these are just in it. Column order and types must
# match the CREATE TABLE statements in ``populate_tap_schema`` exactly - a
# test compares the two.
# (name, datatype, description)
_TAP_SCHEMA_COLUMNS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "schemas": (
        ("schema_name", "char", "Fully qualified schema name"),
        ("description", "char", "Brief description of the schema"),
        ("utype", "char", "UTYPE if schema corresponds to a data model"),
        ("schema_index", "int", "Suggested display order"),
    ),
    "tables": (
        ("schema_name", "char", "Fully qualified schema name"),
        ("table_name", "char", "Fully qualified table name"),
        ("table_type", "char", "One of: table, view"),
        ("description", "char", "Brief description of the table"),
        ("utype", "char", "UTYPE if table corresponds to a data model"),
        ("table_index", "int", "Suggested display order"),
    ),
    "columns": (
        ("table_name", "char", "Fully qualified table name"),
        ("column_name", "char", "Column name"),
        ("description", "char", "Brief description of the column"),
        ("unit", "char", "Unit in VO standard format"),
        ("ucd", "char", "UCD of the column"),
        ("utype", "char", "UTYPE of the column if any"),
        ("datatype", "char", "ADQL datatype as in section 2.5"),
        ("arraysize", "char", "Length of variable length datatypes"),
        ("xtype", "char", "DALI extended type"),
        ("size", "int", "Deprecated: use arraysize"),
        ("principal", "int", "1 for a principal column, 0 otherwise"),
        ("indexed", "int", "1 for an indexed column, 0 otherwise"),
        ("std", "int", "1 for a column defined by a standard, 0 otherwise"),
        ("column_index", "int", "Suggested display order"),
    ),
    "keys": (
        ("key_id", "char", "Unique key identifier"),
        ("from_table", "char", "Fully qualified table name"),
        ("target_table", "char", "Fully qualified table name"),
        ("description", "char", "Description of this key"),
        ("utype", "char", "UTYPE of this key"),
    ),
    "key_columns": (
        ("key_id", "char", "Key identifier from TAP_SCHEMA.keys"),
        ("from_column", "char", "Key column name in the from_table"),
        ("target_column", "char", "Key column name in the target_table"),
    ),
}


def tap_schema_metas() -> dict[str, TableMeta]:
    """Describe the five standard ``TAP_SCHEMA`` tables (TAP 1.1 §4).

    Keyed by lowercase qualified name, the same shape ``caom_lite.table_metas``
    returns, so ``Registry.tables.update`` merges it.
    """
    metas: dict[str, TableMeta] = {}
    for table_name, columns in _TAP_SCHEMA_COLUMNS.items():
        meta = TableMeta(
            schema_name=_TAP_SCHEMA,
            table_name=table_name,
            description=f"TAP 1.1 metadata table TAP_SCHEMA.{table_name}.",
            columns=tuple(
                ColumnMeta(
                    name=name,
                    datatype=datatype,
                    arraysize="*" if datatype == "char" else None,
                    description=description,
                    std=True,
                )
                for name, datatype, description in columns
            ),
        )
        metas[meta.qualified_name.lower()] = meta
    return metas


class Registry:
    """Discovered tables, keyed by lowercase qualified name."""

    def __init__(self, tables: dict[str, TableMeta], skipped: tuple[SkippedFile, ...] = ()) -> None:
        self.tables = tables
        self.skipped = skipped
        self._arrow_cache: dict[str, object] = {}

    def get(self, qualified_name: str) -> TableMeta:
        key = qualified_name.lower()
        try:
            return self.tables[key]
        except KeyError:
            raise UnknownTableError(qualified_name, self.close_matches(qualified_name)) from None

    def close_matches(self, name: str) -> list[str]:
        return difflib.get_close_matches(name.lower(), self.tables.keys(), n=3)

    def attach(self, con: duckdb.DuckDBPyConnection) -> None:
        """Create one schema per TAP schema and one view per table."""
        for schema in sorted({meta.schema_name for meta in self.tables.values()}):
            con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

        for meta in self.tables.values():
            if not meta.source_uris:
                # A service-provided table (tapdrop.query_log): DuckDB already
                # holds it, there is no file to wrap, and a view over it would
                # collide with its own name.
                continue
            select_sql = self._select_sql(con, meta)
            view_name = f'"{meta.schema_name}"."{meta.table_name}"'
            con.execute(f"CREATE OR REPLACE VIEW {view_name} AS {select_sql}")

    def _select_sql(self, con: duckdb.DuckDBPyConnection, meta: TableMeta) -> str:
        from tapdrop.discovery import catalog  # local import: avoids a cycle with discovery

        fmt = catalog.detect_format(meta.source_uris[0]) if meta.source_uris else None
        if fmt == "parquet":
            files = _sql_string_list(meta.source_uris)
            return f"SELECT * FROM read_parquet({files}, hive_partitioning=false)"
        if fmt in ("csv", "tsv"):
            delim = "\t" if fmt == "tsv" else ","
            return (
                f"SELECT * FROM read_csv({_sql_string_list(meta.source_uris)}, "
                f"delim='{delim}', header=true, hive_partitioning=false)"
            )
        if fmt in ("fits", "ecsv", "votable"):
            arrow_table = catalog.read_arrow_table(meta.source_uris, fmt, meta.fits_hdu)
            internal_name = f"_tapdrop_raw_{meta.schema_name}_{meta.table_name}"
            self._arrow_cache[internal_name] = arrow_table  # keep alive for con's lifetime
            con.register(internal_name, arrow_table)
            return f"SELECT * FROM {internal_name}"
        raise ValueError(f"cannot build a view for table {meta.qualified_name!r}: unknown format")

    def populate_tap_schema(self, con: duckdb.DuckDBPyConnection) -> None:
        """Create and fill ``TAP_SCHEMA.schemas/tables/columns/keys/key_columns``.

        The TAP_SCHEMA tables describe themselves too (TAP 1.1 §4), which is
        what makes ``SELECT * FROM TAP_SCHEMA.tables`` pass the allowlist.
        """
        self.tables.update(tap_schema_metas())
        con.execute('CREATE SCHEMA IF NOT EXISTS "TAP_SCHEMA"')
        con.execute("""
            CREATE OR REPLACE TABLE "TAP_SCHEMA"."schemas" (
                schema_name VARCHAR, description VARCHAR, utype VARCHAR, schema_index INTEGER
            )
        """)
        con.execute("""
            CREATE OR REPLACE TABLE "TAP_SCHEMA"."tables" (
                schema_name VARCHAR, table_name VARCHAR, table_type VARCHAR,
                description VARCHAR, utype VARCHAR, table_index INTEGER
            )
        """)
        con.execute("""
            CREATE OR REPLACE TABLE "TAP_SCHEMA"."columns" (
                table_name VARCHAR, column_name VARCHAR, description VARCHAR,
                unit VARCHAR, ucd VARCHAR, utype VARCHAR, datatype VARCHAR,
                arraysize VARCHAR, xtype VARCHAR, size INTEGER, principal INTEGER,
                indexed INTEGER, std INTEGER, column_index INTEGER
            )
        """)
        con.execute("""
            CREATE OR REPLACE TABLE "TAP_SCHEMA"."keys" (
                key_id VARCHAR, from_table VARCHAR, target_table VARCHAR,
                description VARCHAR, utype VARCHAR
            )
        """)
        con.execute("""
            CREATE OR REPLACE TABLE "TAP_SCHEMA"."key_columns" (
                key_id VARCHAR, from_column VARCHAR, target_column VARCHAR
            )
        """)

        schemas = sorted({meta.schema_name for meta in self.tables.values()})
        con.executemany(
            'INSERT INTO "TAP_SCHEMA"."schemas" VALUES (?, ?, ?, ?)',
            [(schema, None, None, None) for schema in schemas],
        )

        table_rows = []
        column_rows = []
        for meta in self.tables.values():
            table_rows.append(
                (meta.schema_name, meta.qualified_name, "table", meta.description, None, None)
            )
            for index, column in enumerate(meta.columns):
                principal = column.principal or column.name in (meta.ra_column, meta.dec_column)
                column_rows.append(
                    (
                        meta.qualified_name,
                        column.name,
                        column.description,
                        column.unit,
                        column.ucd,
                        column.utype,
                        column.datatype,
                        column.arraysize,
                        column.xtype,
                        _declared_size(column),
                        int(principal),
                        int(column.indexed),
                        int(column.std),
                        index,
                    )
                )
        con.executemany('INSERT INTO "TAP_SCHEMA"."tables" VALUES (?, ?, ?, ?, ?, ?)', table_rows)
        con.executemany(
            'INSERT INTO "TAP_SCHEMA"."columns" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            column_rows,
        )


def _declared_size(column: ColumnMeta) -> int | None:
    """TAP_SCHEMA.columns.size: the length ``arraysize`` encodes, as a number.

    Only fixed-length character columns have one. ``"*"`` and multi-dimensional
    forms such as ``"3x4"`` have no single length, so they report NULL rather
    than a number that would misdescribe the column.
    """
    if column.datatype not in _SIZED_DATATYPES or not column.arraysize:
        return None
    return int(column.arraysize) if column.arraysize.isdigit() else None


def _sql_string_list(values: tuple[str, ...]) -> str:
    escaped = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"[{escaped}]"
