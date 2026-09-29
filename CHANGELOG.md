# Changelog

All notable changes to tapdrop are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- M0: project scaffold. hatchling `src/` layout, `uv`-based workflow, typer CLI
  with the full flag surface from the CLI contract, `Settings` with
  `TAPDROP_*` environment variables, the error taxonomy, CI running ruff, mypy
  `--strict`, and pytest on Python 3.11 and 3.12.
- M1: discovery and registry. `sources.py` resolves folders, globs, `s3://`
  and `https://` URIs via fsspec into per-schema table groups. `discovery/
  catalog.py` reads all six v1 formats (Parquet, CSV, TSV, ECSV, VOTable,
  FITS BINTABLE incl. multi-HDU `{table}_hdu{N}` splitting) into a common
  `ColumnMeta`/Arrow representation, with a 3-tier RA/Dec detection algorithm
  (UCD -> name -> unit-range sanity) that records rule + confidence and never
  promotes a guess to fact, plus `tapdrop.yaml` column/table overrides.
  `discovery/hats.py` detects HATS directories and records partition order.
  `registry.py` provides `ColumnMeta`/`TableMeta`/`Registry`, attaches one
  DuckDB schema per TAP schema with one view per table (no file path ever
  appears in a view definition), and populates `TAP_SCHEMA`. `cli.py`'s
  `inspect` command prints a stable, sorted, golden-testable summary
  including skipped/unreadable files, so one bad file never aborts discovery.
- M2: ADQL translator. `adql/dialect.py` (a sqlglot `ADQL` dialect over
  DuckDB: `TOP n`, and `POINT`/`CIRCLE`/`DISTANCE`/`CONTAINS`/`COORDSYS`/
  `COORD1`/`COORD2` as first-class geometry nodes; `BOX`/`POLYGON`/
  `INTERSECTS`/`REGION` parse and are rejected as unsupported), `adql/
  translate.py` (the security gate: single-`SELECT`-only, AST-allowlisted
  table/column/function validation against `Registry`, ICRS-only frame
  enforcement, `AdqlSyntaxError`/`UnsupportedAdqlError`/`UnknownTableError`/
  `UnknownColumnError` with `difflib` close-match suggestions, and a
  `ConeHint` extractor for M4's HEALPix pruning), and `adql/udfs.py`
  (haversine and cone-search DuckDB SQL macros, pole-safe RA/Dec-band
  prefilter). `engine.py`: DuckDB connection setup (`memory_limit`, `httpfs`,
  geometry macro registration, `Registry.attach`/`populate_tap_schema`) and a
  watchdog-thread query timeout raising `QueryTimeoutError`.
  `output.py` serialises results as VOTable BINARY2 (default) and
  TABLEDATA, CSV, TSV, Parquet and JSON, carrying unit, UCD and
  description through from discovery. `api/params.py` parses the TAP
  parameter set and `api/tap.py` serves `GET`/`POST /tap/sync` with MAXREC
  clamping, the `OVERFLOW` flag and TAP `QUERY_STATUS=ERROR` VOTables.
  `tapdrop serve` starts the service, optionally behind a `/t/<token>/`
  secret path prefix.
- M3 (UWS half): in-process async job execution. `uws.py`'s `JobManager` runs
  UWS 1.1 jobs on a bounded thread pool (`max_jobs`), enforcing the legal
  phase transitions (`PENDING`/`QUEUED`/`EXECUTING`/`COMPLETED`/`ERROR`/
  `ABORTED`/`HELD`/`SUSPENDED`/`UNKNOWN`) and writing results to
  `result_store`; jobs submitted past `max_jobs` stay `QUEUED` instead of
  being rejected. `api/uws.py` serves `/tap/async` and its sub-resources
  (`phase`, `quote`, `executionduration`, `destruction`, `error`,
  `parameters`, `results`, `results/result`) as UWS 1.1 XML, with job
  creation returning `303 See Other` to the new job's URL. Aborting an
  in-flight job interrupts the shared DuckDB connection; a failed job carries
  a `<uws:errorSummary>` and a VOTable error document at `.../error`. S3
  result storage is not yet implemented.
- M3 (VOSI half): `/tap/capabilities` (TAPRegExt, declaring only the geometry
  functions and output formats actually implemented, plus the configured row
  and time limits), `/tap/availability` (reporting `downAt` from `--ttl`),
  `/tap/tables` (VODataService table set) and `/tap/examples` (DALI examples
  in RDFa, generated from the discovered tables). Capabilities and tables are
  validated in the test suite against vendored IVOA schemas under
  `tests/conformance/xsd/`.
- TAP_SCHEMA `columns` now also publishes `xtype`, `size` and `column_index`.
- M4 (SCS half): `api/scs.py` serves `GET /scs/{schema}.{table}` for every
  discovered table that has RA/Dec, per SCS 1.03. `RA`, `DEC`, `SR` are
  required and case-insensitive like the TAP parameters, are parsed to floats
  before they ever reach a query string, and are range-checked (`RA` in
  `[0, 360]`, `DEC` in `[-90, 90]`, `SR` in `[0, 180]`, `SR=0` being a valid
  point search per the spec). `VERB` (1/2/3, default 2) selects the RA/Dec
  plus discovered primary key for `VERB=1`, that set plus any TAP_SCHEMA
  "principal" columns for `VERB=2`, and every column for `VERB=3`. The
  request builds an ordinary ADQL `CONTAINS(POINT, CIRCLE)` cone query and
  runs it through the same `adql.translate` / `engine.run_with_timeout` path
  and `run_in_threadpool` as `/tap/sync`, so the translator's AST allowlist
  is the only thing standing between a request and DuckDB here too. Results
  are always serialised as VOTable TABLEDATA rather than the default
  BINARY2, since SCS 1.03 predates BINARY2 and its clients (and `taplint`)
  expect the older serialisation. An unknown table and a table with no
  detected RA/Dec both surface as the existing `UnknownTableError`, since
  neither is part of the resource space this endpoint serves.
- M4 (HATS pruning half): a cone search over a HATS catalog now opens only
  the shard files its cone can intersect, instead of scanning every shard.
  `discovery/hats.py` records each shard's `(order, pixel)` HEALPix address
  and exposes `prune_files(meta, ra, dec, radius)`, using
  `cdshealpix.cone_search` per distinct partition order present (correct for
  a catalog with adaptive depth, RA=0 wraparound and pole-adjacent cones
  handled natively by the library). `adql/translate.py` applies it per query:
  when a query's FROM is a single HATS table with exactly one literal cone
  naming that table's RA/Dec columns, the table's AST node is replaced with
  a subquery over the pruned `read_parquet([...])` file list; anything else
  (a join, an ambiguous or non-literal cone, no cone at all) is left
  unpruned. A radius at or above 90 degrees always falls back to every file,
  since `cdshealpix`'s cone search is unreliable near and above a hemisphere.
- M5: an embedded landing page at `/` (single self-contained HTML file, no
  build step, no external assets) showing the discovered tables, ready-made
  TOPCAT/pyvo/curl snippets and an ADQL box, plus `POST /upload` drag-drop
  ingest under `--allow-upload`: a dropped file is size-capped while
  streaming, discovered, and registered as a table in the `uploads` schema
  for the lifetime of the service. With `--token` set, a request that does
  not carry the `/t/<token>/` prefix is answered `401` unless it presents
  `Authorization: Bearer <token>`, which is how a script avoids putting the
  secret in a URL; `/healthz` stays open for liveness probes. Once `--ttl`
  has passed, every request is refused with `503` until the process exits.
- M4 (TAP UPLOAD half): `UPLOAD=name,param:part` on `POST /tap/sync` makes a
  client's VOTable queryable as `tap_upload.<name>` for exactly that one
  query. The table is registered into a per-request registry and its view is
  dropped when the query finishes, so an uploaded table never becomes part of
  the service's own metadata. Inline uploads only: a URI upload is refused
  with a message naming what is supported, and capabilities advertise
  `upload-inline` alone. Requires `--allow-upload` and obeys
  `TAPDROP_UPLOAD_MAX_MB`.
- M6: the query log. `--log-dir` records every sync request and async job -
  timestamp, endpoint, query, format, effective `MAXREC`, rows returned,
  elapsed time, status and error - into a DuckDB table that is itself
  queryable through the service as `tapdrop.query_log`, and flushes it to
  Parquet in the log directory. The token is never written: the log stores a
  truncated SHA-256 of it. Logging failures are logged and swallowed rather
  than failing the query they describe. `tapdrop serve` now emits its own
  logs as one JSON object per line.
- M7: `tapdrop scan` and image discovery. `discovery/image_fits.py` reads only
  a FITS image's header and WCS (never pixel data - the fsspec-opened file
  object is handed straight to `astropy.io.fits.open` with
  `lazy_load_hdus=True`), and computes an edge-sampled footprint (4 corners
  plus 4 samples per edge, interpolated in pixel space and projected through
  the WCS one point at a time, not interpolated in RA/Dec afterwards) so
  `s_region`/`s_ra`/`s_dec`/`s_fov` stay correct for a footprint crossing
  RA=0 and one adjacent to a pole. `discovery/profiles/` loads profiles as
  data (YAML, not code); `generic-fits-wcs` is the only one in v1.1
  (`roman-wfi` is M8). `discovery/scan.py` walks a directory/file/glob of
  FITS files, extracts the CAOM-lite field set from RDD.md's discovery rules
  (`t_min`/`t_max`/`t_exptime`, `em_min`/`em_max`/`em_filter`, `calib_level`,
  `dataproduct_type`, `obs_collection`, `obs_id`, pixel axes, `s_resolution`),
  and writes `tapdrop.discovered.yaml` with every inferred field's source and
  confidence (`high`/`medium`/`low`) and an `unresolved` list for anything it
  could not determine; the writer/reader round-trip losslessly. One unreadable
  image is skipped with a reason, matching M1's discovery error handling -
  scanning a directory never aborts on the first bad file.

- M9: CAOM-lite, ObsCore and SIA. `caom_lite.py` turns a scan into the
  `caom.observation`/`caom.plane`/`caom.artifact`/`caom.chunk` tables and the
  `ivoa.obscore` view over them, which publishes all 30 ObsCore 1.1 mandatory
  columns in the standard's order (a column tapdrop cannot know from a header
  is an explicit typed `NULL`, never a missing column). `serve --images`
  scans the images, builds the tables and registers them in `TAP_SCHEMA`, so
  `ivoa.obscore` is queryable through ordinary TAP. `/tap/capabilities` now
  declares `<dataModel>ObsCore-1.1</dataModel>` when images are served.
  `api/sia.py` serves `GET /sia/query` (SIA v2) with `POS` (`CIRCLE`,
  `RANGE`, `POLYGON`; a `RANGE` crossing RA=0 is split into two polygons),
  `BAND`, `TIME`, `POL`, `COLLECTION` and `MAXREC`: every interval uses DALI
  1.1 open-interval semantics with `±Inf`, repeated values of one parameter
  are ORed, different parameters are ANDed, and the only non-numeric client
  text (`COLLECTION`, `POL`) is character-restricted rather than escaped.
- M9 (geometry half): ADQL `POLYGON('ICRS', ra1, dec1, ...)`,
  `CONTAINS(POINT, POLYGON | s_region)` and `INTERSECTS` between
  CIRCLE/POLYGON/`s_region` (every pairing except CIRCLE/CIRCLE), evaluated
  by a great-circle crossing-number test verified against `mocpy`/`astropy`.
  ObsCore `s_region` DALI/STC-S `POLYGON ICRS ...` strings are parsed from a
  column or a literal; a malformed value excludes that row rather than
  failing the query. `BOX` and `REGION` stay rejected.
- M10: DataLink 1.1 and previews. `/datalink/links` returns three rows per
  plane - `#this` (the file), `#preview` (a generated PNG) and `#cutout` (a
  SODA service descriptor pointing at `/soda/sync`) - and an unknown ID is
  reported as a row carrying `NotFoundFault`, not an HTTP error, per the
  standard. `/datalink/file` streams the artifact a megabyte at a time (an
  artifact that is already an HTTP URL is linked to directly instead of
  proxied) and `/datalink/preview` serves an asinh-stretched grayscale PNG,
  generated on first request and cached on disk beside the result store.
  Neither route takes a path from the client: the only parameter is a plane
  URI looked up in `caom.artifact`. ObsCore `access_url` points at
  `/datalink/links`, so a TAP or SIA result leads a client to the data.
- M11 (cutout half): `GET /soda/sync` (SODA 1.0 sync). `ID` plus one of
  `CIRCLE`, `POLYGON`, or a DALI `POS` (`CIRCLE`/`RANGE`/`POLYGON`); no shape
  returns the whole image. Cuts with `astropy.nddata.Cutout2D` against the
  `caom.chunk` WCS and returns FITS carrying the shifted WCS, so a client's
  coordinates still resolve in the cutout. Remote FITS is read with fsspec
  range requests (`hdu.section`), never downloaded whole. ASDF cutouts work
  for local files with the `[roman]` extra; remote ASDF returns `501`. Errors
  are SODA-style `text/plain` (`UsageFault:` / `DefaultFault:`).
- M11 (export half): `tapdrop export --caom2-xml OUT --images DIR` writes one
  validated CAOM2 XML document per observation, using the optional `caom2`
  package (`[export]` extra). The export runs the same
  `caom_lite.build_caom` the service does, into an in-memory DuckDB, so an
  exported document and a live `ivoa.obscore` row always agree on what a
  dataset is called.

- `TAP_SCHEMA.schemas`, `.tables`, `.columns`, `.keys` and `.key_columns` are
  registered tables: queryable through `/tap/sync` and `/tap/async`
  case-insensitively (`TAP_SCHEMA.tables` or `tap_schema.tables`), listed in
  `/tap/tables`, and described in `TAP_SCHEMA` itself (TAP 1.1 §4). They are
  read-only like everything else. `schema_index` and `table_index` columns were
  added (NULL) to match the standard's column set.
- `ColumnMeta.utype` and `ColumnMeta.xtype`, written to `TAP_SCHEMA.columns`,
  emitted in `/tap/tables` as `<utype>` and `dataType/@extendedType`, and
  carried onto VOTable `FIELD`s. Every `ivoa.obscore` column carries its
  ObsCore 1.1 utype; `s_region` carries `xtype="adql:REGION"`.
- `/tap/capabilities` declares `POLYGON` and `INTERSECTS` as ADQL geometry
  features.
- `/scs/{schema}.{table}`, `/sia/query`, `/soda/sync` and the three DataLink
  routes accept parameters by `POST` as well as `GET` (DALI 1.1 §2.2).
- `/sia/query` honours `RESPONSEFORMAT` (csv, tsv, parquet, json and the
  VOTable variants) and rejects an unknown value with a `UsageFault`
  (DALI 1.1 §3.3). The default is still TABLEDATA VOTable.

### Notes

- License resolved as MIT, per the default named in `RDD.md`.
- Live interoperability suite (`tests/test_live.py`, marked `live`): a real
  uvicorn server driven by `pyvo` for capabilities, table metadata, a sync
  cone search, `MAXREC`, a query error, and an async job through to its
  result and deletion. CI now runs it on every push.
- `Dockerfile` (two-stage, `python:3.12-slim`, non-root, `TAPDROP_HOST=0.0.0.0`)
  and a CI job that builds the image and runs `tapdrop --version` in it.
  Publishing to ghcr.io stays a manual step.
- `--share` starts a cloudflared quick tunnel, prints the public URL as a
  terminal QR code, and publishes that URL in the VOSI documents. `--ttl` now
  stops the server when the expiry passes, draining in-flight queries first.

### Fixed

- UWS timestamps are written as DALI 1.1 §3.3.3 UTC instants (`...Z`, whole
  seconds) instead of `datetime.isoformat()`, which `pyvo` refuses to parse.
- The `--token` secret no longer reaches uvicorn's access log. `configure_logging`
  only owned the `tapdrop` logger, so uvicorn kept printing the request line —
  `GET /t/<token>/tap/sync ... HTTP/1.1` — to stdout. A filter on uvicorn's own
  loggers rewrites the prefix to `/t/<redacted>`.
- `tapdrop serve --images DIR` works without a catalog source, as RDD.md's own
  example shows; the `SOURCES` argument was required, and the "nothing to
  serve" guard fired before images were attached. An ObsCore `access_url` built
  while bound to `0.0.0.0` now names `127.0.0.1`, which is an address a client
  can actually reach.
- `/datalink/links` no longer hands every row the first request's `ID`: the
  shared cutout descriptor points at the `ID` column with `ref` (DataLink 1.1
  §4.3), so a multi-ID response cuts the right dataset. A request with no `ID`
  returns an empty table rather than an error (§2.1.1), IDs are capped per
  request, and one carrying a control character is rejected instead of being
  written into XML.
- `/datalink/file` opens the artifact before it starts the response, so an
  unreadable file is a `DefaultFault` rather than a `200` with an empty body,
  and sends `Content-Length`. The three DataLink routes accept `POST` as well
  as `GET` (DALI 1.1 §2.2), report `application/x-asdf` for ASDF artifacts,
  look their IDs up in one query off the event loop, and no longer render a
  PNG on the event loop.
- Previews read a strided slice through `hdu.section` instead of pulling the
  whole HDU into memory to throw most of it away, and the preview cache lives
  inside the result store with mode `0700` rather than beside it in a
  predictable, possibly world-writable, parent.
- `/soda/sync` computes the pixel box before reading any pixel and refuses a
  region above `MAX_CUTOUT_PIXELS` (4096 × 4096) with a fault naming the
  requested size and the limit; a whole-image request above the ceiling is
  pointed at `/datalink/file`, which streams. The ASDF path no longer loads the
  full array before cutting, and a server-side failure (unreadable file, S3
  error, astropy error) returns a SODA `text/plain` fault instead of a VOTable.
- `/sia/query` rejects infinite `POS` coordinates instead of leaking a bare
  `inf` token into the generated ADQL, and `POL` states must be letters, so `_`
  can no longer act as a `LIKE` wildcard.
- `SELECT * FROM ivoa.obscore` lost `ucd` and `unit` on `s_ra`, `s_dec`,
  `s_fov`, `t_min` and a dozen more, because the bare `caom.plane` columns of
  the same name were registered first; a column carrying unit or UCD now wins
  the shared-name lookup.
- `ivoa.obscore.s_xel1` and `s_xel2` are delivered as `long`, matching their
  TAP_SCHEMA declaration and ObsCore 1.1.
- The ADQL capability description no longer claims `POLYGON` and `INTERSECTS`
  are unimplemented; they have been executing since M9.
- `/soda/sync` error bodies use the SODA 1.0 §5.2 vocabulary (`UsageError`,
  `MultiValuedParamNotSupported`, `ServiceUnavailable`, `Error`) instead of
  DataLink's `UsageFault`/`DefaultFault`, and SIA error text carries the
  `UsageFault:`/`DefaultFault:` prefix SIA 2.0 §4.2 requires.
- `POS=RANGE` accepts `-Inf`/`+Inf` open bounds in both `/sia/query` and
  `/soda/sync` (SIA 2.0 §2.1.1, SODA 1.0 §3.3) instead of generating ADQL
  containing a bare `inf`. A SODA range whose corner falls behind the image's
  tangent plane is clipped to the image footprint before its pixel bounding box
  is taken.
- `tapdrop export --caom2-xml` no longer silently overwrites one document with
  another when two observation ids sanitise to the same filename (`a/b` and
  `a_b`); the second gets a `-2` discriminator.
