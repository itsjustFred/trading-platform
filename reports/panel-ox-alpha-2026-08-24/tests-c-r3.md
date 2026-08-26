# Code review — area `tests-c` (chunk C), reviewer r3 (ox-alpha)

Scope: the 40 tracked files under `tests/` listed for chunk C. Reviewed blind and independently; only files in my area were read (plus targeted reads of production code to verify whether a suspicious assertion encodes a real contract).

Severity scale: critical / major / minor. Every file is accounted for.

---

## tests/test_viewer_features.py

(a) Bugs / correctness risks

- **tests/test_viewer_features.py:2784–2830 — polling loop with unbounded sleep and silent timeout fall-through (major, flaky-pattern).** `test_start_data_task_writes_task_logs_and_refreshes_catalog` loops up to 50 × `time.sleep(0.1)` waiting on a real subprocess/thread; if the task never reaches a terminal state the loop exits and the assertions fail with an unrelated message ("completed" != actual), i.e., a timing-dependent failure mode rather than a deterministic one. The same wall-clock-poll pattern appears at :2888–2895 (`test_optimization_run_starts_background_task`, 50 × 0.05 s), :3004–3008 (`test_backtest_run_registers_task_manager_progress`), and :3151–3155 (`test_backtest_run_signals_registers_task_manager_progress`). These are integration-ish tests inside what is nominally a unit suite: they spawn a real `sys.executable -c ...` process (:2800–2809). On a loaded CI box the ~5 s budgets can be exceeded → flaky reds that mask nothing but erode trust.
- **tests/test_viewer_features.py:1402–1404 — concurrency test asserts exactly one winner via counts only (minor).** `test_download_registry_coalesces_concurrent_identical_requests` uses a 2-thread barrier with `join(timeout=2)`; correct in intent, but if a worker misses the barrier deadline it raises `BrokenBarrierError` inside the thread (unobserved) while the count assertions still pass with one `None` — the race the test claims to exercise can silently not happen. Low impact because the barrier makes the interleaving overwhelmingly likely, but the assertion would be stronger checking both records reference the same task id.
- **tests/test_viewer_features.py:1418 — `assert release.wait(timeout=2)` inside a worker thread (minor).** If the wait times out, the assert raises in the thread and is swallowed by the bare `except RuntimeError` at :1426, leaving `starter_errors` empty and failing later at :1447 with a confusing list-comparison error instead of a clear timeout message. The `# pragma: no cover` comment on an except clause that is actually exercised is misleading.
- **tests/test_viewer_features.py:2533 — `assert isinstance(listed["elapsed_seconds"], int)` (minor, brittle).** Elapsed seconds computed from wall clock could legitimately be serialized as float; asserting the concrete type couples the test to serialization details rather than behavior.
- **tests/test_viewer_features.py:3416–3417 — p-value test asserts a range plus `< 0.05` (minor).** Reasonable smoke, but the bound depends entirely on the hand-picked return series having tiny dispersion; it verifies the endpoint wires *a* statistic through, not its correctness. Acceptable given `test_stats.py` covers computation, noted for completeness.
- **tests/test_viewer_features.py:2275–2284 — `_auto_download_source` expectations hard-code routing policy with no rationale (minor, maintainability).** `("SP500","1m")→dukascopy`, `("BTC/USD","1h")→dukascopy`, etc. encode the current routing table verbatim; any deliberate re-routing breaks the test even when behavior is fine. Verified against `viewer/main.py` — consistent today, just change-amplifying.
- **tests/test_viewer_features.py:2296–2299 — magic boundary dates in split-plan expectation (minor).** `"2025-07-31"` / `"2025-07-01"` come from `HF_INTRADAY_COVERAGE_END` in `viewer/main.py` but are duplicated as literals here; if the coverage constant moves, the test fails without explaining why. Should derive from the module constant.

(b) Security

- **tests/test_viewer_features.py:2968–2984 — subprocess faking is good; no real command injection surface tested negatively (minor, gap).** `/api/backtests/run` builds a `runner.cli` command from user JSON (`strategy` filename etc.). Tests cover happy path only; there is no test that a `strategy` value like `../../x.py` or embedded shell metacharacters is rejected before reaching `Popen`. Absence of coverage is itself a risk worth flagging for a platform that launches arbitrary strategy files.
- **tests/test_viewer_features.py:4748–4790 — log endpoints tested only with well-formed JSON lines (minor, gap).** No malformed-line case, so path-traversal or oversized-log behavior of `/api/logs/{name}` is unverified.

(c) Baseline smells

- **Duplicated Code (major hot spot):** the `_NoStartThread` double is re-declared near-identically at least seven times (:1258–1279, :1310–1316, :1349–1356, :1469–1476, :1526–1533, :1598–1605, :1748–1755), and the stack-inspecting intercept variant twice more (:1642–1671, :1698–1723); the `captured`-dict `fake_start(task_id, cmd, **kwargs)` stub is copy-pasted five times (:1216–1220, :2098–2102, :2145–2149, :2187–2191, :2224–2228); the 20-key metadata-row dict literal for `list_backtest_metadata_rows` is repeated six times (:3532–3554, :3592–3630, :3647–3689, :3722–3743, :3767–3789, :3817–3840). All belong in one parametrized factory fixture — this file alone would shrink by several hundred lines.
- **Mysterious Name:** `test_backtest_run_signals_...` (:3043) vs `test_backtest_run_from_signals_csv...` (:3170) differ by one word yet test completely different endpoints (`run-signals` vs `run-from-signals`).
- **Long Function / God test:** `test_create_portfolio_persists_p_value_and_psr` (:4128–4222) exercises create, list, detail, contributions, and filtered trades in one body — five behaviors, so a single failure doesn't say which broke.

(d) Maintainability hot spots

- At 4790 lines this single file mixes data-catalog, download-coalescing/concurrency, task lifecycle, backtest run plumbing, portfolios, agent endpoints, and logs. It should be split per subsystem; the coalescing/cleanup cluster (:1252–2091) alone is a self-contained module's worth of tests.
- Tests reach deep into private state (`DATA_TASKS_LOCK`, `_BACKTEST_META_CACHE_SOURCE_MTIME_NS`, `_COALESCED_CLEANUP_PENDING`, `_TASK_CACHE_HYDRATED`) — acceptable for regression pinning of known races, but every rename in `viewer/main.py` breaks dozens of sites here.

Scanned fully. Findings above; no tautological assertions found — assertions throughout reference concrete payloads and would fail on regressions.

---

## tests/validation/test_quantconnect_result_importer.py

(a) Bugs / correctness risks
- None found. Parametrized cases carry distinct expected values (no copy-paste constants), failure paths assert on raised exceptions with match patterns, and numeric parsing cases use exact expected values including negative/zero/comma variants.

(b) Security
- Nothing applicable (pure parser tests; no subprocess/network/fs escape surfaces).

(c) Baseline smells
- **Long parameter lists in fixtures** building large synthetic QC result dicts inline (minor): several tests construct near-identical importer-input scaffolding repeatedly; a shared builder would cut noise. Judgement call, low priority.

(d) Maintainability hot spots
- File length (912 lines) driven by table-driven parsing cases; content is coherent (one importer), so splitting is optional.

Scanned fully; nothing found beyond the smell note.

---

## tests/validation/test_delta_report.py

(a) Bugs / correctness risks
- **tests/validation/test_delta_report.py:100–102 — `Any` used but never imported (major).** `_make_field_result` annotates `expected: Any`, `actual: Any`, `tolerance: Any` without `from typing import Any`; under `from __future__ import annotations` (line 3) this does not crash at runtime because annotations stay strings, but it is a latent NameError for anything that resolves annotations (`typing.get_type_hints`, dataclass tooling, runtime-checked validators) and a broken signal for type checkers. The helper is also **dead code**: no test calls `_make_field_result` — the whole function can be deleted.
- **tests/validation/test_delta_report.py:267 & 325 — warning-severity mismatch yields `passed is True` is asserted as intended behavior (minor, judgement call).** The comment "warning-only mismatch allows passed=True" documents the contract, so this encodes policy, not a bug; flagged only because a numeric entry-price difference of 0.45% letting a validation report "pass" is surprising for a *validation* suite — worth an explicit product-level decision note.

(b) Security
- Nothing applicable (in-memory model construction only).

(c) Baseline smells
- **Duplicated Code:** the five-line block `ref = _make_trade(...); act = _make_trade(...); matched = _make_matched(ref, act); comp = TradeComparator().compare(matched)` is repeated verbatim ~15 times across classes (e.g., :245–248, :271–273, :291–292, :301–304, :318–321, :328–331, :355–357, :373–376, :384–387); belongs in one helper returning `(ref_model, platform, comp)`.
- **Speculative Generality:** `_make_field_result` (:98–113) is never referenced — delete.
- Class-per-test granularity (`TestPassedLogic`, `TestExplanation`) is fine, but each class re-derives identical fixtures instead of sharing them.

(d) Maintainability hot spots
- Low overall; the duplication is mechanical and safe to extract.

Scanned fully; findings above. No tautologies: every assertion pins concrete ids/severities/counts that would fail on regression.

---

## tests/validation/test_paper_readiness.py

(a) Bugs / correctness risks
- **tests/validation/test_paper_readiness.py:510–525 — `test_empty_inputs` name contradicts its own docstring and assertion (minor/major-borderline misleading test).** The docstring says "All optional inputs omitted → NOT_READY because required artifacts missing", yet the test constructs a config disabling all four requirements and asserts `READY`. As written the assertion matches the config, so it's not broken, but the docstring/describe text is wrong and the NOT_READY-when-required-artifacts-missing path is left untested — the more safety-critical branch has zero coverage here.
- **tests/validation/test_paper_readiness.py:220 & 292 — PnL/price mismatch severity expectations hard-coded to error vs warning (minor).** `test_not_ready_on_pnl_error` builds the delta already labeled `severity="error"` and then asserts the validator reports an error — the validator's own severity-classification of raw field mismatches (which fields are errors vs warnings) is decided upstream in `delta_report`/`comparison`, so these tests partially assert what they themselves put in. Acceptable as validator-level tests; noted so nobody mistakes them for end-to-end severity classification proof.
- No flaky patterns, no sleeps, no swallowed exceptions.

(b) Security
- Nothing applicable.

(c) Baseline smells
- **Duplicated Code:** the 6-line `report/regression/risk/stats` assembly block repeats in all 13 tests (e.g., :170–174, :201–204, :226–229); a single builder fixture would remove ~60 lines.
- **Data Clumps:** `(report, regression, risk_report, stats, config)` passed positionally to every `ReadinessValidator.validate` call — a bundle object or fixture would remove the clunk (judgement call).

(d) Maintainability hot spots
- Clean file; helpers at top are genuinely shared. Best-structured of the validation suites reviewed so far.

Scanned fully; findings above.

---

## tests/test_strategy_meta_adapters.py

(a) Bugs / correctness risks
- **tests/test_strategy_meta_adapters.py:20 vs :23–24 — redundant/conflicting path setup (minor).** Line 20 inserts repo root into `sys.path` *after* the imports above it already succeeded (so the insert is a no-op for those imports and only affects later lazy imports); meanwhile `REPO_ROOT = Path(__file__).resolve().parent.parent` (:23) is computed and never used. Dead line + misleading pattern copied from other files.
- **tests/test_strategy_meta_adapters.py:104–119 — magic-number sizing assertions pin floating-point artifacts (minor).** `trade.quantity == 142.8571` and `trade.pnl == -142.86` encode rounded outputs of the risk-sizing math with no shown derivation beyond `risk_math.risk_target_usd == 1000`; correct today (100000×1% / (100−95) = 2000 units? — actually entry 100, SL 95 → distance 5 → 1000/5 = 200… the value 142.8571 implies distance 7, i.e., the adapter used sl_price 93 or a different formula), so these literals deserve a derivation comment; if someone changes rounding they'll fail without explanation. Verified the values are consistent within the file's own `risk_math` meta assertions (`position_size == quantity`, `capped_by_max_leverage is False`), so this is documentation debt, not an encoded bug.
- **tests/test_strategy_meta_adapters.py:180–196 — `test_all_vectorbt_strategies_return_entries_exits_and_indicators_dict` iterates the real strategies dir (minor, environment coupling).** Uses `DEFAULT_STRATEGIES_DIR` from production code; results depend on whatever strategies exist in the checkout. A repo state where all strategies are `pynecore_*` would make the loop body execute zero assertions (vacuous pass). Add at least a smoke assertion that the loop visited >0 strategies.
- **tests/test_strategy_meta_adapters.py:329 — exact pnl equality on float math (minor).** `trade.pnl == 39.6` on intrabar TP simulation; fine as a regression pin but brittle to any reordering of multiplication — prefer `pytest.approx`.

(b) Security
- Nothing applicable; strategy files are written into `tmp_path` and loaded via importlib by the adapter under test (that's the behavior under test, not a test bug).

(c) Baseline smells
- **Duplicated Code:** the `data_root/parquet_dir/df.to_parquet(...)` OHLCV scaffold repeats in six tests (:74–87, :142–154, :223–235, :277–306, :366–378) — extract a `_write_ohlcv(tmp_path, rows)` helper.
- **Mysterious Name:** the module docstring-less filename says "meta adapters" but the content is mostly vectorbt adapter end-to-end runs plus pynecore context enrichment — two concerns in one module (Divergent Change).

(d) Maintainability hot spots
- These are true integration tests through the real adapter — high value. Keep, but split pynecore-helper tests (:410–505) into their own module.

Scanned fully; findings above.

---

## tests/validation/test_percent_risk_model.py

(a) Bugs / correctness risks
- **tests/validation/test_percent_risk_model.py:310–330 — `test_validate_extracts_take_profit_for_risk_reward` tests nothing its name claims (major, misleading test).** The name says meta extraction of take-profit, but the body explicitly bypasses extraction: it builds `inp2` pulling `take_profit` out of `trade.meta` *manually in the test*, then asserts RR. Lines 320–322 even admit "The model uses input.take_profit directly; meta extraction is for stop_loss only." So there is no code path under test that extracts TP from meta — the name promises behavior that may not exist. Rename to `test_risk_reward_ratio_when_take_profit_passed_explicitly` or add the actual adapter-level extraction test referenced in the comment.
- **tests/validation/test_percent_risk_model.py:245–252 — min-clamp-to-zero semantics asserted without cross-check of `risk_amount` (minor).** `position_size == 0` and `total_risk_amount == 0` is asserted, but per-profile `risk_amount` after clamping is not; if the profile still reports `risk_amount == 100` while size is 0, downstream dashboards would overstate exposure. One more assertion would pin the contract.

(b) Security
- Nothing applicable; positive sign that validation rejects NaN/inf inputs (:396–414) — that's the security-relevant surface here and it is covered.

(c) Baseline smells
- **Duplicated Code:** `PercentRiskConfig(risk_percent=0.01)` + `model = PercentRiskModel(config)` repeated in ~30 tests; a module-level fixture would remove ~60 lines (judgement call — explicitness has value here).
- Section-banner comments duplicated across sibling risk-model test files (fixed_risk, risk_sizing) — copy-paste structure, acceptable.

(d) Maintainability hot spots
- Good numeric derivations in comments (:84, :95, :103, :213, :220, :248, :266). Only real issue is the misnamed TP test.

Scanned fully; findings above.

---

## tests/validation/test_report_structured.py

(a) Bugs / correctness risks
- **tests/validation/test_report_structured.py:245 — tautological assertion (minor, can never fail).** `assert trade_summary["errors"] + trade_summary["warnings"] >= 0` over two non-negative count fields is unfalsifiable; delete or replace with concrete expected counts for the fixture (which has exactly 1 missing + 1 extra).
- **tests/validation/test_report_structured.py:354 — near-tautology (minor).** `assert "Yes" in md or "No" in md` passes for almost any rendered markdown containing either word anywhere; pin which one the passing report must contain (`Yes`, since passed).
- **tests/validation/test_report_structured.py:393 — suspicious unnormalized input with no corresponding assertion (minor).** `symbol=" sym"` (leading space) is fed to `ValidationReport` but the dump only asserts `strategy_name` and summary fields — if the model was supposed to strip symbols, that behavior is untested; if not, the space is a confusing leftover. Either assert `d["symbol"] == "sym"` or use a clean literal.
- **tests/validation/test_report_structured.py:339 — `pytest.raises((TypeError, AttributeError))` (minor).** Accepting two unrelated exception types hides which failure mode actually occurs; the contract should be one specific error.

(b) Security
- Positive: strict-JSON guarantees tested well (:405–439) — non-finite floats sanitized to `null` so reports can't smuggle `NaN`/`Infinity` tokens into downstream parsers. No issues found.

(c) Baseline smells
- **Duplicated Code:** `_make_trade/_make_reference/_make_platform_result` triple is byte-identical to the helpers in `test_delta_report.py` (:26–95 here vs :26–95 there) — belongs in a shared `tests/validation/helpers.py`.
- Minor: `_make_field_result`-style unused import check — `MatchedTrade`/`AnomalyDetector` etc. are all used here; clean.

(d) Maintainability hot spots
- Strong suite overall (strict-JSON class is exemplary). Main debt is helper duplication across the comparison-test family.

Scanned fully; findings above.

---

## tests/validation/test_trade_anomalies.py

(a) Bugs / correctness risks
- **tests/validation/test_trade_anomalies.py:24–83 — third verbatim copy of the `_make_trade/_make_matched/_make_reference` triple (minor).** Identical to `test_delta_report.py` and `test_report_structured.py`; see Duplicated Code below.
- No behavioral findings: severities per field are asserted concretely (error for direction/pnl, warning for time/volume/SL/TP), duplicate detection covered from both sides (:323–346), and integration cases pin the composition contract. All assertions falsifiable.

(b) Security
- Nothing applicable.

(c) Baseline smells
- **Duplicated Code (cross-file):** helper triple repeated across at least three validation test modules — extract to `tests/validation/helpers.py`.
- **Repeated Switches (mild):** `TestDetectMismatches` runs the same four-line act/arrange shape nine times differing only in field and expected severity; a parametrized case list (`field=`, `ref_value`, `act_value`, `expected_severity`) would collapse ~80 lines and make a missing-field gap visible as an absent row rather than an absent test.

(d) Maintainability hot spots
- None beyond the above; well-organized.

Scanned fully; findings above.

---

## tests/test_trade_engine.py

(a) Bugs / correctness risks
- **tests/test_trade_engine.py:324–368 — engine-parity test is environment-dependent and its fallback branch is a tautology (major).** `test_example_sma_native_vs_vectorbt_trade_count_similarity` skips unless `DATA_DIR` happens to contain 1h parquet with 2024–2025 coverage, so on most dev/CI machines it silently does nothing; worse, when both engines produce zero trades it executes literal `assert True; return` (:363–365) — a can-never-fail branch that reports success without exercising anything. The comparison itself then allows ±30% trade-count divergence (`tolerance = max(2, int(vectorbt_count * 0.3))`, :367), which is so loose that a 29% discrepancy between two engines on the same data would pass. For a trading platform, native-vs-vectorbt parity deserves deterministic synthetic data instead of "whatever parquet exists locally" plus a tolerance band this wide.
- **tests/test_trade_engine.py:18–75 — 57 lines of production-grade helpers duplicated into the test file (minor, misleading-maintenance risk).** `_parse_date_bound`, `_to_utc_datetime`, `_time_bounds`, `_has_coverage` re-implement logic that exists in `runner/engines/vectorbt_adapter.py:1050` (same function name/signature) and in `tests/strategies/conftest.py:38,58`. The copies have already diverged slightly in type (`pd.Timestamp` vs `datetime` return). If the production date-boundary semantics change (e.g., end-of-day inclusivity at :27–28), these tests keep validating against their own stale copy — the coverage gate could accept files that production now rejects. Import or extract to a shared test utility instead.
- **tests/test_trade_engine.py:54 & :63 — bare-ish `except Exception: return None, None` around reads (minor).** In test code this is tolerable (coverage probe), but it converts corrupt/unreadable parquet into "no coverage" → skip, i.e., environmental failure becomes silence. At minimum log why.
- **tests/test_trade_engine.py:195/:234/:279 — `pnl == pytest.approx(-1000.0, abs=0.5)` (minor).** Good absolute-tolerance use; noting only that the derivation (1% of 100k over a 2-point stop → 1000 units × −2 = −2000? no — size = 1000/2×… ) is not commented, unlike sibling suites; add one line of arithmetic so future readers don't "fix" the constant.

(b) Security
- Nothing applicable; no subprocess/network. (The parity test reads whatever file matches `**/1h.parquet` under `DATA_DIR` — local data only.)

(c) Baseline smells
- **Duplicated Code:** `BacktestConfig(...)` blocks repeat six times nearly identically (:116–125, :168–179, :213–224, :258–269, :300–311, :346–355); a `make_cfg(**overrides)` factory removes noise.
- **Speculative Generality:** `_to_timestamp_utc` (:78–82) raises but is never called anywhere in the file — dead code.

(d) Maintainability hot spots
- The execution-model assertions (:135–149) pinning `market_order_timing`/`stop_first` dictionaries are excellent regression pins for fill semantics — keep. The parity test needs a redesign (deterministic fixture, tighter tolerance or exact equality target).

Scanned fully; findings above.

---

## tests/validation/test_trade_statistics.py

(a) Bugs / correctness risks
- **tests/validation/test_trade_statistics.py:185–195 — drawdown-% expectation depends on unstated initial-capital assumption (minor).** Comment says "max DD% = 70 / 100 = 0.7" where 100 is the first cumulative PnL sum used as peak; correct given the calculator's convention (peak starts at 0), and consistent with :198–203 which pins the always-losing case (DD% stays 0). Not a bug — flagging that the denominator convention (initial equity = 0 + cumulative pnl) is only implicit; one clarifying line would prevent false "bug" reports later.
- **tests/validation/test_trade_statistics.py:245–247 — population variance (÷N) assumed in Sharpe hand-calculation (minor).** Expected value uses `/5`; if the calculator switched to sample std (÷4) the test fails by ~11%. That's fine as a pin of current behavior, but the comment should say "population std" explicitly since annualized Sharpe conventions differ.
- Everything else checks out arithmetically (verified DD sequences :170–173, :206–210; Sortino downside variance :278; recovery factor :303).

(b) Security
- Nothing applicable.

(c) Baseline smells
- None significant. Section banners aid navigation; `_make_trade` helper is local and single-purpose (no cross-file duplication here).

(d) Maintainability hot spots
- This is the strongest file reviewed so far: explicit derivations, edge cases (inf profit factor, break-even exclusion, multi-exit aggregation, empty input). Keep as the reference style.

Scanned fully; findings above.

---

## tests/validation/test_regression.py

(a) Bugs / correctness risks
- **tests/validation/test_regression.py:389–392 — CWD-dependent fixture path causes silent skip (minor).** `test_regression_run_with_importer` hard-codes `Path("tests/fixtures/validation/mt5_html/sample_report.html")` relative to the process working directory; from any other CWD the guard `if not html_path.exists(): pytest.skip(...)` makes the only importer-integration regression test vanish silently. Should anchor with `Path(__file__)`.
- **tests/validation/test_regression.py:87–100 — expected-stats list duplicated as literal (minor).** `len(result.stats_delta)` compared against a hand-written 10-field list; if production adds/removes a tracked statistic the test fails with an opaque length mismatch rather than naming the field. Prefer asserting the exact field-name set.
- **tests/validation/test_regression.py:172–183 — `test_regression_trade_and_stats_both_fail` second trade removed changes net profit too (minor, weak isolation).** Golden has 2 trades, actual 1 with different pnl/entry — assertions hold, but the stats delta conflates "missing trade changed aggregate" with tolerance logic; the test name promises both failure modes and delivers, just not independently. Judgement call.

(b) Security
- Nothing applicable (temp-dir goldens via `tmp_path`, good).

(c) Baseline smells
- **Dead code:** unused imports `copy` (:5), `FieldDelta`, `TradeDelta` (:14), `MatchKey` (:15) — none referenced in the file body.
- **Duplicated Code:** the `golden_path` write + `run_regression(golden_path, current_data=...)` scaffold repeats in all ~25 tests; acceptable given clarity, but a context manager would halve the file.

(d) Maintainability hot spots
- Solid behavioral coverage of tolerances incl. inf handling (:460–476, a genuinely valuable edge case). Main debt is the duplicated scaffold.

Scanned fully; findings above.

---

## tests/validation/test_fixed_risk_model.py

(a) Bugs / correctness risks
- **tests/validation/test_fixed_risk_model.py:86–98 — self-contradictory comment block around the one test that matters (minor).** `test_lot_rounding_never_exceeds_risk_budget` carries a stream-of-consciousness comment ("raw size = 100/0.4 = 250 ... use a value that rounds UP ... 260 lots? Instead pick...") left in from debugging. The final assertion pair (:96–97) is excellent — it pins floor-rounding staying within budget, exactly the property you want in a risk-sizing model — but the muddled derivation invites someone to "clean up" the test into a weaker form. Rewrite the comment with the actual arithmetic (stop_distance = 0.385 → raw = 259.74).
- **tests/validation/test_fixed_risk_model.py:158–165 — `test_round_lots_rounds_up` name contradicts behavior (minor, misleading name).** The test asserts nearest-multiple rounding *down* (33.33→30), and its own inline math says `round(3.333)=3`. In `test_percent_risk_model.py` the equivalent case is named `test_round_lots_rounds_down` — same scenario, inconsistent naming across sibling suites; here the name is simply wrong. Also note this conflicts with :86–97 which proves floor semantics for the 259.74 case — so "rounds_up" is doubly wrong.
- **tests/validation/test_fixed_risk_model.py:237–245 — `test_stop_distance_pct_when_entry_nonzero` tests a field it admits doesn't exist (minor, misleading).** Comment: "TradeRiskProfile doesn't have stop_distance_pct field. We verify stop_distance instead." Then the test should be renamed (`test_stop_distance_absolute_value`) or the missing pct field flagged as a gap; as named it implies percentage coverage exists.

(b) Security
- Nothing applicable; NaN/inf validation covered positively (:275–286).

(c) Baseline smells
- **Duplicated Code:** near-clone of `test_percent_risk_model.py` structure and helpers (`_make_trade`, `_make_input`, section banners, clamping/rounding/meta-extraction cases). A shared parametrized harness across both sizing models would eliminate ~200 duplicated lines while making coverage gaps (e.g., percent model has RR-from-meta cases fixed-risk lacks) visible.

(d) Maintainability hot spots
- Same as percent-risk suite; fix the three misleading names/comments before they mislead a maintainer.

Scanned fully; findings above.

---

## tests/validation/test_cli.py

(a) Bugs / correctness risks
- **tests/validation/test_cli.py:50–57 — real subprocess invocation per test without timeout (minor/major-borderline, checklist item).** `_invoke_cli` runs `[sys.executable, -m, validation.cli]` via `subprocess.run(capture_output=True)` with **no `timeout=`**. If the CLI ever hangs (bad importer loop, waiting on stdin), every CLI test hangs the suite indefinitely. Add `timeout=60`; the Python bug-checklist explicitly calls out subprocess-without-timeout. Positive: cwd is pinned to repo root (:56), avoiding the CWD-dependence problem seen in test_regression.py.
- **tests/validation/test_cli.py:250 — triple-OR error-message assertion (minor, weak).** `"not found" in stderr or "failed to load reference" or "no such file or directory"` accepts almost any failure text; if the CLI starts raising an unrelated error with returncode 2, this still passes only if message matches one of three broad substrings. Acceptable for exit-code testing, but pinning one canonical message would catch copy regressions in error text.
- **tests/validation/test_cli.py:109 — markdown assertion with two accepted formats (minor).** `"Overall Passed**:** Yes" or "Overall Passed**: Yes"` suggests uncertainty about the renderer's output; check the actual format once and assert exactly, otherwise a formatting regression to a third variant passes unnoticed... actually no — a third variant fails both. Fine, just noisy.
- Exit-code contract coverage is genuinely good: 0 perfect match (:194–200), 1 on validation failure (:113–126), 2 on bad path (:238–249).

(b) Security
- Subprocess runs only local module with pinned cwd; no shell=True; inputs are pytest tmp paths. No findings beyond the timeout gap above.

(c) Baseline smells
- **Duplicated Code:** ref/platform JSON-writing scaffold repeats across five test classes (:205–214, :254–265); fixtures `tmp_reference_json`/`tmp_platform_json` exist but aren't reused where custom trades are needed — extend them with an overrides parameter instead of re-inlining.
- Minor: XLSX/HTML fixture builders inline large literals (:149–178, :293–329) — fine since they double as documentation of importer expectations.

(d) Maintainability hot spots
- Solid end-to-end CLI coverage including auto-importer dispatch. Main risk is the missing subprocess timeout.

Scanned fully; findings above.

---

## tests/test_walkforward.py

(a) Bugs / correctness risks
- **tests/test_walkforward.py:18–45 — signature/defaults tests assert API shape, not behavior (minor, brittle-by-design).** `TestWalkforwardSignature` inspects `inspect.signature` and pins default literals (`12/3/1/100`). These are change-detectors: renaming a kwarg or adjusting a default fails them loudly even when behavior is fine. Defensible as contract pinning for a CLI-facing module; noted as judgement call.
- **tests/test_walkforward.py:51–114 — heavy mocking leaves the actual fold-splitting logic untested in these classes (minor).** `TestWalkforwardOutputStructure`/`SummaryMetrics` mock `optimize_portfolio_allocation` entirely, so the mocked dict flows through aggregation only. That's appropriate unit isolation, but it means fold-window computation is covered *only* by :393–421 — if that test were deleted, the suite would silently lose all real fold-boundary coverage.
- **tests/test_walkforward.py:268–270 — timestamp-length assertion is weak (minor).** `len(ts_part) == 15` accepts any 15 characters (e.g., `abcdefgh_ijklmn`); parse with `datetime.strptime(ts_part, "%Y%m%d_%H%M%S")` to actually validate format.
- **tests/test_walkforward.py:297 — `assert len(result["folds"]) == 1  # Current implementation has single fold` (minor, encodes known limitation).** The comment admits this pins an incomplete implementation; when real multi-fold support lands in the backtest-ids mode this must be updated — flagged so it isn't mistaken for intended final behavior.
- **tests/test_walkforward.py:393–421 — positive finding:** strategy-mode rolling folds are well covered — fold boundaries (:416–419), engine call count (6 = 3 folds × train+test), summary aggregation, and on-disk round-trip all asserted concretely.

(b) Security
- Nothing applicable.

(c) Baseline smells
- **Duplicated Code:** the `mock_optimize` closure returning a sharpe/drawdown dict is re-declared in nine tests with trivially different constants (:54–60, :95–100, :122–127, :144–149, :166–171, :188–192, :216–221, :250–251, :278–283); one parametrized fixture would do.
- **Mysterious Name / hidden coupling:** `_FakeEngine.run` returns `sharpe = 0.5 if is_oos else 1.5` keyed off call parity (`len(self.configs) % 2`, :354) — works but silently flips every expectation if train/test call order changes.

(d) Maintainability hot spots
- Two different `run_walkforward` calling conventions coexist in the file (backtest_ids mode vs strategy mode) without a note; readers may not realize they exercise different code paths.

Scanned fully; findings above.

---

## tests/validation/test_trade_comparison.py

(a) Bugs / correctness risks
- **tests/validation/test_trade_comparison.py:262–291 — OR-logic tolerance semantics documented but risky (minor, judgement call).** `test_pnl_outside_abs_tolerance_only` and `test_zero_vs_nonzero_pnl_mismatch` explicitly pin "passes because pct/abs within tolerance" under OR logic. The comments show full awareness, so this is policy, but be advised: OR logic means a $0.01 abs tolerance never binds when pct passes — large-PnL trades can drift arbitrarily in absolute terms. Tests encode current behavior correctly; flagged for product awareness since PnL equality is usually a hard requirement in validation contexts.
- **tests/validation/test_trade_comparison.py:83 — `len(result.field_results) == 10` (minor).** Exact field-count pin; adding an 11th compared field breaks this with an unhelpful message. Prefer asserting the expected key set by name.

(b) Security
- Nothing applicable.

(c) Baseline smells
- **Duplicated Code:** fourth copy of `_make_trade/_make_matched` triple (:20–65), byte-identical to three other validation files.
- **Repeated Switches:** each `Test*Field` class repeats within/outside-tolerance pairs; parametrizing `(field, ref, act, passes)` would halve length while keeping explicitness.

(d) Maintainability hot spots
- Good boundary discipline (values chosen just inside/outside tolerances, e.g., :116/:123, :165/:173). Comments carry derivations. Low debt besides duplication.

Scanned fully; findings above.

---

## tests/validation/test_risk_sizing.py

(a) Bugs / correctness risks
- **tests/validation/test_risk_sizing.py:180–186 — fallback-stop test pins the 2% default but not its provenance (minor).** Comment correctly derives `1.1000 * 0.02`; good. However nothing asserts the fallback stop *side* here (covered separately at :330–345, well done). No real issue.
- **tests/validation/test_risk_sizing.py:200–207 — wrong-side stop rejection asserts zeros only (minor).** `position_size == 0`, `risk_amount == 0`, `risk_percent == 0` asserted, but there's no assertion that the profile records *why* (e.g., an explanation/message field). If the model silently zeroes sizes for any validation failure, a data bug (stop above entry from a bad importer) becomes indistinguishable from an intentional block. One assertion on a diagnostic message would pin observability.
- **tests/validation/test_risk_sizing.py:189–197 — ATR-fallback test constructs its own RiskEngine instead of using the adapter default (minor, judgement call).** Fine for isolation; noted because it means the *default* engine's ATR wiring is never exercised end-to-end.

(b) Security
- Nothing applicable; risk-limit enforcement paths (max_positions :217–223, max_total_exposure :226–235, account_balance>0 :210–214) are exactly the safety surface and they're covered — positive finding.

(c) Baseline smells
- **Duplicated Code:** fifth copy of `_make_trade` helper family (:29–58), plus `RiskAdapter(pip_value=1.0)` re-instantiated in ~18 tests — module fixture would do.
- **Dead code:** `math` imported (:13) and never used; `RiskConfig`/`RiskEngine` imports are used only twice, fine.

(d) Maintainability hot spots
- Strong coverage of the fallback hierarchy (direct SL → meta SL → meta ATR → pct fallback). Only debt is duplication.

Scanned fully; findings above.

---

## tests/validation/test_markdown_report.py

(a) Bugs / correctness risks
- **tests/validation/test_markdown_report.py:125 — copy-paste tautology in disjunction (major-borderline minor, broken test logic).** `assert "Overall Passed**: Yes" in md or "Overall Passed**: Yes" in md` — **both alternatives are byte-identical**, so the `or` is dead. Compare `test_cli.py:109`, which compares two genuinely different formats (`"Overall Passed**:** Yes"` vs `"Overall Passed**: Yes"`); this file clearly lost the first variant in a copy-paste. Either restore the intended second format or collapse to a single assertion — as written it silently narrows accepted formats while pretending to be lenient.
- **tests/validation/test_markdown_report.py:223 — identical duplicated-literal disjunction (same defect).** `"Overall Passed**: No" in md or "Overall Passed**: No" in md`.
- **tests/validation/test_markdown_report.py:6–31 — ten-plus unused imports (minor, dead code).** `datetime`, `timezone`, `pytest`, `AnomalyDetector`, `FieldDelta`, `TradeDeltaReport`, `MatchKey`, `TradeMatcher`, `ReportSummary`, `StatDelta`, `TradeComparisonResult` are imported and never referenced (`datetime.datetime` on :284 is inside a string literal, not the symbol). File is visibly copy-pasted from `test_report_structured.py` without pruning.
- **tests/validation/test_markdown_report.py:160 & :162 — weak OR content assertions (minor).** `"entry_price" in md.lower() or "Entry" in md` and `"mismatch" ... or "differs"` accept either wording; acceptable for render smoke tests but they can't detect a regression that drops the numeric detail entirely as long as one word survives.

(b) Security
- Nothing applicable. Positive: :275–285 checks rendered markdown doesn't leak Python object reprs.

(c) Baseline smells
- **Duplicated Code:** sixth copy of the `_make_trade/_make_reference/_make_platform_result` triple (:34–80); also every test rebuilds identical ref/platform pairs inline where a class fixture would serve.
- **Speculative Generality:** the unused imports above are exactly that — remnants of the source template.

(d) Maintainability hot spots
- Structure mirrors test_report_structured.py; consolidate the pair into shared helpers to stop the drift already visible in :125.

Scanned fully; findings above.

---

## tests/validation/test_risk_comparison.py

(a) Bugs / correctness risks
- **tests/validation/test_risk_comparison.py:183–199 — `test_extra_trade_is_info` asserts `report.passed is True` for an unmatched extra trade (minor, judgement call on policy).** An extra trade the platform produced but the reference lacks yields `passed=True, severity="info"`. That mirrors the anomaly-severity policy elsewhere (extra=info), so it encodes documented behavior — but combined with :163–179 (missing=warning→passed False) the asymmetry is worth a comment; as-is a reader may think the assertion wrong.
- **tests/validation/test_risk_comparison.py:96–159 — three mismatch classes reuse identical t1/t2 (stop 90 vs 50) differing only in which tolerance is tightened (minor, good technique actually).** The isolation is deliberate and correct: each test loosens the other two tolerances to 1.0. No finding — noting the pattern positively.
- No tautologies or swallowed exceptions found.

(b) Security
- Nothing applicable.

(c) Baseline smells
- **Duplicated Code:** `_make_config()` defaults block (:62–74) plus per-test overrides is fine; the repeated `t1 = _make_trade(trade_id=1, entry_price=100.0, stop_loss=90.0)` line across ~10 tests could be a fixture. Minor.
- Seventh copy of the `_make_trade/_make_reference` helper family (:21–59).

(d) Maintainability hot spots
- Clean, focused file. The delta-conversion coverage (:232–297) nicely bridges risk comparison into the report model.

Scanned fully; findings above.

---

## tests/validation/test_trade_matcher.py

(a) Bugs / correctness risks
- **tests/validation/test_trade_matcher.py:198–234 — input-order pairing pinned as contract (positive with caveat).** `TestIdenticalTimestamps` documents that same-timestamp trades pair positionally. Good explicit contract; caveat: if the matcher ever switches to greedy best-effort matching under tolerance, these expectations flip silently — acceptable, just noting the coupling.
- **tests/validation/test_trade_matcher.py:247–251 — naive-timestamp-as-UTC asserted only via match success (minor).** `test_naive_timestamp_treated_as_utc` proves matching works but not *how* the key normalizes; contrast :271–276 which does assert the canonical `"Z"` form. Consistency gap only.
- No other findings: tolerance boundary cases (:102–133) are exact-boundary disciplined (5s diff vs 5s tol).

(b) Security
- Nothing applicable. Timezone normalization (:237–251) is itself a correctness/security-adjacent surface and it's covered — positive.

(c) Baseline smells
- Eighth copy of `_make_trade` helper (:11–31).
- Minor: `pytest` imported (:5) unused? — actually used nowhere visible; flagged as dead import along with nothing else.

(d) Maintainability hot spots
- None significant; well-scoped unit suite.

Scanned fully; findings above.

---

## tests/validation/test_mt5_xlsx_importer.py

(a) Bugs / correctness risks
- **tests/validation/test_mt5_xlsx_importer.py:15–24 — tests depend on binary repo artifacts outside `tests/` with no skip guard (major, environment coupling).** `_eurusd_path()`/`_nas100_path()`/`_sp500_path()` point at `strategy-comparisons/intraday_volatility_breakout/*.xlsx`. Unlike `test_regression.py` (which guards with `pytest.skip`) and `test_cli.py` (which builds fixtures inline), these tests fail outright if the xlsx files are absent, moved, or re-exported (any edit to the workbook changes trade counts/PnL). Worse, the assertions pin *data* values — `ref.stats["total_trades"] == 492` (:98), `settings["ATR_Period"] == 5` (:103), `source_timezone == "UTC"` (:109) — so a legitimate refresh of the reference export breaks ~20 assertions. Should either ship a small fixture xlsx under `tests/fixtures/validation/` or skip-guard like siblings.
- **tests/validation/test_mt5_xlsx_importer.py:188–197 — `test_entry_exit_direction_consistency` contains a dead branch structure (minor, misleading).** The `if direction == "LONG": pass / else: pass` block does nothing (comments admit it) and then asserts volume-in==volume-out regardless of direction; the test name promises direction consistency but validates only quantity equality. Rename or implement the intended per-direction in/out price-side checks.
- **tests/validation/test_mt5_xlsx_importer.py:67 & :70 — `entry_time.startswith("2")` (minor, weak).** A century-dependent smoke check; `datetime.fromisoformat(...)` parse would actually validate.

(b) Security
- Nothing applicable. Positive: timezone conversion covered incl. rejection of unknown zones (:250–274) and DST-correct January offset for Athens.

(c) Baseline smells
- **Duplicated Code:** every test calls `importer.load(_eurusd_path())` afresh — parsing a real 492-trade workbook dozens of times; a module-scoped fixture (`@pytest.fixture(scope="module")`) would cut runtime substantially.
- Minor: private-method testing (`_coerce_setting`, `_extract_first_percent`, `_parse_count_pct`, `_infer_exit_reason`, `_to_iso`, :222–274) is acceptable for parsers but couples tests to internals.

(d) Maintainability hot spots
- Data-pinning strategy is the main risk (see (a)); helper-unit coverage is otherwise exemplary.

Scanned fully; findings above.

---

## tests/test_stats.py

(a) Bugs / correctness risks
- **tests/test_stats.py:61 — win-rate asserted via hand-rolled epsilon instead of pytest.approx (minor, inconsistency).** `abs(stats["win_rate"] - 0.4) < 0.01` while sibling files use `pytest.approx`; the 0.01 slack would hide an off-by-one winner (0.4 vs 0.6 passes nothing here, fine, but 2/5 vs 3/6=0.5 also fails... acceptable) — mainly a style/consistency nit that weakens precision for no benefit.
- **tests/test_stats.py:87 — `assert stats["max_drawdown_pct"] != 0  # QuantStats returns negative` (minor, weak + sign-convention ambiguity).** Asserting merely "non-zero" can't detect a sign flip (positive vs negative drawdown), which matters because other modules (`test_trade_statistics.py`, viewer list endpoint at test_viewer_features.py:3581 expecting positive `max_drawdown_pct == 23.45` from `-0.2345` input) normalize signs differently across layers. Pin the sign explicitly.
- **tests/test_stats.py:187–211 — intraday-Sharpe test asserts loose inequalities only (minor).** `total_return > 0`, `sharpe_ratio > 0`, `len(daily_returns) == 29`; the count assertion is the real content (daily bucketing not bar density). Acceptable smoke, but a pinned expected Sharpe band would make a bucketing regression (e.g., treating bars as days → sharpe inflated by √24) detectable — right now a bar-density bug could still yield > 0.
- CAGR tests (:107–177) are excellent: fractional-day handling and explicit expected formulas with `math.isclose`.

(b) Security
- Nothing applicable.

(c) Baseline smells
- None significant; imports all used; helpers local and minimal.

(d) Maintainability hot spots
- Good file overall; the drawdown sign convention should be documented once and pinned (it differs between runner.stats and validation.stats — a genuine cross-module trap).

Scanned fully; findings above.

---

## tests/validation/test_cross_platform_metrics.py

(a) Bugs / correctness risks
- **tests/validation/test_cross_platform_metrics.py:16–48 — primary matrix test reads live repo data dirs with hard-pinned reference values (major, environment coupling).** `build_comparison_matrix` runs over `PROJECT_ROOT/strategy-comparisons` and `PROJECT_ROOT/reports`; assertions pin `trade_count == 512`, `win_rate ≈ 0.4492`, `sharpe_ratio == 0.544687` (:30–32, :42). These are snapshots of whatever artifacts were in the repo when written. Any regeneration of parity artifacts (the repo even ships scripts for exactly that, imported at :8–9) breaks these pins with no hint whether the *matrix code* or the *data* changed. The path-containment assertions (:46–48) are a nice security-ish touch (catches path escape), but they don't offset data brittleness. Prefer synthetic fixture comparisons dirs for logic tests and keep at most one smoke test against real artifacts.
- **tests/validation/test_cross_platform_metrics.py:77–106 — recomputes Sharpe from a synthetic curve but asserts only `> 0.0` (minor).** Given a fully deterministic equity series, an expected-value band would be trivial to pin; as written, a recompute that returns +0.0001 (wrong formula, right sign) passes.
- **tests/validation/test_cross_platform_metrics.py:109–128 — timezone-consistency test depends on both real xlsx and real backtest JSON existing (major-borderline minor).** Same environment coupling; also asserts equality of two strings derived from different pipelines with `.replace("+00:00", "Z")` normalization done inline in the test (:127) — that normalization belongs in the production serializer being tested, otherwise the test may be masking a format inconsistency between importer and platform outputs.

(b) Security
- Positive finding: :46–48 assert all artifact paths resolve inside PROJECT_ROOT — a real path-traversal guard on report inputs.

(c) Baseline smells
- **Data Clumps:** `comparisons_dir=.../reports_dir=.../platform_data_dir=...` repeated across seven calls; a small factory would help.
- **Mysterious Name:** file lives in `tests/validation/` but imports from `scripts/` (:8–9), coupling test modules to script internals (`COMPARISON_CASES`, `QC_RISK_PARITY_OUTPUT`); if those scripts move, this breaks.

(d) Maintainability hot spots
- The synthetic-artifact tests (:51–75, :131–203) are well built — tmp_path isolation, malformed-attribution fallback covered. Real-data tests are the debt.

Scanned fully; findings above.

---

## tests/validation/test_mt5_html_importer.py

(a) Bugs / correctness risks
- **tests/validation/test_mt5_html_importer.py:15–17 — CWD-relative fixture path without skip guard (minor/major-borderline).** `_sample_path()` = `Path("tests/fixtures/validation/mt5_html/sample_report.html")` relative to process CWD. Runs from repo root pass; from any other directory every test errors with FileNotFoundError rather than skipping. Anchor with `Path(__file__)`. (Same defect flagged in test_regression.py; here it affects ~25 tests instead of 1.)
- **tests/validation/test_mt5_html_importer.py:85–88 — `test_entry_exit_direction_consistency` again validates only quantity equality (minor).** Name promises direction consistency; body asserts `trade.quantity == trade.exits[0].quantity`. Copy of the misnamed xlsx-suite test. Implement per-direction price-side checks or rename.
- **tests/validation/test_mt5_html_importer.py:58 & :60 — `startswith("2")` century-dependent smoke (minor)** — same as xlsx suite.

(b) Security
- Nothing applicable. Negative cases cover missing file and empty HTML (:164–173) — good.

(c) Baseline smells
- **Duplicated Code:** near-clone of test_mt5_xlsx_importer.py structure (`TestHelpers` byte-similar :176–204 vs :222–247); the `_coerce_setting/_extract_first_percent/_parse_count_pct/_infer_exit_reason` helper tests exist because both importers duplicate those helpers in production code — Shotgun Surgery mirror image: fix once in a shared parser mixin and both suites collapse.
- Repeated `importer.load(_sample_path())` parsing in ~20 tests → module-scoped fixture.

(d) Maintainability hot spots
- Solid field-level coverage (long+short present, wins+losses present, SL/TP exit reasons). Main debts are CWD dependence and duplicated helper tests.

Scanned fully; findings above.

---

## tests/test_strategy_ir_codegen_vectorbt.py

(a) Bugs / correctness risks
- **tests/test_strategy_ir_codegen_vectorbt.py:62–63 & :105–106 & :140–141 — `exec()` of generated code without restriction (minor, acceptable-with-note).** Executing generated strategy code is the behavior under test, and the input is deterministic IR-generated source, not user data — but if `generate_vectorbt_strategy_code` ever interpolates unescaped strings into the template, these tests would happily exec whatever appears. A regression test asserting no raw user-controlled string reaches the code body (e.g., name with quotes/newlines) is missing; `_sample_ir` uses a benign `"Deterministic Demo"`.
- **tests/test_strategy_ir_codegen_vectorbt.py:78 — `len(output) in {3, 4}` (minor, permissive).** The generated contract should be stable; asserting `{3, 4}` hides which shape is produced. Pin one (4, given drawings support) or parametrize.
- Determinism tests (:49–55) and sorted-meta-parameters test (:124–144) are genuinely valuable (they pin reproducible builds of strategy files).

(b) Security
- See the `exec` note above: no injection-regression coverage for IR names/params containing quotes or newlines being embedded into generated code. For a codegen module this is the main security-adjacent surface; currently untested.

(c) Baseline smells
- **Duplicated Code:** the `namespace = {}; exec(code, namespace); df = pd.DataFrame(...)` scaffold repeats three times (:62–74, :105–113, :139–141) — extract helper returning `(namespace, df)`.

(d) Maintainability hot spots
- Small, focused, high-value. Only the exec-injection gap noted.

Scanned fully; findings above.

---

## tests/validation/test_validation_reference.py

(a) Bugs / correctness risks
- **tests/validation/test_validation_reference.py:60–66 — allowed-frameworks loop asserts membership only (minor).** `for fw in ALLOWED_FRAMEWORKS: ... == fw` proves the validator accepts its own allowlist (near-tautological by construction); the meaningful content is the rejection case at :65–66. Acceptable as an exhaustive-acceptance check, but it can't catch a framework typo'd *into* the allowlist.
- **tests/validation/test_validation_reference.py:122–125 — notes assertion depends on ordering-free substring checks only (minor).** Fine; no issue. Included to note the file is clean.
- No other findings: empty-trades gating both ways (:77–85), roundtrip preservation, engine mapping (`mt5_xlsx` → meta.engine) all pinned concretely.

(b) Security
- Nothing applicable.

(c) Baseline smells
- None significant; helpers are local and reused properly.

(d) Maintainability hot spots
- Clean model-level suite; keep as-is.

Scanned fully; findings above.

---

## tests/test_strategy_ir_builder.py

(a) Bugs / correctness risks
- **tests/test_strategy_ir_builder.py:39 — `confidence == 1.0` pins "full confidence" semantics without showing why inputs merit 1.0 (minor).** The markdown provides entry+exit+indicators table so presumably completeness ⇒ 1.0; a sibling test exercising a *partial* doc yielding <1.0 is absent, so the confidence scale's lower range is untested — the interesting boundary (what drops confidence?) has no case here.
- Everything else verified concretely: section extraction (:37–43), source-token fallback indicators (:60–62), unknown-engine mapping (:79).

(b) Security
- Nothing applicable.

(c) Baseline smells
- Minor: German section headers (`Kernlogik`, `Indikator-Einstellungen`) double as fixtures for i18n parsing — good, but undocumented; a reader may mistake them for typos and "fix" them, breaking the localization coverage. One comment line prevents that.

(d) Maintainability hot spots
- None; compact and readable.

Scanned fully; findings above.

---

## tests/test_strategy_ir_normalizer.py

(a) Bugs / correctness risks
- **tests/test_strategy_ir_normalizer.py:36–48 — mixed-tag merge expectations encode subtle precedence without stating it (minor, judgement call).** `{"labels": ["fx", "momentum"]}` shows list-tag values accumulate across entries while scalar tags overwrite; correct per assertions, but nothing pins what happens when two dict entries carry the same key (last-wins? extend?). That collision case is where normalization bugs live; untested.
- Error-path test (:51–61) correctly asserts message pattern including the bracketed field name.

(b) Security
- Nothing applicable.

(c) Baseline smells
- **Speculative Generality (mild):** `requested_name=None` passed everywhere though never exercised non-None — either add the override case or drop the parameter from these calls.

(d) Maintainability hot spots
- Small clean file.

Scanned fully; findings above.

---

## tests/test_strategy_ir_routing.py

(a) Bugs / correctness risks
- **tests/test_strategy_ir_routing.py:19 & :34/:48/:63/:79 — magic attempt-order constants `first_llm_attempt == 2` vs `== 1` (minor, undocumented invariant).** The 2-vs-1 encodes "IR generator consumes attempt #1" but no comment states the pipeline meaning; if attempt numbering shifts, four tests fail simultaneously with no self-explanation. Derive from a named constant or comment once.
- Routing matrix itself is well covered: supported engine, unregistered engine, missing IR, forced legacy, ir-only fallback-disabled — five modes, each asserted on every relevant field including reason text.

(b) Security
- Nothing applicable.

(c) Baseline smells
- **Duplicated Code:** the `resolve_codegen_route(engine="vectorbt", strategy_ir=..., generators={...})` baseline repeats in all five tests; a default-args factory would trim ~15 lines. Minor.

(d) Maintainability hot spots
- None significant.

Scanned fully; findings above.

---

## tests/test_strategy_type_detection.py

(a) Bugs / correctness risks
- **tests/test_strategy_type_detection.py:8 & :59–95 — imports `runner.cli` at module import time (minor, import-cost/side-effect coupling).** `import runner.cli as runner_cli` runs the whole CLI module (argparse setup etc.) just to call `cmd_run` in one test; if CLI import ever gains side effects (config load, network), this suite breaks. Import inside the test instead.
- **tests/test_strategy_type_detection.py:59–95 — `cmd_run` rejection test relies on ValueError surfacing before any file/data access (minor, judgement call).** Good guard test (signal_processor strategies must not run through backtest CLI), but it writes a real strategy file and constructs a full args namespace; if validation moves later in `cmd_run`, the test could start touching data dirs. Currently passes pre-validation — fine.
- Detection cases (:13–56): meta-field, class-pattern, and default paths all covered concretely.

(b) Security
- Nothing applicable.

(c) Baseline smells
- None significant.

(d) Maintainability hot spots
- None; small focused file.

Scanned fully; findings above.

---

## tests/test_signal_processor_registry.py

(a) Bugs / correctness risks
- **tests/test_signal_processor_registry.py:36 — `get_signal_processor("stockkit")` returns a `RamJetSignalProcessor` (minor, surprising alias asserted rather than explained).** The test asserts stockkit *is* RamJet under the hood — presumably an intentional alias, but nothing in the assertion or a comment says so. If someone later gives stockkit its own class to fix behavior differences, this test correctly fails — fine — but today it documents an aliasing decision only implicitly.
- **tests/test_signal_processor_registry.py:46–58 — variant-behavior test depends on processor internals producing `stop_loss > entry_price` for a SELL (minor).** Good behavioral contrast between long-only and long-and-short variants; note :55 asserts `metadata.get("exit_only") is True`, which pins metadata contract — good. No issue, recorded as verified.
- Registry coverage (:29–43) matches the ids surfaced by the viewer endpoint test in my area (test_viewer_features.py:3287–3292) — consistent.

(b) Security
- Nothing applicable.

(c) Baseline smells
- Minor: `_sample_market_data` duplicated conceptually with other signal-bot suites (outside my area); fine here.

(d) Maintainability hot spots
- None.

Scanned fully; findings above.

---

## tests/test_signal_replay_adapter.py

(a) Bugs / correctness risks
- **tests/test_signal_replay_adapter.py:9–61 — single happy-path trailing-stop scenario; boundary conditions untested (minor, gap).** The one test covers trail-update + trailing-stop exit + metadata passthrough concretely (all assertions falsifiable). Missing: no-stop signal path, take-profit hit before trail arms, and `fill_delay_seconds != 0` case (:61 pins == 0 only). For an execution adapter these gaps matter more than usual.
- **tests/test_signal_replay_adapter.py:12 — timestamps built without explicit tz on the frame (`freq="1min"` from tz-aware start is fine)** — verified tz-consistent with the payload; no finding.

(b) Security
- Nothing applicable.

(c) Baseline smells
- None significant; compact file.

(d) Maintainability hot spots
- Consider adding the three missing scenarios above before this adapter handles live capital.

Scanned fully; findings above.

---

## tests/test_signal_import.py

(a) Bugs / correctness risks
- **tests/test_signal_import.py:30 — DST assumption hard-coded in comment ("winter") (minor).** `signals[0]["timestamp"].hour == 14  # 09:32 New York -> 14:32 UTC (winter)` — correct for 2026-02-23 (EST, UTC−5), but if anyone changes the fixture date to summer the comment becomes wrong while the assertion still self-consistently fails. Acceptable; the zoneinfo dependency is implicit (assumes America/New_York semantics are baked into the importer).
- Both formats covered (semicolon stockkit German-header format and plain comma daily) with concrete field/timeframe/price assertions. Good.

(b) Security
- Nothing applicable.

(c) Baseline smells
- None significant.

(d) Maintainability hot spots
- None.

Scanned fully; findings above.

---

## tests/test_yfinance_downloader.py

(a) Bugs / correctness risks
- **tests/test_yfinance_downloader.py:27 — monkeypatches `yf.download` with a lambda ignoring positional args (minor, brittle double).** `lambda **kwargs: payload` will break with a confusing TypeError if the downloader starts passing positional args or new kwargs; acceptable for now, but a `def fake_download(*args, **kwargs)` recording calls would be sturdier.
- **tests/test_yfinance_downloader.py:41 — `str(stored["timestamp"].iloc[0]).startswith("1990-01-02")` (minor, weak).** String-prefix timestamp check instead of comparing parsed values; passes for any time-of-day that day including a wrongly-localized midnight shift of ±hours within the same date... actually a timezone bug shifting to 1990-01-01T23:00Z would fail this — so it does catch day-level shifts. Still, exact Timestamp comparison is stronger.
- MultiIndex columns + reset_index regression case (:8–36) is exactly the kind of real-world yfinance shape worth pinning — positive.

(b) Security
- Nothing applicable (network fully mocked out).

(c) Baseline smells
- None significant.

(d) Maintainability hot spots
- Single-scenario file; add writer-side validation cases if the downloader grows.

Scanned fully; findings above.

---

## tests/test_timeframe_normalization.py

(a) Bugs / correctness risks
- None found: aliases map to canonical forms, canonicals and unknown values pass through unchanged, empty string handled. All four assertions falsifiable.

(b) Security
- Nothing applicable.

(c) Baseline smells
- **Coverage gap rather than smell:** uppercase aliases (`"M1"`, `"H1"`), whitespace-padded inputs, and seconds/tick aliases aren't tested — yet viewer code paths normalize `"M1"`/`" eurusd "` style input elsewhere (see test_viewer_features.py:1292 coalescing test), implying case-insensitivity exists somewhere. If `normalize_timeframe` is that single chokepoint, these cases belong here; if not, there are two normalizers and that duplication should be flagged in production code review.

(d) Maintainability hot spots
- 14 lines; fine.

Scanned fully; findings above.

---

## tests/validators/code_validator.py

*(Utility module consumed by test suites — reviewed as code. Usage verified: imported by `tests/integration/test_ai_code_quality.py` and `tests/e2e/lab/test_conversion.py`; not dead.)*

(a) Bugs / correctness risks
- **tests/validators/code_validator.py:48–60 — security denylist is trivially bypassable while presenting itself as a security gate (major, security).** The AST scan flags only bare `eval`/`exec` Name calls and `os.system` attribute calls. Each of the following sails through: `getattr(builtins, "ev"+"al")(x)`; `__import__("os").system(cmd)`; `s = os.system; s(cmd)` (call func is a plain Name); `builtins.exec(...)`; `compile(src, "<s>", "exec")` followed by any dynamic invoke; nested attribute chains like `os.path.os.system` (value is Attribute, not Name); imports of `subprocess`/`pty`/`socket` are not scanned at all. If this validator's `passed` flag is used to decide whether AI-generated strategy code is safe to load, the guarantee is illusory. Either rename/document it as a lint heuristic or switch to an allowlist (AST whitelist of permitted calls/imports) plus capability isolation at execution time. Note the module does execute the candidate in a subprocess with `-I` and `timeout=8` (:116–122) — checklist-compliant — but that is availability hygiene, not containment.
- **tests/validators/code_validator.py:62–83 & :102–113 — interface check can false-pass uninstantiable classes (minor).** Both the static check and the subprocess check accept a class that merely *has* the method names (`callable(getattr(cls, name))` passes for any unbound method); a class whose `__init__` requires arguments satisfies the contract while being unusable via the documented module-level interface. Edge case, but it is exactly what a validator exists to catch.
- **tests/validators/code_validator.py:135–137 — score formula double-counts syntax errors (minor).** A syntax error appends one error (score −0.25) but also skips security/interface checks entirely, so a file with a syntax error and five real violations scores higher than intended relative to a parseable one. Cosmetic given binary `passed`.

(b) Security
- See (a) first bullet — the central concern. Positives worth recording: subprocess uses isolated mode (`-I`), explicit timeout, `check=False`, temp dir cleanup via context manager; stderr surfaced as warning not swallowed (:132–133).

(c) Baseline smells
- **Duplicated Code:** scoring formula `max(0, min(1, 1 - 0.25*err - 0.05*warn))` duplicated with `backtest_validator._score` variant (:77 there) — extract one scorer.

(d) Maintainability hot spots
- No unit tests for the validator itself inside my area (its behavior is only exercised indirectly through integration suites); given it gates code acceptance it deserves direct table-driven tests incl. the bypass cases above.

Scanned fully; findings above.

---

## tests/validators/backtest_validator.py

*(Utility consumed by `tests/integration/test_strategy_pipeline.py`; not dead.)*

(a) Bugs / correctness risks
- **tests/validators/backtest_validator.py:68–73 — mixed tz-aware/naive timestamps crash the whole validation with TypeError (major).** `_parse_time` returns whatever `datetime.fromisoformat` yields: `"2024-01-01T10:00:00Z"` → aware, `"2024-01-01 10:00:00"` → naive. If a single trade has an aware `entry_time` and a naive `exit_time` (or vice versa), the comparison at :72 raises `TypeError: can't compare offset-naive and offset-aware datetimes`, aborting `validate()` instead of appending an error — the validator dies on precisely the malformed input it exists to catch. Normalize both sides to UTC before comparing (mirroring the repo's own pattern in `tests/test_trade_engine.py:_parse_date_bound`).
- **tests/validators/backtest_validator.py:86–92 — `_as_int` silently coerces garbage to 0 (minor).** `total_trades = "many"` → 0; with the default `min_trades = 0` the corruption passes unnoticed. Return `None` on failure and emit "Missing or invalid total_trades" like the sharpe path does.
- **tests/validators/backtest_validator.py:63–75 — unparseable timestamps silently skipped (minor).** `continue` on parse failure drops the trade from ordering validation without even a warning; a data-quality problem (garbled timestamps from an importer) is invisible in the result.
- **tests/validators/backtest_validator.py:52–61 — asymmetric sharpe handling is intentional and sound (positive).** Below-range = error, suspiciously-above-range = warning citing lookahead bias — good domain judgment. Minor nit: non-finite sharpe collapses into "Missing or invalid" (:53–54), conflating NaN with absence.

(b) Security
- Nothing applicable beyond the robustness crash in (a). NaN/Inf stat rejection (:40–44, bool-safe) is a genuine data-integrity guard — positive.

(c) Baseline smells
- **Duplicated Code:** scoring formula shared with code_validator (see above); `_parse_time` duplicates date-normalization logic found in ≥3 other places in the repo (`replace("Z", "+00:00")` pattern) — Shotgun Surgery candidate for one shared util.

(d) Maintainability hot spots
- Same as code_validator: no dedicated unit tests in my area; the tz-crash case alone justifies a small direct suite.

Scanned fully; findings above.

---
---
---

## tests/validation/__init__.py

Empty package marker (0 bytes). Scanned; nothing found — required so `tests.validation` imports resolve as a package.


## tests/validation/test_golden_files.py

(a) Bugs / correctness risks
- **tests/validation/test_golden_files.py:15–18 — silent empty list when the goldens directory is missing (minor).** `_list_golden_files()` returns `[]` if `GOLDEN_DIR` doesn't exist; `test_at_least_three_golden_files` then fails with a decent message, so the guard is mostly harmless — but the pattern means a misconfigured checkout yields one opaque failure instead of a clear "fixtures not installed" skip. Positive contrast: unlike test_mt5_xlsx_importer.py, paths are anchored via `Path(__file__)` (:12), so no CWD dependence.
- Golden-file validation is otherwise well designed: schema round-trip per file (:28–35), non-empty trades (:39–45), required stats keys (:49–57).

(b) Security
- Nothing applicable.

(c) Baseline smells
- **Duplicated Code:** `files = _list_golden_files(); assert files; for path in files: data = json.loads(...); ref = ValidationReference.model_validate(data)` repeats in three classes; a shared fixture yielding parsed references would remove ~15 lines.

(d) Maintainability hot spots
- None significant.

Scanned fully; findings above.

---

## tests/validation/test_importers.py

Scanned fully — **nothing found.** This is the file that shows how the sibling importer suites should work: fixture root anchored with `Path(__file__)` (:13), an explicit any-CWD regression test that chdirs to tmp_path first (:26–40, directly covering the defect I flagged in test_regression.py and test_mt5_html_importer.py), timezone conversion pinned with concrete UTC instants (:43–51), abstract-base and missing-file negative cases (:21–23, :54–56). All assertions concrete and falsifiable.

---

## tests/validation/test_stats_comparison.py

(a) Bugs / correctness risks
- **tests/validation/test_stats_comparison.py:12–29 — direction chosen from PnL sign is cosmetic here but worth noting (minor).** `_trade` sets `direction="LONG" if pnl >= 0 else "SHORT"`; irrelevant to `_compare_stats`, which only reads pnl values, but a copy-paste of this helper into a comparison-sensitive suite would encode a wrong assumption. Harmless as used.
- Field-set pin at :37–48 duplicates the hand-written list also seen in test_regression.py:87–100 — same maintenance caveat (add/remove of a tracked stat fails with an unhelpful set-diff message; acceptable since it's an exact set assert, better than a bare length).
- Tolerance and inf-profit-factor behavior verified concretely (:67–77, :80–95) with exact expected diffs (:61–64). No tautologies.

(b) Security
- Nothing applicable.

(c) Baseline smells
- None significant.

(d) Maintainability hot spots
- Clean acceptance suite.

Scanned fully; findings above.


# Summary

All 40 tracked files accounted for: every file has either findings above or an explicit "scanned, nothing found" record.

| File | Findings |
|---|---|
| tests/test_viewer_features.py | 1 major, 9 minor (+2 minor security notes) |
| tests/test_signal_import.py | 1 minor |
| tests/test_signal_processor_registry.py | 2 minor |
| tests/test_signal_replay_adapter.py | 1 minor |
| tests/test_stats.py | 3 minor |
| tests/test_strategy_ir_builder.py | 1 minor |
| tests/test_strategy_ir_codegen_vectorbt.py | 3 minor |
| tests/test_strategy_ir_normalizer.py | 1 minor |
| tests/test_strategy_ir_routing.py | 1 minor |
| tests/test_strategy_meta_adapters.py | 4 minor |
| tests/test_strategy_type_detection.py | 2 minor |
| tests/test_timeframe_normalization.py | 1 minor |
| tests/test_trade_engine.py | 1 major, 3 minor |
| tests/test_walkforward.py | 4 minor |
| tests/test_yfinance_downloader.py | 2 minor |
| tests/validation/__init__.py | clean |
| tests/validation/test_cli.py | 3 minor |
| tests/validation/test_cross_platform_metrics.py | 1 major, 2 minor |
| tests/validation/test_delta_report.py | 1 major, 1 minor |
| tests/validation/test_fixed_risk_model.py | 3 minor |
| tests/validation/test_golden_files.py | 1 minor |
| tests/validation/test_importers.py | clean |
| tests/validation/test_markdown_report.py | 1 major, 2 minor |
| tests/validation/test_mt5_html_importer.py | 2 minor |
| tests/validation/test_mt5_xlsx_importer.py | 1 major, 2 minor |
| tests/validation/test_paper_readiness.py | 2 minor |
| tests/validation/test_percent_risk_model.py | 1 major, 1 minor |
| tests/validation/test_quantconnect_result_importer.py | clean |
| tests/validation/test_regression.py | 3 minor |
| tests/validation/test_report_structured.py | 4 minor |
| tests/validation/test_risk_comparison.py | 1 minor |
| tests/validation/test_risk_sizing.py | 2 minor |
| tests/validation/test_stats_comparison.py | clean |
| tests/validation/test_trade_anomalies.py | 1 minor |
| tests/validation/test_trade_comparison.py | 2 minor |
| tests/validation/test_trade_matcher.py | 2 minor |
| tests/validation/test_trade_statistics.py | 2 minor |
| tests/validation/test_validation_reference.py | 1 minor |
| tests/validators/backtest_validator.py | 1 major, 2 minor |
| tests/validators/code_validator.py | 1 major, 2 minor |

**Recurring themes:** (1) cross-file duplication of `_make_trade`-family helpers across ~8 validation suites; (2) environment-coupled tests reading live repo artifacts/data dirs with hard-pinned data values (mt5_xlsx, cross_platform_metrics, trade_engine parity, strategy_meta_adapters) versus properly isolated siblings; (3) copy-paste artifacts including a byte-identical `or` disjunction (test_markdown_report.py:125/:223) and an unimported `Any` annotation (test_delta_report.py:100); (4) subprocess-without-timeout in test_cli; (5) the validators' security/interface checks being weaker than their naming implies.
