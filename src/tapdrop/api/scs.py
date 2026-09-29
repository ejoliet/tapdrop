"""Simple Cone Search 1.03: ``GET|POST /scs/{schema}.{table}``.

One route serves every discovered table that has an RA/Dec pair. It builds a
plain ADQL cone query and runs it through the same translator/engine path
``api/tap.py`` uses for ``/tap/sync`` -- the translator's AST allowlist is the
only thing between a request and DuckDB, and SCS gets no exemption from that.

RA/DEC/SR are parsed to floats before they ever touch the query string; no
client-supplied text is interpolated into SQL. Table and column names come
from the ``Registry``, not the request, so they need no such treatment.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import duckdb
from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from tapdrop.adql import translate
from tapdrop.engine import run_with_timeout
from tapdrop.errors import InvalidParameterError, TapdropError, UnknownTableError
from tapdrop.output import VOTABLE_TD, content_type, serialize, votable_error

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tapdrop.config import Settings
    from tapdrop.registry import ColumnMeta, Registry, TableMeta

logger = logging.getLogger("tapdrop")

router = APIRouter()

_RA_RANGE = (0.0, 360.0)
_DEC_RANGE = (-90.0, 90.0)
_SR_RANGE = (0.0, 180.0)  # SCS 1.03: SR=0 is a point match, SR>180 covers more than the sky.
_VALID_VERB = (1, 2, 3)
_DEFAULT_VERB = 2


@router.api_route("/scs/{schema}.{table}", methods=["GET", "POST"])
async def cone_search(schema: str, table: str, request: Request) -> Response:
    settings: Settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    con: duckdb.DuckDBPyConnection = request.app.state.con

    # Lazy, function-scoped import: datalink.py imports sia.py, which imports
    # this module, so a module-scope import here would be circular. Same
    # reasoning as the tap.py import below.
    from tapdrop.api.datalink import _parameters

    try:
        # DALI 1.1 §2.2: a sync resource takes its parameters by GET or POST.
        collected = _collect(await _parameters(request))
        ra = _parse_float(collected, "RA", _RA_RANGE)
        dec = _parse_float(collected, "DEC", _DEC_RANGE)
        sr = _parse_float(collected, "SR", _SR_RANGE)
        verb = _parse_verb(collected)

        meta = registry.get(f"{schema}.{table}")
        if not meta.ra_column or not meta.dec_column:
            # Not an SCS error class of its own in RDD.md's error table: a
            # table with no detected RA/Dec simply is not part of the SCS
            # resource space, so it is "unknown" from this endpoint's point
            # of view, same as a table that was never registered at all.
            raise UnknownTableError(meta.qualified_name)

        query = _cone_query(meta, ra, dec, sr, verb)
        translation = translate(query, registry)
        # Lazy, function-scoped import: tap.py imports this module's router at
        # module load time, so importing tap.py back at module scope here
        # would be a circular import. By request time tap.py is fully loaded.
        from tapdrop.api.tap import _column_meta, _with_maxrec

        maxrec = settings.effective_maxrec(None)  # SCS carries no MAXREC param of its own.
        result = await run_in_threadpool(
            run_with_timeout,
            con,
            _with_maxrec(translation.sql, maxrec),
            float(settings.query_timeout),
        )
    except TapdropError as exc:
        logger.info("SCS query failed: %s", exc.message)
        return _error_response(exc)

    overflow = maxrec == 0 or result.table.num_rows > maxrec
    result_table = result.table.slice(0, maxrec) if overflow else result.table
    # SCS 1.03 predates VOTable BINARY2 and its clients (and validators such
    # as taplint) expect the older, simpler TABLEDATA serialization, so this
    # endpoint always writes votable/td regardless of the service's default.
    body = serialize(result_table, VOTABLE_TD, _column_meta(registry), overflow=overflow)
    return Response(content=body, media_type=content_type(VOTABLE_TD))


def _cone_query(meta: TableMeta, ra: float, dec: float, sr: float, verb: int) -> str:
    columns = _verb_columns(meta, verb)
    select_list = ", ".join(column.name for column in columns) if columns else "*"
    return (
        f"SELECT {select_list} FROM {meta.qualified_name} "
        f"WHERE CONTAINS(POINT('ICRS', {meta.ra_column}, {meta.dec_column}), "
        f"CIRCLE('ICRS', {ra:.10f}, {dec:.10f}, {sr:.10f})) = 1"
    )


def _verb_columns(meta: TableMeta, verb: int) -> list[ColumnMeta]:
    """SCS 1.03 VERB levels, reusing TAP_SCHEMA's own notion of "principal".

    VERB=1: the identifier-ish column (the discovered primary key, if any)
    plus RA/Dec -- enough to locate the match, nothing more.
    VERB=2 (default): VERB=1's columns plus every column ``registry.py``
    already marks principal for TAP_SCHEMA (``column.principal`` is set only
    via a ``tapdrop.yaml`` column override; absent one, VERB=2 equals VERB=1).
    Reusing that flag keeps one definition of "principal" for the whole
    service rather than inventing a second, SCS-only one.
    VERB=3: every column.
    """
    if verb >= 3:
        return list(meta.columns)
    base = {name for name in (meta.ra_column, meta.dec_column, meta.primary_key) if name}
    if verb == 2:
        base |= {column.name for column in meta.columns if column.principal}
    return [column for column in meta.columns if column.name in base]


def _collect(items: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Case-insensitive parameter collection, matching ``api/params.py``."""
    collected: dict[str, list[str]] = {}
    for raw_name, value in items:
        collected.setdefault(raw_name.strip().lower(), []).append(value)
    return collected


def _one(collected: dict[str, list[str]], name: str) -> str | None:
    values = collected.get(name.lower())
    if not values:
        return None
    if len(values) > 1:
        raise InvalidParameterError(name.upper(), "given more than once")
    return values[0]


def _parse_float(collected: dict[str, list[str]], name: str, bounds: tuple[float, float]) -> float:
    raw = _one(collected, name)
    if raw is None or not raw.strip():
        raise InvalidParameterError(name, "required")
    try:
        value = float(raw)
    except ValueError:
        raise InvalidParameterError(name, f"{raw!r} is not a number") from None
    low, high = bounds
    if not (low <= value <= high):
        raise InvalidParameterError(name, f"must be within [{low:g}, {high:g}] degrees")
    return value


def _parse_verb(collected: dict[str, list[str]]) -> int:
    raw = _one(collected, "VERB")
    if raw is None or not raw.strip():
        return _DEFAULT_VERB
    try:
        verb = int(raw)
    except ValueError:
        raise InvalidParameterError("VERB", f"{raw!r} is not an integer") from None
    if verb not in _VALID_VERB:
        raise InvalidParameterError("VERB", "must be 1, 2, or 3")
    return verb


def _error_response(exc: TapdropError) -> Response:
    headers = {"Retry-After": "30"} if exc.retry else None
    return Response(
        content=votable_error(exc.message),
        status_code=exc.http_status,
        media_type="application/x-votable+xml",
        headers=headers,
    )
