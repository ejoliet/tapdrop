"""``tapdrop scan``: image discovery -> ``tapdrop.discovered.yaml`` (RDD.md M7).

Wires the header-only FITS reader (``image_fits.py``) and the profile loader
(``profiles/``) into the provenance-carrying document RDD.md's "Discovery
rules (v1.1, images)" describes: every inferred field records where it came
from and how confident the guess is, and anything not determinable lands in
``unresolved`` rather than being silently omitted. One bad file is skipped
with a reason - scanning a directory never aborts on the first failure,
matching RDD.md's error-handling rule for catalog discovery.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import fsspec
import yaml
from astropy.time import Time

from tapdrop.discovery import image_fits
from tapdrop.discovery.profiles import Profile, load_profile
from tapdrop.sources import SkippedFile, to_uri

_FITS_SUFFIXES = (".fits.gz", ".fits", ".fit")

DEFAULT_PROFILE = "generic-fits-wcs"


@dataclass(frozen=True)
class FieldValue:
    """One inferred field: its value, where it came from, and how sure we are."""

    value: object
    source: str
    confidence: str  # "high" | "medium" | "low"


@dataclass(frozen=True)
class DiscoveredObservation:
    profile: str
    files: str  # source URI for this observation
    fields: dict[str, FieldValue] = field(default_factory=dict)
    unresolved: tuple[str, ...] = ()
    # AIDEV-NOTE: hdu_index and wcs_json are not inferred ObsCore fields, so
    # they carry no provenance and stay out of `fields`. They describe where
    # the pixels live (caom.chunk in M9, the SODA cutout in M11); recording
    # them at scan time means neither has to reopen the file to find the
    # image HDU or rebuild its WCS.
    hdu_index: int = 0
    wcs_json: str | None = None


@dataclass(frozen=True)
class DiscoveredCatalog:
    observations: tuple[DiscoveredObservation, ...] = ()
    skipped: tuple[SkippedFile, ...] = ()


def _is_fits(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(_FITS_SUFFIXES)


def _list_fits_files(source: str) -> tuple[list[str], list[SkippedFile]]:
    """Resolve ``source`` (a file, a directory, or a glob) to FITS file URIs.

    Every matching file becomes its own observation - unlike catalog
    discovery (``sources.py:resolve_sources``), images are never grouped into
    a shared table, so this only needs to list files, not name schemas.
    """
    fs, root = fsspec.core.url_to_fs(source)
    root = root.rstrip("/") or "/"

    if any(ch in root for ch in "*?["):
        matches = sorted(fs.glob(root))
        files = [to_uri(fs, m) for m in matches if _is_fits(m)]
        if not files:
            return [], [SkippedFile(source, "glob matched no FITS files")]
        return files, []

    try:
        is_dir = fs.isdir(root)
    except OSError as exc:
        return [], [SkippedFile(source, f"could not stat source: {exc}")]

    if is_dir:
        try:
            entries = sorted(fs.ls(root, detail=False))
        except OSError as exc:
            return [], [SkippedFile(source, f"could not list directory: {exc}")]
        files = [to_uri(fs, e) for e in entries if _is_fits(e)]
        return files, []

    if not _is_fits(root):
        return [], [SkippedFile(source, "not a FITS file")]
    return [to_uri(fs, root)], []


def _stem(uri: str) -> str:
    base = posixpath.basename(uri.rstrip("/"))
    for suffix in _FITS_SUFFIXES:
        if base.lower().endswith(suffix):
            return base[: -len(suffix)]
    return base


def _time_to_mjd(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(Time(str(value), format="fits").mjd)
    except (ValueError, TypeError):
        return None


def _format_s_region(footprint: image_fits.Footprint) -> str:
    # AIDEV-NOTE: STC-S POLYGON serialization ("ICRS ra1 dec1 ra2 dec2 ..."),
    # the conventional ObsCore s_region shape - not the ADQL POLYGON(...)
    # function-call syntax, which is a query-language construct, not a
    # storage format.
    coords = " ".join(f"{ra:.6f} {dec:.6f}" for ra, dec in footprint.vertices)
    return f"POLYGON ICRS {coords}"


def _wcs_to_json(image: image_fits.FitsImage) -> str | None:
    """The image's WCS as a JSON object of FITS cards, or ``None`` without a WCS.

    Stored verbatim in ``caom.chunk.wcs_json`` so a cutout (M11) can rebuild
    the WCS with ``astropy.wcs.WCS(fits.Header(json.loads(...)))`` instead of
    reopening the file.
    """
    if image.wcs is None:
        return None
    # default=str: header values astropy hands back as numpy scalars are not
    # JSON types, and a card's exact repr is enough to rebuild the header.
    return json.dumps(dict(image.wcs.to_header().items()), sort_keys=True, default=str)


def _scan_one(uri: str, profile: Profile) -> DiscoveredObservation:
    image = image_fits.read_fits_image(uri)
    header = image.header
    fields: dict[str, FieldValue] = {}
    unresolved: list[str] = []

    fields["dataproduct_type"] = FieldValue(
        profile.dataproduct_type, f"profile:{profile.name}.dataproduct_type", "high"
    )
    fields["calib_level"] = FieldValue(
        profile.default_calib_level, f"profile:{profile.name}.calib_level", "low"
    )

    telescop = header.get("TELESCOP")
    if telescop:
        fields["obs_collection"] = FieldValue(str(telescop).strip(), "TELESCOP", "high")
    else:
        fields["obs_collection"] = FieldValue(
            profile.default_obs_collection, f"profile:{profile.name}.obs_collection", "low"
        )

    target = header.get("OBJECT")
    if target:
        fields["target_name"] = FieldValue(str(target).strip(), "OBJECT", "high")
    else:
        unresolved.append("target_name")

    instrument = header.get("INSTRUME")
    if instrument:
        fields["instrument_name"] = FieldValue(str(instrument).strip(), "INSTRUME", "high")
    else:
        unresolved.append("instrument_name")

    if telescop:
        fields["facility_name"] = FieldValue(str(telescop).strip(), "TELESCOP", "high")
    else:
        unresolved.append("facility_name")

    obs_id_raw = header.get("OBS_ID") or header.get("OBSID")
    if obs_id_raw:
        fields["obs_id"] = FieldValue(str(obs_id_raw).strip(), "OBS_ID", "high")
    else:
        fields["obs_id"] = FieldValue(_stem(uri), "filename", "medium")

    fields["naxis1"] = FieldValue(image.naxis1, "NAXIS1", "high")
    fields["naxis2"] = FieldValue(image.naxis2, "NAXIS2", "high")

    exptime = header.get("EXPTIME")
    if exptime is not None:
        fields["t_exptime"] = FieldValue(float(exptime), "EXPTIME", "high")
    else:
        unresolved.append("t_exptime")

    t_min_mjd: float | None = None
    t_min_source = ""
    mjd_obs = header.get("MJD-OBS")
    if mjd_obs is not None:
        try:
            t_min_mjd, t_min_source = float(mjd_obs), "MJD-OBS"
        except (TypeError, ValueError):
            t_min_mjd = None
    if t_min_mjd is None:
        parsed = _time_to_mjd(header.get("DATE-OBS"))
        if parsed is not None:
            t_min_mjd, t_min_source = parsed, "DATE-OBS"
    if t_min_mjd is not None:
        fields["t_min"] = FieldValue(t_min_mjd, t_min_source, "high")
    else:
        unresolved.append("t_min")

    t_max_mjd = _time_to_mjd(header.get("DATE-END"))
    if t_max_mjd is not None:
        fields["t_max"] = FieldValue(t_max_mjd, "DATE-END", "high")
    elif t_min_mjd is not None and exptime is not None:
        fields["t_max"] = FieldValue(
            t_min_mjd + float(exptime) / 86400.0, f"{t_min_source}+EXPTIME", "medium"
        )
    else:
        unresolved.append("t_max")

    filter_name = header.get("FILTER")
    wavelen = header.get("WAVELEN")
    if filter_name:
        fields["em_filter"] = FieldValue(str(filter_name).strip(), "FILTER", "high")
        band = profile.bands.get(str(filter_name).strip().upper())
        if band is not None:
            source = f"profile:{profile.name}.bands.{filter_name}"
            fields["em_min"] = FieldValue(band.em_min, source, "medium")
            fields["em_max"] = FieldValue(band.em_max, source, "medium")
        elif wavelen is not None:
            fields["em_min"] = FieldValue(float(wavelen), "WAVELEN", "medium")
            fields["em_max"] = FieldValue(float(wavelen), "WAVELEN", "medium")
        else:
            unresolved.extend(["em_min", "em_max"])
    elif wavelen is not None:
        fields["em_min"] = FieldValue(float(wavelen), "WAVELEN", "medium")
        fields["em_max"] = FieldValue(float(wavelen), "WAVELEN", "medium")
        unresolved.append("em_filter")
    else:
        unresolved.extend(["em_filter", "em_min", "em_max"])

    footprint = image_fits.compute_footprint(image)
    if footprint is not None:
        fields["s_ra"] = FieldValue(footprint.ra, "WCS", "high")
        fields["s_dec"] = FieldValue(footprint.dec, "WCS", "high")
        fields["s_fov"] = FieldValue(footprint.fov_deg, "WCS", "high")
        fields["s_region"] = FieldValue(
            _format_s_region(footprint), "WCS edge-sampled footprint", "high"
        )
        if footprint.resolution_deg is not None:
            fields["s_resolution"] = FieldValue(
                footprint.resolution_deg * 3600.0, "WCS pixel scale", "high"
            )
        else:
            unresolved.append("s_resolution")
    else:
        unresolved.extend(["s_ra", "s_dec", "s_fov", "s_region", "s_resolution"])

    return DiscoveredObservation(
        profile=profile.name,
        files=uri,
        fields=fields,
        unresolved=tuple(unresolved),
        hdu_index=image.hdu_index,
        wcs_json=_wcs_to_json(image),
    )


def scan_images(source: str, profile_name: str = DEFAULT_PROFILE) -> DiscoveredCatalog:
    """Scan ``source`` (file, directory, or glob) into a :class:`DiscoveredCatalog`.

    A file that fails to read never aborts the scan; it is recorded in
    :attr:`DiscoveredCatalog.skipped` instead, per RDD.md's error handling.
    """
    files, skipped_from_listing = _list_fits_files(source)
    skipped: list[SkippedFile] = list(skipped_from_listing)
    profile = load_profile(profile_name)

    observations: list[DiscoveredObservation] = []
    for uri in files:
        try:
            observations.append(_scan_one(uri, profile))
        except Exception as exc:
            skipped.append(SkippedFile(uri, f"scan failed: {exc}"))

    return DiscoveredCatalog(observations=tuple(observations), skipped=tuple(skipped))


def scan_sources(sources: Iterable[str]) -> DiscoveredCatalog:
    """Scan several sources into one catalog, in the order they were given."""
    observations: list[DiscoveredObservation] = []
    skipped: list[SkippedFile] = []
    for source in sources:
        catalog = scan_images(source)
        observations.extend(catalog.observations)
        skipped.extend(catalog.skipped)
    return DiscoveredCatalog(observations=tuple(observations), skipped=tuple(skipped))


# --------------------------------------------------------------------------
# tapdrop.discovered.yaml writer / reader
# --------------------------------------------------------------------------


def _field_to_dict(fv: FieldValue) -> dict[str, object]:
    return {"value": fv.value, "from": fv.source, "confidence": fv.confidence}


def _observation_to_dict(obs: DiscoveredObservation) -> dict[str, object]:
    return {
        "profile": obs.profile,
        "files": obs.files,
        "hdu_index": obs.hdu_index,
        "wcs_json": obs.wcs_json,
        "fields": {name: _field_to_dict(fv) for name, fv in obs.fields.items()},
        "unresolved": list(obs.unresolved),
    }


def write_discovered_yaml(catalog: DiscoveredCatalog, out: Path) -> None:
    """Write ``catalog`` as ``tapdrop.discovered.yaml`` (RDD.md M7 shape)."""
    doc: dict[str, object] = {
        "observations": [_observation_to_dict(o) for o in catalog.observations]
    }
    if catalog.skipped:
        doc["skipped"] = [{"uri": s.uri, "reason": s.reason} for s in catalog.skipped]
    with out.open("w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)


def _field_from_dict(name: str, data: object) -> FieldValue:
    if not isinstance(data, dict):
        raise ValueError(f"malformed field {name!r} in discovered YAML")
    return FieldValue(
        value=data.get("value"),
        source=str(data.get("from")),
        confidence=str(data.get("confidence")),
    )


def read_discovered_yaml(path: Path) -> DiscoveredCatalog:
    """Read back a ``tapdrop.discovered.yaml`` written by :func:`write_discovered_yaml`."""
    with path.open() as fh:
        doc = yaml.safe_load(fh) or {}

    observations = []
    for raw in doc.get("observations") or []:
        raw_fields = raw.get("fields") or {}
        fields = {name: _field_from_dict(name, data) for name, data in raw_fields.items()}
        observations.append(
            DiscoveredObservation(
                profile=raw["profile"],
                files=raw["files"],
                fields=fields,
                unresolved=tuple(raw.get("unresolved") or ()),
                hdu_index=int(raw.get("hdu_index") or 0),
                wcs_json=raw.get("wcs_json"),
            )
        )

    skipped = tuple(SkippedFile(s["uri"], s["reason"]) for s in (doc.get("skipped") or []))
    return DiscoveredCatalog(observations=tuple(observations), skipped=skipped)
