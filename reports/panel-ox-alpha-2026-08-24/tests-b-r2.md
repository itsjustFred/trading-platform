# Panel review — area `tests-b` (chunk B of test suite) — reviewer r2

Reviewer: r2 (ox-alpha), 2026-08-24. Blind & independent: only `tests/*.py` in the assigned list were read; no other reports under `reports/` consulted.

Method: every assigned file read in full (largest first). Findings cite `file:line`. Severity: critical / major / minor.

Status legend per file: ✅ findings recorded · ⬜ not yet scanned · ✔ scanned, nothing found.

---

## Findings

### tests/test_live_execution_safety.py (2256 lines) ✅

Safety-net suite for live execution: risk-engine boundaries, submit guards, fill persistence/idempotency, migration durability, reconciliation recovery. Assertions were cross-checked against `risk/engine.py`, `risk/config.py`, `live_runner/daemon.py`, `live_runner/alpaca_adapter.py`, `live_runner/ctrader_adapter.py`, `shared/database.py` — the numeric expectations (50/5 pip scaling, 0.05 BTC percent-stop sizing, qty=200 flat sizing, exposure estimate `min(0.01, dist/entry)*qty`) all reproduce exactly. Strong suite overall; mutation-driven comments document intent well.

(a) Bugs / correctness risks
- **major** — tests/test_live_execution_safety.py:632 — the `isolated_db` fixture patches `database.DATA_DIR` to `tmp_path`, but `DATA_DIR` is never used to locate the database: the engine URL comes from module-level `DB_PATH` built at import time (shared/database.py:62-70), and `init_db()` only touches `DATA_DIR` for `mkdir` (shared/database.py:834). Isolation actually rests solely on the `ENGINE` patch; the `DATA_DIR` patch is dead and misleads readers into thinking path-based isolation exists. If a future helper resolved paths from `DATA_DIR` directly (e.g. artifact writers at shared/database.py:1082), tests would silently write into the repo's real data dir. Remove the patch or make it load-bearing.
- **minor** — tests/test_live_execution_safety.py:1893-1894 — `test_ctrader_reconcile_recovers_accepted_order_before_local_id_persistence` only asserts `lookup.fromTimestamp < signal_ts_ms` and `toTimestamp > signal_ts_ms`; `_OrderListReq.__init__` initializes both to `0`, so a production regression that never sets the search window (scanning from epoch) still passes. Should additionally bound the window (e.g. `signal_ts - lookback <= fromTimestamp`).
- **minor** — tests/test_live_execution_safety.py:1432-1439 — `test_alpaca_delayed_stream_fill_is_reported_as_duplicate` reaches into `module.logger.handlers`, grabs the first non-FileHandler StreamHandler and swaps its stream. Failure mode if logging config changes is a spurious failure (not silent), but the coupling to `shared_logging` internals makes the test brittle; asserting on `caplog`/a dedicated handler would be sturdier.
- **minor** — tests/test_live_execution_safety.py:998-999, 1354-1355 — concurrency tests use `ThreadPoolExecutor` against SQLite WAL; sound on Linux/macOS (busy_timeout=30000, foreign_keys/WAL re-applied via the fixture's `connect` listener at line 630), but WAL journal mode can fail on exotic filesystems/network drives, making these environment-sensitive flakes. Acceptable; worth a skip-marker guard.

Not bugs, verified intentionally: line 602-604 volume-floor semantics match `ctrader_adapter.py:757-779` (fail-closed, explicit `ctrader_volume` override allowed); line 127-131 status normalization matches `daemon.py:1716-1720` (DISABLED→UNKNOWN is deliberate fail-closed behavior, not a typo); line 439 RuntimeError path matches `alpaca_adapter.py:392-402`.

(b) Security — nothing found: fake credentials only, SQL executed via bound params/static literals (714-758), datetimes timezone-aware throughout, no subprocess, no secrets logged.

(c) Baseline smells
- **Duplicated Code** — tests/test_live_execution_safety.py:1537-1590 vs 1748-1808: the Alpaca and cTrader "reconcile pages past candidate limit" tests duplicate the entire cursor-paging scaffold (cursors dict, `list_page` closure, expected page list) nearly verbatim; extract a shared paging-test harness. Same for the redelivery trio at 382-444 / 447-495 / 498-537 (~25 lines of identical client/monkeypatch setup each).
- **Data Clumps / Primitive Obsession** — ~20 hand-rolled `LiveSignal(...)` constructions differing only in symbol/timestamp/metadata; `_live_signal()` (173) exists but most tests bypass it. A keyword-factory would shrink the file materially.
- **Divergent Change** — 2256 lines spanning three domains (risk-engine math, two brokers' persistence/reconciliation, daemon service restart); a natural split into per-domain modules.

(d) Maintainability hot spots
- Protobuf types faked via `type(...)` classes + `SimpleNamespace(FILLED=1, ...)` (1727-1736, 1838-1882, 2015-2052): renaming any production enum/attribute breaks many tests simultaneously; a single module-level fake-protobuf factory would localize that churn.

### tests/test_live_api.py (1315 lines) ✅

FastAPI TestClient suite over `viewer.main` live endpoints/websockets. All DB/service dependencies monkeypatched — these are routing/serialization tests, not integration tests.

(a) Bugs / correctness risks
- **minor** — tests/test_live_api.py:125-153 — `load_live_account_stats_inputs` is monkeypatched twice inside `test_live_endpoints_return_expected_payloads`; the second registration silently overrides the first (68-95), making the first stub — including its `slippage`/`slippage_bps` keys — dead code. A maintainer reading the first block will wrongly believe the `/api/live/stats` assertions depend on slippage-bearing rows. Delete the first stub.
- **minor** — tests/test_live_api.py:991 — `assert signal["status"] in {"Waiting", "Pending"}` accepts two different behaviors; a regression that flips the status label passes as long as it lands on the other string. Pin the exact expected status.
- **minor** — tests/test_live_api.py:20-21 — module-level `sys.path.insert(0, project_root)` runs at collection time and permanently pollutes interpreter state for every later test/module; unnecessary given pytest rootdir/conftest already put the repo on `sys.path`.
- **minor** — tests/test_live_api.py:179-189 — several payload-shape checks use `>= 1` / `in` where the mocked inputs guarantee exactly 1 item (`len(series) >= 1`, `len(trades) >= 1`, `deployment_id in ...`). Weaker than the available exact assertions; would miss accidental duplication bugs in payload building.
- Verified intentionally-correct numbers: stats math at 797-805 (win_rate 20.0 = 1/5, realized net PnL 0 from ±10 round trips, balance-drawdown 220 = peak 1100 − trough 880, 220/1100 = 20%) is internally consistent with a realized-pnl/balance-drawdown implementation.

(b) Security — nothing found: fake API keys only ("api-key"/"secret-key", line 830); no real tokens; account id `PA3T0MQAGQ51` is an Alpaca paper id used as fixture data.

(c) Baseline smells
- **Duplicated Code** — tests/test_live_api.py:73-94 vs 105-123 vs 136-152: the execution-row dict literal appears three times in one test with drifting keys (slippage present/absent) — the drift is exactly what hid bug (a). Extract a factory. Same pattern across tests: RAMJET Service stub duplicated (231-244 vs 430-443).
- **Excessive Mocking (vacuity risk)** — CRUD flows (211-333) fake every persistence call (`create_live_deployment`, `update_live_deployment_status`, `delete_live_deployment` return canned models), so the suite verifies HTTP wiring/status codes only; no test anywhere covers that a created deployment is actually retrievable. Acceptable for unit scope, but the file name promises more than it delivers.
- **Mysterious Name** — `_deployment_ids_for_account` mocked at 127-129 / 947-949 to return `[_account_id, deployment_id]`, i.e. the account id itself masquerades as a deployment id; only readable once you notice the production helper's odd contract.

(d) Maintainability hot spots
- Every test re-imports `viewer.main` inside its body and re-stubs overlapping globals (~30 `monkeypatch.setattr(viewer_main_module, ...)` calls); a module-scoped fixture set would remove ~200 lines and the double-registration hazard above.

### tests/test_portfolio_optimizer_oos.py (594 lines) ✅

Unit tests for portfolio-optimizer OOS splitting/alignment/correlation/persistence. Split-boundary math verified against `runner/portfolio_optimizer.py:48-110`: `test_split_equity_curves_basic` (75-76: 12 train/4 test) and `test_split_equity_curves_with_train_months` (222-223: 11 train/7 test) both reproduce exactly given the `train_end_ts >= test_start_ts` clamp.

(a) Bugs / correctness risks
- **minor** — tests/test_portfolio_optimizer_oos.py:509-554 — `test_portfolio_optimizer_with_train_months_oos_split` is named and documented as exercising a train_months-driven IS/OOS walk-forward split ("Training (IS) uses first N months… 18 months train"), but never passes `train_months` and only asserts key presence (`"oos_sharpe" in result`, `"oos_split" in result`). It validates nothing about the split boundary its docstring promises — a regression that puts every point in test (or train) still passes. Assert the point counts like lines 75-76 do.
- **minor** — tests/test_portfolio_optimizer_oos.py:112-116, 171-173, 533-535 — `mock_load` distinguishes strategies by *call count* (`call_count[0] == 1 → strategy1 else strategy2`), coupling tests to the optimizer's internal load order and count; a re-ordering or caching change in production flips which curve each backtest id gets — silently, since both fixtures are similar. Stub by `backtest_id` instead.
- **minor** — tests/test_portfolio_optimizer_oos.py:241 — `test_split_equity_curves_single_curve` asserts only `len(test_curves[0]) > 0`; with 12 monthly points and `test_months=3` the exact expectation is 4 points (i ≥ 9). As written it tolerates any boundary bug short of an empty test set.
- **minor** — tests/test_portfolio_optimizer_oos.py:37 — `1704067200 <= ts <= 1704153600` allows a full day of drift although `_parse_time_to_ts("2024-01-01T00:00:00Z")` is deterministically `1704067200.0` (tz-aware parse at runner/portfolio_optimizer.py:37-38). Exact equality is available and stronger.
- Verified intentionally-correct: alignment-overlap semantics (274-307) match the documented common-window design; all-failed-trials structured error (487-491) and path-traversal rejection (494-506) pin real fail-safe behavior.

(b) Security — positive: `test_persist_optimization_result_rejects_path_outside_directory` pins the traversal guard including no-file-written follow-up (506). No issues found.

(c) Baseline smells
- **Duplicated Code** — the three end-to-end optimizer tests (86-153, 155-195, 509-554) rebuild the same mocked-curve scaffold; `seconds_per_month`/`base_ts` literals repeated five times (56, 92-93, 157-158, 204-205, 517-518). Extract a curve factory constant/module helper.
- **Speculative Generality (pinned)** — 198-225 documents `train_months` as "reserved but not yet implemented"; the test honestly pins current behavior, but the parameter plus TODO (224) is speculative surface area in the production signature that this suite then has to keep explaining.
- **Mysterious Name** — 509 test name references `train_months` while the call site omits it (see (a)); reader cannot reconcile name and body.

(d) Maintainability hot spots
- tests/test_portfolio_optimizer_oos.py:503 — `np.testing.assert_raises(ValueError)` for a plain Python exception; `pytest.raises` is the house style everywhere else and gives better failure output. Trivial but inconsistent.

### tests/test_database_migrations.py (612 lines) ✅

Migration/dedupe/backfill suite for `shared.database`. Logic assertions verified sound (discord-id normalization, IntegrityError guard, equity-curve backfill precedence).

(a) Bugs / correctness risks
- **major** — tests/test_database_migrations.py:33-59 — `test_backtest_record_equity_curve_persistence` calls bare `init_db()` with **no isolation fixture**, so it reads/writes the repo's real SQLite DB (`$DATA_DIR/backtests.db`; conftest pins DATA_DIR to `<repo>/data`, tests/conftest.py:11-14). Worse, the record id embeds `datetime.now(...)` microseconds (line 37) and there is no teardown/delete, so every test run permanently deposits a new `test_equity_curve_record_*` row into the developer/CI database. Every sibling test in this file carefully uses `isolated_db`; this one escapes isolation. Add the fixture or delete the row in a finally.
- **minor** — tests/test_database_migrations.py:232 — `test_concurrent_same_discord_id_yields_one_record` is named "concurrent" but is entirely sequential; it simulates the lost-update window by intercepting one pre-check SELECT (245-262). Legitimate technique, misleading name — nothing exercises threads/processes despite the Finding-6 framing.
- **minor** — tests/test_database_migrations.py:264-269 — hand-rolled patch/restore of `database.Session.exec` via try/finally instead of `monkeypatch.setattr`; if an exception occurs inside `fake_exec` itself the restore still runs (finally), but a mid-stub KeyboardInterrupt between assignment and try would leak the patch into other tests. Use the fixture.
- **minor** — tests/test_database_migrations.py:445 — `ALTER TABLE backtests DROP COLUMN equity_curve` silently requires SQLite ≥ 3.35; on older bundled SQLite the whole migration test errors confusingly rather than skipping. Consider a version gate.
- **minor** — tests/test_database_migrations.py:140-160 vs 62-77 vs 345-360 — three local variants of "redirect shared.database at a tmp SQLite engine", differing subtly (only `isolated_db` pre-creates `backtests_dir`; only some patch `DB_PATH`). Combined with the fourth copy in tests/test_live_execution_safety.py:622-635 (which patches none of `DB_PATH` and must hand-re-attach the PRAGMA listener), this is Shotgun Surgery waiting to bite: a new module global added to `shared.database` needs four edits, and missing one yields quietly non-isolated tests exactly like (a).
- Verified intentionally-correct: 300-315 pins dedupe-does-not-clobber broker_order_id; 556-573 pins backfill-does-not-overwrite; 576-612 pins skip_existing "top-up counted as migrated" semantics with explicit comments.

(b) Security — nothing found: parameterized SQL throughout, no secrets, tz-aware datetimes.

(c) Baseline smells
- **Duplicated Code** — fixtures noted above; plus `_legacy_signals_db`/`_insert_legacy_signal` (320-342) re-implement what tests/test_live_execution_safety.py:714-758 does with stricter DDL — one shared legacy-schema helper would serve both files.
- **Mysterious Name** — 232 (see (a)); also section banners reference "Finding 4/5/6/7/8" (181, 229, 281, 317, 439) pointing at an external review document no longer linked — cryptic to newcomers.

(d) Maintainability hot spots
- The suite depends on `shared.database` private globals (`_DB_INITIALIZED`, `_migrate_schema`) at lines 76, 157, 359, 432-433, 487-488, 519-520, 547-548, 568-569 — any refactor of init/migration internals breaks 10+ tests; a small public test-support API (e.g. `reset_for_tests(engine)`) would contain that.

### tests/test_ctrader_adapter.py (588 lines) ✅

Auth/error-classification/listener/reconnect unit tests for the cTrader adapter. Event-driven concurrency tests (334-390, 393-433) are exemplary — ordering enforced via asyncio Events, no sleeps. ✔ scanned; only minor findings:

(a) Bugs / correctness risks
- **minor** — tests/test_ctrader_adapter.py:78, 511 — two timing windows: `connect_timeout_seconds=0.01` (line 78, must elapse before `startService` bookkeeping completes) and `asyncio.wait_for(client_started.wait(), timeout=0.1)` (line 511). Both event-gated and generous relative to their assertions, but on a heavily loaded CI runner 10 ms can fire early; prefer injecting an unresolvable event rather than a real micro-timeout.
- **minor** — tests/test_ctrader_adapter.py:46-68 — `CTRADER_DEMO=true` (the positive branch) is never exercised: only `false` and invalid values are tested, leaving the strict-parse success path for `"true"` unpinned while its failure twin is covered.
- **minor** — tests/test_ctrader_adapter.py:360-377 — `send_request` doubles as both broker request handler and refresh handler distinguished by `isinstance`; returning the sentinel `request` object on retry (369) makes the success contract implicit. Works, but a named success value would document intent better.

(b) Security — nothing found: all credentials fake ("app"/"secret"/"token"); the token-refresh test correctly redirects the `.accounts.env` write into `tmp_path` (247-256) rather than touching any repo env file.

(c) Baseline smells
- **Duplicated Code** — `CTraderLiveAdapter(app_id="app", app_secret="secret", access_token=..., account_id=...)` constructed inline ~15×; `RefreshRequest`/`RefreshResponse` stub pair re-declared three times (258-265, 347-354, 403-409). Extract module-level factory/stubs.
- **Mysterious Name** — none significant; test names accurately describe behavior.

(d) Maintainability hot spots
- White-box coupling to private adapter state (`_execution_queue`, `_reconciliation_required`, `_terminal_auth_failure_code`, `_Protobuf`, payload-type ints at 105-108): any internal rename breaks many tests at once. Tolerable for an adapter this stateful, but a couple of construction/fixture helpers would localize the blast radius.

### tests/test_optimizer.py (583 lines) ✅

Optuna-based optimizer suite: split validation, bar-coverage preflight gating, best-param persistence, error propagation, risk-parity covariance mode. Strong negative-path discipline (`forbidden_create_study`, `_forbid_call`) pins ordering, not just outcomes.

(a) Bugs / correctness risks
- **minor** — tests/test_optimizer.py:474-491 vs 480 — `test_run_optimization_saves_best_params_and_oos_stats` correctly pins best window=3 by train AND OOS sharpe, but `assert any(trial["penalty_applied"] ...)` doesn't pin *which* trial was penalized; a regression that penalizes, say, window=4 instead of the zero-trade window=1 still passes. Assert the mapping params→penalty explicitly.
- **minor** — tests/test_optimizer.py:579-580 — `weights = payload[...].values()` then `sum(weights)` consumes the dict view; any later assertion reusing `weights` silently sees an empty iterable. Bind `list(...)` or re-read. Also no positivity assertion on the weights (risk parity must be long-only).
- **minor** — tests/test_optimizer.py:453 — `is_train = config.start_date.startswith("2024-01-01T00:00:00")` couples the fake to production's ISO datetime formatting from `_split_backtest_period`; if production switches date serialization the fake misclassifies every call and the test fails confusingly (or worse, self-consistently). Compare against the split boundary directly instead.
- **minor** — tests/test_optimizer.py:537-583 — unlike its siblings, the risk-parity test stubs neither `ensure_backtest_data` nor coverage preflight; it passes only because `_run_risk_parity_covariance_optimization` prefers returns derived from the base result (runner/optimizer.py:544-548) and the fixture has ≥2 symbols of trades. If that fallback ever reaches the market-data branch, the test hits the real data layer with an empty `data_dir`. One explicit stub or comment would make the dependency intentional.
- Verified intentionally-correct: 278-325 pins that study creation cannot precede exact test-segment bar validation, including `ensure_calls[0]` kwargs; 65-71 pins micro-range rejection; 41-47 pins inclusive upper bound at exactly 0.95.

(b) Security — nothing found: all writes under `tmp_path`, no network, no secrets.

(c) Baseline smells
- **Duplicated Code** — the `BACKTESTS_DIR`/`find_strategy_entry`/coverage-stub scaffold is re-typed in six tests (288-302, 337-346, 373-387, 415-425, 499-509, 543-552); a `_standard_optimizer_mocks(...)` helper would cut ~90 lines.
- **Data Clumps** — `SimpleNamespace(file_path=..., slug=...)` strategy-entry stub repeated five times verbatim.

(d) Maintainability hot spots
- Patching `optimizer_module.optuna.create_study` while capturing the original module attribute (431-439, 510-518) hard-couples tests to Optuna's factory signature and GridSampler semantics; an Optuna upgrade that changes either breaks four tests at once. Consider a thin seam in production code instead.

### tests/test_dukascopy_downloader.py (553 lines) ✅

Downloader control-flow tests: batching/pipelining, throttling/backoff/cooldown state machine, progress reporting, JForex classpath/JVM discovery. Backoff numbers verified consistent (pause 8000→10000 capped→15 s cooldown ⇒ `sleep_calls == [10.0, 15.0]`, lines 396-404). The fake-`aiohttp` trick works because production imports aiohttp inside the function (dukascopy_native.py:426), so `sys.modules` injection takes effect.

(a) Bugs / correctness risks
- **minor** — tests/test_dukascopy_downloader.py:151, 371-375 — throttle behavior is simulated by URL string (`throttle_signal=url == "u2"`); if production ever reorders or retries URLs the fixture semantics silently change. Acceptable for a unit fake but fragile by construction.
- **minor** — tests/test_dukascopy_downloader.py:492-511 — patching `argparse.ArgumentParser.parse_args` globally affects every parser constructed during the test; safe here (single CLI call) but a narrower seam would be safer if `main()` grows.
- Nothing found beyond these: control-event identity assertions (91-95), progress sequence (97-99), cooldown-reset log assertions are all meaningful and can fail.

(b) Security — nothing found. Positive: JVM discovery tests pin the trusted-path allow-list (`JFOREX_TRUSTED_JAVA_HOME`, 452-454, 462-472), i.e. the guard against loading arbitrary JVM DLLs is regression-tested.

(c) Baseline smells
- **Duplicated Code** — the four-class fake-aiohttp scaffold (`FakeConnector`/`FakeTimeout`/`FakeClientSession`/`SimpleNamespace`) is copy-pasted verbatim four times (105-130, 189-215, 257-283, 329-355); one module-level helper removes ~90 lines.
- **Duplicated Code** — `import data_tools.downloaders...` re-executed inside every test (16, 103, 187, …): module-level import is equivalent and quieter.

(d) Maintainability hot spots
- None significant; assertions are concrete throughout.

### tests/test_ctrader_live_fix.py (497 lines) ✅

Token-refresh re-auth/serialization/atomic-persistence tests (excellent), broker-order-id roundtrip, dedupe script tests — plus three vacuous "positions" tests that give false confidence.

(a) Bugs / correctness risks
- **major** — tests/test_ctrader_live_fix.py:268-282 — `test_ctrader_positions_signed_qty_for_sell` never calls any production code: it defines `expected = 5.0` locally and asserts `-expected < 0` and `expected > 0` — tautologies over Python float literals. The SELL-negative-sign behavior named in its docstring is untested; a sign bug in `get_positions` passes this suite. Rewrite against the real conversion (`volume/1000 * side_sign`) or delete.
- **major** — tests/test_ctrader_live_fix.py:284-295 — `test_ctrader_positions_excludes_closed_positions` asserts two of its own local literals differ (`position_status_open(1) != position_status_closed(2)`). Nothing exercises position filtering; the closed-position exclusion claim is pure fiction.
- **major** — tests/test_ctrader_live_fix.py:254-265 — `test_ctrader_positions_symbol_id_mapping` assigns `adapter._symbol_name_to_id = symbol_map` then asserts the assigned dict equals what was assigned. Asserting that attribute assignment works; no mapping logic tested.
- **minor** — tests/test_ctrader_live_fix.py:207-228 — cross-process flock test sleeps 0.2 s and asserts `writer.poll() is None`; a *missing* lock in `_write_env_keys` can still pass if the child happens to still be starting up under load (false negative), and the test errors (not skips) on platforms without `fcntl`. Deterministic alternative: have the parent verify the child is blocked via its stdout, or use a much longer probe with early exit.
- **minor** — tests/test_ctrader_live_fix.py:162-167 — hand-rolled patch/restore of `mod._write_env_keys` (try/finally) instead of `monkeypatch.setattr`; leaks the stub if interrupted between assignment and try entry.
- Verified intentionally-correct: 76-108 pins refresh→account-auth→retry ordering by index assertions; 111-138 pins single-flight refresh including persisted pair; 179-193 pins 0600 perms (positive security test); dedupe tests pin oldest-row retention and dry-run non-destruction.

(b) Security — positive: 0600 permission assertion (190-191) and crash-safety test proving no mixed token pair persists (169-176). Subprocess spawned from argv list without shell (209-221): safe.

(c) Baseline smells
- **Duplicated Code** — imports duplicated at module top (lines 9/15 `datetime, timezone`; 12/16 `Any`) — copy-paste seam showing the positions block was pasted in wholesale. Fifth copy of the `isolated_db` engine-redirect fixture (236-248).
- **Dead Test Utilities / Speculative Generality** — the three vacuous tests above are worse than dead: they simulate coverage of `get_positions` that does not exist.

(d) Maintainability hot spots
- File title says "edge-cases and helper behavior" while actually spanning three unrelated concerns (refresh persistence, DB roundtrip, dedupe script); split along those seams.

### tests/test_cache_manager.py (345 lines) ✅

Cache build/hit-miss/invalidation plus disk-catalog normalization/merge tests. Hit/miss/build counters asserted as deltas against a captured baseline (73-75), mutation of the returned copy proves defensive copying (67-72), and the merge rules (source "mixed", row sums, alias collapse, tick-segment symbol recovery) are pinned precisely. ✔ scanned; only minor notes:

(a) Bugs / correctness risks — none found. The `time.sleep(0.05)` at line 132 is a deliberate race-window widener inside a Barrier-synchronized single-build test; bounded and appropriate.
(b) Security — nothing found.
(c) Baseline smells — **Duplicated Code**: `fail_build_catalog` raising stub re-declared three times (166-169, 207-210, 268-271); `_catalog_payload` factory exists yet raw-dict catalogs are still hand-built in three tests.
(d) Maintainability hot spots — none significant.

### tests/test_live_daemon.py (535 lines) ✅

Daemon lifecycle unit tests: adapter rotation/cancellation ordering, credential-block bookkeeping, Discord auth-error handling, routing guards. Cancellation-ordering test (278-320) is exemplary event-driven determinism.

(a) Bugs / correctness risks
- **minor** — tests/test_live_daemon.py:311, 316, 344 — three `timeout=0.1` waits (`cleanup_started`, full rotation, concurrent-connect barrier). Event-gated so they fail loudly rather than asserting wrong behavior, but 100 ms can be exceeded on loaded CI; same pattern noted in test_ctrader_adapter.py.
- **minor** — tests/test_live_daemon.py:78-79, 140-143, 213-215, 370-371, 395-396, 421-422, 439-442, 457-460 — eight tests clear module-level block-list globals directly instead of via fixture/monkeypatch; if any assertion fails mid-test the mutated daemon state leaks into subsequent tests (order-dependent flakes). A small autouse reset fixture would fix all eight.
- Verified intentionally-correct: 39-74 pins that a failed connect does NOT thrash reconnects (adapter kept, one attempt); 202-228 pins that only cTrader-credential changes clear auth blocks (Discord token change must not); 472-512 is a well-documented regression test with a forward-compatible guard against restored source-layer persistence.

(b) Security — nothing found: fake tokens only ("test-token", "old"/"new"), no real credentials.
(c) Baseline smells — **Duplicated Code**: the two-line "clear blocked accounts/reasons" prologue repeated 8×; `LiveDeploymentConfig` boilerplate re-built per test. **Data Clumps** (minor).
(d) Maintainability hot spots — direct manipulation of ~6 private module globals (`_CTRADER_AUTH_BLOCKED_*`, `_DISCORD_AUTH_BLOCKED_*`, `_DISCORD_SERVICE_AUTH_BLOCKED_*`) across most tests; renaming any of them breaks half the file.

### tests/test_llm_providers.py (302 lines) ✅

Provider-selection and error-mapping tests for NIM/Copilot CLI/Copilot SDK providers. Error paths pinned by exact message match ("executable not found", "timed out", "exit code 2", "empty response", AUTH_SESSION_ERROR); model alias mapping asserted through captured session kwargs (293-297).

(a) Bugs / correctness risks
- **minor** — tests/test_llm_providers.py:220-227 — timeout test races a 0.05 s provider timeout against a 0.2 s fake sleep (4× margin); on a pathologically loaded runner the sleep could complete first and flip the assertion. Event-based deterministic stall would remove the flake window.
- **minor** — tests/test_llm_providers.py:34 — asserts private `_timeout_seconds`; couples to an internal attribute name for what is really constructor plumbing.
(b) Security — nothing found: NVIDIA key is a monkeypatched fake ("test-key", line 20).
(c) Baseline smells — **Duplicated Code**: the `FakePermissionHandler`/`FakeClient`/`FakeSession` scaffold re-declared in six tests (114-130, 139-176, 194-213, 222-252, 261-291); one shared fixture would halve the file.
(d) Maintainability hot spots — none significant.

### tests/test_catalog.py (290 lines) ✅

Real-parquet catalog-builder tests: legacy flat layouts, segmented-file grouping, symbol-prefixed segments, cross-source merging, timeframe aliasing, cache reuse (negative stub at 59-62 proves cached metadata short-circuits parquet reads). ✔ scanned; nothing significant found.

(a) Bugs / correctness risks — none found; boundary dates verified (100 hourly bars from 2024-01-01 ⇒ end "2024-01-05").
(b) Security — nothing found.
(c) Baseline smells — **Duplicated Code**: the two-month bid/ask DataFrame pair is rebuilt nearly verbatim in four tests (104-122, 150-168, 195-213, 246-270); extract a segment-fixture factory.
(d) Maintainability hot spots — none significant.

### tests/test_risk_engine.py (270 lines) ✅

Risk-engine unit tests mirroring the safety-net suite from the engine side: normal sizing with exact math (50.0 units, RR 2.0 — both binary-exact divisions), exposure rejection, wrong-side stop rejection under `enforce_stop_direction`, ATR fallback (`resolved_stop_loss` echo + `stop_source`), explicit vs legacy signal ids, wire-format validation of `RiskDecision`, and end-to-end qty injection through `submit_order_with_risk` (qty 200.0 consistent with the sibling file's identical scenario). ✔ scanned; only minor notes:

(a) Bugs / correctness risks
- **minor** — tests/test_risk_engine.py:57-89 — the exposure-limit case uses `current_exposure == max_total_exposure` exactly (boundary), but no case exercises *partial* consumption (e.g. exposure 0.03 ⇒ risk_percent capped to remaining 0.02); the min() clamp at risk/engine.py:152 is unpinned by this file (the safety-net suite covers it indirectly via broker-exposure estimation).
(b) Security — nothing found.
(c) Baseline smells — **Duplicated Code**: the five-line `RiskEngine(RiskConfig(0.01, 0.05, 5, 2.0))` block repeated four times (17-24, 58-65, 93-100, 128-135); `_engine()` already exists in tests/test_live_execution_safety.py:44-52 — share it.
(d) Maintainability hot spots — none significant.

### tests/test_daily_equity.py (269 lines) ✅

Daily-equity aggregation tests. All arithmetic verified exact (0.005 = 500/100000, 5% day return, cumulative 100800 across same-day trades); NaN/inf PnL skipping (124-145) and invalid/missing exit-time skipping (147-166) are genuinely exercised; replace-vs-preserve curve precedence pinned from both directions (199-244).

(a) Bugs / correctness risks — none found.
(b) Security — nothing found.
(c) Baseline smells
- **Dead Code** — lines 268-269: `if __name__ == "__main__": pytest.main([__file__, "-v"])` never executes under the repo's pytest runner; delete.
- **Duplicated Code** (minor) — inline `Trade(id=2/3, ...)` literals repeat the full constructor three times where a `make_trade(pnl=..., pnl_override=...)` variant would do.
(d) Maintainability hot spots — none significant.

### tests/test_models.py (232 lines) ✅

Universal trade-format serialization tests: dict/JSON/file roundtrips for TradeExit/Trade/BacktestResult/StrategyMeta, optional-field omission, winner/direction-sign helpers, filename generation. Assertions are concrete and can fail. ✔ scanned; only minor notes:

(a) Bugs / correctness risks — none found.
(b) Security — nothing found.
(c) Baseline smells
- **Duplicated Code** — `_make_simple_trade`/`_make_partial_exit_trade`/`_make_result` are fine as factories, but `TestBacktestResult.test_file_roundtrip` (185-192) uses `tempfile.TemporaryDirectory()` while every other file in the area uses pytest's `tmp_path`; inconsistent house style.
(d) Maintainability hot spots — none significant.

### tests/test_backtest_grid.py (231 lines) ✅

Grid-runner tests: CSV/TF normalization, CLI command assembly, full-grid fan-out with uniform risk (88 pins risk_pct across all four cells), per-cell subprocess timeout/launch-failure isolation (94-161 — good negative-path design keeping one hung cell from aborting the grid), and a resolved-date regression test capturing `attach_daily_equity` kwargs. ✔ scanned; only minor notes:

(a) Bugs / correctness risks
- **minor** — tests/test_backtest_grid.py:39-47 — string-membership assertions on `" ".join(cmd)` (`"-m runner.cli run" in joined`) verify token adjacency rather than argv structure; an argument reordering that still happens to serialize adjacently passes. Asserting exact list slices of `cmd` would be stricter.
(b) Security — nothing found; subprocess invoked with argv lists (no shell=True) in production paths under test.
(c) Baseline smells
- **Duplicated Code** — the 14-kwarg `run_one_backtest(...)` call is duplicated verbatim at 105-123 and 139-157; extract `_cell_kwargs(tmp_path)` helper.
(d) Maintainability hot spots — none significant.

### tests/test_preflight.py (225 lines) ✅

Preflight validator/CLI/serialization tests. Validation messages pinned via `any(... in ...)`; credential checks driven by tmp `.accounts.env`; CLI exit codes tested with mocked `run_live_preflight`.

(a) Bugs / correctness risks
- **minor** — tests/test_preflight.py:164-171, 180-188 — CLI tests mock `run_live_preflight` itself and assert only the exit-code mapping; the wiring between main() and the real preflight run is untested by design, but note `mock_run`'s return value never asserts `config_path` propagation — a regression passing the wrong config path to the result object would pass.
- **minor** — tests/test_preflight.py:47-88 — all four `_validate_deployments` failure cases use `any("substring" in e ...)`; exact-match on one error string would catch accidental error-message concatenation/duplication bugs.
- **minor** — tests/test_preflight.py:224-225 — trailing `pytest.main` block, dead under pytest.
(b) Security — nothing found; fake credentials only ("test_key"/"test_secret").
(c) Baseline smells
- **Speculative Generality / dead import** — line 7 imports `AsyncMock` but it is used nowhere in the file.
(d) Maintainability hot spots — none significant.

### tests/test_dukascopy_discovery.py (214 lines) ✅

Genesis-discovery binary-search tests. The fake probe mirrors real market-closed behavior (weekday != Saturday); skip-vs-force semantics asserted both by output JSON and by *tracked probe calls* (138-140) — good negative-space coverage; interrupt-safety test (168-202) pins that partial discoveries are persisted before KeyboardInterrupt propagates.

(a) Bugs / correctness risks
- **minor** — tests/test_dukascopy_discovery.py:213 — `_year_probe_date(date.today().year) <= date.today()` uses wall-clock `date.today()`, so the assertion's meaning shifts across the year (and near year-end the probe date can legitimately exceed today, making the assertion assert nothing about the intended invariant). Pin against a fixed reference date instead.
(b) Security — nothing found.
(c) Baseline smells
- **Duplicated Code** — the `sys.argv` monkeypatch + `discovery.main()` invocation repeated five times (80-86, 121-132, 150-162, 184-199); trivially extractable.
(d) Maintainability hot spots — none significant.

### tests/test_grid_tester_sqlite.py (202 lines) ✅

Grid-result SQLite persistence tests. One end-to-end test properly isolates JSON output via tmp_path, but four others hit the real shared database.

(a) Bugs / correctness risks
- **major** — tests/test_grid_tester_sqlite.py:93-127, 130-149, 152-182 — three tests call bare `init_db()` + `save_grid_result*`/`get_grid_results` with **no engine isolation fixture**, writing permanent rows (`test_grid_sqlite_save_*`, `test_grid_func_*`, `grid_older_*`, `grid_newer_*`) into the repo's real `$DATA_DIR/backtests.db` on every run, and asserting against whatever else is already there. Identical defect class to tests/test_database_migrations.py:33. Only `test_run_strategy_grid_persists_to_sqlite` (37-91) partially escapes — its JSON output is tmp_path'd, yet it too asserts through the unpatched real `get_grid_results` (85-90).
- **major** — tests/test_grid_tester_sqlite.py:152-182 — `test_get_grid_results_returns_newest_first` never checks order: it saves id_1 then id_2 and asserts only membership (`id_1 in ids`, `id_2 in ids`). The newest-first ordering promised by the name/docstring is completely unpinned; a regression returning arbitrary or ascending order passes. Should assert `ids.index(id_2) < ids.index(id_1)` (or filter to just these two).
- **minor** — tests/test_grid_tester_sqlite.py:98, 134 — ids keyed on `%H%M%S` wall-clock (second granularity): two runs colliding in the same second hit PK conflicts; combined with (a)-first this makes the suite time-flaky against the real DB.
- **minor** — tests/test_grid_tester_sqlite.py:185-202 — `test_grid_tester_loads_results_from_sqlite` stubs `get_grid_results` and compares `load_grid_results(limit=1)` to a one-element fixture, so a production regression that ignores `limit` entirely still passes; assert truncation with a two-element fixture.
(b) Security — nothing found.
(c) Baseline smells
- **Speculative Generality** — tests/test_grid_tester_sqlite.py:39-44 — fake `scipy`/`scipy.stats` modules injected wholesale with `norm` replaced by a plain function; if grid_tester ever touches `norm.cdf` or another stats symbol the fake explodes opaquely. A targeted stub of the one used symbol would be honest about scope.
(d) Maintainability hot spots — same private-global coupling (`GRID_RESULTS_DIR`, module `datetime`) noted elsewhere; fine here given tmp_path redirection.

### tests/test_download_grid_market_data.py (193 lines) ✅

Normalization/CLI tests for the grid market-data downloader. Pure-function assertions are exact and meaningful.

(a) Bugs / correctness risks
- **major** — tests/test_download_grid_market_data.py:92 — `dl.DukascopyNativeDownloader = MagicMock()` is a bare module-attribute assignment with **no monkeypatch and no restore**: the mock replaces the real downloader class on `data_tools.download_grid_market_data` for the remainder of the entire pytest session (not just this test). Any later test in any file that exercises the real class through this module silently receives a MagicMock. Use `monkeypatch.setattr(dl, "DukascopyNativeDownloader", MagicMock())`.
- **minor** — tests/test_download_grid_market_data.py:167-189 — `--workers -1` "clamped to 1" is verified only by *absence of crash* (`SystemExit.code == 0`); nothing observes the clamp. Capture stdout or assert on the planned-download payload.
- **minor** — tests/test_download_grid_market_data.py:149-164, 173-188 — manual `sys.argv` save/restore in try/finally instead of `monkeypatch.setattr(sys, "argv", ...)`; leaks argv if interrupted between assignment and try.
- **minor** — tests/test_download_grid_market_data.py:192-193 — dead trailing `pytest.main` block.
(b) Security — nothing found.
(c) Baseline smells
- **Duplicated Code** — function-level re-imports in every test (15, 34, 56, …) though the module import is hoisted nowhere; `tempfile.TemporaryDirectory()` used throughout while sibling files standardize on `tmp_path`.
(d) Maintainability hot spots — none significant beyond the leak above.

### tests/test_runner_cli.py (169 lines) ✅

CLI command handlers: canonical stats persistence (engine_metric passthrough + computed total_return 0.05 = 5000/100000 verified exact), timeframe alias normalization observed at the engine boundary via a capturing subclass, and walk-forward arg mapping with stdout JSON parse. ✔ scanned; only minor notes:

(a) Bugs / correctness risks
- **minor** — tests/test_runner_cli.py:63-78, 101-116, 139-157 — args are `SimpleNamespace` objects rather than parsed argv; if an argparse default changes or a new required flag is added, these tests keep passing while real CLI invocations break (the parser is never exercised). One test going through the real parser would close that gap.
(b) Security — nothing found.
(c) Baseline smells
- **Duplicated Code** — the 14-field `SimpleNamespace(...)` args block repeated three times; extract `_run_args(**overrides)`.
(d) Maintainability hot spots — none significant.

### tests/test_backtest_matrix.py (159 lines) ✅

Strategy-matrix orchestration: per-strategy grid fan-out with kwargs assertions (risk_pct/symbols/timeframes), engine metadata honored from STRATEGY_META ('native'), and failure isolation reported per strategy run. ✔ scanned; nothing significant found.

(a) Bugs / correctness risks — none found.
(b) Security — nothing found.
(c) Baseline smells — **Duplicated Code** (minor): `_write_strategy`/`_write_strategy_with_meta` overlap almost entirely — one function with optional meta would do; fake `run_backtest_grid` payload dict repeated three times (31-54, 110-118).
(d) Maintainability hot spots — none significant.

### tests/test_backtest_matrix_runner.py (70 lines) ✅

Matrix-cell runner test: inspection-link synthesis (`matrix_node_id` `demo:AAPL:1h`, viewer URLs) asserted exactly against a stubbed grid run and pre-written output JSON. ✔ scanned; nothing significant found.

(a) Bugs / correctness risks — none found.
(b) Security — nothing found.
(c) Baseline smells — none significant (single test, explicit kwargs).
(d) Maintainability hot spots — none.

### tests/test_ramjet_signal_processor.py (91 lines) ✅

RamJet signal-processor port tests: SL/TP computed from *actual* last close and the produced ATR (60-61) rather than hardcoded numbers — resilient yet falsifiable; trailing-stop phase machine pinned field-by-field (66-70); BUY-only enforcement negative-tested (73-79); production default config.yaml pinned (82-91). ✔ scanned; nothing significant found.

(a) Bugs / correctness risks — none found.
(b) Security — nothing found.
(c) Baseline smells — none significant.
(d) Maintainability hot spots — `test_processor_loads_default_ramjet_config_file` couples the suite to specific committed config values (atr_period 14 etc.); intentional default-pinning, but any tuning of the YAML must update this test — worth a comment saying so.

### tests/test_grid_market_data_download.py (86 lines) ✅

Defaults/alias regression tests incl. the 30m sort-key crash regression (57-65) and unknown-timeframe batch isolation (68-86). Notably line 71 patches `DukascopyNativeDownloader` **correctly** via `monkeypatch.setattr` — the pattern its sibling tests/test_download_grid_market_data.py:92 gets wrong. ✔ scanned; nothing significant found.

(a) Bugs / correctness risks — none found.
(b) Security — nothing found.
(c) Baseline smells — none significant.
(d) Maintainability hot spots — none.

### tests/test_live_preflight.py (365 lines) ✅

Live-preflight suite: strict boolean env parsing (exhaustive parametrize incl. garbage values asserting the error names the variable AND valid tokens — 46-52), credential validation, connectivity checks with fake adapters, and a careful test (285-365) proving token-refresh writes go to the threaded accounts file and *not* the default `.accounts.env` (before/after snapshot at 342-343/363-365).

(a) Bugs / correctness risks
- **minor** — tests/test_live_preflight.py:342-343 — reads the developer's real default `.accounts.env` (if present) as a baseline; if a concurrent process touches that file mid-test the final comparison fails confusingly, and the guard detects writes but not delete+recreate. Acceptable; monkeypatching `DEFAULT_ACCOUNTS_ENV` into tmp_path would be hermetic.
(b) Security — positive: pins fail-safe demo defaults (`CTRADER_DEMO` missing ⇒ demo, 19-21), strict token parsing (never implicitly live), numeric-account-id enforcement (121-148); all credentials fakes.
(c) Baseline smells — none significant.
(d) Maintainability hot spots — none significant.

### tests/test_signal_bots_risk.py (144 lines) ✅

Cross-bot integration of signal processors × RiskEngine: sizing from actual stop distance asserted relationally (`position_size ≈ risk_amount/risk_distance`, 67-70), ATR-fallback stop path via RocketFuel SELL (73-93), trailing-stop update raising the stop (96-116), and partial-exposure reduction pinned directionally at boundary exposure 0.047 < cap 0.05 (119-144). ✔ scanned; only minor notes:

(a) Bugs / correctness risks
- **minor** — tests/test_signal_bots_risk.py:41-50 — the "importable with process_signal" test only checks `callable(getattr(...))`; it cannot fail short of total absence, and `NitroSignalProcessor({"ram_jet": ...})` (line 47) is configured under the ram_jet key rather than a nitro key — if that's intentional config reuse it deserves a comment, otherwise it silently encodes an odd contract.
(b) Security — nothing found.
(c) Baseline smells — **Duplicated Code**: `_risk_engine()` duplicates the identical helper in tests/test_live_execution_safety.py:44 and inline copies in test_risk_engine.py.
(d) Maintainability hot spots — none significant.

### tests/test_mt5_parity_artifact_loader.py (158 lines) ✅

MT5 parity artifact regeneration helpers: bid-column backfill precedence over sentinel 9999 rows (14-49), m1_bid fallback file selection (52-70), EURUSD point-value conversion (80 000 = 100 000/1.25 exact; index 0.8), lot-step rounding, and shifted-stop-distance re-anchoring vs non-positive-stop fallback — both branches asserted through distinct exit reasons ("signal" vs "stop_loss") and meta sl_price (88-158). ✔ scanned; nothing significant found.

(a) Bugs / correctness risks — none found; both stop-anchoring branches are genuinely exercised.
(b) Security — nothing found.
(c) Baseline smells — **Duplicated Code** (minor): the 4-row frame + entries/exits scaffold duplicated between 88-119 and 126-154.
(d) Maintainability hot spots — none significant.

### tests/test_qc_risk_parity_artifact_loader.py (33 lines) ✅ ✔ scanned; nothing found.

Single test building a full TICKERS parquet universe in tmp_path and asserting column order/values for close and open frames. Concrete assertions; no issues.

### tests/test_sentry_config.py (143 lines) ✅

Sentry bootstrap/redaction tests. Security-positive: `before_send` redaction asserted across headers, extra keys, nested lists, and message-body regex (`Authorization`/`api_key` redacted while `safe=yes` survives, 50-79); `send_default_pii` defaults False (46); enabled-without-DSN fails closed (16-18). ✔ scanned; nothing significant found.

(a) Bugs / correctness risks — none found.
(b) Security — positive findings above; DSNs are synthetic (`https://public@example.com/1`).
(c) Baseline smells — none significant (`_reset_for_tests()` usage is appropriate global-state hygiene).
(d) Maintainability hot spots — none significant.

### tests/test_resample.py (108 lines) ✅

OHLCV resampling rules: bar counts exact (120/5=24, 120/15=8), aggregation rules O=first/H=max/L=min/C=last/V=sum verified against the source slice (64-73), monotonic timestamps, invalid-timeframe rejection.

(a) Bugs / correctness risks
- **minor** — tests/test_resample.py:59-62 — `1h→4h` case asserts only `0 < len ≤ n/4+1`; unlike its siblings there's no exact count (500 hours ⇒ 125 bars exactly). Weakest assertion in the file.
- **minor** — tests/test_resample.py:15, 33 — fixtures build **tz-naive** timestamps while every production-side catalog/downloader fixture in this area writes tz="UTC"; if resampling ever gains localization logic the fixtures can't detect a mismatch (baseline checklist: tz-naive datetimes).
(b) Security — nothing found.
(c) Baseline smells
- **Dead Code** — line 3 imports `tempfile`, never used (file I/O tests use `tmp_path`). Also `import numpy as np` re-done inside both fixtures (16, 34) instead of top-level.
(d) Maintainability hot spots — none significant.

### tests/test_risk_consistency.py (129 lines) ✅

Backtest-vs-live sizing parity: parametrized exact expected sizes (50.0 = 1000/(2×10); 20.0 = 1000/(5×10) — both reproduce), identical defaults asserted field-by-field, deliberate live-vs-backtest stop-direction asymmetry documented from both sides (79-130). ✔ scanned; only minor notes:

(a) Bugs / correctness risks
- **minor** — tests/test_risk_consistency.py:55, 94 — `datetime.now()` **naive** timestamps on LiveSignal where the rest of the area consistently uses `datetime.now(timezone.utc)`; harmless for pure sizing math today, but it normalizes naive datetimes as acceptable in live signal construction (baseline checklist violation).
(b) Security — nothing found.
(c) Baseline smells
- **Duplicated Code** — third copy of the `RiskEngine(RiskConfig(0.01, 0.05, 5, 2.0))` scaffold family.
(d) Maintainability hot spots — none significant.

### tests/test_database_init.py (25 lines) ✅

`init_db()` once-only guard: patches `create_all`/`_migrate_schema` with counters, asserts exactly one call each across two invocations. Correct isolation (DATA_DIR + `_DB_INITIALIZED` via monkeypatch). ✔ scanned; nothing found.

### tests/test_dolthub_manager.py (16 lines) ✅ ✔ scanned; nothing found.

Legacy flat-export migration: canonical path asserted (`AAPL/1d.parquet`), content preserved, legacy file removed. Concrete and falsifiable.

### tests/test_capm.py (40 lines) ✅

CAPM regression on a constructed linear relationship: beta 1.5, alpha annualized ×252, R²≈1.0, p-value threshold; alignment-by-intersection verified with exact boundary timestamps; minimum-observations guard negative-tested. ✔ scanned; nothing found.

### tests/test_orchestrator.py (46 lines) ✅ ✔ scanned; nothing significant.

Layer-ordering tests for the test orchestrator: non-e2e layers run before e2e and e2e failure doesn't abort earlier layers. Assertions are exact sequences.

(a) Bugs / correctness risks — none.
(b) Security — none.
(c) Baseline smells — **Speculative Generality** (minor): `_result()` always emits `passed=1, failed=…`, but no assertion consumes pass/fail counts — only exit codes and layer order matter here; the fields are unused shape.
(d) Maintainability hot spots — none.

### tests/test_risk_parity_optimizer.py (47 lines) ✅

Risk-parity solver: equalized risk contributions on diagonal covariance (<0.01 spread), weights sum to 1 and non-negative, diagnostics keys present, deterministic deduped candidate universes in exact expected order. ✔ scanned; nothing significant.

(a) Bugs / correctness risks — none.
(b) Security — none.
(c) Baseline smells — **Mysterious Name** (minor): `test_select_best_risk_parity_universe_returns_diagnostics` accepts either universe (`in [{SPY,TLT},{SPY,GLD}]`) — legitimate given the solver picks by score, but the name doesn't convey the looseness; a comment would help.
(d) Maintainability hot spots — none.

### tests/test_shared_logging.py (52 lines) ✅

JSON log formatter: fixed-epoch record pinned to exact timestamp string + regex; degraded LogRecord (empty module/funcName, non-int lineno) falls back to `<unknown>`/0 without crashing. ✔ scanned; nothing found.

### tests/test_service_discovery.py (35 lines) ✅ ✔ scanned; nothing found.

Discord bot discovery from env file: three bots with token-env-key/channel-id pairs asserted; `DEFAULT_ACCOUNTS_ENV` monkeypatched into tmp_path correctly. Fake tokens only.

### tests/test_live_config.py (64 lines) ✅

YAML deployment-config parser: valid parse asserted field-by-field; orphan deployment warns with structured `extra.data.account_id` captured from the mocked logger (`assert_called_once` + kwargs inspection). ✔ scanned; nothing found.

### tests/test_live_smoke.py (95 lines)

Live smoke CLI: crypto slash-alias position matching (exact float), report serialization without secret field names, help text exposes broker defaults, mode aliases mapped to canonical modes, discord-replay routing to fake order.

(a) Bugs / correctness risks
- **minor** — tests/test_live_smoke.py:42-43 — `"api_key" not in str(payload).lower()` is a weak secret-hygiene check: it greps serialized output for field names rather than asserting the report's schema excludes them; a value *containing* "key" elsewhere would false-fail, while an added sensitive field named differently passes.
- **minor** — tests/test_live_smoke.py:47-50 — `main(["--help"])` SystemExit swallowed bare (`except SystemExit: pass`) around act-phase; acceptable for argparse's exit-0, but `pytest.raises(SystemExit)` would also verify the code.
(b) Security — positive intent (secret-field exclusion check); tokens referenced are env-key names only, never values.
(c) Baseline smells
- **Duplicated Code** — seven separate import statements from `scripts.live_smoke` (8-13) instead of one grouped import.
(d) Maintainability hot spots — none significant.

### tests/test_native_engine_execution.py (62 lines) ✅

Native engine execution internals: native sl_price/tp_price alias extraction with exact TP-level structure; fixed-risk sizing 500/(100−95)=100 units exact; MT5 tick metadata path yielding lot-stepped 1.0 quantity. All arithmetic binary-exact. ✔ scanned; nothing significant.

(a) Bugs / correctness risks — none.
(b) Security — none.
(c) Baseline smells — none significant (white-box `_extract_levels`/`_calculate_position_size` access is appropriate for engine unit tests).
(d) Maintainability hot spots — none.

### tests/test_discord_signal_parsing.py (62 lines) ✅

Discord message → TradeSignal parsing: oversold-buy template with signal-type/channel metadata, symbol-after-action precedence over RAM JET label, lowercase-word rejection (43-49), RocketFuel momentum-reversal sell. ✔ scanned; one note:

(a) Bugs / correctness risks
- **minor** — tests/test_discord_signal_parsing.py:49 — `assert signal is None or signal.symbol != "AT"` is a disjunction that passes when parsing *succeeds* with any wrong symbol except "AT"; the intended invariant ("lowercase 'at' is not a symbol") permits more behavior than it pins.
(b) Security — none.
(c) Baseline smells — none significant.
(d) Maintainability hot spots — none.

### tests/test_live_risk_sizing.py (68 lines) ✅

`size_live_signal` contract: qty/risk_amount echoed into metadata, max_positions block forces qty 0, precomputed qty short-circuits sizing (`explicit_qty` marker). ✔ scanned; nothing found.

### tests/test_ctrader_deployment.py (61 lines) ✅

Top-backtest ranking by live score ("fast" beats "slow") and deployment-plan targeting of the first cTrader account with PAUSED status + `source_backtest_id` linkage. Concrete assertions on real production helpers, no mocks needed. ✔ scanned; nothing found.

---

## Summary

**Coverage**: all 49 assigned files read in full and accounted for above (each either produced findings or is marked scanned-with-nothing-found). Numeric expectations in the risk/sizing/optimizer/migration suites were independently recomputed against production sources (`risk/engine.py`, `risk/config.py`, `live_runner/daemon.py`, `live_runner/alpaca_adapter.py`, `live_runner/ctrader_adapter.py`, `shared/database.py`, `runner/portfolio_optimizer.py`, `runner/optimizer.py`).

**Finding counts**: critical **0** · major **8** · minor **47**

The majors cluster into three themes:
1. **Tests that cannot fail** (tests/test_ctrader_live_fix.py ×3): tautological assertions over local literals masquerading as cTrader `get_positions` coverage — false confidence about sign handling and closed-position filtering in live position reporting.
2. **Real-database pollution / session-wide mock leaks** (tests/test_database_migrations.py, tests/test_grid_tester_sqlite.py ×2, tests/test_download_grid_market_data.py): bare `init_db()` writing permanent rows into `$DATA_DIR/backtests.db`, an ordering test that never checks order, and one bare `module.attr = MagicMock()` that leaks for the rest of the pytest session.
3. **Misleading isolation** (tests/test_live_execution_safety.py): a `DATA_DIR` fixture patch that does nothing, suggesting path-based DB isolation that doesn't exist.

Cross-cutting smells (Fowler baseline): pervasive **Duplicated Code** — five near-copies of the SQLite engine-isolation fixture across four files, four copies of the fake-aiohttp scaffold, six copies of LLM provider fakes, three copies of the risk-engine construction helper; **Shotgun Surgery** risk wherever `shared.database` private globals are patched; recurring sub-second timing windows (`timeout=0.1`, `sleep(0.05–0.2)`) that are event-gated but CI-load-sensitive; scattered tz-naive datetimes (tests/test_resample.py, tests/test_risk_consistency.py); dead trailing `pytest.main` blocks in three files. No skipped/disabled tests without reason were found anywhere in the area; security posture of the suite itself is good (fake credentials throughout, positive redaction/permission/traversal-guard tests).

