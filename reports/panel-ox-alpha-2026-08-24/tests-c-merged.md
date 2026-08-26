# Expert-panel code review — area `tests-c` (chunk C of the test suite)

**Panel:** ox-alpha · 2026-08-24 · 3 blind, independent reviewers (r1, r2, r3), synthesized by ox-alpha.
**Scope:** 40 tracked code files — root-level `tests/test_*.py` from `test_signal_import.py` through `test_yfinance_downloader.py`, plus `tests/validation/**` and `tests/validators/**`.
**Method:** this file is a merge/dedup of the three blind reviews written BEFORE any source-code re-inspection; a bounded verification pass (≤8 spot-checks) settled reviewer-vs-reviewer contradictions and spot-verified critical/major claims afterwards. Verification settlements appear inline as **[SETTLED:]** notes; unresolved disagreements are kept explicit rather than smoothed over.

---

## Executive summary

All three reviewers independently converge on the same picture: the chunk is generally healthy — most suites assert concrete, falsifiable behavior with hand-derived expected values, negative paths are covered, and several files are called out as exemplary by multiple reviewers (`tests/validation/test_trade_statistics.py`, `test_trade_comparison.py`, `test_risk_sizing.py`, `test_quantconnect_result_importer.py`, `test_importers.py`). The recurring weaknesses, ranked:

1. **Environment coupling masquerading as coverage** (all three reviewers flag this as the dominant theme): the engine-parity test silently skips without local user data and tolerates ±30% divergence (`tests/test_trade_engine.py:324-368`; rated **critical** by r2, major by r1/r3); the cross-platform matrix tests read mutable repo working-tree artifacts with hard-pinned values (`tests/validation/test_cross_platform_metrics.py:16-48`); golden tests freeze env-dependent paths at import time (`tests/validation/test_quantconnect_result_importer.py:19-29`); and CWD-relative fixture paths recur across four importer/regression suites. These tests can report green while validating nothing — or red against correct code after a legitimate data refresh.
2. **Misleading names on financially-sensitive paths**: `test_round_lots_rounds_up` asserting floor rounding (`tests/validation/test_fixed_risk_model.py:158-165`), `test_validate_extracts_take_profit_for_risk_reward` verifying no such extraction (`tests/validation/test_percent_risk_model.py:310-331`), `test_zero_vs_nonzero_pnl_mismatch` asserting a pass (`tests/validation/test_trade_comparison.py:281-291`), `test_empty_inputs` docstring contradicting its assertion (`tests/validation/test_paper_readiness.py:510-525`), `min_position_size_clamping_zero` semantics, and the unexplained stockkit→RamJet alias (`tests/test_signal_processor_registry.py:36`).
3. **Security-relevant gaps in the validators**: the `AICodeValidator` denylist is trivially bypassable while its `passed=True` reads as a safety verdict, and it *executes* candidate code in a subprocess whose `-I` isolation is hygiene, not containment (`tests/validators/code_validator.py:20-22,48-60,116-122`); generated code is `exec()`'d in codegen tests with no injection-regression coverage (`tests/test_strategy_ir_codegen_vectorbt.py:62-63,105-106,140-141`).
4. **Vacuous-by-mocking surface**: nearly the whole legacy walk-forward suite asserts pass-through of a stubbed optimizer (`tests/test_walkforward.py:51-205`; major per r2, contested — see settlement inline); the QuantStats path in `tests/test_stats.py:87-93` asserts key presence and non-zero-ness only.
5. **Massive fixture/helper duplication across ~8–15 validation files** (`_make_trade` family re-declared per file; act-blocks repeated up to ~25×/file) — the dominant maintenance tax named by all three reviewers, plus recurring dead imports (`typing.Any` ×3+, unused `Trade`, unused `pytest`).

Positive constants across all three reviews: no mutable default arguments anywhere in the chunk; tz handling consistently UTC-aware and correctly derived; subprocess usage is list-argv with pinned cwd (no `shell=True`) except for the missing-timeout gap; and the bounded-poll loops in viewer tests are bounded (no unbounded sleeps).

Severity totals for the merged section: see the coverage statement at the end of this file.

---

## Root-level tests

### tests/test_signal_import.py

Scanned by all three (50 lines; CSV signal parsing incl. NY→UTC conversion and German decimal commas).

1. **tests/test_signal_import.py:30 — minor — DST correctness asserted for winter only; tz assumption implicit in a comment.**
   r2 verified `hour == 14` is arithmetically correct for the 2026-02-23 winter date (09:32 ET → 14:32 UTC) with a documenting comment; r3 agrees (comment would become wrong if the fixture date moved, though the assertion stays self-consistent). r1 adds: since signal timestamps drive backtest fills, one July-date case asserting EDT (`hour == 13`) would pin that the tz database is genuinely consulted rather than a hardcoded −5 offset. Cheap and valuable.
2. **tests/test_signal_import.py:33-50 — minor (r1) — comma-format variant tested without decimal commas,** so the two format branches differ in more than delimiters; the German-decimal path is exercised only via the semicolon format. (r2/r3 scanned-clean apart from finding 1.)

Baseline smells: none significant (all three). Cross-file note (r2): the StockKit semicolon CSV fixture text is copy-pasted verbatim into `tests/test_viewer_features.py:3174-3183,3220-3228`.

### tests/test_signal_processor_registry.py

1. **tests/test_signal_processor_registry.py:36 — major/minor split — "stockkit" asserted to produce `RamJetSignalProcessor`.**
   - r2 rates **major** and verified production intent against `signal_bots/registry.py:92` (`"stockkit": _build_ramjet`): the test currently matches production, but two publicly distinct options (distinct labels/descriptions, registry.py:62-72) return byte-identical processors under a generic name that hides the aliasing. If deliberate, the assertion belongs in an explicitly named alias test; otherwise a future fix giving StockKit its own class will look like the test breaking production.
   - r1 rates **minor**: looks like intentional aliasing or a copy-paste registry bug blessed by the test; nothing comments which; if aliasing is intended, assert the identity contract explicitly.
   - r3 rates **minor**: surprising alias asserted rather than explained; today it documents an aliasing decision only implicitly.
2. **tests/test_signal_processor_registry.py:46-58 — minor — variant-difference coverage one-sided.** r1: SHORT-stop-above-entry asserted via whatever `_sample_market_data` yields, no expected value pinned; a sizing-formula change altering magnitude but not sign passes silently. r2: never also asserts the long-only variant keeps exit-only metadata with no position-side change beyond `exit_only is True`. r3 records :55 `metadata.get("exit_only") is True` as a good metadata-contract pin; overall judged acceptable.

Baseline smells (r1): processor-id set literal duplicated between :32 here and test_viewer_features.py:3292; extract a shared constant.

### tests/test_signal_replay_adapter.py

Trailing-stop replay behavior with deterministic 4-bar fixture. Assertions specific (exit_reason, meta propagation, update count) — all three reviewers find no bugs.

1. **tests/test_signal_replay_adapter.py:52-61 — minor — trailing-stop path asserted only via outcome markers.** r1: `trailing_stop_updates >= 1` and `exit_reason == "trailing_stop"` confirm the mechanism ran, but the actual stop level at exit is never asserted against the percent-config math; a bug ratcheting the stop in the wrong direction still produces these markers. r2 (:60): a regression updating the stop every bar to a non-monotonic value would pass; assert final stop level relative to entry/close. r2 (:52-53) adds: `backtest_end="2026-01-01"` equals the frame's first day while data spans four same-day bars — end-date inclusivity semantics of `SignalReplayAdapter.run` are silently depended upon; if end handling flips to exclusive, zero trades would be misread as a fill/trailing bug.
2. **tests/test_signal_replay_adapter.py:9-61 — minor (r3, gap) — single happy-path scenario; boundary conditions untested:** no-stop signal path, take-profit hit before trail arms, and `fill_delay_seconds != 0` (:61 pins == 0 only). For an execution adapter these gaps matter more than usual.
3. **tests/test_signal_replay_adapter.py:20-35 — nit (r1)** — `take_profit=140.0` (40% away) fine as "never triggers", but a comment saying so would prevent someone "fixing" it.

### tests/test_stats.py

1. **tests/test_stats.py:87 — minor — assertion on inequality only; sign convention unpinned.** All three reviewers flag `assert stats["max_drawdown_pct"] != 0  # QuantStats returns negative`: it equally passes for positive drawdown (a sign regression reporting gain instead of loss) or any non-zero garbage. Assert `< 0` and an approx magnitude. r3 adds the cross-module trap: sign conventions differ between runner.stats and validation.stats (viewer list endpoint expects positive `max_drawdown_pct == 23.45` from `-0.2345` input, test_viewer_features.py:3581) — document once, pin explicitly.
2. **tests/test_stats.py:88-93 — minor (r2) — key-presence assertions only for the entire QuantStats path** (sharpe/sortino/calmar/cagr/total_return/max_drawdown_duration): dict population proven, metric computation not. Combined with finding 1, `test_with_equity_curve` is near-vacuous beyond smoke value.
3. **tests/test_stats.py:99-105 — minor (r1) — p_value test asserts distribution membership, not the value.** `0 ≤ p < 0.05` verifies some small p-value exists for this fixed series; a changed test statistic could shift p within range and pass. Pair with one pinned literal.
4. **tests/test_stats.py:107-116 — minor (r1) — CAGR window asserted as a loose band (0.09–0.11)** where inputs determine ~0.10 exactly under the documented formula; `math.isclose` against the computed expectation (done properly at :145-146, :175-177) would be consistent.
5. **tests/test_stats.py:61 — minor (r3, consistency)** — win-rate via hand-rolled epsilon instead of `pytest.approx`; style inconsistency weakening precision for no benefit.
6. **tests/test_stats.py:187-211 — minor (r3) — intraday-Sharpe test asserts loose inequalities only** (`total_return > 0`, `sharpe_ratio > 0`, `len(daily_returns) == 29` carries the real content); a bar-density bucketing bug inflating Sharpe by √24 would still pass. A pinned Sharpe band would close it.

Positive (r2): lines 95-211 (p-value, CAGR elapsed-time/fractional-day annualization, intraday aggregation) encode exact formulas via `math.isclose` against hand-computed values — excellent. Datetimes tz-aware UTC throughout (fixed-checklist item respected, r1).

### tests/test_strategy_ir_builder.py

Markdown-section extraction, source-token fallback, unknown-engine hint. No bugs found (r2 scans clean entirely; r1/r3 minor notes only).

1. **tests/test_strategy_ir_builder.py:7-43 — minor (r1) — single happy-path for markdown parsing.** Section-order variations (Exit before Entry), missing sections, and non-German headings untested; if the parser matches German literals ("Kernlogik" hardcoded), that's a hidden locale coupling worth pinning or generalizing. r3 (smell): the German headers double as i18n fixtures but are undocumented — one comment prevents someone "fixing" them as typos and breaking localization coverage.
2. **tests/test_strategy_ir_builder.py:39 — nit (r1/r3) — `confidence == 1.0` asserted only for the fully-populated doc;** partial-doc confidence degradation untested, so the interesting boundary (what drops confidence?) has no case and confidence scoring is effectively uncovered below 1.0.
3. Smells (both r1): `test_build_strategy_ir_falls_back_to_source_indicator_tokens` asserts names extracted from tokens but not params — name promises more than scope; three near-identical `build_strategy_ir(...)` calls could share a helper.

### tests/test_strategy_ir_codegen_vectorbt.py

Determinism + contract tests for IR→vectorbt codegen, executing generated code in a sandbox namespace.

1. **tests/test_strategy_ir_codegen_vectorbt.py:62-63,105-106,140-141 — major (r1/r2) / minor (r3) — `exec()` of generated strategy code with no security gate or injection-regression coverage in the test path.**
   - r1 (**major**): inputs are static repo-controlled IR fixtures so acceptable today, but the sibling validator (`tests/validators/code_validator.py`) exists precisely because generated/AI code must be treated as untrusted; if `_sample_ir` ever becomes LLM-derived or fixture-loaded, these tests become an arbitrary-code-execution sink. Guard comment or route through the project's own `AICodeValidator`.
   - r2 (**major**, coverage-gap framing): no syntax/compile assertion separate from exec, and no assertion that generated code avoids forbidden constructs — output is never round-tripped through `AICodeValidator` despite it existing one directory over.
   - r3 (**minor**, acceptable-with-note): executing generated code *is* the behavior under test; the missing piece is a regression test that raw user-controlled strings (names/params containing quotes/newlines) cannot reach the generated code body — `_sample_ir` uses a benign `"Deterministic Demo"`. This is the module's main security-adjacent surface, currently untested.
2. **tests/test_strategy_ir_codegen_vectorbt.py:78 — minor — loose arity `len(output) in {3, 4}`.** r2: a regression dropping the fourth element (stop metadata) passes silently; pin exact arity now that the generator is deterministic. r3 agrees (pin one shape or parametrize). Related (r1, :86-87): `if len(output) == 4: assert isinstance(output[3], list)` asserts almost nothing about drawings content — fine as shape check only.
3. **tests/test_strategy_ir_codegen_vectorbt.py:49-55 — minor (r1) — determinism test is weak.** Two calls in one interpreter compare equal; cross-process/hash-seed nondeterminism wouldn't be caught; consider hashing normalized output or a checked-in snapshot. (r3 counters that determinism tests :49-55 and sorted-meta-parameters :124-144 are genuinely valuable, pinning reproducible builds.)
4. **tests/test_strategy_ir_codegen_vectorbt.py:76-87 — minor (r1) — generated `generate_signals` exercised but signal semantics never asserted.** Only shape checks (`len == len(df)`); an always-false-entries codegen bug passes. One assertion that entries fire on the crossover bar would pin semantics.

Smells (r3): the `namespace = {}; exec(code, namespace)` scaffold repeats three times (:62-74, :105-113, :139-141) — extract helper returning `(namespace, df)`.

### tests/test_strategy_ir_normalizer.py

Focused normalizer tests: markdown-title fallback, KV-string parsing, mixed tag merging, invalid-parameter rejection. No bugs found (any reviewer).

1. **tests/test_strategy_ir_normalizer.py:36-48 — minor — tag-merge semantics underdocumented by the assertion.** r1: scalar tag string appended to existing list is shown, but nothing pins two scalars of the same key (dedupe? last-wins? list-join?) or dict-vs-dict conflicts. r3 frames it as subtle precedence encoded without stating it — the same-key collision case is where normalization bugs live; untested.
2. **tests/test_strategy_ir_normalizer.py:51-61 — minor (r1) — only one invalid shape covered.** `parameters=[123]` raises (with message regex — good); `parameters="fast"`, `parameters=[["a",1]]`, non-str tag values untested.
3. Smell (r3, mild Speculative Generality): `requested_name=None` passed everywhere though never exercised non-None — add the override case or drop the parameter.

### tests/test_strategy_ir_routing.py

Clean routing-decision table tests: IR-generator vs legacy per engine/IR/rollout-mode, with reason-string checks. No bugs found (r2 scans clean; r1 concurs assertions cover all quadrants plus fallback gating).

1. **Missing case (minor, r1):** no test for `rollout_mode="ir-only"` when the engine is *unregistered* or IR is missing — the mode most likely to need fail-closed behavior (`allow_legacy_fallback=False` + missing generator = what?). Add both combinations. (r3's accounting lists five modes including ir-only-fallback-disabled; the unregistered-engine-under-ir-only combination remains the gap.)
2. **tests/test_strategy_ir_routing.py:19,:34/:48/:63/:79 — minor (r3) — magic attempt-order constants** `first_llm_attempt == 2` vs `== 1` encode "IR generator consumes attempt #1" with no comment; if attempt numbering shifts, four tests fail simultaneously with no self-explanation. Derive from a named constant.
3. Smell (both r1/r3): `resolve_codegen_route(...)` scaffolding repeated 5×; parametrize/default-args factory.

### tests/test_strategy_meta_adapters.py

Good end-to-end coverage of the vectorbt adapter through real parquet fixtures (all three rate these true integration tests — high value).

1. **tests/test_strategy_meta_adapters.py:104-119 — severity split (r1 major; r2/r3 minor) — magic-number sizing assertions without derivation.**
   - r1 (**major**): `trade.quantity == 142.8571` and `trade.pnl == -142.86` encode `1000/7`, i.e. fill at bar-2 close 102 with SL 95 — a subtle intrabar-fill semantic. The test passes only because actual fill price is 102, not the signal-bar price; changing fill timing/rounding fails it with no hint why. At minimum `pytest.approx` plus a documented derivation (`1000/(102−95)`).
   - r2 (**minor**): quantity = 1000/7 (risk $1000 over $7 stop distance) is correct arithmetic but needs a one-line derivation comment.
   - r3 (**minor**): initially couldn't re-derive (entry 100/SL 95 → 200 ≠ 142.86) but verified internal consistency with the file's own `risk_math` meta assertions; documentation debt, not encoded bug.
    *These positions agree on facts (values correct, derivation undocumented); only weight differs. **[SETTLED by verification]** Fixture read confirms the OHLCV series closes at 102 on bar 2 (close column: 100,100,102,104,106) and `meta["sl_price"] == 95.0`, so quantity = risk_target_usd(1000)/(102−95) = 142.8571 — r1's intrabar-fill derivation is exactly right; values are correct, only the missing one-line derivation is the defect. Final severity: **major** per r1's oracle-re-derivability concern.*
2. **tests/test_strategy_meta_adapters.py:106-107,:329 — minor (r1/r3) — float equality without tolerance.** `== -142.86`, `== 39.6` compare floats exactly; safe for fixed inputs but fragile under refactoring; use `pytest.approx`.
3. **tests/test_strategy_meta_adapters.py:180-196 — minor — repo-wide strategy sweep inside a unit test.** r1: iterates live strategies dir via `iter_strategy_files(DEFAULT_STRATEGIES_DIR)`, silently skips `pynecore_*` by name-prefix (:184), passes vacuously if dir empty/relocated — add `assert scanned > 0`. r2: no skip guard and no per-strategy isolation — any broken strategy anywhere fails this (arguably the point of a contract sweep), but one needing params for non-empty indicators fails for environment reasons; also accepts the same loose `{3,4}` arity flagged above. r3: results depend on checkout state; all-`pynecore_*` state → zero loop iterations → vacuous pass.
4. **tests/test_strategy_meta_adapters.py:28-32 — minor (r1) — `_load_module` never registered in `sys.modules`;** breaks dataclasses/pickling/relative imports if strategies use them; fine today, generic-looking helper worth noting.
5. **tests/test_strategy_meta_adapters.py:20,:23-24 — minor (all three) — import-time path setup debt.** Module-level `sys.path.insert(0, ...)` mutates global interpreter state (redundant under normal pytest rootdir config, can shadow modules — r2/r3); r3 adds the insert happens *after* earlier imports already succeeded (no-op for them) and `REPO_ROOT` is computed and never used; `STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR` alias used once (though it does make :183's sweep CWD-independent — r2).

Smells: OHLCV parquet scaffold repeated ~6× (:52-119, :122-176, :199-253, :256-329, :332-401) — extract helpers (r1/r3). Filename says "meta adapters" but content is vectorbt e2e plus pynecore enrichment — two concerns, Divergent Change (r1/r3); r3 suggests splitting pynecore-helper tests (:410-505) into their own module. Positive (r2): intrabar TP-resolution test (256-329) asserts exact fill time/price/PnL from synthetic lower-timeframe replay; pynecore directional SL/TP promotion (410-448) verifies side-correct selection incl. absence of raw `_long/_short` keys.

### tests/test_strategy_type_detection.py

Type detection (meta field, class pattern, default) plus CLI guard against running signal-processor strategies. No bugs found (any reviewer).

1. **tests/test_strategy_type_detection.py:24-39 — minor (r1) — class-pattern detection tested only via name substring.** Nothing pins whether any class containing the token counts or method presence (`process_signal`) is required — both signals appear simultaneously in the fixture, individual weight unknown; add the mutually-exclusive cases to pin precedence.
2. **tests/test_strategy_type_detection.py:8,:59-95 — minor (r3) — imports `runner.cli` at module import time;** runs whole CLI module (argparse setup etc.) for one call; if CLI import gains side effects (config load, network) the suite breaks — import inside the test instead.
3. **tests/test_strategy_type_detection.py:59-95 — nit (r1)/judgement-call (r3) — CLI rejection test builds a 16-field SimpleNamespace** mirroring argparse output, brittle to new args (factory would localize churn); r3 notes it relies on ValueError surfacing before any file/data access — currently pre-validation, fine, but if validation moves later it could start touching data dirs.

### tests/test_timeframe_normalization.py

Alias→canonical mapping plus pass-through cases including empty string. Nothing found for what's covered (all three).

1. **Coverage gap (minor, r1/r3):** uppercase input ("M1"), whitespace (" 1h "), tick/sec aliases, None-handling untested although viewer coalescing paths clearly rely on broader normalization (`m1`/`1m`/`M1` variants appear in viewer tests; r3 cites test_viewer_features.py:1292 normalizing `"M1"`/`" eurusd "` style input elsewhere). r3 sharpens: if `normalize_timeframe` is the single chokepoint these belong here; if not there are two normalizers and that duplication belongs in production review. Two-line additions, outsized value given timeframe canonicalization's role in data joins.

### tests/test_trade_engine.py

Strong execution-model coverage: next-bar-open fills, bar-low stops, SL-alias, stop-distance anchoring, same-bar TP priority — all against synthetic deterministic OHLCV (positive per all three).

1. **tests/test_trade_engine.py:324-368 — CRITICAL (r2) / major (r1,r3) — engine-parity test environment-dependent, skip-heavy, tautological fallback, weakly asserted.**
   - Tolerance: `tolerance = max(2, int(vectorbt_count * 0.3))` (:367) allows two supposedly equivalent engines to disagree on ~30% of trade counts (+2 absolute) — wide enough to hide systematic fill-logic divergences (same-bar stop ordering producing systematically fewer/more trades) (r2 critical; r1/r3 major, "smoke test wearing a correctness-test name"). r2 adds asymmetry: tolerance anchors to vectorbt_count alone, so failures depend on which engine drifts.
   - Skips: three independent skip conditions (missing strategy file :327, no parquet data :332, no coverage window :343) mean the only cross-engine check in the file silently no-ops on fresh checkouts lacking local user data under `DATA_DIR/ohlcv` — in CI the parity guarantee can be vacuous while green (all three).
   - Tautology: `if vectorbt_count == 0 and native_count == 0: assert True; return` (:363-365) — exactly the scenario where both engines break identically goes unnoticed (r2; r1 notes the literal-True branch).
   - Remedy (consensus): mark skips explicitly with surfaced reason strings, replace local-data dependence with a bundled deterministic fixture, tighten tolerance toward exact equality.
   **[SETTLED by verification]** Code read confirms every citation verbatim: skips at :326-327 (strategy file), :331-332 (`No local 1h OHLCV parquet data available`), :343 (coverage window); literal `assert True; return` at :363-365; `tolerance = max(2, int(vectorbt_count * 0.3))` at :367 anchored to vectorbt_count. Critical rating (r2) stands: the sole cross-engine consistency check is optional, accepts ~30% divergence, and its both-zero path is unfalsifiable.
2. **tests/test_trade_engine.py:18-75 — minor (r1/r3) — production-grade date helpers re-implemented inside the test module** (`_parse_date_bound` end-of-day adjustment, `_to_utc_datetime`, `_time_bounds`, `_has_coverage`, ~30-57 lines). r3 verified they mirror logic in `runner/engines/vectorbt_adapter.py:1050` (same function name/signature) and `tests/strategies/conftest.py:38,58`, with copies already diverging slightly in type (`pd.Timestamp` vs `datetime`); if production boundary semantics change, tests keep validating their own stale copy and the coverage gate could accept files production rejects. Import production helpers or trim.
3. **tests/test_trade_engine.py:51-66 — minor (r1; r3 concurs) — broad `except Exception` converts parquet read failure into `(None, None)` → "no coverage" → skip.** Corrupt fixture/schema drift masquerades as missing data instead of failing loudly; catch specific pyarrow errors, or at minimum log why (r3).
4. **tests/test_trade_engine.py:195,:234,:279 — nit (r3) — `pytest.approx(-1000.0, abs=0.5)` used well but derivation uncommented** (unlike sibling suites); one line of arithmetic prevents future readers "fixing" the constant.
5. Smells: `BacktestConfig(...)` construction repeated ~5-6× — builder factory (r1/r3). Dead code (r3): `_to_timestamp_utc` (:78-82) raised but never called. Sibling test :198 duplicates the bar-low test almost entirely via the SL-alias path — parametrize the two entry conventions instead of copy-paste (r1). Positive pins (r2): next-bar-open fill price vs synthetic values (:133), stop-from-bar-low (:191), PnL ≈ −1000 (:195), alias equivalence (:198-234), fill-anchored stop distance (:237-279), same-bar TP priority asserting both reason and meta echo (:282-321).

### tests/test_viewer_features.py

4790 lines; exercised via `TestClient`, tmp_path isolation, meaningful negative assertions. Overall quality high (r1); coalescing edge-case coverage unusually thorough with exact payloads and negatives (r2); r3 finds no tautologies among payload assertions.

1. **tests/test_viewer_features.py:1258-1279,1642-1672,1698-1724 — major (r2) / minor (r1) — thread doubles keyed off `traceback.extract_stack()` frame inspection.**
   - Mechanism: intercepting only threads constructed from frames whose filename ends with `viewer/core.py` and function ∈ {`_start_data_task`, `_start_dukascopy_task`, `_start_jforex_task`} couples the doubles to production stack layout by string match (r1 minor: renaming/moving silently changes which threads get faked).
   - r2 escalates to **major**: if renamed/inlined, interception silently stops and non-intercepted branches fall through to `real_thread(*args, **kwargs)` — the coalescing assertions (`starts == 1`) then evaluate against genuinely spawned workers, i.e. tests can degrade into launching the real download subprocess machinery (network access from unit tests) rather than failing loudly.
   - Consensus remedy: injectable thread factory/starter seam or patching the starter function directly. *Kept major given r2's concrete silent-network-access failure mode; r1's brittleness framing subsumed.*
2. **tests/test_viewer_features.py:2811-2818,2888-2895,3004-3010,3151-3157 — minor — bounded-but-real sleep-polling loops (all three).** Four act-phases spin `time.sleep(0.05|0.1)` up to 50 iterations (~2.5-5 s budget) waiting on live background threads/subprocesses, then read task state unconditionally; on slow CI the loop exhausts and assertions fail with confusing downstream messages ("status is 'running'") instead of clear timeouts. Remedies: deadline + `pytest.fail(..., f"timed out, last={task}")` or event-driven hooks. Boundedness keeps this minor (r1/r2); r3 notes the spawned real `sys.executable -c ...` process (:2800-2809) makes these integration-ish tests inside a unit suite.
3. **tests/test_viewer_features.py:3037 — minor (r1) — tautological/redundant assertion.** `assert "Running backtest... " in log_text` implied by the stronger preceding assertions (:3035-3036); dead padding check.
4. **tests/test_viewer_features.py:1382-1403 — minor (r1) — unbounded `Barrier(2)` wait can hang the suite instead of failing.** `barrier.wait()` has no timeout; a worker dying before the barrier blocks the other forever; `join(timeout=2)`+`is_alive()` only helps after both reach it. Pass a timeout. Related minors (r2 :1416-1419 / r3 :1418): `failing_starter`'s worker-thread `assert release.wait(timeout=2)` escapes `launch()`'s `except RuntimeError`, killing the thread unobserved — failure mode is hang/confusing late list-comparison error, not a clean red; r3 adds the `# pragma: no cover` on an actually-exercised except clause is misleading. And (r3 :1402-1404): the coalescing race can silently not happen — a missed barrier deadline raises BrokenBarrierError inside the thread while count assertions still pass with one `None`; checking both records reference the same task id would strengthen it.
5. **tests/test_viewer_features.py:2296-2299 — severity split (r1 major / r3 minor) — split-plan expectation encodes overlapping ranges / magic boundary dates.**
   - r1 (**major**): `test_auto_download_source_plan_splits_us_intraday_range` asserts US-intraday plan `[{huggingface, 2025-06-20→2025-07-31}, {alpaca, 2025-07-01→2025-08-05}]` — segments overlap a full month (July covered twice). Either the planner double-fetches a month (real bug blessed by test) or overlap is an intentional safety margin the test neither names nor comments; false confidence either way.
   - r3 (**minor**): the boundary dates come from `HF_INTRADAY_COVERAGE_END` in `viewer/main.py` duplicated as literals; derive from the module constant.
   *Not contradictory — different facets (overlap semantics vs literal duplication). Both retained; overlap question flagged for follow-up.*
6. **tests/test_viewer_features.py:2275-2284 — minor (r1/r3).** r1: needless `viewer_client` fixture dependency for pure `_auto_download_source` calls — misleading setup suggesting endpoint coverage. r3: expectations hard-code routing policy verbatim with no rationale — change-amplifying (verified consistent with `viewer/main.py` today).
7. **tests/test_viewer_features.py:3416-3417 (& :4190) — minor (r2/r3) — p-value asserted as range + `< 0.05`;** the range half is near-tautology (broken p=0.999 passes it), `< 0.05` meaningful for the deterministic curve; smoke-acceptable, weak half noted.
8. **tests/test_viewer_features.py:51-52 — minor (r2) — fixture clears module globals but tests mutate them directly** (:1841, :1912, :1973, :2029, :2065); correct under serial pytest, cross-test contamination under xdist/reordering.
9. **Security gaps (r3, minor):** :2968-2984 `/api/backtests/run` builds a runner.cli command from user JSON; happy path only — no test rejects `strategy` values like `../../x.py` or embedded metacharacters before `Popen`, for a platform launching arbitrary strategy files. :4748-4790 log endpoints tested only with well-formed JSON lines — malformed-line/path-traversal/oversized-log behavior of `/api/logs/{name}` unverified. (Positive, r1: rejection paths 400 pre-genesis :315-344, invalid query timeout :2125-2137, unknown provider :4562-4585; OHLCV fields stripped from public payloads :738-769.)
10. **tests/test_viewer_features.py:1416-1419 — minor (r2)** — see finding 4 (worker-thread assert escape).
11. **tests/test_viewer_features.py:2533 — minor (r3) — `assert isinstance(listed["elapsed_seconds"], int)` brittle;** wall-clock seconds legitimately serialize as float; couples to serialization details.

Smells (convergent): **Divergent Change/God-file (major hotspot, all three)** — ≥15 unrelated concerns (dukascopy genesis, grid matrix, gzip middleware, catalog grouping/merging, download coalescing + crash-recovery markers, task lifecycle/pause/resume/delete, subprocess log streaming, optimizations, signal replay, CAPM/p-value endpoints, portfolios, agent convert, logs); split per resource family. **Duplicated Code:** `_NoStartThread` double re-declared ~5-8× (:1310,:1349,:1469,:1526,:1598,:1748-1750,:1793-1795 plus stack-inspecting variants :1642-1672,:1698-1724); `fake_start` capture helper ~5-6× (:1216,:2098,:2145,:2187,:2224); 20-key portfolio metadata-row dict ×6 (:3532-3554,:3592-3630,:3647-3689,:3722-3743,:3767-3789,:3817-3840); `_equity_curve_from_returns` re-implemented here (:3369-3386) vs tests/test_stats.py:28-36; StockKit CSV fixture triplicated (see test_signal_import). **Private-state coupling (hotspot):** tests poke `DATA_TASKS`, `_COALESCED_CLEANUP_PENDING`, `_TASK_CACHE_HYDRATED`, `_BACKTEST_META_CACHE*`, `DATA_TASKS_LOCK` directly — refactor breaks ~20 tests at once. **Naming:** `test_backtest_run_signals_...` (:3043) vs `test_backtest_run_from_signals_csv...` (:3170) test different endpoints differing by one word (r3); god test `test_create_portfolio_persists_p_value_and_psr` (:4128-4222) exercises five behaviors in one body (r3).

### tests/test_walkforward.py

1. **tests/test_walkforward.py:51-205 — severity split — heavy mocking leaves fold construction/aggregation untested.**
   - r2 (**major**, strongest wording): every OutputStructure/SummaryMetrics/FileOutput test monkeypatches `optimize_portfolio_allocation` whose mock returns OOS metrics directly — the suite verifies "did we copy the mock through"; it stays green if `run_walkforward` stopped computing anything real. The strategy-mode `_FakeEngine` test (:393-421) partially redeems the module (fold construction + engine call ordering for real), but portfolio-mode/backtest_ids aggregation remains untested.
   - r1 (**major** but located at :18-45): signature-introspection tests assert API shape not behavior — parameter names and default literals break on legitimate rename/n_trials change; defaults-as-contract defensible for a CLI-facing entry point but must say so; also `strategy`,`engine`,`backtest_ids` asserted simultaneously though the calling modes appear mutually exclusive (:64 vs :395).
   - r3 (**minor**, judgement call): mocking is appropriate unit isolation; the risk is concentration — fold-window computation covered *only* by :393-421, and deleting that test would silently remove all real fold-boundary coverage.
   *Same underlying fact set (mock-dominated legacy mode; single real-coverage test); severity disagreement logged, not smoothed: r2 major / r3 minor. The suite-level exposure is real but bounded by the existence of the _FakeEngine test.*
2. **tests/test_walkforward.py:297 — minor (all three) — test encodes known limitation as spec.** `assert len(result["folds"]) == 1  # Current implementation has single fold`: future multi-fold support requires editing the test which then reads like a regression; name (`test_walkforward_multiple_backtests`) implies real multi-backtest coverage.
3. **tests/test_walkforward.py:268-270 — minor (r2/r3) — timestamp length check `len(ts_part) == 15`** accepts any malformed 15-char token; parse with `strptime(ts_part, "%Y%m%d_%H%M%S")`.
4. **tests/test_walkforward.py:51,93,120,142,164,186 — nit (r1) — `tmp_path` requested and unused** in ~8 tests that never touch the filesystem.
5. Smells: `mock_optimize` redefined identically 8-9× (:54-60…:278-283) — shared canned-payload fixture (r1/r3). `test_walkforward_output_has_all_required_fields` checks only 3 of the fields its docstring claims (r1). Hidden coupling (r3): `_FakeEngine.run` keys sharpe off call parity (`len(self.configs) % 2`) — silently flips expectations if train/test order changes. Hotspot (r1/r3): two calling conventions (`backtest_ids=` vs `strategy=`+engine) coexist without a note; suite leans backtest_ids-with-mocks leaving strategy mode effectively one test — rebalance.

### tests/test_yfinance_downloader.py

Single regression test: MultiIndex-column + named-index normalization into canonical OHLCV parquet.

1. **tests/test_yfinance_downloader.py:27 — severity split (r1 major / r2 major / r3 minor) — mock discards the call contract; critical kwargs unasserted; positional-arg fragility.**
   - Shared core: `monkeypatch.setattr(yf_dl.yf, "download", lambda **kwargs: payload)` accepts any kwargs and records nothing; a production change stopping `auto_adjust=False` (silently altering price semantics of stored data) or dropping start/end passes unchanged. Capture kwargs; assert the financially meaningful ones (`auto_adjust`, `start`, `end`, `interval`).
   - r2 (**major**) adds a sharper edge: the stub accepts **no positional argument**, so it works only if production calls `yf.download(**kwargs)` with the ticker as keyword; yfinance's real signature is positional-first. Either the downloader pins a call shape that would crash against the real library, or it passes ticker positionally and this stub would TypeError if signatures were checked.
   - r3 (**minor**): brittle double; `def fake_download(*args, **kwargs)` recording calls would be sturdier.
   *Facts agree; weight disputed. **[SETTLED by verification]** `data_tools/downloaders/yfinance_dl.py:198-207` calls `yf.download(tickers=..., start=..., end=..., interval=..., auto_adjust=False, progress=False, threads=False, group_by="column")` — all-keyword — and the installed yfinance 1.2.0 signature's first parameter is literally `tickers`. So: production's keyword call is valid against the real library; r2's "positional-first ticker / stub would crash" branch is **refuted**; r1/r3's core (kwargs discarded → `auto_adjust`/`start`/`end`/`interval` unasserted) **confirmed** as the substantive defect. Final severity: **major** (r1/r2 weight on shared facts), with the positional claim struck.*
2. **tests/test_yfinance_downloader.py:38-41 — minor (r2) — assertions cover row count/column order but never values;** no check that open/high/low/close/volume survived MultiIndex→flat unchanged (e.g. `stored["close"].iloc[1] == 2.2`); column-misalignment bug passes.
3. **tests/test_yfinance_downloader.py:41 — minor (r3) — `startswith("1990-01-02")` string-prefix timestamp probe;** catches day-level shifts but exact Timestamp comparison is stronger.
4. Coverage smell (r1): single happy-path for an entire downloader module — no empty-results/network-failure/multi-symbol/intraday coverage from this area; parametrize index shapes (RangeIndex, tz-naive index) to harden the reset_index regression.

---

## tests/validation/

### tests/validation/__init__.py

Empty package marker file (0 bytes), required so `tests.validation` imports resolve as a package. Nothing to review; accounted for as scanned-clean by all three.

### tests/validation/test_cli.py

Real end-to-end CLI coverage via subprocess: exit codes 0/1/2 pinned (:194-200, :113-126, :238-249), JSON/markdown formats, importer auto-detection, tolerance flags both directions. Good contract testing (all three).

1. **tests/validation/test_cli.py:50-57 — minor/major-borderline — subprocess without timeout (fixed-checklist item; all three).** `_invoke_cli` calls `subprocess.run([sys.executable, "-m", "validation.cli"], capture_output=True)` with no `timeout=` across ~15 invocations; a hang in the CLI (waiting on stdin, network retry loop, report-writing deadlock) blocks the suite indefinitely instead of failing the test. Add `timeout=60`. Positive (r3): cwd pinned to repo root (:56); no `shell=True`; list argv (r1).
2. **tests/validation/test_cli.py:237-250 — minor — exit-code-2 test accepts three different error-message shapes via `or` chain** ("not found" OR "failed to load reference" OR "no such file or directory"). r1: passes whether the CLI handles missing files itself or lets an unhandled OSError traceback through — exactly the distinction this test should pin; tighten to the intended message. r2: exit-code assertion carries all the weight; broad OR weakens the contract but acceptable as robustness choice.
3. **tests/validation/test_cli.py:109 — nit/minor — markdown-format probe with two accepted variants.** `"Overall Passed**:** Yes" ... or "Overall Passed**: Yes"` encodes two renderings with an OR fallback. r1 flags brittleness; r2 explicitly contrasts: here the two strings *differ* so this is legitimate dual-format tolerance — flagged only to contrast with test_markdown_report.py:125 where they are identical; r3 concurs (a third variant fails both, so it's noisy rather than wrong).
4. **tests/validation/test_cli.py:144-190,:290-342 — minor (r1) — same file used as both reference and platform.** Legitimate for self-comparison smoke tests, but reference≠platform divergence through importers is covered only on the JSON path (:203-287).

Smells: ref/platform JSON-writing block repeated 3× (:211-214, :262-265, :241-242) — extract helper (r1); fixtures `tmp_reference_json`/`tmp_platform_json` exist but aren't reused where custom trades are needed — extend with overrides param instead of re-inlining (r3). Each run spawns an interpreter (~0.5–1s ×15) — acceptable, batch or go in-process if it grows (r1).

*Reference-report cross-check (t3-8):* the reference alone flags `tests/validation/test_cli.py:144-190` — generated openpyxl Workbook never explicitly closed before `wb.save()`. **[SETTLED by verification]** Confirmed literally true (:149 builds, :178 saves, no `wb.close()`), but openpyxl workbooks hold no OS-level file handles until `save`, and save flushes/closes internally — consequence negligible; adopted as a nit only, not elevated to the panel's findings.*

### tests/validation/test_cross_platform_metrics.py

Integration-style matrix tests over repo artifacts; correct path anchoring (`Path(__file__).resolve().parents[2]`) and path-containment assertions (:46-48).

1. **tests/validation/test_cross_platform_metrics.py:16-48,:205-219 — major (r1/r2/r3) — hard dependency on mutable repo working-tree data with hard-pinned golden values and no absence guard.**
   - Shared core: primary matrix test reads real repo artifacts (`strategy-comparisons/`, `reports/`, `data/backtests/*.json`) and pins `reference.trade_count == 512`, `win_rate ≈ 0.4492`, `sharpe_ratio == 0.544687` (:30-31,:42) from artifact files living outside `tests/fixtures`, regenerated by scripts imported at :8-9.
   - r1 frames it as golden-file pattern without golden-file hygiene: legitimate regeneration breaks tests while stale-but-present artifacts pass silently — no skip guard, no checksum, no documented refresh procedure in-test; most environment-coupled file in the validation suite.
   - r2 sharpens: **no skip guard for absence**, unlike test_trade_engine which at least skips — fresh checkouts lacking committed artifacts hard-fail (KeyError/FileNotFoundError), or pass against *stale* artifacts while asserting exact historical values; coupling is intentional parity-pinning but nothing distinguishes "artifact missing" from "parity broke".
   - r3: any regeneration of parity artifacts breaks pins with no hint whether matrix code or data changed; prefer synthetic fixture comparisons dirs for logic tests plus at most one smoke test against real artifacts.
   - Consensus remedy: reference the regenerating script in a comment, document refresh procedure, consider copying fixtures under `tests/fixtures`, add skip guards.
   **[SETTLED — no spot-check needed]** Unanimous panel agreement (3/3) plus the reference report's independent T3-4 at the same lines; treated as settled without spending a verification slot.
2. **tests/validation/test_cross_platform_metrics.py:77-106 — minor (all three) — recomputation test asserts only sign.** Seeding stats with `-1.0` then asserting `platform["sharpe_ratio"] > 0.0` proves recomputation happened, not that it's correct (+0.0001 with wrong formula, right sign, passes); assert an approx value derivable from the constructed equity series.
3. **tests/validation/test_cross_platform_metrics.py:109-128 — minor/major-borderline (r3) — timezone-consistency test depends on both real xlsx and real backtest JSON existing;** also normalizes `.replace("+00:00", "Z")` inline in the test (:127) — that normalization belongs in the production serializer being tested, else the test masks a format inconsistency between importer and platform outputs.
4. **tests/validation/test_cross_platform_metrics.py:8-9 — minor (r1/r3) — tests import production scripts as modules** (`scripts.regenerate_mt5_parity_artifacts`, `COMPARISON_CASES`, `QC_RISK_PARITY_OUTPUT`): couples test collection to script import side effects/module paths; if scripts move, this breaks.

Smells: two QC-attribution tests (:131-166, :169-202) differ only in two stat values — parametrize (r1); hidden dependency on `PROJECT_ROOT/"reports"` where user-generated reports can perturb outcomes — apply the tmp_path isolation used at :51 elsewhere (r1); Data Clumps: dir-path triple repeated across seven calls (r3). Positive (r2/r3): negative paths good — missing platform artifact (:51-58), malformed attribution fallback (:169-202), containment check inside PROJECT_ROOT (:46-48, a real path-traversal guard).

### tests/validation/test_delta_report.py

Scanned by all three (538 lines).

1. **tests/validation/test_delta_report.py:98-113 — severity split (r3 major / r1-r2 minor) — dead test utility with unimported `Any` annotations.**
   - Shared core: `_make_field_result(...)` annotates parameters with bare `Any` without importing it; masked only by `from __future__ import annotations` (:3). Any runtime annotation resolution (`get_type_hints`, dataclass tooling) raises NameError; type-checkers flag it. The helper is also dead code — no test in the module calls it (all tests build real comparator results).
   - r3 rates the pair **major** (latent NameError + broken signal for type checkers + dead code); r1 splits into minor (the `Any`) + **major** (dead utility per checklist); r2 lists both as minors.
   *Facts identical; weight disputed. **[SETTLED by verification]** Code read confirms both halves exactly: `_make_field_result` defined at :98 with zero call sites in the module, and `expected/actual/tolerance: Any` at :100-102 under `from __future__ import annotations` (:3) with no `typing` import anywhere in the file. Settled at **major**: dead test utility per checklist plus latent NameError on any annotation resolution — the two defects compound (the only consumer of the annotations is a function nobody calls).*
2. **tests/validation/test_delta_report.py:98-113 / :166-178 — minor — vacuous numeric "match" test (r1).** `test_field_delta_numeric_match` sets `relative_diff=0.000009` and `tolerance=0.0001` itself, then asserts `relative_diff < tolerance`: tautology over its own constants; no production code participates. Also (:136-196) constructor round-trip tests (`test_field_delta_creation`, `test_field_delta_explanation_for_match`, `test_trade_delta_creation`) assert only that dataclasses store attributes while names suggest delta-logic coverage (r1).
3. **tests/validation/test_delta_report.py:267 & :325 — judgement call (minor) — warning-severity mismatch yields `passed is True`, asserted as intended behavior.** r1: class name asserting `passed is True` surprises until the inline comment explains warning-severity policy; rename or document. r3: comment documents the contract, so policy not bug — but a numeric entry-price difference of 0.45% letting a validation report "pass" is surprising for a validation suite; worth an explicit product-level decision note.

Smells: five-line act sequence (matched→compare→…) repeated ~12-15× verbatim across classes — parameterized helper (r1/r3); positional 4-arg `DeltaReportBuilder.build(ref, platform, comps, anomalies)` invites argument-order mistakes — prefer keywords (r1); r2 adds unused `pytest` import (:7, checked: no `pytest.` in body). Positive (r3): severity/passed logic matrix (:316-361), explanation formatting per field type (:423-454), all four anomaly-type conversions (:457-520) precisely asserted; every assertion pins concrete ids/severities/counts.

### tests/validation/test_fixed_risk_model.py

Mirrors the percent-risk suite; good budget-boundary test (`round_lots` must never exceed risk budget).

1. **tests/validation/test_fixed_risk_model.py:158-165 — major (r1) / minor (r2,r3) — test name says "rounds up" but asserted behavior rounds DOWN.**
   - Shared facts: `test_round_lots_rounds_up` computes raw = 33.33 and asserts 30 (nearest-lower multiple of 10); inline comment says "rounds to 30 (nearest multiple)". If production switched to true round-half-up (→40, breaching budget) the test would rightly fail; as named, a maintainer "fixing" rounding to match the name would break the safety property :86-97 enforces. The sibling percent-risk file names the equivalent case `test_round_lots_rounds_down` — inconsistent naming across twin suites (r3); together the two tests pin *floor* semantics, so "rounds_up" is doubly wrong (r3).
   - Weight only: r1 **major** ("misleading name on a money-touching code path"); r2/r3 **minor** ("rename to reflect actual semantics").
2. **tests/validation/test_fixed_risk_model.py:86-97 — minor — self-acknowledged confused derivation in `test_lot_rounding_never_exceeds_risk_budget` (all three).** Comment block (:87-89) narrates abandoned alternatives ("250 ... 260 lots? Instead pick...") left from debugging; final math checks out (259.74 → floor 259, risk 99.715 ≤ 100) and r3 stresses the assertion pair is excellent — exactly the property you want pinned — but the muddled derivation invites someone to "clean up" the test into a weaker form. Rewrite with actual arithmetic (stop_distance = 0.385 → raw = 259.74).
3. **tests/validation/test_fixed_risk_model.py:149-155 — nit (r1) — vacuous-rounding case:** raw 50 already a lot-multiple; rounding branch untested here despite the dedicated file (covered at :158).
4. **tests/validation/test_fixed_risk_model.py:188-195 — minor (r1) — same naming contradiction as percent-risk:** `test_min_position_size_clamping_zero` clamps to 0 with min=50 never asserted as such; add the between-0-and-min distinguishing case.
5. **tests/validation/test_fixed_risk_model.py:237-245 — minor (r3) — `test_stop_distance_pct_when_entry_nonzero` tests a field it admits doesn't exist** ("TradeRiskProfile doesn't have stop_distance_pct field"); rename or flag the missing pct field as a gap.

Smells: near-identical twin of test_percent_risk_model.py (~60% shared structure) — parametrized shared harness over both models would cut ~150-200 lines and expose coverage gaps like percent-model RR-from-meta cases fixed-risk lacks (r1/r3). Dead comment :242-244 explains nonexistent `stop_distance_pct` field (r1). Positive: NaN/inf validation (:275-286) right for sizing code; meta-extraction precedence direct-SL-over-meta-SL pinned at :310-315 (r2).

### tests/validation/test_golden_files.py

Golden-file smoke suite over tracked JSON goldens (verified tracked earlier by r1).

1. **tests/validation/test_golden_files.py:15-18 — minor — missing directory yields zero files rather than error (r1/r2/r3, convergent framing).** `_list_golden_files` returns `[]` when GOLDEN_DIR doesn't exist; the ≥3 guard then fails with a clear message (good), so it's diagnostics not false-pass — but partial golden deletion down to exactly 3 passes everything while coverage silently shrank (r1), and only `test_at_least_three_golden_files` names the absent path in its message while other classes say generically "No golden files found" (r2); r3 notes a misconfigured checkout yields one opaque failure instead of a clear skip. Positive contrast: paths anchored via `Path(__file__)` (:12) — no CWD dependence.
2. **tests/validation/test_golden_files.py:28-45 — nit (r1) — loop-based tests report only the first failing file;** parametrize for per-file granularity. Smell: each test re-reads/re-validates every golden — module-scoped parsed fixture (r1/r3). Weak key set (r1): required stats = `{total_pnl, gross_profit}` (:52) though importer tests require far more keys — inconsistent bar.

### tests/validation/test_importers.py

Base-class contract + CWD-independence check + declared-timezone conversion. Scanned-clean by r1/r3 — notably this file anchors paths via `Path(__file__)` (:13) and explicitly tests CWD independence (`monkeypatch.chdir(tmp_path)` :30 — excellent, directly covering the defect flagged in siblings), pins exact trade count 5 (:39), concrete UTC instants (:43-51), abstract-base and missing-file negatives.

1. **tests/validation/test_importers.py:50-51 — minor (r2) — expected timestamps `"2024-01-03T06:00:00Z"`/`"...T14:00:00Z"` are bare magic strings** with no derivation comment for Europe/Athens (+02:00) applied; arithmetically consistent (08:00→16:00 local), but golden values without derivation make off-by-one-hour regressions indistinguishable from intended fixes.
2. Coverage note (nit, r1): only MT5HTMLImporter gets load-path tests here; MT5XLSXImporter's subclass-ness asserted but loading delegated to its own file — fine, noting asymmetry.

### tests/validation/test_markdown_report.py

Reasonable structural coverage of markdown rendering incl. both input modes (ref+platform vs pre-built ValidationReport).

1. **tests/validation/test_markdown_report.py:125 & :223 — severity split (r3 major-borderline "broken test logic" / r1-r2 minor) — copy-paste tautology in OR assertion.**
   - Shared facts: `assert "Overall Passed**: Yes" in md or "Overall Passed**: Yes" in md` — both disjuncts byte-identical, the `or` is dead; intended second variant (presumably `"Overall Passed**:** Yes"`, cf. test_cli.py:109's genuinely distinct pair) was lost to copy-paste. Same pattern at :223 with `"Overall Passed**: No"` twice.
   - r3 escalates: as written it silently narrows accepted formats *while pretending to be lenient* — restore the alternate format or collapse to a single assertion. r1/r2 keep minor: harmless today but can never catch a formatting change.
2. **tests/validation/test_markdown_report.py:6-31 — minor (r2/r3) — ten-plus unused imports (dead code).** `datetime`, `timezone`, `pytest`, `AnomalyDetector`, `FieldDelta`, `TradeDeltaReport`, `MatchKey`, `TradeMatcher`, `ReportSummary`, `StatDelta`, `TradeComparisonResult`, `Any` imported and never referenced (`datetime.datetime` on :284 is inside a string literal, not the symbol). File visibly copy-pasted from test_report_structured.py without pruning. (r1 separately questions whether `TradeStatistics` in test_report_structured.py:32 is used only via `StatDelta` — verify with lint.)
3. **tests/validation/test_markdown_report.py:275-285 — minor (r1) — negative assertions too narrow to be meaningful.** `test_no_internal_python_objects_in_markdown` checks three literal substrings (`<object`, `datetime.datetime`, `Decimal(`); a repr of any other type sails through. The JSON sibling (test_report_structured.py:369-388) does recursive type checking — reuse over non-prose tokens, or rename to spot-check. (Positive, r3: object-repr leak check exists at all.)
4. **tests/validation/test_markdown_report.py:160,:162,:175 — nit — broad lowercase substring probes** ("mismatch"/"differs", "entry_price"/"Entry") accept either wording; can't detect regression dropping numeric detail entirely as long as one word survives (r1/r2/r3 convergent).
5. **tests/validation/test_markdown_report.py:166,:168,:247,:249,:290 — minor (r2) — direct attribute mutation (`ref_trade.exits[0].pnl = 100.0`) on pydantic models;** if Trade/TradeExit become frozen these tests break en masse. Works today; fragility note.

Smells: Duplicated Code **major-by-count (r1)**: 5-line ref/platform setup block repeats verbatim in all ~22 tests — strongest duplication candidate in the group; sixth copy of the `_make_trade/_make_reference/_make_platform_result` triple (r2/r3). Formatting-string probes (`- **Symbol**`) churn with cosmetic renderer changes — assert semantic content via the JSON report instead (r1). Positive (r2): report-object round-trip tests (:228-243, :287-302) verify JSON→model→markdown consistency.

### tests/validation/test_mt5_html_importer.py

Mirrors the xlsx importer suite against the tracked HTML fixture; helper unit tests included.

1. **tests/validation/test_mt5_html_importer.py:15-17 — minor/major-borderline — CWD-relative fixture path without skip guard (r1/r3; r2 implicitly via cross-file note).** `Path("tests/fixtures/validation/mt5_html/sample_report.html")` resolves against process CWD; runs from repo root pass, from any other directory ~25 tests error FileNotFoundError rather than skipping. Anchor with `Path(__file__).resolve().parents[2]` (as test_cross_platform_metrics.py:13 does correctly).
2. **tests/validation/test_mt5_html_importer.py:48-50,:85-88 — minor — weak/dishonest goldens.** r1: `len(ref.trades) >= 5` floor vs xlsx sibling pinning exact counts — an exact count catches row-loss regressions; r2 notes test_importers.py:39 pins exactly 5 for the same fixture so the weaker bound here is redundant-but-safe. r3: `test_entry_exit_direction_consistency` again validates only quantity equality despite its name (copy of the misnamed xlsx-suite test) — implement per-direction price-side checks or rename.
3. **tests/validation/test_mt5_html_importer.py:58,:60 (& xlsx :67,:70) — minor — `startswith("2")` century-dependent smoke checks;** ISO regex, exact fixture timestamp, or `datetime.fromisoformat` parse would actually validate (r2/r3; r1 nit).
4. **Dead imports — minor (r2):** `from typing import Any` unused (:6); `Trade` imported (:10) never referenced.

Smells: `TestHelpers` duplicates `_coerce_setting`/`_extract_first_percent`/`_parse_count_pct`/`_infer_exit_reason` tests nearly verbatim from test_mt5_xlsx_importer.py:222-247 — because production duplicates those helpers too; fix once in a shared parser mixin and both suites collapse (Shotgun Surgery mirror image, r3). Repeated `importer.load(_sample_path())` parsing (~20 fresh parses/run) → module-scoped fixture (r1/r2/r3). Positive (r3): negative cases cover missing file and empty HTML (:164-173); solid field-level coverage (long+short present, wins+losses present, SL/TP exit reasons).

### tests/validation/test_mt5_xlsx_importer.py

Real-data golden tests against three tracked xlsx exports (r1 verified present & git-tracked) plus unit tests of private parsing helpers incl. timezone conversion.

1. **tests/validation/test_mt5_xlsx_importer.py:15-24 — severity split (r3 major / r1-r2 minor) — CWD-dependent repo-artifact dependence with no skip guard and hard-pinned data values.**
   - Shared facts: `_eurusd_path()` etc. use repo-relative CWD-dependent paths (`strategy-comparisons/intraday_volatility_breakout/*.xlsx`) instead of anchoring to `Path(__file__)`; unlike test_regression.py:390 there's no existence check/skip guard, so absent/moved/LFS-migrated workbooks produce noisy errors, not clean skips; assertions pin data values — `total_trades == 492` (:98), `ATR_Period == 5` (:103), `source_timezone == "UTC"` (:109) — so a legitimate re-export breaks ~20 assertions.
   - r3 (**major**, environment coupling): fail outright if files are absent/moved/re-exported; should ship small fixture xlsx under `tests/fixtures/validation/` or skip-guard like siblings.
   - r1/r2 (**minor**): same substance framed as robustness/hygiene (CWD dependence + missing skip guard; literal-vs-crosscheck note below).
   *Kept split pending no contrary evidence; the environment-coupling theme already carries major weight via cross_platform/trade_engine.*
2. **tests/validation/test_mt5_xlsx_importer.py:98,:168 — minor (r1) — exact-count golden duplicated as both literal and cross-check.** :98 pins 492 literally; :168 asserts `len(trades) == stats["total_trades"]` — the cross-check is the valuable invariant; the literal breaks on re-export even when parsing is correct. Know which is load-bearing.
3. **tests/validation/test_mt5_xlsx_importer.py:188-197 — minor (all three) — dead branch structure in consistency test.** `if direction == "LONG": pass else: pass` does nothing; only final volume equality asserted; name promises direction consistency but validates quantity equality. Drop the branch or assert actual in/out side semantics ("buy in, sell out") — currently untested (copy-paste residue).
4. **tests/validation/test_mt5_xlsx_importer.py:67,:70 — nit — `startswith("2")` year probe,** brittle-by-construction (see html sibling).
5. Dead imports (minor, r2): `Any` (:6), `Trade` (:10).

Smells: Performance — every test calls `importer.load(_eurusd_path())` afresh (~20 parses of a 492-trade workbook per run); module-scoped cached fixture cuts suite time substantially (all three). Feature Envy — `TestHelpers`/`TestSourceTimezone` exercise private statics (`_coerce_setting`, `_to_iso`, `_infer_exit_reason`); acceptable white-box choice binding to private API (r1/r3). Positive (r1/r3): timezone handling tested properly (Athens→UTC :250-264 with DST-correct January offset, invalid zone rejected :266-274).

### tests/validation/test_paper_readiness.py

Scanned by all three (525 lines).

1. **tests/validation/test_paper_readiness.py:142-148,:147 — latent fixture bug — severity split (r1 major / r2 minor).**
   - r1 (**major**): `_make_risk_report` double-constructs results so `passed` can contradict its own contents: `(results or [_make_risk_result()])` evaluated twice creates two *different* `RiskComparisonResult` objects — one stored in `trade_results`, another used only for `passed=all(...)`. With defaults both pass so today they agree, but any future failing default makes the report claim `passed=True` while containing failing results — fixtures hiding production bugs.
   - r2 (**minor**) reads the same line differently: the `or` short-circuits so with non-empty results it's fine; the footgun is `results=[]` fabricating one implicit passing result instead of expressing "no risk trades". Edge-case footgun in the helper either way.
   *Same line, different failure modes described (double-construction vs empty-list semantics) — not strictly contradictory; both retained. **[SETTLED by verification]** Code read (:142-148) confirms both readings simultaneously: `(results or [_make_risk_result()])` appears twice — :146 stores one instance, :147 constructs a second solely for `passed=all(...)` (r1's double-construction, real); AND when callers pass an explicit empty list `[]`, `or` falls through to a fabricated passing result rather than expressing "no risk checks" (r2's footgun, also real). r1's latent-bug framing is the more consequential reading; final severity **major** per r1.*
2. **tests/validation/test_paper_readiness.py:510-525 — minor (all three) — test name/docstring contradicts assertion.** Docstring says "All optional inputs omitted → NOT_READY because required artifacts missing"; body constructs ReadinessConfig disabling all four `require_*` flags and asserts READY (:521). Config-plausibly-correct behavior means the docstring is stale — misleading either way (r1); the default-config NOT_READY-on-missing-artifacts path named in the docstring has zero coverage — the more safety-critical branch (r2/r3); fix docstring and add the default-path case.
3. **tests/validation/test_paper_readiness.py:380-406 — minor (r1) — config knobs may not bind to the check under test.** `ReadinessConfig(max_position_size_diff_pct=0.001, max_stop_distance_diff_pct=1.0)` passed but failure comes from pre-baked `FieldComparisonResult(passed=False)`; config appears inert in this path — if the validator ignores them, undetectable; mild false confidence.
4. **tests/validation/test_paper_readiness.py:220 & :292 — minor (r3) — severity expectations hard-coded to error vs warning:** tests build deltas already labeled `severity="error"` then assert the validator reports an error; classification happens upstream in delta_report/comparison, so these partially assert what they themselves put in — fine as validator-level tests, not end-to-end severity-classification proof.
5. **tests/validation/test_paper_readiness.py:74-80 — minor (r1) — regression result fixture hardcodes `/tmp/golden.json`;** bypasses tmp_path hygiene (never opened — harmless) but normalize for hermeticity.

Smells: 6-7-line validate() call block rebuilt ~13-14× — parametrized fixture cuts ~100 lines (r1/r3); `(report, regression, risk, stats, config)` positional clump — bundle/fixture (r3). Speculative Generality nit: `_make_stats(net_profit=...)` mostly defaulted (r1). Positive (r2): threshold boundary for missing trades within/above, severity escalation, explicit level-calculation triptych (:472-508) well pinned; r3 calls it best-structured of the validation suites reviewed.

### tests/validation/test_percent_risk_model.py

Strong parametrization of validation errors, boundary tests for rounding/clamping, adapter-integration checks.

1. **tests/validation/test_percent_risk_model.py:310-331 — major (r1/r3) — test verifies nothing about meta extraction of take-profit; its own comments admit it.**
   - Shared facts: builds `inp` with `take_profit=None`, never calls `validate(inp)` on it (first input dead), then builds `inp2` pulling TP from meta *manually in the test* (`trade.meta.get("take_profit")`) and sizes that. Name promises "validate extracts take profit"; body proves only RR computes when TP supplied. Comment block (:320-322) concedes "meta extraction is for stop_loss only… that's adapter-level". False confidence that the model extracts TP from trade.meta when it does not.
   - r1 **major** (misleading test); r3 **major** (misleading test + dead fixture; rename to `..._when_take_profit_passed_explicitly` or implement/verify real extraction); r2 **minor** with same substance.
2. **tests/validation/test_percent_risk_model.py:245-252 — minor — min-position-size clamping-to-zero semantics ambiguous.** Sets `min_position_size=200.0`, asserts size clamps to **0.0** (not 200); comment says "below min 200 -> clamped to 0" so zeroing-out is evidently intended ("can't take a position that small"), but the reader can't tell whether min means "floor" or "veto threshold" — config value never appears in the assertion. Rename (`test_below_min_position_size_yields_zero`) and add the between-0-and-min case where clamp-vs-veto differ (r1). r3 adds: also assert post-clamp per-profile `risk_amount` (== 0?); if profile still reports risk_amount 100 while size is 0, dashboards overstate exposure.
3. **tests/validation/test_percent_risk_model.py:208-214 — minor (r1) — `test_round_lots_enabled` exercises no rounding.** Raw 50 already a multiple of lot 10; floor-rounding branch never executes (disabled-case :217 covers it); use a non-multiple like :221 does.
4. **tests/validation/test_percent_risk_model.py:217 — minor/nit (r2) — naming drift mirror-image:** `test_round_lots_rounds_down` comment describes nearest-rounding that happens to round down here (`round(33.33/10)=3 → 30`); raw 36 would produce 40 — "down" not guaranteed; consistent with the misleading-naming pattern in test_fixed_risk_model.py:158.
5. **tests/validation/test_percent_risk_model.py:374-379 — minor (r1) — sentinel conflation.** `test_trade_id_zero_when_no_trade` blesses `0` as the "no trade" ID; if Trade ids are 1-based this collides with a real id 0; use an explicit sentinel constant.

Smells: `PercentRiskConfig(risk_percent=0.01)` + model rebuilt in ~25-30 tests — module fixture (r1/r3). Section banner "Meta extraction" (:281) contains the one test that doesn't extract from meta (r1); banner comments copy-pasted across sibling sizing files (r3). Positive (all three): validation-error coverage zero/negative/None/inf balance (:143-172), NaN entry (:396), inf SL (:403), zero-reward RR → None (:354-359), adapter dispatch type errors with matched messages (:455-461) — exactly right for a sizing model; good numeric derivations in comments throughout (r3).

### tests/validation/test_quantconnect_result_importer.py

Called the strongest file in the chunk by r2 (synthetic fail-closed tests match exact anchored-regex messages :202-307/:488-526/:639-719; fee params parametrized over 8 invalid inputs incl. NaN/currency-case :529-552; Decimal precision end-to-end :555-588; CSV↔JSON mismatch both kinds + escaped-tab injection rejection :722-871). r1: extensive fail-closed coverage with exact message matching, Decimal-exact fee derivation. r3 scanned-clean apart from a fixture smell.

1. **tests/validation/test_quantconnect_result_importer.py:19-29 — major (r1/r2) — import-time environment coupling of reference paths.**
   - r1 (**major**): `_reference_dir()` reads `QUANTCONNECT_REFERENCE_DIR` and is evaluated at module import (:26), freezing RESULT_JSON/ORDERS_CSV/TRADES_CSV; a stale exported value redirects all 8 `golden_reference` tests (:302-636) to different data while they claim to validate the tracked reference (`tests/fixtures/validation/quantconnect-reference/`); if fixtures ever absent, module-scoped fixture's bare assert (:304) errors 8 tests unhelpfully instead of clean `skipif`. Files git-tracked today (verified) — robustness/misleadingness rather than breakage.
   - r2 (**major**, overlapping): module-scoped `golden_reference` fixture loads a 597 KB real QC export (`Sleepy Tan Coyote.json`) behind bare `assert RESULT_JSON.is_file()`; absence (fixture pruned for size) is an **error not a skip** — inconsistent with `pytest.skip` usage at test_regression.py:389; erroring defensible for parity pinning but inconsistent with the rest of the suite.
   **[SETTLED by verification]** Code read confirms `_reference_dir()` reads `QUANTCONNECT_REFERENCE_DIR` and `REFERENCE_DIR = _reference_dir()` is frozen once at import (:26), with the three paths derived from it (:27-29) — r1/r2's mechanics exact. r3's clean scan is a **false negative**: the env-capture defect is structural (import-time freeze + bare existence assert in the fixture), independent of test-body quality. Final: **major** stands; reference report's t3-13 rates the same defect minor — panel weight wins.*
2. **tests/validation/test_quantconnect_result_importer.py:328-346,:341-346,:354-376,:414-435,:591-622 — severity split (r2 major / r1 minor) — brittle over-specified goldens with zero derivation.** Exact cluster-size distribution dict, literal 20-element order-ID list, 264 orders / 84 clusters / 236 trades / 160-76 win-loss / fee remainder 81.97 / 5471 equity points — excellent regression pins, but pure goldens with no regeneration story: any legitimate re-export invalidates ~40 assertions at once across four test functions; nothing in-file explains how to regenerate them (regenerator scripts exist for MT5 cases; no QC equivalent referenced). r1 keeps minor: the invariant checks at :451-456 are the valuable part; demote the literal lists. r2 adds intra-test duplication: `observed_remainder = Decimal("2698.00") − Decimal("2616.030")` (:412) restates numbers already asserted at :372-373 instead of deriving from `fees[...]` — classic duplicated-golden hazard.
3. **tests/validation/test_quantconnect_result_importer.py:902 — minor (r1/r2) — statement with no effect.** Bare `json.dumps(reference.raw_metadata)` after first load reads as leftover debug line or undocumented serializability smoke check; can never fail as written (no assert on round-trip); add `assert isinstance(..., str)` or a comment, or delete.
4. Fixture smell (minor, r3): long parameter lists building large synthetic QC result dicts inline repeatedly; shared builder would cut noise. Long-file note: 912 lines driven by table-driven cases but content coherent — splitting optional (r3).

Security (r1, positive): this suite *enforces* fail-closed parsing (non-finite profitLoss :202, fractional order id :215, trailing-text price :225, duplicate/unknown IDs :269/:282/:687/:697, negative fee remainder :510) — security-relevant for import paths.

### tests/validation/test_regression.py

Solid behavioral coverage of golden-file regression incl. tolerances, empty goldens, missing files; tolerance matrix (:306-381) thorough with each side exercised; inf-vs-inf and inf-vs-value profit-factor semantics pinned (:460-476) — genuinely valuable edge case (all three).

1. **tests/validation/test_regression.py:87-101 — minor (all three) — count assertion compares against a hand-copied field list, not the source of truth.** `len(result.stats_delta)` vs length of inline 10-field literal duplicated from production: asserts only the *count*, never the names (r2 sharpens: renamed-field-plus-dropped-field keeping total 10 passes — the sibling test_stats_comparison.py:37-48 does it right with a set equality; copy that shape). If production adds an eleventh metric the test fails confusingly-good, but the list rots independently of what `_compare_stats` emits; assert `RegressionConfig`'s tracked-field definition or specific fields.
2. **tests/validation/test_regression.py:389-391 — minor (all three) — relative fixture path depends on CWD with silent skip.** `Path("tests/fixtures/validation/mt5_html/sample_report.html")` resolves against process working directory; from another directory the `if not html_path.exists(): pytest.skip` makes the only importer-integration regression test vanish silently (golden-file round-trip through a real importer lost). Anchor with `Path(__file__).resolve().parents[2]`.
3. **tests/validation/test_regression.py:172-183 — minor/judgement-call (r3) — weak isolation in `test_regression_trade_and_stats_both_fail`:** second-trade removal also changes net profit, conflating "missing trade changed aggregate" with tolerance logic; delivers both failure modes, just not independently.
4. **tests/validation/test_regression.py:460-468 — minor (r1) — infinity-vs-infinity comparison asserted passed without pinning sign semantics;** `-inf` vs `inf` would also be "equal" under naive implementations — add the `-inf` case.

Smells: Duplicated Code **major-volume (r1)**: the 4-line golden-write+run scaffold repeats verbatim ~20-25× across classes — parametrized helper/context manager halves the file (r1/r3). Dead imports (r3): `copy` (:5), `FieldDelta`, `TradeDelta` (:14), `MatchKey` (:15) unreferenced. Naming nit: `TestImporterAndEdgeCases.test_regression_run_with_importer` exercises importer path *and* asserts round-trip identity (r1).

### tests/validation/test_report_structured.py

Good structural coverage incl. strict-JSON sanitization (inf/nan → null) and precomputed-delta injection paths.

1. **tests/validation/test_report_structured.py:231-245 — minor (all three) — tautological assertion pads summary test.** `assert trade_summary["errors"] + trade_summary["warnings"] >= 0` can only fail on negative counts — effectively asserting keys exist and are numeric; unfalsifiable; replace with concrete counts for the fixture (which has exactly 1 missing + 1 extra).
2. **tests/validation/test_report_structured.py:427-439 — minor (r1) — misleading comment about strict JSON verification.** Comment claims `json.loads` rejects NaN/Infinity without parse hooks — Python's `json.loads` *accepts* them by default; the guarantee comes from `_sanitize_json_numbers` + `json.dumps(..., allow_nan=False)` re-serialization (:439). The final dumps' return value unused; sanitization regression to bare `NaN` tokens would still be caught only at :439 — fine, but the misstatement could mislead future edits.
3. **tests/validation/test_report_structured.py:337-341 / :339 — minor (r1/r3) — over-broad exception match.** `pytest.raises((TypeError, AttributeError))` for a string platform argument accepts either failure mode and keeps passing even if the error drifts to accidental AttributeError deep inside serialization; pin the specific contract.
4. **tests/validation/test_report_structured.py:354 — minor (r2/r3) — near-tautology `assert "Yes" in md or "No" in md`;** almost any rendered markdown containing either word passes; pin `Yes` since the fixture passed.
5. **tests/validation/test_report_structured.py:393 — minor (r2/r3) — suspicious unnormalized input with no corresponding assertion.** `symbol=" sym"` (leading space) fed to ValidationReport but dump asserts only strategy_name/summary fields — if the model strips symbols that behavior is untested; if not, dead/confusing fixture data; assert `d["symbol"] == "sym"` or use a clean literal.
6. **tests/validation/test_report_structured.py:269-278 — minor (r1) — undocumented equivalence claim.** `test_platform_result_as_validation_reference` shows ValidationReference accepted in place of BacktestResult; intentional API flexibility unnamed — one-line comment prevents someone "fixing" it.

Smells: helper triple byte-identical to test_delta_report.py (:26-95 both) — shared `tests/validation/helpers.py` (r1/r3). r1 self-query: `TradeStatistics` import at :32 possibly unused beyond `StatDelta` — verify with lint. Positive (all three): strict-JSON section (:405-439) exemplary; recursive type check (:369-388/:376-388) guards whole-payload JSON safety.

### tests/validation/test_risk_comparison.py

Well-designed isolation tests: each mismatch class verified to fail *only its own field* while others stay green; delta-report conversion incl. missing/extra.

1. **tests/validation/test_risk_comparison.py:183-199 — minor (r1/r3, convergent judgement call) — extra-is-info policy asserted without comment.** Test asserts `report.passed is True` for an unmatched extra trade while sibling missing-case (:163-179) fails the report for a *warning*. May be intended taxonomy (extras informational, missing warnings that block) mirroring anomaly-severity policy — but nothing states it, and the name couples severity to gate semantics; if extras later warn, nothing hints why it flips. Add a one-line policy comment.
2. **tests/validation/test_risk_comparison.py:96-115 — minor (r1) / positive-technique note (r3) — tolerance knobs set ad hoc per test.** Values like stop_distance_pct=0.5 chosen to isolate fields are correct but unexplained — a table-driven parametrize with expected per-field outcomes would document the decision matrix; r3 counters the isolation is deliberate and correct (each test loosens other tolerances to 1.0) — noting positively.

Smells: 6-line prologue repeated ~11 tests (r1); `t1 = _make_trade(trade_id=1, entry_price=100.0, stop_loss=90.0)` line across ~10 tests could be a fixture; seventh copy of helper family (r3); Data Clumps: `(stop_distance_pct, risk_per_trade_pct, position_size_pct)` always adjusted together — named scenario tuple (r1). Summary counts cross-checked against per-trade severities (:202-229) — positive (r2).

### tests/validation/test_risk_sizing.py

Good coverage of signal conversion, fallback-stop semantics (incl. ATR meta), direction enforcement, exposure caps, aggregate risk. No bugs found — assertions specific and hand-computed (r1/r2 scan clean; r3 minor notes only).

1. **tests/validation/test_risk_sizing.py:200-207 — minor (r3) — wrong-side stop rejection asserts zeros only;** no assertion the profile records *why* (diagnostic/explanation field); if sizes silently zero for any validation failure, a bad-importer stop becomes indistinguishable from an intentional block — one observability assertion would pin it.
2. **tests/validation/test_risk_sizing.py:189-197 — judgement-call (r3)** — ATR-fallback test constructs its own RiskEngine rather than the adapter default; isolation fine, but the *default* engine's ATR wiring never exercised end-to-end. Related positive (r2): ATR fallback derivation commented (0.005×3.0=0.015 at :195); risk_amount=100 = 1% of 10k derived at :154.
3. Nits (r1): two constructor round-trip tests (:292-324) re-assert values passed in; imports production privates (`_trade_to_signal`, :18-19/:22) pinning internal names.

Smells: `RiskAdapter(pip_value=1.0)` + `_make_trade` scaffolding ~18-20 tests — fixture (r1/r3). Primitive Obsession: direction strings "LONG"/"SHORT"/"buy"/"sell" as raw literals; shared constants would catch typo drift ("Buy" vs "buy") (r1). Dead import: `math` (:13, r3). Positive: wrong-side stop rejection (:200-207), zero-balance guard with message match (:210-214), exposure/position caps zeroing size (:217-235), pip-value inverse-scaling property (:238-246), LONG/SHORT fallback-stop sides (:330-345) — exactly the safety surface, covered.

### tests/validation/test_stats_comparison.py

Compact and correct: identical-stats field-set equality, tolerance arithmetic verified numerically (:61-64), absolute-tolerance config, inf profit-factor both ways. Scanned-clean by r2.

1. **tests/validation/test_stats_comparison.py:32-49 — minor (r1) — exact field-set assertion duplicates production knowledge** (same 10-field list as test_regression.py:87-101); two tests updated in lockstep if `_compare_stats` grows; centralize the expected set in one shared constant/helper. r3 notes it's the better shape of the pair (exact set assert vs bare length) but same caveat.
2. **tests/validation/test_stats_comparison.py:12-29 — nit (r1/r3) — `_trade` derives direction from pnl sign** (`LONG if pnl >= 0 else SHORT`) — semantically meaningless for stats; harmless here but a copy-paste of this helper into a comparison-sensitive suite would encode a wrong assumption.

### tests/validation/test_trade_anomalies.py

High-quality behavioral suite: severity taxonomy per field exhaustive, duplicate detection both directions, integration paths. No bugs found (any reviewer).

1. **Dead imports — minor (r1):** `FieldComparisonResult`, `TradeComparisonConfig`, `TradeComparisonResult` imported at :14-17 never referenced in module body (only `TradeComparator`'s return value used); copy-paste residue from comparison-test files. (r2's cross-cutting list agrees.)
2. Smell: third verbatim copy of `_make_trade/_make_matched/_make_reference` triple (:24-83) after delta_report/report_structured — shared helpers module (r1/r3); Repeated Switches: `TestDetectMismatches` runs the same four-line shape nine times differing only in field/severity — parametrized `(field, ref, act, expected_severity)` collapses ~80 lines and makes a missing-field gap visible as an absent row (r3).

### tests/validation/test_trade_comparison.py

Exemplary tolerance-boundary suite: every field gets within/outside/custom-tolerance triads plus None-vs-float semantics, OR-tolerance logic documented inline (r1/r2 scan essentially clean).

1. **tests/validation/test_trade_comparison.py:281-291 — minor — copy-paste error in test name: says "mismatch", asserts a pass.** `test_zero_vs_nonzero_pnl_mismatch` ends `_assert_field_passed(result, "pnl")` and `assert result.is_match`; body comments correctly explain OR-tolerance lets |0.001| ≤ 0.01 pass — assertion right, name wrong (left over from pre-OR version or copied from :253). Anyone auditing failures by name wrongly believes zero-vs-nonzero PnL fails validation. Rename e.g. `test_zero_vs_nonzero_pnl_passes_on_abs_tolerance`.
2. **tests/validation/test_trade_comparison.py:262-272 — nit (r1) / product-awareness note (r3):** `test_pnl_outside_abs_tolerance_only` also passes despite name suggesting failure territory (comments mitigate; rename both to describe outcome). r3 adds the substantive point: OR logic means a $0.01 abs tolerance never binds when pct passes — large-PnL trades can drift arbitrarily in absolute terms; tests encode current behavior correctly but PnL equality is usually a hard requirement in validation contexts — flagged for product awareness.
3. **tests/validation/test_trade_comparison.py:83 — nit (r1/r3) — hardcoded field count `len(result.field_results) == 10`** duplicates production knowledge; adding an eleventh compared field breaks with unhelpful message; assert the exact key set (doubles as documentation).

Smells: dead `math` import (:5, r1); fourth copy of `_make_trade/_make_matched` (:20-65, r3); per-field classes repeat within/outside pairs — parametrize `(field, ref, act, passes)` (r3). Positive: boundary discipline (values just inside/outside tolerances :116/:123, :165/:173) with derivations in comments.

### tests/validation/test_trade_matcher.py

Thorough matcher coverage: temporal tolerances (boundary pairs), symbol gating, direction mismatch → missing+extra semantics, empty-input matrix, duplicate-timestamp pairing order, tz normalization incl. naive-as-UTC, MatchKey UTC canonicalization. Scanned-clean by r2 ("Excellent boundary coverage").

1. **tests/validation/test_trade_matcher.py:198-234 — minor (r1) / positive-with-caveat (r3) — input-order pairing asserted as behavior without stating it's a guarantee.** Positional pairing for duplicate keys documented via docstring (good), but the ambiguous case is uncovered: duplicate keys where the wrong pair produces different downstream comparisons (ref [A,B], act [B,A], same timestamps, different prices) — precisely where order-pairing masks real mismatches; add a case asserting the deliberate trade-off (r1). r3's caveat: if the matcher switches to greedy best-effort matching under tolerance, expectations flip silently — acceptable coupling, noted.
2. **tests/validation/test_trade_matcher.py:103-116 — nit (r1) — boundary tested at ±5s exactly but not ±4/±6** to pin inclusive-vs-exclusive; boundary off-by-one is the classic matcher bug. (r2/r3 treat the exact-boundary discipline at :102-133 positively.)
3. **tests/validation/test_trade_matcher.py:247-251 — minor (r3) — naive-timestamp-as-UTC asserted only via match success,** contrasted with :271-276 asserting the canonical "Z" form — consistency gap only.
4. Smell: eighth copy of `_make_trade` family (:11-31); r2/r3 query a possibly-unused `pytest` import (:5).

### tests/validation/test_trade_statistics.py

Excellent numeric suite: hand-computed expectations with shown arithmetic, edge cases inf/zero/single-trade. Called the strongest file reviewed by r3 and "model validation-grade" by r2.

1. **tests/validation/test_trade_statistics.py:243-257 — minor — Sharpe expectation re-implements the production formula rather than an independent value (r1) / population-variance convention implicit (r2/r3).** `expected = (20.0/math.sqrt(2680.0))*math.sqrt(252.0)` mirrors the calculator's expression — a shared mistake (annualization factor, population-vs-sample std) cancels out and still passes; the ≈6.133 sanity value mitigates (r1). r2/r3 sharpen: comment computes variance ÷N (population) — if production switched to n−1 the test fails ~11%, fine as pin, but the comment should say "population std" explicitly since annualized-Sharpe conventions differ. Prefer at least one literal precomputed constant. Same pattern Sortino :275-289, whose downside variance divides by number-of-downside-points (coexisting convention by design — comments are the only spec, r2).
2. **tests/validation/test_trade_statistics.py:185-195 — minor — drawdown-pct denominator convention only implicit.** 0.7 = 70/100 works only because peak is the first trade; higher-peak case covered at :206-219 (good), but peak-equity-vs-initial-capital convention should be pinned in one more asymmetric case to prevent silent convention change (r1); r3 concurs — one clarifying line prevents false "bug" reports.
3. **tests/validation/test_trade_statistics.py:198-203 — nit (r1) — name says "never positive equity"** while scenario is "always losing from zero start"; pct==0 documents a /0-guard; comment-reference production behavior.

Smell: 5-trade `[100,-50,30,-20,40]` fixture duplicated between Sharpe (:248-254) and Sortino (:280-286) — extract once (r1). Everything else verified arithmetically by r3 (DD sequences :170-173/:206-210, downside variance :278, recovery factor :303, break-even exclusion :403-413).

### tests/validation/test_validation_reference.py

Pydantic model contract: allowed enums, empty-trades gating, `to_backtest_result()` mapping fidelity, raw-metadata round-trip.

1. **tests/validation/test_validation_reference.py:79 — minor (r2) — `pytest.raises(ValueError)` without `match=`** on empty-trades rejection (same pattern :65, :73): any unrelated ValueError from pydantic coercion satisfies the assertion; low risk given tight construction, but message-scoped matches prevent false positives. r1's related note (:60-66): the allow-list acceptance loop verifies construction succeeds but can't catch a framework typo'd *into* the allowlist — near-tautological by construction; the rejection case carries the meaning.
2. **tests/validation/test_validation_reference.py:121-125 — minor (r1) — notes assertion checks substring containment** ("mt5_xlsx" + path in notes); formatting changes dilute/break — acceptable. r3: included to note the file is clean otherwise.

Smell: `_make_minimal_valid_reference` overlaps heavily with test_cli.py's `_make_reference` — consolidate (r1). Empty-trades gating both ways (:77-85), roundtrip preservation, engine mapping pinned concretely (r3).

---

## tests/validators/

Production-style helper code under `tests/validators/` — no pytest modules (no test functions); reviewed as code by all three reviewers. Both validators are load-bearing: `AICodeValidator` imported by tests/integration/test_ai_code_quality.py, tests/integration/test_strategy_pipeline.py (per r1 grep), tests/e2e/lab/test_conversion.py; `BacktestValidator` consumed by tests/integration/test_strategy_pipeline.py (r3). Neither has dedicated unit tests inside chunk C — both reviewers independently recommend a direct table-driven suite including bypass/tz-crash cases.

### tests/validators/code_validator.py

1. **tests/validators/code_validator.py:20-22,:48-60 — MAJOR (security) — denylist trivially bypassable while `passed=True` reads as a safety verdict; threat model undocumented (all three, convergent).**
   - Forbidden set = bare-name `eval`/`exec` AST calls plus attribute-call `os.system`. Documented bypasses sailing through: `getattr(builtins,'eval')(...)`, `builtins.eval(...)`, `__import__('os').system(...)`, aliasing (`e = eval`), `os.popen`, nested attribute chains (value is Attribute not Name), decorator tricks; `subprocess`/`pty`/`socket`/`importlib` imports not scanned at all (r1/r2/r3 enumerate overlapping sets).
   - As a lint heuristic this is fine; if any caller treats `passed=True` as "safe to import" it is false assurance (r1 goes as far as calling a critical false-assurance bug *conditional on downstream consumption*; all three settle on major given no evidence of such a consumer in-scope).
   - Compensating control, partially: subprocess import-time check (:85-133/:116-122) catches module-level side effects — but that check *executes* the candidate in a subprocess whose isolation is `-I` + timeout=8 + temp-dir cleanup + `check=False` — availability hygiene, **not containment**: filesystem/network access intact, full user privileges (r1 security finding; r2 calls executing untrusted code by design "the real exposure" and the most significant security-relevant item in the area; r3 concurs).
   - Remedies (consensus): document "best-effort heuristic" loudly, extend forbidden sets (`subprocess`, `os.popen`, `__import__`, `importlib`), or switch to an allowlist AST approach with capability isolation at execution time.
2. **tests/validators/code_validator.py:135-137 — minor — score/passed divergence undocumented (r1/r2) and warnings don't affect `passed` (r1).** A module with 20 warnings scores 0.0 yet `passed=True`; conversely score 0.8 with `passed=False` possible — interplay undocumented for consumers; formula `max(0, min(1, …))` duplicated with backtest_validator scoring (extract one scorer, r1 nit / r3).
3. **tests/validators/code_validator.py:97-122 — minor — stderr of a successful run becomes only a warning (:132-133)** so a strategy emitting tracebacks on import still passes with −0.05 (r1); 8-second timeout short for cold CI interpreter starts — slow pandas import flakily reports "Subprocess import timed out" looking like strategy defects (r2). Checklist compliance noted positively by r1/r2/r3: timeout present, isolated mode, temp cleanup, stderr surfaced not swallowed.
4. **tests/validators/code_validator.py:82-83 & :102-113 — minor (r3) — interface check can false-pass uninstantiable classes:** `callable(getattr(cls, name))` passes for any unbound method; a class whose `__init__` requires args satisfies the contract while unusable via the documented module-level interface — exactly what a validator exists to catch. Related noise heuristic (r2): "Mixed interface style" warning fires whenever module functions coexist with *any* class, relevant or not.
5. **Placement (minor, r1):** production-grade validation logic lives in a non-package directory with no `__init__.py`, imported via path hacks; belongs in the main package where lint/type coverage applies; living under `tests/` risks the context ("only feed first-party AI output") being forgotten.
6. Nit (r1): errors/warnings as raw strings — codes would enable downstream filtering.

No injection risk in `check_script` itself: module path passed as argv, not interpolated (r1, positive).

### tests/validators/backtest_validator.py

1. **tests/validators/backtest_validator.py:68-73,:102-113 — severity split (r3 major / r1 minor) — mixed naive/aware timestamps crash validation with TypeError.** `_parse_time` returns naive datetimes for strings lacking tz and aware ones for `Z`/offset strings; a single trade mixing formats makes the `<` comparison raise uncaught TypeError, aborting `validate()` wholesale instead of appending an error for that trade — the validator dies on precisely the malformed input it exists to catch. Normalize tz before comparing (mirror `_to_utc_datetime` patterns used elsewhere in the suite). Related (r3 minor): unparseable timestamps silently `continue` — dropped from ordering validation without even a warning; garbled importer timestamps invisible.
2. **tests/validators/backtest_validator.py:46,:86-92 — major (r1/r2) — `total_trades` fallback silently coerces bad values to 0 with two conflated sources.**
   - r1 (**major**): non-numeric `stats["total_trades"]` (`None`, `"12abc"`) → `_as_int` returns 0 → misleading "Trade count below expectation: 0 < N" instead of "invalid total_trades"; default branch counts `len(result["trades"])` while stats may report a different canonical count — two sources conflated without documented precedence.
   - r2 (**major**) adds eager-default semantics: `stats.get("total_trades", result.get("trades", []))` evaluates the default every call (harmless) but if the stat is missing AND trades list absent, `_as_int([])` → 0 degrades to "0 < min_trades" instead of the correct "Missing or invalid 'trades' list" raised at :75 — structurally invalid results report inconsistently across two code paths. With default `min_trades=0` (:21) garbage trade counts pass unchallenged (r2 minor framing of the same `_as_int` point).
   **[SETTLED by verification]** Code read confirms `_as_int` swallows TypeError/ValueError → return 0 (:85-92) and both r1's coercion path and r2's missing-stat path are real; one nuance: the "Missing or invalid 'trades' list" error at :74-75 fires *independently* of the total_trades fallback, so such results still get an error — r2's "instead of" framing is overstated, though the inconsistent-message point stands. Sharper verified fact supporting **major**: a *string* `"NaN"` sharpe_ratio parses via `_as_float` to float NaN, then fails both comparisons (`nan < min` False, `nan > max` False) producing zero errors/warnings — non-numeric stat values genuinely bypass validation entirely, exactly the reference report's T3-3 claim. Final severity **major** (r1/r2 + reference weight; r1's own minor rating on the adjacent numeric-scan gap is thereby superseded for the sharpe/total_trades paths).*
3. **tests/validators/backtest_validator.py:40-44 — minor (r1) — NaN/Inf scan covers only int/float.** String `"NaN"` or `Decimal('NaN')` stat values escape the finiteness check; the JSON round-trip hazard re-enters via stringly-typed stats. (Positive, r3: bool-safe NaN/Inf rejection at :40-44 is a genuine data-integrity guard.)
4. **tests/validators/backtest_validator.py:52-61 — positive (r3) with minor nit — asymmetric sharpe handling sound** (below-range = error; suspiciously-above-range = warning citing lookahead bias); non-finite sharpe collapses into "Missing or invalid" (:53-54), conflating NaN with absence.
5. **Scoring divergence — minor (r2):** score computed independently of `passed` (`len(errors) == 0`) — divergence undocumented; informational. Formula duplicated with code_validator (shared scorer candidate, r1 nit).

Smells: Shotgun Surgery — `required_stats_keys` defaults hardcode runner.stats key names also asserted in tests/test_stats.py, two places per schema change (r1); plain-dict return while sibling returns typed ValidationResult (inconsistent contracts, r1); `_parse_time` duplicates the `replace("Z","+00:00")` normalization found ≥3 places repo-wide (r3). Security: none beyond the robustness crash — no eval/exec/subprocess, pure data inspection (r1/r2/r3).

---

## Reference-report cross-check (gpt-5.6-luna 2026-08-23, §9 "tests — chunk 3")

The reference report's chunk-3 section covers the 27-file subset overlapping this chunk (`tests/validation/**`, `tests/validators/**`, `tests/test_yfinance_downloader.py`); its treatment of the remaining root-level files sits in other chunks of that report and was not consulted, per scope rules. Disagreements and agreements against the panel:

| Ref ID | Claim | Panel verdict |
|---|---|---|
| T3-1 | `code_validator.py:97-121` executes candidate code without sandbox — **major** | **Agree** (unanimous panel major; panel adds the denylist-bypass dimension ref understates) |
| T3-2 | `backtest_validator.py:68-72` malformed timestamps silently pass — **major** | **Agree on severity, mechanism sharpened**: verified the mixed-naivety case crashes with TypeError rather than passing silently; the silent-skip half (unparseable → `continue`) is r3's minor and also real |
| T3-3 | `backtest_validator.py:40-45` non-numeric cagr/drawdown/win-rate/PnL bypass finite checks — **major** | **Agree — ref wins over r1's minor**: verification found the concrete bypass (string `"NaN"` sharpe → float NaN → both range comparisons False → zero errors) |
| T3-4 | `test_cross_platform_metrics.py` depends on regeneration scripts + external artifacts — **major** | **Agree** (unanimous panel major at the same lines) |
| t3-13 | QC importer import-time reference-dir capture — **minor** | **Disagree — panel major stands** (verified structural import-time freeze affecting all 8 golden tests) |
| t3-14 | QC brittle golden snapshot — **minor** | **Panel rates major** (r2; ~40 assertions invalidated by any legitimate re-export, no regeneration story) — unresolved 1-vs-1 among raters, kept high |
| t3-1..t3-7, t3-9..t3-12 | trade_statistics aggregate-vs-exit PnL gap; trade_comparison OR-tolerance false positives near zero; matcher reordered-duplicate + naive-as-UTC gaps; paper_readiness disabled-checks-still-READY; markdown weak/duplicated assertions; CLI subprocess timeout; yfinance forwarding/tz/ordering/empty gaps; MT5 direction semantics + suite duplication | **All consistent with panel findings** at matching locations — independently corroborating, no corrections needed |
| t3-8 | `test_cli.py:144-190` generated workbook never closed | **Reference-only claim — verified, downgraded**: literally true (no `wb.close()` before save) but openpyxl holds no OS handles until `save`; adopted as a nit, not elevated to panel findings |

Notable blind-spot asymmetry: the reference has **no counterpart** for the panel's highest-severity item (trade_engine parity test, critical), nor for the viewer stack-walk doubles, walkforward mock-only coverage, delta_report dead helper, or paper_readiness double-construction — the panel materially exceeds the reference on those files.

---

## Coverage statement

**Coverage: 40/40 tracked files in chunk C accounted for** — every file below has findings recorded above or an explicit scanned-clean record:

- Root-level (15): test_signal_import, test_signal_processor_registry, test_signal_replay_adapter, test_stats, test_strategy_ir_builder, test_strategy_ir_codegen_vectorbt, test_strategy_ir_normalizer, test_strategy_ir_routing, test_strategy_meta_adapters, test_strategy_type_detection, test_timeframe_normalization, test_trade_engine, test_viewer_features, test_walkforward, test_yfinance_downloader
- tests/validation (23): __init__ (empty marker), test_cli, test_cross_platform_metrics, test_delta_report, test_fixed_risk_model, test_golden_files, test_importers, test_markdown_report, test_mt5_html_importer, test_mt5_xlsx_importer, test_paper_readiness, test_percent_risk_model, test_quantconnect_result_importer, test_regression, test_report_structured, test_risk_comparison, test_risk_sizing, test_stats_comparison, test_trade_anomalies, test_trade_comparison, test_trade_matcher, test_trade_statistics, test_validation_reference
- tests/validators (2): code_validator, backtest_validator

**Merge/verification accounting:** 142 deduplicated findings merged from the three blind reviews (agreeing findings deduplicated with all original citations preserved). Verification budget used: 8 of 8 permitted spot-checks — (1) trade_engine parity citations/severity, (2) yfinance mock call-contract vs production + installed library signature, (3) delta_report dead-helper/unimported-Any, (4) QC importer import-time env capture, (5) paper_readiness double-construction readings, (6) backtest_validator numeric-stat bypass paths, (7) reference-only CLI workbook-cleanup claim, (8) meta_adapters fill-price derivation behind the 142.8571 oracle. Settlements are logged inline as **[SETTLED…]** notes; two severity splits were settled without code reads on report-internal evidence (percent-risk TP naming corroborated by the fixed-risk sibling; QC goldens carried by r2's regeneration-story analysis) and are marked as such. Counting conventions: numbered findings only (aggregated smell paragraphs excluded); nits counted as minor; pure cross-reference items not double-counted; verified splits take the settled severity, unverified splits take the reviewer majority, true ties take the higher rating (viewer split-plan overlap, QC brittle-goldens).

## Finding counts by severity (merged)

- **critical: 1**
- **major: 15**
- **minor: 126**
- **total: 142**


