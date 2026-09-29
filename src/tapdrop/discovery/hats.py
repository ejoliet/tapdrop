"""HATS (Hierarchical Adaptive Tiling Scheme) directory detection and reading.

A HATS catalog is a directory of Parquet shards under a partitioning scheme,
with either ``partition_info.csv`` or ``_common_metadata`` at its root. v1
detects it, reads its partition order, and treats the shards as one table.
M4 adds :func:`prune_files`: given a cone, the subset of shards whose
HEALPix cell can intersect it.

AIDEV-NOTE: real HATS catalogs bucket directories as ``Norder=k/Dir=d/Npix=n``
to keep any one directory small. The mini fixture used in tests simplifies
this to ``Norder=k/Npix=n.parquet`` - a real catalog is still handled
correctly because discovery just recursively finds every ``*.parquet`` file
under the root rather than parsing the directory scheme.
"""

from __future__ import annotations

import csv
import dataclasses
import re

import astropy.units as u
import duckdb
import fsspec
from astropy.coordinates import Angle, Latitude, Longitude
from cdshealpix import cone_search

from tapdrop.discovery import catalog
from tapdrop.errors import DiscoveryError
from tapdrop.registry import TableMeta
from tapdrop.sources import SkippedFile, SourceGroup, to_uri

_NORDER_DIR_RE = re.compile(r"Norder=(\d+)")
_NPIX_RE = re.compile(r"Npix=(\d+)")

# AIDEV-NOTE: cdshealpix's (and mocpy's, which shares the same underlying
# approximate-coverage algorithm) cone search becomes non-monotonic - and can
# under-cover the true cone - for radii approaching a full hemisphere and
# beyond (empirically: sky_fraction *drops* between radius=120 deg and
# radius=179 deg for a depth-1 cone, before jumping back to 1.0 at exactly
# 180 deg). A missed pixel there would silently drop matching rows, which is
# worse than scanning every file, so pruning is skipped outright above this
# threshold - well inside the range where the library's approximation is
# verified monotonic - and every shard is scanned instead.
_MAX_PRUNE_RADIUS_DEG = 90.0


def is_hats_dir(fs: fsspec.AbstractFileSystem, path: str) -> bool:
    """True when ``path`` looks like the root of a HATS catalog."""
    root = path.rstrip("/")
    try:
        has_info = fs.exists(f"{root}/partition_info.csv")
        has_common_meta = fs.exists(f"{root}/_common_metadata")
        return bool(has_info or has_common_meta)
    except OSError:
        return False


def _read_hats_order(fs: fsspec.AbstractFileSystem, path: str) -> int:
    root = path.rstrip("/")
    info_path = f"{root}/partition_info.csv"
    if fs.exists(info_path):
        with fs.open(info_path, "r") as fh:
            rows = list(csv.DictReader(fh))
        orders = [int(row["Norder"]) for row in rows if row.get("Norder") not in (None, "")]
        if orders:
            return max(orders)
    # Fall back to the Norder=<n> directory names themselves.
    orders = [int(m.group(1)) for entry in fs.find(root) if (m := _NORDER_DIR_RE.search(entry))]
    if not orders:
        raise DiscoveryError(f"could not determine HATS partition order under {root}")
    return max(orders)


def _shard_order_pixel(path: str) -> tuple[int, int] | None:
    """Parse a shard's ``(order, pixel)`` from its path, or ``None`` if absent."""
    order_match = _NORDER_DIR_RE.search(path)
    npix_match = _NPIX_RE.search(path)
    if order_match is None or npix_match is None:
        return None
    return int(order_match.group(1)), int(npix_match.group(1))


def discover_hats_table(
    con: duckdb.DuckDBPyConnection, group: SourceGroup, overrides: dict[str, object]
) -> tuple[list[TableMeta], list[SkippedFile]]:
    """Read a HATS directory into a single :class:`TableMeta`.

    Delegates the actual column/RA/Dec detection to
    :func:`tapdrop.discovery.catalog.discover_parquet` - a HATS catalog is a
    directory of Parquet shards, so reuse the Parquet reader over every shard
    found under the root instead of duplicating that logic.
    """
    root_uri = group.files[0]
    fs, path = fsspec.core.url_to_fs(root_uri)

    try:
        order = _read_hats_order(fs, path)
    except (DiscoveryError, OSError, ValueError, KeyError) as exc:
        return [], [SkippedFile(root_uri, f"could not read HATS partition info: {exc}")]

    try:
        shard_paths = sorted(p for p in fs.find(path) if p.lower().endswith(".parquet"))
    except OSError as exc:
        return [], [SkippedFile(root_uri, f"could not list HATS parquet shards: {exc}")]
    if not shard_paths:
        return [], [SkippedFile(root_uri, "HATS directory has no Parquet shard files")]

    # M4: per-shard (order, pixel) for prune_files(). A real HATS catalog
    # encodes both in every shard's path; if even one shard does not parse
    # (an unexpected layout), pruning is disabled for the whole table rather
    # than pruned against a partial, and therefore unsafe, pixel map.
    pixels = [_shard_order_pixel(p) for p in shard_paths]
    hats_pixels: tuple[tuple[int, int], ...] | None = None
    if all(p is not None for p in pixels):
        hats_pixels = tuple(p for p in pixels if p is not None)

    shard_group = SourceGroup(
        group.schema_name, group.table_name, tuple(to_uri(fs, p) for p in shard_paths)
    )
    metas, skipped = catalog.discover_parquet(con, shard_group, overrides)
    metas = [dataclasses.replace(meta, hats_order=order, hats_pixels=hats_pixels) for meta in metas]
    return metas, skipped


def prune_files(meta: TableMeta, ra0: float, dec0: float, radius_deg: float) -> tuple[str, ...]:
    """Subset of ``meta.source_uris`` whose HEALPix cell can intersect the cone.

    Cell-level filtering only: the result is a *superset* of the files that
    actually contain a matching row. The exact haversine ``WHERE`` clause
    still runs against every row the returned files produce, so a pixel that
    merely overlaps the cone's bounding disk without containing a match costs
    an extra file open, never a wrong row. Falls back to every file - i.e. no
    pruning - when the table is not HATS, the per-shard pixel map is
    unavailable, or the radius is outside the range ``prune_files`` can prove
    safe (see ``_MAX_PRUNE_RADIUS_DEG``).

    Handles a catalog with shards at more than one order: pruning runs once
    per distinct order present, using ``cdshealpix.cone_search`` (a
    cone-search-with-margin primitive, not hand-rolled pixel maths) to get
    the exact set of cells at that order the cone can touch.
    """
    if meta.hats_order is None or meta.hats_pixels is None:
        return meta.source_uris
    if not (0.0 <= radius_deg < _MAX_PRUNE_RADIUS_DEG):
        return meta.source_uris

    touched_by_order: dict[int, frozenset[int]] = {}
    for order in {order for order, _pixel in meta.hats_pixels}:
        ipix, _depth, _fully_covered = cone_search(
            lon=Longitude(ra0 * u.deg),
            lat=Latitude(dec0 * u.deg),
            radius=Angle(radius_deg, unit=u.deg),
            depth=order,
            flat=True,
        )
        touched_by_order[order] = frozenset(int(p) for p in ipix)

    return tuple(
        uri
        for uri, (order, pixel) in zip(meta.source_uris, meta.hats_pixels, strict=True)
        if pixel in touched_by_order[order]
    )
