"""ADQL 2.1 sqlglot dialect, targeting DuckDB.

Subclasses DuckDB directly (rather than the generic base dialect) so ordinary
SQL -- ``WHERE``, ``GROUP BY``, ``HAVING``, ``ORDER BY``, ``JOIN``, subqueries,
aggregates, and most ADQL 2.1 math/string functions -- passes through
unchanged using DuckDB's own parser/generator. Only the two things ADQL adds
on top of standard SQL need code here: ``TOP n`` and IVOA geometry.

AIDEV-NOTE: ``LOG(x)`` (natural log) and ``LOG10(x)`` need no mapping --
sqlglot's base parser already treats single-argument ``LOG`` as natural log
(-> DuckDB ``LN``) and ``LOG10`` as base-10 (-> DuckDB ``LOG(10, x)``), which
matches ADQL 2.1 sec 4.3.7 semantics exactly. Verified interactively against
sqlglot 30.20.0; do not "fix" this without re-checking.

Geometry constructs are modelled as first-class ``exp.Func`` nodes so the
security walk in ``translate.py`` can pattern-match on them structurally
instead of on function-name strings. ``BOX``, ``POLYGON``, ``INTERSECTS``,
``REGION`` all parse (so a query using any of them gets a clean "unsupported
construct" error instead of a syntax error rather than gets it wrong). ADQL
v1.1's spherical polygon support (``POLYGON``, ``CONTAINS(POINT, POLYGON)``,
``INTERSECTS`` between ``CIRCLE``/``POLYGON``/an ObsCore ``s_region``
column) is implemented; ``translate.py`` still rejects ``BOX`` and
``REGION`` (out of scope for this milestone) and ``INTERSECTS`` between two
``CIRCLE``s (not requested, and not needed by anything in scope).
"""

from __future__ import annotations

import typing as t

from sqlglot import TokenType, exp
from sqlglot.dialects.duckdb import DuckDB
from sqlglot.generator import Generator


class Point(exp.Expression, exp.Func):
    """``POINT(coordSys, coord1, coord2)``."""

    arg_types: t.ClassVar[dict[str, bool]] = {"coordsys": True, "ra": True, "dec": True}


class Circle(exp.Expression, exp.Func):
    """``CIRCLE(coordSys, coord1, coord2, radius)``."""

    arg_types: t.ClassVar[dict[str, bool]] = {
        "coordsys": True,
        "ra": True,
        "dec": True,
        "radius": True,
    }


class Box(exp.Expression, exp.Func):
    """``BOX(coordSys, coord1, coord2, width, height)``. v1.1, always rejected here."""

    arg_types: t.ClassVar[dict[str, bool]] = {
        "coordsys": True,
        "ra": True,
        "dec": True,
        "width": True,
        "height": True,
    }


class Polygon(exp.Expression, exp.Func):
    """``POLYGON(coordSys, coord1, coord2, ...)``.

    Only valid as an operand of ``CONTAINS``/``INTERSECTS`` -- see
    ``translate.py``'s ``_validate_polygon_position``.
    """

    arg_types: t.ClassVar[dict[str, bool]] = {"coordsys": True, "expressions": True}
    is_var_len_args = True


class Region(exp.Expression, exp.Func):
    """``REGION(string)``. v1.1, always rejected here."""

    arg_types: t.ClassVar[dict[str, bool]] = {"this": True}


class Distance(exp.Expression, exp.Func):
    """``DISTANCE(point1, point2)`` -> angular separation in degrees."""

    arg_types: t.ClassVar[dict[str, bool]] = {"this": True, "expression": True}


class Contains(exp.Expression, exp.Func):
    """``CONTAINS(geom1, geom2)``.

    ``CONTAINS(POINT, CIRCLE)``, ``CONTAINS(POINT, POLYGON)``, and
    ``CONTAINS(POINT, s_region)`` (a region column/string) are supported --
    see ``translate.py``'s ``_validate_geometry``.
    """

    arg_types: t.ClassVar[dict[str, bool]] = {"this": True, "expression": True}


class Intersects(exp.Expression, exp.Func):
    """``INTERSECTS(geom1, geom2)``.

    Every combination of ``CIRCLE``, ``POLYGON``, and a region column/string
    is supported except ``CIRCLE``/``CIRCLE`` (not requested, not needed) --
    see ``translate.py``'s ``_validate_geometry``.
    """

    arg_types: t.ClassVar[dict[str, bool]] = {"this": True, "expression": True}


class Coordsys(exp.Expression, exp.Func):
    """``COORDSYS(geom)`` -> the frame string of a geometry value."""

    arg_types: t.ClassVar[dict[str, bool]] = {"this": True}


class Coord1(exp.Expression, exp.Func):
    """``COORD1(point)`` -> right ascension of a ``POINT``/``CIRCLE`` center."""

    arg_types: t.ClassVar[dict[str, bool]] = {"this": True}


class Coord2(exp.Expression, exp.Func):
    """``COORD2(point)`` -> declination of a ``POINT``/``CIRCLE`` center."""

    arg_types: t.ClassVar[dict[str, bool]] = {"this": True}


_GEOMETRY_FUNCS = (
    Point,
    Circle,
    Box,
    Polygon,
    Region,
    Distance,
    Contains,
    Intersects,
    Coordsys,
    Coord1,
    Coord2,
)


def _struct_field(self: Generator, expr: exp.Expression, key: str) -> str:
    return self.sql(expr, key)


def _point_sql(self: Generator, e: Point) -> str:
    ra = _struct_field(self, e, "ra")
    dec = _struct_field(self, e, "dec")
    return f"{{'ra': CAST({ra} AS DOUBLE), 'dec': CAST({dec} AS DOUBLE)}}"


def _circle_sql(self: Generator, e: Circle) -> str:
    ra = _struct_field(self, e, "ra")
    dec = _struct_field(self, e, "dec")
    radius = _struct_field(self, e, "radius")
    return (
        "{'ra': CAST(" + ra + " AS DOUBLE), "
        "'dec': CAST(" + dec + " AS DOUBLE), "
        "'radius': CAST(" + radius + " AS DOUBLE)}"
    )


def _distance_sql(self: Generator, e: Distance) -> str:
    # translate.py guarantees e.this / e.expression are both Point nodes
    # before generation is ever reached.
    p1, p2 = e.this, e.expression
    ra1, dec1 = _struct_field(self, p1, "ra"), _struct_field(self, p1, "dec")
    ra2, dec2 = _struct_field(self, p2, "ra"), _struct_field(self, p2, "dec")
    return f"tapdrop_hav_deg({ra1}, {dec1}, {ra2}, {dec2})"


def _region_ra_dec_sql(self: Generator, node: exp.Expression) -> tuple[str, str]:
    """SQL fragments evaluating to the LIST(DOUBLE) ra/dec vertex arrays of *node*.

    translate.py guarantees *node* is a ``Polygon``, an ``exp.Column``, or a
    string ``exp.Literal`` -- never anything else -- by the time generation
    is reached. A literal ``POLYGON(...)`` generates its vertex arrays
    directly (still through sqlglot's own generator for each coordinate
    expression, never raw string interpolation); a column or string literal
    is an ObsCore-style ``s_region`` value, parsed at *query run time* by a
    DuckDB UDF -- the column/literal's own SQL is passed through unchanged,
    so nothing from the query text is reassembled into new SQL text here.
    """
    if isinstance(node, Polygon):
        exprs = node.args.get("expressions") or []
        ra_list = ", ".join(self.sql(e) for e in exprs[0::2])
        dec_list = ", ".join(self.sql(e) for e in exprs[1::2])
        return f"[{ra_list}]", f"[{dec_list}]"
    value_sql = self.sql(node)
    return f"tapdrop_parse_region_ra({value_sql})", f"tapdrop_parse_region_dec({value_sql})"


def _contains_sql(self: Generator, e: Contains) -> str:
    # translate.py guarantees e.this is a Point, and e.expression is a
    # Circle, a Polygon, or a region column/string -- CONTAINS(POINT, *) is
    # the only shape this version supports.
    point, other = e.this, e.expression
    ra, dec = _struct_field(self, point, "ra"), _struct_field(self, point, "dec")
    if isinstance(other, Circle):
        ra0 = _struct_field(self, other, "ra")
        dec0 = _struct_field(self, other, "dec")
        radius = _struct_field(self, other, "radius")
        return f"tapdrop_cone_contains({ra}, {dec}, {ra0}, {dec0}, {radius})"
    poly_ra, poly_dec = _region_ra_dec_sql(self, other)
    return f"tapdrop_point_in_polygon({ra}, {dec}, {poly_ra}, {poly_dec})"


def _intersects_sql(self: Generator, e: Intersects) -> str:
    # translate.py guarantees this is CIRCLE/POLYGON, POLYGON/POLYGON, or a
    # combination with a region column/string -- never CIRCLE/CIRCLE.
    a, b = e.this, e.expression
    circle: Circle | None = None
    other: exp.Expression = b
    if isinstance(a, Circle):
        circle, other = a, b
    elif isinstance(b, Circle):
        circle, other = b, a
    if circle is not None:
        ra0 = _struct_field(self, circle, "ra")
        dec0 = _struct_field(self, circle, "dec")
        radius = _struct_field(self, circle, "radius")
        poly_ra, poly_dec = _region_ra_dec_sql(self, other)
        return f"tapdrop_circle_polygon_intersects({ra0}, {dec0}, {radius}, {poly_ra}, {poly_dec})"
    ra1, dec1 = _region_ra_dec_sql(self, a)
    ra2, dec2 = _region_ra_dec_sql(self, b)
    return f"tapdrop_polygon_polygon_intersects({ra1}, {dec1}, {ra2}, {dec2})"


def _coordsys_sql(self: Generator, e: Coordsys) -> str:
    # translate.py guarantees the frame is a literal 'ICRS' wherever a
    # geometry value is built, so the extractor can just say so.
    return "'ICRS'"


def _coord1_sql(self: Generator, e: Coord1) -> str:
    return _struct_field(self, e.this, "ra")


def _coord2_sql(self: Generator, e: Coord2) -> str:
    return _struct_field(self, e.this, "dec")


class ADQL(DuckDB):
    """ADQL 2.1 (subset), parsed and generated as DuckDB SQL."""

    class Tokenizer(DuckDB.Tokenizer):
        KEYWORDS: t.ClassVar[dict[str, TokenType]] = {
            **DuckDB.Tokenizer.KEYWORDS,
            "TOP": TokenType.TOP,
        }

    # AIDEV-NOTE: DuckDB (the sqlglot dialect class) does not declare its own
    # nested `Parser`/`Generator` classes -- it gets them dynamically via
    # `parser_class`/`generator_class` attributes that sqlglot's `_Dialect`
    # metaclass assigns (see sqlglot/dialects/dialect.py `_Dialect.__new__`).
    # `DuckDB.Parser`/`DuckDB.Generator` resolve to real classes at runtime
    # (verified interactively), but mypy sees a class-level *value*, not a
    # `class Parser(...):` statement, so it cannot treat it as a base class
    # statically. The ignores below are for that mypy limitation, not a bug
    # in this code.
    class Parser(DuckDB.Parser):  # type: ignore[valid-type,misc]
        FUNCTIONS: t.ClassVar[dict[str, t.Callable[..., exp.Expression]]] = {
            **DuckDB.Parser.FUNCTIONS,
            **{
                name: mapping
                for func in _GEOMETRY_FUNCS
                for name, mapping in func.default_parser_mappings().items()
            },
        }

    class Generator(DuckDB.Generator):  # type: ignore[valid-type,misc]
        TRANSFORMS: t.ClassVar[dict[type[exp.Expr], t.Callable[..., str]]] = {
            **DuckDB.Generator.TRANSFORMS,
            Point: _point_sql,
            Circle: _circle_sql,
            Distance: _distance_sql,
            Contains: _contains_sql,
            Intersects: _intersects_sql,
            Coordsys: _coordsys_sql,
            Coord1: _coord1_sql,
            Coord2: _coord2_sql,
            # Box/Region intentionally have no TRANSFORMS entry: translate.py's
            # validation pass always rejects them before .sql() is ever called
            # on a tree that contains one. Polygon also has none -- it is
            # never generated standalone, only unpacked by
            # `_region_ra_dec_sql` from inside a Contains/Intersects node
            # (translate.py's `_validate_polygon_position` guarantees a
            # Polygon never appears anywhere else).
        }
