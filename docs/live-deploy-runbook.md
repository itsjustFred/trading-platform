# Live Integration Deploy Runbook

Status: credential-gated as of 2026-07-13.

## Preconditions

- `.accounts.env` exists and remains gitignored.
- Alpaca account blocks `ACC1..3` (Nitro, RamJet, RocketFuel) contain paper endpoints only; IDs werden aus `.accounts.env` aufgeloest, nicht hier dokumentiert.
- Discord bot tokens and channel IDs are set for Nitro, RamJet, and RocketFuel.
- cTrader credentials are demo credentials if the cTrader smoke is run.

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

## Paper Broker Smoke

For Alpaca smoke, first verify every Alpaca account resolves with `paper=True`.
For cTrader smoke, verify the configured cTrader credentials are demo credentials.

```bash
uv run python scripts/live_smoke.py --mode alpaca --accounts-env .accounts.env --reports-dir docs/reports
uv run python scripts/live_smoke.py --mode ctrader --accounts-env .accounts.env --reports-dir docs/reports
```

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

## Execution prices and commissions (Alpaca, #124)

- `live_executions.commission` is nullable. `0.0` is a known zero (Alpaca equities); `NULL` means the fee is not known yet. Every reader treats `NULL` as "net PnL incomplete", never as zero: the live stats payload reports `net_pnl: null`, `net_pnl_complete: false` and `commission_unknown_count`, and trade/signal payloads carry `commission: null`.
- Alpaca crypto fees are posted end of day as `CFEE` account activities, so crypto fills are stored with `NULL`. The daemon's hourly `commission-backfill` task reads `FILL`, `CFEE` and `FEE` activities over REST, once a trade date is two UTC days old and for fills up to 30 days back, allocates each (pair, trade date)'s fee pro rata by received quantity, converts it to USD and writes it. Everything it cannot attribute unambiguously stays `NULL` and is logged ("Alpaca commission backfill: leaving ..."); see `live_runner/alpaca_fees.py` for the rules.
- The schema migration rebuilds `live_executions` once to drop the `NOT NULL` and turns the hard-coded `0.0` on existing Alpaca fills into `NULL` unless the symbol is provably an equity (`shared/alpaca_symbols.py`): crypto in either spelling (`BTC/USD`, `BTCUSD`) and anything unclassifiable (options, malformed symbols) become unknown. The backfill logs every unknown-commission execution it skips, with the reason (not crypto, trade date not posted yet).
- `actual_price_observed` labels every Alpaca price: `observed` (stated for this fill), `derived` (computed from broker prices: the order's `filled_avg_price` on a single-fill order, or a reconciled remainder), `sent_bad`/`absent` (no usable price), `derived_unavailable` (the remainder's price could not be derived honestly). A stream event never borrows the order's cumulative average for a partial fill.
- A reconciled fill whose order has no usable `filled_avg_price` is not stored; the next reconciliation pass (adapter connect) retries it. Otherwise the missing QUANTITY is always stored. Its price is `(avg*broker_qty - sum(local price*qty)) / missing_qty`, labelled `derived`, when every local leg has a usable price and the result lies within 10% of the order's average (`REMAINDER_PRICE_MAX_REL_DEVIATION`); otherwise it is stored at `0.0` with `derived_unavailable`, so exit sizing and duplicate-order protection still see the quantity.
- Every PnL reader treats a `sent_bad`/`absent`/`derived_unavailable` (or non-positive) price as unknown: API rows carry `actual_price: null` and `slippage: null`, and live stats report `total_pnl: null`, `pnl_complete: false` and `price_unknown_count`; the frontend shows "incomplete" instead of a number.
