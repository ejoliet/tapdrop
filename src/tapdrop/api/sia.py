"""SIA v2: ``GET|POST /sia/query`` (RDD.md M9).

A parameter layer over TAP, nothing more: each SIA constraint becomes a
predicate in an ADQL query against ``ivoa.obscore``, which then goes through
the same translator and engine as ``/tap/sync``. The translator's AST
allowlist stays the only gate to DuckDB, and every number a client sends is
parsed to a float before it reaches the query text.

Per DALI, repeating a parameter means OR within that parameter, and different
parameters are ANDed together.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

import duckdb
from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from tapdrop.adql import translate
from tapdrop.api.scs import _collect, _one
from tapdrop.engine import run_with_timeout
from tapdrop.errors import InvalidParameterError, TapdropError, UnknownTableError
from tapdrop.output import VOTABLE_TD, content_type, normalize_format, serialize, votable_error

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tapdrop.config import Settings
    from tapdrop.registry import Registry

logger = logging.getLogger("tapdrop")

router = APIRouter()

OBSCORE_TABLE = "ivoa.obscore"

_POS_SHAPES = ("circle", "range", "polygon")


@router.api_route("/sia/query", methods=["GET", "POST"])
async def sia_query(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    con: duckdb.DuckDBPyConnection = request.app.state.con

    # Lazy, function-scoped imports: datalink.py imports this module at load
    # time and tap.py imports its router, so either at module scope here would
    # be a circular import. By request time both are fully loaded.
    from tapdrop.api.datalink import _parameters
    from tapdrop.api.tap import _column_meta, _with_maxrec

    try:
        if OBSCORE_TABLE not in registry.tables:
            # No --images, so there is no ObsCore table and nothing this
            # endpoint could search. Same reasoning as SCS on a table with no
            # RA/Dec: the resource simply is not part of this service.
            raise UnknownTableError(OBSCORE_TABLE)

        # SIA 2.0 §2.1 / DALI 1.1 §2.2: parameters arrive by GET or POST.
        collected = _collect(await _parameters(request))
        fmt = _parse_responseformat(collected)
        query = _sia_query(collected)
        translation = translate(query, registry)

        maxrec = settings.effective_maxrec(_parse_maxrec(collected))
        result = await run_in_threadpool(
            run_with_timeout,
            con,
            _with_maxrec(translation.sql, maxrec),
            float(settings.query_timeout),
        )
    except TapdropError as exc:
        logger.info("SIA query failed: %s", exc.message)
        # SIA 2.0 §4.2: the error text starts with one of the SIA fault codes.
        # SIA only - SCS and TAP have no such vocabulary and keep the bare message.
        code = "UsageFault" if exc.http_status < 500 else "DefaultFault"
        return Response(
            content=votable_error(f"{code}: {exc.message}"),
            status_code=exc.http_status,
            media_type="application/x-votable+xml",
            headers={"Retry-After": "30"} if exc.retry else None,
        )

    overflow = maxrec == 0 or result.table.num_rows > maxrec
    result_table = result.table.slice(0, maxrec) if overflow else result.table
    body = serialize(result_table, fmt, _column_meta(registry), overflow=overflow)
    return Response(content=body, media_type=content_type(fmt))


def _parse_responseformat(collected: dict[str, list[str]]) -> str:
    """DALI 1.1 §3.3: honour a supported ``RESPONSEFORMAT``, refuse an unknown one.

    Absent, the answer is TABLEDATA VOTable as before (SIA clients predate
    BINARY2 as much as SCS ones do). ``FORMAT`` is *not* an alias here: in SIA
    2.0 it filters on the data product's format and is out of scope (RDD.md M9).
    """
    requested = _one(collected, "RESPONSEFORMAT")
    if requested is None or not requested.strip():
        return VOTABLE_TD
    return normalize_format(requested)


def _sia_query(collected: dict[str, list[str]]) -> str:
    """Build the ADQL for one SIA request."""
    clauses = [
        _pos_clause(collected.get("pos", [])),
        _interval_clause(collected.get("band", []), "BAND", "em_min", "em_max"),
        _interval_clause(collected.get("time", []), "TIME", "t_min", "t_max"),
        _pol_clause(collected.get("pol", [])),
        _collection_clause(collected.get("collection", [])),
    ]
    predicates = [clause for clause in clauses if clause]
    where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
    return f"SELECT * FROM {OBSCORE_TABLE}{where}"


def _or(predicates: list[str]) -> str:
    """OR the alternatives of one repeated parameter, parenthesised for the AND above."""
    if len(predicates) == 1:
        return predicates[0]
    return "(" + " OR ".join(predicates) + ")"


def _pos_clause(values: list[str]) -> str:
    return _or([_one_pos(value) for value in values]) if values else ""


def _one_pos(value: str) -> str:
    """One ``POS=SHAPE args`` value as an ``INTERSECTS(..., s_region)`` predicate."""
    parts = value.split()
    if not parts:
        raise InvalidParameterError("POS", "is empty")
    shape = parts[0].lower()
    if shape not in _POS_SHAPES:
        raise InvalidParameterError(
            "POS", f"unknown shape {parts[0]!r}; use CIRCLE, RANGE or POLYGON"
        )
    numbers = _floats("POS", parts[1:])
    # _floats lets infinities through because BAND/TIME and RANGE (SIA 2.0
    # §2.1.1, DALI intervals) spell open bounds as -Inf/+Inf. A CIRCLE or
    # POLYGON coordinate has no such reading and would reach the ADQL as a
    # bare `inf` token.
    if shape != "range" and not all(math.isfinite(number) for number in numbers):
        raise InvalidParameterError("POS", "coordinates must be finite numbers")

    if shape == "circle":
        if len(numbers) != 3:
            raise InvalidParameterError("POS", "CIRCLE takes exactly 3 numbers: lon lat radius")
        lon, lat, radius = numbers
        if radius < 0:
            raise InvalidParameterError("POS", "CIRCLE radius must not be negative")
        return _intersects(f"CIRCLE('ICRS', {lon:.10f}, {lat:.10f}, {radius:.10f})")

    if shape == "range":
        if len(numbers) != 4:
            raise InvalidParameterError("POS", "RANGE takes exactly 4 numbers: lon1 lon2 lat1 lat2")
        return _range_predicate(numbers)

    if len(numbers) < 6 or len(numbers) % 2 != 0:
        raise InvalidParameterError("POS", "POLYGON takes an even count of at least 6 numbers")
    coords = ", ".join(f"{number:.10f}" for number in numbers)
    return _intersects(f"POLYGON('ICRS', {coords})")


def _intersects(region: str) -> str:
    return f"INTERSECTS({region}, s_region) = 1"


def _range_predicate(numbers: list[float]) -> str:
    """A ``RANGE lon1 lon2 lat1 lat2`` as an ADQL predicate.

    SIA 2.0 §2.1.1 spells an open bound as ``-Inf``/``+Inf``; those are
    clamped to the sky, ``[0, 360]`` x ``[-90, 90]``, so no infinity ever
    reaches the query text. The whole sky is no constraint at all.

    A bounded range becomes one INTERSECTS(POLYGON, s_region) per lon span.
    SIA v2 allows ``lon1 > lon2`` to mean a range that wraps through 0; a
    single polygon with those corners would instead describe the complement
    (the long way round the sky), so the wrapping case is split at 0/360 into
    two polygons that are ORed together.

    AIDEV-NOTE: a range reaching a pole or spanning all 360 deg of longitude
    is *not* turned into a polygon: two of its corners coincide (the pole, or
    lon 0 == lon 360), which the spherical UDF reads as zero-area, and a
    pole-to-pole strip is wider than the hemisphere the UDF's ray-casting
    assumes. Nudging the latitude to +/-89.999999 would fix the first and not
    the second, so such a range falls back to a band predicate on the ObsCore
    centre (``s_ra``/``s_dec``) instead. That is centre-in-box, not footprint
    overlap: an image whose centre sits just outside the range is missed.
    Acceptable for the "one hemisphere" style of query these ranges express.
    """
    lon1, lon2, lat1, lat2 = numbers
    if lat1 > lat2:
        raise InvalidParameterError("POS", "RANGE latitudes must be given as lat1 <= lat2")
    lon1, lon2 = (min(max(lon, 0.0), 360.0) for lon in (lon1, lon2))
    lat1, lat2 = (min(max(lat, -90.0), 90.0) for lat in (lat1, lat2))
    all_lon = lon1 <= 0.0 and lon2 >= 360.0
    all_lat = lat1 <= -90.0 and lat2 >= 90.0
    if all_lon and all_lat:
        return "1 = 1"
    if all_lon or lat1 <= -90.0 or lat2 >= 90.0:
        bounds = []
        if lat1 > -90.0:
            bounds.append(f"s_dec >= {lat1:.10f}")
        if lat2 < 90.0:
            bounds.append(f"s_dec <= {lat2:.10f}")
        if not all_lon:
            joiner = " AND " if lon1 <= lon2 else " OR "
            bounds.append(f"(s_ra >= {lon1:.10f}{joiner}s_ra <= {lon2:.10f})")
        return "(" + " AND ".join(bounds) + ")"
    spans = [(lon1, lon2)] if lon1 <= lon2 else [(lon1, 360.0), (0.0, lon2)]
    polygons = []
    for start, end in spans:
        corners = ((start, lat1), (end, lat1), (end, lat2), (start, lat2))
        coords = ", ".join(f"{lon:.10f}, {lat:.10f}" for lon, lat in corners)
        polygons.append(_intersects(f"POLYGON('ICRS', {coords})"))
    return _or(polygons)


def _interval_clause(values: list[str], name: str, low_column: str, high_column: str) -> str:
    """``BAND``/``TIME``: an open or closed interval that must overlap the row's own.

    A single number is a point; ``lo hi`` is a closed interval; ``-Inf``/``+Inf``
    (DALI's open-bound spelling) leaves that side unbounded and drops the
    corresponding comparison rather than emitting an infinity literal.
    """
    predicates = []
    for value in values:
        numbers = _floats(name, value.split())
        if len(numbers) == 1:
            numbers = [numbers[0], numbers[0]]
        if len(numbers) != 2:
            raise InvalidParameterError(name, "takes one value or an interval of two")
        low, high = numbers
        if low > high:
            raise InvalidParameterError(name, "interval must be given as low high")
        # Overlap, not containment: a row whose coverage merely touches the
        # requested interval is a match (DALI 1.1 interval semantics).
        bounds = []
        if not math.isinf(high):
            bounds.append(f"{low_column} <= {high:.10f}")
        if not math.isinf(low):
            bounds.append(f"{high_column} >= {low:.10f}")
        predicates.append("(" + " AND ".join(bounds) + ")" if bounds else "1 = 1")
    return _or(predicates) if predicates else ""


def _pol_clause(values: list[str]) -> str:
    predicates = []
    for value in values:
        state = _validated_word("POL", value)
        # The state lands in a LIKE pattern, where `_` is a wildcard; ObsCore
        # states (I, Q, U, V, RR, LL, XX, YY, POLI, ...) are letters only.
        if not state.isalpha():
            raise InvalidParameterError("POL", "must be a polarization state such as I, Q, U, V")
        # pol_states is the ObsCore '/I/Q/' slash-delimited list, so a state
        # matches on its delimited form, never as a bare substring.
        predicates.append(f"pol_states LIKE '%/{state}/%'")
    return _or(predicates) if predicates else ""


def _collection_clause(values: list[str]) -> str:
    predicates = [f"obs_collection = '{_validated_word('COLLECTION', value)}'" for value in values]
    return _or(predicates) if predicates else ""


#: Characters allowed in a value that reaches the query text as a string
#: literal. Deliberately narrow: this is the only client-supplied text that is
#: not a parsed number, so it is checked rather than escaped.
_WORD_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_+. ")


def _validated_word(name: str, value: str) -> str:
    text = value.strip()
    if not text:
        raise InvalidParameterError(name, "is empty")
    if not set(text) <= _WORD_OK:
        raise InvalidParameterError(name, "contains characters that are not allowed")
    return text


def _floats(name: str, tokens: list[str]) -> list[float]:
    numbers = []
    for token in tokens:
        try:
            numbers.append(float(token))
        except ValueError:
            raise InvalidParameterError(name, f"{token!r} is not a number") from None
    if any(math.isnan(number) for number in numbers):
        raise InvalidParameterError(name, "NaN is not a valid coordinate")
    return numbers


def _parse_maxrec(collected: dict[str, list[str]]) -> int | None:
    raw = _one(collected, "MAXREC")
    if raw is None or not raw.strip():
        return None
    try:
        maxrec = int(raw)
    except ValueError:
        raise InvalidParameterError("MAXREC", f"{raw!r} is not an integer") from None
    if maxrec < 0:
        raise InvalidParameterError("MAXREC", "must not be negative")
    return maxrec
