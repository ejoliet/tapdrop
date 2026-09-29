"""DataLink 1.1: ``GET /datalink/links`` and the links it hands out (RDD.md M10).

ObsCore's ``access_url`` points here, so this is where a client arrives after
a TAP or SIA query. Each plane gets three rows: ``#this`` (the file itself),
``#preview`` (a generated PNG) and ``#cutout`` (a service descriptor for
``/soda/sync``).

``#this`` and ``#preview`` need URLs a client can follow, which means this
module also serves the bytes:

* ``/datalink/file`` streams the artifact, for the local and ``s3://`` sources
  a client cannot reach directly. An artifact that is already an HTTP URL is
  linked to directly instead of being proxied.
* ``/datalink/preview`` returns the cached PNG.

DEVIATION: RDD.md's endpoint table lists only ``/datalink/links``; the two
byte-serving routes are added here because ``#this`` and ``#preview`` are
otherwise unreachable for a local file. See implementation-notes.md.

Neither route takes a path from the client: the only parameter is a plane URI,
which is looked up in ``caom.artifact``. A URI that is not in the table serves
nothing, so this cannot be turned into a file-read primitive.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

import duckdb
import fsspec
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from tapdrop.api.sia import OBSCORE_TABLE
from tapdrop.caom_lite import DATALINK_CONTENT_TYPE
from tapdrop.errors import InvalidParameterError, TapdropError, UnknownTableError
from tapdrop.preview import CONTENT_TYPE as PREVIEW_CONTENT_TYPE
from tapdrop.preview import PreviewError, render_preview

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator
    from typing import IO

    from tapdrop.config import Settings
    from tapdrop.registry import Registry

logger = logging.getLogger("tapdrop")

router = APIRouter()

FITS_CONTENT_TYPE = "application/fits"
ASDF_CONTENT_TYPE = "application/x-asdf"

#: Cap on ID values per request: each one is a row to build and, for the
#: single-artifact routes, a file to open.
MAX_IDS = 500

#: Anything outside this set is replaced in a downloaded file's name, which is
#: the one place an artifact URI reaches a client's filesystem.
_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

#: XML 1.0 forbids these outright; an ID carrying one could not be escaped into
#: a well-formed document, so it is rejected instead.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

#: The XML ID of the ``ID`` FIELD, so the cutout descriptor can point a client
#: at each row's own value (DataLink 1.1 §4.3).
_ID_FIELD_REF = "dl_id"

#: DataLink 1.1 §3.2.9: semantics are terms from the DataLink vocabulary.
_SEMANTICS_THIS = "#this"
_SEMANTICS_PREVIEW = "#preview"
_SEMANTICS_CUTOUT = "#cutout"

_CUTOUT_SERVICE_ID = "cutout"


@dataclass(frozen=True)
class _Artifact:
    plane_uri: str
    artifact_uri: str
    content_length: int | None
    hdu_index: int


@router.api_route("/datalink/links", methods=["GET", "POST"])
async def links(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    con: duckdb.DuckDBPyConnection = request.app.state.con

    try:
        _require_obscore(registry)
        ids = _ids(await _parameters(request))
    except TapdropError as exc:
        logger.info("DataLink request failed: %s", exc.message)
        return Response(
            content=_votable([], status=("ERROR", exc.message), base_url=""),
            status_code=exc.http_status,
            media_type=DATALINK_CONTENT_TYPE,
        )

    base_url = _base_url(settings, request)
    # One query for every ID rather than one per ID, off the event loop: DuckDB
    # calls block, and a client may send a few hundred identifiers.
    found = await run_in_threadpool(_lookup_many, con, ids)
    rows: list[str] = []
    for identifier in ids:
        artifact = found.get(identifier)
        if artifact is None:
            rows.append(_row(identifier, "", "", f"NotFoundFault: {identifier}", "", "", "", None))
            continue
        rows.extend(_rows_for(artifact, base_url))

    body = _votable(rows, status=("OK", ""), base_url=base_url)
    return Response(content=body, media_type=DATALINK_CONTENT_TYPE)


@router.api_route("/datalink/file", methods=["GET", "POST"])
async def file(request: Request) -> Response:
    """Serve the artifact behind one plane URI (the ``#this`` link)."""
    con: duckdb.DuckDBPyConnection = request.app.state.con
    try:
        _require_obscore(request.app.state.registry)
        identifier = _one_id(await _parameters(request))
        artifact = await run_in_threadpool(_lookup, con, identifier)
        if artifact is None:
            raise InvalidParameterError("ID", f"no such dataset: {identifier}")
        # Opened before the response starts: a StreamingResponse that fails on
        # its first chunk has already sent 200, and the client sees a truncated
        # file rather than an error.
        handle, size = await run_in_threadpool(_open_artifact, artifact.artifact_uri)
    except TapdropError as exc:
        return _plain_error(exc)
    except OSError as exc:
        logger.warning("artifact for %s is unreadable: %s", identifier, exc)
        return Response(
            content="DefaultFault: artifact is unreadable",
            status_code=500,
            media_type="text/plain",
        )

    filename = _UNSAFE_IN_FILENAME.sub("_", artifact.artifact_uri.rstrip("/").rsplit("/", 1)[-1])
    headers = {"Content-Disposition": f'attachment; filename="{filename or "artifact"}"'}
    if size is not None:
        headers["Content-Length"] = str(size)
    return StreamingResponse(
        _chunks(handle),
        media_type=_content_type(artifact.artifact_uri),
        headers=headers,
    )


def _open_artifact(uri: str) -> tuple[IO[bytes], int | None]:
    """Open the artifact and get its length, or raise ``OSError`` trying."""
    fs, path = fsspec.core.url_to_fs(uri)
    handle: IO[bytes] = fs.open(path, "rb")
    try:
        size = int(fs.size(path))
    except (OSError, TypeError, ValueError):  # a filesystem that cannot size a file
        size = None
    return handle, size


def _chunks(handle: IO[bytes], size: int = 1024 * 1024) -> Iterator[bytes]:
    """Stream a file a megabyte at a time: an image can be larger than memory."""
    with handle:
        while True:
            block = handle.read(size)
            if not block:
                return
            yield block


def _content_type(uri: str) -> str:
    return ASDF_CONTENT_TYPE if uri.lower().endswith(".asdf") else FITS_CONTENT_TYPE


@router.api_route("/datalink/preview", methods=["GET", "POST"])
async def preview(request: Request) -> Response:
    """Serve the generated PNG preview for one plane URI."""
    settings: Settings = request.app.state.settings
    con: duckdb.DuckDBPyConnection = request.app.state.con
    try:
        _require_obscore(request.app.state.registry)
        identifier = _one_id(await _parameters(request))
        artifact = await run_in_threadpool(_lookup, con, identifier)
        if artifact is None:
            raise InvalidParameterError("ID", f"no such dataset: {identifier}")
        # Reading pixels and deflating a PNG are both blocking work.
        png = await run_in_threadpool(
            render_preview, artifact.artifact_uri, artifact.hdu_index, settings.preview_cache_path
        )
    except TapdropError as exc:
        return _plain_error(exc)
    except PreviewError as exc:
        return Response(content=str(exc), status_code=415, media_type="text/plain")
    except OSError as exc:
        logger.warning("preview for %s failed: %s", identifier, exc)
        return Response(
            content="DefaultFault: artifact is unreadable",
            status_code=500,
            media_type="text/plain",
        )

    return Response(content=png, media_type=PREVIEW_CONTENT_TYPE)


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------


def _require_obscore(registry: Registry) -> None:
    if OBSCORE_TABLE not in registry.tables:
        raise UnknownTableError(OBSCORE_TABLE)


async def _parameters(request: Request) -> list[tuple[str, str]]:
    """Request parameters, by GET or POST (DataLink 1.1 §2, DALI 1.1 §2.2)."""
    params = list(request.query_params.multi_items())
    if request.method == "POST":
        form = await request.form()
        params.extend((name, str(value)) for name, value in form.multi_items())
    return params


def _ids(params: list[tuple[str, str]]) -> list[str]:
    """The ID values of a request.

    DataLink 1.1 §2.1.1: a request with no ID is answered normally, with an
    empty results table - it is not an error.
    """
    values = [value for name, value in params if name.strip().lower() == "id"]
    if len(values) > MAX_IDS:
        raise InvalidParameterError("ID", f"too many values (at most {MAX_IDS})")
    for value in values:
        if _CONTROL_CHARACTERS.search(value):
            raise InvalidParameterError("ID", "contains a control character")
    return values


def _one_id(params: list[tuple[str, str]]) -> str:
    """The single ID the byte-serving routes take, unlike ``/datalink/links``."""
    identifiers = _ids(params)
    if not identifiers:
        raise InvalidParameterError("ID", "required")
    if len(identifiers) > 1:
        raise InvalidParameterError("ID", "given more than once")
    return identifiers[0]


_LOOKUP_SQL = """
    SELECT a.plane_uri, a.artifact_uri, a.content_length, coalesce(c.extension, 0)
    FROM "caom"."artifact" a
    LEFT JOIN "caom"."chunk" c ON c.artifact_uri = a.artifact_uri
    WHERE a.plane_uri IN ({placeholders})
"""


def _lookup(con: duckdb.DuckDBPyConnection, plane_uri: str) -> _Artifact | None:
    return _lookup_many(con, [plane_uri]).get(plane_uri)


def _lookup_many(con: duckdb.DuckDBPyConnection, plane_uris: list[str]) -> dict[str, _Artifact]:
    """Resolve several plane URIs in one query. Missing ones are simply absent."""
    if not plane_uris:
        return {}
    # The placeholders are generated from the count, never from the values: the
    # identifiers themselves stay bound parameters.
    placeholders = ", ".join("?" * len(plane_uris))
    rows = con.execute(_LOOKUP_SQL.format(placeholders=placeholders), plane_uris).fetchall()
    return {
        str(row[0]): _Artifact(
            plane_uri=str(row[0]),
            artifact_uri=str(row[1]),
            content_length=None if row[2] is None else int(row[2]),
            hdu_index=int(row[3]),
        )
        for row in rows
    }


# --------------------------------------------------------------------------
# VOTable
# --------------------------------------------------------------------------


def _rows_for(artifact: _Artifact, base_url: str) -> list[str]:
    identifier = artifact.plane_uri
    query = f"?ID={quote(identifier, safe='')}"
    # An artifact that already lives on a public HTTP server is linked to
    # directly; proxying it would only add a hop and hide the real location.
    if artifact.artifact_uri.startswith(("http://", "https://")):
        this_url = artifact.artifact_uri
    else:
        this_url = f"{base_url}/datalink/file{query}"
    return [
        _row(
            identifier,
            this_url,
            "",
            "",
            "The image this dataset was built from",
            _SEMANTICS_THIS,
            _content_type(artifact.artifact_uri),
            artifact.content_length,
        ),
        _row(
            identifier,
            f"{base_url}/datalink/preview{query}",
            "",
            "",
            "Asinh-stretched PNG preview",
            _SEMANTICS_PREVIEW,
            PREVIEW_CONTENT_TYPE,
            None,
        ),
        _row(
            identifier,
            "",
            _CUTOUT_SERVICE_ID,
            "",
            "Server-side cutout of this image",
            _SEMANTICS_CUTOUT,
            FITS_CONTENT_TYPE,
            None,
        ),
    ]


def _row(
    identifier: str,
    access_url: str,
    service_def: str,
    error_message: str,
    description: str,
    semantics: str,
    content_type: str,
    content_length: int | None,
) -> str:
    cells = [
        identifier,
        access_url,
        service_def,
        error_message,
        description,
        semantics,
        content_type,
        "" if content_length is None else str(content_length),
    ]
    return "        <TR>" + "".join(f"<TD>{escape(cell)}</TD>" for cell in cells) + "</TR>"


_FIELDS = f"""      <FIELD name="ID" ID="{_ID_FIELD_REF}" datatype="char" arraysize="*"
             ucd="meta.id;meta.main"/>
      <FIELD name="access_url" datatype="char" arraysize="*" ucd="meta.ref.url"/>
      <FIELD name="service_def" datatype="char" arraysize="*" ucd="meta.ref"/>
      <FIELD name="error_message" datatype="char" arraysize="*" ucd="meta.code.error"/>
      <FIELD name="description" datatype="char" arraysize="*" ucd="meta.note"/>
      <FIELD name="semantics" datatype="char" arraysize="*" ucd="meta.code"/>
      <FIELD name="content_type" datatype="char" arraysize="*" ucd="meta.code.mime"/>
      <FIELD name="content_length" datatype="long" unit="byte" ucd="phys.size;meta.file"/>"""


def _votable(rows: list[str], status: tuple[str, str], base_url: str) -> bytes:
    status_value, status_text = status
    info = (
        f'      <INFO name="QUERY_STATUS" value="{status_value}">{escape(status_text)}</INFO>'
        if status_text
        else f'      <INFO name="QUERY_STATUS" value="{status_value}"/>'
    )
    service = _cutout_service(base_url) if rows else ""
    body = "\n".join(rows)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<VOTABLE version="1.4" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">
  <RESOURCE type="results">
{info}
    <TABLE>
{_FIELDS}
      <DATA>
        <TABLEDATA>
{body}
        </TABLEDATA>
      </DATA>
    </TABLE>
  </RESOURCE>
{service}
</VOTABLE>
""".encode()


def _cutout_service(base_url: str) -> str:
    """The ``#cutout`` service descriptor (DataLink 1.1 §4).

    ``ID`` takes its value from each row's own ``ID`` cell through ``ref``
    (DataLink 1.1 §4.3); one descriptor is shared by every row, so a literal
    value here would tell a client to cut the first dataset for all of them.
    The shape parameters are declared with DALI xtypes and no value, which is
    how a client knows they are the ones to fill in.
    """
    return f"""  <RESOURCE type="meta" utype="adhoc:service" ID="{_CUTOUT_SERVICE_ID}">
    <PARAM name="standardID" datatype="char" arraysize="*"
           value="ivo://ivoa.net/std/SODA#sync-1.0"/>
    <PARAM name="accessURL" datatype="char" arraysize="*"
           value={quoteattr(f"{base_url}/soda/sync")}/>
    <GROUP name="inputParams">
      <PARAM name="ID" datatype="char" arraysize="*" ref="{_ID_FIELD_REF}" value=""/>
      <PARAM name="CIRCLE" datatype="double" arraysize="3" unit="deg" xtype="circle" value=""/>
      <PARAM name="POLYGON" datatype="double" arraysize="*" unit="deg" xtype="polygon" value=""/>
      <PARAM name="POS" datatype="char" arraysize="*" value=""/>
    </GROUP>
  </RESOURCE>"""


def _base_url(settings: Settings, request: Request) -> str:
    """Absolute service base, token prefix included - same rule as VOSI."""
    if settings.public_url:
        return settings.public_url.rstrip("/") + settings.root_path
    return str(request.base_url).rstrip("/") + settings.root_path


def _plain_error(exc: TapdropError) -> Response:
    return Response(content=exc.message, status_code=exc.http_status, media_type="text/plain")
