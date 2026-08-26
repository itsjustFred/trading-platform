## tests-b

**Panel:** model ox-alpha; three blind, independent reviewers (r1, r2, r3), synthesized by ox-alpha.
Contradictions between reviewers (and against the gpt-5.6-luna reference report, consulted only for this chunk's files) were settled by reading the cited source under `tests/`, not by vote; every settlement is annotated inline as **[SETTLED]** or **[DISPUTED — settled: …]**. Findings were merged purely from the reviewers' citations first; the bounded verification pass (8 spot-checks) is logged at the bottom.

**Severity conventions:** counted findings are (a) bugs/correctness risks and (b) security issues, matching the reviewers' own tallies. Fowler baseline smells (c) and maintainability hot spots (d) are preserved per file but uncounted, as in the reviewer reports. Every finding keeps its original `file:line` citation(s); where reviewer line ranges differed slightly, the union is cited.

**Coverage:** chunk B = the alphabetical middle of the root-level tracked `tests/` entries, starting at `tests/test_backtest_grid.py` and ending at `tests/test_signal_bots_risk.py` = **49 tracked files**: 48 pytest modules + one non-module script (`compare_metrics_quantstats.py`, unreviewed by the panel — flagged; full reconciliation in the coverage statement at the bottom). All three reviewers claim full-chunk scans of the test modules, but **r2's report contains no sections for `tests/test_scheduler_signal_bot_health.py`, `tests/test_signal_bot_health_check.py`, or `tests/test_request_logging_middleware.py`** (it claims 49 files read; its headers enumerate 45) — those three files are covered here from r1 + r3 only.

---

### tests/test_backtest_grid.py

- **[B-01] minor — CLI command checks assert token adjacency, not argv structure** *(verified)* — `tests/test_backtest_grid.py:39-47`: assertions on `" ".join(cmd)` (`"-m runner.cli run" in joined`) pass under argument reorderings/duplications that happen to serialize adjacently. Assert exact list slices of `cmd`. **[SETTLED — kept minor]** r1 noticed the same pattern but explicitly declined to file it ("combined with the `--skip-sqlite in cmd` list check the intent is covered"), r2 filed it minor, r3 found nothing; kept on r2's citation — the weakness is real but marginal.
- Smells (r2): the 14-kwarg `run_one_backtest(...)` call is duplicated verbatim at `:105-123` and `:139-157` — extract `_cell_kwargs(tmp_path)`.
- Otherwise clean (r1, r3): fan-out keeps one risk_pct across all 4 cells (`:88`); per-cell timeout/launch-failure isolation (`:94-161`) is good negative-path design; resolved-date regression captures kwargs through to `attach_daily_equity` (`:164-231`).

### tests/test_backtest_matrix.py

- **[B-02] minor — test pins the odd artifact name `matrix_matrix.csv`** — `tests/test_backtest_matrix.py:93`: prefix/suffix doubling ("matrix_" + "_matrix.csv") frozen as expected output; flag for a product-level rename rather than a test change (Mysterious Name inherited from production). (r3.)
- Smells (r2): `_write_strategy`/`_write_strategy_with_meta` overlap almost entirely — one function with optional meta would do; fake `run_backtest_grid` payload dict repeated three times (`:31-54`, `:110-118`).
- Otherwise clean (r1, r2, r3): strategy selection, engine-metadata pass-through, failure isolation asserted against real outputs written to tmp dirs.

### tests/test_backtest_matrix_runner.py

- **[B-03] minor — daily-rows assertion is only as strong as the test's own fixture** — `tests/test_backtest_matrix_runner.py:21-24`: the pre-written fake output JSON invents `stats.daily_returns` itself, then `result["daily_rows"]` is asserted through it; `daily_rows[0]["run_id"]` is weak relative to fixture control. The URL/id link assertions (`:67-69`, `matrix_node_id` `demo:AAPL:1h`, viewer URLs) are exact and valuable. (r3.)
- **[B-04] minor — 17-parameter call site mirrors the production signature verbatim** — `tests/test_backtest_matrix_runner.py:43-64`: Data Clump reflected from `_run_strategy_matrix_cell`; a config object would shrink both. (r3.)
- Otherwise clean (r2: single test, explicit kwargs).

### tests/test_cache_manager.py

- **[B-05] minor (accepted-practice note) — real `time.sleep(0.05)` inside the fake build** — `tests/test_cache_manager.py:132`: forces concurrency overlap in the Barrier-synchronized single-build test. r3 files it minor ("acceptable; alternative is an event-gated build"); r1 explicitly judges the same line a legitimate race-window widener, not a flake pattern. Kept at minor with both readings attached — no reviewer claims active breakage.
- Smells (r2): `fail_build_catalog` raising stub re-declared three times (`:166-169`, `:207-210`, `:268-271`); `_catalog_payload` factory exists yet raw-dict catalogs still hand-built in three tests.
- Verified non-findings (r1+r2+r3): hit/miss counters asserted via deltas (`:65-75`), defensive-copy proven by mutating the returned dict (`:67`), disk-read/merge normalization pinned field-by-field, autouse invalidate fixture prevents leakage.

### tests/test_capm.py

Scanned clean by all three reviewers (β=1.5, α annualized ×252, R²≈1, p-value bound; alignment intersection; minimum-observations guard — all exact).

### tests/test_catalog.py

No bug/security findings (r1+r2+r3). Smells: the two-month bid/ask tick DataFrame pair is rebuilt nearly verbatim in four tests (r2 cites `:104-122,150-168,195-213,246-270`; r3 cites `:100-143,146-186,189-239,242-283` — same smell, slightly different slice points) — extract a segment-fixture factory parameterized by symbol/timeframe. Positives: cached-metadata reuse proven negatively (`read_parquet_summary` forbidden after caching), legacy flat exports, segmented grouping in both layouts, cross-source merge to "mixed", alias normalization.

### tests/test_ctrader_adapter.py

- **[B-06] minor — magic payload-type integers as fragile test doubles** *(verified)* — `tests/test_ctrader_adapter.py:105-107` sets `adapter._error_payload_type = 100` while the sibling negative test (`:131`) uses `101`; r3 adds `:129-132` (`_heartbeat_payload_type = -1` family). Arbitrary stand-ins for protobuf enums; if production ever compares a real enum value colliding with these, fakes match unrelated branches. Works today because production reads only these instance attributes. (r1 + r3; r2's hot-spot note on white-box coupling corroborates.)
- **[B-07] minor — micro-second timing budgets** — `tests/test_ctrader_adapter.py:78` (`connect_timeout_seconds=0.01`) and `:511` (`asyncio.wait_for(..., timeout=0.1)`): event-gated so failures are loud, but 10 ms can elapse early / 100 ms budgets can blow on loaded CI; prefer injecting an unresolvable event. (r2; r3 corroborates `:71-95` as low-risk-acceptable.)
- **[B-08] minor — `CTRADER_DEMO=true` success path never exercised** — `tests/test_ctrader_adapter.py:46-68`: only `false` and invalid values tested; the strict-parse *success* branch for `"true"` is unpinned while its failure twin is covered. (r2.)
- **[B-09] minor — `send_request` doubles as broker and refresh handler** — `tests/test_ctrader_adapter.py:360-377`: distinguished by `isinstance`; returning the sentinel `request` object on retry makes the success contract implicit; a named success value would document intent. (r2.)
- Smells: `CTraderLiveAdapter(app_id="app", ..., account_id="46736452")` constructor block repeats ~14–15× (r1 "~14 of 17", r2 "~15×", r3 "~15×"); `RefreshRequest`/`RefreshResponse` stub pair re-declared three times (`:258-265`, `:347-354`, `:403-409`) (r2).
- Hot spot (r2): white-box coupling to private state (`_execution_queue`, `_reconciliation_required`, `_terminal_auth_blockeds`, payload-type ints) — internal renames break many tests at once.
- Security positive (r2): token-refresh test redirects the `.accounts.env` write into `tmp_path` (`:247-256`) instead of touching any repo env file. Event-driven concurrency tests (`:334-390`, `:393-433`) rated exemplary by r2 (ordering enforced via asyncio Events, no sleeps); r1 confirms refresh/single-flight semantics genuinely tested and matching `live_runner/ctrader_adapter.py:676-680`.

### tests/test_ctrader_deployment.py

Scanned clean by all three reviewers. Ranking ("fast" beats "slow") verified against `live_runner/ctrader_deployment.py:47-56` sort keys; plan building asserts targeting/status/`source_backtest_id`. r1 note (not a finding): neither test exercises the `-inf` score exclusion branch (`:41-42`).

### tests/test_ctrader_live_fix.py

The worst offenders in the chunk — three vacuous "positions" tests whose names claim live-position-safety coverage:

- **[B-10] CRITICAL — `test_ctrader_positions_signed_qty_for_sell` is a pure arithmetic tautology** *(verified)* — `tests/test_ctrader_live_fix.py:268-281(282)`: builds local `expected = 5.0`, asserts `-expected < 0` and `expected > 0`; zero references to any adapter method. A regression flipping SELL positions positive passes. False confidence on a live-money path. **[SEVERITY SETTLED: critical]** (r3 critical, r1/r2 major — see verification log #1.)
- **[B-11] CRITICAL — `test_ctrader_positions_excludes_closed_positions` asserts two local literals differ** *(verified)* — `tests/test_ctrader_live_fix.py:284-295`: `position_status_open = 1`, `position_status_closed = 2`, asserts they differ "(per the adapter code)"; `get_positions` never invoked; closed-position filtering completely unprotected. **[SEVERITY SETTLED: critical]** (r3 critical, r1/r2 major — see verification log #1.)
- **[B-12] major — `test_ctrader_positions_symbol_id_mapping` tests dict assignment, not mapping logic** *(verified)* — `tests/test_ctrader_live_fix.py:254-265`: assigns `adapter._symbol_name_to_id = {...}` then reads the same attribute back. **[SEVERITY SETTLED: major]** (r2/r3 major, r1 minor — see verification log #1; unlike B-10/B-11 it touches a real adapter object, just no logic.)
- **[B-13] minor — duplicated imports** — `tests/test_ctrader_live_fix.py:9-16` (r1 cites `:14-16`): `datetime, timezone` and `typing.Any` imported twice; dead code from a paste seam showing the positions block was inserted wholesale. (All three.)
- **[B-14] minor — cross-process flock probe is timing-shaped** *(verified)* — `tests/test_ctrader_live_fix.py:207-228` (r3 focuses `:222`): sleeps a fixed 0.2 s then asserts `writer.poll() is None`; on a loaded machine the child may not yet have attempted the lock, making "blocked" pass vacuously (false negative), and the test errors (not skips) on platforms without `fcntl`. Deterministic alternative: gate on a sentinel/stdout written by the child after its first lock attempt. r1's milder reading ("legitimate blocking-I/O probe, mild flake vector") is folded in. **[REFERENCE DISAGREEMENT — settled: not adopted]** the gpt-5.6-luna reference (T4, `:209-223`, major) additionally claims the timed-out child is left alive (resource leak); verification log #7 shows the parent reaps via `writer.wait(timeout=2)` on the normal path, so that specific claim was not adopted — only the timing weakness stands.
- **[B-15] minor — hand-rolled patch/restore instead of `monkeypatch.setattr`** — `tests/test_ctrader_live_fix.py:162-167`: `mod._write_env_keys = boom` with manual save/try/finally. r1 judges it style-only (`finally` guarantees restore); r2/r3 note the leak window if interrupted between assignment and `try` entry, plus inconsistency with house style. (All three.)
- Smells: Speculative Generality / dead tests — the vacuous trio simulate coverage of `get_positions` that does not exist (r2, r3); three dedupe tests repeat the identical ~25-line two-execution setup (`:354-393`, `:406-450`, `:453-488` r1; r3 adds LiveSignal/LiveDeploymentConfig construction repetition `:301-348`, `:354-497`); another divergent copy of the `isolated_db` engine-redirect fixture (`:236-248`). **Copy-count disputed**: r1 says this is the suite's *third* `isolated_db` copy, r2 the *fifth*, r3 the *fourth* — reconciled in the verification log (the counts are compatible once per-file multi-copy definitions are counted differently).
- Hot spot (r2): file title says "edge-cases and helper behavior" while spanning three unrelated concerns (refresh persistence, DB roundtrip, dedupe script).
- Security positives (all three): `test_persisted_accounts_file_has_0600_permissions` (`:179-193`); crash-safety test proving no mixed token pair persists (`:141-176` r1, `:169-176` r2); subprocess spawned from argv list without shell (`:209-221`).
- Verified non-findings (r1+r2): refresh→auth→retry ordering by index (`:76-108`), single-flight refresh with persisted pair (`:111-138`) match production behavior.

### tests/test_daily_equity.py

No bug/security findings (r1+r2+r3). Arithmetic verified exact: 0.005 = 500/100000, 5% day return, cumulative 100800 (`:99`); NaN/inf skip (`:124-145`); invalid/missing exit-time skip (`:147-166`); preserve-vs-replace equity-curve precedence pinned from both directions (`:199-244`). Smells (r2, r3): dead `if __name__ == "__main__": pytest.main(...)` tail (`:268-269`) — never executes under the repo's pytest runner; inline `Trade(id=2/3, ...)` literals repeat the full constructor three times where a `make_trade(pnl=..., pnl_override=...)` variant would do. r1's epsilon note (`:89`, `:175`: absolute `1e-10` instead of `pytest.approx` — "fine for these magnitudes") folded in as a style remark, not a counted finding.

### tests/test_database_init.py

- **[B-16] minor — cosmetic `DATA_DIR` patch without `ENGINE` rebuild** — `tests/test_database_init.py:17`: init_db's `create_all`/migrate are faked anyway so nothing touches disk, but the setup suggests isolation it doesn't provide. (r3; trivial.) r2/r1 scanned clean — no conflict, just depth difference.

### tests/test_database_migrations.py

- **[B-17] MAJOR — un-isolated test writes the developer/CI's real database** *(verified)* — `tests/test_database_migrations.py:33-37(59)` (`test_backtest_record_equity_curve_persistence`): bare `init_db()` with **no** isolation fixture while every sibling uses `isolated_db`; conftest pins `DATA_DIR` to the repo's own `data/`, so each run permanently deposits a new `test_equity_curve_record_*` row into the real `data/backtests.db`, runs schema migrations against real data, and depends on whatever is already there. Test-order and xdist-concurrency hazards; can also mask migration failures because the real DB may already be migrated. (All three reviewers concur; strongest consensus major in the chunk alongside B-10/B-11.)
- **[B-18] minor — "concurrent" test is entirely sequential** — `tests/test_database_migrations.py:232` with interception at `:245-262(264)`: simulates the lost-update window by intercepting one pre-check SELECT on `database.Session.exec`; nothing exercises threads/processes despite the Finding-6 framing. Legitimate technique, misleading name. Also flagged by r1 as over-broad matcher (`"discord_message_id" in stmt_text and "select" in stmt_text` will swallow other selects mentioning the column as production queries evolve). (r2 + r1.)
- **[B-19] minor — hand-rolled patch/restore of `database.Session.exec`** — `tests/test_database_migrations.py:264-269`: try/finally instead of `monkeypatch.setattr`; a mid-stub KeyboardInterrupt between assignment and `try` leaks the patch into other tests; class-level patch also affects all sessions created during the window (single-threaded here). (r2 + r3.)
- **[B-20] minor — SQLite ≥ 3.35 environmental assumption** — `tests/test_database_migrations.py:445`: `ALTER TABLE backtests DROP COLUMN equity_curve` errors opaquely on older bundled SQLite rather than skipping; worth a version gate. (r1 + r2.)
- **[B-21] minor — `_DB_INITIALIZED = False` mutated directly with no restore** — `tests/test_database_migrations.py:432,487,519,547,568` (r3): module global left `False` after these tests return, so any later `init_db()`-dependent code re-runs migrations against whatever `ENGINE` is current; works today because every consumer sets its own engine. r2's hot-spot note corroborates the private-global coupling (`:76,157,359,432-433,487-488,519-520,547-548,568-569`).
- **[B-22] minor — top-up semantics encoded confusingly** — `tests/test_database_migrations.py:120` vs `:603`: two tests encode subtly different meanings for the same migrated counter ("skipped" rows re-reported as `"migrated": 1` on top-up) — not wrong, deserves a comment in production code. (r3.)
- Smells: four divergent local variants of "redirect shared.database at a tmp engine" (`isolated_db` `:141`, `legacy_db` `:346` per the verified grep, with additional raw `create_engine` sites at `:66`; r2's `:62-77`/`:345-360` slices are near-misses; only some patch `DB_PATH`; combined with copies in `test_live_execution_safety.py:623` which patches none of DB_PATH and must hand-re-attach the PRAGMA listener, and `test_ctrader_live_fix.py:237`) — Shotgun Surgery; missing an edit yields quietly non-isolated tests exactly like B-17 (r2+r3+r1). `_legacy_signals_db`/`_insert_legacy_signal` (`:320-342`) re-implements what `test_live_execution_safety.py:714-758` does with stricter DDL (r2+r1). Section banners reference "Finding 4/5/6/7/8" (`:181,229,281,317,439`) pointing at an external review document no longer linked (all three).
- Positives: fail-closed rollback of a corrupt legacy DB pinned (`:381-417` r1); dedupe-does-not-clobber broker_order_id (`:300-315`), backfill-does-not-overwrite (`:556-573`), skip_existing top-up semantics (`:576-612`) verified intentionally-correct (r2).

### tests/test_discord_signal_parsing.py

- **[B-23] minor — disjunctive assertion pins almost nothing** *(verified)* — `tests/test_discord_signal_parsing.py:43-49` (r3 focuses `:49`): `assert signal is None or signal.symbol != "AT"` holds whenever the parser returns anything other than a signal with symbol "AT", i.e. almost always; cannot distinguish "parser rejected the message" from "parser extracted a wrong-but-not-'AT' symbol" (e.g. "MARKET"). Pin one concrete expected outcome. (All three reviewers concur.)
- Otherwise clean: oversold-buy template, action-first symbol precedence over RAM JET label, RocketFuel momentum-reversal sell pinned exactly.

### tests/test_dolthub_manager.py

Scanned clean by all three reviewers. Single migration test asserts legacy flat export → `AAPL/1d.parquet` with content intact and source removed.

### tests/test_download_grid_market_data.py

- **[B-24] MAJOR — session-wide MagicMock leak onto the production module** *(verified)* — `tests/test_download_grid_market_data.py:92` (r1 cites `:90-92`): `dl.DukascopyNativeDownloader = MagicMock()` is a bare module-attribute assignment with no monkeypatch and no restore — the mock replaces the real downloader class for the remainder of the entire pytest session. The sibling file does the identical stub *correctly* via `monkeypatch.setattr` at `tests/test_grid_market_data_download.py:71`. One-line fix. (All three reviewers concur; r1 filed it minor, r2 and r3 major — see verification log for the recalibration.)
- **[B-25] minor — drifted test name: balance-row skipping never asserted** — `tests/test_download_grid_market_data.py:112-139`: `test_download_symbol_skip_balance_rows` only checks canonical OHLCV columns exist after normalization, asserting nothing about balance-row skipping. Misleading name; rename or assert the drop/keep explicitly. (r1.)
- **[B-26] minor — workers-clamp under-asserted** — `tests/test_download_grid_market_data.py:167-189` (r2/r3 focus `:167-189`, r1 notes within `:142-189`): `-1` → "clamped to 1" verified only by absence of crash (`SystemExit.code == 0`); nothing observes the clamp. Capture stdout or assert the planned-download payload. (All three.)
- **[B-27] minor — manual `sys.argv` save/restore + bare `except SystemExit`** — `tests/test_download_grid_market_data.py:149-164,173-188` (r1 adds `:142-189`): try/finally instead of `monkeypatch.setattr(sys, "argv", ...)`; `except SystemExit: assert e.code == 0` also swallows the case where main() raises before argparse exits / silently stops checking the exit path if SystemExit stops being raised. (All three.)
- **[B-28] minor — dead trailing `pytest.main` block** — `tests/test_download_grid_market_data.py:192-193`. (r2.)
- Smells: function-level re-imports in every test (`:15,34,56,…`); `tempfile.TemporaryDirectory()` throughout while siblings standardize on `tmp_path` (r2). This file substantially duplicates its sibling `test_grid_market_data_download.py` (normalization tables, download_symbol isolation, bid-column fill) — consolidate into the better one (r1 + r3).

### tests/test_dukascopy_discovery.py

- **[B-29] minor — near-tautological wall-clock probe assertion** *(verified)* — `tests/test_dukascopy_discovery.py:213-214`: `_year_probe_date(date.today().year) <= date.today()` (and month equivalent) shifts meaning across the year; can only fail around year boundaries or if the helper returns a future-dated probe late in December — which r3 notes *is* its point, making this borderline-tautology vs weak-but-purposeful. All three converge on: pin against a fixed reference date instead. Kept minor.
- Verified non-findings (r1+r2+r3): Saturday-closed fake-probe binary search (`:44-61`), skip-vs-force asserted both by output JSON and tracked probe calls (`:105-141`, `:138-140`), force overwrite (`:143-165`), persist-before-KeyboardInterrupt (`:168-202`) — excellent behavior tests with real JSON outputs.
- Smells: repeated `sys.argv` monkeypatch + `main()` invocation five times (r2 cites `:80-86,121-132,150-162,184-199`); doubled comment banner lines `:64-66` look like a merge artifact (r3).

### tests/test_dukascopy_downloader.py

No bug/security findings beyond the notes below (backoff math verified consistent: pause doubling → cap 10 s + 15 s cooldown ⇒ `sleep_calls == [10.0, 15.0]` at `:326-404`/`:396-404`; progress plumbing verified end-to-end).
- **[B-30] minor — throttle simulated by URL string** — `tests/test_dukascopy_downloader.py:151,371-375` (r3 adds the sys.modules-aiohttp swap family `:131,215,283,355`): `throttle_signal=url == "u2"` silently changes fixture semantics if production reorders/retries URLs; the `monkeypatch.setitem(sys.modules, "aiohttp", ...)` global-state hammer works because production imports aiohttp inside the function (`dukascopy_native.py:426`). (r2 + r3.)
- **[B-31] minor — global `argparse.ArgumentParser.parse_args` patch** — `tests/test_dukascopy_downloader.py:492-511`: affects every parser constructed during the test; safe here (single CLI call), narrower seam would be safer if main() grows. (r2.)
- Smells: four verbatim copies of the FakeConnector/FakeTimeout/FakeClientSession/fake-aiohttp scaffold (`:105-130`, `:189-215`, `:257-283`, `:329-355` — all three concur, ~90 removable lines); function-level re-imports of the downloader module in every test (`:16,103,187,…`) though module-level import is equivalent (r2); `native.asyncio.run(...)` attribute-path access noted obscure (r3); repeated `del session, semaphore, …` noise in fakes (r1).
- Security positive (r2): JVM discovery tests pin the trusted-JVM-path allow-list (`JFOREX_TRUSTED_JAVA_HOME`, `:452-472`) — regression coverage against loading arbitrary JVM DLLs.

### tests/test_grid_market_data_download.py

Scanned clean (all three). Notably `:71` patches `DukascopyNativeDownloader` correctly via `monkeypatch.setattr` — the pattern its sibling gets wrong (B-24). Precise bid-fill values (`:43-48`), m30 regression (`:51-54`), sort-key ordering incl. unknown-last (`:57-65`), per-timeframe failure isolation (`:68-86`). Smells: normalization/sort tests overlap heavily with the sibling file — consolidate to one home (r1 + r3); `parse_csv_list(...) == DEFAULT_SYMBOLS` (`:18`) couples default symbol order to parser de-dup semantics (r1).

### tests/test_grid_tester_sqlite.py

- **[B-32] MAJOR — three tests write permanent rows into the repo's real database** *(verified)* — `tests/test_grid_tester_sqlite.py:93-127,130-149,152-182` (r3 cites entry points `:95,132,154`; r1 `:93-149,:152-183`): bare `init_db()` + save/read with **no** isolation fixture — same defect class as B-17. Rows (`test_grid_sqlite_save_*`, `test_grid_func_*`, `grid_older_*`, `grid_newer_*`) persist across runs and other machines' state influences `get_grid_results(limit=100)`; ids keyed on `%H%M%S` wall-clock (`:98,134`) additionally collide within the same second (time-flaky PK conflicts). Only `test_run_strategy_grid_persists_to_sqlite` (`:37-91`) partially escapes via tmp JSON output — yet even it asserts through the unpatched real `get_grid_results` (`:85-90`). The uuid-suffixed slug there is evidence the authors knew about shared-state risk yet left these three unisolated. **Severity split disputed**: all three rate the real-DB writes major except r1, who filed the same observation minor — settled below.
- **[B-33] MAJOR — "newest first" test never checks order** *(verified)* — `tests/test_grid_tester_sqlite.py:152-182`: saves id_1 then id_2, asserts only membership (`id_1 in ids`, `id_2 in ids`); a regression returning arbitrary or ascending order passes. Should assert `ids.index(id_2) < ids.index(id_1)` (or filter to just these two). Name/docstring promise ordering that nothing verifies. (r2 + r3 major; r1 did not file it — covered here from two reviewers; the gpt-5.6-luna reference independently lists the same defect as t8, minor.)
- **[B-34] minor — limit-ignoring regression undetectable** — `tests/test_grid_tester_sqlite.py:185-202`: stubs `get_grid_results` and compares `load_grid_results(limit=1)` to a one-element fixture, so production ignoring `limit` entirely still passes; assert truncation with a two-element fixture. (r2.)
- Smell (Speculative Generality): wholesale `sys.modules` injection of fake `scipy`/`scipy.stats` with `norm` replaced by a plain *function* (`:39-44`) while real `scipy.stats.norm` is an object with methods (r1 adds: any code path calling `norm.ppf`/`norm.cdf` would get constant 0.5 rather than an error); if grid_tester ever touches another stats symbol the fake explodes opaquely (r2 + r1). Whole-`datetime` symbol replacement on the module with a `FixedDateTime` class is similarly broad (`:61-69`, works today since the module only calls `.now(tz)`).

### tests/test_llm_providers.py

- **[B-35] minor — broad `subprocess.run` patch on failure-path tests; successful stdout parsing unpinned** — `tests/test_llm_providers.py:53-61,64-77,80-93`: CopilotCLI failure-path tests patch global `subprocess.run`; any other shelling-out code in the import chain hits the stubs. None of these tests exercise a *successful* CLI generation path with output parsing (only failure modes + SDK-side success), leaving stdout-parsing of the CLI provider unpinned. (r1.)
- **[B-36] minor — timeout race margin** — `tests/test_llm_providers.py:220-227`: 0.05 s provider timeout vs 0.2 s fake sleep = 4× margin; event-loop timers are monotonic so r3 concludes sound, but r2 keeps a flake window on a pathologically loaded runner. Both readings preserved; kept minor either way. (r2 + r3.)
- **[B-37] minor — private `_timeout_seconds` assertion** — `tests/test_llm_providers.py:34`: couples to an internal attribute name for constructor plumbing. (r2.)
- Smells: `FakePermissionHandler`/`FakeClient`/`FakeSession` scaffold re-declared in five-to-six SDK tests (`:114-130,139-176,194-213,222-252,261-291` r2; r1 counts ~20-line blocks ×5) — one shared builder would halve the file (r1 + r2 + r3).
- Verified non-findings (r1+r2+r3): error paths pinned by exact messages ("executable not found", "timed out", "exit code 2", "empty response", AUTH_SESSION_ERROR); model allow-list rejection (`:191-216`); gpt-5→gpt-5.2 alias mapping through captured session kwargs (`:258-297`); nested payload extraction. No real binary invoked; NVIDIA key is a monkeypatched fake.

### tests/test_live_api.py

- **[B-38] minor — `load_live_account_stats_inputs` monkeypatched twice; second silently replaces first** *(verified)* — `tests/test_live_api.py:68-95` vs `:125-153(130)`: inside `test_live_endpoints_return_expected_payloads` the second registration makes the first — including its `slippage`/`slippage_bps` keys — dead setup; readers wrongly believe `/api/live/stats` assertions depend on slippage-bearing rows, and the endpoint's slippage pass-through is never exercised by the stats leg. Delete the first stub. (All three reviewers; the gpt-5.6-luna reference lists the same defect as t13, minor — agreement.)
- **[B-39] minor — WebSocket snapshot/update tests ride a real poll loop** — `tests/test_live_api.py:997-1064` & `:1193-1282(1283)`: correctness rests on the broker poller (`poll_seconds=0.05`, `viewer/main.py:3305-3323`) emitting the "update" frame before the context exits; no explicit synchronization — mildly flaky by construction under CI load; drive the queue directly instead. Verified not vacuous: `_stream_live_payload` really labels the second push "update". (r1 + r2 + r3.)
- **[B-40] minor — status-label disjunction** — `tests/test_live_api.py:991`: `assert signal["status"] in {"Waiting", "Pending"}` accepts two behaviors; pin the exact expected status. (r2.)
- **[B-41] minor — collection-time `sys.path.insert` hack** — `tests/test_live_api.py:20-21`: module-level path insert permanently pollutes interpreter state for every later module; redundant given pytest rootdir/conftest; can shadow modules. (r2 + r3.)
- **[B-42] minor — `>=`/`in` where mocked inputs guarantee exactly-one** — `tests/test_live_api.py:179-189`: `len(series) >= 1`, `len(trades) >= 1`, `deployment_id in ...` would miss accidental duplication bugs in payload building. (r2.)
- **[B-43] minor — win_rate semantics trap cemented by the suite** — `tests/test_live_api.py:800` (with `viewer/main.py:3743`): pins `win_rate == 20.0` for 5 executions with 1 win/1 loss — denominates by ALL executions including non-closing entries (1/5), not decided trades (1/2). The test faithfully encodes a production choice whose metric name invites misreading; flagging the semantics, not a wrong expectation. (r3; r1/r2 verified the same number correct against `viewer/main.py:3738-3772` — realized PnL books only round-trip pairs, drawdown 1100→880 = 220 = 20%.)
- **[B-44] minor — polluted deployment-id list locked in by weak assertions** — `tests/test_live_api.py:128,948` (with `viewer/main.py:3523-3532`): fixtures mirror production stuffing the account id and strategy names into the deployment-id list; tests assert only `deployment_id in payload["deployment_ids"]`, staying green as the list grows more polluted. (r3; r1 verified the odd stub mirrors production — merged as one finding about the *assertion weakness*.)
- **[B-45] minor — god-test spans ~8 endpoints** — `tests/test_live_api.py:24-209`: one failure masks the rest; split per endpoint. (r3.)
- Smells: execution-row dict literal appears three times in one test with drifting keys — the drift is exactly what hid B-38 (`:73-94` vs `:105-123` vs `:136-152`; all three concur, extract `_execution_row()`); RAMJET Service stub duplicated (`:231-244` vs `:430-443`, r2); every persistence function a lambda stub so CRUD flows (`:211-333`) verify routing/serialization only — `update_live_deployment_status` echoes whatever status it was given so the PUT test cannot catch a status-validation regression (Excessive Mocking; r1 judgement-call-minor, r3 major-smell — smell-tier disagreement preserved); every test re-imports `viewer.main` and re-stubs overlapping globals (~30 monkeypatch calls; Shotgun Surgery inherited from `viewer/main.py`'s ~11k-line design — r1 + r2); raw-dict payload assertions throughout (Primitive Obsession, r3); `_deployment_ids_for_account` mock readability note (Mysterious Name, r2).
- Security: none found — fake API keys only ("api-key"/"secret-key"); `PA3T0MQAGQ51` is an Alpaca paper id used as fixture data. Wrong-account Discord deployment → 400 pinned (`:426-466`).

### tests/test_live_config.py

Scanned clean (r1 + r2 + r3): valid parse asserted field-by-field; orphan-deployment warning captured structurally via `call_args.kwargs["extra"]["data"]["account_id"]`. r1's note that `assert_called_once` couples to logging-as-behavior is acceptable here since the warning is documented contract.

### tests/test_live_daemon.py

- **[B-46] minor — direct clearing of module-global block registries, no teardown** *(verified)* — `tests/test_live_daemon.py:78-79,140-143,213-215,232-233,242-243,370-371,395-396,421-422,438-441,456-459`: ten tests `.clear()` `_CTRADER_AUTH_BLOCKED_ACCOUNTS` / `_DISCORD_AUTH_BLOCKED_ACCOUNTS` / `_DISCORD_SERVICE_AUTH_BLOCKED_IDS` etc. directly; a mid-test failure leaks blocked-account state into later tests (order-dependent flakes). An autouse snapshot/reset fixture fixes all sites. Line-set variants across reviewers reconciled: r1 lists ten sites incl. `:232-233,:242-243`; r2 lists eight; r3 ten — union cited. (All three concur.)
- **[B-47] minor — sub-second timing budgets** — `tests/test_live_daemon.py:311,316,337-345` (r2/r3 add `:344`): `asyncio.wait_for(..., timeout=0.1)` around scheduling; event-gated so failures are loud spurious-timeouts rather than logic bugs; 100 ms is tight on loaded CI. (All three.)
- Smells: `_TerminalFailAdapter`/`_FailingAdapter`/`_TrackingAdapter` dataclasses repeat connect/disconnect counting (`:18-36`, `:81-95`) — one parameterized fake would do (r1); clear-globals prologue repeated (all three); three parallel daemon registries the tests must keep straight (Mysterious Name mirrored from production, r3).
- Hot spot (r2): direct manipulation of ~6 private module globals across most tests; renaming any breaks half the file.
- Security: none — fake tokens only. Positives (r2): failed connect does NOT thrash reconnects (`:39-74`); only cTrader credential changes clear auth blocks, Discord token change must not (`:202-228`); `:472-512` well-documented regression with forward-compatible guard (r3 concurs, plus `pytest.fail`-in-forbidden-hooks design praise). Verified non-findings (r1): the `if False: yield None` async-generator idiom (`:411-435`) is correct, not dead code; negative-persistence guard `:496-506` deliberate.

### tests/test_live_execution_safety.py

Overall (all three): high-quality mutation-driven suite; numeric expectations cross-checked against `risk/engine.py`, `risk/config.py`, `live_runner/daemon.py`, `live_runner/alpaca_adapter.py`, `live_runner/ctrader_adapter.py`, `shared/database.py` and all reproduce exactly (fallback_stop_pct=0.02; sizing `risk/(distance*pip)`; exposure estimate; fail-closed `_normalize_deployment_status`; SQLite pragmas). Findings:

- **[B-48] MAJOR — `isolated_db` fixture patches `DATA_DIR`, which does not locate the database** *(verified)* — `tests/test_live_execution_safety.py:632` (fixture `:622-635`): the engine URL comes from module-level `DB_PATH` built at import time (`shared/database.py:62-70`); `init_db()` touches `DATA_DIR` only for mkdir (`shared/database.py:834`). Isolation actually rests solely on the `ENGINE` patch; the `DATA_DIR` patch is dead and misleads readers into thinking path-based isolation exists — a future helper resolving paths from `DATA_DIR` (e.g. artifact writers, `shared/database.py:1082`) would silently write into the repo's real data dir. Remove it or make it load-bearing. (r2 alone; r1/r3 describe the same fixture but only as duplication — no contradiction, adopted from r2.)
- **[B-49] minor — reconcile window lower bound unpinned** — `tests/test_live_execution_safety.py:1893-1894`: `test_ctrader_reconcile_recovers_accepted_order_before_local_id_persistence` asserts only `lookup.fromTimestamp < signal_ts_ms < toTimestamp` while `_OrderListReq.__init__` initializes both to 0 — a regression scanning from epoch still passes; bound the window below as well. (r2.)
- **[B-50] minor — logger-handler surgery coupled to logging layout** *(verified)* — `tests/test_live_execution_safety.py:1432-1439`: picks the first handler that is a StreamHandler but not FileHandler on the module logger and swaps its stream via monkeypatch; hard-couples to `shared/logging.get_logger`'s exact layout (RotatingFileHandler + stdout StreamHandler, `propagate=False`) and JSON formatting; failure mode if config changes is spurious failure or asserting the wrong stream. `caplog`/dedicated handler sturdier. (All three concur.)
- **[B-51] minor — concurrency tests are WAL/environment-sensitive** — `tests/test_live_execution_safety.py:998-999,1354-1355` (r3 analyzes `:1002-1015` incl. the order-insensitive `sorted(...) == [False, True]` and strict `[True, True]` map results at `:1010` — deterministic by construction): ThreadPoolExecutor over SQLite WAL is sound on Linux/macOS (busy_timeout=30000, pragmas re-applied via the fixture listener at `:630`) but WAL journal mode can fail on exotic filesystems/network drives; environment-sensitive flake vector, worth a skip-marker guard. Kept minor (no reviewer claims active breakage).
- **[B-52] minor — explicit `ctrader_volume` override blesses under-minimum qty silently** — `tests/test_live_execution_safety.py:604` (with `live_runner/ctrader_adapter.py:759-761` checking the override before the qty floor): probably intentional ("explicit override wins"; r2 confirms fail-closed floor semantics match `ctrader_adapter.py:757-779`), but given neighboring error text about rejecting silent upsizing, the asymmetric enforcement deserves a documenting comment. (r1; r2 corroborates as intentional.)
- **[B-53] minor — reconcile backfill test's double-stub shape is inconsistent with siblings** — `tests/test_live_execution_safety.py:1727-1729(1738)`: fake response built as `type("ProtoOAOrderDetailsRes", (), {"deal": [deal]})` makes `deal` a class attribute (shared-state mutation risk if production ever appends to it — r3), the fake request has no `ctidTraderAccountId`/`orderId` fields so nothing verifies what was requested (the pagination sibling `:1781-1803` does), and it stubs raw `_send_request` while production calls `_send_request_with_refresh_retry` (`:1173`) — if the fetch method switched, the stub would silently never be called. (r1 + r3; r2's protobuf-fake hot spot corroborates.)
- **[B-54] minor — docstring overstates mutant-killing power** — `tests/test_live_execution_safety.py:233-261`: `test_alpaca_submit_order_blocks_non_positive_quantity` only exercises qty == 0; a mutant rejecting only strictly-negative qty passes; the comment claims more than the test kills. Test itself correct. (r3.)
- **[B-55] minor — compound assert hurts diagnosis only** — `tests/test_live_execution_safety.py:99-100`: fallback-stop compound assertion (`is not None and <`) is not a pure tautology (deleting the fallback fails clause one) but message-less compound makes diagnosis harder; split into two asserts. Judgement call; behavior coverage real. (r3.)
- **[B-56] minor — boundary at the broker volume floor untested** — `tests/test_live_execution_safety.py:601`: reject (0.07→70 units) and accept (5.0→5000) pinned but not the exact floor boundary (e.g. 0.1→100). Coverage gap, not a bug. (r3.)
- Smells: keyset-pagination harnesses duplicated between Alpaca and cTrader reconcile tests (`:1537-1590` vs `:1748-1808` — r1+r2; r3 adds a third copy `:1811-1898`, count disputed but the duplication itself agreed); redelivery trio repeats ~25 lines of client/monkeypatch setup each (`:382-444/:447-495/:498-537`, r2); duplicate `_NewOrderReq` fake protobuf classes inline twice (`:2015-2052` vs `:2094-2117`, r3); ~15–20+ LiveSignal construction blocks bypassing the existing `_live_signal()` factory (`:173`) — ~150 removable lines (all three); `_make_ctrader_adapter()` returns an undocumented `(module, adapter)` tuple used at ~15 call sites (r3).
- Hot spots: 2256 lines spanning four concerns (risk-engine boundaries, Alpaca adapter, cTrader reconciliation, DB migration/persistence durability) — Divergent Change; split per subsystem (all three). Protobuf faked via `type(...)` classes + SimpleNamespace enums (`:1727-1736`, `:1838-1882`, `:2015-2052`) — renames break many tests at once (r2).
- Verified non-findings (r1+r2): status-normalization DISABLED→UNKNOWN is deliberate fail-closed (`daemon.py:1716-1720`); RuntimeError path matches `alpaca_adapter.py:392-402`; no tautologies; act phases unwrapped; result-ordering of `executor.map` deterministic.

### tests/test_live_preflight.py

- **[B-57] minor — "default .accounts.env untouched" guard depends on host state** — `tests/test_live_preflight.py:342-344,363-365`: snapshots the real repo-root `.accounts.env` (via `preflight.DEFAULT_ACCOUNTS_ENV`) before/after; if the file is absent both reads yield None and non-interference isn't proven for a *delete+recreate*; if another test or concurrent process touches the file there is cross-test coupling, and the snapshot-compare is not race-safe under parallel execution. Monkeypatching `DEFAULT_ACCOUNTS_ENV` into tmp_path would be hermetic. (All three concur at minor; r3's inline self-correction — a newly created default file *would* fail the compare — preserved.)
- Security positives (r1 + r2): default-demo posture (`:19-21`), strict boolean parsing matrix incl. garbage values naming the variable (`:19-52`), numeric-account-id enforcement (`:121-148`); connectivity checks use clean fakes with monkeypatch (no network).

### tests/test_live_risk_sizing.py

- **[B-58] minor — sizing magnitude never checked** — `tests/test_live_risk_sizing.py:18-34`: `test_size_live_signal_sets_qty_for_valid_risk` asserts only `> 0`, although entry 100 / stop 98 / balance 10k / 1% risk gives a deterministic 50.0 via `risk/(distance*pip)` (same math pinned exactly in test_live_execution_safety.py:55-79); a sizing mutant producing half/double size passes. Add `pytest.approx(50.0)`. (r1; r2/r3 scanned clean — no contradiction.) Precomputed-qty passthrough (`:54-68`) and max_positions block verified against `risk/live.size_live_signal` (r1+r2+r3).

### tests/test_live_smoke.py

- **[B-59] minor — secret-leak guard greps field-name substrings in self-built data** *(verified)* — `tests/test_live_smoke.py:42-43`: `"api_key" not in str(payload).lower()` inspects data the test itself constructed that never contained credentials; cannot catch a future sensitive field named differently, while a value containing "key" elsewhere would false-fail. Fine as schema smoke; weak as leak guard. (r1 + r2 + r3 concur; security-positive intent preserved.)
- **[B-60] minor — discord-replay smoke's offline-safety is by construction only** — `tests/test_live_smoke.py:79-95`: runs real code paths with a 1 s timeout asserting fabricated order id "discord-smoke-order"; if someone later makes discord-replay hit Discord, this test attempts real I/O. A comment or explicit env guard would harden it. (r3.)
- **[B-61] minor — bare `except SystemExit` around help invocation** — `tests/test_live_smoke.py:47-50`: swallows argparse's exit without verifying the code; `pytest.raises(SystemExit)` would. (r2.)
- Smells: seven separate import statements from `scripts.live_smoke` (`:8-13`) instead of one grouped import (r2 counts seven, r1 five lines `:8-12` — cosmetic either way). Mode aliasing (`:59-76`) and help-text broker-minimum default pinned concretely (r1).

### tests/test_models.py

Smells only (all three): `test_file_roundtrip` uses `tempfile.TemporaryDirectory()` (`:187` / `:185-192`) while the rest of the area standardizes on pytest `tmp_path`. Otherwise clean: serialization round-trips, optional-field omission, winner/direction helpers, filename generation all concrete.

### tests/test_mt5_parity_artifact_loader.py

Scanned clean (r1 + r2 + r3). Bid-column backfill with canonical precedence (`:14-50`), m1_bid fallback selection (`:52-70`), EURUSD conversion 80 000 = 100 000/1.25 exact (`:73-80`), lot-step rounding (`:83-85`), shifted-stop anchoring pair with distinct exit reasons "signal" vs "stop_loss" (`:88-157`). Smell (minor): 4-row frame + entries/exits scaffold duplicated between `:88-119` and `:126-154`.

### tests/test_native_engine_execution.py

Scanned clean (all three): fixed-risk sizing 500/(100−95)=100.0 (`:41`) and MT5 tick-value sizing 500/(5·(1.0/0.01))=1.0 (`:62`) binary-exact; alias extraction pinned. Cosmetic: missing blank line between tests at `:42-43` (r1).

### tests/test_orchestrator.py

Scanned clean on behavior (layer ordering, run-all-layers-even-when-e2e-fails via fake `_run_layer`). Smell (Speculative Generality, r2): `_result()` always emits `passed=1, failed=…` but no assertion consumes pass/fail counts — unused shape. (r1 had noticed the same oddity as harmless.)

### tests/test_optimizer.py

No bug/security findings beyond assertion-strength minors below.
- **[B-62] minor — penalty mapping unpinned** — `tests/test_optimizer.py:474-491` (focus `:480`): `assert any(trial["penalty_applied"] ...)` never pins *which* trial was penalized; a regression penalizing window=4 instead of the zero-trade window=1 still passes. Assert params→penalty explicitly. (r2 + r3 concur.)
- **[B-63] minor — consumed dict view + no positivity check** — `tests/test_optimizer.py:579-580`: `sum(payload[...].values())` consumes the dict view — any later reuse silently sees an empty iterable; also no long-only positivity assertion on risk-parity weights. (r2.)
- **[B-64] minor — fake keyed on ISO-prefix string matching** — `tests/test_optimizer.py:453`: `config.start_date.startswith("2024-01-01T00:00:00")` couples the fake to production datetime serialization from `_split_backtest_period`; a formatting change misclassifies every call confusingly. Compare against the split boundary directly. (r2 + r3 concur.)
- **[B-65] minor — risk-parity test under-stubbed, relies on production fallback** — `tests/test_optimizer.py:537-583`: unlike siblings it stubs neither `ensure_backtest_data` nor coverage preflight; passes only because `_run_risk_parity_covariance_optimization` prefers returns derived from the base result (`runner/optimizer.py:544-548`) with ≥2 symbols of trades; if the fallback ever reaches the market-data branch the test hits the real data layer with an empty `data_dir`. One explicit stub or comment makes the dependency intentional. (r2.)
- Smells: `find_strategy_entry` SimpleNamespace stub re-typed six-to-seven times (r1 cites six sites `:289-296,:338-345,:374-381,:418-425,:502-509,:545-552`; r2 six with slightly different slices; r3 seven) plus `BACKTESTS_DIR`/`OPTIMIZATIONS_DIR` patching — extract `_standard_optimizer_mocks(...)` (~90 lines); Data Clumps `(backtests_dir, optimizations_dir, data_dir)` triple (r3).
- Hot spot (r2): patching `optimizer_module.optuna.create_study` while capturing the original attribute (`:431-439`, `:510-518`) hard-couples to Optuna factory/GridSampler semantics — an Optuna upgrade breaks four tests at once; consider a thin production seam.
- Positives (all three): forbidden-call guards pin study-creation-after-validation sequencing (`:278-406` incl. `ensure_calls[0]` kwargs); micro-range rejection (`:65-71`); inclusive upper bound at exactly 0.95 (`:41-47`); grid-search best selection window=3 on train AND OOS.

### tests/test_portfolio_optimizer_oos.py

- **[B-66] MAJOR — train_months story: dead knob enshrined as validated behavior** *(verified)* — two merged findings: `tests/test_portfolio_optimizer_oos.py:198-225` (`test_split_equity_curves_with_train_months`) documents `train_months` as "reserved but not yet implemented", asserts it is ignored (11 train points where 12 expected under the docstring), leaves a TODO; and `:509-554` (`test_portfolio_optimizer_with_train_months_oos_split`) promises an "18 months train" IS/OOS walk-forward in name/docstring but **never passes `train_months`** (only `test_months=6` at `:543-547`), asserting mere key presence (`"oos_sharpe" in result`, `"oos_split" in result`) — a regression putting every point in train (or test) still passes. Copy-paste error pair; misleading coverage. Either implement the parameter or mark it clearly unsupported. (r3 majors the pair; r1 and r2 file each half minor; reference T2-1 rates the ignored-contract half major — see verification log for settlement.)
- **[B-67] minor — mock_load keyed on call order** — `tests/test_portfolio_optimizer_oos.py:110-120(112-116),169-177(171-173),531-539(533-535)` (line variants across reviewers cited): strategies distinguished by `call_count[0] == 1`; if the optimizer changes iteration order or caches, strategy identities silently swap and assertions still pass (both curves upward-trending). Key the fake on `backtest_id`. (All three concur.)
- **[B-68] minor — Sharpe sign unpinned despite docstring promise** — `tests/test_portfolio_optimizer_oos.py:425-436`: "Should have positive Sharpe for positive trend" but body asserts only `isinstance(sharpe, float)` and `max_dd >= 0.0`; a sign-flip mutant making Sharpe always negative passes. (r1.)
- **[B-69] minor — single-curve split under-asserted** — `tests/test_portfolio_optimizer_oos.py:241`: only `len(test_curves[0]) > 0` where 12 monthly points with `test_months=3` yields exactly 4 (i ≥ 9); tolerates any boundary bug short of empty. (r2.)
- **[B-70] minor — 24-hour-wide timestamp window** — `tests/test_portfolio_optimizer_oos.py:37`: `1704067200 <= ts <= 1704153600` though `_parse_time_to_ts("2024-01-01T00:00:00Z")` is deterministically 1704067200.0 (tz-aware parse at `runner/portfolio_optimizer.py:37-38`); hides off-by-hours/tz bugs. Exact equality available. (r2 + r3 concur.)
- **[B-71] minor — `np.testing.assert_raises` instead of `pytest.raises`** — `tests/test_portfolio_optimizer_oos.py:503`: house style everywhere else is `pytest.raises` (better failure output, match support). (r2 + r3 concur.)
- **[B-72] minor — convoluted status compound reads like tautology trap** — `tests/test_portfolio_optimizer_oos.py:134,188`: `assert "status" not in result or result.get("status") != "error"` is logically fine but hard to read; simpler `result.get("status") != "error"`. (r3.)
- Smells: three near-identical mock_load/OOS harnesses (`:86-153,:155-195,:509-554`); `seconds_per_month`/`base_ts` literals repeated five times (r2 `:56,92-93,157-158,204-205,517-518`; r3 lists `:51,227,244` too); Speculative Generality — the reserved-but-unimplemented `train_months` parameter itself is speculative surface area the suite must keep explaining (r1+r2+r3); Mysterious Name on `:509`.
- Positives (r1+r2): split boundary math 12-train/gap/4-test reproduces exactly given the `train_end_ts >= test_start_ts` clamp (`:75-76`, `:222-223` vs `runner/portfolio_optimizer.py:48-110`); alignment-overlap semantics (`:274-320`); correlation-penalty sign regression guard (`:339-377`); path-traversal rejection with no-file-written follow-up (`:494-506`).

### tests/test_preflight.py

- **[B-73] minor — CLI tests mock `run_live_preflight` wholesale; config-path propagation unpinned** — `tests/test_preflight.py:164-171,180-188(189)`: exit-code mapping tested by design choice, but `mock_run`'s return value never asserts `config_path` propagation — a regression passing the wrong config path through would pass. Acceptable layering, note the gap. (r2 + r3 concur.)
- **[B-74] minor — error-message substring matches** — `tests/test_preflight.py:47-88`: all four `_validate_deployments` failure cases use `any("substring" in e ...)`; one exact match would catch accidental message concatenation/duplication. (r2.)
- **[B-75] minor — fragile YAML formatting + dead tail + unused import** — `tests/test_preflight.py:108-113,122-127,129-133` un-dedented triple-quoted YAML relying on leading-newline tolerance (r3); trailing `pytest.main` block `:224-225` dead under pytest (r2); `AsyncMock` imported at `:7` used nowhere (r2).
- Smells: config-writing boilerplate repeated; overlap with test_live_preflight.py covering similar ground — Divergent Change candidate, consider merging (r3).

### tests/test_qc_risk_parity_artifact_loader.py

Scanned clean (all three): TICKERS-ordered parquet universe in tmp_path, column order/values spot-checked exactly.

### tests/test_ramjet_signal_processor.py

Scanned clean (all three). SL/TP/TP1/trailing-config derived from ATR on synthetic data rather than hardcoded (`:54-70` / r2: computed from actual last close at `:60-61` — resilient yet falsifiable); BUY-only negative test (`:73-79`); default config.yaml pinned from repo tree (`:82-91`). Hot-spot note (r2): the committed-config coupling means any YAML tuning must update this test — worth a comment saying so.

### tests/test_request_logging_middleware.py

Scanned clean by all three reviewers (r2 has no section for this file — covered here from r1 + r3). Handler add/remove fixture with level restore (`:21-32`); method/path/status/duration payload + exc_info presence on 500s; quiet-polling paths log at DEBUG not INFO (`:70-82`); middleware logs avoid leaking bodies. No findings.

### tests/test_resample.py

- **[B-76] minor — tz-naive fixture timestamps** *(verified)* — `tests/test_resample.py:15,33`: `pd.date_range(...)` without tz while production-side fixtures in this area write UTC-stamped data; resampling naive vs aware can shift bucket edges across DST if resampling ever gains localization logic or runs in a non-UTC local zone. Low risk, inconsistent with repo tz-discipline. (r1 + r2 concur.)
- **[B-77] minor — `1h→4h` case lacks exact count** — `tests/test_resample.py:59-62`: only `0 < len ≤ n/4+1` where 500 hours ⇒ exactly 125 bars; weakest assertion in the file, unlike its siblings' exact counts. (r2.)
- Smells: `import numpy as np` inside both fixtures (`:16,34`) instead of top-level (r1+r2+r3); `tempfile` imported at `:3`, never used (r2).
- Verified non-findings: bar-count math 120→24 @5m, →8 @15m; OHLCV aggregation O=first/H=max/L=min/C=last verified bar-by-bar against source slice (`:64-73`); monotonic timestamps; invalid-timeframe rejection.

### tests/test_risk_consistency.py

- **[B-78] minor — tz-naive `datetime.now()` on LiveSignal** *(verified)* — `tests/test_risk_consistency.py:55,94`: every sibling uses `datetime.now(timezone.utc)`; sizing ignores the timestamp today so it's harmless here, but it normalizes naive usage inside live-signal construction — the very hazard the suite exists to police (UTC normalization pinned elsewhere, e.g. test_live_execution_safety.py:1200-1232). Use timezone-aware now. (All three reviewers; the gpt-5.6-luna reference independently lists the same defect — agreement.)
- **[B-79] minor — wrong-side-stop backtest asymmetry encoded as correct without a loud flag** — `tests/test_risk_consistency.py:110-130` (with the documented asymmetry block `:79-130`): backtest engine silently sizes positions using a stop on the wrong side of entry unless callers opt in via `metadata={"enforce_stop_direction": True}` (seen in test_risk_engine.py:108); the live engine rejects it. The test faithfully pins current behavior; flagged as an encoded-questionable-default deserving a loud production comment. (r3; r1/r2 treat the same asymmetry as verified-intentional — disagreement is about framing, not facts; kept minor per r3.)
- Verified non-findings: parity expectations 1000 = 1% of 100k; sizes 50.0 = 1000/(2×10), 20.0 = 1000/(5×10) reproduce exactly; identical defaults asserted field-by-field.

### tests/test_risk_engine.py

- **[B-80] minor — partial-exposure min() clamp unpinned** — `tests/test_risk_engine.py:57-89`: exposure case uses `current_exposure == max_total_exposure` exactly (boundary); no case exercises partial consumption (e.g. exposure 0.03 ⇒ risk_percent capped to remaining 0.02) so the min() clamp at `risk/engine.py:152` is unpinned by this file (covered indirectly elsewhere via broker-exposure estimation). (r2 alone; no contradiction.)
- Smells: `RiskEngine(RiskConfig(0.01, 0.05, 5, 2.0))` construction repeated four-to-five times (`:17-24,:58-65,:93-100,:128-135`) — `_engine()` already exists in test_live_execution_safety.py:44-52 and test_signal_bots_risk.py solved it with `_risk_engine()`: share one helper (r1+r2+r3 concur).
- Verified clean otherwise (r1+r2+r3): sizing 50.0 units exact, RR 2.0, exposure rejection reason, wrong-side stop gated behind `enforce_stop_direction` (`:92-124`), ATR fallback echo + `stop_source` (`:160`), decision-id formats, daemon qty=200 consistent with the safety-net file's identical scenario.

### tests/test_risk_parity_optimizer.py

- **[B-81] minor — winner-universe disjunction can't detect scoring flip** — `tests/test_risk_parity_optimizer.py:36`: `assert set(result["best_symbols"]) in [{"SPY","TLT"},{"SPY","GLD"}]` accepts either candidate; defensible given hand-picked returns, but a ranking regression flipping the winner arbitrarily (even to "last evaluated") passes as long as weights sum to 1. Pin the actual winner with a comment explaining why. (All three concur; r2 adds Mysterious Name framing.)
- Verified sound: weights ≥ 0, contributions sum to 1, max-min spread < 0.01, deterministic deduped universes in exact expected order.

### tests/test_runner_cli.py

- **[B-82] minor — args built as SimpleNamespace, parser never exercised** — `tests/test_runner_cli.py:63-78,101-116,139-157`: an argparse default change or new required flag keeps tests green while real CLI invocations break; one test going through the real parser closes the gap. (r2.)
- **[B-83] minor — exact float equality on total_return** — `tests/test_runner_cli.py:86`: `== 0.05` passes because the stat derives from integer-ish PnL division (5000/100000 canonical double); an equity-ratio-derived implementation (100000×1.05 → 0.050000000000000044) would fail. `pytest.approx`. (r3; r1 verified 0.05 correct for the dummy engine's curve — same fact, different concern.)
- Smells: 14-field `SimpleNamespace(...)` args block repeated three times — `_run_args(**overrides)` (r2); `_DummyEngine.validate_config` returning unused `[]` (Speculative Generality, trivial, r3).

### tests/test_scheduler_signal_bot_health.py

Covered from r1 + r3 only (r2 has no section for this file — see coverage note).

- **[B-84] MAJOR — error-detection test blind to error handling** *(verified)* — `tests/test_scheduler_signal_bot_health.py:36-37,62-63` (body `:42-63`; happy path `:15-39` milder): everything mocked and assertions are only `mock_check.called` + `mock_upsert.call_count >= 2` — healthy and unhealthy scenarios produce identical assertion sets; the job could record `"status": "ok"` even when bots report errors and `test_signal_bot_health_check_job_detects_errors` stays green. Assert recorded status/error fields on captured `mock_upsert.call_args`. (r3 major, r1 minor — severity dispute settled below; the gpt-5.6-luna reference lists the missing-assertions defect as T2-3, minor.)
- **[B-85] minor — import-smoke test asserts near-nothing** — `tests/test_scheduler_signal_bot_health.py:10-12(13)`: `callable(run)` only; harmless but tautology-class. Kept as import smoke by both reviewers who mention it.
- Smell: Excessive Mocking — the job's entire interaction with persistence is stubbed; fold real argument assertions into existing mocks rather than adding more tests (r3).

### tests/test_sentry_config.py

Scanned clean by all three reviewers — security-positive testing done right: DSN-less disable, enabled-without-DSN fails closed (`:16-18`), sample-rate validation, `send_default_pii` defaults False (`:46`), `before_send` redaction asserted across headers / extra keys / nested lists / message-body regex (`Authorization`/`api_key` redacted while `safe=yes` survives, `:50-79`), validated option pass-through, scoped capture context. Synthetic DSNs only; `_reset_for_tests()` usage is appropriate global-state hygiene (r2).

### tests/test_service_discovery.py

Scanned clean by all three reviewers. Env-file discovery maps three Discord bots' token-env-key/channel-id pairs via a tmp `.accounts.env`; `DEFAULT_ACCOUNTS_ENV` monkeypatched into tmp_path correctly (no host state touched); obviously fake placeholder tokens only.

### tests/test_shared_logging.py

Scanned clean by all three reviewers. JSON log formatter: fixed-epoch record pinned to exact timestamp string + regex; degraded LogRecord (empty module/funcName, bogus lineno) falls back to `<unknown>`/0 without crashing, exercised with deliberately corrupted fields (`:43-45`). Correct and focused.

### tests/test_signal_bot_health_check.py

Covered from r1 + r3 only (r2 has no section — see coverage note).

- **[B-86] minor — stdout grep couples to dict serialization order/quoting** — `tests/test_signal_bot_health_check.py:28-35` (r1 focuses `:28-35`): `'"status": "ok"' in captured.out` breaks silently if output switches to compact separators or reorders keys; prefer `json.loads(captured.out)` then assert fields. (r1.)
- **[B-87] minor — lower-bound registry assertion tolerates bloat** — `tests/test_signal_bot_health_check.py:22`: `len(result["results"]) >= 5` with a comment enumerating expected bots; registry duplication/bloat passes silently. Prefer exact set comparison. (r3.)
- Note (r3): depends on repo-tracked `signal_bots/*/config.yaml` files (`:18-26`) — environment-stable within the repo, acceptable. Registry-shape smoke checks `:6-25` reasonable (r1); the gpt-5.6-luna reference flags isolation concerns for `:6-25` (T2-4) which the panel did not corroborate — noted as unconfirmed reference entry, not adopted.

### tests/test_signal_bots_risk.py

- **[B-88] minor — near-vacuous importability test + odd config key** — `tests/test_signal_bots_risk.py:41-51` (r2 cites `:41-50`): `test_signal_processors_are_importable_with_process_signal` asserts only `callable(getattr(...))` on three processors — cannot fail short of deleting the method; keep only if intended as import smoke. Additionally `NitroSignalProcessor({"ram_jet": ...})` at `:45-47` is constructed under ram_jet's config section — if intentional config reuse it deserves a comment, otherwise it silently encodes an odd contract. (r1 + r2 concur.)
- Verified non-findings (r1+r2+r3): sizing parity computed from the actual resolved stop relationally (`:67-70`), ATR-fallback stop flow, trailing-stop tightening direction, exposure-reduction monotonicity at boundary exposure 0.047 < cap 0.05 (`:119-144`) — genuine behavioral checks consistent with `risk/engine.py`.
- Smells: `_risk_engine()` duplicated with test_live_execution_safety.py:44 and inline copies in test_risk_engine.py (r1+r2); market-data helpers duplicated with test_ramjet_signal_processor.py (r1).

---

## Cross-cutting observations

1. **Divergent `isolated_db`/engine-redirect fixtures** *(verified by grep during synthesis)* — fixture definitions: `isolated_db` at test_database_migrations.py `:141` (+ separate `legacy_db` at `:346`, + a raw `create_engine` at `:66`), test_ctrader_live_fix.py `:237`, test_live_execution_safety.py `:623`; ad-hoc engines additionally in test_portfolio_optimizer_oos.py `:562` — r1: "only the last attaches the production SQLite connect listener, shared/database.py:81-87"; r3 concurs the live_execution_safety copy must hand-re-attach the PRAGMA listener. **Copy-count disputed between reviewers** (three/four/five) — reconciled in verification log #8. One shared conftest fixture would fix all sites and remove the quietly-non-isolated failure mode behind B-17/B-32/B-48.
2. **Real-database writes from unit tests**: test_database_migrations.py `:33` and test_grid_tester_sqlite.py `:93/:95, :130/:132, :152/:154` run against whatever DATA_DIR resolves to (repo `data/` under conftest defaults) — B-17 + B-32.
3. **Suite-wide duplication**: LiveSignal construction (~25–40 sites across files, all three), adapter constructors (~14–15× ctrader_adapter), aiohttp fakes ×4, LLM provider fakes ×5–6, optimizer mocks ×6–7, risk-engine builders ×3 files — a `tests/helpers.py` would remove several hundred lines.
4. **Recurring sub-second timing windows** (`timeout=0.01/0.1`, `sleep(0.05–0.2)`) — event-gated and currently deterministic, but CI-load-sensitive (B-07/B-14/B-36/B-47/B-51).
5. **Security posture of the suite itself is good** (all three): fake credentials throughout; positive regression coverage for Sentry PII redaction, credential-file 0600 permissions + cross-process flock, fail-closed auth/deployment-status normalization, CTRADER_DEMO strict parsing, JVM trust-path allow-list, path-traversal rejection, smoke-report secret-field absence. No skipped/disabled tests without reason anywhere in the chunk; no mutable-default arguments; subprocess usage monkeypatched or timeout-guarded.

---

## Verification log (bounded pass, 8 spot-checks)

Scope rule applied: only reviewer-vs-reviewer flat contradictions and critical-severity findings were verified, by reading the source under `tests/` (and the specific production line a citation leaned on). Log:

1. **B-10 / B-11 / B-12 severity (r3 critical vs r1/r2 major; r1 minor for B-12)** — read `tests/test_ctrader_live_fix.py:250-296`. Confirmed verbatim: local `expected = 5.0` with asserts `-expected < 0` / `expected > 0` (`:268-281`); literals `position_status_open = 1` / `position_status_closed = 2` asserted unequal ("per the adapter code", `:284-295`); `adapter._symbol_name_to_id = symbol_map` then read back (`:254-265`). No production call in any of the three. Severities settled **critical / critical / major**, following r3 and the tests-a panel precedent ("asserts nothing about claimed live-money behavior" ⇒ critical), over r1/r2's major for B-10/B-11 and r1's minor for B-12 (which unlike its siblings does construct a real adapter — just exercises no logic).
2. **B-24 severity (major vs r1's minor)** — read `tests/test_download_grid_market_data.py:85-95` and sibling `tests/test_grid_market_data_download.py:68-73`. Confirmed bare assignment `dl.DukascopyNativeDownloader = MagicMock()` with no restore at `:92`, and the sibling's scoped `monkeypatch.setattr` at `:71`. Settled **major** (session-wide pollution of production module state; 2-of-3 reviewers; mechanism confirmed).
3. **B-33 existence (r1 silent vs r2+r3 major)** — read `tests/test_grid_tester_sqlite.py:150-183`. Confirmed membership-only assertions (`id_1 in ids`, `id_2 in ids`), no ordering check. Adopted as **major** on r2+r3 agreement plus independent confirmation by the reference report (t8, rated minor there — mechanism identical; panel keeps the reviewers' major since the name/docstring explicitly claims ordering).
4. **B-32 internal severity split (r1 minor vs r2/r3 major)** — read `tests/test_grid_tester_sqlite.py:90-150` and `tests/conftest.py:11-15`. Confirmed bare `init_db()` with no fixture at `:95/:98`, `%H%M%S` id suffixes, writes into `$DATA_DIR/backtests.db` resolved from repo `data/`. Settled **major** (identical defect class to B-17 which all three rated major; consistency wins — recalibration is exactly the "reviewers clearly disagree" case).
5. **B-66 severity split (r3 major pair vs r1/r2 each-half-minor; reference T2-1 major)** — read `tests/test_portfolio_optimizer_oos.py:195-225` and `:509-554`. Confirmed: docstring admits "reserved but not yet implemented", TODO at `:224`; `:543-547` passes only `test_months=6`; result assertions are key-presence only. Settled **one merged major**: the claim-vs-body mismatch spans two tests and enshrines a dead public parameter as validated behavior (r3 + reference agree on major for the substance; r1/r2's minor ratings addressed only each half in isolation).
6. **B-84 severity (r3 major vs r1 minor)** — read `tests/test_scheduler_signal_bot_health.py:10-64`. Confirmed assertions are exactly `mock_check.called` and `mock_upsert.call_count >= 2` with zero argument inspection, in the test *named* "detects_errors". Settled **major**: the test cannot catch the one regression its name claims (same criterion that made B-33 major; r1's own finding text agrees the error-recording mutant would pass — the disagreement was calibration only).
7. **Reference-report T4 (ctrader flock child left alive on timeout — resource leak, major) vs panel B-14** — read `tests/test_ctrader_live_fix.py:196-233`. The child is spawned via `subprocess.Popen` from an argv list, parent sleeps 0.2 s, asserts `writer.poll() is None`, then reaps with `writer.wait(timeout=2)` inside the flow (`:228`) and asserts returncode 0 plus post-lock file contents. On the normal path there is no leak; the reference's resource-leak scenario would require `wait(timeout=2)` raising an unhandled TimeoutExpired (a wedged child), which no panel reviewer flagged as written. **Flagged as unresolved reference disagreement, NOT adopted**: only the timing weakness stands (B-14, minor).
8. **isolated_db copy-count contradiction (r1 "three", r3 "four", r2 "fifth copy")** — counted engine-redirect definitions across the four files: test_database_migrations.py defines two locally (`isolated_db`, `legacy_db`), plus one each in test_ctrader_live_fix.py and test_live_execution_safety.py, plus ad-hoc engines in test_portfolio_optimizer_oos.py. All three counts are reconcilable depending on whether local multi-copy definitions are bundled; the substantive point (divergent, hand-rolled, PRAGMA-inconsistent copies needing one conftest fixture) is unanimous. Recorded once in cross-cutting #1 with the count ambiguity noted.

Not spot-checked (below threshold per scope rule): B-01 (r1 explicitly declines to file what r2 files minor — both readings preserved in place), B-38 (unanimous + reference concurrence), B-78 (unanimous + reference concurrence), B-17 (unanimous major).

---

## Coverage statement & finding totals

**Coverage — 49 tracked files reconciled.** Chunk B is the alphabetical middle of the root-level tracked `tests/` entries from `tests/test_backtest_grid.py` through `tests/test_signal_bots_risk.py`. Enumerating the repository shows that range contains **49 tracked files**: 48 pytest modules plus one non-module entry, `tests/compare_metrics_quantstats.py` — a 284-line standalone metrics-validation *script* (`python tests/compare_metrics_quantstats.py`; compares `runner.stats` Sharpe/Sortino/Calmar/MaxDD against quantstats on synthetic scenarios; contains **no `test_*` functions and no assertions**, single `__main__` block at `:203`). This resolves the reviewers' split self-claims: r1/r2 said "49 files", r3 said "48 **test** files" — both were counting correctly over different sets. **Flagged explicitly rather than absorbed: `compare_metrics_quantstats.py` was not reviewed by any of the three panelists** (none of the three reports mentions it); since it defines no tests, the merged findings are unaffected, but a follow-up review of that script is owed if the brief intended it in scope.

Of the 48 test modules, every one has a dedicated section above: **31 files produced counted findings** (B-01…B-88); **17 were scanned with zero counted findings**: test_capm, test_catalog, test_ctrader_deployment, test_daily_equity, test_dolthub_manager, test_grid_market_data_download, test_live_config, test_models, test_mt5_parity_artifact_loader, test_native_engine_execution, test_orchestrator, test_qc_risk_parity_artifact_loader, test_ramjet_signal_processor, test_request_logging_middleware, test_sentry_config, test_service_discovery, test_shared_logging (several carry smells/positives recorded inline but no counted bug/security finding). Reviewer self-coverage caveat stands: r2 claims 49 files read but enumerates 45 — its report omits `tests/test_scheduler_signal_bot_health.py`, `tests/test_signal_bot_health_check.py`, and `tests/test_request_logging_middleware.py`, which are covered here from r1 + r3.

**Finding totals (counted categories: bugs/correctness risks + security issues; Fowler smells/hot spots preserved per file but uncounted, matching the reviewers' own tally conventions):**

| Severity | Count |
|---|---|
| Critical | 2 |
| Major | 7 |
| Minor | 79 |

Total: **88 counted findings** (B-01…B-88) across the chunk. Criticals: B-10, B-11 (vacuous cTrader position-safety tests on a live-money path). Majors: B-12 (attribute-echo mapping test), B-17 (real-DB migration test), B-24 (MagicMock session leak onto production module), B-32 (real-DB grid-tester trio), B-33 (ordering test never checks order), B-48 (misleading DATA_DIR isolation), B-66 (train_months dead-knob pair), B-84 (health-check error detection blind). Reviewer baselines for calibration: r1 = 0C/3Maj/35min · r2 = 0C/8Maj/47min · r3 = 2Crit/8Maj/51min. Severity recalibrations made during synthesis, each justified in the verification log or inline: B-12 minor→major (verification #1), B-24 minor→major (#2), B-33 adopted major on r2+r3 (+reference mechanism match) (#3), B-32 minor→major for consistency with the unanimous major B-17 (#4), B-66 two minors→one merged major (#5), B-84 minor→major (#6); B-10/B-11 kept at r3's critical over r1/r2's major (#1).



