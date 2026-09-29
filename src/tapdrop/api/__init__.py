"""HTTP layer: TAP, VOSI, and the landing page."""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["dali_timestamp"]


def dali_timestamp(moment: datetime) -> str:
    """Format *moment* the way DALI 1.1 §3.3.3 requires: UTC, ``Z``, no offset.

    AIDEV-NOTE: ``datetime.isoformat()`` emits ``+00:00`` and microseconds, and
    pyvo's UWS parser rejects both (``Cannot parse datetime ...``). Every
    timestamp this service puts on the wire goes through here.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
