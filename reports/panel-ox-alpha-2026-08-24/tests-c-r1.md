# Code Review — tests-c (chunk C of test suite), reviewer r1

Reviewer: ox-alpha (panel r1) · 2026-08-24 · Blind & independent review of `tests/test_*.py` (signal→walkforward group), `tests/validation/**`, `tests/validators/**`.

Scope: all 40 tracked code files listed in the assignment — each has a section below; files with no findings are recorded as scanned-clean. Findings cite `file:line` with severity critical/major/minor.

## Executive summary

The chunk is generally healthy: most suites assert real behavior with specific expected values, negative paths are covered, and several files (test_trade_comparison, test_trade_anomalies, test_trade_statistics, test_quantconnect_result_importer, test_importers) are exemplary. The recurring weaknesses are:

1. **Environment coupling masquerading as coverage** (biggest theme): engine-parity and cross-platform-matrix tests silently skip or read mutable repo/user data (`tests/test_trade_engine.py:324`, `tests/validation/test_cross_platform_metrics.py:30`), golden tests freeze env-dependent paths at import time (`tests/validation/test_quantconnect_result_importer.py:26`), and CWD-relative fixture paths appear in four importer/regression files. These tests report green while validating nothing on machines that lack the data.
2. **Misleading names on financially-sensitive paths**: `test_round_lots_rounds_up` asserting floor rounding (`test_fixed_risk_model.py:158`), `test_zero_vs_nonzero_pnl_mismatch` asserting a pass (`test_trade_comparison.py:281`), `min_position_size_clamping_zero` semantics (`test_percent_risk_model.py:245`), TP-from-meta test that verifies no such extraction (`test_percent_risk_model.py:310`). Each can send a future maintainer the wrong way about risk behavior.
3. **Security-relevant gaps in the validators**: the `AICodeValidator` blocklist is trivially bypassable while its `passed=True` reads as a safety verdict (`tests/validators/code_validator.py:20`), and generated code is `exec()`'d unguarded in codegen tests (`tests/test_strategy_ir_codegen_vectorbt.py:63`).
4. **Massive fixture/helper duplication across ~15 validation files** (`_make_trade` & friends re-declared per file; act-blocks repeated verbatim up to 22×/file) — the dominant maintenance tax and drift vector.
5. No flaky sleeps beyond bounded polls in viewer tests; subprocess-without-timeout found once (validation CLI); tz handling is consistently good suite-wide.

Severity counts for the whole area: **0 critical · 14 major · 73 minor**.

---


## Group A: tests/test_viewer_features.py

Scanned in full (4790 lines). Overall quality is high: real endpoints exercised via `TestClient`, tmp_path isolation, meaningful negative assertions (`not in`, 404/400/409 paths). Findings:

**(a) Bugs / correctness risks**

1. **tests/test_viewer_features.py:2296-2299 — major — expected value encodes an overlapping (double-downloading) split plan.**
   `test_auto_download_source_plan_splits_us_intraday_range` asserts the US-intraday plan is
   `[{huggingface, 2025-06-20 → 2025-07-31}, {alpaca, 2025-07-01 → 2025-08-05}]`.
   The two segments overlap by a full month (July 1–31 is covered by *both* sources). Either the production planner double-fetches a month of data (a real bug blessed by this test), or the overlap is an intentional safety margin — in which case the test name ("splits range") and lack of any comment mislead the reader. A test named "splits" that also asserts duplication should state why; as written it gives false confidence that partitioning is correct.

2. **tests/test_viewer_features.py:3037 — minor — tautological/redundant assertion.**
   `assert "Running backtest... " in log_text` is implied by the two preceding stronger assertions (`"...45%"`, `"...58%"` at :3035-3036); it can never fail if they pass. Dead check that pads the test.

3. **tests/test_viewer_features.py:1382-1403 — minor — unbounded `Barrier(2)` wait can hang the suite instead of failing.**
   `barrier.wait()` is called with no timeout; if either worker dies before the barrier (e.g. an import/attribute error), the other blocks forever. The subsequent `join(timeout=2)` + `is_alive()` guard only helps after both reach the barrier. Pass a timeout to `wait()` or the Barrier constructor so the failure mode is a failed assert, not a hung CI job.

4. **tests/test_viewer_features.py:2811-2815, 2888-2892, 3004-3008, 3151-3155 — minor — bounded-but-real sleep-polling loops.**
   Four act-phases spin on `time.sleep(0.05/0.1)` up to 50 iterations waiting on live background threads, then read `DATA_TASKS[...]` unconditionally. On a slow CI runner the loop can exhaust and the test still proceeds to assert against a not-yet-finished task, producing a confusing downstream failure instead of a clear timeout message. Consider a deadline + explicit `pytest.fail("task did not finish")`, or an event-driven hook. (Boundedness keeps this minor.)

5. **tests/test_viewer_features.py:1258-1279, 1642-1672, 1698-1724 — minor — thread doubles keyed off `traceback.extract_stack()` frame inspection.**
   Intercepting only threads constructed from `viewer/core.py` frames couples the tests to the *file name and function names* of production internals; renaming/moving `_start_data_task` in `viewer/core.py` silently changes which threads get faked, potentially letting real downloader threads spawn during unit tests. A seam (injectable thread factory/starter) would be less brittle.

6. **tests/test_viewer_features.py:2275-2284 — minor — needless fixture dependency.**
   `test_auto_download_source_prefers_yfinance_for_non_forex_symbols` takes `viewer_client` (which swaps six module globals and spins up a TestClient) but performs pure function calls on `_auto_download_source`. Misleading setup suggests endpoint coverage where there is none.

**(b) Security** — nothing found. Tests do exercise rejection paths (400 pre-genesis :315-344, invalid query timeout :2125-2137, unknown provider :4562-4585) and confirm OHLCV price fields are stripped from public payloads (:738-769), which is good hygiene coverage.

**(c) Baseline smells**

- **Divergent Change / Long Module (major hotspot)**: one 4790-line file covers ≥15 unrelated concerns — dukascopy genesis, grid matrix overview, gzip middleware, catalog/data-overview grouping & merging, download request coalescing + crash-recovery markers, task lifecycle/pause/resume/restart/delete, subprocess log streaming, optimization runs, signal replay, CAPM/p-value stats endpoints, portfolios, agent convert, logs endpoint. Every new viewer feature lands here; split per resource area.
- **Duplicated Code (minor)**: the `_NoStartThread` test double with its comment "This test double intentionally ignores Thread construction arguments." is re-declared verbatim 5× (:1750, :1795, plus variants at :1310, :1349, :1598); the `fake_start` capture helper is re-written ~6× (:1216, :2098, :2145, :2187, :2224). Extract module-level helpers/fixtures.
- **Mysterious Name (nit)**: `test_data_download_does_not_apply_recovery_metadata_to_coalesced_registration` (:1305) describes behavior but the class under test seam (`_NoStartThread`) says nothing; acceptable.

**(d) Maintainability hot spots**: file size (above); the coalescing/cleanup-marker cluster (:1785-2091) pokes many private underscore attributes (`DATA_TASKS`, `_COALESCED_CLEANUP_PENDING`, `_TASK_CACHE_HYDRATED`, `_BACKTEST_META_CACHE*`) directly from tests — any refactor of those privates breaks ~20 tests at once (shotgun-surgery coupling between test and implementation detail).

---

## tests/validation/test_quantconnect_result_importer.py

Scanned in full (912 lines). Strong file: extensive fail-closed coverage with exact error-message matching, Decimal-exact fee derivation, CSV↔JSON cross-checks.

**(a) Bugs / correctness risks**

1. **tests/validation/test_quantconnect_result_importer.py:19-29 — major — reference paths resolved once at import time from ambient environment; golden tests silently retarget.**
   `_reference_dir()` reads `QUANTCONNECT_REFERENCE_DIR` and is evaluated at module import (:26), freezing `RESULT_JSON`/`ORDERS_CSV`/`TRADES_CSV`. A stale exported value redirects all 8 `golden_reference` tests (:302-636) to different data while they still claim to validate the tracked reference (`tests/fixtures/validation/quantconnect-reference/`). Additionally, if the fixture files are ever absent (partial checkout), the module-scoped fixture's bare `assert` (:304) errors 8 tests with an unhelpful message instead of a clean `pytest.mark.skipif`. Files are git-tracked today (verified), so this is robustness/misleadingness rather than breakage.
2. **tests/validation/test_quantconnect_result_importer.py:341-346, 414-435 — minor — brittle over-specified goldens.** Exact cluster-size distribution dict and a literal 20-element order-ID list make these fail on any re-export of the reference dataset even when importer behavior is unchanged. The invariant checks at :451-456 are the valuable part; consider demoting the literal lists.
3. **tests/validation/test_quantconnect_result_importer.py:902 — minor — statement with no effect.** Bare `json.dumps(reference.raw_metadata)` after the first load looks like a leftover debug line or an undocumented JSON-serializability check; as written it can never fail (no assert on round-trip) and is dead code.

**(b) Security** — nothing found; this suite *enforces* fail-closed parsing (non-finite profitLoss :202, fractional order id :215, trailing-text price :225, duplicate/unknown IDs :269/:282/:687/:697, negative fee remainder :510), which is security-relevant for import paths.

**(c) Baseline smells**

- **Duplicated Code (minor)**: trades-CSV fieldnames list spelled out three times verbatim (:740-755, :786-800, :832-847); extract a module constant.
- **Long Test / logic reimplementation (minor)**: `test_real_export_derives_unreferenced_order_fees_from_source_quantities` (:379-456) rebuilds ~25 lines of fee-model arithmetic inside the test to construct expectations; it cross-checks but partially mirrors production logic.

**(d) Maintainability hot spots**: import-time env coupling above; otherwise solid.

---

## tests/validation/test_delta_report.py

Scanned in full (538 lines).

**(a) Bugs / correctness risks**

1. **tests/validation/test_delta_report.py:98-105 — minor — `Any` used in annotations but never imported.** `_make_field_result(...)` annotates parameters with `typing.Any` without importing `Any`; masked only by `from __future__ import annotations` (:3). Any runtime introspection (`get_type_hints`) or removal of the future import raises `NameError`; type-checkers flag it.
2. **tests/validation/test_delta_report.py:98-113 — major — dead test utility: `_make_field_result` is defined and never called anywhere in the module.** Dead test utility per checklist; delete or wire up.
3. **tests/validation/test_delta_report.py:166-178 — minor — vacuous numeric "match" test.** `test_field_delta_numeric_match` sets `relative_diff=0.000009` and `tolerance=0.0001` itself, then asserts `relative_diff < tolerance`: a tautology over constants it chose; no production code participates.
4. **tests/validation/test_delta_report.py:136-196 — minor — constructor round-trip tests assert only that dataclasses store attributes** (`test_field_delta_creation`, `test_field_delta_explanation_for_match`, `test_trade_delta_creation`); misleading names suggest delta-logic coverage.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (minor)**: the 5-line act sequence building matched→compare→reference→platform→report repeats verbatim in ~12 tests across classes; extract a parameterized helper.
- **Mysterious Name (nit)**: class `TestReportWithMismatch.test_entry_price_mismatch_report` asserting `passed is True` (:267) surprises until the inline comment explains warning-severity policy; rename or document.

**(d) Maintainability hot spots**: low; positional 4-arg `DeltaReportBuilder.build(ref, platform, comps, anomalies)` invites argument-order mistakes in new tests — prefer keywords.

---

## tests/test_strategy_meta_adapters.py

Scanned in full (505 lines). Good end-to-end coverage of the vectorbt adapter through real parquet fixtures; several findings on magic expected values and repo-wide scans inside unit tests.

**(a) Bugs / correctness risks**

1. **tests/test_strategy_meta_adapters.py:104-107 — major — exact-size assertions re-derive the production sizing math with unexplained magic numbers.**
   `trade.quantity == 142.8571` and `trade.pnl == -142.86` encode `risk_target_usd(1000) / (entry 100 − sl 95) = 200` … but the asserted value is 142.8571 = 1000/7, i.e. entry 102 − sl 95. The test passes today only because the adapter's actual fill price is bar-2 close 102, not the signal-bar price — a subtle intrabar-fill semantic. If anyone changes fill timing or rounding, this fails with no hint of *why* 142.8571. At minimum assert `pytest.approx` and document the derivation (`1000 / (102−95)`); as written it's an oracle nobody can re-derive from the test body.
2. **tests/test_strategy_meta_adapters.py:106-107, 329 — minor — float equality without tolerance.** `== -142.86`, `== 39.6` compare floats exactly; safe for these fixed inputs but fragile to any refactor of order-of-operations. Use `pytest.approx`.
3. **tests/test_strategy_meta_adapters.py:180-196 — minor — repo-wide scan inside a single unit test.**
   `test_all_vectorbt_strategies_return_entries_exits_and_indicators_dict` iterates every strategy file in the live strategies dir via `iter_strategy_files(DEFAULT_STRATEGIES_DIR)`. It silently skips `pynecore_*` files by name-prefix convention (:184) and will pass vacuously if the strategies dir is empty or relocated — nothing asserts at least one strategy was scanned. Add `assert scanned > 0` and report the count.
4. **tests/test_strategy_meta_adapters.py:28-32 — minor — `_load_module` never registered in `sys.modules`.**
   Loading strategy modules by `spec_from_file_location` + `exec_module` without inserting into `sys.modules` breaks modules that use dataclasses/pickling or relative imports; fine today, worth noting since helper is generic-looking.
5. **tests/test_strategy_meta_adapters.py:20, 23-24 — minor — module-level `sys.path.insert` + dead constant.** `STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR` adds an alias used once; `sys.path.insert(0, ...)` at import time mutates global interpreter state for all tests in the session (project root already on path under pytest).

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (minor)**: the "write strategy file + mkdir ohlcv/test_source/equities/AAPL + write 1d.parquet + run VectorbtAdapter" scaffolding repeats ~6× (:52-119, :122-176, :199-253, :256-329, :332-401); extract helpers.
- **Mysterious Name (nit)**: file named `test_strategy_meta_adapters.py` but tests cover vectorbt/pynecore engine adapters broadly; name undersells scope.
- **Repeated Switches (nit)**: per-test `if len(output) == 4` / prefix checks on strategy files duplicate adapter-side conventions inline.

**(d) Maintainability hot spots**: finding 1's magic numbers are the main trap; secondarily any rename of the strategies directory layout breaks six tests identically (shotgun surgery), favoring shared fixtures.

---

## tests/validation/test_regression.py

Scanned in full (476 lines). Solid behavioral coverage of golden-file regression incl. tolerances, empty goldens, missing files.

**(a) Bugs / correctness risks**

1. **tests/validation/test_regression.py:87-101 — minor — count assertion compares against a hand-copied field list instead of the source of truth.**
   `len(result.stats_delta)` is compared to the length of an inline literal list of 10 field names duplicated from production. If production adds an eleventh metric the test fails confusingly (good), but the list itself can rot independently of what `_compare_stats` actually emits — asserting against `RegressionConfig`'s tracked-field definition (or just specific fields) would be less duplicative.
2. **tests/validation/test_regression.py:389-391 — minor — relative fixture path depends on CWD.**
   `Path("tests/fixtures/validation/mt5_html/sample_report.html")` resolves against the process working directory, not the repo root; running pytest from another directory silently trips the `pytest.skip`, hiding coverage. Use `Path(__file__).resolve().parents[2] / ...`.
3. **tests/validation/test_regression.py:460-468 — minor — infinity-vs-infinity comparison asserted passed without pinning semantics.**
   `test_compare_stats_infinity_both` verifies inf==inf passes, which is correct for regression equality, but the test doesn't guard the *sign* of infinity (`-inf` vs `inf` would also be "equal" under some naive implementations). A `-inf` case would strengthen it.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (major, volume)**: the 4-line block `ref_trades=[...]; act_trades=[...]; golden=_make_reference(...); _write_golden(golden, golden_path); result=run_regression(...)` appears verbatim ~20× across classes; a parametrized helper would halve the file. This is the dominant maintenance tax.
- **Mysterious Name (nit)**: `TestImporterAndEdgeCases.test_regression_run_with_importer` both exercises the importer path *and* asserts round-trip identity; split naming would clarify.

**(d) Maintainability hot spots**: duplication above; otherwise healthy — good use of tmp_path, explicit failure-path assertions, and tolerance boundary tests (pass/fail pairs at :307-369 are exemplary).

---

## tests/validation/test_percent_risk_model.py

Scanned in full (461 lines). Strong parametrization of validation errors, boundary tests for rounding/clamping, and adapter-integration checks.

**(a) Bugs / correctness risks**

1. **tests/validation/test_percent_risk_model.py:245-252 — minor — test name contradicts asserted behavior.**
   `test_min_position_size_clamping_zero` sets `min_position_size=200.0` and asserts size clamps to **0.0** (not 200). The comment (:248) says "below min 200 -> clamped to 0", so zeroing-out is evidently intended semantics ("can't take a position that small"), but the *name* says "clamping" while the config value 200 never appears in the assertion — a reader can't tell whether min means "floor" or "veto threshold". Rename (e.g. `test_below_min_position_size_yields_zero`) and add the missing case: a raw size between 0 and min where clamping-to-min vs veto differ.
2. **tests/validation/test_percent_risk_model.py:310-331 — major — test verifies nothing about meta extraction; its own comments admit it.**
   `test_validate_extracts_take_profit_for_risk_reward` builds `inp` with `take_profit=None`, never calls `validate(inp)` on it, then builds a second input pulling TP from meta *in the test itself* (`trade.meta.get("take_profit")`) and sizes that. The name promises "validate extracts take profit"; the body proves only that RR computes when TP is supplied. The docstring block at :320-322 even concedes "meta extraction is for stop_loss only… that's adapter-level". This is a misleading test: it gives false confidence that the model extracts TP from trade.meta when it does not.
3. **tests/validation/test_percent_risk_model.py:208-214 — minor — `test_round_lots_enabled` exercises no rounding.**
   Raw size 50 is already a multiple of lot_size 10, so the floor-rounding branch never executes; the disabled-case test at :217 covers it. The enabled test should use a non-multiple (like :221 does).
4. **tests/validation/test_percent_risk_model.py:374-379 — minor — sentinel conflation.** `test_trade_id_zero_when_no_trade` blesses `0` as the "no trade" ID; if Trade ids are 1-based this silently collides with a real trade id 0. Minor, but worth an explicit sentinel constant.

**(b) Security** — nothing found; validation-error coverage (zero/negative/None/inf balance :143-172, NaN entry :396, inf SL :403) is exactly right for a sizing model.

**(c) Baseline smells**

- **Duplicated Code (minor)**: `PercentRiskConfig(risk_percent=0.01)` + `PercentRiskModel(config)` rebuilt in ~25 tests; a module fixture would cut noise.
- **Mysterious Name (nit)**: section banner "Meta extraction" (:281) contains one test that doesn't extract from meta (finding 2).

**(d) Maintainability hot spots**: low.

---

## tests/validation/test_trade_anomalies.py

Scanned in full (424 lines). High-quality behavioral suite: severity taxonomy per field covered exhaustively, duplicate detection both directions, integration paths. No bugs found.

**(a) Bugs / correctness risks** — nothing found. Assertions are specific (field names inside messages, severity per anomaly class, exact counts).

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Dead imports (minor)**: `FieldComparisonResult`, `TradeComparisonConfig`, `TradeComparisonResult` imported at :14-17 but never referenced in the module body (only `TradeComparator`'s return value is used). Dead code suggesting copy-paste from comparison-test files.
- **Duplicated Code (nit)**: `_make_trade`/`_make_matched` duplicated verbatim from test_delta_report.py / test_report_structured.py; move to shared conftest.

**(d) Maintainability hot spots**: none beyond the duplication above.

---

## tests/test_walkforward.py

Scanned in full (421 lines).

**(a) Bugs / correctness risks**

1. **tests/test_walkforward.py:18-45 — major — signature-introspection tests assert API shape, not behavior.**
   `TestWalkforwardSignature` verifies parameter *names* and *default values* of `run_walkforward` via `inspect.signature`. These tests break on any legitimate rename or default change (e.g. n_trials 100→200 as a product decision) while catching no behavioral regression. Defaults-as-contract is defensible for a CLI-facing entry point, but then the test should say so; today it's a pure change-detector. Also `strategy`, `engine`, `backtest_ids` are asserted present simultaneously although the two calling modes appear mutually exclusive (:64 vs :395).
2. **tests/test_walkforward.py:54-205 — minor — heavy mocking leaves fold construction untested in most tests.**
   Every `TestWalkforwardOutputStructure`/`SummaryMetrics`/`FileOutput` test monkeypatches `optimize_portfolio_allocation`, whose mock returns OOS Sharpe/drawdown directly. The suite validates pass-through aggregation and file writing, but actual IS/OOS split math is covered only by the single `_FakeEngine` test (:343-421, which is good). Regressions in backtest_ids-mode fold slicing would go unnoticed.
3. **tests/test_walkforward.py:297 — minor — test encodes a known limitation as expected behavior.** Inline comment admits "Current implementation has single fold"; asserting it makes the future fix require editing the test, and the name (`test_walkforward_multiple_backtests`) implies real multi-backtest coverage.
4. **tests/test_walkforward.py:51, 93, 120, 142, 164, 186 — nit — `tmp_path` fixture requested and unused** in ~8 tests that never touch the filesystem.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Speculative Generality / Duplicated Code (minor)**: `mock_optimize` redefined identically 8×; extract a helper returning canned payloads.
- **Mysterious Name (nit)**: `test_walkforward_output_has_all_required_fields` checks only 3 of the fields its own docstring claims ("all required fields").

**(d) Maintainability hot spots**: the two-mode entry point (`backtest_ids=` vs `strategy=`+engine) means tests must pick a mode; the suite leans almost entirely on backtest_ids-with-mocks, leaving strategy mode with exactly one test. Rebalance.

---

## tests/validation/test_trade_comparison.py

Scanned in full (417 lines). Exemplary tolerance-boundary suite: every field gets within/outside/custom-tolerance triads plus None-vs-float semantics, with OR-tolerance logic documented inline.

**(a) Bugs / correctness risks**

1. **tests/validation/test_trade_comparison.py:281-291 — minor — copy-paste error in test name: says "mismatch", asserts a pass.**
   `test_zero_vs_nonzero_pnl_mismatch` ends with `_assert_field_passed(result, "pnl")` and `assert result.is_match`. The body comments correctly explain that OR-tolerance lets |0.001| ≤ 0.01 pass — so the *assertion* is right and the *name* is wrong (left over from a pre-OR version or copied from :253). Anyone auditing failures by test name will wrongly believe zero-vs-nonzero PnL fails validation. Rename to e.g. `test_zero_vs_nonzero_pnl_passes_on_abs_tolerance`.
2. **tests/validation/test_trade_comparison.py:262-272 — nit — same class of confusion milder:** `test_pnl_outside_abs_tolerance_only` also passes despite its name suggesting failure territory; comments mitigate. Consider renaming both to describe the outcome, not the input condition.
3. **tests/validation/test_trade_comparison.py:83 — nit — hardcoded field count** `len(result.field_results) == 10` duplicates production knowledge; adding an eleventh compared field breaks this test even when everything else is consistent. Prefer asserting the exact key set (which doubles as documentation).

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Dead import (minor)**: `math` imported at :5, never used.
- **Duplicated Code (nit)**: `_make_trade`/`_make_matched` are the Nth verbatim copies across the validation suite (see recurring note below).

**(d) Maintainability hot spots**: none significant; this is one of the strongest files in the chunk.

---


## tests/validation/test_report_structured.py

Scanned in full (439 lines). Good structural coverage incl. strict-JSON sanitization (inf/nan → null), precomputed-delta injection paths.

**(a) Bugs / correctness risks**

1. **tests/validation/test_report_structured.py:231-245 — minor — tautological assertion pads the summary test.**
   `assert trade_summary["errors"] + trade_summary["warnings"] >= 0` can never fail for numeric counts; delete or assert concrete expected values.
2. **tests/validation/test_report_structured.py:427-439 — minor — misleading comment about strict JSON verification.**
   Comment says "Strict parser (json.loads rejects NaN/Infinity only with parse hooks, so assert via allow_nan=False round-trip instead)" — but `json.loads` *accepts* NaN/Infinity by default and the actual guarantee comes from `_sanitize_json_numbers` + the `json.dumps(..., allow_nan=False)` re-serialization (:439). The final `json.dumps(data, allow_nan=False)` has its return value unused; if sanitization regressed to emit bare `NaN` tokens, `json.loads(json_str)` at :438 would succeed and only :439 would catch it — fine, but the comment misstates Python JSON semantics and could mislead future edits.
3. **tests/validation/test_report_structured.py:337-341 — minor — over-broad exception match.**
   `pytest.raises((TypeError, AttributeError))` for a string platform argument accepts either failure mode; whichever it is today, the test will keep passing even if the error type drifts to an accidental AttributeError deep inside serialization rather than an upfront type check. Prefer asserting the specific contract.
4. **tests/validation/test_report_structured.py:269-278 — minor — undocumented equivalence claim.**
   `test_platform_result_as_validation_reference` shows a ValidationReference is accepted in place of a BacktestResult; nothing in the test names that this is intentional API flexibility. A one-line comment would prevent someone "fixing" it.

**(b) Security** — nothing found; the strict-JSON sanitizer tests (:405-439) actively guard against malformed report output (a downstream-consumer hazard).

**(c) Baseline smells**

- **Duplicated Code (minor)**: `_make_trade`/`_make_reference`/`_make_platform_result` triplicated across test_delta_report.py, test_report_structured.py, test_paper_readiness.py with tiny variations — candidate for a shared `tests/validation/conftest.py`.
- **Dead import risk (nit)**: `TradeStatistics` imported at :32 alongside `compute_trade_statistics`; `TradeStatistics` is used only in `test_with_precomputed_stats_delta` via `StatDelta` — actually unused? `StatDelta` is imported from json_report; `TradeStatistics` appears unused in the file body (only `compute_trade_statistics` would be needed). Verify with lint.

**(d) Maintainability hot spots**: low.

---

## tests/validation/test_trade_statistics.py

Scanned in full (413 lines). Excellent numeric suite: hand-computed expectations with shown arithmetic (drawdown sequences, Sharpe/Sortino derivations), edge cases for inf/zero/single-trade.

**(a) Bugs / correctness risks**

1. **tests/validation/test_trade_statistics.py:243-257 — minor — Sharpe expectation re-implements the production formula rather than an independent value.**
   `expected = (20.0 / math.sqrt(2680.0)) * math.sqrt(252.0)` mirrors the calculator's exact expression; a shared mistake (e.g. wrong annualization factor or population-vs-sample std) cancels out and the test still passes. The comment's ≈6.133 sanity value mitigates. Same pattern at :275-289 for Sortino. Prefer at least one test with a literal precomputed constant.
2. **tests/validation/test_trade_statistics.py:185-195 — minor — drawdown-pct test underdetermined.**
   `test_max_drawdown_pct` asserts 0.7, which equals both `70 / initial-peak 100` and `70 / running peak` only because the peak is the first trade. A sequence where the max DD occurs after a *higher* peak is covered by :206-219 (good), but the pct-denominator convention (peak equity vs initial capital) should be pinned in one more asymmetric case to prevent silent convention changes.
3. **tests/validation/test_trade_statistics.py:198-203 — nit — name says "never positive equity"** while the scenario is "always losing from zero start"; the pct==0 assertion documents a /0-guard, worth a comment reference to production behavior.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (minor)**: the 5-trade `[100, -50, 30, -20, 40]` fixture duplicated between Sharpe (:248-254) and Sortino (:280-286); extract once.

**(d) Maintainability hot spots**: low.

---

## tests/test_trade_engine.py

Scanned in full (368 lines). Strong execution-model coverage: next-bar-open fills, bar-low stops, SL-alias, stop-distance anchoring, same-bar TP priority — all against synthetic deterministic OHLCV.

**(a) Bugs / correctness risks**

1. **tests/test_trade_engine.py:324-368 — major — engine-parity test is environment-dependent, skip-heavy and weakly asserted.**
   `test_example_sma_native_vs_vectorbt_trade_count_similarity` depends on (a) a repo-relative `strategies/example_sma/example_sma.py`, (b) *local user data* under `DATA_DIR/ohlcv` with 2024–2025 coverage. On any machine without that data it silently skips (`pytest.skip` ×3) — in CI the entire parity guarantee can be vacuous while the suite reports green. Where data exists, the final assertion allows ±30% (or ±2 trades) divergence between engines (:367-368), i.e. two engines may disagree on ~30% of trade counts and still pass; and the both-zero path asserts literal `True` (:364). This is a smoke test wearing a correctness-test name. At minimum mark `skipif` explicitly with reason strings surfaced in CI summaries, tighten tolerance, and pin a bundled fixture dataset.
2. **tests/test_trade_engine.py:51-66 — minor — broad `except Exception` swallows real errors as "no coverage".**
   `_time_bounds` converts any parquet read failure into `(None, None)` → "no coverage" → skip. A corrupt fixture or schema drift thus masquerades as missing data instead of failing loudly. Catch specific pyarrow errors.
3. **tests/test_trade_engine.py:18-48 — minor — ~30 lines of production-grade date-parsing helpers re-implemented inside the test module** (`_parse_date_bound` end-of-day adjustment, tz coercion). If this logic mirrors production filtering code, it's duplicated logic that can drift from the real implementation (tests verifying themselves); if it doesn't mirror, it's untested dead weight. Either import production helpers or trim to what tests need.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (minor)**: the 10-line `BacktestConfig(...)` construction repeated 5×; extract builder.
- **Mysterious Name (nit)**: `test_trade_engine_stop_uses_bar_low_and_risk_sizing_1pct` — good name; but sibling :198 duplicates it almost entirely via the alias path; parametrize the two entry conventions instead of copy-paste.

**(d) Maintainability hot spots**: low.

---

## tests/validation/test_fixed_risk_model.py

Scanned in full (366 lines). Mirrors the percent-risk suite; good budget-boundary test (`round_lots` must never exceed risk budget).

**(a) Bugs / correctness risks**

1. **tests/validation/test_fixed_risk_model.py:158-165 — major — test name says "rounds up" but the asserted behavior rounds DOWN.**
   `test_round_lots_rounds_up` computes raw = 33.33 and asserts 30 (nearest-lower multiple of 10), with the inline comment itself saying "rounds to 30 (nearest multiple)". If production ever switched to true round-half-up (33.33→40, breaching budget) this test would rightly fail — but as named, a future maintainer "fixing" rounding to match the name would break the safety property the sibling test :86-97 exists to enforce. Misleading name on a money-touching code path.
2. **tests/validation/test_fixed_risk_model.py:86-97 — minor — self-acknowledged confused derivation in `test_lot_rounding_never_exceeds_risk_budget`.**
   The comment block (:87-89) narrates abandoned alternatives ("250 ... 260 lots? Instead pick...") before settling on stop_distance=0.385. The final math checks out (259.74 → floor 259, risk 99.715 ≤ 100) but the reasoning-as-committed is noise; also note it depends on floor semantics while the neighboring test name claims round-up — the two tests together pin *floor*, so rename both consistently ("floors to lot multiple").
3. **tests/validation/test_fixed_risk_model.py:149-155 — nit — same vacuous-rounding case as the percent-risk file:** raw 50 already a lot-multiple; rounding branch untested here despite the dedicated file for it (covered at :158).
4. **tests/validation/test_fixed_risk_model.py:188-195 — minor — same naming contradiction as percent-risk:** `test_min_position_size_clamping_zero` clamps to 0 with min=50 never asserted as such; add the between-0-and-min distinguishing case.

**(b) Security** — nothing found; NaN/inf validation coverage (:275-286) is right for sizing code.

**(c) Baseline smells**

- **Duplicated Code (minor)**: near-identical twin of test_percent_risk_model.py (~60% shared structure); a parametrized shared harness over both models would eliminate ~150 lines and prevent drift between the two suites.
- **Dead comment (nit)**: :242-244 explains a field that doesn't exist (`stop_distance_pct`); fine, but reads like a TODO.

**(d) Maintainability hot spots**: low.

---

## tests/validation/test_cli.py

Scanned in full (353 lines). Real end-to-end CLI coverage via subprocess: exit codes 0/1/2, JSON/markdown formats, importer auto-detection, tolerance flags. Good contract testing.

**(a) Bugs / correctness risks**

1. **tests/validation/test_cli.py:50-57 — minor — subprocess without timeout (fixed-checklist item).**
   `_invoke_cli` calls `subprocess.run(...)` with no `timeout=`; a hang in the CLI (e.g. waiting on stdin, deadlock in report writing) blocks the suite indefinitely instead of failing the test. ~15 invocations across the file. Add `timeout=60`.
2. **tests/validation/test_cli.py:237-250 — minor — exit-code-2 test accepts three different error-message shapes via `or` chain.**
   The stderr assertion tolerates "not found" OR "failed to load reference" OR "no such file or directory" — i.e. it passes whether the CLI handles missing files itself or lets an unhandled OSError traceback through (a traceback would contain "No such file or directory"). That distinction is exactly what this test should pin down. Tighten to the intended message.
3. **tests/validation/test_cli.py:109 — nit — brittle markdown-format probe.**
   `"Overall Passed**:** Yes" in ... or "Overall Passed**: Yes" in ...` encodes two specific markdown renderings with an OR fallback; any third rendering silently breaks it while neither branch documents which is canonical.
4. **tests/validation/test_cli.py:144-190, 290-342 — minor — same file used as both reference and platform.** Legitimate for self-comparison smoke tests, but note these never exercise reference≠platform divergence through importers; divergence is covered only on the JSON path (:203-287).

**(b) Security** — nothing found; no shell=True, arguments passed as list, cwd pinned to repo root. Good practice.

**(c) Baseline smells**

- **Duplicated Code (minor)**: ref/platform JSON-writing block repeated 3× (:211-214, :262-265, :241-242); extract helper.
- **Mysterious Name (nit)**: `TestCliMissingTradeExitCode1` also asserts payload contents; fine.

**(d) Maintainability hot spots**: each CLI run spawns an interpreter (~0.5–1s ×15) — acceptable but worth noting for suite latency; batch or use in-process runner for pure-parse cases if it grows.

---

## tests/validation/test_risk_sizing.py

Scanned in full (351 lines). Good coverage of signal conversion, fallback-stop semantics (incl. ATR meta), direction enforcement, exposure caps, and aggregate risk. No bugs found — assertions are specific and hand-computed.

**(a) Bugs / correctness risks** — nothing found.

Minor notes only:
1. **tests/validation/test_risk_sizing.py:292-324 — nit — two constructor round-trip tests** (`test_trade_risk_profile_model_creation`, `test_risk_sizing_result_model_creation`) re-assert values passed in; marginal.
2. **tests/validation/test_risk_sizing.py:22, :18-19 — nit — imports production privates** (`_trade_to_signal`) directly; acceptable for unit scope but pins an internal name.

**(b) Security** — nothing found; wrong-side stop rejection (:200-207) and zero-balance guard (:210-214) are exactly the checks sizing code needs tested.

**(c) Baseline smells**

- **Duplicated Code (minor)**: `RiskAdapter(pip_value=1.0)` + `_make_trade(...)` scaffolding in ~20 tests; fixture would help.
- **Primitive Obsession (nit)**: direction strings "LONG"/"SHORT"/"buy"/"sell" asserted as raw strings throughout; a shared enum/constant set would catch typo drift (e.g. "Buy" vs "buy") at one place. Not enforced anywhere here.

**(d) Maintainability hot spots**: none significant.

---

## tests/validation/test_markdown_report.py

Scanned in full (312 lines). Reasonable structural coverage of markdown rendering incl. both input modes (ref+platform vs pre-built ValidationReport).

**(a) Bugs / correctness risks**

1. **tests/validation/test_markdown_report.py:125 — minor — copy-paste tautology in OR assertion.**
   `assert "Overall Passed**: Yes" in md or "Overall Passed**: Yes" in md` — both sides of the `or` are the identical string, so the `or` is dead and the second variant (presumably intended as `"Overall Passed**:** Yes"`, cf. test_cli.py:109 which has two distinct variants) was lost to copy-paste. Same pattern at :223 with `"Overall Passed**: No"` twice. Either restore the intended alternates or drop the `or`.
2. **tests/validation/test_markdown_report.py:275-285 — minor — negative assertions too narrow to be meaningful.**
   `test_no_internal_python_objects_in_markdown` checks only three literal substrings (`<object`, `datetime.datetime`, `Decimal(`). A repr of any other type (`Trade(...)`, `nan`, memoryview) sails through. The JSON sibling test (test_report_structured.py:369-388) does recursive type checking — reuse that approach over the markdown body's non-prose tokens, or accept the test is a spot check and rename it.
3. **tests/validation/test_markdown_report.py:160, 162, 175 — nit — broad lowercase substring probes** ("mismatch" or "differs") accept either wording; fine for smoke but won't catch regressions to unhelpful text.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (major by count)**: the 5-line ref/platform setup block repeats verbatim in all ~22 tests; one parametrized fixture would shrink file by half. Strongest duplication candidate in this group.
- **Duplicated Code across files (minor)**: `_make_trade`/`_make_reference`/`_make_platform_result` again near-verbatim from test_report_structured.py.

**(d) Maintainability hot spots**: low; formatting-string probes (`- **Symbol**`) will churn with any cosmetic renderer change — consider asserting semantic content via the JSON report instead of markdown punctuation.

---

## tests/validation/test_risk_comparison.py

Scanned in full (297 lines). Well-designed isolation tests: each mismatch class (stop distance / risk-per-trade / position size) verified to fail *only its own field* while the others stay green — good negative-space coverage. Delta-report conversion covered incl. missing/extra.

**(a) Bugs / correctness risks**

1. **tests/validation/test_risk_comparison.py:183-199 — minor — test name says "extra is info" and asserts `report.passed is True`, but the sibling missing-case (:163-179) fails the report for a *warning*.**
   The asymmetry itself may be intended policy (extras are informational, missing are warnings that block) — but nothing in either test states it, and `TestExtraTrade.test_extra_trade_is_info` asserting overall pass couples severity taxonomy to gate semantics without comment. If someone later decides extras should also warn, this test's name gives no hint why it flips. Add a one-line policy comment.
2. **tests/validation/test_risk_comparison.py:96-115 — minor — tolerance knobs set ad hoc per test obscure boundary semantics.**
   `stop_distance_pct=0.5` etc. chosen per-test to isolate fields; values like 0.5 (50%!) as "tight enough to fail 80%-vs-10% stop distance but loose elsewhere" are correct here but unexplained; a table-driven parametrize with expected per-field outcomes would document the decision matrix.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (minor)**: t1/t2/ref1/ref2/config/comparator/report 6-line prologue repeated in all ~11 tests.
- **Data Clumps (nit)**: `(stop_distance_pct, risk_per_trade_pct, position_size_pct)` always adjusted together; bundle into a named scenario type or parametrize tuple.

**(d) Maintainability hot spots**: none significant.

---

## tests/validation/test_mt5_xlsx_importer.py

Scanned in full (274 lines). Real-data golden tests against three tracked xlsx exports (verified present & git-tracked) plus unit tests of private parsing helpers incl. timezone conversion. Good.

**(a) Bugs / correctness risks**

1. **tests/validation/test_mt5_xlsx_importer.py:15-24 — minor — relative fixture paths depend on CWD, and absence yields `FileNotFoundError` failures rather than skips.**
   Paths like `strategy-comparisons/intraday_volatility_breakout/EURUSD_H1.xlsx` resolve against the process CWD (fine under repo-root pytest, silently broken otherwise). Unlike test_regression.py:390 there's no existence check/skip guard: if the multi-MB xlsx artifacts are ever moved to LFS or a light checkout, every test in `TestEURUSDH1` errors noisily instead of skipping cleanly. Add a module-level `pytestmark = pytest.mark.skipif(not _eurusd_path().exists(), ...)` or anchor via `Path(__file__)`.
2. **tests/validation/test_mt5_xlsx_importer.py:98, 168 — minor — exact-count golden (`total_trades == 492`) duplicated as both literal and cross-check.**
   :98 pins 492; :168 asserts `len(trades) == stats["total_trades"]`. The cross-check is the valuable invariant; the literal breaks on any re-export of the workbook even when parsing is correct. Acceptable for goldens but worth knowing which is load-bearing.
3. **tests/validation/test_mt5_xlsx_importer.py:188-197 — minor — dead branch structure in consistency test.** The `if direction == "LONG": pass else: pass` block does nothing; only the final volume equality is asserted. Either drop the branch or assert the actual in/out side semantics the comment promises ("buy in, sell out") — currently untested.
4. **tests/validation/test_mt5_xlsx_importer.py:67-70 — nit — `startswith("2")` year probe** is brittle-by-construction; trivial.

**(b) Security** — nothing found; timezone handling tested properly (Athens→UTC :257-264, invalid zone rejected :266-274).

**(c) Baseline smells**

- **Performance/Duplicated work (minor)**: every test calls `importer.load(_eurusd_path())` afresh (~20 parses of the same workbook per run); a module-scoped cached fixture would cut suite time substantially.
- **Feature Envy (nit)**: `TestHelpers`/`TestSourceTimezone` exercise private statics (`_coerce_setting`, `_to_iso`, `_infer_exit_reason`) directly — acceptable white-box choice, but binds tests to private API.

**(d) Maintainability hot spots**: CWD-relative fixtures (finding 1).

---

## tests/validation/test_mt5_html_importer.py

Scanned in full (204 lines). Mirrors the xlsx importer suite against the tracked HTML fixture; helper unit tests included.

**(a) Bugs / correctness risks**

1. **tests/validation/test_mt5_html_importer.py:15-16 — minor — CWD-relative fixture path, same issue as the xlsx file.** `Path("tests/fixtures/...")` resolves against CWD; anchor with `Path(__file__).resolve().parents[2]` (as test_cross_platform_metrics.py:13 does correctly).
2. **tests/validation/test_mt5_html_importer.py:48-50 — nit — `len(ref.trades) >= 5` is a weak golden bound;** the xlsx sibling pins exact counts. Fine for a small fixture, but an exact count would catch row-loss regressions (e.g. dropped duplicate rows).

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (minor)**: `TestHelpers` duplicates `_coerce_setting`/`_extract_first_percent`/`_parse_count_pct`/`_infer_exit_reason` tests nearly verbatim from test_mt5_xlsx_importer.py:222-247; if both importers share helper implementations, one shared parametrized test module would prevent divergence.
- **Duplicated work (nit)**: ~20 fresh parses of the same HTML per run; module-scoped fixture would help.

**(d) Maintainability hot spots**: low.

---

## tests/validation/test_cross_platform_metrics.py

Scanned in full (219 lines). Integration-style matrix tests over repo artifacts; correct path anchoring (`Path(__file__).resolve().parents[2]`) and containment assertions (:46-48).

**(a) Bugs / correctness risks**

1. **tests/validation/test_cross_platform_metrics.py:30-31, 42 — major — hard dependency on mutable repo working-tree data (`strategy-comparisons/`, `reports/`, `data/backtests/*.json`).**
   `reference.trade_count == 512`, `win_rate ≈ 0.4492`, `sharpe == 0.544687` pin values from artifact files that live outside `tests/fixtures` and are regenerated by scripts (`regenerate_*`, imported at :8-9). Any legitimate regeneration that shifts a metric breaks these tests; conversely the tests silently pass on machines where stale-but-present artifacts happen to match. This is a golden-file pattern without golden-file hygiene: no skip guard, no checksum, no documented refresh procedure in-test. Reference the regenerating script in a comment and consider copying fixtures into `tests/fixtures`.
2. **tests/validation/test_cross_platform_metrics.py:77-106 — minor — recomputation test asserts only sign.**
   `platform["sharpe_ratio"] > 0.0` after seeding stats with `-1.0` proves recomputation happened but not that it's *correct*; assert an approx value derivable from the constructed equity series.
3. **tests/validation/test_cross_platform_metrics.py:8-9 — minor — tests import production scripts as modules** (`scripts.regenerate_mt5_parity_artifacts`), coupling test collection to script import side effects; acceptable but pins module paths.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (minor)**: the two QC-attribution tests (:131-166, :169-202) differ only in two stat values; parametrize.
- **Hidden dependency (minor)**: reading `PROJECT_ROOT / "reports"` during tests means user-generated reports can perturb outcomes; the missing-artifact case (:51) correctly isolates via tmp_path — apply the same isolation elsewhere.

**(d) Maintainability hot spots**: finding 1 — most environment-coupled file in the validation suite.

---

## tests/test_strategy_ir_codegen_vectorbt.py

Scanned in full (144 lines). Determinism + contract tests for IR→vectorbt codegen, executing generated code in a sandbox namespace.

**(a) Bugs / correctness risks**

1. **tests/test_strategy_ir_codegen_vectorbt.py:63, :106, :141 — major — `exec()` of generated strategy code with no security gate in the test path.**
   The tests `exec` code produced by `generate_vectorbt_strategy_code` from repo-controlled IR fixtures — acceptable today since inputs are static — but the sibling validator (`tests/validators/code_validator.py`) exists precisely because generated/AI code must be treated as untrusted. If `_sample_ir` ever becomes LLM-derived or fixture-loaded from disk, these tests become an arbitrary-code-execution sink. Worth a guard comment or routing through the project's own `AICodeValidator` first.
2. **tests/test_strategy_ir_codegen_vectorbt.py:49-55 — minor — determinism test is weak.** Two calls in one interpreter compare equal; dict-ordering nondeterminism across processes/hash seeds wouldn't be caught. Consider asserting a hash of normalized output or comparing against a checked-in snapshot.
3. **tests/test_strategy_ir_codegen_vectorbt.py:76-87 — minor — generated `generate_signals` exercised but its signal semantics never asserted.** Only shape checks (`len == len(df)`); a codegen bug emitting always-false entries would pass. One assertion that entries fire on the crossover bar would pin semantics.

**(b) Security** — see finding 1; no other issues.

**(c) Baseline smells**

- **Speculative Generality (nit)**: `if len(output) == 4: assert isinstance(output[3], list)` (:86-87) asserts almost nothing about drawings content; fine as shape check only.

**(d) Maintainability hot spots**: low.

---

## tests/validators/code_validator.py

Note: this file is **production-style helper code living under `tests/validators/`**, not a pytest module (no test functions). Reviewed as code.

**(a) Bugs / correctness risks**

1. **tests/validators/code_validator.py:20-22, 48-60 — major — security blocklist is trivially bypassable, and nothing documents its threat model.**
   Forbidden set = bare `eval`/`exec` calls plus `os.system`. Bypasses that sail through: `getattr(builtins,'eval')(...)`, `builtins.eval`, `__import__('os').system`, `subprocess.Popen/run/call`, `os.popen`, attribute chains via variables, aliases, decorator tricks. As a *linter heuristic* this is fine; if any caller treats `passed=True` as "safe to import", it is a critical false-assurance bug. The subprocess-import check (:85-133) partially compensates (catches import-time side effects) but not call-time behavior. At minimum document "best-effort heuristic" and add `subprocess`, `os.popen`, `__import__`, `importlib` to the forbidden sets.
2. **tests/validators/code_validator.py:116-124 — good — subprocess has timeout=8 and `-I` isolated mode**; checklist item respected. Minor gap: stderr of a *successful* run becomes only a warning (:132-133), so a strategy emitting tracebacks on import still passes with −0.05 score.
3. **tests/validators/code_validator.py:135-137 — minor — warnings don't affect `passed`:** a module with 20 warnings scores 0.0 yet `passed=True`. If score gates anything downstream, document the interplay.

**(b) Security**

1. **tests/validators/code_validator.py:97-122 — minor — writes untrusted code to a temp dir and executes it in a subprocess.** Isolated mode `-I` is good, but the child still runs with full user privileges; on machines without process isolation this is deliberate arbitrary-code execution. Acceptable *if* callers feed only first-party AI output; flagged because the file lives under `tests/` where that context may be forgotten.
2. No injection risk in `check_script` itself: module path passed as argv, not interpolated into the script string. Good.

**(c) Baseline smells**

- **Mysterious Name / Misplaced Code (minor)**: class `AICodeValidator` in `tests/validators/` is referenced by `tests/e2e/lab/test_conversion.py`, `tests/integration/test_strategy_pipeline.py`, and `tests/integration/test_ai_code_quality.py` (verified by grep) — so it *is* load-bearing, but it lives in a non-package directory with no `__init__.py`, imported via path hacks. It's production-grade validation logic that belongs in the main package; keeping it under `tests/` risks it being excluded from lint/type coverage of production code.
- **Primitive Obsession (nit)**: errors/warnings as raw strings; codes would enable downstream filtering.

**(d) Maintainability hot spots**: the blocklist-vs-bypass gap (finding 1) is the headline; add bypass regression tests if this validator is load-bearing.

---

## tests/validators/backtest_validator.py

Also production-style helper code under `tests/validators/` (no pytest tests). Reviewed as code.

**(a) Bugs / correctness risks**

1. **tests/validators/backtest_validator.py:46, :86-92 — major — `total_trades` fallback silently coerces bad values to 0.**
   When `stats["total_trades"]` exists but is non-numeric (`None`, `"12abc"`), `_as_int` returns 0 → misleading error "Trade count below expectation: 0 < N" instead of "invalid total_trades". Also the default branch counts `len(result["trades"])` while stats may report a different canonical count — two sources conflated without documented precedence.
2. **tests/validators/backtest_validator.py:40-44 — minor — NaN/Inf scan covers only int/float.** String `"NaN"` or `Decimal('NaN')` stat values escape the finiteness check; the JSON round-trip hazard this guards against can re-enter via stringly-typed stats.
3. **tests/validators/backtest_validator.py:68-73, :102-113 — minor — mixed naive/aware timestamps crash instead of validating.**
   `_parse_time` returns naive datetimes for strings lacking tz and aware ones for `Z`/offset strings; a trade pair mixing formats makes the `<` comparison at :72 raise uncaught `TypeError`, aborting validation of the whole result instead of reporting an error for that trade. Normalize tz (see the `_to_utc_datetime` pattern used elsewhere in the suite).
4. **tests/validators/backtest_validator.py:77 vs code_validator.py:136 — nit — scoring formula duplicated** (same 0.2/0.05 weights); extract shared helper if both are used together.

**(b) Security** — nothing found (no eval/exec/subprocess; pure data inspection).

**(c) Baseline smells**

- **Shotgun Surgery (nit)**: `required_stats_keys` defaults hardcode runner.stats key names also asserted in tests/test_stats.py — two places per schema change.
- **Inconsistent contracts (nit)**: returns plain dict while sibling `AICodeValidator` returns typed `ValidationResult`.

**(d) Maintainability hot spots**: findings 1 & 3 are real correctness gaps for a validator whose job is catching bad results.

---

## tests/test_strategy_ir_routing.py

Scanned in full (82 lines). Clean routing-decision table tests: IR-generator vs legacy per engine/IR/rollout-mode, with reason-string checks. No bugs found.

**(a) Bugs / correctness risks** — nothing found. Assertions cover all four routing quadrants plus fallback gating.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Missing case (minor)**: no test for `rollout_mode="ir-only"` when the engine is *unregistered* or IR is missing — the mode most likely to need fail-closed behavior (`allow_legacy_fallback=False` + missing generator = what?). Add the two combinations.
- **Duplicated Code (nit)**: `resolve_codegen_route(...)` scaffolding repeated 5×; parametrize.

**(d) Maintainability hot spots**: low.

---

## tests/test_strategy_ir_builder.py

Scanned in full (79 lines). Markdown-section extraction, source-token fallback, unknown-engine hint. No bugs found.

**(a) Bugs / correctness risks**

1. **tests/test_strategy_ir_builder.py:7-43 — minor — single happy-path for markdown parsing.**
   Only one markdown shape is tested; section-order variations (Exit before Entry), missing sections, and non-German headings ("Kernlogik" hardcoded — is the parser locale-dependent?) are untested. If the parser matches German literals, that's a hidden coupling worth pinning or generalizing.
2. **tests/test_strategy_ir_builder.py:39 — nit — `confidence == 1.0` asserted only for the fully-populated doc;** partial-doc confidence degradation untested, so confidence scoring is effectively uncovered.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Mysterious Name (nit)**: `test_build_strategy_ir_falls_back_to_source_indicator_tokens` asserts names extracted from code tokens but not their params; name promises "fallback", scope is narrower.
- **Duplicated Code (nit)**: three near-identical `build_strategy_ir(...)` calls; a helper would help marginally.

**(d) Maintainability hot spots**: low.

---

## tests/test_strategy_ir_normalizer.py

Scanned in full (61 lines). Focused normalizer tests: markdown-title fallback, KV-string parsing, mixed tag merging, invalid-parameter rejection. No bugs found.

**(a) Bugs / correctness risks**

1. **tests/test_strategy_ir_normalizer.py:36-48 — minor — tag-merge semantics underdocumented by the assertion.**
   `{"labels": ["fx", "momentum"]}` shows a scalar tag string is appended to an existing list, but nothing pins what happens for two scalars of the *same* key (dedupe? last-wins? list-join?) or dict-vs-dict conflicts. One more case would prevent silent policy changes in merge order.
2. **tests/test_strategy_ir_normalizer.py:51-61 — minor — only one invalid shape covered.** `parameters=[123]` raises; `parameters="fast"`, `parameters=[["a",1]]`, tags with non-str values are untested; the error-message regex pins message format (good).

**(b) Security** — nothing found.

**(c) Baseline smells** — none significant; file is clean and appropriately sized.

**(d) Maintainability hot spots**: low.

---

## tests/test_signal_processor_registry.py

Scanned in full (67 lines). Registry completeness + behavioral differentiation between processors. Assertions are specific (`exit_only` flag, SL presence/side).

**(a) Bugs / correctness risks**

1. **tests/test_signal_processor_registry.py:35-43 — minor — "stockkit" asserted to produce `RamJetSignalProcessor`.**
   `get_signal_processor("stockkit", {})` returning a `RamJetSignalProcessor` looks like either intentional aliasing or a copy-paste registry bug blessed by this test. Nothing comments which. If aliasing is intended, assert it explicitly (`get_signal_processor("ramjet", {})` *is* the same instance/class contract); if not, it's a production bug hidden in plain sight.
2. **tests/test_signal_processor_registry.py:46-58 — minor — variant test depends on synthetic-data coincidence.**
   `float(long_short_signal.stop_loss) > long_short_signal.entry_price` asserts SHORT-stop-above-entry using whatever the processor derives from `_sample_market_data`; no expected value pinned. A sizing-formula change that alters magnitude but not sign passes silently.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (nit)**: processor-id set literal duplicated between :32 here and test_viewer_features.py:3292; extract a shared constant so both stay in sync.

**(d) Maintainability hot spots**: low.

---

## tests/test_signal_replay_adapter.py

Scanned in full (61 lines). Trailing-stop replay behavior with deterministic 4-bar fixture. No bugs found; assertions specific (exit_reason, meta propagation, update count).

**(a) Bugs / correctness risks**

1. **tests/test_signal_replay_adapter.py:55-61 — minor — trailing-stop path asserted only via outcome markers.**
   `trailing_stop_updates >= 1` and `exit_reason == "trailing_stop"` confirm the mechanism ran, but the actual stop level at exit is never asserted against the percent-config math (1% of running high). A bug ratcheting the stop in the wrong direction could still produce these markers. Assert final stop value or exit price ≈ expected.
2. **tests/test_signal_replay_adapter.py:20-35 — nit — `take_profit=140.0` (40% away) is fine as "never triggers",** but a comment saying so would prevent someone "fixing" it.

**(b) Security** — nothing found.

**(c) Baseline smells** — none significant.

**(d) Maintainability hot spots**: low.

---

## tests/test_signal_import.py

Scanned in full (50 lines). CSV signal parsing incl. NY→UTC conversion and German decimal commas. Good tz-aware assertion (:29-30).

**(a) Bugs / correctness risks**

1. **tests/test_signal_import.py:30 — minor — DST correctness asserted for winter only.**
   Comment says "winter"; a July date would need EDT (hour == 13). Since signal timestamps drive backtest fills, one summer-date case would pin that the tz database is actually used (vs a hardcoded −5 offset). Cheap and valuable.
2. **tests/test_signal_import.py:33-50 — minor — comma-format variant tested without decimal commas,** so the two format branches differ in more than delimiters; the German-decimal path is exercised only via the semicolon format.

**(b) Security** — nothing found.

**(c) Baseline smells** — none significant.

**(d) Maintainability hot spots**: low.

---

## tests/test_yfinance_downloader.py

Scanned in full (41 lines). Single regression test: MultiIndex-column + named-index normalization into canonical OHLCV parquet.

**(a) Bugs / correctness risks**

1. **tests/test_yfinance_downloader.py:27 — major — mock discards the call contract; critical kwargs unasserted.**
   `monkeypatch.setattr(yf_dl.yf, "download", lambda **kwargs: payload)` accepts any kwargs and records nothing. A production change that stops passing `auto_adjust=False` (silently altering *price semantics* of stored data), or drops start/end, would pass this test unchanged. Capture kwargs and assert the financially meaningful ones (`auto_adjust`, `start`, `end`, `interval`).
2. **tests/test_yfinance_downloader.py:8-41 — minor — single happy-path test for an entire downloader module:** no coverage of empty results, network failures, multi-symbol, or intraday timeframes from this area.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Single-Case Coverage (minor)**: parametrize index shapes (RangeIndex, tz-naive index) to harden the reset_index regression this test pins.

**(d) Maintainability hot spots**: low.

---

## tests/test_timeframe_normalization.py

Scanned in full (14 lines). Alias→canonical mapping plus pass-through cases including empty string.

**(a) Bugs / correctness risks** — nothing found for what's covered.

**(b) Security** — nothing found.

**(c) Baseline smells**

1. **Missing cases (minor)**: uppercase input ("M1"), whitespace (" 1h "), tick/sec aliases (used elsewhere per viewer tests), None-handling are untested although other modules clearly rely on alias breadth (`m1` vs `1m` vs `M1` appear in viewer coalescing tests). Two-line additions with outsized value given how central timeframe canonicalization is to data joins.

**(d) Maintainability hot spots**: none.

---

## tests/validation/test_golden_files.py

Scanned in full (57 lines). Golden-file smoke suite over tracked JSON goldens (verified tracked earlier).

**(a) Bugs / correctness risks**

1. **tests/validation/test_golden_files.py:15-18 — minor — missing directory yields zero files rather than error.**
   `_list_golden_files` returns `[]` when GOLDEN_DIR doesn't exist; the ≥3 guard then fails with a clear message (good), but partial golden deletion down to exactly 3 passes all tests while coverage silently shrank. Acceptable given the guard.
2. **tests/validation/test_golden_files.py:28-45 — nit — loop-based tests report only the first failing file;** parametrize over files for per-file granularity.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (nit)**: each test re-reads and re-validates every golden; a module-scoped parsed fixture would parse once.
- **Weak key set (nit)**: required stats = only `{total_pnl, gross_profit}` (:52) though importer tests require far more keys; inconsistent bar.

**(d) Maintainability hot spots**: none.

---

## tests/validation/test_importers.py

Scanned in full (56 lines). Base-class contract + CWD-independence check (`monkeypatch.chdir(tmp_path)` :30 — excellent) + declared-timezone conversion.

**(a) Bugs / correctness risks** — nothing found. Notably this file anchors paths via `Path(__file__)` and explicitly tests CWD independence — covering the gap flagged in the xlsx/html suites.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Coverage note (nit)**: only MT5HTMLImporter gets load-path tests here; MT5XLSXImporter's subclass-ness is asserted but its loading is delegated to its own file (fine, noting the asymmetry).

**(d) Maintainability hot spots**: none.

---

## tests/test_stats.py

**(a) Bugs / correctness risks**

1. **tests/test_stats.py:87 — minor — assertion on inequality only.**
   `assert stats["max_drawdown_pct"] != 0  # QuantStats returns negative` pins the sign convention by accident-of-comment; it would equally pass for a positive drawdown or any non-zero garbage. Assert the sign (`< 0`) and an approx magnitude, since the comment claims a specific convention.
2. **tests/test_stats.py:99-105 — minor — p_value test asserts distribution membership, not the value.** `0 ≤ p < 0.05` verifies the pipeline produced *some* small p-value for this fixed return series; a regression that changed the test statistic (e.g. t → normal approx) could shift p within range and pass. Acceptable smoke level, but pair with one pinned literal.
3. **tests/test_stats.py:107-116 — minor — CAGR window asserted as a loose band (0.09–0.11)** where the inputs determine ~0.10 exactly under the documented formula; `math.isclose` against the computed expectation (as done properly at :145-146, :175-177) would be consistent.

**(b) Security** — nothing found. Datetimes are tz-aware UTC throughout (fixed-checklist item respected).

**(c) Baseline smells**

- **Inconsistent assertion rigor (minor)**: three styles coexist — exact equality (:52), tolerance bands (:61, :115), `math.isclose` with derived expectation (:146). Standardize; the isclose style is the strongest.
- **Dead import risk (nit)**: `datetime`/`timedelta` used; fine. No dead imports found.

**(d) Maintainability hot spots**: none significant.

---







## tests/validation/test_paper_readiness.py

Scanned in full (525 lines).

**(a) Bugs / correctness risks**

1. **tests/validation/test_paper_readiness.py:142-148 — major — `_make_risk_report` double-constructs results so `passed` can contradict its own contents.**
   The default branch evaluates `(results or [_make_risk_result()])` twice, constructing two *different* `RiskComparisonResult` objects: one stored in `trade_results`, another used only for `passed=all(...)`. With defaults both pass, so today they agree, but any future default that fails (or partial override) makes the report claim `passed=True` while containing failing results — and conversely. Compute `results = results or [_make_risk_result()]` once. Latent fixture bug that could hide validator bugs (fixtures hiding production bugs).
2. **tests/validation/test_paper_readiness.py:510-525 — minor — test name/docstring contradicts assertion.**
   `test_empty_inputs` docstring says "should be NOT_READY" while the code asserts `READY` (:521). The config explicitly disables all four requirements, so READY is plausibly correct behavior — meaning the docstring is wrong/stale, not the assert. Misleading either way.
3. **tests/validation/test_paper_readiness.py:380-406 — minor — config knobs in test may not bind to the check under test.**
   `ReadinessConfig(max_position_size_diff_pct=0.001, max_stop_distance_diff_pct=1.0)` is passed but the failure actually comes from the pre-baked `FieldComparisonResult(passed=False)`; the config values appear inert in this path (nothing asserts their effect). If the validator ignores them, the test cannot detect it — mild false confidence.
4. **tests/validation/test_paper_readiness.py:74-80 — minor — regression result fixture hardcodes `/tmp/golden.json`**, bypassing tmp_path hygiene; harmless here (never opened) but normalizing on `tmp_path` would keep the suite hermetic.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (minor)**: the same 7-line validate() call block with `regression/risk/stats` rebuilt per test appears ~14 times; a parametrized fixture would cut ~100 lines.
- **Speculative Generality (nit)**: `_make_stats` accepts `net_profit` that most callers leave default; fine, noted only.

**(d) Maintainability hot spots**: low; the fixture duplication is the main tax when `ReadinessValidator.validate` gains a parameter.

---

## tests/test_strategy_type_detection.py

Scanned in full (95 lines). Type detection (meta field, class pattern, default) plus CLI guard against running signal-processor strategies. No bugs found.

**(a) Bugs / correctness risks**

1. **tests/test_strategy_type_detection.py:24-39 — minor — class-pattern detection tested only via name substring.**
   `ExampleSignalProcessor` matches presumably by "SignalProcessor" in a class name; nothing pins whether *any* class containing the token counts, or method presence (`process_signal`) is required — both signals appear in the fixture simultaneously, so their individual weight is unknown. One test with the class name but no `process_signal` method (and vice versa) would pin precedence.
2. **tests/test_strategy_type_detection.py:59-95 — nit — CLI rejection test builds a 16-field SimpleNamespace** mirroring argparse output; brittle to new CLI args (must keep adding fields). A small factory helper would localize that churn.

**(b) Security** — nothing found.

**(c) Baseline smells** — none significant beyond the above.

**(d) Maintainability hot spots**: low.

---

## tests/validation/test_stats_comparison.py

Scanned in full (95 lines). Compact and correct: identical-stats field-set equality, tolerance arithmetic verified numerically (:61-64), absolute-tolerance config, inf profit-factor both ways.

**(a) Bugs / correctness risks**

1. **tests/validation/test_stats_comparison.py:32-49 — minor — exact field-set assertion duplicates production knowledge** (same 10-field list as test_regression.py:87-101); if `_compare_stats` grows an eleventh metric, two tests must be updated in lockstep. Centralize the expected set in one shared constant/helper.
2. **tests/validation/test_stats_comparison.py:12-29 — nit — `_trade` derives direction from pnl sign (`LONG if pnl >= 0 else SHORT`)** which is semantically meaningless for stats but could confuse future readers into thinking direction matters; harmless.

**(b) Security** — nothing found.

**(c) Baseline smells** — none significant.

**(d) Maintainability hot spots**: none.

---

## tests/validation/test_trade_matcher.py

Scanned in full (276 lines). Thorough matcher coverage: temporal tolerances (boundary pairs), symbol gating on/off, direction mismatch → missing+extra semantics, empty-input matrix, identical-timestamp pairing order, timezone normalization incl. naive-as-UTC, MatchKey UTC canonicalization.

**(a) Bugs / correctness risks**

1. **tests/validation/test_trade_matcher.py:198-234 — minor — input-order pairing asserted as behavior without stating it's a guarantee.**
   `test_pairs_in_input_order` documents position-based pairing for duplicate keys via docstring (good), but nothing covers the ambiguous case: duplicate keys where the *wrong* pair would produce different downstream comparisons (e.g. ref [A,B], act [B,A] same timestamps, different prices). That's precisely where order-pairing can mask real mismatches; add one case asserting the deliberate trade-off or smarter matching.
2. **tests/validation/test_trade_matcher.py:103-116 — nit — tolerance boundary tested at ±5s exactly but not at ±4/±6** to pin inclusive-vs-exclusive comparison; boundary off-by-one is the classic matcher bug.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (minor)**: `_make_trade` is yet another near-identical copy of the validation-suite helper (see recurring note).

**(d) Maintainability hot spots**: none significant.

---

## tests/validation/test_validation_reference.py

Scanned in full (138 lines). Pydantic model contract: allowed enums, empty-trades gating, `to_backtest_result()` mapping fidelity, raw-metadata round-trip. No bugs found.

**(a) Bugs / correctness risks**

1. **tests/validation/test_validation_reference.py:60-66 — minor — enum acceptance loop doesn't verify each value is distinct behavior,** just that construction succeeds; fine for allow-list testing. The rejection case is present (good).
2. **tests/validation/test_validation_reference.py:121-125 — minor — notes assertion checks substring containment** ("mt5_xlsx" and path in notes); formatting changes to notes could break or dilute this, acceptable.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code (nit)**: `_make_minimal_valid_reference` overlaps heavily with test_cli.py's `_make_reference`; consolidate.

**(d) Maintainability hot spots**: none.

---

## tests/validation/__init__.py

Empty package marker file. Nothing to review; accounted for as scanned with nothing found.

---


