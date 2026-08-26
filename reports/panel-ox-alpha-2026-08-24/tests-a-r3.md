# Panel review — area `tests-a` (chunk A of test suite) — reviewer r3 (ox-alpha)

Blind, independent review of the 39 tracked files listed for this area. Read-only; nothing edited.
Findings cite `file:line` with severity critical/major/minor. Files scanned clean are recorded as such.

## Working notes

(incremental — appended per group)

---

# Findings

## Group 1 — suite infrastructure & fixtures

### `tests/conftest.py`

(a) Bugs/correctness risks
- **minor** `tests/conftest.py:13-14` — `os.environ.setdefault("DATA_DIR", ...)` only sets the var when absent. If the developer/CI machine already exports `DATA_DIR` pointing elsewhere (or an earlier import of `shared.constants` resolved `DATA_DIR` before conftest runs), every data-dependent test silently targets a different tree than intended. Because `shared.constants.DATA_DIR` is imported at module scope by consumers (`tests/strategies/conftest.py:11`), env mutation in conftest is order-sensitive. Deterministic intent stated in the comment is not guaranteed.
- **minor** `tests/conftest.py:42-55` — `_playwright_browser_launchable` is `lru_cache`d and *launches a real browser* once per browser name at first e2e test. Correct idea, but the probe result also caches **transient** failures (e.g. a one-off zombie-browser lock or display hiccup) for the whole session, turning a recoverable environment into permanent skips. Also `except Exception` (line 54) swallows everything including KeyboardInterrupt-adjacent base errors from playwright internals; message-based skip detection then depends on Playwright error prose (`_is_missing_browser_error`, lines 34-39) which is brittle across Playwright versions — if wording changes, missing browsers surface as hard **errors** instead of skips, or worse, any unrelated exception containing "browser … not found" silently skips real tests.
- **minor** `tests/conftest.py:66-69` — skip message says `playwright install firefox` unconditionally while the skipped browser may be chromium/webkit depending on parametrization/cLI option (line 17-31 resolves any browser name). Copy-paste in the guidance string.

(b) Security — nothing found.
(c) Smells
- **Speculative Generality (mild)** `tests/conftest.py:24-30` — handles `browser` option being list/tuple/str defensively; the plugin defines one shape. Harmless but untested defensive branching.

(d) Maintainability — acceptable overall; the launchable-probe helper is the one piece worth isolating/testing.

### `tests/orchestrator.py`

(a) Bugs/correctness risks
- **major** `tests/orchestrator.py:38` — the "unit" layer runs `pytest tests -m "not integration and not e2e and not ai_code"`. The strategies-layer regression suites under `tests/strategies/` carry **no markers** (verified across all `tests/strategies/test_*.py` — none sets `pytestmark`), so they run inside the "unit" layer via full-network/engine backtests, and the marker contract implied by this filter does not exist. Conversely `pytest -m integration` style selection cannot select them. Layer taxonomy is fictional relative to actual marks → misleading CI signal about what "unit" means.
- **major** `tests/orchestrator.py:63-66` — if pytest exits non-zero because the JSON report plugin is missing or crashed mid-run, `report_path.exists()` is False and `summary={}` → the layer reports `passed=0 failed=0 skipped=0 errors=0` with status derived solely from exit code. A red run shows as "FAIL 0/0/0/0", and more importantly a **green-looking zero-count row** can hide a collection crash where pytest still exits 0 (e.g. `--json-report-file` unwritable dir would raise inside pytest-json-report after tests pass). Counts are never cross-checked against `summary.get("total", ...)`.
- **minor** `tests/orchestrator.py:104` — layer execution order is validation→strategies→unit→e2e, but the CLI choices/help imply the natural order; cosmetic only.
- **minor** `tests/orchestrator.py:55-60` — `subprocess.run` without `timeout=`; a hung pytest layer blocks the orchestrator forever (Python checklist: subprocess without timeout).
- **minor** `tests/orchestrator.py:36` — e2e layer hardcodes `--browser firefox` while `tests/conftest.py:68`'s skip hint mentions firefox too, but `e2e_frontend.js` (see below) drives its own browser choice; two sources of truth for which browser e2e uses.
- **minor** `tests/orchestrator.py:75` — reads `summary.get("error", 0)`; pytest-json-report emits key `"errors"` for setup errors count in some versions — worth verifying against the pinned plugin version, else Errors column always 0.

(b) Security — nothing found (no shell=True, args are fixed lists).
(c) Smells
- **Duplicated Code / Repeated Switches (mild)** `tests/orchestrator.py:30-39` — the layer→args mapping is fine, but each layer's identity (name, paths, extra flags) is split between this function and the `choices=` list at line 97; adding a layer requires touching both plus `requested_layers` at 103-105 (Shotgun Surgery for a new layer).
(d) Maintainability — the tool is useful; needs timeout + total-count sanity check.

### `tests/test_auto_download.py`

(a) Bugs/correctness risks
- **major** `tests/test_auto_download.py:10-11` — test name says *"US stock intraday still uses intraday stock plan"* but asserts `_auto_download_source("AAPL","1h") == "huggingface"`. The name describes the **plan-splitting behavior** (`_auto_download_source_plan`, which splits HF coverage ≤2025-07-31 with Alpaca ≥2025-07-01), while the assertion only checks the single-source label. The plan function's boundary logic — including the overlap window 2025-07-01…2025-07-31 where **both** huggingface and alpaca steps are emitted (`data_tools/auto_download.py:377-391`) — has **zero test coverage**, despite being exactly what the test title claims to cover. Misleading green test over genuinely risky date-boundary logic.
- **minor** `tests/test_auto_download.py:4-7` — only index-CFD aliases tested; `_is_us_intraday_stock` guards (`data_tools/auto_download.py:338` `endswith("USD")`, forex 6-letter rule colliding with tickers like "GOOG"→len6 alpha is treated forex! see below) untested.
- Related production-risk note surfaced while verifying expectations (not part of area, flagged for panel): `_is_forex_symbol` (`data_tools/auto_download.py:305`) classifies **any 6-letter uppercase symbol** (e.g. real tickers like `GOOGL` is 5 so safe, but e.g. `BRK.B` no; six-letter NYSE tickers do exist historically) as forex → wrong downloader. Tests don't pin this edge.

(b) Security — nothing found.
(c) Smells — file is minimal; fine.
(d) Maintainability — should be renamed/refocused on `_auto_download_source_plan`.

### `tests/fixtures/sample_strategy.py`

(a) — This is Pine Script source used by the e2e lab-conversion test; syntactically valid v5 EMA-cross strategy. No issues found. Note only: filename ends `.py` while containing Pine Script (intentional — it feeds the converter), but it will be picked up by any `glob("*.py")` over `tests/fixtures/`; currently harmless because `ai_generated_strategies` is a sibling dir, not this one.
(b) Security — nothing found.
(c) Smells — **Mysterious Name (minor)**: `.py` extension for Pine content invites accidental Python imports/globs; a `.pine` extension plus explicit read path would be safer.
(d) Maintainability — fine.

### `tests/fixtures/ai_generated_strategies/valid_trend_strategy.py`

(a) Bugs/correctness risks
- **minor** `tests/fixtures/ai_generated_strategies/valid_trend_strategy.py:18` — entries are `(fast_ma > slow_ma)` — i.e. **state-based** ("in an uptrend"), not crossover events. Combined with the pipeline validator accepting `min_trades=0`, these fixtures validate code-quality scoring only, never trading semantics. Not a bug per se, but the fixture name suggests a trend-*strategy* whose entry logic would double-enter every bar in engines that treat entries as level-triggered. Acceptable as fixture; noted because `test_strategy_pipeline.py` backtests engine="native" against repo strategies, not these.
(b) Security — nothing found.
(c) Smells — none beyond above.
(d) Maintainability — fine.

### `tests/fixtures/ai_generated_strategies/intentionally_broken_strategy.py`

(a) Bugs/correctness risks
- **major (misleading fixture)** `tests/fixtures/ai_generated_strategies/intentionally_broken_strategy.py:1-19` — the file named "intentionally broken" is **functionally valid**: well-formed module, correct signature, returns proper `(entries, exits)` Series. Its only "brokenness" is semantic (threshold 0.0 momentum logic). It is consumed by `tests/integration/test_ai_code_quality.py:16-20`, which parametrizes over **every** `*.py` in the fixtures dir and asserts **each passes** validation (`result.passed`). So the fixture designed to prove the AI validator rejects bad code instead asserts it accepts it — there is **no negative test anywhere** for `AICodeValidator` rejection in this suite. If `AICodeValidator` regressed to accept anything (score floor removed, syntax check dropped), every test here stays green. That is false confidence at the exact point the suite exists to guard.
(b) Security — nothing found.
(c) Smells — **Misleading Name** on the fixture; either add a genuinely-invalid fixture + negative assertions, or rename.
(d) Maintainability — the glob-driven parametrization (`test_ai_code_quality.py:11`) silently changes test count when files are added/removed; combined with the above, deleting the broken fixture would go unnoticed.

### `tests/integration/test_ai_code_quality.py`

(a) Bugs/correctness risks
- **major** `tests/integration/test_ai_code_quality.py:16-20` — as above: the only per-fixture assertion is positive (`result.passed`); the intentionally-broken fixture is asserted *valid*. Negative-path coverage of the validator is absent → validator regressions undetectable.
- **major** `tests/integration/test_ai_code_quality.py:23-33` — `test_ai_generated_strategy_average_score` duplicates the exact same loop and re-asserts `all(result.passed …)` (line 33) — identical to the parametrized test — before computing the average. The *actual* distinctive assertion (average score ≥ 0.7) is one line; the rest is duplicated code that turns a scoring-threshold test into a third copy of the validity test. If individual scores were {1.0, 1.0, 0.1} the average gate could still trip correctly, but the redundant `all(passed)` makes failure messages misleading (reports validity errors when the subject is the score).
- **minor** `tests/integration/test_ai_code_quality.py:9-10` — `FIXTURES_DIR.mkdir(parents=True, exist_ok=True)` at import time: a **test module creating production-tree directories** during collection. Side effect at import is unnecessary (the dir is tracked) and can mask packaging errors where fixtures were excluded from a deploy — the mkdir would resurrect the empty dir and the `assert STRATEGY_FILES` at line 24 would then fail with a confusing message.
- **minor** `tests/integration/test_ai_code_quality.py:34` — division guarded only implicitly by line 24's assert; fine today since STRATEGY_FILES non-empty is checked, OK.

(b) Security — nothing found.
(c) Smells — **Duplicated Code** (line 26-32 vs 18-20); import-time side effect.
(d) Maintainability — small file, clear intent; needs negative-case fixtures.

### `tests/integration/test_strategy_pipeline.py`

(a) Bugs/correctness risks
- **critical (vacuous assertion)** `tests/integration/test_strategy_pipeline.py:155-158` — the end-to-end pipeline test validates each discovered strategy's completed backtest with `BacktestExpectation(min_trades=0)`. With defaults that means: stats keys present, sharpe ∈ [-2, 10], **any number of trades including zero**, trades-list merely well-formed. A strategy that produces **zero trades and flat equity** — the classic silent pipeline break (bad signal alignment, empty dataframe, all-NaN indicators) — **passes**. For an integration test whose entire job is proving "strategy → run → task → backtest id → result" works, asserting `min_trades=0` reduces it to a schema smoke test while reading like a functional one. At minimum a known-good subset of strategies should require ≥1 trade.
- **major** `tests/integration/test_strategy_pipeline.py:104-110` — module-level discovery executes network I/O at **import time**; if the API is down the whole module self-skips (`allow_module_level=True`). Reasonable for optional integration, but combined with `pytest.mark.integration` (not deselected in default local runs by default unless `-m` filters are used — and `tests/orchestrator.py:38` excludes it from "unit"), developers running plain `pytest tests/` will hit live HTTP calls against localhost:5556 with 20s timeouts and 120s polls whenever a server happens to be running — flaky-by-environment design.
- **major** `tests/integration/test_strategy_pipeline.py:71-96` — `_poll_task_completion`: if `/api/tasks` payload doesn't contain the task (current_task None), it sleeps and retries until timeout, then raises AssertionError mentioning only "did not finish". A **404/never-listed task** is indistinguishable from a slow task, and the final message reports `last=unknown` even though the real cause was "task never appeared in listing" — misleads debugging. Also `response.raise_for_status()` inside the loop (line 77) converts a transient 502 into an immediate HTTPStatusError instead of retry — poll loop isn't resilient to blips, causing flakes.
- **minor** `tests/integration/test_strategy_pipeline.py:46,66` — `MIN_STRATEGIES = 5` gates the whole matrix; a registry regression dropping to 4 discoverable strategies skips the entire module rather than failing — environment drift hides as "skipped".
- **minor** `tests/integration/test_strategy_pipeline.py:59-62` — hardcoded 2024 H1 window; data-dependent results (trade counts) drift as local datasets change, but with min_trades=0 that risk is moot — see vacuous assertion above.

(b) Security — nothing found (localhost assumption; no secrets handled).
(c) Smells
- **Excessive mocking/vacuity** is inverted here — no mocks at all — but the expectation object is so loose it approximates one.
- **Data Clump (minor)** `case` dict keys repeated at 117-127; a small dataclass would prevent key typos.
(d) Maintainability — good structure otherwise (discovery/poll/assert separated cleanly).

### `tests/e2e/lab/test_conversion.py`

(a) Bugs/correctness risks
- **critical (self-defeating fallback)** `tests/e2e/lab/test_conversion.py:74-82` — if no generated code can be extracted from the agent output, the test **falls back to reading a known-good fixture file** (`valid_trend_strategy.py`) and validates *that*. The subsequent assertions (`generated_code.strip()` line 80, `AICodeValidator().validate` line 84-88) therefore pass even when the lab conversion feature produced **nothing at all** — the core behavior under test. An agent that crashes immediately, returns garbage without a fenced block, or writes output the regex misses, yields a green test validating fixture text. This is precisely a "fixture hiding production bug": the fallback should at minimum mark the extraction as degraded (e.g. assert extraction succeeded, use fallback only for validator-shape smoke).
- **major** `tests/e2e/lab/test_conversion.py:67` — `page.locator("div.font-mono").first` grabs the first monospace div on `/lab`; if the page renders any other mono block above the agent output (log pane, editor gutter), the test reads the wrong element, finds no "Starting strategy conversion agent" within 60 s, and fails — or worse, matches an empty/stale container whose `.inner_text()` yields "" and triggers the fallback path above. Selector is fragile and coupled to styling classes.
- **minor** `tests/e2e/lab/test_conversion.py:61-65` — parses the task id out of rendered body text with a regex instead of using app state/API; brittle to copy changes (acknowledged trade-off for e2e, but combined with `.first` on body-wide `get_by_text` a second matching hint elsewhere flips which id wins).
- **minor** `tests/e2e/lab/test_conversion.py:12` — hardcoded `BASE_URL` ignoring the `BASE_URL` env var honored by `tests/integration/test_strategy_pipeline.py:13`; running the two integration layers against a non-default host silently breaks only one of them.
- **minor** `tests/e2e/lab/test_conversion.py:86` — strategy_name includes `int(time.time())`, making the validated name unique per run; fine, but screenshots likewise timestamped (line 90) accumulate unbounded in `artifacts/screenshots` with no cleanup.
- **minor** `tests/e2e/lab/test_conversion.py:44` — no timeout guard around total runtime; a slow agent keeps the browser session open indefinitely (Playwright default timeouts bound individual expects, but the sum is minutes; acceptable but worth noting vs the orchestrator's lack of subprocess timeout).

(b) Security — nothing found (screenshots of internal app into repo-relative artifacts dir; mild info-disclosure hygiene only).
(c) Smells
- **Mysterious Name (minor)** `_extract_generated_code` also reads files from disk based on agent output text (`path_match`, lines 30-34) — a test-side file-read driven by untrusted UI text; surprising side effect in a "extractor" (and a subtle path-injection vector if the agent echo'd an arbitrary path — low impact locally, but the test would read and validate arbitrary repo files).
(d) Maintainability — the fallback logic must go or be explicitly asserted-against.

## Group 2 — `tests/strategies/` part 1

### `tests/strategies/conftest.py`

(a) Bugs/correctness risks
- **major** `tests/strategies/conftest.py:117` — autouse fixture is `scope="module"` but performs **skip decisions from module attributes**; correct, however `_covers_requested_period` (lines 87-114) treats coverage as satisfied if **any single candidate file** covers start and end — with multiple parquet shards unioned via min/max (lines 92-101), **gaps between shards are invisible**: shard A covering Jan–Mar and shard B covering Dec satisfy a Apr–Nov request. Skip-gating built on a union-of-extents check gives false "covered" verdicts; the gated tests then fail deep inside engines with confusing errors (or worse, silently backtest on partial windows if engines clip).
- **minor** `tests/strategies/conftest.py:27-29` — for `timeframe == "1d"` it globs `**/{SYMBOL}.parquet` as canonical daily data. Any symbol-shaped file (e.g. metadata exports) counts as OHLCV coverage for `_has_ohlcv_data` (line 34-35), which only checks existence, not readability/schema → skip gate can wrongly *not* skip on corrupt files, deferring failure to the test body. Contrast with `_path_time_coverage` which does read timestamps — but only when start/end bounds are requested.
- **minor** `tests/strategies/conftest.py:71,80` — bare `except Exception` returning `(None, None)` on unreadable parquet: a corrupt dataset silently becomes "no coverage" → skip, hiding data-corruption regressions as environmental skips (swallowed exception around decision phase).
- **minor** `tests/strategies/conftest.py:58-65` — end-bound expansion adds one day minus 1µs for date-only strings; correct for UTC-daily bars, but for intraday timeframes a request `end=2025-06-30` against data ending `2025-06-30 21:00` passes although the last *session* may be partial; acceptable heuristic, undocumented.

(b) Security — nothing found.
(c) Smells — **Duplicated Code**: `_to_utc_datetime`/coverage logic re-implements `data_tools/auto_download.py` `_normalize_coverage_date`/`has_ohlcv_bars_in_range` (same domain, subtly different semantics — union-of-extents vs per-file bar check). One shared helper would prevent divergence (already diverged: auto_download checks bars-in-range per file; conftest unions extents).
(d) Maintainability — central gating is a good idea; correctness of the coverage check is the hot spot.

### `tests/strategies/test_intraday_volatility_breakout.py`

(a) Bugs/correctness risks
- **major (tautology)** `tests/strategies/test_intraday_volatility_breakout.py:233-236` — `days.nunique() == len(entry_indices)` proves only that no two entries share a calendar day **when entry_indices is empty too** (0==0 passes). The comment says "At most one entry per day"; with the constructed flat-then-breakout series the test never verifies ≥1 entry occurred, so a strategy that stopped producing entries entirely (regression!) passes. Should assert `len(entry_indices) >= 1`.
- **minor** `tests/strategies/test_intraday_volatility_breakout.py:205-212` — comment claims "2-day synthetic dataset" but 300 minutes from 16:00 spans ~5 hours — a single day. The day-count premise of the test is wrong (works only because assertion is vacuous, see above).
- **minor** `tests/strategies/test_intraday_volatility_breakout.py:497-508` — `entry_reference_price` compared to `df["close"].iloc[entry_idx + 1]` twice (lines 501-503 and 506-508) — duplicated assertion, second is dead weight (copy-paste remnant).
- **minor** `tests/strategies/test_intraday_volatility_breakout.py:538-540` — asserts `entry_reference_price ≈ df.loc[entry_idx, "high"]` while the candle-close test (line 501) asserts it equals **next bar close**; both can be legitimately true under different modes, but nothing distinguishes which mode each test exercises in the assertion message; a future change to reference-price semantics will produce contradictory failures with no diagnostic. Documentation-level issue.
- **minor** `tests/strategies/test_intraday_volatility_breakout.py:36,43` — `np.random.seed(42)` global seeding inside helper (fine for determinism) but `open_/high/low` construction can violate `low <= open,close <= high`? Verified: high = max(o,c)+|N|·ε, low = min(o,c)−|N|·ε — invariant holds. OK.
- **minor** `tests/strategies/test_intraday_volatility_breakout.py:12` + `sys.path.insert` — repo-root path injection at import; consistent across strategy tests but each module mutates `sys.path` independently (repeated 15+ times across the suite); pytest rootdir/conftest should own this (**Shotgun Surgery** smell).
(b) Security — nothing found.
(c) Smells — duplicated next-bar-close assertion (above); magic timestamps throughout acceptable for fixtures.
(d) Maintainability — strongest file in the strategies folder; keep.

### Regression-template family: `test_channel_breakout.py`, `test_gold_momentum.py`, `test_cross_asset_momentum.py`, `test_mean_reversion_rsi_bb.py`, `test_post_vix_dip_buy.py`, `test_short_term_reversal.py`, `test_trend_following.py`, `test_volatility_breakout.py`, `test_stat_arb_pairs.py`, `test_sector_rotation.py`, `test_pine_superosc_vumanchu.py`, `test_pine_supertrend_dema.py`, `test_pynecore_orb.py`, `test_pynecore_sma_crossover.py`, `test_qg_30min_es_breakout_strategy.py`, `test_custom_swings_supertrend.py`

Shared findings (identical template copied 16×):

- **major (Duplicated Code)** — the four-test class (`test_trade_count`, `test_net_profit`, `test_all_trades_have_sl_in_meta`, `test_no_trade_exceeds_risk_limit`) plus fixture is byte-for-byte duplicated in every file with only constants swapped (~70 lines × 16). A fix to the risk-check math must be applied 16 times; history shows divergence already (qg/custom variants differ slightly — see below). Extract a shared base/parametrized harness keyed on constants.
- **major (brittle golden values / environment coupling)** — every file pins exact trade counts and net profit to $0.50 tolerance (`math.isclose(..., abs_tol=0.50)`), dependent on local parquet data contents, vectorbt version, and float summation order. These are labeled "Regression-Test … zukuenftige Core-Updates … nicht unbemerkt brechen" — but they equally fire on benign data refreshes and library bumps, training maintainers to regenerate constants blindly (encoding whatever changed as "correct"). The doc header even forbids changing the period (`NICHT verändern`) without stating why. Tolerance of 0.50 on sums of dozens of float pnls (gold_momentum sums 29 trades) is tighter than float associativity guarantees across platforms — flake-prone.
- **major (risk-limit check can pass vacuously)** `test_no_trade_exceeds_risk_limit` in all template copies (e.g. `tests/strategies/test_channel_breakout.py:65-76`) — computes `equity` cumulatively but **only divides by current equity**, asserting ≤1.5%; when `sl_price` exists but quantity sizing came from another field the check is real, yet when a trade's `meta["sl_price"]` is far from entry (wide stop), risk_pct grows — fine — but the equity variable starts at literal `100_000.0` even when `BacktestConfig.initial_capital` differs in future edits (hardcoded duplication of the config value; qg variant at `test_qg_30min_es_breakout_strategy.py:74` already fixed this to `backtest_result.meta.initial_capital`, the other 15 copies did not — **divergent copies confirm the maintenance hazard**). Additionally the check ignores whether the engine actually sized positions off sl_price; it validates arithmetic, not the risk model.
- **minor (copy-paste doc rot)** `tests/strategies/test_qg_30min_es_breakout_strategy.py:8` — header claims `Symbol/Timeframe: XAUUSD / 1h` for a strategy **named ES breakout** (an index-futures strategy) with TEST_START `2006-01-02` — 2006 XAUUSD 1h data availability is dubious; constants look inherited from gold_momentum template and possibly never re-validated ("Validiert am: 2026-05-07" notwithstanding). Wrong-symbol constants would make this test skip forever (via conftest data gate) while appearing maintained.
- **minor** `tests/strategies/test_custom_swings_supertrend.py:130-144` — this variant **silently skips** risk verification when `sl_price` is None **or NaN** (`if sl is not None: if not _math.isnan(...)`): trades without stops contribute zero risk assertions, whereas sibling files assert `sl is not None`. Two different guarantees under the same test name across the family — the weaker one can hide missing-stop regressions. Also re-imports `math as _math` inside loop (line 136) though `math` is already module-imported.
- **minor** `tests/strategies/test_custom_swings_supertrend.py:81-87,157-163` — `np.random.uniform` **without seeding** in two tests: indicator-key/meta-shape assertions are deterministic, but any future assertion touching values would flake; also random OHLC violates `low<=open/close<=high` (open∈[1800,2000] vs low∈[1700,1800] can produce open<low) — currently harmless because only shapes are asserted, but the fixture is a landmine.
- **minor** `tests/strategies/test_custom_swings_supertrend.py:88` — `mod.generate_signals(df)` invoked **without params argument**, unlike production call sites that pass parameter dicts; if `generate_signals` requires params in the engine path, the AST/default-presence tests exercise a call convention the engine never uses.
- **minor** — several family members duplicate `sys.path.insert(0, project_root)` (e.g. `test_channel_breakout.py:17`) — see Shotgun Surgery note above.
- File-specific: `tests/strategies/test_stat_arb_pairs.py:59-62` additionally checks `ZScore` in meta — good, unique among family.

### `tests/strategies/test_volatility_breakout_reference_logic.py`

(a) Bugs/correctness risks
- **minor** `tests/strategies/test_volatility_breakout_reference_logic.py:28-29` — asserts `indicators["direction"].iloc[5] == "LONG"` then checks `pd.notna(indicators["stop_loss"].iloc[8])` for the SHORT entry but `sl_price` for LONG — asymmetric key usage (`sl_price` vs `stop_loss`) suggests one of the two is a typo'd key that happens to exist; if the strategy emits both keys this passes while asserting nothing about consistency between them.
- **minor** `tests/strategies/test_volatility_breakout_reference_logic.py:25` — hard-coded entry indices `[5, 8]` with handcrafted bars — good precision, but no assertion that exits align to specific bars (`exits.any()` only, line 30): exit timing regression (e.g. exits one bar late) undetected.
(b) Security — nothing found.
(c) Smells — none significant.
(d) Maintainability — compact, precise; strengthen exit assertions.

### `tests/strategies/test_risk_parity.py`

(a) Bugs/correctness risks
- **minor** `tests/strategies/test_risk_parity.py:87-90` — crisis-regime assertion accepts `"elevated"` **or** `"crisis"`: a regression collapsing all non-normal regimes to "elevated" passes. Documented compromise; still weakens the guarantee given the very next test asserts regime-dependent de-risking only for "crisis".
- **minor** `tests/strategies/test_risk_parity.py:93-98` — `adjust_for_regime` crisis test asserts equity total strictly decreases but places **no bound** on how much (could go to zero or negative weights); combined with line 96 filtering by `ASSETS[t]["class"]`, a bug that zeroes *all* weights passes.
- **minor** `tests/strategies/test_risk_parity.py:169-178` — heavy monkeypatching of five internals of `run_backtest` couples the test to implementation names; refactor of `run_backtest` internals (e.g. renaming `apply_tail_risk_hedge`) breaks the test with AttributeError rather than a meaningful failure — acceptable for deterministic unit testing, noted as maintenance cost. Monkeypatched lambdas also drop keyword-arg passthrough (`lambda *args, **kwargs:` fine).
- **minor** `tests/strategies/test_risk_parity.py:190-192` — filters trades by `exit_time.startswith("2024-01-02")` string prefix; timezone representation change (e.g. `+00:00` suffix format change) silently empties the lists and then line 206's `len(jan_trades)==len(TICKERS)` fails with an opaque count mismatch — brittle parsing of datetime-as-string.
(b) Security — nothing found.
(c) Smells — none major; the file is a decent example of deterministic unit testing of portfolio math.
(d) Maintainability — fine.

*(group 2 continues in next sections)*

## Group 3 — remaining root-level test files

### `tests/test_account_discovery.py`

(a) Bugs/correctness risks
- **major** `tests/test_account_discovery.py:59-81` — `test_discover_alpaca_accounts_sets_broker_login` stubs `sys.modules` entries for `alpaca`, `alpaca.trading`, `alpaca.trading.client` via `monkeypatch.setitem`. This only works if the production code does `import alpaca.trading.client` **inside** the function; if `live_runner/account_discovery.py` (or anything imported earlier in the pytest session) already imported the real module, the real `TradingClient` may be used and attempt live network auth — or conversely, once any other test in the session imports `alpaca.*` for real, this stub leaks ordering dependencies. Fragile sys.modules surgery with no guard that the real SDK stays unimported. Also constructs credentials `"k"`, `"s"` inline — fine as dummies.
- **minor** `tests/test_account_discovery.py:75-77` — the fake module graph is incomplete relative to typical import chains (`alpaca.common` etc. often touched); works today by luck of import placement inside function bodies — an implementation-detail coupling worth a comment or a fixture.
- Everything else in the file is well-targeted unit testing of pure helpers (`_ctrader_account_name`, `_message_to_debug_fields`, host/env matching) with meaningful negative cases — good.

(b) Security — nothing found (no real secrets; env vars monkeypatched).
(c) Smells — none significant.
(d) Maintainability — fine apart from the sys.modules fragility above.

### `tests/test_account_routing_safety.py`

(a) Bugs/correctness risks
- **minor** `tests/test_account_routing_safety.py:108-150` — `test_daemon_fails_closed_before_adapter_creation_for_duplicate_ctrader_target` monkeypatches `daemon.asyncio.sleep` globally to raise CancelledError. If `sync_deployments_loop` calls `asyncio.sleep` anywhere *before* the duplicate check (e.g. a startup delay), the test would fail for the wrong reason; currently passes because validation precedes sleeping, but the technique asserts "no adapter created", not "fails closed at config load" — the failure point isn't pinned (a refactor moving validation after first sleep flips the error source silently while keeping the same assertion green).
- **minor** `tests/test_account_routing_safety.py:139-148` — relies on `asyncio.CancelledError` propagating out of `asyncio.run`; if daemon catches cancellation internally for cleanup, `pytest.raises` fails spuriously. Acceptable but brittle-by-design.
- Positive checks (duplicate id / numerically-equivalent ctid rejection, lines 18-76) are genuinely valuable safety tests — good coverage of fail-closed routing.

(b) Security — nothing found; these tests actively guard account-misrouting risk. Good.
(c) Smells — none major.
(d) Maintainability — good.

### `tests/test_alpaca_account_routing.py`

Scanned in full. This is a model test file: each case documents the mutant it kills ("Kills mutant: default key/secret pair short-circuits…"), covers precedence orderings (hint block > default pair > blocks[0]) and the ambiguous-hint fail-closed case (`credentials is None`). No findings beyond:
- **minor** `tests/test_alpaca_account_routing.py:23,63,82` — asserts paper flag as bare boolean tuple element (`("rocket-key","rocket-secret",True)`); a swap of tuple arity/order in production would surface as equality failure — acceptable, though named fields/dataclass would give better diagnostics.
(b) Security — nothing found (fake keys only).
(c/d) Nothing further.

### `tests/test_analyze_mt5_parity.py`

(a) Bugs/correctness risks
- **minor** `tests/test_analyze_mt5_parity.py:16-44` — `_trade()` helper hardcodes `id=1` for every trade; harmless here because matching helpers key on entry_time+direction, but any future assertion on trade identity would be ambiguous.
- **minor** `tests/test_analyze_mt5_parity.py:115-122` — `test_top_session_minutes_uses_athens_local_time` asserts Athens-local buckets (14:45Z→16:45) but never pins the timezone conversion explicitly against a winter-date DST edge (Jan dates are CET=UTC+2 — correct); a July entry would need +3. Single-season coverage of a DST-sensitive function; not wrong, just thin.
- **minor** `tests/test_analyze_mt5_parity.py:143-171` — sharpe-candidates test asserts all three conventions produce non-None values and one ordering relation (`deals > trades` variant); it never compares against expected numeric ranges, so a systematic scaling bug (e.g. ×√252 dropped everywhere) passes as long as ordering holds.
(b) Security — nothing found.
(c) Smells — none significant.
(d) Maintainability — decent helper-based structure.

### `tests/test_api.py`

(a) Bugs/correctness risks
- **critical (condition-skip pattern → tests that can pass without testing)** `tests/test_api.py:38-48` — `test_get_strategy_detail` guards the entire body behind `if strategies:` — with an empty registry the test body never executes and the test **passes**. Same anti-pattern in `test_get_backtest_info/stats/trades/equity` (`:82-115`, all guarded by `if backtests:`) and `test_get_portfolio_detail` (`:129-135`, `if portfolios:`). These are precisely the endpoints where an empty database (fresh clone, broken DB init, wrong DATA_DIR) yields silent green results — false confidence on the core read API.
  - Contrast `test_list_strategies` (`:27-36`) which correctly asserts `len(data) > 0`.
- **major** `tests/test_api.py:147-156` — `test_benchmark_valid_symbol` accepts either 200-with-body or 404 (`if resp.status_code == 200: … else: assert resp.status_code == 404`) and `test_benchmark_with_dates` (`:158-160`) accepts `in (200, 404)` — the endpoint can regress to always-404 and every benchmark test stays green. Combined with the condition-skips above, most of this file cannot fail on functional regressions.
- **major** `tests/test_api.py:18-23` — the `client` fixture builds `TestClient` on the **real app with no isolation**: no tmp DATA_DIR override per-test (conftest's `setdefault` won't help if env pre-set), no seeded fixtures, so every assertion above reads whatever the developer machine happens to have in `data/`. Tests are environment-oracle rather than fixed-fixture based; two machines can disagree.
- **minor** `tests/test_api.py:9` — imports `patch, MagicMock` unused — dead imports hinting removed mocking (dead test utility).
- **minor** `tests/test_api.py:15` — another copy of `sys.path.insert(0, project_root)` (redundant under pytest rootdir handling).
- **minor** `tests/test_api.py:52-53` — `next((item for item in strategies if item["slug"] == ...), None)` then `assert target is not None` — depends on repo shipping `example_sma` strategy; acceptable pin, but if renamed the message gives no hint of available slugs.
- **minor** `tests/test_api.py:177-182` — `test_symbols_has_content` requires downloaded market data present (`len(data["symbols"]) > 0`) without skip-guard rationale; fails confusingly on fresh environments instead of skipping like data-gated tests elsewhere.

(b) Security — nothing found.
(c) Smells
- **Duplicated Code** `tests/test_api.py:82-115` — four near-identical "fetch list then fetch detail" bodies begging for a parametrized helper.
(d) Maintainability — this file needs fixture seeding + removal of conditional skips; highest fix-value-to-effort ratio in area.

### `tests/test_auto_agent.py`

(a) Bugs/correctness risks
- **minor** `tests/test_auto_agent.py:131-135` etc. — `fake_run_backtest` parses `cmd[cmd.index("--output") + 1]`; if production stops passing `--output`, `.index` raises ValueError inside the fake — failure mode is a crash in the stub rather than a clear contract message. Cosmetic.
- **minor** `tests/test_auto_agent.py:203-239` — retry test asserts call counts and prompt contents thoroughly (good), including traceback feedback to step-3 prompt (line 239). No issue found; noting the file overall mocks at the right seam (LLM provider + subprocess) without becoming vacuous.
- **minor** `tests/test_auto_agent.py:167-199` — the "long code" test's distinctive assertions are provider kwargs (`timeout_seconds == 600.0`) and log strings containing emoji-prefixed messages (lines 197-199). String-matching log prose couples tests to wording; wording change = test churn. Acceptable but brittle.
- **minor** `tests/test_auto_agent.py:242-254` — `test_normalize_strategy_meta_accepts_list_tags` asserts exact dict equality on tags; fine. No issue.
(b) Security — nothing found (provider fully faked; no network).
(c) Smells — none significant.
(d) Maintainability — good seams; keep.

### `tests/test_auto_download.py` — covered in Group 1.

### `tests/test_backtest_equity_curve_migration.py`

(a) Bugs/correctness risks
- **major** `tests/test_backtest_equity_curve_migration.py:21-31,160-169` — both "migration" tests call `init_db()` against whatever `shared.database.ENGINE` resolves to (repo-default SQLite path from env at import time) and assert the column exists. On any machine where the DB was already migrated (the normal case after first run), these tests verify nothing about migration logic — they re-verify current schema state. There is no setup that creates a **pre-migration** schema and runs the migration; the actual upgrade path (existing table lacking `equity_curve`) is never exercised. Test names promise more than they deliver — misleading green on the risky code path.
- **major** — the whole file mutates the developer's **real application database**: records written at lines 62-64, 104-106, 149 are inserted into the shared ENGINE target with timestamped ids and never cleaned up (no teardown, no transaction rollback, no tmp_path DB). Repeated runs accumulate garbage rows in the production-shaped store, and assertions like line 173's `SELECT … LIMIT 1` read arbitrary prior rows. Tests should use an isolated temp SQLite file.
- **minor** `tests/test_backtest_equity_curve_migration.py:176-177` — `if result and result[0]: assert isinstance(result[0], str)` — conditional assertion: when column is empty/NULL the type check is skipped silently (same anti-pattern family as test_api).
- **minor** `tests/test_backtest_equity_curve_migration.py:45,88,125` — uniqueness via `%f` microseconds of `datetime.now` — two rapid calls within the same microsecond collide; practically unlikely, but `uuid4()` is the idiomatic choice.
(b) Security — nothing found.
(c) Smells — conditional assertion (above); **Resource hygiene**: shared mutable DB state across tests (ordering-dependent suite).
(d) Maintainability — rewrite around tmp DB + explicit pre-migration schema fixture.

### `tests/e2e_frontend.js`

(a) Bugs/correctness risks
- **critical (assert-nothing test)** `tests/e2e_frontend.js:148-154` — `'No critical console errors'`: collects page errors across the whole run, filters favicon, then… only logs them. The final `assert` is absent — the test **cannot fail** by construction (comment admits "We don't fail on console errors"). It inflates the passed count with a guaranteed-green row. Either assert `critical.length === 0` or delete the test.
- **critical (conditional-pass pattern)** `tests/e2e_frontend.js:92-102` — `'Symbol autocomplete works'`: whole body wrapped in `if (await input.count() > 0)`. If the modal didn't open (previous test failed to click) or placeholder changed, the test silently passes having tested nothing. Same pattern at `:107-116` (`'Shows stats and equity chart'`, `if (items.length > 0)`) — with zero backtests the detail-page test passes without visiting any detail page.
- **major** `tests/e2e_frontend.js:80-90` — 'Create Backtest modal opens' depends on state produced by the *previous* test (modal opened there), and `'Symbol autocomplete works'` (`:93` comment "Modal should still be open from previous test") chains three tests on hidden shared browser state. Any earlier failure cascades misleadingly; also violates test independence.
- **major** `tests/e2e_frontend.js:51,65,75,86,98,112,130,140` — blanket `waitForTimeout(2000)` sleeps everywhere: classic flaky timing pattern — slow machines flake, fast machines waste ~20s per run. Playwright auto-waiting/`expect.poll` exists precisely for this.
- **major** `tests/e2e_frontend.js:107-123` — 'Shows stats and equity chart' navigates to a backtest detail page, then 'Benchmark dropdown exists' (`:118-123`) asserts selects/Benchmark text **on whatever page the previous test left open** — its own `goto` is absent; if the preceding conditional skipped navigation, this asserts against `/backtests` list page and can pass/fail arbitrarily depending on data.
- **minor** `tests/e2e_frontend.js:56` — width threshold `>1400` hardcoded against a 1920 viewport; on smaller CI viewports everything fails for layout reasons unrelated to regressions.
- **minor** `tests/e2e_frontend.js:13-22` — custom `test()` harness swallows error stack (only `err.message`), and process exit code is computed from `failed` counter — OK, but no per-test isolation or retries; acceptable for a smoke script.
- **minor** `tests/e2e_frontend.js:34` — collects only `pageerror`, not console `error` events despite the test being named "console errors".
(b) Security — nothing found (local app only).
(c) Smells
- **Duplicated Code** — repeated goto+sleep+textContent pattern throughout; a `gotoAndSettle(path)` helper plus expect-poll would halve the file.
- **Mysterious Name** — 'No critical console errors' name vs no assertion.
(d) Maintainability — candidate for replacement by the Python Playwright e2e suite which does this properly (`tests/e2e/lab/test_conversion.py`).

### `tests/compare_metrics_quantstats.py`

(a) Bugs/correctness risks
- **major** — this is a **manual diagnostic script, not a test**: it defines comparisons and thresholds (`abs(diff) <= 0.01` → "✓ OK"/"✗ DIFF") but has **zero assertions** — running it always exits 0 regardless of diffs (`if __name__ == "__main__": print_results(...)` only, lines 203-214). Under pytest collection, pytest will try to import it; nothing breaks because no `test_` functions exist — meaning it contributes nothing to CI and its "Status" columns are advisory ink. If the intent was regression protection for metrics math, the protection is illusory.
- **major (documents a suspected production bug without fixing or testing it)** `tests/compare_metrics_quantstats.py:105-112` — computes `our_calmar_bugged = annualized_return / abs(mdd_fraction / 100)` labeled "BUG CANDIDATE: divides by max_drawdown_pct/100". Cross-checking production `runner/stats.py`: `_max_drawdown_pct_manual` returns a **fraction** (`runner/stats.py:412-424`, docstring "positive fraction (0.05 = 5%)") and `compute_stats` feeds it directly to `_safe_calmar_ratio` (`runner/stats.py:143-144,152,161`, which divides by `drawdown` as-is, `:370-375`) — i.e. production Calmar divides by the fraction, which matches this script's **fixed** branch. So production looks correct today, but the script keeps a stale "bugged" narrative alive; anyone reading the script may "fix" stats.py into the actual bug (dividing fraction by 100). The dead `our_calmar_bugged` computation should be deleted or the script turned into a real parametrized pytest with tolerances.
- **minor** `tests/compare_metrics_quantstats.py:92,221,249` — `daily_rf = RISK_FREE_RATE / 252` computed and unused in `compare_scenario` (line 92) and recomputed-but-unused inside deep-dives; dead locals.
- **minor** `tests/compare_metrics_quantstats.py:30-38` — `equity_to_returns_series` default index uses business-day range starting 2020-01-02 regardless of scenario dates — fine for qs, but the deep-dive Sharpe at line 237 passes `rf=daily_rf` to quantstats while the main comparison at line 117 passes annual rf with `annualize=True` — the two call conventions differ deliberately (deep-dive explains deannualization) yet nothing asserts consistency between them; a reader can't tell which convention production uses from this script alone (production: `rf=risk_free_rate, periods=252`, `runner/stats.py:126-137` — consistent with line 117).
- **minor** `tests/compare_metrics_quantstats.py:58` — module-level RNG seeding then re-seeding at 71/79 — harmless but redundant.

(b) Security — nothing found.
(c) Smells
- **Speculative Generality / Dead Code** — `our_calmar_bugged` pipeline (lines 105-108, 140-145, 182-193) kept "for comparison" indefinitely.
- **Mysterious Name** — file lives in tests/ but is neither collected nor asserted; name doesn't say "manual tool".
(d) Maintainability — convert to `test_compare_metrics_quantstats.py` with `pytest.approx` bounds or move to `scripts/`.

### Family members verified individually (constants/headers read in full)

- `tests/strategies/test_cross_asset_momentum.py:59-64` — meta check asserts `Composite_Momentum`; `test_sector_rotation.py` (lines 59-63) asserts `Momentum_63`. Both fine.
- `tests/strategies/test_trend_following.py`, `test_pine_supertrend_dema.py` — headers consistent with constants (US500/1h, 2025 Q2/Q3). No file-specific issues beyond family findings.
- **minor** `tests/strategies/test_qg_30min_es_breakout_strategy.py:30-33` — TEST_START `2006-01-02` with XAUUSD 1h: predates plausible local dataset depth; if data gate skips, EXPECTED values are never exercised (silent coverage loss). Also only this member passes explicit `data_dir` (`:50`) — all other 15 members rely on ambient DATA_DIR; inconsistent isolation.
- Marker registration note: `pytest.ini:2-8` registers e2e/integration/unit/slow/ai_code; grep confirms **no** strategies-family or root-level account/api files carry marks — corroborates the orchestrator "unit layer" finding above.

## Per-file accounting (complete area list)

| File | Status |
|---|---|
| `tests/__init__.py` | Scanned — empty file, nothing found. Note: exists only to make `tests` a package so `from tests.validators…` imports resolve; required, fine. |
| `tests/compare_metrics_quantstats.py` | Findings above (assertion-less diagnostic script; stale "bugged Calmar" narrative). |
| `tests/conftest.py` | Findings above. |
| `tests/e2e/lab/test_conversion.py` | Findings above (self-defeating fixture fallback is the headline). |
| `tests/e2e_frontend.js` | Findings above (assert-nothing console test, conditional passes, sleep-based waits). |
| `tests/fixtures/ai_generated_strategies/intentionally_broken_strategy.py` | Findings above (fixture is not broken; enables validator false-confidence). |
| `tests/fixtures/ai_generated_strategies/valid_trend_strategy.py` | Findings above. |
| `tests/fixtures/sample_strategy.py` | Findings above (Pine content under `.py` name). |
| `tests/integration/test_ai_code_quality.py` | Findings above (no negative-path validator coverage; duplicated loop). |
| `tests/integration/test_strategy_pipeline.py` | Findings above (`min_trades=0` vacuous integration assertion). |
| `tests/orchestrator.py` | Findings above. |
| `tests/strategies/conftest.py` | Findings above (union-of-extents coverage check hides gaps). |
| `tests/strategies/test_channel_breakout.py` | Regression-family findings (template anchor). |
| `tests/strategies/test_cross_asset_momentum.py` | Regression-family findings; meta key verified correct. |
| `tests/strategies/test_custom_swings_supertrend.py` | Regression-family findings **plus** weaker silent-skip risk check, unseeded random fixtures, param-less generate_signals calls. |
| `tests/strategies/test_gold_momentum.py` | Regression-family findings. |
| `tests/strategies/test_intraday_volatility_breakout.py` | Findings above (one-trade-per-day tautology). |
| `tests/strategies/test_mean_reversion_rsi_bb.py` | Regression-family findings. |
| `tests/strategies/test_pine_superosc_vumanchu.py` | Regression-family findings. |
| `tests/strategies/test_pine_supertrend_dema.py` | Regression-family findings; header constants consistent. |
| `tests/strategies/test_post_vix_dip_buy.py` | Regression-family findings. |
| `tests/strategies/test_pynecore_orb.py` | Regression-family findings (PyneCore variant with `(trade.meta or {})` guards — slightly safer than vectorbt siblings, more divergence evidence). |
| `tests/strategies/test_pynecore_sma_crossover.py` | Regression-family findings (PyneCore variant). |
| `tests/strategies/test_qg_30min_es_breakout_strategy.py` | Regression-family findings **plus** wrong-symbol/2006-window header suspicion; the improved equity-init handling exists only here (divergence proof). |
| `tests/strategies/test_risk_parity.py` | Findings above. |
| `tests/strategies/test_sector_rotation.py` | Regression-family findings; meta key verified correct. |
| `tests/strategies/test_short_term_reversal.py` | Regression-family findings. |
| `tests/strategies/test_stat_arb_pairs.py` | Regression-family findings (+ ZScore meta check, good). |
| `tests/strategies/test_trend_following.py` | Regression-family findings; header constants consistent. |
| `tests/strategies/test_volatility_breakout.py` | Regression-family findings. |
| `tests/strategies/test_volatility_breakout_reference_logic.py` | Findings above (asymmetric sl_price/stop_loss keys; exit timing untested). |
| `tests/test_account_discovery.py` | Findings above (sys.modules stub fragility). |
| `tests/test_account_routing_safety.py` | Findings above (minor); safety tests themselves are valuable. |
| `tests/test_alpaca_account_routing.py` | Scanned in full — model quality; 1 minor diagnostic note. Clean otherwise. |
| `tests/test_analyze_mt5_parity.py` | Findings above (thin DST/sharpe-scale assertions). |
| `tests/test_api.py` | Findings above (conditional-skip pattern across backtest/portfolio endpoints; either-or benchmark assertions). |
| `tests/test_auto_agent.py` | Findings above (all minor; well-seamed mocking). |
| `tests/test_auto_download.py` | Findings above (title/assertion mismatch leaves plan-splitting logic untested). |
| `tests/test_backtest_equity_curve_migration.py` | Findings above (no pre-migration schema exercised; writes into shared real DB). |

## Top priorities for the panel

1. **`tests/e2e/lab/test_conversion.py:76-78`** — fallback to known-good fixture makes the flagship e2e conversion test unable to detect conversion failure (critical).
2. **`tests/e2e_frontend.js:148-154, 92-102, 107-116`** — assert-nothing + conditional-pass tests (critical).
3. **`tests/test_api.py:38-135`** — conditional skips let core API tests pass on empty data (critical).
4. **`tests/integration/test_strategy_pipeline.py:155-158`** — `min_trades=0` reduces the pipeline e2e to schema checking (critical).
5. **`tests/integration/test_ai_code_quality.py` + intentionally_broken fixture** — AI-code validator has no negative test anywhere (major).
6. **16× duplicated regression template with $0.50 golden values and divergent fixes** (major, systemic).
7. **`tests/test_backtest_equity_curve_migration.py`** — never exercises pre-migration path; pollutes real DB (major).

## Assumptions

- Reviewed blind; did not open any other reviewer's reports.
- Production cross-checks were limited to verifying test expectations against `data_tools/auto_download.py`, `runner/stats.py`, and `shared/models.py` behavior where a test's correctness depended on it.
- German-language assertions treated as repo standard (not renamed as a smell).
- Severity scale: critical = test can pass while the behavior it claims to verify is broken; major = misleading confidence, flaky-by-design, or significant maintenance hazard; minor = robustness/clarity/duplication debt.

---

## Finding counts (by severity, textual findings in this report)

Counted as individual finding bullets above (the e2e_frontend.js conditional-pass bullet covers two test sites; test_api.py's condition-skip bullet covers five test methods — each bullet counted once):

- critical: 5
- major: 24
- minor: 60