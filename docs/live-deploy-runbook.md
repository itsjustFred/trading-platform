# Live Integration Deploy Runbook

Status: credential-gated as of 2026-07-13.

## Preconditions

- `.accounts.env` exists and remains gitignored.
- Alpaca account blocks `ACC1..3` (Nitro, RamJet, RocketFuel) contain paper endpoints only; IDs werden aus `.accounts.env` aufgeloest, nicht hier dokumentiert.
- Discord bot tokens and channel IDs are set for Nitro, RamJet, and RocketFuel.
- cTrader credentials are demo credentials if the cTrader smoke is run.
- Trailing stops: live support is per broker (see "Live Trailing Stops" below). Alpaca executes
  `none` and `percent`; cTrader executes `none` only. Any other combination is rejected when the
  deployment is validated and again at signal time, before anything reaches the broker.

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

Two rules the replay implements today; a live implementation must copy them exactly, or paper
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

**Where an unsupported mode is rejected**

- Deployment validation: `LiveDeploymentConfig` rejects a `trailing_mode` override no live broker
  executes; `create_live_deployment`, `update_live_deployment_account` (re-point) and the YAML
  parser reject one the deployment's account broker does not execute.
- Signal time: the signal-processor gate and the order-preparation gate both check the mode
  against the adapter that would submit the order (broker table and the adapter's own
  `supports_stop_amendment` flag). A refused signal is not traded; a requested trail is never
  dropped silently.
- Stored rows: a stored deployment whose trailing override no live broker executes still loads
  (so it stays listed and can be stopped); the daemon logs an error for it at load and every
  signal it produces is refused before it reaches the broker.

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
  `trailing_mode: percent` in `signal_processor_config` is rejected at API create and YAML parse. A
  deployment whose trailing mode only comes from the signal processor's own default cannot be
  checked before a signal exists; it is refused at its first entry. Session-aligned daily trailing
  is a follow-up.
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
**Verified on Alpaca paper (2026-11-16)**

Probe on a paper account (endpoint host checked as `paper-api.alpaca.markets`): 1 share of `F`
as a market bracket entry (stop 3 % below, take profit 5 % above), then listings, a replace and a
clean-up.

- Right after the entry fills: parent `filled`; take-profit leg `new`; stop leg `held`.
- `status=open` (nested or flat) lists the take-profit leg only. **The `held` stop leg is not in
  any open listing.** `AlpacaLiveAdapter.list_protective_stops` therefore also reads filled
  parents of symbols with an open position from a `status=all`, `nested=true` listing and takes
  their `held` stop legs (fixed in this PR; before it a real bracket position reported "no stop").
- `PATCH /v2/orders/{id}` on the `held` stop leg is accepted: the response is a new order id,
  `held`, with the new `stop_price`; the parent's `legs` then show the new stop and the unchanged
  take-profit leg. Through the adapter the stop moved 11.35 -> 11.52 with the take profit intact.
- Not covered by this PR: `close_all_positions` on a bracket with a `held` stop leg returned
  `orders-still-working ... NO-STOP-RESTORED` (the sweep lists open orders only); the leg was
  cancelled by Alpaca's OCO handling and the position was closable afterwards. Tracked separately.

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
deployment. The cTrader top-N deploy with `activate` is different: it creates
the deployments `ACTIVE` directly, without that re-verification, on any account
the gate passes. Only the daemon's demo-only block stops their orders. Refusing
`activate` on a non-demo account there is a maintainer decision, not yet made.
Lifting the demo-only block is a separate, deliberate step.

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

### Recording a promotion (CLI only)

```bash
uv run python -m runner.cli promote \
    --strategy example_sma \
    --promoted-by "Jane Doe" \
    --optimization-result optimizations/opt-123.json \
    --paper-account-id 10105152 --paper-broker ctrader \
    --paper-start 2026-08-01 --paper-end 2026-10-15 --paper-trades 42 \
    --paper-report docs/reports/paper_example_sma.csv \
    --parity-reference lean \
    --parity-deviation total_return=0.012 --parity-deviation sharpe_ratio=0.008 \
    --parity-report docs/reports/parity_example_sma.json
```

For a signal bot use `--signal-processor ramjet` and pass the deployment's
`signal_processor_config` as `--params '{...}'` (`--params` is refused for a
strategy). The out-of-sample evidence is read from the result file, not typed
in. Both report files must exist; their SHA256 is recorded. Every argument is
mandatory, and the command exits non-zero without writing anything if any
evidence is missing or below a threshold.

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

## Demo Daemon

Only run after the paper smoke succeeds.

```bash
set -a
. ./.accounts.env
set +a
set -o pipefail
uv run python -m live_runner.daemon 2>&1 | tee "docs/reports/live_daemon_$(date -u +%Y%m%dT%H%M%SZ).log"
```

> NOTE (Stand 2026-09): `daemon.main()` nimmt kein `--config` — Config kommt aus der DB via `shared/database.list_live_deployments`, nicht aus `live_deployments.yaml`.

Keep the daemon running long enough to verify Discord connection, paper order submission, fill handling, and `reconcile_executions`.

## Market-data symbol classification (Alpaca, #122)

`live_runner/symbol_class.py` decides which feed a symbol belongs to, by *asset class* and never by string shape alone: a pair is recognised only when its base currency is in `CRYPTO_BASES` and its quote currency is in `CRYPTO_QUOTES` (that quote list is shared with `shared/alpaca_symbols.py`, so fee backfill and market-data routing cannot drift on it). Share-class equities (`BRK.B`, `BF-B`) are equities, exactly as in the fee backfill.

Anything else is refused, not guessed: `refresh_from_deployments` logs "Refusing to subscribe unclassifiable symbol to market data" with the deployment id and skips it, so that symbol gets no quotes. Fail closed on purpose — a crypto pair routed to the stock feed produces no bars either, but silently. A new coin Alpaca lists has to be added to `CRYPTO_BASES`; an unknown six-letter ticker is a config error, not a fed fallback.

Bars are keyed by canonical pair (`BTC/USD`) in every spelling (`BTCUSD`, `btc-usd`), on the manual/backfill ingest path and on the live `_on_bar` callback alike, so a quote lookup cannot miss bars that arrived in the other form. A bar that still arrives under an unclassified symbol is stored under its own spelling and logged ("Storing market-data bars under an unclassified symbol key") rather than dropped in silence.

## Execution prices and commissions (Alpaca, #124)

- `live_executions.commission` is nullable. `0.0` is a known zero (Alpaca equities); `NULL` means the fee is not known yet. Every reader treats `NULL` as "net PnL incomplete", never as zero: the live stats payload reports `net_pnl: null`, `net_pnl_complete: false` and `commission_unknown_count`, and trade/signal payloads carry `commission: null`.
- Alpaca crypto fees are posted end of day as `CFEE` account activities, so crypto fills are stored with `NULL`. The daemon's hourly `commission-backfill` task reads `FILL`, `CFEE` and `FEE` activities over REST, once a trade date is two UTC days old and for fills up to 30 days back, allocates each (pair, trade date)'s fee pro rata by received quantity, converts it to USD and writes it. Everything it cannot attribute unambiguously stays `NULL` and is logged ("Alpaca commission backfill: leaving ..."); see `live_runner/alpaca_fees.py` for the rules.
- The schema migration rebuilds `live_executions` once to drop the `NOT NULL` and turns the hard-coded `0.0` on existing Alpaca fills into `NULL` unless the symbol is provably an equity (`shared/alpaca_symbols.py`): crypto in any accepted spelling (`BTC/USD`, `BTC-USD`, `BTCUSD`) and anything unclassifiable (options, malformed symbols) become unknown. The backfill logs every unknown-commission execution it skips, with the reason (not crypto, trade date not posted yet).
- `actual_price_observed` labels every Alpaca price: `observed` (stated for this fill), `derived` (computed from broker prices: the order's `filled_avg_price` on a single-fill order, or a reconciled remainder), `sent_bad`/`absent` (no usable price), `derived_unavailable` (the remainder's price could not be derived honestly). A stream event never borrows the order's cumulative average for a partial fill.
- A reconciled fill whose order has no usable `filled_avg_price` is not stored; the next reconciliation pass (adapter connect) retries it. Otherwise the missing QUANTITY is always stored. Its price is `(avg*broker_qty - sum(local price*qty)) / missing_qty`, labelled `derived`, when every local leg has a usable price and the result lies within 10% of the order's average (`REMAINDER_PRICE_MAX_REL_DEVIATION`); otherwise it is stored at `0.0` with `derived_unavailable`, so exit sizing and duplicate-order protection still see the quantity.
- `live_executions.timestamp` drives PnL ordering and is never invented (#135). A fill takes the stream event's own timestamp, then the order's `filled_at`, then its `updated_at`; a stamp that carries no time of day (a bare date such as `2026-11-03`) is refused rather than read as midnight. When none parses, no row is stored: the stream path logs the quantity it dropped (`filled_qty`, `broker_cumulative_filled_qty`) and the reconciled path logs `missing_filled_qty`, and both say the retry is the next adapter connect, because Alpaca reconciliation runs on `connect()` only - there is no periodic pass. Until then the local filled quantity understates the broker's, so exit sizing and duplicate-order protection size against a flat position.
- Every PnL reader treats a `sent_bad`/`absent`/`derived_unavailable` (or non-positive) price as unknown: API rows carry `actual_price: null` and `slippage: null`, and live stats report `total_pnl: null`, `pnl_complete: false` and `price_unknown_count`; the frontend shows "incomplete" instead of a number.

## Commission sign migration (cTrader, #205)

`live_executions.commission` is a positive cost for every broker. The first `init_db` of a build with this change converts existing cTrader rows once (`abs` of the stored value, recorded in `schema_data_migrations`). Stop the daemon (and any process on the same SQLite file) before starting the new build: an old build that still writes cTrader fills after the marker is recorded stores a raw negative charge, which is indistinguishable from a new-convention rebate and is never converted. Roll out as stop, deploy, start; do not run old and new builds side by side.
