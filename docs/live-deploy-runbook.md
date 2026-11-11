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
| Alpaca  | yes    | yes       | no (#168)         |
| cTrader | yes    | no        | no (#168)         |

cTrader stays on `none`: the daemon has no cTrader market data to trail on, and the adapter does
not declare the `supports_stop_amendment` capability. Unknown brokers get `none` only.

**Where an unsupported mode is rejected**

- Deployment validation: `LiveDeploymentConfig` rejects a `trailing_mode` override no live broker
  executes; `create_live_deployment`, `update_live_deployment_account` (re-point) and the YAML
  parser reject one the deployment's account broker does not execute.
- Signal time: the signal-processor gate and the order-preparation gate both check the mode
  against the adapter that would submit the order (broker table and the adapter's own
  `supports_stop_amendment` flag). A refused signal is not traded; a requested trail is never
  dropped silently.

**How `percent` is executed (Alpaca)**

- Arithmetic: `RiskEngine.apply_trailing_stop`, the same call the replay backtest makes, applied
  once per closed bar to the bar close. A stop only ever moves in the favourable direction. The
  broker stop is the computed stop rounded to Alpaca's price tick away from the market (down for
  a long, up for a short), so it is at most one tick looser than the replay stop.
- Trigger: each closed bar of the deployment's signal timeframe (`signal_config.timeframe`, else
  the deployment timeframe). The loop wakes every 15 s but only compares timestamps; it reads
  positions, orders and bars (`AlpacaLiveAdapter.get_market_data`) when a bar boundary has passed
  or a retry is due. A bar that is not published yet is re-requested with backoff (15 s doubling
  to 5 min).
- Amendment: the protective stop order is replaced in place (`PATCH /v2/orders/{id}`). The
  previous stop keeps working until the replacement is accepted and stays if it is refused; the
  stop is never cancelled in order to move it.
- Failure: a refused or failed amendment, a missing or ambiguous stop, or missing market data is
  logged at ERROR (`Live trailing stop could not be updated; ...`, with account, symbol, signal,
  stop order id, current and target stop, consecutive failure count) and sent to Sentry
  (`component=live_trailing_stops`). It is retried with the same backoff; the previous
  broker-side stop stays in force meanwhile.
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
- The Alpaca replace of a bracket stop leg is covered by tests against the fake trading client
  only. Confirm it once on a paper account (open a `percent` deployment, watch for
  `Trailed live stop` in the daemon log) before relying on it.

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
