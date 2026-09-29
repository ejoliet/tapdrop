"""Result serialisation.

One Arrow table in, one response body out. The query layer never builds bytes
itself, so the ``FORMAT``/``RESPONSEFORMAT`` contract lives in exactly one
place. VOTable is the default and the only format that carries column metadata
(unit, UCD, description), which is why ``ColumnMeta`` is threaded through here
rather than stopping at the registry.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

import numpy as np
import pyarrow as pa
from astropy.io.votable import from_table, tree
from astropy.table import Column, MaskedColumn, Table

from tapdrop.errors import UnsupportedFormatError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tapdrop.registry import ColumnMeta

VOTABLE = "votable"
VOTABLE_TD = "votable/td"
CSV = "csv"
TSV = "tsv"
PARQUET = "parquet"
JSON = "json"

#: Every spelling a client may send, lowercased, mapped to a canonical format.
#: TAP 1.1 allows both short names and media types, and pyvo/TOPCAT use the
#: ``serialization=`` parameter form for the VOTable variants.
FORMAT_ALIASES: dict[str, str] = {
    "votable": VOTABLE,
    "votable/b2": VOTABLE,
    "votable/binary2": VOTABLE,
    "application/x-votable+xml": VOTABLE,
    "application/x-votable+xml;serialization=binary2": VOTABLE,
    "votable/td": VOTABLE_TD,
    "tabledata": VOTABLE_TD,
    "application/x-votable+xml;serialization=tabledata": VOTABLE_TD,
    "csv": CSV,
    "text/csv": CSV,
    "text/csv;header=present": CSV,
    "tsv": TSV,
    "text/tab-separated-values": TSV,
    "parquet": PARQUET,
    "application/x-parquet": PARQUET,
    "application/vnd.apache.parquet": PARQUET,
    "json": JSON,
    "application/json": JSON,
}

CONTENT_TYPES: dict[str, str] = {
    VOTABLE: "application/x-votable+xml",
    VOTABLE_TD: "application/x-votable+xml",
    CSV: "text/csv;header=present",
    TSV: "text/tab-separated-values",
    PARQUET: "application/vnd.apache.parquet",
    JSON: "application/json",
}

_VOTABLE_SERIALIZATION = {VOTABLE: "binary2", VOTABLE_TD: "tabledata"}

_ERROR_DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<VOTABLE version="1.4" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">\n'
    '  <RESOURCE type="results">\n'
    '    <INFO name="QUERY_STATUS" value="ERROR">{message}</INFO>\n'
    "  </RESOURCE>\n"
    "</VOTABLE>\n"
)


def normalize_format(requested: str | None) -> str:
    """Resolve a client ``FORMAT``/``RESPONSEFORMAT`` to a canonical format name."""
    if requested is None or not requested.strip():
        return VOTABLE
    key = "".join(requested.lower().split())
    try:
        return FORMAT_ALIASES[key]
    except KeyError:
        raise UnsupportedFormatError(requested, sorted(set(FORMAT_ALIASES.values()))) from None


def content_type(fmt: str) -> str:
    """Media type to send for a canonical format name."""
    return CONTENT_TYPES[fmt]


def serialize(
    table: pa.Table,
    fmt: str,
    columns: Mapping[str, ColumnMeta] | None = None,
    *,
    overflow: bool = False,
) -> bytes:
    """Render a result table in the requested canonical format.

    ``overflow`` marks a result truncated by MAXREC. Only VOTable can carry that
    flag, which is why TAP clients that need to detect truncation must ask for
    VOTable.
    """
    if fmt in _VOTABLE_SERIALIZATION:
        return _write_votable(table, _VOTABLE_SERIALIZATION[fmt], columns, overflow)
    if fmt == CSV:
        return _write_delimited(table, ",")
    if fmt == TSV:
        return _write_delimited(table, "\t")
    if fmt == PARQUET:
        return _write_parquet(table)
    if fmt == JSON:
        return _write_json(table, columns)
    raise UnsupportedFormatError(fmt, sorted(set(FORMAT_ALIASES.values())))


def votable_error(message: str) -> bytes:
    """Build the TAP error document: a results RESOURCE with QUERY_STATUS=ERROR.

    TAP 1.1 section 2.6 requires this shape even for errors that also carry an
    HTTP status, because clients read the reason from the INFO, not the status.
    """
    return _ERROR_DOCUMENT.format(message=escape(message)).encode("utf-8")


def _write_delimited(table: pa.Table, delimiter: str) -> bytes:
    from pyarrow import csv as pa_csv

    buf = io.BytesIO()
    # Arrow always quotes the header row; TAP clients expect bare column names,
    # so write the header here and let Arrow handle only the data rows.
    buf.write((delimiter.join(table.column_names) + "\n").encode("utf-8"))
    pa_csv.write_csv(
        _stringify_temporal(table),
        buf,
        write_options=pa_csv.WriteOptions(include_header=False, delimiter=delimiter),
    )
    return buf.getvalue()


def _write_parquet(table: pa.Table) -> bytes:
    from pyarrow import parquet as pa_parquet

    buf = io.BytesIO()
    pa_parquet.write_table(table, buf, compression="snappy")
    return buf.getvalue()


def _write_json(table: pa.Table, columns: Mapping[str, ColumnMeta] | None) -> bytes:
    meta = columns or {}
    payload: dict[str, Any] = {
        "metadata": [
            {
                "name": name,
                "datatype": _votable_datatype(table.schema.field(name).type),
                "unit": getattr(meta.get(name), "unit", None),
                "ucd": getattr(meta.get(name), "ucd", None),
                "description": getattr(meta.get(name), "description", None),
            }
            for name in table.column_names
        ],
        "data": [
            [_jsonable(value) for value in row]
            for row in zip(*(col.to_pylist() for col in table.columns), strict=True)
        ]
        if table.num_columns
        else [],
    }
    return json.dumps(payload, allow_nan=False).encode("utf-8")


def _jsonable(value: Any) -> Any:
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None  # JSON has no NaN/Infinity; allow_nan=False would raise
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _write_votable(
    table: pa.Table,
    serialization: str,
    columns: Mapping[str, ColumnMeta] | None,
    overflow: bool,
) -> bytes:
    votable = from_table(_to_astropy(table))
    resource = votable.resources[0]
    resource.type = "results"
    # QUERY_STATUS=OK must be the first child of the results RESOURCE.
    resource.infos.insert(0, tree.Info(name="QUERY_STATUS", value="OK"))

    element = resource.tables[0]
    element.name = "result"
    meta = columns or {}
    for field in element.fields:
        _apply_column_meta(field, table.schema.field(field.name).type, meta.get(field.name))

    buf = io.BytesIO()
    votable.to_xml(buf, tabledata_format=serialization)
    xml = buf.getvalue()
    if overflow:
        # TAP 1.1 puts the OVERFLOW INFO after the table: truncation is only
        # known once the rows are written. astropy always emits resource.infos
        # ahead of the TABLE, so splice this one in directly.
        xml = xml.replace(
            b"</RESOURCE>",
            b'<INFO name="QUERY_STATUS" value="OVERFLOW"/>\n </RESOURCE>',
            1,
        )
    return xml


def _apply_column_meta(field: Any, arrow_type: pa.DataType, meta: ColumnMeta | None) -> None:
    """Fix the datatype astropy inferred and attach the discovered metadata.

    astropy maps numpy dtypes, which loses two things that matter to VO clients:
    booleans become ``bit`` rather than ``boolean``, and strings become a fixed
    ``unicodeChar`` array rather than variable-length ``char``.
    """
    if pa.types.is_boolean(arrow_type):
        field.datatype = "boolean"
        field.arraysize = None
    elif _is_stringlike(arrow_type):
        field.datatype = "char"
        field.arraysize = "*"
    elif pa.types.is_timestamp(arrow_type) or pa.types.is_date(arrow_type):
        field.datatype = "char"
        field.arraysize = "*"
        field.xtype = "timestamp"

    if meta is None:
        return
    if getattr(meta, "unit", None):
        field.unit = meta.unit
    if getattr(meta, "ucd", None):
        field.ucd = meta.ucd
    if getattr(meta, "description", None):
        field.description = meta.description
    if getattr(meta, "utype", None):
        field.utype = meta.utype
    # A declared xtype wins over the timestamp one inferred above: a column that
    # carries its own (ObsCore's ``adql:REGION``) is not a timestamp.
    if getattr(meta, "xtype", None):
        field.xtype = meta.xtype


def _to_astropy(table: pa.Table) -> Table:
    """Convert Arrow to an astropy Table without losing integer or boolean types.

    ``ChunkedArray.to_numpy`` widens a nullable int to float and a nullable bool
    to object, so nulls are filled with a placeholder and carried in the mask
    instead. Strings are the exception: VOTable has no null for ``char``, so a
    null string becomes the empty string.
    """
    return Table([_to_column(name, table[name]) for name in table.column_names])


def _to_column(name: str, chunked: pa.ChunkedArray) -> Column:
    array = chunked.combine_chunks()
    mask = np.asarray(array.is_null()) if array.null_count else None

    if _is_stringlike(array.type):
        values = _as_str_array([v if v is not None else "" for v in array.to_pylist()])
        mask = None  # empty string is the only null a char field can express
    elif pa.types.is_timestamp(array.type) or pa.types.is_date(array.type):
        values = _as_str_array([v.isoformat() if v is not None else "" for v in array.to_pylist()])
        mask = None
    elif pa.types.is_binary(array.type) or pa.types.is_large_binary(array.type):
        values = _as_str_array([v.hex() if v is not None else "" for v in array.to_pylist()])
        mask = None
    else:
        filled = array.fill_null(False) if pa.types.is_boolean(array.type) else array.fill_null(0)
        values = filled.to_numpy(zero_copy_only=False)

    if mask is None:
        return Column(values, name=name)
    return MaskedColumn(values, mask=mask, name=name)


def _as_str_array(values: Sequence[str]) -> np.ndarray[Any, Any]:
    """Unicode array with a non-zero item size, which VOTable arraysize requires."""
    array = np.array(values, dtype=str)
    if array.dtype.itemsize == 0:
        return array.astype("U1")
    return array


def _is_stringlike(arrow_type: pa.DataType) -> bool:
    return bool(
        pa.types.is_string(arrow_type)
        or pa.types.is_large_string(arrow_type)
        or pa.types.is_dictionary(arrow_type)
    )


def _stringify_temporal(table: pa.Table) -> pa.Table:
    """Render timestamps as ISO-8601 for the text formats.

    Arrow's CSV writer emits its own timestamp spelling; DALI wants ISO-8601 in
    every serialisation, so the conversion happens once here.
    """
    columns = []
    changed = False
    for name in table.column_names:
        column = table[name]
        if pa.types.is_timestamp(column.type) or pa.types.is_date(column.type):
            columns.append(
                pa.array([v.isoformat() if v is not None else None for v in column.to_pylist()])
            )
            changed = True
        else:
            columns.append(column)
    return pa.table(dict(zip(table.column_names, columns, strict=True))) if changed else table


def _votable_datatype(arrow_type: pa.DataType) -> str:
    """VOTable datatype name for an Arrow type, used by the JSON metadata block."""
    if pa.types.is_boolean(arrow_type):
        return "boolean"
    if pa.types.is_int16(arrow_type) or pa.types.is_uint8(arrow_type):
        return "short"
    if pa.types.is_int32(arrow_type) or pa.types.is_uint16(arrow_type):
        return "int"
    if pa.types.is_integer(arrow_type):
        return "long"
    if pa.types.is_float32(arrow_type):
        return "float"
    if pa.types.is_floating(arrow_type) or pa.types.is_decimal(arrow_type):
        return "double"
    return "char"


__all__ = [
    "CONTENT_TYPES",
    "CSV",
    "FORMAT_ALIASES",
    "JSON",
    "PARQUET",
    "TSV",
    "VOTABLE",
    "VOTABLE_TD",
    "content_type",
    "normalize_format",
    "serialize",
    "votable_error",
]
