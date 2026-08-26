# Code review — area `tests-b` (chunk B of test suite) — reviewer r3 (panel-ox-alpha-2026-08-24)

Blind, independent review of 48 test files under `tests/`. Findings cite `file:line` with severity
(critical/major/minor). Files with no findings are recorded as "scanned, nothing found".
Work-in-progress; appended incrementally per file group.

---

## Group 1 — largest files

### tests/test_live_execution_safety.py

(a) Bugs / correctness risks:

- **minor** `tests/test_live_execution_safety.py:99-100` — The fallback-stop assertions are compound
  (`buy.stop_loss is not None and buy.stop_loss < buy.entry_price`). If the production fallback-stop
  code were deleted entirely (stop stays `None`), the first clause fails so the test is not a pure
  tautology, but the message-less compound assert makes diagnosis harder than two separate asserts.
  Judgement call only; behavior coverage is real.
- **minor** `tests/test_live_execution_safety.py:233-261` (`test_alpaca_submit_order_blocks_non_positive_quantity`)
  — The docstring says it kills the mutant "relaxing the qty <= 0 guard to qty < 0", but the test only
  exercises qty == 0 (risk engine returns 0 when max positions reached). A mutant that rejects only
  strictly-negative quantities would pass this test. To truly kill `qty < 0` relaxation a negative-qty
  case would be needed; as written the comment overstates the kill power. Test itself is correct.
- **minor** `tests/test_live_execution_safety.py:1727-1738` (`test_ctrader_reconcile_backfills_missing_fill`)
  — `_ProtoOAOrderDetailsRes` is mocked as `type(..., {"deal": [deal]})`: `deal` becomes a *class
  attribute* on the fake response type while the production code presumably reads `response.deal`.
  Works, but note the sibling test at line 1786 uses `deal = []` class attr too — if production ever
  appends to `response.deal` this mock would mutate shared class state across tests. Fragile mock
  shape rather than an active bug.
- **minor** `tests/test_live_execution_safety.py:1432-1439`
  (`test_alpaca_delayed_stream_fill_is_reported_as_duplicate`) — The test reaches into
  `module.logger.handlers` and monkeypatches the stream of the first non-FileHandler StreamHandler it
  finds. If logging config changes (handler order, added handlers), this silently asserts against the
  wrong stream and can pass for the wrong reason or fail confusingly. Also depends on JSON log
  formatting of that handler.
- **minor** `tests/test_live_execution_safety.py:1002-1015` — The two concurrency tests
  (`ThreadPoolExecutor(max_workers=2)`) rely on SQLite write serialization timing; they are
  deterministic enough under `check_same_thread=False` + WAL/default journal because SQLite locks the
  write, but on very slow CI both threads could interleave such that either ordering satisfies the
  sorted assertion — fine here since the assertion is order-insensitive (`sorted(...) == [False, True]`),
  but `test_concurrent_discord_claims_are_independent_across_deployments` (line 1010) asserts strict
  `[True, True]` list equality on `executor.map` results which is order-stable by construction. OK,
  noted only as inherently timing-shaped concurrency tests (no sleeps — good).
- **minor** `tests/test_live_execution_safety.py:601` — `pytest.raises(ValueError, match="below the broker minimum")`
  pins broker min volume = 1000 implicitly via `qty: 0.07`; the boundary between reject (0.07→70 units)
  and accept (5.0→5000) is untested exactly at the floor (e.g. 0.1 → 100). Boundary-coverage gap, not a bug.

(b) Security issues: none found (tests never use real credentials; secrets are `"k"/"s"/"secret"` placeholders).

(c) Baseline smells:
- **Duplicated Code (major, maintainability)** `tests/test_live_execution_safety.py:1537-1590` vs
  `1748-1808` vs `1811-1898` — three near-identical keyset-pagination harnesses (`list_page`,
  `cursors`, `_OrderListReq/_OrderDetailsReq` fakes) duplicated between Alpaca and cTrader reconcile
  tests; extract shared fixtures/helpers.
- **Duplicated Code** `tests/test_live_execution_safety.py:2015-2052` vs `2094-2117` — duplicate
  `_NewOrderReq` fake protobuf classes defined inline twice in adjacent tests.
- **Mysterious Name (minor)** `tests/test_live_execution_safety.py:31` — `_make_ctrader_adapter()`
  returns `(module, adapter)` tuple; name doesn't hint at tuple return ("Bootstrap a detached..."
  helps little). Data Clump-ish return shape repeated at ~15 call sites.

(d) Maintainability hot spots: the file mixes four concerns (risk-engine math, Alpaca adapter,
cTrader adapter, shared-database persistence/migrations/recovery). At 2256 lines it should split into
per-subsystem modules (Divergent Change).

### tests/test_live_api.py

(a) Bugs / correctness risks:

- **minor** `tests/test_live_api.py:68-95` vs `130-153` — In
  `test_live_endpoints_return_expected_payloads`, `load_live_account_stats_inputs` is monkeypatched
  **twice**; the second registration (line 130) silently replaces the first (line 70), making the
  first block — including its `slippage`/`slippage_bps` fields — dead setup. Misleading: readers
  believe slippage flows through the stats-inputs path under test; also copy-paste drift between
  the two stubs (the surviving one dropped the slippage keys). Verified against
  `viewer/main.py:3738-3772` that the surviving assertions themselves are correct (see below);
  the defect here is dead/duplicated setup, not a wrong expectation.
- **verified-correct (no finding)** `tests/test_live_api.py:196-208` — Draft suspicion removed after
  cross-checking `viewer/main.py:3674-3735`: `_calculate_realized_live_pnl` books PnL only on
  *closing* fills, so a lone buy fill correctly yields `total_pnl == 0`, `winning_trades == 0`.
  The expectations encode real open/close accounting; a naming/comment improvement would prevent
  the next reviewer re-suspecting it.
- **minor** `tests/test_live_api.py:800` (with `viewer/main.py:3743`) — the parity test pins
  `win_rate == 20.0` for 5 executions of which 1 won and 1 lost: i.e. win_rate denominates by ALL
  executions including non-closing entries (1/5), not by decided trades (1/2 = 50%). The test
  faithfully encodes this production choice, but the metric name invites misreading (operators
  expecting close-to-close win rate see half the number). Flagging as a semantics trap the suite
  cements; not a test bug per se.
- **minor** `tests/test_live_api.py:128,948` (with `viewer/main.py:3523-3532`) — fixtures mirror
  production `_deployment_ids_for_account`, which stuffs the *account id* and even *strategy names*
  into the deployment-id list. Tests then assert only `deployment_id in payload["deployment_ids"]`,
  which stays green even if the list grows more polluted. Production design smell (Primitive
  Obsession on string ids) locked in by weak assertions.
- **minor** `tests/test_live_api.py:997-1064` & `1193-1282` — WebSocket snapshot/update tests drive
  real event-loop polling with `poll_seconds=0.05`; no sleeps (good) but the second `receive_json()`
  relies on the poller emitting an "update" frame before the context exits — a slow CI machine could
  time out flakily. Low risk, inherent timing dependence.
- **minor** `tests/test_live_api.py:21` — `sys.path.insert(0, str(Path(__file__).parent.parent))` is a
  leftover path hack; redundant under the suite's conftest and can shadow modules.
- **minor** `tests/test_live_api.py:24-209` — single test function asserts ~8 endpoints
  (`accounts`, `deployments`, `balances`, `trades`, `stats`). One failure masks the rest; split per
  endpoint.

(b) Security issues: none found — all credentials/brokers are stubbed; no real tokens in fixtures.

(c) Baseline smells:
- **Duplicated Code (major)** `tests/test_live_api.py:73-94` vs `105-124` vs `136-152` — the same
  execution-row dict literal appears three times within one test (twice inside the same function via
  the double monkeypatch, once in `list_live_signal_executions_for_deployments`). Extract a module-level
  `_execution_row()` factory.
- **Excessive Mocking / vacuous risk (major)** `tests/test_live_api.py` overall — every data-layer
  function is monkeypatched (`list_live_accounts`, `create_live_deployment`, ...), so the CRUD tests at
  lines 211-333 verify routing + serialization only; e.g. `update_live_deployment_status` stub echoes
  back whatever status it was given (lines 272-288), so the PUT test cannot catch a status-validation
  regression. Acceptable for API-shape tests, but nothing here covers real persistence paths.
- **Primitive Obsession (minor)** — trade/stat payloads asserted as raw dicts throughout; a typed
  helper/model would prevent silent field-rename drift.

(d) Maintainability hot spots: `test_live_endpoints_return_expected_payloads` (lines 24-209) is the
hot spot — god-test + double stub + duplicated payloads.

---

## Group 2 — mid-size files

### tests/test_database_migrations.py

(a) Bugs / correctness risks:
- **major** `tests/test_database_migrations.py:33-59` (`test_backtest_record_equity_curve_persistence`)
  — calls bare `init_db()` with **no isolation fixture**. `tests/conftest.py:13-14` pins `DATA_DIR`
  to the repo's own `data/`, so this test creates/writes the real `data/backtests.db` used by the app
  and by other suites. Test-order dependence: it silently depends on (and pollutes) shared state;
  concurrent pytest runs (xdist) or an unlucky leftover row with the same timestamped id can break it.
  Every other test in this file properly isolates via `isolated_db`/`legacy_db` — this one predates
  them. Fixture-hiding-production-behavior category: it can also mask migration failures because the
  real DB may already be migrated.
- **minor** `tests/test_database_migrations.py:432,487,519,547,568` — `database._DB_INITIALIZED = False`
  mutates module global state directly (no monkeypatch, no restore). Because `_DB_INITIALIZED` stays
  `False` after these tests return, any later test that calls `init_db()`-dependent code without its
  own isolation will re-run migrations against whatever `ENGINE` is current. Works today only because
  every consumer sets up its own engine; fragile ordering coupling.
- **minor** `tests/test_database_migrations.py:245-264` (`fake_exec`) — the race simulation patches
  `database.Session.exec` on the class itself and restores in `finally`; if an assertion inside the
  `try` fails the restore happens, good — but the patch leaks to *all* sessions created concurrently
  in-process during that window. Single-threaded here, acceptable; noted as a sharp-edged technique.
- **minor** `tests/test_database_migrations.py:120` — `first == {"migrated": 1, ...}` pins the full
  summary dict; fine, but `test_migrate_json_tops_up_empty_equity_curve_when_skipping:603` documents
  that "skipped" rows can be re-reported as `"migrated": 1` on top-up — the two tests encode subtly
  different semantics for the same counter (top-up counts as migrated even though row existed). Not
  wrong, but confusing contract worth a comment in production code.

(b) Security issues: none found.

(c) Baseline smells:
- **Duplicated Code (minor)** `isolated_db` fixture (140-160) vs `legacy_db` (345-360) vs the same
  fixture re-implemented in `test_live_execution_safety.py:623` and `test_ctrader_live_fix.py:237`
  (four copies across files with slight variations). Extract into conftest.
- **Mysterious Name (minor)** — section comments name "Finding 4/5/6/7/8" (181, 229, 281, 317, 439),
  referencing an external review document not in the repo; meaningless to future readers.

(d) Maintainability hot spots: file is well structured overall; the non-isolated first test is the
main liability.

### tests/test_portfolio_optimizer_oos.py

(a) Bugs / correctness risks:
- **major** `tests/test_portfolio_optimizer_oos.py:198-225` (`test_split_equity_curves_with_train_months`)
  — encodes **unimplemented behavior as passing**: docstring admits "train_months is reserved but not
  yet implemented", asserts `train_months=12` is ignored (11 train points instead of 12), and leaves a
  TODO. This is a placeholder test that gives false confidence the walk-forward split works with
  train_months; combined with `test_portfolio_optimizer_with_train_months_oos_split` (509-554), whose
  docstring claims "train_months creates proper IS/OOS split" but which **never passes train_months**
  (line 543-547 passes only `test_months=6`) — copy-paste error: the test does not exercise what its
  name/docstring promise. Misleading test pair.
- **minor** `tests/test_portfolio_optimizer_oos.py:112-116` — `mock_load` distinguishes strategies by
  call order (`call_count[0] == 1`); if production ever loads curves in a different order or caches,
  strategy identities silently swap and the test still passes (order-coupled stub). Same pattern at
  171-173 and 531-535. Prefer keying off `backtest_id`.
- **minor** `tests/test_portfolio_optimizer_oos.py:37` — `_parse_time_to_ts("2024-01-01T00:00:00Z")`
  asserted as `1704067200 <= ts <= 1704153600` — a 24-hour-wide window for what should be an exact
  value (1704067200). Overly loose assertion hides off-by-hours bugs (e.g. tz-naive parsing treated as
  UTC vs local). Should be exact equality.
- **minor** `tests/test_portfolio_optimizer_oos.py:503` — `np.testing.assert_raises(ValueError)` used
  instead of `pytest.raises`; works, but nonstandard and easy to misuse (no match support).
- **minor** `tests/test_portfolio_optimizer_oos.py:134,188` — `assert "status" not in result or
  result.get("status") != "error"` — convoluted compound that tolerates a result containing
  `status == "error"` only if key absent... it's logically equivalent to "if status present it must not
  be error" but reads like a tautology trap; simpler: `result.get("status") != "error"`.

(b) Security issues: none found.

(c) Baseline smells:
- **Duplicated Code (minor)** — seconds_per_month curve-building boilerplate repeated in 5+ tests
  (51, 86, 155, 204, 227, 244, 509); extract a `_monthly_curve(...)` factory.
- **Speculative Generality (minor)** — `train_months` parameter tested-as-unimplemented (198-225)
  suggests API surface ahead of implementation; either implement or drop from tests/docs.

(d) Maintainability hot spots: the train_months pair (198-225 + 509-554) is where a reader will be
misled; fix naming or implement.

### tests/test_ctrader_adapter.py

(a) Bugs / correctness risks:
- **minor** `tests/test_ctrader_adapter.py:71-95` (`test_connect_override_limits_attempts`) — relies on
  `asyncio.TimeoutError` raised within `connect_timeout_seconds=0.01`; tight timing budget is
  intentionally tiny, but on a loaded CI box the single attempt's internal steps could exceed other
  awaits — acceptable, low risk. Also asserts `attempts == 1`, good mutant-killing shape.
- **minor** `tests/test_ctrader_adapter.py:105-107,129-132` — payload-type attributes set to magic
  negatives (`_heartbeat_payload_type = -1`, etc.) mimic protobuf constants; brittle coupling to
  production attribute names, but that's inherent to unit-testing this adapter.
- Nothing else: strong file — real state-machine assertions (stale callbacks 284-303, refresh races
  305-391, cancellation teardown 482-523, disconnect timeout 526-556).

(b) Security issues:
- **minor (positive note)** `tests/test_ctrader_adapter.py:246-281` verifies refreshed tokens land in
  `.accounts.env`; sibling file checks 0600 perms (see below). No secrets committed in tests.

(c) Baseline smells:
- **Duplicated Code (minor)** — the `CTraderLiveAdapter(app_id="app", ..., account_id="46736452")`
  constructor block repeats ~15 times; extract builder. Same smell family as sibling files.

(d) Maintainability hot spots: none significant.

### tests/test_optimizer.py

(a) Bugs / correctness risks:
- **minor** `tests/test_optimizer.py:453` — `is_train = config.start_date.startswith("2024-01-01T00:00:00")`
  couples the fake backtest metric selection to string formatting of the produced datetime; if
  `_split_backtest_period` changes isoformat precision the fake silently returns test metrics for all
  calls and assertions on train stats fail confusingly. Acceptable but brittle.
- **minor** `tests/test_optimizer.py:480` — `assert any(trial["penalty_applied"] for trial in
  payload["top_trials"])` — with window=1 producing trade_count=0 (penalized), this holds, but it never
  verifies *which* trial was penalized nor that best_params excludes window=1 explicitly; a regression
  that penalizes everything equally would pass. Moderate assertion strength only.
- Nothing else notable; coverage-validation-before-study tests (278-406) are excellent negative-path
  tests using forbidden-call guards.

(b) Security issues: none found.

(c) Baseline smells:
- **Duplicated Code (minor)** — the four-line `monkeypatch.setattr(optimizer_module,
  "find_strategy_entry", lambda _: SimpleNamespace(file_path=..., slug=...))` block appears 7 times
  (289-296, 338-345, 374-381, 418-425, 502-509, 545-552...); extract helper next to
  `_allow_bar_coverage_preflight`.
- **Data Clumps (minor)** — `(backtests_dir, optimizations_dir, data_dir)` triple set up identically in
  most tests; a fixture would remove ~30 lines/file.

(d) Maintainability hot spots: none significant beyond duplication.

### tests/test_dukascopy_downloader.py

(a) Bugs / correctness risks:
- **minor** `tests/test_dukascopy_downloader.py:131,215,283,355` — `monkeypatch.setitem(sys.modules,
  "aiohttp", fake_aiohttp)` replaces aiohttp globally per-test; safe under monkeypatch restoration, but
  if the module was already imported by another test in-process, the swap changes identity mid-suite
  for anything holding a late-bound reference (`import aiohttp` inside function bodies). Works, noted
  as a global-state hammer.
- **minor** `tests/test_dukascopy_downloader.py:167,241,312,387` — `native.asyncio.run(...)` reaches
  asyncio through the module attribute rather than importing `asyncio` directly; harmless but obscure.
- **minor** `tests/test_dukascopy_downloader.py:182` — `assert sleep_calls == [2.0]` pins throttle
  backoff doubling (1000→2000ms) implicitly; boundary values elsewhere (cap 10s, cooldown 15s) are
  pinned at 326-404 — good. No issues.

(b) Security issues: none found (JNLP URL is example.com; JVM path trust checks tested positively at
443-472 — good security-relevant coverage of `_is_trusted_custom_jvm_path`).

(c) Baseline smells:
- **Duplicated Code (major, within file)** — `FakeConnector`/`FakeTimeout`/`FakeClientSession` +
  `fake_aiohttp` SimpleNamespace block repeated verbatim 4× (105-130, 189-214, 257-282, 329-354);
  extract module-level factory.
- **Duplicated Code (minor)** — `fake_fetch_one` signature-and-discard boilerplate repeated 4×.

(d) Maintainability hot spots: the quadruplicated aiohttp fake is the main cost.

### tests/test_live_daemon.py

(a) Bugs / correctness risks:
- **minor** `tests/test_live_daemon.py:77-78,139-143,213-218,231-233,241-243,370-371,395-396,421-422,438-441,456-459`
  — many tests reach directly into daemon module-global registries
  (`_CTRADER_AUTH_BLOCKED_ACCOUNTS.clear()` etc.) without fixture encapsulation; a failure mid-test can
  leave entries behind and leak into later tests sharing those globals. A setup/teardown fixture would
  make isolation structural rather than manual.
- **minor** `tests/test_live_daemon.py:311,316,337-345` — event-loop synchronization via
  `await asyncio.wait_for(..., timeout=0.1)` and `await asyncio.sleep(0)`: deterministic enough
  (event-driven waits, not sleeps-on-production-paths), but the 0.1s budgets can flake on extremely
  slow CI. Low risk; pattern is correct.
- **positive** 472-512 (`test_discord_poll_does_not_claim_live_signal_before_deployment_routing`) is a
  well-designed guard using `pytest.fail` inside forbidden persistence hooks.

(b) Security issues: none found.

(c) Baseline smells:
- **Duplicated Code (minor)** — blocked-account clear/assert boilerplate repeated across ~8 tests; the
  `_FailingAdapter`/`_TrackingAdapter` dataclass shapes could unify with inline ones (81-96, 295-298).
- **Mysterious Name (minor)** — `daemon._DISCORD_SERVICE_AUTH_BLOCKED_IDS` vs
  `_DISCORD_AUTH_BLOCKED_ACCOUNTS` vs `_CTRADER_AUTH_BLOCKED_ACCOUNTS`: three parallel registries the
  tests must keep straight; production design smell mirrored in tests.

(d) Maintainability hot spots: global registry management scattered through the file.

### tests/test_ctrader_live_fix.py

(a) Bugs / correctness risks — **this file contains the worst offenders in the chunk: three vacuous
tests that assert nothing about production code while their names claim live-position-safety coverage**:
- **critical** `tests/test_ctrader_live_fix.py:268-281` (`test_ctrader_positions_signed_qty_for_sell`)
  — Docstring: "SELL side should be stored as negative qty." Body: builds local `expected = 5.0`,
  then asserts `-expected < 0` and `expected > 0`. Pure arithmetic tautology; the adapter's sign
  convention is never exercised. A regression flipping SELL positions positive would pass this test.
  False confidence in a live-trading money path.
- **critical** `tests/test_ctrader_live_fix.py:284-295` (`test_ctrader_positions_excludes_closed_positions`)
  — Docstring: "get_positions ... only returns open positions." Body: assigns
  `position_status_open = 1`, `position_status_closed = 2`, asserts they differ, citing "(per the
  adapter code)". Asserts two distinct local literals are distinct; `get_positions` is never invoked.
  Filters-out-closed-positions behavior is completely unprotected.
- **major** `tests/test_ctrader_live_fix.py:254-265` (`test_ctrader_positions_symbol_id_mapping`) —
  Docstring: "get_positions must map symbol IDs to names." Body: sets
  `adapter._symbol_name_to_id = {"EURUSD": 1}` then reads the same attribute back — tests dict
  assignment, not mapping logic; no conversion function called.
- **minor** `tests/test_ctrader_live_fix.py:9-16` — duplicated imports: `datetime, timezone`, `Any`
  imported twice (lines 9-12 then again 15-16). Dead code, lint-visible.
- **minor** `tests/test_ctrader_live_fix.py:222` — `time.sleep(0.2)` then asserting the subprocess is
  still blocked on the flock: timing-based; a very slow CI could see the child not yet started (still
  passes — poll() is None covers both) but conversely a broken child that exits early *fails* correctly.
  The real risk is the fixed 0.2s being too short for the child to reach the lock on loaded machines,
  making the "blocked" assertion pass vacuously (child hasn't attempted the lock yet). Flaky-timing
  smell; better to gate on a sentinel written by the child after first lock attempt.
- **minor** `tests/test_ctrader_live_fix.py:162-167` — hand-rolled save/restore
  (`original = mod._write_env_keys; mod._write_env_keys = boom; try/finally`) instead of
  `monkeypatch.setattr`; inconsistent with the rest of the suite and skips pytest's safety net.

(b) Security issues:
- **positive** 179-193 pins 0600 permissions on the token env file — genuinely useful security test.
- **positive** 196-233 validates cross-process file locking around token persistence.

(c) Baseline smells:
- **Speculative Generality / dead tests** — the three position tests above are dead weight pretending
  to be coverage; delete or replace with real `get_positions` mapping tests (the adapter exposes the
  data needed).
- **Duplicated Code (minor)** — LiveSignal/LiveDeploymentConfig construction blocks repeated across
  the roundtrip and dedupe tests (301-348, 354-497); extract factory.

(d) Maintainability hot spots: lines 254-295 — the vacuous trio; highest-priority fix in this chunk
alongside the test_live_api double-stub.

---

## Group 3 — remaining mid-size files

### tests/test_live_preflight.py

(a) Bugs / correctness risks:
- **minor** `tests/test_live_preflight.py:342-344,363-365` — the "default .accounts.env untouched"
  guard snapshots `preflight.DEFAULT_ACCOUNTS_ENV` contents before and compares after; good intent,
  but if that default file doesn't exist on a dev machine both reads yield `None` and the assertion
  passes without ever proving non-interference (a regression writing a *new* default file would also
  pass — `default_after == default_before == None` only holds if still absent... actually a new file
  would make after≠None so it WOULD fail — the residual weakness is only that the check is
  environment-dependent rather than forced into tmp). Judgement call, low severity.

(b) Security issues: none found. Positive: strict boolean parsing tests (19-52) explicitly prevent
"CTRADER_DEMO garbage implicitly selects live" — security-relevant coverage done well.

(c) Baseline smells: none significant. Clean file overall.

(d) Maintainability hot spots: none.

### tests/test_cache_manager.py

(a) Bugs / correctness risks:
- **minor** `tests/test_cache_manager.py:132` — `time.sleep(0.05)` inside the fake build to force
  concurrency overlap; deterministic enough with an 8-thread barrier, but it's a real sleep in tests.
  Acceptable; alternative is an event-gated build.
- Nothing else — thread-safety test (123-159) is exemplary: barrier + single-build assertion +
  serial-tag uniqueness.

(b) Security issues: none found.

(c) Baseline smells:
- **Duplicated Code (minor)** — `_catalog_payload` fixture reuse is good, but the four disk-based
  normalization tests (162-345) repeat the same write-catalog.json/fail_build/assert-merged shape;
  could parametrize over raw payloads.

(d) Maintainability hot spots: none significant.

### tests/test_llm_providers.py

(a) Bugs / correctness risks:
- **minor** `tests/test_llm_providers.py:220 vs 227` — timeout test uses 0.05s budget against a 0.2s
  sleep: ratio is safe locally, but under heavy CI load the *outer* asyncio loop could be starved
  such that the sleep completes first? No — event-loop timers are monotonic; this is sound. No issue.
- Nothing else; error-path coverage (missing executable, timeout, non-zero exit, empty output, missing
  SDK, auth failure, unsupported model) is thorough and each maps to a distinct message.

(b) Security issues: none found (`NVIDIA_API_KEY` set to dummy "test-key"; subprocess calls all
monkeypatched out — no real binary invoked).

(c) Baseline smells:
- **Duplicated Code (major, within file)** — the `FakePermissionHandler`/`FakeClient` scaffolding is
  redefined in 5 of 7 SDK tests (136-216, 219-255, 258-297) with small variations; extract builders.

(d) Maintainability hot spots: SDK test scaffolding duplication.

### tests/test_catalog.py

(a) Bugs / correctness risks: none found — grouping/aliasing/merge behaviors pinned precisely with
real parquet fixtures (no mocking of pandas); strong file.

(b) Security issues: none found.

(c) Baseline smells:
- **Duplicated Code (minor)** — jan/feb tick DataFrame construction repeated across 4 tests
  (100-143, 146-186, 189-239, 242-283); extract factory parameterized by symbol/timeframe.

(d) Maintainability hot spots: none significant.

### tests/test_risk_engine.py

(a) Bugs / correctness risks: none found — expected values verified arithmetically consistent
(risk 1% × balance 10k ÷ stop distance 2.0 = 50 units at lines 42-44; exposure-cap rejection and
wrong-side-stop rejection paths asserted with explicit rejection reasons). Good mutant-killing shape.

(b) Security issues: none found.

(c) Baseline smells:
- **Duplicated Code (minor)** — RiskConfig construction repeated verbatim 5× (17-24, 58-65, 93-100,
  128-135); sibling file test_signal_bots_risk.py solved this with a `_risk_engine()` helper — adopt it
  here.

(d) Maintainability hot spots: none.

### tests/test_daily_equity.py

(a) Bugs / correctness risks: none found — NaN/inf skipping (124-145), invalid exit-time skipping
(147-166), curve-preservation rules (199-244) all assert exact values.

(b) Security issues: none found.

(c) Baseline smells:
- **Duplicated Code (minor)** — inline `Trade(...)` construction for invalid trades repeats fields;
  `make_trade()` helper exists but isn't extended with overrides for exit lists.
- **Dead code (trivial)** — `if __name__ == "__main__": pytest.main(...)` tail (268-269): dead in CI
  context, harmless.

(d) Maintainability hot spots: none.

### tests/test_models.py

(a) Bugs / correctness risks:
- **minor** `tests/test_models.py:187-192` — `test_file_roundtrip` uses `tempfile.TemporaryDirectory()`
  instead of pytest's `tmp_path` used elsewhere; inconsistent isolation style (still safe).

(b) Security issues: none found.

(c) Baseline smells:
- **Inconsistent fixture style** as above; otherwise clean roundtrip-focused file.

(d) Maintainability hot spots: none.

### tests/test_backtest_grid.py

(a) Bugs / correctness risks: none found — subprocess timeout/launch-failure isolation per cell is
well tested via monkeypatched `subprocess.run`; date-resolution regression test (164-231) captures
kwargs through to `attach_daily_equity`.

(b) Security issues: none found. Positive: grid runs cells via subprocess with timeouts asserted
isolated per-cell.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_preflight.py

(a) Bugs / correctness risks:
- **minor** `tests/test_preflight.py:164-171,180-189` — CLI tests patch `run_live_preflight`
  wholesale, so `main()`'s exit-code mapping is tested but nothing prevents the mock from diverging
  from real result semantics; acceptable layering choice.
- **minor** `tests/test_preflight.py:108-113,122-127,129-133` — YAML written with un-dedented
  triple-quoted strings relying on leading-newline YAML tolerance; works because YAML skips the first
  newline, but fragile formatting compared to sibling file's `dedent` usage.

(b) Security issues: none found (dummy credentials only).

(c) Baseline smells:
- **Duplicated Code (minor)** — config-writing boilerplate repeated; overlap with
  test_live_preflight.py which covers overlapping ground (two preflight files = Divergent Change
  candidate; consider merging).

(d) Maintainability hot spots: none.

### tests/test_dukascopy_discovery.py

(a) Bugs / correctness risks:
- **minor** `tests/test_dukascopy_discovery.py:213` — asserts probe dates derived from
  `date.today()` are `<= today`; date-dependent but always true by construction of the helpers —
  borderline tautology (it does catch a helper returning future-dated probes, which is its point).
  Fine.
- **positive** 168-202 pins persistence-before-interrupt semantics — genuinely useful crash-safety
  coverage.

(b) Security issues: none found.

(c) Baseline smells:
- **Mysterious Name (trivial)** — doubled comment banner lines 64-66 (`# ----` twice) look like a
  merge artifact.

(d) Maintainability hot spots: none.

### tests/test_risk_consistency.py

(a) Bugs / correctness risks:
- **minor** `tests/test_risk_consistency.py:55` — `timestamp=datetime.now()` tz-naive (repo checklist:
  tz-naive datetimes). LiveSignal presumably coerces or stores naive; every sibling file uses
  `datetime.now(timezone.utc)`. Inconsistency could hide tz-handling regressions this very suite is
  meant to protect (cf. `get_live_signal_timestamp` UTC normalization tested elsewhere).
  Same at line 94.
- **minor** `tests/test_risk_consistency.py:110-130` (`test_backtest_engine_allows_wrong_stop_direction`)
  — documents that the backtest engine silently sizes positions using a stop on the wrong side of
  entry (buy @100, stop 105 → positive size). The live engine rejects this (79-107). The asymmetry is
  asserted as correct behavior ("allows ... by default"); combined with
  `metadata={"enforce_stop_direction": True}` opt-in seen in test_risk_engine.py:108, backtests can
  produce sizes from nonsensical risk distances unless callers opt in. At minimum the divergence
  deserves a loud production comment; flagging as encoded-questionable-default.

(b) Security issues: none found.

(c) Baseline smells: none beyond above.

(d) Maintainability hot spots: none.

### tests/test_signal_bots_risk.py

(a) Bugs / correctness risks: none found — sizing-from-stop-distance identity (67-70), ATR fallback,
trailing-stop raise-after-price-move, and exposure-limit reduction all assert relationships rather
than constants where appropriate. Solid integration-style file.

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_sentry_config.py

(a) Bugs / correctness bugs: none found. This file is security-positive: PII redaction
(`Authorization`, `api_key`, refresh-token, URL query secrets) is asserted end-to-end through
`_before_send` (50-79), including "secret must not appear anywhere in message".

(b) Security issues: covered above — redaction coverage present and correct; no leaks introduced by
the tests themselves.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

---

## Group 4 — small files

### tests/test_backtest_matrix.py

(a) Bugs / correctness risks:
- **minor** `tests/test_backtest_matrix.py:93` — pins the odd artifact name `matrix_matrix.csv`
  ("matrix_" prefix + "_matrix.csv" suffix doubling). The test faithfully freezes a confusing
  production naming choice (Mysterious Name); flag for product-level rename rather than test change.

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_runner_cli.py

(a) Bugs / correctness risks:
- **minor** `tests/test_runner_cli.py:86` — `assert payload["stats"]["total_return"] == 0.05` uses
  exact float equality; passes only because the stat is derived from integer-ish PnL division
  (5000/100000 rounds to the canonical 0.05 double), but an equity-ratio-derived implementation
  (100000×1.05) would produce 0.050000000000000044 and fail. Use `pytest.approx`.

(b) Security issues: none found.

(c) Baseline smells:
- **Speculative Generality (trivial)** — `_DummyEngine.validate_config` returns unused `[]`; fine.

(d) Maintainability hot spots: none.

### tests/test_download_grid_market_data.py

(a) Bugs / correctness risks:
- **major** `tests/test_download_grid_market_data.py:92` — `dl.DukascopyNativeDownloader =
  MagicMock()` assigns directly onto the production module **without monkeypatch** and is never
  restored: the patch leaks process-wide after this test ends. Sibling file
  `test_grid_market_data_download.py:71` does the identical stub correctly via
  `monkeypatch.setattr`. Any later test touching that module inherits a MagicMock. Fix is one-line.
- **minor** `tests/test_download_grid_market_data.py:146-164,171-189` — manual `sys.argv`
  save/swap/restore in try/finally instead of `monkeypatch.setattr(sys, "argv", ...)`; also swallows
  `SystemExit` while asserting code 0 — if `main()` ever stops raising SystemExit the tests silently
  stop checking the exit path.

(b) Security issues: none found.

(c) Baseline smells:
- **Inconsistent technique** as above (raw assignment vs monkeypatch in the sibling file) — Shotgun
  Surgery risk when the constructor signature changes.

(d) Maintainability hot spots: the leaked MagicMock is the item to fix first in this file.

### tests/test_grid_tester_sqlite.py

(a) Bugs / correctness risks:
- **major** `tests/test_grid_tester_sqlite.py:95,132,154` — three tests call bare `init_db()` with no
  isolation fixture; same exposure as `test_database_migrations.py:35`: reads/writes the real
  repo-pinned `data/backtests.db` (conftest sets `DATA_DIR` to the repo `data/`). Timestamp-suffix ids
  (`%H%M%S`) additionally collide within the same second across repeated runs — the tests filter by
  id afterward, so collisions mostly cause flaky count mismatches rather than corruption, but the
  shared-DB dependence stands.
- **major** `tests/test_grid_tester_sqlite.py:152-182`
  (`test_get_grid_results_returns_newest_first`) — name and docstring promise newest-first ORDERING
  verification, but the body only asserts both ids are *members* of the result list
  (`id_1 in ids`, `id_2 in ids`). A regression sorting oldest-first (or unordered) passes. Misleading
  vacuous-ordering test; should assert `ids.index(id_2) < ids.index(id_1)` (or full slice equality).
- **minor** `tests/test_grid_tester_sqlite.py:37-90` — `run_strategy_grid` test monkeypatches scipy
  out via `sys.modules` fakes; heavy but scoped by monkeypatch. The uuid-suffixed slug guards against
  real-DB row collisions — evidence the authors knew about shared-state risk yet still left the three
  `init_db()` tests unisolated.

(b) Security issues: none found.

(c) Baseline smells:
- **Mysterious Name** as above (ordering claim without ordering check).

(d) Maintainability hot spots: the three unisolated `init_db()` tests.

### tests/test_ramjet_signal_processor.py

(a) Bugs / correctness risks: none found — SL/TP/trailing-config port asserted against ATR-derived
identities; rejects non-BUY actions; default YAML config pinned (repo-relative path, stable).

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_request_logging_middleware.py

(a) Bugs / correctness risks: none found — fixture properly adds/removes handler and restores level;
500-path test intentionally lets RuntimeError propagate through TestClient and asserts exc_info
captured. Clean.

(b) Security issues: none found (positive: middleware logs avoid leaking bodies; only method/path/
status/duration asserted).

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_resample.py

(a) Bugs / correctness risks: none found — OHLCV aggregation rules verified bar-by-bar against source
frame (64-73); invalid timeframe raises.

(b) Security issues: none found.

(c) Baseline smells:
- **Duplicated Code (trivial)** — `import numpy as np` inside two fixtures instead of top-level.

(d) Maintainability hot spots: none.

### tests/test_mt5_parity_artifact_loader.py

(a) Bugs / correctness risks: none found — bid-column fallback, EUR/USD unit conversion (80 000 /
0.8), MT5 lot rounding steps, and shifted-stop-distance fill anchoring all assert exact values; the
non-positive-stop-distance fallback (126-158) is a well-chosen edge case.

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_live_risk_sizing.py

(a) Bugs / correctness risks: none found — qty written back into metadata, zero-size blocking on max
positions, and precomputed-qty passthrough (`explicit_qty`) all pinned.

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_scheduler_signal_bot_health.py

(a) Bugs / correctness risks:
- **major** `tests/test_scheduler_signal_bot_health.py:42-63`
  (`test_signal_bot_health_check_job_detects_errors`) — everything is mocked and the only assertions
  are `mock_check.called` and `mock_upsert.call_count >= 2`. The test would stay green even if
  `run()` ignored the bots' `"status": "error"` entirely and recorded success — the very regression
  its name claims to catch is invisible. Assert the status/error content passed *into*
  `upsert_background_task_record` (e.g. `mock_upsert.call_args`). Same weakness milder in the happy
  test at 15-39 (asserts nothing about recorded outcome).
- **minor** `tests/test_scheduler_signal_bot_health.py:10-12`
  (`test_signal_bot_health_check_job_imports`) — asserts `callable(run)`: an import-smoke test
  asserting near-nothing; harmless but pure tautology-class coverage.

(b) Security issues: none found.

(c) Baseline smells:
- **Excessive Mocking** as above — the job's entire interaction with persistence is stubbed, so the
  tests verify wiring, not behavior.

(d) Maintainability hot spots: this file overall; fold real argument assertions into the existing
mocks rather than adding more tests.

### tests/test_signal_bot_health_check.py

(a) Bugs / correctness risks:
- **minor** `tests/test_signal_bot_health_check.py:22` — `len(result["results"]) >= 5` with comment
  enumerating expected bots: lower-bound assertion tolerates registry bloat/duplication silently.
  Prefer exact set comparison.
- Depends on repo-tracked `signal_bots/*/config.yaml` files existing (18-26); environment-stable
  within the repo, fine.

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_service_discovery.py

(a) Bugs / correctness risks: none found — env-file discovery maps token/channel keys correctly;
dummy tokens only.

(b) Security issues: none found (positive: tokens are obviously fake placeholders).

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_shared_logging.py

(a) Bugs / correctness bugs: none found — pins timestamp format defensively (regex + exact), and
coercion of corrupt fields (`lineno="invalid"` → `line == 0`) is exactly the right robustness check.

(b) Security issues: none found.

(c) Baseline smells: none.

(d) Maintainability hot spots: none.

### tests/test_capm.py

(a) Bugs / correctness risks: none found — synthetic linear relationship recovers β=1.5, α=0.0005×252,
R²≈1 with tight tolerance; alignment-intersection and minimum-observation guard tested.

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_orchestrator.py

(a) Bugs / correctness risks: none found — layer ordering and continue-on-e2e-failure behavior pinned
via `_run_layer` stub; exit-code mapping asserted both ways.

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_risk_parity_optimizer.py

(a) Bugs / correctness risks:
- **minor** `tests/test_risk_parity_optimizer.py:36` — `assert set(result["best_symbols"]) in
  [{...}, {...}]` accepts either universe as "best"; the test cannot distinguish a ranking regression
  that flips the winner. Compute-free looseness; acceptable but weaker than it looks.

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_qc_risk_parity_artifact_loader.py

(a) Bugs / correctness risks: none found — builds a full local parquet universe from TICKERS and
verifies column order/close/open extraction exactly.

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_discord_signal_parsing.py

(a) Bugs / correctness risks:
- **minor** `tests/test_discord_signal_parsing.py:43-49` —
  `assert signal is None or signal.symbol != "AT"` is a disjunction that also passes if the parser
  returns a *different* garbage symbol (e.g. "MARKET"). Intent is "don't treat lowercase word after
  action as symbol"; tighter: assert `signal is None` (given current contract) or enumerate allowed
  symbols.

(b) Security issues: none found.

(c) Baseline smells: none significant beyond above.

(d) Maintainability hot spots: none.

### tests/test_native_engine_execution.py

(a) Bugs / correctness risks: none found — alias extraction (`sl_price`/`tp_price`), fixed-risk sizing
identity (500/(100−95)=100 units), and MT5 tick-metadata sizing (1.0 lots) all exact.

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_live_config.py

(a) Bugs / correctness risks: none found — structured-warning assertion via `call_args.kwargs["extra"]
["data"]["account_id"]` pins the logging contract precisely.

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_ctrader_deployment.py

(a) Bugs / correctness risks: none found — ranking by composite live score and plan-building
(account targeting, PAUSED default, source_backtest_id linkage) asserted.

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_dolthub_manager.py

(a) Bugs / correctness risks: none found — legacy flat-export migration to canonical
`AAPL/1d.parquet` layout asserted including content preservation and removal of the old file.

(b) Security issues: none found.

(c) Baseline smells: none.

(d) Maintainability hot spots: none.

### tests/test_database_init.py

(a) Bugs / correctness risks:
- **minor** `tests/test_database_init.py:17` — `DATA_DIR` is monkeypatched but `ENGINE` is not
  rebuilt, so the patch is cosmetic (init_db's create_all/migrate are faked anyway, so nothing
  touches disk — but the setup suggests isolation it doesn't provide). Trivial.

(b) Security issues: none found.

(c) Baseline smells: none significant.

(d) Maintainability hot spots: none.

### tests/test_backtest_matrix_runner.py

(a) Bugs / correctness risks:
- **minor** `tests/test_backtest_matrix_runner.py:21-24` — the test pre-writes a fake output JSON
  whose `stats.daily_returns` shape is invented by the test itself, then `fake_load_daily_rows`-style
  data is asserted through `result["daily_rows"]`; the roundtrip through real parsing is only as
  strong as the fixture's fidelity to what `run_backtest_grid` actually writes. The URL/id link
  assertions (67-69) are exact and valuable; the daily-rows assertion (`daily_rows[0]["run_id"]`)
  is weak relative to the fixture control. Low risk.
- **minor** `tests/test_backtest_matrix_runner.py:43-64` — `_run_strategy_matrix_cell` invoked with
  17 positional/keyword args inline; keyword-only explosion mirrors production signature (Data
  Clump in production API reflected here).

(b) Security issues: none found.

(c) Baseline smells:
- **Data Clumps (minor)** — the 17-parameter call site duplicates the production signature verbatim;
  a config object would shrink both.

(d) Maintainability hot spots: none significant.

### tests/test_grid_market_data_download.py

(a) Bugs / correctness risks: none found — this file does correctly what its sibling
`test_download_grid_market_data.py:92` does wrongly: `monkeypatch.setattr(dl,
"DukascopyNativeDownloader", ...)` at line 71 (scoped, auto-restored). Regression tests for m30
normalization (51-54), sort-key ValueError abort (57-61), unknown-timeframe isolation (64-65,
68-86), and bid-column backfill with NaN (23-48) are all precise.

(b) Security issues: none found.

(c) Baseline smells:
- **Duplicated Code (minor)** — normalization/sort tests overlap heavily with sibling file
  `test_download_grid_market_data.py` (both cover `normalize_timeframe_value`, `normalize_symbol`,
  `parse_csv_list`); consolidate to one home.

(d) Maintainability hot spots: none.

### tests/test_live_smoke.py

(a) Bugs / correctness risks:
- **minor** `tests/test_live_smoke.py:79-95` (`test_discord_replay_smoke_routes_to_fake_order`) —
  runs `run_smoke(mode="discord-replay")` against real code paths with a 1s timeout and asserts a
  fabricated order id `"discord-smoke-order"`; good smoke-shape coverage, though it depends on the
  replay path remaining offline-safe by construction (no network). If someone later makes the
  discord-replay mode hit Discord, this test would attempt real I/O. A comment or explicit env guard
  would harden it.
- **minor** `tests/test_live_smoke.py:42-43` — secret-absence check greps `str(payload)` for
  "api_key"/"secret" substrings; a field literally named e.g. "co_secret_ratio" would false-positive,
  and non-obvious key names slip through. Acceptable heuristic for a smoke report.

(b) Security issues:
- **positive** 24-44 asserts the serialized smoke report carries no credential-shaped fields — a
  genuine leak-prevention test.

(c) Baseline smells:
- **Mysterious Name (trivial)** — three separate import statements from the same module (8-12);
  collapse to one.

(d) Maintainability hot spots: none.

---

## Cross-file summary

Highest-value fixes in priority order:
1. **Vacuous position-safety tests** — `tests/test_ctrader_live_fix.py:254-295` (3 tests asserting
   arithmetic tautologies / attribute echo while claiming live-position sign, closed-position
   filtering, and symbol-ID mapping coverage). Critical because they sit on a live-money path.
2. **Leaked/unrestored patches** — `tests/test_download_grid_market_data.py:92` (MagicMock onto
   production module, no restore) and the un-isolated `init_db()` trio in
   `tests/test_grid_tester_sqlite.py:95,132,154` + `test_database_migrations.py:35` writing the real
   repo `data/backtests.db`.
3. **Misleading claim-vs-body mismatches** — `test_grid_tester_sqlite.py:152` (ordering test checks
   membership only), `test_portfolio_optimizer_oos.py:509` (train_months never passed despite name)
   and `:198-225` (unimplemented behavior enshrined), `test_scheduler_signal_bot_health.py:42`
   (error-detection test blind to error handling).
4. **Duplication debt** — aiohttp fake ×4 (dukascopy_downloader), keyset-pagination harnesses ×3
   (live_execution_safety), `isolated_db` fixture ×4 across files, LLM SDK scaffolding ×5,
   find_strategy_entry stub ×7 (optimizer).

Security-relevant positives worth preserving: Sentry PII redaction tests, cTrader 0600 token-file
permission + cross-process lock tests, strict CTRADER_DEMO boolean parsing, JVM trust-path checks,
and smoke-report secret-field absence.

No test in this chunk skips/disables itself (no `pytest.mark.skip`/`xfail` found), uses real network
or broker credentials, or contains mutable-default-argument bugs; subprocess usage in tests is either
monkeypatched or timeout-guarded (`test_ctrader_live_fix.py:228` waits with timeout).

### Finding counts by severity

| Severity | Count |
|----------|-------|
| critical | 2 |
| major | 8 |
| minor | 51 |
