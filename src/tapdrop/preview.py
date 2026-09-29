"""PNG previews of discovered images (RDD.md M10).

One asinh-stretched grayscale thumbnail per image, generated on first request
and cached on disk. This is the only place in tapdrop that reads image pixels;
discovery (``discovery/image_fits.py``) deliberately never does.

AIDEV-NOTE: the PNG is written by hand (``_png_bytes``) rather than with
Pillow or matplotlib. A thumbnail is one 8-bit grayscale image with no palette,
no transparency and no interlacing, which is a dozen lines of ``zlib`` -
cheaper than adding an imaging stack to a service whose point is that it
installs in seconds.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from typing import TYPE_CHECKING

import fsspec
import numpy as np
from astropy.io import fits
from astropy.visualization import AsinhStretch, PercentileInterval

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

__all__ = ["CONTENT_TYPE", "MAX_SIDE", "render_preview"]

CONTENT_TYPE = "image/png"

#: Longest side of a generated preview, in pixels. Big enough to recognise the
#: field in TOPCAT or Aladin, small enough to build from a strided read.
MAX_SIDE = 512

#: Percentile clip before the stretch: a few hot pixels otherwise flatten the
#: whole frame to black.
_INTERVAL = PercentileInterval(99.5)


class PreviewError(Exception):
    """The image has no previewable pixel data."""


def render_preview(uri: str, hdu_index: int, cache_dir: Path | None = None) -> bytes:
    """PNG bytes for ``uri``'s image HDU, generated once and then cached.

    ``cache_dir`` of ``None`` disables caching and regenerates every time.
    """
    cache_file = None if cache_dir is None else cache_dir / _cache_name(uri, hdu_index)
    if cache_file is not None and cache_file.is_file():
        return cache_file.read_bytes()

    png = _png_bytes(_thumbnail(uri, hdu_index))
    if cache_file is not None:
        # 0o700: the cache holds renderings of data the service may be serving
        # under a secret token, so it is not readable by other local users.
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)  # type: ignore[union-attr]
        # Written under a temporary name and moved into place: two requests for
        # the same new preview race, and a half-written PNG must never be
        # served to whichever of them loses.
        temporary = cache_file.with_suffix(f".{id(png):x}.part")
        temporary.write_bytes(png)
        temporary.replace(cache_file)
    return png


def _cache_name(uri: str, hdu_index: int) -> str:
    digest = hashlib.sha256(f"{uri}#{hdu_index}".encode()).hexdigest()[:32]
    return f"{digest}.png"


def _thumbnail(uri: str, hdu_index: int) -> np.ndarray:
    """Read the image, downsample it, and stretch it to 8-bit grayscale.

    AIDEV-NOTE: the shape comes from the header and the pixels come through
    ``hdu.section``, so a 20 GB image costs a strided read rather than a full
    one. Touching ``hdu.data`` here would load the whole HDU into memory before
    throwing all but 1/step of it away.

    The messages raised here reach an HTTP client, so they name the HDU but
    never the artifact's path.
    """
    fs, path = fsspec.core.url_to_fs(uri)
    with fs.open(path, "rb") as fh, fits.open(fh, memmap=False) as hdul:
        hdu = hdul[hdu_index]
        shape = tuple(hdu.shape)
        if len(shape) < 2 or 0 in shape[-2:]:
            raise PreviewError(f"HDU {hdu_index} holds no previewable 2-D image")
        step = max(1, -(-max(shape[-2:]) // MAX_SIDE))  # ceil division
        # A cube's higher axes are not previewable; the first plane is.
        strided = slice(None, None, step)
        sample = np.asarray(hdu.section[(0,) * (len(shape) - 2) + (strided, strided)], dtype=float)

    finite = sample[np.isfinite(sample)]
    if finite.size == 0:
        return np.zeros(sample.shape, dtype=np.uint8)
    low, high = _INTERVAL.get_limits(finite)
    if high <= low:
        return np.zeros(sample.shape, dtype=np.uint8)

    normalized = np.clip((sample - low) / (high - low), 0.0, 1.0)
    stretched = AsinhStretch()(np.nan_to_num(normalized, nan=0.0))
    # FITS rows run bottom-up, PNG rows run top-down.
    return np.flipud((np.asarray(stretched) * 255.0).round()).astype(np.uint8)


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def _png_bytes(gray: np.ndarray) -> bytes:
    """An 8-bit grayscale PNG (colour type 0, no filtering) of ``gray``."""
    height, width = gray.shape
    raw = b"".join(b"\x00" + gray[row].tobytes() for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, 6))
        + _png_chunk(b"IEND", b"")
    )
