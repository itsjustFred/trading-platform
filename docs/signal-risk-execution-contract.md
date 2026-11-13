# Signal, Risk, Execution Contract

Branch owner: `codex/sre-contract-design`
Status: draft contract for downstream implementation branches

## Purpose

This document defines the intended boundary between strategy logic, risk sizing,
execution/fill modelling, and reporting. The goal is to make MT5,
QuantConnect/LEAN, native backtests, signal replay, and live trading comparable
without requiring every strategy to encode broker, sizing, and reporting
semantics directly.

## Pipeline

```text
market data
  -> strategy signal layer
  -> risk decision layer
  -> execution/fill layer
  -> trade/event ledger
  -> reporting and validation
```

Each stage should be testable in isolation. A later stage may enrich earlier
intent with execution facts, but it must not rewrite the upstream decision.

## Strategy Signal Layer

Strategies own market interpretation only. They may compute indicators,
entries, exits, and intended protective levels. They should not own account
risk, position sizing, fill price selection, commission, slippage, or broker
stop-trigger semantics.

### `SignalIntent`

Canonical fields:

| Field | Meaning |
|---|---|
| `signal_id` | Stable, collision-safe identifier for this signal intent. |
| `strategy_id` | Stable strategy slug. |
| `symbol` | Reference or display symbol. |
| `execution_symbol` | Optional broker/data symbol if different. |
| `timeframe` | Signal timeframe. |
| `timestamp` | Bar/event timestamp that produced the signal. |
| `side` | `LONG` or `SHORT`. |
| `order_timing` | Examples: `same_bar_close`, `next_bar_open`, `next_bar_close`, `scheduled_rebalance`. |
| `order_type` | Examples: `market`, `limit`, `stop`, `target_weight`. |
| `entry_reference_price` | Price used by the strategy to compute levels, not necessarily the fill price. |
| `stop_price` | Optional absolute stop level. |
| `stop_distance` | Optional distance from eventual fill price; preferred when market orders fill later than the signal bar. |
| `take_profit_prices` | Optional ordered target levels. |
| `metadata` | Indicator values and source-specific diagnostics. |

Fill timing for exits (owner decision 2026-11-13, supersedes any earlier
"signal exits fill at the trigger bar's close" / "protective exits always fill at
the level" reading of the fields below): a signal exit uses `order_timing =
next_bar_open` like an entry, so an `exits` row on bar *N* fills at bar *N+1*'s
open (a last-bar exit is closed by the end-of-data close and flagged
`signal_exit_deferred_to_end_of_data`). A protective stop/target fills at its
level on an intrabar touch and at the bar's open when the bar opens beyond it.
`same_bar_close` remains valid only as an explicit legacy opt-in
(`signal_exit_fill="close"`) and for signal replay, whose external signals are
acted on at arrival. See `docs/cross-engine-backtest-contract.md` §1.

Rules:

- `signal_id` must be unique within the strategy run and stable across replay
  of the same input data. Adapters may derive it from
  `strategy_id`, `symbol`, `timeframe`, `timestamp`, `side`, and an ordinal for
  same-bar duplicates, but the derived value must be persisted before risk,
  execution, or reporting consumes it.
- `stop_distance` is preferred over reconstructing distance from shifted
  `stop_price` and signal close.
- If both `stop_price` and `stop_distance` exist, execution must preserve both:
  `stop_price` as strategy/reference level, `stop_distance` as fill-relative
  anchoring instruction.
- Strategies may emit no stop, but risk may then apply a documented fallback.
- Strategy output must include enough metadata to reproduce the signal decision
  without depending on later fills.

### `ExitSignal`

Canonical fields:

| Field | Meaning |
|---|---|
| `strategy_id` | Stable strategy slug. |
| `symbol` | Symbol or execution symbol. |
| `timestamp` | Bar/event timestamp that produced the exit signal. |
| `scope` | `position`, `symbol`, or `portfolio`. |
| `position_id` | Required when `scope == "position"` unless the adapter documents a deterministic single-open-position resolution rule. |
| `reason` | Examples: `signal`, `time_exit`, `rebalance`, `risk_off`. |
| `metadata` | Indicator values and source-specific diagnostics. |

Exit signals express strategy intent. Stop-loss and take-profit exits generated
by execution path simulation are `ExecutionFill` / `TradeEvent` facts, not
strategy exit signals.

Position-scoped exits must be uniquely addressable. If a legacy strategy only
emits a symbol-level exit series, the adapter must either emit `scope="symbol"`
or attach a documented resolution rule such as "oldest open position for this
strategy and symbol" in metadata.

## Compatibility With Existing `generate_signals`

Existing strategies commonly return:

```python
entries, exits, indicators, drawings = generate_signals(df, params)
```

Compatibility mapping:

| Existing Output | Contract Mapping |
|---|---|
| `entries` boolean series | One `SignalIntent` per true row. |
| `exits` boolean series | One `ExitSignal` per true row. |
| `indicators["direction"]` | `SignalIntent.side`. |
| `indicators["sl_price"]` | `SignalIntent.stop_price`. |
| `indicators["stop_distance"]` | `SignalIntent.stop_distance`. |
| `indicators["tp_price"]` / `take_profit` | `SignalIntent.take_profit_prices`. |
| `indicators["exit_reason"]` | `ExitSignal.reason` when generated by the strategy. |
| Other indicators | `metadata`. |

Adapters should keep accepting legacy tuple outputs while internally converting
them to contract objects. Strategies should migrate gradually.

Adapter context supplies required fields that legacy tuples do not contain:

| Contract Field | Legacy Source Or Default |
|---|---|
| `signal_id` | Deterministic adapter-generated id from strategy, symbol, timeframe, timestamp, side, and same-bar ordinal. |
| `strategy_id` | Strategy registry slug or explicit adapter argument. |
| `symbol` | Backtest/live symbol context. |
| `execution_symbol` | Broker/data symbol context when different, otherwise omitted. |
| `timeframe` | Dataframe or run configuration timeframe. |
| `timestamp` | Index timestamp of the true `entries` or `exits` row. |
| `order_timing` | Adapter default, currently explicit configuration such as `next_bar_open` (default for entries **and** signal exits since 2026-11-13) or `same_bar_close` (legacy opt-in). |
| `order_type` | Adapter default, usually `market` for legacy entry series. |
| `entry_reference_price` | Row close or explicitly configured reference column used by the legacy strategy. |

Defaults must be declared by the adapter so identical tuple outputs produce
identical contract objects across native backtests, signal replay, and live
dry-runs.

## Risk Decision Layer

Risk owns whether a signal is tradable at the account level and, if so, how much
size to allocate. It must not decide whether the signal is strategically valid,
and it must not choose fill prices.

### `RiskDecision`

Canonical fields:

| Field | Meaning |
|---|---|
| `signal_id` | Stable identifier of the source `SignalIntent`. |
| `accepted` | Boolean. |
| `quantity` | Sized quantity or lots; zero when rejected. |
| `risk_amount` | Account currency risk. |
| `risk_percent` | Account risk fraction. |
| `risk_distance` | Distance used for sizing. |
| `stop_price_used` | Stop used by the risk engine, including fallback if applied. |
| `take_profit_used` | Take-profit level used for risk/reward, if any. |
| `rejection_reason` | Optional reason, e.g. `max_positions`, `bad_stop_direction`. |
| `metadata` | Exposure, config, pip value, contract size, etc. |

Rules:

- Native backtest, signal replay, and live paths should converge on the same
  `risk/engine.py` sizing semantics where possible.
- Intentional live strictness, such as enforcing stop direction, must be
  encoded as explicit configuration or metadata and documented in test names.
- Risk decisions may resolve missing stops using configured fallbacks, but must
  record that fallback in `stop_price_used` and metadata.

## Execution/Fill Layer

Execution owns how accepted risk decisions become fills and trade events. It
must be configurable enough to model platform-native, MT5-style, and
QuantConnect-style conventions without changing strategy code.

### `ExecutionFill`

Canonical fields:

| Field | Meaning |
|---|---|
| `signal_id` | Source `SignalIntent`. |
| `risk_decision_id` | Source `RiskDecision`. |
| `symbol` | Executed symbol. |
| `timestamp` | Fill timestamp. |
| `side` | `LONG` or `SHORT`. |
| `quantity` | Filled quantity. |
| `price` | Fill price. |
| `price_side` | `bid`, `ask`, `mid`, `close`, `open`, or `model`. |
| `order_type` | Filled order type. |
| `fees` | Commission/spread/slippage costs when modelled. |
| `metadata` | Data source and execution model settings. |

### `TradeEvent`

Canonical event types:

- `entry_fill`
- `partial_exit_fill`
- `exit_fill`
- `stop_loss`
- `take_profit`
- `strategy_exit`
- `rebalance`
- `mark_to_market`

Canonical payload:

| Field | Meaning |
|---|---|
| `event_id` | Stable event identifier unique within the run. |
| `trade_id` | Stable trade or position identifier. |
| `position_id` | Broker/platform position id when available; otherwise the canonical trade id. |
| `event_type` | One of the canonical event types above. |
| `timestamp` | Event timestamp in UTC. |
| `symbol` | Executed symbol. |
| `side` | Position side affected by the event. |
| `quantity` | Signed or side-qualified quantity affected. |
| `price` | Execution or mark price, when applicable. |
| `execution_fill_id` | Linked `ExecutionFill` identifier for fill-derived events. |
| `exit_reason` | Required for exit, stop-loss, take-profit, and strategy-exit events. |
| `metadata` | Execution model settings, bid/ask side, replay source, timeframe, and source row ids. |

Rules:

- Stop/take-profit hits are execution facts. Their ordering must be governed by
  an explicit execution model.
- Same-bar ambiguity must be represented as a model setting, not hidden in a
  strategy.
- Lower-timeframe replay must record the replay source and timeframe.
- Bid/ask side selection must be explicit for entry, stop, take-profit, and
  signal exits.
- Reporting and validation consume `TradeEvent` as immutable facts. Later
  stages may append correction or adjustment events, but must not rewrite prior
  event identity or source metadata.

## Portfolio Target Signals

Portfolio allocation strategies such as Risk Parity should not be forced into a
single-symbol entry/exit contract.

### `PortfolioTargetSignal`

Canonical fields:

| Field | Meaning |
|---|---|
| `strategy_id` | Stable strategy slug. |
| `timestamp` | Rebalance decision timestamp. |
| `target_weights` | Symbol to target portfolio weight. |
| `rebalance_threshold` | Optional materiality threshold. |
| `cash_target` | Optional cash weight. |
| `metadata` | Covariance, risk contributions, universe, regime, hedge state. |

Portfolio targets pass through a portfolio-aware risk gate before execution.
The gate may reuse `RiskDecision` with `signal_id` pointing to the target
signal, or emit a dedicated portfolio decision containing:

- accepted/rejected status,
- adjusted target weights,
- cash and leverage checks,
- concentration and exposure limits,
- rejection or adjustment reasons,
- metadata for sizing constraints.

Execution turns approved or adjusted target weights into orders/fills.
Rejected targets are recorded for reporting and validation. Reporting separately
records:

- equity-curve net profit,
- realized reduction-trade PnL,
- rebalance-attribution PnL,
- mark-to-market returns.

These bases must not overwrite one another.

## Reporting And Validation Consumers

Reporting consumes canonical events and must preserve source semantics:

- Header-based reference stats stay distinct from recomputed trade stats.
- Imported reference timestamps must record source timezone and conversion.
- Cross-platform rows must disclose whether metrics come from header stats,
  recomputed trades, equity curves, or attribution fields.
- Malformed imported metrics must not replace valid stored metrics.

Validation should compare like with like:

- MT5 header metrics against platform summary metrics.
- MT5 trade-level diagnostics only after timezone normalization.
- QuantConnect attribution metrics only when the reference basis is known.

## Golden Fixture Expectations

Before refactoring runtime behavior, create fixtures that pin current behavior:

1. `SignalIntent` conversion from a small intraday dataframe.
2. `stop_distance` vs shifted `sl_price` behavior for next-bar market fills.
3. Fixed-risk sizing with and without stop-direction enforcement.
4. Native execution ordering for stop-loss, take-profit, and signal exit.
5. Risk Parity target weights, threshold filtering, open-fill execution, and
   separate PnL bases.
6. Reference import metadata for MT5 source timezone and QC attribution fields.

Golden tests should assert event-level facts, not only final summary metrics:

- signal timestamp,
- side,
- reference price,
- stop price,
- stop distance,
- risk amount,
- fill timestamp,
- fill price,
- exit reason,
- metric source.

## Migration Rules

1. Add adapters before changing strategy outputs.
2. Preserve existing artifact behavior until a branch explicitly changes a
   documented execution model.
3. Keep branch ownership disjoint as listed in
   `reports/architecture-orchestration-plan-20260713.md` (historisch, mit `reports/`-Ordner entfernt).
4. Rebase dependent branches after contract changes merge.
5. Do not tune MT5/QC parity numbers without stronger reference evidence.
6. Every branch must document changed contracts, focused tests, and remaining
   compatibility risks before CodeRabbit review.
