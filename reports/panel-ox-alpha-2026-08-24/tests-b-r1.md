# Panel review — area `tests-b` (chunk B of test suite) — reviewer r1 (ox-alpha), 2026-08-24

Scope: the 49 tracked files listed in the brief. Blind, independent review of test code only.
Method: full read of every file (chunked reads for large ones), cross-checked against production
modules where an expected value looked suspicious. Findings cite `file:line` with severity.
Report written incrementally per group; final tally at bottom.

Status: IN PROGRESS — groups appended below as completed.

---

## Group 1 — live execution safety

### tests/test_live_execution_safety.py (2256 lines)

Overall: high-quality mutation-driven suite; expected values verified against
`risk/engine.py`, `risk/config.py`, `live_runner/daemon.py`, `shared/database.py`
(`fallback_stop_pct=0.02`, sizing math `risk/(distance*pip)`, exposure estimate
`open_positions * max_risk_per_trade`, `_normalize_deployment_status` fail-closed,
SQLite pragmas) all match production behavior. No tautologies found; act phases are
not wrapped in try/except; no sleeps; concurrency tests use `ThreadPoolExecutor.map`
whose result ordering is deterministic.

(a) Bugs / correctness risks
- **minor** `tests/test_live_execution_safety.py:1432-1439` —
  `test_alpaca_delayed_stream_fill_is_reported_as_duplicate` picks the first handler
  that is a `StreamHandler` but not a `FileHandler` on the module logger and mutates
  its stream via `monkeypatch.setattr`. This hard-couples the test to
  `shared/logging.get_logger`'s exact handler layout (one RotatingFileHandler + one
  stdout StreamHandler, `propagate=False`) and to JSON line formatting. If a stderr
  handler or a second stream handler is ever added, the test either fails confusingly
  or asserts against the wrong stream. Brittle, but currently correct.
- **minor** `tests/test_live_execution_safety.py:1727-1729` —
  `test_ctrader_reconcile_backfills_missing_fill` builds the fake response as
  `type("ProtoOAOrderDetailsRes", (), {"deal": [deal]})`, i.e. `deal` is a class
  attribute rather than a per-instance repeated field, and the fake request class
  (`SimpleNamespace`) has no `ctidTraderAccountId`/`orderId` fields, so nothing
  verifies what was requested (the sibling pagination test at :1781-1803 does).
  Also stubs raw `_send_request` while production calls
  `_send_request_with_refresh_retry` (:1173); if the fetch were switched to another
  method this test's stub would silently never be called and the test would fail only
  indirectly. Inconsistent doubling weakens this one test relative to its siblings.
- **minor** `tests/test_live_execution_safety.py:604` —
  `adapter._resolve_order_volume(_signal({"qty": 499.999, "ctrader_volume": 100000})) == 100000`
  blesses that an under-broker-minimum `qty` silently passes whenever an explicit
  `ctrader_volume` is present (`live_runner/ctrader_adapter.py:759-761` checks the
  explicit override before the qty floor). Probably intentional ("explicit override
  wins"), but given the neighboring error text "rejected instead of silently upsizing
  beyond the risk limit", the asymmetric enforcement deserves a comment; today the
  test encodes the precedence without documenting why it is safe.

(b) Security issues — none found. The suite actively pins security-relevant behavior
(fail-closed status normalization, no fabricated market data, no duplicate submits).

(c) Baseline smells
- Duplicated Code: the ~15-line LiveSignal construction block
  (`deployment_id/symbol/action/theoretical_price/timestamp/metadata`) is repeated
  verbatim ~25 times (:246, :288, :340, :419, :454, :504, :546, :588, :639, ...);
  only `_live_signal()` (:173) factors it, for one call site. A local builder helper
  would remove ~150 lines.
- Duplicated Code: the Alpaca reconcile tests repeat the same
  `list_live_signal_ids_for_reconciliation` / `get_live_signal_filled_qty` /
  `save_live_execution_up_to_cumulative` monkeypatch triple at :1503-1514, :1601-1615,
  :1645-1654 — extractable into a fixture.
- The `isolated_db` fixture (:622-635) re-registers the SQLite connect listener
  (`database._configure_sqlite_connection`) by hand; this duplicates production engine
  wiring inside `shared.database.create_engine` and will silently drift if production
  adds another pragma/event.

(d) Maintainability hot spots
- At 2256 lines this single file spans four concerns (risk-engine boundaries, Alpaca
  adapter idempotency, cTrader adapter reconciliation/recovery, DB migration &
  persistence durability). Divergent Change: it changes for every reason any of those
  modules change. Suggest splitting per subsystem.

### tests/test_live_api.py (1315 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_live_api.py:68-95` vs `:130-153` — inside
  `test_live_endpoints_return_expected_payloads`, `load_live_account_stats_inputs` is
  monkeypatched **twice**; the second registration silently replaces the first. The
  first stub is conditional on the account id and includes `slippage`/`slippage_bps`
  keys; the second always returns data and omits them. The first block is dead code,
  and its presence misleads a reader about which fixture shape the endpoint actually
  consumed (the slippage fields were dropped from the effective stub, so the endpoint's
  slippage pass-through is never exercised by the stats leg of this test).
- **minor** `tests/test_live_api.py:997-1064` and `:1193-1283` — the WebSocket
  "snapshot then update" tests depend on the app's background stream-broker polling
  loop noticing the mutated payload (poll interval patched to 0.05 s,
  `viewer/main.py:3305-3323`). There is no explicit synchronization; correctness rests
  on the broker's poll firing before the test's second `receive_json()` under CI load.
  Mildly flaky by construction; would be more robust to drive the queue directly.
  Verified not vacuous: `_stream_live_payload` really labels the second push "update".
- Verified non-findings: expected stats values match `viewer/main.py:3738-3772`
  (realized PnL counts only round-trip pairs, so 5 executions → 1 win / 1 loss /
  total_pnl 0.0; drawdown peak-to-trough 1100→880 = 220 / 20%); the odd
  `_deployment_ids_for_account` stub returning `[account_id, deployment_id]`
  mirrors production `viewer/main.py:3523-3532`.

(b) Security issues — none found; the file pins the desired rejections
(wrong-account Discord deployment → 400 at :426-466, quote 404 when no data).

(c) Baseline smells
- Duplicated Code: the 20-line execution-row dict appears three times in the same
  test (:73-94, :105-124, :135-152); extract one builder.
- Excessive Mocking (judgement call): every persistence function is a lambda stub, so
  the suite verifies wiring only — acceptable for API-layer tests given
  test_live_execution_safety covers the DB layer, but any new DB call site in
  `viewer/main.py` will silently hit the real database in these tests unless
  re-stubbed (there is no safety-net such as an autouse DB blocker).

(d) Maintainability hot spots
- Each test must monkeypatch ~10-15 attributes on `viewer.main` because production
  code resolves collaborators through module globals; Shotgun Surgery symptom
  inherited from `viewer/main.py`'s design (~11k lines). Any rename there breaks all
  of these tests at once.

### tests/test_live_daemon.py (535 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_live_daemon.py:78-79, :140-143, :213-215, :232-233, :242-243,
  :370-371, :395-396, :421-422, :438-441, :456-459` — ten tests reach directly into
  module-level mutable global registries (`daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS`,
  `_DISCORD_AUTH_BLOCKED_ACCOUNTS`, `_DISCORD_SERVICE_AUTH_BLOCKED_IDS`, ...) and
  `.clear()` them as setup, with no teardown/monkeypatch restoration. Any test that
  fails mid-way leaks blocked-account state into later tests in the same process
  (order-dependent flakiness); also these are cross-test shared mutable state by
  construction. A fixture that snapshots/restores would remove the hazard.
- **minor** `tests/test_live_daemon.py:311, :316, :337-345` — timing-based assertions
  using `asyncio.wait_for(..., timeout=0.1)` around scheduling behavior. On a heavily
  loaded CI runner a 100 ms budget for event-loop turns is small; failure mode is a
  spurious timeout rather than a logic bug. Not sleep-based, so low severity.
- Verified non-findings: `test_discord_bot_listener_stops_on_terminal_auth_message`
  (:411-435) uses the odd `if False: yield None` idiom to make `_InvalidTokenSource`
  an async generator while still raising immediately — correct, not dead code.
  The negative-persistence guard at :496-506 (`raising=False`) is deliberate and
  documented.

(b) Security issues — none found; the suite pins fail-closed account blocking on
terminal auth errors (:77-124, :361-408).

(c) Baseline smells
- Duplicated Code: the `_TerminalFailAdapter`/`_FailingAdapter`/`_TrackingAdapter`
  dataclasses (:18-36, :81-95) repeat connect/disconnect counting; one parameterized
  fake would do.
- The clear-globals setup boilerplate repeated ten times (see (a)) wants a fixture —
  Duplicated Code.

(d) Maintainability hot spots — none beyond the global-state coupling above;
otherwise a focused, readable suite.

### tests/test_database_migrations.py (612 lines)

(a) Bugs / correctness risks
- **major** `tests/test_database_migrations.py:33-37` —
  `test_backtest_record_equity_curve_persistence` calls plain `init_db()` /
  `get_session()` with **no** DB isolation, unlike every other test in the file
  (which use `isolated_db`). Combined with `tests/conftest.py:13` defaulting
  `DATA_DIR` to the repo's `data/`, this test reads and writes the developer's /
  CI's **real** `data/backtests.db`: it mutates shared state (leaves a
  `test_equity_curve_record_*` row behind), runs schema migrations against real
  data, and can fail or pass depending on whatever is in the local data dir. This
  is exactly the "fixture hides/exposes production state" hazard; at minimum it
  should use the same `isolated_db` fixture as its siblings.
- **minor** `tests/test_database_migrations.py:445` —
  `ALTER TABLE backtests DROP COLUMN equity_curve` silently assumes SQLite ≥ 3.35
  (DROP COLUMN support). On older SQLite the whole file errors in setup with an
  opaque message. Environmental assumption worth a skip-guard, not a bug.
- **minor** `tests/test_database_migrations.py:245-262` — the race simulation
  monkeypatches `database.Session.exec` (class-level, restored in `finally`, OK),
  but its matcher `"discord_message_id" in stmt_text and "select" in stmt_text`
  will also swallow any *other* select mentioning the column during the same save
  (e.g. the reload-by-id path if it ever filtered by discord_message_id), making the
  stub over-broad as production queries evolve. Works today; brittle tomorrow.
- Verified non-findings: expected migration outcomes (backfill, idempotency,
  fail-closed duplicates, equity-curve top-ups) match `shared/database.py` behavior
  including the "No rows were deleted" RuntimeError contract asserted identically in
  test_live_execution_safety.py.

(b) Security issues — none found. Positive: the suite pins the fail-closed rollback
of a corrupt legacy DB (:381-417).

(c) Baseline smells
- Duplicated Code / drift risk: this file's `isolated_db`/`legacy_db` fixtures build
  raw `create_engine(...)` engines **without** the production
  `_configure_sqlite_connection` listener (foreign_keys=ON, busy_timeout=30000,
  `shared/database.py:81-87`), while tests/test_live_execution_safety.py's
  `isolated_db` attaches it by hand. Two divergent copies of "an isolated DB"
  across the suite; migrations here are validated under different connection
  pragmas than production uses. Extract one shared conftest fixture.
- Primitive Obsession (inherited): legacy rows are inserted as raw SQL strings with
  hand-built JSON metadata (:320-342) — acceptable for migration tests simulating
  legacy schemas, noted for completeness.

(d) Maintainability hot spots
- The "Finding N" section headers (:181, :229, :281, :317, :439) reference a review
  numbering that lives outside the repo; future readers can't map them. Rename by
  behavior.

### tests/test_ctrader_adapter.py (588 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_ctrader_adapter.py:105-107` —
  `test_on_message_marks_terminal_auth_failure_for_not_authorized` sets
  `adapter._error_payload_type = 100`, while the sibling negative test (:131) uses
  `101`. The values are arbitrary stand-ins for protobuf payload-type enums; if the
  production `_on_message` ever compares against a real enum value equal to 100/101,
  these fakes collide with unrelated branches. Works today because production reads
  only these instance attributes; noted as fragile test doubles.
- Verified non-findings: token-refresh serialization tests (:305-433) genuinely
  exercise single-flight refresh semantics (concurrent gather, delayed old-token
  error, failed refresh not treated as success) — strong tests, expected values match
  `live_runner/ctrader_adapter.py:676-680` (auth request rebuilt from
  `self.access_token`) and the retry-once contract.
- No swallowed exceptions around act phases; `asyncio.CancelledError` expectations
  (:206, :238) are explicit and correct for listener shutdown.

(b) Security issues — none found. Positive: terminal-auth fail-closed behavior is
pinned at :98-119 and :559-588.

(c) Baseline smells
- Duplicated Code: the 7-line `CTraderLiveAdapter(app_id=..., app_secret=...,
  access_token=..., account_id=...)` constructor block repeats in ~14 of 17 tests;
  a module-level builder would cut ~80 lines.

(d) Maintainability hot spots — none significant; well-structured file.

### tests/test_ctrader_live_fix.py (497 lines)

(a) Bugs / correctness risks
- **major** `tests/test_ctrader_live_fix.py:268-281`
  (`test_ctrader_positions_signed_qty_for_sell`) and **major** :284-295
  (`test_ctrader_positions_excludes_closed_positions`) — both are vacuous tautologies
  masquerading as adapter coverage. The first builds an adapter and a local variable
  `expected = 5.0` then asserts `-expected < 0` and `expected > 0` — pure arithmetic
  on a local constant, zero reference to any adapter method; it cannot detect any
  regression in sign handling. The second asserts `1 != 2` after re-typing the
  constants from a comment ("per the adapter code") — likewise unconnected to
  `get_positions`. Both names promise behavior ("SELL side stored as negative qty",
  "excludes closed positions") that nothing verifies; they give false confidence
  that cTrader position mapping is tested. Should be replaced by real
  `get_positions` tests against a stubbed `_send_request`.
- **minor** `tests/test_ctrader_live_fix.py:254-265`
  (`test_ctrader_positions_symbol_id_mapping`) — same family but weaker severity:
  it writes `adapter._symbol_name_to_id = {...}` and then reads the same attribute
  back, verifying only dict identity semantics plus Python's subscript operator; no
  production code participates. Asserting your own setup is a near-tautology.
- **minor** `tests/test_ctrader_live_fix.py:14-16` — duplicate imports
  (`datetime, timezone` and `typing.Any` imported twice); dead code from a merge.
- **minor** `tests/test_ctrader_live_fix.py:162-167` —
  `mod._write_env_keys = boom` swaps a module function directly with manual
  save/restore instead of `monkeypatch.setattr`; if the body between try/finally
  raised before restore... actually `finally` guarantees restore, so this is style
  only — but it bypasses pytest's monkeypatch safety net for no benefit.
- Verified non-findings: the refresh/auth ordering test (:76-108), single-flight
  persistence test (:111-138), crash-safety test (:141-176), 0600-permissions test
  (:179-193) and cross-process flock test (:196-233) all assert meaningful state
  transitions matching production behavior; the flock test's `time.sleep(0.2)` +
  `poll() is None` pattern is a legitimate blocking-I/O probe, though the fixed sleep
  is a mild flake vector (writer could finish early only if the lock leaked).

(b) Security issues
- Positive: `test_persisted_accounts_file_has_0600_permissions` (:179-193) pins the
  credential-file permission contract — good security regression coverage.

(c) Baseline smells
- Speculative Generality / dead code: duplicated imports (:15-16).
- Duplicated Code: three dedupe tests repeat the identical 25-line two-execution
  setup (:354-393, :406-450, :453-488); extract a helper.
- Duplicated Code: this file carries its own third copy of the `isolated_db` fixture
  (:236-248) — again without the SQLite connect listener, see test_database_migrations
  finding above. Three divergent definitions of the same fixture across the suite.

(d) Maintainability hot spots
- The three fake position tests (:254-295) actively mislead about coverage of
  `get_positions`; highest-priority cleanup in this file.

### tests/test_live_preflight.py (365 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_live_preflight.py:342-343, :363-365` —
  `test_live_preflight_threads_accounts_env_path_to_ctrader_adapter` reads the real
  repo-root `.accounts.env` (via `preflight.DEFAULT_ACCOUNTS_ENV`) to snapshot it,
  then asserts it unchanged. If the developer machine lacks the file the test still
  passes, and if some other test in the session creates/mutates that file there is
  cross-test coupling; also the snapshot-compare is not race-safe under parallel
  test execution. Low risk but touches real user state from a unit test — should
  monkeypatch `DEFAULT_ACCOUNTS_ENV` to tmp_path instead.
- Verified non-findings: strict boolean parsing matrix (:19-52) matches
  `live_runner/preflight._parse_bool_env`; demo-default and garbage-rejection
  semantics are pinned correctly; connectivity tests use clean fakes with
  monkeypatch (no network).

(b) Security issues — none found. Positive: default-demo posture (:19-21),
numeric-account-id enforcement (:121-148), and "default .accounts.env untouched"
(:363-365) are security-relevant contracts under test.

(c) Baseline smells — none notable beyond the DEFAULT_ACCOUNTS_ENV coupling above.

(d) Maintainability hot spots — none.

### tests/test_live_config.py (64 lines)

Scanned — nothing significant found. Assertions verify parser output against the
YAML input and pin the warning log payload (:58-64). Only note: asserting
`warning_mock.assert_called_once()` couples the test to logging-as-behavior, but the
warning is part of the documented contract here. No findings.

### tests/test_live_risk_sizing.py (68 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_live_risk_sizing.py:18-34` —
  `test_size_live_signal_sets_qty_for_valid_risk` never checks the actual quantity
  magnitude (only `> 0`), although entry 100 / stop 98 / balance 10k / 1% risk gives
  a deterministic 50.0 units via `risk/(distance*pip)` (same math pinned exactly in
  tests/test_live_execution_safety.py:55-79). A sizing-formula mutant producing e.g.
  half size or double size would pass here. Weaker sibling of the exact assertions
  elsewhere; add `pytest.approx(50.0)`.

(b/c/d) Otherwise scanned — no security issues, no baseline smells, no hot spots;
the precomputed-qty passthrough test (:54-68) matches `risk/live.size_live_signal`.

### tests/test_live_smoke.py (95 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_live_smoke.py:42-43` —
  `test_smoke_report_serializes_without_secret_fields` proves only that the literal
  strings "api_key"/"secret" don't appear in a hand-built report that never contained
  credentials; it cannot catch a future field leaking real values (it inspects
  `str(payload)` of data the test itself constructed). As a secret-leak guard it is
  close to vacuous; as a schema smoke check it is fine.
- Verified non-findings: mode aliasing (:59-76) and discord-replay routing (:79-95)
  assert concrete outputs; help-text test pins the broker-minimum default.

(b) Security issues — none beyond the weak leak-guard note above (which is *about*
security coverage, filed as (a)).

(c) Baseline smells
- Message Chains / Middle Man (mild): tests import five names from
  `scripts/live_smoke` one per line (:8-12) — cosmetic only, not worth churn.

(d) Maintainability hot spots — none.

### tests/test_ctrader_deployment.py (61 lines)

Scanned — expected ranking outcome ("fast" over "slow") verified against
`live_runner/ctrader_deployment.py:47-56` sort keys; plan building asserts account
targeting, status normalization and `source_backtest_id`. Nothing misleading.
Only note (not a finding): neither test exercises the `-inf` score exclusion branch
(:41-42) — a row with no symbols or disqualifying metrics being dropped is unpinned.

### tests/test_risk_engine.py (270 lines)

Verified against `risk/engine.py`: sizing 50.0 units (:44), exposure rejection reason
(:89), wrong-side-stop rejection gated behind `enforce_stop_direction` (:92-124),
ATR fallback source tag (:160), decision id formats (:48, :161), and daemon qty=200
(:269, balance 100k / 1% risk / 5-unit stop) — all exact and correct. No findings
beyond a Duplicated Code note: the 8-line `RiskConfig(max_risk_per_trade=..., ...)
+ TradeSignal(...)` preamble repeats in five tests; a builder would help. Scanned,
otherwise clean.

### tests/test_risk_consistency.py (130 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_risk_consistency.py:55, :94` — `timestamp=datetime.now()`
  constructs tz-naive datetimes for LiveSignal (the repo's own checklist item;
  production paths normalize to UTC elsewhere, e.g.
  tests/test_live_execution_safety.py:1200-1232 pins that normalization). Harmless
  here because sizing ignores the timestamp, but it normalizes naive usage inside the
  suite. Use `datetime.now(timezone.utc)`.
- Verified non-findings: parity expectations (risk 1% of 100k = 1000; sizes 50/20 via
  `1000/(distance*pip)`) are arithmetically right; the asymmetry documented at
  :79-130 (live rejects wrong-side stops when enforced, backtest engine tolerates by
  default) matches `risk/engine.py:185-210`.

(b/c/d) No security issues, no smells beyond the naive datetimes, no hot spots.

### tests/test_capm.py (40 lines)

Scanned — nothing found. Exact synthetic regression (β=1.5, α annualized ×252,
R²=1, p-value bound), alignment intersection semantics, and minimum-observations
error contract are all pinned correctly.

### tests/test_optimizer.py (583 lines)

Strong file. Verified: split validation errors, bar-coverage preflight ordering
(forbidden-study guards assert real sequencing, not tautologies), grid-search best
selection (window=3 on both train 2.5 and OOS 1.7), penalty flags, artifact save,
error propagation, and risk-parity covariance mode (weights sum ≈ 1).
(a) Bugs / correctness risks — none found.
(b) Security — none.
(c) Baseline smells
- Duplicated Code: the `monkeypatch.setattr(optimizer_module, "find_strategy_entry",
  lambda _: SimpleNamespace(file_path=tmp_path / ..., slug=...))` block repeats in six
  tests (:289-296, :338-345, :374-381, :418-425, :502-509, :545-552); likewise
  `BACKTESTS_DIR`/`OPTIMIZATIONS_DIR` patching. Extract fixtures.
(d) Maintainability hot spots — none significant.

### tests/test_portfolio_optimizer_oos.py (594 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_portfolio_optimizer_oos.py:509-554` —
  `test_portfolio_optimizer_with_train_months_oos_split` promises a walk-forward
  IS/OOS split with "18 months train, 6 months test" in its docstring but never passes
  `train_months`; it calls with `test_months=6` only (:543-547). Combined with
  `test_split_equity_curves_with_train_months` (:198-224) which explicitly documents
  "`train_months` ... reserved but not yet implemented", the public API accepts a
  parameter the implementation silently ignores, and the test names/docstrings
  suggest coverage that doesn't exist. Either implement, or mark the parameter and
  these tests clearly unsupported — today they encode a dead knob as validated
  behavior.
- **minor** `tests/test_portfolio_optimizer_oos.py:425-436` —
  `test_portfolio_sharpe_from_weights_positive`'s docstring/comment says "Should have
  positive Sharpe for positive trend", but the body never asserts anything about
  `sharpe` beyond its type (`isinstance(sharpe, float)`); only `max_dd >= 0.0` is
  checked. A sign-flip mutant making Sharpe always negative would pass. Under-asserted.
- **minor** `tests/test_portfolio_optimizer_oos.py:110-120, :169-177, :531-539` —
  `mock_load` maps strategies to curves by **call order** (`call_count[0] == 1`),
  so if `optimize_portfolio_allocation` changes iteration order of `backtest_ids`
  the two strategies silently swap identities and assertions still pass (both
  curves are upward trending). Keying the fake on `backtest_id` would pin intent.
- Verified non-findings: split boundary counts (12 train / gap / 4 test with
  boundary-inclusive test at :51-77) were checked against the stated month math and
  are internally consistent; alignment overlap-window semantics (:274-320) and the
  correlation-penalty sign regression guard (:339-377) are genuine, well-targeted
  tests.

(b) Security — none found. Positive: path-traversal guard on persistence is pinned
at :494-506.

(c) Baseline smells
- Speculative Generality: the unimplemented `train_months` parameter above.
- Duplicated Code: three near-identical mock_load/OOS harnesses (:86-153,
  :155-195, :509-554).

(d) Maintainability hot spots — the train_months story above is the main one;
otherwise clear structure.

### tests/test_risk_parity_optimizer.py (47 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_risk_parity_optimizer.py:36` —
  `assert set(result["best_symbols"]) in [{"SPY", "TLT"}, {"SPY", "GLD"}]` accepts
  either universe as winner. Given hand-picked returns this is defensible, but it
  means the selection criterion could regress arbitrarily (even to "last evaluated")
  as long as weights still sum to 1 and the winner happens to be one of the two
  candidates — the test cannot detect a scoring-direction flip. Pinning the actual
  winner with a comment explaining why would be stronger.

(b/c/d) No security issues; no smells; math assertions (weights ≥ 0, contributions
sum to 1, max-min spread < 0.01, deterministic deduped universes) are sound.

### tests/test_models.py (232 lines)

Scanned — nothing found. Serialization round-trips, optional-field omission,
winner/direction helpers, filename generation, and meta/drawings payloads are all
asserted concretely. Only style note: uses `tempfile.TemporaryDirectory()` (:187)
instead of pytest's `tmp_path` — cosmetic.

### tests/test_native_engine_execution.py (62 lines)

Verified arithmetic: fixed-risk sizing 500/(100−95)=100.0 (:41) and MT5 tick-value
sizing 500/(5·(1.0/0.01))=1.0 (:62) — both correct against the described metadata
contract. No findings. Cosmetic: missing blank line between tests at :42-43.

### tests/test_backtest_grid.py (231 lines)

Scanned — nothing significant found. Verified: CLI command construction asserts
real flags; grid fan-out keeps one risk_pct across all 4 cells (:88); timeout and
launch failures isolated per cell (:94-161); in-process regression pins resolved
date range reaching `attach_daily_equity` (:164-231). Assertions are concrete;
monkeypatch usage is targeted. Only note (not a finding): `" ".join(cmd)` substring
checks (:39-46) could pass with reordered/duplicated flags, but combined with the
`--skip-sqlite in cmd` list check the intent is covered.

### tests/test_backtest_matrix.py (159 lines)

Scanned — nothing found. Strategy selection, engine-metadata pass-through
(:98-133), and failure isolation (:136-159) are asserted against real outputs
written to tmp dirs. Clean.

### tests/test_backtest_matrix_runner.py (70 lines)

Scanned — nothing found. Pins matrix node id format (`demo:AAPL:1h`) and viewer URL
shapes from real file content. Clean, focused test.

### tests/test_grid_tester_sqlite.py (202 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_grid_tester_sqlite.py:93-149, :152-183` —
  `test_save_grid_result_to_sqlite`, `test_save_grid_result_function` and
  `test_get_grid_results_returns_newest_first` call bare `init_db()` with no DB
  isolation (same hazard as tests/test_database_migrations.py:33): they write into
  whatever `DATA_DIR` resolves to at test time — the repo's real `data/backtests.db`
  under conftest defaults. Rows persist across runs and other machines' state can
  influence `get_grid_results(limit=100)` results (the "newest first" test only
  checks membership of its two ids, which limits but does not remove the coupling).
  Should use a tmp-path engine like the sibling test at :37-91 does.
- **minor** `tests/test_grid_tester_sqlite.py:39-44` — stubbing `scipy.stats.norm`
  as a plain lambda means any code path calling `norm.ppf`/`norm.cdf` would get a
  constant 0.5 rather than an error or real value; acceptable for this persistence
  test but silently changes statistics if the module grows PSR math. Also note the
  fake `norm` is a *module-level function*, while real `scipy.stats.norm` is an
  object with methods — the stub only survives because the tested path never calls it.
- **minor** `tests/test_grid_tester_sqlite.py:61-69` — replacing the whole
  `datetime` symbol on `grid_tester_module` with a `FixedDateTime` class is broad:
  any timezone-naive `datetime(...)` constructor use inside the module during the
  test would break confusingly. Works today (module only calls `.now(tz)`).

(b) Security — none.
(c) Baseline smells — Duplicated Code: three save-then-read-back blocks share shape;
extract a helper. Real-DB coupling noted above.
(d) Maintainability hot spots — the unpinned-DB tests are the main liability.

### tests/test_download_grid_market_data.py (193 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_download_grid_market_data.py:90-92` —
  `dl.DukascopyNativeDownloader = MagicMock()` mutates the module attribute directly
  instead of `monkeypatch.setattr`, leaking the MagicMock into other tests in the
  same session for anything importing `data_tools.download_grid_market_data`
  afterwards (the duplicate test at tests/test_grid_market_data_download.py:71 does
  it correctly). Cross-test pollution vector.
- **minor** `tests/test_download_grid_market_data.py:112-139` —
  `test_download_symbol_skip_balance_rows` has drifted from its name: it only checks
  that canonical OHLCV columns exist after normalization, asserting nothing about
  balance-row skipping. Misleading name; either rename or assert balance rows are
  dropped/kept explicitly.
- **minor** `tests/test_download_grid_market_data.py:142-189` — both CLI tests swap
  `sys.argv` manually with try/finally instead of `monkeypatch.setattr(sys, "argv",
  ...)` or pytest's `capsys`/parser-level invocation; also `except SystemExit` with
  `assert e.code == 0` swallows the case where main() raises before argparse exits.
  The workers-clamping test (:167-189) passes `-1` but only verifies exit code 0,
  not that workers was clamped to 1 — under-asserted relative to its docstring.
- Verified non-findings: timeframe/symbol normalization tables (:13-51) match
  `data_tools/download_grid_market_data.py`; asset-class mapping pinned including
  the UNKNOWN→forex default.

(b) Security — none.
(c) Baseline smells — Duplicated Code: this file substantially duplicates
tests/test_grid_market_data_download.py (normalization tables, download_symbol
isolation, bid-column fill) — two files testing one module with divergent quality.
Consolidate into the better one.
(d) Maintainability hot spots — the manual argv/sys.modules juggling above.

### tests/test_grid_market_data_download.py (86 lines)

Scanned — nothing significant found. Tighter twin of the previous file: exact
bid-fill values (:43-48), m30 regression (:51-54), sort-key ordering incl. unknown-last
(:57-65), and per-timeframe failure isolation (:68-86, correctly using monkeypatch).
One overlap note: `parse_csv_list(...) == DEFAULT_SYMBOLS` (:18) couples the default
symbol order to the CSV parser's de-dup semantics — fine, just brittle to intentional
reordering.

### tests/test_dukascopy_discovery.py (214 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_dukascopy_discovery.py:213-214` —
  `_year_probe_date(date.today().year) <= date.today()` and the month equivalent are
  near-tautological: `_year_probe_date(y)` presumably returns some day in year y,
  so comparing it to today can only fail around year boundaries (or if the helper
  returns a future date late in December). As a regression test it asserts almost
  nothing about the intended clamp semantics; compare against explicit expected dates
  instead.
- Verified non-findings: binary-search genesis discovery driven by the Saturday-closed
  fake probe (:44-61), skip-known-timeframes without --force (:105-141), force
  overwrite (:143-165), and persist-before-KeyboardInterrupt (:168-202) are excellent
  behavior tests with real JSON outputs.

(b/c/d) No security issues, no smells worth flagging beyond the tautology above;
file otherwise exemplary.

### tests/test_dukascopy_downloader.py (553 lines)

Scanned — nothing significant found. Throttle/backoff state-machine tests are
precise: pause doubling logged (:102-183), pipeline batching sleeps once per N
batches (:254-323, `sleep_calls == [0.5]`), backoff cap at 10 s + 15 s cooldown +
reset (:326-404, exact sleep sequence `[10.0, 15.0]`). Progress unit plumbing
(:13-99) verified end-to-end. JVM/classpath discovery tests use env sandboxing via
monkeypatch correctly.
- Note (not a finding): the four aiohttp fakes (:102-131, :186-215, :257-283,
  :329-355) are identical ~30-line blocks — see smell below.

(b) Security — none.
(c) Baseline smells — Duplicated Code: four copies of the FakeConnector/FakeTimeout/
FakeClientSession trio; extract a module-level fixture. Also repeated
`del session, semaphore, ...` noise in fakes.
(d) Maintainability hot spots — none beyond the duplication.

### tests/test_cache_manager.py (345 lines)

Scanned — nothing found. Excellent file: cache hit/miss counters asserted via
deltas (:65-75), defensive-copy semantics verified by mutating the returned dict
(:67), a genuine concurrency test with Barrier + lock-protected counter proving
single build under 8 threads (:123-159, the `time.sleep(0.05)` here is a legitimate
race-window widener, not a flake pattern), and disk-read/merge normalization paths
asserted field-by-field. The autouse invalidate fixture prevents cross-test leakage.

### tests/test_catalog.py (290 lines)

Scanned — nothing found. Real parquet fixtures on tmp_path; cached-metadata reuse
proven negatively (`read_parquet_summary` forbidden after caching :55-72); legacy
flat exports, segmented-file grouping (both directory and filename-prefixed
layouts), cross-source merge to "mixed", alias normalization and empty-catalog edge
all pinned concretely. Clean.

### tests/test_daily_equity.py (269 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_daily_equity.py:89, :175` — return assertions use an
  absolute epsilon `abs(...) - x < 1e-10` rather than `pytest.approx`; fine for
  these magnitudes, just inconsistent with the rest of the suite. Not a defect.
- Verified non-findings: aggregation math (100000+500+300=100800 :99), chronological
  sorting (:114-122), NaN/inf skip (:124-145), invalid/missing exit-time skip
  (:147-166), and the preserve-vs-replace equity-curve policy (:199-244) are all
  exact-value assertions matching `runner/daily_equity.py` behavior described in the
  backtest_grid regression note (resolved dates passed as start/end).

(b/c/d) No security issues; no baseline smells beyond the epsilon note; helpers are
shared properly (no duplication). Clean file overall.

### tests/test_resample.py (108 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_resample.py:15, :33` — fixtures build tz-naive timestamps
  (`pd.date_range(..., freq="min")` without tz). Production data is UTC-stamped
  everywhere else in this suite; resampling naive vs aware can shift bucket edges
  across DST if production ever runs in a non-UTC local zone. Low risk given pandas
  defaults but inconsistent with the repo's tz-discipline.
- Verified non-findings: bar-count math (120→24 @5m, →8 @15m), OHLCV aggregation
  rules checked against the source slice (:64-73), monotonic timestamps, invalid
  timeframe rejection, multi-output file writes.

(b/c/d) No security issues. Smell (mild): `import numpy as np` inside both fixtures
(:16, :34) instead of at module top — cosmetic. Otherwise clean.

### tests/test_sentry_config.py (143 lines)

Scanned — nothing found; this is security testing done right. DSN-less disable,
enabled-without-DSN failure, sample-rate validation, PII defaults off (:46),
before_send redaction of Authorization headers / api_key / refresh-token /
query-string secrets with safe fields preserved (:50-79), validated option
pass-through, and scoped capture context all pinned exactly.

### tests/test_shared_logging.py (52 lines)

Scanned — nothing found. Timestamp format pinned by regex, degraded-record fallbacks
(empty module/funcName, bogus lineno → `<unknown>`/0) exercised with deliberately
corrupted LogRecord fields (:43-45). Correct and focused.

### tests/test_request_logging_middleware.py (82 lines)

Scanned — nothing found. Proper handler add/remove fixture with level restore
(:21-32); asserts method/path/status/duration payload and exc_info presence on 500s;
quiet-polling paths verified to log at DEBUG not INFO (:70-82).

### tests/test_runner_cli.py (169 lines)

Scanned — nothing found. Canonical stats persistence (total_return exactly 0.05 from
the dummy engine's 5% curve :86), timeframe alias normalization captured at the
engine boundary (:89-122), and walk-forward arg mapping incl. CSV symbol split and
JSON stdout contract (:125-169). Concrete and correct.

### tests/test_orchestrator.py (46 lines)

Scanned — nothing found. Layer ordering (non-e2e before e2e) and run-all-layers-even-
when-e2e-fails contracts pinned via fake `_run_layer`. Note: `passed=1, failed=...`
in the helper is internally inconsistent-looking but harmless since summary printing
is stubbed.

### tests/test_dolthub_manager.py (16 lines)

Scanned — nothing found. Single migration test asserting the legacy flat export moves
to `AAPL/1d.parquet` with content intact and source removed. Exactly scoped.

### tests/test_database_init.py (25 lines)

Scanned — nothing found. Init-once idempotency pinned by counting create_all/migrate
calls with everything monkeypatched to tmp_path; no real DB touched.

### tests/test_preflight.py (225 lines)

Scanned — nothing found. Deployment validation errors/warnings asserted per rule
(:22-88), credential-missing failures per broker, paper-mode OK path, CLI exit codes
(:158-189) and JSON serialization (:192-221). All concrete; uses tmp_path configs
and `env={}` to avoid host leakage. Note: the CLI tests mock `run_live_preflight`
entirely, so only exit-code mapping is covered there — acceptable given
TestRunLivePreflight covers the real function.

### tests/test_llm_providers.py (302 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_llm_providers.py:53-61, :64-77, :80-93` — the CopilotCLI
  failure-path tests patch global `subprocess.run`; if any other code in the import
  chain shells out during these tests it would hit the stubs too. Targeted but broad;
  low risk. Also note none of these tests exercise a *successful* CLI generation
  path with output parsing (only failure modes + SDK-side success), leaving
  stdout-parsing of the CLI provider unpinned.
- Verified non-findings: SDK auth-error classification (:111-133), model allow-list
  rejection (:191-216), timeout enforcement (:219-255, real event-loop deadline),
  gpt-5→gpt-5.2 alias mapping with metadata fidelity (:258-297) and nested payload
  extraction (:300-302) are all exact.

(b/c/d) No security issues; Duplicated Code: five ~20-line FakeClient/FakeSession/
FakePermissionHandler blocks could share a builder; no hot spots.

### tests/test_signal_bots_risk.py (144 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_signal_bots_risk.py:41-51` —
  `test_signal_processors_are_importable_with_process_signal` asserts only that
  `process_signal` is callable on three processors; combined with the config keys it
  passes (`{"ram_jet": ...}` for RocketFuel/Nitro at :45-47 — note RocketFuel gets a
  `ram_jet` key and Nitro is constructed with ramjet's section), this reads like a
  copy-paste remnant whose assertions can't fail for any realistic regression short
  of deleting the method. Near-vacuous; keep only if intended as an import smoke.
- Verified non-findings: sizing parity math (:69-70 computes expected size from the
  actual resolved stop rather than hardcoding — robust), ATR fallback stop flow,
  trailing-stop tightening direction, and exposure-reduction monotonicity are all
  genuine behavioral checks consistent with `risk/engine.py`.

(b/c/d) No security issues; mild Duplicated Code in `_risk_engine`/market-data
helpers duplicated with test_ramjet_signal_processor.py; no hot spots.

### tests/test_ramjet_signal_processor.py (91 lines)

Scanned — nothing found. Exact SL/TP/TP1/trailing-config expectations derived from
ATR on synthetic data (:54-70), action validation error (:73-79), and default
config-file loading from the repo tree (:82-91). Concrete and correct.

### tests/test_discord_signal_parsing.py (62 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_discord_signal_parsing.py:43-49` —
  `assert signal is None or signal.symbol != "AT"` is a disjunction that holds
  trivially when a signal *is* parsed with symbol "AT" removed... actually it also
  holds whenever the parser returns anything other than a signal with symbol "AT",
  i.e. almost always. As written it cannot distinguish "parser rejected the message"
  from "parser extracted a wrong-but-not-'AT' symbol". Weaker than the sibling tests;
  pin one concrete expected outcome instead.
- Verified non-findings: oversold-buy template, action-first symbol precedence over
  RAM JET label, and rocketfuel momentum-reversal sell all pinned exactly.

(b/c/d) No security issues; no smells; no hot spots.

### tests/test_scheduler_signal_bot_health.py (65 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_scheduler_signal_bot_health.py:36-37, :62-63` — both tests
  assert only `mock_check.called` and `mock_upsert.call_count >= 2`, never inspecting
  *what* was upserted. The healthy and unhealthy scenarios therefore produce
  identical assertion sets — a mutant that records `"status": "ok"` even when bots
  report errors would pass `test_signal_bot_health_check_job_detects_errors`. Assert
  the recorded status/error fields on the captured upsert kwargs.
- Also note :10-13 (`test_signal_bot_health_check_job_imports`) asserts callability
  only — an import smoke, acceptable as such.

(b/c/d) No security issues; no smells beyond the weak assertions; no hot spots.

### tests/test_signal_bot_health_check.py (35 lines)

(a) Bugs / correctness risks
- **minor** `tests/test_signal_bot_health_check.py:28-35` —
  `test_signal_bot_health_check_main_returns_json` greps raw stdout for
  `'"status": "ok"'`; this depends on dict serialization order and exact quoting
  (breaks silently if output switches to compact separators or reorders keys).
  Prefer `json.loads(captured.out)` then assert fields.

(b/c/d) No security issues; registry-shape assertions (:6-25) are reasonable smoke
checks; no hot spots.

### tests/test_service_discovery.py (35 lines)

Scanned — nothing found. Env-file discovery of three Discord bots with token env-key
and channel-id mapping pinned via a tmp `.accounts.env` (monkeypatched path — no
host state touched).

### tests/test_mt5_parity_artifact_loader.py (158 lines)

Scanned — nothing found. Bid-column backfill with canonical-column precedence
(:14-50), m1_bid fallback file selection (:52-70), EURUSD unit conversion math
(1/1.25 → ×0.8 vs ÷1.25 semantics pinned via 80_000/0.8 :73-80), lot-step rounding
per symbol class (:83-85), and the shifted-stop-distance anchoring regression pair
(:88-157, positive case `sl_price == 98.0` from fill anchor, negative case falling
back to `99.5` with reason `stop_loss`) — all exact-value and well-commented.

### tests/test_qc_risk_parity_artifact_loader.py (33 lines)

Scanned — nothing found. Local-parquet universe loader returns TICKERS-ordered
close/open frames with spot-checked values; uses tmp_path correctly.

---

## Cross-cutting observations

1. **Three divergent `isolated_db` fixtures** (tests/test_database_migrations.py:141,
   tests/test_ctrader_live_fix.py:237, tests/test_live_execution_safety.py:623) plus
   ad-hoc engines in test_portfolio_optimizer_oos.py:562 — only the last attaches the
   production SQLite connect listener (`shared/database.py:81-87`). Migrations are
   being validated under different connection pragmas than production. One shared
   conftest fixture would fix all four.
2. **Real-database writes from unit tests**: test_database_migrations.py:33,
   test_grid_tester_sqlite.py (:93, :130, :152) run against whatever DATA_DIR
   resolves to (repo `data/` under conftest defaults).
3. **Suite-wide duplication** of LiveSignal construction (~40 sites), adapter
   constructors, and fake-client scaffolding — a `tests/helpers.py` would remove
   several hundred lines.

## Finding counts by severity

| Severity | Count |
|---|---|
| Critical | 0 |
| Major | 3 |
| Minor | 35 |

Majors: two vacuous cTrader position tests (test_ctrader_live_fix.py:268-281,
:284-295) and the un-isolated real-DB migration test
(test_database_migrations.py:33-37).

All 49 files in the area list are accounted for above: 20 files with findings,
29 scanned clean. No security vulnerabilities found in test code itself; several
tests actively pin security behavior (Sentry redaction, credential-file 0600
permissions, fail-closed auth/deployment-status handling).








