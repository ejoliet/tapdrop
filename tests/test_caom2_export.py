"""``tapdrop export --caom2-xml``: CAOM2 documents per observation (RDD.md M11)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tapdrop.caom2_export import _as_uri, _enum, _filename, _unique, export_caom2
from tapdrop.cli import app
from tapdrop.discovery.scan import scan_sources

pytest.importorskip("caom2", reason="CAOM2 export needs the [export] extra")

from caom2 import CalibrationLevel, DataProductType, ObservationReader

DATA_DIR = Path(__file__).parent / "data"
IMAGE_DIR = DATA_DIR / "images"

runner = CliRunner()


@pytest.fixture
def exported(tmp_path: Path) -> dict[str, Path]:
    catalog = scan_sources([str(IMAGE_DIR)])
    written = export_caom2(catalog, tmp_path / "caom2")
    return {path.stem: path for path in written}


def read(path: Path) -> object:
    """Read a document back through the caom2 package's validating reader."""
    return ObservationReader(validate=True).read(str(path))


def test_one_document_per_observation(exported: dict[str, Path]) -> None:
    # The four readable image fixtures; corrupt.fits is skipped by the scan.
    assert set(exported) == {
        "TAPDROP-TEST_basic",
        "UNKNOWN_no_wcs",
        "UNKNOWN_pole",
        "UNKNOWN_ra0",
    }


def test_documents_validate_against_the_caom2_schema(exported: dict[str, Path]) -> None:
    for path in exported.values():
        assert read(path) is not None


def test_observation_carries_the_scanned_metadata(exported: dict[str, Path]) -> None:
    observation = read(exported["TAPDROP-TEST_basic"])
    assert observation.collection == "TAPDROP-TEST"
    assert observation.observation_id == "basic"
    assert observation.target.name == "M42"
    assert observation.instrument.name == "TAPDROP-CAM"
    assert observation.telescope.name is not None


def test_plane_carries_calibration_level_and_product_type(exported: dict[str, Path]) -> None:
    plane = next(iter(read(exported["TAPDROP-TEST_basic"]).planes.values()))
    assert plane.calibration_level is CalibrationLevel.CALIBRATED
    assert plane.data_product_type is DataProductType.IMAGE


def test_position_polygon_matches_the_scanned_s_region(exported: dict[str, Path]) -> None:
    catalog = scan_sources([str(IMAGE_DIR / "basic.fits")])
    s_region = catalog.observations[0].fields["s_region"].value
    coords = [float(token) for token in str(s_region).split()[2:]]

    plane = next(iter(read(exported["TAPDROP-TEST_basic"]).planes.values()))
    points = plane.position.bounds.points
    assert [c for point in points for c in (point.cval1, point.cval2)] == pytest.approx(coords)
    # The vertex list repeats the points and ends with a CLOSE.
    assert len(plane.position.bounds.samples.vertices) == len(points) + 1


def test_position_is_omitted_when_the_image_has_no_wcs(exported: dict[str, Path]) -> None:
    plane = next(iter(read(exported["UNKNOWN_no_wcs"]).planes.values()))
    assert plane.position is None


def test_artifact_points_at_the_file_and_names_its_extension(exported: dict[str, Path]) -> None:
    plane = next(iter(read(exported["TAPDROP-TEST_basic"]).planes.values()))
    (artifact,) = plane.artifacts.values()
    assert artifact.uri == f"file://{IMAGE_DIR / 'basic.fits'}"
    assert artifact.content_type == "application/fits"
    (part,) = artifact.parts.values()
    assert part.name == "0"
    assert len(part.chunks) == 1


def test_export_is_repeatable(tmp_path: Path) -> None:
    catalog = scan_sources([str(IMAGE_DIR)])
    first = export_caom2(catalog, tmp_path / "out")
    second = export_caom2(catalog, tmp_path / "out")
    assert [p.name for p in first] == [p.name for p in second]


def test_as_uri_leaves_a_real_uri_alone() -> None:
    assert _as_uri("s3://bucket/x.fits") == "s3://bucket/x.fits"
    assert _as_uri("/data/x.fits") == "file:///data/x.fits"


def test_names_that_sanitise_to_the_same_file_do_not_overwrite() -> None:
    """``a/b`` and ``a_b`` both become ``a_b.xml`` before the discriminator."""
    taken: set[str] = set()
    assert _unique(_filename("C", "a/b"), taken) == "C_a_b.xml"
    assert _unique(_filename("C", "a_b"), taken) == "C_a_b-2.xml"
    assert _unique(_filename("C", "a b"), taken) == "C_a_b-3.xml"


def test_unknown_enum_values_do_not_abort_the_export() -> None:
    assert _enum(DataProductType, "not-a-caom2-term") is None
    assert _enum(DataProductType, None) is None
    assert _enum(DataProductType, "image") is DataProductType.IMAGE


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_writes_documents(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["export", str(tmp_path / "out"), "--caom2-xml", "--images", str(IMAGE_DIR)]
    )
    assert result.exit_code == 0, result.output
    assert "Wrote 4 CAOM2 document(s)" in result.output
    assert len(list((tmp_path / "out").glob("*.xml"))) == 4


def test_cli_requires_the_format_flag(tmp_path: Path) -> None:
    result = runner.invoke(app, ["export", str(tmp_path / "out"), "--images", str(IMAGE_DIR)])
    assert result.exit_code != 0
    assert "--caom2-xml" in result.stderr


def test_cli_without_images_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAPDROP_IMAGES", raising=False)
    result = runner.invoke(app, ["export", str(tmp_path / "out"), "--caom2-xml"])
    assert result.exit_code != 0
    assert "Nothing to export" in result.stderr


def test_cli_reads_images_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAPDROP_IMAGES", str(IMAGE_DIR))
    result = runner.invoke(app, ["export", str(tmp_path / "out"), "--caom2-xml"])
    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "out").glob("*.xml"))) == 4
