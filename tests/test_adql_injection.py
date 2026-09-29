"""Injection attempts: every case must be rejected before any SQL executes.

The gate is AST-shape based (``translate.py``'s ``AIDEV-NOTE``), so these
cases exercise that no clever framing -- comments, CTEs, statement-stacking,
file paths -- gets a dangerous construct past it.
"""

from __future__ import annotations

import pytest

from adql_fixtures import make_registry
from tapdrop.adql.translate import translate
from tapdrop.errors import TapdropError, UnsupportedAdqlError


@pytest.fixture
def registry():
    return make_registry()


@pytest.mark.parametrize(
    "adql",
    [
        "SELECT * FROM cats.stars; DROP TABLE cats.stars",
        "COPY cats.stars TO '/tmp/exfil.csv'",
        "COPY (SELECT * FROM cats.stars) TO '/tmp/exfil.csv'",
        "ATTACH '/etc/passwd' AS pwn",
        "ATTACH DATABASE '/tmp/evil.db' AS evil",
        "INSTALL httpfs",
        "LOAD httpfs",
        "PRAGMA database_list",
        "SET memory_limit='100GB'",
        "SELECT * FROM read_parquet('/etc/passwd')",
        "SELECT * FROM read_csv('s3://bucket/secret.csv')",
        "SELECT * FROM '/etc/passwd'",
        "SELECT * FROM 'cats/stars.parquet'",
        "WITH x AS (SELECT * FROM read_csv('s3://bucket/x.csv')) SELECT * FROM x",
        "SELECT read_parquet('/etc/passwd')",
    ],
)
def test_injection_rejected_before_execution(registry, adql):
    with pytest.raises(TapdropError):
        translate(adql, registry)


def test_sql_comment_is_stripped_not_smuggled(registry):
    """A trailing SQL comment must not let a second statement survive.

    ``-- ; DROP TABLE ...`` is a single valid SELECT with a comment; sqlglot
    parses it as one statement and ``comments=False`` on generation drops the
    comment text entirely, so it can never reappear in the emitted SQL.
    """
    adql = "SELECT * FROM cats.stars -- ; DROP TABLE cats.stars"
    result = translate(adql, registry)
    assert "DROP" not in result.sql.upper()
    assert "--" not in result.sql


def test_cte_hiding_table_function_rejected(registry):
    adql = "WITH x AS (SELECT * FROM read_parquet('/etc/passwd')) SELECT * FROM x"
    with pytest.raises(UnsupportedAdqlError):
        translate(adql, registry)


def test_nested_subquery_table_function_rejected(registry):
    adql = "SELECT * FROM (SELECT * FROM read_csv('/etc/passwd')) AS t"
    with pytest.raises(UnsupportedAdqlError):
        translate(adql, registry)


def test_anonymous_scalar_table_function_call_rejected(registry):
    adql = "SELECT glob('/etc/*') FROM cats.stars"
    with pytest.raises(UnsupportedAdqlError):
        translate(adql, registry)
