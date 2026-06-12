# GPT-5 Code Review - Trading Platform

Date: 2026-06-09
Scope: Current branch, repository-wide review with emphasis on trading performance, risk calculation, backtests, live execution, correlation, and optimization.

## Executive Summary

Status: Changes requested.

The platform has good foundations in the focused risk engine tests and a useful typed model layer, but several high-impact trading assumptions are still implicit. The highest-risk areas are portfolio-level capital/exposure handling in multi-symbol backtests, live execution exposure limits, and comparison/correlation reporting. Mutation testing killed the key RiskEngine mutations I tried, which is encouraging, but broader backtest/comparison paths have weaker or currently failing test signals.

## Findings

### Critical - Multi-symbol vectorbt backtests size each symbol independently against full capital

Files:
- `runner/engines/vectorbt_adapter.py:142`
- `runner/engines/vectorbt_adapter.py:191`
- `runner/engines/vectorbt_adapter.py:217`
- `runner/engines/vectorbt_adapter.py:249`
- `runner/engines/vectorbt_adapter.py:354`
- `runner/engines/vectorbt_adapter.py:402`

`_process_symbol()` owns its own `equity = config.initial_capital` and runs independently per symbol. When `num_symbols > 1`, each worker sizes trades as if it had the full account. Afterward, `VectorbtAdapter.run()` merges all trades and builds one equity curve from sorted trades. This can massively understate portfolio exposure and overstate performance when multiple symbols have overlapping trades.

Why it matters: risk-per-trade, max-position, leverage, drawdown, Sharpe, optimizer objective values, and portfolio approval can all be based on impossible capital usage. This is especially dangerous for correlation-heavy strategies where simultaneous entries are normal.

Suggested fix: move sizing/equity accounting to a portfolio-level event loop after all symbol signals are normalized, or explicitly constrain vectorbt multi-symbol backtests to single-symbol sizing semantics and block risk-sized multi-symbol runs until portfolio accounting exists. Add a regression with two symbols entering at the same timestamp and assert total notional/risk cannot exceed configured limits.

### Critical - Live Discord execution ignores existing broker exposure by default

Files:
- `live_runner/daemon.py:454`
- `live_runner/daemon.py:459`
- `live_runner/daemon.py:492`
- `live_runner/daemon.py:625`

`submit_order_with_risk()` accepts `current_exposure=0.0` by default. The Discord listener calls it without passing exposure. `RiskEngine.calculate_position_size()` therefore receives zero current exposure even if the broker already has open positions.

Why it matters: `max_total_exposure` is enforced only per submitted signal in the main live path. A sequence of accepted signals can exceed the configured portfolio risk cap.

Suggested fix: before sizing, query `adapter.get_positions()`, compute open risk/notional exposure using stop loss or broker/metadata fallback, and pass it to `submit_order_with_risk()`. Add a test where an existing position consumes most of `max_total_exposure` and the next Discord signal is blocked or clipped.

### High - Comparison endpoint silently compares only the first two selected strategies

Files:
- `viewer/main.py:7221`
- `viewer/main.py:7226`
- `viewer/main.py:7261`
- `viewer/comparison.py:139`
- `viewer/comparison.py:162`
- `viewer/comparison.py:163`

`/api/compare` accepts `?ids=id1,id2,id3`, but `compare_backtests()` only reads `strategies[0]` and `strategies[1]` for combined equity and correlations. Additional strategies are returned in the payload but excluded from the combined view.

Why it matters: correlation and combined drawdown are central to portfolio selection. A user can think they reviewed a basket of strategies while the risk math only covers two.

Suggested fix: generalize `compute_combined_equity()` and correlation stats to N strategies with explicit weights, or reject more than two IDs with a clear API error. Add a direct unit test for three strategies where the third changes the combined result.

### High - Legacy comparison aligns only exact timestamps and drops non-overlapping dates

Files:
- `viewer/comparison.py:53`
- `viewer/comparison.py:59`
- `viewer/comparison.py:63`
- `viewer/comparison.py:80`

`_align_series()` intersects exact timestamp strings. Strategies with different trade times, daily close timestamps, time zones, or sparse trade-derived equity curves can lose most data. No forward-fill or calendar alignment is applied in this legacy comparison path.

Why it matters: return correlation and drawdown correlation can become meaningless for realistic multi-strategy comparison. This is especially risky for intraday versus daily strategies.

Suggested fix: convert to UTC timestamps, resample to a declared calendar/frequency, forward-fill equity, and report overlap coverage. Refuse correlation if overlap is below a minimum threshold.

### High - Vectorbt equity curve is stamped at entry time after full trade PnL is known

Files:
- `runner/engines/vectorbt_adapter.py:402`
- `runner/engines/vectorbt_adapter.py:405`
- `runner/engines/vectorbt_adapter.py:407`

The vectorbt adapter adds each closed trade's PnL to equity and stores the point at `trade.entry_time`. That makes PnL appear at entry, not exit.

Why it matters: drawdown duration, CAGR period bounds, return timing, Sharpe/Sortino, and correlation can all be distorted. It also makes optimization over time-split results less trustworthy because equity timing can imply future PnL was already available.

Suggested fix: stamp realized PnL at `trade.last_exit_time` and include an initial capital point at the backtest start. Add a test with a long-held losing trade to ensure drawdown begins at exit or, if mark-to-market is desired, implement bar-level equity.

### Medium - Vectorbt metadata reports trade count as `total_bars`

File:
- `runner/engines/vectorbt_adapter.py:438`

`BacktestMeta.total_bars` is set to `sum(1 for _ in all_trades)`, which is trade count, not loaded OHLCV bars.

Why it matters: data coverage and performance diagnostics become misleading. A short backtest with 10 trades and a long backtest with 10 trades look identical by this field.

Suggested fix: return loaded bar counts from `_process_symbol()` and sum them in `VectorbtAdapter.run()`.

### Medium - Optimizer maximizes train metric with no minimum robustness constraints

Files:
- `runner/optimizer.py:135`
- `runner/optimizer.py:143`
- `runner/optimizer.py:160`
- `runner/optimizer.py:184`
- `runner/optimizer.py:193`

The optimizer selects by a single training metric, penalizes no-trade trials with `0.0`, and only evaluates the best trial on the test split after selection. There is no minimum trade count, drawdown cap, turnover/slippage guard, correlation penalty, or walk-forward validation.

Why it matters: high Sharpe/Calmar parameter sets can be selected from too few trades or from regimes that do not generalize. This can create classic overfit backtests.

Suggested fix: add configurable constraints such as min trades, max drawdown, minimum PSR, and optional multi-objective optimization. Persist rejected-trial reasons.

### Medium - Live signal processor can trade on synthetic market data

Files:
- `live_runner/daemon.py:334`
- `live_runner/daemon.py:348`
- `live_runner/daemon.py:351`
- `live_runner/daemon.py:381`

If `market_data` is absent, `_build_processor_market_data()` creates synthetic linear OHLCV around the entry price and feeds it into the signal processor.

Why it matters: processors such as volatility, ATR, trend, or liquidity filters can approve live trades on fabricated context.

Suggested fix: make missing market data a rejection in live mode, or mark synthetic context and require processors to explicitly opt in. Add a test that a processor requiring real OHLCV rejects absent market data.

### Medium - Backtest task result can lose `output_path` on Windows JSON log lines

Files:
- `viewer/main.py:7582`
- `viewer/main.py:7609`
- `viewer/main.py:7627`
- `tests/test_viewer_features.py:2020`

Current test evidence shows `tests/test_viewer_features.py::test_backtest_run_registers_task_manager_progress` fails on the current branch because `task["result"]["output_path"]` is missing. The simulated runner line includes a Windows path in JSON; if parsing fails, `output_path` remains empty and `compact_result` strips it.

Why it matters: completed backtest tasks can lose their result link in the UI/API, reducing operator confidence and making audit/replay harder.

Suggested fix: make `_runner_log_message()` robust to raw `Saved to:` lines and JSON messages containing Windows paths, or make the runner emit JSON through `json.dumps()`. Preserve `output_path` in the result payload even when empty if the API contract requires it.

### Medium - Logging defaults write outside the workspace and can break tests/imports

File:
- `shared/logging.py:84`

Importing modules that initialize loggers writes to `~/logs/<service>.log`. In the sandboxed test run this caused collection errors before any test executed.

Why it matters: tests and local tools become environment-dependent. CI or restricted runners can fail before reaching application logic.

Suggested fix: support `TRADING_PLATFORM_LOG_DIR`, default to a project-local/log-temp path in tests, and degrade to stdout-only if file handler creation fails.

## Graphify Overview

I used the existing `graphify-out` artifacts first. The graph report shows 4,473 nodes and 10,358 relationships across 348 communities. The most relevant hubs for this review were:

- `RiskConfig` - high connectivity and central to live/replay risk sizing.
- `AlpacaLiveAdapter` - broker execution bridge.
- `TradeSignal` - shared live/replay signal contract.
- `SignalReplayAdapter` - signal-to-backtest bridge.
- `VectorbtAdapter` - main backtest bridge.
- `BacktestResult` - persistence/API/reporting bridge.

The graph report also flags many inferred relationships around `RiskConfig` and `AlpacaLiveAdapter`, which I treated as prompts to verify source code rather than as evidence.

## Qwen Report Reference

I read `reports/qwen3_7_max_codereview.md`. It focuses on `strategies/qg_30min_es_breakout_strategy` and raises plausible issues around Pine/Python logic drift, misleading docs, missing data, and strategy test mismatch. I did not rely on it for the repository-wide findings above; I treated it as a separate prior review. Its strongest point that still generalizes is the need to document intentional strategy deviations and test against the intended asset/timeframe, not a convenient substitute.

## Mutation Testing

Baseline command:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_risk_engine.py tests/test_live_risk_sizing.py tests/test_optimizer.py -q
```

Baseline result: 10 passed, 2 warnings. The same command inside the sandbox failed at import because logging tried to write `C:\Users\user\logs\database.log`; the passing baseline required elevated execution.

Mutation results:

| # | File | Mutation | Result | Diagnostic | Notes |
|---|------|----------|--------|------------|-------|
| 1 | `risk/engine.py` | Changed max-position guard from `>=` to `>` | Killed | Clear | `test_size_live_signal_blocks_when_max_positions_reached` failed directly. |
| 2 | `risk/engine.py` | Changed `min(max_risk_per_trade, remaining_exposure)` to `max(...)` | Killed | Clear/cascading | Four focused tests failed across normal sizing, exposure cap, fallback SL, and live submission. |
| 3 | `viewer/comparison.py` | Changed comparison threshold from `>= 2` to `>= 3` | Inconclusive | Baseline failure | The broad viewer test suite failed on an unrelated current failure; no direct comparison test was found. |

Mutation score for completed meaningful mutations: 2/2 killed. Including the inconclusive comparison probe: 2 killed, 1 inconclusive.

Test gaps revealed:

- RiskEngine core arithmetic is reasonably covered.
- Live exposure integration is not covered because the tested live submission uses the default `current_exposure=0.0`.
- Legacy comparison/correlation lacks direct unit tests.
- Vectorbt multi-symbol portfolio accounting lacks focused tests.

## Pre-Mortem

### Incident 1 - Portfolio backtest approved impossible leverage

Severity: Critical
Component: `runner/engines/vectorbt_adapter.py`
Fragility type: Invisible invariant / semantic coupling

A multi-symbol strategy passed research review with acceptable drawdown and high Sharpe. In live paper trading, simultaneous entries consumed far more capital than the backtest implied. The root cause was that each symbol was sized independently against full initial capital, then trades were merged afterward.

Hardening: centralize portfolio-level event accounting, add overlap tests, and store exposure/notional over time.

### Incident 2 - Live account exceeded max exposure despite valid RiskConfig

Severity: Critical
Component: `live_runner/daemon.py`, `risk/engine.py`
Fragility type: Load-bearing default

A Discord signal sequence opened several positions and exceeded the configured `max_total_exposure`. The RiskEngine was correct in isolation, but the live caller passed the default exposure of `0.0`.

Hardening: compute exposure from broker positions before every new order and test that the second order is clipped/blocked.

### Incident 3 - Correlation dashboard missed the risky third strategy

Severity: High
Component: `viewer/comparison.py`, `/api/compare`
Fragility type: String/API contract mismatch

A user compared three strategies, saw a combined result, and assumed all three were included. The third strategy was returned in the list but excluded from combined equity/correlation math.

Hardening: either support N-way weighted comparison or reject requests with more than two IDs.

### Incident 4 - Optimizer selected a low-trade parameter island

Severity: High
Component: `runner/optimizer.py`
Fragility type: Assumption baked into optimization objective

Optimization selected a parameter set with high train Sharpe based on very few trades. The test split was reported but not used to constrain selection, and the system deployed an overfit configuration.

Hardening: require min trades, PSR threshold, max drawdown cap, and walk-forward/multi-split validation.

## The Fool / Red-Team Pass

Steelmanned thesis: The platform aims to provide a unified research-to-live workflow where strategies can be backtested, optimized, compared, and deployed with shared models and reusable risk controls.

Strongest challenges:

1. A correct unit-tested RiskEngine does not guarantee portfolio-safe execution if callers do not pass real exposure.
2. Backtest performance is only as truthful as the event clock; stamping realized PnL at entry time undermines time-series metrics.
3. Correlation tooling must be conservative, because misleading diversification is worse than no diversification signal.
4. Optimization needs robustness constraints, not just best-train-metric selection.
5. Live signal processors should never silently replace missing market context with synthetic data.

Synthesis: The next hardening phase should treat account exposure, timestamp alignment, and validation coverage as first-class domain objects. The code already has many reusable pieces; the risk is mostly in the boundaries between them.

## Recommended Priority

1. Fix live exposure calculation before broker submission.
2. Fix vectorbt multi-symbol portfolio accounting or block risk-sized multi-symbol runs.
3. Fix vectorbt equity timestamping and `total_bars`.
4. Add direct comparison/correlation tests for 2, 3, and mismatched timestamp strategies.
5. Add optimizer constraints and persist rejected-trial diagnostics.
6. Fix test/logging portability and the current `output_path` task regression.
