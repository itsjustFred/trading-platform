# Cross-Platform Parity Final Report - 2026-07-12

Branch: `codex/new-main`  
Evidence base head: `8c4a792` (`Add MT5 unmatched entry diagnostic`)  
Primary status log: `reports/backtest-alignment-status-20260712.md`  
Comparison matrix: `reports/strategy_metric_comparison_matrix.json`

## Executive Summary

The parity loop corrected confirmed platform-side issues and then stopped short
of manufacturing agreement where the available references are not sufficiently
rich. The current branch now has:

- MT5 XLSX reference timestamps loaded as `Europe/Athens` local time and
  converted to UTC for trade-level diagnostics.
- MT5 parity artifacts regenerated with stops anchored from explicit
  `stop_distance` where available, avoiding the previously too-tight shifted
  `sl_price` reconstruction.
- Risk Parity rebalance fills aligned to scheduled open prices while retaining
  the QuantConnect source strategy's documented `> 0.02` rebalance materiality
  threshold.
- Separate Risk Parity PnL bases for equity-curve net profit, reduction-trade
  realized PnL, and rebalance-attribution PnL.
- Reproducible MT5 diagnostics for matched stop paths, Sharpe convention
  candidates, and unmatched entry clusters.

Remaining residuals are not safely fixable from the checked-in artifacts alone.
The MT5 rows need either broker/tick-level bid-ask exports or per-trade EA
ATR/stop-distance exports to prove the exact stop path. The QuantConnect Risk
Parity row needs a genuine QC trade export or richer PDF extraction because the
checked-in JSON reference is synthetic/stale relative to the corrected local
attribution semantics.

## Current Matrix Residuals

Matrix generated at `2026-07-12T21:00:43.947056+00:00`.

| Case | Trade Count Delta | Win Rate Delta | Net Profit Delta | CAGR Delta | Sharpe Delta |
|---|---:|---:|---:|---:|---:|
| SP500 M1 MT5 | +64 | -10.20 pp | +16,469.81 | +1.4975 pp | -3.926321 |
| EURUSD H1 MT5 | +4 | -5.00 pp | +6,506.21 | +0.7385 pp | -2.215672 |
| NAS100 M15 MT5 | -8 | -10.44 pp | -8,883.73 | -0.7064 pp | -4.209890 |
| Risk Parity QC JSON | +1 | -3.3015 pp | +330,088.56 | -1.5288 pp | -0.121887 |

## Implemented Fixes

### MT5 Timestamp Alignment

`validation/statistics/cross_platform.py` now passes
`source_timezone="Europe/Athens"` for MT5 XLSX references. Matrix metrics remain
header/stat based, but imported trade timestamps now align with platform UTC
artifacts for diagnostics.

Evidence:

- Old UTC-default import produced zero exact entry-timestamp overlaps.
- Athens-local import produced exact timestamp overlaps:
  - EURUSD H1: `177`
  - NAS100 M15: `132`
  - SP500 M1: `118`
- SP500's first MT5 trade converts to `2018-01-16T14:45:00Z`, matching the
  platform artifact.

### MT5 Stop-Distance Artifact Anchoring

`scripts/regenerate_mt5_parity_artifacts.py` now prefers explicit shifted
`stop_distance` when reconstructing platform stops for MT5 parity artifacts.
This fixed a confirmed artifact-generation bug where shifted `sl_price` could
be compared to the signal-bar close and shrink the effective stop.

The fix modestly improved MT5 win-rate deltas but did not eliminate the exit
mix gap, which means the residual is not a single stop anchoring bug.

### Risk Parity Rebalance Semantics

`scripts/run_risk_parity_backtest.py` was verified and corrected to use
scheduled open prices for accepted rebalance fills when open data is available.
The `rebalance_threshold=0.02` default remains intentional because the checked
QuantConnect source calls `SetHoldings()` only when
`abs(current_weight - target_weight) > 0.02`.

Rejected experiments:

- Removing the threshold produced roughly `1098` trade rows, far from the
  `240` trade reference.
- Drifted-current-weight handling produced `252` rows and worsened net-profit
  agreement.

## Residual MT5 Evidence

### Stop/Exit Path

After timestamp alignment, exact entry timestamp plus direction matches are
still a minority of trades:

| Case | Entry+Direction Matches | Reference Trades | Platform Trades |
|---|---:|---:|---:|
| EURUSD H1 | 176 | 492 | 496 |
| NAS100 M15 | 127 | 597 | 589 |
| SP500 M1 | 113 | 512 | 576 |

The remaining bad matched bucket is `MT5 signal/blank -> platform stop_loss`.
In that bucket, platform stops are materially closer than MT5 stop-implied
levels, and local OHLC replay touches platform stops far more often than MT5
stops:

| Case | Bad Bucket Count | Median Platform/MT5 Stop-Distance Ratio | Local OHLC MT5 Stop Touches | Local OHLC Platform Stop Touches |
|---|---:|---:|---:|---:|
| EURUSD H1 | 23 | 0.7672 | 3 / 23 | 23 / 23 |
| NAS100 M15 | 19 | 0.7068 | 1 / 19 | 19 / 19 |
| SP500 M1 | 11 | 0.7109 | 2 / 11 | 11 / 11 |

This is evidence of stop-path/reference-data mismatch, not proof of another
platform execution defect. OHLC bars cannot establish the intrabar order of
events, and the MT5 XLSX trade rows do not expose the EA's per-trade ATR or
broker-side stop trigger path.

### Unmatched Entry Clusters

The unmatched-entry diagnostic shows pure timestamp-only direction conflicts
are small:

| Case | Timestamp-Only Overlap | Direction Conflicts |
|---|---:|---:|
| EURUSD H1 | 177 | 1 |
| NAS100 M15 | 132 | 5 |
| SP500 M1 | 118 | 5 |

The index cases have a strong unmatched platform cluster at `16:45`
Athens-local time:

| Case | Top Platform-Only Session-Minute Buckets |
|---|---|
| NAS100 M15 | 16:45 (178), 17:00 (101), 17:15 (79) |
| SP500 M1 | 16:45 (147), 17:00 (112), 17:15 (64) |

This narrows the trade-count residual to signal/data timing rather than trade
aggregation or direction decoding.

### Sharpe Convention

The tested MT5 Sharpe candidates include trade-PnL Sharpe, trade-return Sharpe,
calendar-day and business-day equity-return Sharpe, nonzero-day Sharpe, monthly
Sharpe, and a Sortino-like downside check. No tested convention matches all
three MT5 XLSX header values. The platform Sharpe formula was therefore left
unchanged.

## Residual Risk Parity Evidence

The platform artifact now stores distinct PnL bases:

- `net_profit` / `total_pnl`: equity curve final value minus initial value,
  currently `464381.17`.
- `realized_trade_net_profit`: reduction-trade rows only, currently
  `-103583.76`.
- `rebalance_attribution_net_profit`: previous-holding rebalance attribution,
  currently `456272.05`.

The comparison matrix intentionally uses `rebalance_attribution_*` fields only
for the `qc-parity` platform artifact. The remaining gap is dominated by the
reference side: the checked-in JSON is not a full independent QuantConnect
trade export, and the PDF exposes only partial summary fields.

## Validation

Latest validation runs completed during this parity sequence:

- `python scripts/compare_strategy_metrics.py --output reports/strategy_metric_comparison_matrix.json`
  - Passed.
- `pytest -q tests/test_analyze_mt5_parity.py tests/validation/test_cross_platform_metrics.py tests/validation/test_mt5_xlsx_importer.py tests/test_mt5_parity_artifact_loader.py`
  - `61 passed`.
- `pytest tests/validation/ tests/validators/ -q`
  - `467 passed`.
- `python -m compileall scripts/analyze_mt5_parity.py tests/test_analyze_mt5_parity.py`
  - Passed.
- `cd frontend && npm ci && npm run build`
  - Passed. `npm ci` reported 4 existing audit findings; Vite reported existing
    chunk-size warnings.
- CodeRabbit review on the latest diagnostic/report changes:
  - `findings:0`.

## Required External Evidence For Further Alignment

Do not tune the platform further for superficial parity without one of the
following:

1. MT5 tick or M1 bid-ask exports for the mismatched stop windows, including
   the broker-side prices used for stop triggering.
2. MT5 EA per-trade ATR and stop-distance exports, so platform-generated stop
   levels can be compared to the reference levels without inferring them from
   XLSX exit rows.
3. A genuine QuantConnect Risk Parity trade export with rebalance fills,
   timestamps, quantities, prices, and per-trade/rebalance PnL basis.
4. A richer QC report extraction that identifies whether the PDF net profit and
   win rate correspond to closed reductions, rebalance attribution, full equity
   curve, or another reporting convention.

Until those artifacts are available, the remaining differences are documented
and reproducible, but not safely removable by code changes.
