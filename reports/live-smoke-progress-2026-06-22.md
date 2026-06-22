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

## cTrader Demo EURUSD roundtrip

Command:

```bash
.venv/bin/python scripts/live_smoke.py --mode ctrader-demo-roundtrip --accounts-env .accounts.env --ctrader-symbol EURUSD --ctrader-volume 100000 --timeout-seconds 60
```

Result:

| Account | Symbol | Volume | Broker order | Position | Close | Remaining position | Status |
|---|---|---:|---|---|---|---|---|
| 47644617 | EURUSD | 100000 | 26549660 | 37178928 | ProtoOAExecutionEvent | none | OK |

Notes:
- First cTrader order attempt with `volume=1000` was rejected by the real broker as `TRADING_BAD_VOLUME`; the broker reported `Order volume = 10.00` and minimum `1000.00`.
- The successful EURUSD smoke used `volume=100000`, which corresponds to the broker minimum in this Open API volume scale.
- The smoke runner closes cTrader positions with `ProtoOAClosePositionReq` rather than an opposite market order, so hedged demo accounts do not keep an offsetting position open.
- Evidence: `reports/live_smoke_ctrader-demo-roundtrip_20260622T232608Z.json`.

## Discord parser-to-order replay

Command:

```bash
.venv/bin/python scripts/live_smoke.py --mode discord-replay --accounts-env .accounts.env --timeout-seconds 10
```

Result:

| Check | Channel | Symbol | Action | Qty | Broker order | Status |
|---|---|---|---|---:|---|---|
| discord-replay-routing | discord-smoke-channel | AAPL | buy | 262.4671916010497 | discord-smoke-order | OK |

Notes:
- This is a controlled replay smoke, not a long-running Discord gateway listener.
- It uses the real Discord message parser, channel metadata, deployment symbol filter, `_trade_signal_to_live_signal`, `submit_order_with_risk`, and a fake broker adapter.
- The fake adapter prevents accidental live/Paper orders while still proving parser-to-submission routing and risk sizing.
- Evidence: `reports/live_smoke_discord-replay_20260622T233019Z.json`.
