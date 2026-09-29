"""TAP sync endpoint and the FastAPI application factory.

Every request follows the same four steps: parse parameters, translate the
ADQL, run the generated SQL under a timeout, serialise. Nothing else reaches
DuckDB, which is what makes the translator's allowlist the single security
gate (see ``adql/translate.py``).
"""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING, Any

import duckdb
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from tapdrop.adql import translate
from tapdrop.api.datalink import router as datalink_router
from tapdrop.api.params import parse_tap_request
from tapdrop.api.scs import router as scs_router
from tapdrop.api.sia import router as sia_router
from tapdrop.api.soda import router as soda_router
from tapdrop.api.tapupload import uploaded_tables
from tapdrop.api.ui import UploadArea
from tapdrop.api.ui import router as ui_router
from tapdrop.api.uws import router as uws_router
from tapdrop.api.vosi import create_router as create_vosi_router
from tapdrop.engine import run_with_timeout
from tapdrop.errors import (
    InvalidParameterError,
    ServiceExpiredError,
    TapdropError,
    UnauthorizedError,
)
from tapdrop.output import content_type, serialize, votable_error
from tapdrop.querylog import QueryLog, QueryRecord, token_hash
from tapdrop.querylog import table_meta as query_log_table
from tapdrop.uws import JobManager

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Callable

    from tapdrop.api.params import TapRequest
    from tapdrop.config import Settings
    from tapdrop.engine import QueryResult
    from tapdrop.registry import ColumnMeta, Registry

logger = logging.getLogger("tapdrop")


def create_app(settings: Settings, registry: Registry, con: duckdb.DuckDBPyConnection) -> FastAPI:
    """Build the ASGI app.

    The connection is created once by the caller rather than per request: it
    holds the registered views and geometry macros, and DuckDB serialises
    concurrent use of a single connection itself.
    """
    uploads = UploadArea()
    query_log = QueryLog(con, settings.log_dir)
    if query_log.enabled:
        # RDD.md M6: the log is queryable through the service it belongs to.
        # TAP_SCHEMA was filled by create_connection, so it is refilled here
        # rather than teaching the engine about a table it does not own.
        registry.tables["tapdrop.query_log"] = query_log_table()
        registry.populate_tap_schema(con)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        query_log.flush()  # the Parquet file is what outlives the process
        uploads.cleanup()  # uploaded files are as temporary as the service

    app = FastAPI(
        title="tapdrop",
        version=_version(),
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.uploads = uploads
    app.state.settings = settings
    app.state.registry = registry
    app.state.con = con
    app.state.query_log = query_log
    app.state.upload_lock = Lock()
    app.state.uws = JobManager(settings, registry, con, query_log=query_log)

    router = APIRouter()

    @router.get("/tap/sync")
    async def sync_get(request: Request) -> Response:
        return await _handle_sync(request, list(request.query_params.multi_items()))

    @router.post("/tap/sync")
    async def sync_post(request: Request) -> Response:
        form = await request.form()
        params: list[tuple[str, str]] = []
        parts: dict[str, bytes] = {}
        for name, value in form.multi_items():
            if isinstance(value, StarletteUploadFile):
                # An UPLOAD table travels as its own multipart part, referenced
                # by name from the UPLOAD parameter (TAP 1.1 §2.5.2).
                parts[name] = await value.read()
            else:
                params.append((name, str(value)))
        params.extend(request.query_params.multi_items())
        return await _handle_sync(request, params, parts)

    # The secret token is a path prefix, not a header: TOPCAT and other VO
    # clients cannot be made to send a custom header. Every router goes behind
    # it, VOSI included — a discoverable capabilities document would hand out
    # the service to anyone who guessed the unprefixed path.
    app.include_router(router, prefix=settings.root_path)
    app.include_router(uws_router, prefix=settings.root_path)
    app.include_router(create_vosi_router(settings, registry), prefix=settings.root_path)
    app.include_router(scs_router, prefix=settings.root_path)
    app.include_router(sia_router, prefix=settings.root_path)
    app.include_router(datalink_router, prefix=settings.root_path)
    app.include_router(soda_router, prefix=settings.root_path)
    app.include_router(ui_router, prefix=settings.root_path)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> Response:
        # Outside the token prefix on purpose: a load balancer probing liveness
        # has no token, and a bare "ok" discloses nothing about the data.
        return PlainTextResponse("ok")

    @app.middleware("http")
    async def accept_bearer_token(request: Request, call_next: Callable[..., Any]) -> Response:
        """Let a client send the token as a bearer header instead of in the path.

        TOPCAT cannot set a header, which is why the path prefix exists at all;
        a script can, and asking it to paste a secret into a URL is worse. A
        request that carries the right bearer is routed as if it had the prefix.
        """
        if not settings.token or request.url.path == "/healthz":
            return await _call(call_next, request)
        if request.url.path.startswith(settings.root_path):
            return await _call(call_next, request)

        header = request.headers.get("authorization", "")
        scheme, _, credential = header.partition(" ")
        if scheme.lower() == "bearer" and secrets.compare_digest(credential, settings.token):
            request.scope["path"] = settings.root_path + request.url.path
            return await _call(call_next, request)
        return _error_response(UnauthorizedError())

    @app.middleware("http")
    async def refuse_after_expiry(request: Request, call_next: Callable[..., Any]) -> Response:
        # RDD.md: once downAt has passed the service is gone, even in the window
        # between the deadline and the process actually exiting.
        down_at = settings.down_at
        if down_at is not None and datetime.now(UTC) >= down_at and request.url.path != "/healthz":
            return _error_response(ServiceExpiredError(down_at.isoformat()))
        response: Response = await call_next(request)
        return response

    @app.exception_handler(TapdropError)
    async def tapdrop_error_handler(_: Request, exc: TapdropError) -> Response:
        return _error_response(exc)

    @app.exception_handler(404)
    async def not_found_handler(_: Request, __: Exception) -> Response:
        return PlainTextResponse("Not found.", status_code=404)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> Response:
        # TAP 1.1 §2.6: a client gets a VOTable with QUERY_STATUS="ERROR" for
        # every failure, including the ones we did not anticipate. Starlette's
        # default 500 is JSON, which a VO client cannot read. The message is
        # generic on purpose - an internal traceback is not client-facing.
        logger.exception("unhandled error", exc_info=exc)
        return Response(
            content=votable_error("Internal server error."),
            status_code=500,
            media_type="application/x-votable+xml",
        )

    return app


async def _handle_sync(
    request: Request, params: list[tuple[str, str]], parts: dict[str, bytes] | None = None
) -> Response:
    settings: Settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    con: duckdb.DuckDBPyConnection = request.app.state.con
    query_log: QueryLog = request.app.state.query_log
    upload_lock: Lock = request.app.state.upload_lock

    started_at = datetime.now(UTC)
    started = time.monotonic()
    tap_request = None
    try:
        tap_request = parse_tap_request(params)
        maxrec = tap_request.effective_maxrec(settings)
        if tap_request.uploads and not settings.allow_upload:
            raise InvalidParameterError(
                "UPLOAD", "not enabled; start the server with --allow-upload"
            )
        # DuckDB and Arrow work is blocking; keep it off the event loop. The
        # upload tables live only for this call, so translation and execution
        # happen inside the same block that created them.
        result = await run_in_threadpool(
            _run_query,
            con,
            registry,
            settings,
            tap_request,
            parts or {},
            maxrec,
            upload_lock,
        )
    except TapdropError as exc:
        logger.info("sync query failed: %s", exc.message)
        _record(
            query_log,
            settings,
            "sync",
            tap_request,
            started_at,
            time.monotonic() - started,
            rows=0,
            error=exc.message,
        )
        return _error_response(exc)

    # DALI 1.1 §4.4.1: MAXREC=0 is a metadata request, and it always reports
    # OVERFLOW - the client asked for no rows, so "no rows came back" cannot
    # tell it whether the query had any.
    overflow = maxrec == 0 or result.table.num_rows > maxrec
    table = result.table.slice(0, maxrec) if overflow else result.table
    body = serialize(table, tap_request.fmt, _column_meta(registry), overflow=overflow)
    _record(
        query_log,
        settings,
        "sync",
        tap_request,
        started_at,
        time.monotonic() - started,
        rows=table.num_rows,
        error=None,
    )
    return Response(content=body, media_type=content_type(tap_request.fmt))


def _run_query(
    con: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    tap_request: TapRequest,
    parts: dict[str, bytes],
    maxrec: int,
    upload_lock: Lock,
) -> QueryResult:
    """Translate and run one query, with any UPLOAD tables attached around it.

    The lock is held only while uploads exist: ``tap_upload.<name>`` is a fixed
    name in a shared DuckDB connection, so two concurrent uploads of the same
    name would otherwise see each other's rows.
    """
    if not tap_request.uploads:
        translation = translate(tap_request.query, registry)
        return run_with_timeout(con, _with_maxrec(translation.sql, maxrec), _timeout(settings))

    with (
        upload_lock,
        uploaded_tables(tap_request.uploads, parts, con, registry, settings) as scoped,
    ):
        translation = translate(tap_request.query, scoped)
        return run_with_timeout(con, _with_maxrec(translation.sql, maxrec), _timeout(settings))


def _timeout(settings: Settings) -> float:
    return float(settings.query_timeout)


def _record(
    query_log: QueryLog,
    settings: Settings,
    endpoint: str,
    tap_request: TapRequest | None,
    started_at: datetime,
    elapsed: float,
    *,
    rows: int,
    error: str | None,
) -> None:
    """Log one request. ``tap_request`` is None when the parameters never parsed."""
    if not query_log.enabled:
        return
    query_log.record(
        QueryRecord(
            endpoint=endpoint,
            query=tap_request.query if tap_request else "",
            response_format=tap_request.fmt if tap_request else "",
            maxrec=tap_request.effective_maxrec(settings) if tap_request else 0,
            rows=rows,
            elapsed_seconds=elapsed,
            status="error" if error else "ok",
            error=error,
            token_hash=token_hash(settings.token),
            started_at=started_at,
        )
    )


async def _call(call_next: Callable[..., Any], request: Request) -> Response:
    """Run the rest of the stack. Exists only to keep the middlewares typed."""
    response: Response = await call_next(request)
    return response


def _with_maxrec(sql: str, maxrec: int) -> str:
    """Cap the row count, asking for one extra row so OVERFLOW can be detected.

    Wrapping in a subquery rather than appending ``LIMIT``: the translated SQL
    may already end in its own ``LIMIT`` from ``TOP n``, and the tighter of the
    two has to win.
    """
    return f"SELECT * FROM ({sql}) AS tapdrop_result LIMIT {maxrec + 1}"


def _column_meta(registry: Registry) -> dict[str, ColumnMeta]:
    """Column metadata keyed by bare column name.

    Result columns come from expressions as well as tables, so this is a
    best-effort lookup: a column keeps its unit and UCD when its name survives
    the query, and carries none when it does not. ponytail: on a name collision
    across tables the first definition that carries a unit or UCD wins (so
    ``caom.plane.s_ra``, registered bare, cannot blank ``ivoa.obscore.s_ra``);
    per-query column provenance is a translator change, worth it only once a
    join renames units apart.
    """
    meta: dict[str, ColumnMeta] = {}
    for table_meta in registry.tables.values():
        for column in table_meta.columns:
            current = meta.get(column.name)
            if current is None or (
                not (current.unit or current.ucd) and (column.unit or column.ucd)
            ):
                meta[column.name] = column
    return meta


def _error_response(exc: TapdropError) -> Response:
    headers = {"Retry-After": "30"} if exc.retry else None
    return Response(
        content=votable_error(exc.message),
        status_code=exc.http_status,
        media_type="application/x-votable+xml",
        headers=headers,
    )


def _version() -> str:
    from tapdrop import __version__

    return __version__
