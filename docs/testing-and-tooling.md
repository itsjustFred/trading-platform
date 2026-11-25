# Testing and tooling

How the test suite, the gate runner, lint, type checking and the Sonar gate are set up. `AGENTS.md` keeps the short rules; this file holds the detail behind them.

## Gate runner and test isolation

`scripts/gate-tests.sh` runs the checks whose silent failure would make a green result a lie (the import-origin guard, the output-path guard, the test-suite isolation guard, the runner's own tests, F-lint on the scripts that mutate data). It fails a gate that has no command and exits 2 when the roster does not match what ran. `PYTHON=... scripts/gate-tests.sh` picks the interpreter; `--list` prints the roster. It is a fast subset, not a replacement for the test suite; CI runs both.

The suite keeps the checkout's `data/`, `logs/` and credential files out of reach: `tests/conftest.py` forces `DATA_DIR`/`TRADING_DATA_DIR`/`LOG_DIR` to a per-process temp dir, points every `DEFAULT_ACCOUNTS_ENV` copy at a sandbox path that deliberately does not exist, redirects the import-time `PROJECT_ROOT/.env` load to an empty session file, and refuses connect, name lookup (forward and reverse) and datagram sends (`sendto`/`sendmsg`) to non-local hosts - localhost and the wildcard addresses stay allowed. It then fails the session if a watched directory (`data/`, `logs/`) changed, if the real `.accounts.env` or `.env` was opened, if a module still resolves the real accounts env, or if a remote host was named - a test that needs credentials writes its own under `tmp_path`, and the integration suite needs `TP_INTEGRATION_BASE_URL` (it has no default port, so it never guesses a viewer). Tests must write under `tmp_path` or `DATA_DIR`, never a hardcoded `data/`. The directory guard watches only the time between session start and finish, and it cannot tell which process changed `data/`: stop a running viewer/daemon in the checkout, or run the suite from a separate checkout. The guards live in the pytest process itself: a Python subprocess a test starts inherits the exported `DATA_DIR`/`LOG_DIR` but not the audit hook, the `.env` redirect or the patched module constants, so a child that imports `shared.constants` loads the checkout's real `.env` - tests that spawn children must hand them a sandboxed environment explicitly. Because `DATA_DIR` is now a throwaway directory, local dataset-backed strategy tests need an explicit read-only opt-in: `TP_TEST_OHLCV_DIR=/path/to/ohlcv` (otherwise they skip with a message naming this variable).

## Lint and type checking

Ruff and mypy are configured in `pyproject.toml` (`[tool.ruff.lint]`, `[tool.mypy]`). Install the tooling with `uv sync --frozen --extra dev` (both tools are dev dependencies; `--frozen` keeps the local environment on the committed `uv.lock`, which is what CI checks out, so a run cannot quietly re-resolve a different mypy or ruff) and run, from the repo root:

```
uv run ruff check <changed files>
uv run mypy
```

`mypy` takes no arguments: the scope comes from `files` in `[tool.mypy]`. Run both before a PR. The ruff selection is E/F/I/W plus bug-class families only (bugbear, DTZ, PERF, selected PLE/PLW/RUF); each family was enabled only after every hit was fixed, and the comment block under it records the families deliberately left off. Mypy covers `risk`, `live_runner`, `signal_bots`, `strategies`, `shared/models.py` and `shared/market_calendar.py`; widen it by adding paths, never by loosening flags, and do not add `# type: ignore` without a specific error code and a reason. CI runs `ruff check .` (advisory, existing E501 backlog), a blocking `ruff check --select <the enabled bug-class families>` (the advisory step cannot catch a new hit there), and mypy (blocking, same `uv run mypy` command and the same pinned version from `uv.lock`).

Test order is deliberately fixed by `pytest.ini` (`addopts = -p no:randomly`), so local runs, `ci`, and agent workflows stay in declared order. `pytest-randomly` is installed with the dev extra and runs weekly in the `randomised-tests` workflow to catch order-dependent tests; its header prints `Using --randomly-seed=N` - reproduce a failure with `uv run pytest -p randomly --randomly-seed=N` (or dispatch the workflow with the `seed` input).

## Code quality (Sonar)

The Sonar gate applies to **new code on PRs only**. The existing S3776
(cognitive-complexity) backlog in old code is not chased item by item; it may
be reduced opportunistically — money path (`live_runner`, `risk`, engines)
first, and only where tests pin the behaviour. Splitting `viewer/main.py` to
retire most of that backlog is tracked in #283.
