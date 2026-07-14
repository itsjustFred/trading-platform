# Live Integration Status

Date: 2026-07-13  
Branch: `codex/live-integration`  
Base: `codex/new-main`  
Draft PR: https://github.com/itsjustFred/trading-platform/pull/6  
Mode: live execution gated; all broker connections/orders remain paper/demo-only.

## Credential Gate

- `.accounts.env` is gitignored: passed (`git check-ignore .accounts.env` returned `.accounts.env`).
- `.accounts.env` exists: no.
- Alpaca paper credentials: gated, no `ACC1..3` blocks available.
- Discord bot tokens/channel IDs: gated, no `.accounts.env` source available.
- cTrader demo credentials: gated, no `.accounts.env` source available.
- No live broker connection, Discord bot session, daemon, paper order, fill, or reconciliation was run.

Missing fields to resume live paper/demo validation:

- `ALPACA_API_KEY_ACC1..3`
- `ALPACA_SECRET_KEY_ACC1..3`
- `ALPACA_ENDPOINT_ACC1..3` with paper URLs only
- `ALPACA_ACCOUNT_NUMBER_ACC1..3`
- `ALPACA_ACCOUNT_NAME_ACC1..3`
- `DISCORD_BOT_NITRO_TOKEN`, `DISCORD_BOT_NITRO_CHANNEL_ID`
- `DISCORD_BOT_RAMJET_TOKEN`, `DISCORD_BOT_RAMJET_CHANNEL_ID`
- `DISCORD_BOT_ROCKETFUEL_TOKEN`, `DISCORD_BOT_ROCKETFUEL_CHANNEL_ID`
- `CTRADER_APP_ID`, `CTRADER_APP_SECRET`, `CTRADER_ACCESS_TOKEN`, `CTRADER_REFRESH_TOKEN` for demo only

## Routing Matrix

The checked deployment prep and code routing map are:

| Discord bot | Alpaca paper account | Deployment account |
|---|---:|---:|
| Nitro | `PA3E767TJ91D` | `PA3E767TJ91D` |
| RamJet | `PA3T0MQAGQ51` | `PA3T0MQAGQ51` |
| RocketFuel | `PA3EFBL4P7BU` | `PA3EFBL4P7BU` |

`live_deployments.yaml` is generated in a paused state and uses the same route IDs.

## Validation Results

| Check | Result | Notes |
|---|---|---|
| Branch check | Passed | `git rev-parse --abbrev-ref HEAD` -> `codex/live-integration` |
| Dependency sync | Passed | `uv sync --all-extras`; fixed cTrader/protobuf resolver conflict and added `openpyxl` for XLSX validators |
| Preflight | Gated/expected fail | `.venv/bin/python -m live_runner.preflight --config live_deployments.yaml --accounts-env .accounts.env` failed only for missing Alpaca credentials; deployment structure had no errors |
| Smoke help | Passed | `.venv/bin/python scripts/live_smoke.py --help` exposes `alpaca` and `ctrader` aliases |
| Alpaca smoke credential gate | Gated/expected fail | `.venv/bin/python scripts/live_smoke.py --mode alpaca --accounts-env .accounts.env --reports-dir /tmp/live-gated-smoke-reports` returned no matching credentials and did not write a repo artifact |
| Discord replay smoke | Passed | Safe parser/routing replay only; report saved under `reports/` |
| Routing / parsing / cTrader focused tests | Passed | 51 passed |
| Validators | Passed | 467 passed, 1 skipped, warnings only |
| Frontend build | Passed | `npm ci`; `npm run build`; Vite reported chunk-size warnings |
| Frontend dependency audit | Existing warnings | `npm ci` reported 4 vulnerabilities (2 low, 1 moderate, 1 high); no broad `npm audit fix` was applied because frontend dependencies were not changed |
| Changed Python syntax | Passed | `py_compile` for `scripts/live_smoke.py` and `tests/test_live_smoke.py` |

## Artifacts

- Safe smoke report: `reports/live_smoke_discord-replay_20260713T223118Z.json`
- Deployment runbook: `reports/live-deploy-runbook.md`
- Status report: `reports/live-integration-status.md`
- Generated paused deployment config: `live_deployments.yaml`

No `reports/live_daemon_*.log` artifact was produced because daemon startup is gated by missing demo/paper credentials.

## Open Items

1. Provide `.accounts.env` with the fields listed above.
2. Re-run Alpaca paper smoke with `uv run python scripts/live_smoke.py --mode alpaca --accounts-env .accounts.env --reports-dir reports`.
3. If cTrader demo credentials are present, run `uv run python scripts/live_smoke.py --mode ctrader --accounts-env .accounts.env --reports-dir reports`.
4. Start the daemon only after `resolve_alpaca_credentials` confirms each Alpaca account has `paper=True`.
5. Capture `reports/live_daemon_*.log` after Discord bot connection, paper order fill, and `reconcile_executions` verification.
