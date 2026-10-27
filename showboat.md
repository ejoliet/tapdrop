# M6.1: async UPLOAD

*2026-10-27T21:32:26Z by Showboat 0.6.1*
<!-- showboat-id: 04d185e3-afa5-4e0c-b2f3-84047c45d592 -->

Red step: this test posts UPLOAD to /tap/async. It failed with Unknown table 'tap_upload.mine' before the fix; it passes now.

```bash
uv run pytest tests/test_tap_upload.py -q -p no:cacheprovider 2>&1 | grep -E 'passed|failed' | sed -E 's/ in [0-9.]+s.*//'
```

```output
9 passed, 1 warning
```

Flaky log test now waits for the job to finish. Run 3 times.

```bash
for i in 1 2 3; do uv run pytest tests/test_querylog.py -q -p no:cacheprovider 2>&1 | grep -E 'passed|failed' | sed -E 's/ in [0-9.]+s.*//'; done
```

```output
11 passed, 1 warning
11 passed, 1 warning
11 passed, 1 warning
```

Live: pyvo submit_job with uploads= through a real uvicorn server.

```bash
uv run pytest tests -m live -q -p no:cacheprovider -k 'async_job or async' 2>&1 | grep -E 'passed|failed' | sed -E 's/ in [0-9.]+s.*//'
```

```output
2 passed, 1 skipped, 603 deselected, 1 warning
```

Gates: ruff and mypy --strict.

```bash
uv run ruff check 2>&1 | tail -1; uv run mypy src 2>&1 | tail -1
```

```output
All checks passed!
Success: no issues found in 35 source files
```
