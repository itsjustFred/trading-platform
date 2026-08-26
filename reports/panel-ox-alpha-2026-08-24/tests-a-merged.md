## tests-a

**Panel:** model ox-alpha; three blind, independent reviewers (r1, r2, r3), synthesized by ox-alpha.
Contradictions between reviewers (and against the gpt-5.6-luna reference report) were settled by reading the cited source under `tests/`, not by vote; every settlement is annotated inline. Findings marked *(verified)* were confirmed directly against the code during synthesis.

**Finding totals (this merged section): critical 4 · major 19 · minor 71 — 94 findings across 39 tracked files.**
Reviewer baselines for calibration: r1 = 0C/22Maj/47min, r2 = 0C/~9Maj/~40min (no explicit tally), r3 = 5Crit/24Maj/60min. Divergent severities are recalibrated inline with reasons.

**Coverage:** all 39 tracked files in this chunk were scanned by all three reviewers and are accounted for in the per-file table at the end. Files with no findings are listed as clean rather than dropped.

---

## Theme 1 — Silent-skip architecture: suite infrastructure (`tests/conftest.py`, `tests/orchestrator.py`, `tests/strategies/conftest.py`)

Three independent mechanisms can zero out large parts of the suite while runs stay green. All three reviewers independently identified this as the highest-impact structural problem in the area.

### tests/strategies/conftest.py

- **M1 — Major — autouse fixture silently skips entire strategy modules** *(verified)* — `tests/strategies/conftest.py:117-138`: `skip_when_required_data_or_dependency_missing` (`autouse=True`, module scope) skips every module whose parquet dataset under `DATA_DIR/ohlcv` is missing or lacks requested coverage. There is no marker, no aggregated warning, no strict-mode opt-out, and no surfaced skip count; on any machine without the local datasets the whole ~16-file regression block degrades to skips behind a green exit code, and `tests/orchestrator.py` reports the skip column without failing. Textbook "environment-dependent disabled tests" pattern. Fix direction (r1/r2 agreed): emit a session-end summary of data-skips or support a fail-on-missing-data env flag. (r1+r2+r3 concur; r1 cites `:100-138`, r2 `:79`.)
- **M2 — Major — coverage check unions extents, hiding gaps between shards** *(verified)* — `tests/strategies/conftest.py:87-114`: `_covers_requested_period` takes min(start)/max(end) across *all* candidate files, so shard A covering Jan–Mar plus shard B covering Dec satisfies an Apr–Nov request. Gated tests then fail deep inside engine code with confusing errors — or silently backtest clipped windows. Also asserted against the union, not the file actually used downstream. (r3 found it; corroborated by reference-report t5; consistent with r1's `:76-96` variant of the same observation.)
- **M3 — Major — swallowed exceptions convert data corruption into "skip"** *(verified)* — `tests/strategies/conftest.py:68-84` (`_path_time_coverage`, two `except Exception: return None, None` blocks): a corrupt/unreadable parquet (permission error, schema change like a renamed `timestamp` column) becomes "no coverage" → module skip, hiding data-corruption regressions as environmental unavailability. At minimum log the exception. (r1+r2+r3 concur.)
- **m28 — Minor — `1d` fallback counts any symbol-shaped file as coverage** *(verified)* — `tests/strategies/conftest.py:27-29,34-35`: for `timeframe == "1d"` any `<SYMBOL>.parquet` matches, and `_has_ohlcv_data` checks existence only (no readability/schema), so the skip gate can wrongly *not* skip on a junk file and defer failure into the test body.
- **m26 — Minor — redundant glob pattern** — `tests/strategies/conftest.py:24`: `root.rglob(f"**/{symbol}/{timeframe}.parquet")` — `rglob` already recurses; the `**/` prefix is a no-op that confuses readers. (r2 only; harmless.)
- **m27 — Minor — end-bound expansion heuristic is undocumented and partial-session-blind** — `tests/strategies/conftest.py:58-65`: date-only `end` gains one day minus 1µs; correct for UTC-daily bars, but for intraday timeframes a request `end=2025-06-30` passes although the last session may be partial; an end given *with* a time component excludes the rest of that day, surprising for inclusive-calendar reasoning. (r1 `:53-59` + r3 `:58-65`; same mechanism, merged.)
- Smell *(verified)*: the coverage/normalization logic re-implements domain logic that also exists in `data_tools/auto_download.py` (`_normalize_coverage_date`/bars-in-range) with subtly different semantics (union-of-extents vs per-file bar check) — already diverged; one shared helper would prevent drift (r3).

### tests/orchestrator.py

- **M6 — Major — layer names misrepresent what executes** *(verified)* — `tests/orchestrator.py:30-38`: `"validation"` → `tests/integration/test_ai_code_quality.py`; `"strategies"` → `tests/integration/test_strategy_pipeline.py`. Neither layer ever runs `tests/validation/**` (353+ lines) or the actual `tests/strategies/**` regression block. The real strategy suites land in the `"unit"` selection (`tests -m "not integration and not e2e and not ai_code"`) purely because they carry **no markers** — r3 verified via grep that no `tests/strategies/*` file sets `pytestmark` and `pytest.ini` registers only e2e/integration/unit/slow/ai_code. An operator reading `_print_summary` concludes the strategy and validation suites ran when they did not. Rename layers to match content or fix the mappings. (r1+r2+r3 concur — strongest consensus finding in the area alongside M1.)
- **M7 — Major — failed/broken layers render as zero-count rows** *(verified)* — `tests/orchestrator.py:63-76`: if pytest dies before writing the JSON report (plugin missing → usage error; unwritable report dir crashing the reporter after tests passed), `summary={}` and the layer reports `FAIL/PASS 0/0/0/0` derived solely from exit code, with no cross-check of `summary["total"]` against actual collection. Triage-active misinformation. (r1 `:63-66` + r2 `:47-53` + r3 `:63-66`; r3 additionally notes a green-looking zero-count row can hide a collection crash.)
- **m5 — Minor — `subprocess.run` without timeout** *(verified)* — `tests/orchestrator.py:55-60`: a hung layer (integration suite's 120 s-per-case polling × N cases, stuck Playwright launch) blocks the orchestrator forever. **Severity disagreement flagged:** the reference report rates the identical issue **major** (T3); the panel unanimously rates it minor because this is a manually invoked entry-point script, not a wired CI stage. The defect itself is real and all three reviewers + reference agree on the mechanism.
- **m6 — Minor — unguarded `json.loads` on the report** — `tests/orchestrator.py:65`: a truncated/corrupt report crashes the orchestrator mid-summary instead of being reported as a layer failure. (r1.)
- **m7 — Minor — `"error"` vs `"errors"` JSON key** — `tests/orchestrator.py:75` reads `summary.get("error", 0)`; depending on the pinned pytest-json-report version setup-error counts may be emitted as `"errors"` — worth verifying, else the Errors column is always 0. (r3; r2 notes the same mapping is at least undocumented.)
- **m8 — Minor — layer identity duplicated across the file; e2e browser pinned twice** *(verified)* — the layer→args mapping at `:30-39` must be kept in sync with `choices=` at `:97` and the order list at `:104` (Shotgun Surgery for adding a layer); separately `:36` hardcodes `--browser firefox` while `tests/e2e_frontend.js` picks its own browser — two sources of truth. Cosmetic layer-order note (r3 `:104`) folded here.

### tests/conftest.py

- **m2 — Minor — Playwright probe: cached transient failures + prose-based skip detection** *(verified; settles an r1/r2/r3 disagreement)* — `tests/conftest.py:34-55`: `_playwright_browser_launchable` is `lru_cache`d and launches a real browser once per name; a one-off zombie-lock/display hiccup is cached for the whole session (r3, r1). Skip gating keys off substrings `"executable doesn't exist"` / `browser`+`not found` (`:34-39`). **Recalibration note:** r2 rated the overall skip policy **major** ("a machine with broken Playwright shows a fully green e2e layer"), r1/r3 rated it minor. Reading the code settles it toward minor-with-teeth: when the probe returns False and the message does *not* match, `pytest_runtest_setup` (`:58-69`) does **not** skip — the test proceeds and fails loudly at browser launch, so unknown failure modes surface as errors, not silent skips. Only message-matching failures skip, which is the intended purpose; the residual risks are (a) transient failures cached permanently and (b) Playwright wording changes flipping intended skips into hard errors across versions. Hence minor, not major.
- **m1 — Minor — env pinning is order-sensitive and aims at the real data dir** *(verified)* — `tests/conftest.py:13-14`: `os.environ.setdefault("DATA_DIR", ...)` applies only when absent; if the machine pre-exports `DATA_DIR` (or `shared.constants` was imported earlier), data-dependent tests silently target another tree, while by default they target the repo's real `data/` — combined with the DB-writing migration tests (M8) test runs touch live workspace data. (r3 + r2, merged — same two lines.)
- **m3 — Minor — skip hint hardcodes firefox** — `tests/conftest.py:66-69` always suggests `python -m playwright install firefox` even when the resolved browser (`:17-31`) is chromium/webkit. (r3.)
- **m4 — Minor — speculative option-shape handling** — `tests/conftest.py:24-30` defensively handles list/tuple `browser` options the plugin never produces. (r2 `:22-26` + r3; unexercised branches.)

---

## Theme 2 — Tests that cannot fail: vacuous and conditional passing

### tests/test_api.py

- **C1 — Critical — six endpoint tests execute zero assertions when the store is empty** *(verified)* — `tests/test_api.py:38-48, 82-115, 129-136`: `test_get_strategy_detail`, `test_get_backtest_info`, `test_get_backtest_stats`, `test_get_backtest_trades`, `test_get_backtest_equity`, `test_get_portfolio_detail` all wrap their bodies in `if <list>:` with **no else** — on a fresh checkout, cleaned data dir, or broken DB init the core read API is never exercised yet reported green, leaving no trace in the skip count (unlike `pytest.skip`). `test_list_strategies` (`:27-36`) correctly requires `len(data) > 0`, proving the authors knew the pattern. (r3 critical, r2 major, r1 major; reference t6 agrees on mechanism at minor — panel keeps **critical**: this is the definition of "passes while the claimed behavior is unverified", and it spans the entire detail-API surface.)
- **m42 — Minor — benchmark tests codify ambiguity** *(verified)* — `tests/test_api.py:147-156,158-160`: `test_benchmark_valid_symbol` accepts 200-with-body *or* bare 404; `test_benchmark_with_dates` accepts `(200, 404)`. The endpoint can regress to always-404 and every benchmark test stays green. (All three reviewers; reference t7 concurs. Rated minor rather than major by panel consensus since a dedicated `test_benchmark_nonexistent` pins true 404 behavior at `:162-164`.)
- **m43 — Minor — double assertion guarantees churn** *(verified)* — `tests/test_api.py:58-65`: `test_get_strategy_detail_includes_parameters` asserts parameters equal `load_strategy_meta(...)` output *and* the literal `{"fast_length": 9, "slow_length": 21}` — the literal repins the test to one strategy's defaults on every `example_sma` edit. (r2.)
- **m44 — Minor — ambient environment oracle, no seeding/isolation** *(verified)* — `tests/test_api.py:18-23`: the fixture builds `TestClient` on the real `viewer.main` app against whatever the developer machine has in `data/`; no tmp `DATA_DIR`, no seeded records, so the suite is meaningful on one workstation and vacuous on another, and results shift as other tests write backtests (ties directly into C1's trigger condition). (r1 `:30-33` + r2 + r3.)
- **m45 — Minor — dead imports** *(verified)* — `tests/test_api.py:3-9`: `json`, `os`, `tempfile`, `patch`, `MagicMock` imported, never used — remnants of removed mocking. (r2+r3.)
- **m46 — Minor — opaque pin on `example_sma`** — `tests/test_api.py:52-53`: `next((item for item in strategies if item["slug"] == ...), None)` + `assert target is not None` gives no hint of available slugs when the strategy is renamed. (r3.)
- **m47 — Minor — `test_symbols_has_content` has no skip-guard rationale** — `tests/test_api.py:177-182` requires downloaded market data present and fails confusingly on fresh environments, inconsistently with the data-gated skips used elsewhere. (r3.)
- Smell: the `resp = client.get("/api/backtests"); backtests = resp.json().get("items", [])` preamble repeats five times (`:82-115`) — extract a fixture that creates a known-good backtest rather than discovering one from ambient state (r1+r2).

### tests/e2e_frontend.js

- **C2 — Critical — "No critical console errors" structurally cannot fail** *(verified)* — `tests/e2e_frontend.js:148-154`: collects `pageerror`s all run, filters favicons, prints up to three warnings, then asserts nothing ("We don't fail on console errors — just report"). A guaranteed-green row inflating the pass count; the name promises a check the body cannot perform. Either `assert(critical.length === 0)` or remove it from the tally. (r3 critical, r2 major, r1 major; reference t1 agrees on mechanism at minor. Panel escalates to critical per the "asserts nothing" criterion.)
- **C3 — Critical — conditional-pass tests: autocomplete and detail page** *(verified)* — `tests/e2e_frontend.js:92-102`: `'Symbol autocomplete works'` wraps its whole body in `if (await input.count() > 0)` with no else — if the modal from the previous test closed or the placeholder changed, the test silently passes having done nothing. Same pattern at `:107-116`: `'Shows stats and equity chart'` passes without navigating when zero backtests exist. Knock-on: `'Benchmark dropdown exists'` (`:118-123`) has **no `goto` of its own** and asserts selects/Benchmark text on whatever page the previous (possibly skipped) test left open — arbitrary pass/fail depending on data. (r3 critical covering `:92-116`, r1/r2 minor on the same sites; panel adopts critical — identical anti-pattern family as C1.)
- **M15 — Major — hidden cross-test state chains three tests** *(verified)* — `'Create Backtest modal opens'` (`:80-90`) opens the modal that `'Symbol autocomplete works'` depends on (comment at `:93`), and the detail-page pair (`:107-123`) shares navigation state; any earlier failure cascades misleadingly and violates test independence. (r3 major; r2 minor on the same mechanism — escalated because the chain crosses the C3 conditional boundary, making failures actively misleading.)
- **M14 — Major — blanket fixed sleeps throughout** *(verified)* — `waitForTimeout(2000)` ×9 (`:51,:65,:75,:82,:98,:112,:130,:140`…), `1000` (`:86`), `800` (`:98`), `3000` (`:112`): classic flaky timing — slow machines intermittently fail layout/text assertions measured after arbitrary waits, fast machines waste ~20 s/run; Playwright auto-waiting/`expect.poll` exists for this. (r3 major, r2 minor — kept major: nine sites across every test in the file makes the whole harness flaky-by-design.)
- **m71 — Minor — near-tautological text assertions** — `text.includes('Log') || text.includes('log')` (`:138-142`) matches "login", "catalog", "technology"; likewise the Portfolio check (`:128-133`). Pass on virtually any prose-bearing page. (r2.)
- **m72 — Minor — viewport-coupled layout threshold** — `width > 1400` (`:56`) hardcoded against the 1920 viewport (`:30`); smaller CI viewports fail for layout reasons unrelated to regressions. (r3.)
- **m73 — Minor — collects only `pageerror`, not console `error` events** — `:34` — despite the flagship test being named "console errors", further limiting the already toothless C2. (r2+r3.)
- **m74 — Minor — hygiene: cleanup and config** — `browser.close()` sits outside try/finally (`:157`; an IIFE throw relies on process exit to reap the browser), and `BASE = 'http://localhost:5556'` (`:9`) ignores env override. Homegrown runner also duplicates what the pytest-playwright layer provides and is invisible to `tests/orchestrator.py` summaries (framework-level duplication smell, r1+r2).

### tests/integration/test_strategy_pipeline.py

- **M11 — Major — import-time network discovery + module-level self-skip** *(verified)* — `tests/integration/test_strategy_pipeline.py:104-110`: `_DISCOVERY = _discover_strategy_matrix()` performs HTTP at **collection time** (even `pytest --collect-only` needs a live server), and any failure triggers `pytest.skip(..., allow_module_level=True)`. Combined with the orchestrator's skip-tolerant exit codes, the entire integration layer disappears on any machine without the viewer daemon; plain `pytest tests/` runs hit live localhost HTTP whenever a server happens to be up. Lazy session-scoped-fixture discovery preserves behavior hermetically. (r1 minor / r2 major / r3 major — kept major; the collection-time side effect is the structural hotspot all three identify.)
- **M12 — Major — `min_trades=0` reduces the pipeline test to schema checking** *(verified)* — `tests/integration/test_strategy_pipeline.py:155-159`: the sole assertion battery is `BacktestResultValidator().validate(result, BacktestExpectation(min_trades=0))` — stats-keys presence, finiteness, `sharpe ∈ [-2,10]` (>10 only a warning per `tests/validators/backtest_validator.py`), `exit_time >= entry_time`. A strategy producing **zero trades and flat equity** — the classic silent break — passes, while the test reads as functional coverage. **Severity disagreement flagged and settled:** r3 rates this **critical**, r1/r2 minor/major. Panel sets **major**: the critique is fully verified, but the test *is* honest about being a smoke layer (marked integration+slow, validator warnings visible), unlike C1/C3 where the test names promise verification it cannot perform. Recommended fix (all reviewers): require `min_trades >= 1` for a known-good subset of strategies in the fixed 2024-H1 AAPL window.
- **m59 — Minor — registry regression becomes a skip** — `:46-53,16`: `MIN_STRATEGIES = 5`; dropping to 4 discoverable strategies skips the whole module instead of failing — environment drift hides as "skipped". (r2+r3.)
- **m60 — Minor — evicted tasks burn the full timeout with a misleading error; poll loop not blip-resistant** — `:71-101`: a task absent from `/api/tasks` is treated as "not yet present" until the 120 s deadline, then reported "did not finish (last=unknown)" even though the real cause was eviction; `raise_for_status()` inside the loop (`:77`) turns a transient 502 into immediate abort instead of retry. (r2+r3; reference t3's "fixed sleep polling" note is superseded by this more precise version — the loop does use a `time.monotonic()` deadline, which r1 confirms.)
- **m61 — Minor — arbitrary alphabetical cap** — `:45,66`: `unique_names[:MIN_STRATEGIES]` restricts the matrix to the first five sorted slugs; adding strategies adds no coverage and which five run shifts with naming. (r2.)
- **m62 — Minor — fixed 2024-H1 window** — `:61-62`: data-dependent results drift as local datasets change (currently moot given M12's vacuity). (r3.)
- **m63 — Minor — discovery folds all errors into skip** — `:25-31`: `except Exception` around discovery converts auth/config errors into the same module-level skip (same silent-skip family as M1/M3; reference t2 concurs). (r1.)
- Smell: the `case` dict's eight keys repeated at `:113-127` want a small dataclass to prevent typos (r3).

### tests/e2e/lab/test_conversion.py

- **C4 — Critical — fallback validates a checked-in fixture instead of the system under test** *(verified)* — `tests/e2e/lab/test_conversion.py:74-88`: when `_extract_generated_code` yields nothing, the test substitutes `valid_trend_strategy.py` and validates *that*; the subsequent `assert generated_code.strip()` (`:80-82`) and `AICodeValidator().validate(...)` therefore pass even when the Auto-Convert pipeline produced **nothing** — precisely the scenario this flagship e2e test exists to catch. Extraction races, agent crashes, or regex misses all convert to green-by-fixture. Delete the fallback or assert extraction succeeded before validating. (r3 critical, r1 major, r2 major; reference T1 agrees at major — panel keeps critical: the test's sole purpose defeated by construction. Verified the fallback branch and the fixture path resolve to a real, valid file.)
- **m65 — Major→Minor — fragile output selector** *(verified)* — `:67-72`: `page.locator("div.font-mono").first` grabs the first monospace div on `/lab`; any other mono block above the agent output breaks extraction — which then routes into C4's fallback and masks the breakage as a pass. r3 rated major, r2 minor; panel sets **minor** only because its harm is entirely mediated by C4 — fixing C4 removes the masking, leaving ordinary selector brittleness. The two combine badly today, which is why both are kept.
- **m66 — Minor — task id scraped by regex from rendered body text** — `:61-65`: brittle to copy changes; `.first` on a body-wide match means a second matching hint flips which id wins. (r2+r3.)
- **m67 — Minor — extraction race** — `:74`: `output_text` captured immediately after "Starting strategy conversion agent" becomes visible while LLM generation may still be streaming into `output_area` — intermittent empty extractions push runs into the C4 fallback. (r1.)
- **m68 — Minor — hardcoded base URL, inconsistent convention** *(verified)* — `:12` ignores `os.getenv("BASE_URL")` honored by `tests/integration/test_strategy_pipeline.py:13`; running the layers against a non-default host silently breaks only one suite. (r1+r2+r3.)
- **m69 — Minor — screenshot naming collides and accumulates** — `:86,90`: names keyed on `int(time.time())` collide on retries within the same second; screenshots accumulate unbounded in `<repo>/artifacts/screenshots` with no cleanup. (r1+r2+r3.)
- **m70 — Minor — extractor reads disk paths found in UI text** — `:30-34`: `path_match` reads any `*.py` path echoed by agent output — surprising side effect in an "extractor" and a low-impact path-injection vector (the test would validate arbitrary readable repo files). (r3.)

## Theme 3 — Strategy regression template: 16 copy-pasted files under `tests/strategies/`

The family: `test_channel_breakout.py`, `test_gold_momentum.py`, `test_mean_reversion_rsi_bb.py`, `test_post_vix_dip_buy.py`, `test_short_term_reversal.py`, `test_trend_following.py`, `test_volatility_breakout.py`, `test_stat_arb_pairs.py`, `test_sector_rotation.py`, `test_cross_asset_momentum.py`, `test_pine_superosc_vumanchu.py`, `test_pine_supertrend_dema.py`, `test_pynecore_orb.py`, `test_pynecore_sma_crossover.py` (14 pure-template files), plus the divergent variants `test_qg_30min_es_breakout_strategy.py` and `test_custom_swings_supertrend.py`. Shared findings cited once at the template anchor (`tests/strategies/test_channel_breakout.py`) unless file-specific.

- **M4 — Major — golden-master snapshots are unfalsifiable and environment-coupled** *(verified)* — every file pins exact trade count and net profit with `math.isclose(..., abs_tol=0.50)` (anchor: `tests/strategies/test_channel_breakout.py:28-29,53-57`), captured from one run against a specific local parquet snapshot ("Validiert am: …"). Any data refresh, fee/fill change, library bump, or float summation-order drift flips these red with no signal whether strategy or data changed; conversely, because of M1 the tests simply don't run in most environments. Net effect: brittle-false-red or silently-absent. The `$0.50` tolerance on sums of dozens of float PnLs is tighter than float associativity across platforms; docstrings forbid changing the period ("NICHT verändern") without stating why, training maintainers to regenerate constants blindly. Per-file pins recorded by r1 for churn ranking (all major as instances of M4): gold_momentum `:31-32` ($15,439.32 / 29 trades), cross_asset_momentum `:30-32` (2-year window, 21 trades), mean_reversion_rsi_bb `:30-31` (**2 trades**, −$324.18), short_term_reversal `:30-31` (**1 single trade**, $2,441.70 — weakest possible regression signal), pine_superosc_vumanchu `:30-31`, pine_supertrend_dema `:30-31` (3 trades), post_vix_dip_buy `:30-31` (3 trades, losing), trend_following `:30-31` (2 trades), volatility_breakout `:30-31` (9 trades, losing), stat_arb_pairs `:30-31` (4-year window), sector_rotation `:30-31` (2023–2024, losing). The pynecore pair is rated **minor** by r1 because those modules are dependency-gated to skips without pynecore installed anyway (`test_pynecore_orb.py:29-30`, `test_pyneecore_sma_crossover.py:29-30`) — adopted.
- **M5 — Major — risk-limit gate is 1.5× looser than configured and divides by a hardcoded baseline** *(verified)* — anchor `tests/strategies/test_channel_breakout.py:65-76`: `equity = 100_000.0` hardcoded while `BacktestConfig(risk_per_trade_pct=0.01)`, assertion `<= 0.015` — a sizing bug inflating risk up to 50% passes, and the slack constant is unexplained. The check validates arithmetic against a locally simulated equity path, not that the engine actually sized positions off `sl_price`. Reference t16's note that the assertions omit point value/multiplier/currency folds in here as an additional reason the check may not exercise canonical sizing. (r1+r2 agree on mechanism; r2 adds the stale-equity ordering observation.)
- **M13 — Major — 16× duplicated template with proven divergent fixes (Shotgun Surgery)** *(verified)* — ~70 lines × 16 files duplicating fixture + four-test class; a fix must be hand-applied 16 times, and history proves the cost: `test_qg_30min_es_breakout_strategy.py:80` already switched to `backtest_result.meta.initial_capital` with a division guard while the other copies still hardcode `100_000.0` — concrete evidence of lagging duplicates. Extract one parametrized harness keyed on a `(strategy, symbol, timeframe, start, end, expected_trades, expected_profit)` table. Smell add-ons (r2): Primitive Obsession/Data Clump — bare module-level floats instead of an `ExpectedResult` record; `sys.path.insert(0, project_root)` boilerplate repeated in all files plus `tests/test_api.py:15` and others (20+ sites suite-wide; pytest rootdir should own it); `test_all_trades_have_sl_in_meta` duplicates the `sl is not None` check `test_no_trade_exceeds_risk_limit` makes anyway (r1).

File-specific findings:

- **m29 — Minor — wrong-symbol header suspicion on the QG variant** *(verified)* — `tests/strategies/test_qg_30min_es_breakout_strategy.py:8`: header claims `Symbol/Timeframe: XAUUSD / 1h` for an **ES (index futures)** breakout strategy, with `TEST_START = "2006-01-02"` (`:30`) predating plausible local dataset depth — constants look inherited from the gold_momentum template. If the data gate then skips (M1), the pins decay unobserved while the file appears maintained. Also the only member passing explicit `data_dir` into `BacktestConfig` (`:50`) while all others rely on ambient `DATA_DIR` (inconsistent isolation).
- **m30 — Minor — single-symbol validation defeats the strategy concept** — "Cross-Asset Momentum" (`test_cross_asset_momentum.py:1,28`), "Sector Rotation" (`test_sector_rotation.py:1,28`), and stat-arb **pairs** (`test_stat_arb_pairs.py:1,28`) all backtest a single AAPL series — no pairing/rotation/multi-asset logic is exercised; golden values encode degenerate fallback behavior, giving false confidence the core logic is regression-covered. (r1 per-file majors folded here as instances; r2 found the same independently.) Counter-note (r3): `test_stat_arb_pairs.py:59-62` uniquely checks `ZScore` in meta — good.
- **m31 — Minor — engine-contract divergence between pynecore and vectorbt siblings** — `test_pynecore_orb.py:52,59,71` and `test_pynecore_sma_crossover.py:71` guard `trade.meta or {}` while all vectorbt siblings dereference `trade.meta` directly (e.g. `test_channel_breakout.py:66`); if meta can be None in one engine it presumably can in the other — one of the two contracts is misunderstood. (r1+r2 concur.)
- **m48 — Minor — weaker risk-check variant silently bypasses NaN stops** *(verified)* — `tests/strategies/test_custom_swings_supertrend.py:130-144`: this variant `continue`s when `sl_price` is None *or NaN* (also re-importing `math as _math` inside the loop, `:136`), so trades without stops contribute zero risk assertions where siblings hard-assert — two different guarantees under the same test name across the family; NaN sl (a real bug symptom) escapes the gate. Same root pattern noted by reference t14/t16 territory but distinct site.
- **m49 — Minor — unseeded RNG fixtures that violate bar invariants** *(verified)* — `tests/strategies/test_custom_swings_supertrend.py:81-87,156-163`: `np.random.uniform` without seeding, sampled independently so `open < low` / `close > high` occur constantly (open∈[1800,2000] vs low∈[1700,1800]). Currently harmless (shape-only assertions), but any value-sensitive future assertion flakes, and ATR/stops paths may hit NaN nondeterministically across numpy versions. Seed like `_make_df` does in the intraday test and build self-consistent bars. (r2+r3 concur; reference t14 agrees.)
- **m50 — Minor — AST default-presence check is weak and call convention unrepresentative** *(verified)* — `tests/strategies/test_custom_swings_supertrend.py:101-121`: counts `defaults + kw_defaults > 0` — a single positional default passes while the message claims it protects the optimization UI; misses `ast.AsyncFunctionDef`; and `mod.generate_signals(df)` (`:88,164`) is invoked without the params dict production passes, exercising a call convention the engine may never use. (r2+r3 concur; reference t15 agrees.)
- **m51 — Minor — triple-pasted importlib loader inside the custom-swings file** — `:72-79,148-155,173-175` repeat the module-load block verbatim inside individual tests instead of a module-level helper/fixture (within-file duplication atop M13). (r2.)
- **m52 — Minor — helper contract doesn't match its use** — `test_intraday_volatility_breakout.py`: `_make_df(n_bars, start=…)`'s "one trading day" docstring vs callers passing 200–300 bars spanning multiple days (`:185,:429`). (r2.)

### tests/strategies/test_intraday_volatility_breakout.py

- **M9 — Major→Minor recalibration — "one trade per day" test passes vacuously when entries vanish** *(verified)* — `tests/strategies/test_intraday_volatility_breakout.py:233-236`: `days.nunique() == len(entry_indices)` holds at 0 == 0; there is no `assert entries.any()` guard, so a refactor killing all entries keeps the test green. r3 rates major, r1/r2 minor. Panel sets **minor**: agreed mechanism, but the sibling tests in this file do assert `entries.any()` and the file has strong adjacent coverage (real ATR-recurrence expectations, DST-aware session test, NaN-gap handling — all three reviewers rank it among the strongest files); the missing-guard fix is one line. Related documentation-level minors merged in: the comment claims a "2-day dataset" though 300 minutes from 16:00 spans one day (r3 `:205-212`), and the `entry_reference_price ≈ signal-bar high` pin at `:538-540` sits under `order_timing="next_bar_open"` (`:536`) — r1 finds the fill-reference semantics suspicious enough to warrant deliberate confirmation against the signal contract, while r2 notes both reference-price conventions can be legitimate under different modes; panel keeps it minor (confirm semantics, add mode-distinguishing messages).
- **m53 — Minor — duplicated assertion** *(verified)* — `tests/strategies/test_intraday_volatility_breakout.py:501-508`: identical `entry_reference_price == approx(df["close"].iloc[entry_idx + 1])` twice back-to-back; second occurrence dead weight (copy-paste remnant). (r1+r2+r3 concur.)
- **m54 — Minor — degenerate warmup in regime-filter test** — `:416`: `atr_regime_factor = 1e9` with only 60 bars of warmup exercises the degenerate path rather than the regime logic itself. (r2.)
- **m55 — Minor — repeated param-block duplication** — every signal test repeats the 5–7-line override block (`use_candle_close=False`, `entry_time="16:30"`, …) with values already diverging (`"18:00"` vs `"18:05"`) unstated; also `np.random.seed(42)` global seeding in helpers (r3 verified the OHLC invariant `low <= open/close <= high` holds — OK). (r1+r2.)

### tests/strategies/test_risk_parity.py

Panel consensus: best-written file in the group (seeded RNGs, PSD/symmetry checks, warmup-error path, threshold-boundary coverage). Findings are all minor:

- **m32 — Minor — crisis-regime assertion accepts either label** — `tests/strategies/test_risk_parity.py:87-98`: `assert regime in ("elevated", "crisis")` lets a regression collapsing non-normal regimes pass, while the very next test asserts de-risking only for "crisis" — the distinction is behaviorally relevant. (r1+r2+r3 concur.)
- **m33 — Minor — unbounded de-risking** — `:93-98`: the crisis adjustment asserts equity total strictly decreases with no lower bound — a bug zeroing all weights passes. (r3.)
- **m34 — Minor — heavy monkeypatch couples to implementation names** — `:169-178`: five internals of `run_backtest` mocked out; renames break with AttributeError rather than meaningful failures; the weight-sequence iterator yields only 3 dicts, so added intermediate rebalances raise StopIteration (loud, acceptable). No lighter-mocked complement exists in the module. (r1+r2 concur.)
- **m35 — Minor — string-prefix datetime filtering is brittle** — `:190-192`: `exit_time.startswith("2024-01-02")` breaks silently if timezone representation changes (`+00:00` suffix formats), surfacing as an opaque count mismatch later. (r3.)
- **m36 — Minor — classification margin near boundary** — fixed seeds make it deterministic today, but the normal/elevated margin at n=30 recent days is unverified. (r2.)

### tests/strategies/test_volatility_breakout_reference_logic.py

Right *style* of test per all reviewers (synthetic bars, exact assertions); two precision gaps:

- **m37 — Minor — asymmetric indicator keys suggest a typo'd key that happens to exist** *(verified)* — `tests/strategies/test_volatility_breakout_reference_logic.py:28-29`: LONG entry checks `sl_price`, SHORT entry checks `stop_loss`. Verified in source: both lines exist exactly as cited. If the strategy emits both keys, the assertions pass while pinning nothing about their consistency; one is likely a typo'd key that should fail. (r3; unique finding.)
- **m38 — Minor — exit timing unpinned** — `:25-30`: entry indices `[5, 8]` and directions exact (good), but exits only via `exits.any()` — a one-bar-late exit regression goes undetected. (r1+r3 concur.)
- **m39 — Minor — direct package import couples run to layout** — `:3`: `from strategies.volatility_breakout…` assumes repo-root `sys.path` insertion from elsewhere; standalone invocation from another cwd ImportErrors. Portability inconsistency with siblings that self-insert. (r2.)

---

## Theme 4 — Root-level account/API/agent/parity tests

### tests/test_account_discovery.py

- **M10 — Major→Minor recalibration — sys.modules surgery for the Alpaca SDK** *(verified)* — `tests/test_account_discovery.py:74-77`: three `monkeypatch.setitem(sys.modules, …)` stand-ins fake `alpaca`, `alpaca.trading`, `alpaca.trading.client`; works only because production resolves imports through exactly those names inside function bodies. r3 rates **major** (risk that a real SDK import earlier in the session leaks through and attempts live network auth), r2 minor (sturdy seam argument). Panel sets **minor**: verified the stub installs before `discover_alpaca_accounts("k","s",True)` runs in the same test, credentials are dummies, and the failure modes are ordering fragility/error — not silent false confidence; the test fails loudly if the fake misses. Residual findings kept: incomplete fake graph vs typical `alpaca.common` chains (r3 `:75-77`) and the obscuring `__import__("sys")` round-trip instead of plain `import sys` (r2).
- **m56 — Minor — happy path only** — discovery tested solely for success shape (`:59-82`); no API-failure, partial-accounts, or `is_live` coverage. (r2.)

### tests/test_account_routing_safety.py

Positive consensus: some of the best safety coverage in the area — duplicate-ID rejection, numerically equivalent ctids `"00123"` vs `"123"`, adapter-never-created invariant.

- **m57 — Minor — loop termination via patched global `asyncio.sleep`** *(verified)* — `tests/test_account_routing_safety.py:105-107` (patch), `:99-101,:139-148` (CancelledError propagation): patches `daemon.asyncio.sleep` — shared stdlib attribute reached through the daemon's import — to raise CancelledError on first call; the duplicate-detection assertion rides entirely on `adapter_creations == []`. Safe under pytest's single-threaded model, but mutates shared state mid-session and breaks confusingly if daemon pacing changes or validation ever moves after a startup sleep; internal cancellation handling would also spook `pytest.raises`. (r1+r2+r3 concur at minor.)

### tests/test_alpaca_account_routing.py

Scanned clean by all three reviewers — model file (mutant-killing rationale in docstrings, hint-block precedence, ambiguity refusal instead of fallback). Two diagnostic notes kept as minor: bare boolean tuple elements in assertions give poor swap diagnostics (`:23,:63,:82`, r3), and near-identical ACC{1..3} env dicts across five tests want a builder (r1 smell).

### tests/test_analyze_mt5_parity.py

Positive consensus: high-value parity checks (UTC-spelling normalization `:59-71`, Athens-local bucketing, exit-reason classification) asserted concretely. Minors:

- **m58 — Minor cluster** — `_trade()` helper hardcodes `id=1` (`:16-44`): harmless today since matching keys on (entry_time, direction), but a landmine if parity ever keys on trade id (r2+r3); `test_series_stats_handles_empty_and_quartiles` name overstates coverage — asserts n/mean/median, never touches quartiles (`:135-141`, r2); sharpe-candidates test checks existence + one ordering relation but no numeric ranges, so a systematic scaling bug (×√252 dropped everywhere) passes (`:143-171`, r3); Athens DST pinned only for winter dates — July would need +3 (`:115-122`, r3); post-construction mutation of `trades[i].pnl` stylistic (`:118-125`, r1).

### tests/test_auto_agent.py

Positive consensus: mocks at the right seams (LLM provider queue with loud exhaustion, subprocess), precise attempt/prompt assertions.

- **m63b — Minor — validation leg mocked away** — `fake_run_backtest` unconditionally writes `'{"status":"ok"}'` and returns 0 (`:137-142,:176-181,:210-220`): retry-on-nonzero-exit is covered but parsing/validation of real backtest output is not; a misparsed-output regression passes this suite. Accepted unit-scope trade-off wanting one integration counterpart. (r2; r1/r3 concur on fidelity limits.)
- **m64 — Minor — fixture encodes a divergent generate_signals contract** *(verified)* — `STEP_THREE_CODE`/`FIXED_STEP_THREE_CODE` return a **3-tuple** (`:51-65`,`:67-75`) while the platform contract enforced elsewhere in this area is 4 elements `(entries, exits, indicators, drawings)` (`test_custom_swings_supertrend.py:71,89`; `test_intraday_volatility_breakout.py`). Two fixtures teach two signatures; whichever is wrong is enshrined as valid by green tests. (r2; unique.)
- **m64b — Minor — brittle couplings** — persona-name (`:230-235`) and emoji-decorated log-line (`:195-200`) assertions break on copy edits; `fake_run_backtest` pasted identically in three tests wants a factory helper (`:137,:176,:210`); `cmd.index("--output")` raising ValueError inside the fake is a cosmetic crash mode (`:131-135`). (r2+r3+r1 merged.)

Reference-report t11 ("stale failed-attempt artifacts not asserted absent") was checked during synthesis: grep of `tests/test_auto_agent.py` shows only `result["attempts"]` assertions (`:149,:232`) and no artifact/cleanup handling — the hint is consistent with the file but no panelist flagged it and it describes absent negative-coverage rather than a defect in cited code; recorded here for completeness, not counted.

### tests/test_auto_download.py

- **M16 — Major — test title promises what the assertion cannot check** *(verified; reference-hint adjudication)* — `tests/test_auto_download.py:10-11`: `test_us_stock_intraday_still_uses_intraday_stock_plan` asserts `_auto_download_source("AAPL","1h") == "huggingface"` — the single-source label — while the title describes plan-splitting behavior implemented in `_auto_download_source_plan` (`data_tools/auto_download.py:353`, whose HF≤2025-07-31 / Alpaca≥2025-07-01 overlap window has **zero test coverage**). Verified both functions exist at the cited locations. **Adjudication:** none of the three panelists raised this; it comes solely from the reference report's chunk-2 list. It is verified true against the code (title/assert mismatch confirmed verbatim) and material (date-boundary logic untested), so it enters as major despite being a sole-source finding — the brief permits including verified reference entries. Companion minor: only index-CFD aliases tested (`:4-7`); the `_is_us_intraday_stock` guards and the six-letter-uppercase `_is_forex_symbol` classification edge (production risk flagged by r3 outside the area) are unpinned. Fix: rename/refocus the test on `_auto_download_source_plan`.

---

## Theme 5 — Shared-state, migration, and AI-quality gating

### tests/test_backtest_equity_curve_migration.py

- **M8 — Major — tests write to the developer's real application database with no isolation or teardown** *(verified)* — `tests/test_backtest_equity_curve_migration.py:23,:36,:86,:117,:162` call `init_db()` against the ambient `shared.database.ENGINE` (repo-default SQLite path); records inserted at `:62-64,:104-106,:149` persist forever (timestamped ids `:45,:88,:125` avoid collisions, ensuring silent accumulation), polluting `/api/backtests` listings consumed by the viewer and by `tests/test_api.py`, making suites order/history-dependent, and racing parallel CI on shared SQLite state. Standard fix: tmp_path-scoped engine (top-level conftest already pins DATA_DIR, so one monkeypatch away). (r1+r2+r3 concur — unanimous major.)
- **M17 — Major — the actual migration path is never exercised** *(verified)* — `:160-176` ("migration on existing table") never constructs a pre-migration schema lacking `equity_curve`; after any successful prior run created the column, the test re-verifies current schema state forever — a broken migration passes permanently once the column exists. Test names promise more than they deliver (misleading-green on the risky upgrade path). r1 rated the same mechanism minor, r2/r3 major; panel sets **major**: unlike a merely weak smoke test, the file's entire stated purpose is the migration and the setup guarantees it is untested after first success.
- **m75 — Minor — conditional tail assertion** *(verified)* — `:172-177`: `SELECT … LIMIT 1` guarded by `if result and result[0]:` — on an empty table the read-back claim is unchecked (same anti-pattern family as C1); also asserts a storage-format detail (JSON string) rather than behavior.
- **m76 — Minor — tz-naive equity timestamps** — `"2024-01-01T00:00:00"` naive strings (`:39-43,:120-123`) perpetuate the naive-time convention the team's own checklist flags.
- **m77 — Minor — microsecond-id collisions theoretically possible** — `%f` strftime ids (`:45,:88,:125`) collide within one microsecond; `uuid4()` idiomatic. (r3.)
- Smell: record-construction boilerplate repeated four times (`:34-60,:84-112,:115-157`) — a `make_record()` helper shrinks the file by a third (r2).

### tests/fixtures/* and the AI-quality chain

- **C5/M-misleading-fixture — intentionally_broken_strategy is fully valid → no negative control anywhere in the AI-quality toolchain** *(verified empirically by r2, corroborated by r1/r3 static analysis)* — `tests/fixtures/ai_generated_strategies/intentionally_broken_strategy.py:1-19`: verified the file defines all required methods with correct signatures, imports cleanly, uses no forbidden calls — its only "brokenness" is semantic (threshold 0.0 momentum). Consumed by the glob-driven positive gate at `tests/integration/test_ai_code_quality.py:16-20` which asserts `result.passed` for **every** fixture — so the suite demonstrates no rejection path whatsoever; `AICodeValidator` could regress to accepting garbage unnoticed. **Severity settlement:** r2 critical-by-implication (empirical PASS 1.0, "headline"), r1/r3 major, reference T2 major. Panel records **major**: the validator-gate blindness is severe, but it is a missing-negative-control defect (coverage gap) rather than a live test asserting false behavior — distinct from C1–C4 where named tests actively pass while broken. Kept prominent as the theme's headline. Fixes (all reviewers converge): add genuinely-invalid fixtures (missing `generate_signals`, syntax error, `eval` usage) asserted to FAIL, or move them outside the pass-gated glob and rename this one. Smell: Mysterious Name in the adverse direction.
- **M18 — Major — glob-driven parametrization + import-time mkdir can silently empty the matrix** *(verified)* — `tests/integration/test_ai_code_quality.py:9-11`: `FIXTURES_DIR.mkdir(parents=True, exist_ok=True)` at collection recreates a lost directory (side effect on the repo tree), after which `sorted(FIXTURES_DIR.glob("*.py"))` parametrizes **zero** cases and the headline test trivially passes; only `assert STRATEGY_FILES` in the average-score test (`:24`) catches emptiness, with a confusing message. Deleting fixture files leaves the AI-code gate green. (r1 major + r3 minor + r2 minor — kept major: the gate's subject matter makes vacuity costly; the mkdir side effect folds in here rather than standing alone.)
- **m78 — Minor — average-score test re-asserts validity, diluting its distinctive assertion** — `:23-34`: `assert all(result.passed …)` (`:33`) duplicates the parametrized test before computing the score; with all fixtures scoring 1.0 the `>= 0.7` gate (`:34-35`) currently cannot bind, and each fixture is validated twice. Failure messages report validity errors when the subject is the score. (r2+r3+r1 concur.)
- **m79 — Minor — valid_trend_strategy models state-based entries, not crossover events** *(verified)* — `valid_trend_strategy.py:17-18`: `entries = (fast_ma > slow_ma)` are boolean regions whereas the Pine source they mirror uses crossover events (`sample_strategy.py:10-11`); as the canonical blessed exemplar (and C4's fallback payload) it teaches generators that region-style entries are the house contract; combined with min_trades=0 (M12) nothing tests its trading semantics. r2 minor, r3 minor-with-notes, r1 clean — kept minor (acceptable as fixture; documented hazard).
- **m80 — Minor — sample_strategy.py is Pine Script under a `.py` name** — picked up by any `glob("*.py")` over `tests/fixtures/`; currently harmless (ai_generated_strategies is a sibling dir) but a `.pine` extension plus explicit read path would be safer. (r3; r1/r2 clean.)

---

## Theme 6 — Manual diagnostic script: tests/compare_metrics_quantstats.py

All three reviewers converge: this is a script, not a test, and it carries a stale production-bug narrative.

- **M19 — Major — manual harness that can never fail** — no `test_*` functions and all execution under `if __name__ == "__main__"` (`:203+` per r3's read; print-only ✓/✗ results at `:156-218`), so pytest never collects it and it **always exits 0** — even wired into CI it could never fail on metric drift between `runner.stats` and QuantStats. Inside `tests/` it risks being mistaken for coverage. (r1/r2 minor, r3 major; panel sets major — the entire file's protective purpose is illusory by construction.)
- **M20 — Major — stale "BUG CANDIDATE" Calmar narrative contradicts verified production behavior** *(verified against runner/stats.py by r3)* — `tests/compare_metrics_quantstats.py:105-112` computes `our_calmar_bugged = annualized_return / abs(mdd_fraction / 100)` labeled as the suspect, while r3 cross-checked production: `_max_drawdown_pct_manual` returns a fraction (`runner/stats.py:412-424`) fed directly to `_safe_calmar_ratio` (`runner/stats.py:143-144,:152,:161,:370-375`) which divides by the fraction as-is — i.e. production matches this script's **fixed** branch today. The kept-alive "bugged" narrative invites someone to "fix" stats.py into an actual bug. Delete the dead pipeline or convert the file into a real parametrized test with tolerances. (r1/r2 rated minor; panel sets major because acting on the stale comment would introduce a production regression — a hazard, not just rot.)
- **m81 — Minor — inconsistent rf conventions across sections under one column label** *(verified)* — main comparison passes annual rf with `annualize=True` (`:117-121`, documented at `:115-116`) while deep-dives pass `rf=daily_rf` (`:237,:270-271`); the printed quantities differ under identical labels and nothing asserts consistency between them (production convention matches line 117 per r3). (r2+r3 merged.)
- **m82 — Minor — dead code** *(verified)* — `daily_rf` computed and unused in `compare_scenario` (`:92`, triple-repeated at `:221,:249`); `make_equity_curve_objs`/`make_dummy_trade` never called (`:41-52`). (r1+r2+r3 concur.)
- **m83 — Minor — module-level global seeding churns all scenarios on reorder** — repeated `np.random.seed(...)` at module level (`:58,:71,:79`); prefer local `default_rng(seed)`. (r1+r3.)

---

## Adjacent-context observations (files outside this chunk's list, flagged for their owners)

- `tests/validators/code_validator.py:97-133` executes submitted/AI code via `subprocess.run([sys.executable, "-I", "-c", …], timeout=8)` — isolated-mode + timeout are good hygiene, but `-I` is not a sandbox (no network/socket or filesystem restriction); consumers treating this as safe execution of untrusted LLM output should revisit that assumption. (r2.)
- `tests/validators/backtest_validator.py:22-23,:55-61` treats implausibly high Sharpe (>10, "possible lookahead bias") as warning-only, and with `min_trades=0` defaults makes the validator nearly unfalsifiable as used by M12. (r2+r3.)
- `data_tools/auto_download.py:305` `_is_forex_symbol` classifies any six-letter uppercase symbol as forex (six-letter NYSE tickers historically exist) → wrong downloader; unpinned by tests. Production-side, outside this chunk. (r3.)

## Cross-cutting summary judgment

Strongest material in the area: `test_alpaca_account_routing.py` (model file), `test_account_routing_safety.py`, `test_risk_parity.py`, `test_auto_agent.py` (well-seamed mocking), `test_analyze_mt5_parity.py`, and the synthetic-bars style of `test_volatility_breakout_reference_logic.py` / `test_intraday_volatility_breakout.py`.
Weakest: the three-mechanism silent-skip architecture (parquet-coverage skips M1–M3, browser-probe skips m2, API-discovery module skip M11) that can zero coverage behind green CI; the conditional/vacuous test families (C1, C2, C3, m42, m75); the e2e fallback validating a fixture instead of the product (C4); the migration tests that never migrate and pollute the real DB (M8, M17); and the negative control that isn't one (C5-entry).
Dominant maintenance cost: the 16× copy-pasted regression template with divergent fixes (M13, M4, M5).

---

## Coverage statement — 39/39 tracked files accounted for

| # | File | Outcome |
|---|---|---|
| 1 | `tests/__init__.py` | Clean — empty package marker (required for `from tests.validators…` imports). All 3 reviewers. |
| 2 | `tests/compare_metrics_quantstats.py` | Theme 6: M19, M20, m81–m83 |
| 3 | `tests/conftest.py` | m1–m4 |
| 4 | `tests/e2e/lab/test_conversion.py` | C4, m65–m70 |
| 5 | `tests/e2e_frontend.js` | C2, C3, M14, M15, m71–m74 |
| 6 | `tests/fixtures/ai_generated_strategies/intentionally_broken_strategy.py` | C5-entry (Theme 5) |
| 7 | `tests/fixtures/ai_generated_strategies/valid_trend_strategy.py` | m79 |
| 8 | `tests/fixtures/sample_strategy.py` | m80 (+clean content check) |
| 9 | `tests/integration/test_ai_code_quality.py` | C5-entry, M18, m78 |
| 10 | `tests/integration/test_strategy_pipeline.py` | M11, M12, m59–m63 |
| 11 | `tests/orchestrator.py` | M6, M7, m5–m8 |
| 12 | `tests/strategies/conftest.py` | M1, M2, M3, m26–m28 |
| 13 | `tests/strategies/test_channel_breakout.py` | Template anchor: M4, M5, M13 |
| 14 | `tests/strategies/test_cross_asset_momentum.py` | Family M4/M5/M13 + m30 |
| 15 | `tests/strategies/test_custom_swings_supertrend.py` | Family + m48–m51 |
| 16 | `tests/strategies/test_gold_momentum.py` | Family (pin recorded in M4) |
| 17 | `tests/strategies/test_intraday_volatility_breakout.py` | M9 (minor), m52–m55 |
| 18 | `tests/strategies/test_mean_reversion_rsi_bb.py` | Family (pin in M4) |
| 19 | `tests/strategies/test_pine_superosc_vumanchu.py` | Family (pin in M4) |
| 20 | `tests/strategies/test_pine_supertrend_dema.py` | Family (headers verified consistent, r3) |
| 21 | `tests/strategies/test_post_vix_dip_buy.py` | Family (pin in M4) |
| 22 | `tests/strategies/test_pynecore_orb.py` | Family at minor + m31 |
| 23 | `tests/strategies/test_pynecore_sma_crossover.py` | Family at minor + m31 |
| 24 | `tests/strategies/test_qg_30min_es_breakout_strategy.py` | Family + m29 |
| 25 | `tests/strategies/test_risk_parity.py` | m32–m36 |
| 26 | `tests/strategies/test_sector_rotation.py` | Family + m30 |
| 27 | `tests/strategies/test_short_term_reversal.py` | Family (pin in M4) |
| 28 | `tests/strategies/test_stat_arb_pairs.py` | Family + m30 (+ZScore positive note) |
| 29 | `tests/strategies/test_trend_following.py` | Family (headers verified consistent, r3) |
| 30 | `tests/strategies/test_volatility_breakout.py` | Family (pin in M4) |
| 31 | `tests/strategies/test_volatility_breakout_reference_logic.py` | m37–m39 |
| 32 | `tests/test_account_discovery.py` | M10 (minor), m56 |
| 33 | `tests/test_account_routing_safety.py` | m57 |
| 34 | `tests/test_alpaca_account_routing.py` | Clean + 2 diagnostic minors (tuple asserts, env-dict builder) |
| 35 | `tests/test_analyze_mt5_parity.py` | m58 cluster |
| 36 | `tests/test_api.py` | C1, m42–m47 |
| 37 | `tests/test_auto_agent.py` | m63b, m64, m64b |
| 38 | `tests/test_auto_download.py` | M16 + companion minor |
| 39 | `tests/test_backtest_equity_curve_migration.py` | M8, M17, m75–m77 |

---

## Appendix A — Adjudicated disagreements (reviewer vs reviewer vs reference report)

Every contradiction encountered during synthesis, and how it was settled:

1. **test_api.py conditional skips** — C1 severity: r3 critical / r2 major / r1 major / reference t6 minor → **critical** kept (verified in source; spans six endpoint tests with zero else-branches).
2. **e2e_frontend.js assert-nothing console test** — r3 critical / r2 major / r1 major / reference t1 minor → **critical** kept (verified: no assert statement exists in the test body).
3. **e2e_frontend.js conditional-pass autocomplete/detail tests** — r3 critical / r1-r2 minor → **critical** kept (same anti-pattern family as #1; benchmark-dropdown knock-on verified at `:118-123`).
4. **test_conversion.py fixture fallback** — r3 critical / r1-r2 major / reference T1 major → **critical** kept (fallback branch and valid fallback file verified in source).
5. **intentionally_broken_strategy / AI-quality negative control** — r2 empirical-critical / r1-r3 major / reference T2 major → **major** recorded as coverage-gap defect, headline of its theme (rationale in finding text).
6. **strategy_pipeline min_trades=0** — r3 critical / r1 minor / r2 loose-major → **major** set: verified critique, but honest smoke semantics unlike C1–C4.
7. **strategies/conftest silent skip** — unanimous major → kept major (all three independently flagged; reference t5 corroborates the union-extents sub-mechanism at minor).
8. **conftest.py Playwright skip policy** — r2 major / r1-r3 minor / reference t4 minor → **minor**: code shows non-matching failures fail loudly rather than skip (settlement reasoning in m2). The reference's stale-cache concern (t4) is real and folded into m2.
9. **orchestrator subprocess timeout** — panel-unanimous minor / reference T3 major → **minor** kept with reason (manual entry-point, not a wired CI stage).
10. **orchestrator layer mislabeling** — r1-r3 major → **major** kept (reference has no equivalent entry; strongest consensus in area alongside #7).
11. **intraday one-trade-per-day tautology** — r3 major / r1-r2 minor → **minor** (recalibration rationale in M9; mechanism undisputed).
12. **account_discovery sys.modules stubbing** — r3 major / r2 minor / r1 clean → **minor** (fails loudly if the fake misses; not silent false confidence — settlement in M10).
13. **compare_metrics_quantstats "cannot fail"** — r3 major / r1-r2 minor → **major** raised (whole purpose illusory; M19). Calmar narrative: r1-r2 minor / r3 major-with-production-verification → **major** kept (M20).
14. **migration shared-DB writes** — unanimous major → **major** kept. Migration-path-never-exercised: r1 minor / r2-r3 major → **major** set (M17).
15. **golden masters & template duplication** — r1 files-per-file majors, r2 single group-level majors, r3 majors → consolidated M4/M13 majors; pynecore pair demoted to minor per r1's dependency-gating argument (adopted).
16. **Reference-only entries**: T2/t6/t7/t14/t15/t16/t5/T1/t1–t4 corroborated and absorbed above. **M16 (test_auto_download title/assertion mismatch)** entered from the reference list alone after verification against `data_tools/auto_download.py:343,353` — sole-source but confirmed. Reference t11 (auto_agent artifacts) inspected, consistent but absent from all three panelist reports; recorded uncounted in Theme 4.
17. **No reference claim was found false**, but several were materially mis-scaled relative to panel consensus; scale corrections above follow the code, not any majority.

## Appendix B — Method note

Synthesis rules applied: strict duplicates merged with all original citations preserved; mechanisms agreed by ≥2 reviewers merged into one finding with attribution; severities recalibrated only where reviewers disagreed or where reading the code showed a clear error of scale (each such case listed in Appendix A); nothing dropped except exact duplicates. Findings marked *(verified)* were checked directly against source during synthesis (files read in full or at cited offsets: `tests/conftest.py`, `tests/strategies/conftest.py`, `tests/orchestrator.py`, `tests/test_api.py`, `tests/e2e_frontend.js`, `tests/e2e/lab/test_conversion.py`, `tests/integration/test_ai_code_quality.py`, `tests/integration/test_strategy_pipeline.py`, `tests/fixtures/ai_generated_strategies/intentionally_broken_strategy.py`, `tests/strategies/test_channel_breakout.py`, `tests/strategies/test_custom_swings_supertrend.py`, `tests/strategies/test_intraday_volatility_breakout.py:495-544`, `tests/strategies/test_volatility_breakout_reference_logic.py`, `tests/strategies/test_qg_30min_es_breakout_strategy.py:1-60`, `tests/test_account_discovery.py:1-85`, `tests/test_auto_agent.py:40-74`, `tests/test_auto_download.py`, `tests/test_backtest_equity_curve_migration.py`, `tests/compare_metrics_quantstats.py:85-144`).

---

## Finding counts (merged section)

| Severity | Count | IDs |
|---|---|---|
| Critical | 4 | C1 (test_api conditional skips), C2/C3 (e2e_frontend.js assert-nothing & conditional-pass), C4 (test_conversion fixture fallback) |
| Major | 19 | M1, M2, M3, M4, M5, M6, M7, M8, M11, M12, M13, M14, M15, M16, M17, M18, M19, M20, plus the intentionally-broken-fixture negative-control entry (C5-entry) |
| Minor | 71 | m1–m8, m26–m39, m42–m47, m48–m55, m56–m58, m59–m70, m63b/m64/m64b, m71–m83, plus M9 and M10 (both recalibrated major→minor) and the two unnumbered test_alpaca_account_routing diagnostics and the test_auto_download companion note |
| **Total** | **94** | |

(M9 and M10 carry M-labels only because reviewer disagreement adjudications reference them; they are counted as minors per the recalibrations recorded in Appendix A. Per-file golden-master pins inside M4 count once under M4; the e2e_frontend.js conditional-pass bullet covers three test sites; the test_api condition-skip bullet covers six test methods; each bullet counted once.)


