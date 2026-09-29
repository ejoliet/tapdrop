"""SODA 1.0 sync cutouts (RDD.md M11).

Cutouts are checked against ``astropy.nddata.Cutout2D`` run directly on the
whole array. Remote FITS goes through a local ``http.server`` that honours
``Range`` and counts the bytes it serves, which is how the range-read claim is
proven. Never a real network.
"""

from __future__ import annotations

import functools
import http.server
import io
import json
import re
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import duckdb
import numpy as np
import pytest
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.nddata import Cutout2D
from astropy.wcs import WCS
from fastapi.testclient import TestClient

from tapdrop.api import soda
from tapdrop.api.tap import create_app
from tapdrop.caom_lite import attach_images
from tapdrop.config import Settings
from tapdrop.discovery import discover
from tapdrop.engine import create_connection

DATA_DIR = Path(__file__).parent / "data"
IMAGE_DIR = DATA_DIR / "images"

BASIC_PLANE = "caom:TAPDROP-TEST/basic/2"
NO_WCS_PLANE = "caom:UNKNOWN/no_wcs/2"

#: A TAN projection at RA=200, Dec=-10 with 1.8 arcsec pixels.
HEADER = {
    "CTYPE1": "RA---TAN",
    "CTYPE2": "DEC--TAN",
    "CRVAL1": 200.0,
    "CRVAL2": -10.0,
    "CRPIX1": 100.5,
    "CRPIX2": 100.5,
    "CDELT1": -0.0005,
    "CDELT2": 0.0005,
    "CUNIT1": "deg",
    "CUNIT2": "deg",
}


@dataclass
class Service:
    client: TestClient
    con: duckdb.DuckDBPyConnection

    def register(self, plane_uri: str, uri: str, header: dict[str, object] | None) -> None:
        """Insert one synthetic artifact + chunk so ``plane_uri`` resolves to ``uri``."""
        wcs_json = None if header is None else json.dumps(dict(WCS(header).to_header().items()))
        self.con.execute(
            'INSERT INTO "caom"."artifact" VALUES (?, ?, ?, ?, ?, ?)',
            [uri, plane_uri, "", "application/fits", None, "science"],
        )
        self.con.execute(
            'INSERT INTO "caom"."chunk" VALUES (?, ?, ?, ?, ?, ?)',
            [f"{uri}#0", uri, 0, None, None, wcs_json],
        )


@pytest.fixture
def service(tmp_path: Path) -> Iterator[Service]:
    settings = Settings(
        sources=[str(DATA_DIR / "gaia.parquet")],
        images=[str(IMAGE_DIR)],
        result_store=str(tmp_path / "results"),
    )
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    attach_images(con, registry, settings.images, "http://testserver")
    try:
        yield Service(TestClient(create_app(settings, registry, con)), con)
    finally:
        con.close()


def write_image(path: Path, side: int = 200) -> np.ndarray:
    """A FITS image whose pixel values are their own flat index, so a wrong
    slice is visible in the data, not just in the header."""
    data = np.arange(side * side, dtype="float32").reshape(side, side)
    hdu = fits.PrimaryHDU(data=data)
    for key, value in HEADER.items():
        hdu.header[key] = value
    hdu.writeto(path, overwrite=True)
    return data


def read_fits(content: bytes) -> tuple[np.ndarray, fits.Header]:
    with fits.open(io.BytesIO(content)) as hdul:
        return np.asarray(hdul[0].data), hdul[0].header.copy()


def assert_same_cutout(content: bytes, expected: Cutout2D) -> None:
    data, header = read_fits(content)
    np.testing.assert_array_equal(data, expected.data)
    # The header must carry the *shifted* WCS: the cutout's own pixel (0, 0)
    # has to land on the same sky position as the original pixel it came from.
    returned = WCS(header).pixel_to_world(0, 0)
    original = expected.wcs.pixel_to_world(0, 0)
    assert returned.separation(original) < 1e-6 * u.arcsec
    assert header["CRPIX1"] == pytest.approx(expected.wcs.wcs.crpix[0])
    assert header["CRPIX2"] == pytest.approx(expected.wcs.wcs.crpix[1])


# --------------------------------------------------------------------------
# cutouts vs direct Cutout2D
# --------------------------------------------------------------------------


def test_circle_matches_direct_cutout2d(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "img.fits"
    data = write_image(path)
    service.register("caom:T/img/1", str(path), HEADER)

    response = service.client.get(
        "/soda/sync", params={"ID": "caom:T/img/1", "CIRCLE": "200.01 -10.005 0.004"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/fits"

    expected = Cutout2D(
        data, SkyCoord(200.01, -10.005, unit="deg"), 0.008 * u.deg, wcs=WCS(HEADER), mode="trim"
    )
    assert expected.data.shape == (16, 16)
    assert_same_cutout(response.content, expected)


def test_pos_circle_is_the_same_as_circle(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "img.fits"
    write_image(path)
    service.register("caom:T/img/1", str(path), HEADER)

    via_circle = service.client.get(
        "/soda/sync", params={"ID": "caom:T/img/1", "CIRCLE": "200.01 -10.005 0.004"}
    )
    via_pos = service.client.get(
        "/soda/sync", params={"ID": "caom:T/img/1", "POS": "CIRCLE 200.01 -10.005 0.004"}
    )
    assert via_pos.status_code == 200
    assert via_pos.content == via_circle.content


def test_polygon_cuts_the_pixel_bounding_box(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "img.fits"
    data = write_image(path)
    service.register("caom:T/img/1", str(path), HEADER)
    wcs = WCS(HEADER)

    vertices = [200.02, -10.01, 200.0, -10.012, 199.99, -9.995]
    response = service.client.get(
        "/soda/sync",
        params={"ID": "caom:T/img/1", "POLYGON": " ".join(str(v) for v in vertices)},
    )
    assert response.status_code == 200

    xs, ys = wcs.wcs_world2pix(vertices[0::2], vertices[1::2], 0)
    x0, x1 = int(np.floor(xs.min())), int(np.ceil(xs.max()))
    y0, y1 = int(np.floor(ys.min())), int(np.ceil(ys.max()))
    expected = Cutout2D(
        data, ((x0 + x1) / 2, (y0 + y1) / 2), (y1 - y0 + 1, x1 - x0 + 1), wcs=wcs, mode="trim"
    )
    assert_same_cutout(response.content, expected)
    # Every vertex must sit inside the returned cutout.
    _, header = read_fits(response.content)
    px, py = WCS(header).wcs_world2pix(vertices[0::2], vertices[1::2], 0)
    assert (px >= -0.5).all() and (px <= expected.data.shape[1] - 0.5).all()
    assert (py >= -0.5).all() and (py <= expected.data.shape[0] - 0.5).all()


def test_pos_range_covers_its_corners(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "img.fits"
    write_image(path)
    service.register("caom:T/img/1", str(path), HEADER)

    response = service.client.get(
        "/soda/sync", params={"ID": "caom:T/img/1", "POS": "RANGE 199.99 200.01 -10.01 -9.99"}
    )
    assert response.status_code == 200
    data, header = read_fits(response.content)
    corners = SkyCoord([199.99, 200.01, 200.01, 199.99], [-10.01, -10.01, -9.99, -9.99], unit="deg")
    px, py = WCS(header).world_to_pixel(corners)
    assert (px >= -0.5).all() and (px <= data.shape[1] - 0.5).all()
    assert (py >= -0.5).all() and (py <= data.shape[0] - 0.5).all()


def test_pos_range_open_bounds_are_clamped_to_the_sky(service: Service, tmp_path: Path) -> None:
    """SODA 1.0 section 3.3: RANGE takes -Inf/+Inf as open bounds, like DALI intervals."""
    path = tmp_path / "img.fits"
    write_image(path)
    service.register("caom:T/img/1", str(path), HEADER)

    # Open on every side: the whole sky, so the whole image.
    whole = service.client.get(
        "/soda/sync", params={"ID": "caom:T/img/1", "POS": "RANGE -Inf +Inf -Inf +Inf"}
    )
    assert whole.status_code == 200, whole.text
    assert read_fits(whole.content)[0].shape == (200, 200)

    # A pole-to-pole strip of longitude: full height, only the strip's width,
    # even though its +90 corner projects behind this image's tangent plane.
    strip = service.client.get(
        "/soda/sync", params={"ID": "caom:T/img/1", "POS": "RANGE 199.99 200.01 -Inf +Inf"}
    )
    assert strip.status_code == 200, strip.text
    data, header = read_fits(strip.content)
    assert data.shape[0] == 200
    assert data.shape[1] < 60
    px, _ = WCS(header).world_to_pixel(SkyCoord([199.99, 200.01], [-10.0, -10.0], unit="deg"))
    assert (px >= -0.5).all() and (px <= data.shape[1] - 0.5).all()

    # A strip elsewhere on the sky still does not overlap.
    miss = service.client.get(
        "/soda/sync", params={"ID": "caom:T/img/1", "POS": "RANGE 10 20 -Inf +Inf"}
    )
    assert miss.status_code == 400
    assert "does not overlap" in miss.text


def test_post_is_accepted_with_the_same_result_as_get(service: Service, tmp_path: Path) -> None:
    """SODA 1.0 section 2.1 / DALI 1.1 section 2.2: parameters by GET or POST."""
    path = tmp_path / "img.fits"
    write_image(path)
    service.register("caom:T/img/1", str(path), HEADER)
    params = {"ID": "caom:T/img/1", "CIRCLE": "200.01 -10.005 0.004"}

    via_get = service.client.get("/soda/sync", params=params)
    via_post = service.client.post("/soda/sync", data=params)
    assert via_post.status_code == 200, via_post.text
    assert via_post.content == via_get.content


def test_cutout_partly_off_the_image_is_trimmed(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "img.fits"
    data = write_image(path)
    service.register("caom:T/img/1", str(path), HEADER)

    # Centre on the image corner: the full box would run off two edges.
    corner = WCS(HEADER).pixel_to_world(0, 0)
    response = service.client.get(
        "/soda/sync",
        params={"ID": "caom:T/img/1", "CIRCLE": f"{corner.ra.deg} {corner.dec.deg} 0.002"},
    )
    assert response.status_code == 200
    expected = Cutout2D(data, corner, 0.004 * u.deg, wcs=WCS(HEADER), mode="trim")
    assert_same_cutout(response.content, expected)


def test_no_shape_returns_the_whole_image_with_its_wcs(service: Service) -> None:
    response = service.client.get("/soda/sync", params={"ID": BASIC_PLANE})
    assert response.status_code == 200
    data, header = read_fits(response.content)
    assert data.shape == (100, 100)
    assert header["CTYPE1"] == "RA---TAN"
    assert header["CRVAL1"] == 200.0


def test_whole_image_without_wcs_still_works(service: Service) -> None:
    response = service.client.get("/soda/sync", params={"ID": NO_WCS_PLANE})
    assert response.status_code == 200
    data, _ = read_fits(response.content)
    assert data.shape == (10, 10)


# --------------------------------------------------------------------------
# remote FITS: range reads, not a download
# --------------------------------------------------------------------------


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """``SimpleHTTPRequestHandler`` plus ``Range`` support and a byte counter.

    The stdlib handler ignores ``Range`` and fsspec refuses a 200 where it
    asked for a 206, so a range-capable server is needed to exercise the
    fsspec path at all. Counting what is served is the test's evidence.
    """

    served: ClassVar[list[int]] = []

    def send_head(self) -> io.BytesIO | None:  # type: ignore[override]
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        path = Path(self.translate_path(self.path))
        if match is None or not path.is_file():
            if self.command == "GET" and path.is_file():
                self.served.append(path.stat().st_size)
            head = super().send_head()
            return head  # type: ignore[return-value]
        size = path.stat().st_size
        start = int(match[1])
        end = min(int(match[2]) if match[2] else size - 1, size - 1)
        with path.open("rb") as fh:
            fh.seek(start)
            payload = fh.read(end - start + 1)
        self.send_response(206)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if self.command == "GET":
            self.served.append(len(payload))
        return io.BytesIO(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def range_server(tmp_path: Path) -> Iterator[str]:
    RangeHandler.served = []
    handler = functools.partial(RangeHandler, directory=str(tmp_path))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_remote_fits_is_cut_with_range_reads(
    service: Service, tmp_path: Path, range_server: str
) -> None:
    path = tmp_path / "big.fits"
    data = write_image(path, side=2000)
    file_size = path.stat().st_size
    assert file_size > 16_000_000
    service.register("caom:T/big/1", f"{range_server}/big.fits", HEADER)

    response = service.client.get(
        "/soda/sync", params={"ID": "caom:T/big/1", "CIRCLE": "200.01 -10.005 0.004"}
    )
    assert response.status_code == 200, response.text
    expected = Cutout2D(
        data, SkyCoord(200.01, -10.005, unit="deg"), 0.008 * u.deg, wcs=WCS(HEADER), mode="trim"
    )
    assert_same_cutout(response.content, expected)

    served = sum(RangeHandler.served)
    assert 0 < served < file_size / 4, f"served {served} of {file_size} bytes"


# --------------------------------------------------------------------------
# ASDF
# --------------------------------------------------------------------------


def test_remote_asdf_is_not_implemented(service: Service) -> None:
    service.register("caom:T/roman/1", "https://example.invalid/r0001.asdf", HEADER)
    response = service.client.get(
        "/soda/sync", params={"ID": "caom:T/roman/1", "CIRCLE": "200 -10 0.01"}
    )
    assert response.status_code == 501
    assert "remote ASDF" in response.text
    assert "local" in response.text


def test_local_asdf_without_the_roman_extra_names_it(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "asdf", None)  # makes `import asdf` raise ImportError
    path = tmp_path / "r0001.asdf"
    path.write_bytes(b"#ASDF 1.0.0\n")
    service.register("caom:T/roman/1", str(path), HEADER)

    response = service.client.get(
        "/soda/sync", params={"ID": "caom:T/roman/1", "CIRCLE": "200 -10 0.01"}
    )
    assert response.status_code == 501
    assert "[roman]" in response.text


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


def test_region_off_the_image_is_a_clean_400(service: Service) -> None:
    response = service.client.get(
        "/soda/sync", params={"ID": BASIC_PLANE, "CIRCLE": "10.0 40.0 0.01"}
    )
    assert response.status_code == 400
    assert response.text.startswith("UsageError:")
    assert "does not overlap" in response.text


@pytest.mark.parametrize(
    "params",
    [
        {"CIRCLE": "200 -10 -0.01"},
        {"CIRCLE": "200 -10 nan"},
        {"CIRCLE": "200 -10"},
        {"CIRCLE": "200 -10 inf"},
        {"CIRCLE": "abc -10 0.01"},
        {"POS": "CIRCLE 200 -10 -0.01"},
        {"POS": "BOX 200 -10 1 1"},
        {"POS": "RANGE 199 201 -11"},
        {"POS": ""},
        {"POLYGON": "200 -10 201 -10"},
        {"POLYGON": "200 -10 201 -10 201"},
    ],
)
def test_bad_shapes_are_400(service: Service, params: dict[str, str]) -> None:
    response = service.client.get("/soda/sync", params={"ID": BASIC_PLANE, **params})
    assert response.status_code == 400
    # SODA 1.0 section 5.2 vocabulary, not DataLink's UsageFault.
    assert response.text.startswith("UsageError:")


def test_region_above_the_pixel_ceiling_is_refused_before_reading(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-sky circle trims to the full image; above the ceiling that is a 400
    naming the limit and the size, not an allocation."""
    monkeypatch.setattr(soda, "MAX_CUTOUT_PIXELS", 100 * 100)
    path = tmp_path / "img.fits"
    write_image(path, side=200)
    service.register("caom:T/img/1", str(path), HEADER)

    response = service.client.get(
        "/soda/sync", params={"ID": "caom:T/img/1", "CIRCLE": "200 -10 180"}
    )
    assert response.status_code == 400
    assert response.text.startswith("UsageError:")
    assert "200 x 200" in response.text
    assert "10000-pixel" in response.text

    # A region under the ceiling on the same image still cuts.
    small = service.client.get(
        "/soda/sync", params={"ID": "caom:T/img/1", "CIRCLE": "200.01 -10.005 0.004"}
    )
    assert small.status_code == 200


def test_whole_image_above_the_ceiling_points_at_datalink_file(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(soda, "MAX_CUTOUT_PIXELS", 100 * 100 - 1)
    response = service.client.get("/soda/sync", params={"ID": BASIC_PLANE})
    assert response.status_code == 400
    assert response.text.startswith("UsageError:")
    assert "100 x 100" in response.text
    assert "/datalink/file" in response.text


def test_unreadable_file_is_a_soda_error(service: Service, tmp_path: Path) -> None:
    """An OSError from the file must come back as SODA text/plain, not a VOTable 500."""
    service.register("caom:T/gone/1", str(tmp_path / "gone.fits"), HEADER)
    response = service.client.get("/soda/sync", params={"ID": "caom:T/gone/1"})
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.startswith("Error:")


def test_two_shapes_in_one_sync_request_are_refused(service: Service) -> None:
    response = service.client.get(
        "/soda/sync",
        params={"ID": BASIC_PLANE, "CIRCLE": "200 -10 0.01", "POS": "CIRCLE 200 -10 0.01"},
    )
    assert response.status_code == 400
    assert response.text.startswith("UsageError:")
    # SODA 1.0 section 5.2: a parameter given more than once has its own code.
    repeated = service.client.get(
        "/soda/sync",
        params=[("ID", BASIC_PLANE), ("CIRCLE", "200 -10 0.01"), ("CIRCLE", "200 -10 0.02")],
    )
    assert repeated.status_code == 400
    assert repeated.text.startswith("MultiValuedParamNotSupported:")


def test_shape_on_an_image_without_wcs_is_501(service: Service) -> None:
    response = service.client.get(
        "/soda/sync", params={"ID": NO_WCS_PLANE, "CIRCLE": "200 -10 0.01"}
    )
    assert response.status_code == 501
    assert response.text.startswith("Error:")
    assert "WCS" in response.text


def test_unknown_id_is_400(service: Service) -> None:
    response = service.client.get("/soda/sync", params={"ID": "caom:nope/nope/1"})
    assert response.status_code == 400
    assert "no such dataset" in response.text


def test_id_is_required_and_single(service: Service) -> None:
    missing = service.client.get("/soda/sync")
    assert missing.status_code == 400
    assert missing.text.startswith("UsageError:")
    response = service.client.get("/soda/sync", params=[("ID", BASIC_PLANE), ("ID", NO_WCS_PLANE)])
    assert response.status_code == 400
    assert response.text.startswith("MultiValuedParamNotSupported:")


def test_id_cannot_be_a_file_path(service: Service) -> None:
    """The only file-selecting input is a plane URI looked up in caom.artifact."""
    response = service.client.get("/soda/sync", params={"ID": str(IMAGE_DIR / "basic.fits")})
    assert response.status_code == 400
    assert "no such dataset" in response.text


def test_without_images_the_endpoint_is_an_unknown_table(tmp_path: Path) -> None:
    settings = Settings(
        sources=[str(DATA_DIR / "gaia.parquet")], result_store=str(tmp_path / "results")
    )
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    try:
        client = TestClient(create_app(settings, registry, con))
        response = client.get("/soda/sync", params={"ID": BASIC_PLANE})
    finally:
        con.close()
    assert response.status_code == 400
    assert "ivoa.obscore" in response.text
