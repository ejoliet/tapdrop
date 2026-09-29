"""Per-format readers, RA/Dec + UCD detection.

Parquet and CSV/TSV are read straight through DuckDB (schema via ``DESCRIBE``,
value ranges via ``min``/``max``) so file paths stay server-side and never
touch a query the ADQL translator emits. FITS, ECSV, and VOTable go through
astropy and are converted to Arrow for DuckDB registration.

RA/Dec detection follows RDD.md "Discovery rules (v1, catalogs)" in order:
UCD, then name list, then unit-range sanity. Every candidate - however it was
found - is sanity-checked against its numeric range before being accepted;
a name match with values outside [0, 360]/[-90, 90] is a mislabeled column,
not a hit, and detection falls through to the next rule rather than
publishing a guess as a fact.
"""

from __future__ import annotations

import dataclasses
import io
import posixpath
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb
import fsspec
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from astropy.io import fits
from astropy.table import Table, vstack

from tapdrop.errors import DiscoveryError
from tapdrop.registry import ColumnMeta, TableMeta
from tapdrop.sources import SkippedFile, SourceGroup

# AIDEV-NOTE: UCD match is case-sensitive per the UCD1+ vocabulary in principle,
# but real catalogs are inconsistent about casing, so compare lowercase.
_RA_UCD = "pos.eq.ra;meta.main"
_DEC_UCD = "pos.eq.dec;meta.main"
_RA_NAMES = ("ra", "ra_icrs", "raj2000", "ra_deg", "alpha", "s_ra")
_DEC_NAMES = ("dec", "dec_icrs", "dej2000", "dec_deg", "delta", "s_dec")

_DUCKDB_TYPE_MAP = {
    "BOOLEAN": "boolean",
    "TINYINT": "short",
    "SMALLINT": "short",
    "UTINYINT": "short",
    "USMALLINT": "short",
    "INTEGER": "int",
    "UINTEGER": "int",
    "BIGINT": "long",
    "UBIGINT": "long",
    "HUGEINT": "long",
    "REAL": "float",
    "FLOAT": "float",
    "DOUBLE": "double",
    "DECIMAL": "double",
    "VARCHAR": "char",
    "BLOB": "char",
}

RangeFn = Callable[[str], "tuple[float | None, float | None]"]


def detect_format(path: str) -> str | None:
    """One of the six v1 formats, or ``None`` when unrecognized."""
    lower = path.lower()
    if lower.endswith(".fits.gz") or lower.endswith(".fits") or lower.endswith(".fit"):
        return "fits"
    if lower.endswith(".parquet"):
        return "parquet"
    if lower.endswith(".csv"):
        return "csv"
    if lower.endswith(".tsv"):
        return "tsv"
    if lower.endswith(".ecsv"):
        return "ecsv"
    if lower.endswith(".vot") or lower.endswith(".xml"):
        return "votable"
    return None


def duckdb_type_to_tap(type_name: str) -> str:
    base = type_name.split("(")[0].strip().upper()
    return _DUCKDB_TYPE_MAP.get(base, "char")


def numpy_dtype_to_tap(dtype: np.dtype) -> str:
    kind = dtype.kind
    if kind == "b":
        return "boolean"
    if kind in ("i", "u"):
        if dtype.itemsize <= 2:
            return "short"
        return "int" if dtype.itemsize <= 4 else "long"
    if kind == "f":
        return "float" if dtype.itemsize <= 4 else "double"
    return "char"


@dataclass(frozen=True)
class RaDecGuess:
    ra_column: str | None = None
    dec_column: str | None = None
    rule: str | None = None  # "ucd" | "name" | "unit_sanity" | None
    confidence: str | None = None  # "high" | "medium" | "low" | None


def _pair_sane(range_fn: RangeFn, ra_col: str, dec_col: str) -> bool:
    ra_min, ra_max = range_fn(ra_col)
    dec_min, dec_max = range_fn(dec_col)
    if ra_min is None or ra_max is None or dec_min is None or dec_max is None:
        return False
    ra_ok = 0.0 <= ra_min <= 360.0 and 0.0 <= ra_max <= 360.0
    dec_ok = -90.0 <= dec_min <= 90.0 and -90.0 <= dec_max <= 90.0
    return ra_ok and dec_ok


def detect_ra_dec(columns: Sequence[ColumnMeta], range_fn: RangeFn) -> RaDecGuess:
    by_name = {c.name.lower(): c.name for c in columns}

    ra_ucd = next((c.name for c in columns if (c.ucd or "").lower() == _RA_UCD), None)
    dec_ucd = next((c.name for c in columns if (c.ucd or "").lower() == _DEC_UCD), None)
    if ra_ucd and dec_ucd and _pair_sane(range_fn, ra_ucd, dec_ucd):
        return RaDecGuess(ra_ucd, dec_ucd, "ucd", "high")

    ra_name = next((by_name[n] for n in _RA_NAMES if n in by_name), None)
    dec_name = next((by_name[n] for n in _DEC_NAMES if n in by_name), None)
    if ra_name and dec_name and _pair_sane(range_fn, ra_name, dec_name):
        # DEVIATION: RDD.md does not assign confidence levels to catalog RA/Dec
        # rules (only images have a worked example). UCD is an explicit,
        # provider-supplied hint -> high; name matching is a heuristic -> medium;
        # a pure numeric-range guess -> low. See implementation-notes.md.
        return RaDecGuess(ra_name, dec_name, "name", "medium")

    numeric_names = [c.name for c in columns if c.datatype in ("float", "double")]
    for ra_candidate in numeric_names:
        ra_min, ra_max = range_fn(ra_candidate)
        if ra_min is None or ra_max is None or not (ra_min >= 0.0 and ra_max <= 360.0):
            continue
        for dec_candidate in numeric_names:
            if dec_candidate == ra_candidate:
                continue
            dec_min, dec_max = range_fn(dec_candidate)
            if dec_min is None or dec_max is None or not (dec_min >= -90.0 and dec_max <= 90.0):
                continue
            return RaDecGuess(ra_candidate, dec_candidate, "unit_sanity", "low")

    return RaDecGuess()


# --------------------------------------------------------------------------
# DuckDB-backed formats: Parquet, CSV, TSV
# --------------------------------------------------------------------------


def _sql_list(files: Sequence[str]) -> str:
    return "[" + ", ".join("'" + f.replace("'", "''") + "'" for f in files) + "]"


def duckdb_source_expr(files: Sequence[str], fmt: str, delim: str = ",") -> str:
    # hive_partitioning=false: a directory named "Norder=0" is HATS partitioning,
    # not a data column - don't let DuckDB infer one from the path.
    if fmt == "parquet":
        return f"read_parquet({_sql_list(files)}, hive_partitioning=false)"
    return f"read_csv({_sql_list(files)}, delim='{delim}', header=true, hive_partitioning=false)"


def _describe(con: duckdb.DuckDBPyConnection, source_expr: str) -> list[tuple[str, str]]:
    rows = con.execute(f"DESCRIBE SELECT * FROM {source_expr}").fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def duckdb_range_fn(con: duckdb.DuckDBPyConnection, source_expr: str) -> RangeFn:
    def fn(col: str) -> tuple[float | None, float | None]:
        try:
            row = con.execute(f'SELECT min("{col}"), max("{col}") FROM {source_expr}').fetchone()
        except duckdb.Error:
            return (None, None)
        if row is None or row[0] is None or row[1] is None:
            return (None, None)
        try:
            return (float(row[0]), float(row[1]))
        except (TypeError, ValueError):
            return (None, None)

    return fn


def _parquet_field_metadata(file_uri: str) -> dict[str, dict[str, str]]:
    """Unit/UCD/description carried as Arrow field metadata (our own convention)."""
    fs, path = fsspec.core.url_to_fs(file_uri)
    try:
        with fs.open(path, "rb") as fh:
            schema = pq.ParquetFile(fh).schema_arrow
    except (OSError, pa.ArrowException):
        return {}
    result: dict[str, dict[str, str]] = {}
    for field in schema:
        if not field.metadata:
            continue
        raw = {k.decode(): v.decode() for k, v in field.metadata.items()}
        wanted = {k: raw[k] for k in ("unit", "ucd", "description") if raw.get(k)}
        if wanted:
            result[field.name] = wanted
    return result


def _sidecar_metadata(file_uri: str, table_name: str) -> dict[str, object]:
    """``<stem>.meta.yaml`` next to the Parquet file(s), read second (after Arrow metadata)."""
    fs, path = fsspec.core.url_to_fs(file_uri)
    sidecar = posixpath.join(posixpath.dirname(path), f"{table_name}.meta.yaml")
    try:
        if not fs.exists(sidecar):
            return {}
        with fs.open(sidecar, "r") as fh:
            data = yaml.safe_load(fh)
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


def discover_parquet(
    con: duckdb.DuckDBPyConnection, group: SourceGroup, overrides: dict[str, object]
) -> tuple[list[TableMeta], list[SkippedFile]]:
    source_expr = duckdb_source_expr(group.files, "parquet")
    try:
        described = _describe(con, source_expr)
    except duckdb.Error as exc:
        return [], [SkippedFile(", ".join(group.files), f"could not read Parquet schema: {exc}")]

    field_meta = _parquet_field_metadata(group.files[0])
    sidecar = _sidecar_metadata(group.files[0], group.table_name)
    sidecar_columns = sidecar.get("columns")
    sidecar_columns = sidecar_columns if isinstance(sidecar_columns, dict) else {}

    columns = []
    for name, duck_type in described:
        fm = field_meta.get(name, {})
        sc = sidecar_columns.get(name)
        sc = sc if isinstance(sc, dict) else {}
        columns.append(
            ColumnMeta(
                name=name,
                datatype=duckdb_type_to_tap(duck_type),
                unit=fm.get("unit") or sc.get("unit"),
                ucd=fm.get("ucd") or sc.get("ucd"),
                description=fm.get("description") or sc.get("description"),
            )
        )

    guess = detect_ra_dec(columns, duckdb_range_fn(con, source_expr))
    table_description = sidecar.get("description")
    meta = TableMeta(
        schema_name=group.schema_name,
        table_name=group.table_name,
        columns=tuple(columns),
        ra_column=guess.ra_column,
        dec_column=guess.dec_column,
        description=table_description if isinstance(table_description, str) else None,
        source_uris=group.files,
        ra_dec_rule=guess.rule,
        ra_dec_confidence=guess.confidence,
        unresolved=() if guess.ra_column else ("ra", "dec"),
    )
    return [_apply_overrides(meta, overrides)], []


def discover_csv(
    con: duckdb.DuckDBPyConnection, group: SourceGroup, overrides: dict[str, object], fmt: str
) -> tuple[list[TableMeta], list[SkippedFile]]:
    delim = "\t" if fmt == "tsv" else ","
    source_expr = duckdb_source_expr(group.files, fmt, delim)
    try:
        described = _describe(con, source_expr)
    except duckdb.Error as exc:
        reason = f"could not read {fmt.upper()} schema: {exc}"
        return [], [SkippedFile(", ".join(group.files), reason)]

    columns = [ColumnMeta(name=n, datatype=duckdb_type_to_tap(t)) for n, t in described]
    guess = detect_ra_dec(columns, duckdb_range_fn(con, source_expr))
    meta = TableMeta(
        schema_name=group.schema_name,
        table_name=group.table_name,
        columns=tuple(columns),
        ra_column=guess.ra_column,
        dec_column=guess.dec_column,
        source_uris=group.files,
        ra_dec_rule=guess.rule,
        ra_dec_confidence=guess.confidence,
        unresolved=() if guess.ra_column else ("ra", "dec"),
    )
    return [_apply_overrides(meta, overrides)], []


# --------------------------------------------------------------------------
# astropy-backed formats: FITS, ECSV, VOTable
# --------------------------------------------------------------------------


def _read_ecsv(uri: str) -> Table:
    fs, path = fsspec.core.url_to_fs(uri)
    with fs.open(path, "r") as fh:
        text = fh.read()
    # AIDEV-NOTE: Table.read rejects a StringIO for ascii.ecsv ("must be a
    # string or an iterable"); a plain str works, verified against astropy 6.
    return Table.read(text, format="ascii.ecsv")


def _read_votable(uri: str) -> Table:
    fs, path = fsspec.core.url_to_fs(uri)
    with fs.open(path, "rb") as fh:
        data = fh.read()
    return Table.read(io.BytesIO(data), format="votable")


def _read_fits_bintables(uri: str) -> list[tuple[int, Table, dict[str, str]]]:
    """Every BINTABLE HDU in ``uri`` as ``(hdu_index, table, ucd_by_column)``."""
    fs, path = fsspec.core.url_to_fs(uri)
    with fs.open(path, "rb") as fh:
        data = fh.read()
    results: list[tuple[int, Table, dict[str, str]]] = []
    with fits.open(io.BytesIO(data)) as hdul:
        for idx, hdu in enumerate(hdul):
            if not isinstance(hdu, fits.BinTableHDU):
                continue
            table = Table.read(io.BytesIO(data), format="fits", hdu=idx)
            ucd_by_column: dict[str, str] = {}
            for i, colname in enumerate(hdu.columns.names, start=1):
                ucd = hdu.header.get(f"TUCD{i}")
                if ucd:
                    ucd_by_column[colname] = str(ucd)
            results.append((idx, table, ucd_by_column))
    return results


def _columns_from_table(
    table: Table, ucd_by_column: dict[str, str] | None = None
) -> list[ColumnMeta]:
    ucd_by_column = ucd_by_column or {}
    columns = []
    for name in table.colnames:
        col = table[name]
        unit = str(col.unit) if col.unit is not None else None
        description = col.description or None
        ucd = ucd_by_column.get(name)
        if ucd is None and hasattr(col, "meta"):
            meta_ucd = col.meta.get("ucd") if isinstance(col.meta, dict) else None
            ucd = str(meta_ucd) if meta_ucd else None
        arraysize = "x".join(str(d) for d in col.shape[1:]) if col.ndim > 1 else None
        columns.append(
            ColumnMeta(
                name=name,
                datatype=numpy_dtype_to_tap(col.dtype),
                unit=unit,
                ucd=ucd,
                description=str(description) if description else None,
                arraysize=arraysize,
            )
        )
    return columns


def table_range_fn(table: Table) -> RangeFn:
    def fn(col: str) -> tuple[float | None, float | None]:
        try:
            arr = np.asarray(table[col], dtype=float)
        except (TypeError, ValueError):
            return (None, None)
        arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            return (None, None)
        return (float(arr.min()), float(arr.max()))

    return fn


def discover_astropy(
    group: SourceGroup, overrides: dict[str, object], fmt: str
) -> tuple[list[TableMeta], list[SkippedFile]]:
    if fmt == "fits" and len(group.files) == 1:
        bintables = _read_fits_bintables(group.files[0])
        if not bintables:
            return [], [SkippedFile(group.files[0], "no BINTABLE HDU found")]
        multi = len(bintables) > 1
        metas = []
        for idx, table, ucd_by_column in bintables:
            name = f"{group.table_name}_hdu{idx}" if multi else group.table_name
            columns = _columns_from_table(table, ucd_by_column)
            guess = detect_ra_dec(columns, table_range_fn(table))
            meta = TableMeta(
                schema_name=group.schema_name,
                table_name=name,
                columns=tuple(columns),
                ra_column=guess.ra_column,
                dec_column=guess.dec_column,
                source_uris=group.files,
                fits_hdu=idx if multi else None,
                ra_dec_rule=guess.rule,
                ra_dec_confidence=guess.confidence,
                unresolved=() if guess.ra_column else ("ra", "dec"),
            )
            # Per-table overrides only make sense for the single-table case;
            # a multi-HDU split has no single qualified name to key overrides on.
            metas.append(_apply_overrides(meta, {} if multi else overrides))
        return metas, []

    tables: list[Table] = []
    for uri in group.files:
        if fmt == "fits":
            bintables = _read_fits_bintables(uri)
            if not bintables:
                raise DiscoveryError(f"no BINTABLE HDU found in {uri}")
            tables.append(bintables[0][1])
        elif fmt == "ecsv":
            tables.append(_read_ecsv(uri))
        else:
            tables.append(_read_votable(uri))

    table = tables[0] if len(tables) == 1 else vstack(tables)
    columns = _columns_from_table(table)
    guess = detect_ra_dec(columns, table_range_fn(table))
    meta = TableMeta(
        schema_name=group.schema_name,
        table_name=group.table_name,
        columns=tuple(columns),
        ra_column=guess.ra_column,
        dec_column=guess.dec_column,
        source_uris=group.files,
        ra_dec_rule=guess.rule,
        ra_dec_confidence=guess.confidence,
        unresolved=() if guess.ra_column else ("ra", "dec"),
    )
    return [_apply_overrides(meta, overrides)], []


def read_arrow_table(source_uris: tuple[str, ...], fmt: str, hdu: int | None = None) -> pa.Table:
    """Re-read FITS/ECSV/VOTable file(s) into Arrow for DuckDB registration.

    Used by :meth:`tapdrop.registry.Registry.attach`. Reads happen again here
    (rather than caching bytes from discovery) to keep the registry itself
    small - files, not row data, are what tapdrop holds onto between runs.
    """
    tables: list[Table] = []
    for uri in source_uris:
        if fmt == "fits":
            if hdu is not None:
                fs, path = fsspec.core.url_to_fs(uri)
                with fs.open(path, "rb") as fh:
                    data = fh.read()
                tables.append(Table.read(io.BytesIO(data), format="fits", hdu=hdu))
            else:
                bintables = _read_fits_bintables(uri)
                if not bintables:
                    raise DiscoveryError(f"no BINTABLE HDU found in {uri}")
                tables.append(bintables[0][1])
        elif fmt == "ecsv":
            tables.append(_read_ecsv(uri))
        elif fmt == "votable":
            tables.append(_read_votable(uri))
        else:
            raise ValueError(f"unsupported format for Arrow conversion: {fmt}")
    table = tables[0] if len(tables) == 1 else vstack(tables)
    return _astropy_table_to_arrow(table)


def _astropy_table_to_arrow(table: Table) -> pa.Table:
    arrays = {name: pa.array(np.asarray(table[name]).tolist()) for name in table.colnames}
    return pa.table(arrays)


# --------------------------------------------------------------------------
# Dispatch, overrides, orchestration
# --------------------------------------------------------------------------


def discover_table(
    con: duckdb.DuckDBPyConnection, group: SourceGroup, overrides: dict[str, object]
) -> tuple[list[TableMeta], list[SkippedFile]]:
    fmt = detect_format(group.files[0])
    if fmt is None:
        return [], [SkippedFile(group.files[0], "unrecognized format")]
    if any(detect_format(f) != fmt for f in group.files[1:]):
        return [], [SkippedFile(", ".join(group.files), "group mixes incompatible file formats")]

    try:
        if fmt == "parquet":
            return discover_parquet(con, group, overrides)
        if fmt in ("csv", "tsv"):
            return discover_csv(con, group, overrides, fmt)
        return discover_astropy(group, overrides, fmt)
    except Exception as exc:
        return [], [SkippedFile(", ".join(group.files), f"discovery failed: {exc}")]


def _apply_overrides(meta: TableMeta, overrides: dict[str, object]) -> TableMeta:
    """``tapdrop.yaml`` ``tables:`` block. Overrides win over detection."""
    if not overrides:
        return meta

    description = overrides.get("description", meta.description)
    ra_column = overrides.get("ra", meta.ra_column)
    dec_column = overrides.get("dec", meta.dec_column)
    primary_key = overrides.get("primary_key", meta.primary_key)

    column_overrides = overrides.get("columns")
    columns = meta.columns
    if isinstance(column_overrides, dict) and column_overrides:
        updated = []
        for col in meta.columns:
            co = column_overrides.get(col.name)
            if isinstance(co, dict):
                col = dataclasses.replace(
                    col,
                    unit=co.get("unit", col.unit),
                    ucd=co.get("ucd", col.ucd),
                    description=co.get("description", col.description),
                )
            updated.append(col)
        columns = tuple(updated)

    ra_dec_overridden = bool(overrides.get("ra") or overrides.get("dec"))
    ra_dec_rule = "override" if ra_dec_overridden else meta.ra_dec_rule
    ra_dec_confidence = "override" if ra_dec_overridden else meta.ra_dec_confidence
    unresolved = () if (ra_column and dec_column) else meta.unresolved

    return dataclasses.replace(
        meta,
        description=description if isinstance(description, str) else meta.description,
        ra_column=ra_column if isinstance(ra_column, str) else meta.ra_column,
        dec_column=dec_column if isinstance(dec_column, str) else meta.dec_column,
        primary_key=primary_key if isinstance(primary_key, str) else meta.primary_key,
        columns=columns,
        ra_dec_rule=ra_dec_rule,
        ra_dec_confidence=ra_dec_confidence,
        unresolved=unresolved,
    )


def load_overrides(config_path: Path) -> dict[str, dict[str, object]]:
    """``tapdrop.yaml``'s ``tables:`` block, keyed by qualified name.

    The ``obscore:`` block (v1.1) is parsed-and-ignored here on purpose - it
    is not an error, it just does nothing until v1.1.
    """
    with open(config_path) as fh:
        data = yaml.safe_load(fh) or {}
    tables = data.get("tables") if isinstance(data, dict) else None
    if not isinstance(tables, dict):
        return {}
    return {k: v for k, v in tables.items() if isinstance(v, dict)}
