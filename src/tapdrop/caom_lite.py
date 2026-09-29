"""CAOM-lite tables and the ``ivoa.obscore`` view (RDD.md M9).

A scan (``discovery/scan.py``) produces one observation per image file; this
module turns those into the four-table CAOM-lite model RDD.md describes -
``caom.observation`` / ``caom.plane`` / ``caom.artifact`` / ``caom.chunk`` -
and the ``ivoa.obscore`` view that joins them into the ObsCore 1.1 mandatory
column set.

Like ``tapdrop.query_log``, every table here is service-provided: DuckDB holds
the rows directly, so each :class:`~tapdrop.registry.TableMeta` carries no
``source_uris`` and ``Registry.attach`` leaves it alone instead of trying to
build a view over a file that does not exist.

AIDEV-NOTE: RDD.md's CAOM-lite column listing has no home for two ObsCore 1.1
mandatory columns, so this adds one column to each of two tables rather than
serving a NULL the scanner already knows the value of:
``caom.observation.facility`` (ObsCore ``facility_name``, from ``TELESCOP``)
and ``caom.plane.s_resolution`` (ObsCore ``s_resolution``, from the WCS pixel
scale). Both are additive - every column RDD.md lists is still present.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from tapdrop.registry import ColumnMeta, TableMeta

if TYPE_CHECKING:  # pragma: no cover - typing only
    import duckdb

    from tapdrop.discovery.scan import DiscoveredCatalog, DiscoveredObservation
    from tapdrop.registry import Registry
    from tapdrop.sources import SkippedFile

__all__ = ["DATALINK_CONTENT_TYPE", "attach_images", "build_caom", "table_metas"]

#: What ``/datalink/links`` returns (DataLink 1.1 §3.3); ObsCore ``access_format``
#: for an artifact whose access_url is a DataLink document.
DATALINK_CONTENT_TYPE = "application/x-votable+xml;content=datalink"

_SCHEMAS = ("caom", "ivoa")


def _value(obs: DiscoveredObservation, name: str) -> object | None:
    field_value = obs.fields.get(name)
    return None if field_value is None else field_value.value


def _str(obs: DiscoveredObservation, name: str) -> str | None:
    value = _value(obs, name)
    return None if value is None else str(value)


def _float(obs: DiscoveredObservation, name: str) -> float | None:
    # FieldValue.value is deliberately `object`: a scanned field's type depends
    # on the header card it came from, so it is narrowed here, at the point the
    # column's type is known.
    value = _value(obs, name)
    return None if value is None else float(value)  # type: ignore[arg-type]


def _int(obs: DiscoveredObservation, name: str) -> int | None:
    value = _value(obs, name)
    return None if value is None else int(value)  # type: ignore[call-overload]


def _create_tables(con: duckdb.DuckDBPyConnection) -> None:
    for schema in _SCHEMAS:
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    con.execute("""
        CREATE OR REPLACE TABLE "caom"."observation" (
            obs_uri VARCHAR, collection VARCHAR, obs_id VARCHAR,
            instrument VARCHAR, target_name VARCHAR, facility VARCHAR
        )
    """)
    con.execute("""
        CREATE OR REPLACE TABLE "caom"."plane" (
            plane_uri VARCHAR, obs_uri VARCHAR, calib_level INTEGER,
            dataproduct_type VARCHAR, s_region VARCHAR, s_ra DOUBLE, s_dec DOUBLE,
            s_fov DOUBLE, s_resolution DOUBLE, t_min DOUBLE, t_max DOUBLE,
            t_exptime DOUBLE, em_min DOUBLE, em_max DOUBLE, em_filter VARCHAR
        )
    """)
    con.execute("""
        CREATE OR REPLACE TABLE "caom"."artifact" (
            artifact_uri VARCHAR, plane_uri VARCHAR, access_url VARCHAR,
            content_type VARCHAR, content_length BIGINT, product_type VARCHAR
        )
    """)
    con.execute("""
        CREATE OR REPLACE TABLE "caom"."chunk" (
            chunk_id VARCHAR, artifact_uri VARCHAR, extension INTEGER,
            naxis1 INTEGER, naxis2 INTEGER, wcs_json VARCHAR
        )
    """)


# AIDEV-NOTE: the ObsCore 1.1 mandatory columns, in the standard's order
# (ObsCore 1.1 Table 1). Columns tapdrop cannot know from a header scan are
# explicitly CAST(NULL AS ...) rather than omitted: a client that SELECTs a
# mandatory column must get a column back, not an unknown-identifier error.
_OBSCORE_VIEW_SQL = """
    CREATE OR REPLACE VIEW "ivoa"."obscore" AS
    SELECT
        p.dataproduct_type      AS dataproduct_type,
        p.calib_level           AS calib_level,
        o.collection            AS obs_collection,
        o.obs_id                AS obs_id,
        p.plane_uri             AS obs_publisher_did,
        a.access_url            AS access_url,
        a.content_type          AS access_format,
        CAST(a.content_length / 1024 AS BIGINT) AS access_estsize,
        o.target_name           AS target_name,
        p.s_ra                  AS s_ra,
        p.s_dec                 AS s_dec,
        p.s_fov                 AS s_fov,
        p.s_region              AS s_region,
        p.s_resolution          AS s_resolution,
        CAST(c.naxis1 AS BIGINT) AS s_xel1,
        CAST(c.naxis2 AS BIGINT) AS s_xel2,
        p.t_min                 AS t_min,
        p.t_max                 AS t_max,
        p.t_exptime             AS t_exptime,
        CAST(NULL AS DOUBLE)    AS t_resolution,
        CAST(1 AS BIGINT)       AS t_xel,
        p.em_min                AS em_min,
        p.em_max                AS em_max,
        CAST(NULL AS DOUBLE)    AS em_res_power,
        CAST(1 AS BIGINT)       AS em_xel,
        CAST('phot.flux' AS VARCHAR) AS o_ucd,
        CAST(NULL AS VARCHAR)   AS pol_states,
        CAST(0 AS BIGINT)       AS pol_xel,
        o.facility              AS facility_name,
        o.instrument            AS instrument_name
    FROM "caom"."plane" p
    JOIN "caom"."observation" o ON o.obs_uri = p.obs_uri
    LEFT JOIN "caom"."artifact" a ON a.plane_uri = p.plane_uri
    LEFT JOIN "caom"."chunk" c ON c.artifact_uri = a.artifact_uri
"""


def build_caom(con: duckdb.DuckDBPyConnection, catalog: DiscoveredCatalog, base_url: str) -> None:
    """Create and fill the CAOM-lite tables and ``ivoa.obscore`` from ``catalog``.

    ``base_url`` is the service's externally reachable prefix (including the
    token path when there is one); artifact ``access_url``s point at this
    service's own ``/datalink/links`` so a client follows DataLink to reach
    the file or a cutout, per RDD.md M10/M11.
    """
    _create_tables(con)

    observations: dict[str, tuple[str, str, str, str | None, str | None, str | None]] = {}
    planes: list[tuple[object, ...]] = []
    artifacts: list[tuple[object, ...]] = []
    chunks: list[tuple[object, ...]] = []
    used_plane_uris: set[str] = set()

    for obs in catalog.observations:
        collection = _str(obs, "obs_collection") or "UNKNOWN"
        obs_id = _str(obs, "obs_id") or obs.files
        obs_uri = f"caom:{collection}/{obs_id}"
        observations.setdefault(
            obs_uri,
            (
                obs_uri,
                collection,
                obs_id,
                _str(obs, "instrument_name"),
                _str(obs, "target_name"),
                _str(obs, "facility_name"),
            ),
        )

        calib_level = _int(obs, "calib_level")
        plane_uri = f"{obs_uri}/{calib_level if calib_level is not None else 'unknown'}"
        # Two files can describe the same observation at the same calibration
        # level (a mosaic split across HDUs, or a re-reduction); each is still
        # its own plane, so the URI gets a discriminator rather than colliding.
        if plane_uri in used_plane_uris:
            suffix = 2
            while f"{plane_uri}-{suffix}" in used_plane_uris:
                suffix += 1
            plane_uri = f"{plane_uri}-{suffix}"
        used_plane_uris.add(plane_uri)

        planes.append(
            (
                plane_uri,
                obs_uri,
                calib_level,
                _str(obs, "dataproduct_type"),
                _str(obs, "s_region"),
                _float(obs, "s_ra"),
                _float(obs, "s_dec"),
                _float(obs, "s_fov"),
                _float(obs, "s_resolution"),
                _float(obs, "t_min"),
                _float(obs, "t_max"),
                _float(obs, "t_exptime"),
                _float(obs, "em_min"),
                _float(obs, "em_max"),
                _str(obs, "em_filter"),
            )
        )

        artifact_uri = obs.files
        access_url = f"{base_url}/datalink/links?ID={quote(plane_uri, safe='')}"
        artifacts.append(
            (artifact_uri, plane_uri, access_url, DATALINK_CONTENT_TYPE, None, "science")
        )
        chunks.append(
            (
                f"{artifact_uri}#{obs.hdu_index}",
                artifact_uri,
                obs.hdu_index,
                _int(obs, "naxis1"),
                _int(obs, "naxis2"),
                obs.wcs_json,
            )
        )

    con.executemany(
        'INSERT INTO "caom"."observation" VALUES (?, ?, ?, ?, ?, ?)', list(observations.values())
    )
    con.executemany(
        'INSERT INTO "caom"."plane" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', planes
    )
    con.executemany('INSERT INTO "caom"."artifact" VALUES (?, ?, ?, ?, ?, ?)', artifacts)
    con.executemany('INSERT INTO "caom"."chunk" VALUES (?, ?, ?, ?, ?, ?)', chunks)
    con.execute(_OBSCORE_VIEW_SQL)


# (name, datatype, unit, ucd, utype, description), in the order the view declares
# them. utypes are ObsCore 1.1 Table 6, spelled as the standard's own example
# VOTable (ObsCore 1.1 Appendix C) spells them; the "obscore:" prefix is added
# in table_metas().
_OBSCORE_COLUMNS: tuple[tuple[str, str, str | None, str, str, str], ...] = (
    (
        "dataproduct_type",
        "char",
        None,
        "meta.code.class",
        "ObsDataset.dataProductType",
        "Logical data product type",
    ),
    (
        "calib_level",
        "int",
        None,
        "meta.code;obs.calib",
        "ObsDataset.calibLevel",
        "Calibration level, 0-4",
    ),
    ("obs_collection", "char", None, "meta.id", "DataID.collection", "Name of the data collection"),
    ("obs_id", "char", None, "meta.id", "DataID.observationID", "Observation identifier"),
    (
        "obs_publisher_did",
        "char",
        None,
        "meta.ref.ivoid",
        "Curation.publisherDID",
        "Dataset identifier from the publisher",
    ),
    (
        "access_url",
        "char",
        None,
        "meta.ref.url",
        "Access.reference",
        "URL used to access the data set",
    ),
    (
        "access_format",
        "char",
        None,
        "meta.code.mime",
        "Access.format",
        "Content format of the data set",
    ),
    (
        "access_estsize",
        "long",
        "kbyte",
        "phys.size;meta.file",
        "Access.size",
        "Estimated size of the data set",
    ),
    ("target_name", "char", None, "meta.id;src", "Target.name", "Astronomical object observed"),
    (
        "s_ra",
        "double",
        "deg",
        "pos.eq.ra",
        "Char.SpatialAxis.Coverage.Location.Coord.Position2D.Value2.C1",
        "Central right ascension, ICRS",
    ),
    (
        "s_dec",
        "double",
        "deg",
        "pos.eq.dec",
        "Char.SpatialAxis.Coverage.Location.Coord.Position2D.Value2.C2",
        "Central declination, ICRS",
    ),
    (
        "s_fov",
        "double",
        "deg",
        "phys.angSize;instr.fov",
        "Char.SpatialAxis.Coverage.Bounds.Extent.diameter",
        "Diameter of the covered region",
    ),
    (
        "s_region",
        "char",
        None,
        "pos.outline;obs.field",
        "Char.SpatialAxis.Coverage.Support.Area",
        "Region covered, as an STC-S shape",
    ),
    (
        "s_resolution",
        "double",
        "arcsec",
        "pos.angResolution",
        "Char.SpatialAxis.Resolution.Refval.value",
        "Spatial resolution of the data",
    ),
    (
        "s_xel1",
        "long",
        None,
        "meta.number",
        "Char.SpatialAxis.numBins1",
        "Number of elements along the first spatial axis",
    ),
    (
        "s_xel2",
        "long",
        None,
        "meta.number",
        "Char.SpatialAxis.numBins2",
        "Number of elements along the second spatial axis",
    ),
    (
        "t_min",
        "double",
        "d",
        "time.start;obs.exposure",
        "Char.TimeAxis.Coverage.Bounds.Limits.StartTime",
        "Start time, MJD",
    ),
    (
        "t_max",
        "double",
        "d",
        "time.end;obs.exposure",
        "Char.TimeAxis.Coverage.Bounds.Limits.StopTime",
        "Stop time, MJD",
    ),
    (
        "t_exptime",
        "double",
        "s",
        "time.duration;obs.exposure",
        "Char.TimeAxis.Coverage.Support.Extent",
        "Total exposure time",
    ),
    (
        "t_resolution",
        "double",
        "s",
        "time.resolution",
        "Char.TimeAxis.Resolution.Refval.value",
        "Temporal resolution FWHM",
    ),
    (
        "t_xel",
        "long",
        None,
        "meta.number",
        "Char.TimeAxis.numBins",
        "Number of elements along the time axis",
    ),
    (
        "em_min",
        "double",
        "m",
        "em.wl;stat.min",
        "Char.SpectralAxis.Coverage.Bounds.Limits.LoLimit",
        "Start in spectral coordinates",
    ),
    (
        "em_max",
        "double",
        "m",
        "em.wl;stat.max",
        "Char.SpectralAxis.Coverage.Bounds.Limits.HiLimit",
        "Stop in spectral coordinates",
    ),
    (
        "em_res_power",
        "double",
        None,
        "spect.resolution",
        "Char.SpectralAxis.Resolution.ResolPower.refVal",
        "Spectral resolving power",
    ),
    (
        "em_xel",
        "long",
        None,
        "meta.number",
        "Char.SpectralAxis.numBins",
        "Number of elements along the spectral axis",
    ),
    (
        "o_ucd",
        "char",
        None,
        "meta.ucd",
        "Char.ObservableAxis.ucd",
        "UCD of the observable quantity",
    ),
    (
        "pol_states",
        "char",
        None,
        "meta.code;phys.polarization",
        "Char.PolarizationAxis.stateList",
        "Polarization states present",
    ),
    (
        "pol_xel",
        "long",
        None,
        "meta.number",
        "Char.PolarizationAxis.numBins",
        "Number of elements along the polarization axis",
    ),
    (
        "facility_name",
        "char",
        None,
        "meta.id;instr.tel",
        "Provenance.ObsConfig.Facility.name",
        "Name of the facility used",
    ),
    (
        "instrument_name",
        "char",
        None,
        "meta.id;instr",
        "Provenance.ObsConfig.Instrument.name",
        "Name of the instrument used",
    ),
)

_CAOM_COLUMNS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "observation": (
        ("obs_uri", "char", "CAOM observation identifier"),
        ("collection", "char", "Data collection this observation belongs to"),
        ("obs_id", "char", "Observation identifier within the collection"),
        ("instrument", "char", "Instrument that took the observation"),
        ("target_name", "char", "Astronomical object observed"),
        ("facility", "char", "Facility (telescope) that took the observation"),
    ),
    "plane": (
        ("plane_uri", "char", "CAOM plane identifier"),
        ("obs_uri", "char", "Observation this plane belongs to"),
        ("calib_level", "int", "Calibration level, 0-4"),
        ("dataproduct_type", "char", "Logical data product type"),
        ("s_region", "char", "Region covered, as an STC-S shape"),
        ("s_ra", "double", "Central right ascension, ICRS degrees"),
        ("s_dec", "double", "Central declination, ICRS degrees"),
        ("s_fov", "double", "Diameter of the covered region, degrees"),
        ("s_resolution", "double", "Spatial resolution, arcsec"),
        ("t_min", "double", "Start time, MJD"),
        ("t_max", "double", "Stop time, MJD"),
        ("t_exptime", "double", "Exposure time, seconds"),
        ("em_min", "double", "Start in spectral coordinates, metres"),
        ("em_max", "double", "Stop in spectral coordinates, metres"),
        ("em_filter", "char", "Filter name as the header spells it"),
    ),
    "artifact": (
        ("artifact_uri", "char", "URI of the file this artifact describes"),
        ("plane_uri", "char", "Plane this artifact belongs to"),
        ("access_url", "char", "DataLink document listing ways to access the file"),
        ("content_type", "char", "Media type of access_url"),
        ("content_length", "long", "Size of the file in bytes, when known"),
        ("product_type", "char", "Role of the artifact within the plane"),
    ),
    "chunk": (
        ("chunk_id", "char", "Identifier of this chunk within its artifact"),
        ("artifact_uri", "char", "Artifact this chunk belongs to"),
        ("extension", "int", "HDU index (FITS) or node index (ASDF)"),
        ("naxis1", "int", "Pixels along the first spatial axis"),
        ("naxis2", "int", "Pixels along the second spatial axis"),
        ("wcs_json", "char", "WCS header cards as a JSON object"),
    ),
}


def table_metas() -> dict[str, TableMeta]:
    """Describe the CAOM-lite tables and ``ivoa.obscore`` for ``TAP_SCHEMA``.

    Keyed by lowercase qualified name, ready to merge into ``Registry.tables``.
    None carries ``source_uris``: DuckDB already holds them.
    """
    metas: dict[str, TableMeta] = {}
    for table_name, columns in _CAOM_COLUMNS.items():
        meta = TableMeta(
            schema_name="caom",
            table_name=table_name,
            description=f"CAOM-lite {table_name} records.",
            columns=tuple(
                ColumnMeta(
                    name=name,
                    datatype=datatype,
                    arraysize="*" if datatype == "char" else None,
                    description=description,
                )
                for name, datatype, description in columns
            ),
        )
        metas[meta.qualified_name.lower()] = meta

    obscore = TableMeta(
        schema_name="ivoa",
        table_name="obscore",
        description="ObsCore 1.1 view over the discovered images.",
        ra_column="s_ra",
        dec_column="s_dec",
        ra_dec_rule="ucd",
        ra_dec_confidence="high",
        columns=tuple(
            ColumnMeta(
                name=name,
                datatype=datatype,
                unit=unit,
                ucd=ucd,
                utype=f"obscore:{utype}",
                # ObsCore 1.1 Table 6: s_region is the one column with an xtype.
                xtype="adql:REGION" if name == "s_region" else None,
                description=description,
                arraysize="*" if datatype == "char" else None,
                principal=True,
                std=True,
            )
            for name, datatype, unit, ucd, utype, description in _OBSCORE_COLUMNS
        ),
    )
    metas[obscore.qualified_name.lower()] = obscore
    return metas


def attach_images(
    con: duckdb.DuckDBPyConnection,
    registry: Registry,
    image_sources: list[str],
    base_url: str,
) -> tuple[SkippedFile, ...]:
    """Scan ``image_sources``, build CAOM-lite, and register the tables.

    Called from ``serve`` after the catalog tables are attached, because
    ``TAP_SCHEMA`` is refilled here to include what this adds - the same
    sequence the query log uses.
    """
    from tapdrop.discovery.scan import scan_sources

    merged = scan_sources(image_sources)
    build_caom(con, merged, base_url)
    registry.tables.update(table_metas())
    registry.populate_tap_schema(con)
    return merged.skipped
