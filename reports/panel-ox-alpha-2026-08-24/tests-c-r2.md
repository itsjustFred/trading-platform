# Code review — area `tests-c` (chunk C of test suite), reviewer r2

Blind, independent review of tests/ chunk C. Findings cite `file:line` with severity.
Status: COMPLETE — all 41 tracked files reviewed and accounted for below.

---

## Group 1 — small root-level test files

### `tests/test_timeframe_normalization.py`
Scanned. Nothing found. (14 lines; covers alias mapping, canonical passthrough, unknown/empty passthrough.)

### `tests/test_yfinance_downloader.py`
- **major** — `tests/test_yfinance_downloader.py:27` — the monkeypatch stub `lambda **kwargs: payload` accepts **no positional argument**, so it only works if production calls `yf.download(**kwargs)` with the ticker passed as a keyword. yfinance's real signature is `download(ticker, start=..., ...)` — ticker is positional-first. Either (a) the downloader genuinely calls `yf.download(symbol=...)`, in which case the test pins a call shape that would crash against the real library, or (b) the downloader passes the ticker positionally and this stub would raise if signatures were checked. Because the stub also discards every kwarg, nothing about request formation (interval, start/end, auto_adjust) is verified — the mock makes part of the unit under test vacuous.
- **minor** — `tests/test_yfinance_downloader.py:38-41` — assertions cover row count and column order but never values: no check that open/high/low/close/volume survived the MultiIndex→flat transformation unchanged (e.g. `stored["close"].iloc[1] == 2.2`). A column-misalignment bug in the flatten step would pass.

### `tests/test_signal_import.py`
Scanned. Nothing found. DST-sensitive hour assertion (`hour == 14`, line 30) is correct for the 2026-02-23 winter date used; a comment documents it.

### `tests/validation/test_importers.py`
- **minor** — `tests/validation/test_importers.py:50-51` — expected timestamps `"2024-01-03T06:00:00Z"` / `"2024-01-03T14:00:00Z"` are bare magic strings with no derivation comment for the Europe/Athens (+02:00) offset being applied. Values are arithmetically consistent (08:00→16:00 local), but golden-value tests without derivation make off-by-one-hour regressions indistinguishable from intended fixes.

### `tests/validation/test_golden_files.py`
- **minor** — `tests/validation/test_golden_files.py:16-17` — `_list_golden_files()` silently returns `[]` when the goldens directory does not exist. The `assert files` guards in three test classes turn a missing-directory misconfiguration into "No golden files found" instead of naming the absent path; only `test_at_least_three_golden_files` includes the path in its message. Cosmetic diagnostics issue, not a false-pass (guards do fail).
Otherwise scanned, nothing found.

## Group 2 — signal processor registry, replay adapter, strategy IR

### `tests/test_signal_processor_registry.py`
- **major** — `tests/test_signal_processor_registry.py:36` — asserts `get_signal_processor("stockkit", {})` returns a `RamJetSignalProcessor`. Verified intentional against `signal_bots/registry.py:92` (`"stockkit": _build_ramjet`), so the test currently matches production. However it *documents* that two publicly listed options ("StockKit" and "RamJet", distinct labels/descriptions at registry.py:62-72) return byte-identical processors under a generic test name that hides the aliasing. If the alias is deliberate, the assertion belongs in an explicitly named alias test; as written, a future fix that gives StockKit distinct behavior will look like the *test* breaking production rather than production diverging from marketing. Judgement call: misleading-by-naming, not wrong.
- **minor** — `tests/test_signal_processor_registry.py:46-58` — variant-difference coverage is one-sided: asserts long-short variant gets a stop above entry (correct for shorts) but never asserts the long-only variant still produces exit-only metadata *and* no position-side change beyond `exit_only is True`. Acceptable; noted for completeness.
Otherwise scanned, nothing found.

### `tests/test_signal_replay_adapter.py`
- **minor** — `tests/test_signal_replay_adapter.py:52-53` — `backtest_end="2026-01-01"` equals the frame's first day while data spans four same-day bars; the test silently depends on end-date inclusivity semantics of `SignalReplayAdapter.run` without asserting or documenting them. If end handling ever flips to exclusive, the failure mode (zero trades) will be misread as a fill/trailing bug.
- **minor** — `tests/test_signal_replay_adapter.py:60` — `trailing_stop_updates >= 1` verifies update counting but not ratcheting: a regression that updates the stop every bar to a non-monotonic value would pass. Asserting the final stop level relative to entry/close would close the gap.
Otherwise scanned, nothing found.

### `tests/test_strategy_ir_builder.py`
Scanned. Nothing found. Structural assertions plus `confidence == 1.0` meaningfully pin full-confidence extraction; fallback-to-source-token test checks both indicator names.

### `tests/test_strategy_ir_routing.py`
Scanned. Nothing found. All five routing scenarios assert distinct observable fields including reason substrings; no tautologies detected.

### `tests/test_strategy_ir_normalizer.py`
Scanned. Nothing found. Covers markdown-title fallback, key=value string parsing, mixed tag-list merging, and TypeError with message regex for invalid parameter entries.

## Group 3 — validators (test-support production code), codegen test

### `tests/validators/backtest_validator.py`
- **major** — `tests/validators/backtest_validator.py:46` — `total_trades = self._as_int(stats.get("total_trades", result.get("trades", [])))`. The `dict.get(key, default)` default is evaluated **eagerly**, so `result.get("trades", [])` runs on every call; harmless here, but the real defect is semantic: if `stats["total_trades"]` is missing AND `result["trades"]` is absent, `_as_int([])` returns 0 and the check degrades to comparing 0 against `min_trades` — a structurally invalid result (no trades list at all) reports a clean "0 < min_trades" trade-count error instead of the "Missing or invalid 'trades' list" error that line 75 correctly raises. Two different code paths report the same class of malformation inconsistently.
- **minor** — `tests/validators/backtest_validator.py:46,86-92` — `_as_int` swallows `TypeError`/`ValueError` and returns 0, so `stats["total_trades"] = None` or `"many"` silently becomes 0 rather than an explicit invalid-stat error; combined with `min_trades=0` (the default, line 21) this means garbage trade counts pass validation unchallenged in the default configuration.
- **minor** — `tests/validators/backtest_validator.py:77` — score formula `1.0 - 0.2*errors - 0.05*warnings` clamps at 0 but `passed` is computed independently (`len(errors) == 0`); a result can have `score == 0.8` with `passed=False` and vice versa is impossible but the score/passed divergence is undocumented for consumers. Informational.
- Note: this file is production-like helper code living under `tests/`; it has **no direct unit tests of its own** in chunk C (no `tests/test_backtest_validator.py` in my list). Its only exercise is indirect.

### `tests/validators/code_validator.py`
- **major** — `tests/validators/code_validator.py:20-22,48-60` — the security scan blocks only bare-name calls to `eval`/`exec` and attribute call `os.system`. It is trivially bypassed by `builtins.eval(...)`, `getattr(builtins, 'eval')(...)`, `import os; os.popen(...)`, `__import__('os').system(...)`, or aliasing (`e = eval`). As a security control for AI-generated strategy code this is a speed bump, not a gate — and since it lives under `tests/validators/`, its verdicts may be consumed as if authoritative. The subprocess import check (line 116-122) partially compensates because malicious module-level code would execute there — inside a **subprocess with no sandboxing beyond `-I`**, meaning arbitrary generated code actually *runs* during validation. That is the real exposure: `validate()` executes untrusted code by design (isolated mode blocks user site-packages/env vars but not filesystem/network access).
- **minor** — `tests/validators/code_validator.py:97-100` — writes the untrusted code to a temp dir and imports it; no resource concern thanks to `TemporaryDirectory`, good. But the 8-second timeout at line 120 is short for cold interpreter starts on CI; a slow import (pandas) will flakily produce "Subprocess import timed out" errors that look like strategy defects.
- **minor** — `tests/validators/code_validator.py:82-83` — warning "Mixed interface style" fires whenever both module functions and any class methods exist — including classes that are irrelevant helpers; noisy heuristic, informational.
- Same note as above: no dedicated unit-test file for `code_validator` exists in my area list.

### `tests/test_strategy_ir_codegen_vectorbt.py`
- **major** — `tests/test_strategy_ir_codegen_vectorbt.py:62-63,105-106,140-141` — all three execution tests `exec()` the generated strategy source directly into a dict namespace. Generated code is repo-controlled so this is acceptable practice, but note there is no syntax/compile assertion separate from exec, and no assertion that generated code avoids forbidden constructs — i.e., the codegen output is never round-tripped through `AICodeValidator`, despite that validator existing one directory over. Coverage gap, judgement call.
- **minor** — `tests/test_strategy_ir_codegen_vectorbt.py:76-78` — `assert len(output) in {3, 4}` then conditionally checks element 4. Accepting either shape means a regression that drops the fourth element (e.g., stop metadata) passes silently. The test should pin the exact arity now that the generator is deterministic.
- Scanned otherwise, nothing found. Determinism test (lines 49-55) is genuinely meaningful.

## Group 4 — stats & trade engine

### `tests/test_stats.py`
- **minor** — `tests/test_stats.py:87` — `assert stats["max_drawdown_pct"] != 0` with comment "QuantStats returns negative". Asserting merely non-zero is weak: it cannot distinguish −9% from −0.0001% or from a sign flip to +9%... wait, +9% would still be `!= 0` and would PASS. A sign regression in max-drawdown (reporting gain instead of loss) passes this assertion. Should assert `< 0` (or an exact expected value).
- **minor** — `tests/test_stats.py:88-93` — `"sharpe_ratio" in stats` etc. assert key presence only, never values, for the entire QuantStats path (sharpe/sortino/calmar/cagr/total_return/max_drawdown_duration). Key-presence assertions give false confidence that metric computation works when only dict population is proven. Combined with line 87 this makes `test_with_equity_curve` nearly vacuous beyond smoke value.
- Positive note: lines 95-211 (p-value, CAGR elapsed-time, fractional-day annualization, intraday Sharpe daily-aggregation) are excellent — they encode exact expected formulas (`math.isclose` against hand-computed values) rather than echoing implementation.
- Scanned otherwise, nothing found.

### `tests/test_trade_engine.py`
- **critical** — `tests/test_trade_engine.py:324-368` — `test_example_sma_native_vs_vectorbt_trade_count_similarity` compares trade counts between engines with `tolerance = max(2, int(vectorbt_count * 0.3))`: two supposedly equivalent backtest implementations are allowed to disagree about **30% of their trades plus up to 2 absolute** before failing. For an engine whose selling point is deterministic replication between native and vectorbt adapters, this tolerance is wide enough to hide systematic fill-logic divergences (e.g., same-bar stop ordering differences producing systematically fewer/more trades). Additionally:
  - line 327/332/343: three independent skip conditions (missing strategy file, no parquet data, no coverage window) mean this test silently no-ops on any fresh checkout without downloaded data — the only cross-engine consistency check in the file is effectively optional.
  - line 363-365: `if vectorbt_count == 0 and native_count == 0: assert True; return` — an explicit tautology branch that skips comparison when both produce zero trades, i.e. exactly the scenario where both engines being broken identically would go unnoticed.
- **minor** — `tests/test_trade_engine.py:367` — tolerance anchored to `vectorbt_count` alone; if vectorbt undercounts toward 0 the tolerance shrinks toward `max(2, ...)` while native stays wrong — asymmetric anchoring makes failures depend on which engine drifts.
- Positive notes: lines 106-321 are high-quality execution-model tests — next-bar-open fill price asserted against synthetic data values (line 133), stop-from-bar-low fill (line 191), risk sizing PnL ≈ −1000 (line 195), SL-price alias equivalence (198-234), fill-anchored stop distance (237-279), and same-bar TP-priority override (282-321, asserting both exit reason and meta echo).

## Group 5 — `tests/test_viewer_features.py` (4790 lines)

### Bugs / correctness risks
- **major** — `tests/test_viewer_features.py:1258-1268,1642-1659,1698-1712` — three separate `threading.Thread` test doubles decide whether to intercept a thread by walking `traceback.extract_stack()` looking for frames whose filename ends with `viewer/core.py` and function name ∈ {`_start_data_task`, `_start_dukascopy_task`, `_start_jforex_task`}. This couples the test doubles to production *stack layout* by string match. If those functions are renamed/moved/inlined, interception silently stops: non-intercepted branches fall through to `real_thread(*args, **kwargs)`, so the "coalescing" assertions (`starts == 1`) would still be evaluated against genuinely spawned worker threads — i.e., the tests can degrade into launching the real download subprocess machinery (network access from unit tests) rather than failing loudly. A sentinel injected via parameter or patching the starter function directly would be robust.
- **minor** — `tests/test_viewer_features.py:2811-2818,2888-2895,3004-3010,3151-3157` — four poll-loops of the shape `for _ in range(50): ... time.sleep(0.05|0.1)` followed by an unconditional `assert task["status"] == "completed"`. On a slow CI box the loop can exhaust (~2.5–5 s budget) while the background thread is still running, producing an assertion failure that looks like a product bug ("status is 'running'") instead of a timeout. A final `task["status"] == "completed", f"timed out, last={task}"` message or event-based wait would remove the ambiguity. Bounded sleeps are at least present (no infinite waits).
- **minor** — `tests/test_viewer_features.py:1416-1419` — `failing_starter` does `assert release.wait(timeout=2)` inside a worker thread; if the wait times out the raised `AssertionError` escapes `launch()`'s `except RuntimeError` and kills the thread unobserved, after which the main-thread singleflight call at line 1433 may block or misbehave depending on internal locking. Low probability (2 s is generous) but the failure mode is a hang, not a clean red.
- **minor** — `tests/test_viewer_features.py:51-52` — the `viewer_client` fixture clears module-global `DATA_TASKS` / `_COALESCED_CLEANUP_PENDING` on entry but relies on monkeypatch restore otherwise; several tests then mutate these globals directly (e.g. 1841, 1912, 1973, 2029, 2065). Correct under pytest's serial execution, but any move to pytest-xdist or test ordering changes will produce cross-test contamination with confusing symptoms.
- **minor** — `tests/test_viewer_features.py:3416-3417` — p-value asserted only as `0.0 <= p <= 1.0` and `< 0.05`. The `< 0.05` half is meaningful for this deterministic curve; the range half is a near-tautology (a broken p-value of 0.999 passes the first assertion). Same pattern at 4190. Acceptable as smoke; noting the weak half.

### Baseline smells
- **Duplicated Code** — `_equity_curve_from_returns` re-implemented at `tests/test_viewer_features.py:3369-3386` vs `tests/test_stats.py:28-36` (same logic, different signatures/defaults); the StockKit semicolon CSV fixture text is copy-pasted verbatim in `tests/test_signal_import.py:10-19`, `tests/test_viewer_features.py:3174-3183` and `3220-3228`.
- **Divergent Change / God file** — one 4790-line module mixes at least eight unrelated concerns: dukascopy genesis, data catalog views, download-task coalescing/cleanup lifecycle, task manager controls, backtest progress streaming, optimizations, portfolios, CAPM/p-value endpoints, agent LLM convert, log endpoints. Any change to any subsystem edits this file; split per endpoint family.
- **Repeated Switches (fixture shape)** — the `_NoStartThread` double is redefined inline at least five times (1259, 1310, 1349, 1469, 1526, 1598, 1748, 1793) with slightly different behaviors instead of one parameterized factory helper.
- **Data Clumps** — the portfolio JSON fixtures repeat `{"component_id","strategy_name","symbol","timeframe","weight","risk_config","settings","backtest_id"}` blocks at 4259-4271, 4336-4346, 4379-4389, 4475-4485; a builder would cut ~60 lines and drift risk.

### Positive
Coverage of coalescing edge cases (aliases 1342-1375, irrelevant options 1519-1552, finished-task non-coalescing 1462-1501, cleanup-marker recovery 1832-2090) is unusually thorough and asserts exact payloads, including negative cases. Keep.

## Group 6 — validation suite: stats comparison, reference, cross-platform metrics

### `tests/validation/test_stats_comparison.py`
Scanned. Nothing found. Identical-stats case asserts the full delta-field set (line 37-48) so a silently dropped field would fail; tolerance and inf-profit-factor branches both exercised with explicit expected/actual values.

### `tests/validation/test_validation_reference.py`
- **minor** — `tests/validation/test_validation_reference.py:79` — `with pytest.raises(ValueError): _make_minimal_valid_reference(trades=[])` does not match on message; any unrelated `ValueError` from pydantic field coercion (e.g. bad trade shape) would satisfy the assertion. Same pattern at lines 65, 73. Low risk given tight construction, but message-scoped matches (`match=`) would prevent false positives.
Otherwise scanned, nothing found.

### `tests/validation/test_cross_platform_metrics.py`
- **major** — `tests/validation/test_cross_platform_metrics.py:16-48,205-219` — the primary matrix test reads real repo artifacts (`strategy-comparisons/`, `reports/`, `data/backtests/*.json`) with **no skip guard** for their absence. Unlike `test_trade_engine.py`, which at least skips when data is missing, these tests will hard-fail (KeyError/IndexError/FileNotFoundError) on a fresh checkout lacking the committed comparison artifacts, or worse, pass against *stale* artifacts while asserting exact historical values (`trade_count == 512`, `sharpe_ratio == 0.544687`). The coupling to committed binary-ish fixtures is intentional parity-pinning, but there is no guard distinguishing "artifact missing" from "parity broke".
- **minor** — `tests/validation/test_cross_platform_metrics.py:30-31,42,128` — exact golden values (`0.4492`, `512`, `"2018-01-16T14:45:00Z"`) asserted without derivation comments or tolerance beyond `approx` default; a legitimate upstream data refresh requires editing tests in lockstep, and the reason for each constant is not recorded.
- Positive note: negative-path coverage is good — missing platform artifact (51-58), malformed attribution stats fall back to base fields (169-202), path-containment check inside project root (46-48).

## Group 7 — MT5 importers

### `tests/validation/test_mt5_html_importer.py`
- **minor** — `tests/validation/test_mt5_html_importer.py:50` — `assert len(ref.trades) >= 5` is a floor, not the exact count; combined with `test_total_trades_matches_trade_count` (line 161) this tolerates a parser regression that *drops* trades from the fixture as long as ≥5 survive and internal consistency holds. `tests/validation/test_importers.py:39` pins exactly 5 for the same fixture, so the weaker bound here is redundant-but-safe; still, exact counts belong in the dedicated importer test.
- **minor** — `tests/validation/test_mt5_html_importer.py:57-60` — `first.entry_time.startswith("2")` asserts timestamps begin with the character "2" — a decade-boundary-bound placeholder that will silently mislead when dates reach 3000 and reads oddly today; comparing against an ISO regex or exact fixture timestamp is sturdier. Same pattern at `test_mt5_xlsx_importer.py:67,70`.
- **minor** — `tests/validation/test_mt5_html_importer.py:6` — dead import (`from typing import Any`, unused); also `Trade` imported at line 10 but never referenced directly.
Otherwise scanned, nothing found. Helper unit tests (176-204) are direct and message-exact.

### `tests/validation/test_mt5_xlsx_importer.py`
- **minor** — `tests/validation/test_mt5_xlsx_importer.py:188-197` — `test_entry_exit_direction_consistency` contains an if/else whose branches are both `pass` with comments ("basic sanity", "SHORT") — dead conditional code asserting nothing in either branch; only the final quantity equality does any work. Copy-paste residue; delete the if/else or assert something direction-specific (e.g. exit price vs entry price plausibility per side).
- **minor** — `tests/validation/test_mt5_xlsx_importer.py:15-24` — `_eurusd_path()` etc. use repo-relative CWD-dependent paths (`Path("strategy-comparisons/...")`) instead of anchoring to `Path(__file__)`; running pytest from any other directory breaks these tests. Inconsistent with `test_golden_files.py`/`test_importers.py`, which anchor correctly.
- **minor** — `tests/validation/test_mt5_xlsx_importer.py:6` — same dead `Any` import; `Trade` also unused at line 10.
Otherwise scanned, nothing found. Timezone conversion tests (250-264) correctly derive Athens→UTC arithmetic with a comment.

## Group 8 — trade matcher & markdown report

### `tests/validation/test_trade_matcher.py`
Scanned. Nothing found. Excellent boundary coverage: temporal tolerance both directions (103-133), symbol/direction mismatch semantics, all empty-input combinations (176-195), duplicate-timestamp pairing order with explicit ID-pair assertions (198-234), and UTC key normalization including offset equivalence (237-276).

### `tests/validation/test_markdown_report.py`
- **minor** — `tests/validation/test_markdown_report.py:125` — `assert "Overall Passed**: Yes" in md or "Overall Passed**: Yes" in md` — both disjuncts are the *identical string*; the `or` is dead code. Same tautological-or at line 223 (`"Overall Passed**: No" in md or "Overall Passed**: No" in md`). Harmless today but suggests a copy-paste where one branch was meant to check an alternate format; as written it can never catch a formatting change.
- **minor** — `tests/validation/test_markdown_report.py:166,168,247,249,290` — direct attribute mutation (`ref_trade.exits[0].pnl = 100.0`) on pydantic models; if `Trade`/`TradeExit` ever become frozen/validated-on-mutation these tests break en masse. Works today; fragility note only.
- **minor** — `tests/validation/test_markdown_report.py:160,162` — `"entry_price" in md.lower() or "Entry" in md` — mixed-case disjunction is loose but intentional-looking; acceptable.
- **minor** — `tests/validation/test_markdown_report.py:7` — dead import `Any`.
Otherwise scanned, nothing found. The report-object round-trip tests (228-243, 287-302) are a nice touch verifying JSON→model→markdown consistency.

## Group 9 — risk comparison & risk sizing

### `tests/validation/test_risk_comparison.py`
Scanned. Nothing found. Each mismatch class isolates one field (stop_distance/risk_per_trade/position_size) by relaxing the other two tolerances — good isolation discipline; missing vs extra severity asymmetry is explicitly pinned (162-199); summary counts cross-checked against per-trade severities (202-229).

### `tests/validation/test_risk_sizing.py`
Scanned. Nothing found. Strong file: hand-computed expectations with derivation comments (risk_amount=100 = 1% of 10k at line 154, ATR fallback 0.005×3.0=0.015 at line 195), direction-validity rejection (200-207), zero-balance error message match (210-214), exposure/position caps zeroing size (217-235), pip-value inverse scaling property test (238-246), and LONG/SHORT fallback-stop side assertions (330-345).

## Group 10 — validation CLI

### `tests/validation/test_cli.py`
- **minor** — `tests/validation/test_cli.py:50-57` — every test spawns a real subprocess (`python -m validation.cli`) with no `timeout=` argument on `subprocess.run`. If the CLI ever blocks (waiting on stdin, network retry loop) the suite hangs indefinitely; the fixed-risk checklist item "subprocess without timeout" applies verbatim. The rest of the file is a genuinely good end-to-end contract suite (exit codes 0/1/2 pinned, tolerance flags both directions).
- **minor** — `tests/validation/test_cli.py:250` — three-way `or` over stderr substrings ("not found"/"failed to load reference"/"no such file or directory") is so broad it passes for nearly any failure text mentioning files; acceptable robustness choice but weakens the contract — the exit code 2 assertion carries all the weight.
- **minor** — `tests/validation/test_cli.py:109` — again a tautological-looking or: `"Overall Passed**:** Yes" in ... or "Overall Passed**: Yes" in ...` — here the two strings *differ* (colon placement), so this one is legitimate dual-format tolerance; flagged only to contrast with test_markdown_report.py:125 where they are identical.
Otherwise scanned, nothing found.

## Group 11 — fixed risk model

### `tests/validation/test_fixed_risk_model.py`
- **minor** — `tests/validation/test_fixed_risk_model.py:158` — test named `test_round_lots_rounds_up` actually asserts **nearest-multiple rounding downward** (raw 33.33 → 30, comment on line 161 says "rounds to 30 (nearest)"). Rounding *up* would be 40. The name misdescribes verified behavior — and the companion test at lines 86-97 asserts floor-to-budget semantics (260 → 259). Together the two tests suggest real behavior is "floor/nearest, never exceeding budget"; the misleading name will steer future maintainers wrong when changing rounding policy. Rename to reflect actual semantics.
- **minor** — `tests/validation/test_fixed_risk_model.py:87-89` — thinking-out-loud comment left in place ("raw size = 100/0.4 = 250 ... use a value that rounds UP under round() but would breach the risk budget: 100 / (1/2.6) -> 260 lots? Instead pick a..."). Confused arithmetic residue; rewrite.
Otherwise scanned, nothing found. Validation-error tests match exact messages; meta-extraction precedence (direct SL over meta SL, 310-315) is pinned explicitly — good.

## Group 12 — trade statistics, trade comparison, anomalies

### `tests/validation/test_trade_statistics.py`
- **minor** — `tests/validation/test_trade_statistics.py:245-247` — the Sharpe derivation comment computes variance as `sum(diff²)/5` (population) but the code under test may use sample std; the test then sidesteps it by recomputing with `math.sqrt(2680.0)` at line 256 — i.e. expected mirrors whichever convention was observed. The comment's arithmetic (`2680/5`) is asserted to match, so if production switched to n−1 the test *would* fail — fine. But note the same file asserts Sortino downside variance as `(2500+400)/2` (line 278, dividing by 2 = number of downside points). The two conventions coexist by design of the implementation; no bug, just flagging the comments are the only spec.
Otherwise scanned, nothing found. This is a model validation-grade file: hand-derived drawdown sequences with peak/DD traces in comments (170-219), inf/zero edge cases for profit factor and recovery factor explicitly pinned, break-even trades excluded from win/loss counts (403-413).

### `tests/validation/test_trade_comparison.py`
Scanned. Nothing found. Tolerance boundaries tested from both sides for every field (entry/exit time, prices, volume, position_size, pnl, SL/TP); OR-tolerance logic for pnl documented with numeric reasoning in comments (253-291); None-vs-value semantics covered both directions (230-243, 295-308, 326-339).

### `tests/validation/test_trade_anomalies.py`
Scanned. Nothing found. Severity taxonomy per field fully enumerated (error for direction/pnl/duplicates, warning for times/volume/SL/TP, info for extras); duplicate detection on both reference and platform sides; integration tests verify combined anomaly types and that absent comp-results suppress mismatch anomalies (388-403).

## Group 13 — structured report

### `tests/validation/test_report_structured.py`
- **minor** — `tests/validation/test_report_structured.py:354` — `assert "Yes" in md or "No" in md` is a near-tautology: any markdown report containing either word passes regardless of correctness of the verdict. The sibling markdown tests in `test_markdown_report.py` do this properly; here it is a placeholder-strength assertion.
- **minor** — `tests/validation/test_report_structured.py:245` — `trade_summary["errors"] + trade_summary["warnings"] >= 0` can only fail on negative counts — effectively asserting the keys exist and are numeric. Weakest assertion in an otherwise precise file.
- **minor** — `tests/validation/test_report_structured.py:393` — `symbol=" sym"` (leading space) in `test_report_model_directly` looks like a typo for `"sym"`; if the model strips whitespace this test would catch it, but nothing asserts on `d["symbol"]`, so the odd value is unobserved — dead fixture data.
Positive notes: strict-JSON compatibility section (405-439) is exactly the right kind of test — non-finite floats sanitized to null and verified via `allow_nan=False` round-trip; recursive type check at 376-388 guards whole-payload JSON-safety.

## Group 14 — percent risk model

### `tests/validation/test_percent_risk_model.py`
- **minor** — `tests/validation/test_percent_risk_model.py:310-331` — `test_validate_extracts_take_profit_for_risk_reward` never actually calls `model.validate(inp)` on the first constructed input (line 313-319): it builds `inp`, then builds `inp2` with TP pulled from meta manually (`trade.meta.get("take_profit")`) and sizes that. The name promises meta-extraction validation for TP; the body instead demonstrates the *absence* of adapter-level TP extraction, per its own comment (lines 320-322). The first `inp` is dead. Misleading test name + dead fixture; either rename to "rr calculated when tp provided directly" or implement/verify real extraction.
- **minor** — `tests/validation/test_percent_risk_model.py:217` — same naming drift as the fixed-risk suite: `test_round_lots_rounds_down` comment says `round(33.33/10)=round(3.33)=3 -> 30`, i.e. nearest-rounding that happens to round down here; a raw value of 36 would produce 40 under this description ("down" is not guaranteed). Cosmetic but consistent with the misleading-naming pattern in `test_fixed_risk_model.py:158`.
Otherwise scanned, nothing found. Balance/risk-percent validation errors all message-matched; zero-reward RR → None edge case (354-359) pinned; adapter dispatch type errors verified with matched messages (455-461).

## Group 15 — regression suite

### `tests/validation/test_regression.py`
- **minor** — `tests/validation/test_regression.py:87-100` — the "field set" assertion compares `len(result.stats_delta)` against `len([...literal list...])` — i.e. it asserts only the *count* (10) and never that the field names match. If `_compare_stats` started emitting a wrong/renamed field (say `net_profit` → `profit`) plus dropped another, keeping total 10, this passes. The sibling file `test_stats_comparison.py:37-48` does it correctly with a set equality over `{delta.field ...}`; copy that shape here.
- **minor** — `tests/validation/test_regression.py:389-391` — importer test uses CWD-relative fixture path with an existence-based skip (`if not html_path.exists(): pytest.skip`). Two issues: CWD dependence (same as `test_mt5_xlsx_importer.py:15-24`), and skip-on-missing-fixture means the golden-file round-trip through a real importer silently vanishes if fixtures move.
Otherwise scanned, nothing found. Tolerance matrix (absolute-only, relative-only, both, custom trade config — lines 306-381) is thorough and each side of pass/fail is exercised; inf-vs-inf and inf-vs-value profit-factor semantics pinned (460-476).

## Group 16 — strategy meta adapters & walkforward

### `tests/test_strategy_meta_adapters.py`
- **minor** — `tests/test_strategy_meta_adapters.py:180-196` — `test_all_vectorbt_strategies_return_entries_exits_and_indicators_dict` iterates every repo strategy via `iter_strategy_files(STRATEGIES_DIR)` with **no skip guard and no per-strategy isolation**: any broken strategy file anywhere in the repo fails this test, which is arguably the point (a contract sweep), but a strategy that needs params to produce non-empty indicators will fail `isinstance(indicators, dict) and indicators` (line 194) for environment reasons rather than contract reasons. Also accepts `len(output) in {3, 4}` — same loose-arity pattern flagged in `test_strategy_ir_codegen_vectorbt.py:78`.
- **minor** — `tests/test_strategy_meta_adapters.py:104-119` — hard-coded magic expectations `trade.quantity == 142.8571`, `trade.pnl == -142.86`, `risk_target_usd == 1000.0`. The quantity encodes 1000/7 (risk $1000 over a $7 stop distance) — correct arithmetic, but no derivation comment; a sizing-formula change requires reverse-engineering these constants. Add the one-line derivation.
- **minor** — `tests/test_strategy_meta_adapters.py:20,23-24` — `sys.path.insert(0, ...)` at import time plus unused-looking `REPO_ROOT`; `STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR` makes line 183's sweep CWD-independent (good), but the `sys.path` mutation is redundant under normal pytest rootdir config and can shadow modules.
Positive notes: intrabar take-profit resolution test (256-329) asserts exact fill time/price/PnL from a synthetic lower-timeframe replay — genuinely strong execution-semantics coverage; pynecore directional SL/TP promotion (410-448) verifies side-correct price selection including absence of the raw `_long/_short` keys.

### `tests/test_walkforward.py`
- **major** — `tests/test_walkforward.py:51-114,120-205,211-341` — the entire legacy walk-forward surface is tested against `mock_optimize`, a stub returning fixed dicts. Every fold metric (`oos_sharpe`, drawdown), every summary aggregate, and the file-output contents are therefore just "did we copy the mock through" — the *actual* optimization logic is never exercised. This is the definition of excessive mocking making tests vacuous: the suite would stay green if `run_walkforward` stopped computing anything real. The newer strategy-mode test (393-421) with `_FakeEngine` partially redeems the module (it exercises fold construction and engine call ordering for real), but portfolio-mode aggregation remains untested.
- **minor** — `tests/test_walkforward.py:297` — `assert len(result["folds"]) == 1  # Current implementation has single fold` pins a known limitation as if it were a spec; when multi-fold support lands, this test breaks and reads like a regression rather than an upgrade.
- **minor** — `tests/test_walkforward.py:270` — timestamp length check `len(ts_part) == 15` would also accept a malformed-but-15-char token (no format regex); weak but harmless.

## Group 17 — paper readiness & delta report

### `tests/validation/test_paper_readiness.py`
- **minor** — `tests/validation/test_paper_readiness.py:510-525` — `test_empty_inputs` docstring says "→ NOT_READY because required artifacts missing" but the body constructs `ReadinessConfig` with all four `require_*` flags **False** and then asserts `READY`. The docstring contradicts both the fixture and the assertion — one of them is stale. As written the test verifies the permissive-config path only; the default-config "required artifacts missing → NOT_READY" path named in the docstring is never tested. Fix docstring and add the missing default-path case.
- **minor** — `tests/validation/test_paper_readiness.py:147` — `_make_risk_report` evaluates `all(r.passed for r in (results or [_make_risk_result()]))`, constructing a *second* fresh `RiskComparisonResult` just to compute `passed` when results are provided (the `or` short-circuits, so with non-empty `results` it's fine; with empty list `[]` it fabricates a passing result instead of representing zero checks). Passing `results=[]` cannot express "no risk trades" — it silently means "one implicit pass". Edge-case footgun in the helper.
Otherwise scanned, nothing found. Threshold boundary for missing trades (within/above), severity escalation, and the explicit level-calculation triptych (472-508) are all well pinned.

### `tests/validation/test_delta_report.py`
- **minor** — `tests/validation/test_delta_report.py:98-105` — `_make_field_result` uses bare `Any` annotations and defaults (`expected: Any = 1.1000`); more importantly this helper is **dead code** — no test in the file calls it (all tests build real comparator results). Dead test utility.
- **minor** — `tests/validation/test_delta_report.py:7` — imports `pytest` unused? (checked: no `pytest.` reference in file body) — dead import.
Otherwise scanned, nothing found. Severity/passed logic matrix (316-361), explanation formatting per field type (423-454), and all four anomaly-type conversions (457-520) are precisely asserted.

## Group 18 — QuantConnect result importer

### `tests/validation/test_quantconnect_result_importer.py`
This is the strongest file in the chunk: synthetic-payload fail-closed tests all match exact error messages with anchored regexes (202-307, 488-526, 639-719); fee-model parameter validation is parametrized over 8 invalid inputs including NaN/Infinity/currency-case (529-552); Decimal-precision preservation is verified end-to-end against a hand-formatted source file (555-588); and CSV-vs-JSON mismatch handling covers both kinds plus missing-columns and escaped-tab injection rejection (722-871). Remaining findings:
- **major** — `tests/validation/test_quantconnect_result_importer.py:302-311` — the module-scoped `golden_reference` fixture loads a 597 KB real QC export (`Sleepy Tan Coyote.json`) with a bare `assert RESULT_JSON.is_file()` guard. Unlike the CWD-relative tests this one *does* check existence — but the failure mode when the external reference export is absent (e.g. fixture pruned for repo size) is an **error**, not a skip: six tests hard-fail with an AssertionError naming the path. Given the file's own README documents it as an external artifact, `pytest.skip` (as used in `test_regression.py:389`) would be the consistent choice; erroring is defensible for parity pinning but inconsistent with the rest of the suite. Judgement call, flagged.
- **major** — `tests/validation/test_quantconnect_result_importer.py:328-346,354-376,591-622` — golden assertions pin exact counts from the real export (264 orders, 84 clusters, 236 trades, 160/76 win/loss split, size-distribution dict `{1:17, 2:16, ...}`, fee remainder 81.97, 5471 equity points). These are excellent regression pins but pure goldens with zero derivation; any legitimate re-export of the reference (QC platform re-run) invalidates ~40 assertions at once across four test functions, and nothing in-file explains how to regenerate them (the regenerator scripts exist for MT5 cases but no QC equivalent is referenced here).
- **minor** — `tests/validation/test_quantconnect_result_importer.py:902` — `json.dumps(reference.raw_metadata)` called with result discarded, as a serialization smoke check; fine idiom but deserves an assertion or comment (`assert isinstance(..., str)`), otherwise reads like debug residue.
- **minor** — `tests/validation/test_quantconnect_result_importer.py:412` — `observed_remainder = Decimal("2698.00") - Decimal("2616.030")` re-states numbers already asserted at lines 372-373 instead of deriving from `fees[...]`; if the golden changes, line 412 must be edited in lockstep manually — a classic duplicated-golden hazard inside one test.

## Group 19 — coverage accounting & cross-cutting summary

All 41 tracked files accounted for above. Cross-cutting observations:

1. **CWD-dependent paths** appear in two files (`test_mt5_xlsx_importer.py:15-24`, `test_regression.py:389`) plus the root-level `tests/test_signal_import.py` is safe; siblings anchor via `Path(__file__)` — Shotgun-Surgery-prone inconsistency; a shared `tests/validation/paths.py` helper would fix it once.
2. **Fixture duplication**: `_make_trade`/`_make_reference`/`_make_matched` helpers are copy-pasted near-identically across at least eight validation files (matcher, comparison, anomalies, delta_report, report_structured, markdown_report, risk_comparison, paper_readiness). A `conftest.py` under `tests/validation/` would remove ~300 duplicated lines and the drift already visible (e.g. `_make_field_result` dead in delta_report, `Any` defaults in several copies). Duplicated Code / Data Clumps.
3. **Dead imports** recur (`typing.Any` in three files, unused `Trade`, unused `pytest` in test_delta_report) — mechanical cleanup.
4. **Security posture of test-support validators** (`tests/validators/code_validator.py`) — see Group 3; executing untrusted generated code with only `-I` isolation is the most significant security-relevant item in the area.
5. No flaky sleep patterns beyond bounded poll-loops noted in Group 5; no mutable default arguments found anywhere in chunk C; tz handling in tests consistently UTC-aware (positive).

## Per-file accounting (all 41 tracked files)

| File | Findings |
|---|---|
| tests/test_signal_import.py | none |
| tests/test_signal_processor_registry.py | 1 major, 1 minor |
| tests/test_signal_replay_adapter.py | 2 minor |
| tests/test_stats.py | 2 minor |
| tests/test_strategy_ir_builder.py | none |
| tests/test_strategy_ir_codegen_vectorbt.py | 1 major, 1 minor |
| tests/test_strategy_ir_normalizer.py | none |
| tests/test_strategy_ir_routing.py | none |
| tests/test_strategy_meta_adapters.py | 3 minor |
| tests/test_strategy_type_detection.py | none (scanned: meta-field detection, class-pattern detection, market-data default, and CLI rejection of signal-processor strategies all assert concrete outcomes with matched error messages) |
| tests/test_timeframe_normalization.py | none |
| tests/test_trade_engine.py | 1 critical, 1 minor |
| tests/test_viewer_features.py | 1 major, 5 minor + 4 smells |
| tests/test_walkforward.py | 1 major, 2 minor |
| tests/test_yfinance_downloader.py | 1 major, 1 minor |
| tests/validation/__init__.py | empty package marker — nothing to review |
| tests/validation/test_cli.py | 3 minor |
| tests/validation/test_cross_platform_metrics.py | 1 major, 1 minor |
| tests/validation/test_delta_report.py | 2 minor |
| tests/validation/test_fixed_risk_model.py | 2 minor |
| tests/validation/test_golden_files.py | 1 minor |
| tests/validation/test_importers.py | 1 minor |
| tests/validation/test_markdown_report.py | 4 minor |
| tests/validation/test_mt5_html_importer.py | 3 minor |
| tests/validation/test_mt5_xlsx_importer.py | 3 minor |
| tests/validation/test_paper_readiness.py | 2 minor |
| tests/validation/test_percent_risk_model.py | 2 minor |
| tests/validation/test_quantconnect_result_importer.py | 2 major, 2 minor |
| tests/validation/test_regression.py | 2 minor |
| tests/validation/test_report_structured.py | 3 minor |
| tests/validation/test_risk_comparison.py | none |
| tests/validation/test_risk_sizing.py | none |
| tests/validation/test_stats_comparison.py | none |
| tests/validation/test_trade_anomalies.py | none |
| tests/validation/test_trade_comparison.py | none |
| tests/validation/test_trade_matcher.py | none |
| tests/validation/test_trade_statistics.py | 1 minor |
| tests/validation/test_validation_reference.py | 1 minor |
| tests/validators/backtest_validator.py | 1 major, 2 minor |
| tests/validators/code_validator.py | 1 major (security), 2 minor |

## Finding counts by severity

- **critical: 1** (`tests/test_trade_engine.py` cross-engine tolerance / tautology / triple-skip)
- **major: 10** (yfinance stub contract; stockkit alias naming; codegen loose arity; backtest_validator trade-count path; code_validator security bypass + untrusted exec; viewer stack-walk doubles; cross-platform missing-artifact guard; walkforward mock-only coverage; QC golden fixture hard-error; QC golden regeneration)
- **minor: 54**















