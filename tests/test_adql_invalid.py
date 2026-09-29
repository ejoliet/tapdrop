"""Invalid ADQL: syntax errors, unknown tables, unknown columns.

Every case must raise before any SQL reaches DuckDB.
"""

from __future__ import annotations

import pytest

from adql_fixtures import make_registry
from tapdrop.adql.translate import translate
from tapdrop.errors import AdqlSyntaxError, UnknownColumnError, UnknownTableError


@pytest.fixture
def registry():
    return make_registry()


@pytest.mark.parametrize(
    "adql",
    [
        "SELECT * FROM cats.stars WHERE (",
        "SELECT * FROM cats.stars WHERE mag <",
        "SELEKT * FROM cats.stars",
        "SELECT * cats.stars",  # missing FROM
        "SELECT * FROM cats.stars WHERE",
        "SELECT * FROM cats.stars JOIN",
        "SELECT * FROM (SELECT",
        "SELECT * FROM cats.stars WHERE mag IN (",
        "SELECT TOP FROM cats.stars",
        "SELECT * FROM cats.stars WHERE mag < 10 AND",
        "SELECT * FROM cats.stars HAVING",
    ],
)
def test_syntax_error_raises_with_position(registry, adql):
    with pytest.raises(AdqlSyntaxError) as excinfo:
        translate(adql, registry)
    assert excinfo.value.message  # non-empty, client-actionable


def test_syntax_error_position_is_reported(registry):
    with pytest.raises(AdqlSyntaxError) as excinfo:
        translate("SELECT * FROM cats.stars WHERE (", registry)
    assert excinfo.value.position == 31


def test_unknown_table_reports_close_match(registry):
    with pytest.raises(UnknownTableError) as excinfo:
        translate("SELECT * FROM cats.star", registry)  # missing 's'
    assert "cats.stars" in excinfo.value.close_matches


def test_unknown_table_no_close_match(registry):
    with pytest.raises(UnknownTableError) as excinfo:
        translate("SELECT * FROM zzz.nonexistent_qqq", registry)
    assert excinfo.value.close_matches == []


def test_unknown_column_qualified(registry):
    with pytest.raises(UnknownColumnError) as excinfo:
        translate("SELECT s.bogus_col FROM cats.stars AS s", registry)
    assert excinfo.value.name == "bogus_col"
    assert excinfo.value.table == "cats.stars"


def test_unknown_column_unqualified_reports_close_match(registry):
    with pytest.raises(UnknownColumnError) as excinfo:
        translate("SELECT magg FROM cats.stars", registry)  # typo for "mag"
    assert "mag" in excinfo.value.close_matches


def test_unknown_column_in_where(registry):
    with pytest.raises(UnknownColumnError):
        translate("SELECT ra FROM cats.stars WHERE nope > 1", registry)


def test_unknown_column_in_join_condition(registry):
    with pytest.raises(UnknownColumnError):
        translate(
            "SELECT s.ra FROM cats.stars AS s JOIN cats.galaxies AS g ON s.ra = g.bogus_column",
            registry,
        )


def test_negative_literal_radius_rejected(registry):
    adql = (
        "SELECT ra FROM cats.stars WHERE "
        "CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 10.0, 20.0, -1.0)) = 1"
    )
    with pytest.raises(AdqlSyntaxError):
        translate(adql, registry)
