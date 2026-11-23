# Live Integration Deploy Runbook

Status: credential-gated as of 2026-07-13.

## Preconditions

- `.accounts.env` exists and remains gitignored. Entry points that need a different file take it
  as an explicit argument (preflight's `--accounts-env`, the daemon's `accounts_env` parameter);
  there is no environment override, so every reader resolves the same path.
- Alpaca account blocks `ACC1..3` (Nitro, RamJet, RocketFuel) contain paper endpoints only; IDs werden aus `.accounts.env` aufgeloest, nicht hier dokumentiert.
- Discord bot tokens and channel IDs are set for Nitro, RamJet, and RocketFuel.
- cTrader credentials are demo credentials if the cTrader smoke is run.
- Trailing stops: live support is per broker (see "Live Trailing Stops" below). Alpaca executes
  `none` and `percent`; cTrader executes `none` only. Any other combination is rejected when the
  deployment is validated and again at signal time, before anything reaches the broker — including
  a mode that only comes from the bot's own config default, which is read from the processor the
  deployment runs.

## Ops Alert Channel (#304)

The daemon can push alert-worthy log records to a Discord-compatible webhook so an operator is
paged instead of only finding problems in the logs. Implementation: `shared/alerts.py`, installed
by `run_daemon()` at daemon start; importing `live_runner.daemon` does not install the alert sink
(logging and Sentry setup still happen at import).

Set the variables in the environment the daemon runs with — e.g. exported before start or in the
sourced `.accounts.env` / a systemd `EnvironmentFile`.

- `OPS_ALERT_WEBHOOK_URL` — webhook URL. **Unset disables alerting entirely**; the daemon then
  behaves exactly as before. There is no other configuration needed to turn it on.
- `OPS_ALERT_MAX_PER_MINUTE` (default `12`) — alerts per sliding minute; overflow is dropped and
  the drop count is folded into the next alert that does go out.
- `OPS_ALERT_DEDUP_SECONDS` (default `300`) — alerts with the same dedup key inside this window
  send once. The key is the `data.alert` marker plus the record's `account_id`,
  `deployment_id`, `service_id` and `symbol` when present — so the same marker on a different
  deployment is a different alert, but a changed payload (e.g. new mismatch quantities) under the
  same marker and scope is still suppressed until the window expires. For records without a
  marker the key is logger + level + message template (not the formatted message) plus the same
  scope identifiers.
- `OPS_ALERT_MIN_LEVEL` (default `ERROR`) — minimum level for level-triggered alerts, as a level
  name (`CRITICAL`) or number. Raise it to `CRITICAL` to page only on emergencies; an explicit
  `data.alert` marker still bypasses the floor because it is a deliberate operator alert. An
  invalid value falls back to `ERROR` rather than muting the channel.
- `OPS_ALERT_EVENTS` (unset = everything) — comma-separated allowlist of event classes. An entry
  matches a record's `data.alert` marker (e.g. `exit_ledger_mismatch`) or its logger name; when set,
  only matching records page, so routine errors can be filtered without touching call sites. Logger
  names match the logging hierarchy: `trading.live-runner` also covers
  `trading.live-runner.<child>` but not `trading.live-runnerx`. **Markers and `CRITICAL` records
  always page regardless of the allowlist** — the allowlist exists to filter routine errors, and
  muting those two would hide real emergencies. Note that today all live modules log under the
  single logger `trading.live-runner` (`get_logger("live-runner")`), so a logger-name allowlist can
  only select "the whole live daemon", not one module inside it.
- `OPS_ALERT_IGNORE` (unset = nothing ignored) — comma-separated denylist of fnmatch patterns
  (e.g. `poll failed for *`, `*heartbeat*`) matched against the message template and the rendered
  message. Matching is `fnmatchcase`: case-sensitive, and a pattern cannot itself contain a comma
  (the comma separates entries). A match drops the record even when it is a `data.alert` marker or
  `CRITICAL`, so this is the explicit "this one routine error is noise" switch — a broad pattern
  such as `*failed*` can silently mute the known emergency markers, which is why setting it logs a
  startup warning. Use it to mute a single noisy ERROR without touching call sites; prefer it over
  a broad `OPS_ALERT_EVENTS` allowlist.

The effective filter (min level, allowlist, denylist) is logged once at startup on the
`trading.alerts` logger, and setting `OPS_ALERT_EVENTS` or `OPS_ALERT_IGNORE` additionally logs a
warning — a filter that matches nothing, or a denylist wide enough to mute the emergency markers,
would otherwise leave the channel silently dead or silently mute.

What is alerted: every `ERROR`/`CRITICAL` record (or whatever `OPS_ALERT_MIN_LEVEL` sets), plus
any record carrying `data.alert` (the explicit operator-alert marker, e.g. `exit_ledger_mismatch`)
at any level — `data.alert` markers route even when `LOG_LEVEL` would keep the record out of the
file/console output — then filtered by `OPS_ALERT_IGNORE` and, for routine errors only,
`OPS_ALERT_EVENTS`.

Guarantees: the sink is fire-and-forget on a bounded queue (256 alerts). A full queue or a slow
webhook drops and counts alerts; it can never block or raise into a trading path. A failed send
(e.g. a Discord 429 or 5xx) is counted and logged on the `trading.alerts` logger, not retried — a single
lost alert is accepted for v1 rather than risking a retry storm against a struggling webhook.
The sink's own logger (`trading.alerts`) never pages, so a failing webhook cannot feed alerts
back into the channel.
Payloads are
redacted with the same rules as the JSON log formatter before they leave the process. Only the
live daemon installs the sink today; other entry points are unaffected.

## Live Trailing Stops

Central definition: `shared/trailing.py` (`LIVE_SUPPORTED_TRAILING_MODES_BY_BROKER`). Execution:
`live_runner/trailing_stops.py`.

| Broker  | `none` | `percent` | `supertrend_flip` |
|---------|--------|-----------|-------------------|
| Alpaca  | yes    | yes       | no (see below)    |
| cTrader | yes    | no        | no                |

cTrader stays on `none`: the daemon has no cTrader market data to trail on, and the adapter does
not declare the `supports_stop_amendment` capability. Unknown brokers get `none` only.

**Why `supertrend_flip` is not live yet (#168)**

The replay backtest executes it (`runner/engines/signal_replay_adapter.py`); live rejects it on
every broker, at deployment validation and at signal time like any other unsupported mode. The
blocker is the entry order, not the trailing loop:

- The Alpaca entry is a BRACKET for the full quantity. Its take-profit leg sits at the signal's
  `take_profit`, which for RamJet *is* the TP1 price, so a `supertrend_flip` entry would close
  100 % at TP1 instead of `tp1_exit_percent`.
- Alpaca reserves the quantity of every working reducing-side order. While the bracket's stop
  leg holds the full quantity, no separate TP1 limit (or partial market) order for part of it
  is accepted, and shrinking the stop first would leave the TP1 part without a stop.

Proposed design for the follow-up: for `supertrend_flip` entries, submit the position as two
orders with deterministic client order ids derived from the signal id: a BRACKET for the TP1
tranche (take profit at TP1, stop at the initial stop) and an OTO for the remainder (stop only).
Tranche quantities must be whole shares (Alpaca brackets refuse fractional quantities) and the
rounding must be mirrored by the replay. The trailing loop then (a) detects TP1 as the tranche
bracket's take-profit fill and amends the remainder's stop to breakeven with the existing
replace, (b) evaluates the RamJet Supertrend on each closed bar, with enough history to match the
replay's warm-up, and (c) on the first red bar after activation closes the remainder through the
adapter's locked flatten path (cancel, close, restore the stop on failure), so no stop is left
orphaned. The only state that is not derivable from the broker is "activated"; it would be
persisted on the entry signal row (`live_signals.metadata`). This changes order submission,
recovery and fill reconciliation, which today assume one broker order per signal.

**Replay semantics to mirror live (decided 2026-11-16, `#176`)**

Three rules the replay implements today; a live implementation must copy them exactly, or paper
results stop predicting live behaviour:

- *TP1 tranche in whole shares, floored.* The tranche is
  `int(entry_quantity * tp1_exit_percent)` — the original bot's arithmetic
  (`shared/trailing.py: tp1_partial_quantity`) — and the remainder is the entry quantity minus
  that tranche. A tranche of zero whole shares (1 share at a 0.5 fraction) is skipped, not
  rounded up: no sell, no breakeven stop, and the Supertrend workflow never arms, so the whole
  position keeps its original stop. The trade's `supertrend_flip` metadata records it as
  `tp1_skipped_time` so a backtest never hides it.
- *The trail starts on the bar after the TP1 bar.* The TP1 bar after the TP1 fill is dead for
  the workflow: its close is not read (a green close there does not activate the trail, a red
  close there cannot flip it) and its range is not checked against the stop moved on it, so the
  breakeven stop only takes effect from the next bar. Nothing that happens at or inside the
  TP1 bar after the TP1 fill may move or trigger the trailing stop — live that means the
  amendment waits for the first bar that closes after the TP1 fill.
- *A bar that opens beyond a protective level fills at the open.* The pre-TP1 stop, the
  post-TP1 stop (initial or breakeven) and the TP1 tranche all follow the gap rule (#287):
  the open is the bar's first event, so a bar opening past a level fills at `bar_open` —
  never the skipped level — and beats any later same-bar touch of the other level
  (`contract.resolve_protective_exit` checks the gap before the stop-first same-bar policy).
  One qualification inside the flip workflow: a bar that gaps through TP1 fills the
  *tranche* at the open, but the remainder's pre-TP1 stop is still working for the rest of
  that bar — the OTO stop sits at the initial level and the breakeven amendment only takes
  effect on the next bar — so a later same-bar trade through it stops the remainder at the
  stop level. Live that means the open-versus-level check must run before the bar's range
  is evaluated, and an entry fill must never be checked against a level on its own fill bar
  (the replay never evaluates the entry bar).

**Where an unsupported mode is rejected**

- Deployment validation: `LiveDeploymentConfig` rejects a `trailing_mode` override no live broker
  executes; `create_live_deployment`, `update_live_deployment_account` (re-point) and the YAML
  parser reject one the deployment's account broker does not execute.
- Deployment validation, bot default: the mode is a setting of the *bot*
  (`signal_bots/<bot>/config.yaml`) and the processor emits it with every signal, so a default that
  the deployment's broker cannot execute is just as fatal. Those three stages build the processor
  the deployment actually runs (bot config merged with the deployment's
  `signal_processor_config`, exactly as the daemon builds it) and ask it for the mode
  (`shared.trailing.validate_live_deployment_trailing_mode`). A deployment that sets
  the override explicitly is covered by the check above and is not built twice.
- Signal time: the signal-processor gate and the order-preparation gate both check the mode
  against the adapter that would submit the order (broker table and the adapter's own
  `supports_stop_amendment` flag). A refused signal is not traded; a requested trail is never
  dropped silently.
- Stored rows: a stored deployment whose trailing override no live broker executes, or whose
  processor's own default one does not, still loads (so it stays listed and can be stopped); the
  daemon logs an error for it at load and every signal it produces is refused before it reaches
  the broker.

**How `percent` is executed (Alpaca)**

- Arithmetic: `RiskEngine.apply_trailing_stop`, the same call the replay backtest makes, applied
  once per closed bar to the bar close. A stop only ever moves in the favourable direction. The
  broker stop is the computed stop rounded to Alpaca's price tick away from the market (down for
  a long, up for a short), so it is at most one tick looser than the replay stop.
- Trigger: each closed bar of the deployment's signal timeframe (`signal_config.timeframe`, else
  the deployment timeframe). The loop wakes every 15 s but only compares timestamps; it reads
  positions, orders and bars (`AlpacaLiveAdapter.get_market_data`) when a bar boundary has passed
  or a retry is due. A bar that is not published yet is re-requested with backoff (15 s doubling
  to 5 min), but only for up to 15 minutes after the boundary (at most one bar): after that the
  feed is treated as closed (night, weekend, holiday) and the next bar boundary tries again.
- Timeframes: only `1m`, `5m`, `15m`, `30m` and `1h` can be trailed (`LIVE_TRAILING_TIMEFRAMES_BY_BROKER`
  in `shared/trailing.py`). Their bars are stamped on UTC-aligned boundaries, which is what the
  manager's wake-up slots and the "closed at open + length" rule assume. `1d` is NOT trailable
  yet: Alpaca stamps equity daily bars at New-York midnight (05:00Z/04:00Z), so the trail would run
  a session late versus replay. `4h`/`1w` are not served by the adapter at all. Per #150 such an
  entry is refused, not traded untrailed: a `percent` entry on an untrailable timeframe is rejected
  at signal time (processor gate and order-preparation gate), and a deployment that sets
  `trailing_mode: percent` in `signal_processor_config` is rejected at API create and YAML parse, and
  so is a `percent` mode that only comes from the bot's own config default. Session-aligned daily
  trailing is a follow-up.
- Amendment: the protective stop order is replaced in place (`PATCH /v2/orders/{id}`). The
  previous stop keeps working until the replacement is accepted and stays if it is refused; the
  stop is never cancelled in order to move it. The replace and the adapter's order sweeps (STOP
  flatten, cancel of exposure-opening orders, orphan cleanup) are serialised per adapter, so a
  flatten never misses a stop that was replaced under it.
- Failure: a refused or failed amendment, a missing or ambiguous stop, or missing market data is
  logged at ERROR (`Live trailing stop could not be updated; ...`, with account, symbol, signal,
  stop order id, current and target stop, consecutive failure count) and sent to Sentry
  (`component=live_trailing_stops`). It is retried with the same backoff; the previous
  broker-side stop stays in force meanwhile.
  A failure that repeats is not retried forever: once failures have lasted 40 minutes without a
  single success (`TRAILING_GIVE_UP_AFTER_SECONDS`, measured from the first of the consecutive
  failures, so a 1m deployment is not given up on any sooner than a 1h one) the position is given
  up on with one CRITICAL alert (`Live trailing stop given up on ...`). A failure of the
  account-wide stop listing counts once per account, not once per position. A given-up position is
  not retried at the normal pace and raises no further alerts, but it is probed every 15 minutes
  (`TRAILING_GIVEN_UP_PROBE_SECONDS`): the first pass that succeeds resumes trailing (INFO
  `Live trailing stop resumed ...`) and it catches up on the closed bars it missed (up to 50).
  Failures no retry can fix (an unservable timeframe, an adapter without market data) are never
  probed and last until the entry signal or the position changes (a daemon restart also starts
  counting again). The broker-side stop stays at its last level while given up.
  This includes a trailed stop the market has already fallen through since the bar closed: the
  broker refuses it, the old stop keeps protecting the position, and the replay backtest would
  have exited on that bar instead.
- Restart: nothing needs to be restored. The current stop is read from the broker on every pass
  and the trailing config from the persisted entry signal (`live_signals.metadata`,
  `trailing_sl_config`). After a restart the trail continues from the broker's stop, catches up on
  up to 50 closed bars, and cannot move a stop backwards.
- Cleanup: in-memory progress is dropped as soon as the position is gone at the broker (stop or
  take profit hit, exit signal, manual close) or its deployment is no longer ACTIVE. A partial
  exit keeps the trail on the remaining quantity.
- Stop quantity: an amendment moves the price only. After a partial exit a bracket stop leg can
  still be sized to the original entry; the loop logs a WARNING once per position when the stop
  covers more than the position holds, but does not resize it. What Alpaca does when such a stop
  triggers (cap, oversell, reject) has **not** been verified on paper.

**Limits to know when operating it**

- Only ACTIVE deployments are trailed. PAUSED leaves orders untouched and STOPPED flattens; the
  stop stays at its last broker level. Resuming continues from that level.
- A position is attributed to the newest filled entry signal for its symbol among the account's
  ACTIVE deployments. A position opened outside the platform on a symbol whose newest filled
  platform entry asked for `percent` trailing would be trailed too; keep manual trades off
  accounts that run trailing deployments.
- Exactly one working plain `stop` order on the reducing side is required per symbol. Several
  stops on one symbol (pyramided entries), a stop-limit, or no stop at all is reported as a
  failure on every pass and nothing is amended.
- Real-money accounts are never amended: the loop only receives connected demo/paper adapters,
  the same hard block that applies to orders.
**Verified on Alpaca paper (2026-11-16, parts re-checked 2026-11-20)**

Probe on a paper account (endpoint host checked as `paper-api.alpaca.markets`): 1 share of `F`
as a market bracket entry (stop 3 % below, take profit 5 % above), then listings, a replace and a
clean-up.

- Right after the entry fills: parent `filled`; take-profit leg `new`; stop leg `held`.
- `status=open` (nested or flat) lists the take-profit leg only. **The `held` stop leg is not in
  any open listing.** `AlpacaLiveAdapter.list_protective_stops` therefore also reads filled
  parents of symbols with an open position from a `status=all`, `nested=true` listing and takes
  their `held` stop legs (fixed in this PR; before it a real bracket position reported "no stop").
- The same leg, on the same account and shape, is what the STOPPED sweeps have to cancel: the
  flatten (`close_all_positions`) and the orphan-protection sweep (`cancel_orphaned_protections`)
  now read the identical `status=all`, `nested=true` listing (the flatten for the position's
  symbol, the orphan sweep account-wide), a leg in `pending_cancel` counts as resting, and a
  refused cancel is excused only for an order that is no longer resting *and* left without
  executing (#245; before it the flatten returned
  `orders-still-working ... NO-STOP-RESTORED` on every bracket position and an orphaned stop leg
  could survive a stop). Cancelling one leg cancels the other at the broker, so the second cancel
  of a pair is refused for an order the broker has already removed - that is the OCO, not a
  dropped cancel.
- The `status=all` listing is newest-500-rows at a time, and a mature account fills that page
  with terminal orders, so a full page pages back through the history (bounded at 20 pages)
  instead of failing: an account trading one bracket a day reaches 500 orders within two
  years, and a flatten that failed on its own order book would leave the position open. A 20th
  full page fails too: exactly 10,000 orders cannot be told apart from a longer history. The page cursor goes past the oldest row it saw by a
  microsecond, so no order at the boundary is skipped. (`status=open` still refuses a full
  page: 500 *working* orders means orders the sweep cannot see.)
- The orphan sweep's leg lookup is account-wide, bounded by the same 20-page budget: a bracket's
  take profit names the symbol a `held` stop leg rests on, but the OTO entry this platform
  submits for an unbounded take profit has a stop leg and no take-profit sibling to derive the
  symbol from.
- A stop that filled while its leg was being cancelled ends the flatten with the position already
  flat, and restoring a stop then would create the orphan these sweeps exist to remove, so the
  restore re-reads the position first and skips itself on a flat one. The same read sizes and
  sides the stop it restores: the leg that fills on its way out leaves a smaller position than
  the flatten started from, and a stop sized from the old quantity oversells it. A position the
  broker will not report is not sized either - the restore refuses and the sweep reports the
  naked position, rather than placing an order for a quantity nobody read.
- The orphan sweep re-reads the working book after its cancels, as the flatten does, and reports
  anything it cancelled that is still resting as a failure so the sweep is incomplete: Alpaca
  keeps a cancel it accepted listed as `pending_cancel` until it settles, and a leg that is
  still listed can still fill. A verification it cannot make is a failure too. (An accepted
  cancel that left the leg resting is *not* reported as `pending_cancel` here, unlike the
  entry-cancel sweep: the daemon gates the STOPPED stamp on this sweep being `complete`, and
  `pending_cancel` does not make an outcome incomplete, so it would be read as a clean sweep.)
- That verification is scoped to the symbols the sweep cancelled, and the scope is those
  symbols however many cancels it took to clear them - both legs of one orphaned bracket, or two
  brackets on one symbol, do not widen it, because the daemon runs this sweep every sync tick
  and retries while it is incomplete, so two 20-page nested scans per pass is what turns a long
  order history into a rate limit. Only an id whose symbol could not be resolved falls back to
  account-wide, because that id names no bound.
- The flatten places a held leg by the parent it rests under, in both its cancel list and its
  cancel verification: a leg that arrives without a symbol of its own is still on the symbol
  being flattened. Read as "another symbol" it survives the cancel, its stop keeps reserving the
  position's quantity, and the close is then refused for that reservation - a flatten that cannot
  finish. A leg whose parent names another symbol is still left out.
- A stop is not restored onto a working book nobody could re-read. That re-read is what proves
  no stop still works, and it is also the part that can fail now that the held-leg lookup is
  paged; restoring anyway places a second stop beside whatever the broker kept, and two stops
  reduce a position twice - the extra one an orphan the moment the first fills. The position is
  then unprotected until the next enforcement retry, which the sweep reports.
- The excusal is settled by one read of the order by id, not by re-listing the working book: a
  terminal state already proves the order is not resting, and the book is the page-limited
  nested history - up to 20 requests of 500 rows - so a sweep with several refused cancels on a
  mature account is what turns into a rate limit, and a rate limit fails the sweep closed. A
  cancelled order that partly filled does not excuse its refusal (it executed), and neither
  does one whose filled quantity cannot be read. Alpaca has no `done` order status; its
  end-of-day state is `done_for_day`, which resumes the next session and is not an excusal.
- Paper check of #245 (2026-11-20, `ACC1`, endpoint host read as
  `https://paper-api.alpaca.markets`, 1 share of `F` as a resting limit bracket, nothing left
  behind after the run). The filled-bracket shape could **not** be produced in that session: the
  equity market was closed until the next Monday 09:30 ET, and the broker refuses both `bracket
  orders do not support extended hours trading` (40310000) and crypto brackets (`crypto orders
  not allowed for advanced order_class: otoco`). Verified live instead: the `status=all,
  nested=true` request (scoped by `symbols`, and account-wide) is accepted and parsed against real
  alpaca-py objects, a `held` leg under a *not yet filled* parent is correctly not treated as a
  protection of a position, the orphan sweep cancelled a resting bracket parent on a flat symbol
  and left nothing behind, and the OCO behaviour the excusal is built on: cancelling the
  take-profit leg takes the stop leg with it, and both then read back by id as `canceled`. The
  run left no working order and no position behind. The flatten and the stop restore on the
  recorded (filled-parent) shape are pinned by `tests/test_alpaca_adapter.py`, which reproduces
  it. Re-run the sweep check at the next regular session to confirm on the live shape
  (tracked in #322).
- `PATCH /v2/orders/{id}` on the `held` stop leg is accepted: the response is a new order id,
  `held`, with the new `stop_price`; the parent's `legs` then show the new stop and the unchanged
  take-profit leg. Through the adapter the stop moved 11.35 -> 11.52 with the take profit intact.

## Internal strategy live runner (#348)

ACTIVE deployments with `signal_source`/`signal_source_type` = `internal` are
evaluated by `live_runner/internal_strategy_runner.py` (daemon task
`internal-strategy-loop`, 15 s tick). Discord deployments are unaffected —
their signals still arrive through the channel listeners.

**Opt-in.** An internal deployment is only evaluated when
`settings["internal_runner_enabled"]` is explicitly truthy (`true`, `1`,
`yes`, `on`). Deployments that predate the runner stay inert until an
operator sets the flag per deployment — there is no way to distinguish a
pre-existing ACTIVE row from a new one, so the flag gates all of them.

**Risk policy (maintainer decision 2026-11-22).**

- An entry with **no take-profit level** is allowed: it is stamped
  `take_profit_policy=unbounded_exit_on_opposite_signal`, so the strategy's
  own `exits` signals are its exit mechanism — matching the backtest. A stop
  is still required.
- An entry with **no stop loss** gets a broker stop at the engine's fallback
  distance (`fallback_stop_pct` of the entry price — the same fallback the
  engine uses for sizing). Live never runs unprotected.
- Entries per symbol are capped by `settings["internal_max_open_per_symbol"]`
  (falling back to a same-named `STRATEGY_META` parameter): **default 1 open
  position per deployment+symbol**. Only `1` is accepted today — N>1 is not
  supported end-to-end (per-position sizing, protection and exits on the
  qty-netted live ledger are unresolved; maintainer decision 2026-11-22:
  N=1, N>1 is #382), so any other value fails closed: **entries are disabled
  for the deployment but exits keep flowing** — a bad cap must never
  suppress an exit on an open position. Open positions are counted from the
  deployment's own persisted ledger with oldest-first FIFO attribution of
  reducing fills; an entry with no fills counts only while the broker still
  lists its order as working or holds a non-zero position in the symbol
  (a filled-but-unbooked entry is not working, so the position is what
  proves it open), so a canceled or expired unfilled order does not lock
  the symbol. The count is updated within a pass so same-pass signals
  cannot exceed the cap, and the deployment total is capped at the engine
  maximum of 5. If the ledger, the broker's working-order list or its
  positions cannot be read, entries fail closed the same way — exits still
  evaluate.
  A same-bar reversal (exit + new entry on one bar) emits the exit but the
  entry waits one bar: the closing position still counts against the cap
  until the next evaluation.
  **Known window:** an entry that reached `adapter.submit_order` but whose
  `broker_order_id` was never persisted (a crash between the broker accept
  and the write, or a lost submit response) carries only its signal id — no
  broker order id and no fills yet. While the broker's position in the
  symbol is flat *and* the order is no longer working, that row counts as 0
  open entries, so a same-symbol entry could be submitted in that window.
  The window closes as soon as either the fill is booked or the broker
  position goes non-zero; there is no persisted "submitted" marker on the
  signal row to close it earlier (#360 review).
- **Session-bound symbols** (anything the venue calendar does not resolve to
  24/7) are only *actioned* while the venue is open, using
  `shared.venue_calendar`. Daily-and-longer bars may be held over the market
  close; a bar whose data runs into the session close becomes actionable at
  the next session open, matching the engine's next-bar-open fill. A bar
  counts when it *overlaps* a trading interval, not when it opens inside
  one — a 09:00 ET hourly bar is regular-hours data for its 09:30-10:00
  portion. Session logic gates actionability, never the indicator input:
  intraday strategies see every nominally closed feed bar, including
  extended-hours bars, matching the unfiltered backtest dataset. Bars with
  no session overlap produce no signals, and an unresolvable venue calendar
  fails closed.

**What runs.** For each deployment symbol the runner fetches bars through the
adapter's existing `get_market_data` (no new network client; an adapter that
cannot serve bars for the timeframe fails closed and the deployment is
skipped), keeps only *closed* bars, and calls the deployment strategy's own
`generate_signals(df, params)` through the native engine's
`_call_strategy`/`_normalize_strategy_output`/`_resolve_direction`/
`_extract_levels` — the same code path a backtest uses, with
`STRATEGY_META["parameters"]` defaults (per-deployment
`settings.strategy_parameters` stays refused, exactly as the promotion gate
binds it). Up to 1000 bars of history are requested per evaluation, which
bounds indicator warmup; a strategy needing deeper history than the feed
serves cannot be evaluated faithfully.

**Timing contract.** The native engine evaluates a signal on bar T's close,
records T's `close` as the entry reference, and fills the resulting market
order at the *next* bar's open; signal exits fill at the trigger bar's close.
Live maps this to: evaluate on the just-closed bar T and submit immediately —
the broker fill lands at (or very near) the open of bar T+1 for entries, and
at approximately T's close for exits. Sizing geometry uses T's `close`, the
same reference the engine records. A signal is only actionable while bar T+1
is still forming (30 s grace plus a feed-lag allowance of
`min(120 s, 10% of the bar)`, measured from the bar's session-aware close —
never a full extra bar); an older bar is stale and is
never traded. Bars that closed while the daemon was down are stale by
definition: only the newest closed bar is actionable after a restart, and
missed bars are never backfilled with orders. Missing, malformed, unclosed or
out-of-window bars fail closed — no order is emitted.

**Orders.** Emitted intents go through the daemon's
`_submit_internal_strategy_signal` → `_trade_signal_to_live_signal` →
`_prepare_claim_and_submit_signal`, the same prepare/claim/submit path as
Discord signals: live level resolution, risk sizing, the SL/TP guard, the
promotion gate, the demo/paper hard block, the submission lease and adapter
persistence all apply unchanged. A strategy `exits` row becomes a
reduce-only `FULL_EXIT` against the deployment's own ledger quantity — it can
never open or extend a position. A `SHORT` entry on a deployment without
`allow_short` is skipped (it must not be reinterpreted as an exit). Entries
carrying more than one take-profit level — or a single partial (`tp_pct` /
`fraction < 1`) level — are skipped: the live bracket carries a single
full-size TP and emitting only the first would silently drop the native
partial-exit ladder. Entries carrying engine sizing-only stop fields
(`sizing_sl_price`/`sizing_stop`/`sizing_sl`/`stop_distance`) are likewise
skipped — the live path cannot reproduce the engine's sizing math. A
`signal_processor` configuration is refused outright: processors are a
Discord-pipeline concept the internal path never runs. An entry with no TP
level is emitted with the unbounded-TP policy above; an entry with no stop
gets the fallback-distance broker stop, so the daemon's SL/TP guard always
sees a real stop. A *stale* exit is skipped like a stale entry — with the
unbounded-TP policy the position is then held until the fallback stop or
the next exit signal.

**Idempotency.** Each emitted intent carries a producer-stable
`metadata.idempotency_key` derived from deployment, symbol, timeframe, bar
close and intent kind. Within a process the runner tracks the newest
processed bar per deployment+symbol; across a restart the same closed bar
re-evaluates to the same key, and the adapter's `save_live_signal` dedupe
returns the persisted row instead of placing a second broker order.

## Periodic Broker Reconciliation (#303)

The daemon runs `live_runner/broker_reconciliation.py` on the deployment sync
snapshot. For every account with an ACTIVE or PAUSED deployment it compares:

- the signed ledger quantity per symbol (`live_executions` fills by deployment)
  with the broker's net position for that symbol;
- broker order ids/client order ids the ledger still expects to be working with
  the adapter's normalized working-order view (bracket legs and attached
  stop/target orders are marked as protection, not untracked entries/exits);
- protective stops with open broker positions when the adapter can list them.

Unreadable broker state is a mismatch (fail closed), but it must not flap: a
deployment's entries are halted only after
`broker_reconciliation_consecutive_mismatches` consecutive bad passes
(default `2`), and a clean pass resets the streak *and* releases the halt.

The halt is **entry-only**. It is a persisted flag on the deployment (the
`broker_reconciliation_entries_halted` settings key), re-checked atomically
inside the submission claim immediately before an entry order is submitted -
the deployment stays
`ACTIVE`, so exits, in-flight orders, trailing stops and STOPPED enforcement
keep their normal semantics. A `PAUSED` status was rejected for this on
purpose: it would also freeze exits and trailing, exactly when a broker/ledger
divergence makes them most needed. The halt and its release are written to the
live control audit trail as `broker_reconciliation_entries_halt` /
`broker_reconciliation_entries_resume` (actor `broker_reconciliation`).

To release a halt early, set the deployment `PAUSED` and back to `ACTIVE` (the
resume transition clears the flag); a still-mismatched account is re-halted on
the next pass, so this cannot hide a live divergence. To make the halt
permanent, leave the flag - or `PAUSED`/`STOPPED` the deployment.

Only signals younger than 7 days and belonging to ACTIVE/PAUSED deployments
count as orders the ledger still expects to be working: both live order paths
expire fast (Alpaca `DAY`, cTrader `IMMEDIATE_OR_CANCEL`), so an older
unfilled row is a cancelled/expired/rejected historical order, not a working
one. A younger order missing from the broker's working list is not trusted on
the age bound alone: the reconciler asks the adapter for that order's state
(`get_order_state`, bounded lookups per pass), and only a proven `terminal`
state clears the finding - IOC remainders, cancelled orders and rejected
submissions all classify terminal, while `working`, unknown, timed-out or
unreadable states keep it. Accounts holding positions or orders placed
*outside* the platform
(manual trades, another tool) will always mismatch - the ledger cannot vouch
for them; flatten or adopt them at the broker before relying on a clean
report.

Settings live in deployment `settings` (see `live_deployments.example.yaml`).
Broker state is account-level, so the strictest values across an account's
deployments win:

- `broker_reconciliation_interval_seconds` (default `300`, bounds 30-86400)
- `broker_reconciliation_qty_tolerance` (`null` = adapter precision, strict
  default when unknown)
- `broker_reconciliation_consecutive_mismatches` (default `2`, bounds 1-10)
- `broker_reconciliation_enabled` (default `true`; the account reconciles unless
  every deployment on it opts out)

All four are validated at deployment creation: an unusable value is refused
instead of being silently defaulted. At runtime an unrecognised boolean still
fails closed to *enabled* (a typo like `"flase"` cannot disable
reconciliation).

The latest pass is written to
`$DATA_DIR/live/broker_reconciliation_report.json` and served read-only at
`GET /api/live/reconciliation`. Every mismatch logs the structured diff; the
halt threshold logs at ERROR with `alert=broker_reconciliation_mismatch`.

### Open decision

Interval, tolerance and the consecutive-mismatch count are maintainer decisions.
The shipped defaults are deliberately conservative (5-minute interval, adapter
precision or the strict 1e-9 quantity tolerance, two consecutive mismatches)
because a false halt is recoverable while a missed broker/ledger divergence is
not. Whether account-level mismatches should halt entries on *all* ACTIVE
deployments on the account (current conservative choice) or only the
deployments whose ledger rows implicate the symbol is also open; the
account-wide halt is kept because broker positions and working orders are
netted per account and cannot be attributed to one deployment safely.

## Strategy Promotion Gate (#165)

Only code a named human reviewer promoted to `production` may trade on a
**real-money** account. Paper/demo accounts run `experimental` code without
promotion. There are only these two stages.

### Relation to the demo-only block

The platform already hard-blocks real money: the daemon refuses any broker
adapter that is not provably demo/paper (`_adapter_is_demo`, "refusing
continued use"), and the viewer refuses to activate a deployment unless the
broker reports a demo account. **That block stays.** The promotion gate is an
additional layer under it, not a replacement. Today a promoted strategy on a
real-money account therefore still cannot trade: preflight passes the gate,
but the daemon will not keep a non-demo adapter. The viewer's activate path
re-verifies the account against broker discovery and will not arm the
deployment. The cTrader top-N deploy creates its deployments directly, without
that re-verification, so it never arms a non-demo account either (#261): the
rows are created `PAUSED` and a maintainer activates them by hand, which is the
path that re-verifies - and, like every path, only arms an account the broker
reports as demo. Lifting the demo-only block is a separate, deliberate step.

### Required evidence and thresholds

| Evidence | Requirement | Default |
|---|---|---|
| Out of sample | Optimization result (#164) with `three_way.overfitting.passed == true`; untouched test segment length and trade count | ≥ 180 days, ≥ 50 trades |
| Paper trading | Paper/demo run duration and trade count, with a report file | ≥ 60 days, ≥ 30 trades |
| Parity | Cross-engine or reference parity: relative deviation of every compared metric | ≤ 2 % |

No max-drawdown bound. Defaults live in
`runner.promotion_gate.PromotionThresholds`; override them with a
`promotion_thresholds.json` at the repository root (keys
`min_test_segment_days`, `min_test_trade_count`, `min_paper_trading_days`,
`min_paper_trade_count`, `max_parity_deviation`). An unknown key or a malformed
file makes nothing `production`. The thresholds in force are applied when a
promotion is recorded **and** every time it is checked, so raising a
threshold demotes promotions that no longer meet it.

For a strategy, the out-of-sample result must be for the same strategy file
name, and its `effective_best_parameters` must equal the bound parameter set.
A signal bot's processor is not what the optimizer runs, so for a bot only the
verdict, window and trade count are checked; name the result that backs the
bot's logic.

### What a promotion is bound to

A promotion is bound to the SHA256 of one file **and** the SHA256 of a
canonical parameter set (JSON, sorted keys, `10` and `10.0` equal). Change one
byte or one parameter and the code is `experimental` again.

| Deployment | Hashed file | Bound parameters |
|---|---|---|
| Internal strategy (no `signal_processor`) | `strategies/<strategy_name>/<strategy_name>.py` | `STRATEGY_META["parameters"]` defaults |
| Signal bot (`signal_config.signal_processor` set) | `signal_bots/<bot>/processor.py` | processor id, `signal_config.signal_processor_config`, SHA256 of the bot's `config.yaml`, SHA256 of every repository module the processor imports |

A signal bot's import closure is the set of repository modules Python executes to
load its processor: the modules it imports, the packages those modules live in
(their `__init__.py` runs on import, and `signal_bots/__init__.py` imports every
bot), transitively. Standard-library and third-party modules are not bound. A
change to any bound module makes that bot `experimental` again, including a
change to another bot's processor or to a shared model. Imports that are not
static (`importlib`, `__import__`) are not visible to the gate and are a
reviewer responsibility.

No live code path applies per-deployment strategy parameters, so a strategy is
promoted with its `STRATEGY_META` defaults: write optimised parameters into the
file, then promote. A deployment that carries `settings.strategy_parameters` is
refused on real money. Other `settings` keys (risk limits, selection metadata)
are not strategy parameters and are not bound. A non-internal (Discord)
deployment without a `signal_processor` trades the raw channel signal, so no
strategy file runs and nothing can be hashed: it is refused on real money even
if a strategy of the same name is promoted. Only internal deployments are
checked against a strategy file.

The gate sits in the daemon, the preflight and the viewer. The operator tools
`live_runner/test_signal_trigger.py --live` and `scripts/live_smoke.py` do not
go through it: they are explicit, human-driven commands and bypass the gate.

A signal bot's binding covers every repository module its processor imports, which includes
the adapters, `risk/` and `shared/`. Any change to those files invalidates every signal-bot
promotion, so re-promote after such a change before trading real money. A module that resolves
outside the repository (e.g. a symlink) is refused.

The ledger is `<file>.promoted.json` next to the hashed file (for example
`strategies/foo/foo.py.promoted.json`) and must be committed: the promotion is
only in force where the ledger is. It follows `runner/approval_gate.py` and
reuses its hashing and atomic-write helpers. A missing, unreadable or malformed
ledger, a missing strategy file or unreadable `STRATEGY_META`, an unknown
processor: all mean `experimental`, and the deployment is refused on real money.

### The evidence files are re-hashed on every check (#260)

A promotion also records the **path and SHA256 of each of the three evidence
files**: the optimization result, the paper-trading report and the parity
report. They are re-opened and re-hashed every time the promotion is checked,
so a result or a report edited after the promotion -- or deleted -- makes the
code `experimental` again, exactly like a changed strategy file. Without that,
the verdict could stand on a file nobody had looked at since the promotion.

- An evidence file inside the repository is recorded as a repository-relative
  POSIX path, so the ledger stays portable across checkouts.
- An evidence file outside the repository (a data directory, a machine-local
  report) is recorded absolutely, and only verifies where that file still
  exists: a promotion is then in force only on the machine that recorded it. That
  is the safest reading available -- the alternative would be a path nothing can
  resolve elsewhere, recorded as if it could.
- The optimizer writes its result to `DATA_DIR/optimizations` (git-ignored, so
  `<repo>/data/optimizations` while `DATA_DIR` is unset), so a machine that trades
  real money needs that data directory: without the recorded files the promotion
  is refused there rather than waved through. `runner/portfolio_optimizer.py`
  still writes its own hardcoded `<repo>/data/optimizations` and ignores a
  configured `DATA_DIR`, so check where the tool that produced the result put it
  -- the recorded path is the one every check re-hashes.
- Ledgers written before #260 record the file *names* only. Such a record cannot
  be re-verified, so it is refused until the promotion is recorded again with the
  current CLI (one command, the same evidence): the new record replaces the old
  one for the same parameter set, and records of other parameter sets are kept.
  A promotion recorded this way is what the ledger holds from then on.
- Re-recording needs the evidence files themselves, so an evidence file that was
  cleaned up (an old `DATA_DIR/optimizations` run, a scratch paper report) has no
  recovery shortcut: re-run what produced it -- the optimization for the
  out-of-sample part, the paper run for the paper report, the parity harness for
  the parity report -- and record the promotion again with the new files. The
  command exits non-zero naming the missing file, and writes nothing, so a
  half-re-recorded promotion is not possible.
- Before deploying this change, look for `*.promoted.json` ledgers (they sit next
  to the promoted file) and re-record every one of them: each pre-#260 ledger
  drops its deployment to `experimental` on the first check after the upgrade and
  the daemon then refuses its entry orders until it is re-recorded. Check the
  evidence files still exist first; the runbook of an upgrade that must not stop
  trading is to re-record and only then restart the daemon.

### Recording a promotion (CLI only)

```bash
uv run python -m runner.cli promote \
    --strategy example_sma \
    --promoted-by "Jane Doe" \
    --optimization-result data/optimizations/opt-123.json \
    --paper-account-id 10105152 --paper-broker ctrader \
    --paper-start 2026-08-01 --paper-end 2026-10-15 --paper-trades 42 \
    --paper-report docs/reports/paper_example_sma.csv \
    --parity-reference lean \
    --parity-deviation total_return=0.012 --parity-deviation sharpe_ratio=0.008 \
    --parity-report docs/reports/parity_example_sma.json
```

The paths are the ones this repository uses: `data/optimizations` is
`DATA_DIR/optimizations` with `DATA_DIR` unset (the default) -- pass the path the
optimizer actually wrote wherever `DATA_DIR` points.

An evidence file no other checkout can see makes a promotion that is not
portable: the ledger has to be committed and its evidence files have to exist
where the ledger is in force. This repository ignores every `*.csv`, so the
`--paper-report` above is git-ignored and a promotion recorded with it would be
refused on the next clone. Such a report belongs outside the repository (recorded
absolutely, in force on that machine only) or as a copy that git tracks.

For a signal bot use `--signal-processor ramjet` and pass the deployment's
`signal_processor_config` as `--params '{...}'` (`--params` is refused for a
strategy). The out-of-sample evidence is read from the result file, not typed
in. All three files must exist; their path and SHA256 are recorded and re-hashed
on every check. Every argument is mandatory, and the command exits non-zero
without writing anything if any evidence is missing, below a threshold, or
cannot be named for the ledger.

Re-record a promotion that was made before #260 (same command, same evidence)
to bind the evidence files; see "The evidence files are re-hashed on every
check" above.

### Enforcement points (fail closed)

All of them call the one function `live_runner.promotion.enforce_promotion_gate`
with the deployment and their own real-money verdict, taken from the same
source the demo-only block uses there. Unknown means real money.

| Point | Real-money verdict |
|---|---|
| `live_runner.preflight` (every configured deployment) | the paper/demo mode preflight resolved from the credentials |
| Daemon, `prepare_order_with_risk` (every entry order, internal and Discord) | `_adapter_is_demo(adapter)` |
| Viewer `POST /api/live/deployments` (created `ACTIVE`) | broker discovery `is_demo` (asserted) |
| Viewer `PUT /api/live/deployments/{id}/status` → `ACTIVE` | the fresh broker re-verification |
| Viewer `POST /api/live/deployments/ctrader/top5`, dry run included | broker discovery `is_demo` (asserted) |

The viewer answers HTTP 409 with the reason. Exit-only orders are never blocked:
closing a position must not depend on a promotion. The daemon check runs on
every entry, so a deployment edited after activation is caught at order time.



### The cTrader top-N deploy never arms a non-demo account (#261)

That endpoint creates its rows in one shot, with no per-deployment
re-verification of the account; `activate` therefore used to be the only route
that put an armed deployment on an account the broker does not report as demo.
With `activate` on a broker-reported demo account the behaviour is unchanged:
the rows are created `ACTIVE`. On any other account - a reported live account, or
one the broker could not assert as demo, which fails closed - the rows are
created `PAUSED` instead, and the response's `paused_as_real_money` plus a
`WARNING` in the viewer log name each one and say why. The owner activates them
by hand, through `PUT /api/live/deployments/{id}/status`, which re-verifies the
account against the broker and still refuses a non-demo one. The dry run reports
the same: a preview never shows `ACTIVE` rows the deploy would not get.

## Broker Position Quantity (#263)

Every entry order reads the broker's open positions twice: as a count for
`max_positions` and as the risk-at-stake estimate for `max_total_exposure`
(`live_runner.daemon._broker_exposure_estimate`). A position row the platform
cannot read a quantity from — a value that is not a number, `nan`/`inf`, a
boolean, a missing or `None` `qty`, or Alpaca's `qty_unparseable` marker — used
to be counted as zero, which reads as "the account is flat". It now fails
closed, because sizing an entry against an unknown book could open a position
on top of a cap that is already full.

- Refusal: `UnreadableBrokerPositionQtyError` from `prepare_order_with_risk`, so
  the entry takes the same skip-and-log path every other entry refusal does and
  nothing reaches the broker. Exit-only orders are unaffected: they size
  against the deployment's own ledger, not the count.
- Alert: ERROR `ALERT: broker position quantity is unreadable; ...`
  (`alert=broker_position_qty_unreadable`, with account, deployment, signal
  symbol, the row's symbol, the raw quantity and its row index) and a Sentry
  event. There is no automatic retry: each new signal of that deployment refuses
  the same way while the broker reports an unreadable row.
- Operator action: read the position on the broker, reconcile the ledgers if
  the quantity is real, and pause the deployment if the broker keeps reporting
  a row without a usable quantity.

## Provisioning and Rotation

Provision the deployment file from `live_deployments.example.yaml` into the
ignored `live_deployments.yaml`. Replace every environment placeholder with a
value obtained from the broker or Discord administration console; do not copy
account references, channel IDs, or author IDs into the example or any tracked
file.

Keep secrets and account references in the ignored `.accounts.env` and local
deployment file. When a broker account, API key, OAuth token, Discord channel,
or trusted author changes, provision the replacement through the provider,
update the local files, run the non-ordering preflight, and revoke the old
credential or access grant. Keep deployments paused until paper verification is
complete. Account references are routing metadata rather than secrets, but they
must still be rotated when an account is retired or reassigned.

## Non-Ordering Preflight

```bash
uv sync --all-extras
uv run python -m live_runner.preflight --config live_deployments.yaml --accounts-env .accounts.env
```

The test suite never reads this file: `tests/conftest.py` points every default at a sandbox path and
fails the run if anything opens the real one, so a local run and CI resolve the same credentials
(none). A test that needs credentials writes its own under `tmp_path`.

The daemon trades the ACTIVE rows of the `live_deployments` table, not the YAML. Add `--check-db`
(read-only; `--db-path` overrides `DATABASE_URL` / `DATA_DIR/backtests.db` and implies
`--check-db`) to validate those rows too. The JSON result gains `db_deployments` with `matched`, `only_in_db`, `only_in_yaml`,
`active_count`, `errors` and `warnings`. It fails the preflight (`ok: false`, exit 1) for:

- an ACTIVE DB deployment absent from the YAML (the daemon trades something the YAML preflight
  never saw);
- an ACTIVE row whose `account_id` is not in `live_accounts`, or whose symbols or timeframe are
  empty;
- an ACTIVE row with a stored symbol that is padded, blank or not a string (`" AAPL"`, `null`): the
  daemon's Discord signal match compares a stored symbol verbatim, so that symbol never matches;
- an ACTIVE row whose stored `signal_source` is neither `internal` nor `discord` (case aside, and
  not blank): the daemon compares it verbatim, so the row does not route as configured;
- two ACTIVE rows for the same `(strategy_name, account_id)` — the daemon dispatches every ACTIVE
  match, so the same strategy would submit a second order per signal;
- a `DATABASE_URL` that points at an in-memory database, which no read-only check can inspect;
- a missing or unreadable database.

Warnings (the preflight still exits 0): a YAML deployment with no ACTIVE DB row (the daemon will
not trade it), a YAML deployment listed twice, and any field where the DB value and the YAML
disagree — `symbols`, `timeframe`, `signal_source` (the flag that routes a deployment's Discord
signals) and `signal_source_id` (which credential it is dispatched with). Symbols and timeframes are
normalized on both sides before they are compared, so a case or alias difference (`aapl`, `1H`) is
not reported as drift. Whitespace around a stored symbol is not normalized away: the row is an error
above, because the daemon does not match it. The DB value is what runs, so a warning means the daemon is about to trade
something other than what the YAML says.

The read-only open cannot run WAL recovery. Against a WAL database left needing recovery (after an
unclean stop) in a directory this process cannot write, the check fails with a message that says
so; a plain open by any writer checkpoints it.

```bash
uv run python -m live_runner.preflight --config live_deployments.yaml --accounts-env .accounts.env --check-db
```

```bash
# Also connect to every broker account and read balance/positions (still no orders):
uv run python -m live_runner.preflight --config live_deployments.yaml --accounts-env .accounts.env --check-connectivity
uv run python scripts/live_smoke.py --mode discord-replay --reports-dir docs/reports
```

The preflight prints exactly one JSON document on stdout (parseable with `jq`); its log lines go to stderr.

`${VAR}` references in the deployment file expand here with the precedence every
other live_runner component resolves configuration with: a key defined in
`--accounts-env` wins - and a key present there but empty stays empty, so
blanking it disables it - while the process environment fills only keys the file
does not define. A reference that resolves nowhere fails the preflight and names
the variable, so a literal `${...}` never reaches the daemon. Credentials
themselves resolve from the file only, so a credential that exists only in the
process environment is reported as missing rather than borrowed; aligning that
with the daemon's env-filled credential resolution is an open decision, not a
bug.

An accounts env file that exists but cannot be read logs a warning and fails the
preflight with missing credentials. A config that fails to parse or validate is
reported, not crashed on: `deployment_errors` names the error type and the
offending field, and never an expanded value - that value can be a credential,
and `deployment_errors` is printed as JSON. This holds for every exception raised
while parsing, not only validation errors. A YAML syntax error reports its
problem and its line and column, never the source line, which can hold a literal
secret. Redaction covers the values of every accounts env file key and every
process-env key the config references through `${VAR}` or `$VAR`, so a secret
that only lives in the process environment is reported as `${VAR}` too. Process-env
keys the config never names are not redacted. The cTrader `CTRADER_DEMO` error,
which quotes the rejected value, is redacted the same way: every accounts env file
value is redacted there. A missing or unreadable config file is the one case that
still crashes: the JSON document names the path, and the traceback goes to stderr
and the log at ERROR. That traceback, like the JSON document, is redacted with
every accounts env file value and every process-env value, because the config is
not re-read after a crash and its references are unknown. Only the exception type
remains in the JSON if the accounts env file cannot be resolved at all, and then
no traceback is printed.

## Paper Broker Smoke

For Alpaca smoke, first verify every Alpaca account resolves with `paper=True`.
For cTrader smoke, verify the configured cTrader credentials are demo credentials.

```bash
uv run python scripts/live_smoke.py --mode alpaca --accounts-env .accounts.env --reports-dir docs/reports
uv run python scripts/live_smoke.py --mode ctrader --accounts-env .accounts.env --reports-dir docs/reports
```

Both cTrader smoke order paths - the order worker (`--mode ctrader-order-worker`) and the
strategy-e2e worker (`--mode ctrader-e2e-worker`) - anchor on a live price (the close of the latest
M1 trendbar) with a real stop-loss and take-profit 0.5 % away. Each fails with a clear message when
the market is closed (no M1 bar in the last 30 minutes) rather than building an order on an
invented price.

The cTrader smoke registers the demo account in `live_accounts` if it is not there yet (a fresh
database), because its parent deployment row has a foreign key to it. An account that already
exists is left untouched.

The smoke places its orders through `submit_order_with_risk`, so it is refused like any other entry
while its account is halted (see "Kill Switch" below). That is deliberate: a smoke is still an order.

## Demo Daemon

Only run after the paper smoke succeeds.

```bash
set -a
. ./.accounts.env
set +a
set -o pipefail
uv run python -m live_runner.daemon 2>&1 | tee "docs/reports/live_daemon_$(date -u +%Y%m%dT%H%M%SZ).log"
```

The daemon takes no options: deployments come from the database (`live_deployments` via
`shared/database.list_live_deployments`), not from `live_deployments.yaml`, and there is no
`--config`. An unknown option is a usage error (exit 2), never a silently ignored argument.

`python -m live_runner.daemon --help` is safe to run anywhere, at any time: it prints the usage
and exits 0 without opening the database, running migrations, or connecting to a broker. It used
to start the daemon instead (#310).

**One daemon per database.** Before the first migration or broker connect, the daemon takes an
exclusive OS lock in a file next to the live database (`DATA_DIR/backtests.db.lock`, or the
`DATABASE_URL` file's sibling when one is set). A second start is refused with a clear error and
exit code 3, and the running daemon keeps its lock. The lock is owned by the OS, so a daemon that
was killed (even `kill -9`) releases it on death and never blocks the next start. The data
directory must be local: `flock` semantics vary on NFS/SMB mounts, so do not put `DATA_DIR` - or
the directory of a SQLite `DATABASE_URL` file - on a network filesystem. On an in-process stop (Ctrl-C, or a cancelled coroutine) the daemon first
disconnects its broker adapters and then waits - bounded, `WORKER_JOIN_TIMEOUT_SECONDS` = 30s - for
its in-flight database work, so the lock is never handed on while its own writes are still
running; if a worker thread is still stuck after the budget the daemon logs CRITICAL and exits 4
with the lock held until the process dies. `systemctl stop` sends SIGTERM, which this build does
not handle: the daemon dies immediately with no drain (graceful SIGTERM shutdown is a separate
change). Deploy this build as stop, deploy, start - the first rollout is **not** protected against
a daemon already running an older, lockless build: stop the old daemon before starting this one.
A systemd unit with `Restart=always` (see `scripts/setup_services.sh`)
therefore retries a refused start every `RestartSec` and logs the refusal each time: that is the
signal to find and stop the other daemon, not to delete the lock file - the file is a handle, the
lock is the kernel's.

**A newer build runs data migrations.** Starting a build whose schema or one-time data migrations
the running build does not have yet applies them on startup (see "Commission sign migration
(cTrader, #205)"). Roll out as stop, deploy, start: never run an old and a new build side by side.

Keep the daemon running long enough to verify Discord connection, paper order submission, fill handling, and `reconcile_executions`.

## Indicator Kernel Warm-up (#378)

The shared indicator hot loops (`shared/indicators/`) run as `numba.njit`
compiled kernels. Importing `shared.indicators` stays cheap — numba is
imported lazily — but the **first call of each kernel in a process pays a
one-time compile of roughly 0.1–0.8 s** (four kernels: the two Supertrend
recursions and the two `custom_swings` ATR loops). For a live signal bot
that is the first bar it evaluates; for a backtest it is inside the first
strategy evaluation and is amortised over the run. Compiled kernels are
cached in the `__pycache__` directory next to `shared/indicators/` (or
under `NUMBA_CACHE_DIR` when set), so a warm checkout does not recompile
after the first process. Set `TP_DISABLE_NUMBA=1` to force the pure
Python/NumPy path everywhere (parity is pinned bit-for-bit either way).

## Loss Circuit Breaker (#301)

Every live account and every deployment carries a persisted loss breaker
(`live_loss_breaker_state`, one row per `account:<id>` and `deployment:<id>`
scope). The account scope measures the broker account's equity — for cTrader
that is balance **plus unrealized P&L** of open positions, so an open position
under water counts before it is closed. The deployment scope cannot read a
"deployment equity" from the broker (positions on a netted account belong to
the account), so it observes a synthetic equity: account equity minus every
OTHER deployment's trading P&L, reconstructed from the persisted fill ledger
and marked to market. The scope therefore moves only with its own
deployment's realized+unrealized P&L, on the account-equity scale:

- **Daily loss** — equity down the configured fraction (default 5 %) of the
  start-of-day equity. The baseline is the last equity observed before the UTC
  day boundary, so an overnight gap counts as the new day's loss. The breaker
  re-bases and clears automatically at the UTC day boundary.
- **Overall drawdown** — equity down the configured fraction (default 20 %)
  from the peak equity ever observed. The latch only clears on the explicit
  operator reset, which also re-bases the peak to the current equity.

On breach the breaker blocks **new entries only** — exits are never blocked
(PAUSED semantics), and resting entry orders already at the broker are NOT
cancelled; they are the same class of exposure a manual PAUSE leaves. A
breach latches and alerts once per episode through the `data.alert` log path —
until #331 lands that is an ERROR log line (picked up by Sentry), not a
webhook. An unreadable breaker state, unreadable equity, or a deployment P&L
that cannot be measured (untrusted fill price, missing quote/pip value) fails
closed and refuses entries.

Deposits and withdrawals are NOT separated from P&L: a 5 % withdrawal trips
the daily breaker and a 20 % withdrawal latches drawdown. After a planned
withdrawal, run `reset-drawdown` once the new equity is being observed.

Thresholds, most specific first: deployment `settings` keys
(`loss_breaker_enabled`, `loss_breaker_daily_loss_fraction`,
`loss_breaker_max_drawdown_fraction`), then per-account env
(`LOSS_BREAKER_<ACCOUNT>_ENABLED`, `LOSS_BREAKER_<ACCOUNT>_DAILY_LOSS_FRACTION`,
`LOSS_BREAKER_<ACCOUNT>_MAX_DRAWDOWN_FRACTION`), then the global env
(`LOSS_BREAKER_ENABLED`, `LOSS_BREAKER_DAILY_LOSS_FRACTION`,
`LOSS_BREAKER_MAX_DRAWDOWN_FRACTION`), then the defaults above. Env resolves
file-first through `.accounts.env`. Malformed values are refused at deployment
creation and at dispatch.

```bash
# Read every scope's state, latches and current ratios (read-only):
uv run python -m scripts.live_loss_breaker status
uv run python -m scripts.live_loss_breaker status --account alpaca-paper

# The only way to clear a drawdown latch: re-bases the peak to the current
# equity. Resetting an account scope ALSO resets that account's deployment
# scopes (each to its own last observed equity) and prints any scopes still
# latched - the daily latches intentionally survive until the UTC rollover:
uv run python -m scripts.live_loss_breaker reset-drawdown --account alpaca-paper
uv run python -m scripts.live_loss_breaker reset-drawdown --deployment <id>
```

Without `--equity` a scope re-bases to its OWN last observed breaker equity -
the same series the breaker measures (on cTrader that includes unrealized
P&L, which the balance snapshot's equity slot does not carry). An account
scope refuses an observation older than 10 minutes; pass `--equity` to state
the figure explicitly. A deployment scope always re-bases to its own last
observed (P&L-adjusted) equity; `--equity` does not apply to it.

There is deliberately no daily reset command: the daily breaker resets itself
at the UTC boundary.

### When deployment P&L is unmeasurable (`loss_breaker_pnl_unmeasurable`)

Deployment-scoped entries fail closed whenever the fill-ledger P&L cannot be
measured exactly — that is deliberate: an unmeasurable scope is never "no
loss". The account scope keeps observing and the account breaker still
works; only `deployment:<id>` entries are refused. Each episode logs ONE
ERROR with `data.alert = "loss_breaker_pnl_unmeasurable"` and a `reason`:

- `unpriced_fills` — a fill on the account has no trusted price
  (`actual_price_observed` is `sent_bad`/`absent`/`derived_unavailable`, or a
  non-positive `actual_price`). The alert's `untrusted_fills` list names the
  execution ids, deployments, symbols and quantities involved. Repair: fix or
  re-derive the fill's price through the normal reconciliation path (the same
  one that writes `actual_price_observed`), or correct the ledger row by hand
  only with an audit entry. Until then the scope stays refused.
- `ledger_reconciliation` — an unresolved `ledger_reconciliation_required`
  audit row exists for the account (#277/#330 merge-order dependency: the
  ledger may be missing broker-side closes). The alert's
  `unresolved_flags` lists the `account:SYMBOL` targets. Repair: reconcile
  the flagged symbol against the broker by hand, then record the resolution -
  the audit trail is append-only, so resolving writes a
  `ledger_reconciliation_resolved` row rather than deleting the flag:

  ```bash
  uv run python -m scripts.live_loss_breaker resolve-ledger-flag \
      --account alpaca-paper --symbol AAPL --note "reconciled vs statement"
  ```

  A flag counts as unresolved only while it is newer than the latest
  resolution for the same `account:SYMBOL`; a NEW flag after a resolution
  blocks again. Until every flag on the account is resolved, deployment
  scopes stay refused (the account scope keeps working).
- `missing_pip_value` / `unusable_quote` — the broker could not supply a pip
  value or a two-sided quote for a symbol with an open ledger position.
  Usually transient (market closed, symbol halted); if it persists, check the
  symbol's market-data classification and the adapter's quote/pip path.
- `flows_unreadable` — the ledger query itself raised; check the database.

Pip values are cached per account+symbol per UTC day (a pip value is in the
account's currency, so accounts never share one): the ledger can name symbols
the account traded months ago, and re-fetching a pip value for every one of
them each poll would fail on delisted/closed symbols. The day bucket means an
unresolvable symbol fails again the NEXT day (with an alert) - the cache only
skips re-fetching within the same UTC day. Quotes for symbols with an
OPEN ledger position are still fetched fresh every evaluation — a closed
market there fails closed by design.

Ledger limitations the deployment scope inherits: swap/financing charges are
NOT in the fill ledger (they never appear as fills), so a deployment holding
overnight FX or swap-accruing positions has a P&L the breaker cannot see;
treat deployment scopes on such accounts as approximate. A NULL commission is
summed as 0 — a KNOWN APPROXIMATION: Alpaca crypto fees stay NULL until the
CFEE backfill lands (about two days), so a crypto deployment's P&L is
overstated by those fees until then. Failing closed instead would stop crypto
deployments for days after each fill, so this is accepted and documented
here. Pip values convert
through the account currency at the day's cached rate — intraday FX moves are
not tracked.

### ACTIVE deployments nothing can trade (#242)

The daemon's only signal path is Discord. A deployment whose `signal_source` is `internal` (the default)
with status `ACTIVE` has no live strategy runner behind it: it still gets a broker adapter and an execution
listener, so the dashboard and the listener log make it look live, but it will never place an order. The same
holds for any stored `signal_source` that is neither `internal` nor `discord`.

The deployment-sync loop alerts this at ERROR, carrying `alert: "untradeable_active_deployment"` and
`untradeable_reason` in the log `data`, with the id, account and signal source of each affected deployment.
Where Sentry is configured (a DSN: `initialize_sentry` is a no-op without one), its `LoggingIntegration`
sends ERROR as an event, so the alert reaches Sentry too — as a field of the record's `data` (an extra on the
event), not as a Sentry tag, so filter it on `untradeable_reason`/`alert` as event data.

It is not repeated on every tick of the (permanent) sync loop: the first tick of every daemon start reports
each affected row, and a row is reported again only when its status or signal source changes, so restarting
or repointing is what you see again. The daemon does not block or stop: the operator stops the row or moves
it to a Discord signal source.

Both preflights warn for these rows using the same definition
(`live_runner.deployment_policy.untradeable_signal_source_reason`), so a clean preflight no longer hides
them, but they see different things:

- **yaml preflight** (`live_runner/preflight.py`): warns for every ACTIVE non-Discord deployment in the
  `live_deployments.yaml` it validates.
- **`--check-db` preflight** (`live_runner/preflight_db.py`): reads the ACTIVE rows out of SQLite instead.
  It warns for an untradeable row whose YAML twin is *tradeable* (Discord) or absent, because the YAML
  side is then quiet about it; when the twin is itself untradeable the YAML check has already reported
  the pair and a second warning for the same row would only double-count. A stored `signal_source` outside
  `KNOWN_SIGNAL_SOURCES` (`internal` plus everything the daemon can trade) still fails this preflight (exit
  1) rather than warning, because nothing routes the row as configured. Adding a source to the daemon's
  tradeable set does not make it storable: `LiveDeploymentConfig.signal_source` is a Literal over
  `internal`/`discord` and the DB read coerces anything else to `internal`, so a new source is a
  model-and-migration change (`live_runner/deployment_policy.py` says so where the set is defined).

A live strategy runner is a separate decision; until one exists, every `ACTIVE` `internal` row is untradeable by
design, not by accident.

## Kill Switch (#300)

One operator action halts trading. It is written to the `live_kill_switch` table of the live
database, so it survives a restart, a redeploy and the daemon being down altogether: the
daemon reads it again on its first tick after starting.

| Mode | New entries | Working orders | Open positions |
|------|-------------|----------------|----------------|
| `entries` | refused | left alone | left alone |
| `orders` | refused | cancelled | left alone |
| `flatten` | refused | cancelled | closed, then the protections the flatten orphaned are swept |

Two checkpoints, both fail closed:

- **Before every entry.** `prepare_order_with_risk`, the single chokepoint every entry order of
  every deployment (Discord and internal alike) passes through. It runs *after* the exit branch
  there, so a halt never blocks closing a position - trapping an operator in open exposure is
  the opposite of what the feature is for. It also runs before any broker state is read, so a
  halted account costs no round-trip.
- **On every tick.** The deployment-sync loop, so a halt written while nothing was running is
  enforced by the first tick after a restart. A completed sweep is repeated every
  `KILL_SWITCH_SWEEP_INTERVAL_SECONDS` (30 s) while the halt stands, because refusing entries
  stops the daemon opening risk, not a human at the broker.

Fail closed means the cheap failure: a halt state that cannot be read, or a stored mode this
build cannot interpret, refuses **entries** and sweeps **nothing**. An empty read is
indistinguishable from "nothing is halted", which is the one answer a kill switch must never
produce by accident. Nothing is ever liquidated on a database error.

### Scopes

One halt per scope: the reserved `global` scope, plus one scope per live account. The strictest
halt wins, so a per-account write may only add restriction, never lift a platform-wide one. A
scope that names no live account is refused - a typo'd halt would look armed while halting
nothing. `accounts` lists the scopes that exist.

A scope is cleared by stamping `cleared_at` rather than deleting the row, so "who halted this
account, when, and who lifted it" stays answerable after the incident. A standing halt may only
be made stricter: re-writing a halted scope with a weaker mode is refused, because a step-down
from `flatten` to `entries` would stop the sweeps behind a row that still reads as halted. Lift
the halt instead - that is an attributed action, and the sweep ledger then starts clean.

### CLI

```bash
uv run python -m scripts.live_kill_switch status
uv run python -m scripts.live_kill_switch accounts
uv run python -m scripts.live_kill_switch halt --scope global --mode flatten --reason "incident 42"
uv run python -m scripts.live_kill_switch halt --scope ACC1 --mode orders --actor "Jane Doe"
uv run python -m scripts.live_kill_switch resume --scope global
```

`--help` answers without touching the database, and importing the module starts nothing: the
daemon is never started from here. `status` and `accounts` are read-only. The script carries the
import-origin guard the other data-mutating scripts carry, so it must be run as
`python -m scripts.live_kill_switch` from the repository root - a direct `python
scripts/live_kill_switch.py` is refused rather than halting trading against another tree's
database.

### Viewer

| Endpoint | Effect |
|----------|--------|
| `GET /api/live/kill-switch` | the state the daemon enforces, plus every scope's mode, since, by and reason |
| `POST /api/live/kill-switch/halt` | halt one scope; `confirmed_scope` required for `orders` and `flatten` |
| `POST /api/live/kill-switch/resume` | lift the halt on one scope; `confirmed_scope` required |

`entries` is deliberately unconfirmed: it refuses new entries and can cost nothing, so it stays
one click in an incident. The modes that move money, and resuming trading, are confirmed on the
server - the API is directly callable, so a dialog in the UI constrains nothing but the UI.
Every attempt, refused ones included, is attributed in `live_control_audit`
(`GET /api/live/audit`, or `X-Operator` for the claimant).

### What a halt does not do

- It does not disconnect the adapter. A halted account that still needs a sweep keeps its
  adapter connected for the sweep - the same retention STOPPED enforcement uses. Only the
  *deployment execution* listeners are gated on the runnable set, so retention never grants
  trading permission; a Discord connection for an ACTIVE deployment can still be listening, and
  what stops its orders is the entry gate, not a missing listener.
- It does not stop the live trailing-stop loop (#167). A `flatten` sweep cancels what the flatten
  orphaned; an `entries` halt leaves every working order exactly where it is.
- It does not replace STOPPED. A STOPPED deployment is a deployment-level stop enforced once per
  transition; the kill switch is an account- or platform-level halt that stays in force until it
  is lifted.
- It is not a guarantee about the broker. An account whose halt cannot be enforced (no adapter
  can be connected, the broker refuses a cancel, or a cancel is still settling in
  `pending_cancel` - such an order can still fill in a cancel/fill race) is reported as `unmet`
  or `without_adapter` in an ERROR every tick, and its entries are still refused. A halt that
  could not be enforced says so rather than looking clean.
- It does not cover manual order paths that bypass the daemon's entry gate.
  `live_runner/test_signal_trigger.py --live` calls `adapter.submit_order` directly for
  slippage-pipeline testing, so the kill switch does not see it. `scripts/live_smoke.py`
  is not gated either: only some of its checks go through `submit_order_with_risk`,
  while its cTrader order worker calls `adapter.submit_order` directly and its Alpaca
  round-trip check submits `MarketOrderRequest`s straight to the broker client (the
  smoke buy *and* its close). Do not run either `--live` tool against a halted
  account.

### Verifying

```bash
uv run python -m scripts.live_kill_switch status        # what is in force right now
curl -s localhost:5556/api/live/kill-switch | jq .      # same state, from the viewer
curl -s "localhost:5556/api/live/audit?limit=5" | jq .   # who set or lifted what
```

In the daemon log: `Enforced a halted account: orders cancelled and positions flattened` for a
completed sweep, `Halt not fully enforced on this account` for one that fell short, and `Entry
refused by the kill switch` at the entry gate.

## Market-data symbol classification (Alpaca, #122)

`live_runner/symbol_class.py` decides which feed a symbol belongs to, by *asset class* and never by string shape alone: a pair is recognised only when its base currency is in `CRYPTO_BASES` and its quote currency is in `CRYPTO_QUOTES` (that quote list is shared with `shared/alpaca_symbols.py`, so fee backfill and market-data routing cannot drift on it). Share-class equities (`BRK.B`, `BF-B`) are equities, exactly as in the fee backfill.

Anything else is refused, not guessed: `refresh_from_deployments` logs "Refusing to subscribe unclassifiable symbol to market data" with the deployment id and skips it, so that symbol gets no quotes. Fail closed on purpose — a crypto pair routed to the stock feed produces no bars either, but silently. A new coin Alpaca lists has to be added to `CRYPTO_BASES`; an unknown six-letter ticker is a config error, not a fed fallback.

Bars are keyed by canonical pair (`BTC/USD`) in every spelling (`BTCUSD`, `btc-usd`), on the manual/backfill ingest path and on the live `_on_bar` callback alike, so a quote lookup cannot miss bars that arrived in the other form. A bar that still arrives under an unclassified symbol is stored under its own spelling and logged ("Storing market-data bars under an unclassified symbol key") rather than dropped in silence.

## Execution prices and commissions (Alpaca, #124)

- `live_executions.commission` is nullable. `0.0` is a known zero (Alpaca equities); `NULL` means the fee is not known yet. Every reader treats `NULL` as "net PnL incomplete", never as zero: the live stats payload reports `net_pnl: null`, `net_pnl_complete: false` and `commission_unknown_count`, and trade/signal payloads carry `commission: null`.
- Alpaca crypto fees are posted as `CFEE` account activities in batches, so crypto fills are stored with `NULL`. `CFEE.date` is the *posting* date (the New York date of `created_at`), not the trade date, and observed posting delays reach ~51 h, so there is no fixed delay to wait out. The daemon's hourly `commission-backfill` task reads `FILL`, `CFEE` and `FEE` activities over REST for fills up to 30 days back. `CFEE` entries are clustered into posting batches by `created_at` (entries within 15 minutes of each other are one batch); a batch's fee covers the fills since the previous batch's latest `created_at` up to its own. A fill newer than the latest posted batch stays `NULL` and is retried on the next pass - "not posted yet" is never treated as "no fee". Two fee kinds exist: asset fees (on buys) name their pair and a base-asset `qty`; USD fees (on sells) carry only a negative `net_amount` and *no* `symbol`, so they form a per-account, per-batch USD pool that is allocated to a pair only when exactly one crypto pair has sells in the batch window - with several selling pairs the USD part fails closed while pair-attributed asset fees are still priced. Each fee is allocated pro rata by received quantity and converted to USD. Everything it cannot attribute unambiguously stays `NULL` and is logged ("Alpaca commission backfill: leaving ..."); see `live_runner/alpaca_fees.py` for the rules. The first fetched batch fails closed when an earlier batch or earlier fills may exist outside the fetch window, so a posting stall longer than the lookback leaves a batch unpriced rather than risk the wrong attribution. Checking for earlier crypto fills is a full-history `FILL` scan per adapter, bounded by `MAX_ACTIVITY_PAGES` (200) requests against the shared ~200/min Alpaca budget. A found instant and an unfinished scan are cached, but a completed scan that finds *no* crypto fill is deliberately rescanned every pass - a fill posted later can still carry an earlier `transaction_time`, so `None` is not fixed history. That state only persists while crypto executions await a commission that the broker reports no fills for (for example local rows Alpaca does not report - a wrong account or symbol mismatch), which the pass logs as a warning; on a healthy account the scan resolves to a cached instant or UNKNOWN and runs once.
- The schema migration rebuilds `live_executions` once to drop the `NOT NULL` and turns the hard-coded `0.0` on existing Alpaca fills into `NULL` unless the symbol is provably an equity (`shared/alpaca_symbols.py`): crypto in any accepted spelling (`BTC/USD`, `BTC-USD`, `BTCUSD`) and anything unclassifiable (options, malformed symbols) become unknown. The backfill logs every unknown-commission execution it skips, with the reason (not crypto, no posted CFEE batch covers it yet).
- `actual_price_observed` labels every Alpaca price: `observed` (stated for this fill), `derived` (computed from broker prices: the order's `filled_avg_price` on a single-fill order, or a reconciled remainder), `sent_bad`/`absent` (no usable price), `derived_unavailable` (the remainder's price could not be derived honestly). A stream event never borrows the order's cumulative average for a partial fill.
- A reconciled fill whose order has no usable `filled_avg_price` is not stored; the next reconciliation pass (adapter connect) retries it. Otherwise the missing QUANTITY is always stored. Its price is `(avg*broker_qty - sum(local price*qty)) / missing_qty`, labelled `derived`, when every local leg has a usable price and the result lies within 10% of the order's average (`REMAINDER_PRICE_MAX_REL_DEVIATION`); otherwise it is stored at `0.0` with `derived_unavailable`, so exit sizing and duplicate-order protection still see the quantity.
- `live_executions.timestamp` drives PnL ordering and is never invented (#135). A fill takes the stream event's own timestamp, then the order's `filled_at`, then its `updated_at`; a stamp that carries no time of day (a bare date such as `2026-11-03`) is refused rather than read as midnight. When none parses, no row is stored: the stream path logs the quantity it dropped (`filled_qty`, `broker_cumulative_filled_qty`) and the reconciled path logs `missing_filled_qty`, and both say the retry is the next adapter connect, because Alpaca reconciliation runs on `connect()` only - there is no periodic pass. Until then the local filled quantity understates the broker's, so exit sizing and duplicate-order protection size against a flat position.
- Every PnL reader treats a `sent_bad`/`absent`/`derived_unavailable` (or non-positive) price as unknown: API rows carry `actual_price: null` and `slippage: null`, and live stats report `total_pnl: null`, `pnl_complete: false` and `price_unknown_count`; the frontend shows "incomplete" instead of a number.

## Signal Bot Health Check (#158)

The signal bot health check stays a standalone script plus scheduler job —
there is deliberately no `runner.cli` subcommand for it.

- Run it manually (no flags; JSON on stdout, exit code `0` = ok, `1` = error):

  ```bash
  uv run python -m signal_bots.health_check
  ```

- Schedule it via the viewer **Services** page ("+ Add Scheduled Job", job type
  `signal_bot_health_check`, cron in UTC) or `POST /api/services` with
  `type: "scheduled_job"`, `status: "running"`, and
  `config: {"cron": "...", "job_type": "signal_bot_health_check"}`. The
  scheduler runs inside `live_runner.daemon`; results land as background task
  records visible in the viewer. Details: `docs/signal_bots.md` ("Health-Check
  Status").

## Commission sign migration (cTrader, #205)

`live_executions.commission` is a positive cost for every broker. The first `init_db` of a build with this change converts existing cTrader rows once (`abs` of the stored value, recorded in `schema_data_migrations`). Stop the daemon (and any process on the same SQLite file) before starting the new build: an old build that still writes cTrader fills after the marker is recorded stores a raw negative charge, which is indistinguishable from a new-convention rebate and is never converted. Roll out as stop, deploy, start; do not run old and new builds side by side.

## Conflicting broker order id on a duplicate signal (#126)

A redelivered or duplicate live signal that carries a **different non-empty**
`broker_order_id` than the stored one means one of two things: a second broker
order was placed for one logical signal, or the ids were mapped wrongly. Either
way a broker order may exist that the ledger does not track, and an untracked
open position is an unmanaged one. **Fail closed:** the deployment is halted,
not warned at.

**What the platform does** (`shared/database.py`,
`live_runner/broker_order_conflicts.py`)

- The tracked id is never overwritten. The conflicting id is recorded on the
  signal: `live_signals.metadata_json["conflicting_broker_order_ids"]`,
  deduplicated across redeliveries, in the same transaction as the halt.
- The **affected deployment** is set to `PAUSED`. The other deployments on the
  account keep trading, and their positions are untouched. A `STOPPED`
  deployment is never downgraded: its own cancel-and-flatten enforcement handles
  it, so check `stopped_enforced_at` on the deployment payload (and the broker)
  before treating it as closed. A `STOPPED` row without that stamp still has
  exposure at the broker - `exposure_may_be_open` reports exactly that.
- `ERROR` log with `alert=live_broker_order_id_conflict` (Sentry turns it into
  an event), naming the signal, the deployment, the tracked and the conflicting
  id, and whether this call wrote the halt.
- Every service sync tick re-applies the halt, before anything else in that tick
  and in its own error handling: a restart, a status write or a row written by an
  older build cannot leave a conflicted deployment trading again. Every halt write
  logs the `ERROR` alert, at detection and at a tick re-halt alike; the tick adds
  an `INFO` summary naming the deployments it re-halted. An already-halted
  deployment logs nothing (re-writing it would move `updated_at` every pass), and
  a halt that cannot be written is its own `ERROR` alert.

**`PAUSED` still allows exits** (maintainer decision, #126). A pause blocks new
entries and still dispatches exit-only signals, matching the `PAUSED` row of the
"Operator safety controls" comment in `live_runner/base_adapter.py`
("stop opening new positions; orders and positions are left untouched"): `ACTIVE` or
`PAUSED` for a close, `ACTIVE` alone for an entry, decided per deployment from the
raw signal - a parsed exit intent, or a plain `SELL`/`SHORT` on a deployment that
cannot short - and checked at the fan-out, the pre-submit re-check and the atomic
submission claim. `STOPPED` is excluded - its exposure is closed by the stop
sweeps. This applies to every pause, manual ones included. Consequence: a
deployment halted for a conflict can still close the position the ledger *does*
know about; it just cannot add anything.

For that to be reachable the account has to stay wired up: the broker adapter, the
Discord listener and the per-account task set are kept for an account whose
deployments are all `PAUSED` (they were selected from an `ACTIVE`-only account
set, which dropped every exit at the task level). Those tasks dispatch and record;
what may be *opened* is still decided per signal, so no entry can come out of it.

**Where an operator sees it**

- `GET /api/live/deployments`: `broker_order_conflicts` and
  `halted_for_broker_order_conflict` per deployment.
- `GET /api/live/dashboard`: `broker_order_conflicts` (fleet-wide `status`,
  `deployment_ids`, signal rows). A halted deployment is not in
  `active_strategies` at all, which is why the conflict has its own block.
- SQL: `shared.database.list_live_signal_broker_order_conflicts()` (signal rows)
  and `shared.database.deployments_with_broker_order_conflicts()` (grouped by
  deployment).
- Health read: `live_runner.broker_order_conflicts.check_broker_order_conflicts()`
  → `{"status": "ok" | "error", "deployment_ids": [...], "conflicts": [...]}`.

**Cost, and what grows it.** Every one of those reads scans `live_signals` for the JSON
key, because there is no index on it: once per 60 s service-sync tick in the daemon, and
once per dashboard or deployments request. That is fine at today's signal counts and it is
the reason the resolution path is an endpoint rather than something a page polls in a
loop. If the fleet ever grows enough for it to matter, the fix is structural - a partial
index on the conflicts key, or a dedicated per-deployment flag column - and not a cap on
the read: the reads that decide a halt and a resolution above are deliberately uncapped,
so a trimming that hides an older conflict cannot happen there.

**How to resolve one**

1. Look the conflicting id up at the broker (open orders and positions). This is
   a manual step: nothing in the platform queries the broker for the conflicting
   id yet, so a position nobody tracks has to be found by hand. Keep the order id
   in the reason you record below - it is the only external reference to what was
   reconciled.
2. **Cancel first, then close.** A conflicting id that is still resting at the broker
   is an order the platform will never manage: cancel it at the broker and confirm it
   reached a terminal state before you record anything. Cancelling is not closing - a
   working order can still fill, and a fill of a cancelled order is exactly the untracked
   position this runbook exists for.
3. Close whatever did fill and the ledger does not track, at the broker, and account
   for it in the ledger if it belongs to this platform.
4. Record the resolution: `POST
   /api/live/deployments/{id}/broker-order-conflicts/resolve` with
   `{"confirmed_deployment_id": "<deployment id>", "reason": "<what you found>"}`.
   The reason is
   required and audited; the call clears every recorded conflict for the
   deployment (a partial clear would still leave it halted) and reports the
   in-flight submission drain with it.
5. Resume: `PUT /api/live/deployments/{id}/status` with
   `{"status": "ACTIVE", "confirmation": {"confirmed_deployment_id": "<deployment id>"}}`
   (an unconfirmed write is a `428`, and a confirmation naming another deployment a
   `409`), which does its own broker re-verification. The write is **refused with
   `409` while a conflict is still recorded**: the way out of the halt is step 4,
   not a status write. A resume refused with that `409` means a conflict is still
   recorded: step 4 has not run, or a new conflict arrived since. A resume that was
   accepted and is later re-paused means a new conflict was recorded after step 4,
   typically a duplicate still being redelivered: the conflict ledger's own
   read-modify-write wins that race on purpose. Investigate the new conflict and
   repeat steps 3-5.

`live_executions.commission` is a positive cost for every broker; a broker rebate cannot be stored as a negative cost, so the cTrader adapter stores it as `NULL` (fee unknown) and logs an ERROR — the fill is kept and net PnL stays flagged incomplete (#285). The first `init_db` of a build with this change converts existing cTrader rows once (`abs` of the stored value, recorded in `schema_data_migrations`). Stop the daemon (and any process on the same SQLite file) before starting the new build: an old build that still writes cTrader fills after the marker is recorded stores a raw negative charge that violates the positive-cost convention and is never converted (the marker is already recorded, and new-convention rebates are stored as NULL, so the stray negative is at least distinguishable). Roll out as stop, deploy, start; do not run old and new builds side by side.

## Release / rollback

The live state that must survive a deploy is the SQLite database (`live_executions`, the
ledgers, `live_deployments`, `schema_data_migrations`). `scripts/db_backup.py` backs it up and
restores it; run it from the repo root with the same `.env`/`DATA_DIR` the daemon uses.

### Backup

```bash
uv run python -m scripts.db_backup backup          # snapshot now, keep 14
uv run python -m scripts.db_backup backup --keep 30
uv run python -m scripts.db_backup list            # what is retained
uv run python -m scripts.db_backup verify <file>   # integrity + applied migrations
```

Backups use the SQLite online backup API (`pages=-1`, one step under a single read
transaction), so they are **safe while the daemon runs** - the copy is always a consistent
committed-state snapshot, no stop needed. Files land in `<db parent>/backups/<db file
name>-<UTC stamp>.db` - `<DATA_DIR>/backups/backtests.db-<UTC stamp>.db` for the default
database, or pass `--backup-dir` to choose another directory - one self-contained file each
(rollback-journal mode), rotating to `--keep` per database *within that directory*. Schedule
it (cron/systemd timer) independently of deploys; the release procedure below takes one
explicitly anyway, and pre-deploy snapshots should go to their own `--backup-dir` (or be
copied off-box) so daily rotation cannot prune the backup a rollback past a migration needs.

### Restore

```bash
uv run python -m scripts.db_backup restore <backup-file>
```

Restore **refuses while the database may be in use** and exits 2. Before overwriting, it
holds the daemon's single-instance lock for the whole operation (so no daemon can start
mid-restore), proves no other process has the database open by switching it to
rollback-journal mode - which fails while the viewer, a smoke run, or any other connection
holds it - takes a safety snapshot of the current database into the same backup directory
(skip with `--no-safety-snapshot`), verifies the backup (`PRAGMA quick_check`), stages the
restore beside the target, and swaps it in with `os.replace` - a failed copy never leaves a
half-written database, and stale `-wal`/`-shm`/`-journal` sidecars are removed first so old
frames cannot replay over the restored file. It prints the `schema_data_migrations` delta
between the backup and the pre-restore database; read it before starting any build.

The guard works in three layers:

* **Single-instance lock.** Once the daemon lock from PR #313
  (`shared/single_instance.py`, `<db>.lock` next to the database file) is on main, the script
  uses it; until then it holds the same `flock` inline on the same file. Either way the lock
  is *held* through the swap, not just probed.
* **Process scan.** The script scans the process table for a `live_runner.daemon` command
  line (`ps` on Linux/macOS, PowerShell CIM on Windows) and refuses when one is found,
  whatever the lock reports: during the first release after #313 the running daemon is an old
  build that never takes the lock, and a daemon on another `DATABASE_URL` cannot be told
  apart from one on this database. If you are certain the found process trades a different
  database, re-run with `--force` (the override is logged to stderr). Check what the guard
  sees with `python -m scripts.db_backup status`.
* **Open-database probe.** `PRAGMA journal_mode=DELETE` on the live file fails while any
  other process - the viewer above all - has it open, because `os.replace` would strand that
  connection on the unlinked inode and its later commits would be silently lost. **Stop the
  viewer and every process using the database before restoring**; `--force` overrides this
  too, with the same data-loss risk as a live daemon.

### Release procedure

1. **Stop** the daemon (SIGINT/SIGTERM; confirm no `live_runner.daemon` process remains and
   `db_backup status` shows a free lock) **and stop the viewer and every other process using
   the database** - restore refuses while any of them holds it open.
2. **Backup**: `python -m scripts.db_backup backup` - for a risky deploy take it into a
   dedicated directory (`--backup-dir <dir>`) or copy the file off-box; the default backup
   dir is local disk only and its `--keep` rotation will eventually prune it.
3. **Deploy** the new build.
4. **Migrate**: the first `init_db` runs schema and one-way data migrations exactly once
   (recorded in `schema_data_migrations`). There is no separate migrate step - do not start two
   builds side by side across a migration boundary (see #205 below).
5. **Start** the daemon, then verify fills/ledgers reconcile (`reconcile_executions`).

### Rollback

* **Code-only rollback** (no data migration ran): stop, check out the previous build, start.
  Safe at any time.
* **Data rollback**: stop everything on the database (daemon *and* viewer),
  `python -m scripts.db_backup restore <pre-deploy backup>`, then **before starting anything**
  reconcile the restored state against the broker: the restore rolls the database back to the
  backup, so orders, fills and positions created since are invisible to it - compare broker
  positions and open orders with the restored DB, flatten or manually reconcile anything newer
  than the backup, and confirm no signal source will re-ingest post-backup messages (a replay
  would create duplicate live orders; signal-ingestion cursors written after the backup are
  gone too). Only then start the build that matches the backup's schema. The restore prints
  which `schema_data_migrations` the backup lacks; the next `init_db` re-applies them, which
  is only correct if the backup predates them. Note the ledger does not see every data
  change: the #205-style commission backfill rewrites rows without a
  `schema_data_migrations` marker, so "no migration ran" cannot be read off the ledger alone.
* **One-way migrations cannot be rolled back by an old build.** The #205 commission-sign
  migration and the other `schema_data_migrations` entries transform data in place; an old build
  starting on a migrated database misreads it (e.g. a raw negative cTrader charge is
  indistinguishable from a new-convention rebate). To go back past one, restore a backup taken
  *before* that migration and accept losing every fill written since. `live_executions` can
  only be rebuilt through reconciliation for fills whose signal the backup already contains -
  `reconcile_executions` walks local `live_signals`, so orders created after the backup have
  no local signal and stay invisible; the broker remains the source of truth for those, per
  the reconciliation steps above.
