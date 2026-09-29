"""Tunnel, QR and TTL tests.

No real tunnel is ever started: ``cloudflared`` is replaced by a shell script
on ``PATH`` that prints what the real binary prints. Nothing here touches the
network.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tapdrop.share import TunnelError, start_tunnel, stop_when_expired, terminal_qr

URL = "https://tender-rock-1234.trycloudflare.com"

# What cloudflared actually prints on stderr, boxed banner and all.
BANNER = f"""\
2026-09-28T00:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...
+--------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at:                   |
|  {URL}                                       |
+--------------------------------------------------------------------+
"""


def fake_cloudflared(tmp_path: Path, script: str) -> None:
    """Put a ``cloudflared`` on PATH that runs *script* instead of tunnelling."""
    binary = tmp_path / "cloudflared"
    binary.write_text(f"#!/bin/sh\n{script}\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{tmp_path}{os.pathsep}{os.environ['PATH']}"


@pytest.fixture
def clean_path() -> object:
    original = os.environ["PATH"]
    yield
    os.environ["PATH"] = original


def test_start_tunnel_returns_the_public_url(tmp_path: Path, clean_path: object) -> None:
    fake_cloudflared(tmp_path, f"cat >&2 <<'EOF'\n{BANNER}EOF\nsleep 30")

    url, process = start_tunnel(8000)
    try:
        assert url == URL
    finally:
        process.terminate()


def test_start_tunnel_passes_the_port_to_cloudflared(tmp_path: Path, clean_path: object) -> None:
    recorded = tmp_path / "argv"
    fake_cloudflared(tmp_path, f"echo \"$@\" > {recorded}\ncat >&2 <<'EOF'\n{BANNER}EOF\nsleep 30")

    _, process = start_tunnel(9123)
    try:
        assert "http://127.0.0.1:9123" in recorded.read_text()
    finally:
        process.terminate()


def test_missing_cloudflared_explains_how_to_install_it(tmp_path: Path, clean_path: object) -> None:
    os.environ["PATH"] = str(tmp_path)  # empty directory: no cloudflared anywhere

    with pytest.raises(TunnelError) as excinfo:
        start_tunnel(8000)

    assert "brew install cloudflared" in str(excinfo.value)
    assert "--share" in str(excinfo.value)


def test_a_tunnel_that_never_prints_a_url_is_an_error(tmp_path: Path, clean_path: object) -> None:
    fake_cloudflared(tmp_path, "echo 'ERR failed to connect' >&2")

    with pytest.raises(TunnelError, match="did not print a tunnel URL"):
        start_tunnel(8000, timeout=5)


def test_terminal_qr_is_a_square_block_of_characters() -> None:
    lines = terminal_qr(URL).splitlines()

    assert lines
    assert len({len(line) for line in lines}) == 1  # every row the same width
    assert set("".join(lines)) <= {"█", "▀", "▄", " "}


class FakeServer:
    should_exit = False


def test_expiry_stops_the_server_after_the_ttl() -> None:
    server = FakeServer()
    slept: list[float] = []
    down_at = datetime.now(UTC) + timedelta(seconds=2)

    thread = stop_when_expired(server, down_at, sleep=slept.append)
    thread.join(timeout=5)

    assert server.should_exit
    assert slept and 0 < slept[0] <= 2


def test_an_expiry_already_in_the_past_stops_the_server_at_once() -> None:
    server = FakeServer()

    def never(_: float) -> None:
        raise AssertionError("must not wait for a deadline that has passed")

    thread = stop_when_expired(server, datetime.now(UTC) - timedelta(hours=1), sleep=never)
    thread.join(timeout=5)

    assert server.should_exit
