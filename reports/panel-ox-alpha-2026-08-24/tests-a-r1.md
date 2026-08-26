# Code Review — area `tests-a` (chunk A of the test suite) — reviewer r1

Scope: all 39 tracked files under `tests/` listed in the brief. Reviewed blind and independently; no other review files read.
Conventions: (a)=bugs/correctness incl. misleading tests, (b)=security, (c)=Fowler smells, (d)=maintainability. Severities critical/major/minor are assigned to (a)/(b) findings; (c)/(d) are narrative with line references. Files with nothing found are explicitly recorded.

---

## Group 1 — largest files

### tests/compare_metrics_quantstats.py

(a) Bugs/correctness risks
- **[minor]** `tests/compare_metrics_quantstats.py:92` — `daily_rf = RISK_FREE_RATE / 252` computed in `compare_scenario()` but never used (all QuantStats calls pass annual `RISK_FREE_RATE`). Dead variable that misleads a reader into thinking the comparison involves a daily rf.
- **[minor]** Whole file is a script, not a test: filename escapes pytest collection (`test_*.py`), contains zero assertions, and always exits 0. The ✓ OK / ✗ DIFF column (`print_results`) is cosmetic — CI can never fail on metric drift between `runner.stats` and QuantStats. As a validation artifact it enforces nothing; parity regressions go unnoticed unless someone manually reruns and reads stdout.
- **[minor]** `tests/compare_metrics_quantstats.py:105-113` — hardcodes both `our_calmar_bugged` (divides MDD fraction by 100 again) and `our_calmar_fixed`, labelling one "BUG CANDIDATE". This encodes a suspected production bug in `runner.stats.compute_stats()` (Calmar dividing by `max_drawdown_pct/100`) as print-only commentary instead of a failing regression test. Either way this is wrong: if the bug lives on, no test pins correct behavior; if fixed, the scaffolding is stale.
- **[minor]** `tests/compare_metrics_quantstats.py:58,71,79` — scenario data generated via repeated global `np.random.seed(...)` at module level; reordering or adding scenarios silently changes all downstream data. Prefer local `np.random.default_rng(seed)`.

(b) Security
- Nothing found.

(c) Baseline smells
- **Speculative Generality**: dual bugged/fixed Calmar columns plus two "DEEP-DIVE" print sections (lines 218, 246-280) are exploratory output kept long after their question was answered.
- **Duplicated Code**: population-vs-sample Sharpe math (~lines 227-243) and Sortino downside variants (~lines 255-275) re-implement formulas already in `runner.stats` (`_sharpe`, `_sortino`) — three copies of the same math drifting apart.

(d) Maintainability hot spots
- Mixed concerns (scenario fixtures, metric reimplementations, formatting, reporting) in one file.

### tests/strategies/test_intraday_volatility_breakout.py

(a) Bugs/correctness risks
- **[minor]** `tests/strategies/test_intraday_volatility_breakout.py:202-233` (`test_one_trade_per_day`) — assertion `days.nunique() == len(entry_indices)` is vacuously true when **no entries fire** (`0 == 0`). There is no `assert entries.any()` guard, so a refactor that kills all entries keeps this "one trade per day" test green.
- **[minor]** `tests/strategies/test_intraday_volatility_breakout.py:501-507` — identical assertion `indicators["entry_reference_price"].iloc[entry_idx] == pytest.approx(df["close"].iloc[entry_idx + 1])` appears twice back-to-back (copy-paste leftover; second occurrence adds nothing).
- **[minor]** `tests/strategies/test_intraday_volatility_breakout.py:538-540` — pins `entry_reference_price ≈ signal-bar high` while `order_timing == "next_bar_open"` (line 534). Using the signal bar's high as fill reference for a next-bar-open market order is surprising; if this field feeds slippage/PnL downstream the test cements a suspicious value as correct. Deserves deliberate confirmation against the signal-contract docs, not silent acceptance.

(b) Security
- Nothing found.

(c) Baseline smells
- **Duplicated Code**: every signal test repeats the same 5–7-line param override block (`use_candle_close=False`, `entry_time="16:30"`, `max_entry_time=…`, `atr_regime_factor=2.0`, `atr_period=1`, `atr_long_period=1`); values already diverge (`"18:00"` vs `"18:05"`) with no stated reason.
- **Mysterious Name**: `_two_day_breakout_df` builds 2880 bars whose day-1 flatness is a load-bearing precondition for ATR/regime filters — name communicates none of this.

(d) Maintainability hot spots
- One of the stronger files in the suite (real ATR recurrence expectations, DST-aware session test, NaN-gap handling). Main exposure is the param-block duplication.

---

## Group 2 — infrastructure, integration, account-routing

### tests/conftest.py

(a) Bugs/correctness risks
- **[minor]** `tests/conftest.py:33-41` — `_playwright_browser_launchable` is `lru_cache`d forever, so a browser installed mid-session is invisible until process restart; cached failures also apply across parameterized e2e cases. Acceptable trade-off, undocumented.
- **[minor]** `tests/conftest.py:47-53` — broad `except Exception` around the Playwright probe (comment acknowledges it): any non-"executable doesn't exist"/"browser not found" failure (display, driver/version mismatch) surfaces as raw ERROR instead of the intended skip. Deliberate fail-loud choice, but partial installs produce noisy red runs rather than skips.

(b) Security — Nothing found.
(c) Smells — Nothing significant; file is tight and well-commented.
(d) Hot spots — None.

### tests/orchestrator.py

(a) Bugs/correctness risks
- **[major]** `tests/orchestrator.py:30-38` (`_layer_pytest_args`) — layer names misrepresent what runs: `"validation"` executes `tests/integration/test_ai_code_quality.py`; `"strategies"` executes `tests/integration/test_strategy_pipeline.py`. Neither runs `tests/validation/**` nor the actual `tests/strategies/**` suite those names advertise; the real suites are reachable only through the `"unit"` layer's marker filter `-m "not integration and not e2e and not ai_code"`. None of the `tests/strategies/*` files carry any marker (verified: no `pytestmark`/markers in the regression templates), so their inclusion in `unit` rests purely on default marker-less collection — while the layer names tell a reader those directories have dedicated layers. Whole suites can be silently un-executed while the summary table reports PASS. Rename layers or point them at real paths.
- **[minor]** `tests/orchestrator.py:63-66` — when pytest dies before writing the JSON report, `summary={}` yields `Failed=0 Passed=0` for a failed layer; the printed table actively misleads triage.
- **[minor]** `tests/orchestrator.py:65` — unguarded `json.loads` on the report: a truncated/corrupt report crashes the orchestrator mid-summary instead of being reported.
- **[minor]** `tests/orchestrator.py:55-60` — `subprocess.run` without `timeout=`; a hung layer blocks forever (Python checklist item: subprocess without timeout).

(b) Security — Nothing found.
(c) Smells — **Mysterious Name** (layer names vs executed targets, above).
(d) Hot spots — The layer→path mapping is the single point where a typo silently excludes suites; deserves a self-test.

### tests/strategies/conftest.py

(a) Bugs/correctness risks
- **[major]** `tests/strategies/conftest.py:100-138` — autouse module-scoped fixture **silently skips entire strategy-test modules** whenever OHLCV data is missing or lacks requested coverage. On any machine without `data/ohlcv/<SYMBOL>/<TF>.parquet`, the whole strategy backtest suite degrades to skips with a green exit code. There is no strict-mode opt-out, no visible aggregated warning, and no marker to filter these skips in reporting — the textbook "environment-dependent disabled tests" pattern hiding coverage gaps. At minimum emit a prominent skipped-due-to-data summary or support a fail-on-data-missing env flag.
- **[minor]** `tests/strategies/conftest.py:60-65,67-72` — bare-family `except Exception: return None, None` around parquet reads converts a **corrupt** local data file into "no coverage" → skip, instead of an error. Corrupt data hides as unavailability.
- **[minor]** `tests/strategies/conftest.py:53-59` — end-bound expansion adds a full day minus 1µs only when the end string has no time component; an end of `"2024-06-30T00:00"` excludes all data later that day, surprising for users reasoning in inclusive calendar dates.
- **[minor]** `tests/strategies/conftest.py:76-96` — `_covers_requested_period` unions min/max across *all* candidate paths (including the `<symbol>.parquet` fallback globbed for `1d`); disjoint or stale duplicate datasets can therefore satisfy a range check that the canonical file alone would fail. Coverage is asserted against the union, not the file actually used downstream.

(b) Security — Nothing found.
(c) Smells — **Divergent Change**: the autouse fixture handles three unrelated reasons to change (dependency detection, data presence, date-range coverage).
(d) Hot spots — Together with `tests/orchestrator.py`, this controls whether strategy tests execute at all — highest-leverage correctness surface in the area.

### tests/e2e/lab/test_conversion.py

(a) Bugs/correctness risks
- **[major]** `tests/e2e/lab/test_conversion.py:75-80` — if no code can be extracted from the agent output, the test substitutes `valid_trend_strategy.py` from fixtures and validates **that** instead of the system output. The headline assertion (`AICodeValidator().validate(generated_code)`) then checks fixture content, so a conversion pipeline returning garbage passes as long as the fallback file exists; the final `assert generated_code.strip()` can likewise never fire when the fallback exists. Remove the fallback or fail loudly on empty extraction.
- **[minor]** `tests/e2e/lab/test_conversion.py:74` — `output_text` is captured immediately after the first expected text becomes visible; LLM generation may still be streaming into `output_area`, so extraction races UI updates — intermittent empty extractions push runs into the fallback path above, masking real behavior most of the time.
- **[minor]** `tests/e2e/lab/test_conversion.py:86,90` — filenames keyed on `int(time.time())`: two runs within the same second overwrite screenshot/validation names; the parsed task id earlier in the test would be a better key.
- **[minor]** `tests/e2e/lab/test_conversion.py:12` — hardcoded `BASE_URL` ignores environment override, inconsistent with `tests/integration/test_strategy_pipeline.py:15` which honors `BASE_URL` env (copy-paste drift between entry points).

(b) Security — Nothing found.
(c) Smells — Fixture-substitution-for-output (see major); **Duplicated Code**: base-URL/config duplicated across e2e/integration instead of shared via conftest.
(d) Hot spots — Extract-or-fallback logic is the core weakness.

### tests/integration/test_strategy_pipeline.py

(a) Bugs/correctness risks
- **[minor]** `tests/integration/test_strategy_pipeline.py:104` — live HTTP discovery (`_discover_strategy_matrix()`) executes **at import/collection time**; collecting the suite hits `http://localhost:5556` even for runs that will deselect these tests, stalling whole pytest sessions when the server is slow/down before the module-level skip even gets decided.
- **[minor]** `tests/integration/test_strategy_pipeline.py:157` — `BacktestExpectation(min_trades=0)` makes the end-to-end validation structural-only: a pipeline silently producing zero trades over six months of daily data passes. If intentional smoke semantics, comment it; otherwise floor per strategy class.
- **[minor]** `tests/integration/test_strategy_pipeline.py:31-36` — `except Exception` around discovery folds auth/config errors into a module-level skip too (same silent-skip family as `tests/strategies/conftest.py`); acceptable for integration+slow marking, but the skip reason embeds raw exception text in CI logs.
- **[minor]** Polling (`_poll_task_completion`, lines 57-83) uses `time.monotonic()` correctly; no flaky-sleep issue beyond inherent polling. No further findings.

(b) Security — Nothing found.
(c) Smells — Import-time side effects (above).
(d) Hot spots — None beyond above.

### tests/integration/test_ai_code_quality.py

(a) Bugs/correctness risks
- **[major]** `tests/integration/test_ai_code_quality.py:10-11` — `FIXTURES_DIR.mkdir(parents=True, exist_ok=True)` runs at collection (side effect on the repo tree), then `STRATEGY_FILES = sorted(FIXTURES_DIR.glob("*.py"))` fixes parametrization. If the fixture directory is emptied/missing, the **parametrized** validation suite generates zero cases and trivially passes; only the separate average-score test would catch emptiness. Deleting fixture files leaves the headline AI-code gate green — false confidence by construction.
- **[minor]** `tests/integration/test_ai_code_quality.py:29-35` — `average_score >= 0.7` over only two fixture files swings ~0.15+ per added/removed fixture (fragile gate), and re-asserts `all(result.passed)` duplicating the per-file parametrized test.

(b) Security — Nothing found.
(c) Smells — **Speculative Generality** (mkdir "just in case"); minor **Duplicated Code** (validator run twice over the same corpus).
(d) Hot spots — Filesystem-driven parametrization couples fixture churn to test matrix changes invisibly.

### tests/fixtures/ai_generated_strategies/intentionally_broken_strategy.py

(a) Bugs/correctness risks
- **[major]** Misleading-by-name fixture: despite the filename, the code defines all three required methods (`get_name`, `get_parameters`, `generate_signals`), parses, imports, contains no forbidden calls — i.e. it is **valid** under `tests/validators/code_validator.py` (`_REQUIRED_METHODS`, security walk). It is globbed by `tests/integration/test_ai_code_quality.py:11` and asserted to PASS (`assert result.passed`). So either (1) the validator cannot detect whatever "brokenness" the name promises — making the fixture dead as a negative control and the validator's advertised sensitivity untested — or (2) someone will eventually "fix" the fixture to actually break and the quality-gate test will start failing confusingly. A true negative-control fixture should live outside the pass-gated glob or be asserted to FAIL in its own test. As it stands the name lies and the suite gains nothing from it.

(b) Security — Nothing found in the fixture itself (it is inert).
(c) Smells — **Mysterious Name** (the entire point of the finding).
(d) Hot spots — Interaction with the pass-gate glob described above.

### tests/fixtures/ai_generated_strategies/valid_trend_strategy.py

(a) Bugs/correctness risks — Nothing found. Valid, minimal, matches the validator's required interface; appropriate as a fallback/positive control (its use as an e2e fallback is criticized under `tests/e2e/lab/test_conversion.py`).
(b) Security — Nothing found.
(c) Smells — Nothing.
(d) Hot spots — None.

### tests/fixtures/sample_strategy.py

(a) Bugs/correctness risks — Nothing found. Pine Script v5 source used as conversion input for the lab e2e; content matches its documented purpose (EMA cross with entry/exit).
(b) Security — Nothing found.
(c) Smells — Nothing.
(d) Hot spots — None.

### tests/__init__.py

Scanned: empty package marker. Nothing found (no findings of any category).

### tests/test_api.py

(a) Bugs/correctness risks
- **[major]** Conditional-vacuous detail tests: `test_get_strategy_detail` guards on `if strategies:` (`tests/test_api.py:39-45`), and `test_get_backtest_info` (:82-88), `test_get_backtest_stats` (:90-94), `test_get_backtest_trades` (:96-101), `test_get_backtest_equity` (:103-108), `test_get_portfolio_detail` (:129-133) all follow `items = ...; if items: <assert>`. When the store is empty (fresh checkout, cleaned artifacts) these tests execute **zero assertions and pass** — endpoints never exercised yet reported green. Seed fixture data in setup or skip-with-reason so vacuity is visible.
- **[minor]** `tests/test_api.py:147-156` (`test_benchmark_valid_symbol`) — accepts `200`-with-shape **or** bare `404`, and `test_benchmark_with_dates` (:158-159) accepts `(200, 404)`: almost any implementation passes; tautology-adjacent.
- **[minor]** `tests/test_api.py:15` — redundant `sys.path.insert(0, project root)` hack (pytest rootdir already provides imports); recurs across many files in this area (see Group 4).
- **[minor]** `tests/test_api.py:30-33` — client fixture reads whatever ambient `artifacts/data` state exists; results are order/environment-dependent (other tests writing backtests concurrently change listings). Integration behavior masquerading as API unit tests without seeding/isolation.

(b) Security — Nothing found (local endpoints).
(c) Smells — **Duplicated Code**: the `if backtests:` prologue repeats five times; extract a seeding fixture/helper.
(d) Hot spots — The conditional-assertion cluster is the top false-confidence source in this file.

### tests/test_account_discovery.py

(a) Bugs/correctness risks — Nothing found. Direct, concrete assertions; both polarities for host-environment filters and error classifiers; `sys.modules` monkeypatching (:51-54) is correctly scoped and reverted.
(b) Security — Nothing found.
(c) Smells — Nothing significant.
(d) Hot spots — None.

### tests/test_account_routing_safety.py

(a) Bugs/correctness risks — Nothing found. Strong file: duplicate-ID rejection, numerically-equivalent ctid collision (`"00123"` vs `"123"`), fail-closed-before-adapter-creation all precisely asserted.
- **[minor]** `tests/test_account_routing_safety.py:126-150` — loop termination relies on monkeypatched `asyncio.sleep` raising `CancelledError` on first call; a production refactor away from sleep-based pacing breaks the harness confusingly. Note only.

(b) Security — Nothing found (positively: tests pin fail-closed routing).
(c) Smells — Nothing significant.
(d) Hot spots — None.

### tests/test_alpaca_account_routing.py

(a) Bugs/correctness risks — Nothing found. Mutation-informed tests ("Kills mutant:" docstrings) covering hint precedence, ambiguity refusal, and default-pair fallback ordering. Model example.
(b) Security — Positive: ambiguity-refusal test documents that unmatched broker_login must not fall back to default credentials — directly prevents cross-account order routing. No issues.
(c) Smells — Minor **Duplicated Code**: near-identical ACC{1..3} env dicts across five tests; a builder would help.
(d) Hot spots — None.

### tests/test_auto_agent.py

(a) Bugs/correctness risks — Nothing material; retry semantics asserted via LLM call sequence + backtest call counts.
- **[minor]** `tests/test_auto_agent.py:100-107` etc. — fake `_run_backtest` writes `{"status":"ok"}` regardless of command contents; contract fidelity beyond `--output` index lookup is untested (acceptable at unit scope).

(b) Security — Nothing found.
(c) Smells — **Duplicated Code**: `fake_run_backtest` redefined identically in three tests (~lines 100, 135, 160); hoist to a helper.
(d) Hot spots — None.

### tests/test_analyze_mt5_parity.py

(a) Bugs/correctness risks — Nothing found. UTC-normalization matching, Athens-local session bucketing, exit-reason classification all asserted concretely with positive/negative cases.
- **[minor]** `tests/test_analyze_mt5_parity.py:118-125` — mutates `trades[i].pnl`/exit times post-construction instead of constructing directly; stylistic.

(b) Security — Nothing found.
(c) Smells — Nothing significant.
(d) Hot spots — None.

### tests/test_backtest_equity_curve_migration.py

(a) Bugs/correctness risks
- **[major]** Every test calls `init_db()` against the ambient `ENGINE` database and inserts timestamped rows with **no teardown/tmp-path isolation** (`tests/test_backtest_equity_curve_migration.py:26,42-77,82-110,115-148,153+`). Persistent `test_equity_curve_*`/`test_save_result_*` rows accumulate in the shared dev SQLite DB, polluting `/api/backtests` listings consumed by the viewer and by other tests (cf. `tests/test_api.py`), and racing other suites in parallel CI on shared SQLite state.
- **[minor]** `tests/test_backtest_equity_curve_migration.py:152-176` (`test_equity_curve_migration_on_existing_table`) — never constructs a legacy table lacking `equity_curve` and migrating it; it only re-checks the column exists after `init_db()`, so the actual migration logic is untested while the name promises it (misleading test).
- **[minor]** `tests/test_backtest_equity_curve_migration.py:172-176` — `SELECT ... LIMIT 1` conditioned on `if result and result[0]:` is vacuous on an empty table.

(b) Security — Nothing found.
(c) Smells — **Duplicated Code** in record-construction boilerplate; shared mutable global state (ENGINE) per the major above.
(d) Hot spots — DB-touching tests without isolation are the biggest operational hazard in this group.

---

## Group 3 — frontend e2e script

### tests/e2e_frontend.js

(a) Bugs/correctness risks
- **[major]** `tests/e2e_frontend.js:148-155` — the console-error test collects `pageerror` events all run long and then deliberately asserts nothing ("We don't fail on console errors — just report"). A test named "No critical console errors" that cannot fail is pure false confidence; either assert `critical.length === 0` or remove it from the pass/fail tally (currently it increments `passed`, inflating the score).
- **[minor]** `tests/e2e_frontend.js:112-118` — backtest-detail test wraps its only assertions in `if (items.length > 0)`: with no backtests in the store it passes having verified nothing (same conditional-vacuity family as `tests/test_api.py`).
- **[minor]** `tests/e2e_frontend.js:97-104` — symbol-autocomplete test wraps everything in `if (await input.count() > 0)`; if the modal didn't stay open from the previous test (stateful coupling across tests), the test silently passes doing nothing.
- **[minor]** Flaky timing throughout: fixed `waitForTimeout(2000/3000/800/1000)` at :51, :65, :75, :82, :86, :98, :112, :130, :140 instead of wait-until conditions; slow CI boxes intermittently fail, fast boxes waste seconds. Also `const BASE = 'http://localhost:5556'` (:9) hardcoded, ignoring env config used elsewhere.

(b) Security — Nothing found (local pages).
(c) Smells — Homegrown runner duplicates what the Playwright-pytest layer already provides (**Duplicated Code** at framework level); cross-test state coupling (modal left open for next test).
(d) Hot spots — Standalone script outside pytest means results aren't in JSON reports/orchestrator summaries — invisible to `tests/orchestrator.py`.

---

## Group 4 — strategy regression suite (`tests/strategies/`)

Sixteen files share an identical copy-pasted template (docstring "Regression-Test fuer Strategie …", module constants, module-scoped `backtest_result` fixture running the engine once, then `TestStrategyRegression` with `test_trade_count`, `test_net_profit`, `test_all_trades_have_sl_in_meta`, `test_no_trade_exceeds_risk_limit`). Template-wide defects, cited once here and flagged per file where severity warrants:

- **Golden-master brittleness**: exact `EXPECTED_TRADE_COUNT` + `math.isclose(net_profit, EXPECTED_NET_PROFIT, abs_tol=0.50)` pin one historical engine run. Any legitimate improvement (fee model, fill timing, indicator rounding) breaks dozens of tests at once, training maintainers to bulk-update expectations — at which point the numbers become unfalsifiable. With `abs_tol=0.50` on floats summed over trades, even benign float-ordering drift can trip it. This is the classic wrong-tool regression net: it detects *change*, not *regression*.
- **Risk-limit check semantics** (every template file): recomputes `risk_pct` against a locally simulated equity path starting at hardcoded `100_000.0` with threshold `0.015`, while the engine was configured with `risk_per_trade_pct=0.01` — a 50% looser gate that would miss any sizing bug between 1% and 1.5% of equity.
- `sys.path.insert(0, ...)` boilerplate at :17/:18 in all 18 files (redundant under pytest rootdir).
- `test_all_trades_have_sl_in_meta` duplicates the `sl is not None` assertion that `test_no_trade_exceeds_risk_limit` makes anyway.

Per-file findings:

### tests/strategies/test_channel_breakout.py
- **[major]** `tests/strategies/test_channel_breakout.py:31-32` — golden master over a single recent quarter (US500 1h, 2025-04-01…06-30, 8 trades, $2,886.67 ±0.50); see template note. Highest churn likelihood.
- **[minor]** `:69-79` — risk gate at 1.5% vs configured 1% (template note).

### tests/strategies/test_cross_asset_momentum.py
- **[major]** `tests/strategies/test_cross_asset_momentum.py:30-32` — golden master over a 2-year window (21 trades, $1,830.69 ±0.50); long windows make exact-count pins even less reproducible across data-vendor revisions.
- **[minor]** `:1,28` — "Cross-Asset Momentum" validated on a **single** symbol (AAPL); the multi-asset essence of the strategy is untested — name promises coverage the test cannot deliver.

### tests/strategies/test_custom_swings_supertrend.py
- **[major]** `tests/strategies/test_custom_swings_supertrend.py:35-37` — golden master pinning a **losing** period ($-2,197.47, 13 trades); pins loss-making behavior as canonical.
- **[minor]** `:139-150` (`test_no_trade_exceeds_risk_limit` variant) — silently `continue`s when `sl_price` is NaN, weakening the risk check relative to sibling files (a strategy emitting NaN stops escapes the gate entirely).
- **[minor]** `:87-99, 152-166` — contract tests feed `np.random.uniform` DataFrames **without a seed**: nondeterministic inputs; shape-only assertions currently survive, but any session/date-sensitive logic makes outcomes run-dependent.
- (smell) Inline module-load helper duplicated three times within the file (**Duplicated Code**).

### tests/strategies/test_gold_momentum.py
- **[major]** `tests/strategies/test_gold_momentum.py:31-32` — golden master, single quarter 2025 Q4, $15,439.32 ±0.50 on 29 trades.
- **[minor]** `:69-79` — 1.5% vs configured 1% risk gate (template).

### tests/strategies/test_mean_reversion_rsi_bb.py
- **[major]** `tests/strategies/test_mean_reversion_rsi_bb.py:30-31` — golden master on a **two-trade** sample ($-324.18 ±0.50): statistically meaningless and maximally brittle; a single changed fill flips the test.

### tests/strategies/test_pine_superosc_vumanchu.py
- **[major]** `tests/strategies/test_pine_superosc_vumanchu.py:30-31` — golden master, 2025 Q1, 10 trades, $4,491.05 ±0.50.

### tests/strategies/test_pine_supertrend_dema.py
- **[major]** `tests/strategies/test_pine_supertrend_dema.py:30-31` — golden master, 3 trades, $589.93 ±0.50.

### tests/strategies/test_post_vix_dip_buy.py
- **[major]** `tests/strategies/test_post_vix_dip_buy.py:30-31` — golden master, 3 trades, losing period $-1,160.43 ±0.50.

### tests/strategies/test_pynecore_orb.py
- **[minor]** `tests/strategies/test_pynecore_orb.py:29-30` — same golden-master pattern (8 trades, $405.15 ±0.50); rated minor because the module is dependency-gated (skipped entirely without pynecore per `tests/strategies/conftest.py:106-109`), so its practical enforcement is conditional anyway.
- (positive) `(trade.meta or {})` None-safety here (:52, :59) is correct — but inconsistent with all vectorbt-template siblings that assume `meta` non-None.

### tests/strategies/test_pynecore_sma_crossover.py
- **[minor]** `tests/strategies/test_pynecore_sma_crossover.py:29-30` — golden master (8 trades, $2,178.03 ±0.50); same dependency-gating caveat as above.

### tests/strategies/test_qg_30min_es_breakout_strategy.py
- **[major]** `tests/strategies/test_qg_30min_es_breakout_strategy.py:33-37` — golden master requiring **2006 XAUUSD 1h** data (62 trades, $9,408.30 ±0.50): the oldest, rarest dataset in the suite; effectively unrunnable on fresh machines (and silently skipped per conftest), so the pin decays unobserved.
- **[minor]** `:56` — only template file passing an explicit `data_dir` into `BacktestConfig`; inconsistency implies siblings depend on ambient `DATA_DIR` resolution — divergence worth normalizing.

### tests/strategies/test_sector_rotation.py
- **[major]** `tests/strategies/test_sector_rotation.py:30-31` — golden master over 2023–2024 (17 trades, $-1,576.84 ±0.50), pinning a losing outcome as canonical.
- **[minor]** `:1,28` — "Sector Rotation" validated on one symbol (AAPL); rotation across sectors untested.

### tests/strategies/test_short_term_reversal.py
- **[major]** `tests/strategies/test_short_term_reversal.py:30-31` — golden master on **one single trade** ($2,441.70 ±0.50): the weakest possible regression signal; essentially a coin-flip fixture.

### tests/strategies/test_stat_arb_pairs.py
- **[major]** `tests/strategies/test_stat_arb_pairs.py:30-31` — golden master over a 4-year window (11 trades, $4,006.10 ±0.50).
- **[minor]** `:1,28` — a **pairs/stat-arb** strategy regression-tested on a single symbol (AAPL): the spread/pair leg central to the strategy is absent from its own regression test.

### tests/strategies/test_trend_following.py
- **[major]** `tests/strategies/test_trend_following.py:30-31` — golden master, 2 trades, $2,261.02 ±0.50.

### tests/strategies/test_volatility_breakout.py
- **[major]** `tests/strategies/test_volatility_breakout.py:30-31` — golden master, 9 trades, losing period $-6,122.70 ±0.50.

### tests/strategies/test_volatility_breakout_reference_logic.py

(a) Bugs/correctness risks
- **[minor]** `tests/strategies/test_volatility_breakout_reference_logic.py:23-30` — asserts exact entry indices `[5, 8]` and directions (good, deterministic unit-style), but exit coverage is only `exits.any()` — no exit indices/reasons pinned, so exit-logic regressions within this reference implementation are largely unchecked.

(b) Security — Nothing found.
(c) Smells — Nothing.
(d) Hot spots — None; this is the right *style* of test (synthetic bars, exact assertions) — the rest of the folder would benefit from copying it.

### tests/strategies/test_risk_parity.py

(a) Bugs/correctness risks — Overall strong: seeded RNGs, PSD/symmetry checks, bounds, warm-up validation error path.
- **[minor]** `tests/strategies/test_risk_parity.py:89-90` — crisis detection asserted only as `regime in ("elevated", "crisis")`: a regression mapping every input to "crisis" would pass; acceptable given stochastic input, but the disjunction weakens the pin.
- **[minor]** `tests/strategies/test_risk_parity.py:169-178` — rebalance-threshold test mocks out *all* weighting functions (covariance, RP weights, regime, vol scaling, tail hedge); the interaction of thresholding with the real weighting pipeline is untested here (fine for unit scope, but the module has no lighter-mocked complement).

(b) Security — Nothing found.
(c) Smells — Nothing significant.
(d) Hot spots — None.

---

## Cross-area summary

1. **Silent-skip architecture** (highest impact): `tests/strategies/conftest.py:100-138` + `tests/orchestrator.py:30-38` jointly allow large parts of the suite to be un-executed while runs report green.
2. **Vacuous/conditional assertions**: `tests/test_api.py` (six tests), `tests/e2e_frontend.js` (:112, :148), `tests/e2e/lab/test_conversion.py:75-80`, `tests/integration/test_ai_code_quality.py:10-14`.
3. **Golden-master template** across 16 strategy files pins engine outputs (sometimes on 1–3-trade samples) instead of properties — high maintenance cost, low defect-detection value; `test_volatility_breakout_reference_logic.py` shows the better pattern already in-repo.
4. **Shared-state leakage**: DB-writing migration tests with no isolation (`tests/test_backtest_equity_curve_migration.py`).
5. Recurring minor hygiene: `sys.path.insert` in 20+ files, duplicated param/env blocks, hardcoded localhost URLs, unseeded RNG in two spots, subprocess without timeout in the orchestrator.

## Files scanned with nothing found (per-category)

- `tests/__init__.py` — empty; nothing found.
- `tests/fixtures/sample_strategy.py` — nothing found.
- `tests/fixtures/ai_generated_strategies/valid_trend_strategy.py` — nothing found.
- `tests/test_account_discovery.py` — no bug/security findings.
- `tests/test_alpaca_account_routing.py` — no bug/security findings.

## Finding counts

- critical: 0
- major: 22
- minor: 47
