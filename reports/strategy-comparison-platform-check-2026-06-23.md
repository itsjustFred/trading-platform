# Strategy Comparison Platform Check — 2026-06-23

## Scope

Compared the platform implementation against the local references in
`strategy-comparisons/`:

- `intraday_volatility_breakout/EURUSD_H1.html`
- `intraday_volatility_breakout/SP500_M1.html`
- `intraday_volatility_breakout/US100_M15.html`
- `risk_parity/risk_parity_report_quantconnect.pdf`

## Findings

1. Runner auto-download incorrectly routed `SP500`, `NAS100` and `US100` to the
   US-stock/yfinance-style path. These are Dukascopy index-CFD aliases and must
   use Dukascopy.
2. Platform `volatility_breakout` was materially different from the MT5
   reference:
   - old platform version was effectively long-only,
   - reference uses long and short breakouts,
   - reference default `ATR_Multiplier` is `0.25`, not `0.3`,
   - reference uses `Stop_ATR_Multiplier=0.25`,
   - reference restricts entries to `16:30` through `18:05` and exits at `22:55`,
   - reference includes a short/long ATR regime filter.

## Fixes Applied

- Added Dukascopy aliases:
  - `SP500` -> `usa500idxusd`
  - `NAS100` -> `usatechidxusd`
  - `US100` -> `usatechidxusd`
- Updated runner/viewer auto-download source selection so these aliases use
  Dukascopy instead of yfinance/HuggingFace/Alpaca stock routing.
- Aligned `strategies/volatility_breakout/volatility_breakout.py` with the
  reference defaults and mechanics where the platform engine supports them:
  long/short direction, ATR breakout bands, ATR stop, ATR regime filter and
  trading-hour gates.

## Platform Runs

Before strategy alignment:

| Reference | Platform file | Trades | PnL | Profit Factor | Sharpe |
|---|---:|---:|---:|---:|---:|
| EURUSD H1 | `reports/strategy_comparison_backtest_EURUSD_H1_20260623.trades.json` | 217 | -267.96 | 0.7806 | -1.2609 |
| SP500 M1 | `reports/strategy_comparison_backtest_SP500_M1_20260623.trades.json` | 79 | 6825.47 | 17.5496 | 0.2410 |
| US100 M15 | `reports/strategy_comparison_backtest_US100_M15_20260623.trades.json` | 34 | 10655.24 | 11.5610 | 0.2924 |
| Risk Parity ETFs | `reports/strategy_comparison_backtest_risk_parity_20260623.trades.json` | 67 | 108851.57 | 2.4986 | 0.3900 |

After strategy alignment:

| Reference | Platform file | Trades | PnL | Profit Factor | Sharpe |
|---|---:|---:|---:|---:|---:|
| EURUSD H1 | `reports/strategy_comparison_backtest_EURUSD_H1_after_align_20260623.trades.json` | 71 | 11310.19 | 1.7590 | 0.3453 |
| SP500 M1 | `reports/strategy_comparison_backtest_SP500_M1_after_align_20260623.trades.json` | 239 | -4800.04 | 0.7227 | -0.2174 |
| US100 M15 | `reports/strategy_comparison_backtest_US100_M15_after_align_20260623.trades.json` | 319 | -5598.22 | 0.8072 | -0.4270 |

## Remaining Differences

- The platform engine still models execution and sizing differently from MT5:
  reference reports use fixed risk amount (`Fixed_Risk_Amount=500.0`) and MT5
  symbol contract semantics; the platform uses its own risk-percent sizing.
- The MT5 reports show 15-minute confirmation logic. The platform strategy now
  accepts the same reference parameter names, but does not yet resample a
  separate confirmation timeframe inside `generate_signals`.
- SP500 M1 and US100 M15 remain far from the reference performance after logic alignment.
  This points to remaining execution/modeling differences: MT5 contract value,
  timezone/session semantics, fixed-risk sizing, spread/commission and
  confirmation-timeframe handling.
- The QuantConnect Risk Parity PDF is not text-extractable with `pdftotext`;
  the platform run validates the current simplified ETF strategy, not an exact
  QuantConnect portfolio-weighting reproduction.
