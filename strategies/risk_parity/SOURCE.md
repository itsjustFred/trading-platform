# Strategy source

## Origin

- URL(s):
  - `https://github.com/desenyon/quantconnect-strategies/blob/main/risk_parity_strategy.py` (platform strategy source URL and frozen QC reference algorithm)
  - `https://github.com/desenyon/RiskParityAlpha` (linked from `STRATEGY_META`; returned 404 when checked on 2026-11-28)
- Author: `desenyon` (GitHub handle)
- Date taken: `2026-03-08` for the platform strategy conversion; `2026-06-22` for the QC reference algorithm and PDF report; `2026-06-24` for the two reference JSON files

## Licence / redistribution status

- Status: `unknown`
- Notes: No `LICENSE` was found in `desenyon/quantconnect-strategies` (GitHub API reported `license: null` on 2026-11-28). `desenyon/RiskParityAlpha` was not reachable, so no licence could be verified there either.

## Conversion notes

- `risk_parity.py` is a single-instrument volatility-regime long/flat strategy, not the multi-asset risk-parity allocator; the `risk_parity` slug is retained for backtest-artifact compatibility.
- `reference/risk_parity_strategy.py` is the QuantConnect Classic Algorithm used as the parity reference. It differs from the current upstream file only in whitespace, the final newline and one rename (`n_obs` → `_`).
- `reference/risk_parity_report_quantconnect.pdf` is the frozen QuantConnect report.
- `reference/risk_parity_reference_vref.json` holds the stats read from that PDF.
- `reference/risk_parity_reference.json` is **not** external ground truth: its top-level stats come from the platform's Python reimplementation (`scripts/run_risk_parity_backtest.py`, yfinance data) and are regenerated (see its `raw_metadata.note`). Only its `pdf_stats` and `lean_ground_truth` blocks come from QuantConnect.
- The Python reimplementation used for parity lives in `scripts/run_risk_parity_backtest.py`.
