"""CLI surface: the contract in RDD.md must stay reachable from the shell."""

from __future__ import annotations

from typer.testing import CliRunner

from tapdrop import __version__
from tapdrop.cli import app

runner = CliRunner()


def test_version_prints_the_package_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_bare_invocation_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "serve" in result.stdout
    assert "inspect" in result.stdout


def test_serve_help_exposes_the_contract_flags() -> None:
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    for flag in ("--ttl", "--share", "--token", "--allow-upload", "--result-store", "--cache"):
        assert flag in result.stdout
