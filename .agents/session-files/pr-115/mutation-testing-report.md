# Mutation testing report — PR #115 (P7 Data/Validation) verify cycle (2026-10-30)

Scope: the production files changed by the PR only (`git diff --name-only main...HEAD`,
tests excluded). Clean tree, full focused suite green before starting (1011 passed).
Each mutant was applied singly, tested against the narrowest relevant subset, and
reverted immediately from the in-memory original (a driver script, so no mutant was
ever left in the tree — `git status` was checked after every file).

Files mutated: `shared/market_calendar.py`, `data_tools/auto_download.py`,
`data_tools/validate.py`, `data_tools/resample.py`, `data_tools/dolthub_manager.py`,
`data_tools/download_grid_market_data.py`, `runner/backtest_grid.py`,
`shared/constants.py`, `validation/readiness/{models,validator}.py`,
`validation/importers/mt5_xlsx.py`.

`validation/comparison/__init__.py` is an import-order change with no logic; not mutated.

## Results

| # | File | Mutation | Result | Diagnostic | Notes |
|---|------|----------|--------|------------|-------|
| 1 | `market_calendar.py` | drop the Sunday shift for New Year | Killed | Clear | `test_a_sunday_new_years_day_is_observed_on_monday` |
| 2 | `market_calendar.py` | drop the Saturday shift for fixed-date holidays | Killed | Clear | 2026 closure table |
| 3 | `market_calendar.py` | weekday bound `< 5` → `< 6` | Killed | Indirect | killed by a `last_session_at_or_before` case |
| 4 | `market_calendar.py` | `end < start` → `end <= start` | Killed | Indirect | killed via the daily-lag case |
| 5 | `market_calendar.py` | drop the pre-open rollback | Killed | Clear | `test_last_session_at_or_before[...09-09]` |
| 6 | `market_calendar.py` | drop the "no session strictly inside" guard | Survived → test added → Killed | Clear | new cases with a live Monday inside the hole |
| 7 | `market_calendar.py` | `[bar, bar+span)` → `(bar, bar+span]` | Survived → test added → Killed | Clear | new bar-stamped-exactly-at-the-open case |
| 8 | `market_calendar.py` | `AD_HOC_CLOSURE_TOLERANCE_SESSIONS` 2 → 0 | Killed | Clear | closure-week case |
| 9 | `market_calendar.py` | session span doubled | Killed | Clear | `test_session_span_seconds...` |
| 10 | `auto_download.py` | venue gate returns True (excuse every venue) | Killed | Clear | FX outage cases |
| 11 | `auto_download.py` | drop the unreadable-endpoint guard | Killed | Clear | `test_a_gap_with_unreadable_endpoints_is_never_excused` |
| 12 | `auto_download.py` | allowance × 2 | Killed | Clear | "wider than the closure" case |
| 13 | `auto_download.py` | drop the one-bar term | Killed | Clear | allowance derivation test |
| 14 | `auto_download.py` | fail only on `error`, not on gaps | Killed | Clear | small-gap/large-gap |
| 15 | `auto_download.py` | `>` → `>=` at the fail threshold | Survived → test added → Killed | Clear | new exactly-3.00-day case |
| 16 | `auto_download.py` | drop timeframe scaling | Killed | Clear | weekly coverage case |
| 17 | `auto_download.py` | `and` → `or` in the venue-metadata gate | Killed | Clear | non-US-equity venue case |
| 18 | `auto_download.py` | drop the closure-shape check | Killed | Clear | 3.6-day 1m hole case |
| 19 | `validate.py` | segmented file validated at `candidate.stem` | Killed | Clear | segmented-timeframe case |
| 20 | `validate.py` | `MAX_REPORTED_GAPS` 20 → 0 | Killed | Clear | gap-bounds case |
| 21 | `validate.py` | canonical source-meta key misspelled | Killed | Clear | canonical-key case |
| 22 | `validate.py` | malformed session keeps the sentinel | Survived → test added → Killed | Clear | new classification-equivalence test |
| 23 | `validate.py` | missing separator → `None` | Survived → test added → Killed | Clear | new `_session_window` cases |
| 24 | `validate.py` | drop the once-per-value memo | Survived → test added → Killed | Clear | new warning-count test |
| 25 | `validate.py` | empty window → malformed | Survived → test added → Killed | Clear | same `_session_window` cases |
| 26 | `resample.py` | `W-MON` → `W-SUN` | Killed | Clear | weekly-label test |
| 27 | `resample.py` | `label/closed` left → right | Killed | Clear | 1m→5m bucket test |
| 28 | `resample.py` | pass `origin` to every rule | Killed | Clear | origin-only-tick table |
| 29 | `resample.py` | drop `sort_index()` | Survived (equivalent) | — | pandas 3.x sorts internally; the sort guards pandas 2.x, where `resample` raises. Test added that pins the behaviour either way |
| 30 | `resample.py` | zero-volume flag `<= 0` → `< 0` | Killed | Clear | merged-volume test |
| 31 | `dolthub_manager.py` | drop the timeout from the symbol-listing guard | Killed | Clear | failing-repo test |
| 32 | `dolthub_manager.py` | ignore the `symbol` filter | Survived → test added → Killed | Clear | new SQL-builder tests |
| 33 | `download_grid_market_data.py` | unsupported set loses `1w` | Killed | Clear | served-set test |
| 34 | `download_grid_market_data.py` | order keeps unsupported entries | Killed | Clear | every-canonical-timeframe test |
| 35 | `download_grid_market_data.py` | CLI default restated | Killed | Clear | default-is-the-served-set test |
| 36 | `readiness/models.py` | Sharpe floor 0.05 → 0.1 | Killed | Clear | floor-pin test |
| 37 | `readiness/models.py` | switch default → True | Killed | Clear | default-is-off test |
| 38 | `readiness/validator.py` | drop `applicable` from the error count | Survived (equivalent) | — | the only producer of a non-applicable check also sets severity `info`, so the count is unchanged; the filter is defence for a future producer |
| 39 | `readiness/validator.py` | `and` → `or` in the not-comparable condition | Killed | Clear | switch-does-not-affect-non-zero test |
| 40 | `readiness/validator.py` | severity ignores not-comparable | Survived → test added → Killed | Clear | new assertion that a gross drift keeps severity `info` |
| 41 | `mt5_xlsx.py` | drop the Trade-column guard | Killed | Clear | `test_missing_trade_column_raises` |
| 42 | `mt5_xlsx.py` | look the column up under a wrong name | Killed | Clear | importer fixtures |
| 43 | `backtest_grid.py` | alias map becomes a copy | Killed | Clear | identity test |
| 44 | `constants.py` | `m15` → `1m` | Killed | Clear | documented-alias-set test |

## Mutation score

42 of 44 killed (95%). Both survivors are behaviourally equivalent mutants, not
coverage gaps:

- `readiness-applicable-count`: `applicable` is only ever set `False` together with
  `severity="info"`, so removing the filter from the error sum changes nothing today.
  It is kept as the contract for a future producer.
- `res-sort-index`: pandas 3.x sorts inside `resample`; the call exists for pandas 2.x,
  where an unsorted index raises. A test now pins the behaviour, but it cannot kill the
  mutant on the installed pandas.

## Tests added (implemented)

- `tests/test_market_calendar.py`: interior-session holes, bar at the open.
- `tests/test_coverage_gaps.py`: exactly-3.00-day threshold, malformed-session class,
  report-path containment.
- `tests/test_dolthub_sql_and_timeouts.py`: symbol filter + unsafe symbol.
- `tests/test_readiness_sharpe_tolerance.py`: severity stays `info` when declining.
- `tests/test_resample.py`: unsorted input.

All pass on clean code and fail with the corresponding mutant applied.

---
Model: deepseek-v4.1-flash, Harness: T3 Code (ACP, 2026-10-30)
