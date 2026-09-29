"""``tapdrop inspect``: golden-output test plus a CLI smoke test."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from tapdrop.cli import app, format_inspect
from tapdrop.discovery import discover

DATA_DIR = Path(__file__).parent / "data"
GOLDEN_DIR = DATA_DIR / "golden"

runner = CliRunner()


def test_format_inspect_is_stable_and_readable() -> None:
    """Fixed 2-file fixture (tests/data/golden/): one good table, one skipped file.

    The skipped URI is resolved from the fixture path at test time rather than
    hardcoded, since fsspec reports local paths as absolute and that absolute
    prefix is machine-dependent.
    """
    registry = discover([str(GOLDEN_DIR)])
    broken_uri = str((GOLDEN_DIR / "broken.fits").resolve())

    expected = (
        "golden.t1 (3 columns)\n"
        "  ra/dec: ra, dec (rule=ucd, confidence=high)\n"
        "  columns:\n"
        "    - id (long)\n"
        "    - ra (double) [unit=deg, ucd=pos.eq.ra;meta.main]\n"
        "    - dec (double) [unit=deg, ucd=pos.eq.dec;meta.main]\n"
        "skipped:\n"
        f"  - {broken_uri}: discovery failed: No SIMPLE card found, this file "
        "does not appear to be a valid FITS file. If this is really a FITS "
        "file, try with ignore_missing_simple=True"
    )
    assert format_inspect(registry) == expected


def test_format_inspect_empty_registry() -> None:
    registry = discover([])
    assert format_inspect(registry) == "No tables discovered."


def test_inspect_cli_prints_the_golden_output() -> None:
    result = runner.invoke(app, ["inspect", str(GOLDEN_DIR)])
    assert result.exit_code == 0
    assert "golden.t1" in result.stdout
    assert "ra/dec: ra, dec (rule=ucd, confidence=high)" in result.stdout
    assert "skipped:" in result.stdout


def test_inspect_cli_applies_config_overrides() -> None:
    result = runner.invoke(
        app,
        [
            "inspect",
            str(DATA_DIR / "override_src"),
            "--config",
            str(DATA_DIR / "overrides.yaml"),
        ],
    )
    assert result.exit_code == 0
    assert "override_src.widget" in result.stdout
    assert "description: Override test table" in result.stdout
    assert "ra/dec: x, y (rule=override, confidence=override)" in result.stdout
