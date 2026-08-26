# Expert Panel Review — area `live_runner` — reviewer r2 (blind, independent)

Scope: all 18 tracked code files under `live_runner/` (incl. `live_runner/signal_sources/`).
Method: full read of every file (daemon.py 2144 ln, ctrader_adapter.py 1767 ln, account_discovery.py 811 ln, alpaca_adapter.py 729 ln, discord_source.py 665 ln, preflight.py 413 ln, market_data_stream.py 301 ln, and all remaining modules in full). No files under `reports/` were read. Read-only; nothing was edited.

Severity counts: **critical 3 · major 14 · minor 48** (65 findings). Area-critical chains are consolidated as C-01/C-02/C-03 in §3 (each also referenced from its per-file section); remaining findings are tagged inline by severity per file. Smell-only observations are tagged `[smell]` and are not counted as findings.

---

## 1. Executive summary

This area executes **real** orders through Alpaca and cTrader. The engineering discipline is visibly high in the reconciliation/duplicate-submission paths (client-order-id conventions, fail-closed refusals to resubmit, cumulative-fill deltas, deal-id dedupe). However:

1. **No protective orders are ever attached to live orders on either broker.** The daemon computes and persists `stop_loss`/`take_profit` in signal metadata, but neither adapter transmits them (no bracket/OTO for Alpaca, no `stopLoss`/`takeProfit` on the cTrader `ProtoOANewOrderReq`). Position sizing *assumes* a stop (risk_amount / stop_distance); a live position without a stop therefore has unbounded downside while sized as if bounded. (C-01)
2. **Live trade decisions are parsed from free-form Discord chat with layered regexes**, including a fallback that treats *any* standalone uppercase word as a ticker and *any* dollar figure as a price — with a comma-as-decimal-point corruption that turns `$1,234` into price `1.234` (a 1000× error). (C-02)
3. **A credential-routing fallback can send one account's orders through another account's broker keys** (`resolve_alpaca_credentials` → `blocks[0]`). (C-03)

The dominant maintainability problem is **duplication with divergence**: bool parsers ×3 styles, `PROJECT_ROOT/.accounts.env` ×5 modules, ACC-block parsers ×2 that have *already drifted* (paper-mode decided differently), cTrader credential resolvers ×3, retry/backoff listener scaffolds ×4, broker-dispatch switches ×4, and error-classification-by-string-markers ×4 independent vocabularies. The second systemic issue is **environment-precedence inversion**: some readers prefer `.accounts.env` over `os.environ`, others the reverse, so a bot/service can be *registered* with one credential set and *executed* with another.

---

## 2. Per-file findings

### `live_runner/__init__.py`

Scanned — nothing found. Exports only the three adapter classes; consistent with `__all__`.

### `live_runner/base_adapter.py`

- **F62 [minor][smell] Interface drift / duck-typing pressure:** the ABC declares `connect/submit_order/listen_executions/get_balance` but not `disconnect` or `get_positions` (base_adapter.py:8-25). Both are real protocol requirements — the daemon duck-types them via `getattr(adapter, "disconnect")` (daemon.py:1012) and `getattr(adapter, "get_positions")` (daemon.py:576, 608). Every concrete adapter implements them anyway. Add them to the ABC (and make `get_positions` abstract) so the compiler enforces the contract instead of `getattr` + `callable()` checks at runtime.

### `live_runner/daemon.py`

Bugs / correctness:

- **F02 [major] Process-wide mutable singleton registries for auth-blocking** (daemon.py:64-70: `_DISCORD_AUTH_BLOCKED_ACCOUNTS`, `_DISCORD_SERVICE_AUTH_BLOCKED_IDS`, `_CTRADER_AUTH_BLOCKED_ACCOUNTS`, `_CTRADER_ACCOUNT_CREDENTIAL_SIGNATURES` + reason maps). These are module globals mutated from multiple concurrent tasks. Safe today only because everything runs on one event loop and each mutation happens in a synchronous block between awaits; nothing documents or enforces that invariant (a single `asyncio.to_thread` write or a second loop breaks it). They are also volatile: all cTrader auth blocks vanish on daemon restart (the blocked account is immediately retried against the broker — retry storm), while Discord *service* blocks are deliberately cleared wholesale on any `.accounts.env` mtime change (daemon.py:1428-1438), even when the changed keys are unrelated. Money-safety angle: the block lists are the only thing preventing hammering a broker with unauthorized-session attempts; their lifecycle is heuristic, not designed. Bundle into a small class with explicit methods and clear invalidation rules.
- **F03 [major] A single transient reconnect failure permanently FAILs a Discord bot service until daemon restart** (daemon.py:1265-1290): `_reconnect_discord_bot_source` marks the service FAILED, and `_sync_running_discord_bot` only processes services with status RUNNING (daemon.py:1399), while `_recover_failed_services` runs **once at startup** (daemon.py:2075). One network blip ⇒ the bot stops receiving signals for the rest of the daemon's uptime with no automatic recovery. Missed signals = missed (or unhedged) trades. Either retry transient reconnects like every other loop in this file, or re-arm recovery.
- **F04 [major] Exit-only sizing closes only the *first* matching position** (daemon.py:556-568): `_resolve_exit_only_target_qty` breaks after the first position whose symbol matches and sets `explicit_qty` to that single position's qty. Alpaca aggregates positions per symbol, but cTrader returns one entry per open position (hedging accounts routinely hold several). A `sell`/exit-only signal on such an account closes one lot and silently leaves the rest open. Sum matched quantities (signed handling for the long-only case) instead of first-match-and-break.
- **F05 [major] Discord token/channel resolution precedence is inverted relative to service discovery, with a shared-token fallback** (daemon.py:728-746 for account-level listeners; daemon.py:1108-1120 `_resolve_discord_bot_runtime` for services): the daemon prefers the `.accounts.env` file value and falls back to `os.environ`; `service_discovery.discover_discord_bots_from_env` does the exact opposite (env **overwrites** file values, service_discovery.py:22-24). A bot registered because of an env-var channel/token can subsequently run with the file's channel/token — i.e. it listens on a channel nobody intended. Additionally both paths fall back to the generic `DISCORD_BOT_TOKEN` (daemon.py:745), silently sharing one bot identity across accounts. Precedence must be defined once and shared.
- **F06 [major] One bot token per account, chosen arbitrarily, and one inaccessible channel blocks the account** (daemon.py:892-900): `primary_deployment = next(iter(active_discord_deployments))` decides the token for *all* channels of the account. If the deployments span two Discord bots, the channels owned by the other bot are unreachable; worse, a 403 from an inaccessible channel raises `DiscordAuthenticationError` (discord_source.py:405-408), which `_run_discord_listener_with_retries` classifies as terminal and blocks the whole account's Discord trading (daemon.py:1080-1093) — including channels that worked fine. Scope the auth-block per channel, or resolve tokens per deployment-group.
- **F07 [minor] Daemon shutdown cancels tasks without awaiting them** (daemon.py:2116-2121): listener/discord tasks are `cancel()`led but never awaited (only `service_sync_task` is), so `asyncio.run` tears down the loop with unfinished tasks; adapter sockets/stream threads may die ungracefully mid-write of a fill record. Await the gathered cancellations (`return_exceptions=True`) before returning.
- **F08 [minor] `_build_processor_market_data` ignores its `timeframe` parameter** (daemon.py:442-461): the argument is accepted and never used; the docstring-level intent ("compute ATR/volatility/trend filters") is timeframe-sensitive. Either validate that the frame matches the deployment timeframe or drop the parameter ([smell] Speculative Generality / dead parameter).
- **F09 [minor] Unknown action strings coerce to `sell`** (daemon.py:677): `action="buy" if signal.action in {"buy","long"} else "sell"` — a processor bug producing `"exit"`/`"flat"`/`""` becomes a naked market SELL wherever `allow_short` is enabled (with shorts disabled it at least degrades to exit-only, daemon.py:670-672). Whitelist `{"buy","long","sell","short"}` and reject anything else.
- **F10 [minor] `_coerce_live_int` silently truncates floats** (daemon.py:317-329): `int(float(value))` maps `max_positions: 2.9 → 2` with no warning. For risk knobs, reject non-integral values.
- **F11 [minor] Misleading skip diagnostics** (daemon.py:836-861): the `for…else` logs "Skipping discord signal with symbol not covered by deployment" even when the loop skipped because of a StockKit route mismatch (daemon.py:837-838) — an operator debugging "why didn't my signal trade" is pointed at the wrong cause. Log the actual rejection reason at the point of rejection.
- **F12 [minor] Silent `except ValueError: continue` when registering discovered services** (daemon.py:198-213): duplicate/invalid bot registrations disappear without a log line.
- **F13 [minor] Typo'd deployment status silently disables trading** (daemon.py:1716-1720, 1944): `_normalize_deployment_status` collapses anything but the three known spellings to `UNKNOWN`, and `UNKNOWN` simply isn't active. Fail-safe direction, but the only trace is one warning (daemon.py:1751-1762); a deployment meant to trade quietly doesn't. Consider failing the sync iteration loudly on UNKNOWN.
- **F14 [minor] `pip_value` defaults to `1.0`** (daemon.py:522-523): for equity deployments "pip" is a unit fiction; if the risk engine's formula consumes `pip_value` directly, equity position sizing rests on a silent default. Require an explicit value per asset class or derive it.
- **F16 [minor] `_adapter_is_ready` reaches into adapter private attributes** (daemon.py:127-134): `getattr(adapter, "_connected")` / `"_authenticated"` couples the daemon to both adapters' internals. Promote a public `is_ready` property on `BaseLiveAdapter` (also fixes F62's drift).

Security:

- Positive: exception payloads and logs carry account ids and error text only; no token/secret is logged anywhere in the file; `.accounts.env` is read, never written, here.
- The F05 precedence inversion is also a security-adjacent issue (credentials used differ from credentials audited) and feeds C-03's blast radius.

Smells ([smell], not counted):

- Duplicated Code: the exponential backoff + terminal-auth-exit scaffold appears three times in this file alone (daemon.py:1059-1105, 1123-1167, 1694-1725 in ctrader_adapter, 697-729 in alpaca_adapter — four total across the area). Extract a shared `run_with_backoff` helper.
- Repeated Switches: broker dispatch `if broker == "alpaca" / == "ctrader"` recurs in daemon.py:924-990, preflight.py:250-295, account_discovery.py:763-806, market_data_stream.py:204-253. Introduce an adapter/credential factory keyed by broker.
- Data Clumps: `(api_key, secret_key, paper)` and `(app_id, app_secret, access_token, refresh_token, demo)` travel as positional tuples through daemon.py:240-252, 943-978 and preflight.py:214-241. Give them types.
- Divergent Change / god module: daemon.py (2144 lines) owns credential loading, risk-sizing orchestration, Discord plumbing, service lifecycle, balance polling and shutdown. Splitting signal-source orchestration from adapter lifecycle would isolate the two fastest-changing concerns.
- Primitive Obsession: deployment statuses as raw strings normalized ad hoc (`_normalize_deployment_status`) while `ServiceStatus` enum exists for services.

Maintainability hot spots: `sync_deployments_loop` (daemon.py:1974-2056) — correct but dense; the block-state registries (F02); `_resolve_account_discord_token` (F05/F06).

### `live_runner/ctrader_adapter.py`

Bugs / correctness:

- **[major] Stop-loss / take-profit computed upstream are silently dropped** — this file's half of area-critical **C-01**: `_build_new_order_request` (ctrader_adapter.py:1437-1448) sets only account/symbol/MARKET/IOC/volume/clientOrderId; `signal.metadata["stop_loss"]`/`["take_profit"]` (written by the daemon, daemon.py:664-667) are never read here. Full chain consolidated as C-01 in §3.
- **[major] Metadata volume bypasses the risk engine** (ctrader_adapter.py:206-217, consumed at 1445): `ctrader_volume`/`volume` in metadata is submitted verbatim as the broker volume, ahead of the risk-sized `qty`. Any upstream code path (or hand-crafted signal) that sets `volume` trades an arbitrary size with zero risk checks. If explicit volume must exist, clamp it to the risk-engine's maximum, not bypass it.
- **[major] Fill quantities are persisted in raw broker units while everything else is normalized** (ctrader_adapter.py:1660-1669 `_resolve_filled_volume` returns `filledVolume`/`executedVolume`/`volume` unscaled; same at 1221-1224 in reconciliation) versus `qty = volume / 1000.0` for positions (1531-1532) and `volume = qty * 1000` for orders (763-772). `LiveExecution.filled_qty` for cTrader fills is therefore ~1000× the quantity recorded for the same trade concept everywhere else (signals, positions, and all Alpaca records). Any cross-broker aggregation, slippage report, or exposure math touching cTrader executions is off by three orders of magnitude. Normalize at the boundary.
- **[major] Persist-before-validate wedges signals permanently** (ctrader_adapter.py:1330-1355): `save_live_signal` executes *before* symbol resolution (`_resolve_symbol_id`, 1430-1435) and action validation (1351-1352). An unknown symbol or unsupported action raises *after* the signal row exists, so every redelivery hits the duplicate guard (1333-1344) and is refused forever — the signal can never trade and reconciliation will keep scanning for an order that never existed. Validate symbol/action *before* the first persistence.
- **[major] Double sizing with divergent configurations** (ctrader_adapter.py:1321-1328; mirrored in alpaca_adapter.py:480-487): the daemon already sized the signal (`size_live_signal_for_submission`, daemon.py:505-553) using the deployment's `RiskConfig`, writing `qty` into metadata; `submit_order` then calls `size_live_signal(signal, account_balance=...)` again and reads `qty` back from metadata. Whichever sizing `size_live_signal` performs wins at the final gate — the deployment's configured `max_risk_per_trade`/`max_total_exposure` can be overridden by adapter-default risk parameters. Two owners for one safety-critical decision is a defect waiting to bite; adapters should trust the incoming `qty` (already risk-checked) and reject if absent, not re-derive it.
- **[minor] Global `_ASYNCIO_REACTOR_VALIDATED` + `asyncioreactor.install(loop)`** (ctrader_adapter.py:30, 179-203): installs the Twisted reactor against whichever loop first connects and caches success globally. Correct inside the daemon's single loop, but breaks `preflight`'s per-account `asyncio.run` loops (see preflight finding) and any future multi-loop embedding.
- **[minor] Token write-back mutates `os.environ`** (ctrader_adapter.py:484-487) from async code; combined with the daemon's file-first readers this widens the precedence split. The file write itself (138-171) is excellent: fcntl-locked, temp-file + `os.replace`, `chmod 0o600`, no secret logging — a positive.
- **[minor] `disconnect()` wipes state before the transport stops and uses a 0.1 s stop timeout** (ctrader_adapter.py:1273-1303): flags, symbol cache, order-id map and the *queue object* are reset before `stopService` completes; in-flight callbacks landing in that window hit a half-dead adapter.
- **[minor] Execution-queue replacement drops buffered events** (ctrader_adapter.py:1284): `self._execution_queue = asyncio.Queue(...)` swaps the object while `listen_executions` (1703) re-reads the attribute each iteration; events buffered in the old queue are lost rather than drained (mitigated by reconciliation, but only for orders already persisted).
- **[minor] Equity falls back to balance** (ctrader_adapter.py:1483-1488): if the trader payload lacks `equity`, exposure limits use balance and ignore floating PnL — conservative in losses, optimistic in gains.
- **[minor] `_to_datetime_from_ms` fabricates `now()` on malformed/non-positive inputs** (ctrader_adapter.py:67-76): a corrupted executionTimestamp silently re-dates a fill to ingestion time. Prefer raising or persisting a sentinel.
- **[minor] `_explicit_metadata_volume` truncates fractional volumes** (ctrader_adapter.py:210-213): `int(float(v))` maps `1.9 → 1` lot silently; reject non-integral.
- **[minor] Silent `except Exception: pass` blocks in teardown** (ctrader_adapter.py:998-1007): swallowing stop failures entirely hides transport leaks; at least log at debug.

Security: no secret material logged; auth-error classification is string-based but fail-closed. The fixed-checklist items (bare except, mutable defaults, subprocess, SQL, tz-naive) are clean in this file; datetimes are consistently tz-aware UTC.

Smells ([smell]): duplicated strict bool parser + token sets (49-64 ≅ daemon.py:143-158 ≅ preflight.py:60-81); duplicated `PROJECT_ROOT/DEFAULT_ACCOUNTS_ENV` (31-32, also daemon.py:59-60, account_discovery.py:40-41, service_discovery.py:9-10, test_signal_trigger.py:13-14); the 30+ `Any`-typed proto attributes (264-316) are unavoidable given lazy imports but constitute a large untyped surface; `connect()` (781-828) is long and mixes retry policy with auth and reconciliation.

Maintainability hot spots: `submit_order` ordering (above), the unit convention (no single constant/type for "broker volume vs lots"), `_send_request_with_refresh_retry`'s re-auth path (651-674) which sets `_authenticated = True` (670) without repopulating the symbol cache (later compensated by `_authenticate`'s cache check at 684 — subtle and undocumented).

### `live_runner/alpaca_adapter.py`

- **[major] C-03 (per-file view): Credential fallback can route an account through another account's keys** — see area-critical **C-03** for the full analysis (alpaca_adapter.py:157-183, `blocks[0]` fallback at 183).
- **[major] Blocking REST call on the event loop** (alpaca_adapter.py:225-229): `self._trading_client.get_account()` runs synchronously inside `async def connect`. In the daemon, `_connect_active_accounts` gathers connects for all accounts (daemon.py:1865-1884), so one slow/hanging Alpaca endpoint stalls the *entire* daemon loop — every other account's balance poll, Discord dispatch, and fill persistence included. Wrap in `asyncio.to_thread` like every other client call in this file (e.g. 263, 511).
- **[major] `TradingStream.run` dispatched via `asyncio.to_thread` is version-fragile, and handlers stack up on retries** (alpaca_adapter.py:697-711): `await asyncio.to_thread(self._trading_stream.run)` assumes `run()` is synchronous. Several alpaca-py versions expose `run()` as a *coroutine function*; in that case `to_thread` merely constructs an un-awaited coroutine and returns immediately — the loop then spins: subscribe → fake-run → else-branch resets backoff → repeat, with `subscribe_trade_updates(self._on_trade_update)` re-registered every iteration and **fills never streaming** (only the connect-time reconciler backfills). Even with a sync `run()`, re-subscribing on every reconnect attempt without unsubscribing risks duplicate handler registrations (dedupe keys contain the damage, but the log spam and CPU are real). Pin/verify the alpaca-py version, detect the coroutine case explicitly, and subscribe once per stream instance.
- **[minor] Unvalidated action mapped to SELL** (alpaca_adapter.py:443, 452): same defect as daemon F09 at the final gate — anything not `"buy"` becomes a SELL order.
- **[minor] `_to_datetime` fabricates `now()` on unparseable timestamps** (alpaca_adapter.py:50-58): fill timestamps silently re-dated on bad data.
- **[minor] Fractional-quantity handling is float-native** (alpaca_adapter.py:482, 441-455): `qty=float` goes straight into `MarketOrderRequest`; Alpaca rejects >2dp fractionals for some assets — a `round(qty, 2)` / Decimal step is missing (the file already imports `Decimal` for `_to_float`).
- **[minor] Reconcile dedupe key can collide across successive reconciliations** (alpaca_adapter.py:286-291): `alpaca:order:{id}:reconciled:{cum}` is stable per cumulative level, which is the point — but two *different* partial sequences reaching the same cumulative value (impossible for one order, possible after a cancel/refill edge) would dedupe away a legitimate delta. Low probability; note for the record.

Security: credentials resolved from env/file mappings only, never logged; stream uses same key material as trading (unavoidable). Checklist: no bare excepts, no subprocess, no SQL, tz-aware datetimes.

Smells ([smell]): the lenient `_parse_bool_env` (33-36) coexists with strict variants in daemon/preflight/ctrader — three parsing dialects for the same env flags; the ACC-block scanner (30, 70-108) duplicates account_discovery.py's (43, 114-153) **and has already diverged**: here paper-mode comes from `ALPACA_PAPER_ACC{N}`, there from `ALPACA_ENDPOINT_ACC{N}` containing "paper" — classic Shotgun Surgery evidence; `_build_order_request`'s six-parameter signature (421-431) carrying request classes is a Data Clump that exists only to defer imports.

### `live_runner/signal_sources/base_source.py`

- **F63 [minor][smell] Single-implementation ABC (Speculative Generality candidate)** (base_source.py:9-20): `BaseSignalSource` has exactly one concrete subclass. As a seam for future sources (Telegram, Twitter) this is defensible — judgement call, keep — but note that `listen_signals` is declared `async def … -> AsyncIterator` while implementations are plain async generators; the ABC's signature doesn't match its only implementation's actual shape (an async generator function), which mypy would flag.

### `live_runner/signal_sources/discord_source.py`

- **[major] Free-text regex parser directly drives live orders** — see area-critical **C-02** for the full three-part chain (garbage-ticker fallback, comma-thousands price corruption, broad sell classifier) with evidence at discord_source.py:174-197, 55-97, 45-47, 200-209, 49, 217, and the unenforced daemon-side symbol whitelist at daemon.py:749-754.
- **[minor] Channel id interpolated unvalidated into the REST URL** (discord_source.py:384): `f".../channels/{channel_id}/messages..."` — values originate from config/env, but a stray character changes the API path. Validate `^\d+$` (Discord snowflakes).
- **[minor] Timestamp fabrication on parse failure** (discord_source.py:244-251, 310): unparseable `created_at` silently becomes `now()`, shifting candle-close anchoring (`received_at`) by the polling lag.
- **[minor] Unbounded signal queue** (discord_source.py:338): `asyncio.Queue()` with no maxsize; a stalled consumer (e.g. a deployment blocked in `wait_for_candle_close` for a 1d timeframe) lets memory grow without bound during a signal burst.
- **[minor] DB write per parsed signal** (discord_source.py:480-483): `RUNNING + last_activity` update on every parsed signal — chatty; throttle or rely on the poll loop's health.
- **[minor] `"401"` substring classified as auth failure** (discord_source.py:107-117): any error message containing "401" (an amount, an id) is treated as terminal auth and blocks the source. Match on status codes (as `_fetch_messages` already does) rather than message substrings.

Security: token sent via `Authorization` header, never logged; HTTPS enforced; `requests` calls carry `timeout=10` (good — checklist item satisfied); rate-limit handling honors `retry_after` (409-425, 527-539).

Smells ([smell]): the 1-second `wait_for` poll inside `listen_signals` (662-665) is a latency/latency tradeoff, fine, but the generator's tri-state exits (paused / auth error / unexpected death) are subtle enough to warrant a state enum. The message adapter (`_DiscordMessageAdapter`, 302-313) is a clean Middle Man hiding REST payload shapes — positive.

### `live_runner/signal_sources/execution_timing.py`

- **[minor] Daily/weekly candle boundaries assume UTC calendar alignment** (execution_timing.py:57-60): `_next_candle_close` floors to UTC epoch multiples. Equity daily candles close at session end (e.g. 20:00 UTC), not 00:00 UTC, so `wait_for_candle_close(timeframe="1d")` waits until midnight UTC — up to four hours *after* the real close, executing on stale signal context. Correct for intraday fx/crypto; wrong for equity d/w. Align per-asset-class session calendars or restrict the feature to intraday timeframes.
- Otherwise clean: fully tz-aware datetimes, cancellation-safe (`asyncio.sleep` inside the caller's task), graceful no-op for unparseable timeframes (31-54).

### `live_runner/preflight.py`

- **[major] `asyncio.run()` per account conflicts with the globally-installed Twisted reactor** (preflight.py:120-124, 232-246; root cause ctrader_adapter.py:179-203): each `_run_broker_connectivity_check` spins a fresh event loop; the first cTrader account installs the asyncio reactor onto *its* loop and sets the global validated flag; the second account's loop gets `ReactorAlreadyInstalledError` swallowed and then OpenApiPy's `deferred.asFuture(oldLoop)` attaches to a closed loop — hangs or spurious failures. Multi-account cTrader preflight is unreliable today. Run all checks in one loop (`asyncio.run` around a gather), or reset/install the reactor per loop.
- **[minor] Adapter construction sits outside error capture** (preflight.py:232-241): `CTraderLiveAdapter(...)` raising (bad account id) escapes `run_live_preflight` and crashes the CLI instead of becoming a structured per-account error like everything else.
- **[minor] Connectivity results print balances/positions to stdout** (preflight.py:408): acceptable for a local operator tool; avoid in CI logs (financial data in pipelines).

Smells ([smell]): third copy of the strict bool parser and the cTrader credential resolver (60-94 ≅ daemon.py:240-252 ≅ ctrader_adapter.py:49-64). Positive: the preflight *does* catch the live-vs-demo misconfiguration (warnings at 152-153, 215-218) — good money-safety posture for a gate tool.

### `live_runner/market_data_stream.py`

- **[minor] Boolean precedence bug in `_is_crypto_symbol`** (market_data_stream.py:19-23): `"/" in normalized or normalized.endswith("USD") and "-" in normalized` parses as `A or (B and C)`. Intent was almost certainly `(A or B) and … ` / explicit grouping. Today Alpaca crypto symbols use `/`, so behavior survives, but `BTC-USD`-style inputs and the bare `BTCUSD` form route to the *stock* stream where they silently receive no data.
- **[minor] Credentials resolved from `os.environ` only** (market_data_stream.py:216-219): `resolve_alpaca_credentials` is called *without* the daemon's `.accounts.env` mapping, so accounts whose credentials live only in the file never get a stream — quote bars silently missing for exactly the accounts the daemon trades.
- **[minor] Worker thread races and leaks** (market_data_stream.py:104-182): `set_symbols` (161-173) touches `self.stream` and calls `unsubscribe_bars`/`subscribe_bars` from the *caller's* thread while `_run` (146) may be swapping `self.stream` after a disconnect — unsubscribe can land on a dying/new instance; `stop()` (175-182) sets `_running = False` but never joins the thread, so rapid refresh cycles can pile up orphan threads each holding an SDK websocket until GC.
- **[minor] Bar serialization can raise and kill the stream loop** (market_data_stream.py:41-59): `datetime.fromisoformat(ts.replace("Z", "+00:00"))` raises `ValueError` on malformed timestamps; the exception propagates out of the SDK callback into `_run`'s generic handler → full stream teardown + reconnect churn for one bad bar. Guard per-bar.

Positive: the `_lock` discipline around the bars deques (188-192, 273-301) is correct; `deque(maxlen=…)` bounds memory per symbol; connection-limit errors stop cleanly (122-134).

### `live_runner/account_discovery.py`

- **[major] Blocking socket I/O inside `async` functions** (account_discovery.py:691-715, 334-380): `_discover_ctrader_accounts_on_host` performs TLS handshake, application auth, account listing, and *per-account* detail/auth/trader round-trips — each with 12–20 s timeouts — synchronously inside a coroutine. Called from `discover_configured_accounts` (763+) on the daemon's loop, discovery of a few accounts against a flaky endpoint freezes all live trading for minutes. Move to `asyncio.to_thread` (the function body is deliberately thread-free and would port unchanged).
- **[minor] Unbounded length-prefixed frame read** (account_discovery.py:365-368): `frame_size = struct.unpack(">I", recv_exact(4))[0]` then `recv_exact(frame_size)` with no ceiling — a hostile/misbehaving endpoint can advertise a 4 GiB frame and exhaust memory. Cap at the protocol's plausible maximum (a few MiB) and raise beyond it. (Security-hardening finding.)
- **[minor] `load_dotenv(accounts_env)` mutates process env** (account_discovery.py:766-767): same cross-cutting precedence hazard as F05 — discovery reads merged env while the daemon reads the file first; discovered accounts and traded accounts can disagree about credentials/mode.
- **[minor] Balance silently defaults to 0.0 on partial discovery** (account_discovery.py:197-200, 588, 684): when trader details are unavailable ("keep" path, 607-643) the account is registered with `balance=0.0` rather than unknown — downstream dashboards/routing see a credible-looking zero.
- **[minor] Demo/live host dedupe assumes globally-unique account ids** (account_discovery.py:727-758): accounts from `demo.ctraderapi.com` and `live.ctraderapi.com` are merged by string id with demo preferred. If cTrader id spaces are per-environment (worth verifying against the OpenAPI docs), two distinct accounts could collapse into one entry pointing at demo.
- **[minor] Error classification by `repr()` substrings** (account_discovery.py:676-678, 100-111, 607-643): decisions like skip-vs-keep hinge on `repr(exc)` containing "NOT AUTHORIZED" — brittle across library version changes; classify on typed errors/error codes as ctrader_adapter does.

Security positives: `ssl.create_default_context()` with `minimum_version = TLSv1_2` and hostname verification (698-701); sockets closed via context managers (no leak); secrets never logged (debug field dumps cover account payloads, not tokens).

Smells ([smell]): fourth copy of the ACC-block scanner and lenient bool parser (43, 78-88); the label/broker/login field-name candidate tuples (50-65) read as accumulated guesses (Speculative Generality) — trim to observed fields.

### `live_runner/config_parser.py`

- **[minor] Unknown-account deployments are warn-only here, error-level in preflight** (config_parser.py:42-53 vs preflight.py:320-323): the same condition produces a warning in the loader and an error in the gate tool — pick one contract (error) so automation can't wave a broken config through the loader path.
- Otherwise clean: `yaml.safe_load` (no unsafe loader), explicit type checks, path validation.

### `live_runner/service_discovery.py`

- **[major] Env-over-file precedence inverts the daemon's resolution order** (service_discovery.py:17-24 vs daemon.py:1114-1118): discovery builds `env_values` from the file then lets `os.environ` overwrite it; the daemon's `_resolve_discord_bot_runtime` prefers the file. Consequence: a DISCORD_BOT service is *created* with the env's token/channel and *runs* with the file's — or vice versa after an edit — so the bot can operate on an unintended channel with an unintended identity. This is the registration half of finding F05; fix both halves with one shared resolver.
- Otherwise clean: token-pattern matching is tight (`^DISCORD_BOT_[A-Z0-9_]+_TOKEN$`), no secret material is returned (only key names).

### `live_runner/stockkit_routing.py`

- **[minor] Production account ids hardcoded in source** (stockkit_routing.py:8-12): `STOCKKIT_ACCOUNT_ROUTES` embeds live Alpaca account numbers; rotating an account requires a code change and redeploy (and the fail-closed behavior is otherwise excellent — misroute raises, daemon blocks the deployment, daemon.py:412-439). Load from config/env.
- **[minor] Unguarded `KeyError` in `expected_account_for_stockkit_route`** (59-60): safe for today's three routes (callers guard via `infer_stockkit_route` returning ""), but adding an alias without a route entry turns a config mistake into a KeyError in the hot path. `.get(route)` + explicit error.

### `live_runner/ctrader_deployment.py`

- **[minor] Score weights are unscaled and arbitrary** (ctrader_deployment.py:17-27): `2*sharpe + cagr + psr − dd_penalty` mixes a ratio (sharpe, typically −3..3), a percentage (cagr, 0..100+?) and a probability (psr 0..1) with hand-tuned coefficients — the ranking is dominated by whichever metric has the widest natural range. Document intent or normalize terms; as-is, "top 5 for live" is effectively "highest CAGR".
- **[minor] All planned deployments land on the first cTrader account** (66-69): `ctrader_accounts[0]` with no spread or capacity consideration; fine for a planner stub, surprising for operators reading "ctrader_top5".
- Smell ([smell]): NaN guard `value if value == value else default` (14) — use `math.isnan` for legibility.

### `live_runner/account_routing.py`

Scanned — **nothing found.** Duplicate-id and duplicate-routing-key validation is sound, including numeric normalization (`int(identifier)` collapsing "007"/"7") and the positive-value guard (account_routing.py:13-40). This is the kind of fail-closed validation the rest of the area should imitate.

### `live_runner/test_signal_trigger.py`

- **[minor] Live-order operator tool with no paper guard** (test_signal_trigger.py:30-52, 55-66): submits a real market order using ambient credentials; if `ALPACA_PAPER=false` is set (or an ACC block resolves live), the "slippage pipeline test" trades live by default. Add `--confirm-live` gating on `paper=False`.
- **[minor] `assert` for control flow** (33): `assert adapter.api_key and adapter.secret_key` vanishes under `-O`; raise explicitly.
- **[minor] Blocking data fetch inside the event loop + `load_dotenv` mutation** (34, 65): `_fetch_latest_price` runs a synchronous REST call inside `async _run`; `load_dotenv` mutates process env (precedence family again). Both trivially fixable.
- Smell ([smell]): fifth copy of `PROJECT_ROOT/DEFAULT_ACCOUNTS_ENV`.

---

## 3. Consolidated area-critical findings

- **C-01 [critical] — Stop-loss / take-profit are never sent to the broker.**
  Evidence chain: the daemon computes and persists SL/TP into signal metadata (daemon.py:517-518 `stop_loss`/`sl_price`/`stop_price`, 518 take-profit aliases; 664-667 copies them onto every live signal; the risk engine is explicitly told `enforce_stop_direction: True`, daemon.py:532, and sizes positions as `risk_amount / stop_distance`). Neither adapter acts on it: `CTraderLiveAdapter._build_new_order_request` (ctrader_adapter.py:1437-1448) sets only account/symbol/MARKET/IOC/volume/clientOrderId — no `stopLoss`, no `takeProfit`, no follow-up protective order — and `AlpacaLiveAdapter._build_order_request` (alpaca_adapter.py:421-456) builds a plain market or single-leg limit with no `OrderClass.BRACKET`/OTO. Result: every live position is opened **naked**, while the sizing math assumed a bounded loss. On leveraged FX (cTrader) an adverse gap is unbounded. This is the single most important defect in the area. Fix: attach native protective orders (cTrader `ProtoOANewOrderReq.stopLoss/takeProfit` in absolute price, respecting `relativeDistance` constraints; Alpaca bracket orders), and refuse submission when a signal carries SL metadata the adapter cannot honor.
- **C-02 [critical] — Live trade actions are parsed from free-form Discord chat by layered regexes with garbage-tolerant fallbacks** (discord_source.py). Three compounding defects in one subsystem:
  - **Fallback ticker = any uppercase word** (discord_source.py:174-197, 55-97): `_fallback_uppercase_symbol` takes the first non-stopword uppercase token of the first non-empty line. `_SKIP_WORDS` (55-97) omits ordinary words like **ON, IS, AT, A, TO, OF, IN, UP, IT, BE, BY, DO, GO, WE** … so a message like *"BUY SIGNAL ON TSLA PRICE $200"* parses `ON` as the ticker in earlier stages' absence, and *"THIS IS HUGE"* yields ticker `THIS`. The only thing standing between garbage tickers and live orders is the deployment `symbols` whitelist — which preflight *requires* (preflight.py:324-325) but the **daemon never enforces** for DB-created deployments (`_deployment_covers_discord_symbol`, daemon.py:749-754, treats an empty symbol set as "cover everything").
  - **Comma-thousands price corruption** (discord_source.py:45-47, 200-209): `\$\s*(\d+(?:[.,]\d+)?)` captures `"1,234"` from `"$1,234"` and `float("1,234".replace(",", "."))` produces **1.234** — a 1000× wrong entry price fed to `TradeSignal.entry_price`, sizing sanity checks, and slippage baselines. European decimal commas ("$1,50") are equally misread as 1.50. Parse thousands separators properly (`1,234.56` → 1234.56) or reject ambiguous formats.
  - **Broad sell classifier** (discord_source.py:49, 217): any message containing "momentum reversal" — or the bare words sell/short — becomes action `sell` regardless of context (discussion, warnings, commentary). Combined with the ticker fallback above, a *chat message about a stock* can mint a live short-side order (degrading to exit-only where shorts are disabled, which then exercises F04).
  These are inherent risks of regex-on-chat, but the specific combination (garbage ticker + plausible price + action) sails straight through to `submit_order_with_risk`. Minimum hardening: enforce non-empty `symbols` at dispatch time in the daemon; require the extracted symbol to appear in a ticker-like context (`$SYM`, bold, or labeled) before falling back; validate price magnitude against recent market data before submission.
- **C-03 [critical] — A credential-routing fallback can send one account's orders through another account's broker keys** (alpaca_adapter.py:157-183): with multiple `ACC{N}` blocks configured and *no* hint keys present anywhere (`_alpaca_blocks_have_hints` false, 134-138), any account whose `broker_login` is empty falls through to `return _alpaca_block_credentials(blocks[0])` (183) — the *first* block, regardless of which account it belongs to. Two accounts, no hints ⇒ both trade through ACC1's keys and both fills stream into both listeners. The comment at 175-176 shows the authors understood this hazard for the hinted case; the unhinted case retains the hole. Return `None` (skip with a loud warning) instead of `blocks[0]`.

## 4. Cross-cutting themes

1. **Environment precedence is undefined.** File-first: daemon (`_load_accounts_env_values`, `_resolve_discord_bot_runtime`), ctrader credential resolution. Env-over-file: `service_discovery` (overwrites), `account_discovery` (`load_dotenv`), `market_data_stream` (env only), `test_signal_trigger` (`load_dotenv`). Three different effective answers for "what credentials is this account using?" (F05, C-03, discovery/market-data/test findings).
2. **Duplication with divergence** (Fowler: Duplicated Code + Shotgun Surgery): strict/lenient bool parsers ×3 styles ×5 files; `PROJECT_ROOT/.accounts.env` ×5; ACC-block scanners ×2 (already semantically diverged on paper-mode); cTrader credential resolvers ×3 (daemon, preflight, adapter env fallback); retry/backoff scaffolds ×4; error-marker vocabularies ×4 (daemon `_is_terminal_ctrader_runtime_error`, ctrader `_is_terminal_auth_error`, discord `_is_discord_auth_error`, account_discovery `_is_ctrader_account_not_authorized_error` — each with slightly different marker lists, so the *same* broker error can be terminal in one layer and retryable in another).
3. **Broker dispatch is a Repeated Switch** (4 sites) begging for a registry/factory; **credential tuples are Data Clumps**; **statuses/quantities/volumes are Primitives** lacking types (the cTrader volume-unit confusion is precisely a missing `BrokerVolume`/`Lots` distinction).
4. **Fail-closed instincts are strong** (refusing duplicate submissions, refusing ambiguous order recovery, refusing synthetic OHLCV, refusing sub-minimum upsizing) — the gaps are concentrated at the *boundaries*: parser output trusted as truth (C-02), metadata `volume` trusted as truth, `blocks[0]` trusted as identity (C-03).

## 5. Fixed bug/security checklist (Python) — area-wide verdict

| Item | Verdict |
|---|---|
| Mutable default args | Clean — `field(default_factory=…)` used correctly everywhere |
| Bare except | No bare `except:`; several silent `except Exception: pass` (ctrader_adapter.py:998-1007, market_data_stream.py:168-171/178-182) — logged as minors |
| Race conditions on shared state | Module-global registries (F02) safe only under the single-loop invariant; `_StreamWorker.stream` swap race (market_data_stream finding 3); otherwise lock discipline correct |
| SQL string building | None in area (DB access via `shared.database` helpers) |
| Subprocess without timeout | No subprocess use |
| tz-naive datetimes | Consistently handled — naive inputs are coerced with `replace(tzinfo=utc)`; residual issue is *fabricated* `now()` on parse failure (multiple files) and UTC-aligned daily candles (execution_timing) |
| Resource leaks | Threads not joined (`market_data_stream`), execution queue swapped under consumers (ctrader), shutdown without awaiting cancelled tasks (daemon F07); sockets/files all use context managers |

## 6. Maintainability hot-spot ranking

1. **daemon.py (2144 lines)** — credential management + risk orchestration + Discord plumbing + lifecycle in one module (Divergent Change); split along those seams.
2. **The SL/TP pipeline** — computed upstream, dropped downstream with no contract tying metadata to broker orders (C-01); introduce an adapter capability check (`supports_protective_orders`) enforced in `submit_order_with_risk`.
3. **Unit conventions for cTrader volume** — no shared constant/type; the ÷1000 rule is re-derived (sometimes forgotten) at each site.
4. **Credential resolution** — five modules, three precedences, two divergent parsers; consolidate into one module owning file+env merge, per-broker blocks, and paper/demo inference.
5. **Error classification vocabulary** — one shared `classify_broker_error()` instead of four marker lists.

---

## Finding counts by severity

- **Critical: 3** (C-01, C-02, C-03)
- **Major: 14** (daemon F02–F06; ctrader_adapter: volume bypass, fill-unit mismatch, persist-before-validate, double sizing; alpaca_adapter: blocking connect, stream-run fragility; discord_source: regex-parser chain (see C-02); preflight reactor conflict; account_discovery blocking I/O; service_discovery env-over-file precedence — the per-file C-pointer bullets resolve to the criticals above and are not counted twice)
- **Minor: 48** (all remaining `[minor]` findings across every file, including the security-hardening frame-cap finding)

*(Per-file accounting: every one of the 18 tracked files produced findings or is explicitly recorded above as scanned with nothing found — `__init__.py`, `account_routing.py`, and `signal_sources/__init__.py` are the clean ones; `base_adapter.py` and `signal_sources/base_source.py` produced minor/smell-level notes only.)*
