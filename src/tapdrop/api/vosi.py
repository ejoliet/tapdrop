"""VOSI endpoints and the TAPRegExt capabilities document.

These four documents are what a registry, TOPCAT and ``stilts taplint`` read to
decide what the service can do, so they are declarations the rest of the code
has to keep true: an output format listed here must be one ``output.serialize``
produces, and a geometry function listed here must be one the translator
accepts.

The documents are built as strings rather than through ``ElementTree``: the
``xsi:type`` attribute values carry namespace prefixes, which a generic XML
writer will not manage for you. Everything interpolated from discovery is
escaped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, Request, Response

from tapdrop.api import dali_timestamp
from tapdrop.output import CONTENT_TYPES

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tapdrop.config import Settings
    from tapdrop.registry import ColumnMeta, Registry, TableMeta

XML_MEDIA_TYPE = "text/xml"

#: Geometry functions the translator implements. TAPRegExt calls these
#: "adqlgeo" features; declaring one the translator rejects is worse than
#: declaring none, because a client will build a query around it. POLYGON and
#: INTERSECTS are accepted on any table (adql/translate.py: POLYGON inside
#: CONTAINS/INTERSECTS, INTERSECTS between CIRCLE/POLYGON/region values), not
#: only on ivoa.obscore, so they are declared unconditionally.
ADQL_GEOMETRY_FEATURES = (
    "POINT",
    "CIRCLE",
    "POLYGON",
    "CONTAINS",
    "INTERSECTS",
    "DISTANCE",
    "COORD1",
    "COORD2",
    "COORDSYS",
)

_ADQL_DESCRIPTION = (
    "ADQL 2.1, translated to DuckDB SQL. Geometry is ICRS only; "
    "POLYGON and INTERSECTS accept ObsCore s_region; BOX and REGION are not implemented."
)

#: TAP_SCHEMA datatype (as discovery records it) to the VOTable datatype VOSI
#: tables reports. TAPType is the VODataService spelling of the same set.
_VOTABLE_TYPES = {
    "boolean": "boolean",
    "short": "short",
    "int": "int",
    "long": "long",
    "float": "float",
    "double": "double",
    "char": "char",
}

_OUTPUT_FORMATS = (
    (
        "votable",
        "application/x-votable+xml;serialization=BINARY2",
        "ivo://ivoa.net/std/TAPRegExt#output-votable-binary2",
    ),
    ("votable/td", "application/x-votable+xml", "ivo://ivoa.net/std/TAPRegExt#output-votable-td"),
    ("csv", CONTENT_TYPES["csv"], None),
    ("tsv", CONTENT_TYPES["tsv"], None),
    ("parquet", CONTENT_TYPES["parquet"], None),
    ("json", CONTENT_TYPES["json"], None),
)

_STARTED_AT = datetime.now(UTC)


def create_router(settings: Settings, registry: Registry) -> APIRouter:
    """Build the VOSI router. Mounted by ``api.tap.create_app``."""
    router = APIRouter()

    @router.get("/tap/capabilities")
    async def capabilities(request: Request) -> Response:
        return _xml(
            capabilities_document(settings, _base_url(settings, request), _data_models(registry))
        )

    @router.get("/tap/availability")
    async def availability(request: Request) -> Response:
        return _xml(availability_document(settings))

    @router.get("/tap/tables")
    async def tables(request: Request) -> Response:
        return _xml(tableset_document(registry))

    @router.get("/tap/examples")
    async def examples(request: Request) -> Response:
        return Response(
            content=examples_document(registry, _base_url(settings, request)),
            media_type="text/html; charset=utf-8",
        )

    return router


def _xml(body: str) -> Response:
    return Response(content=body.encode("utf-8"), media_type=XML_MEDIA_TYPE)


def _data_models(registry: Registry) -> tuple[tuple[str, str], ...]:
    """The IVOA data models this instance serves, as ``(ivo-id, name)`` pairs."""
    if "ivoa.obscore" in registry.tables:
        return (("ivo://ivoa.net/std/ObsCore#core-1.1", "ObsCore-1.1"),)
    return ()


def _base_url(settings: Settings, request: Request) -> str:
    """Absolute service base, including the secret token prefix.

    ``TAPDROP_PUBLIC_URL`` wins when set: behind a tunnel the request the server
    sees is the tunnel's, not the one the client used, and every URL in these
    documents is one a client will follow.
    """
    if settings.public_url:
        return settings.public_url.rstrip("/") + settings.root_path
    return str(request.base_url).rstrip("/") + settings.root_path


def capabilities_document(
    settings: Settings, base_url: str, data_models: tuple[tuple[str, str], ...] = ()
) -> str:
    """VOSI capabilities with the TAP capability described in TAPRegExt.

    ``data_models`` is ``(ivo-id, name)`` pairs for the IVOA data models this
    instance actually serves - only ObsCore, and only when images were scanned
    (RDD.md M9). Advertising a model whose tables are absent would send a
    client to query something that is not there.
    """
    tap_base = f"{base_url}/tap"
    models = "\n".join(
        f'    <dataModel ivo-id="{escape(ivo_id)}">{escape(name)}</dataModel>'
        for ivo_id, name in data_models
    )
    geometry = "\n".join(
        f"        <feature><form>{name}</form></feature>" for name in ADQL_GEOMETRY_FEATURES
    )
    formats = "\n".join(
        _output_format(alias, mime, ivo_id) for alias, mime, ivo_id in _OUTPUT_FORMATS
    )
    # Only inline: api/tapupload.py refuses a URI upload, so advertising
    # upload-http would promise a method a client would then be refused.
    upload = (
        '    <uploadMethod ivo-id="ivo://ivoa.net/std/TAPRegExt#upload-inline"/>'
        if settings.allow_upload
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<vosi:capabilities
    xmlns:vosi="http://www.ivoa.net/xml/VOSICapabilities/v1.0"
    xmlns:vod="http://www.ivoa.net/xml/VODataService/v1.1"
    xmlns:tr="http://www.ivoa.net/xml/TAPRegExt/v1.0"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
{_vosi_capability("capabilities", f"{tap_base}/capabilities")}
{_vosi_capability("availability", f"{tap_base}/availability")}
{_vosi_capability("tables", f"{tap_base}/tables")}
  <capability standardID="ivo://ivoa.net/std/TAP" xsi:type="tr:TableAccess">
    <interface xsi:type="vod:ParamHTTP" role="std" version="1.1">
      <accessURL use="base">{escape(tap_base)}</accessURL>
    </interface>
{models}
    <language>
      <name>ADQL</name>
      <version ivo-id="ivo://ivoa.net/std/ADQL#v2.1">2.1</version>
      <description>{_ADQL_DESCRIPTION}</description>
      <languageFeatures type="ivo://ivoa.net/std/TAPRegExt#features-adqlgeo">
{geometry}
      </languageFeatures>
    </language>
{formats}
{upload}
    <retentionPeriod>
      <default>{settings.async_query_timeout}</default>
    </retentionPeriod>
    <executionDuration>
      <default>{settings.query_timeout}</default>
      <hard>{settings.async_query_timeout}</hard>
    </executionDuration>
    <outputLimit>
      <default unit="row">{settings.max_rows}</default>
      <hard unit="row">{settings.hard_max_rows}</hard>
    </outputLimit>
    <uploadLimit>
      <hard unit="byte">{settings.upload_max_mb * 1024 * 1024}</hard>
    </uploadLimit>
  </capability>
</vosi:capabilities>
"""


def _vosi_capability(kind: str, url: str) -> str:
    return (
        f'  <capability standardID="ivo://ivoa.net/std/VOSI#{kind}">\n'
        f'    <interface xsi:type="vod:ParamHTTP" role="std">\n'
        f'      <accessURL use="full">{escape(url)}</accessURL>\n'
        f"    </interface>\n"
        f"  </capability>"
    )


def _output_format(alias: str, mime: str, ivo_id: str | None) -> str:
    attr = f' ivo-id="{ivo_id}"' if ivo_id else ""
    return (
        f"    <outputFormat{attr}>\n"
        f"      <mime>{escape(mime)}</mime>\n"
        f"      <alias>{escape(alias)}</alias>\n"
        f"    </outputFormat>"
    )


def availability_document(settings: Settings, now: datetime | None = None) -> str:
    """VOSI availability. ``downAt`` is the TTL expiry, which is the point of it."""
    moment = now or datetime.now(UTC)
    down_at = settings.down_at
    available = down_at is None or moment < down_at
    lines = [
        f"  <vosi:available>{'true' if available else 'false'}</vosi:available>",
        f"  <vosi:upSince>{dali_timestamp(_STARTED_AT)}</vosi:upSince>",
    ]
    if down_at is not None:
        lines.append(f"  <vosi:downAt>{dali_timestamp(down_at)}</vosi:downAt>")
        lines.append(
            "  <vosi:note>Temporary service: it stops serving at the time"
            " given in downAt.</vosi:note>"
        )
    body = "\n".join(lines)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<vosi:availability
    xmlns:vosi="http://www.ivoa.net/xml/VOSIAvailability/v1.0"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
{body}
</vosi:availability>
"""


def tableset_document(registry: Registry) -> str:
    """VOSI tables: the VODataService tableset for everything discovered."""
    by_schema: dict[str, list[TableMeta]] = {}
    for meta in registry.tables.values():
        by_schema.setdefault(meta.schema_name, []).append(meta)

    schemas = "\n".join(
        _schema_element(name, sorted(tables, key=lambda meta: meta.table_name))
        for name, tables in sorted(by_schema.items())
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<vosi:tableset
    xmlns:vosi="http://www.ivoa.net/xml/VOSITables/v1.0"
    xmlns:vod="http://www.ivoa.net/xml/VODataService/v1.1"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
{schemas}
</vosi:tableset>
"""


def _schema_element(name: str, tables: list[TableMeta]) -> str:
    body = "\n".join(_table_element(meta) for meta in tables)
    return f"  <schema>\n    <name>{escape(name)}</name>\n{body}\n  </schema>"


def _table_element(meta: TableMeta) -> str:
    description = (
        f"      <description>{escape(meta.description)}</description>\n" if meta.description else ""
    )
    columns = "\n".join(_column_element(column) for column in meta.columns)
    return (
        f'    <table type="table">\n'
        f"      <name>{escape(meta.qualified_name)}</name>\n"
        f"{description}"
        f"{columns}\n"
        f"    </table>"
    )


def _column_element(column: ColumnMeta) -> str:
    parts = [f"        <name>{escape(column.name)}</name>"]
    if column.description:
        parts.append(f"        <description>{escape(column.description)}</description>")
    if column.unit:
        parts.append(f"        <unit>{escape(column.unit)}</unit>")
    if column.ucd:
        parts.append(f"        <ucd>{escape(column.ucd)}</ucd>")
    if column.utype:
        parts.append(f"        <utype>{escape(column.utype)}</utype>")
    datatype = _VOTABLE_TYPES.get(column.datatype, "char")
    size = f" arraysize={quoteattr(column.arraysize)}" if column.arraysize else ""
    # VODataService 1.1 spells VOTable's xtype as the extendedType attribute.
    xtype = f" extendedType={quoteattr(column.xtype)}" if column.xtype else ""
    parts.append(f'        <dataType xsi:type="vod:VOTableType"{size}{xtype}>{datatype}</dataType>')
    if column.indexed:
        parts.append("        <flag>indexed</flag>")
    body = "\n".join(part for part in parts if part)
    return f"      <column>\n{body}\n      </column>"


def examples_document(registry: Registry, base_url: str) -> str:
    """DALI examples as RDFa.

    Examples are generated from what was actually discovered, not from a canned
    list: an example that names a table this service does not have is worse than
    no examples at all.
    """
    items = "\n".join(_example(meta, index) for index, meta in enumerate(_example_tables(registry)))
    body = items or "    <p>No tables were discovered, so there are no examples.</p>"
    return f"""<!DOCTYPE html>
<html xmlns:vocab="http://www.ivoa.net/rdf/examples#">
  <head>
    <title>tapdrop ADQL examples</title>
  </head>
  <body>
    <h1>ADQL examples</h1>
    <p>Service: <a href={quoteattr(base_url + "/tap")}>{escape(base_url)}/tap</a></p>
{body}
  </body>
</html>
"""


def _example_tables(registry: Registry) -> list[TableMeta]:
    """A couple of tables to build examples from: positional ones first."""
    tables = sorted(registry.tables.values(), key=lambda meta: meta.qualified_name)
    positional = [meta for meta in tables if meta.ra_column and meta.dec_column]
    return (positional or tables)[:3]


def _example(meta: TableMeta, index: int) -> str:
    name = meta.qualified_name
    if meta.ra_column and meta.dec_column:
        title = f"Cone search on {name}"
        query = (
            f"SELECT TOP 100 * FROM {name}\n"
            f"WHERE CONTAINS(POINT('ICRS', {meta.ra_column}, {meta.dec_column}),\n"
            f"               CIRCLE('ICRS', 10.68, 41.27, 0.5)) = 1"
        )
    else:
        title = f"First rows of {name}"
        query = f"SELECT TOP 100 * FROM {name}"
    return (
        f'    <div typeof="example" id="ex{index}" resource="#ex{index}">\n'
        f'      <h2 property="name">{escape(title)}</h2>\n'
        f'      <p>Table: <span property="table">{escape(name)}</span></p>\n'
        f'      <pre property="query">{escape(query)}</pre>\n'
        f"    </div>"
    )
