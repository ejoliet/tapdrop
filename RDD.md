# tapdrop

> Point at catalog or image files (folder, drag-drop, S3, HTTP). Get a temporary, shareable, standards-compliant TAP endpoint in one command.

**Type**: A — Spec. An agent implements from this README.
**Repo**: `github.com/ejoliet/tapdrop`
**Status**: Draft, 2026-09-26

## Purpose

Astronomers who want to share a catalog today have two choices: email files, or ask an archive to stand up a TAP service. Standing up DaCHS, vollt, or a PostgreSQL + pgsphere stack takes days and a sysadmin.

tapdrop makes a **temporary** Virtual Observatory (VO) service from files in minutes. It reads data in place, needs no database server, and works with pyvo, astroquery, and TOPCAT unchanged.

**Who benefits**: a PI sharing a catalog with collaborators, a workshop instructor, a mission team (Roman, SPHEREx) previewing products, a reviewer checking a paper's catalog.

## Versions at a glance

| Version | Scope | Standards |
|---|---|---|
| **v1.0 — Catalogs** | Tables from Parquet/HATS, FITS bintable, CSV/TSV/ECSV, VOTable | TAP 1.1, ADQL 2.1 subset, UWS, VOSI, TAP_SCHEMA, TAPRegExt, DALI examples, SCS, TAP UPLOAD |
| **v1.1 — Images** | FITS and Roman ASDF images, auto-discovered | ObsCore 1.1 (view over CAOM-lite), SIA v2, DataLink, SODA sync cutouts |
| **v2 — Backlog** | See [Non-Goals](#non-goals) | — |

## User experience

### Publisher

```bash
# Local folder, one command, no install
uvx --from git+https://github.com/ejoliet/tapdrop tapdrop serve ./catalogs

# Remote data, never copied
uvx --from git+https://github.com/ejoliet/tapdrop tapdrop serve \
  s3://my-bucket/survey/ https://example.org/cat.fits

# Temporary and shared: 24 h lifetime, public tunnel, secret URL
tapdrop serve ./catalogs --ttl 24h --share --token

# Images (v1.1), Roman extra
uvx --from 'git+https://github.com/ejoliet/tapdrop[roman]' tapdrop serve --images ./roman_l2/

# Docker, same shape
docker run -p 8000:8000 -v "$PWD/catalogs:/data" ghcr.io/ejoliet/tapdrop serve /data
```

On start, tapdrop prints the endpoint, a QR code (with `--share`), the expiry time, and ready-to-paste snippets for the first detected table.

### Consumer

```bash
TAP=http://localhost:8000/tap

# Sync query
curl -G "$TAP/sync" \
  --data-urlencode "LANG=ADQL" \
  --data-urlencode "FORMAT=csv" \
  --data-urlencode "QUERY=SELECT TOP 10 source_id, ra, dec FROM cats.gaia_subset
    WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 56.75, 24.12, 0.5))"

# Async: submit and run, get job URL from Location header
JOB=$(curl -s -o /dev/null -w '%{redirect_url}' "$TAP/async" \
  --data-urlencode "LANG=ADQL" \
  --data-urlencode "PHASE=RUN" \
  --data-urlencode "QUERY=SELECT * FROM cats.gaia_subset WHERE phot_g_mean_mag < 12")

# Poll until COMPLETED (or ERROR)
curl -s "$JOB/phase"

# Fetch result (VOTable by default)
curl -s "$JOB/results/result" -o result.vot

# Simple Cone Search
curl "http://localhost:8000/scs/cats.gaia_subset?RA=56.75&DEC=24.12&SR=0.5"
```

```python
import pyvo
svc = pyvo.dal.TAPService("http://localhost:8000/tap")
svc.run_sync("SELECT TOP 5 * FROM cats.gaia_subset").to_table()
job = svc.submit_job("SELECT * FROM cats.gaia_subset"); job.run(); job.wait()
```

TOPCAT: *VO → Table Access Protocol (TAP) Query → TAP URL* = `http://localhost:8000/tap`.

## Architecture

```text
 sources (folder | s3:// | https:// | drag-drop upload)
        │
        ▼
 ┌──────────────┐   ┌─────────────────┐   ┌──────────────────────┐
 │ discovery    │──▶│ registry        │──▶│ TAP_SCHEMA (DuckDB)  │
 │ catalogs(v1) │   │ tables, columns │   │ + CAOM-lite (v1.1)   │
 │ images(v1.1) │   │ RA/Dec, UCDs    │   │ + ivoa.ObsCore view  │
 └──────────────┘   └─────────────────┘   └──────────────────────┘
                                                   │
 HTTP (FastAPI) ──▶ ADQL → sqlglot AST → DuckDB SQL ─┘
   /tap/sync  /tap/async (UWS)  /tap/tables  /tap/capabilities
   /scs  /sia  /datalink  /soda  /  (embedded landing page)
        │
        ▼
 result store (local dir | s3 prefix)   query log (Parquet)
```

**Key design choices**

- **File-native.** DuckDB scans Parquet/CSV in place, locally or over `httpfs`. FITS, ECSV, and VOTable are read with astropy and registered as Arrow tables. Nothing is copied unless `--cache` is set.
- **One engine for both versions.** Images become rows in CAOM-lite tables inside the same DuckDB. ObsCore is a view. SIA v2 is a parameter layer over TAP.
- **Temporary by design.** `--ttl` is a first-class setting. The expiry is shown in `/tap/availability` (`<downAt>`) and on the landing page.

> 💡 CAOM-lite, not full CAOM. The CADC reference stack is Java on PostgreSQL. tapdrop borrows only the hierarchy (observation → plane → artifact → chunk), which gives DataLink and SODA the per-file, per-extension WCS they need. `tapdrop export --caom2-xml` keeps an upgrade path to a real archive.

## Recommended stack

| Layer | Chosen | Why | Rejected |
|---|---|---|---|
| Query engine | DuckDB (Python) | Parquet/CSV/S3/HTTP native, in-process, fast scans | PostgreSQL + pgsphere/Q3C (server to run) |
| ADQL → SQL | Custom `sqlglot` dialect | Pure Python, no Java/antlr toolchain, targets DuckDB | `queryparser-python3` (PostgreSQL target, antlr to regenerate), CDS ADQL lib (Java) |
| Table I/O | astropy (`Table.read`/`write`), pyarrow | FITS, VOTable, ECSV with units and UCDs | pyarrow-only |
| HEALPix | `cdshealpix` + `mocpy` | Cone → pixel list for HATS pruning | healpy (no Windows wheel, heavy) |
| HTTP | FastAPI + uvicorn | Async, small, OpenAPI | Flask, Litestar |
| Async jobs | In-process UWS; job state in DuckDB; results in local dir or S3 | No Redis/Celery for a temporary service | Celery, RQ |
| Remote I/O | DuckDB `httpfs`, `fsspec`/`s3fs` for astropy readers | One credential chain (AWS env/profile) | boto3 direct |
| Images (v1.1) | astropy WCS, `asdf`, `gwcs`, `roman_datamodels` (`[roman]` extra) | Roman L2/L3 native | fits2caom2 blueprints (FITS-only) |
| CAOM export (v1.1) | `caom2` Python package | Upgrade path to CADC-style archive | — |
| Tunnel | `cloudflared` quick tunnel (shell-out) | No account needed | ngrok (account), bundled binary |
| Packaging | `uv`, hatchling, `uvx --from git+…` | Runs straight from GitHub | PyPI-only |

## Repository layout

```text
tapdrop/
├── pyproject.toml            # [project.scripts] tapdrop = tapdrop.cli:app; extras: roman, dev
├── README.md                 # this spec, later user docs
├── DEVELOPER.md              # local setup, test commands, adding a dialect rule
├── CHANGELOG.md
├── Dockerfile
├── src/tapdrop/
│   ├── cli.py                # typer: serve, inspect, scan, export
│   ├── config.py             # pydantic-settings, env + flags
│   ├── sources.py            # resolve folder / s3 / https / upload into file lists
│   ├── discovery/
│   │   ├── catalog.py        # per-format readers, RA/Dec + UCD detection
│   │   ├── hats.py           # partition_info, _healpix_29, pruning
│   │   ├── image_fits.py     # v1.1
│   │   ├── image_asdf.py     # v1.1, imports roman extra lazily
│   │   └── profiles/         # v1.1: generic-fits-wcs.yaml, roman-wfi.yaml, spherex.yaml
│   ├── registry.py           # table/column model, TAP_SCHEMA population
│   ├── caom_lite.py          # v1.1: tables + ObsCore view
│   ├── adql/
│   │   ├── dialect.py        # sqlglot ADQL dialect (TOP, geometry fns)
│   │   ├── translate.py      # ADQL AST → DuckDB SQL, pruning hints
│   │   └── udfs.py           # haversine, point/circle helpers registered in DuckDB
│   ├── engine.py             # DuckDB connection pool, limits, timeouts
│   ├── uws.py                # job model, phases, runner, result store
│   ├── output.py             # votable (BINARY2), csv, tsv, parquet, json
│   ├── api/
│   │   ├── tap.py            # /tap/sync, /tap/async, /tap/tables, /tap/capabilities, /tap/availability, /tap/examples
│   │   ├── scs.py
│   │   ├── sia.py            # v1.1
│   │   ├── datalink.py       # v1.1
│   │   ├── soda.py           # v1.1
│   │   └── ui.py             # serves embedded landing page, upload endpoint
│   ├── share.py              # tunnel, QR, token, TTL timer
│   ├── querylog.py
│   └── static/index.html     # single embedded HTML landing page
└── tests/
    ├── data/                 # tiny fixtures: parquet, fits, ecsv, votable, mini HATS, 2 FITS images, 1 ASDF
    ├── test_adql_*.py
    ├── test_discovery_*.py
    ├── test_api_*.py         # httpx against the app, pyvo against a live uvicorn
    └── conformance/          # expected VOSI XML shapes
```

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| AWS credentials | — | Only for `s3://` sources or S3 result store. Standard env/profile chain |
| `cloudflared` | any | Optional, for `--share` |
| Java 11+ + STILTS | — | Emmanuel's conformance checks only (`taplint`) |

## CLI contract

```text
tapdrop serve SOURCE... [--images DIR|URI ...]
    [--host 127.0.0.1] [--port 8000]
    [--ttl DURATION]            # e.g. 2h, 24h, 7d; default none (local), 24h if --share
    [--share]                   # start cloudflared quick tunnel; print URL + QR + snippets
    [--token]                   # generate secret; service lives under /t/<token>/
    [--allow-upload]            # enable drag-drop and TAP UPLOAD
    [--config tapdrop.yaml]     # overrides for discovery
    [--result-store PATH|s3://prefix]
    [--max-rows 100000] [--hard-max-rows 10000000]
    [--query-timeout 300] [--memory-limit 4GB]
    [--cache]                   # copy remote columns to local DuckDB on first use
    [--log-dir PATH]

tapdrop inspect SOURCE...       # print discovered tables, columns, RA/Dec guess, confidence, unresolved fields
tapdrop scan DIR|URI            # v1.1: write tapdrop.discovered.yaml for images
tapdrop export --caom2-xml OUT  # v1.1: CAOM2 XML per observation
tapdrop --version
```

> 💡 `--token` uses a **secret path prefix**, not only a header, because TOPCAT cannot easily set custom headers. `Authorization: Bearer <token>` is also accepted.

## HTTP interface

All paths are relative to the service root (`/` or `/t/<token>/`).

| Path | Method | Standard | Version |
|---|---|---|---|
| `/` | GET | Landing page (embedded HTML) | v1 |
| `/upload` | POST | Drag-drop ingest (`--allow-upload`) | v1 |
| `/tap/sync` | GET, POST | TAP 1.1 sync | v1 |
| `/tap/async` | GET, POST | TAP 1.1 async, UWS 1.1 | v1 |
| `/tap/async/{job}` + `/phase`, `/quote`, `/executionduration`, `/destruction`, `/error`, `/parameters`, `/results/result` | GET, POST, DELETE | UWS 1.1 | v1 |
| `/tap/capabilities` | GET | VOSI + TAPRegExt | v1 |
| `/tap/availability` | GET | VOSI, with `<downAt>` from TTL | v1 |
| `/tap/tables` | GET | VOSI tables | v1 |
| `/tap/examples` | GET | DALI examples (RDFa) | v1 |
| `/scs/{schema}.{table}` | GET | SCS 1.03 (`RA`, `DEC`, `SR`, `VERB`) | v1 |
| `/sia/query` | GET | SIA v2 (`POS`, `BAND`, `TIME`, `POL`, `COLLECTION`, `MAXREC`) | v1.1 |
| `/datalink/links` | GET | DataLink 1.1 (`ID`) | v1.1 |
| `/soda/sync` | GET | SODA 1.0 sync (`ID`, `CIRCLE`, `POLYGON`, `POS`) | v1.1 |
| `/healthz` | GET | Liveness | v1 |

**Output formats** via `FORMAT` / `RESPONSEFORMAT`: `votable` (BINARY2, default), `votable/td`, `csv`, `tsv`, `parquet`, `json`. VOTable overflow is flagged with `<INFO name="QUERY_STATUS" value="OVERFLOW"/>` when `MAXREC` truncates.

### ADQL support

| Feature | v1 | v1.1 | Translation |
|---|---|---|---|
| SELECT, WHERE, GROUP BY, HAVING, ORDER BY, JOIN, subqueries | ✅ | | Pass-through via sqlglot |
| `TOP n` | ✅ | | `LIMIT n` |
| Math/string functions in ADQL 2.1 | ✅ | | Mapped or UDF |
| `POINT`, `CIRCLE`, `DISTANCE` | ✅ | | UDFs; haversine in degrees |
| `CONTAINS(POINT, CIRCLE)` | ✅ | | Haversine + dec band prefilter + HEALPix pruning when available |
| `COORDSYS`, `COORD1`, `COORD2` | ✅ | | ICRS only; others → error |
| `BOX`, `POLYGON`, `INTERSECTS`, `REGION` | ❌ | ✅ for `ivoa.ObsCore.s_region` | Spherical polygon UDF (v1.1) |
| TAP UPLOAD (`tap_upload.*`) | ✅ sync, size-capped | | VOTable → temp DuckDB table |
| Non-ICRS frames | ❌ | ❌ | Explicit error |

Unsupported constructs return a VOTable error with `QUERY_STATUS=ERROR` and a message naming the construct.

## Discovery rules (v1, catalogs)

1. **Table naming**: schema = source folder name (sanitized), table = file stem or HATS catalog name. Globs with a shared schema become one table (`gaia_*.parquet` → `cats.gaia`).
2. **Formats**: `.parquet`, HATS directory (`partition_info.csv` or `_common_metadata`), `.fits/.fit/.fits.gz` (first BINTABLE HDU, or all as `name_hduN`), `.csv/.tsv`, `.ecsv`, `.vot/.xml` VOTable.
3. **RA/Dec detection**, in order:
   - UCD `pos.eq.ra;meta.main` / `pos.eq.dec;meta.main` from FITS `TUCDn` or VOTable `FIELD ucd`.
   - Names: `ra`, `ra_icrs`, `raj2000`, `ra_deg`, `alpha`, `s_ra` and dec equivalents (case-insensitive).
   - Unit sanity: values within [0, 360] and [-90, 90].
4. **Column metadata**: unit, UCD, description from FITS/VOTable/ECSV. Parquet: Arrow field metadata, then a sidecar `<stem>.meta.yaml` if present.
5. **Overrides** in `tapdrop.yaml`:

```yaml
tables:
  cats.gaia_subset:
    description: "Gaia DR3 subset around the Pleiades"
    ra: ra
    dec: dec
    primary_key: source_id
    columns:
      phot_g_mean_mag: {unit: mag, ucd: phot.mag;em.opt.G}
obscore:            # optional: expose a table as ivoa.ObsCore via column mapping (v1)
  source: cats.my_observations
  map: {obs_id: obsid, s_ra: ra, s_dec: dec, access_url: url, dataproduct_type: "'image'"}
```

## Discovery rules (v1.1, images)

`tapdrop scan` (and `serve --images`) reads headers only, never pixels, and writes `tapdrop.discovered.yaml`.

| Field | FITS source | Roman ASDF source |
|---|---|---|
| Footprint → `s_region`, `s_ra`, `s_dec`, `s_fov` | astropy WCS, corners + 4 samples per edge | `gwcs` footprint + edge samples |
| `t_min`, `t_max`, `t_exptime` | `DATE-OBS`, `MJD-OBS`, `EXPTIME`, `DATE-END` | exposure start/end/duration in `meta` |
| `em_min`, `em_max`, `em_filter` | `FILTER`, `WAVELEN`, profile lookup table | optical element in `meta` + profile band table |
| `calib_level`, `dataproduct_type` | profile rule | profile rule (L2 → 2, L3 mosaic → 3) |
| `obs_collection`, `obs_id` | profile rule, filename pattern | `meta` + filename |
| `s_resolution`, pixel axes | WCS pixel scale, `NAXISn` | `gwcs`, array shape |

Each inferred value records provenance and confidence:

```yaml
profile: roman-wfi
files: s3://bucket/roman/l2/*.asdf
fields:
  t_min: {from: "<meta path>", confidence: high}
  em_filter: {from: "<meta path>", confidence: high}
  em_min: {from: "profile:roman-wfi.bands", confidence: medium}
unresolved: [s_resolution]
```

> ⚠️ Roman `meta` key paths must be verified against a pinned `roman_datamodels` version and real L2/L3 files. They are placeholders here on purpose.

### CAOM-lite schema (v1.1)

| Table | Key columns |
|---|---|
| `caom.observation` | `obs_uri`, `collection`, `obs_id`, `instrument`, `target_name` |
| `caom.plane` | `plane_uri`, `obs_uri`, `calib_level`, `dataproduct_type`, `s_region`, `s_ra`, `s_dec`, `s_fov`, `t_min`, `t_max`, `t_exptime`, `em_min`, `em_max`, `em_filter` |
| `caom.artifact` | `artifact_uri`, `plane_uri`, `access_url`, `content_type`, `content_length`, `product_type` |
| `caom.chunk` | `chunk_id`, `artifact_uri`, `extension` (HDU index or ASDF node), `naxis1`, `naxis2`, `wcs_json` |

`ivoa.ObsCore` is a view joining `observation` and `plane` to `artifact`, with `access_url` pointing at `/datalink/links?ID=<plane_uri>` and `access_format` set to the DataLink content type.

**DataLink rows** per plane: `#this` (file), `#preview` (PNG generated on first request, cached), `#cutout` (service descriptor for `/soda/sync`).

**SODA**: sync only. `CIRCLE` and `POS` for both formats, `POLYGON` for FITS. Uses `astropy.nddata.Cutout2D` on the chunk WCS and returns FITS. Remote FITS uses fsspec range reads. Remote ASDF cutouts return `501` with a message unless the file is local or cached.

## Configuration reference

Every flag has an env var: `TAPDROP_<FLAG>` with upper snake case.

| Env var | Type | Default | Required | Notes |
|---|---|---|---|---|
| `TAPDROP_SOURCES` | comma-separated URIs | — | Yes (or CLI arg) | Folder, `s3://`, `https://` |
| `TAPDROP_IMAGES` | comma-separated URIs | — | No | v1.1 |
| `TAPDROP_HOST` | str | `127.0.0.1` | No | `0.0.0.0` in Docker image |
| `TAPDROP_PORT` | int | `8000` | No | |
| `TAPDROP_TTL` | duration | none | No | |
| `TAPDROP_TOKEN` | str | none | No | Set explicitly, or `--token` generates one |
| `TAPDROP_ALLOW_UPLOAD` | bool | `false` | No | |
| `TAPDROP_RESULT_STORE` | path or `s3://` | `~/.cache/tapdrop/results` | No | Use S3 on scale-to-zero hosts |
| `TAPDROP_MAX_ROWS` | int | `100000` | No | Default `MAXREC` |
| `TAPDROP_HARD_MAX_ROWS` | int | `10000000` | No | Upper bound on `MAXREC` |
| `TAPDROP_QUERY_TIMEOUT` | int seconds | `300` sync, `3600` async | No | |
| `TAPDROP_MEMORY_LIMIT` | str | `75%` of container | No | DuckDB `memory_limit` |
| `TAPDROP_MAX_JOBS` | int | `4` | No | Concurrent async jobs; excess queue, then `429` |
| `TAPDROP_UPLOAD_MAX_MB` | int | `200` | No | Drag-drop and TAP UPLOAD |
| `TAPDROP_LOG_DIR` | path | none | No | Enables query log |
| `TAPDROP_PUBLIC_URL` | URL | derived | No | Needed behind proxies for correct VOSI URLs |
| `AWS_*` | — | — | For S3 | Standard AWS chain |

## Error handling

| Error class | HTTP | Response | Retry |
|---|---|---|---|
| `AdqlSyntaxError` | 400 | VOTable error, position of failure | No |
| `UnsupportedAdqlError` | 400 | Names the construct | No |
| `UnknownTableError` / `UnknownColumnError` | 400 | Lists close matches | No |
| `SourceUnavailableError` (S3/HTTP) | 503 | Source URI, underlying cause | Client may retry |
| `QueryTimeoutError` | sync 408, async job `ERROR` | Elapsed time | No |
| `ResourceLimitError` (memory, job queue) | 429 | `Retry-After` | Yes |
| `UploadTooLargeError` | 413 | Limit | No |
| `ServiceExpiredError` | 503 | `downAt` passed; server exits after drain | No |
| `CutoutNotSupportedError` (v1.1) | 501 | Reason (remote ASDF, non-ICRS) | No |

Remote reads retry 3 times with exponential backoff (0.5 s base). Discovery failures on one file never stop the server; the file shows up in `inspect` and on the landing page as skipped, with the reason.

## Security

- **Read-only by construction.** DuckDB runs with only `SELECT` permitted on registered tables. The translator rejects anything but a single `SELECT` statement. `COPY`, `ATTACH`, `INSTALL`, `read_*` table functions, and file paths inside queries are blocked by an AST allowlist.
- Binds to `127.0.0.1` by default. `--share` and Docker are the only paths to public exposure.
- `--token` is 32 bytes URL-safe random. It is never logged; the query log records a hash.
- No secrets in code, docs, or the image. AWS credentials come from the environment only.
- Uploads are written to a temp dir, parsed with astropy, and deleted on shutdown.

## Non-Goals

**v1 and v1.1**
- Authentication beyond the secret token (use a reverse proxy)
- IVOA registry publication (capabilities are registry-ready; publishing is manual)
- Non-ICRS coordinate frames
- Write access, user-managed tables beyond UPLOAD
- Async SODA, spectral/cube cutouts, SSA
- Lambda deployment (async UWS conflicts with the 15-minute limit)
- Deploy automation for Cloud Run, Fly.io, HF Spaces (docs only)

**v2 backlog**
- Static export to GitHub Pages with DuckDB-WASM
- ADQL in the browser via Pyodide
- Remote ASDF cutouts via block range reads
- MOC endpoint per table, HiPS preview
- Cross-server JOINs with remote TAP services
- ObsCore for spectra and cubes; SSA

## Milestones and plan

Each milestone lists the **agent's** deliverables and done-when checks (automated, cheap), then **Emmanuel's** checks (manual or heavy, with exact commands). The agent must not run browsers, TOPCAT, STILTS, tunnels, pushes, or PRs.

### v1.0 — Catalogs

#### M0 — Scaffold and packaging

| Agent deliverable | Done when |
|---|---|
| `pyproject.toml` (hatchling, `[project.scripts]`, extras `roman`, `dev`), `src/` layout, typer CLI stub, CI workflow (ruff, mypy `--strict`, pytest), `DEVELOPER.md`, `CHANGELOG.md` | `uv run ruff check`, `uv run mypy src`, `uv run pytest` pass; `uv run tapdrop --version` prints version |

Emmanuel:
```bash
uvx --from git+https://github.com/ejoliet/tapdrop@m0 tapdrop --version
```

#### M1 — Discovery and registry

| Agent deliverable | Done when |
|---|---|
| Source resolution (folder, glob, `s3://`, `https://`); readers for all six formats; HATS detection; RA/Dec and UCD detection; TAP_SCHEMA population; `tapdrop inspect` | Fixture tests per format; S3 tested with `moto`; HTTP with a local `http.server` fixture; `inspect` golden-output test |

Emmanuel:
```bash
tapdrop inspect ./my_real_catalogs/
tapdrop inspect s3://<your-bucket>/<hats-catalog>/
```

#### M2 — ADQL translator and sync TAP

| Agent deliverable | Done when |
|---|---|
| sqlglot ADQL dialect; geometry UDFs; statement allowlist; `/tap/sync`; output writers (votable, csv, tsv, parquet, json); `MAXREC` + overflow | ≥ 60 translator cases (valid, invalid, unsupported, injection attempts); cone results match a brute-force astropy `SkyCoord.separation` reference on fixtures |

Emmanuel:
```bash
tapdrop serve ./tests/data &
curl -G http://localhost:8000/tap/sync --data-urlencode "LANG=ADQL" \
  --data-urlencode "QUERY=SELECT TOP 5 * FROM data.sample"
uv run --with pyvo python -c "import pyvo; print(pyvo.dal.TAPService('http://localhost:8000/tap').run_sync('SELECT TOP 5 * FROM data.sample').to_table())"
```

#### M3 — Async (UWS), VOSI, capabilities

| Agent deliverable | Done when |
|---|---|
| UWS job lifecycle, runner with `MAX_JOBS`, result store (local + S3), job destruction; `/tap/capabilities` with TAPRegExt (languages, geometry functions, output formats, upload methods, limits); `/tap/availability`; `/tap/tables`; `/tap/examples` | UWS phase transition tests; XML validated against IVOA XSDs vendored in `tests/conformance/`; pyvo `submit_job` test against live uvicorn |

Emmanuel:
```bash
stilts taplint tapurl=http://localhost:8000/tap
# TOPCAT: VO → TAP Query → http://localhost:8000/tap ; browse tables, run an example, run async
```

#### M4 — SCS, UPLOAD, HATS pruning

| Agent deliverable | Done when |
|---|---|
| `/scs/{table}` for every RA/Dec table; TAP UPLOAD (`UPLOAD=name,param:x` multipart) with size cap; HEALPix pruning for HATS (cone → pixel list at partition order → file filter) | SCS parameter/error tests; UPLOAD cross-match test; pruning test asserts files touched ≤ expected on a mini HATS fixture |

Emmanuel:
```bash
stilts taplint tapurl=http://localhost:8000/tap   # now includes UPLOAD
time curl -G http://localhost:8000/tap/sync --data-urlencode "LANG=ADQL" \
  --data-urlencode "QUERY=SELECT COUNT(*) FROM <schema>.<hats_table> WHERE 1=CONTAINS(POINT('ICRS',ra,dec),CIRCLE('ICRS',56.75,24.12,0.2))"
```

#### M5 — Landing page, share, TTL, token

| Agent deliverable | Done when |
|---|---|
| `static/index.html` (single file, no build): table browser, column view, ADQL box, snippets (curl sync/async, pyvo, astroquery, TOPCAT) pre-filled with a cone on the first RA/Dec table, drag-drop upload; `--share` (cloudflared shell-out, install hint if missing), terminal QR, `--ttl` with graceful drain, `--token` path prefix + bearer | API tests for `/upload`, token routing (401 without, 200 with), TTL shutdown with a 2 s TTL; HTML served with correct content type; no external JS except pinned CDN assets listed in `DEVELOPER.md` |

Emmanuel:
```bash
brew install cloudflared            # or your distro package
tapdrop serve ./my_real_catalogs --share --token --ttl 2h --allow-upload
# Open the printed URL on phone and laptop; drag a FITS table in; copy each snippet and run it
```

#### M6 — Ops, Docker, release v1.0

| Agent deliverable | Done when |
|---|---|
| Query log (Parquet, exposed as `tapdrop.query_log`); DuckDB memory limit and timeouts; `/healthz`; structured JSON logs; `Dockerfile` (`python:3.12-slim`, non-root, `0.0.0.0`); deploy docs for Cloud Run and HF Spaces (docs only); user-facing README section replaces this spec's UX section | Log tests; timeout test; `docker build` step in CI (build only, no push) |

Emmanuel:
```bash
docker build -t tapdrop:local .
docker run --rm -p 8000:8000 -v "$PWD/tests/data:/data" tapdrop:local serve /data
stilts taplint tapurl=http://localhost:8000/tap
git tag v1.0.0 && git push --tags     # then push image to ghcr.io/ejoliet/tapdrop
```

**v1.0 acceptance**
- [ ] `stilts taplint` reports no errors (warnings triaged in `CHANGELOG.md`)
- [ ] pyvo sync, async, and UPLOAD work; TOPCAT browses tables and runs examples
- [ ] Cone results match the astropy brute-force reference
- [ ] Works from `uvx --from git+…` on a clean macOS and Linux machine
- [ ] Remote Parquet/HATS on S3 served without copying
- [ ] Token, TTL, and share work end to end

### v1.1 — Images

#### M7 — Scan framework and FITS images

| Agent deliverable | Done when |
|---|---|
| `tapdrop scan`; header-only FITS reader; footprint with edge sampling; profile loader; `tapdrop.discovered.yaml` writer with provenance/confidence; `generic-fits-wcs` profile | Footprint tests on fixtures with known corners (including one crossing RA=0 and one near a pole); YAML round-trip test |

Emmanuel:
```bash
tapdrop scan ./some_fits_images/ && cat tapdrop.discovered.yaml
```

#### M8 — Roman ASDF discovery

| Agent deliverable | Done when |
|---|---|
| `[roman]` extra (`asdf`, `asdf-astropy`, `gwcs`, `roman_datamodels`, pinned); lazy import with a clear error if the extra is missing; `roman-wfi` profile (band table, calib levels) | Tests against a small synthetic ASDF fixture built with `roman_datamodels` maker utilities; `meta` key paths pinned and documented |

Emmanuel (real files; the agent does not download them):
```bash
uvx --from 'git+https://github.com/ejoliet/tapdrop@m8[roman]' tapdrop scan ./roman_l2_sample/
# Check t_min, em_filter, s_region against known values for 2–3 files
```

#### M9 — CAOM-lite, ObsCore view, SIA v2

| Agent deliverable | Done when |
|---|---|
| CAOM-lite tables; ObsCore 1.1 view with full mandatory columns; ObsCore listed in TAPRegExt data models; spherical polygon UDF for `s_region` (`INTERSECTS`, `CONTAINS(POINT, s_region)`); `/sia/query` | ObsCore column set checked against the standard's mandatory list; SIA `POS=CIRCLE/RANGE/POLYGON`, `BAND`, `TIME` tests; polygon tests across RA=0 |

Emmanuel:
```bash
tapdrop serve --images ./roman_l2_sample/ &
stilts taplint tapurl=http://localhost:8000/tap      # ObsCore checks
curl "http://localhost:8000/sia/query?POS=CIRCLE+<ra>+<dec>+0.1&MAXREC=10"
```

#### M10 — DataLink and previews

| Agent deliverable | Done when |
|---|---|
| `/datalink/links`; `#this`, `#preview`, `#cutout` service descriptor; PNG preview (asinh stretch, cached on disk); ObsCore `access_url` → DataLink | DataLink VOTable schema tests; preview generation test on fixtures |

Emmanuel:
```bash
# TOPCAT or Aladin: run an ObsCore query, open DataLink for a row, view preview
```

#### M11 — SODA cutouts, CAOM2 export, release v1.1

| Agent deliverable | Done when |
|---|---|
| `/soda/sync` (`CIRCLE`, `POS`; `POLYGON` for FITS); remote FITS range reads; ASDF local/cached; `501` for remote ASDF; `tapdrop export --caom2-xml` | Cutout WCS/pixel tests vs direct `Cutout2D`; remote FITS test against a local HTTP fixture; CAOM2 XML validated with the `caom2` package reader |

Emmanuel:
```bash
curl -o cut.fits "http://localhost:8000/soda/sync?ID=<plane_uri>&CIRCLE=<ra>+<dec>+0.01"
uv run --with astropy python -c "from astropy.io import fits; fits.info('cut.fits')"
git tag v1.1.0 && git push --tags
```

**v1.1 acceptance**
- [ ] `taplint` clean including ObsCore
- [ ] A real Roman L2 folder is served with correct footprints, times, and filters for sampled files
- [ ] SIA v2 and DataLink work from pyvo and TOPCAT; SODA cutout opens in DS9
- [ ] Catalog-only install (`uvx` without `[roman]`) still works and stays light

## Agent build instructions

> Implement end to end using only this README. Resolve Open Questions with Emmanuel before the milestone that needs them.

**Constraints**
- Python 3.11+, typed signatures, `ruff` + `mypy --strict` clean.
- Package so `uvx --from git+https://github.com/ejoliet/tapdrop tapdrop …` works at every milestone tag.
- Tests never hit real networks: `moto` for S3, local HTTP server fixtures, tiny committed fixtures (< 5 MB total).
- No blocking I/O in async handlers; run DuckDB and astropy work in a thread pool.
- Use `AIDEV-NOTE:` / `AIDEV-TODO:` comments for non-obvious decisions (translator rewrites, pruning, WCS edge cases).
- Heavy imports (`roman_datamodels`, `gwcs`) only inside v1.1 modules, imported lazily.
- Do not run browsers, TOPCAT, STILTS, tunnels, Docker pushes, `git push`, or PR creation. List the exact commands for Emmanuel in the milestone's PR description instead.
- Update `CHANGELOG.md` and `DEVELOPER.md` in the same change as behavior changes.

**Testing**

| Suite | Command | Covers |
|---|---|---|
| Unit | `uv run pytest tests -m "not live"` | Translator, discovery, writers, UWS model |
| Live | `uv run pytest tests -m live` | uvicorn + pyvo + httpx end to end |
| Coverage | `uv run pytest --cov=tapdrop` | Target ≥ 80% |

## Open Questions

- [ ] License: MIT (default for the tool portfolio) or Apache-2.0 for IVOA community reuse?
- [ ] Default schema name when serving a single file (`data` vs file stem)?
- [ ] Should `--share` also support `tailscale funnel` in v1, or cloudflared only?
- [ ] Roman `meta` key paths and band edges for the `roman-wfi` profile: need 2–3 real L2 and L3 files and the target `roman_datamodels` version (before M8).
- [ ] SPHEREx profile in v1.1, or wait for real products?
- [ ] Preview stretch for Roman L2 (asinh default vs zscale)?
- [ ] Performance target for a remote HATS cone search (seconds at 0.2° on Gaia DR3), to be set after M4 measurement.

## Next Steps

1. Answer the Open Questions marked for v1 (license, default schema, tunnel choice).
2. Create `ejoliet/tapdrop` and hand this README to the coding agent for M0–M2.
3. Run the M2 and M3 Emmanuel checks, especially `stilts taplint`.
4. Collect 2–3 Roman L2/L3 sample files before starting M8.
