"""Valid ADQL: translation should succeed and produce runnable DuckDB SQL.

Each case both calls ``translate()`` (checking it does not raise) and, where
meaningful, executes the resulting SQL against a real DuckDB connection to
confirm the generated SQL is not just accepted but actually runs.
"""

from __future__ import annotations

import pytest

from adql_fixtures import make_engine_connection, make_registry
from tapdrop.adql.translate import translate


@pytest.fixture
def registry():
    return make_registry()


@pytest.fixture
def con():
    connection = make_engine_connection()
    connection.execute(
        "INSERT INTO cats.stars VALUES "
        "(1, 10.0, 20.0, 5.0, 'a'), (2, 100.0, -5.0, 8.0, 'b'), (3, 0.5, 89.9, 12.0, 'c')"
    )
    connection.execute(
        "CREATE TABLE cats.galaxies (galaxy_id BIGINT, ra DOUBLE, dec DOUBLE, redshift DOUBLE)"
    )
    connection.execute("INSERT INTO cats.galaxies VALUES (1, 10.0, 20.0, 0.1)")
    return connection


def _run(con, registry, adql):
    result = translate(adql, registry)
    con.execute(result.sql)
    return result


@pytest.mark.parametrize(
    "adql",
    [
        "SELECT * FROM cats.stars",
        "SELECT ra, dec FROM cats.stars",
        "SELECT TOP 1 ra, dec FROM cats.stars",
        "SELECT TOP 100 * FROM cats.stars WHERE mag < 10",
        "SELECT DISTINCT mag FROM cats.stars",
        "SELECT ra, dec FROM cats.stars WHERE mag BETWEEN 1 AND 10",
        "SELECT ra, dec FROM cats.stars WHERE name LIKE 'a%'",
        "SELECT ra, dec FROM cats.stars WHERE name IN ('a', 'b')",
        "SELECT ra, dec FROM cats.stars WHERE mag IS NOT NULL",
        "SELECT ra, dec FROM cats.stars WHERE mag < 10 AND dec > 0",
        "SELECT ra, dec FROM cats.stars WHERE NOT (mag > 10)",
        "SELECT ra, dec FROM cats.stars ORDER BY mag DESC",
        "SELECT ra, dec FROM cats.stars ORDER BY mag ASC, ra DESC",
        "SELECT mag, COUNT(*) FROM cats.stars GROUP BY mag",
        "SELECT mag, COUNT(*) FROM cats.stars GROUP BY mag HAVING COUNT(*) > 0",
        "SELECT AVG(mag), MIN(mag), MAX(mag), SUM(mag) FROM cats.stars",
        "SELECT UPPER(name), LOWER(name) FROM cats.stars",
        "SELECT SUBSTRING(name, 1, 1) FROM cats.stars",
        "SELECT TRIM(name) FROM cats.stars",
        "SELECT ABS(mag), ROUND(mag), SQRT(mag) FROM cats.stars",
        "SELECT POWER(mag, 2), MOD(id, 2) FROM cats.stars",
        "SELECT LOG(mag), LOG10(mag) FROM cats.stars WHERE mag > 0",
        "SELECT CEILING(mag), FLOOR(mag), TRUNCATE(mag) FROM cats.stars",
        "SELECT s.ra, s.dec FROM cats.stars AS s WHERE s.mag < 10",
        (
            "SELECT s.ra, g.redshift FROM cats.stars AS s "
            "JOIN cats.galaxies AS g ON s.id = g.galaxy_id"
        ),
        ("SELECT s.ra FROM cats.stars AS s LEFT JOIN cats.galaxies AS g ON s.id = g.galaxy_id"),
        "WITH bright AS (SELECT * FROM cats.stars WHERE mag < 6) SELECT ra FROM bright",
        "SELECT ra FROM cats.stars WHERE id = (SELECT MIN(id) FROM cats.stars)",
        "SELECT ra AS right_ascension, dec AS declination FROM cats.stars",
        "select ra, dec from cats.stars where mag < 10",  # lowercase keywords
    ],
)
def test_valid_query_translates_and_runs(con, registry, adql):
    _run(con, registry, adql)


def test_top_becomes_limit(registry):
    result = translate("SELECT TOP 5 ra FROM cats.stars", registry)
    assert "LIMIT 5" in result.sql
    assert "TOP" not in result.sql.upper()


def test_point_circle_contains_translates_to_cone_macro(con, registry):
    adql = (
        "SELECT ra, dec FROM cats.stars WHERE "
        "CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 10.0, 20.0, 1.5)) = 1"
    )
    result = _run(con, registry, adql)
    assert "tapdrop_cone_contains" in result.sql
    assert result.cone_hint is not None
    assert result.cone_hint.ra0 == 10.0
    assert result.cone_hint.dec0 == 20.0
    assert result.cone_hint.radius_deg == 1.5


def test_cone_hint_only_extracted_for_literal_circle(con, registry):
    adql = (
        "SELECT ra, dec FROM cats.stars WHERE "
        "CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', ra, dec, 1.0)) = 1"
    )
    result = _run(con, registry, adql)
    assert result.cone_hint is None  # ra/dec are columns, not literals


def test_distance_translates_to_haversine_macro(con, registry):
    adql = "SELECT DISTANCE(POINT('ICRS', ra, dec), POINT('ICRS', 0.0, 0.0)) FROM cats.stars"
    result = _run(con, registry, adql)
    assert "tapdrop_hav_deg" in result.sql


def test_coord1_coord2_coordsys(con, registry):
    adql = (
        "SELECT COORD1(POINT('ICRS', ra, dec)), COORD2(POINT('ICRS', ra, dec)), "
        "COORDSYS(POINT('ICRS', ra, dec)) FROM cats.stars"
    )
    _run(con, registry, adql)
