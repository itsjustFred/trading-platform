# MT5 Backtest Alignment Plan

## Purpose

Make platform backtests comparable against the MT5 strategy reports in
`strategy-comparisons/intraday_volatility_breakout/` without replacing the
current backtest engine. The target is an explicit MT5-alignment mode with
deterministic report ingestion, MT5-like fixed-risk sizing, instrument contract
metadata, confirmation-timeframe support, and a comparison report that explains
remaining differences.

Current MT5 reference files:

- `strategy-comparisons/intraday_volatility_breakout/EURUSD_H1.html`
- `strategy-comparisons/intraday_volatility_breakout/SP500_M1.html`
- `strategy-comparisons/intraday_volatility_breakout/US100_M15.html`

Important data-source constraint: Dukascopy does provide intraday US index CFD
data. Keep `SP500`/`US500` and `US100`/`NAS100`/`USTEC` routed through
Dukascopy index-CFD aliases.

## Current Baseline

The first strategy-alignment pass is documented in
`reports/strategy-comparison-platform-check-2026-06-23.md` (historisch, mit `reports/`-Ordner entfernt; Vergleichsbasis heute: `strategy-comparisons/`).

Main remaining gaps:

- MT5 reports use `Fixed_Risk_Amount=500.0`; platform engines currently use
  full-equity or percent-risk sizing.
- MT5 symbol contract semantics are not modeled: lot, volume step, point size,
  tick value, margin, spread, commission and swap.
- `Confirmation_Timeframe=15` is accepted as a parameter name by
  `strategies/volatility_breakout/volatility_breakout.py`, but not implemented.
- Report extraction is manual. The HTML reports are UTF-16LE with a BOM and
  German labels; parser code must handle this deterministically.
- Platform comparison artifacts do not align trade lists by time, side, price
  and PnL.
- Platform timestamps are currently interpreted as UTC; MT5 report timezone is
  not yet established.

## Phase 1: MT5 Report Ingestion

Likely files:

- Add `scripts/mt5_report_parser.py` or `shared/mt5_report.py`.
- Add `tests/test_mt5_report_parser.py`.
- Add small fixtures under `tests/fixtures/mt5_reports/`.

Implementation tasks:

- Build a deterministic parser using `BeautifulSoup` with `lxml` if available,
  falling back to stdlib `html.parser` if needed.
- Decode reports by BOM first. The current comparison reports are UTF-16LE; keep
  fallback handling for UTF-8 and CP1252.
- Normalize text cells by stripping NUL-like artifacts, non-breaking spaces and
  MT5 thousands separators such as `122 009.20`.
- Extract metadata:
  - report path
  - detected encoding
  - language label, initially `de`
  - symbol, timeframe, start date, end date
  - timezone assumption, initially explicit `unknown`
- Extract inputs/settings:
  - `ATR_Period`
  - `ATR_Multiplier`
  - `Stop_ATR_Multiplier`
  - `Use_Candle_Close`
  - `Confirmation_Timeframe`
  - `ATR_Short_Period`
  - `ATR_Long_Period`
  - `ATR_Regime_Factor`
  - `Regime_Filter_Mode`
  - `Trade_Direction`
  - `Reverse_Mode`
  - `Fixed_Risk_Amount`
  - `Entry_Time`
  - `Max_Entry_Time`
  - `Exit_Time`
  - `Magic_Number`
- Extract summary metrics with German label aliases:
  - net profit, gross profit, gross loss
  - profit factor
  - Sharpe
  - drawdown, including absolute and relative values where available
  - bars, ticks, symbols
  - expected payoff
  - recovery factor
  - total trades and deal count
- Extract order/deal rows:
  - timestamp
  - order/deal id
  - symbol
  - side
  - `in`/`out`
  - volume
  - price
  - SL/TP where present
  - profit
  - balance
  - comment
- Return typed dataclasses or Pydantic models; avoid exposing raw parser table
  shape to comparison code.
- Preserve both orders and deals if MT5 exports both sections. For first
  alignment, build a normalized trade-pair view from `in`/`out` deal rows.

Tests to add/run:

- Parser fixture tests against all three existing HTML reports.
- Assertions for exact extracted settings from the handoff values.
- Assertions for symbol/timeframe:
  - `EURUSD`, `H1`
  - `USA500.IDX`, `M1`
  - `USATECH.IDX`, `M15`
- Assertions for headline metrics such as profit factor and Sharpe.
- Assertions that deal rows parse with timestamp, volume, price, profit and
  balance.
- Run `pytest tests/test_mt5_report_parser.py`.

Acceptance criteria:

- The parser can read all three MT5 HTML files without manual cleanup.
- Parsed settings and key metrics are stable enough to snapshot in tests.
- Parsed rows include enough fields to align entry and exit deals into trades.

Risks/unknowns:

- MT5 HTML labels may differ by terminal language or broker template.
- The report timezone is not encoded in the file; keep it explicit and
  configurable.

Suggested commit:

- `reports: add deterministic mt5 html parser`

## Phase 2: MT5 Instrument Metadata

Likely files:

- Add `shared/mt5_instruments.py`.
- Add `shared/mt5_instruments.json`.
- Add `tests/test_mt5_instruments.py`.

Implementation tasks:

- Add a small explicit metadata mapping for only the comparison instruments:
  - `EURUSD`
  - `USA500.IDX`, `SP500`, `US500`
  - `USATECH.IDX`, `US100`, `NAS100`, `USTEC`
- Store canonical symbol, aliases and MT5-like contract fields:
  - contract size
  - lot size
  - min volume
  - max volume
  - volume step
  - point size
  - tick size
  - tick value
  - quote currency
  - account currency assumption
  - pip or point value
  - leverage or margin rate
  - spread assumption
  - commission assumption
  - swap assumption
- Implement helpers:
  - `resolve_mt5_instrument(symbol: str)`
  - `price_distance_to_risk(distance, volume, metadata)`
  - `round_volume_to_step(volume, metadata)`
  - `normalize_symbol_alias(symbol)`
- Keep this in `shared/` instead of `data/`, because sizing and comparison need
  it at runtime and it is not market data. Avoid coupling it to the Dukascopy
  downloader config, but keep aliases consistent with
  `data_tools/downloaders/dukascopy_native.py`.

Tests to add/run:

- Alias resolution for `SP500`, `US500`, `USA500.IDX`, `US100`, `NAS100`,
  `USTEC`, `USATECH.IDX`.
- Volume-step rounding for forex and index CFDs.
- Risk conversion from price distance to account currency for all three
  canonical instruments.
- Run `pytest tests/test_mt5_instruments.py`.

Acceptance criteria:

- Comparison code and engine code can resolve metadata without knowing broker
  aliases.
- Fixed-risk calculations can be unit-tested without a full backtest.

Risks/unknowns:

- Exact broker contract specs may differ from the MT5 account that generated the
  reports. Start with an explicit checked-in mapping and annotate source or
  assumption per field.

Suggested commit:

- `shared: add mt5 instrument metadata`

## Phase 3: Fixed-Risk Sizing Support

Likely files:

- `runner/engines/base.py`
- `runner/engine.py`
- `runner/engines/vectorbt_adapter.py`
- `shared/mt5_instruments.py`
- `tests/test_trade_engine.py`
- Add `tests/test_mt5_fixed_risk_sizing.py`

Implementation tasks:

- Extend `BacktestConfig` with opt-in fixed-risk fields:
  - `fixed_risk_amount: float | None`
  - `sizing_mode: Literal["default", "risk_pct", "fixed_risk"]` if a mode field
    is preferable to implicit precedence
  - `instrument_profile: str | None`, default `None`
- Preserve existing behavior when `fixed_risk_amount` is absent.
- Implement one canonical sizing helper in `shared/mt5_instruments.py` or a
  small `runner/sizing.py` module:
  - input: symbol, entry price, stop price, direction, fixed risk amount,
    instrument metadata
  - output: rounded volume/quantity plus risk diagnostics
- Use the helper from `TradeEngine._calculate_position_size`.
- In `VectorbtAdapter`, avoid duplicating math. Call the same helper in the
  current risk block where it computes `qty`.
- Decide canonical engine for MT5 alignment runs:
  - Use `native` as the primary alignment engine because it already simulates
    pending next-bar market orders, intrabar stop checks and trade exits.
  - Keep vectorbt support best-effort by sharing sizing math, but do not require
    exact vectorbt/native parity for MT5 validation.
- Add trade metadata:
  - sizing mode
  - fixed risk amount
  - stop distance
  - raw volume
  - rounded volume
  - instrument profile
  - assumed tick value and tick size
- Revisit notional/leverage caps for MT5 mode:
  - In fixed-risk mode, do not apply the platform `max_position_pct` cap unless
    explicitly provided.
  - Apply optional margin/leverage validation as diagnostics first, not as a
    silent cap, until broker margin rules are confirmed.

Tests to add/run:

- Unit tests for fixed-risk volume calculation with known entry/SL distances.
- Native engine test verifying `Fixed_Risk_Amount=500.0` produces expected
  rounded quantity and approximately expected loss on stop.
- Regression test confirming percent-risk sizing behavior remains unchanged.
- Vectorbt unit test for shared sizing helper use, not full parity.
- Run:
  - `pytest tests/test_mt5_fixed_risk_sizing.py`
  - `pytest tests/test_trade_engine.py`

Acceptance criteria:

- A backtest can opt into fixed dollar risk per trade without changing existing
  backtests.
- MT5 alignment trades carry enough sizing diagnostics to explain PnL deltas.

Risks/unknowns:

- Index CFD quantity semantics may be broker-specific. Keep metadata editable
  and report assumptions clearly.

Suggested commit:

- `runner: support mt5 fixed-risk sizing`

## Phase 4: Execution Semantics

Likely files:

- `runner/engine.py`
- `runner/engines/base.py`
- `runner/engines/vectorbt_adapter.py`
- `strategies/volatility_breakout/volatility_breakout.py`
- `tests/test_trade_engine.py`
- `tests/strategies/test_volatility_breakout_reference_logic.py`

Implementation tasks:

- Add an explicit execution-mode parameter for comparison runs:
  - `execution_timing`: `next_open` initially, because native engine currently
    creates market orders on the signal bar and fills them at the next bar open.
  - Document current vectorbt behavior, which shifts entries and exits one bar.
- `Use_Candle_Close=true`:
  - Keep signal generation based on close-crossing bands.
  - Ensure the comparison report states signals are generated on close and
    executed with the configured fill timing.
- Stop hit intrabar behavior:
  - Native engine currently checks stops before pending fills on each bar.
  - Keep stop fill price at stop price.
  - When lower-resolution intrabar data is available through
    `modeling_resolution`, use it to resolve stop/TP ordering.
- Same-bar entry/exit:
  - For first MT5 alignment, disallow same-bar market entry and stop exit unless
    intrabar data confirms a post-entry stop. Document this limitation.
- Spread/slippage/commission:
  - Add config fields and metadata placeholders:
    - `spread_points`
    - `slippage_points`
    - `commission_per_lot`
  - Implement them only after fixed-risk sizing is stable. First pass should
    report them as assumptions and default to zero unless supplied by the user.
- Session forced exit:
  - Keep strategy-level `Exit_Time` signal.
  - Verify forced exit uses the expected bar close or next-open timing under
    `execution_timing`.
- Direction and reverse mode:
  - Current strategy supports long/short direction. Add tests for
    `Trade_Direction` values.
  - Add `Reverse_Mode` parsing and strategy behavior only if the MT5 reports
    require it. Otherwise parse and report parity.
- Timezone:
  - Add `report_timezone` to comparison CLI.
  - Add `strategy_timezone` or `session_timezone` parameter to
    `volatility_breakout`.
  - Convert timestamps before calculating `Entry_Time`, `Max_Entry_Time` and
    `Exit_Time`.
  - Default stays UTC until MT5 report timezone is identified.

Tests to add/run:

- Native engine tests for next-open entry and stop fill remain green.
- Strategy tests for entry window and forced exit with a non-UTC timezone.
- Tests for `Use_Candle_Close=true` close-crossing behavior.
- Run:
  - `pytest tests/test_trade_engine.py`
  - `pytest tests/strategies/test_volatility_breakout_reference_logic.py`

Acceptance criteria:

- The comparison report can state the exact execution assumptions in force.
- Time window semantics are configurable rather than implicit UTC only.

Risks/unknowns:

- MT5 tester bar modeling may differ from native OHLC stop ordering. Keep this
  visible in the comparison report instead of hiding it behind silent heuristics.

Suggested commit:

- `runner: expose mt5 alignment execution assumptions`

## Phase 5: Confirmation Timeframe

Likely files:

- `strategies/volatility_breakout/volatility_breakout.py`
- `runner/engines/native_adapter.py`
- Possibly `runner/engines/base.py`
- `tests/strategies/test_volatility_breakout_reference_logic.py`

Implementation tasks:

- Interpret `Confirmation_Timeframe=15` as 15-minute bars.
- Support two data paths:
  - If the main timeframe is finer than 15 minutes, resample main OHLCV to
    15-minute bars inside the strategy.
  - If the main timeframe is equal to 15 minutes, use main bars as confirmation.
  - If the main timeframe is coarser than 15 minutes, document confirmation as
    unavailable unless a separate lower timeframe data input is added.
- Keep first implementation strategy-local. Avoid broad multi-timeframe runner
  plumbing until comparison requires multiple independent feeds.
- Define no-lookahead alignment:
  - A confirmation bar is available only after its close.
  - M1 signals at `16:30` may use the 15-minute bar that closed at `16:30`, not
    the bar ending at `16:45`.
  - Use pandas resampling with closed/right labels if this matches the report
    timestamps; otherwise lock the chosen convention in tests.
- Add indicator columns for comparison debugging:
  - confirmation ATR
  - confirmation regime boolean
  - confirmation bar timestamp used
- Apply confirmation filter to raw long/short entries before the in-trade state
  machine.

Tests to add/run:

- M1 synthetic data where a 15-minute confirmation becomes available exactly at
  a boundary.
- Test that the strategy does not look ahead into an incomplete 15-minute bar.
- Test M15 main timeframe uses its own bars as confirmation.
- Test H1 main timeframe documents or bypasses confirmation without crashing.
- Run `pytest tests/strategies/test_volatility_breakout_reference_logic.py`.

Acceptance criteria:

- `Confirmation_Timeframe=15` affects SP500 M1 signals deterministically.
- Boundary behavior is covered by tests.
- H1 and M15 reference runs remain valid.

Risks/unknowns:

- The exact MT5 confirmation condition is not yet proven from source code. Treat
  this as a hypothesis to validate by trade alignment.

Suggested commit:

- `strategies: add volatility breakout confirmation timeframe`

## Phase 6: Comparison CLI And Report

Likely files:

- Add `scripts/compare_mt5_report.py`
- Add `tests/test_compare_mt5_report.py`
- Add generated markdown reports under `docs/reports/` (früher `reports/`, Ordner im Repo-Cleanup entfernt)

Proposed command:

```bash
python -m scripts.compare_mt5_report \
  --mt5 strategy-comparisons/intraday_volatility_breakout/SP500_M1.html \
  --platform [entfernt] reports/strategy_comparison_backtest_SP500_M1_after_align_20260623.trades.json \
  --output docs/reports/mt5_alignment_SP500_M1_YYYYMMDD.md
```

Implementation tasks:

- Load parsed MT5 report and platform `BacktestResult` JSON.
- Normalize symbols using the MT5 instrument alias map.
- Normalize timestamps with `--report-timezone` and `--platform-timezone`,
  defaulting platform to UTC and report to `unknown` unless provided.
- Compare settings/input parity:
  - report every MT5 input and the platform parameter it maps to
  - flag missing, mismatched and ignored settings
- Compare summary metrics:
  - net profit
  - gross profit/loss
  - profit factor
  - Sharpe
  - drawdown
  - expected payoff
  - recovery factor
  - trade and deal count
- Compare trade lists:
  - direction count
  - first/last trade timestamps
  - first N mismatches by nearest timestamp and side
  - entry price delta
  - exit price delta
  - volume delta
  - PnL delta
  - unmatched MT5 trades and unmatched platform trades
- Add suspected-cause hints:
  - sizing mismatch
  - timezone/session offset
  - confirmation timeframe filter mismatch
  - stop ordering/intrabar mismatch
  - spread/commission/slippage mismatch
  - symbol contract mismatch
- Emit both markdown and optional JSON (`--json-output`) so tests can assert
  structured results.

Tests to add/run:

- Synthetic MT5-like HTML fixture plus tiny platform JSON.
- Test exact match yields no mismatches.
- Test one timestamp offset yields timezone/session hint.
- Test side mismatch yields direction hint.
- Run `pytest tests/test_compare_mt5_report.py`.

Acceptance criteria:

- A user can run one command and get an actionable alignment report.
- The report names assumptions and suspected causes, not only metric deltas.

Risks/unknowns:

- MT5 deal rows may split partial exits differently than platform `Trade`
  objects. Normalize to deal-level first, then pair to trade-level where clear.

Suggested commit:

- `reports: add mt5 comparison cli`

## Phase 7: Validation Runs And Fixtures

Likely files:

- `tests/fixtures/mt5_reports/`
- `docs/reports/mt5_alignment_*.md` (früher `reports/`)
- `docs/reports/strategy_comparison_backtest_*_mt5_align_*.trades.json` (früher `reports/`)
- `tests/test_auto_download.py`
- `tests/test_dukascopy_downloader.py`

Implementation tasks:

- Add small synthetic fixtures for unit tests. Do not rely on full 1 MB MT5 HTML
  files in fast tests unless repository size policy allows it.
- Add parser smoke tests against the three real reports if they are already
  committed and stable.
- Create an integration validation script or documented command sequence:
  - run native platform backtest for each reference with MT5 alignment params
  - run comparison CLI
  - save markdown reports under `docs/reports/` (früher `reports/`)
- Add non-regression coverage for data routing:
  - AAPL intraday routes to existing equity path, HuggingFace/Alpaca/yfinance as
    configured today.
  - SP500, NAS100 and US100 route to Dukascopy.

Tests to add/run:

- `pytest tests/test_mt5_report_parser.py`
- `pytest tests/test_mt5_instruments.py`
- `pytest tests/test_mt5_fixed_risk_sizing.py`
- `pytest tests/strategies/test_volatility_breakout_reference_logic.py`
- `pytest tests/test_compare_mt5_report.py`
- `pytest tests/test_auto_download.py tests/test_dukascopy_downloader.py`

Acceptance criteria:

- All new unit tests pass.
- Existing trade-engine and data-routing tests do not regress.
- Comparison reports can be regenerated from checked-in MT5 references and
  platform artifacts.

Risks/unknowns:

- The exact MT5 broker timezone and contract specification may remain unknown.
  The comparison report should make those assumptions visible and configurable.

Suggested commit:

- `tests: add mt5 alignment validation coverage`

## Suggested Implementation Order

1. Parser and parsed-report model.
2. Instrument metadata and sizing helpers.
3. Native fixed-risk sizing path, then vectorbt helper reuse.
4. Comparison CLI with current artifacts, even before confirmation timeframe.
5. Strategy timezone/session cleanup.
6. Confirmation-timeframe implementation.
7. Spread, slippage, commission and margin diagnostics.
8. Full validation reports for EURUSD H1, SP500 M1 and US100 M15.

This order gives useful comparison tooling early, so every later engine or
strategy change can be measured against the MT5 references.

## Recommended First Implementation Task

Start with Phase 1: implement `scripts/mt5_report_parser.py` and
`tests/test_mt5_report_parser.py`.

Reason: the parser turns the MT5 reports from manual reference files into typed,
testable inputs. It also lets subsequent phases verify that fixed risk,
instrument metadata, strategy parameters and summary metrics are aligned against
the actual report contents instead of hand-copied values.
