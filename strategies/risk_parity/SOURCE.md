# Strategy source

## Origin

- URL(s):
  - `https://github.com/desenyon/quantconnect-strategies/blob/main/risk_parity_strategy.py` (platform strategy source URL and frozen QC reference algorithm)
  - `https://github.com/desenyon/RiskParityAlpha` (linked from `STRATEGY_META`; returned 404 when checked on 2026-11-28)
- Author: `desenyon` (GitHub handle; no personal name found)
- Date taken: `2026-03-08` for the platform strategy conversion; `2026-06-22` for the QC reference algorithm/report artifacts

## Licence / redistribution status

- Status: `unknown`
- Notes: No `LICENSE` was found in `desenyon/quantconnect-strategies` (GitHub API reported `license: null` on 2026-11-28). `desenyon/RiskParityAlpha` was not reachable, so no licence could be verified there either.

## Conversion notes

- `risk_parity.py` is a single-instrument volatility-regime long/flat strategy, not the multi-asset risk-parity allocator; the `risk_parity` slug is retained for backtest-artifact compatibility.
- `reference/risk_parity_strategy.py` is the frozen QuantConnect Classic Algorithm used as the parity reference.
- `reference/risk_parity_report_quantconnect.pdf` and the `risk_parity_reference*.json` files are frozen QC report/reference outputs.
- The Python reimplementation used for parity lives in `scripts/run_risk_parity_backtest.py`.
