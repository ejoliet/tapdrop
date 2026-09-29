"""Error taxonomy.

Every error the service raises on a request path maps to exactly one row of the
error table in ``RDD.md``: an HTTP status, a client-visible message, and whether
a retry is worth attempting. The API layer turns these into VOTable errors with
``QUERY_STATUS=ERROR``; nothing else should invent a status code.
"""

from __future__ import annotations


class TapdropError(Exception):
    """Base for every error tapdrop reports to a client.

    Subclasses set ``http_status`` and ``retry``. ``retry`` drives whether a
    ``Retry-After`` header is worth sending, not just documentation.
    """

    http_status: int = 500
    retry: bool = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AdqlSyntaxError(TapdropError):
    """ADQL that does not parse. Carries the failure position when known."""

    http_status = 400

    def __init__(self, message: str, position: int | None = None) -> None:
        # The position goes into the message itself, not just the attribute:
        # the VOTable error carries one string, and RDD.md's error table
        # requires the client to be told where the query failed.
        if position is not None:
            message = f"{message} (at character {position})"
        super().__init__(message)
        self.position = position


class UnsupportedAdqlError(TapdropError):
    """ADQL that parses but uses a construct this version does not implement.

    The message must name the construct; "unsupported query" is not acceptable
    because the client cannot act on it.
    """

    http_status = 400


class InvalidParameterError(TapdropError):
    """A TAP request parameter is missing, repeated, or not a legal value."""

    http_status = 400

    def __init__(self, parameter: str, reason: str) -> None:
        self.parameter = parameter
        super().__init__(f"Invalid {parameter}: {reason}")


class UnsupportedFormatError(TapdropError):
    """Client asked for a RESPONSEFORMAT this service does not produce.

    Lists what is available, because the client cannot discover it from a bare
    rejection.
    """

    http_status = 400

    def __init__(self, requested: str, supported: list[str]) -> None:
        self.requested = requested
        self.supported = supported
        super().__init__(
            f"Unsupported response format '{requested}'. Supported: {', '.join(supported)}."
        )


class UnknownTableError(TapdropError):
    """Query references a table that is not registered."""

    http_status = 400

    def __init__(self, name: str, close_matches: list[str] | None = None) -> None:
        self.name = name
        self.close_matches = close_matches or []
        hint = f" Did you mean: {', '.join(self.close_matches)}?" if self.close_matches else ""
        super().__init__(f"Unknown table '{name}'.{hint}")


class UnknownColumnError(TapdropError):
    """Query references a column that does not exist on the named table."""

    http_status = 400

    def __init__(
        self, name: str, table: str | None = None, close_matches: list[str] | None = None
    ) -> None:
        self.name = name
        self.table = table
        self.close_matches = close_matches or []
        where = f" on table '{table}'" if table else ""
        hint = f" Did you mean: {', '.join(self.close_matches)}?" if self.close_matches else ""
        super().__init__(f"Unknown column '{name}'{where}.{hint}")


class SourceUnavailableError(TapdropError):
    """A configured source (S3, HTTP) could not be read. The client may retry."""

    http_status = 503
    retry = True

    def __init__(self, uri: str, cause: str) -> None:
        self.uri = uri
        self.cause = cause
        super().__init__(f"Source unavailable: {uri} ({cause})")


class QueryTimeoutError(TapdropError):
    """Query exceeded its time budget. Sync returns 408; async fails the job."""

    http_status = 408

    def __init__(self, elapsed_seconds: float, limit_seconds: float) -> None:
        self.elapsed_seconds = elapsed_seconds
        self.limit_seconds = limit_seconds
        super().__init__(
            f"Query exceeded the {limit_seconds:g} s limit after {elapsed_seconds:.1f} s."
        )


class ResourceLimitError(TapdropError):
    """Memory ceiling or job queue full. Retryable with a Retry-After hint."""

    http_status = 429
    retry = True

    def __init__(self, message: str, retry_after_seconds: int = 30) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class UploadTooLargeError(TapdropError):
    """Upload exceeded ``TAPDROP_UPLOAD_MAX_MB``."""

    http_status = 413

    def __init__(self, limit_mb: int) -> None:
        self.limit_mb = limit_mb
        super().__init__(f"Upload exceeds the {limit_mb} MB limit.")


class UnauthorizedError(TapdropError):
    """A protected route reached without the secret token.

    The token normally travels as the ``/t/<token>/`` path prefix, because VO
    clients cannot set headers; a bearer header is the alternative for scripts.
    """

    http_status = 401

    def __init__(self) -> None:
        super().__init__(
            "This service requires a token. Use the /t/<token>/ URL or a bearer token."
        )


class ServiceExpiredError(TapdropError):
    """The TTL ``downAt`` has passed; the server drains and exits."""

    http_status = 503

    def __init__(self, down_at: str) -> None:
        self.down_at = down_at
        super().__init__(f"Service expired at {down_at}.")


class CutoutNotSupportedError(TapdropError):
    """SODA cannot serve this cutout (remote ASDF, non-ICRS frame)."""

    http_status = 501


class DiscoveryError(TapdropError):
    """A single file could not be read during discovery.

    Never fatal: discovery collects these and reports the file as skipped, with
    the reason, in ``tapdrop inspect`` and on the landing page.
    """

    http_status = 400
