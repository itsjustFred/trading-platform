# Expert Panel Review — area `live_runner` — reviewer r3 (ox-alpha)

Scope: all 18 tracked code files under `live_runner/` (per the assigned list). Reviewed blind and
independently; only `live_runner/**` sources were read, plus targeted reads of `risk/live.py`,
`shared/database.py`, `shared/models.py`, and `risk/engine.py` strictly to verify cross-module
contracts that findings depend on (sizing semantics, DB helper behavior, model fields). No files
were edited.

Severity scale: **critical** (can lose money or trade the wrong account in normal operation),
**major** (wrong-account/missed-trade/data-integrity risk in realistic conditions, or fail-open
behavior), **minor** (latent bugs, hygiene, hardening). Baseline smells cited per Fowler ch.3,
subject to the repo-override rule.

---

## 1. `live_runner/daemon.py` (2144 lines)

### (a) Bugs / correctness risks

- **[major] Exposure fed to the risk engine mixes incompatible units** — `daemon.py:644-655`
  with `daemon.py:602-615`. `_broker_exposure_estimate` computes
  `broker_exposure = open_positions_count * risk_config.max_risk_per_trade` (a sum of *risk-at-stake*
  amounts) and then `submit_order_with_risk` passes
  `current_exposure=max(float(current_exposure), broker_exposure)` into
  `RiskEngine.calculate_position_size`. Verified in `risk/engine.py:126,151`: the engine treats
  `current_exposure` as *budget already consumed* against `max_total_exposure`
  (`remaining = max_total_exposure - exposure`). Conflating "number of open positions × risk fraction"
  with "capital deployed" means the cap binds at essentially `max_positions` and almost never at
  `max_total_exposure` (e.g. defaults: 5 positions × 1% = 5% = cap, so `max_total_exposure` is
  redundant; any other configuration decouples the two limits). Additionally `max()` takes the larger
  of two dimensionally different quantities. Direction is conservative (blocks/undersizes rather than
  oversizes) so this is not critical, but the documented intent at `daemon.py:642-643` ("max_positions
  / max_total_exposure actually bind") is only half true.
- **[major] One Discord signal executes on at most one deployment** — `daemon.py:836-850`.
  `_dispatch_discord_trade_signal` iterates `deployments_for_channel` and hits `break` immediately
  after the first deployment that passes route/symbol filters. If an operator attaches two ACTIVE
  deployments to the same channel/symbol (e.g. same strategy on two accounts routed via
  `signal_processor`, or a copy for different sizing), the second silently never trades. Selection is
  DB row order, so behavior changes when rows are reordered. Missed trades are money-relevant and the
  failure is completely silent (only the no-match `else` branch logs).
- **[minor] Busy-spin when the account listener returns immediately** — `daemon.py:1059-1105`.
  `_run_discord_listener_with_retries` loops `while True` and resets `backoff_seconds = 2` after the
  call, but only *sleeps* in the exception branch. `_run_discord_signal_listener_for_account` returns
  promptly when the re-filtered deployment set is empty (e.g. `daemon.py:871-889` idle returns), so a
  task whose account still qualifies externally but has no routable channels spins with no sleep at
  full CPU until the next sync cancels it.
- **[minor] Arbitrary "primary" deployment picks token and timeframe** — `daemon.py:892-900`.
  `next(iter(active_discord_deployments))` decides which bot token resolves and which timeframe all
  signals on the account get by default; insertion-order dependent.
- **[minor] Lenient boolean parser is fail-open for `allow_short`** — `daemon.py:137-140` used at
  `daemon.py:373-377`. `_parse_bool_env` maps any unrecognized value to… whatever the caller asked,
  here `default=False` but the token check `value.strip().lower() in {"1","true","yes","on"}`
  returns False for typos *and* for genuine truthy spellings like `"y"`/`"enabled"`. Contrast the
  strict parser right below it (`daemon.py:147-158`, raising on invalid input, matching
  `ctrader_adapter.py:53-64` and `preflight.py:64-81`). A typo'd `allow_short: Yml` silently disables
  shorting (safe) but a config meant to disable it spelled oddly cannot fail loudly — inconsistent
  fail-safe direction across the same file.
- **[minor] Sizing inputs (entry price, pip value, stops) are taken verbatim from Discord-derived
  metadata** — `daemon.py:514-539`. `theoretical_price`, `stop_loss`, `pip_value` flow unvalidated
  from `discord_source` parsing (see §14) into the risk engine. Garbage prices mostly yield tiny/zero
  size (fails safe), but there is no sanity clamp anywhere in the live path (e.g. price ≤ 0 is only
  rejected incidentally by the risk engine).
- **[minor] Blanket unblock of Discord services on any `.accounts.env` touch** — `daemon.py:1424-1438`.
  Any mtime change (adding an unrelated key) clears *all* blocked service IDs, so a still-bad token
  immediately triggers a reconnect/auth-fail cycle per sync interval. Self-healing but noisy;
  contrast the cTrader path which correctly gates on a per-account credential signature
  (`daemon.py:264-286`).
- **[minor] Shutdown does not cancel listener tasks or disconnect brokers** — `daemon.py:2111-2136`.
  The `finally` cancels discord tasks/bots and stops the scheduler, but `active_listener_tasks` are
  neither cancelled nor awaited, and `active_adapters[*].disconnect()` is never called — cTrader
  Twisted transports and Alpaca streams are abandoned to interpreter teardown.

### (b) Security

- Tokens are handled correctly here: logs carry `token_env_key`, never token material
  (`daemon.py:1118-1120, 1347-1364`). No subprocess, no SQL string building (all DB access via the
  `shared.database` layer), timestamps tz-aware throughout (`datetime.now(timezone.utc)`).
- Trust boundary: see the discord_source findings (§14) — the daemon adds no author/symbol
  authentication layer of its own; `daemon.py:749-754` treats an empty `deployment.symbols` as
  "every symbol tradable".

### (c) Baseline smells

- **Divergent Change / god module**: 2144 lines mixing credential parsing, risk sizing, Discord
  fan-out, service lifecycle, balance polling, and the main loop. Deployment orchestration
  (`sync_deployments_loop`), Discord-bot supervision (`_sync_discord_bot_services` et al.), and
  signal normalization+sizing (`size_live_signal_for_submission`, `_apply_signal_processor`) are
  three separate reasons to change this file.
- **Data Clumps**: the 5-tuple `(app_id, app_secret, access_token, refresh_token, demo)` is threaded
  positionally through `daemon.py:240-252`, `preflight.py:84-94`, and the adapter constructor;
  `(balance, equity)` pairs likewise. Begs one `CTraderCredentials` type.
- **Duplicated Code**: strict-vs-lenient bool parsing exists twice in this file alone
  (`daemon.py:137-140` vs `147-158`) and four more times across the area (see cross-cutting notes).
- Module-level mutable globals as coordination state (`daemon.py:64-70`) — safe in a single event
  loop but fragile under any future threading; a supervisor object would own these.

### (d) Maintainability hot spots

`sync_deployments_loop` (`daemon.py:1974-2056`) is the heart of the runner and reads well, but its
correctness depends on eight private helper families and three dicts of tasks; a state-machine
extract would make the adapter/task lifecycle testable without a live DB. `_apply_signal_processor`
(`daemon.py:464-502`) rebuilds a raw dict to feed processors — schema implicit and easy to drift.

---

## 2. `live_runner/ctrader_adapter.py` (1767 lines)

### (a) Bugs / correctness risks

- **[major] Refreshed OAuth tokens are written to the shared global `.accounts.env` and exported into
  the process environment** — `ctrader_adapter.py:463-487`. Every cTrader account's adapter rotates
  `CTRADER_ACCESS_TOKEN`/`CTRADER_REFRESH_TOKEN` in the *same* file (`DEFAULT_ACCOUNTS_ENV`,
  `ctrader_adapter.py:32`) that the daemon watches for *global* credential-signature changes
  (`daemon.py:1997-1999`). Consequences: (i) with multiple cTrader accounts, each refresh rewrites
  the one shared token pair — last writer wins, and the daemon's signature change then tears down and
  reconnects *all* cTrader adapters (`daemon.py:1887-1916`); (ii) two daemon/viewer processes
  holding different in-memory tokens interleave writes under separate locks (the fcntl lock at
  `ctrader_adapter.py:147-148` only serializes writers within reach of the same lock file — it does,
  to be fair, protect concurrent writers on one host); (iii) exporting to `os.environ` defeats the
  daemon's stated design of reading the file instead of mutating env (`daemon.py:1987`). Single-token
  deployments work; the multi-account shape this file otherwise supports (per-account
  `account_config_id`, ACC-style routing elsewhere) silently degrades.
- **[major] `filled_qty` is persisted in raw broker volume units (×1000) on the streaming path while
  the position API divides by 1000** — `ctrader_adapter.py:1576-1601` +
  `_resolve_filled_volume` `1660-1669` persist `deal.filledVolume` verbatim, whereas
  `get_positions` documents and applies the opposite convention ("qty == broker volume / 1000",
  `ctrader_adapter.py:1490-1532`), and the daemon's exit-only sizing consumes *that* convention
  (`daemon.py:556-587`). The reconciliation builder `ctrader_adapter.py:1221-1240` also persists raw
  volume. Result: `LiveExecution.filled_qty` rows for cTrader are overstated 1000× relative to
  Alpaca rows (shares) and to the adapter's own position quantities, and each record mixes units
  (volume-in-points next to `commission` in account currency). Anything that sums fills
  (`get_live_signal_filled_qty`, verified `shared/database.py:1465-1477`) inherits the distortion.
- **[minor] A drained execution event is lost if its DB persist fails** — `ctrader_adapter.py:1703-1704`:
  `event = await wait_for(queue.get())` then `await self._persist_execution_event(event)`; on
  exception the generic handler backs off and retries the *loop*, not the event. The
  `_reconciliation_required` flag (`ctrader_adapter.py:555-562`) is only set on queue overflow, so a
  transient DB error during normal operation leaves that fill unpersisted until the next
  reconnect-time reconcile (`ctrader_adapter.py:846-861`).
- **[minor] Invalid broker timestamps are masked with `now()`** — `ctrader_adapter.py:67-76`
  (`_to_datetime_from_ms`): a zero/garbage `executionTimestamp` silently becomes the processing time
  in the persisted fill, corrupting the audit trail rather than flagging bad data. Same pattern in
  `alpaca_adapter._to_datetime` (§3).
- **[minor] Sizing is evaluated twice with different inputs** — the daemon sizes via
  `size_live_signal_for_submission` (`daemon.py:505-553`) and then `submit_order` re-calls
  `size_live_signal(signal, account_balance=balance)` (`ctrader_adapter.py:1321-1328`) purely to
  obtain `risk_percent` for an error message, while the actual quantity comes back out of
  `metadata['qty']` in `_resolve_order_volume` (`ctrader_adapter.py:757-779`). Two sizing call sites
  invite drift; the balance fetch also costs a round trip per submission.

Correct-by-design notes worth recording (checked, not bugs): duplicate-submit protection is genuinely
fail-closed (`submit_order` `1305-1348` → persisted-broker-id recovery `_recover_broker_order_id`
`1009-1055` refuses ambiguous/`hasMore` windows; `save_live_signal` conflict path
`_recover_duplicate_signal_order_id` `1405-1428`); no-default-size policy in `_resolve_order_volume`
(`774-779`) and the sub-minimum rejection (`766-771`) are excellent money-safety posture; token
refresh recursion is bounded (`640-674`); terminal-auth handling is coherent end to end.

### (b) Security

- Secret handling is careful: `_write_env_keys` (`138-171`) writes atomically with `mkstemp`
  (0600 by default) + `chmod(0o600)` + `os.replace` under an exclusive lock, and never logs token
  material. Good.
- Residual risk is the shared-file topology described above (multi-tenant secret in one world-writable-to-owner path, plus `os.environ` export at `486-487`).

### (c) Baseline smells

- **Speculative Generality / boilerplate field mirroring**: 30+ `self._ProtoOA*` slots declared and
  assigned one by one (`281-312`, `361-396`). A single dict/namespace would delete ~70 lines and the
  per-field `is None` guards sprinkled afterwards (`e.g. 414, 663`).
- **Duplicated Code**: `_parse_bool_env` (`53-64`), `_to_float` (`79-83`), `_TRUE/_FALSE_BOOL_TOKENS`
  re-implemented in `daemon.py`, `preflight.py`, `alpaca_adapter.py`, `account_discovery.py`.
- Long methods decomposed admirably (`connect` → `_retry_or_raise_connect_failure` etc.) — no Middle
  Man abuse; delegation depth is justified.

### (d) Maintainability hot spots

The error-classification string matching (`_is_terminal_auth_error`, `_is_already_logged_in_error`,
`_is_ctrader_pending_deferred_error` over in `daemon.py:112-118`) is brittle substring matching on
broker messages — a broker wording change silently reclassifies terminal vs retriable failures.
Centralize codes in one table with tests.

---

## 3. `live_runner/alpaca_adapter.py` (729 lines)

### (a) Bugs / correctness risks

- **[major] Hint-less multi-account configs can route every DB account through the first credential
  block** — `resolve_alpaca_credentials`, `alpaca_adapter.py:141-183`. The refusal of ambiguous
  fallback (`161-173`) fires only when `_alpaca_blocks_have_hints(blocks)` is true. With two or more
  `ALPACA_*_ACC{N}` blocks and *neither* `ALPACA_ACCOUNT_ID_ACC{N}` nor
  `ALPACA_BROKER_LOGIN_ACC{N}` set for any of them, an explicit `account_id` from the DB finds no
  match, the ambiguity guard is skipped (no hints exist), the default pair is absent — and
  `blocks[0]` (`183`) hands *its* keys to *every* account. Both deployments then trade the same
  broker account while the DB believes otherwise; per-account exposure tracking diverges from reality.
  Requires an ops mistake (hints omitted), hence major not critical — but the failure mode is
  wrong-account live trading with no error.
- **[minor] Reconciliation swallows all errors with zero logging** — `_reconcile_signal_fill`,
  `alpaca_adapter.py:262-268`: `except Exception: return False` merges "order truly unknown to this
  account" (expected) with auth/rate-limit/network faults (should surface). A systematically failing
  reconciliation looks identical to a fully reconciled book. This is the checklist's broad-except
  item; add at least a debug log distinguishing exception types.
- **[minor] Unparsable timestamps become `now()`** — `_to_datetime`, `alpaca_adapter.py:50-58`;
  same audit-trail masking as `ctrader_adapter.py:67-76`.
- **[minor] Trade-update handler re-registered on clean `run()` return** — `listen_executions`,
  `alpaca_adapter.py:697-711`: the `while True` re-invokes `subscribe_trade_updates` on the *same*
  stream instance whenever `run()` exits without raising; alpaca-py appends handlers, so duplicate
  delivery chains can accumulate across such cycles. DB-side `dedupe_key` prevents double-persist
  (verified `save_live_execution_up_to_cumulative` dedupe), so impact is wasted work/log noise, not
  corruption. (I verified locally that `TradingStream.run` is a plain blocking method that starts its
  own loop/thread — so `asyncio.to_thread(self._trading_stream.run)` at `711` is correct, and the
  initial suspicion of a blocking-the-loop bug here is *not* confirmed.)
- **[minor] Inconsistent off-loop discipline** — `connect()` calls `self._trading_client.get_account()`
  synchronously on the event loop (`alpaca_adapter.py:226`) while every later call wraps in
  `asyncio.to_thread` (`511, 531, 540`). Brief loop stalls during startup.

Positive checks: partial-fill cumulative-vs-delta handling is correct and thoughtfully commented
(`581-603`, and `save_live_execution_up_to_cumulative` clamps atomically — verified
`shared/database.py:1413-1458`); duplicate-submission recovery fails closed (`377-419`); extended-hours
path enforces a positive limit price and DAY TIF (`432-448`).

### (b) Security

- Keys are never logged (`512-525`, `672-690` log ids/quantities only). Paper-mode resolution
  defaults safely to paper (`199-203`, endpoint sniffing at `91-95` is lowercase-contains-"paper",
  which also misclassifies a hypothetical endpoint merely containing "paper" — negligible).

### (c) Baseline smells

- **Duplicated Code**: `_parse_bool_env`/`_to_float`/`_to_datetime` triplets again; `_ALPACA_ACC_RE`
  and block-parsing duplicated nearly wholesale with `account_discovery.py:43,114-153` — credential
  scheme changes require synchronized edits in two modules (**Shotgun Surgery**).
- `_build_order_request` taking four classes as parameters (`421-456`) is **Repeated Switches**
  inverted — passing enums/classes around instead of a tiny factory; readable but unusual.

### (d) Maintainability hot spots

Credential resolution (`61-183`) is the most safety-dense pure function in the area and has no
unit-test-visible seam here (it does in tests/, outside my scope) — extracting it to its own module
shared with `account_discovery` would shrink both.

---

## 4. `live_runner/account_discovery.py` (811 lines)

### (a) Bugs / correctness risks

- **[major] Module import installs the Twisted default reactor, contradicting the file's own
  invariant and poisoning later adapter use in the same process** — `account_discovery.py:15-31`.
  The comment promises "must not install or mutate a process-wide Twisted reactor at import time",
  but the top-level `from ctrader_open_api import Protobuf` (and the pb2 imports) execute
  `ctrader_open_api/__init__` → `Client` → `TcpProtocol` → `from twisted.internet import reactor`,
  which installs the *default* (non-asyncio) reactor. `CTradeLiveAdapter._ensure_asyncio_twisted_reactor`
  (`ctrader_adapter.py:179-203`) then hits `ReactorAlreadyInstalledError`, swallows it, and its
  follow-up check (`196-201`) raises `RuntimeError: Unsupported Twisted reactor already installed`.
  Today the importer (viewer/main) and the daemon are separate processes, so this is latent — but any
  future tool that imports discovery and the adapter together (tests, a unified CLI) breaks at
  connect time with a baffling message. Either defer these imports into the functions that use them,
  or fix the adapter to tolerate a pre-installed default reactor by switching it.
- **[minor] Fully blocking TLS conversation inside `async def`s** — `_discover_ctrader_accounts_on_host`
  (`691-715`) runs socket auth + up to N×(detail+auth+trader) round trips with 20 s timeouts inline;
  wrapped coroutines (`717-760`, `763-811`) therefore starve the hosting loop for tens of seconds.
  Should be `asyncio.to_thread`/`run_in_executor`.
- **[minor] Unbounded frame allocation from the wire** — `_ctrader_wait_response`,
  `account_discovery.py:365-368`: `frame_size` is read from the socket and handed straight to
  `recv_exact`; a hostile/garbled peer can request a gigantic buffer. Cap it (protocol frames are
  small).
- **[minor] `load_dotenv(accounts_env)` mutates the whole process environment** —
  `account_discovery.py:766-767`. Side effect leaks into the host process (viewer) and interacts with
  the adapter writing rotated tokens back to the same file; `_load_accounts_env_values` in
  `daemon.py:161-167` shows the non-mutating pattern already in-repo.

Checked, not bugs: NaN-guarded balance (`177-179`); demo/live host-environment cross-check
(`91-97, 665-668`) is a good wrong-environment tripwire; per-host dedupe prefers demo rank then live
(`727-760`).

### (b) Security

- TLS: `ssl.create_default_context()` with TLSv1.2 minimum and hostname verification via
  `server_hostname` (`698-701`) — correct. App secret/token never logged (debug payloads print
  protobuf *account* fields, `304-331`, `491-500`, `652-660` — these are account metadata, not
  credentials; acceptable, though `including_default_value_fields=True` dumps can be noisy).

### (c) Baseline smells

- **Duplicated Code**: `_parse_bool_env` (`78-81`), `_ALPACA_ACC_RE` (`43`) + `_iter_alpaca_credentials`
  (`114-153`) parallel `alpaca_adapter.py:30,61-108`; `PROJECT_ROOT/DEFAULT_ACCOUNTS_ENV` defined
  identically in five modules (`here:40-41`, `ctrader_adapter.py:31-32`, `daemon.py:59-60`,
  `service_discovery.py:9-10`, `test_signal_trigger.py:13-14`).
- The label-extraction heuristics (`_CTRADER_ACCOUNT_NAME_FIELDS` probing six field names across
  nested messages, `203-262`) are a **Mysterious Name**-prone blob — a typed view over the protobuf
  would document which broker exposes which field.

### (d) Maintainability hot spots

The hand-rolled framing protocol (`334-380`) is solid but is a second, independent cTrader client
beside the OpenApiPy one in the adapter — two implementations to keep in sync when the wire protocol
or auth flow changes (classic **Shotgun Surgery** setup).

---

## 5. `live_runner/market_data_stream.py` (301 lines)

### (a) Bugs / correctness risks

- **[minor] Crypto detection misses the common `BTCUSD` spelling** — `_is_crypto_symbol`,
  `market_data_stream.py:19-23`. Requires `/` or (`endswith USD` **and** `-`); `BTCUSD` (Alpaca's own
  docs often show `BTC/USD`, but user configs frequently don't) is subscribed on the *stock* stream,
  where it silently yields no bars. Operator sees an empty quote panel with no error. Also note
  Python precedence: `a or b and c` = `a or (b and c)` — correct here, but only by luck; parenthesize.
- **[minor] Worker dies permanently on connection-limit with no surfaced state** — `_run`,
  `market_data_stream.py:122-134`: sets `_running=False` and exits the thread, yet the worker remains
  registered in `self._workers` (`240-253`), so subsequent refresh cycles see it alive and never
  restart it; bars silently freeze. At minimum evict the dead worker or mark it failed.
- **[minor] Best-effort unsubscribe can strand stale subscriptions** — `set_symbols`,
  `market_data_stream.py:165-173`: `unsubscribe_bars()` (no args) is speculative — alpaca-py's
  unsubscribe expects symbol arguments — and any failure is `except Exception: pass`, after which new
  symbols are *added* on top of the old set (duplicate streams on the same symbols).

### (b) Security

Nothing found: credentials only construct clients; no logging of secrets; thread boundaries use locks
consistently (`88, 163-164, 192, 259-261, 274-278, 299-301`).

### (c) Baseline smells

- **Speculative Generality**: the arg-less `unsubscribe_bars()` probe (`167-171`).
- **Duplicated Code**: `_to_float`/`_normalize_symbol` once more (`26-34`).

### (d) Maintainability hot spots

None serious — this is the cleanest large-ish file in the area. The dual-kind (stock/crypto) worker
keying is tidy.

---

## 6. `live_runner/preflight.py` (413 lines)

### (a) Bugs / correctness risks

- **[minor] Per-account `asyncio.run()` breaks multi-account cTrader connectivity checks** —
  `_run_broker_connectivity_check`, `preflight.py:97-124`. Each call creates and closes a fresh event
  loop, but `_ASYNCIO_REACTOR_VALIDATED` is process-global (`ctrader_adapter.py:30,187-192`): the
  first account binds the asyncio reactor to loop #1, which is then closed; the second account's
  adapter skips installation and talks to a dead loop → spurious timeouts/errors. Results are
  fail-*closed* (false negatives), so this is a reliability/UX defect in a safety tool rather than a
  hazard — but a preflight that can't validate two accounts invites operators to skip preflight.
  Run all checks in one loop, or one process per account.

### (b) Security

Good: strict bool parsing with the explicit "malformed value can never implicitly select live
trading" rationale (`64-81`); live-mode configurations produce warnings (`152-153`, `215-218`);
missing refresh token flagged (`217-218`). Connectivity mode is genuinely non-ordering
(balance/positions only, `385-400`).

### (c) Baseline smells

- **Duplicated Code**: third copy each of `_parse_bool_env` (`60-81`) and
  `_resolve_ctrader_credentials` (`84-94`, cf. `daemon.py:240-252`).

### (d) Maintainability hot spots

None beyond the duplication; the dataclass result model (`24-47`) is a good pattern the rest of the
area could reuse for structured diagnostics.

---

## 7. `live_runner/config_parser.py` (55 lines)

### (a) Bugs / correctness risks

- **[minor] Unknown `account_id` references are warning-only at parse time** — `config_parser.py:42-53`.
  Preflight re-raises the same condition as an error (`preflight.py:320-323`), so the gate exists —
  but anything that loads deployments through this parser without running preflight deploys against a
  nonexistent account (the daemon then just logs "references unknown account" each cycle,
  `daemon.py:1810-1814`). Failing fast here would cost nothing.

### (b) Security

`yaml.safe_load` only (`config_parser.py:25`) — correct choice for untrusted-ish config.

### (c) Baseline smells

None. Tight, single-purpose module.

### (d) Maintainability hot spots

None.

---

## 8. `live_runner/account_routing.py` (40 lines)

Scanned — **nothing found**. Duplicate-id and duplicate-routing-key validation is fail-closed with
precise error messages (`account_routing.py:13-40`); numeric canonicalization (`28-32`) correctly
collapses `"007"`/`"7"` style duplicates. Model: a textbook small validator.

## 9. `live_runner/stockkit_routing.py` (79 lines)

### (a) Bugs / correctness risks

Scanned with nothing found beyond the smell below; `validate_stockkit_deployment_route` (`63-79`)
fails closed exactly as documented, and `infer_stockkit_route` candidate ordering (`43-56`) is
deterministic.

### (b) Security

Nothing found.

### (c) Baseline smells

- **Primitive Obsession / environment constants in code**: `STOCKKIT_ACCOUNT_ROUTES` hardcodes three
  production Alpaca account IDs (`stockkit_routing.py:8-12`). Changing brokers/accounts is a code
  deploy; a config-backed map with these defaults would keep the fail-closed behavior while
  un-hardening ops.

### (d) Maintainability hot spots

`expected_account_for_stockkit_route` raises bare `KeyError` (`59-60`) on an unmapped route — every
caller must remember to infer first; a `.get()` with an explicit error would be safer.

## 10. `live_runner/service_discovery.py` (44 lines)

Scanned — **nothing found** beyond a documentation nit: discovery depends on the undocumented sibling
naming convention `DISCORD_BOT_{NAME}_CHANNEL_ID` (`service_discovery.py:32-35`); env correctly
overrides file values (`22-24`); malformed keys degrade to skips.

## 11. `live_runner/base_adapter.py` (25 lines)

Scanned — **nothing found**. Note for the panel: `disconnect` is not part of the interface, which is
why `daemon.py:1012-1018` and `preflight` must defensively `getattr` it — adding it to the ABC (with
a default no-op) would remove that defensive scattering (**Repeated Switches**, mild).

## 12. `live_runner/signal_sources/base_source.py` (20 lines)

Scanned — **nothing found**. Same interface note as §11: `pause()`, `reconnect()`, `is_paused`,
`is_connected` are relied upon by `daemon.py` duck-typed but absent from the ABC.

## 13. `live_runner/signal_sources/__init__.py` (5 lines) and `live_runner/__init__.py` (7 lines)

Both scanned — **nothing found**. (`live_runner/__init__.py` eagerly imports the cTrader adapter, but
since the adapter defers Twisted/OpenApiPy imports to `connect()` (`ctrader_adapter.py:318-359`),
importing the package stays cheap and reactor-free — verified consistent.)

## 14. `live_runner/signal_sources/discord_source.py` (665 lines)

### (a) Bugs / correctness risks

- **[major] More than 20 messages between polls are permanently skipped** — `_poll_once`,
  `discord_source.py:451-488`. After processing, `self._seen_ids[channel_id]` is replaced with only
  the IDs on the fetched page (`488`) and `_last_seen_message_ids` jumps to the newest (`485-487`).
  With `POLL_LIMIT = 20` (`24-25`) and `POLL_INTERVAL = 8` (`24`), a burst of >20 posts (exactly what
  a fast signal channel produces during volatility) silently orphans everything beyond the newest 20:
  older messages are neither emitted nor retained as unseen. For a live-money ingestion path this is
  a missed-trade bug with no log line. Fix: keep the cursor monotonic (fetch `before=` the watermark
  until drained) instead of snapshotting page membership.
- **[major] No authentication of signal origin: any channel member's message becomes a live order** —
  `_parse_trade_signal_from_message` (`254-299`) + `_poll_once` (`459-484`). Author ID is recorded
  (`286`) but never checked against an allowlist; action/symbol/price come from regex heuristics over
  free text (`43-54, 152-209`), and downstream `_deployment_covers_discord_symbol`
  (`daemon.py:749-754`) treats an empty `symbols` list as "all symbols". A prank, a compromised
  poster, or a look-alike announcement containing e.g. "buy signal for OPEN Price: $42" produces a
  real market order (subject only to risk-engine sizing, which happily sizes a nonsense price with a
  plausible stop). Rate limiting by Discord is the only brake. At minimum: allowlist author IDs /
  bot IDs and require a marker phrase per deployment.
- **[minor] Symbol heuristics misfire in both directions** — `_SKIP_WORDS` (`55-97`) bans legitimate
  tickers that are English words (`ALL`, `LOW`, `OPEN`, `KEY`-like entries), while
  `_fallback_uppercase_symbol` (`174-182`) promotes any capitalized shout to a ticker. Combined with
  the price extractor accepting the first `$N` number in the message (`200-209`), a formatted-but-
  atypical post parses to a *different instrument or price* than a human reader would assume.
- **[minor] Watermark comparison assumes numeric snowflake ordering across pages** —
  `discord_source.py:463, 486`: correct for Discord snowflakes, but a non-numeric `id` from a mocked/
  altered payload would raise `ValueError` mid-poll; cheap to guard.

Checked, not bugs: dedupe + priming logic (`490-517`) correctly suppresses history replay on
restart; 401/403 → terminal classification integrates cleanly with daemon blocking; rate-limit
handling honors `retry_after` (`409-425, 521-553`); tz handling in `_parse_timestamp`
(`244-251`) is aware.

### (b) Security

- Token sent only via Authorization header; never logged (`381-403` log channel/service ids only).
- The author-trust gap above is the principal security finding; secondary: message content is copied
  into `metadata["discord_content"]` (`287`) and flows into DB records/logs downstream — benign today,
  worth remembering before anyone renders it.

### (c) Baseline smells

- The regex constellation (`43-54`) plus skip-list plus six-stage symbol cascade (`185-197`) is a
  **Mysterious Name**-heavy heuristic blob — extract into a dedicated parser module with golden
  tests; right now the only spec is the code itself.
- **Duplicated Code**: `_parse_timestamp` here ≈ `_to_datetime` in both adapters ≈
  `_parse_signal_timestamp` in `execution_timing.py`.

### (d) Maintainability hot spots

This file decides *what to buy* from *free text*; it is the least-tested-looking highest-stakes
parser in the area. Priority for characterization tests.

## 15. `live_runner/signal_sources/execution_timing.py` (89 lines)

### (a) Bugs / correctness risks

- **[minor] Candle-close alignment is pure UTC epoch math and ignores market sessions** —
  `_next_candle_close`, `execution_timing.py:57-60`. For `1d` equities timeframes, "next candle
  close" is UTC midnight — markets are closed then, so the delayed market order queues as DAY and
  fills at the *next* open with overnight gap risk, defeating the feature's intent for daily
  strategies. Fine for crypto/24h instruments.
- **[minor] Unknown/typo'd timeframes silently disable the wait** — `execution_timing.py:65-67`:
  a malformed timeframe (`"45min"`, `"H1"` if normalization misses) returns `None` and the trade
  executes *immediately* instead of at candle close — a silent behavior switch on a config typo.
- Checked: tz-awareness everywhere (`16-28, 70-72`); overdue timestamps degrade to immediate
  execution (`73-75`).

### (b/c/d) Security/smells

Clean. Small **Duplicated Code** note: timeframe parsing partially duplicates
`shared.constants.normalize_timeframe` semantics. No hot spots.

## 16. `live_runner/ctrader_deployment.py` (100 lines)

### (a) Bugs / correctness risks

- **[minor] Auto-generated deployments always land on the first cTrader account** —
  `build_ctrader_deployment_plan`, `ctrader_deployment.py:66-69`: `ctrader_accounts[0]` receives
  *every* plan entry regardless of strategy fit, balance, or load. With multiple funded cTrader
  accounts, all top-5 backtest deployments concentrate on one arbitrary account (list-order
  dependent). Mitigated: entries default to `PAUSED` (`64`, and the fail-safe `80` forces unknown
  statuses back to `PAUSED`) so nothing trades without an operator activating it — hence minor, not
  major.
- **[minor] `_metric` guards NaN but not ±inf** — `ctrader_deployment.py:9-14`: the `value == value`
  test filters NaN back to the default, but an `inf`/`-inf` sharpe/cagr in a corrupt backtest row
  flows straight into the score; `top_backtests_for_ctrader` then ranks that row permanently first
  (and `round(score, 6)` keeps the inf). One bad row poisons the whole selection. Use
  `math.isfinite` as `account_discovery._safe_float` already does.

Checked, not bugs: `row.get("symbols", [])` upstream plus the guaranteed `"symbols"` key from
`top_backtests_for_ctrader` make the direct `row["symbols"]` access safe (`78`); zero-trade rows are
excluded (`24-25, 41-42`).

### (b) Security

Nothing found — pure computation over in-memory dicts, no I/O.

### (c) Baseline smells

None significant; module is small and single-purpose.

### (d) Maintainability hot spots

None.

## 17. `live_runner/test_signal_trigger.py` (70 lines)

### (a) Bugs / correctness risks

- **[minor] Live-order trigger with no dry-run/confirmation gate** — `test_signal_trigger.py:30-52`.
  By design ("for slippage pipeline testing") it submits a real order; safety rests on paper-by-default
  credential resolution (`alpaca_adapter.py:199-203`) and `--deployment-id` defaulting to
  `alpaca-paper`. The `--qty` path sets `metadata["qty"]` which `size_live_signal` honors as explicit
  (`risk/live.py:47-80` — verified), i.e. it deliberately bypasses percent-risk sizing; fine for a
  test probe, but a `--paper/--live` assertion or loud banner would prevent an oops against live keys.
- `assert adapter.api_key and adapter.secret_key` (`33`) vanishes under `python -O`; use an explicit
  check.

### (b/c/d)

No secrets logged; `_fetch_latest_price` has no timeout parameter exposure (client default) —
negligible for a CLI. Otherwise clean; no smells beyond the above.

---

## Cross-cutting observations

1. **Bool-env parsing exists five times in three flavors** (strict in `ctrader_adapter.py:53`,
   `daemon.py:147`, `preflight.py:64`; lenient in `daemon.py:137`, `alpaca_adapter.py:33`,
   `account_discovery.py:78`). Extract one strict parser; make leniency explicit where wanted.
2. **`_to_float`/timestamp-coercion helpers duplicated in six files**, two of which mask bad data
   with `now()` — one shared, strict-by-default utility would eliminate a class of silent
   audit-trail corruption.
3. **Two independent cTrader protocol stacks** (adapter via OpenApiPy; discovery via raw TLS
   framing) plus **two Alpaca credential-block parsers** (adapter, discovery) — the area's main
   Shotgun Surgery risks.
4. **Money-safety posture is generally strong**: fail-closed duplicate-submit recovery in both
   adapters, no-default-size cTrader volumes, cumulative-clamped Alpaca persistence, terminal-auth
   blocking with credential-rotation recovery. The majors above are concentrated in *peripheral*
   paths (token persistence topology, unit conventions, credential-block fallback, Discord ingestion)
   rather than the core order path.

## File accounting (all 18)

Findings: `daemon.py`, `ctrader_adapter.py`, `alpaca_adapter.py`, `account_discovery.py`,
`market_data_stream.py`, `preflight.py`, `config_parser.py`, `stockkit_routing.py`,
`ctrader_deployment.py`, `discord_source.py`, `execution_timing.py`, `test_signal_trigger.py`
(12 files).
Scanned, nothing found: `account_routing.py`, `service_discovery.py`, `base_adapter.py`,
`signal_sources/base_source.py`, `signal_sources/__init__.py`, `live_runner/__init__.py` (6 files).
Total 18/18 accounted for.

## Finding counts by severity

| Severity | Count |
|---|---|
| Critical | 0 |
| Major | 8 |
| Minor | 28 |
