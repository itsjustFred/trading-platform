# Backtest Golden Pre-Mortem Report

**Scope:** `tests/test_backtest_golden.py`, `tests/fixtures/backtest/native_cli_v1.json`, and the exercised CLI/data/native-engine/statistics path
**Date:** 2026-09-25

## Summary

The golden is hermetic and intentionally strict, but four future failure modes could erode trust: automatic golden blessing, blanket tolerance expansion, dependency/platform float drift, and false confidence outside the native single-symbol path. The current semantic assertions, explicit generator command, sanitized child process, frozen lockfile, and mutation testing reduce these risks; they do not replace review policy or broader engine coverage.

## Implementation status on port to `main`

Added when the golden was ported to `main` on top of the accounting slice (G1):

- **Golden regeneration masks an economic regression** — the `--update-golden` path now prints a before/after summary of every changed field plus a separate economic ledger (equity endpoints, PnL, fees, prices, trade counts, ratios). A change to that ledger additionally requires `--accept-economic-change`; the economic decision is therefore a second, explicit step instead of a side effect of making the test green. The test itself still never writes the fixture.
- **Silent cross-checkout blessing** — the generator prepends its own repo root to `sys.path`, so a worktree run cannot mix the tested checkout with whatever an editable install points at.
- **A dependency upgrade increases tolerances** — unchanged: the tolerance ceilings in `_assert_json_equal` are untouched. Curve closes are now asserted against the exact cent-rounded value rather than a slack tolerance.


## Cycle 2 (2026-11-03) — after merging PR 107 and PR 113

Scope: the production files this PR changes — `shared/models.py` and
`runner/daily_equity.py`. Hardening was limited to what the changed code owns;
nothing here is a request to redesign the accounting slices.

### The in-memory equity evidence status diverges from the serialised one

**Severity:** Medium
**Component:** `shared/models.py::BacktestResult.to_dict`, `runner/daily_equity.py::attach_daily_equity`
**Fragility type:** Invisible invariants

#### What happened

A validation run reported "equity evidence: missing" for a backtest that had a
4-point daily equity curve. The report was produced in-process right after the
backtest, so it read `result.equity_evidence_status`, which was still
`"unknown"`; the JSON that was written to disk said `"available"`.

#### The change that caused it

A developer added an in-process validation summary to avoid re-reading the
result file, calling `validator.validate(platform=result)` on the object the
runner had just finished building.

#### Why it broke

`to_dict()` derives the status from the curve (`_derived_equity_evidence_status`),
but the dataclass attribute is only written in `__post_init__`. `attach_daily_equity`
attaches the curve afterwards and never touches the attribute, so the object
carries `"unknown"` for the rest of its life while serialising `"available"`.
Any consumer that reads the attribute instead of `to_dict()` — `validation/cli.py`
already compares `platform.equity_evidence_status == "available"` — sees the
stale value.

#### How it was caught

Nothing catches it today. Every existing path either serialises the object or
loads it from disk (`from_dict` → `__post_init__` recomputes correctly), so the
window between attach and save is currently unobserved. A test that asserts the
attribute after `attach_daily_equity` would pin it.

### The window basis is published before the curve build can fail

**Severity:** Medium
**Component:** `runner/daily_equity.py::attach_daily_equity`
**Fragility type:** Non-atomic compound operations

#### What happened

A run aborted with `Daily equity accumulation is non-finite`. The exception was
handled upstream and the partially mutated result was still written by a retry
path, which now claimed `meta.data["window_start_basis"] == "net_ledger"` while
carrying the *previous* run's equity curve.

#### The change that caused it

A developer moved `result.meta.data["window_start_basis"] = window_start.basis`
next to the other early metadata writes to keep the "publish before return"
pattern consistent.

#### Why it broke

`attach_daily_equity` writes `window_start_basis` into `result.meta.data` before
calling `build_daily_equity_curve`, which can raise on a non-finite balance.
The write is not rolled back, so a failed attach leaves the result with new
metadata and an old curve. The function mutates the caller's object in place and
has no transactional boundary.

#### How it was caught

Only by the exception text; no test asserts the state of `result.meta.data`
after a failed attach. A test that raises through `attach_daily_equity` and
checks `window_start_basis` would pin the atomicity requirement.

### Published equity is quantised but the published return is not

**Severity:** Medium
**Component:** `runner/daily_equity.py::build_daily_equity_curve`
**Fragility type:** Invisible invariants

#### What happened

A viewer recomputed daily returns as `equity[i] / equity[i-1] - 1` from the
published `daily_returns` rows and showed a -0.1277% day as -0.1277% but the
third day off in the fifth decimal, then disagreed with the platform's
`total_return` in a reconciliation report.

#### The change that caused it

A developer computed a "gross return" column from the published equity values
because the rows already carry both `equity` and `return`, assuming they are
consistent with each other.

#### Why it broke

`build_daily_equity_curve` publishes `equity = round(running_equity, 2)` but
`return = (running_equity - previous) / previous` from the *unrounded* balance.
The two fields in the same row are not related by the identity a reader assumes.
The golden test pins both values, but nothing states the invariant in the
production code.

#### How it was caught

The golden fixture compares exactly, so any change to either convention fails
there. A downstream consumer outside this repo would not be warned.

### The reconciliation tolerance is a stack of load-bearing constants

**Severity:** High
**Component:** `runner/daily_equity.py::_reconciliation_tolerance`
**Fragility type:** Load-bearing defaults

#### What happened

An engine upgrade changed its equity rounding from cents to mills. The
reconciliation still passed, and a 3-cent drift per trade accumulated into a
$900 difference between the published curve and the trade ledger before anyone
noticed.

#### The change that caused it

A developer bumped `FLOAT64_ACCUMULATION_SAFETY` from 8.0 to 64.0 while chasing
a flaky large-account test, on the grounds that "more headroom is safer".

#### Why it broke

The tolerance is assembled from `EQUITY_HALF_CENT`, a per-trade cent term, and
`FLOAT64_EPSILON * expected_final * trade_count * FLOAT64_ACCUMULATION_SAFETY`.
Each term is individually justified in a comment, but nothing tests the
*combination* against a real engine/ledger disagreement at scale. Raising the
safety factor widens every reconciliation silently, and the engine curve is then
adopted even though it disagrees with the net ledger.

#### How it was caught

`TestReconciliationTolerance` pins the cliff at a small account and a
trillion-dollar account, so a constant change fails there. It does not pin the
per-trade term for a large trade count, which is where the safety factor
actually bites.

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
