# Live Integration Deploy Runbook

Status: credential-gated as of 2026-07-13.

## Preconditions

- `.accounts.env` exists and remains gitignored.
- Alpaca account blocks `ACC1..3` (Nitro, RamJet, RocketFuel) contain paper endpoints only; IDs werden aus `.accounts.env` aufgeloest, nicht hier dokumentiert.
- Discord bot tokens and channel IDs are set for Nitro, RamJet, and RocketFuel.
- cTrader credentials are demo credentials if the cTrader smoke is run.

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
but the daemon will not keep a non-demo adapter and the viewer will not arm the
deployment. Lifting the demo-only block is a separate, deliberate step.

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
| Strategy (no `signal_processor`) | `strategies/<strategy_name>/<strategy_name>.py` | `STRATEGY_META["parameters"]` defaults |
| Signal bot (`signal_config.signal_processor` set) | `signal_bots/<bot>/processor.py` | processor id, `signal_config.signal_processor_config`, SHA256 of the bot's `config.yaml` |

No live code path applies per-deployment strategy parameters, so a strategy is
promoted with its `STRATEGY_META` defaults: write optimised parameters into the
file, then promote. A deployment that carries `settings.strategy_parameters` is
refused on real money. Other `settings` keys (risk limits, selection metadata)
are not strategy parameters and are not bound. A Discord deployment without a
`signal_processor` has nothing to hash unless a strategy of the same name
exists, so it is refused on real money.

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
