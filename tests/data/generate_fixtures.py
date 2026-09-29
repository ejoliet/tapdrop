"""Generate the committed M1 test fixtures under ``tests/data/``.

Deterministic, no randomness: every value below is a literal so re-running
this script reproduces byte-identical fixtures (aside from Parquet/FITS
writer timestamps in their format metadata, which nothing here asserts on).

Run with ``uv run python tests/data/generate_fixtures.py``.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from astropy.io import fits
from astropy.table import Column, Table

HERE = Path(__file__).parent


def _write_parquet_with_metadata(
    path: Path, columns: dict[str, list[object] | np.ndarray], field_meta: dict[str, dict[str, str]]
) -> None:
    arrays = {name: pa.array(values) for name, values in columns.items()}
    fields = []
    for name, arr in arrays.items():
        meta = {k.encode(): v.encode() for k, v in field_meta.get(name, {}).items()}
        fields.append(pa.field(name, arr.type, metadata=meta or None))
    table = pa.table(arrays, schema=pa.schema(fields))
    pq.write_table(table, path)


def make_gaia_parquet() -> None:
    """UCD-rule fixture; unit/ucd/description round-trip via Arrow field metadata."""
    _write_parquet_with_metadata(
        HERE / "gaia.parquet",
        {
            "source_id": [1, 2, 3, 4, 5],
            "ra": [10.0, 45.5, 120.25, 200.75, 350.1],
            "dec": [-60.0, -10.5, 0.0, 30.25, 89.9],
            "phot_g_mean_mag": [12.1, 15.3, 9.8, 18.7, 13.4],
        },
        {
            "ra": {"unit": "deg", "ucd": "pos.eq.ra;meta.main", "description": "Right ascension"},
            "dec": {"unit": "deg", "ucd": "pos.eq.dec;meta.main", "description": "Declination"},
            "phot_g_mean_mag": {
                "unit": "mag",
                "ucd": "phot.mag",
                "description": "Mean G magnitude",
            },
        },
    )


def make_clusters_parquet_and_sidecar() -> None:
    """Name-rule fixture; unit/description round-trip via the ``.meta.yaml`` sidecar."""
    _write_parquet_with_metadata(
        HERE / "clusters.parquet",
        {
            "cluster_id": [1, 2, 3],
            "ra_icrs": [15.0, 180.0, 300.0],
            "dec_icrs": [-45.0, 0.0, 45.0],
            "n_members": [120, 340, 58],
        },
        {},  # no embedded Arrow metadata: forces the sidecar path
    )
    sidecar = {
        "description": "Star cluster catalog",
        "columns": {
            "ra_icrs": {"unit": "deg", "description": "ICRS right ascension"},
            "dec_icrs": {"unit": "deg", "description": "ICRS declination"},
        },
    }
    with (HERE / "clusters.meta.yaml").open("w") as fh:
        yaml.safe_dump(sidecar, fh, sort_keys=False)


def make_stars_csv() -> None:
    """Unit-sanity-rule fixture: ``lon``/``lat`` are not RA/Dec names but pass the range guard."""
    rows = [
        ("id", "lon", "lat", "flux"),
        (1, 15.0, -45.0, 1.2),
        (2, 200.0, 10.0, 3.4),
        (3, 359.9, 89.0, 5.6),
        (4, 0.1, -89.0, 7.8),
    ]
    with (HERE / "stars.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerows(rows)


def make_phot_tsv() -> None:
    """No plausible RA/Dec columns: detection must correctly report ``unresolved``."""
    rows = [
        ("object_id", "flux_u", "flux_g", "flux_r"),
        (1, 1023.4, 2048.7, 512.1),
        (2, 998.2, 1500.0, 733.9),
        (3, 512.5, 640.1, 305.4),
    ]
    with (HERE / "phot.tsv").open("w", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerows(rows)


def make_spec_ecsv() -> None:
    """Name-rule fixture in ECSV; unit/description round-trip via astropy Column."""
    table = Table()
    table["id"] = Column([1, 2, 3])
    table["ra"] = Column([10.5, 200.2, 300.9], unit="deg", description="Right ascension")
    table["dec"] = Column([-30.0, 0.0, 60.0], unit="deg", description="Declination")
    table["teff"] = Column([5000.0, 6000.0, 7000.0], unit="K", description="Effective temperature")
    table.write(HERE / "spec.ecsv", format="ascii.ecsv", overwrite=True)


def make_sources_votable() -> None:
    """UCD-rule fixture in VOTable; ucd travels through ``Column(meta={"ucd": ...})``."""
    table = Table()
    table["id"] = Column([1, 2])
    table["ra"] = Column([50.0, 250.0], unit="deg", meta={"ucd": "pos.eq.ra;meta.main"})
    table["dec"] = Column([-20.0, 40.0], unit="deg", meta={"ucd": "pos.eq.dec;meta.main"})
    table.write(HERE / "sources.vot", format="votable", overwrite=True)


def make_single_fits() -> None:
    """One BINTABLE HDU: no ``_hduN`` split. RA/Dec via ``TUCDn`` header keys."""
    cols = [
        fits.Column(name="id", format="K", array=np.array([1, 2, 3], dtype="i8")),
        fits.Column(name="ra", format="D", array=np.array([12.0, 130.0, 280.0])),
        fits.Column(name="dec", format="D", array=np.array([-15.0, 5.0, 55.0])),
    ]
    hdu = fits.BinTableHDU.from_columns(cols, name="CAT")
    hdu.header["TUCD2"] = "pos.eq.ra;meta.main"
    hdu.header["TUCD3"] = "pos.eq.dec;meta.main"
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(HERE / "single.fits", overwrite=True)


def make_multi_fits() -> None:
    """Two BINTABLE HDUs: exercises the ``{table}_hdu{N}`` split rule."""
    cols_a = [
        fits.Column(name="id", format="K", array=np.array([1, 2], dtype="i8")),
        fits.Column(name="value", format="D", array=np.array([1.5, 2.5])),
    ]
    cols_b = [
        fits.Column(name="id", format="K", array=np.array([10, 20, 30], dtype="i8")),
        fits.Column(name="ra", format="D", array=np.array([1.0, 2.0, 3.0])),
        fits.Column(name="dec", format="D", array=np.array([-1.0, -2.0, -3.0])),
    ]
    hdu_a = fits.BinTableHDU.from_columns(cols_a, name="A")
    hdu_b = fits.BinTableHDU.from_columns(cols_b, name="B")
    hdu_b.header["TUCD2"] = "pos.eq.ra;meta.main"
    hdu_b.header["TUCD3"] = "pos.eq.dec;meta.main"
    fits.HDUList([fits.PrimaryHDU(), hdu_a, hdu_b]).writeto(HERE / "multi.fits", overwrite=True)


def make_empty_fits() -> None:
    """Zero BINTABLE HDUs: discovery must report this file skipped, not crash."""
    fits.HDUList([fits.PrimaryHDU()]).writeto(HERE / "empty.fits", overwrite=True)


def make_corrupt_fits() -> None:
    """Garbage bytes with a ``.fits`` extension: proves one bad file never aborts discovery."""
    (HERE / "corrupt.fits").write_bytes(b"this is not a FITS file\x00\x01\x02")


def make_hats_catalog() -> None:
    """Mini HATS catalog: ``partition_info.csv`` + ``Norder=k/Npix=n.parquet`` shards.

    AIDEV-NOTE: real HATS nests shards as ``Norder=k/Dir=d/Npix=n.parquet``;
    this fixture simplifies to ``Norder=k/Npix=n.parquet`` (see the note in
    ``discovery/hats.py``) since discovery only recursively globs
    ``*.parquet`` and never parses the directory scheme itself.
    """
    root = HERE / "hats_catalog"
    root.mkdir(exist_ok=True)
    with (root / "partition_info.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Norder", "Npix"])
        writer.writerow([0, 0])
        writer.writerow([0, 1])
        writer.writerow([1, 4])

    shards = {
        "Norder=0": {
            "Npix=0.parquet": {
                "id": [1, 2],
                "ra": [5.0, 15.0],
                "dec": [-5.0, 5.0],
            },
            "Npix=1.parquet": {
                "id": [3, 4],
                "ra": [95.0, 105.0],
                "dec": [10.0, 20.0],
            },
        },
        "Norder=1": {
            "Npix=4.parquet": {
                "id": [5, 6],
                "ra": [200.0, 210.0],
                "dec": [-30.0, -40.0],
            },
        },
    }
    field_meta = {
        "ra": {"unit": "deg", "ucd": "pos.eq.ra;meta.main"},
        "dec": {"unit": "deg", "ucd": "pos.eq.dec;meta.main"},
    }
    for subdir, files in shards.items():
        (root / subdir).mkdir(exist_ok=True)
        for filename, columns in files.items():
            _write_parquet_with_metadata(root / subdir / filename, columns, field_meta)


def make_hats_pruning_catalog() -> None:
    """Mini HATS catalog for M4 pruning tests, with real HEALPix-consistent placement.

    Unlike ``make_hats_catalog()`` (which only tests directory-based
    discovery and never checks a row's actual sky position against its
    shard), every row here truly sits inside the nested HEALPix cell its
    filename claims - verified against ``cdshealpix.lonlat_to_healpix`` when
    this fixture was designed - so a geometrically-correct pruner keeps
    exactly the shards a cone can touch. Two partition orders are present
    (0 and 1) to exercise a catalog with adaptive depth. One order-0 shard
    (``Npix=4``) straddles the RA=0/360 seam on purpose.
    """
    root = HERE / "hats_pruning"
    root.mkdir(exist_ok=True)
    with (root / "partition_info.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Norder", "Npix"])
        writer.writerow([1, 0])
        writer.writerow([1, 25])
        writer.writerow([0, 9])
        writer.writerow([0, 4])

    shards = {
        "Norder=1": {
            # Pleiades-ish patch; order-1 pixel 0.
            "Npix=0.parquet": {"id": [1, 2], "ra": [56.75, 56.80], "dec": [24.12, 24.14]},
            # A distant patch; order-1 pixel 25.
            "Npix=25.parquet": {"id": [3, 4], "ra": [200.0, 200.05], "dec": [-10.0, -10.02]},
        },
        "Norder=0": {
            # Near the south pole; order-0 pixel 9.
            "Npix=9.parquet": {"id": [5, 6], "ra": [100.0, 95.0], "dec": [-87.0, -88.0]},
            # Straddles RA=0/360; order-0 pixel 4.
            "Npix=4.parquet": {"id": [7, 8], "ra": [0.3, 359.7], "dec": [-40.0, -40.0]},
        },
    }
    field_meta = {
        "ra": {"unit": "deg", "ucd": "pos.eq.ra;meta.main"},
        "dec": {"unit": "deg", "ucd": "pos.eq.dec;meta.main"},
    }
    for subdir, files in shards.items():
        (root / subdir).mkdir(exist_ok=True)
        for filename, columns in files.items():
            _write_parquet_with_metadata(root / subdir / filename, columns, field_meta)


def make_override_fixture() -> None:
    """Isolated fixture for the ``tapdrop.yaml`` overrides test.

    Lives in a subdirectory so it is not recursed into (and does not change
    the output) when ``tapdrop inspect tests/data`` scans the whole folder.
    """
    root = HERE / "override_src"
    root.mkdir(exist_ok=True)
    with (root / "widget.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "x", "y"])
        writer.writerow([1, 12.5, -8.0])
        writer.writerow([2, 200.0, 40.0])

    overrides = {
        "tables": {
            "override_src.widget": {
                "description": "Override test table",
                "ra": "x",
                "dec": "y",
                "columns": {"x": {"unit": "deg", "description": "Longitude override"}},
            }
        }
    }
    with (HERE / "overrides.yaml").open("w") as fh:
        yaml.safe_dump(overrides, fh, sort_keys=False)


def _write_fits_image(
    path: Path,
    naxis1: int,
    naxis2: int,
    wcs_header: dict[str, object] | None,
    extra_header: dict[str, object] | None = None,
) -> None:
    """A single-HDU FITS image: real (all-zero) pixel data plus the given header cards.

    Data-light on purpose (M7: "header-heavy, data-light") - callers pick
    NAXIS1/NAXIS2 small except where a test needs a non-trivial payload to
    prove header-only reads skip it.
    """
    data = np.zeros((naxis2, naxis1), dtype=np.float32)
    hdu = fits.PrimaryHDU(data=data)
    if wcs_header:
        for key, value in wcs_header.items():
            hdu.header[key] = value
    if extra_header:
        for key, value in extra_header.items():
            hdu.header[key] = value
    hdu.writeto(path, overwrite=True)


def make_image_ra0() -> None:
    """M7 fixture: footprint straddles the RA=0/360 seam (CRVAL1=0, equator)."""
    root = HERE / "images"
    root.mkdir(exist_ok=True)
    _write_fits_image(
        root / "ra0.fits",
        naxis1=20,
        naxis2=20,
        wcs_header={
            "CTYPE1": "RA---TAN",
            "CTYPE2": "DEC--TAN",
            "CRVAL1": 0.0,
            "CRVAL2": 0.0,
            "CRPIX1": 10.5,
            "CRPIX2": 10.5,
            "CDELT1": -0.05,
            "CDELT2": 0.05,
            "CUNIT1": "deg",
            "CUNIT2": "deg",
        },
    )


def make_image_pole() -> None:
    """M7 fixture: footprint sits near the north celestial pole (CRVAL2=89.95)."""
    root = HERE / "images"
    root.mkdir(exist_ok=True)
    _write_fits_image(
        root / "pole.fits",
        naxis1=20,
        naxis2=20,
        wcs_header={
            "CTYPE1": "RA---TAN",
            "CTYPE2": "DEC--TAN",
            "CRVAL1": 120.0,
            "CRVAL2": 89.95,
            "CRPIX1": 10.5,
            "CRPIX2": 10.5,
            "CDELT1": -0.02,
            "CDELT2": 0.02,
            "CUNIT1": "deg",
            "CUNIT2": "deg",
        },
    )


def make_image_basic() -> None:
    """M7 fixture away from any seam, full CAOM-lite metadata (time, filter,
    collection), and a 100x100 data payload big enough to prove header-only
    reads never touch pixels (see tests/test_image_fits.py)."""
    root = HERE / "images"
    root.mkdir(exist_ok=True)
    _write_fits_image(
        root / "basic.fits",
        naxis1=100,
        naxis2=100,
        wcs_header={
            "CTYPE1": "RA---TAN",
            "CTYPE2": "DEC--TAN",
            "CRVAL1": 200.0,
            "CRVAL2": -10.0,
            "CRPIX1": 50.5,
            "CRPIX2": 50.5,
            "CDELT1": -0.0005,
            "CDELT2": 0.0005,
            "CUNIT1": "deg",
            "CUNIT2": "deg",
        },
        extra_header={
            "DATE-OBS": "2025-06-01T00:00:00",
            "DATE-END": "2025-06-01T00:01:00",
            "EXPTIME": 60.0,
            "FILTER": "V",
            "TELESCOP": "TAPDROP-TEST",
            "INSTRUME": "TAPDROP-CAM",
            "OBJECT": "M42",
        },
    )


def make_image_no_wcs() -> None:
    """M7 fixture with no WCS and no time/filter keywords: every inferred field
    but NAXIS1/NAXIS2 must land in `unresolved`, and discovery must not crash."""
    root = HERE / "images"
    root.mkdir(exist_ok=True)
    _write_fits_image(root / "no_wcs.fits", naxis1=10, naxis2=10, wcs_header=None)


def make_image_corrupt() -> None:
    """Garbage bytes with a ``.fits`` extension: proves one bad image never aborts a scan."""
    root = HERE / "images"
    root.mkdir(exist_ok=True)
    (root / "corrupt.fits").write_bytes(b"this is not a FITS file\x00\x01\x02")


def make_golden_fixture() -> None:
    """Tiny, stable subset used only by the ``tapdrop inspect`` golden-output test."""
    root = HERE / "golden"
    root.mkdir(exist_ok=True)
    _write_parquet_with_metadata(
        root / "t1.parquet",
        {"id": [1, 2], "ra": [30.0, 40.0], "dec": [-5.0, 5.0]},
        {
            "ra": {"unit": "deg", "ucd": "pos.eq.ra;meta.main"},
            "dec": {"unit": "deg", "ucd": "pos.eq.dec;meta.main"},
        },
    )
    (root / "broken.fits").write_bytes(b"not a fits file")


def main() -> None:
    make_gaia_parquet()
    make_clusters_parquet_and_sidecar()
    make_stars_csv()
    make_phot_tsv()
    make_spec_ecsv()
    make_sources_votable()
    make_single_fits()
    make_multi_fits()
    make_empty_fits()
    make_corrupt_fits()
    make_hats_catalog()
    make_hats_pruning_catalog()
    make_override_fixture()
    make_golden_fixture()
    make_image_ra0()
    make_image_pole()
    make_image_basic()
    make_image_no_wcs()
    make_image_corrupt()


if __name__ == "__main__":
    main()
