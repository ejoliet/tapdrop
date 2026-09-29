# tapdrop

Temporary, standards-compliant IVOA TAP service over files in place (DuckDB + FastAPI, no database server).

## The spec is `RDD.md`

`RDD.md` is the README-driven-development spec and the single source of truth: CLI contract, HTTP interface, ADQL support matrix, discovery rules, config reference, error table, milestones (M0–M11), and acceptance checks. Read the relevant section before implementing. `README.md` is a stub that M6 replaces with user docs.

When code and `RDD.md` disagree, `RDD.md` wins unless the user says otherwise. Open Questions in `RDD.md` are unresolved — ask rather than guess, and do not silently pick a default for one that blocks the current milestone.

## Commands

Python work goes through `uv`, never bare `pip`/`python`/`.venv/bin/*`.

```bash
uv run pytest tests -m "not live"   # unit
uv run pytest tests -m live         # uvicorn + pyvo end to end
uv run pytest --cov=tapdrop         # target >= 80%
uv run ruff check
uv run mypy src                     # --strict, must be clean
uv run tapdrop --version
```

## Constraints

- Python 3.11+, typed signatures, `ruff` + `mypy --strict` clean.
- Tests never touch real networks: `moto` for S3, local `http.server` fixtures, committed fixtures under 5 MB total.
- No blocking I/O in async handlers — DuckDB and astropy work runs in a thread pool.
- Heavy imports (`roman_datamodels`, `gwcs`, `asdf`) stay inside v1.1 modules and import lazily, with a clear error when the `[roman]` extra is missing.
- Mark non-obvious decisions with `AIDEV-NOTE:` / `AIDEV-TODO:` comments (translator rewrites, HEALPix pruning, WCS edge cases).
- Update `CHANGELOG.md` and `DEVELOPER.md` in the same change as behavior changes.
- Every CLI flag has a `TAPDROP_<FLAG>` env var; add both or neither.

## Not the agent's job

Do not run browsers, TOPCAT, STILTS/`taplint`, cloudflared tunnels, Docker pushes, `git push`, or PR creation. Those are Emmanuel's manual checks. List the exact commands for him in the milestone summary instead.

## Security invariants

These are load-bearing, not cleanups — never relax them for convenience:

- A query is a single `SELECT`. The translator rejects everything else.
- AST allowlist blocks `COPY`, `ATTACH`, `INSTALL`, `read_*` table functions, and file paths inside queries.
- DuckDB holds `SELECT` only on registered tables.
- Binds to `127.0.0.1` by default; `--share` and Docker are the only public paths.
- Tokens are never logged — the query log stores a hash.

## Agent routing

Project agents live in `.claude/agents/`. They own the domain-specific work; OMC's generic agents (`executor`, `code-reviewer`, `test-engineer`, `debugger`, `verifier`, `writer`) still handle everything else.

| Work | Agent |
|---|---|
| ADQL dialect, geometry UDFs, ADQL→DuckDB translation, statement allowlist | `adql-translator` |
| Format readers, RA/Dec + UCD detection, HATS, WCS footprints, output writers | `astro-io` |
| TAP/UWS/VOSI/TAPRegExt/SCS/ObsCore/SIA/DataLink/SODA wire-format review | `ivoa-conformance` (read-only) |

Authoring and review stay separate passes: an implementing agent never approves its own output. Route standards review to `ivoa-conformance`, general code review to `code-reviewer`.

## Docs

Use Context7 for `pyvo`, `astropy`, `duckdb`, `sqlglot`, `fastapi`, `typer`, `pydantic-settings`, `mocpy`, `cdshealpix` APIs before writing against them. For IVOA standards themselves, cite the spec section (e.g. "TAP 1.1 §2.7") rather than recalling from memory; the vendored XSDs in `tests/conformance/` are the checkable artifact.

## Command output

Command output in this session is condensed by `rtk`. Treat it as complete. Batch related commands into one call. Re-run as `rtk proxy <cmd>` only when a result is unusable (empty when output was expected, contradicting its exit code, or garbled).
