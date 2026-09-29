"""ADQL that parses but names a construct this milestone does not implement.

Every case must raise ``UnsupportedAdqlError`` naming the construct, not a
generic "unsupported query" message.
"""

from __future__ import annotations

import pytest

from adql_fixtures import make_registry
from tapdrop.adql.translate import translate
from tapdrop.errors import UnsupportedAdqlError


@pytest.fixture
def registry():
    return make_registry()


def test_box_rejected(registry):
    adql = (
        "SELECT * FROM cats.stars WHERE "
        "CONTAINS(POINT('ICRS', ra, dec), BOX('ICRS', 10, 20, 1, 1)) = 1"
    )
    with pytest.raises(UnsupportedAdqlError, match="BOX"):
        translate(adql, registry)


def test_polygon_non_icrs_frame_rejected(registry):
    # POLYGON('ICRS', ...) is supported as of M9 (see test_adql_polygon.py);
    # a non-ICRS frame is the remaining rejection case for it.
    adql = (
        "SELECT * FROM cats.stars WHERE "
        "CONTAINS(POINT('ICRS', ra, dec), POLYGON('GALACTIC', 0, 0, 1, 0, 1, 1)) = 1"
    )
    with pytest.raises(UnsupportedAdqlError, match="ICRS"):
        translate(adql, registry)


def test_polygon_outside_contains_or_intersects_rejected(registry):
    # POLYGON only has a defined SQL generation as a direct CONTAINS/
    # INTERSECTS operand.
    adql = "SELECT POLYGON('ICRS', 0, 0, 1, 0, 1, 1) FROM cats.stars"
    with pytest.raises(UnsupportedAdqlError, match="POLYGON"):
        translate(adql, registry)


def test_intersects_rejected(registry):
    adql = (
        "SELECT * FROM cats.stars WHERE "
        "INTERSECTS(CIRCLE('ICRS', ra, dec, 1), CIRCLE('ICRS', 0, 0, 1)) = 1"
    )
    with pytest.raises(UnsupportedAdqlError, match="INTERSECTS"):
        translate(adql, registry)


def test_region_rejected(registry):
    # REGION(...) is unconditionally unsupported (checked in the first pass
    # over the whole tree, before any CONTAINS/INTERSECTS shape check), so it
    # is named even though INTERSECTS -- its parent here -- would otherwise
    # accept this shape (REGION/REGION); REGION alone, unwrapped, is
    # exercised implicitly since it can only ever appear inside an
    # INTERSECTS call in this grammar.
    adql = "SELECT * FROM cats.stars WHERE INTERSECTS(REGION('circle 0 0 1'), REGION('circle 1 1 1')) = 1"
    with pytest.raises(UnsupportedAdqlError, match="REGION"):
        translate(adql, registry)


def test_non_icrs_frame_in_point_rejected(registry):
    adql = "SELECT COORD1(POINT('GALACTIC', ra, dec)) FROM cats.stars"
    with pytest.raises(UnsupportedAdqlError, match="ICRS"):
        translate(adql, registry)


def test_non_icrs_frame_in_circle_rejected(registry):
    adql = (
        "SELECT * FROM cats.stars WHERE "
        "CONTAINS(POINT('ICRS', ra, dec), CIRCLE('FK5', 10, 20, 1)) = 1"
    )
    with pytest.raises(UnsupportedAdqlError, match="ICRS"):
        translate(adql, registry)


def test_non_literal_coordsys_rejected(registry):
    adql = "SELECT COORD1(POINT(name, ra, dec)) FROM cats.stars"
    with pytest.raises(UnsupportedAdqlError):
        translate(adql, registry)


def test_multiple_statements_rejected(registry):
    with pytest.raises(UnsupportedAdqlError, match="single SELECT"):
        translate("SELECT * FROM cats.stars; SELECT * FROM cats.galaxies", registry)


def test_insert_statement_rejected(registry):
    with pytest.raises(UnsupportedAdqlError, match="INSERT"):
        translate("INSERT INTO cats.stars VALUES (1, 1.0, 1.0, 1.0, 'x')", registry)


def test_delete_statement_rejected(registry):
    with pytest.raises(UnsupportedAdqlError, match="DELETE"):
        translate("DELETE FROM cats.stars WHERE id = 1", registry)


def test_update_statement_rejected(registry):
    with pytest.raises(UnsupportedAdqlError, match="UPDATE"):
        translate("UPDATE cats.stars SET mag = 0", registry)


def test_create_statement_rejected(registry):
    with pytest.raises(UnsupportedAdqlError, match="CREATE"):
        translate("CREATE TABLE evil (x INT)", registry)


def test_contains_wrong_argument_order_rejected(registry):
    adql = (
        "SELECT * FROM cats.stars WHERE "
        "CONTAINS(CIRCLE('ICRS', 10, 20, 1), POINT('ICRS', ra, dec)) = 1"
    )
    with pytest.raises(UnsupportedAdqlError, match="CONTAINS"):
        translate(adql, registry)


def test_distance_with_non_point_rejected(registry):
    adql = "SELECT DISTANCE(CIRCLE('ICRS', 0, 0, 1), POINT('ICRS', 1, 1)) FROM cats.stars"
    with pytest.raises(UnsupportedAdqlError, match="DISTANCE"):
        translate(adql, registry)


def test_coord1_on_non_point_rejected(registry):
    with pytest.raises(UnsupportedAdqlError, match="COORD1"):
        translate("SELECT COORD1(ra) FROM cats.stars", registry)
