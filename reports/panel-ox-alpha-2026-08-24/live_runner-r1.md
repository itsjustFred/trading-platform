# Code Review — `live_runner` (Reviewer r1, blind/independent)

Area: `/home/ubuntu/Desktop/trading-platform/live_runner` — executes LIVE trades via Alpaca and cTrader broker APIs.
Scope: all 18 tracked files listed in the brief. Every file is accounted for below (finding or "scanned, nothing found").
Method: full read of every file; alpaca-py behavior verified against the installed package (`.venv .../alpaca/trading/stream.py`) where a finding depended on third-party semantics. Read-only; nothing edited.

Severity scale: **critical** = can lose money or violate an operator control on live funds; **major** = correctness/security defect with realistic money or operational impact; **minor** = robustness/hygiene/smell.

---

## 1. `live_runner/daemon.py` (2,144 lines)

### (a) Bugs / correctness risks

- **CRITICAL — Paused deployments keep trading: Discord listeners capture a stale deployment snapshot.**
  `sync_deployments_loop` rebuilds the grouping dict each iteration (`deployments_by_account = _group_deployments_by_account(deployments)` — daemon.py:2006), but running listener tasks were handed the *old* dict object at creation (daemon.py:1642–1663) and re-read only that same object on retry (`deployments_by_account.get(account_id, [])` — daemon.py:1069). Inside the listener, `_active_discord_deployments_for_account` (daemon.py:684–694) filters by `status == "ACTIVE"` **against the stale list**, so flipping a single deployment to `PAUSED`/`STOPPED` in the DB does not stop its live trading as long as any other Discord deployment on the account is still active (which keeps the task alive via `_cancel_inactive_discord_tasks`, daemon.py:1616–1630 — it only cancels when the whole *account* leaves the Discord-active set). Operator pause intent on live funds is not honored. Fix: pass a fresh lookup (or re-query deployments) per dispatch, or key tasks per-deployment.

- **MAJOR — Any unmapped signal action is converted to a SELL order.**
  `_trade_signal_to_live_signal`: `action="buy" if signal.action in {"buy","long"} else "sell"` (daemon.py:677). A signal processor emitting `"close"`, `"hold"`, `"flat"`, or a typo'd action becomes a live market SELL (converted to exit-only only if shorts are disallowed — and if a short *is* allowed, an outright short is opened). Whitelist `{buy, long, sell, short}` and raise otherwise; fail-closed, not fail-short.

- **MAJOR — Malformed boolean config silently disables candle-close waiting (immediate execution).**
  `_deployment_wait_for_candle_close` (daemon.py:380–388) parses configured values and `DISCORD_WAIT_FOR_CANDLE_CLOSE` with the *lenient* `_parse_bool_env` (daemon.py:137–140): any unrecognized token (`"ture"`, `"enabled"`, `"1 " != "1"` is fine, but e.g. `"yes."`) returns **False** → the daemon submits immediately instead of waiting for the candle close, changing execution timing/price for live orders. The repo already has the strict parser for exactly this class of hazard (`_parse_strict_bool_env`, daemon.py:147–158, used for `CTRADER_DEMO`) — use it here. Same lenient-parse footgun in `_deployment_allows_short` (daemon.py:373–377), though there the silent-False outcome is at least conservative.

- **MAJOR — Per-account Discord token/timeframe taken from an arbitrary "primary" deployment.**
  `primary_deployment = next(iter(active_discord_deployments))` (daemon.py:892) decides the bot token *and* the listener timeframe for all channels of the account. Two deployments on one account intended for different bots (different `DISCORD_BOT_<NAME>_TOKEN`) will poll both channels with whichever bot sorts first; the second bot is not a member of the first bot's channels → 403 → `DiscordAuthenticationError` → the entire account gets auth-blocked (daemon.py:1080–1093), halting legitimate trading. Route token/channel pairs per deployment, not per account.

- **MINOR — Submission failures are swallowed with only a warning.**
  `except Exception` around `submit_order_with_risk` logs and returns (daemon.py:797–811); a rejected/failed live order produces no retry, no dead-letter, no metric — the trade silently never happens. Deliberate best-effort design perhaps, but for live money this deserves at least a Sentry capture (`capture_exception` is imported and used only for daemon crash).

- **MINOR — Exit-only closes only the first matching position.**
  `_resolve_exit_only_target_qty` takes the first position matching the symbol and `break`s (daemon.py:556–568). On accounts/brokers that permit multiple lots per symbol (cTrader hedging accounts), a "sell/exit" signal flattens only one lot, leaving residual exposure the operator believes is closed.

- **MINOR — Deployment channel fallback to a global env var can cross-contaminate strategies.**
  `_group_discord_deployments_by_channel` falls back to `accounts_env_values["DISCORD_CHANNEL_ID"]` for any deployment without an explicit `channel_id` (daemon.py:703–708) — several deployments then silently share one signal stream.

- **MINOR — `_coerce_live_int` silently truncates fractional config.**
  `int(float(value))` turns `max_positions: 4.9` into 4 (daemon.py:317–329). Prefer rejecting non-integral values for risk limits.

- **MINOR — Orphaned tasks on daemon crash path.**
  In `run_daemon`'s `finally` (daemon.py:2111–2136), `sync_task` and `balance_task` are never cancelled (only `service_sync_task` is); if `gather` raises, those loops keep running as orphans while the coroutine unwinds.

- **MINOR — Balance poller is serial with no per-call timeout budget.**
  `_poll_balances` awaits each adapter's `get_balance()` sequentially (daemon.py:1559–1562); one slow broker stalls snapshots (and thus exposure estimates fed to sizing) for all other accounts.

- **MINOR — Two independent `.accounts.env` caches can disagree.**
  `sync_services_loop` and `sync_deployments_loop` each keep their own `mtime → values` cache (daemon.py:1419–1426, 1983–1991); between the two detection points, Discord-bot sync and adapter rotation use different credential snapshots.

### (b) Security

- **MINOR — Broad terminal-error classification by substring.**
  `_is_terminal_ctrader_runtime_error` treats any exception whose text contains `"unauthorized"`/`"invalid token"` as terminal (daemon.py:93–109). An unrelated library message containing those substrings permanently blocks an account until credentials change. Low likelihood, but string-matching on error prose for a fund-moving control is fragile.

### (c) Baseline smells

- **Global Data / shared mutable module state**: six module-level dicts/sets (`_DISCORD_AUTH_BLOCKED_*`, `_DISCORD_SERVICE_AUTH_BLOCKED_*`, `_CTRADER_AUTH_BLOCKED_*`, `_CTRADER_ACCOUNT_CREDENTIAL_SIGNATURES` — daemon.py:64–70) mutated from two concurrent loops. Single-event-loop serialization mostly saves you, but the invariant "who owns which map" is spread across ~15 helper functions.
- **Duplicated Code (with semantic drift)**: four boolean-env parsers exist in the area — daemon lenient (137–140) + daemon strict (147–158), ctrader strict (ctrader_adapter.py:49–64), alpaca lenient (alpaca_adapter.py:33–36), account_discovery lenient (78–81), preflight strict (preflight.py:60–81). Two different semantics for the same concept is how the candle-close bug above happened. Extract one strict parser into `shared`.
- **Feature Envy / Inappropriate Intimacy**: `_adapter_is_ready` reaches into adapters' private `_connected`/`_authenticated` attributes (daemon.py:127–134) instead of asking the adapter (e.g., `is_ready()`).
- **Repeated Switches**: `if account.broker == "alpaca" … elif "ctrader"` adapter construction duplicated in `_create_adapter_for_account` (daemon.py:924–990) and again in `preflight._preflight_account` (preflight.py:250–295). A registry/factory keyed by broker would gather this.
- **Divergent Change**: `daemon.py` mixes deployment lifecycle, Discord bot lifecycle, credential rotation, balance persistence, risk sizing, and signal normalization in one 2,100-line module — it changes for many reasons.
- **Long Function / shotgun cleanup helpers**: eight near-identical task bookkeeping functions (`_cleanup_finished_listener_tasks`, `_cleanup_finished_discord_tasks`, `_cleanup_finished_discord_bot_tasks`, `_cancel_inactive_tasks`, `_cancel_inactive_discord_tasks`, … daemon.py:1570–1668) share one shape — extract a small `TaskRegistry`.

### (d) Maintainability hot spots

The adapter/task lifecycle state machine is the hottest spot in the area: correctness depends on subtle interactions between two sync loops, per-account task dicts, and module-global block sets. The stale-snapshot critical bug is a direct consequence. Second hot spot: signal normalization (`size_live_signal_for_submission`, `_apply_signal_processor`, `_trade_signal_to_live_signal`, `submit_order_with_risk`) — a pipeline worth its own module with an explicit action whitelist.

---

## 2. `live_runner/ctrader_adapter.py` (1,767 lines)

### (a) Bugs / correctness risks

- **MAJOR — Unknown symbol poisons the signal permanently after it is already persisted.**
  `submit_order` persists the signal (ctrader_adapter.py:1346) *before* resolving the symbol; `_resolve_symbol_id` returns `None` for an unknown symbol (ctrader_adapter.py:1430–1435), then `request.symbolId = int(symbol_id)` raises `TypeError` (ctrader_adapter.py:1440). Every subsequent retry of the same signal hits the persisted-timestamp branch (ctrader_adapter.py:1330–1344) → recovery finds no order → `RuntimeError("Persisted cTrader signal has no unique recoverable broker order…")` forever. The order is never placed and never retriable without manual DB surgery, while alarms scream "possible duplicate". Validate symbol/action **before** `save_live_signal` (the action check at 1351–1352 is also after the persist).

- **MAJOR — Hardcoded volume scale factor; no per-symbol volume validation.**
  `qty → volume` multiplies by 1000 (ctrader_adapter.py:766) and `volume → qty` divides by 1000 (ctrader_adapter.py:1532). The cTrader Open API volume unit is expressed in cents-of-units and its meaning depends on the symbol's lot size / `volumeInUnitsMin/Max/Step`; the adapter never fetches symbol details (only `symbolId` from the list, ctrader_adapter.py:741–744) and applies one constant factor to everything. For instruments where a "lot" is not 1000 units this mis-sizes orders by orders of magnitude — either rejected by the broker (benign) or filled at the wrong size (not benign). At minimum validate against per-symbol min/step and derive the factor from symbol metadata.

- **MAJOR — Protective stop-loss / take-profit are never sent to the broker.**
  `_build_new_order_request` (ctrader_adapter.py:1437–1448) sends a naked MARKET/IOC order; `signal.stop_loss` / `take_profit` (present on the model and used for *sizing*) are silently dropped, and no position monitor attaches them later. Live leveraged positions run unprotected. (Same pattern in the Alpaca adapter — see below.) If out-of-scope by product decision, it deserves a loud comment and an operator-visible warning at submit time, not silence.

- **MAJOR — Fill events are dequeued before persistence and lost on failure/crash.**
  `listen_executions` pops from `_execution_queue` and then persists (ctrader_adapter.py:1703–1704); a DB failure or crash between `get()` and `save_live_execution()` drops the event. `reconcile_executions` backstops this (good), but reconciliation only runs at connect time or after a *queue-full* overflow flag (ctrader_adapter.py:555–563, 1727–1751) — a single failed persist with no overflow does not schedule reconciliation. Persist-then-ack (or requeue on failure) would close the gap.

- **MINOR — `disconnect()` discards buffered execution events.**
  The queue is replaced wholesale (ctrader_adapter.py:1284), dropping any not-yet-persisted fills accumulated during the disconnect window (again, reconciliation is the only net).

- **MINOR — Fabricated timestamps on bad/absent broker timestamps.**
  `_to_datetime_from_ms` returns `datetime.now()` for `None`/non-positive/unparseable inputs (ctrader_adapter.py:67–76), so execution records can carry a plausible-but-wrong fill time — an audit-integrity hazard for a trade ledger.

- **MINOR — Process-global Twisted reactor flag breaks multi-loop usage.**
  `_ASYNCIO_REACTOR_VALIDATED` (ctrader_adapter.py:30, 179–203) installs the asyncio reactor for the *first* loop it sees and never revalidates. A second `asyncio.run()` context (e.g., `preflight --check-connectivity` with two cTrader accounts, tests) binds the reactor to a dead loop; `deferred.asFuture(self._loop)` then schedules on the wrong loop. Benign inside the single daemon loop, misleading elsewhere.

- **MINOR — `submit_order` re-runs the risk engine but discards its result.**
  `size_live_signal(...)` is called (ctrader_adapter.py:1321–1322) and then only metadata `qty` is used (1323); the engine's `position_size` is not enforced as a ceiling. Since `risk.live` honors `explicit_qty`, this is a validation gate, not a control — the real sizing trust boundary lives entirely upstream in the daemon.

- **MINOR — Silent `except Exception: pass` in `_teardown_client`.**
  ctrader_adapter.py:997–1007 swallows stop-service failures with no log; a wedged Twisted service would be invisible.

### (b) Security

- **Positive / verified**: token refresh persistence is done well — atomic temp-file + `os.replace` + `chmod(0o600)` + `fcntl` lock, secrets never logged (ctrader_adapter.py:138–176, 463–482).
- **MINOR — Refreshed tokens written to `os.environ`** (ctrader_adapter.py:486–487): mutates process-wide state other components read; combined with per-account adapters sharing one OAuth pair, a refresh by one adapter silently rewrites the environment for everything else.

### (c) Baseline smells

- **Divergent Change / god class**: transport lifecycle, Twisted/protobuf plumbing, OAuth refresh, order building, execution streaming, reconciliation, and DB persistence in one 1,767-line class. The 30+ lazily-loaded `_ProtoOA*` attribute declarations (ctrader_adapter.py:281–315) alone are a module boundary begging to be extracted (`CTraderProto` wrapper).
- **Primitive Obsession**: broker order ids as `str|int` juggled through `_coerce_broker_order_id` (1104–1109); account identity as three overlapping strings (`ctid_trader_account_id`, `account_config_id`, `raw_account_id`, 242–249).
- **Duplicated Code**: `_parse_bool_env` + token sets (49–64) duplicate daemon/preflight versions; `_NOT_AUTHORIZED_MARKER` logging duplicated with daemon's `_ACCOUNT_NOT_AUTHORIZED_LOG` path.

### (d) Maintainability hot spots

`submit_order`'s five-way duplicate/recovery branching (1318–1348 + `_recover_persisted_signal_order_id` + `_recover_duplicate_signal_order_id`) is carefully written but dense — the poisoning bug shows why the "persist last" ordering matters. Extract an idempotency gate (check → validate → persist → send) into its own collaborator.

---

## 3. `live_runner/alpaca_adapter.py` (729 lines)

*(Verified against installed alpaca-py 0.44.0: `TradingStream.subscribe_trade_updates` is synchronous and `run()` is blocking — the un-awaited call at line 710 is correct. No finding.)*

### (a) Bugs / correctness risks

- **MAJOR — Same poisoned-signal pattern as cTrader: persist before broker-side validation.**
  `save_live_signal` (alpaca_adapter.py:496) precedes `submit_order`; if the broker rejects (unknown symbol, bad qty for asset), every retry takes the `not created` path → `_recover_existing_signal_order` → `get_order_by_client_id` raises → `RuntimeError("refusing possible duplicate submission")` forever (alpaca_adapter.py:377–397). Fail-closed, but permanently stuck with misleading alarms.

- **MAJOR — Non-`buy` actions silently become SELL orders.**
  `side=order_side.BUY if signal.action == "buy" else order_side.SELL` (alpaca_adapter.py:443, 452) — identical defect to daemon.py:677; defense must exist at least once, ideally both layers.

- **MAJOR — Credential fallback can pick an arbitrary ACC block (wrong-account routing).**
  `resolve_alpaca_credentials` refuses ambiguous matches only when the account *has* an id/login hint (alpaca_adapter.py:157–173); an account row with neither silently falls through to default creds and then `blocks[0]` (alpaca_adapter.py:177–183). In a multi-block setup that routes live orders to whichever block sorts first. Fail closed instead.

- **MINOR — `connect()` is not locked and can leak websocket streams.**
  Unlike the cTrader adapter (`_connect_lock`), concurrent `submit_order`/`get_balance`/`listen_executions` can each trigger `connect()` (alpaca_adapter.py:471–473, 528–531, 697–699); a second `TradingStream` overwrites `self._trading_stream` without stopping the first (also on retry at 704–709) — leaked ws connections eventually hit Alpaca's "connection limit exceeded".

- **MINOR — Fabricated fill timestamps.** `_to_datetime` falls back to `datetime.now()` for missing/unparseable values (alpaca_adapter.py:50–58) — same audit-integrity concern as cTrader.

- **MINOR — Trade-update handler swallows all exceptions.** `_on_trade_update`'s blanket `except Exception` (alpaca_adapter.py:691–695) turns persist failures into silently vanished fills (cumulative-qty reconciliation is the net).

- **MINOR — `paper` flag resolution differs from discovery (cross-file inconsistency).** The adapter honors `ALPACA_PAPER_ACC{idx}` (alpaca_adapter.py:91–95); `account_discovery._iter_alpaca_credentials` ignores it (see §4). See that finding for the impact.

### (b) Security

- **MINOR — Credential material handled as plain tuples/logs-adjacent.** Keys are passed around as loose strings and included in `_CredentialKey`-style dicts elsewhere; no masking helper exists in the area. No actual leak found in log statements (checked), so informational.

### (c) Baseline smells

- **Data Clumps**: `(api_key, secret_key, paper)` tuple threaded through 6+ functions (61–183) — an `AlpacaCredentials` value type would bundle it.
- **Duplicated Code**: `_ALPACA_ACC_RE` + block parsing duplicated nearly verbatim with `account_discovery.py` (compare 30, 70–116 with account_discovery.py:43, 114–153) — and the duplication has already *drifted* (PAPER_ACC handling). Textbook Shotgun Surgery evidence.
- **Primitive Obsession**: order-type classes smuggled as `Any` through `_build_order_request` parameters (421–456).

### (d) Maintainability hot spots

Credential resolution (`resolve_alpaca_credentials` + helpers, 61–183) is the most safety-relevant pure function in the area and the most duplicated; make it the single source of truth and have discovery consume it.

---

## 4. `live_runner/account_discovery.py` (811 lines)

### (a) Bugs / correctness risks

- **MAJOR — Blocking TLS/REST I/O inside `async` functions stalls the event loop.**
  `discover_ctrader_accounts`/`_discover_ctrader_accounts_on_host` perform raw socket + TLS + multi-request handshakes with 12–20 s timeouts (account_discovery.py:334–411, 698–715), and `discover_alpaca_accounts` does a synchronous REST call (166–167) — all invoked *directly* from `async discover_configured_accounts` without `asyncio.to_thread` (776, 789). Run inside any live loop, this freezes the entire process (heartbeats, fill streams, schedulers) for tens of seconds per host/block. Either declare these functions sync (honest API) or wrap call sites in threads.

- **MAJOR — Discovered `is_demo` can contradict the adapter's actual paper/live mode.**
  Discovery derives Alpaca `paper` solely from `ALPACA_ENDPOINT_ACC{idx}` and defaults to True (account_discovery.py:127–135), ignoring `ALPACA_PAPER_ACC{idx}` which the trading adapter honors (alpaca_adapter.py:91–95). Config `ALPACA_PAPER_ACC2=false` with no endpoint ⇒ discovery records a demo account while live orders are routed. Safety metadata in the DB lies.

- **MINOR — Bare `TimeoutError()` with no message** (account_discovery.py:363) makes host-failure logs opaque (`repr` shows nothing useful).

- **MINOR — `load_dotenv(accounts_env)` mutates process env as a side effect** (account_discovery.py:766) — the exact anti-pattern daemon.py:1987 comments against; discovery values then blend with pre-existing env vars.

### (b) Security

- **Positive**: TLS 1.2+ minimum with default cert verification (698–699); heartbeat/response framing parsed with length-prefix bounds; no secrets logged.
- **MINOR — Hardcoded endpoint + port** `demo/live.ctraderapi.com:5035` (700, 727) duplicates constants the SDK already exposes; drift risk if the broker changes ports.

### (c) Baseline smells

- **Duplicated Code**: Alpaca ACC-block parsing (43, 114–153) vs alpaca_adapter.py (see §3); `_trader_balance` money-scaling duplicated with `ctrader_adapter.get_balance` (197–200 vs 1482–1484).
- **Long Parameter List / Data Clump**: `_CtraderAccountContext` is a good bundling, but `_build_ctrader_live_account(host, sock, access_token, account_item)` still threads the clump (646–651) — fold into the context.
- **Mysterious Name**: `_PROTO_OA_CTID_TRADER_ACCOUNT_REQ/RES` optional-message indirection (44–49, 521–534) hides a version-compat shim; a comment/named helper would help.

### (d) Maintainability hot spots

The protobuf label-extraction helpers (`_ctrader_message_name_sources` + field-name tuples, 203–279) encode broker-payload folklore in magic field-name lists; they're the first thing that breaks silently on an SDK upgrade and deserve fixture-driven tests.

---

## 5. `live_runner/market_data_stream.py` (301 lines)

### (a) Bugs / correctness risks

- **MINOR — Cross-thread `self.stream` swap race.** `_StreamWorker._run` replaces `self.stream` on the worker thread during reconnect (146) while `set_symbols` (called from the service thread via `_refresh_sync`) subscribes on whatever object it reads (161–173); a subscription can land on the discarded stream and be lost until the next reconnect.
- **MINOR — `_is_crypto_symbol` precedence is subtle/likely wrong.** `"BTCUSD"` → False (needs `-`), `"BTC/USD"` → True, `"BTC-USD"` → True via `endswith("USD") and "-" in s` (line 23). The `and`-clause binding inside `or` works, but exchange-style `BTCUSD` crypto symbols route to the *stock* stream (wrong feed/permissions). Add parentheses and a `BTCUSD`-style case or a comment proving intent.
- **MINOR — Malformed bar timestamps crash the ws loop.** `_serialize_bar_payload` raises `ValueError` from `fromisoformat` on a bad string (45); depending on alpaca-py's handler isolation this can bounce the stream into a crash-reconnect cycle on one poison bar.
- **MINOR — Unbounded per-symbol memory.** Deques are capped per symbol (500) but the symbol set itself is unbounded (188, 289) — a long-running daemon ingesting ever-new tickers grows without limit.
- **MINOR — Lock-held blocking stop.** `_refresh_sync` calls `worker.stop()` while holding the service lock (235–238); `TradingStream.stop()` blocks on `run_coroutine_threadsafe(...).result()` — the whole bars service (including `_append_bar_sync` consumers) stalls behind a slow ws shutdown.

### (b) Security
- Nothing found (credentials used only for client construction; not logged).

### (c) Baseline smells
- **Mysterious Name / misleading contract**: `unsubscribe_bars()` called with zero args to mean "unsubscribe everything" (167–171) relies on SDK default semantics.
- **Inline import**: `import time` inside the retry loop (151).
- **Shotgun Surgery (lite)**: symbol normalization + float coercion reimplemented here despite siblings (`_to_float`, `_normalize_symbol` vs adapters).

### (d) Maintainability hot spots
`_StreamWorker` conflates thread ownership, reconnection policy, and subscription state; extracting the subscription-set diffing would make the races obvious and testable.

---

## 6. `live_runner/preflight.py` (413 lines)

### (a) Bugs / correctness risks

- **MINOR — Fresh `asyncio.run` per account collides with the global Twisted reactor.** `_run_broker_connectivity_check` runs each adapter in its own event loop (121); for a *second* cTrader account the module-global reactor (ctrader_adapter.py:187–192) is still bound to the first, closed loop → misleading connectivity failures. Preflight is exactly the tool that must not lie.
- **MINOR — `errors.append` gating quirk.** `_preflight_ctrader_account` adds "missing credentials" only `if not errors` (207–212) — a prior validation error suppresses the more actionable credentials message.
- **MINOR — Unbounded wall-clock.** Each cTrader account inherits 5 connect attempts × exponential backoff (~30 s) plus per-request timeouts; a many-account preflight takes minutes with no progress output.

### (b) Security
- Nothing found (fails closed: `ok = not errors` including connectivity errors, 369; warnings distinguish live mode, 152–153, 215–218 — good touch).

### (c) Baseline smells
- **Duplicated Code**: strict bool parser (60–81) — fourth copy in the area; `_resolve_ctrader_credentials` (84–94) duplicates daemon.py:240–252.
- **Repeated Switches**: broker dispatch duplicated with daemon (266–295).

### (d) Maintainability hot spots
None beyond the duplications; this is the cleanest larger file in the area.

---

## 7. `live_runner/signal_sources/discord_source.py` (665 lines)

### (a) Bugs / correctness risks

- **MAJOR — Free-text regex parsing decides live trades; fallback extractors can invent symbols/prices.**
  `_extract_symbol` descends to `_fallback_uppercase_symbol` — the first all-caps word (≥1 char, not in `_SKIP_WORDS`) of the first non-empty line (174–182) — and `_extract_price` takes the *first* `$` number anywhere in the message (46, 200–209). Prose like "PRICE ALERT: SUPPORT HOLDS AT $420 FOR NVDA" yields symbol `SUPPORT`-style nonsense or a wrong ticker/entry price, which flows into `TradeSignal.entry_price` and downstream sizing/slippage math. Mitigated by deployment symbol filters and risk sizing, but the parse layer has no confidence threshold and no reject-log for low-quality matches. Require corroboration (e.g., bold/cashtag/labeled forms only) or annotate signals with extraction stage and let deployments opt in to fallback tiers.

- **MINOR — Snowflake comparison assumes numeric message IDs.** `int(message_id) <= int(last_seen)` (463, 486) raises `ValueError` on any non-numeric id; it's caught upstream and retried forever without progress. Guard with a try/except and fall back to the seen-set only.

- **MINOR — Signal queue is unbounded.** `self._queue: asyncio.Queue()` (338) grows without limit if consumers stall/disconnect while the poll loop keeps enqueueing.

- **MINOR — Full message text retained in signal metadata** (287) — persists channel prose (usernames in content, etc.) into the trade store; retention/noise concern.

- **MINOR — `_prime_cursors` + immediate `_poll_once` in `connect()` double-fetches** the first channel (571–572) — harmless today (cursor suppression), wasteful and easy to break.

### (b) Security

- **Positive**: bearer token scoped to the Authorization header, never logged; 401/403 mapped to a terminal typed error (405–408); request timeout present (387); rate-limit `retry_after` honored (409–425, 553).
- **MINOR — Channel ID interpolated into URL unvalidated** (384) — values originate from operator config, but validating `^\d+$` would foreclose any config-injection surprise.

### (c) Baseline smells

- **Divergent Change**: message *parsing* (43–299, the risky half), REST *transport* (381–449), and *lifecycle/status* (554–665) in one class; the parser deserves its own module with table-driven tests.
- **Speculative Generality / Data Clump**: `channel_id` *and* `channel_ids` constructor params overlap (316–336); the singular exists only to seed the plural.
- **Mysterious Name**: `_SKIP_WORDS` mixes two purposes (ticker-vs-prose stopwords and signal vocabulary) in one ad-hoc set (55–97).
- **Refused Bequest (lite)**: `BaseSignalSource.listen_signals` is declared `async def -> AsyncIterator` (base_source.py:17–20) while implementations are async *generators* — the ABC contract cannot be called as written.

### (d) Maintainability hot spots
The regex constellation (43–54) plus `_SKIP_WORDS` is the area's highest-churn, highest-risk surface; every new signal phrase edits live-trade semantics. A golden-message test corpus is the cheapest insurance available.

---

## 8. `live_runner/signal_sources/execution_timing.py` (89 lines)

### (a) Bugs / correctness risks
- Scanned; nothing found beyond trivia. Epoch-aligned candle math is DST-safe; all datetimes tz-aware; `asyncio.sleep` is cancellable. One nuance worth a comment: a stale `received_at` (backlogged message) yields `delay <= 0` → immediate submission, which is the right fail-open for old signals.

### (b) Security
- Nothing found.

### (c) Baseline smells
- **Minor Duplicated Code / redundancy**: the `"s1"` special case (35–36) is already handled by the regex path; `_timeframe_seconds` reimplements timeframe parsing that `shared.constants.normalize_timeframe` presumably owns half of.

### (d) Maintainability hot spots
None.

---

## 9. `live_runner/signal_sources/base_source.py` (20 lines)

### (a) Bugs / correctness risks
- **MINOR — ABC method signature doesn't match the async-generator idiom implementations must use** (`async def listen_signals(...) -> AsyncIterator[TradeSignal]` declared as a plain coroutine-returning abstractmethod, lines 17–20). Subclasses diverge structurally; typing won't catch a broken implementation.

### (b) Security
- Nothing found.

### (c) Baseline smells
- **Speculative Generality**: an ABC with one concrete implementation whose third method is structurally awkward — consider Protocol + explicit iterator method (`def signals(self) -> AsyncIterator`).

### (d) Maintainability hot spots
None.

---

## 10. `live_runner/signal_sources/__init__.py` (5 lines)
Scanned — re-export only, `__all__` correct. Nothing found.

---

## 11. `live_runner/__init__.py` (7 lines)
Scanned — re-export only, `__all__` correct. Nothing found. (Note: importing the package pulls in both heavy SDKs transitively; acceptable for an application package.)

---

## 12. `live_runner/base_adapter.py` (25 lines)

### (a) Bugs / correctness risks
- **MINOR — Interface drift forces duck-typing downstream.** `get_positions` and `disconnect` are implemented by both adapters and feature-envied by the daemon via `getattr` probes (daemon.py:576–578, 608–609, 1012–1014) but are absent from the ABC. Add them (with a default no-op where sensible) so the daemon can stop probing privates.

### (b) Security
- Nothing found.

### (c) Baseline smells
- **Incomplete Abstraction**: the ABC covers 4 of ~6 operations the daemon actually relies on (see above).

### (d) Maintainability hot spots
None.

---

## 13. `live_runner/config_parser.py` (55 lines)

### (a) Bugs / correctness risks
- Scanned; nothing found. `yaml.safe_load` used; type-shape validated before model_validate; unknown-account references warned (consistent with preflight treating them as errors).

### (b) Security
- Nothing found (`safe_load`, no exec path).

### (c) Baseline smells
- Nothing found (small, single-purpose).

### (d) Maintainability hot spots
None.

---

## 14. `live_runner/account_routing.py` (40 lines)

### (a) Bugs / correctness risks
- **MINOR — Numeric-string normalization can still collide distinct ids.** Routing key normalizes `007` → `7` (27–32) but a cTID of `7` and login `"0007"` from *different* accounts collapse correctly (good); however two accounts with identifier `"7 "` vs `"7"` rely on earlier `.strip()`. Edge only; behavior is sound as written.
- Scanned otherwise; nothing found — the duplicate-identifier fail-closed check is exactly right for money routing.

### (b) Security
- Nothing found.

### (c) Baseline smells
- Nothing found.

### (d) Maintainability hot spots
None.

---

## 15. `live_runner/service_discovery.py` (44 lines)

### (a) Bugs / correctness risks
- Scanned; nothing functional. Note: bots whose channel key is missing are silently skipped (35–36) — reasonable, but a debug log would save setup headaches.

### (b) Security
- Nothing found (token *values* never copied into results — only key names; good).

### (c) Baseline smells
- **Duplicated Code / Shotgun Surgery**: the `DISCORD_BOT_{NAME}_TOKEN` naming convention is re-derived independently in daemon.py:735–736, daemon.py:1116–1117, and here (12, 32–33). Rename the prefix once, break three modules — centralize the derivation.

### (d) Maintainability hot spots
None.

---

## 16. `live_runner/stockkit_routing.py` (79 lines)

### (a) Bugs / correctness risks
- **MINOR — Hardcoded production account IDs in source.** `STOCKKIT_ACCOUNT_ROUTES` (8–12) pins Alpaca account numbers in code; rotating/renaming an account requires a code change and the map silently mismatches DB rows otherwise. Load from config/env with the code map as fallback.
- **MINOR — `expected_account_for_stockkit_route` raises bare `KeyError`** for unknown routes (59–60); all current callers pre-validate via `infer_stockkit_route`, so it's latent.

### (b) Security
- Nothing found (fail-closed validation, 63–79, is a positive: known bot → wrong account is refused).

### (c) Baseline smells
- **Duplicated Code**: `_ALIASES` (14–24) restates the route names it maps to; generate aliases (space/underscore variants) programmatically to stop the map rotting.

### (d) Maintainability hot spots
None.

---

## 17. `live_runner/ctrader_deployment.py` (100 lines)

### (a) Bugs / correctness risks
- **MINOR — Arbitrary account selection**: `ctrader_accounts[0]` (69) decides which live account receives auto-generated deployments; tie it to an explicit config choice.
- Scanned otherwise; NaN-guarded metrics (`value == value`, 14) and PAUSED-by-default plans (64, 80) are good money-safety posture.

### (b) Security
- Nothing found.

### (c) Baseline smells
- **Minor Primitive Obsession**: metric rows stay untyped `dict[str, Any]` throughout; a `BacktestMetrics` dataclass would concentrate the coercion.

### (d) Maintainability hot spots
None.

---

## 18. `live_runner/test_signal_trigger.py` (70 lines)

### (a) Bugs / correctness risks
- **MINOR — Bypasses `resolve_alpaca_credentials`.** Constructs `AlpacaLiveAdapter(account_id=deployment_id)` directly (31), so ACC{N} credential blocks are invisible — with a multi-block env the tool trades the *default* keys while believing it targets the configured account. Route through the shared resolver.
- **MINOR — Places a real order with defaults** (`--symbol AAPL --qty 1`, action `buy`, 36–50): appropriate for its stated slippage-testing purpose and paper-default, but a `--paper/--live` confirmation gate would prevent fat-finger live runs.
- **MINOR — `assert` used for runtime validation** (33) — stripped under `python -O`.

### (b) Security
- Nothing found (credentials loaded via dotenv into env; not printed).

### (c) Baseline smells
- **Minor**: sync price fetch inside async `_run` (34) blocks the loop briefly; no adapter disconnect on exit (process exit compensates).

### (d) Maintainability hot spots
None.

---

# Cross-cutting summary

**Top money-safety items, in priority order:**
1. Stale deployment snapshot → PAUSE not honored on live Discord trading (daemon.py:1069/1655/2006) — **critical**.
2. Unmapped action → live SELL (daemon.py:677, alpaca_adapter.py:443/452) — **major**.
3. Naked orders: SL/TP never transmitted, no position monitor (ctrader_adapter.py:1437–1448, alpaca_adapter.py:421–456) — **major**.
4. Persist-before-validate poisons signals irrecoverably (ctrader_adapter.py:1346→1440; alpaca_adapter.py:496→377–397) — **major**.
5. Hardcoded cTrader volume scale without per-symbol volume bounds (ctrader_adapter.py:766/1532) — **major**.
6. Lenient bool parsing can disable candle-close waits on typos (daemon.py:380–388) — **major**.
7. Heuristic Discord prose parsing can mint wrong symbol/price trades (discord_source.py:174–209) — **major** (mitigated downstream).
8. Wrong-account credential fallback / discovery-adapter paper-mode divergence (alpaca_adapter.py:177–183; account_discovery.py:127–135) — **major**.
9. Blocking discovery I/O in async context (account_discovery.py:776/789) — **major**.

**Repo-wide smells (Fowler baseline)**: the boolean-env parser exists 4× with 2 semantics; Alpaca credential parsing exists 2× and has already drifted; the Discord token-key convention is derived in 3 places; broker dispatch switches are duplicated in daemon and preflight; daemon and ctrader_adapter are both multi-reason god-modules. All are judgement-call refactors, not violations.

**Checklist coverage (Python)**: mutable default args — none found (audited all defs/dataclasses); bare `except:` — none (broad `except Exception` flagged where money-relevant); race conditions on shared state — flagged (daemon dual-loop state §1, market_data_stream §5, alpaca connect §3); SQL string building — none in area (DB layer external); subprocess — none; tz-naive datetimes — none found (all creation sites tz-aware; naive inputs defensively coerced); resource leaks — flagged (alpaca ws replace-without-stop §3, ctrader queue discard §2, lock-file residue trivial).

---

## Finding counts by severity

| Severity | Count |
|---|---|
| Critical | 1 |
| Major | 13 |
| Minor | 44 |

Major list (for cross-reviewer reference), as filed per file section: (1) daemon.py unmapped-action→SELL; (2) daemon.py lenient bool parse disabling candle-close wait; (3) daemon.py arbitrary per-account bot token/timeframe; (4) ctrader_adapter.py symbol-poisoning after persist; (5) ctrader_adapter.py hardcoded volume scale w/o per-symbol bounds; (6) ctrader/alpaca naked orders — SL/TP never transmitted (one root finding, cited in both files); (7) ctrader_adapter.py dequeued fills lost on persist failure; (8) alpaca_adapter.py persist-before-validate poisoning; (9) alpaca_adapter.py ambiguous ACC-block credential fallback; (10) account_discovery.py blocking I/O inside async funcs; (11) account_discovery.py is_demo/paper-mode divergence vs adapter; (12) alpaca_adapter.py non-buy→SELL bullet (same root defect as #1, filed at both defense layers); (13) discord_source.py heuristic prose parsing minting wrong symbol/price. Deduplicated root causes: 12.
