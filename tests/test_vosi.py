"""VOSI and TAPRegExt tests.

Each document is validated against the vendored IVOA schema, then checked for
the things a schema cannot see: that the service does not promise a capability
it lacks, and that every URL it hands out is one a client can actually follow.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lxml import etree

from conformance import assert_valid
from tapdrop.api.tap import create_app
from tapdrop.api.vosi import ADQL_GEOMETRY_FEATURES, availability_document
from tapdrop.config import Settings
from tapdrop.discovery import discover
from tapdrop.engine import create_connection

DATA_DIR = Path(__file__).parent / "data"

NS = {
    "vosi": "http://www.ivoa.net/xml/VOSICapabilities/v1.0",
    "avail": "http://www.ivoa.net/xml/VOSIAvailability/v1.0",
    "tables": "http://www.ivoa.net/xml/VOSITables/v1.0",
    "vod": "http://www.ivoa.net/xml/VODataService/v1.1",
    "tr": "http://www.ivoa.net/xml/TAPRegExt/v1.0",
}


def build_client(**overrides: object) -> Iterator[TestClient]:
    settings = Settings(sources=[str(DATA_DIR / "gaia.parquet")], **overrides)  # type: ignore[arg-type]
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    try:
        yield TestClient(create_app(settings, registry, con))
    finally:
        con.close()


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield from build_client()


def tree_of(response: object) -> etree._Element:
    return etree.fromstring(response.content)  # type: ignore[attr-defined]


def test_capabilities_validate_against_the_vosi_schema(client: TestClient) -> None:
    response = client.get("/tap/capabilities")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    assert_valid(response.content, "bundle-capabilities.xsd")


def test_capabilities_declare_the_three_vosi_endpoints_and_tap(client: TestClient) -> None:
    ids = [c.get("standardID") for c in tree_of(client.get("/tap/capabilities"))]

    assert "ivo://ivoa.net/std/TAP" in ids
    for kind in ("capabilities", "availability", "tables"):
        assert f"ivo://ivoa.net/std/VOSI#{kind}" in ids


def test_capabilities_only_advertise_geometry_the_translator_accepts(client: TestClient) -> None:
    """A declared function a query cannot use is worse than an undeclared one."""
    from tapdrop.adql import translate
    from tapdrop.discovery import discover as run_discovery

    forms = tree_of(client.get("/tap/capabilities")).xpath("//form/text()")
    assert set(forms) == set(ADQL_GEOMETRY_FEATURES)

    registry = run_discovery([str(DATA_DIR / "gaia.parquet")], None)
    translate(
        "SELECT ra FROM data.gaia WHERE CONTAINS(POINT('ICRS', ra, dec), "
        "CIRCLE('ICRS', 1, 2, 3)) = 1 AND DISTANCE(POINT('ICRS', ra, dec), "
        "POINT('ICRS', 1, 2)) < 1 AND COORD1(POINT('ICRS', ra, dec)) > 0 "
        "AND COORD2(POINT('ICRS', ra, dec)) > 0 "
        "AND COORDSYS(POINT('ICRS', ra, dec)) = 'ICRS' "
        "AND CONTAINS(POINT('ICRS', ra, dec), POLYGON('ICRS', 0, 0, 20, 0, 20, 50)) = 1 "
        "AND INTERSECTS(CIRCLE('ICRS', 1, 2, 3), POLYGON('ICRS', 0, 0, 20, 0, 20, 50)) = 1",
        registry,
    )


def test_declared_polygon_and_intersects_run_on_a_plain_catalog(client: TestClient) -> None:
    """They are declared unconditionally, so they must work without ivoa.obscore."""
    for adql in (
        "SELECT TOP 1 ra FROM data.gaia WHERE CONTAINS(POINT('ICRS', ra, dec), "
        "POLYGON('ICRS', 0, -80, 40, -80, 40, -20, 0, -20)) = 1",
        "SELECT TOP 1 ra FROM data.gaia WHERE INTERSECTS(CIRCLE('ICRS', ra, dec, 1), "
        "POLYGON('ICRS', 0, -80, 40, -80, 40, -20, 0, -20)) = 1",
    ):
        response = client.get(
            "/tap/sync", params={"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": adql}
        )
        assert response.status_code == 200, response.text


def test_capabilities_description_does_not_disown_what_the_translator_accepts(
    client: TestClient,
) -> None:
    description = tree_of(client.get("/tap/capabilities")).xpath("//language/description/text()")[0]
    assert "POLYGON and INTERSECTS accept ObsCore s_region" in description
    assert "BOX and REGION are not implemented" in description


def test_capabilities_advertise_every_output_format_the_writer_produces(
    client: TestClient,
) -> None:
    from tapdrop.output import FORMAT_ALIASES

    aliases = tree_of(client.get("/tap/capabilities")).xpath("//alias/text()")

    assert set(aliases) == set(FORMAT_ALIASES.values())


def test_capabilities_report_the_configured_limits() -> None:
    for client in build_client(max_rows=500, hard_max_rows=5000, query_timeout=60):
        root = tree_of(client.get("/tap/capabilities"))

        assert root.xpath("//outputLimit/default/text()") == ["500"]
        assert root.xpath("//outputLimit/hard/text()") == ["5000"]
        assert root.xpath("//executionDuration/default/text()") == ["60"]


def test_upload_methods_appear_only_when_upload_is_enabled() -> None:
    for client in build_client():
        assert not tree_of(client.get("/tap/capabilities")).xpath("//uploadMethod")
    for client in build_client(allow_upload=True):
        assert tree_of(client.get("/tap/capabilities")).xpath("//uploadMethod")


def test_access_urls_are_absolute_and_carry_the_token_prefix() -> None:
    for client in build_client(token="s3cr3t"):
        urls = tree_of(client.get("/t/s3cr3t/tap/capabilities")).xpath("//accessURL/text()")

        assert urls, "capabilities must publish accessURLs"
        for url in urls:
            assert url.startswith("http://")
            assert "/t/s3cr3t/tap" in url


def test_public_url_overrides_the_request_host() -> None:
    """Behind a tunnel the request host is the tunnel's, not the client's."""
    for client in build_client(public_url="https://tapdrop.example.org"):
        urls = tree_of(client.get("/tap/capabilities")).xpath("//accessURL/text()")

        assert all(url.startswith("https://tapdrop.example.org/tap") for url in urls)


def test_availability_validates_and_reports_up(client: TestClient) -> None:
    response = client.get("/tap/availability")

    assert_valid(response.content, "VOSIAvailability-v1.0.xsd")
    assert tree_of(response).xpath("//avail:available/text()", namespaces=NS) == ["true"]
    assert not tree_of(response).xpath("//avail:downAt", namespaces=NS)


def test_availability_publishes_down_at_from_the_ttl() -> None:
    settings = Settings(ttl="2h")
    settings.down_at = settings.resolve_down_at()

    document = availability_document(settings)

    assert_valid(document, "VOSIAvailability-v1.0.xsd")
    assert "<vosi:downAt>" in document
    assert "<vosi:available>true</vosi:available>" in document


def test_availability_is_false_once_the_ttl_has_passed() -> None:
    settings = Settings()
    settings.down_at = datetime.now(UTC) - timedelta(minutes=1)

    document = availability_document(settings)

    assert "<vosi:available>false</vosi:available>" in document


def test_tables_validate_against_the_vosi_schema(client: TestClient) -> None:
    response = client.get("/tap/tables")

    assert response.status_code == 200
    assert_valid(response.content, "bundle-tables.xsd")


def test_tables_list_every_discovered_table_with_its_metadata(client: TestClient) -> None:
    root = tree_of(client.get("/tap/tables"))

    assert root.xpath("//schema/name/text()") == ["TAP_SCHEMA", "data"]
    assert "data.gaia" in root.xpath("//table/name/text()")
    ra = root.xpath("//column[name='ra']")[0]
    assert ra.xpath("unit/text()") == ["deg"]
    assert ra.xpath("ucd/text()") == ["pos.eq.ra;meta.main"]
    assert ra.xpath("dataType/text()") == ["double"]


def test_tables_agree_with_what_sync_returns(client: TestClient) -> None:
    """VOSI tables and /tap/sync must not describe different columns."""
    declared = set(
        tree_of(client.get("/tap/tables")).xpath("//table[name='data.gaia']/column/name/text()")
    )
    returned = client.get(
        "/tap/sync",
        params={"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": "SELECT TOP 1 * FROM data.gaia"},
    )
    import io

    from astropy.io.votable import parse as parse_votable

    fields = {f.name for f in parse_votable(io.BytesIO(returned.content)).get_first_table().fields}

    assert declared == fields


def test_examples_are_html_and_name_real_tables(client: TestClient) -> None:
    response = client.get("/tap/examples")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'typeof="example"' in response.text
    assert "data.gaia" in response.text
    assert "CIRCLE('ICRS'" in response.text  # gaia has RA/Dec, so a cone example


def test_example_queries_actually_run(client: TestClient) -> None:
    root = etree.fromstring(client.get("/tap/examples").content, etree.HTMLParser())
    queries = root.xpath("//pre[@property='query']/text()")

    assert queries
    for query in queries:
        response = client.get(
            "/tap/sync", params={"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": query}
        )
        assert response.status_code == 200, query


def test_vosi_routes_are_behind_the_token_prefix() -> None:
    for client in build_client(token="s3cr3t"):
        for path in ("capabilities", "availability", "tables", "examples"):
            assert client.get(f"/tap/{path}").status_code == 401
            assert client.get(f"/t/s3cr3t/tap/{path}").status_code == 200


# --------------------------------------------------------------------------
# with ivoa.obscore registered
# --------------------------------------------------------------------------


@pytest.fixture
def obscore_client() -> Iterator[TestClient]:
    from tapdrop.caom_lite import attach_images

    settings = Settings(sources=[str(DATA_DIR / "gaia.parquet")], images=[str(DATA_DIR / "images")])
    registry = discover(settings.sources, settings.config_file)
    con = create_connection(settings, registry)
    attach_images(con, registry, settings.images, "http://testserver")
    try:
        yield TestClient(create_app(settings, registry, con))
    finally:
        con.close()


def test_tables_carry_obscore_utype_and_xtype_and_still_validate(
    obscore_client: TestClient,
) -> None:
    response = obscore_client.get("/tap/tables")
    assert_valid(response.content, "bundle-tables.xsd")
    root = tree_of(response)

    s_ra = root.xpath("//table[name='ivoa.obscore']/column[name='s_ra']")[0]
    assert s_ra.xpath("utype/text()") == [
        "obscore:Char.SpatialAxis.Coverage.Location.Coord.Position2D.Value2.C1"
    ]
    s_region = root.xpath("//table[name='ivoa.obscore']/column[name='s_region']/dataType")[0]
    assert s_region.get("extendedType") == "adql:REGION"
    # TAP_SCHEMA lists itself here too (TAP 1.1 §4).
    assert "TAP_SCHEMA.columns" in root.xpath("//table/name/text()")


def test_capabilities_with_obscore_validate_and_declare_polygon_and_intersects(
    obscore_client: TestClient,
) -> None:
    response = obscore_client.get("/tap/capabilities")
    assert_valid(response.content, "bundle-capabilities.xsd")
    forms = set(tree_of(response).xpath("//form/text()"))
    assert {"POLYGON", "INTERSECTS", "CONTAINS"} <= forms
    assert tree_of(response).xpath("//dataModel/text()") == ["ObsCore-1.1"]
