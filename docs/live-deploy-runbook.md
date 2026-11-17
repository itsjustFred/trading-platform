# Live Integration Deploy Runbook

Status: credential-gated as of 2026-07-13.

## Preconditions

- `.accounts.env` exists and remains gitignored.
- Alpaca account blocks `ACC1..3` (Nitro, RamJet, RocketFuel) contain paper endpoints only; IDs werden aus `.accounts.env` aufgeloest, nicht hier dokumentiert.
- Discord bot tokens and channel IDs are set for Nitro, RamJet, and RocketFuel.
- cTrader credentials are demo credentials if the cTrader smoke is run.
- Trailing stops are not executed live yet (#167). A deployment whose
  `signal_config.signal_processor_config.trailing_mode` is anything other than `none` is rejected
  when it is created or parsed from YAML. A stored deployment that already has such a value still
  loads (so it stays listed and can be stopped), the daemon logs an error for it at load, and each
  of its signals is refused before it reaches the broker. See `shared/trailing.py` and
  `docs/signal_bots.md`.

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
- The schema migration rebuilds `live_executions` once to drop the `NOT NULL` and turns the hard-coded `0.0` on existing Alpaca fills into `NULL` unless the symbol is provably an equity (`shared/alpaca_symbols.py`): crypto in either spelling (`BTC/USD`, `BTCUSD`) and anything unclassifiable (options, malformed symbols) become unknown. The backfill logs every unknown-commission execution it skips, with the reason (not crypto, trade date not posted yet).
- `actual_price_observed` labels every Alpaca price: `observed` (stated for this fill), `derived` (computed from broker prices: the order's `filled_avg_price` on a single-fill order, or a reconciled remainder), `sent_bad`/`absent` (no usable price), `derived_unavailable` (the remainder's price could not be derived honestly). A stream event never borrows the order's cumulative average for a partial fill.
- A reconciled fill whose order has no usable `filled_avg_price` is not stored; the next reconciliation pass (adapter connect) retries it. Otherwise the missing QUANTITY is always stored. Its price is `(avg*broker_qty - sum(local price*qty)) / missing_qty`, labelled `derived`, when every local leg has a usable price and the result lies within 10% of the order's average (`REMAINDER_PRICE_MAX_REL_DEVIATION`); otherwise it is stored at `0.0` with `derived_unavailable`, so exit sizing and duplicate-order protection still see the quantity.
- Every PnL reader treats a `sent_bad`/`absent`/`derived_unavailable` (or non-positive) price as unknown: API rows carry `actual_price: null` and `slippage: null`, and live stats report `total_pnl: null`, `pnl_complete: false` and `price_unknown_count`; the frontend shows "incomplete" instead of a number.
