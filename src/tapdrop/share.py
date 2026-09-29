"""Public exposure: the cloudflared quick tunnel, the terminal QR code, and
the expiry clock that stops the server when the TTL runs out.

Everything here is about a service that is deliberately temporary. The tunnel
is a child process, not a library call, because ``cloudflared`` is a Go binary
and shelling out is the whole integration. A tunnelled service always has an
expiry (``Settings.resolve_down_at``), so this module is also where the clock
that stops uvicorn lives.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

__all__ = ["INSTALL_HINT", "TunnelError", "start_tunnel", "stop_when_expired", "terminal_qr"]

#: cloudflared prints the quick-tunnel URL on stderr, in a box drawn with
#: ASCII art. The URL is the only part of that output worth parsing.
_URL_PATTERN = re.compile(rb"https://[a-z0-9][-a-z0-9]*\.trycloudflare\.com")

INSTALL_HINT = (
    "cloudflared is not installed. Install it and retry:\n"
    "  macOS:  brew install cloudflared\n"
    "  Linux:  see https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/\n"
    "Or drop --share and serve on localhost only."
)


class TunnelError(RuntimeError):
    """The tunnel could not be started, or never printed a URL."""


class _Stoppable(Protocol):
    """The one attribute of ``uvicorn.Server`` the expiry clock needs."""

    should_exit: bool


def start_tunnel(port: int, *, timeout: float = 30.0) -> tuple[str, subprocess.Popen[bytes]]:
    """Start a cloudflared quick tunnel to ``port`` and return its public URL.

    The caller owns the returned process and must terminate it; the tunnel dies
    with it, which is what makes the shared URL temporary.
    """
    command = [
        "cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--url",
        f"http://127.0.0.1:{port}",
    ]
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise TunnelError(INSTALL_HINT) from exc

    url = _read_url(process, timeout)
    if url is None:
        process.terminate()
        raise TunnelError(f"cloudflared did not print a tunnel URL within {timeout:g}s.")
    return url, process


def _read_url(process: subprocess.Popen[bytes], timeout: float) -> str | None:
    """Scan cloudflared's stderr for the quick-tunnel URL, giving up after *timeout*."""
    assert process.stderr is not None  # stderr=PIPE above
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stderr.readline()
        if not line:  # cloudflared exited without ever printing a URL
            return None
        match = _URL_PATTERN.search(line)
        if match:
            return match.group().decode()
    return None


def terminal_qr(url: str) -> str:
    """Render *url* as a QR code made of block characters, for a terminal."""
    import qrcode

    code = qrcode.QRCode(border=1)
    code.add_data(url)
    code.make(fit=True)
    # AIDEV-NOTE: two half-height rows per text line, so the code stays square
    # in a terminal whose cells are twice as tall as they are wide.
    matrix: list[list[bool]] = code.get_matrix()
    blank = [False] * len(matrix[0])
    lines = []
    for top, bottom in zip(matrix[::2], [*matrix[1::2], blank], strict=False):
        lines.append(
            "".join(_HALF_BLOCKS[(upper, lower)] for upper, lower in zip(top, bottom, strict=True))
        )
    return "\n".join(lines)


#: (upper cell dark, lower cell dark) -> character. Dark modules are drawn as
#: foreground blocks on the terminal's own background.
_HALF_BLOCKS = {
    (True, True): "█",
    (True, False): "▀",
    (False, True): "▄",
    (False, False): " ",
}


def stop_when_expired(
    server: _Stoppable,
    down_at: datetime,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> threading.Thread:
    """Ask *server* to exit once *down_at* has passed.

    Setting ``should_exit`` is uvicorn's own graceful path: it stops accepting
    connections and lets in-flight requests finish, so a running query is not
    cut off mid-result. The thread is a daemon, so it never keeps a server
    alive that is already shutting down for another reason.
    """

    def wait_and_stop() -> None:
        remaining = (down_at - now()).total_seconds()
        if remaining > 0:
            sleep(remaining)
        server.should_exit = True

    thread = threading.Thread(target=wait_and_stop, name="tapdrop-ttl", daemon=True)
    thread.start()
    return thread
