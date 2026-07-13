# Reference Artifact Contract

Branch owner: `codex/reference-artifact-contract`
Status: schema contract for future external evidence

## Purpose

External platform parity must be driven by auditable reference artifacts, not by
tuning platform metrics until they match a summary report. This contract defines
the minimum evidence required before MT5, QuantConnect/LEAN, or future broker
exports can be used to justify changes in signal, risk, execution, or reporting
behavior.

The current parity baseline already imports MT5 XLSX reports with
`source_timezone="Europe/Athens"` and keeps comparison-matrix metrics based on
the XLSX header stats. This document does not change those semantics. It defines
the next artifact shapes needed to diagnose remaining differences such as
intraday stop paths, bid/ask fill side, lower-timeframe replay, Sharpe
conventions, and QuantConnect rebalance fills.

## Contract Principles

1. Preserve source provenance. Every normalized field must identify its original
   file, section, column, timezone, and interpretation rule when practical.
2. Keep header metrics distinct from recomputed trade metrics, equity metrics,
   and attribution metrics.
3. Treat timestamps as source-local until an importer explicitly records the
   source timezone and UTC conversion.
4. Do not overwrite valid stored metrics with malformed imported fields.
5. Do not infer execution semantics from summary metrics when trade-level or
   tick-level evidence is missing.
6. Schema additions may be merged before new external artifacts exist; behavior
   changes require real evidence.

## MT5 XLSX Baseline

Current importer behavior:

| Source Area | Normalized Use |
|---|---|
| Header metadata | Strategy name, symbol, timeframe, initial capital, settings. |
| Header statistics | Matrix reference metrics such as trade count, win rate, net profit, Sharpe. |
| Orders table | Entry order stop-loss and take-profit levels keyed by order id. |
| Trades table | Paired entry/exit trades, prices, volume, PnL, swap, commission, comments. |
| `source_timezone` | Report-local timestamp timezone, currently `Europe/Athens` for parity cases. |

Rules:

- Matrix rows continue to compare MT5 header statistics with platform summary
  statistics.
- Trade-level diagnostics may compare timestamps only after importer timezone
  normalization to UTC.
- Exit comments such as `sl <price>` are evidence for MT5 stop-loss exits.
  Empty comments are not sufficient evidence for a platform behavior change.
- XLSX reports do not expose bid/ask spread, intra-bar path, or EA-calculated
  stop distance at signal time. Those must come from supplemental artifacts.

## MT5 Supplemental Execution Evidence

Evidence requirements are claim-specific. Market-path, same-bar ordering, and
bid/ask trigger claims require replay evidence. Signal-time levels, stop
distance, sizing, and broker rounding claims require an EA signal snapshot.
Claims that attribute a difference across signal, risk, and execution layers
require both artifacts joined to the same XLSX trade.

### Tick Or M1 Replay Window

Required fields:

| Field | Meaning |
|---|---|
| `case_id` | Cross-platform case id. |
| `symbol` | MT5 report symbol. |
| `timeframe` | Strategy timeframe. |
| `timezone` | Source timezone for all raw timestamps. |
| `entry_order_id` | MT5 order id used to join the XLSX trade, when applicable. |
| `entry_deal_id` | MT5 deal id used to join the XLSX trade, when applicable. |
| `join_key_type` | Identifier type used for the deterministic XLSX join, e.g. `order_id`, `deal_id`, or composite. |
| `timestamp` | Tick or bar timestamp. |
| `bid` | Bid price when available. |
| `ask` | Ask price when available. |
| `open` / `high` / `low` / `close` | Required for bar replay artifacts. |
| `volume` | Optional source volume. |
| `source_file` | Original exported file path or identifier. |

Validation expectations:

- The replay window must cover the entry bar, stop/exit bar, and any ambiguous
  bars between them.
- Bid/ask artifacts must state which side MT5 used for long entries, short
  entries, stop-loss triggers, and signal exits.
- Bar-only artifacts cannot resolve same-bar high/low ordering by themselves.

### EA Signal Snapshot

Required fields:

| Field | Meaning |
|---|---|
| `case_id` | Cross-platform case id. |
| `entry_order_id` | MT5 order id used to join the XLSX trade, when applicable. |
| `entry_deal_id` | MT5 deal id used to join the XLSX trade, when applicable. |
| `join_key_type` | Identifier type used for the deterministic XLSX join, e.g. `order_id`, `deal_id`, or composite. |
| `signal_timestamp` | Timezone-explicit timestamp when the EA decided to enter. |
| `entry_reference_price` | Price used by the EA to compute levels. |
| `fill_timestamp` | Timezone-explicit actual MT5 fill timestamp. |
| `fill_price` | Actual MT5 fill price. |
| `side` | `LONG` or `SHORT`. |
| `stop_price` | Absolute stop level submitted to MT5. |
| `stop_distance` | Fill-relative distance used for sizing, if available. |
| `risk_percent` | EA risk setting used for the trade. |
| `calculated_lots` | EA lot size before broker rounding. |
| `filled_lots` | MT5 executed lot size. |
| `exit_reason` | EA or broker exit reason if available. |

Validation expectations:

- `signal_timestamp` and `fill_timestamp` must be UTC or include an explicit
  offset/timezone. Importers normalize them to UTC before joining replay windows
  or platform trades.
- `stop_price` and `stop_distance` must remain separate. A shifted signal-close
  stop should not be used to reconstruct fill-relative distance.
- Broker rounding must be explicit before lot-size differences are attributed
  to the platform risk layer.

## QuantConnect / LEAN Reference Evidence

The current Risk Parity JSON is a synthetic/stale reference with limited
auditable trade detail. Future QC artifacts should include fills and portfolio
targets exported directly from LEAN.

### Portfolio Target Export

Required fields:

| Field | Meaning |
|---|---|
| `case_id` | Cross-platform case id. |
| `algorithm_id` | QC/LEAN run identifier. |
| `decision_timestamp` | UTC or timezone-explicit timestamp when target weights were generated. |
| `fill_model` | LEAN fill model name/configuration. |
| `rebalance_cadence` | Scheduled cadence and market-hours convention. |
| `universe` | Symbols eligible at the decision timestamp. |
| `target_weights` | Symbol-to-target-weight mapping. |
| `current_holdings` | Symbol-to-current-quantity/weight mapping before orders. |
| `covariance_window` | Lookback and estimator metadata. |
| `risk_contributions` | Per-symbol contribution if available. |

### Fill Export

Required fields:

| Field | Meaning |
|---|---|
| `order_id` | LEAN order id. |
| `symbol` | Filled symbol. |
| `timestamp` | Fill timestamp in UTC or with explicit timezone. |
| `quantity` | Signed filled quantity. |
| `fill_price` | Fill price. |
| `fees` | Fee amount and currency. |
| `order_status` | LEAN status. |
| `order_type` | Market, limit, stop, or other. |
| `tag` | Rebalance or attribution tag. |

Validation expectations:

- Target and fill timestamps must be UTC or include an explicit offset/timezone.
  Importers normalize them to UTC before joining target generation, orders, and
  execution fills.
- Equity-curve net profit, realized reduction-trade PnL, and
  rebalance-attribution PnL must remain separate metric bases.
- Rebalance materiality thresholds and open/close fill timing must be explicit.
- QC Sharpe comparisons must state the return frequency and risk-free-rate
  convention before platform statistics are changed.

## Normalized Metadata

Every importer should populate `raw_metadata` with at least:

| Field | Meaning |
|---|---|
| `source_framework` | External framework, e.g. `mt5_xlsx`, `mt5_html`, `quantconnect`. |
| `source_path` | Source path or stable artifact identifier. |
| `source_timezone` | Original timestamp timezone or `UTC` when truly UTC. |
| `timestamp_conversion` | Description of local-to-UTC conversion. |
| `metric_basis` | `header`, `trades`, `equity_curve`, `attribution`, or mixed details. |
| `schema_version` | Contract version used by the importer. |
| `extraction_warnings` | Non-fatal parse or evidence limitations. |

## Comparison Matrix Rules

Cross-platform matrix rows should disclose:

- reference framework and path,
- platform artifact path,
- source timezone for imported timestamps,
- metric basis for each compared value,
- missing artifacts,
- known evidence limitations.

Matrix summary metrics should not switch from header stats to recomputed trade
stats unless the row explicitly states that basis change and tests cover it.

## Protected Baseline Files

The following files contain parity-sensitive behavior and must not be changed
without a focused test and a before/after matrix note:

- `validation/importers/mt5_xlsx.py`
- `validation/statistics/cross_platform.py`
- `scripts/regenerate_mt5_parity_artifacts.py`
- `scripts/regenerate_qc_risk_parity_artifact.py`
- `scripts/run_risk_parity_backtest.py`
- `tests/validation/test_mt5_xlsx_importer.py`
- `tests/validation/test_cross_platform_metrics.py`
- `tests/test_mt5_parity_artifact_loader.py`
- `tests/test_qc_risk_parity_artifact_loader.py`

For schema-only updates, prefer documentation or new importer scaffolding over
editing these protected paths.

## Acceptance Checklist

Before a new reference artifact can justify runtime behavior changes:

1. The artifact has a documented schema version and provenance.
2. Timestamp timezone and conversion rules are tested.
3. Metric basis is explicit and does not overwrite another basis.
4. Trade-level joins are deterministic or unmatched rows are reported.
5. Execution evidence distinguishes strategy intent, risk sizing, fill model,
   and reporting facts.
6. The comparison matrix records before/after deltas and remaining limitations.
