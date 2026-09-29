# tapdrop

A temporary, standards-compliant IVOA TAP (Table Access Protocol) service over
files in place. No database server, no import step: point it at a folder,
an S3 prefix, or an HTTP URL and query the files directly with ADQL from
pyvo, astroquery, or TOPCAT.

Under the hood: DuckDB scans the files, a `sqlglot`-based translator turns
ADQL into DuckDB SQL, and FastAPI serves TAP 1.1, UWS, VOSI, SCS, TAP
UPLOAD, and — with `--images` — ObsCore, SIA, DataLink, and SODA cutouts.
See `RDD.md` for the full spec and `DEVELOPER.md` for local development and
deployment.

Images are v1.1: `--images DIR` scans FITS headers (never pixel data) and
publishes `ivoa.obscore`, `/sia/query`, `/datalink/links` and `/soda/sync`
alongside the catalog tables. Roman ASDF discovery (`[roman]` extra) is the
one v1.1 item still outstanding.

## Install

Not on PyPI yet — install from the repository:

```bash
uvx --from git+https://github.com/ejoliet/tapdrop tapdrop serve ./catalogs
# or install the command permanently
uv tool install git+https://github.com/ejoliet/tapdrop

# from source, for development
git clone https://github.com/ejoliet/tapdrop
cd tapdrop
uv sync --extra dev
```

## Quickstart

```bash
tapdrop serve ./catalogs
```

This discovers every catalog file under `./catalogs`, prints the tables it
found, and serves a TAP endpoint at `http://127.0.0.1:8000/tap`. Point pyvo,
astroquery, or TOPCAT at that URL.

```bash
uv run tapdrop serve tests/data   # try it against the repo's own fixtures
```

## Connecting

### pyvo

```python
import pyvo

service = pyvo.dal.TAPService("http://127.0.0.1:8000/tap")
result = service.run_sync(
    "SELECT TOP 10 * FROM cats.my_table "
    "WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 56.75, 24.12, 0.5))"
)
table = result.to_table()
```

### TOPCAT

*VO → Table Access Protocol (TAP) Query*, then set the TAP URL to
`http://127.0.0.1:8000/tap` and load.

### curl

```bash
curl -G http://127.0.0.1:8000/tap/sync \
  --data-urlencode "LANG=ADQL" \
  --data-urlencode "QUERY=SELECT TOP 5 * FROM cats.my_table"
```

### Endpoints

| Path | What it is |
|---|---|
| `/` | Landing page: discovered tables, ready-made snippets, an ADQL box |
| `/tap/sync` | TAP 1.1 synchronous queries (`GET` and `POST`) |
| `/tap/async` | TAP 1.1 asynchronous queries, UWS 1.1 job resources |
| `/tap/capabilities`, `/tap/availability`, `/tap/tables`, `/tap/examples` | VOSI and DALI examples |
| `/scs/{schema}.{table}` | Simple Cone Search 1.03, for every table with RA/Dec |
| `/sia/query` | SIA v2 over `ivoa.obscore`, with `--images` |
| `/datalink/links` | DataLink 1.1 links per image: file, preview, cutout service |
| `/datalink/file`, `/datalink/preview` | The image itself, and a PNG preview |
| `/soda/sync` | SODA 1.0 cutouts (`CIRCLE`, `POLYGON`, `POS`) |
| `/upload` | Drag-drop ingest, with `--allow-upload` |
| `/healthz` | Liveness, outside the token prefix |

With `--token`, everything except `/healthz` lives under `/t/<token>/`. The
DAL endpoints (`/tap/sync`, `/scs/...`, `/sia/query`, `/datalink/...`,
`/soda/sync`) take their parameters by `GET` or `POST`.

## Discovery

`tapdrop serve` and `tapdrop inspect` read files in place and build a table
registry automatically:

- **Formats**: Parquet, HATS catalog directories (detected by
  `partition_info.csv` or `_common_metadata`), FITS/`.fit`/`.fits.gz`
  (first binary table HDU), CSV, TSV, ECSV, and VOTable (`.vot`/`.xml`).
- **Table naming**: schema = source folder name, table = file stem or HATS
  catalog name. A glob with a shared schema collapses into one table (for
  example `gaia_*.parquet` under `cats/` becomes `cats.gaia`).
- **RA/Dec detection**: first by UCD (`pos.eq.ra;meta.main` /
  `pos.eq.dec;meta.main`), then by common column names (`ra`, `ra_icrs`,
  `raj2000`, `alpha`, `s_ra`, and the Dec equivalents, case-insensitive),
  then sanity-checked against `[0, 360]` / `[-90, 90]`.
- **Column metadata** (unit, UCD, description) comes from FITS/VOTable/ECSV
  headers, or Parquet field metadata.

Run `tapdrop inspect SOURCE...` to see what would be discovered — tables,
columns, the RA/Dec guess and its confidence, and any unresolved or skipped
files — without starting a server.

Sources can be local folders/globs, `s3://` prefixes, or `https://` URLs;
remote files are queried in place unless `--cache` is set.

## Images (v1.1)

```bash
tapdrop serve ./catalogs --images ./images
```

FITS headers and WCS are read (never pixel data) into CAOM-lite tables and
an `ivoa.obscore` view, so images are queryable as ordinary TAP:

```sql
SELECT obs_id, s_ra, s_dec, access_url FROM ivoa.obscore
WHERE INTERSECTS(CIRCLE('ICRS', 200.0, -10.0, 0.5), s_region) = 1
```

`access_url` leads to `/datalink/links`, which offers the file itself, a PNG
preview, and a `/soda/sync` cutout service. SIA v2 clients can use
`/sia/query?POS=CIRCLE 200 -10 0.5` instead.

Two commands work on images without serving them:

```bash
tapdrop scan ./images --out tapdrop.discovered.yaml  # what was inferred, and how confidently
tapdrop export ./out --caom2-xml --images ./images   # CAOM2 XML per observation ([export] extra)
```

Roman ASDF files need the `[roman]` extra and are not yet discovered.

## ADQL support

tapdrop translates a subset of ADQL 2.1 to DuckDB SQL:

- `SELECT`, `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY`, `JOIN`, subqueries,
  and ADQL math/string functions, passed through or mapped.
- `TOP n`, translated to `LIMIT n`.
- Geometry: `POINT`, `CIRCLE`, `POLYGON`, `DISTANCE`,
  `CONTAINS(POINT(...), CIRCLE(...) | POLYGON(...) | s_region)` and
  `INTERSECTS` between circles, polygons and ObsCore `s_region` values
  (every pairing except circle/circle). Coordinates are ICRS only; other
  frames are rejected with an explicit error.
- `tap_upload.*` for TAP UPLOAD (sync only, size-capped — see Uploads below).
- `TAP_SCHEMA.schemas`, `.tables`, `.columns`, `.keys` and `.key_columns`,
  queryable like any other table (read-only, case-insensitive).

Not supported: `BOX`, `REGION`, `INTERSECTS(CIRCLE, CIRCLE)`, and non-ICRS
frames. An unsupported construct returns a VOTable error naming it rather
than failing silently.

## Sharing

```bash
tapdrop serve ./catalogs --share --token --ttl 24h
```

- `--share` starts a `cloudflared` quick tunnel and prints the public URL.
- `--token` mints a random secret and serves the endpoint under
  `/t/<token>/`; the same token is also accepted as
  `Authorization: Bearer <token>`. This is a secret path, not real
  authentication — put a reverse proxy in front for anything more.
- `--ttl DURATION` (e.g. `2h`, `24h`, `7d`) sets when the service shuts
  itself down; `--share` alone defaults to 24h so a public endpoint always
  has an expiry.

## Uploads

With `--allow-upload`, two upload paths are enabled:

- Drag-and-drop a file onto the landing page (`POST /upload`); it is
  registered as a new table under the `uploads` schema, visible to every
  client.
- TAP UPLOAD: a client sends a VOTable alongside a query
  (`UPLOAD=name,param:x`) and joins against it as `tap_upload.name` for the
  duration of that one sync query.

Both are capped by `TAPDROP_UPLOAD_MAX_MB` (default 200); there is no CLI
flag for it.

## Query log

`--log-dir PATH` records every query — text, format, row count, elapsed
time, status, and a hash of the token if one was used (never the token
itself) — and makes it queryable through the service itself as
`tapdrop.query_log`. Rows are also flushed to `PATH/query_log.parquet`.

## Configuration reference

Every CLI flag has a matching `TAPDROP_<FLAG>` environment variable.

| Flag | Env var | Default | Notes |
|---|---|---|---|
| `SOURCES` (argument) | `TAPDROP_SOURCES` | — | Comma-separated in the env var |
| `--images` | `TAPDROP_IMAGES` | — | FITS images to publish as ObsCore/SIA/DataLink/SODA |
| `--host` | `TAPDROP_HOST` | `127.0.0.1` | `0.0.0.0` in the Docker image |
| `--port` | `TAPDROP_PORT` | `8000` | |
| `--ttl` | `TAPDROP_TTL` | none | `24h` if `--share` and unset |
| `--share` | `TAPDROP_SHARE` | `false` | |
| `--token` | `TAPDROP_TOKEN` | none | CLI flag generates one; env var sets it explicitly |
| `--allow-upload` | `TAPDROP_ALLOW_UPLOAD` | `false` | |
| `--config` | `TAPDROP_CONFIG_FILE` | none | `tapdrop.yaml` overrides |
| `--result-store` | `TAPDROP_RESULT_STORE` | `~/.cache/tapdrop/results` | `s3://` prefix is documented but not yet implemented; PNG previews are cached in `previews/` inside it |
| `--max-rows` | `TAPDROP_MAX_ROWS` | `100000` | Default `MAXREC` |
| `--hard-max-rows` | `TAPDROP_HARD_MAX_ROWS` | `10000000` | Upper bound on `MAXREC` |
| `--query-timeout` | `TAPDROP_QUERY_TIMEOUT` | `300` | Sync query timeout, seconds |
| `--memory-limit` | `TAPDROP_MEMORY_LIMIT` | `75%` | DuckDB `memory_limit` |
| `--cache` | `TAPDROP_CACHE` | `false` | Copy remote columns to local DuckDB on first use |
| `--log-dir` | `TAPDROP_LOG_DIR` | none | Enables the query log |
| — | `TAPDROP_UPLOAD_MAX_MB` | `200` | No CLI flag; env var only |
| — | `TAPDROP_MAX_JOBS` | `4` | No CLI flag; env var only |
| — | `TAPDROP_PUBLIC_URL` | derived | No CLI flag; needed behind proxies |
| `AWS_*` | — | — | Standard AWS credential chain, for `s3://` sources |

## Security

- Binds to `127.0.0.1` by default; `--share` and Docker are the only paths
  to public exposure.
- A query is a single `SELECT`. The translator's AST allowlist blocks
  `COPY`, `ATTACH`, `INSTALL`, `read_*` table functions, and file paths
  inside queries.
- The `--token` secret is never logged; the query log stores a hash of it.

## More

See `DEVELOPER.md` for running tests, adding an ADQL rule, and deploying to
Docker, Cloud Run, or Hugging Face Spaces.
