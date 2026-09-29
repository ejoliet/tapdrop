"""``tapdrop scan`` orchestration and ``tapdrop.discovered.yaml`` I/O (RDD.md M7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tapdrop.discovery import scan

DATA_DIR = Path(__file__).parent / "data" / "images"


def _uri(name: str) -> str:
    return str((DATA_DIR / name).resolve())


# --------------------------------------------------------------------------
# _list_fits_files
# --------------------------------------------------------------------------


def test_list_fits_files_single_file() -> None:
    files, skipped = scan._list_fits_files(_uri("basic.fits"))
    assert files == [_uri("basic.fits")]
    assert skipped == []


def test_list_fits_files_directory_lists_all_fits() -> None:
    files, skipped = scan._list_fits_files(str(DATA_DIR))
    assert skipped == []
    names = {Path(f).name for f in files}
    assert {"basic.fits", "ra0.fits", "pole.fits", "no_wcs.fits", "corrupt.fits"} <= names


def test_list_fits_files_glob() -> None:
    files, skipped = scan._list_fits_files(str(DATA_DIR / "*.fits"))
    assert skipped == []
    assert {Path(f).name for f in files} >= {"basic.fits", "ra0.fits"}


def test_list_fits_files_glob_no_match_is_skipped() -> None:
    files, skipped = scan._list_fits_files(str(DATA_DIR / "*.nope"))
    assert files == []
    assert len(skipped) == 1
    assert "glob matched no FITS files" in skipped[0].reason


def test_list_fits_files_not_fits_is_skipped(tmp_path: Path) -> None:
    other = tmp_path / "notes.txt"
    other.write_text("hello")
    files, skipped = scan._list_fits_files(str(other))
    assert files == []
    assert len(skipped) == 1
    assert "not a FITS file" in skipped[0].reason


# --------------------------------------------------------------------------
# scan_images: field extraction + provenance/confidence
# --------------------------------------------------------------------------


def test_scan_images_basic_fully_resolved() -> None:
    catalog = scan.scan_images(_uri("basic.fits"))
    assert len(catalog.observations) == 1
    obs = catalog.observations[0]

    assert obs.profile == "generic-fits-wcs"
    assert obs.unresolved == ()

    assert obs.fields["obs_collection"].value == "TAPDROP-TEST"
    assert obs.fields["obs_collection"].confidence == "high"
    assert obs.fields["t_exptime"].value == 60.0
    assert obs.fields["t_exptime"].source == "EXPTIME"
    assert obs.fields["em_filter"].value == "V"
    assert obs.fields["em_min"].value < obs.fields["em_max"].value
    assert obs.fields["naxis1"].value == 100
    assert obs.fields["naxis2"].value == 100
    assert obs.fields["s_ra"].confidence == "high"
    assert obs.fields["s_region"].value.startswith("POLYGON ICRS ")
    assert obs.fields["s_resolution"].value == 0.0005 * 3600.0

    # DATE-OBS/DATE-END give an exact 1-minute span, matching EXPTIME=60s.
    assert obs.fields["t_max"].value - obs.fields["t_min"].value == pytest.approx(60.0 / 86400.0)


def test_scan_images_no_wcs_leaves_spatial_and_optional_fields_unresolved() -> None:
    catalog = scan.scan_images(_uri("no_wcs.fits"))
    assert len(catalog.observations) == 1
    obs = catalog.observations[0]

    for name in ("s_ra", "s_dec", "s_fov", "s_region", "s_resolution"):
        assert name in obs.unresolved
        assert name not in obs.fields
    for name in ("t_exptime", "t_min", "t_max", "em_filter", "em_min", "em_max"):
        assert name in obs.unresolved

    # obs_id falls back to the filename stem when OBS_ID/OBSID is absent.
    assert obs.fields["obs_id"].value == "no_wcs"
    assert obs.fields["obs_id"].source == "filename"
    assert obs.fields["obs_id"].confidence == "medium"
    # obs_collection falls back to the profile default when TELESCOP is absent.
    assert obs.fields["obs_collection"].value == "UNKNOWN"
    assert obs.fields["obs_collection"].confidence == "low"


def test_scan_images_ra0_and_pole_have_resolved_footprints() -> None:
    for name in ("ra0.fits", "pole.fits"):
        catalog = scan.scan_images(_uri(name))
        obs = catalog.observations[0]
        assert "s_region" in obs.fields
        assert "s_ra" not in obs.unresolved


# --------------------------------------------------------------------------
# scan_images: a bad file is skipped, never fatal
# --------------------------------------------------------------------------


def test_scan_images_skips_corrupt_file_without_raising() -> None:
    catalog = scan.scan_images(str(DATA_DIR))
    names = {Path(o.files).name for o in catalog.observations}
    assert "corrupt.fits" not in names
    skipped_names = {Path(s.uri).name: s.reason for s in catalog.skipped}
    assert "corrupt.fits" in skipped_names
    assert skipped_names["corrupt.fits"]  # a reason is recorded, not blank

    # Every other fixture in the directory still gets scanned.
    assert {"basic.fits", "ra0.fits", "pole.fits", "no_wcs.fits"} <= names


# --------------------------------------------------------------------------
# YAML round-trip (required by RDD.md M7's done-when criteria)
# --------------------------------------------------------------------------


def test_discovered_yaml_round_trip(tmp_path: Path) -> None:
    catalog = scan.scan_images(str(DATA_DIR))
    out = tmp_path / "tapdrop.discovered.yaml"

    scan.write_discovered_yaml(catalog, out)
    round_tripped = scan.read_discovered_yaml(out)

    assert round_tripped.observations == catalog.observations
    assert round_tripped.skipped == catalog.skipped


def test_discovered_yaml_shape_matches_rdd(tmp_path: Path) -> None:
    catalog = scan.scan_images(_uri("basic.fits"))
    out = tmp_path / "tapdrop.discovered.yaml"
    scan.write_discovered_yaml(catalog, out)

    import yaml

    doc = yaml.safe_load(out.read_text())
    assert "observations" in doc
    entry = doc["observations"][0]
    assert entry["profile"] == "generic-fits-wcs"
    assert entry["files"] == _uri("basic.fits")
    assert entry["fields"]["t_exptime"] == {"value": 60.0, "from": "EXPTIME", "confidence": "high"}
    assert entry["unresolved"] == []
