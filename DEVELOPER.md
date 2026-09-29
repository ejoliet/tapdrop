# Developing tapdrop

`RDD.md` is the spec. This file is how you run the thing.

## Setup

```bash
uv python pin 3.12          # 3.11+ is supported; 3.12 is what CI and the image use
uv sync --extra dev         # add --extra export for CAOM2 export, --extra roman for the ASDF path
```

No `pip`, no `python -m venv`, no `.venv/bin/<tool>`. Everything goes through `uv run`.

## The loop

```bash
uv run pytest tests -m "not live"     # unit suite, no servers, no network
uv run pytest tests -m live           # starts uvicorn, drives it with pyvo and httpx
uv run pytest --cov=tapdrop           # target >= 80%
uv run ruff check                     # lint
uv run ruff format                    # format
uv run mypy src                       # --strict, must be clean
```

Run the service against the committed fixtures:

```bash
uv run tapdrop serve tests/data
uv run tapdrop inspect tests/data
uv run tapdrop serve tests/data/gaia.parquet --images tests/data/images   # ObsCore, SIA, DataLink, SODA
```

## Layout

| Path | Holds |
|---|---|
| `src/tapdrop/cli.py` | typer commands: `serve`, `inspect`, `scan`, `export` |
| `src/tapdrop/config.py` | `Settings`; every flag has a `TAPDROP_<FLAG>` env var |
| `src/tapdrop/errors.py` | the error taxonomy; one class per row of the RDD error table |
| `src/tapdrop/sources.py` | folder / glob / `s3://` / `https://` / upload → file lists |
| `src/tapdrop/discovery/` | per-format readers, HATS, RA/Dec + UCD detection, image profiles |
| `src/tapdrop/registry.py` | table and column model, `TAP_SCHEMA` population |
| `src/tapdrop/caom_lite.py` | `caom.*` tables and the `ivoa.obscore` view built from an image scan |
| `src/tapdrop/caom2_export.py` | `tapdrop export --caom2-xml`; needs the `[export]` extra |
| `src/tapdrop/preview.py` | asinh-stretched PNG thumbnails, cached on disk |
| `src/tapdrop/adql/` | sqlglot dialect, ADQL → DuckDB translation, geometry UDFs |
| `src/tapdrop/engine.py` | DuckDB connections, limits, timeouts |
| `src/tapdrop/uws.py` | async job model, phases, runner, result store |
| `src/tapdrop/output.py` | VOTable / CSV / TSV / Parquet / JSON writers |
| `src/tapdrop/api/` | FastAPI routers per standard |
| `tests/conformance/` | vendored IVOA XSDs and expected VOSI shapes |

## Adding a dialect rule

A "rule" is one ADQL construct that DuckDB does not accept verbatim.

1. Write the failing case first, in `tests/test_adql_valid.py` (or the
   `tests/test_adql_*.py` file matching the category: `invalid`, `unsupported`,
   `injection`, `geometry`, `engine`). Assert on the generated SQL for a
   syntax rewrite, or on rows for a semantic one.
2. If the construct is a function, decide between a **mapping** (ADQL name →
   existing DuckDB function, handled in `adql/dialect.py`) and a **UDF**
   (`adql/udfs.py`, registered on the connection in `engine.py`). Prefer the
   mapping: a UDF crosses the Python boundary per row.
3. Geometry rules must keep the optimizer honest. A declination-band prefilter
   or a HEALPix file prune may only *narrow what is scanned*, never change the
   result set. Every such rule needs a test comparing against brute-force
   `astropy.coordinates.SkyCoord.separation` on a fixture.
4. Anything the rule deliberately does not handle raises `UnsupportedAdqlError`
   naming the construct.
5. Declare it. `/tap/capabilities` advertises the geometry function list through
   TAPRegExt; a function that works but is not declared is invisible to TOPCAT,
   and one that is declared but missing is worse.

## Test rules

- Tests never touch a real network. `moto` for `s3://`, a local `http.server`
  fixture for `https://`.
- Fixtures live in `tests/data/` and stay under 5 MB in total. Generate them with
  a script rather than committing anything large.
- Mark anything that binds a port with `@pytest.mark.live`.

## What the agent does not run

Browsers, TOPCAT, STILTS (`taplint`), cloudflared tunnels, Docker pushes,
`git push`, PR creation. Those are manual checks; each milestone in `RDD.md`
lists the exact commands.

## Front-end assets

`src/tapdrop/static/index.html` is a single file with no build step. Any external
asset must be a pinned CDN URL, listed here:

| Asset | URL | Why |
|---|---|---|
| _(none yet)_ | | |

## Deployment

These are docs only (RDD.md M6: "deploy automation for Cloud Run, Fly.io, HF
Spaces" is explicitly a non-goal). Building and pushing images, and running
`gcloud`/cloud CLIs, are Emmanuel's manual steps.

### Docker

```bash
docker build -t tapdrop:local .
docker run --rm -p 8000:8000 -v "$PWD/tests/data:/data" tapdrop:local serve /data
```

The image (`Dockerfile`) already sets `TAPDROP_HOST=0.0.0.0` and
`TAPDROP_PORT=8000`, and runs as the non-root user `tapdrop` (uid 10001).
The entrypoint is `docker-entrypoint.sh`, which copies a host-provided `PORT`
into `TAPDROP_PORT` and then `exec`s the CLI, so the command line after the
image name is the CLI's own (`serve /data`, `inspect /data`, `--help`, ...).

### Google Cloud Run

Cloud Run injects the port to listen on as the `PORT` environment variable.
tapdrop itself reads only `TAPDROP_*` settings and never `PORT`; the image's
entrypoint bridges the two, so no port flag is needed at deploy time.

```bash
gcloud run deploy tapdrop \
  --image ghcr.io/ejoliet/tapdrop \
  --set-env-vars TAPDROP_SOURCES=s3://my-bucket/catalogs \
  --set-env-vars TAPDROP_TOKEN=<secret>,TAPDROP_RESULT_STORE=/tmp/results \
  --set-env-vars TAPDROP_MEMORY_LIMIT=1GB \
  --allow-unauthenticated
```

Running the CLI outside this image (a bare `uvx`/`pip` install) still needs
`--port` or `TAPDROP_PORT` set explicitly.

Other environment variables worth setting explicitly: `TAPDROP_SOURCES`
(the data to serve — a `s3://` prefix works well for a stateless container),
`TAPDROP_TOKEN` (skip `--token`'s random generation so the secret is stable
across revisions), and `TAPDROP_MEMORY_LIMIT` (match it to the Cloud Run
service's memory limit, since the default `75%` is relative to whatever the
container sees).

`TAPDROP_RESULT_STORE=s3://...` is documented as supported in RDD.md's
config reference, but `src/tapdrop/uws.py`'s `_write_result` currently
raises `NotImplementedError` for any non-local path — async job results
still require local disk. On Cloud Run's scale-to-zero, local disk and
in-memory UWS job state do not survive an instance being recycled between
requests, so async jobs (`/tap/async`) are not reliable there today; sync
queries (`/tap/sync`) are unaffected since they return the result directly
without touching the result store.

### Hugging Face Spaces (Docker SDK)

Spaces builds this repository's `Dockerfile` directly. Two changes a Space
needs beyond what's in the repo:

- Spaces expects the container to listen on port `7860` and passes it as
  `PORT`, which the image's entrypoint turns into `TAPDROP_PORT`. Setting
  `TAPDROP_PORT=7860` in the Space's environment is the explicit alternative.
- A `README.md` front-matter block Spaces reads for its listing (this is
  separate from and in addition to this project's own `README.md` content):

  ```yaml
  ---
  title: tapdrop
  sdk: docker
  app_port: 7860
  ---
  ```

Put the token and any AWS credentials in the Space's **Secrets** (not
public variables): `TAPDROP_TOKEN`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` as needed for `s3://` sources.

Spaces also scale to zero on inactivity by default, so the same limitation
as Cloud Run applies: in-memory UWS job state and any local result files
are lost when the instance restarts, and the S3 result store path needed to
avoid that is not implemented yet (see above).
