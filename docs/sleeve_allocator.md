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
  The loader cannot tell IS from OOS, so the CLI requires an explicit
  `--matrix-is-oos` acknowledgement; the report lists each sleeve's `source`
  and warns that matrix sleeves are not verified out-of-sample.
* Walk-forward result JSONs (`runner.walkforward` payloads) — only each fold's
  `oos_equity_curve` is read; train folds and in-sample stats are ignored by
  construction. Per-fold `oos_stats.total_trades` and the test window bounds
  feed time-aware trade-count eligibility.

`load_sleeve_returns(matrix_csv=..., matrix_summary=..., walkforward_paths=...,
trade_counts=...)` returns a `SleeveReturns`: a business-day-indexed DataFrame
of simple returns plus per-sleeve `SleeveMetadata`.

## Calendar contract

`trade_counts` overrides the loaded `total_trades` — and, for walk-forward
sleeves, replaces the fold `trade_periods` so the override is the single
authoritative count basis.

All sleeves share one Monday–Friday calendar. Saturday/Sunday observations
compound into the next business day (Monday), so weekend P&L of 24/7 sleeves
lands on Monday instead of being dropped. Business days a sleeve did not
observe stay `NaN` — never zero-filled (the #229 failure mode). Applying
weights treats an unobserved sleeve-day as flat; estimation statistics stay
pairwise-complete. Non-weekend gaps (holidays, feed outages) are NOT
compounded into a single return: walk-forward fold returns leave the first
day after a gap unobserved when the gap contains a weekday that was not a
US equity holiday (mirroring `runner.walkforward`), so a multi-day move is
never booked as a one-day return. Matrix `daily_return` values are taken
per observation as recorded.

## Eligibility

`filter_eligible_sleeves(sleeve_returns, config, as_of=...)` admits a sleeve
only with: history span ≥ `min_history_days` (default 730 ≈ 2y),
`total_trades`/period trades ≥ `min_trades` (default 100), and no ruin
(minimum running equity ratio > `ruin_equity_floor`, default 0.01). Missing
evidence rejects (fail closed). `as_of` is exclusive — the harness evaluates
eligibility at each rebalance `t` on data strictly before `t`, and a
`trade_periods` window counts only when its END is strictly before `t`.
`total_trades`-only sleeves (matrix runs) have no per-period timing, so under
`as_of` they are rejected with `trades_unknown_as_of` by default — the
count includes trades after the decision date. `--allow-full-sample-trade-counts`
opts in; the `full_sample` basis stays disclosed in the report either way.

## Harness

`walk_forward_allocation(sleeve_returns, AllocationConfig(...))` rebalances on
the last observed business day of each month (maintainer decision: monthly).
The rebalance labelled "month-end day d" is decided on data strictly before
`d` — effectively the close of `d−1` — and its weights earn `d`'s return.
At rebalance `t`, weights are estimated on the trailing `lookback_days`
(default 252) business days before `t` and applied until the next rebalance.
Eligibility at `t` additionally requires at least `min_window_observations`
(default 30) non-NaN returns inside that estimation window — a sleeve whose
series ended before the window (e.g. a strategy that stopped trading) is
rejected with `insufficient_window_observations`, so `equal_weight` and
`inverse_vol` always see the same universe.

Weight semantics within a segment: weights are a CONSTANT daily mix — the
portfolio is notionally rebalanced back to target every day (daily
rebalancing to the budget, not buy-and-hold drift). A sleeve whose series
ends mid-segment contributes 0 return from its last observation onward,
i.e. its weight sits as cash; sleeves with no observation on a business day
are treated as flat for that day only.

When NO sleeve is eligible at a rebalance the whole budget goes to cash
(all weights 0, flat returns, `cash=true` on the record) — the previous
allocation is never carried onto sleeves the filter just rejected. When a
solver cannot estimate (e.g. inverse-vol with too little data), the harness
carries only the still-eligible part of the previous weights, renormalised;
with nothing to carry it falls back to 1/N over the eligible set
(`fallback_equal_weight=true`, distinct from `carried`).

The chained OOS portfolio curve per objective is the only performance number
reported. The report (`result.report`) always contains `equal_weight` and
`inverse_vol` under `objectives`, with metrics (total/annualised return,
volatility, Sharpe, max drawdown), rebalance counts (carried /
fallback-equal-weight / cash), per-sleeve `sources`, and average turnover.
Metrics annualise at the measured observation density of each chained
curve (#349 convention: observations per calendar year), falling back to
sqrt(252) only when the density cannot be measured — so a calendar-filled
curve annualises at ~365.25 and a business-day curve at ~261, not a fixed
252.

## CLI

```
python -m runner.sleeve_allocator \
    --matrix-csv data/backtest_matrix/matrix_daily_equity.csv \
    --matrix-is-oos \
    --matrix-summary data/backtest_matrix/matrix_summary.json \
    --walkforward data/walkforward_results/wf_xxx.json \
    --allow-full-sample-trade-counts \
    --output data/portfolios/sleeve_allocation_report.json
```

`--matrix-is-oos` is required whenever `--matrix-csv` is given: matrix rows
are full-window backtests and the loader cannot verify they are
out-of-sample. `--allow-full-sample-trade-counts` admits `total_trades`-only
sleeves at point-in-time rebalances on a future-inclusive count; it is off
by default.

Phase 1 produces research artifacts only — nothing here is wired to live
deployments.
