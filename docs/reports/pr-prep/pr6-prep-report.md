# PR #6 Remediation Prep Report

Generated offline from branch `codex/live-integration` at commit `7984f7d5dd4abe2173e8f948df69334cfc75f351` against base `codex/new-main`. No GitHub PR state was touched.

## Scope of Changes

PR #6 introduces live trading integration work across adapters, daemons, database, signal sources, scripts, and tests. Key modules with dense churn: `live_runner/*.py`, `shared/database.py`, `live_runner/signal_sources/discord_source.py`, plus new tests under `tests/`.

## Test Results

Ran targeted live/integration tests covering the PR surfaces:

- `tests/test_live_execution_safety.py`
- `tests/test_database_migrations.py`
- `tests/test_account_routing_safety.py`
- `tests/test_live_daemon.py`
- `tests/test_discord_signal_parsing.py`
- `tests/test_ctrader_adapter.py`
- `tests/test_live_smoke.py`
- `tests/test_live_preflight.py`

Result: `165 passed, 12 warnings in 12.68s`

Assessment: The PR’s changed behavior appears well-covered by existing tests. No failures were observed.

## Quality-Gate Evidence

### Lint / Static Analysis (ruff)

Used `ruff` locally as a code-quality gate proxy. No Sonar scanner was available on this host, so Sonar Cloud/Server metrics were **not gathered**.

Command:
```
cd /home/ubuntu/worktrees/live-integration
.venv/bin/python -m ruff check live_runner shared tests --select=E,F,I,W --statistics
```

Findings summary:
- Total: **356** errors/warnings
  - `E501` line-too-long: **185**
  - `F401` unused-import: **80**
  - `I001` unsorted-imports: **66**
  - `F841` unused-variable: **7**
  - `W292` missing-newline-at-end-of-file: **7**
  - invalid-syntax: **4**
  - `F811` redefined-while-unused: **3**
  - `F821` undefined-name: **3**
  - `W291` trailing-whitespace: **1`

Fixability:
- **157 issues are auto-fixable** via `ruff --fix` (with 7 more hidden `--unsafe-fixes` possible).
- Remaining **199 issues require manual attention**, especially `E501`, `invalid-syntax`, and any `undefined-name` occurrences.

Conclusion: ruff gate is **FAILING** for a zero-tolerance policy.

### Deprecation Warnings

During tests, repeated warnings from `shared/database.py:563`:
```
datetime.datetime.utcnow() is deprecated and scheduled for removal
```
Risk: Future Python versions can break runtime behavior. Should migrate to `datetime.datetime.now(datetime.UTC)`.

### Type Checking

`mypy` is not installed in this environment, so no type-check gate could be executed. If type-check is required, install it and run against changed modules.

### Sonar

Sonar scanner / sonar-project.properties were **not available** in this environment. If Sonar is part of the official quality gate, note `Blocker`/`Critical` rule gaps were **not evaluated here**.

## Likely Fixes

1. **Auto-fix lint debt**
   - Run: `cd /home/ubuntu/worktrees/live-integration && .venv/bin/python -m ruff check live_runner shared tests --select=E,F,I,W --fix`
   - This resolves ~157 fixable findings (unused imports, sort errors, small whitespace/newline issues).

2. **Manually resolve remaining lint issues**
   - Review `invalid-syntax` and `undefined-name` occurrences — these are potential blockers beyond style.
   - Trim long lines (E501) in hot files: `live_runner/ctrader_adapter.py`, `live_runner/daemon.py`, `live_runner/alpaca_adapter.py`, `shared/database.py`.

3. **Fix datetime deprecation**
   - Replace `datetime.utcnow()` at `shared/database.py:563` with `datetime.now(datetime.UTC)`.

4. **Optional: prune manufactured output from PR**
   - Consider removing per-run `reports/*.json` smoke/test artifacts from the branch. They bloat diff history but do not appear to affect tests.

5. **Type-check pass**
   - Install `mypy` (optional dev dep).
   - Run: `mypy shared tests live_runner/account_*.py live_runner/config_parser.py live_runner/preflight.py live_runner/daemon.py live_runner/alpaca_adapter.py live_runner/ctrader_adapter.py live_runner/signal_sources/discord_source.py`

## Action Checklist for Remediation

- [ ] `ruff --fix` in changed files
- [ ] Resolve remaining ruff findings manually
- [ ] Address `invalid-syntax` and `undefined-name` issues
- [ ] Replace `datetime.utcnow()` with timezone-aware alternative
- [ ] Run targeted tests again and expand inclusion if needed
- [ ] Re-run lint gate from clean state
- [ ] Decide on Sonar / mypy requirement and execute

## Reporter Notes

- No patches were applied to the worktree and no PR branch state was changed.
- Only local evidence was collected under `/home/ubuntu/review-artifacts/pr6-prep/`.
- Artifacts directory exists for any extended logs if needed.
