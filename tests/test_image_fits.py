"""Header-only FITS reader and WCS footprint (``discovery/image_fits.py``, RDD.md M7)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from tapdrop.discovery import image_fits

DATA_DIR = Path(__file__).parent / "data" / "images"


def _uri(name: str) -> str:
    return str((DATA_DIR / name).resolve())


# --------------------------------------------------------------------------
# read_fits_image
# --------------------------------------------------------------------------


def test_read_fits_image_basic_axes_and_wcs() -> None:
    image = image_fits.read_fits_image(_uri("basic.fits"))
    assert image.naxis1 == 100
    assert image.naxis2 == 100
    assert image.wcs is not None
    assert image.wcs.has_celestial


def test_read_fits_image_no_wcs_has_none_wcs() -> None:
    image = image_fits.read_fits_image(_uri("no_wcs.fits"))
    assert image.naxis1 == 10
    assert image.naxis2 == 10
    assert image.wcs is None


def test_read_fits_image_corrupt_file_raises() -> None:
    with pytest.raises(Exception):  # noqa: B017 - any astropy parse failure is fine
        image_fits.read_fits_image(_uri("corrupt.fits"))


def test_read_fits_image_never_reads_pixel_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """basic.fits carries a 100x100 float32 payload (40000 bytes); header-only
    reads must never pull that far into the file."""
    real_bytes = (DATA_DIR / "basic.fits").read_bytes()
    header_size = 2880  # one FITS header block; the payload starts well after this
    assert len(real_bytes) > header_size + 40_000

    class _CountingFile(io.BytesIO):
        max_offset_seen = 0

        def read(self, size: int = -1) -> bytes:
            data = super().read(size)
            type(self).max_offset_seen = max(type(self).max_offset_seen, self.tell())
            return data

    class _FakeFS:
        def open(self, path: str, mode: str = "rb") -> _CountingFile:
            return _CountingFile(real_bytes)

    monkeypatch.setattr(image_fits.fsspec.core, "url_to_fs", lambda uri: (_FakeFS(), "basic.fits"))
    image_fits.read_fits_image("basic.fits")
    # Pixel data lives past the header; a header-only read stays inside it.
    assert _CountingFile.max_offset_seen <= header_size


# --------------------------------------------------------------------------
# compute_footprint
# --------------------------------------------------------------------------


def test_compute_footprint_none_without_wcs() -> None:
    image = image_fits.read_fits_image(_uri("no_wcs.fits"))
    assert image_fits.compute_footprint(image) is None


def test_compute_footprint_basic_center_and_vertex_count() -> None:
    image = image_fits.read_fits_image(_uri("basic.fits"))
    footprint = image_fits.compute_footprint(image)
    assert footprint is not None
    # 4 corners + 4 edge samples per edge = 20 vertices.
    assert len(footprint.vertices) == 20
    # Independently computed via WCS.all_pix2world at pixel (50, 50) - see
    # implementation-notes.md for the probe command.
    assert footprint.ra == pytest.approx(199.999746, abs=1e-5)
    assert footprint.dec == pytest.approx(-9.99975, abs=1e-5)
    assert footprint.fov_deg > 0
    assert footprint.resolution_deg == pytest.approx(0.0005, abs=1e-9)
    for ra, dec in footprint.vertices:
        assert 0.0 <= ra < 360.0
        assert -90.0 <= dec <= 90.0


def test_compute_footprint_crosses_ra_zero_seam() -> None:
    """ra0.fits (CRVAL1=0, equator): corners independently computed via
    WCS.all_pix2world straddle the 0/360 seam (~0.47 and ~359.48)."""
    image = image_fits.read_fits_image(_uri("ra0.fits"))
    footprint = image_fits.compute_footprint(image)
    assert footprint is not None

    ras = [ra for ra, _dec in footprint.vertices]
    assert any(ra < 10.0 for ra in ras)
    assert any(ra > 350.0 for ra in ras)
    # Center sits at RA ~ 359.975 (just west of the seam), not at 180 - the
    # wrong answer naive world-space corner-averaging would give.
    assert footprint.ra == pytest.approx(359.975, abs=1e-3) or footprint.ra == pytest.approx(
        -0.025 % 360, abs=1e-3
    )
    assert footprint.dec == pytest.approx(0.025, abs=1e-3)
    # A seam-crossing footprint must not be reported as if it spanned ~360 deg
    # of sky; edge sampling keeps s_fov small (image is ~1 deg across).
    assert footprint.fov_deg < 5.0


def test_compute_footprint_near_pole() -> None:
    """pole.fits (CRVAL2=89.95, north): corners independently computed via
    WCS.all_pix2world spread across nearly all RA values despite the image
    covering under a degree of sky - the signature of a pole-adjacent image."""
    image = image_fits.read_fits_image(_uri("pole.fits"))
    footprint = image_fits.compute_footprint(image)
    assert footprint is not None

    decs = [dec for _ra, dec in footprint.vertices]
    assert min(decs) > 89.0
    assert footprint.dec == pytest.approx(89.958769, abs=1e-4)
    # Corners spread across a wide range of RA - confirms pixel-space
    # sampling (not naive RA/Dec-space interpolation) was used near the pole.
    ras = [ra for ra, _dec in footprint.vertices]
    assert max(ras) - min(ras) > 90.0
    # The image is under a degree across on the sky even though its corner
    # RAs differ wildly.
    assert footprint.fov_deg < 2.0
