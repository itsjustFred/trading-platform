# Sleeve allocator (issue #307, phase 1)

`runner/sleeve_allocator/` combines per-sleeve out-of-sample daily returns —
one sleeve = strategy × asset × timeframe at a fixed base risk — into a
portfolio allocation under ONE common risk budget (weights are non-negative
and sum to 1).

Phase 1 scope: loader, eligibility filter, the two required baselines
(1/N and inverse-vol), and a monthly walk-forward harness whose report always
shows both baselines. ERC/HRP objectives, shrinkage, caps, PBO/DSR and live
wiring are later phases; there is no Optuna tuning of individual sleeve
weights (maintainer decision: only a few allocator hyperparameters may ever
be tuned, walk-forward on OOS curves).

## Inputs

* `matrix_daily_equity.csv` — full-window backtest daily equity/returns.
  Callers must only feed OOS windows; the loader cannot tell IS from OOS.
* Walk-forward result JSONs (`runner.walkforward` payloads) — only each fold's
  `oos_equity_curve` is read; train folds and in-sample stats are ignored by
  construction. Per-fold `oos_stats.total_trades` and the test window bounds
  feed time-aware trade-count eligibility.

`load_sleeve_returns(matrix_csv=..., matrix_summary=..., walkforward_paths=...,
trade_counts=...)` returns a `SleeveReturns`: a business-day-indexed DataFrame
of simple returns plus per-sleeve `SleeveMetadata`.

## Calendar contract

All sleeves share one Monday–Friday calendar. Saturday/Sunday observations
compound into the next business day (Monday), so weekend P&L of 24/7 sleeves
lands on Monday instead of being dropped. Business days a sleeve did not
observe stay `NaN` — never zero-filled (the #229 failure mode). Applying
weights treats an unobserved sleeve-day as flat; estimation statistics stay
pairwise-complete.

## Eligibility

`filter_eligible_sleeves(sleeve_returns, config, as_of=...)` admits a sleeve
only with: history span ≥ `min_history_days` (default 504 ≈ 2y),
`total_trades`/period trades ≥ `min_trades` (default 100), and no ruin
(minimum running equity ratio > `ruin_equity_floor`, default 0.01). Missing
evidence rejects (fail closed). `as_of` is exclusive — the harness evaluates
eligibility at each rebalance `t` on data strictly before `t`. Trade counts
from `trade_periods` are point-in-time; `total_trades`-only sleeves use a
full-sample count, disclosed via `trade_count_basis="full_sample"` in the
report notes.

## Harness

`walk_forward_allocation(sleeve_returns, AllocationConfig(...))` rebalances on
the last observed business day of each month (maintainer decision: monthly).
At rebalance `t`, weights are estimated on the trailing `lookback_days`
(default 252) business days before `t` and applied until the next rebalance.
The chained OOS portfolio curve per objective is the only performance number
reported. The report (`result.report`) always contains `equal_weight` and
`inverse_vol` under `objectives`, with metrics (total/annualised return,
volatility, Sharpe on the platform sqrt(252) convention, max drawdown),
rebalance count, carried-weight count and average turnover.

## CLI

```
python -m runner.sleeve_allocator \
    --matrix-csv data/backtest_matrix/matrix_daily_equity.csv \
    --matrix-summary data/backtest_matrix/matrix_summary.json \
    --walkforward data/walkforward_results/wf_xxx.json \
    --output data/portfolios/sleeve_allocation_report.json
```

Phase 1 produces research artifacts only — nothing here is wired to live
deployments.
