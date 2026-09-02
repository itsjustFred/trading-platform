# Money-Path Semantics — Cleanup 16-D1 Decision Records

Baseline: `6caf461` (tag `cleanup-baseline-2026-08-26`). Sub-issue #39.
Each record names the finding from the parent issue (#16), the behavior change,
and the conformance test that pins it.

## D1 — Signal-row merge is element-wise (F20)

**Before:** `TradeEngine.run` merged the OHLCV bar with the signal row via
`pd.concat([row, signal_row])` — a *vertical* stack producing a row of length
`len(row) + len(signal_row)` with duplicated labels. Any signal/indicator named
like an OHLCV column (`close`, `high`, `low`, `open`) made the OHLCV lookups
ambiguous; a colliding `close` crashed `_create_order` with a hard
`TypeError: float() argument must be … not 'Series'` (worse than the reported
silent corruption).

**Decision:** the merge overlays the signal values onto the OHLCV row
element-wise, aligned by label (`row.combine_first(signal_row)`). OHLCV labels
keep their authority; signal-only labels (direction, stop levels, meta
columns) are added. A signal column named like an OHLCV column cannot shadow
the price data.

**Pinned by:** `tests/test_engine_behavior.py::TestSignalMergeElementWise`.

## D2 — Sizing capital excludes unrealized PnL (F21)

**Before:** `TradeEngine._calculate_position_size` sized from
`self.equity = initial + realized + unrealized` — open positions' paper profit
compounded into the next entry's quantity, and paper drawdown shrank it.

**Decision:** sizing draws on `_sizing_equity = initial_capital + realized_pnl`
(realized cash only, updated in `_close_partial`). `self.equity` remains the
mark-to-market *reporting* equity for the curve. Sizing is therefore
indifferent to open-trade fluctuations: unrealized profit is not spendable,
and unrealized loss does not shrink new positions.

**Pinned by:** `tests/test_engine_behavior.py::TestSizingEquityExcludesUnrealizedPnl`.

## D3 — Engine risk policy is not strategy-configurable (F22)

**Before:** `TradeEngine` read `max_positions` out of
`config.parameters` — any strategy parameter silently rewrote engine-level
risk policy.

**Decision:** the concurrent-position limit is the module constant
`TRADE_ENGINE_MAX_POSITIONS = 5`. Strategy `parameters` can no longer reach
risk policy; engine-level knobs are `BacktestConfig` risk fields
(`risk_per_trade_pct`, `max_position_pct`, `max_leverage`).

**Pinned by:** `tests/test_engine_behavior.py::TestMaxPositionsPolicy`.

## D4 — Every order is sized exactly once, at fill (F23)

**Before:** MARKET orders were sized at creation (signal-bar close) and
re-sized at the next-bar-open fill; LIMIT/STOP/STOP_LIMIT orders kept their
creation-price estimate forever.

**Decision (documented policy):** orders size once, at the price they
actually fill. MARKET re-sizes at the next-bar open (creation sizing is only
the pending estimate); LIMIT/STOP/STOP_LIMIT re-size from their trigger price
at fill. Quantities across order types are now computed from the same rule:
quantity = sizing rule evaluated at the fill price.

**Pinned by:** `tests/test_engine_behavior.py::TestOrderTypeSizingSemantics`.

## D5 — Stats definitions are environment-independent (F17)

**Before:** with quantstats installed, `total_return` was the compounded
product of resampled daily returns and Calmar used that annualization; without
it, `total_return` was the equity-endpoint ratio and Calmar used an
inferred-frequency annualization. The same backtest JSON published different
Sharpe/Calmar/total_return on different machines.

**Decision:** one definition per metric on both paths — `total_return` is the
equity-endpoint ratio (`E_end / E_start − 1`); `calmar_ratio` = elapsed-time
CAGR / max drawdown; Sharpe/Sortino on daily-resampled returns annualized with
252 periods (quantstats when available, manual fallback otherwise). MaxDD and
duration are computed once, outside the branches.

**Pinned by:** `tests/test_stats.py` (existing suite, green on both paths) —
the QS and fallback formulas now provably share the total_return/Calmar
computation.

## D6 — Risk-parity covariance uses asset prices first (F30)

**Before:** `_run_risk_parity_covariance_optimization` preferred
`_asset_returns_frame_from_base_result` (per-symbol trade-PnL pseudo-equity)
and only fell back to market data when fewer than two symbols had trades —
so with ≥2 traded symbols the market-data path never ran. Correlations
measured strategy-trade overlap, not asset co-movement, yet drove covariance
weights and universe selection.

**Decision:** the precedence is inverted. Market-data returns (asset prices)
are the primary covariance input; the trade-PnL pseudo-equity frame is the
explicit last-resort fallback when no market data is loadable, and its use is
visible in the payload (`risk_parity` section's return source is whichever
frame was selected).

**Pinned by:** `tests/test_optimizer_validation.py::TestRiskParityCovarianceSource`.

## D7 — No-trade trials cannot win the optimization (AC 2)

**Before:** no-trade trials returned the penalty `0.0` under
`direction="maximize"` — *beating* every honest trial with negative Sharpe,
so Optuna was steered toward parameter regions that avoid trading. An
all-failing study also raised an opaque Optuna error at `study.best_trial`.

**Decision:** a no-trade trial scores strictly below the worst completed
trading trial so far (margin −1.0; a large finite negative constant
`_NO_TRADE_PENALTY = −1e9` applies to the first trial) and keeps the explicit
`penalty_applied=True` marker. An empty study raises a plain
`RuntimeError("Optimization produced no completed trials …")`. Selecting a
no-trade trial is therefore impossible without the selection being an
explicit, visible penalty event.

**Pinned by:** `tests/test_optimizer_validation.py::TestNoTradeTrialsCannotWin`.

## D8 — Portfolio Sharpe is annualized (F36)

**Before:** `portfolio_optimizer._portfolio_sharpe_from_weights` returned the
raw per-point mean/std — not annualized, incomparable to `runner.stats`'s
Sharpe though both surface under the name "sharpe".

**Decision:** per-observation mean/std scaled by
`sqrt(periods_per_year)`, with the cadence inferred from the aligned equity
timestamps (median spacing; calendar-daily curves map to the 252
trading-period convention; sub-daily scales proportionally; fallback 252).
Both `sharpe` and `oos_sharpe` in the payload are annualized.

**Pinned by:** `tests/test_portfolio_sharpe_annualization.py::TestPortfolioSharpeAnnualized`.

## D9 — Replay sizing marks open positions to market (F39)

**Before:** `SignalReplayAdapter` sized new entries from
`initial_capital + realized_pnl` (floor $1) — unrealized drawdown of open
positions was invisible to sizing, so entries opened *during* a drawdown were
sized as if nothing had been lost; sizing-time and reporting-time equity also
diverged.

**Decision:** sizing balance = `initial_capital + realized_pnl + unrealized
MTM PnL of open positions` (marked at the last bar ≤ the signal's fill time),
floored at $1 as before. The sizing path and the reported equity path now
measure the same capital.

**Pinned by:** `tests/test_portfolio_sharpe_annualization.py::TestReplaySizingEquity`.

## D10 — vectorbt: max_leverage is an independent cap; sizing semantics (F35, F7)

**Before:** the leverage branch silently promoted a conservative
`max_leverage` to the position cap
(`if max_leverage < max_position_pct: max_leverage = max_position_pct`) and
its `if/elif` arms were behaviorally identical while logging "leverage used".

**Decision:** `max_position_pct` stays the per-position notional cap
(documented in `--help`); `max_leverage` is an independent, respected limit.
The binding notional is `min(position cap, leverage limit)`; the
`risk_underfilled` warning fires only when the risk target actually had to be
underfilled.

**Pinned by:** `tests/test_vectorbt_sizing_semantics.py::TestLeverageSemantics`.

## D11 — vectorbt total_bars and equity-curve semantics (F6, F5)

**Before:** `meta.total_bars` carried the **trade count**; the portfolio curve
credited PnL at `trade.entry_time` (the simulator credits on close) — the
persisted curve described a trajectory never traded.

**Decision:** `total_bars` is the loaded bar count across symbols (same
contract as native/pynecore). The portfolio curve is a realized-PnL curve
over **exit** time (documented limitation: not an intraday mark-to-market
path — per-symbol sizing compounds from shared capital and summing those
paths would multiply capital by symbol count). Pynecore's curve is aligned to
exit time for cross-engine comparability.

**Pinned by:** `tests/test_vectorbt_sizing_semantics.py::TestTotalBarsIsBarCount`,
`::TestPortfolioEquityCurveSemantics`.

## D12 — `--max-position-pct` semantics (F7) — defused, not renamed

The finding documents three meanings (native risk-policy exposure, vectorbt
per-position cap, CLI per-position help). The **rename to
`max_position_exposure_pct` happens in #41 / PR #64** and is deliberately NOT
done here. This branch defuses the ambiguity:

- the CLI help documents the per-position notional-cap semantics explicitly
  (and that it is neither a risk-policy exposure limit nor a leverage grant);
- the vectorbt sizing no longer folds `max_leverage` into the position cap
  (D10), so the flag has one behavioral meaning per consumer, documented;
- the native engine's mapping of the flag onto its single-symbol risk
  engine's exposure limit is commented at the construction site.

A dedicated test pins the vectorbt meaning (`TestLeverageSemantics`); the
rename in #64 will carry the unification.

## Golden-master diffs

Deterministic A/B (`/home/ubuntu/tmp/golden_master.py`, 2 symbols × 300 hourly
bars, SMA 5/20; left = baseline `6caf461` checkout, right = this branch):

| Metric | native before | native after | vectorbt before | vectorbt after |
|---|---|---|---|---|
| trade_count | 4 | 4 | 4 | 4 |
| total_pnl | 57340.16 | 57340.16 | 54798.56 | 54798.56 |
| quantities (first 4) | 995.02/995.02/1128.68/1128.68 | unchanged | 990.10/990.10/1117.54/1117.54 | unchanged |
| drawdown | not directly captured (curve diff below) | — | — | — |
| vectorbt total_bars | **4 (trade count)** | **600 (bar count)** | — | — |
| vectorbt first curve point | entry-time 2024-01-05 05:00 | exit-time 2024-01-07 07:00 | — | — |

Trade count, PnL and per-trade quantities are **identical** on this fixture:
the multi-symbol/no-parallel-position scenarios where D2/D4 shift sizing are
covered by the deterministic regression fixtures instead (this fixture opens
at most one position at a time, so realized-only vs MTM sizing coincide).
The changed fields (vectorbt `total_bars`, curve timing) are exactly the
documented semantics fixes.
