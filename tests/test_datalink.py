"""DataLink 1.1 links, file serving and PNG previews (RDD.md M10)."""

from __future__ import annotations

import io
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest
from astropy.io.votable import parse as parse_votable
from fastapi.testclient import TestClient

from tapdrop.api.datalink import MAX_IDS
from tapdrop.api.tap import create_app
from tapdrop.caom_lite import DATALINK_CONTENT_TYPE, attach_images
from tapdrop.config import Settings
from tapdrop.discovery import discover
from tapdrop.engine import create_connection

DATA_DIR = Path(__file__).parent / "data"
IMAGE_DIR = DATA_DIR / "images"

BASIC_PLANE = "caom:TAPDROP-TEST/basic/2"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        sources=[str(DATA_DIR / "gaia.parquet")],
        images=[str(IMAGE_DIR)],
        result_store=str(tmp_path / "results"),
    )
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    attach_images(con, registry, settings.images, "http://testserver")
    try:
        yield TestClient(create_app(settings, registry, con))
    finally:
        con.close()


def rows(response: object) -> list[dict[str, str]]:
    table = parse_votable(io.BytesIO(response.content)).get_first_table()  # type: ignore[attr-defined]
    astropy_table = table.to_table(use_names_over_ids=True)
    return [{name: str(row[name]) for name in astropy_table.colnames} for row in astropy_table]


# --------------------------------------------------------------------------
# /datalink/links
# --------------------------------------------------------------------------


def test_links_declares_the_datalink_content_type(client: TestClient) -> None:
    response = client.get("/datalink/links", params={"ID": BASIC_PLANE})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(DATALINK_CONTENT_TYPE)


def test_links_has_the_datalink_field_set(client: TestClient) -> None:
    response = client.get("/datalink/links", params={"ID": BASIC_PLANE})
    table = parse_votable(io.BytesIO(response.content)).get_first_table()
    names = [field.name for field in table.fields]
    assert names == [
        "ID",
        "access_url",
        "service_def",
        "error_message",
        "description",
        "semantics",
        "content_type",
        "content_length",
    ]


def test_links_returns_this_preview_and_cutout(client: TestClient) -> None:
    response = client.get("/datalink/links", params={"ID": BASIC_PLANE})
    by_semantics = {row["semantics"]: row for row in rows(response)}
    assert set(by_semantics) == {"#this", "#preview", "#cutout"}

    assert by_semantics["#this"]["access_url"].endswith(
        "/datalink/file?ID=caom%3ATAPDROP-TEST%2Fbasic%2F2"
    )
    assert by_semantics["#preview"]["content_type"] == "image/png"
    # A service descriptor row carries service_def instead of a URL.
    assert by_semantics["#cutout"]["service_def"] == "cutout"
    assert by_semantics["#cutout"]["access_url"] == ""

    for row in by_semantics.values():
        assert row["ID"] == BASIC_PLANE


def test_links_includes_the_soda_service_descriptor(client: TestClient) -> None:
    response = client.get("/datalink/links", params={"ID": BASIC_PLANE})
    votable = parse_votable(io.BytesIO(response.content))
    descriptors = [resource for resource in votable.resources if resource.type == "meta"]
    assert len(descriptors) == 1

    params = {param.name: param.value for param in descriptors[0].params}
    assert params["standardID"] == "ivo://ivoa.net/std/SODA#sync-1.0"
    assert params["accessURL"].endswith("/soda/sync")

    groups = descriptors[0].groups
    assert [group.name for group in groups] == ["inputParams"]
    input_params = {param.name for param in groups[0].entries}
    assert {"ID", "CIRCLE", "POLYGON", "POS"} <= input_params


def test_links_accepts_several_ids(client: TestClient) -> None:
    other = "caom:UNKNOWN/no_wcs/2"
    response = client.get("/datalink/links", params=[("ID", BASIC_PLANE), ("ID", other)])
    identifiers = {row["ID"] for row in rows(response)}
    assert identifiers == {BASIC_PLANE, other}


def test_links_reports_an_unknown_id_as_a_row_not_an_error(client: TestClient) -> None:
    response = client.get("/datalink/links", params={"ID": "caom:nope/nope/1"})
    assert response.status_code == 200
    (row,) = rows(response)
    assert row["error_message"].startswith("NotFoundFault")
    assert row["access_url"] == ""


def test_links_without_an_id_is_an_empty_table(client: TestClient) -> None:
    """DataLink 1.1 §2.1.1: no ID is a normal response, not an error."""
    response = client.get("/datalink/links")
    assert response.status_code == 200
    assert b'value="OK"' in response.content
    assert rows(response) == []


def test_links_refuses_more_ids_than_the_cap(client: TestClient) -> None:
    response = client.get("/datalink/links", params=[("ID", BASIC_PLANE)] * (MAX_IDS + 1))
    assert response.status_code == 400
    assert b'value="ERROR"' in response.content


def test_links_refuses_a_control_character_in_an_id(client: TestClient) -> None:
    """An ID carrying one could not be escaped into a well-formed document."""
    response = client.get("/datalink/links", params={"ID": "caom:a\x0bb/c/1"})
    assert response.status_code == 400


def test_links_accepts_post(client: TestClient) -> None:
    """DALI 1.1 §2.2: a sync resource takes parameters by GET or POST."""
    response = client.post("/datalink/links", data={"ID": BASIC_PLANE})
    assert response.status_code == 200
    assert {row["ID"] for row in rows(response)} == {BASIC_PLANE}


def test_cutout_descriptor_points_at_the_id_field_not_one_value(client: TestClient) -> None:
    """DataLink 1.1 §4.3: one descriptor serves every row, so ID comes by ref."""
    other = "caom:TAPDROP-TEST/no_wcs/2"
    response = client.get("/datalink/links", params=[("ID", BASIC_PLANE), ("ID", other)])
    body = response.content.decode()
    assert '<FIELD name="ID" ID="dl_id"' in body
    assert '<PARAM name="ID" datatype="char" arraysize="*" ref="dl_id" value=""/>' in body
    assert f'value="{BASIC_PLANE}"' not in body


# --------------------------------------------------------------------------
# /datalink/file
# --------------------------------------------------------------------------


def test_file_serves_the_artifact(client: TestClient) -> None:
    response = client.get("/datalink/file", params={"ID": BASIC_PLANE})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/fits"
    assert response.content[:6] == b"SIMPLE"
    assert response.content == (IMAGE_DIR / "basic.fits").read_bytes()
    assert response.headers["content-length"] == str(len(response.content))


def test_file_refuses_an_unknown_id(client: TestClient) -> None:
    response = client.get("/datalink/file", params={"ID": "caom:nope/nope/1"})
    assert response.status_code == 400


def test_file_cannot_be_pointed_at_an_arbitrary_path(client: TestClient) -> None:
    """The only parameter is a plane URI; a file path is not a valid one."""
    response = client.get("/datalink/file", params={"ID": "/etc/passwd"})
    assert response.status_code == 400
    assert response.text == "Invalid ID: no such dataset: /etc/passwd"


# --------------------------------------------------------------------------
# /datalink/preview
# --------------------------------------------------------------------------


def _png_size(payload: bytes) -> tuple[int, int]:
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", payload[16:24])
    return width, height


def test_preview_returns_a_png(client: TestClient) -> None:
    response = client.get("/datalink/preview", params={"ID": BASIC_PLANE})
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert _png_size(response.content) == (100, 100)


def test_file_of_a_missing_artifact_is_reported_before_the_body_starts(
    client: TestClient, tmp_path: Path
) -> None:
    """A StreamingResponse that fails on its first chunk has already sent 200."""
    con = client.app.state.con  # type: ignore[attr-defined]
    plane = "caom:TAPDROP-TEST/gone/2"
    con.execute(
        'INSERT INTO "caom"."artifact" (artifact_uri, plane_uri, content_type, content_length) '
        "VALUES (?, ?, 'application/fits', NULL)",
        [str(tmp_path / "gone.fits"), plane],
    )
    response = client.get("/datalink/file", params={"ID": plane})
    assert response.status_code == 500
    assert response.text.startswith("DefaultFault")


def test_preview_is_cached_on_disk_and_reused(client: TestClient, tmp_path: Path) -> None:
    cache = tmp_path / "results" / "previews"
    first = client.get("/datalink/preview", params={"ID": BASIC_PLANE})
    cached = list(cache.glob("*.png"))
    assert len(cached) == 1

    # Overwrite the cache entry: a second request must serve the cached bytes,
    # which is only observable if it does not regenerate them.
    cached[0].write_bytes(first.content + b"cache-marker")
    second = client.get("/datalink/preview", params={"ID": BASIC_PLANE})
    assert second.content.endswith(b"cache-marker")


def test_preview_of_an_unknown_id_is_refused(client: TestClient) -> None:
    response = client.get("/datalink/preview", params={"ID": "caom:nope/nope/1"})
    assert response.status_code == 400


def test_preview_downsamples_a_large_image(tmp_path: Path) -> None:
    import numpy as np
    from astropy.io import fits

    from tapdrop.preview import MAX_SIDE, render_preview

    path = tmp_path / "big.fits"
    fits.PrimaryHDU(np.arange(1200 * 1200, dtype="float32").reshape(1200, 1200)).writeto(path)

    png = render_preview(str(path), 0, None)
    width, height = _png_size(png)
    assert max(width, height) <= MAX_SIDE


def test_preview_of_a_flat_image_is_all_black(tmp_path: Path) -> None:
    import numpy as np
    from astropy.io import fits

    from tapdrop.preview import render_preview

    path = tmp_path / "flat.fits"
    fits.PrimaryHDU(np.zeros((16, 16), dtype="float32")).writeto(path)

    png = render_preview(str(path), 0, None)
    assert _png_size(png) == (16, 16)


def test_preview_reads_a_stride_not_the_whole_hdu(tmp_path: Path) -> None:
    """A preview of a 20 GB image must not pull 20 GB through memory."""
    import numpy as np
    from astropy.io import fits

    from tapdrop.preview import render_preview

    path = tmp_path / "big.fits"
    fits.PrimaryHDU(np.arange(1200 * 1200, dtype="float32").reshape(1200, 1200)).writeto(path)

    with fits.open(path) as hdul:
        hdu_type = type(hdul[0])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            hdu_type,
            "data",
            property(lambda self: pytest.fail("the whole HDU was read")),
        )
        assert _png_size(render_preview(str(path), 0, None))[0] <= 512


def test_the_preview_cache_is_not_readable_by_other_users(tmp_path: Path) -> None:
    from tapdrop.preview import render_preview

    cache = tmp_path / "previews"
    render_preview(str(IMAGE_DIR / "basic.fits"), 0, cache)
    assert cache.stat().st_mode & 0o077 == 0


def test_preview_of_a_headerless_hdu_raises(tmp_path: Path) -> None:
    from astropy.io import fits

    from tapdrop.preview import PreviewError, render_preview

    path = tmp_path / "nodata.fits"
    fits.PrimaryHDU().writeto(path)

    with pytest.raises(PreviewError):
        render_preview(str(path), 0, None)
