"""TAP request parameter parsing.

TAP parameter names are case-insensitive and may legally repeat (UPLOAD does,
and some clients send FORMAT twice). Both the sync endpoint and the async job
creation path need the same reading of them, so the rules live here and the
handlers only deal with a validated ``TapRequest``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from tapdrop.config import Settings
from tapdrop.errors import InvalidParameterError
from tapdrop.output import VOTABLE, normalize_format

#: Accepted LANG values. TAP clients send the bare name or a version suffix, and
#: a 2.0 query is a subset of what the 2.1 translator handles.
SUPPORTED_LANGS = {"adql", "adql-2.0", "adql-2.1"}

#: Parameters the service reads. Anything else is ignored rather than rejected:
#: TAP 1.1 section 2.1 lets clients pass extra parameters.
_SINGLE_VALUED = {"request", "lang", "query", "format", "responseformat", "maxrec", "runid"}


@dataclass(frozen=True)
class TapRequest:
    """A validated TAP query request."""

    query: str
    fmt: str = VOTABLE
    maxrec: int | None = None
    runid: str | None = None
    uploads: tuple[tuple[str, str], ...] = field(default=())

    def effective_maxrec(self, settings: Settings) -> int:
        return settings.effective_maxrec(self.maxrec)


def parse_tap_request(params: Mapping[str, str] | Iterable[tuple[str, str]]) -> TapRequest:
    """Validate a doQuery request.

    Raises ``InvalidParameterError`` for anything a client can fix by changing
    the request, so the handler can turn every failure into one 400 with a
    VOTable error body.
    """
    items = params.items() if isinstance(params, Mapping) else list(params)
    collected: dict[str, list[str]] = {}
    for raw_name, value in items:
        collected.setdefault(raw_name.strip().lower(), []).append(value)

    for name in _SINGLE_VALUED:
        if len(collected.get(name, [])) > 1:
            raise InvalidParameterError(name.upper(), "given more than once")

    request = _one(collected, "request")
    if request is None:
        raise InvalidParameterError("REQUEST", "required, must be doQuery")
    if request.lower() != "doquery":
        raise InvalidParameterError("REQUEST", f"expected doQuery, got {request!r}")

    lang = _one(collected, "lang")
    if lang is None:
        raise InvalidParameterError("LANG", "required, must be ADQL")
    if lang.strip().lower() not in SUPPORTED_LANGS:
        raise InvalidParameterError("LANG", f"{lang!r} is not supported; use ADQL")

    query = _one(collected, "query")
    if query is None or not query.strip():
        raise InvalidParameterError("QUERY", "required and must not be empty")

    # RESPONSEFORMAT is the TAP 1.1 name; FORMAT is the 1.0 spelling clients
    # still send. When both arrive, RESPONSEFORMAT wins as the newer name.
    requested_format = _one(collected, "responseformat") or _one(collected, "format")

    return TapRequest(
        query=query,
        fmt=normalize_format(requested_format),
        maxrec=_parse_maxrec(_one(collected, "maxrec")),
        runid=_one(collected, "runid"),
        uploads=tuple(_parse_uploads(collected.get("upload", []))),
    )


def _one(collected: Mapping[str, list[str]], name: str) -> str | None:
    values = collected.get(name)
    return values[0] if values else None


def _parse_maxrec(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        maxrec = int(value)
    except ValueError:
        raise InvalidParameterError("MAXREC", f"{value!r} is not an integer") from None
    if maxrec < 0:
        raise InvalidParameterError("MAXREC", "must not be negative")
    return maxrec


def _parse_uploads(values: list[str]) -> list[tuple[str, str]]:
    """Split UPLOAD into (table name, URI) pairs.

    The syntax is ``name,uri`` with commas separating several assignments, and
    the URI itself may contain commas only when percent-encoded, so the split is
    on the first comma of each assignment.
    """
    uploads: list[tuple[str, str]] = []
    for value in values:
        for assignment in value.split(";") if ";" in value else [value]:
            for spec in _split_assignments(assignment):
                name, _, uri = spec.partition(",")
                if not name.strip() or not uri.strip():
                    raise InvalidParameterError("UPLOAD", f"{spec!r} is not name,uri")
                uploads.append((name.strip(), uri.strip()))
    return uploads


def _split_assignments(value: str) -> list[str]:
    """Split ``a,uri1,b,uri2`` into ``['a,uri1', 'b,uri2']``.

    TAP allows several assignments in one UPLOAD value separated by commas,
    which is ambiguous with the name/URI separator; pairs are taken two fields
    at a time, as the reference implementations do.
    """
    parts = [part for part in value.split(",") if part.strip()]
    if len(parts) % 2:
        raise InvalidParameterError("UPLOAD", f"{value!r} is not a list of name,uri pairs")
    return [f"{parts[i]},{parts[i + 1]}" for i in range(0, len(parts), 2)]
