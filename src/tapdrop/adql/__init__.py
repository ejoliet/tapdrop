"""ADQL 2.1 (subset) to DuckDB translation.

``dialect.py`` teaches sqlglot the ADQL geometry functions and ``TOP n``.
``translate.py`` is the security gate: it walks the parsed AST, resolves
tables/columns against the ``Registry``, and only then asks sqlglot to
generate DuckDB SQL. ``udfs.py`` registers the DuckDB macros the generated
SQL calls for cone search.
"""

from tapdrop.adql.translate import ConeHint, TranslationResult, translate

__all__ = ["ConeHint", "TranslationResult", "translate"]
