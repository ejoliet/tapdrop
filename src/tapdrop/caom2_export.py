"""``tapdrop export --caom2-xml``: CAOM2 XML per observation (RDD.md M11).

This is the upgrade path RDD.md promises: what tapdrop serves as CAOM-lite in
DuckDB is written out as the real CAOM2 documents a CADC-style archive ingests.

AIDEV-NOTE: the export goes through ``caom_lite.build_caom`` into an in-memory
DuckDB rather than reading ``DiscoveredCatalog`` directly, so the identifiers
(observation URI, plane URI and its collision discriminator, chunk id) are
produced by exactly the code that serves them. An exported document and a live
``ivoa.obscore`` row therefore always agree on what a dataset is called.

The ``caom2`` package is an optional dependency (``[export]`` extra) and is
imported inside :func:`export_caom2`, the same way the Roman readers are: a
service that never exports should not pay for it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import duckdb

from tapdrop.adql.udfs import _parse_stcs_polygon as parse_stcs_polygon
from tapdrop.caom_lite import build_caom

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from tapdrop.discovery.scan import DiscoveredCatalog

__all__ = ["CaomExportError", "export_caom2"]

_ROWS_SQL = """
    SELECT o.obs_uri, o.collection, o.obs_id, o.instrument, o.target_name, o.facility,
           p.plane_uri, p.calib_level, p.dataproduct_type, p.s_region, p.s_resolution,
           p.t_min, p.t_max, p.t_exptime, p.em_min, p.em_max, p.em_filter,
           a.artifact_uri, a.content_type, a.content_length,
           c.extension, c.naxis1, c.naxis2
    FROM "caom"."plane" p
    JOIN "caom"."observation" o ON o.obs_uri = p.obs_uri
    LEFT JOIN "caom"."artifact" a ON a.plane_uri = p.plane_uri
    LEFT JOIN "caom"."chunk" c ON c.artifact_uri = a.artifact_uri
    ORDER BY o.obs_uri, p.plane_uri
"""

_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class CaomExportError(Exception):
    """The export cannot run: the ``[export]`` extra is missing."""


def export_caom2(catalog: DiscoveredCatalog, out_dir: Path) -> list[Path]:
    """Write one CAOM2 XML document per observation into ``out_dir``.

    Returns the paths written, in observation order.
    """
    caom2 = _import_caom2()

    con = duckdb.connect()
    try:
        # AIDEV-NOTE: an empty base_url is deliberate. Artifact access URLs are a
        # property of a running service, not of an archive document, and CAOM2
        # carries the artifact URI itself; nothing in the output depends on it.
        build_caom(con, catalog, base_url="")
        rows = con.execute(_ROWS_SQL).fetchall()
    finally:
        con.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    observations: dict[str, Any] = {}
    names: set[str] = set()

    for row in rows:
        obs_uri = str(row[0])
        observation = observations.get(obs_uri)
        if observation is None:
            observation = _observation(caom2, row)
            observations[obs_uri] = observation
            written.append(out_dir / _unique(_filename(str(row[1]), str(row[2])), names))
        plane = _plane(caom2, row)
        artifact = _artifact(caom2, row)
        if artifact is not None:
            plane.artifacts[artifact.uri] = artifact
        observation.planes[plane.product_id] = plane

    writer = caom2.ObservationWriter(validate=True)
    for path, observation in zip(written, observations.values(), strict=True):
        with path.open("w", encoding="utf-8") as handle:
            writer.write(observation, handle)
    return written


def _import_caom2() -> Any:
    try:
        import caom2
    except ImportError as exc:  # pragma: no cover - exercised with the extra absent
        raise CaomExportError(
            "CAOM2 export needs the caom2 package. Install it with the export extra: "
            'uv sync --extra export (or pip install "tapdrop[export]").'
        ) from exc
    return caom2


def _filename(collection: str, obs_id: str) -> str:
    """A filesystem-safe document name; obs_id comes from a FITS header."""
    return f"{_UNSAFE_IN_FILENAME.sub('_', collection)}_{_UNSAFE_IN_FILENAME.sub('_', obs_id)}.xml"


def _unique(name: str, taken: set[str]) -> str:
    """Disambiguate names that sanitising collapsed together.

    Two observation ids that differ only in a character ``_filename`` replaces
    (``a/b`` and ``a_b``) produce the same document name, and one export would
    silently overwrite the other.
    """
    stem = name.removesuffix(".xml")
    candidate = name
    discriminator = 2
    while candidate in taken:
        candidate = f"{stem}-{discriminator}.xml"
        discriminator += 1
    taken.add(candidate)
    return candidate


def _observation(caom2: Any, row: tuple[Any, ...]) -> Any:
    collection, obs_id, instrument, target_name, facility = row[1:6]
    return caom2.SimpleObservation(
        collection=str(collection),
        observation_id=str(obs_id),
        instrument=None if instrument is None else caom2.Instrument(str(instrument)),
        telescope=None if facility is None else caom2.Telescope(str(facility)),
        target=None if target_name is None else caom2.Target(str(target_name)),
    )


def _plane(caom2: Any, row: tuple[Any, ...]) -> Any:
    plane_uri, calib_level, dataproduct_type = row[6:9]
    s_region, s_resolution = row[9:11]
    t_min, t_max, t_exptime, em_min, em_max, em_filter = row[11:17]
    naxis1, naxis2 = row[21:23]

    plane = caom2.Plane(
        product_id=str(plane_uri).rsplit("/", 1)[-1],
        calibration_level=_enum(caom2.CalibrationLevel, calib_level),
        data_product_type=_enum(caom2.DataProductType, dataproduct_type),
    )
    plane.position = _position(caom2, s_region, s_resolution, naxis1, naxis2)
    plane.energy = _energy(caom2, em_min, em_max, em_filter)
    plane.time = _time(caom2, t_min, t_max, t_exptime)
    return plane


def _position(caom2: Any, s_region: Any, s_resolution: Any, naxis1: Any, naxis2: Any) -> Any | None:
    coords = parse_stcs_polygon(None if s_region is None else str(s_region))
    if coords is None:
        return None
    points = [caom2.Point(float(coords[i]), float(coords[i + 1])) for i in range(0, len(coords), 2)]
    # AIDEV-NOTE: CAOM2 requires the vertex list (`samples`) alongside the
    # point list, and the schema rejects a polygon without it. The vertices
    # repeat the points as MOVE + LINE... + a CLOSE at (0, 0), which is the
    # convention the caom2 reader round-trips.
    vertices = [
        caom2.Vertex(
            point.cval1,
            point.cval2,
            caom2.SegmentType.MOVE if index == 0 else caom2.SegmentType.LINE,
        )
        for index, point in enumerate(points)
    ]
    vertices.append(caom2.Vertex(0.0, 0.0, caom2.SegmentType.CLOSE))
    return caom2.Position(
        bounds=caom2.Polygon(points=points, samples=caom2.MultiPolygon(vertices)),
        dimension=(
            None
            if naxis1 is None or naxis2 is None
            else caom2.Dimension2D(int(naxis1), int(naxis2))
        ),
        resolution=None if s_resolution is None else float(s_resolution),
    )


def _energy(caom2: Any, em_min: Any, em_max: Any, em_filter: Any) -> Any | None:
    bounds = _interval(caom2, em_min, em_max)
    if bounds is None and em_filter is None:
        return None
    return caom2.Energy(bounds=bounds, bandpass_name=None if em_filter is None else str(em_filter))


def _time(caom2: Any, t_min: Any, t_max: Any, t_exptime: Any) -> Any | None:
    bounds = _interval(caom2, t_min, t_max)
    if bounds is None and t_exptime is None:
        return None
    return caom2.Time(bounds=bounds, exposure=None if t_exptime is None else float(t_exptime))


def _interval(caom2: Any, lower: Any, upper: Any) -> Any | None:
    if lower is None or upper is None:
        return None
    low, high = float(lower), float(upper)
    # A CAOM2 interval carries its sub-samples; one image contributes one
    # sample, and the schema requires at least that.
    return caom2.Interval(low, high, samples=[caom2.Interval(low, high)])


def _artifact(caom2: Any, row: tuple[Any, ...]) -> Any | None:
    artifact_uri, content_length = row[17], row[19]
    extension, naxis1, naxis2 = row[20:23]
    if artifact_uri is None:
        return None

    artifact = caom2.Artifact(
        uri=_as_uri(str(artifact_uri)),
        product_type=caom2.ProductType.SCIENCE,
        release_type=caom2.ReleaseType.DATA,
        # The stored content_type is the DataLink document a client is sent to;
        # the artifact itself is the file, so the exported type is the file's.
        content_type="application/fits",
        content_length=None if content_length is None else int(content_length),
    )
    if extension is None:
        return artifact

    part = caom2.Part(str(int(extension)), product_type=caom2.ProductType.SCIENCE)
    if naxis1 is not None and naxis2 is not None:
        part.chunks.append(caom2.Chunk(naxis=2, position_axis_1=1, position_axis_2=2))
    artifact.parts[part.name] = part
    return artifact


def _as_uri(value: str) -> str:
    """CAOM2 artifact URIs are URIs; a bare local path is not one."""
    return value if "://" in value else f"file://{value}"


def _enum(enum_type: Any, value: Any) -> Any | None:
    """Map a stored value onto a CAOM2 enum, or ``None`` when it is not one.

    A scanned header can hold anything, and a value CAOM2 has no term for must
    not abort an export of 500 other observations.
    """
    if value is None:
        return None
    try:
        return enum_type(value)
    except ValueError:
        return None
