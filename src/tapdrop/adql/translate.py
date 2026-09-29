"""ADQL -> DuckDB SQL translation: the security gate.

``translate()`` is the only way ADQL text becomes SQL that reaches DuckDB.
It parses with sqlglot, then walks the resulting AST end to end -- checking
statement shape, table/column references against the ``Registry``, geometry
frames, and function/table-source allowlisting -- before ever asking
sqlglot's generator for SQL text. Nothing here executes a query; the
contract is that every rejection in this module happens strictly before any
SQL is handed to DuckDB (see ``tests/test_adql_injection.py``).

AIDEV-NOTE: the allowlist is structural (AST node shape), not a regex or
string search over raw ADQL text, per RDD.md "Security". A ``read_parquet``
call is rejected because its node is an ``exp.Func`` sitting in a table
position, not because the substring "read_" appears anywhere in the query.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from tapdrop.adql.dialect import (
    ADQL,
    Box,
    Circle,
    Contains,
    Coord1,
    Coord2,
    Coordsys,
    Distance,
    Intersects,
    Point,
    Polygon,
    Region,
)
from tapdrop.errors import (
    AdqlSyntaxError,
    UnknownColumnError,
    UnsupportedAdqlError,
)

if TYPE_CHECKING:
    from tapdrop.registry import Registry, TableMeta


@dataclass(frozen=True)
class ConeHint:
    """A cone extracted from a literal ``CONTAINS(POINT, CIRCLE)`` predicate.

    For M4's HEALPix file pruning; this module only extracts it, it never
    prunes anything itself.
    """

    ra0: float
    dec0: float
    radius_deg: float


@dataclass(frozen=True)
class TranslationResult:
    sql: str
    cone_hint: ConeHint | None


# Defense in depth: table functions in a FROM/JOIN position are rejected
# generically by node shape in `_table_scope` regardless of name. This name
# check additionally covers the same functions appearing *outside* a table
# position (e.g. `SELECT read_csv('x')` as a scalar expression), which would
# otherwise fall through as an ordinary unresolved-function call.
_BLOCKED_ANONYMOUS_PREFIXES = ("read_", "glob", "copy_", "pragma_", "install_", "load_")

_STATEMENT_NAMES: dict[type[exp.Expr], str] = {
    exp.Copy: "COPY",
    exp.Attach: "ATTACH",
    exp.Detach: "DETACH",
    exp.Install: "INSTALL",
    exp.Pragma: "PRAGMA",
    exp.Set: "SET",
    exp.Command: "a non-SELECT command",
    exp.Drop: "DROP",
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
    exp.Create: "CREATE",
    exp.Merge: "MERGE",
}

_UNSUPPORTED_V11: dict[type[exp.Expr], str] = {
    Box: "BOX",
    Region: "REGION",
}

_ICRS = "icrs"


def translate(adql: str, registry: Registry) -> TranslationResult:
    """Translate one ADQL query into DuckDB SQL.

    Raises ``AdqlSyntaxError``, ``UnsupportedAdqlError``, ``UnknownTableError``,
    or ``UnknownColumnError`` -- see ``errors.py`` -- and never partially
    translates: either a ``TranslationResult`` comes back, or an exception
    does.
    """
    statement = _parse_single_select(adql)

    _validate_functions(statement)
    cte_names = _cte_names(statement)
    _validate_tables_and_columns(statement, registry, cte_names)
    _validate_geometry(statement)
    cone_hint = _extract_cone_hint(statement)
    _prune_hats_source(statement, registry, cte_names)

    sql = statement.sql(dialect=ADQL, comments=False)
    return TranslationResult(sql=sql, cone_hint=cone_hint)


def _parse_single_select(adql: str) -> exp.Select:
    statements = _parse(adql)
    if len(statements) != 1:
        raise UnsupportedAdqlError(
            f"Only a single SELECT statement is permitted; found {len(statements)} statements."
        )
    statement = statements[0]
    if not isinstance(statement, exp.Select):
        name = _STATEMENT_NAMES.get(type(statement), type(statement).__name__.upper())
        raise UnsupportedAdqlError(f"{name} is not a permitted ADQL statement; only SELECT is.")
    return statement


def _parse(adql: str) -> list[exp.Expr]:
    try:
        statements = sqlglot.parse(adql, read=ADQL)
    except ParseError as exc:
        detail = exc.errors[0] if exc.errors else {}
        position = _char_offset(adql, detail.get("line"), detail.get("col"))
        raise AdqlSyntaxError(str(exc), position=position) from exc
    non_empty = [s for s in statements if s is not None]
    if not non_empty:
        raise AdqlSyntaxError("No ADQL statement was found in the query.")
    return non_empty


def _char_offset(sql: str, line: object, col: object) -> int | None:
    if not isinstance(line, int) or not isinstance(col, int):
        return None
    lines = sql.splitlines(keepends=True)
    if line < 1 or line > len(lines):
        return None
    return sum(len(text) for text in lines[: line - 1]) + max(col - 1, 0)


def _validate_functions(statement: exp.Select) -> None:
    for node in statement.walk():
        if isinstance(node, (exp.ReadCSV, exp.ReadParquet)):
            raise UnsupportedAdqlError(f"'{node.sql_name()}' is not a permitted ADQL function.")
        if isinstance(node, exp.Anonymous):
            fname = (node.name or "").lower()
            if fname.startswith(_BLOCKED_ANONYMOUS_PREFIXES):
                raise UnsupportedAdqlError(f"'{node.name}' is not a permitted ADQL function.")


def _cte_names(statement: exp.Select) -> set[str]:
    return {cte.alias.lower() for cte in statement.find_all(exp.CTE) if cte.alias}


def _select_sources(select: exp.Select) -> list[exp.Expression]:
    sources: list[exp.Expression] = []
    from_ = select.args.get("from_")
    if from_ is not None and from_.this is not None:
        sources.append(from_.this)
    for join in select.args.get("joins") or []:
        if join.this is not None:
            sources.append(join.this)
    return sources


def _func_display_name(func: exp.Expr) -> str:
    name = getattr(func, "name", None)
    if name:
        return str(name)
    return type(func).__name__.upper()


def _table_scope(
    select: exp.Select, registry: Registry, cte_names: set[str]
) -> dict[str, TableMeta | None]:
    scope: dict[str, TableMeta | None] = {}
    for source in _select_sources(select):
        if isinstance(source, exp.Table):
            if isinstance(source.this, exp.Func):
                raise UnsupportedAdqlError(
                    f"'{_func_display_name(source.this)}' is not a permitted table source; "
                    "only registered tables are."
                )
            if not isinstance(source.this, exp.Identifier):
                raise UnsupportedAdqlError(
                    "Only plain table names are permitted as a FROM/JOIN source."
                )
            name = source.name
            alias = source.alias or name
            if not source.db and name.lower() in cte_names:
                scope[alias] = None
                continue
            qualified = f"{source.db}.{name}".lower() if source.db else name.lower()
            scope[alias] = registry.get(qualified)  # raises UnknownTableError, close matches
        elif isinstance(source, exp.Subquery):
            scope[source.alias or "_subquery"] = None
        # Any other source shape (e.g. UNNEST) is outside ADQL's grammar and
        # left unvalidated rather than guessed at.
    return scope


def _own_columns(select: exp.Select) -> list[exp.Column]:
    columns: list[exp.Column] = []
    for node in select.walk(prune=lambda n: n is not select and isinstance(n, exp.Select)):
        if isinstance(node, exp.Column):
            columns.append(node)
    return columns


def _has_column(meta: TableMeta, name: str) -> bool:
    lowered = name.lower()
    return any(column.name.lower() == lowered for column in meta.columns)


def _close_matches(name: str, candidates: list[str]) -> list[str]:
    return difflib.get_close_matches(name, candidates, n=3)


def _validate_columns(select: exp.Select, scope: dict[str, TableMeta | None]) -> None:
    known_metas = [meta for meta in scope.values() if meta is not None]
    unknown_source_present = any(meta is None for meta in scope.values())

    for column in _own_columns(select):
        colname = column.name
        qualifier = column.table

        if qualifier:
            if qualifier not in scope:
                continue  # not a source of this SELECT: a correlated outer reference
            meta = scope[qualifier]
            if meta is None:
                continue  # CTE or derived table: schema not tracked here, trust it
            if not _has_column(meta, colname):
                raise UnknownColumnError(
                    colname,
                    table=meta.qualified_name,
                    close_matches=_close_matches(colname, [c.name for c in meta.columns]),
                )
            continue

        if not known_metas:
            continue  # nothing but CTEs/subqueries in scope: schema not tracked here
        if any(_has_column(meta, colname) for meta in known_metas):
            continue
        if unknown_source_present:
            continue  # could belong to an untyped CTE/subquery source
        all_names = [c.name for meta in known_metas for c in meta.columns]
        raise UnknownColumnError(colname, close_matches=_close_matches(colname, all_names))


def _validate_tables_and_columns(
    statement: exp.Select, registry: Registry, cte_names: set[str]
) -> None:
    for select in statement.find_all(exp.Select):
        scope = _table_scope(select, registry, cte_names)
        _validate_columns(select, scope)


def _literal_string(expr: exp.Expression | None) -> str | None:
    if isinstance(expr, exp.Literal) and expr.is_string:
        return str(expr.this)
    return None


def _literal_number(expr: exp.Expression | None) -> float | None:
    if expr is None:
        return None
    if isinstance(expr, exp.Neg):
        inner = _literal_number(expr.this)
        return -inner if inner is not None else None
    if isinstance(expr, exp.Paren):
        return _literal_number(expr.this)
    if isinstance(expr, exp.Literal) and not expr.is_string:
        try:
            return float(expr.this)
        except (TypeError, ValueError):
            return None
    return None


def _validate_coordsys(node: Point | Circle | Polygon) -> None:
    value = _literal_string(node.args.get("coordsys"))
    if value is None:
        raise UnsupportedAdqlError(
            f"{type(node).__name__.upper()} requires a literal coordinate system, e.g. 'ICRS'."
        )
    if value.lower() != _ICRS:
        raise UnsupportedAdqlError(f"Coordinate frame '{value}' is not supported; only ICRS is.")


def _validate_polygon_vertex_count(node: Polygon) -> None:
    # DEVIATION: this taxonomy has no "invalid argument value" error class
    # (same gap `_validate_radius_literal` hits for a negative radius), so
    # AdqlSyntaxError is the closest fit -- see implementation-notes.md.
    exprs = node.args.get("expressions") or []
    if len(exprs) < 6 or len(exprs) % 2 != 0:
        raise AdqlSyntaxError(
            "POLYGON requires an even number of coordinates forming at least 3 vertices."
        )


def _validate_polygon_position(node: Polygon) -> None:
    # A POLYGON literal only has a defined SQL generation when it sits
    # directly inside CONTAINS/INTERSECTS -- `_region_ra_dec_sql` in
    # dialect.py unpacks it from there. Anywhere else (e.g. a bare
    # `SELECT POLYGON(...)`) it would fall through to DuckDB's generic
    # function-call generator with no registered `POLYGON` function, which is
    # a confusing runtime SQL error rather than a clean 400.
    if not isinstance(node.parent, (Contains, Intersects)):
        raise UnsupportedAdqlError(
            "POLYGON is only supported as an argument to CONTAINS or INTERSECTS in this version."
        )


def _is_region_operand(node: exp.Expression) -> bool:
    """True for a bare column or string literal used as a region value.

    This is how an ObsCore ``s_region`` argument reaches ``CONTAINS``/
    ``INTERSECTS``: ADQL has no ``REGION``-from-string-column syntax, so an
    ``s_region`` reference just parses as an ordinary ``exp.Column`` (or, for
    an inline STC-S test value, a plain string ``exp.Literal``). Deliberately
    narrow -- anything else (a subquery, a function call, ...) is rejected,
    since dialect.py passes this node's own generated SQL straight into a
    parsing UDF call and nothing wider than "a value" should reach that.
    """
    if isinstance(node, exp.Column):
        return True
    return isinstance(node, exp.Literal) and node.is_string


def _classify_intersects_operand(node: exp.Expression) -> str | None:
    if isinstance(node, Circle):
        return "circle"
    if isinstance(node, Polygon):
        return "polygon"
    if _is_region_operand(node):
        return "region"
    return None


def _validate_radius_literal(circle: Circle) -> None:
    # Only catches a *literal* negative radius; a non-literal (column,
    # parameter) negative radius is left to the runtime `radius >= 0.0`
    # guard in adql/udfs.py's tapdrop_cone_contains, which yields no rows
    # rather than raising. DEVIATION: this taxonomy has no "invalid
    # argument value" error class, so AdqlSyntaxError is the closest fit --
    # see implementation-notes.md.
    value = _literal_number(circle.args.get("radius"))
    if value is not None and value < 0:
        raise AdqlSyntaxError(f"CIRCLE radius must not be negative: {value:g}.")


def _validate_geometry(statement: exp.Select) -> None:
    # Checked as its own pass, before CONTAINS/DISTANCE shape checks below: a
    # BOX/POLYGON/REGION/INTERSECTS should always be named in the error,
    # never masked by e.g. "CONTAINS is only supported between POINT and
    # CIRCLE" just because it happens to sit inside a CONTAINS(...) call.
    for node in statement.walk():
        unsupported_name = _UNSUPPORTED_V11.get(type(node))
        if unsupported_name is not None:
            raise UnsupportedAdqlError(
                f"{unsupported_name} is not supported in this version (v1.1)."
            )

    for node in statement.walk():
        if isinstance(node, (Point, Circle, Polygon)):
            _validate_coordsys(node)
        if isinstance(node, Polygon):
            _validate_polygon_vertex_count(node)
            _validate_polygon_position(node)
        if isinstance(node, Contains):
            other = node.expression
            if not isinstance(node.this, Point):
                raise UnsupportedAdqlError(
                    "CONTAINS is only supported between POINT and CIRCLE, POLYGON, "
                    "or a region column/string in this version."
                )
            if isinstance(other, Circle):
                _validate_radius_literal(other)
            elif not (isinstance(other, Polygon) or _is_region_operand(other)):
                raise UnsupportedAdqlError(
                    "CONTAINS is only supported between POINT and CIRCLE, POLYGON, "
                    "or a region column/string in this version."
                )
        if isinstance(node, Intersects):
            left = _classify_intersects_operand(node.this)
            right = _classify_intersects_operand(node.expression)
            if left is None or right is None or {left, right} == {"circle"}:
                raise UnsupportedAdqlError(
                    "INTERSECTS is only supported between CIRCLE, POLYGON, and region "
                    "columns/strings (not between two CIRCLEs) in this version."
                )
        if isinstance(node, Distance) and not (
            isinstance(node.this, Point) and isinstance(node.expression, Point)
        ):
            raise UnsupportedAdqlError(
                "DISTANCE is only supported between two POINT values in this version."
            )
        if isinstance(node, (Coord1, Coord2, Coordsys)) and not isinstance(
            node.this, (Point, Circle)
        ):
            raise UnsupportedAdqlError(
                f"{type(node).__name__.upper()} requires a POINT or CIRCLE argument "
                "in this version."
            )


def _extract_cone_hint(statement: exp.Select) -> ConeHint | None:
    for node in statement.find_all(Contains):
        if not (isinstance(node.this, Point) and isinstance(node.expression, Circle)):
            continue
        circle = node.expression
        ra0 = _literal_number(circle.args.get("ra"))
        dec0 = _literal_number(circle.args.get("dec"))
        radius = _literal_number(circle.args.get("radius"))
        if ra0 is not None and dec0 is not None and radius is not None:
            return ConeHint(ra0=ra0, dec0=dec0, radius_deg=radius)
    return None


def _own_contains(statement: exp.Select) -> list[Contains]:
    """``Contains`` nodes belonging to *statement* itself, not a nested SELECT."""
    return [
        node
        for node in statement.walk(prune=lambda n: n is not statement and isinstance(n, exp.Select))
        if isinstance(node, Contains)
    ]


def _column_matches(node: exp.Expression | None, table_alias: str, column_name: str | None) -> bool:
    """True when *node* is a bare or *table_alias*-qualified reference to *column_name*."""
    if column_name is None or not isinstance(node, exp.Column):
        return False
    if node.name.lower() != column_name.lower():
        return False
    qualifier = node.table
    return not qualifier or qualifier.lower() == table_alias.lower()


def _file_list_sql(files: tuple[str, ...]) -> str:
    escaped = ", ".join("'" + f.replace("'", "''") + "'" for f in files)
    return f"[{escaped}]"


def _pruned_source_sql(files: tuple[str, ...]) -> str:
    """DuckDB SQL scanning exactly *files* - server-generated, never from query text.

    Mirrors ``Registry._select_sql``'s unpruned ``read_parquet(...)`` shape so
    a pruned HATS table still produces the same columns as its unpruned view.
    """
    return f"SELECT * FROM read_parquet({_file_list_sql(files)}, hive_partitioning=false)"


def _prune_hats_source(statement: exp.Select, registry: Registry, cte_names: set[str]) -> None:
    """Narrow a single HATS table's FROM source to the files its cone can touch.

    AIDEV-NOTE: this is the only place a query's shape influences which files
    get opened, and it never trusts the ADQL text itself for that - the
    replacement ``read_parquet([...])`` call is built entirely from
    ``TableMeta.source_uris`` (server state from discovery), the same way
    ``Registry._select_sql`` builds the unpruned view; nothing derived from
    the query text is interpolated into SQL. Every condition below must hold
    for a rewrite to happen at all; if any is unprovable, the FROM clause is
    left untouched and the full, unpruned view is scanned instead - a wrong
    prune would silently drop matching rows, which is worse than scanning
    extra files. In scope: exactly one FROM source (no JOIN, no CTE/subquery),
    naming a HATS table, with exactly one literal cone in this SELECT's own
    scope whose POINT names that table's RA/Dec columns.
    """
    sources = _select_sources(statement)
    if len(sources) != 1:
        return
    source = sources[0]
    if not isinstance(source, exp.Table) or isinstance(source.this, exp.Func):
        return

    scope = _table_scope(statement, registry, cte_names)
    alias = source.alias or source.name
    meta = scope.get(alias)
    if meta is None or meta.hats_order is None or meta.hats_pixels is None:
        return

    matches = [
        node
        for node in _own_contains(statement)
        if isinstance(node.this, Point)
        and isinstance(node.expression, Circle)
        and _column_matches(node.this.args.get("ra"), alias, meta.ra_column)
        and _column_matches(node.this.args.get("dec"), alias, meta.dec_column)
    ]
    if len(matches) != 1:
        return  # absent or ambiguous: only a single, unambiguous cone is pruned

    circle = matches[0].expression
    ra0 = _literal_number(circle.args.get("ra"))
    dec0 = _literal_number(circle.args.get("dec"))
    radius = _literal_number(circle.args.get("radius"))
    if ra0 is None or dec0 is None or radius is None:
        return  # non-literal cone: prune_files() needs concrete numbers

    # Local import: adql -> discovery, not the reverse (keeps the layering one-way).
    from tapdrop.discovery.hats import prune_files

    files = prune_files(meta, ra0, dec0, radius)
    if not files:
        # DuckDB's read_parquet() rejects an empty file list outright, so an
        # empty prune (the cone matches no shard) falls back to one arbitrary
        # real file with an always-false predicate: correct (zero rows), and
        # still far cheaper than scanning the whole table.
        select = sqlglot.parse_one(_pruned_source_sql(meta.source_uris[:1]), read="duckdb")
        assert isinstance(select, exp.Select)
        select = select.where("FALSE")
    else:
        select = sqlglot.parse_one(_pruned_source_sql(files), read="duckdb")
        assert isinstance(select, exp.Select)

    replacement = exp.Subquery(this=select, alias=exp.TableAlias(this=exp.to_identifier(alias)))
    source.replace(replacement)
