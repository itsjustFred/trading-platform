# Handoff Prompt: Plan MT5 Backtest Alignment Work

You are starting a new coding/planning session in `/home/ubuntu/Desktop/trading-platform` on branch `codex_rewrite`.

Your task is to create an implementation plan for the changes needed so platform backtests can be effectively compared against MT5 strategy reports in `strategy-comparisons/`.

## Context

Recent commits already did the first round of comparison work:

- `c3a1602 data: route index cfd comparisons via dukascopy`
- `2a34bda reports: add strategy comparison backtest artifacts`

Read these first:

- `reports/strategy-comparison-platform-check-2026-06-23.md`
- `strategy-comparisons/intraday_volatility_breakout/EURUSD_H1.html`
- `strategy-comparisons/intraday_volatility_breakout/SP500_M1.html`
- `strategy-comparisons/intraday_volatility_breakout/US100_M15.html`
- `strategies/volatility_breakout/volatility_breakout.py`
- `runner/engine.py`
- `runner/engines/native_adapter.py`
- `runner/engines/vectorbt_adapter.py`
- `data_tools/downloaders/dukascopy_native.py`
- `todo/codex-todo.md`

Important correction: Dukascopy does have intraday US index CFDs. Do not reintroduce old notes that say otherwise. `SP500`/`US500` and `US100`/`NAS100`/`USTEC` should use Dukascopy, not yfinance proxies.

## Current Findings

Platform-vs-reference comparison has been started and documented.

Baseline platform runs before strategy alignment:

| Reference | Platform artifact | Trades | PnL | Profit Factor | Sharpe |
|---|---:|---:|---:|---:|---:|
| EURUSD H1 | `reports/strategy_comparison_backtest_EURUSD_H1_20260623.trades.json` | 217 | -267.96 | 0.7806 | -1.2609 |
| SP500 M1 | `reports/strategy_comparison_backtest_SP500_M1_20260623.trades.json` | 79 | 6825.47 | 17.5496 | 0.2410 |
| US100 M15 | `reports/strategy_comparison_backtest_US100_M15_20260623.trades.json` | 34 | 10655.24 | 11.5610 | 0.2924 |

After first strategy alignment:

| Reference | Platform artifact | Trades | PnL | Profit Factor | Sharpe |
|---|---:|---:|---:|---:|---:|
| EURUSD H1 | `reports/strategy_comparison_backtest_EURUSD_H1_after_align_20260623.trades.json` | 71 | 11310.19 | 1.7590 | 0.3453 |
| SP500 M1 | `reports/strategy_comparison_backtest_SP500_M1_after_align_20260623.trades.json` | 239 | -4800.04 | 0.7227 | -0.2174 |
| US100 M15 | `reports/strategy_comparison_backtest_US100_M15_after_align_20260623.trades.json` | 319 | -5598.22 | 0.8072 | -0.4270 |

MT5 references still differ materially, especially for SP500 and US100.

Known remaining gaps:

- MT5 fixed-risk sizing: reference uses `Fixed_Risk_Amount=500.0`; platform currently uses percent-risk sizing and platform-specific notional/leverage caps.
- MT5 symbol contract semantics: point value, lot size, volume step, tick size, tick value, pip/point conversion and margin/leverage are not modeled like MT5.
- MT5 spread/commission/swap/slippage assumptions are not represented in the comparison layer.
- Session/timezone semantics may differ. Reference entry window is `16:30` to `18:05`, exit at `22:55`; platform currently interprets timestamps as UTC.
- Reference includes `Confirmation_Timeframe=15`; current strategy accepts parameter names but does not resample or align a separate confirmation timeframe inside `generate_signals`.
- Report parsing is still manual-ish. We need robust extraction of MT5 settings, summary metrics and trade/order rows from the HTML reports.
- Comparison tooling should align trade lists, signals and metrics by time, symbol and direction, not only compare headline PnL.

## Goal For Your Session

Produce a concrete, staged implementation plan for making the platform able to compare its backtests against MT5 reports effectively.

Do not jump straight into broad implementation unless explicitly asked. First produce a plan that identifies:

1. Minimal data model changes.
2. Execution/sizing engine changes.
3. Strategy changes.
4. MT5 report parser changes.
5. Comparison/reporting CLI or viewer changes.
6. Tests and validation fixtures.
7. Order of implementation with small commits.

## Planning Requirements

Your plan should include these workstreams:

### 1. MT5 Report Ingestion

Plan a parser for MT5 HTML reports that extracts:

- Inputs/settings: `ATR_Period`, `ATR_Multiplier`, `Stop_ATR_Multiplier`, `Use_Candle_Close`, `Confirmation_Timeframe`, `ATR_Short_Period`, `ATR_Long_Period`, `ATR_Regime_Factor`, `Regime_Filter_Mode`, `Trade_Direction`, `Reverse_Mode`, `Fixed_Risk_Amount`, `Entry_Time`, `Max_Entry_Time`, `Exit_Time`, `Magic_Number`.
- Summary metrics: net profit, gross profit, gross loss, profit factor, Sharpe, drawdown, bars, ticks, symbols, expected payoff, recovery factor.
- Trade/order rows: timestamp, order/deal id, symbol, side, in/out, volume, price, SL/TP where present, profit, balance, comment.
- Metadata: symbol, timeframe, report file, language/encoding, timezone assumption.

Prefer a deterministic parser using BeautifulSoup/lxml or stdlib HTML parsing. The existing reports contain NUL bytes and CP1252/German labels; handle that explicitly.

### 2. MT5 Instrument/Sizing Model

Plan how to represent MT5-like instrument metadata:

- contract size
- lot size and volume step
- min/max volume
- point size / tick size
- tick value
- quote currency
- pip/point value
- leverage/margin
- commission and spread assumptions

Start with only the instruments needed by the reports:

- `EURUSD`
- `USA500.IDX` / `SP500` / `US500`
- `USATECH.IDX` / `US100` / `NAS100` / `USTEC`

Decide whether this belongs in `data/`, `shared/`, or a new comparison-specific module. Prefer a small explicit JSON/YAML/py mapping plus tests.

### 3. Engine Support For Fixed Risk Amount

Plan platform engine support for MT5-style fixed risk:

- Add config/params path for fixed dollar risk per trade.
- Size trades from `fixed_risk_amount / stop_distance_value`.
- Convert stop distance in price units into account-currency risk using instrument metadata.
- Round volume/quantity to MT5-like step.
- Make this opt-in so existing percent-risk backtests do not regress.

Check both `runner/engine.py` and `runner/engines/vectorbt_adapter.py`; decide whether one canonical implementation is enough for MT5 alignment runs.

### 4. Execution Semantics

Plan explicit support or documentation for:

- signal bar vs next-bar execution
- `Use_Candle_Close=true`
- stop hit intrabar behavior
- same-bar entry/exit behavior
- spread/slippage/commission
- end-of-session forced exit
- long/short direction and reverse mode
- timezone conversion for MT5 report times vs platform OHLCV timestamps

Be specific about what can be implemented now and what should be documented as a limitation.

### 5. Confirmation Timeframe

Plan how to implement `Confirmation_Timeframe=15` for `volatility_breakout`:

- Determine whether the main timeframe can be M1/H1/M15 and confirmation always means 15-minute bars.
- Decide how to resample from main data or load a separate timeframe through the runner.
- Define signal alignment rules: when does a 15-minute confirmation become available to a 1-minute strategy?
- Add tests around boundary times and no-lookahead behavior.

### 6. Comparison CLI/Report

Plan a tool, for example:

```bash
python -m scripts.compare_mt5_report \
  --mt5 strategy-comparisons/intraday_volatility_breakout/SP500_M1.html \
  --platform reports/strategy_comparison_backtest_SP500_M1_after_align_20260623.trades.json \
  --output reports/mt5_alignment_SP500_M1_YYYYMMDD.md
```

It should compare:

- settings/input parity
- summary metric differences
- trade count and direction count
- first N mismatched trades by time/side/price
- PnL differences after applying MT5-like sizing if available
- actionable suspected causes

### 7. Tests

Plan tests before implementation:

- parser fixture tests using the three existing MT5 HTML reports
- instrument metadata tests for EURUSD/SP500/US100
- fixed-risk sizing unit tests
- strategy signal tests for long/short, entry window, exit window, ATR regime and confirmation timeframe
- integration comparison test using a tiny synthetic MT5-like HTML fixture and platform JSON
- non-regression tests that AAPL intraday still routes to HuggingFace/Alpaca while SP500/NAS100/US100 route to Dukascopy

## Deliverable

Create or update a plan document, preferably:

- `docs/mt5-backtest-alignment-plan.md`

The plan should be actionable. For each phase include:

- files likely touched
- exact implementation tasks
- tests to add/run
- acceptance criteria
- risk/unknowns
- suggested commit boundaries

At the end, include the recommended first implementation task.

Keep the plan grounded in the current codebase. Do not propose replacing the whole backtest engine. The goal is a pragmatic MT5-alignment mode and reliable comparison tooling.
