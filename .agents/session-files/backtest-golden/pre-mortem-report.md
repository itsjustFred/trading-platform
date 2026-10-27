# Backtest Golden Pre-Mortem Report

**Scope:** `tests/test_backtest_golden.py`, `tests/fixtures/backtest/native_cli_v1.json`, and the exercised CLI/data/native-engine/statistics path
**Date:** 2026-09-25

## Summary

The golden is hermetic and intentionally strict, but four future failure modes could erode trust: automatic golden blessing, blanket tolerance expansion, dependency/platform float drift, and false confidence outside the native single-symbol path. The current semantic assertions, explicit generator command, sanitized child process, frozen lockfile, and mutation testing reduce these risks; they do not replace review policy or broader engine coverage.

## Post-Mortems

### Golden regeneration masks an economic regression

**Severity:** High
**Component:** `tests/test_backtest_golden.py::test_native_cli_pipeline_matches_golden`
**Fragility type:** load-bearing workflow default

#### What happened

A performance change reduced stop distance or shifted entry timing, altering quantities and PnL. An agent saw the golden test fail, ran `python tests/test_backtest_golden.py --update-golden`, regenerated the fixture, and marked the task complete. Review treated a green test as proof that the new economics were intended, so the strategy began trading with materially different risk.

#### The change that caused it

A developer changed entry timing or stop extraction to reduce lookahead and regenerated all expected output in the same task. The change was reasonable, but the economic decision and the expected-value update were not independently reviewed.

#### Why it broke

The update path is separate from pytest and requires an explicit CLI flag, but it still permits one task to change production behavior and rewrite the independent expectation. The hand-written sizing formula verifies arithmetic, not whether the selected entry, stop, or fill policy is correct.

#### How it was caught

The existing test would not reject a coherent but unintended strategy change. Independent spec review and domain-owner approval are required; the golden is a regression lock, not an oracle for desired financial behavior.

#### Hardening suggestions

Require golden changes to include a separately reviewed before/after ledger, changed risk assumptions, and owner approval. Treat `python tests/test_backtest_golden.py --update-golden` as a verification-lane change with elevated risk. Add a CI check that golden changes are authored separately from the production logic change whenever practical.

### A dependency upgrade increases tolerances until every result passes

**Severity:** High
**Component:** `tests/test_backtest_golden.py::_assert_json_equal`
**Fragility type:** invisible invariant

#### What happened

A pandas, NumPy, or SciPy upgrade changed last-bit floating-point values. The golden test failed. A developer increased trade and statistics tolerances by several orders of magnitude, making the gate pass while accepting materially different quantities, fees, and PnL.

#### The change that caused it

The lockfile was upgraded to receive security or compatibility fixes. Rather than reviewing each numeric delta, the team treated all differences as platform noise.

#### Why it broke

The comparator contains both path-specific tolerances and a full-payload exactness rule, but no policy distinguishes harmless representation noise from an economic shift. The test also cannot infer the intended tolerance from the candidate or dependency change.

#### How it was caught

A human must inspect the lockfile diff and the full golden delta. The 13 current-code mutation tests prove sensitivity but do not prove the chosen tolerances are correctly calibrated.

#### Hardening suggestions

Keep the current tight tolerances as the ceiling. Require every tolerance change to enumerate affected fields, maximum observed delta, economic impact, and cross-version evidence. Add a two-environment matrix for dependency-update PRs and reject unexplained changes outside already-accepted round-off bounds.

### A future multi-engine refactor leaves the golden green but weaker

**Severity:** Medium
**Component:** native CLI golden coverage
**Fragility type:** coincidental correctness

#### What happened

The CLI was refactored to run vectorbt for simple strategies while native remained the explicit fallback. The golden still passed because its requested engine and fixture happened to select the old path. A vectorbt sizing or fee regression shipped without detection.

#### The change that caused it

A developer added automatic engine routing and preserved the existing explicit `--engine native` golden to avoid coupling the test to routing policy.

#### Why it broke

The test covers one real pipeline, not the engine-selection decision. It cannot prove that every engine obeys the same trade, fee, equity, and statistics contract.

#### How it was caught

Existing parity tests would provide partial warning, but their broad tolerances and engine-specific known divergences leave room for silent drift.

#### Hardening suggestions

Add representative hand-reviewed ledgers for native and vectorbt, then a smaller cross-engine semantic comparison for identical signals. Treat automatic routing as a separate interface with its own contract tests and persisted run manifest.

### A future refactor removes the golden's isolated process boundary

**Severity:** Medium
**Component:** sanitized child process and import-time tripwires
**Fragility type:** semantic coupling through shared mutable state

#### What happened

A future refactor moved the CLI back into the pytest process beside tests that changed `BACKTESTS_DIR`, fee configuration, logging, environment loading, or data coverage globals. The golden read inherited state, wrote a repository log, or sent telemetry before its in-process monkeypatches ran, producing order-dependent CI failures or a false pass.

#### The change that caused it

The team simplified the test by importing `runner.cli` at module scope or removed the dedicated child because it was slower. Production modules still resolve logging, Sentry, dotenv, data, and database globals at import time.

#### Why it broke

The current child uses an allowlisted environment, disables repository dotenv loading, blocks network and SQLite connections, blocks Git subprocesses, redirects logs, and runs with temporary data. An in-process import bypasses those controls before fixture-level tripwires can be installed.

#### How it was caught

Static review identified import-time side effects before collection. Independent review then required moving the public CLI exercise into the sanitized child process.

#### Hardening suggestions

Keep money-path goldens in a dedicated process with a sanitized environment, or redesign configuration behind an immutable per-run context. Add a random-order/repeat command and a regression check that the child forbids network, SQLite, repository dotenv loading, Git subprocesses, and non-temporary logging.

## Themes and recommendations

The golden should be treated as reviewed financial evidence, not a disposable snapshot. Keep semantic invariants outside the golden, require independent review for updates, prohibit tolerance widening without cross-version evidence, and expand from one native pipeline to engine-specific and routing-specific contracts.
