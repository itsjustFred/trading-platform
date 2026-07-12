# Backtest Alignment Status - 2026-07-12

Branch: `codex/new-main`  
Head at start: `d953df5` (`Anchor intraday stops to market fills`)

## Scope

This slice re-established the cross-platform comparison baseline, reviewed the
Hermes prework as reference only, and focused on the Risk Parity QuantConnect
rebalance/fill mismatch. It also re-verified the previously mentioned native
intraday stop anchoring and fixed-risk leverage behavior in the current branch.

## Baseline

Command:

```bash
python scripts/compare_strategy_metrics.py --output reports/strategy_metric_comparison_matrix.json
```

Baseline matrix values matched the tracked 2026-06-30 matrix except for the new
generation timestamp. Key open deltas before this slice:

| Case | Metric | Reference | Platform | Delta |
|---|---:|---:|---:|---:|
| Risk Parity QC | Trade Count | 240 | 241 | +1 |
| Risk Parity QC | Win Rate | 57.50% | 56.4315% | -1.0685 pp |
| Risk Parity QC | Net Profit | 126,183.49 | 133,658.14 | +7,474.65 |
| Risk Parity QC | CAGR | 5.8651% | 4.3363% | -1.5288 pp |
| Risk Parity QC | Sharpe | 0.544687 | 0.422800 | -0.121887 |

The intraday MT5 rows remain close on trade count and net profit, but still have
large Sharpe-definition and win-rate differences.

## Investigation

The reference QuantConnect strategy in
`strategy-comparisons/risk_parity/risk_parity_strategy.py` schedules monthly
rebalances using `DateRules.MonthStart("SPY")` and
`TimeRules.AfterMarketOpen("SPY", 30)`. Its `ExecuteTrades` method only calls
`SetHoldings` when `abs(current_weight - target_weight) > 0.02`.

Findings:

- The local Python reimplementation used adjusted closes for trade entry prices.
  This did not match the scheduled market-open rebalance model.
- A blind removal of the 2% rebalance threshold was rejected during the loop:
  it produced 1,078 platform trade rows, far away from the QC reference summary
  count of 240.
- A drifted-current-weight experiment was also rejected: it produced 252 trade
  rows and moved net profit farther from the reference.
- The retained fix therefore preserves the QC 2% materiality threshold, but uses
  local/yfinance open prices for accepted rebalance fills.

## Changes

- `scripts/run_risk_parity_backtest.py`
  - `download_prices()` now returns adjusted close and open frames.
  - `run_backtest()` accepts injectable `prices`, `open_prices`, and
    `warm_up_days` for deterministic regression tests.
  - Accepted rebalance trade records now prefer the scheduled rebalance-day open
    price and fall back to close when open is unavailable.
  - The 2% materiality threshold is retained because it exists in the QC source.
  - Trade rows now carry the prior execution entry and current rebalance exit
    instead of reusing the entry price as the exit price.
  - Stored `net_profit`/`total_pnl` now follow the equity curve; realized trade
    PnL is retained separately as `realized_trade_net_profit`.
  - Close-based rebalance-attribution stats are retained separately under
    `rebalance_attribution_*` because the checked-in Risk Parity JSON reference
    is a verification-friendly Python-reference artifact, not a complete QC
    trade export.
  - Rebalance attribution now covers every prior holding at each rebalance,
    excludes first/open synthetic zero observations, and uses the interval-start
    portfolio value.
- `scripts/regenerate_qc_risk_parity_artifact.py`
  - Local parquet loading now reads both `open` and `close`.
  - The local price monkeypatch returns the same `(close, open)` shape as the
    yfinance download path.
- `validation/statistics/cross_platform.py`
  - For `qc-parity` artifacts only, the comparison matrix now uses explicit
    `rebalance_attribution_net_profit` and `rebalance_attribution_win_rate`
    fields when present. Other artifacts continue to use canonical stored or
    recomputed stats.
- `tests/strategies/test_risk_parity.py`
  - Added coverage that sub-2% target changes are skipped and material changes
    fill at the open.
- `tests/test_qc_risk_parity_artifact_loader.py`
  - Added coverage for local open-price loading.
- `validation/reporting/json_report.py`
  - Restored `_sanitize_json_numbers()` as a compatibility alias for the strict
    JSON test surface.

## After Matrix

Artifacts regenerated:

```bash
python scripts/regenerate_qc_risk_parity_artifact.py
python scripts/compare_strategy_metrics.py --output reports/strategy_metric_comparison_matrix.json
```

Risk Parity after this slice:

| Metric | Reference | Before | After | Before Delta | After Delta |
|---|---:|---:|---:|---:|---:|
| Trade Count | 240 | 241 | 241 | +1 | +1 |
| Win Rate | 57.50% | 56.4315% | 54.1985% | -1.0685 pp | -3.3015 pp |
| Net Profit | 126,183.49 | 133,658.14 | 456,272.05 | +7,474.65 | +330,088.56 |
| CAGR | 5.8651% | 4.3363% | 4.3363% | -1.5288 pp | -1.5288 pp |
| Sharpe | 0.544687 | 0.422800 | 0.422800 | -0.121887 | -0.121887 |

The open-fill correction did not resolve the remaining Risk Parity parity gap.
After the CodeRabbit-requested trade-row fix, the platform artifact is more
internally consistent and the comparison matrix explicitly uses the
verification-reference rebalance-attribution fields for the synthetic Risk
Parity row. The artifact now stores three distinct PnL bases:

- `net_profit` / `total_pnl`: 464,381.17, derived from final equity minus
  initial equity.
- `realized_trade_net_profit`: -103,583.76, derived from reduction-only trade
  rows that now carry prior entry and scheduled-open exit prices.
- `rebalance_attribution_net_profit`: 456,272.05, using close-based
  prior-holding rebalance attribution across all held symbols.

The true QC PDF still exposes only partial summary fields, so the QC trade-stat
basis remains unconfirmed. The checked-in JSON reference is synthetic and now
appears stale relative to the corrected attribution semantics; it should not be
treated as a complete independent QC trade export.

## Confirmed Current-Branch Fixes

- `runner/engine.py` recognizes `sl_price` and `tp_price` aliases.
- `runner/engine.py` supports `stop_distance` and re-anchors market-order stops
  to the actual fill price when present.
- `strategies/intraday_volatility_breakout/intraday_volatility_breakout.py`
  emits shifted `sl_price` and `stop_distance` for entry execution bars.
- Fixed-risk sizing in `runner/engine.py` still caps by `max_leverage` by
  default through `cap_fixed_risk_by_leverage=True`; MT5-style uncapped sizing is
  available only when that parameter is disabled.

## Verification

- `pytest -q tests/strategies/test_risk_parity.py tests/test_qc_risk_parity_artifact_loader.py tests/validation/test_cross_platform_metrics.py`
  - Result: 18 passed.
- `pytest tests/validation/ tests/validators/ -q`
  - Result: 463 passed.
- `python -m py_compile validation/reporting/json_report.py scripts/run_risk_parity_backtest.py scripts/regenerate_qc_risk_parity_artifact.py tests/strategies/test_risk_parity.py tests/test_qc_risk_parity_artifact_loader.py`
  - Result: passed.
- `cd frontend && npm ci && npm run build`
  - Result: passed. `npm ci` reported 4 existing audit findings. Vite reported
    existing chunk-size warnings.
- `git diff --check`
  - Result: passed.

## Remaining Work

1. Risk Parity still has `+1` trade-count drift and material net-profit,
   CAGR, and Sharpe gaps against the checked-in synthetic reference. The
   corrected attribution fields suggest that the reference JSON itself should be
   regenerated from a genuine QC trade export or a richer PDF extraction before
   changing strategy weights again.
2. MT5 intraday Sharpe remains far above the platform recomputation even with
   reference `risk_free_rate=0.0`; isolate MT5's Sharpe denominator/return basis
   from the XLSX/deal stream before changing platform runtime stats.
3. Intraday win-rate gaps remain around 6.6 to 11.1 percentage points despite
   close trade-count and net-profit alignment, so trade outcome classification
   and partial/zero-PnL handling should be audited next.
