# Code Review — area `tests-a` (chunk A of test suite), reviewer r2

Blind, independent review of the 39 tracked files listed for this area. Findings cite `file:line`
with severity (critical/major/minor). Files with nothing found are recorded as scanned.

---

## Group 1 — `tests/strategies/`

### tests/strategies/test_intraday_volatility_breakout.py

**(a) Bugs / correctness risks**

- **Minor — duplicated assertion** `test_intraday_volatility_breakout.py:501` and
  `test_intraday_volatility_breakout.py:506`: the same
  `indicators["entry_reference_price"] == pytest.approx(df["close"].iloc[entry_idx + 1])` check appears twice in
  `test_candle_close_signal_emits_previous_bar_for_next_open_execution`. Harmless but signals a copy-paste remnant;
  one of them was likely meant to assert something else (e.g. the entry price vs reference price).
- **Minor — weak tautology risk in "one trade per day" test** `test_intraday_volatility_breakout.py:234`:
  `assert days.nunique() == len(entry_indices)` also passes vacuously when `entries` is empty (`nunique()==0`,
  `len==0`). The test name promises "one trade per day" but a silent regression to zero entries would still pass.
  There is no `assert entries.any()` guard, unlike sibling tests.
- **Minor — brittle magic expectations**: `test_regime_filter_blocks_when_condition_not_met`
  (`test_intraday_volatility_breakout.py:416`) uses `atr_regime_factor = 1e9`; fine as an "impossible filter", but
  combined with only 60 bars of warmup it exercises the degenerate path rather than the regime logic itself.

**(b) Security** — none found (pure offline signal generation).

**(c) Baseline smells**

- **Duplicated Code**: `_load_module()` + `importlib.util.spec_from_file_location("ivb", ...)` re-implements the
  module-loading helper already needed by every strategy test; `test_custom_swings_supertrend.py` repeats the exact
  pattern three times (see below). Extract into `conftest`.
- **Mysterious Name**: `_make_df(n_bars, start="2024-01-01 15:00")` docstring says "covering one trading day" but
  callers pass 200–300 bars spanning multiple days (`test_intraday_volatility_breakout.py:185`, `:429`) — the helper
  contract doesn't match its use.

**(d) Maintainability hot spots**: file is well structured overall (good ATR edge-case coverage at lines
102–148); main debt is the copy-paste loader pattern shared across the strategies suite.

### tests/strategies/conftest.py

**(a) Bugs / correctness risks**

- **Major — broad auto-skip fixture can silently disable whole modules**
  `conftest.py:79` (`skip_when_required_data_or_dependency_missing`, `autouse=True`, module scope): every strategy
  module in this directory is skipped whenever the parquet dataset under `DATA_DIR/ohlcv` is missing or lacks
  coverage. Combined with `_path_time_coverage` swallowing *all* exceptions (`except Exception: return None, None`,
  `conftest.py:60-63`, repeated at `:67-70`), any corrupt/unreadable parquet file silently degrades to "no coverage"
  → skip → green CI with zero regression tests actually run. This is exactly the "fixtures hiding production bugs /
  tests disabled without reason" anti-pattern: there is no marker, no warning, and no reported count of skipped
  regression suites. At minimum the except should log, or the skip should be surfaced as `xfail`/warning summary.
- **Minor — exception breadth**: two bare-ish `except Exception` around act-phase IO
  (`_path_time_coverage`) convert hard failures (permission errors, schema changes like renamed `timestamp`
  column) into skips. A missing column after a producer change will never be noticed here.
- **Minor — glob duplication**: `_candidate_ohlcv_paths` uses `root.rglob(f"**/{symbol}/{timeframe}.parquet")`;
  `**/` prefix plus rglob already recurses, so the pattern is redundant (`rglob("**/X/Y.parquet")` ≡ `rglob("X/Y.parquet")`)
  — harmless but confusing.

**(b) Security** — none found.

**(c) Baseline smells**

- **Speculative Generality (mild)**: `_parse_date_bound`'s `is_end` end-of-day expansion is only meaningful for date-only
  strings; fine, but undocumented interaction with `TEST_END` constants (all modules pass date-only strings).
- The fixture reads entire parquet timestamp columns via pyarrow on every module — acceptable, but note it makes
  these "regression smoke tests" depend on local data availability with no CI guarantee.

**(d) Maintainability hot spots**: this conftest is single point of failure for ~18 regression modules; a silent
skip here multiplies across the suite.

### tests/strategies/test_channel_breakout.py, test_gold_momentum.py, test_mean_reversion_rsi_bb.py,
### test_post_vix_dip_buy.py, test_short_term_reversal.py, test_trend_following.py, test_volatility_breakout.py,
### test_pine_superosc_vumanchu.py, test_pine_supertrend_dema.py, test_cross_asset_momentum.py,
### test_sector_rotation.py, test_stat_arb_pairs.py, test_pynecore_orb.py, test_pynecore_sma_crossover.py

These 14 files are the same template (golden-value regression smoke tests). Shared findings apply to all; per-file
notes where they differ.

**(a) Bugs / correctness risks**

- **Major — golden values are unfalsifiable snapshots with tight coupling to environment**
  (e.g. `test_channel_breakout.py:29-30` `EXPECTED_TRADE_COUNT = 8`, `EXPECTED_NET_PROFIT = 2886.67`):
  expected trade counts/net profits were captured from one run ("Validiert am: 2026-03-08") against a specific
  parquet snapshot of US500/AAPL/XAUUSD/SPY data. Any data refresh, broker-adjusted history, timezone handling tweak,
  or float summation-order change flips these to red with no indication whether the strategy regressed or the data
  changed. Conversely, because the conftest skips when data is absent, in most environments these tests are simply
  skipped (see conftest finding). Net effect: either brittle-false-red or silently-absent. The tolerance
  `abs_tol=0.50` on net profit (e.g. `test_channel_breakout.py:57`) is far tighter than any realistic data drift.
- **Major — `test_no_trade_exceeds_risk_limit` divides by stale equity baseline** (identical code in all 14 files,
  e.g. `test_channel_breakout.py:75-87`): the loop computes `risk_pct = risk_usd / equity` where `equity` starts at
  `100_000.0` and is only updated *after* the check (`equity += trade.pnl` at the end of each iteration). That part
  is sequential-by-design, but the deeper issue: risk sizing is evaluated against *initial* equity for the first
  trade only if pnl updates lag — more importantly the check asserts `<= 0.015` while config sets
  `risk_per_trade_pct=0.01`, i.e. the assertion tolerates 1.5× the configured risk; a sizing bug that inflates risk
  by up to 50% passes. The slack constant is unexplained and untested against intent.
- **Minor — copy-paste error in docstrings**: `test_stat_arb_pairs.py` runs a *single-symbol* AAPL backtest
  (`TEST_SYMBOL = "AAPL"`, `STRATEGY_PATH = .../stat_arb_pairs.py`) yet claims to validate "Statistical Arbitrage
  Pairs"; likewise `test_sector_rotation.py` validates a sector-rotation strategy on a single symbol AAPL. A pairs
  trading / rotation strategy backtested on one ticker cannot exercise its core pairing/rotation logic — the golden
  values encode whatever degenerate single-leg behavior the strategy falls back to. This is a misleading test:
  it gives false confidence that stat-arb pair logic is regression-covered when only the fallback path is.
- **Minor — pynecore variants correctly guard `trade.meta or {}`** (`test_pynecore_orb.py:71`,
  `test_pynecore_sma_crossover.py:71`) while all Vectorbt-based siblings dereference `trade.meta` directly
  (e.g. `test_channel_breakout.py:66`). If meta can be `None` in the pynecore engine, it presumably can be in the
  vectorbt engine too — inconsistent defensive posture suggests one of the two engines' contracts is misunderstood.

**(b) Security** — none.

**(c) Baseline smells**

- **Duplicated Code (severe)**: ~14 near-identical files (~76 lines each) duplicating the full class body:
  fixture construction, `test_trade_count`, `test_net_profit`, `test_all_trades_have_sl_in_meta`,
  `test_no_trade_exceeds_risk_limit`. This is textbook Duplicated Code; the correct shape is one parametrized module
  driven by a table of `(strategy, symbol, timeframe, start, end, expected_trades, expected_profit)` plus a shared
  conftest fixture. Today a fix to the risk-limit logic must be hand-applied 14 times (Shotgun Surgery risk).
- **Primitive Obsession**: expected values are bare module-level floats/ints with naming conventions instead of a
  small `ExpectedResult` record bundling trades+profit+window (Data Clump).

**(d) Maintainability hot spots**: the template duplication is the dominant cost of this directory; any change to
`BacktestConfig` fields touches all 14 fixtures.

### tests/strategies/test_custom_swings_supertrend.py

**(a) Bugs / correctness risks**

- Shares all template findings above (golden values at `test_custom_swings_supertrend.py:34-35`, risk-limit test at
  `:96-110`).
- **Minor — non-deterministic random data without seed**: `test_generate_signals_returns_4_elements`
  (`test_custom_swings_supertrend.py:73-89`) and `test_indicators_dict_has_required_keys` (`:127-143`) build OHLC from
  `np.random.uniform` **without seeding** (`np.random.uniform(1800, 2000, n)` etc.), and construct bars where
  `low > close` is possible ordering-wise garbage (high range 2000-2100, low 1700-1800, close 1850-1950 are sampled
  independently, so `close > high` happens constantly). If `generate_signals` computes ATR/stops it may hit NaN or
  exception paths nondeterministically across numpy versions. Seed it (`np.random.seed(...)` as sibling
  `test_intraday_volatility_breakout._make_df` does) and generate self-consistent bars.
- **Minor — AST check is weaker than it looks**: `test_generate_signals_has_parameter_defaults`
  (`test_custom_swings_supertrend.py:146-162`) counts `fn.args.defaults + kw_defaults` and requires `> 0`. A function
  with a single positional default (`def generate_signals(df, x=1)`) passes even with no configurable keyword args;
  the message claims it protects the optimization UI. Also misses async functions (`ast.AsyncFunctionDef`) — not
  used today, but the check silently wouldn't apply if the strategy migrated.
- **Note**: `test_no_trade_exceeds_risk_limit` here (`:96-110`) *does* handle `sl is None` and NaN sl gracefully
  (skipping those trades) — unlike the 14 template files which hard-assert. Ironically that makes this variant
  weaker: NaN sl_price (a real bug symptom) silently bypasses the risk check instead of failing.

**(b) Security** — none.

**(c) Baseline smells**

- **Duplicated Code within the file**: the importlib module-load block is pasted three times verbatim
  (`:68-74`, `:122-128`, `:150-156`) inside individual tests instead of a module-level helper/fixture.
- **Duplicated Code across files** (as above).

**(d) Maintainability hot spots**: the triple-pasted loader and the mixed defensive/non-defensive risk checks make
this the least consistent file of the group.

### tests/strategies/test_qg_30min_es_breakout_strategy.py

**(a) Bugs / correctness risks**

- Template findings apply (golden values `test_qg_30min_es_breakout_strategy.py:35-36`, risk-limit `:78-90`).
- This variant improves on the template: `equity = backtest_result.meta.initial_capital`
  (`:80`) instead of hardcoded 100_000, and guards `equity <= 0` before division (`:86-87`). Good — but the guard
  order still divides after the fact: `risk_pct = risk_usd / equity` executes *after* `pytest.fail` check, which is
  correct here; however `equity <= 0` can only trigger mid-run since initial capital is asserted positive — fine.
  Remaining nit: the other 13 files did NOT get this fix (inconsistency, see above).

**(b) Security** — none.

**(c) Baseline smells**

- **Inconsistent duplicate**: this file diverges positively from the template (uses `meta.initial_capital`, guards
  division) demonstrating that the other 13 copies lag behind a fix already made once — concrete evidence of the
  Shotgun Surgery cost of the duplicated template.

**(d) Maintainability hot spots**: same as group.

### tests/strategies/test_risk_parity.py

**(a) Bugs / correctness risks**

- **Minor — over-mocked act phase in the rebalance test** `test_risk_parity.py:186-205`: five monkeypatches replace
  covariance, weights, regime adjustment, vol scaling, tail hedge and correlation regimes. The test then verifies
  plumbing (thresholds, timestamps, attribution) — legitimate, but note `weight_sequence` iterator yields only 3
  weight dicts; if production code requested a 4th rebalance the test would raise StopIteration rather than fail an
  assertion — acceptable (fails loudly), just noting the mock rigidity means any added intermediate rebalance breaks
  the harness, not the assertion.
- **Minor — `test_detect_correlation_regime` asserts a disjunction** `test_risk_parity.py:97`:
  `assert regime in ("elevated", "crisis")` — accepts either label, so a regression that misclassifies crisis as
  elevated (or vice versa) is invisible. Given the next test (`adjust_for_regime`) treats regimes differently, the
  distinction is behaviorally relevant; pinning one expected label with a comment on classifier noise would be
  stronger.
- **Minor — statistical flake potential**: `test_detect_correlation_regime` builds synthetic factor returns with fixed
  seeds (good), but `recent_normal` vs `historical` both beta≈0.5 relies on average-correlation comparison margins;
  with n=30 recent days the margin between "normal" and "elevated" classification could sit near the boundary for
  the given seed. Fixed seed makes it deterministic today — flagging only that the margin is unverified.
- Otherwise strong file: seeded RNGs, PSD assertions, explicit warmup validation test (`:283-297`),
  threshold-boundary coverage (`:209-281`). Nothing misleading found.

**(b) Security** — none.

**(c) Baseline smells** — none significant; this is the best-written file in the group.

**(d) Maintainability hot spots**: none.

### tests/strategies/test_volatility_breakout_reference_logic.py

**(a) Bugs / correctness risks**

- **Minor — direct package import couples test run to installed layout** `test_volatility_breakout_reference_logic.py:3`:
  `from strategies.volatility_breakout.volatility_breakout import generate_signals` assumes repo root on `sys.path`
  (no `sys.path.insert` like siblings; works only when pytest is invoked with rootdir conftest inserting it, e.g. the
  top-level `tests/conftest.py`). If run standalone (`pytest tests/strategies/test_volatility_breakout_reference_logic.py`
  from another cwd without rootdir insertion) it ImportErrors. Minor portability inconsistency.
- Assertions themselves are concrete and falsifiable (`:26-33` exact entry indices `[5, 8]`, direction labels,
  non-NaN stops, exits present) — good.

**(b) Security** — none. **(c) Smells** — none beyond the above. **(d)** — none.

---

## Group 2 — root-level tests, infra, fixtures, integration/e2e

### tests/test_account_discovery.py

**(a) Bugs / correctness risks**

- **Minor — brittle package faking via `sys.modules`** `test_account_discovery.py:75-77`: the Alpaca SDK is faked by
  inserting `SimpleNamespace` stand-ins for `alpaca`, `alpaca.trading` and `alpaca.trading.client` into
  `sys.modules`. This works only because production resolves the import chain through exactly those three names;
  any production change (e.g. `from alpaca.trading.client import TradingClient` at module import time *before* the
  patch, or an added intermediate module) silently bypasses the fake and either hits the real SDK or errors. A
  stub installed via the seam the code actually uses (client factory/injection) would be sturdier.
- **Minor — happy path only**: `discover_alpaca_accounts` is tested solely for the success shape
  (`test_account_discovery.py:59-82`); no coverage of API failure, partial accounts, or the `is_live` flag effect
  on discovery.

**(b) Security** — none (dummy credentials `"k"/"s"` used correctly; no real secrets in the file).

**(c) Baseline smells**

- **Mysterious Name / obscurity**: `monkeypatch.setitem(__import__("sys").modules, ...)` (`:75-77`) — the
  `__import__("sys")` round-trip instead of a plain `import sys` obscures an already-delicate operation.

**(d) Maintainability hot spots**: none beyond the above; otherwise a crisp unit file.

### tests/test_account_routing_safety.py

**(a) Bugs / correctness risks**

- **Minor — test couples to a global patch point**: `test_daemon_fails_closed_before_adapter_creation_for_duplicate_ctrader_target`
  patches `daemon.asyncio.sleep` (`test_account_routing_safety.py:105-107`), i.e. the *global* `asyncio` module
  attribute reached through the daemon's import, not a daemon-owned seam. Safe under pytest's single-threaded model,
  but it mutates shared stdlib state mid-session and breaks if the daemon switches to `time.sleep` or
  `asyncio.wait_for`.
- **Minor — termination protocol is implicit**: the loop is stopped by the patched sleep raising
  `CancelledError` on first call (`:99-101`, `:117-118`). The duplicate-detection assertion is carried entirely by
  `adapter_creations == []` (`:119`); if the daemon ever stops sleeping each iteration the test fails for a
  structural reason unrelated to routing safety.

**(b) Security** — none. Positive: this file genuinely tests fail-closed routing (duplicate IDs, numerically
equivalent ctids `00123` vs `123`, adapter-never-created invariant) — some of the best safety coverage in the area.

**(c) Baseline smells** — none significant.

**(d)** — none.

### tests/test_alpaca_account_routing.py

Scanned — **nothing found**. Exemplary credential-routing tests: priority ordering (hint block beats default pair,
`:66-97`), refusal-on-ambiguity instead of fallback (`:117-134`), and explicit mutant-killing rationale in
docstrings (`:43-44`, `:99-100`). No real credentials, no mocking excess.

### tests/test_analyze_mt5_parity.py

**(a) Bugs / correctness risks**

- **Minor — every synthetic trade has `id=1`**: the `_trade` helper hardcodes `id=1`
  (`test_analyze_mt5_parity.py:16-36`), so `test_sharpe_convention_candidates_include_trade_and_daily_variants`
  builds three trades sharing one id (`:143-160`). Harmless today because matching keys on (entry_time, direction),
  but a landmine if parity logic ever keys on trade id.
- **Minor — name overstates coverage**: `test_series_stats_handles_empty_and_quartiles` (`:135-141`) asserts `n`,
  `mean`, `median` and never touches quartiles despite the name — a reader auditing quartile handling believes it's
  covered.

**(b) Security** — none.

**(c) Baseline smells** — none significant. The UTC-spelling normalization tests (`:59-71`) are exactly the kind
of high-value parity checks that catch real cross-platform bugs; good file overall.

**(d)** — none.

### tests/test_api.py

**(a) Bugs / correctness risks**

- **Major — eight tests silently pass when the data is empty** (conditional-skip bodies with no skip marker):
  `test_get_strategy_detail` (`test_api.py:38-48`), `test_get_backtest_info` (`:82-90`),
  `test_get_backtest_stats` (`:92-98`), `test_get_backtest_trades` (`:100-107`),
  `test_get_backtest_equity` (`:109-116`), `test_get_portfolio_detail` (`:129-136`) — all do
  `if <list>: ...detail-assertions` with **no else**, so an API whose detail endpoints are 500-ing for every
  resource still reports green whenever the list comes back empty (fresh checkout, cleaned data dir). Unlike
  `pytest.skip`, these leave no trace in the skip count. `test_list_strategies` (`:27-36`) partially mitigates by
  requiring `len(data) > 0`, but the dependent detail tests remain vacuous-capable.
- **Minor — benchmark tests codify ambiguity**: `test_benchmark_valid_symbol` (`:147-156`) accepts 200 *or* 404,
  and `test_benchmark_with_dates` (`:158-160`) accepts `(200, 404)` — a broken benchmark route returning 404 for
  existing data is indistinguishable from "no data downloaded".
- **Minor — redundant double assertion**: `test_get_strategy_detail_includes_parameters` (`:63-65`) asserts the
  parameters both equal `load_strategy_meta(...)` output *and* the literal `{"fast_length": 9, "slow_length": 21}`;
  the literal pins the test to one strategy's defaults, guaranteeing churn edits whenever `example_sma` changes.
- **Minor — environment dependence**: the module tests the real `viewer.main` app against repo-local data
  directories (fixture at `:19-24`); results depend on whatever backtests/symbols exist on the machine — no tmp
  isolation, so the same suite is green on one workstation and vacuous on another (ties into the D1 mechanism).
- **Minor — dead imports**: `json`, `os`, `tempfile`, `patch`, `MagicMock` imported (`:4-11`) and never used.

**(b) Security** — none (read-only GET endpoints exercised; no injection surface in tests themselves).

**(c) Baseline smells**

- **Duplicated Code**: the `resp = client.get("/api/backtests"); backtests = resp.json().get("items", [])`
  preamble repeats five times (`:74-116`); extract a fixture returning one known-good backtest id (created by the
  test, not discovered from ambient state).

**(d) Maintainability hot spots**: the combination of ambient-data dependence + conditional bodies makes this the
least trustworthy API file in the area.

### tests/test_auto_agent.py

**(a) Bugs / correctness risks**

- **Minor — the validation leg is mocked away**: `fake_run_backtest` unconditionally writes `'{"status":"ok"}'` and
  returns 0 (`test_auto_agent.py:137-142`, `:176-181`, `:210-220`), so the retry test exercises *retries on
  nonzero exit* but never the parsing/validation of real backtest output; a strategy whose backtest output the
  agent misparses would pass this suite. Accepted trade-off for unit scope, but worth one integration-level
  counterpart.
- **Minor — fixture encodes a divergent contract**: `STEP_THREE_CODE` / `FIXED_STEP_THREE_CODE` return a
  **3-tuple** from `generate_signals` (`:41-56`, `:58-72`), while the platform contract enforced elsewhere in this
  very area is 4 elements `(entries, exits, indicators, drawings)` (`test_custom_swings_supertrend.py:64-89`,
  `test_intraday_volatility_breakout.py:183-196`). Two fixtures in one repo teaching two different strategy
  signatures — whichever is wrong is enshrined as "valid" by a green test.
- **Minor — brittle string couplings**: assertions on persona names (`"The Analyst"` … `:230-235`) and exact
  emoji-decorated log lines (`:195-200`) break on prompt/copy edits that are not behavior changes.

**(b) Security** — none (LLM provider fully faked; no network calls; temp dirs via `tmp_path`).

**(c) Baseline smells**

- **Duplicated Code (light)**: identical `fake_run_backtest` closure pasted in three tests (`:137`, `:176`, `:210`);
  hoist to a factory helper.

**(d)** — otherwise a well-designed file: queue-based fake provider with loud exhaustion
(`AssertionError` when responses run dry, `:107-109`), attempt-count and per-step prompt assertions are precise.

### tests/test_auto_download.py

Scanned — **nothing found**. Two crisp mapping assertions; appropriately small.

### tests/test_backtest_equity_curve_migration.py

**(a) Bugs / correctness risks**

- **Major — tests run against the real shared database with no isolation or cleanup**: every test calls
  `init_db()` and uses the module-level `ENGINE` (`test_backtest_equity_curve_migration.py:23`, `:36`, `:86`,
  `:117`, `:162`) — i.e. the repo's actual SQLite DB, not a tmp copy. Consequences: (1) rows persist across runs
  (ids are timestamp-unique, `:45`, `:88`, `:125`, so no collision error, only silent accumulation); (2) tests are
  order- and history-dependent — `test_equity_curve_column_exists_in_backtests_table` (`:21-32`) and the migration
  test (`:160-176`) assert against whatever schema state previous runs left behind, so a *broken* migration can
  pass forever after one successful run created the column; (3) developer/local data is polluted by test records.
  Standard fix: session-scoped fixture pointing `DATA_DIR`/engine at `tmp_path_factory` (the top-level conftest
  already pins `DATA_DIR` env, so this was one `monkeypatch` away).
- **Minor — vacuous tail assertion**: `test_equity_curve_migration_on_existing_table` ends with a conditional
  read (`:172-176`) that asserts only `isinstance(result[0], str)` *if* a row exists — on an empty table the
  "verify we can read from the column" claim is unchecked; it also asserts a storage-format detail (JSON string)
  rather than behavior.
- **Minor — tz-naive datetimes**: equity points use `"2024-01-01T00:00:00"` naive strings (`:39-43`,
  `:118-121`); consistent with the storage layer, but perpetuates the naive-time convention the team's own
  checklist flags.

**(b) Security** — none.

**(c) Baseline smells**

- **Duplicated Code**: record-construction boilerplate repeated four times (`:37-60`, `:85-101`, `:116-148`);
  a `make_record()` helper would shrink the file by a third.

**(d) Maintainability hot spots**: the shared-DB coupling is the hotspot; it makes these the only tests in the
area that can permanently alter workspace state.

### tests/conftest.py

**(a) Bugs / correctness risks**

- **Major — e2e layer silently evaporates when browsers are (or appear to be) missing**:
  `pytest_runtest_setup` skips every `e2e`-marked item when `_playwright_browser_launchable` returns False with a
  "missing browser"-looking message (`tests/conftest.py:57-69`). Detection is substring-based
  (`_is_missing_browser_error`, `:34-42`: `"executable doesn't exist"` or `browser`+`not found`) applied to the
  string of **any** exception (`except Exception`, `:44-47`) — a display/sandbox/permission launch failure whose
  message happens to contain "browser ... not found" (or an unknown failure mode that still blocks launching)
  becomes a skip. Skips don't fail CI, and `tests/orchestrator.py` exits 0 with a "Skipped" column (see its
  finding), so a machine with broken Playwright shows a fully green e2e layer. At minimum: warn once per session,
  or surface skip-ratio in the orchestrator summary.
- **Minor — env pinning side effect**: `DATA_DIR`/`TRADING_DATA_DIR` default to the repo's real `data/` dir
  (`:12-14`); combined with the migration tests writing to the shared DB, test runs touch live workspace data.

**(b) Security** — none.

**(c) Baseline smells**

- **Speculative Generality (mild)**: `_requested_browser_name` handles tuple/list option shapes (`:22-26`)
  for a plugin config this repo always passes as a single string — unexercised branches.

**(d)** — the file is otherwise thoughtful (deterministic env, cached probe); the skip policy is the risk, not
the mechanics.

### tests/__init__.py

Scanned — **nothing found**. Empty package marker (0 lines), required for `from tests.validators...` imports to
resolve; correct as-is.

### tests/orchestrator.py

**(a) Bugs / correctness risks**

- **Major — layer names misrepresent what runs**: `_layer_pytest_args` (`tests/orchestrator.py:30-37`) maps
  `"validation"` → `tests/integration/test_ai_code_quality.py` and `"strategies"` →
  `tests/integration/test_strategy_pipeline.py`. Neither layer ever runs `tests/validation/` (353+ lines of
  validation-suite tests living one directory over) or `tests/strategies/` (the ~1,900-line strategy regression
  block). Meanwhile the `"unit"` selection (`tests -m "not integration and not e2e and not ai_code"`, `:37`)
  *does* include all the strategy golden-backtest modules since they carry no markers — so slow vectorbt
  backtests land in "unit" while the name "strategies" points at an API smoke test. An operator reading
  `_print_summary` output concludes the strategy regression suite and validation suite ran when they did not.
  Either rename layers to match content or fix the mappings.
- **Minor — `subprocess.run` without timeout** (`:58-66`): a hung pytest layer (e.g. the integration suite's
  120s-per-case polling multiplied by N cases, or a stuck Playwright launch) blocks the orchestrator forever —
  the Python checklist item verbatim.
- **Minor — silent summary degradation**: if `pytest-json-report` isn't installed, pytest exits with a usage
  error; the code then reports FAIL with all-zero counts and no hint that the *reporter*, not the tests, broke
  (`:47-53` reads `summary` only if the file exists).

**(b) Security** — none (subprocess args are static).

**(c) Baseline smells**

- **Mysterious Name**: layer labels as above; also `LayerResult.errors` is populated from the JSON key `"error"`
  (`:83`) while the column prints "Errors" — pytest's json-report uses `error` for setup errors; fine, but
  undocumented.

**(d)** — small utility, big blast radius: it is the entry point that decides what "green" means for the repo;
its mapping deserves to be correct or honestly named.

### tests/e2e/lab/test_conversion.py

**(a) Bugs / correctness risks**

- **Major — fallback validates a checked-in file instead of the system under test**: when no code can be scraped
  from the UI output, the test substitutes the static fixture `valid_trend_strategy.py`
  (`tests/e2e/lab/test_conversion.py:73-81`, extraction helpers at `:29-45`). The subsequent validation and
  `assert generated_code.strip()` therefore pass even when the Auto-Convert pipeline produced nothing at all —
  the one scenario this e2e test exists to catch. The fallback converts a real defect (empty output) into a
  green result; delete the fallback and fail loudly instead.
- **Minor — fragile output scraping**: task id parsed by regex over `page.locator("body").inner_text()`
  (`:54-58`) and generated code from `div.font-mono` first-match (`:60-64`); any layout/copy change breaks
  extraction, which then triggers the I1 fallback and masks the breakage as a pass — the two combine badly.
- **Minor — hardcoded base URL**: `BASE_URL = "http://localhost:5556"` (`:14`) ignores env override, unlike
  `integration/test_strategy_pipeline.py:13` (`os.getenv("BASE_URL", ...)`) — inconsistent convention between
  the two server-dependent suites.
- **Minor — repo pollution**: screenshots written into `<repo>/artifacts/screenshots` (`:20`, `:87-89`) with
  second-resolution timestamps (`int(time.time())`) that collide on retry within the same second.

**(b) Security** — none in the test itself; note it triggers server-side LLM conversion (marked `ai_code`), and
the validated code is later exec-imported by the validator (see adjacent-context note at the end).

**(c) Baseline smells**

- **Duplicated Code (ecosystem)**: a third Playwright harness variant coexists with `tests/e2e_frontend.js` and
  the pytest-playwright conftest gating — three frameworks for the same job (consolidation candidate).

**(d)** — the fallback logic is the hotspot: it was written for resilience but defeats the test's purpose.

### tests/compare_metrics_quantstats.py

**(a) Bugs / correctness risks**

- **Minor — manual harness, not a test**: no `test_*` functions and all execution under `if __name__ == "__main__"`
  (`:243+`), so pytest never collects it; and it **always exits 0** — `print_results` only prints ✓/✗
  (`:156-218`) with no non-zero exit on DIFF, so even if someone wires it into CI it can never fail. As a
  diagnostic notebook-as-script that's legitimate, but nothing prevents regressions in the metrics it inspects.
- **Minor — inconsistent rf conventions between sections**: the main comparison passes the *annual* rf to
  QuantStats with `annualize=True` (`:119-121`, documented at `:115-117`), while the deep-dive sections pass
  `rf=daily_rf` (`:237`, `:270-271`) — the QuantStats numbers printed in the two sections are different
  quantities under the same column label; a reader comparing them will draw wrong conclusions.
- **Minor — documents a suspected production bug without tracking it**: the "BUG CANDIDATE" Calmar block
  (`:104-113`, printed as "Calmar (bugged /100)" vs "Calmar (fixed)" at `:186-203`) embeds the author's belief
  that `runner.stats` divides by `max_drawdown_pct/100` incorrectly. Printing both variants is useful forensics,
  but the suspicion lives only in this throwaway script — if real, it needs a ticket/fix in `runner/stats.py`;
  if fixed, this file's labels rot.
- **Minor — dead code**: `daily_rf` computed and unused in `compare_scenario` (`:92`);
  `make_equity_curve_objs`/`make_dummy_trade` (`:41-52`) never called anywhere.

**(b) Security** — none.

**(c) Baseline smells**

- **Speculative Generality**: the two dead helper factories above.
- **Duplicated Code**: the population-vs-sample variance block and the Sortino downside block re-implement the
  statistics inline (`:221-247`, `:249-280`) instead of reusing `runner.stats` internals — intentional for a
  cross-check, but the triple repetition of `daily_rf = RISK_FREE_RATE / 252` (`:92`, `:221`, `:249`) wants a
  constant.

**(d)** — fine as a scratch instrument; risky if mistaken for coverage (it is inside `tests/`).

### tests/e2e_frontend.js

**(a) Bugs / correctness risks**

- **Major — a test that cannot fail**: `await test('No critical console errors', ...)` collects `pageerror`s
  (`tests/e2e_frontend.js:28-29`), filters favicons, prints at most three warnings, and asserts nothing
  (`:145-153`). Its name promises a check it structurally cannot perform — pure false confidence, the exact
  "asserts nothing" anti-pattern.
- **Minor — flaky fixed sleeps throughout**: `waitForTimeout(2000)` ×9 (`:51`, `:65`, `:75`, `:82`, `:130`,
  `:140`, …), `1000` (`:86`), `800` (`:98`), `3000` (`:112`) — classic timing-dependent assertions
  (width > 1400px measured after an arbitrary 2s) that flicker on slow machines instead of waiting on conditions
  (the Python e2e suite manages this correctly with `expect(...).to_be_visible(timeout=...)`).
- **Minor — vacuous conditional bodies**: the autocomplete test silently passes when the input is absent
  (`:94-103`, `if (await input.count() > 0)` with no else), and the detail-page test passes without navigating
  when no backtests exist (`:106-115`).
- **Minor — hidden cross-test state**: `'Symbol autocomplete works'` depends on the modal opened by the previous
  test (comment at `:93-94`); reorder or a prior failure cascades misleadingly.
- **Minor — near-tautological text assertions**: `text.includes('Log') || text.includes('log')` (`:138-141`)
  matches "login", "catalog", "technology"; same for the Portfolio check (`:132-135`) — these pass on virtually
  any page containing normal prose.
- **Minor — cleanup not guarded**: `browser.close()` sits outside try/finally (`:144`); any thrown error in the
  IIFE leaves the browser to be reaped by process exit via unhandled rejection — works, but untidy; and only
  `pageerror` is captured, not `console.error` (`:28-29`), further limiting the (already toothless) error test.

**(b) Security** — none (local fetches only).

**(c) Baseline smells**

- **Duplicated Code**: the goto/wait/textContent prologue repeats in ten tests; one `visitAndAssert(path, substrings)`
  helper would halve the file and remove the sleep copies.

**(d)** — as a manual smoke script it has some value; as a gated test file it mostly demonstrates the pitfalls
the review brief lists (sleeps, vacuous passes, assert-nothing tests).

### tests/integration/test_ai_code_quality.py

**(a) Bugs / correctness risks**

- **Major — the negative-control fixture passes the gate (verified empirically)**: I executed
  `AICodeValidator().validate()` on both fixtures in this repo:
  `valid_trend_strategy` → PASS, score 1.0; **`intentionally_broken_strategy` → PASS, score 1.0** (no errors, no
  warnings). The "intentionally broken" fixture defines all three required methods with correct signatures, uses
  no forbidden calls, and imports cleanly in the validator's subprocess — so nothing about it is detected as
  broken. Consequently `test_ai_generated_strategy_validates` (`tests/integration/test_ai_code_quality.py:16-20`)
  is green for the broken fixture for the *wrong reason*: the suite demonstrates no rejection path whatsoever,
  and the validator could regress to accepting outright garbage (e.g. stop checking required methods) without any
  test here noticing, as long as some valid fixture remains. Fix: make the fixture actually violate the contract
  (missing `generate_signals`, syntax error, or `eval` usage) and add an explicit
  `assert not result.passed` negative test, or rename the fixture to stop claiming to be a negative control.
- **Minor — test code mutates the source tree at import time**: `FIXTURES_DIR.mkdir(parents=True, exist_ok=True)`
  runs at module import (`:10`); if the tracked fixtures were ever lost, the parametrized test would silently run
  zero cases (glob over an empty recreated dir) — only `test_ai_generated_strategy_average_score`'s
  `assert STRATEGY_FILES` (`:24`) would catch it.
- **Minor — threshold currently decorative + duplicated work**: every fixture is validated twice (once per
  parametrized case, once in the average test `:23-34`), and `average_score >= 0.7` (`:33`) cannot bind while
  both fixtures score 1.0 — the gate adds value only after multiple fixtures fail, by which point the
  parametrized test already failed.

**(b) Security** — see adjacent-context note: the validator this test drives executes fixture code in a
subprocess (`sys.executable -I`, 8s timeout) — reasonable guardrails, not a sandbox.

**(c) Baseline smells** — light; the double-validation overlap above.

**(d)** — the missing negative control is the hotspot: the file looks like it tests rejection and doesn't.

### tests/integration/test_strategy_pipeline.py

**(a) Bugs / correctness risks**

- **Major — module-level network discovery + module-level skip**: `_DISCOVERY = _discover_strategy_matrix()` runs
  an HTTP call at *collection* time (`tests/integration/test_strategy_pipeline.py:104-105`), so even
  `pytest --collect-only` needs a live server on `BASE_URL`; when the server is absent the whole module self-skips
  via `pytest.skip(..., allow_module_level=True)` (`:109-110`). Together with the orchestrator's skip-tolerant
  exit code, the entire integration layer disappears silently on any machine without the viewer daemon running.
  Lazy discovery in a fixture (session-scoped, skipping per-module with the same message) would preserve the
  behavior while keeping collection hermetic.
- **Major — expectations so loose the test verifies little**: the sole assertion battery is
  `BacktestResultValidator().validate(backtest_result, BacktestExpectation(min_trades=0))` (`:155-158`):
  required-stats-keys presence, finiteness, `sharpe ∈ [-2, 10]` where >10 is only a *warning*
  (`tests/validators/backtest_validator.py:22-23`, `:55-61`), and `exit_time >= entry_time`. Any completed
  backtest — including one with a lookahead-inflated Sharpe of 50 or a nonsense equity curve — passes. As a
  pipeline smoke test that's defensible, but the name and the validator's presence suggest behavioral coverage
  that isn't there; at minimum `min_trades >= 1` for strategies expected to trade in a 6-month AAPL window, and
  treat suspicious-Sharpe as a failure for a fixed known strategy.
- **Minor — evicted-task edge burns the full timeout with a misleading error**: a task missing from `/api/tasks`
  is treated as "not yet present" and polled until the 120s deadline, then reported as "did not finish"
  (`:80-90`, `:98-101`) — if the tasks list prunes completed entries, a successful run is misreported as a
  timeout.
- **Minor — arbitrary coverage cap**: `unique_names[:MIN_STRATEGIES]` (`:66`) silently restricts the matrix to
  the first five alphabetically-sorted strategy slugs; adding strategies doesn't add coverage, and which five are
  covered changes with naming.

**(b) Security** — none (local API, no credentials).

**(c) Baseline smells** — none significant; polling loop is honest (`time.monotonic` deadline, last-status in the
error message).

**(d)** — the import-time HTTP call is the structural hotspot (test-order, offline-collection, and CI-skip
implications all flow from it).

### tests/fixtures/sample_strategy.py

Scanned — **nothing found**. Valid Pine v5 input fixture (EMA cross with `ta.crossover`/`ta.crossunder`);
internally consistent (inputs 9/21 match downstream expectations elsewhere).

### tests/fixtures/ai_generated_strategies/valid_trend_strategy.py

**(a) Bugs / correctness risks**

- **Minor — models state, not events**: `entries = (fast_ma > slow_ma)`, `exits = (fast_ma < slow_ma)`
  (`valid_trend_strategy.py:17-18`) are boolean *regions*, whereas the Pine source these fixtures nominally mirror
  uses crossover *events* (`sample_strategy.py:10-11`). Under an engine that re-arms entries while True, behavior
  differs materially from the intended crossover. As the canonical "valid" exemplar blessed by the AI-quality gate
  (and used as the e2e fallback payload), it teaches generators and maintainers that region-style entries are the
  house contract.

**(b) Security** — none. **(c) Smells** — none. **(d)** — none.

### tests/fixtures/ai_generated_strategies/intentionally_broken_strategy.py

**(a) Bugs / correctness risks**

- **Major (same root cause as the test_ai_code_quality finding, cited here for completeness)**: despite the name,
  the file is a fully conforming, working strategy — empirically PASSes `AICodeValidator` with score 1.0. There
  is no negative control anywhere in the AI-quality toolchain. The name actively misleads maintainers into
  believing rejection paths are covered.

**(b) Security** — none. **(c) Smells** — **Mysterious Name** in the adverse direction: the name claims a property
(code is broken) the content contradicts. **(d)** — none.

---

## Adjacent-context observations (files outside my assigned list, flagged for their owners)

- `tests/validators/code_validator.py:97-133` executes submitted/AI code via `subprocess.run([sys.executable, "-I", "-c", ...], timeout=8)`. Isolated-mode + timeout are good hygiene, but `-I` is not a security sandbox (no network/socket or filesystem restriction); any consumer treating this as safe execution of untrusted LLM output should revisit that assumption.
- `tests/validators/backtest_validator.py:22-23,55-61` treats implausibly high Sharpe (>10, "possible lookahead bias") as a warning only; combined with `min_trades=0` defaults it makes the validator close to unfalsifiable as used by the pipeline test.

## Summary judgment

The strongest material in this area: `test_alpaca_account_routing.py`, `test_account_routing_safety.py`,
`test_risk_parity.py`, `test_auto_agent.py`, and the ATR edge-case tests in
`test_intraday_volatility_breakout.py`. The weakest: the skip-heavy scaffolding around the strategy regression and
integration layers (three independent mechanisms — parquet-coverage skips, browser-launch skips, module-level
API-discovery skips — each capable of silently zeroing coverage while CI stays green), the vacuous/conditional
API and frontend tests, the shared-database migration tests, the e2e fallback that validates a fixture instead of
the product, and the negative-control fixture that isn't one. The dominant maintenance cost is the 14×
copy-pasted strategy regression template.

