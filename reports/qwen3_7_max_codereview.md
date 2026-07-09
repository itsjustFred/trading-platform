# Code Review: QG-30Min ES Breakout Strategy (qwen3_7_max)

## Summary
- **Status**: Changes Requested
- **Critical Issues**: 2
- **Major Issues**: 3
- **Minor Issues**: 2

## Critical Issues
### C1: Undocumented Logic Deviation in `breakout_short` Calculation
- **Location**: `strategies/qg_30min_es_breakout_strategy/qg_30min_es_breakout_strategy.py:105`
- **Severity**: Critical
- **Description**: The Python implementation calculates `breakout_short = high - fract * (high - open_).abs()`. However, the original Pine Script uses `breakout_short = high - FRACT * math.abs(open - low)`.
- **Impact**: If this was an intentional "fix" for a suspected copy-paste error in the original Pine script, it fundamentally changes the short-entry signals. Without documentation, this is a silent, unverified behavioral change.
- **Fix**: Either revert to `(open_ - low).abs()` to match the Pine script exactly, or explicitly document this as a deliberate correction in the README with justification.

### C2: Contradictory Documentation in README
- **Location**: `strategies/qg_30min_es_breakout_strategy/README.md`
- **Severity**: Critical
- **Description**: The README states: "Long-Filter: `ADX(45, smooth=5) > 30`" and "Long: `can_trade and (adx > 30) and (close > breakout_long)`". However, both the original Pine Script and the Python implementation use `adx < 30` and `close < breakout_long` for long entries.
- **Impact**: Future developers will be severely misled when trying to understand or modify the strategy logic, leading to incorrect assumptions and bugs.
- **Fix**: Update the README to accurately reflect the actual implemented logic (`adx < 30` and `close < breakout_long` for Long).

## Major Issues
### M1: Regression Tests Cannot Be Executed (Missing Data)
- **Location**: `tests/strategies/test_qg_30min_es_breakout_strategy.py`
- **Severity**: Major
- **Description**: Running `pytest` results in all 4 tests being skipped due to: `missing OHLCV dataset for XAUUSD/1h under C:\Users\user\PycharmProjects\trading-platform\data\ohlcv`.
- **Impact**: The core mandate of a code review is to verify correctness. Without the ability to execute the tests, the regression guarantees are void.
- **Fix**: Provide the required test dataset in the repository (or via a download script), or mock the data within the test file itself to ensure it can run in any environment.

### M2: Misleading "Vectorized" Claim vs Stateful Loop Implementation
- **Location**: `strategies/qg_30min_es_breakout_strategy/README.md` and `qg_30min_es_breakout_strategy.py:120`
- **Severity**: Major
- **Description**: The README recommends the strategy for a "vektorbasierten Engine" (vectorized engine). However, the `generate_signals` function uses a pure Python `for` loop with stateful variables (`in_trade`, `active_sl`, `trades_today`), which is O(N) and explicitly non-vectorized.
- **Impact**: This defeats the performance benefits of using `vectorbt`. For large datasets, this will be significantly slower than a true vectorized implementation.
- **Fix**: Either rewrite the signal generation using vectorized pandas/numpy operations (e.g., `groupby` for daily trade limits, `shift` for state), or update the README to accurately describe it as a "stateful, event-driven adaptation" and remove the vectorization claim.

### M3: Test Asset/Timeframe Mismatch
- **Location**: `tests/strategies/test_qg_30min_es_breakout_strategy.py:28-29`
- **Severity**: Major
- **Description**: The strategy is explicitly named "QG-30Min **ES** Breakout Strategy" (implying S&P 500 E-mini futures, 30-minute timeframe). However, the regression test is hardcoded to run on `TEST_SYMBOL = "XAUUSD"` and `TEST_TIMEFRAME = "1h"`.
- **Impact**: A passing test on XAUUSD 1h provides zero confidence that the strategy works correctly on ES 30min data, which is its stated purpose.
- **Fix**: Rename the test file to reflect it is a "XAUUSD 1h Smoke Test", or ideally, provide a small sample dataset for ES 30min and test against that.

## Minor Issues
### m1: Hardcoded Drawing Limits
- **Location**: `strategies/qg_30min_es_breakout_strategy/qg_30min_es_breakout_strategy.py:185`
- **Severity**: Minor
- **Description**: The `drawings` list generation arbitrarily caps markers at `[:200]` without configuration.
- **Impact**: For long backtests, early signals will be silently dropped from the visualization.
- **Fix**: Make this limit a parameter in `STRATEGY_META["parameters"]` (e.g., `max_drawings: 200`).

### m2: Time Exit Adaptation Needs Explicit Documentation
- **Location**: `strategies/qg_30min_es_breakout_strategy/qg_30min_es_breakout_strategy.py:98`
- **Severity**: Minor
- **Description**: The Python code triggers the forced exit at 14:30 NY, whereas the Pine script uses 14:55 NY. The comment explains this is for "Next-Open-Ausführung im vectorbt-Adapter".
- **Impact**: While logically sound for the adapter, it is a behavioral deviation from the source material.
- **Fix**: Add a "Known Deviations from Original Pine" section in the README to explicitly list this time shift.

## Positive Notes
- The Wilder's RMA and ADX calculations are correctly implemented and match the Pine Script's `ta.rma` behavior.
- The addition of a technical stop-loss (`sl_price`) is a prudent risk-management enhancement over the original Pine script, which had no stop-loss.
- Type hinting and docstrings are generally well-maintained.

## Recommendations
1. **Resolve the C1 and C2 issues immediately** by aligning the code, tests, and documentation.
2. **Address M1** by ensuring the test suite can actually run. A test that is always skipped is technical debt.
3. **Refactor for Vectorization (M2)**: Use `pandas` groupby operations to handle the "one trade per day" rule, and vectorized boolean masks for entry/exit conditions, eliminating the slow Python `for` loop.

---

## Pre-Mortem: QG-30Min ES Breakout Strategy Implementation

**Timeframe:** 3 months from now (Post-deployment to live trading or extensive backtesting)

### Failure Narratives

#### 1. Silent State Corruption in Production Backtests — Likelihood: Medium | Impact: High

It's 3 months from now. The strategy is run on a large, multi-year ES 30min dataset. The input DataFrame contains a few duplicate timestamps or unsorted indices from the data provider. Because the `generate_signals` function relies on a stateful pure Python `for` loop without defensive checks (like `df = df.sort_index().dropna()`), the `trades_today` counter fails to reset correctly on day boundaries, and `active_sl` propagates as `np.nan`. The team discovers the problem when the backtest reports an impossible 99% win rate with zero stop-loss hits. By then, the strategy was already approved for paper trading based on these flawed metrics. The root cause was the assumption that input data is always perfectly clean and chronologically sorted.

**Consequence chain:**
- 1st order: State variables (`in_trade`, `trades_today`) desync from actual bar progression.
- 2nd order: Backtest generates artificially inflated performance metrics (missing SL hits, extra trades).
- 3rd order: Strategy is approved for live/paper trading based on false data, leading to real capital risk or wasted engineering time debugging live discrepancies.

#### 2. False Regression Confidence Due to Asset Mismatch — Likelihood: High | Impact: Medium

It's 6 months from now. A developer "fixes" the missing test data issue by providing the XAUUSD 1h dataset, and the regression tests pass. The team assumes the strategy is robust. Later, the strategy is deployed to its intended target: ES 30min data. Because of the undocumented `breakout_short` calculation deviation (C1: Python uses `(high - open_).abs()` while Pine uses `math.abs(open - low)`), the short-entry logic behaves completely differently on the volatile ES 30min timeframe than it did on XAUUSD 1h. The strategy suffers unexpected drawdowns. The root cause was that the test suite validated the wrong asset class, masking the logic deviation.

**Consequence chain:**
- 1st order: Tests pass on XAUUSD 1h, giving a false sense of security.
- 2nd order: Strategy is deployed to ES 30min, where the undocumented logic deviation triggers excessive or mistimed short entries.
- 3rd order: Unexpected drawdowns occur, and debugging is delayed because the team trusts the "passing" regression tests.

#### 3. Timezone and Session Boundary Fragility — Likelihood: Medium | Impact: High

It's 3 months from now. The data pipeline switches to a new provider that delivers timestamps in local exchange time instead of UTC, or daylight saving time transitions are handled differently. The `timestamps_ny = timestamps_utc.dt.tz_convert("America/New_York")` logic silently misaligns. The `in_time` and `exit_now` flags trigger at the wrong bars (e.g., exiting at 13:30 NY instead of 15:00 NY). The team discovers this when a post-trade audit shows all forced exits happening an hour early, missing the intended session close liquidity. The root cause was the brittle assumption that the input `timestamp` column is always strictly UTC.

**Consequence chain:**
- 1st order: NY session minute calculations are offset by 1-2 hours.
- 2nd order: Entries occur outside the intended 08:00-15:00 window, and exits trigger prematurely.
- 3rd order: Strategy performance degrades significantly, and the root cause is hard to trace because the code "looks" correct for UTC data.

### Early Warning Signs

| Signal | Failure It Predicts | Check Frequency |
|--------|-------------------|-----------------|
| `df.isna().any()` returns True before signal generation | Failure #1 (State Corruption) | Every test run / CI pipeline |
| Test suite passes but uses XAUUSD instead of ES | Failure #2 (False Confidence) | Code review / PR check |
| Backtest trade count differs from Pine Script on identical data | Failure #1 or #3 (Logic/Timezone drift) | Every strategy update |
| "We'll clean the data later" or missing data validation step | Failure #1 (State Corruption) | Sprint planning |

### Mitigations

| Failure | Mitigation | Effort | Reduces Risk By |
|---------|-----------|--------|-----------------|
| #1 | Add defensive data validation at the start of `generate_signals`: `df = df.sort_index().dropna(subset=['open', 'high', 'low', 'close'])` | Low | 80% (Prevents silent NaN propagation) |
| #2 | Provide a minimal, mocked ES 30min dataset in the repo, or explicitly rename test to `test_xauusd_1h_smoke` and add a separate ES test. | Medium | 90% (Ensures tests validate the actual target domain) |
| #3 | Add explicit timezone validation: assert that `df.index.tz` is UTC or naive, and document the expected input format in the docstring. | Low | 70% (Fails fast on bad data instead of silent misalignment) |

### Inversion Check

**What would guarantee failure:**
1. Running the strategy on unsorted or NaN-filled data.
2. Deploying to ES 30min without ever testing on ES 30min data.
3. Feeding the function non-UTC timestamps without conversion.

**Do any exist now?**
- Yes, #1 is a risk because there are no defensive checks in the `for` loop.
- Yes, #2 exists because the test explicitly uses XAUUSD 1h, not ES 30min.

---

## Mutation Testing Notes
- **Status**: Blocked.
- **Reason**: Mutation testing requires a passing, executable test suite. Because the tests are skipped due to missing data (M1), no mutations can be evaluated.
- **Hypothetical Mutation**: If we mutate line 105 to `breakout_short = high - fract * (open_ - low).abs()` (matching Pine), a robust test suite *should* fail, proving it catches this logic discrepancy. The current setup cannot verify this.

---

## CodeRabbit Second Pass
- **Status**: Skipped.
- **Failed Prerequisites**:
  1. The `coderabbit` CLI is not installed in the native Windows environment (commands `coderabbit` and `npx coderabbit` failed with "command not found").
  2. The `coderabbit` CLI is also not installed in the WSL environment (`wsl coderabbit` and `wsl npx coderabbit` failed).
  3. The `.agents/control.json` configuration file does not exist to enable `coderabbitReview: true`.
- **Action Taken**: Attempted to execute `coderabbit review --agent -t uncommitted` natively, via `npx`, and via WSL. All attempts failed. Per skill guidelines, this second pass is formally recorded as skipped due to these exact missing prerequisites.
