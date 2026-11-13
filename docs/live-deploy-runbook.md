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
# Also connect to every broker account and read balance/positions (still no orders):
uv run python -m live_runner.preflight --config live_deployments.yaml --accounts-env .accounts.env --check-connectivity
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

### Stopping the daemon

`SIGTERM` (systemd stop, `kill`) and `SIGINT` (Ctrl-C) start a graceful shutdown:

1. No new order submissions are accepted (a signal arriving now is logged and dropped before the broker request).
2. An order submission already in flight is allowed to finish (default 30 s).
3. Listeners, Discord bots, the scheduler and broker connections are closed (default 20 s per phase).
4. Exit code `0` on a clean shutdown, `1` if the drain or a cleanup phase timed out or failed. After `1`, the outcome of an in-flight order is unknown: the submission lease is kept, so reconcile the account before sending new signals.
5. A second signal forces an immediate exit (code `130`) and skips all cleanup.

Shutdown never cancels or closes open orders or positions at the broker; they are left exactly as they were. On systemd set `TimeoutStopSec` above 90 s so the unit is not killed mid-drain. On Windows only Ctrl-C/Ctrl-Break (`SIGINT`) can be delivered; `SIGTERM` cannot, so stop the process with Ctrl-C.
