"""Header-only FITS image reader and WCS footprint (RDD.md M7).

Reads only what ``tapdrop scan`` needs to describe an image: header cards and
the WCS they define. Pixel data is never touched: the file object fsspec
hands back is passed straight to ``astropy.io.fits.open`` instead of being
read into memory first (unlike ``discovery/catalog.py``'s bintable reader,
which reads whole small catalog files), so astropy seeks past each HDU's data
using ``NAXISn`` rather than fetching it - see ``read_fits_image``.
"""

from __future__ import annotations

from dataclasses import dataclass

import fsspec
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales

# RDD.md M7: "footprint with edge sampling" - corners plus this many samples
# strictly between each pair of corners.
_EDGE_SAMPLES = 4


class NotAnImageError(Exception):
    """No HDU with ``NAXIS`` >= 2 and both axes non-zero was found."""


@dataclass(frozen=True)
class FitsImage:
    """The first image-shaped HDU of a FITS file: header, pixel axes, WCS."""

    uri: str
    hdu_index: int
    header: fits.Header
    naxis1: int
    naxis2: int
    wcs: WCS | None  # None when the header has no celestial WCS (no CTYPEn)


@dataclass(frozen=True)
class Footprint:
    """Edge-sampled sky footprint of one image."""

    vertices: tuple[tuple[float, float], ...]  # (ra_deg [0, 360), dec_deg), closed implicitly
    ra: float  # s_ra: image center, deg
    dec: float  # s_dec: image center, deg
    fov_deg: (
        float  # s_fov: diameter of the smallest circle centered on (ra, dec) covering every vertex
    )
    resolution_deg: float | None  # mean pixel scale, deg/pixel; None if the WCS has no usable scale


def _first_image_hdu(hdul: fits.HDUList) -> tuple[int, fits.Header]:
    for index, hdu in enumerate(hdul):
        header = hdu.header
        if header.get("NAXIS", 0) < 2:
            continue
        naxis1 = int(header.get("NAXIS1", 0))
        naxis2 = int(header.get("NAXIS2", 0))
        if naxis1 > 0 and naxis2 > 0:
            return index, header
    raise NotAnImageError("no HDU with NAXIS >= 2 and non-zero axes")


def read_fits_image(uri: str) -> FitsImage:
    """Read the first image-shaped HDU's header and WCS from ``uri``.

    ``uri`` is resolved with ``fsspec`` the same way ``discovery/catalog.py``
    resolves remote sources (``fsspec.core.url_to_fs``), but the file object
    is handed to ``astropy.io.fits.open`` directly rather than read into
    memory first: with ``lazy_load_hdus=True``, astropy parses each HDU's
    header and then seeks past its data (computed from ``NAXISn``) instead of
    reading it, so a remote image's pixels are never fetched - see
    ``tests/test_image_fits.py`` for a byte-count assertion of this.
    """
    fs, path = fsspec.core.url_to_fs(uri)
    with (
        fs.open(path, "rb") as fh,
        fits.open(fh, memmap=False, lazy_load_hdus=True, do_not_scale_image_data=True) as hdul,
    ):
        hdu_index, header = _first_image_hdu(hdul)
        naxis1 = int(header["NAXIS1"])
        naxis2 = int(header["NAXIS2"])
        # AIDEV-NOTE: WCS(header) only parses header cards already in memory
        # (CTYPEn/CRVALn/CRPIXn/CDELTn/...); it never touches hdu.data, so
        # building it here keeps read_fits_image header-only.
        wcs = WCS(header, naxis=2)
        image_wcs = wcs if wcs.has_celestial else None
        return FitsImage(
            uri=uri,
            hdu_index=hdu_index,
            header=header.copy(),
            naxis1=naxis1,
            naxis2=naxis2,
            wcs=image_wcs,
        )


def compute_footprint(image: FitsImage) -> Footprint | None:
    """Edge-sampled ICRS footprint of ``image``, or ``None`` with no celestial WCS."""
    wcs = image.wcs
    if wcs is None:
        return None

    corners_px = [
        (0.0, 0.0),
        (float(image.naxis1), 0.0),
        (float(image.naxis1), float(image.naxis2)),
        (0.0, float(image.naxis2)),
    ]
    # AIDEV-NOTE: samples are interpolated in *pixel* space, one point at a
    # time through the WCS, not interpolated in RA/Dec after the fact.
    # Interpolating in world space breaks at both fixtures this module is
    # tested against: across the RA=0 seam, averaging e.g. RA=359.9 and
    # RA=0.1 gives 180 (the antipodal point, not a point on the image); near
    # a pole, a straight line in (RA, Dec) does not track the image edge at
    # all since a fixed RA step covers a shrinking sky distance close to the
    # pole. Projecting each pixel-space sample through the WCS individually
    # sidesteps both failure modes.
    pixel_points: list[tuple[float, float]] = []
    corner_positions: list[int] = []
    for i in range(4):
        x0, y0 = corners_px[i]
        x1, y1 = corners_px[(i + 1) % 4]
        corner_positions.append(len(pixel_points))
        pixel_points.append((x0, y0))
        for k in range(1, _EDGE_SAMPLES + 1):
            t = k / (_EDGE_SAMPLES + 1)
            pixel_points.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))

    xs = [p[0] for p in pixel_points]
    ys = [p[1] for p in pixel_points]
    world_ra, world_dec = wcs.all_pix2world(xs, ys, 0)
    vertices = tuple(
        (float(ra) % 360.0, float(dec)) for ra, dec in zip(world_ra, world_dec, strict=True)
    )

    center_ra_arr, center_dec_arr = wcs.all_pix2world([image.naxis1 / 2.0], [image.naxis2 / 2.0], 0)
    center_ra = float(center_ra_arr[0]) % 360.0
    center_dec = float(center_dec_arr[0])

    # s_fov: diameter of the smallest circle centered on the image center that
    # covers every corner/edge sample, using great-circle separation (not a
    # planar RA/Dec difference, which is wrong near a pole and across RA=0).
    center = SkyCoord(center_ra, center_dec, unit="deg", frame="icrs")
    others = SkyCoord([v[0] for v in vertices], [v[1] for v in vertices], unit="deg", frame="icrs")
    fov_deg = float(center.separation(others).deg.max()) * 2.0

    scales = proj_plane_pixel_scales(wcs)
    resolution_deg = float(sum(abs(s) for s in scales) / len(scales)) if len(scales) else None

    return Footprint(
        vertices=vertices,
        ra=center_ra,
        dec=center_dec,
        fov_deg=fov_deg,
        resolution_deg=resolution_deg,
    )
