# Strategy source

## Origin

- URL(s): `https://discord.com/channels/1230977283888648315/1506277517147897998/1506277517147897998` (from the EA's `#property link`)
- Author: `RobustifyTrading` (MQL5 copyright header)
- Date taken: `2026-06-24`

## Licence / redistribution status

- Status: `restricted`
- Notes: The EA header says `Copyright 2025, RobustifyTrading` and `All rights reserved`; no redistribution licence was found in the repository or at the linked Discord URL.

## Conversion notes

- Converted from `mt5/IntradayVolatilityBreakout.mq5` to `intraday_volatility_breakout.py`.
- MT5 tester XLSX audits live in `reference/`; the audit tooling lives in `scripts/intraday_volatility_breakout_audit/`.
- Conversion/parity details are documented in `README.md`, including MT5 SMA-ATR semantics, broker-clock day boundaries, D1 filler bars, and next-open exit fills.
