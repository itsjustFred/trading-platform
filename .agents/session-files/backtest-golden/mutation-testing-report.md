# Backtest Golden Mutation Report

Date: 2026-09-25 (historical draft run) — current cycle 2026-11-03

## Current cycle (2026-11-03) — PR #108 after the accounting stack merge

Scope: the production files changed by this PR against `fix/perma-accounting`:
`shared/models.py` and `runner/daily_equity.py`. Test files are not mutated.

Baseline: `114 passed` (`tests/test_models.py`, `tests/test_daily_equity.py`).
Every mutation was applied alone, tested, and reverted with the original bytes
restored; `git status` was clean after each revert.

| # | File | Mutation | Result | Diagnostic | Caught by |
|---:|---|---|---|---|---|
| M1 | `shared/models.py` | `_derived_equity_evidence_status` returns the raw attribute instead of deriving `available` | Killed | Clear | `test_curve_attached_after_construction_serializes_evidence_status` |
| M2 | `shared/models.py` | `to_dict` serialises the stale attribute instead of the derived status | Killed | Clear | same |
| M3 | `shared/models.py` | drop the `available requires a curve` invariant | Killed (after fix) | Clear | `test_available_status_without_a_curve_is_rejected` — **added this cycle** |
| M4 | `runner/daily_equity.py` | a ledger/engine disagreement reports `basis="engine"` | Killed | Clear | `test_unreconciled_start_warns_once_and_reports_the_ledger_basis` |
| M5 | `runner/daily_equity.py` | never publish `meta.data["window_start_basis"]` | Killed | Clear | `test_reconciled_start_reports_the_engine_basis_without_warning` |
| M6 | `runner/daily_equity.py` | leave a stale `window_start_basis` on the empty-result path | Killed | Clear | `test_empty_result_clears_stale_daily_fields` |
| M7 | `runner/daily_equity.py` | publish the unquantised `running_equity` in the daily-return row | Killed | Clear | `test_daily_returns_equity_is_cent_quantised`, `test_daily_returns_equity_matches_the_published_curve_close` |
| M8 | `runner/daily_equity.py` | drop the float64 accumulation term from the reconciliation tolerance | Killed | Clear | `TestReconciliationTolerance` |
| M9 | `runner/daily_equity.py` | demote the basis-fallback warning to `debug` | Killed | Clear | `test_unreconciled_start_warns_once_and_reports_the_ledger_basis` |

**Score: 9/9 killed (100%), 0 survivors.** M3 survived the first pass and the
missing test was added rather than the mutant accepted: a payload claiming
`equity_evidence_status="available"` with no curve feeds the validation reports
with evidence that does not exist, so the guard needs a test of its own.

No tolerance was widened to kill a mutant.

---

# Historical draft run (2026-09-25)

> **Provenance.** This section is a historical record produced on the draft
> commit `1867c93` (`agent/backtest-golden-current`, based on the stale
> `origin/pr-103`). It scores mutants in `runner/daily_equity.py` and
> `runner/stats.py`, i.e. the accounting code that now lives in the separate
> G1/113/107 slices. Its recorded baseline SHA-256 values no longer match the
> files on this branch, so it is kept for provenance only and is superseded by
> the 2026-11-03 cycle above.

## Current result

- Scope: `runner/daily_equity.py` and `runner/stats.py` only.
- Current-code mutation set: **13 meaningful mutants** — 8 in daily equity and 5 in statistics.
- Result: **13/13 killed (100%)**, **0 survivors**.
- Current focused baseline: **78 passed, 5 warnings**.
- Production mutants remaining: **0**.

Tests were mutation detectors only and were not mutation targets. Import, docstring, formatting, and type-only changes were not counted.

The current implementation intentionally removed the previously surviving redundant prior-point finite prefilter. That removed mutant is not retained or scored in the current set. The revised tests now kill the prior initial-capital, daily-overflow, and fixed-ISO parser mutants.

## Current baseline and byte-exact restoration

| Target | Current baseline SHA-256 | Final SHA-256 | Result |
|---|---|---|---|
| `runner/daily_equity.py` | `14b2eb5f9526aa2ed44ce61f1e2741694a06df396befee435a92a013c9917ef9` | `14b2eb5f9526aa2ed44ce61f1e2741694a06df396befee435a92a013c9917ef9` | Exact match |
| `runner/stats.py` | `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` | `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` | Exact match |

No commit, stash, reset, checkout, or restore-from-HEAD operation was used. Each of the three requested rerun mutants was applied alone, tested, reversed by its exact inverse edit, and followed immediately by a SHA-256 restoration check before the next mutation.

Current inventory command:

```bash
git status --short --untracked-files=all && sha256sum "runner/daily_equity.py" "runner/stats.py"
```

Focused verification command, rerun after the final test-only hardening:

```bash
uv run --frozen pytest -q -p no:cacheprovider tests/test_daily_equity.py tests/test_stats.py tests/test_backtest_golden.py
```

Result on the final test set: **78 passed, 5 warnings**.

Final no-cache lint command:

```bash
uv run --frozen ruff check --no-cache "runner/daily_equity.py" "runner/stats.py"
```

Result: **All checks passed**.

Final restoration check:

```bash
git diff --check -- "runner/daily_equity.py" "runner/stats.py" && sha256sum "runner/daily_equity.py" "runner/stats.py" && git status --short --untracked-files=all
```

The production diff check produced no output, both final hashes exactly matched the current baseline, and no file other than this report was intentionally changed by the mutation run.

## Requested rerun evidence

| Rerun | Exact production edit | SHA-256: baseline → mutant → restored | Exact focused command and result | Diagnostic |
|---:|---|---|---|---|
| R1 | `runner/daily_equity.py`: generated-curve guard `not math.isfinite(initial_capital)` → `initial_capital == 0.0` | `14b2eb5f9526aa2ed44ce61f1e2741694a06df396befee435a92a013c9917ef9` → `399459ab3a4181eee58c6b8fc2816c31c844f38d7fc7456be9c86f531425083a` → `14b2eb5f9526aa2ed44ce61f1e2741694a06df396befee435a92a013c9917ef9` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_daily_equity.py::TestBuildDailyEquityCurve::test_rejects_nonfinite_initial_capital` — **3 failed** | Clear: NaN, positive infinity, and negative infinity all produced `DID NOT RAISE ValueError`. |
| R2 | `runner/daily_equity.py`: accumulation guard `not math.isfinite(running_equity)` → `running_equity == 0.0` | `14b2eb5f9526aa2ed44ce61f1e2741694a06df396befee435a92a013c9917ef9` → `c9eb2b1137cb02349f808091348bfeb2b0451d41a026b1351467e308a13f1615` → `14b2eb5f9526aa2ed44ce61f1e2741694a06df396befee435a92a013c9917ef9` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_daily_equity.py::TestBuildDailyEquityCurve::test_rejects_daily_balance_overflow` — **1 failed** | Clear: overflow bypassed the accumulation guard and surfaced the later engine-curve non-finite error instead. |
| R3 | `runner/stats.py`: CAGR parser `format="mixed"` → `format="ISO8601"` | `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` → `ffd07a50b8c454545cdd60a6142fdcd0262b3d12e6ffd0ad62bd0cdb4ec7d8f3` → `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_stats.py::TestComputeStats::test_cagr_handles_mixed_fractional_curve_timestamps` — **1 failed** | Clear: fixed ISO8601 dropped the lowercase-`t` endpoint and produced CAGR `10.278582` instead of `2.358360...`. |

## Final current-code mutation set

Rows marked **retained** are isolated kills from the prior run whose exact production edits still exist in the final code. Their SHA evidence records that original execution. Rows marked **rerun** use the current baseline hashes above. Together they form the final current-code set of 13.

| # | Source | Exact production edit | Execution SHA-256: baseline → mutant → restored | Exact focused command and result | Diagnostic |
|---:|---|---|---|---|---|
| 1 | `runner/daily_equity.py` | `float(trade.net_pnl)` → `float(trade.pnl or 0.0)` | Retained: `fc61cb51acd25675b5fa3d66d5ccb44f524736692f0bad043bb1db4616f7b642` → `7385ca4532412f5148007caf3989ef33d7114d202f92a00c2e9a8394119ea1c3` → `fc61cb51acd25675b5fa3d66d5ccb44f524736692f0bad043bb1db4616f7b642` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_daily_equity.py::TestAttachDailyEquity::test_window_rebases_from_net_pnl_not_gross_engine_point` — **1 failed** | Clear |
| 2 | `runner/daily_equity.py` | `curve_end < max(trade_end_times)` → `curve_end <= max(trade_end_times)` | Retained: `fc61cb51acd25675b5fa3d66d5ccb44f524736692f0bad043bb1db4616f7b642` → `5990e9d11474df06e4b1fdabc2487f10a5dda1e9530cb5cdad9c02baa8ebc419` → `fc61cb51acd25675b5fa3d66d5ccb44f524736692f0bad043bb1db4616f7b642` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_daily_equity.py::TestAttachDailyEquity::test_duplicate_engine_timestamps_are_coalesced` — **1 failed** | Indirect |
| 3 | `runner/daily_equity.py` | Generated-curve guard `not math.isfinite(initial_capital)` → `initial_capital == 0.0` | Rerun R1: `14b2eb5f9526aa2ed44ce61f1e2741694a06df396befee435a92a013c9917ef9` → `399459ab3a4181eee58c6b8fc2816c31c844f38d7fc7456be9c86f531425083a` → `14b2eb5f9526aa2ed44ce61f1e2741694a06df396befee435a92a013c9917ef9` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_daily_equity.py::TestBuildDailyEquityCurve::test_rejects_nonfinite_initial_capital` — **3 failed** | Clear |
| 4 | `runner/daily_equity.py` | Accumulation guard `not math.isfinite(running_equity)` → `running_equity == 0.0` | Rerun R2: `14b2eb5f9526aa2ed44ce61f1e2741694a06df396befee435a92a013c9917ef9` → `c9eb2b1137cb02349f808091348bfeb2b0451d41a026b1351467e308a13f1615` → `14b2eb5f9526aa2ed44ce61f1e2741694a06df396befee435a92a013c9917ef9` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_daily_equity.py::TestBuildDailyEquityCurve::test_rejects_daily_balance_overflow` — **1 failed** | Clear |
| 5 | `runner/daily_equity.py` | Strict-curve guard `not math.isfinite(equity)` → `math.isnan(equity)` | Retained: `fc61cb51acd25675b5fa3d66d5ccb44f524736692f0bad043bb1db4616f7b642` → `7c616c041b5810faff5e6195eda7b2eeabe0840bce2eadddca4d5ddfee71ba89` → `fc61cb51acd25675b5fa3d66d5ccb44f524736692f0bad043bb1db4616f7b642` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_daily_equity.py::TestAttachDailyEquity::test_engine_only_curve_rejects_nonfinite_equity` — **1 failed** | Clear |
| 6 | `runner/daily_equity.py` | Naive timestamp `parsed.replace(tzinfo=timezone.utc)` → `parsed.astimezone()` | Retained: `fc61cb51acd25675b5fa3d66d5ccb44f524736692f0bad043bb1db4616f7b642` → `0dda61e2062ad1f816be19506060eeac7a84c66017f27bfd1eea89e99051d999` → `fc61cb51acd25675b5fa3d66d5ccb44f524736692f0bad043bb1db4616f7b642` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_daily_equity.py::TestBuildDailyEquityCurve::test_naive_exit_time_is_treated_as_utc` — **1 failed** | Clear |
| 7 | `runner/daily_equity.py` | Duplicate replacement `coalesced[-1] = point` → `continue` | Retained: `fc61cb51acd25675b5fa3d66d5ccb44f524736692f0bad043bb1db4616f7b642` → `3cde8ded0dfc9f792341264612c4773a2bffa0e3ed6d719b82ff18c0f7f4a65b` → `fc61cb51acd25675b5fa3d66d5ccb44f524736692f0bad043bb1db4616f7b642` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_daily_equity.py::TestAttachDailyEquity::test_duplicate_engine_timestamps_are_coalesced` — **1 failed** | Clear |
| 8 | `runner/daily_equity.py` | Preserved endpoint `rel_tol=0.0` → `rel_tol=1e-9` | Retained: `fc61cb51acd25675b5fa3d66d5ccb44f524736692f0bad043bb1db4616f7b642` → `c39c50c44ce2da692558f7eab5396ca5bd1e9867bdd28fc4526b0c9684d1541c` → `fc61cb51acd25675b5fa3d66d5ccb44f524736692f0bad043bb1db4616f7b642` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_daily_equity.py::TestAttachDailyEquity::test_large_account_endpoint_drift_uses_absolute_tolerance` — **1 failed** | Clear |
| 9 | `runner/stats.py` | CAGR parser `format="mixed"` → `format="ISO8601"` | Rerun R3: `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` → `ffd07a50b8c454545cdd60a6142fdcd0262b3d12e6ffd0ad62bd0cdb4ec7d8f3` → `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_stats.py::TestComputeStats::test_cagr_handles_mixed_fractional_curve_timestamps` — **1 failed** | Clear |
| 10 | `runner/stats.py` | Equity bounds `min(...), max(...)` → `max(...), min(...)` | Retained: `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` → `05577672c2ccf8fe8ccaee0370afa84b0abf032840d3b76207f13d51acd7c346` → `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_stats.py::TestComputeStats::test_cagr_uses_elapsed_time_for_sparse_equity_curve` — **1 failed** | Indirect |
| 11 | `runner/stats.py` | Elapsed-day conversion `/ SECONDS_PER_DAY` → `/ (SECONDS_PER_DAY * 2)` | Retained: `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` → `cc857324aa3d2f00b536a25d1a808b4e3da8868f0591ed71ed83bbf1004a127a` → `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_stats.py::TestComputeStats::test_cagr_uses_fractional_days_from_trade_bounds` — **1 failed** | Clear |
| 12 | `runner/stats.py` | Annual exponent `365.25` → `365.0` | Retained: `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` → `24ce6a443bdc210487cc431a0ef4274e49cbcdf47d12cda06c033409971a71f8` → `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_stats.py::TestComputeStats::test_cagr_uses_fractional_days_from_trade_bounds tests/test_stats.py::TestComputeStats::test_cagr_handles_mixed_fractional_curve_timestamps` — **1 failed, 1 passed** | Clear |
| 13 | `runner/stats.py` | CAGR parser `format="mixed"` → `format="%Y-%m-%dT%H:%M:%S.%f%z"` | Retained: `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` → `324c53c6efb2c08d35cea5710da6c909a03a7539f03f6b1ea781421e468dbafb` → `3e6d6166b0a33d818314a5cbeddaaa47e08f9bdeea7b9f46c32931e1b1810e8c` | `uv run --frozen pytest -q -p no:cacheprovider tests/test_stats.py::TestComputeStats::test_cagr_handles_mixed_fractional_curve_timestamps` — **1 failed** | Clear |

## Score and diagnostic quality

- `runner/daily_equity.py`: **8/8 killed (100%)**.
- `runner/stats.py`: **5/5 killed (100%)**.
- Overall: **13/13 killed (100%)**, **0 survived**.
- Diagnostic quality: **11/13 clear**, **2/13 indirect** (rows 2 and 10), **0 cascading**.
- Survivor-specific recommended tests: none; the current set has no surviving mutants.

## Historical evidence (superseded)

The immediately preceding two-file run used daily-equity baseline SHA-256 `fc61cb51acd25675b5fa3d66d5ccb44f524736692f0bad043bb1db4616f7b642`, the same stats baseline, and 71 focused tests. It scored 10/14 with four survivors: the removed prior-point finite prefilter, the initial-capital guard, the daily accumulation guard, and fixed `ISO8601` instead of `format="mixed"`.

The redundant prefilter was subsequently removed from production, intentionally making the new daily baseline `14b2eb5f9526aa2ed44ce61f1e2741694a06df396befee435a92a013c9917ef9`. It is not a current mutant and is not scored. New regression tests for non-finite initial capital and finite-input daily overflow, plus the revised lowercase-`t` timestamp fixture, killed the other three prior survivors in R1–R3. The ten unaffected prior kills are retained as exact-edit evidence in the final table.

An even earlier broad run covering CLI and engine mutations reported 30/30 killed. That broader historical score is not combined with the current two-file score.

## Current conclusion

The final current code and tests detect all 13 meaningful mutants in scope, including net-versus-gross window rebasing, preserved-curve trade-time coverage, non-finite capital and overflow rejection, strict curve finiteness, UTC interpretation, duplicate coalescing, endpoint reconciliation, and mixed-ISO CAGR parsing and annualization. There are no current survivors. Both production files were restored byte-for-byte to the current baseline SHA-256 values, the final focused suite passed all **78 tests**, Ruff passed, and this report is the only file intentionally written by the continuation.
