# Live Integration Deploy Runbook

Status: credential-gated as of 2026-07-13.

## Preconditions

- `.accounts.env` exists and remains gitignored.
- Alpaca account blocks `ACC1..3` contain paper endpoints only:
  - Nitro: `PA3E767TJ91D`
  - RamJet: `PA3T0MQAGQ51`
  - RocketFuel: `PA3EFBL4P7BU`
- Discord bot tokens and channel IDs are set for Nitro, RamJet, and RocketFuel.
- cTrader credentials are demo credentials if the cTrader smoke is run.

## Non-Ordering Preflight

```bash
uv sync --all-extras
uv run python -m live_runner.preflight --config live_deployments.yaml --accounts-env .accounts.env
uv run python scripts/live_smoke.py --mode discord-replay --reports-dir reports
```

## Paper Broker Smoke

For Alpaca smoke, first verify every Alpaca account resolves with `paper=True`.
For cTrader smoke, verify the configured cTrader credentials are demo credentials.

```bash
uv run python scripts/live_smoke.py --mode alpaca --accounts-env .accounts.env --reports-dir reports
uv run python scripts/live_smoke.py --mode ctrader --accounts-env .accounts.env --reports-dir reports
```

## Demo Daemon

Only run after the paper smoke succeeds.

```bash
set -a
. ./.accounts.env
set +a
set -o pipefail
uv run python -m live_runner.daemon --config live_deployments.yaml 2>&1 | tee "reports/live_daemon_$(date -u +%Y%m%dT%H%M%SZ).log"
```

Keep the daemon running long enough to verify Discord connection, paper order submission, fill handling, and `reconcile_executions`.
