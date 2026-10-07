# Contributing to tapdrop

Bug reports, client compatibility reports (TOPCAT, pyvo, Firefly, Aladin…),
and pull requests are welcome.

## Report a problem

Open an issue with the command you ran, the client and its version, the ADQL
query, and the error. `stilts taplint` output helps for anything
standards-related.

## Send a change

```bash
git clone https://github.com/ejoliet/tapdrop && cd tapdrop
uv sync --extra dev
uv run pytest tests -m "not live"   # plus: uv run pytest tests -m live
uv run ruff check && uv run ruff format --check && uv run mypy src
```

All of those must pass. Before opening a PR, check that your change:

- **Follows the spec.** [`RDD.md`](RDD.md) is the source of truth. Where code
  and spec disagree, raise it in the issue.
- **Comes with a test.** It should fail without your change. Tests never touch
  the network: use `moto` for S3 and local HTTP fixtures.
- **Updates `CHANGELOG.md`** (and `DEVELOPER.md` if the workflow changed).
- **Pairs flags with env vars.** A new CLI flag needs a matching
  `TAPDROP_<FLAG>` env var.
- **Keeps the security rules.** A query stays a single `SELECT`; the
  allowlist, the `127.0.0.1` default bind, and token redaction don't get
  relaxed.

See [`DEVELOPER.md`](DEVELOPER.md) for the code layout and test rules.

By contributing you agree your work is released under the [MIT License](LICENSE).
