"""SODA 1.0 sync cutouts: ``GET|POST /soda/sync`` (RDD.md M11).

The ``#cutout`` service descriptor in ``/datalink/links`` points here. One
``ID`` (a plane URI) plus one shape - ``CIRCLE``, ``POLYGON`` or a DALI ``POS``
string - yields a FITS file cut with ``astropy.nddata.Cutout2D`` against the
chunk's WCS, so the header the client gets back still resolves sky coordinates.
No shape returns the whole image. Either way the pixel box is worked out before
any pixel is read and refused above ``MAX_CUTOUT_PIXELS``.

The only client input that selects a file is the plane URI, looked up in
``caom.artifact``; every number is parsed to a float before it is used. A
remote FITS file is read through fsspec range requests (``hdu.section``), never
downloaded whole. ASDF is served from local files only; a remote ASDF cutout is
a ``501`` until block range reads land (RDD.md v2 backlog).
"""

from __future__ import annotations

import io
import json
import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import duckdb
import fsspec.utils
import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.nddata import Cutout2D, NoOverlapError
from astropy.wcs import WCS
from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from tapdrop.api.datalink import FITS_CONTENT_TYPE, _ids, _parameters, _require_obscore
from tapdrop.api.scs import _collect, _one
from tapdrop.errors import CutoutNotSupportedError, InvalidParameterError, TapdropError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tapdrop.registry import Registry

logger = logging.getLogger("tapdrop")

router = APIRouter()

#: fsspec block size for remote FITS. Each cache miss costs one range request
#: of this many bytes, so this trades request count against bytes fetched for
#: a small cutout. ponytail: fixed 256 KiB, make it a setting if S3 latency bites.
REMOTE_BLOCK_SIZE = 256 * 1024

#: Most pixels one sync cutout may return. The client picks the region, and
#: the cut plus the FITS write hold it in memory at once, so this bounds what
#: a single request can make the server allocate. A safety limit, not a knob:
#: whole images above it go through ``/datalink/file``, which streams.
MAX_CUTOUT_PIXELS = 4096 * 4096

_SHAPE_PARAMS = ("circle", "polygon", "pos")


@dataclass(frozen=True)
class _Chunk:
    artifact_uri: str
    extension: int
    wcs_json: str | None


@dataclass(frozen=True)
class Shape:
    """A parsed cutout region: ``circle`` is ``(lon, lat, radius)``, anything
    else is a flat ``lon lat ...`` vertex list cut to its pixel bounding box."""

    kind: str
    numbers: list[float]


class _MultiValuedParameterError(InvalidParameterError):
    """A parameter given more than once: SODA 1.0 §5.2 ``MultiValuedParamNotSupported``."""


@router.api_route("/soda/sync", methods=["GET", "POST"])
async def soda_sync(request: Request) -> Response:
    registry: Registry = request.app.state.registry
    con: duckdb.DuckDBPyConnection = request.app.state.con

    try:
        _require_obscore(registry)
        # SODA 1.0 §2.1 / DALI 1.1 §2.2: parameters arrive by GET or POST.
        params = await _parameters(request)
        identifiers = _ids(params)
        if not identifiers:
            raise InvalidParameterError("ID", "required")
        if len(identifiers) > 1:
            raise _MultiValuedParameterError("ID", "given more than once")
        shape = parse_shape(_collect(params))
        chunk = _lookup(con, identifiers[0])
        if chunk is None:
            raise InvalidParameterError("ID", f"no such dataset: {identifiers[0]}")
        body = await run_in_threadpool(
            cutout, chunk.artifact_uri, chunk.extension, chunk.wcs_json, shape
        )
    except TapdropError as exc:
        logger.info("SODA request failed: %s", exc.message)
        return _fault(exc)
    except Exception as exc:
        # A vanished file, an S3 read error or astropy choking on the file is
        # still a SODA 1.0 section 5.2 text/plain error, not the TAP VOTable 500.
        logger.exception("SODA cutout failed")
        return Response(content=f"Error: {exc}", status_code=500, media_type="text/plain")

    return Response(
        content=body,
        media_type=FITS_CONTENT_TYPE,
        headers={"Content-Disposition": 'attachment; filename="cutout.fits"'},
    )


def _fault(exc: TapdropError) -> Response:
    # SODA 1.0 section 5.2: errors are text/plain, the message prefixed with
    # one of SODA's own codes (not DataLink's UsageFault/DefaultFault).
    if isinstance(exc, _MultiValuedParameterError):
        code = "MultiValuedParamNotSupported"
    elif exc.http_status < 500:
        code = "UsageError"
    elif exc.http_status == 503:
        code = "ServiceUnavailable"
    else:
        code = "Error"
    return Response(
        content=f"{code}: {exc.message}", status_code=exc.http_status, media_type="text/plain"
    )


def _lookup(con: duckdb.DuckDBPyConnection, plane_uri: str) -> _Chunk | None:
    row = con.execute(
        """
        SELECT a.artifact_uri, coalesce(c.extension, 0), c.wcs_json
        FROM "caom"."artifact" a
        LEFT JOIN "caom"."chunk" c ON c.artifact_uri = a.artifact_uri
        WHERE a.plane_uri = ?
        """,
        [plane_uri],
    ).fetchone()
    if row is None:
        return None
    return _Chunk(
        artifact_uri=str(row[0]),
        extension=int(row[1]),
        wcs_json=None if row[2] is None else str(row[2]),
    )


# --------------------------------------------------------------------------
# shape parameters
# --------------------------------------------------------------------------


def parse_shape(collected: dict[str, list[str]]) -> Shape | None:
    """The one shape in the request, or ``None`` for the whole image.

    AIDEV-NOTE: SODA 1.0 lets async jobs carry several shapes (one result
    each); a sync request has one response body, so more than one shape here
    is a 400 rather than a silent pick.
    """
    given = [name for name in _SHAPE_PARAMS if collected.get(name)]
    if not given:
        return None
    if len(given) > 1:
        raise InvalidParameterError(
            given[0].upper(), "a sync request takes one shape: CIRCLE, POLYGON or POS"
        )
    if len(collected[given[0]]) > 1:
        raise _MultiValuedParameterError(given[0].upper(), "given more than once")
    name = given[0]
    raw = _one(collected, name) or ""
    if name == "circle":
        return _circle("CIRCLE", raw.split())
    if name == "polygon":
        return _polygon("POLYGON", raw.split())
    return _pos(raw)


def _pos(value: str) -> Shape | None:
    parts = value.split()
    if not parts:
        raise InvalidParameterError("POS", "is empty")
    shape = parts[0].lower()
    if shape == "circle":
        return _circle("POS", parts[1:])
    if shape == "polygon":
        return _polygon("POS", parts[1:])
    if shape == "range":
        # SODA 1.0 §3.3: as in DALI intervals, an open bound is -Inf/+Inf.
        # Clamped to the sky here, so the cut never sees an infinity; a range
        # that covers the whole sky is the whole image.
        numbers = _floats("POS", parts[1:], finite=False)
        if len(numbers) != 4:
            raise InvalidParameterError("POS", "RANGE takes exactly 4 numbers: lon1 lon2 lat1 lat2")
        lon1, lon2, lat1, lat2 = numbers
        if lat1 > lat2:
            raise InvalidParameterError("POS", "RANGE latitudes must be given as lat1 <= lat2")
        lon1, lon2 = (min(max(lon, 0.0), 360.0) for lon in (lon1, lon2))
        lat1, lat2 = (min(max(lat, -90.0), 90.0) for lat in (lat1, lat2))
        if lon1 <= 0.0 and lon2 >= 360.0 and lat1 <= -90.0 and lat2 >= 90.0:
            return None
        return Shape("range", [lon1, lon2, lat1, lat2])
    raise InvalidParameterError("POS", f"unknown shape {parts[0]!r}; use CIRCLE, RANGE or POLYGON")


def _circle(name: str, tokens: list[str]) -> Shape:
    numbers = _floats(name, tokens)
    if len(numbers) != 3:
        raise InvalidParameterError(name, "CIRCLE takes exactly 3 numbers: lon lat radius")
    if numbers[2] < 0:
        raise InvalidParameterError(name, "CIRCLE radius must not be negative")
    if not -90.0 <= numbers[1] <= 90.0:
        raise InvalidParameterError(name, "latitude must be within [-90, 90]")
    return Shape("circle", numbers)


def _polygon(name: str, tokens: list[str]) -> Shape:
    numbers = _floats(name, tokens)
    if len(numbers) < 6 or len(numbers) % 2 != 0:
        raise InvalidParameterError(name, "POLYGON takes an even count of at least 6 numbers")
    return Shape("polygon", numbers)


def _floats(name: str, tokens: list[str], *, finite: bool = True) -> list[float]:
    numbers = []
    for token in tokens:
        try:
            numbers.append(float(token))
        except ValueError:
            raise InvalidParameterError(name, f"{token!r} is not a number") from None
    if any(math.isnan(number) for number in numbers):
        raise InvalidParameterError(name, "coordinates must be finite numbers")
    if finite and any(math.isinf(number) for number in numbers):
        raise InvalidParameterError(name, "coordinates must be finite numbers")
    return numbers


# --------------------------------------------------------------------------
# cutting
# --------------------------------------------------------------------------


def cutout(uri: str, extension: int, wcs_json: str | None, shape: Shape | None) -> bytes:
    """FITS bytes for ``shape`` cut from HDU ``extension`` of ``uri``.

    Runs in a thread pool: it does file I/O and astropy work.
    """
    wcs = _wcs(wcs_json) if shape is not None else _wcs_if_any(wcs_json)
    if uri.lower().endswith(".asdf"):
        return _write(_cut_asdf(uri, extension, wcs, shape))
    return _write(_cut_fits(uri, extension, wcs, shape))


def _wcs_if_any(wcs_json: str | None) -> WCS | None:
    return WCS(fits.Header(json.loads(wcs_json))) if wcs_json else None


def _wcs(wcs_json: str | None) -> WCS:
    wcs = _wcs_if_any(wcs_json)
    if wcs is None:
        raise CutoutNotSupportedError(
            "This image has no celestial WCS; only a whole-image request works."
        )
    # AIDEV-NOTE: Non-ICRS frames are a stated non-goal (RDD.md). An empty
    # RADESYS defaults to ICRS in astropy; FK5 J2000 differs from ICRS by far
    # less than a pixel and is accepted as-is.
    if not wcs.has_celestial or wcs.wcs.radesys.upper() not in ("", "ICRS", "FK5"):
        frame = wcs.wcs.radesys or "non-celestial"
        raise CutoutNotSupportedError(f"Cutouts need an ICRS celestial WCS; this image is {frame}.")
    return wcs


def _cut_fits(
    uri: str, extension: int, wcs: WCS | None, shape: Shape | None
) -> tuple[Any, fits.Header]:
    if fsspec.utils.get_protocol(uri) == "file":
        opener = fits.open(uri, memmap=True)
    else:
        # AIDEV-NOTE: hdu.section over an fsspec file turns the cutout into a
        # handful of HTTP range requests instead of a whole-file download.
        opener = fits.open(uri, use_fsspec=True, fsspec_kwargs={"block_size": REMOTE_BLOCK_SIZE})
    with opener as hdul:
        hdu = hdul[extension]
        if not isinstance(hdu, fits.PrimaryHDU | fits.ImageHDU) or hdu.header.get("NAXIS") != 2:
            raise CutoutNotSupportedError(f"HDU {extension} of this dataset is not a 2-D image.")
        return _cut(hdu.section, wcs, shape)


def _cut_asdf(
    uri: str, extension: int, wcs: WCS | None, shape: Shape | None
) -> tuple[Any, fits.Header]:
    if fsspec.utils.get_protocol(uri) != "file":
        raise CutoutNotSupportedError(
            "Cutouts of remote ASDF files are not supported; the file must be local."
        )
    try:
        import asdf
    except ImportError:
        raise CutoutNotSupportedError(
            "ASDF cutouts need the [roman] extra: pip install 'tapdrop[roman]'."
        ) from None
    # AIDEV-NOTE: Roman L2/L3 files keep pixels at roman.data; a plain ASDF
    # image is looked up at the root. `extension` is the caom.chunk node index
    # and is 0 for both today, so it is accepted but not yet used.
    del extension
    with asdf.open(uri, lazy_load=True, memmap=False) as af:
        tree = af.tree
        data = tree["roman"].get("data") if "roman" in tree else tree.get("data")
        if data is None:
            raise CutoutNotSupportedError("This ASDF file has no data array at roman.data or data.")
        # The lazy node knows its shape without loading the block; _cut checks
        # the ceiling on that before slicing it.
        return _cut(data, wcs, shape)


def _cut(data: Any, wcs: WCS | None, shape: Shape | None) -> tuple[Any, fits.Header]:
    """Cut ``shape`` from ``data``, which is only sliced after the box passes the ceiling.

    ``data`` is anything with ``.shape`` and ``__getitem__`` that reads on
    demand: ``hdu.section`` for FITS, the lazy array node for ASDF.
    """
    if shape is None or wcs is None:
        _check_pixels("ID", data.shape)
        return data[:, :], fits.Header() if wcs is None else wcs.to_header()
    position, size = _box(wcs, shape, data.shape)
    try:
        # AIDEV-NOTE: Cutout2D on a zero-byte stand-in of the image's shape
        # gives the trimmed pixel box and the shifted WCS before a single pixel
        # is read, without re-implementing its angular-size and trim logic.
        probe = Cutout2D(
            np.broadcast_to(np.float32(0), data.shape), position, size, wcs=wcs, mode="trim"
        )
    except NoOverlapError:
        raise InvalidParameterError(
            "POS", "the requested region does not overlap the image"
        ) from None
    except (ValueError, TypeError) as exc:
        # Cutout2D reports e.g. a position behind the tangent plane (NaN
        # pixels) or a zero-size box with a plain ValueError.
        raise InvalidParameterError("POS", f"cannot cut this region: {exc}") from None
    _check_pixels("POS", probe.shape)
    return data[probe.slices_original], probe.wcs.to_header()


def _check_pixels(parameter: str, shape: tuple[int, ...]) -> None:
    if math.prod(shape) <= MAX_CUTOUT_PIXELS:
        return
    size = " x ".join(str(n) for n in reversed(shape))
    raise InvalidParameterError(
        parameter,
        f"the requested region is {size} pixels, above the {MAX_CUTOUT_PIXELS}-pixel cutout "
        "limit; ask for a smaller region, or stream the whole file from /datalink/file",
    )


def _box(wcs: WCS, shape: Shape, image_shape: tuple[int, ...]) -> tuple[Any, Any]:
    """``(position, size)`` for ``Cutout2D``.

    A circle is passed as a sky centre and an angular side of ``2 * radius``,
    which Cutout2D converts with the image's own pixel scale. A polygon or
    range is projected to pixels first and cut to its bounding box, which is
    what SODA 1.0 section 3.3 asks for and also sidesteps the RA=0 seam.
    """
    if shape.kind == "range":
        xs, ys = _range_pixels(wcs, shape.numbers, image_shape)
    else:
        vertices = shape.numbers[:2] if shape.kind == "circle" else shape.numbers
        xs, ys = wcs.celestial.wcs_world2pix(vertices[0::2], vertices[1::2], 0)
        if not (np.all(np.isfinite(xs)) and np.all(np.isfinite(ys))):
            # Behind the tangent plane: no pixel to centre on, so no overlap.
            raise InvalidParameterError("POS", "the requested region does not overlap the image")
    if shape.kind == "circle":
        lon, lat, radius = shape.numbers
        return SkyCoord(lon, lat, unit="deg", frame="icrs"), 2 * radius * u.deg
    x0, x1 = math.floor(xs.min()), math.ceil(xs.max())
    y0, y1 = math.floor(ys.min()), math.ceil(ys.max())
    return ((x0 + x1) / 2, (y0 + y1) / 2), (y1 - y0 + 1, x1 - x0 + 1)


def _range_pixels(wcs: WCS, numbers: list[float], image_shape: tuple[int, ...]) -> tuple[Any, Any]:
    """Pixel positions sampled over a ``RANGE``, clipped to the image's own extent.

    AIDEV-NOTE: a range's four corners are not enough once -Inf/+Inf are
    allowed (SODA 1.0 section 3.3). Clamped to a pole, a corner can sit behind
    the tangent plane and project to NaN, and the two meridians of a narrow
    pole-to-pole strip are straight lines in a TAN projection that fan out
    far from the image, so the bounding box of the *whole* range spans the
    image even where range-and-image is a sliver. The range is therefore
    clipped to the footprint's lon/lat box first - lon left alone when either
    the footprint or the range wraps through 0, lat left alone on a side whose
    pole lies inside the image - and then sampled on a grid; only the samples
    that project feed the bounding box, and none at all is "no overlap".
    """
    lon1, lon2, lat1, lat2 = numbers
    ny, nx = image_shape
    footprint = wcs.celestial.calc_footprint(axes=(nx, ny))
    lons, lats = footprint[:, 0], footprint[:, 1]
    wraps = lon1 > lon2
    if not wraps and lons.max() - lons.min() <= 180.0:
        lon1, lon2 = max(lon1, lons.min()), min(lon2, lons.max())
    px, py = wcs.celestial.wcs_world2pix([0.0, 0.0], [-90.0, 90.0], 0)
    pole_inside = (
        np.isfinite(px)
        & np.isfinite(py)
        & (px > -0.5)
        & (px < nx - 0.5)
        & (py > -0.5)
        & (py < ny - 0.5)
    )
    if not pole_inside[0]:
        lat1 = max(lat1, lats.min())
    if not pole_inside[1]:
        lat2 = min(lat2, lats.max())
    if (not wraps and lon1 > lon2) or lat1 > lat2:
        raise InvalidParameterError("POS", "the requested region does not overlap the image")
    lon_steps = np.linspace(lon1, lon2 + 360.0 if wraps else lon2, 33) % 360.0
    lon_grid, lat_grid = np.meshgrid(lon_steps, np.linspace(lat1, lat2, 33))
    xs, ys = wcs.celestial.wcs_world2pix(lon_grid.ravel(), lat_grid.ravel(), 0)
    keep = np.isfinite(xs) & np.isfinite(ys)
    if not keep.any():
        raise InvalidParameterError("POS", "the requested region does not overlap the image")
    return xs[keep], ys[keep]


def _write(cut: tuple[Any, fits.Header]) -> bytes:
    data, header = cut
    buffer = io.BytesIO()
    fits.HDUList([fits.PrimaryHDU(data=np.asarray(data), header=header)]).writeto(buffer)
    return buffer.getvalue()
