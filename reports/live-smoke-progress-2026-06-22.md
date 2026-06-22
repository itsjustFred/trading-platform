# Live Smoke Progress — 2026-06-22

Branch: `codex_rewrite`

## Order-free Alpaca Paper connectivity

Command:

```bash
.venv/bin/python scripts/live_smoke.py --mode account-check --accounts-env .accounts.env --timeout-seconds 45
```

Result:

| Account | Broker | Mode | Cash | Equity | Positions | Status |
|---|---|---|---:|---:|---:|---|
| Nitro | Alpaca | Paper | 99999.98 | 99999.98 | 0 | OK |
| RocketFuel | Alpaca | Paper | 99999.98 | 99999.98 | 0 | OK |
| RamJet | Alpaca | Paper | 99999.99 | 99999.99 | 0 | OK |
| 47644617 | cTrader | Demo | 100000.00 | 100000.00 | 0 | OK |

Notes:
- No orders were placed by this preflight.
- `alpaca-py` is available in `.venv`, not in system Python.
- cTrader discovery found 5 demo accounts; adapter connectivity was run in an isolated worker process to avoid Twisted reactor shutdown hangs.
- Evidence: `reports/live_smoke_account-check_20260622T231539Z.json`.

## Alpaca Paper BTC/USD roundtrip

Command:

```bash
.venv/bin/python scripts/live_smoke.py --mode alpaca-crypto-roundtrip --accounts-env .accounts.env --alpaca-symbol BTC/USD --alpaca-notional 10 --timeout-seconds 60
```

Result:

| Account | Symbol | Notional | Buy | Close | Remaining qty | Status |
|---|---|---:|---|---|---:|---|
| Nitro | BTC/USD | 10.00 | FILLED | FILLED | 0.0 | OK |
| RocketFuel | BTC/USD | 10.00 | FILLED | FILLED | 0.0 | OK |
| RamJet | BTC/USD | 10.00 | FILLED | FILLED | 0.0 | OK |

Notes:
- First dry attempt with `qty=0.0001` failed because Alpaca requires a minimum order cost of 10 USD.
- First notional attempt exposed a close-sizing edge: the close must use the actual broker position quantity, not the buy order's filled quantity.
- The runner now closes using the broker position quantity and verifies `remaining_qty=0.0`.
- Evidence: `reports/live_smoke_alpaca-crypto-roundtrip_20260622T231721Z.json`.
