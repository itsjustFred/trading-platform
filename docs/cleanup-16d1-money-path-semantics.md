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

**Scope note:** F21 also flags the hidden per-call sizing protocol
(`fixed_risk_amount`, `tick_value`, `lot_step`, `min_lot`, `max_lot`,
`cap_fixed_risk_by_leverage` read straight from `config.parameters`). That
protocol is **deliberately untouched** here — it is a deliberate MT5-alignment
interface (see `docs/mt5-backtest-alignment-plan.md`), documented rather than
removed; only the equity base it reads was wrong and is fixed.

**Pinned by:** `tests/test_engine_behavior.py::TestSizingEquityExcludesUnrealizedPnl`.

## D3 — Engine risk policy is not strategy-configurable (F22)

**Before:** `TradeEngine` read `max_positions` out of
`config.parameters` — any strategy parameter silently rewrote engine-level
concurrency risk policy.

**Decision:** the concurrent-position limit is the module constant
`TRADE_ENGINE_MAX_POSITIONS = 5`. The concurrency limit specifically can no
longer be reached from strategy `parameters`. Scope note: the MT5-alignment
sizing protocol (`fixed_risk_amount`, `tick_value`, `lot_step`, …) is still
read from `config.parameters` by design — that is a strategy-facing sizing
interface, documented in D2, not a risk-policy rewrite.

**Pinned by:** `tests/test_engine_behavior.py::TestMaxPositionsPolicy`.

## D4 — Every order is sized exactly once, at fill (F23)

**Before:** MARKET orders were sized at creation (signal-bar close) and
re-sized at the next-bar-open fill; LIMIT/STOP/STOP_LIMIT orders kept their
creation-price estimate forever.

**Decision (documented policy):** orders size once, at the price they
actually fill. MARKET re-sizes at the next-bar open (creation sizing is only
the pending estimate); LIMIT/STOP/STOP_LIMIT re-size from their trigger price
at fill. Quantities across order types are now computed from the same rule:
quantity = sizing rule evaluated at the fill price. One documented exception:
a MARKET order **without any stop** has no fill-time risk input, so its
creation-time estimate (signal-bar close) stands.

**Scope note:** F23 also lists the O(n) intrabar replay
(`engine.py` `iterrows()` window per bar instead of searchsorted). That is a
performance facet, deliberately **not** addressed here — this branch is
behavior only; the perf fix lands with the structural cleanup (16-D2) to keep
the golden-master diffs reviewable.

**Pinned by:** `tests/test_engine_behavior.py::TestOrderTypeSizingSemantics`
(LIMIT, STOP and the stopless-MARKET exception).

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
duration are computed once, outside the branches. The F17 third facet — QS
installed but the daily series unresolvable, metrics silently 0.0 — is closed
by falling through to the manual 252-period path on raw period returns in that
case instead of staying at 0.0. The dead `_infer_periods_per_year` /
`_annualize_total_return` helpers of the old fallback path are removed.

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
floored at $1 as before. Entries opened during a drawdown are no longer sized
as if nothing had been lost.

**Precision note (residual divergence):** the *reported* equity curve is
still a realized-PnL replay in exit order (same realized-only convention as
the other engines, D11). Sizing-time equity now includes unrealized PnL while
the curve does not — the two paths measure different capital by design, and
that is the documented distortion. Closing it fully (an MTM-marked replay
curve) requires the structural replay rework deferred to 16-D2.

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
over **exit** time, replayed in exit order (documented limitation: not an
intraday mark-to-market path — per-symbol sizing compounds from shared
capital and summing those paths would multiply capital by symbol count).
Pynecore's curve is aligned to exit time for cross-engine comparability.

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

## D8 appendix — annualization cadence

`_infer_portfolio_periods_per_year` infers the annualization period count
from the median equity-point spacing: equity points spaced more than ~8.7
hours apart are treated as daily-or-coarser and map to the 252 convention;
faster cadences scale linearly (4-hourly points → ≈2192, hourly → ≈8766).
There is no artificial ceiling: clamping an intraday curve's period count
(the earlier `252 × 4` ≈ 1008 cap) would understate the annualization
relative to the daily-resampled convention stats.py reports — a 4-hourly
curve annualized with sqrt(1008) ≈ sqrt(6) too small. Sub-hourly curves
still need enough bars for the per-observation mean/std to be meaningful,
which is a sample-size question the cadence cap never addressed.

## D11 appendix — vectorbt bar-count reload

`VectorbtAdapter.run` re-loads each symbol's OHLCV once after the per-symbol
processing loop purely to count bars for `meta.total_bars` (F6). The count
could be threaded out of `_process_symbol` instead; the second read is
bounded by the same parquet pages the loop just touched and keeps
`_process_symbol`'s signature (and its ProcessPoolExecutor pickling surface)
unchanged. Revisit when 16-D2 touches the adapter.

## Changelog note for consumers of the metric definitions (D5)

As of Cleanup 16-D1 the following definitions changed for runs produced by
this code (older persisted artifacts keep their original numbers):

- `total_return` is the equity-endpoint ratio (`E_end / E_start − 1`). The
  pre-16-D1 QuantStats path published the compounded product of resampled
  daily returns, which differs from the endpoint ratio on curves with
  intraday swings (e.g. 100k → 90k → 111k same day → 121k next day:
  endpoint 0.21, daily product 0.09).
- `calmar_ratio` divides elapsed-time CAGR by max drawdown on BOTH paths.
- Portfolio-optimizer `sharpe`/`oos_sharpe` are annualized (F36); values
  produced before 16-D1 are per-observation and ~15.9x smaller on daily
  curves.

If a fresh run's metrics disagree with a persisted pre-16-D1 artifact, this
is the expected cause — re-run comparisons within one code generation.
