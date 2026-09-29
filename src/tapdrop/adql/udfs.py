"""Geometry helpers registered on the DuckDB connection.

AIDEV-NOTE: the cone predicate is a DuckDB SQL macro, not a Python UDF -- the
haversine formula and the pole-safe declination/right-ascension prefilter are
both expressible as plain DuckDB scalar expressions, and a macro is inlined
by DuckDB's optimizer -- no interpreter round trip per row. A cone over a
large table would be dominated by a Python ``create_function`` UDF, so one is
only worth using for something a macro genuinely cannot express.

The polygon/``s_region`` predicates below *do* need Python UDFs: they loop
over a variable-length vertex list and do vector math (great-circle segment
intersection, point-in-spherical-polygon), which a DuckDB SQL macro cannot
express. They are only ever evaluated for rows a query actually filters on
(no bulk-scan path depends on them the way the cone prefilter does), so the
per-row Python round trip is an acceptable cost for this milestone.

``tapdrop_cone_contains`` is the exact test (haversine <= radius) *and* the
declination-band and right-ascension-band prefilters, ANDed together. The
prefilters only ever narrow what ``dec BETWEEN`` / the RA check would let
through -- the haversine term is always evaluated too, so a wrong or
degenerate prefilter can only cost performance, never correctness. See
``tests/test_adql_udfs.py`` for the oracle test (astropy ``SkyCoord``) and
the test asserting the prefiltered and unprefiltered predicates return the
same rows.
"""

from __future__ import annotations

import math

import duckdb
import duckdb.func

_Vec3 = tuple[float, float, float]

_MACROS = (
    # Great-circle separation in degrees between (ra1, dec1) and (ra2, dec2),
    # all in degrees. Clamped into [0, 1] before asin() because floating
    # point can push the haversine term a hair over 1 for near-antipodal or
    # coincident points, which would otherwise NaN.
    """
    CREATE OR REPLACE MACRO tapdrop_hav_deg(ra1, dec1, ra2, dec2) AS (
        2.0 * degrees(asin(least(1.0, sqrt(
            pow(sin(radians(dec2 - dec1) / 2.0), 2)
            + cos(radians(dec1)) * cos(radians(dec2))
              * pow(sin(radians(ra2 - ra1) / 2.0), 2)
        ))))
    )
    """,
    # The declination-band edge farther from the equator, after clamping the
    # band to [-90, 90]. Using the *more poleward* edge (rather than dec0
    # itself) to derive the RA half-width below is what keeps the RA
    # prefilter a safe superset: cos(dec) is smallest, so radius / cos(dec)
    # is largest, at whichever edge of the band sits closer to a pole.
    """
    CREATE OR REPLACE MACRO tapdrop_dec_edge(dec0, radius) AS (
        CASE
            WHEN abs(least(90.0, dec0 + radius)) >= abs(greatest(-90.0, dec0 - radius))
            THEN least(90.0, dec0 + radius)
            ELSE greatest(-90.0, dec0 - radius)
        END
    )
    """,
    # Half-width in RA degrees that safely bounds the circle at this
    # declination band. Once the band edge is within ~0.01 deg of a pole (or
    # the projected half-width would exceed 180 deg anyway), the RA
    # prefilter degenerates and must widen to the full sky rather than
    # exclude anything -- that is the "must never change the result set"
    # invariant for this optimization.
    """
    CREATE OR REPLACE MACRO tapdrop_ra_half_width(dec0, radius) AS (
        CASE
            WHEN cos(radians(tapdrop_dec_edge(dec0, radius))) <= 1e-4 THEN 180.0
            ELSE least(180.0, radius / cos(radians(tapdrop_dec_edge(dec0, radius))))
        END
    )
    """,
    # Angular separation in RA alone, wrapped across the 0/360 seam into
    # [0, 180] (e.g. ra=1, ra0=359 -> 2, not 358).
    """
    CREATE OR REPLACE MACRO tapdrop_ra_sep_deg(ra, ra0) AS (
        abs((ra - ra0) - 360.0 * round((ra - ra0) / 360.0))
    )
    """,
    # The predicate CONTAINS(POINT, CIRCLE) compiles to. radius < 0 is
    # rejected earlier, at translate time, when the radius is a literal
    # (AdqlSyntaxError); `radius >= 0.0` here is the runtime fallback for a
    # non-literal radius (a column or parameter), so a negative value still
    # yields no rows instead of a false positive.
    """
    CREATE OR REPLACE MACRO tapdrop_cone_contains(ra, obj_dec, ra0, dec0, radius) AS (
        radius >= 0.0
        AND obj_dec BETWEEN greatest(-90.0, dec0 - radius) AND least(90.0, dec0 + radius)
        AND tapdrop_ra_sep_deg(ra, ra0) <= tapdrop_ra_half_width(dec0, radius)
        AND tapdrop_hav_deg(ra, obj_dec, ra0, dec0) <= radius
    )
    """,
)


def _radec_to_xyz(ra: float, dec: float) -> _Vec3:
    ra_r, dec_r = math.radians(ra), math.radians(dec)
    cd = math.cos(dec_r)
    return (cd * math.cos(ra_r), cd * math.sin(ra_r), math.sin(dec_r))


def _dot(a: _Vec3, b: _Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: _Vec3, b: _Vec3) -> _Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _norm(a: _Vec3) -> float:
    return math.sqrt(_dot(a, a))


def _angle_between(a: _Vec3, b: _Vec3) -> float:
    return math.atan2(_norm(_cross(a, b)), _dot(a, b))


def _neg(a: _Vec3) -> _Vec3:
    return (-a[0], -a[1], -a[2])


def _add(a: _Vec3, b: _Vec3) -> _Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a: _Vec3, b: _Vec3) -> _Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(a: _Vec3, s: float) -> _Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def _normalize(a: _Vec3) -> _Vec3:
    n = _norm(a)
    return (a[0] / n, a[1] / n, a[2] / n)


_ON_ARC_EPS = 1e-9
# Deterministic, non-axis-aligned nudge for the ray-casting reference point in
# `_reference_point` below. Any fixed offset works; only its lack of
# alignment with any RA/Dec grid direction matters -- see that function's
# AIDEV-NOTE.
_REFERENCE_JITTER: _Vec3 = (0.013712, -0.025193, 0.019384)


def _on_minor_arc(x: _Vec3, a: _Vec3, b: _Vec3) -> bool:
    """True if *x* lies on the minor (<= 180 deg) great-circle arc a -> b.

    AIDEV-NOTE: checking ``angle(a, x) <= arc_length`` and
    ``angle(b, x) <= arc_length`` independently is *not* sufficient -- for an
    arc whose length approaches 180 deg, points on the reflex side of the
    same great circle also satisfy both inequalities individually. The
    correct test is that the two distances sum to exactly the arc length
    (the equality case of the spherical triangle inequality). Verified
    empirically: this was the root cause of false edge crossings for
    near-antipodal reference-point rays during development (see
    implementation-notes.md).
    """
    arc = _angle_between(a, b)
    return abs(_angle_between(a, x) + _angle_between(b, x) - arc) < _ON_ARC_EPS


def _segments_intersect(a1: _Vec3, a2: _Vec3, b1: _Vec3, b2: _Vec3) -> bool:
    """True if great-circle minor arcs a1-a2 and b1-b2 cross."""
    n1 = _cross(a1, a2)
    n2 = _cross(b1, b2)
    line = _cross(n1, n2)
    length = _norm(line)
    if length < 1e-12:
        return False  # coincident or parallel great circles: no isolated crossing
    x1 = _scale(line, 1.0 / length)
    x2 = _neg(x1)
    return (_on_minor_arc(x1, a1, a2) and _on_minor_arc(x1, b1, b2)) or (
        _on_minor_arc(x2, a1, a2) and _on_minor_arc(x2, b1, b2)
    )


def _reference_point(verts: list[_Vec3]) -> _Vec3:
    """A point guaranteed outside the polygon *verts*, for ray casting.

    AIDEV-NOTE: the antipode of the polygon's own first vertex lies outside
    the polygon whenever the polygon's angular diameter is < 180 deg (true of
    every realistic ObsCore footprint): for any point X inside the polygon,
    sep(antipode(V0), X) = 180 - sep(V0, X) > 180 - diameter > 0. A pathological
    polygon spanning more than a hemisphere is out of scope for this
    milestone and not guaranteed correct here.

    The tiny fixed offset is necessary, not cosmetic: without it, a
    *symmetric* polygon (vertices evenly spaced, as many real footprints and
    every hand-written test polygon are) puts this ray exactly through
    another vertex for certain query points (e.g. the polygon's own centroid,
    or the pole for a ring centered on it), double-counting or dropping a
    crossing. The offset value is arbitrary; only its lack of alignment with
    any RA/Dec grid direction matters.
    """
    q0 = _neg(verts[0])
    return _normalize(_add(q0, _REFERENCE_JITTER))


def _is_degenerate_polygon(verts: list[_Vec3]) -> bool:
    """True if every vertex lies on a single great circle (zero enclosed area).

    AIDEV-NOTE: a zero-area polygon has an empty interior by definition, so it
    must contain no points -- but the crossing-number ray in
    ``_point_in_polygon_xyz`` is not reliably well-defined for one: every edge
    lies on the *same* great circle, so a ray that grazes that circle crosses
    an even or odd number of mutually-overlapping edges depending on
    floating-point boundary alignment (observed concretely: a point sitting on
    a shared vertex of a 3-point collinear "polygon" was classified inside).
    Detecting and short-circuiting this case sidesteps that ill-defined
    boundary behavior instead of trying to patch the ray-casting tie-break.
    """
    normal = None
    for i in range(1, len(verts)):
        candidate = _cross(verts[0], verts[i])
        if _norm(candidate) > 1e-12:
            normal = _normalize(candidate)
            break
    if normal is None:
        return True  # every vertex coincides with (or is antipodal to) verts[0]
    return all(abs(_dot(v, normal)) < 1e-9 for v in verts)


def _point_in_polygon_xyz(p: _Vec3, verts: list[_Vec3]) -> bool:
    if len(verts) < 3 or _is_degenerate_polygon(verts):
        return False
    q = _reference_point(verts)
    crossings = 0
    n = len(verts)
    for i in range(n):
        a, b = verts[i], verts[(i + 1) % n]
        if _segments_intersect(p, q, a, b):
            crossings += 1
    return crossings % 2 == 1


def _point_segment_min_angle(p: _Vec3, a: _Vec3, b: _Vec3) -> float:
    """Minimum angular distance from *p* to the minor arc a-b."""
    normal = _cross(a, b)
    normal_len = _norm(normal)
    if normal_len < 1e-15:
        return min(_angle_between(p, a), _angle_between(p, b))
    normal = _scale(normal, 1.0 / normal_len)
    cross_track = abs(math.pi / 2.0 - _angle_between(p, normal))
    foot = _sub(p, _scale(normal, _dot(p, normal)))
    foot_len = _norm(foot)
    if foot_len < 1e-15:
        return min(_angle_between(p, a), _angle_between(p, b))
    foot = _scale(foot, 1.0 / foot_len)
    if _on_minor_arc(foot, a, b):
        return cross_track
    return min(_angle_between(p, a), _angle_between(p, b))


def _polygon_vertices(ra_list: list[float], dec_list: list[float]) -> list[_Vec3]:
    return [_radec_to_xyz(ra, dec) for ra, dec in zip(ra_list, dec_list, strict=True)]


def tapdrop_point_in_polygon(
    ra: float, dec: float, poly_ra: list[float], poly_dec: list[float]
) -> bool:
    """``CONTAINS(POINT, POLYGON)`` / ``CONTAINS(POINT, s_region)`` predicate.

    Great-circle crossing-number (ray casting) test: winding-order
    independent by construction (only the parity of the crossing count
    matters, and reversing the vertex order reverses every edge but leaves
    which great circles the ray crosses unchanged), so a polygon given in
    either winding order -- both occur in the wild for ObsCore ``s_region``
    values -- classifies the same points as inside.
    """
    verts = _polygon_vertices(poly_ra, poly_dec)
    return _point_in_polygon_xyz(_radec_to_xyz(ra, dec), verts)


def tapdrop_circle_polygon_intersects(
    ra0: float,
    dec0: float,
    radius_deg: float,
    poly_ra: list[float],
    poly_dec: list[float],
) -> bool:
    """``INTERSECTS(CIRCLE, POLYGON)`` predicate (either argument order)."""
    verts = _polygon_vertices(poly_ra, poly_dec)
    if len(verts) < 3 or radius_deg < 0:
        return False
    center = _radec_to_xyz(ra0, dec0)
    radius = math.radians(radius_deg)
    if _point_in_polygon_xyz(center, verts):
        return True
    n = len(verts)
    for v in verts:
        if _angle_between(center, v) <= radius:
            return True
    for i in range(n):
        a, b = verts[i], verts[(i + 1) % n]
        if _point_segment_min_angle(center, a, b) <= radius:
            return True
    return False


def tapdrop_polygon_polygon_intersects(
    ra1: list[float], dec1: list[float], ra2: list[float], dec2: list[float]
) -> bool:
    """``INTERSECTS(POLYGON, POLYGON)`` predicate."""
    v1 = _polygon_vertices(ra1, dec1)
    v2 = _polygon_vertices(ra2, dec2)
    if len(v1) < 3 or len(v2) < 3:
        return False
    for v in v1:
        if _point_in_polygon_xyz(v, v2):
            return True
    for v in v2:
        if _point_in_polygon_xyz(v, v1):
            return True
    n1, n2 = len(v1), len(v2)
    for i in range(n1):
        a1, a2 = v1[i], v1[(i + 1) % n1]
        for j in range(n2):
            b1, b2 = v2[j], v2[(j + 1) % n2]
            if _segments_intersect(a1, a2, b1, b2):
                return True
    return False


def _parse_stcs_polygon(value: str | None) -> list[float] | None:
    """Parse an ObsCore ``s_region`` STC-S string: ``POLYGON ICRS ra1 dec1 ...``.

    Returns the flat ``[ra1, dec1, ra2, dec2, ...]`` list, or ``None`` if
    *value* is not a recognisable ICRS ``POLYGON``. AIDEV-NOTE: degrades to
    ``None`` on any parse failure -- wrong token count, non-ICRS frame,
    non-numeric coordinate -- rather than raising, the same convention
    ``tapdrop_cone_contains`` uses for a runtime-only-detectable bad value
    (there a negative radius from a column): a malformed value in one row's
    data must not fail the whole query, only exclude that row from a match.
    """
    if not value:
        return None
    tokens = value.split()
    if len(tokens) < 8 or tokens[0].upper() != "POLYGON" or tokens[1].upper() != "ICRS":
        return None
    coords = tokens[2:]
    if len(coords) % 2 != 0:
        return None
    try:
        return [float(c) for c in coords]
    except ValueError:
        return None


def tapdrop_parse_region_ra(value: str | None) -> list[float] | None:
    """Right ascensions of an ObsCore ``s_region`` STC-S ``POLYGON`` string."""
    coords = _parse_stcs_polygon(value)
    return None if coords is None else coords[0::2]


def tapdrop_parse_region_dec(value: str | None) -> list[float] | None:
    """Declinations of an ObsCore ``s_region`` STC-S ``POLYGON`` string."""
    coords = _parse_stcs_polygon(value)
    return None if coords is None else coords[1::2]


def register_geometry_udfs(con: duckdb.DuckDBPyConnection) -> None:
    """Register the cone macros and polygon/region UDFs the dialect emits calls to."""
    for macro_sql in _MACROS:
        con.execute(macro_sql)

    double_t = duckdb.type("DOUBLE")
    bool_t = duckdb.type("BOOLEAN")
    varchar_t = duckdb.type("VARCHAR")
    double_list_t = duckdb.list_type(double_t)

    con.create_function(
        "tapdrop_point_in_polygon",
        tapdrop_point_in_polygon,
        [double_t, double_t, double_list_t, double_list_t],
        bool_t,
    )
    con.create_function(
        "tapdrop_circle_polygon_intersects",
        tapdrop_circle_polygon_intersects,
        [double_t, double_t, double_t, double_list_t, double_list_t],
        bool_t,
    )
    con.create_function(
        "tapdrop_polygon_polygon_intersects",
        tapdrop_polygon_polygon_intersects,
        [double_list_t, double_list_t, double_list_t, double_list_t],
        bool_t,
    )
    # AIDEV-NOTE: null_handling="special" is required here, not cosmetic --
    # these two genuinely return SQL NULL for a non-NULL, well-formed-looking
    # *string* input that just isn't a recognisable STC-S POLYGON (e.g. a
    # non-ICRS frame or a malformed token). DuckDB's default null handling
    # forbids a UDF from doing that (only auto-propagated NULL, for a NULL
    # argument, is allowed) and raises at query time otherwise.
    con.create_function(
        "tapdrop_parse_region_ra",
        tapdrop_parse_region_ra,
        [varchar_t],
        double_list_t,
        null_handling=duckdb.func.FunctionNullHandling.SPECIAL,
    )
    con.create_function(
        "tapdrop_parse_region_dec",
        tapdrop_parse_region_dec,
        [varchar_t],
        double_list_t,
        null_handling=duckdb.func.FunctionNullHandling.SPECIAL,
    )
