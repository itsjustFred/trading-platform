# Risk Parity Backtest – Iteration Report

## Setup

- **Source strategy**: `strategy-comparisons/risk_parity/risk_parity_strategy.py` (QuantConnect `QCAlgorithm`)
- **Reference report**: `strategy-comparisons/risk_parity/risk_parity_report_quantconnect.pdf`
- **Data period**: 2016-01-01 – 2024-12-31 (same as the QC algorithm)
- **Initial cash**: $1,000,000

## LEAN local execution attempt

1. Installed `lean==1.0.226` in the project venv.
2. `lean init` requires QuantConnect user-id / API token; no credentials were available.
3. `lean project-create strategies/risk_parity --language python` succeeded and produced a LEAN project skeleton.
4. `lean backtest strategies/risk_parity` pulled `quantconnect/lean:latest` but failed with `No module named 'pkg_resources'` inside the container.
5. No local daily equity data for `SPY`, `QQQ`, `TLT`, etc. exists in `/mnt/block-volume-data`.

**Conclusion**: A native LEAN backtest is blocked by missing credentials, missing local equity data, and a container dependency issue. As an agreed fallback (Ferment success criteria explicitly allow summary comparison when trade extraction is not feasible), the strategy was reimplemented in pure Python using `yfinance` daily adjusted close prices.

## Python reimplementation

- **Script**: `scripts/run_risk_parity_backtest.py`
- Mirrors the QC logic:
  - Universe: SPY, QQQ, EFA, EEM, TLT, IEF, LQD, TIP, GLD, DBC, VNQ
  - Monthly rebalancing on first trading day of the month
  - 120-day covariance (Ledoit-Wolf shrinkage 0.2, annualized ×252)
  - Risk-parity weights via SLSQP optimization with min/max bounds
  - Volatility scaling to 10% target volatility, capped at 1.5× leverage
  - Tail-risk hedge: reduce equity/commodity/real-asset exposure when SPY is below its 200-day SMA × 0.98

## Outputs

- `data/backtests/risk_parity_yfinance.trades.json`
- `data/backtests/risk_parity_yfinance_vref.json`
- `data/backtests/risk_parity_yfinance_summary_vref.json` (stats only, no trades)
- `data/backtests/risk_parity_reference_vref.json` (PDF summary stats)
- `data/backtests/risk_parity_yfinance_report.md`
- `data/backtests/risk_parity_yfinance_summary_report.md`

## Reference stats from PDF (OCR)

| Metric | Value |
|--------|-------|
| Runtime Days | 3287 |
| Drawdown | 17.7% |
| Turnover | 1% |
| Probabilistic SR | 9% |
| CAGR | 7.6% |
| Capacity | $1B |
| Trades per Day | 0.4 |
| Sharpe Ratio | 0.4 |
| Sortino Ratio | 0.4 |
| Information Ratio | 0.6 |

## Platform stats (Python reimplementation)

| Metric | Value |
|--------|-------|
| Total return | 66.9% |
| CAGR | 5.87% |
| Annualized volatility | 11.76% |
| Sharpe Ratio | 0.54 |
| Sortino Ratio | 0.59 |
| Max drawdown | -25.8% |
| Net profit | $126,183 |
| Win rate | 57.5% |
| Total trades | 240 |

## Key deviations vs. PDF

- **CAGR**: 5.87% vs. 7.6% (~22% relative)
- **Max drawdown**: -25.8% vs. -17.7% (~46% relative)
- **Sharpe**: 0.54 vs. 0.4 (~35% relative)
- **Sortino**: 0.59 vs. 0.4 (~48% relative)
- **Trade count/turnover**: 240 trades vs. ~1,315 implied by 0.4 trades/day (PDF also reports 1% turnover)

## Likely causes of deviation

1. **Data source**: `yfinance` adjusted close vs. QuantConnect’s raw/dividend/split-adjusted daily data; different corporate-action treatments and possibly different data vendors.
2. **Transaction model**: The Python reimplementation applies a 2% weight-change threshold to reduce churn but does not model slippage, commissions, or market-impact; QC likely models these.
3. **Rebalance timing**: The reimplementation trades on the first trading day close of the month; QC schedules 30 minutes after SPY market open.
4. **Covariance / optimization**: Both use the same high-level recipe, but numerical details (SLSQP iterations, handling of non-convergence, shrinking target) can differ.
5. **Tail-risk hedge**: The 200-day SMA proxy uses adjusted closes; regime thresholds and hedge multipliers are approximations.

## Next steps (out of scope for this Ferment)

- Run the original `risk_parity_strategy.py` in QuantConnect Cloud or a correctly configured local LEAN instance with official data.
- Extract the actual monthly return series from QuantConnect to align the Python reimplementation.
- Add transaction-cost assumptions and rebalance-timing details to narrow the gap.

## Decision

Given the missing LEAN prerequisites and the fact that the PDF contains only summary statistics, the Ferment accepts the documented summary-level comparison as the validation result. The validation framework was extended to support references/platform results with empty trades but populated `stats` (see `validation/reporting/json_report.py`).
