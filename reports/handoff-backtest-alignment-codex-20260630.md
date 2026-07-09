# Handoff: Backtest Alignment Loop - 2026-06-30

## Mission

Continue in the existing worktree and branch:

- Worktree: `/home/ubuntu/Desktop/trading-platform-codex_copy`
- Branch: `codex_30-06-2026`
- Latest stats-audit commit at handoff time: `67684f4 Fix intraday stats annualization audit`

The next Codex agent should continue the cross-platform validation loop until platform backtests match MT5/QuantConnect as closely as possible. Small residual differences may remain because the platform uses Dukascopy website data while the MT5 references use Dukascopy MT5 data.

## Preserve Existing Work

Do not reset, checkout, clean, or revert the worktree. There is an earlier architecture slice still uncommitted and intentionally preserved:

- Modified frontend/API/live/matrix/optimizer files:
  - `frontend/src/api/client.ts`
  - `frontend/src/api/types.ts`
  - `frontend/src/components/OptimizeModal.tsx`
  - `frontend/src/pages/LiveHub.tsx`
  - `live_runner/daemon.py`
  - `runner/backtest_matrix.py`
  - `runner/optimizer.py`
  - `shared/database.py`
  - `tests/test_live_api.py`
  - `tests/test_live_daemon.py`
  - `tests/test_optimizer.py`
  - `viewer/main.py`
- Untracked architecture files:
  - `live_runner/ctrader_deployment.py`
  - `live_runner/stockkit_routing.py`
  - `runner/risk_parity_optimizer.py`
  - `tests/test_backtest_matrix_runner.py`
  - `tests/test_ctrader_deployment.py`
  - `tests/test_risk_parity_optimizer.py`

Work with these changes in place. If a future fix must touch any of those files, inspect the local diff first and preserve the existing intent.

## Current Validation State

The stats-calculation bug class has already been fixed in `runner/stats.py`:

- Sharpe, Sortino, probabilistic Sharpe ratio, and p-value now use daily close-to-close equity returns with `252` periods/year.
- CAGR and total return still use the full elapsed backtest period.
- The exact run artifact `native-intraday_volatility_breakout-20260625-001317` was not present in this worktree, so the fix targets the underlying intraday annualization anomaly.

Current comparison artifacts:

- Comparison script: `scripts/compare_strategy_metrics.py`
- Comparison module: `validation/statistics/cross_platform.py`
- Generated matrix: `reports/strategy_metric_comparison_matrix_20260630.json`
- Ongoing progress report: `reports/codex_30-06-2026.md`
- Strategy references:
  - `strategy-comparisons/intraday_volatility_breakout/SP500_M1.xlsx`
  - `strategy-comparisons/intraday_volatility_breakout/EURUSD_H1.xlsx`
  - `strategy-comparisons/intraday_volatility_breakout/NAS100_M15.xlsx`
  - `strategy-comparisons/risk_parity/risk_parity_reference.json`
  - `strategy-comparisons/risk_parity/risk_parity_reference_vref.json`
- Current platform backtest artifacts:
  - `reports/strategy_comparison_backtest_SP500_M1_after_align_20260623.trades.json`
  - `reports/strategy_comparison_backtest_EURUSD_H1_after_align_20260623.trades.json`
  - `reports/strategy_comparison_backtest_US100_M15_after_align_20260623.trades.json`
  - `reports/strategy_comparison_backtest_risk_parity_20260623.trades.json`

## Current Metric Deltas

All platform values below are recomputed with the updated canonical stats calculator.

| Case | Metric | Reference | Platform | Absolute Delta |
|---|---:|---:|---:|---:|
| SP500 M1 MT5 | Trade Count | 512 | 239 | -273 |
| SP500 M1 MT5 | Win Rate | 44.92% | 20.50% | -24.42 pp |
| SP500 M1 MT5 | Net Profit | 39255.92 | -4800.04 | -44055.96 |
| SP500 M1 MT5 | CAGR | 3.99% | -10.44% | -14.42 pp |
| SP500 M1 MT5 | Sharpe | 4.595921 | -0.2183 | -4.814221 |
| EURUSD H1 MT5 | Trade Count | 492 | 71 | -421 |
| EURUSD H1 MT5 | Win Rate | 45.12% | 39.44% | -5.68 pp |
| EURUSD H1 MT5 | Net Profit | 21868.81 | 11310.19 | -10558.62 |
| EURUSD H1 MT5 | CAGR | 2.36% | 9.82% | +7.46 pp |
| EURUSD H1 MT5 | Sharpe | 2.711472 | 0.2102 | -2.501272 |
| NAS100 M15 MT5 | Trade Count | 597 | 319 | -278 |
| NAS100 M15 MT5 | Win Rate | 43.55% | 27.27% | -16.28 pp |
| NAS100 M15 MT5 | Net Profit | 43791.14 | -5598.22 | -49389.36 |
| NAS100 M15 MT5 | CAGR | 4.38% | -12.39% | -16.77 pp |
| NAS100 M15 MT5 | Sharpe | 4.677290 | -0.4031 | -5.080390 |
| Risk Parity QC JSON | Trade Count | 240 | 67 | -173 |
| Risk Parity QC JSON | Win Rate | 57.50% | 46.27% | -11.23 pp |
| Risk Parity QC JSON | Net Profit | 126183.49 | 108851.57 | -17331.92 |
| Risk Parity QC JSON | CAGR | 5.87% | 9.98% | +4.12 pp |
| Risk Parity QC JSON | Sharpe | 0.544687 | 0.2106 | -0.334087 |

## Required Loop

Repeat this loop until the MT5/QuantConnect metrics match or nearly match:

1. Run/refresh platform backtests for every reference case.
2. Regenerate the comparison matrix:
   - `python scripts/compare_strategy_metrics.py --output reports/strategy_metric_comparison_matrix_20260630.json`
3. Compare:
   - Trade Count
   - Win Rate
   - Net Profit
   - CAGR
   - Sharpe Ratio
4. Investigate the largest deltas first.
5. Determine whether each delta comes from:
   - Platform execution model: fills, slippage, spread, commission, pip/point value, contract sizing, margin, order timing, stop/target behavior.
   - Risk model: fixed risk, position sizing, leverage, capital base, compounding, currency conversion.
   - Data model: timezone alignment, session windows, missing bars, bid/ask vs mid/close, Dukascopy website data vs MT5 data.
   - Strategy implementation: indicator windowing, breakout definitions, signal timing, entry/exit rules, stop-loss/take-profit math, rebalance timing, covariance estimator, universe selection.
   - Statistics model: annualization, risk-free-rate convention, equity curve construction, trade aggregation.
6. Implement the smallest correct fix.
7. Add or update focused tests for the fixed behavior.
8. Rerun affected backtests and regenerate the matrix.
9. Update `reports/codex_30-06-2026.md` with:
   - The exact delta investigated.
   - Root cause.
   - Code changed.
   - Backtests rerun.
   - Before/after metric table.
   - Remaining unexplained deltas.
10. Commit atomically after each coherent fix/report update.

## Investigation Priorities

Start with trade-count mismatches because they usually reveal strategy or execution-model divergence before profit metrics can be trusted.

Recommended order:

1. `EURUSD_H1`: Platform has only `71` trades vs MT5 `492`.
   - Check strategy signal frequency, session/date range, symbol mapping, H1 bar construction, timezone, and whether only a subset of signals are being persisted/executed.
2. `SP500_M1`: Platform has `239` trades vs MT5 `512` and profit sign is inverted.
   - Check contract sizing, index point value, spread/commission assumptions, M1 data alignment, stop/target behavior, and long/short direction handling.
3. `NAS100_M15`: Platform has `319` trades vs MT5 `597` and profit sign is inverted.
   - Check US100/NAS100 symbol aliasing, point value, trading sessions, and stop/target handling.
4. `risk_parity_quantconnect`: Platform net profit is closer but trade count and Sharpe still differ.
   - Check rebalance cadence, trade aggregation semantics, weight drift thresholds, covariance estimator, universe, and QuantConnect risk-free-rate convention.

## Verification Commands Already Used

Known-good baseline after the stats audit:

- `pytest -q tests/test_stats.py tests/validation/test_cross_platform_metrics.py tests/validation/test_mt5_xlsx_importer.py`
  - Result: `60 passed`
- `pytest -q tests/test_optimizer.py tests/test_runner_cli.py tests/test_backtest_grid.py tests/test_stats.py tests/validation/test_cross_platform_metrics.py`
  - Result: `33 passed`
- `python -m py_compile runner/stats.py validation/statistics/cross_platform.py scripts/compare_strategy_metrics.py`
  - Result: passed
- `git diff --check`
  - Result: passed

For each loop iteration, run at least the directly affected tests plus the comparison script. Before committing, run `git diff --check`.

## Acceptance Target

The loop is complete when:

- Trade counts are identical or the remaining difference is explicitly traced to data-source differences or documented MT5-vs-platform trade aggregation semantics.
- Win rate, net profit, CAGR, and Sharpe are within a documented tolerance for each case.
- Remaining differences are small, explained, and recorded in `reports/codex_30-06-2026.md`.
- New fixes have focused regression tests.
- Backtest artifacts and `reports/strategy_metric_comparison_matrix_20260630.json` reflect the latest run.

