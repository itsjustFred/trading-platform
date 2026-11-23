# Sizing Semantics Contract

Status: contract for the sizing knobs of the backtest runner (`runner/`),
established by the parent Cleanup 16 Decision Record (2026-08-31) and
implemented in Cleanup 16-E (#41). Pinned by `tests/test_sizing_semantics.py`.

## Sizing basis

Every fraction below is taken of **realized cash**: `initial_capital` plus the
net PnL of *closed* trades. The mark-to-market equity that feeds the equity
curve is never a sizing input — an open position's paper profit is not
spendable, and an unrealized drawdown is not a realized loss. The one
exception is the #347 free-margin leverage check, which reads
mark-to-market equity and used margin by design (MT5-style margin) — it is
a clamp on the sized quantity, not a sizing basis (decision record
D2 in `docs/cleanup-16d1-money-path-semantics.md`, which also records why MT5
and the vectorbt runner are *consistent* with this basis without being able to
decide it). `signal replay` is the documented exception: it marks open
positions to market for sizing (decision record D9).

## The three knobs — one documented meaning each

All exposure fractions are validated as finite values greater than 0. The
aggregate caps (`max_risk_per_trade`, `max_total_exposure`,
`RiskConfig.max_total_exposure`) stay bounded to `<= 1`; the per-position
notional ceilings (`BacktestConfig.max_position_exposure_pct`,
`RiskConfig.max_notional_per_trade`) may exceed 1 — that is leverage, bounded
per path by the broker maximum for the account and asset (#240, maintainer
decisions on #240/#329): the effective ceiling is
`min(configured, broker_max_leverage)`, and an undeterminable broker maximum
fails closed to 1x (`risk.config.effective_max_notional_per_trade`; the live
daemon resolves it through the adapter's `get_max_leverage`, signal replay
takes a declared `max_leverage`, the native engine uses `max_leverage`).
`max_leverage` is a separate multiple of equity and is not a
fraction-valued exposure field: it is the broker maximum leverage for the
account/asset. An unset value means an unknown maximum, and the fallback is
per engine: native clamps to 1x (#347), while the vectorbt and pynecore
adapters resolve it through `runner.engines._shared.resolve_max_leverage`
to 2.0x for the aggregate — and, since #387, both refuse a
`max_position_exposure_pct` above 1 rather than honouring per-position
leverage the aggregate rule cannot see.

| Name | Layer | Meaning (Decision Record, verbatim) |
|---|---|---|
| `max_position_exposure_pct` | `BacktestConfig` field, CLI `--max-position-exposure-pct` | **Position-size semantics**: the maximum notional of ONE position as a fraction of equity (e.g. `0.2` = a single position may reach 20% of equity). Not an aggregate-portfolio limit and not a loss budget. |
| `risk_per_trade_pct` | `BacktestConfig` field, CLI `--risk-pct` | **Maximum loss per trade**: the maximum fraction of equity a single trade may lose if its stop is hit. Sizing derives quantity from the actual stop distance: `qty = (equity × risk_per_trade_pct) / |entry − stop|`. |
| `max_total_exposure` | `risk.RiskConfig` field (live + signal-replay + native risk engine) | **Aggregate exposure limit**: the maximum total open exposure as a fraction of equity. |

## Deprecated alias

`max_position_pct` (config field, CLI flag `--max-position-pct`, and the
private helper `_resolve_max_position_pct`) is a **deprecated alias** of
`max_position_exposure_pct` with identical per-position semantics. Old
invocations keep working during the migration and emit a
`DeprecationWarning`; the alias will be removed in a future release. Passing
both spellings at once is rejected.

Persisted artifacts keep the historical key `max_position_pct` inside
`trade.meta["risk_math"]` (and `grid_summary.json` carries
`max_position_exposure_pct`) so the frontend `TradeRiskMath` type and the
SQLite `risk_percent` fallback stay stable; the key's meaning is the one
documented here.

## Engine mapping

- **vectorbt / pynecore**: `max_position_exposure_pct` caps the per-position
  notional (`equity × max_position_exposure_pct`); asset-class defaults apply
  when unset (equities 100%, other 20%). `risk_per_trade_pct` sizes from the
  stop distance; the position is filled up to the cap, never beyond.
- **native (`TradeEngine`)**: the flag caps one position's notional at
  realized cash `× max_position_exposure_pct`; the aggregate `RiskConfig.max_total_exposure`
  remains the risk engine default (`0.05`). `max_leverage` is an independent
  ceiling, so an omitted position cap preserves the native leverage behavior.
  Since #347 the leverage check is MT5-style: the order's margin
  (notional / `max_leverage`) must fit **free margin** — mark-to-market
  equity minus the margin already used by open positions — and an order that
  would need more leverage than the broker maximum is clamped down (floored
  to `lot_step` when it binds) instead of refused. The exposure cap itself
  stays on realized cash.
  Two documented limitations of the native free-margin check:
  - **Per symbol, not per account.** `TradeEngine` runs once per symbol, so
    in multi-symbol native runs each engine judges its order against its own
    equity and positions; margin used by other symbols' positions is
    invisible to it (pre-existing for the leverage cap, now stated for the
    free-margin rule).
  - **Pending orders hold no margin.** Several pending orders created on the
    same bar are each judged against the full free margin, so with
    pyramiding they can jointly exceed it — the same convention MT5 applies
    to pending orders.
  `fixed_risk_amount` (the MT5-alignment sizing protocol) routes through the
  same `RiskEngine.calculate_position_size` via `risk_amount_override` on a
  dedicated engine instance: `max_positions` (pending orders included) and
  the aggregate `max_total_exposure` ceiling — which clamps the declared
  amount to the remaining exposure budget — apply, with their own
  configurable limits (`fixed_risk_max_positions`,
  `fixed_risk_max_total_exposure`). `None` on either field means UNCAPPED —
  the settled parity-safe default, because the MT5 EA the protocol mirrors
  applies neither cap; setting a
  field opts that one cap back in. `fixed_risk_amount = 0` is the #170
  "off" idiom: the order falls through to pct sizing like an absent key,
  while NaN/inf/negative amounts raise `ValueError`. Open
  risk is measured in account currency: each position contributes
  `|entry - stop| * qty * (tick_value / tick_size)`, and positions sized on
  an explicit `stop_distance` carry their declared per-unit risk instead of
  being re-measured against `|entry - stop|` — except once the stop has
  trailed off its sizing value (breakeven/locked profit), where the smaller
  of the declared per-unit risk and the current `|entry - stop|` counts.
  The accepted quantity is
  recomputed in the legacy MT5 order `(risk / distance) *
  (tick_size / tick_value)` so unclamped sizes are bit-identical to the
  pre-#279 formula; the per-position notional cap and the free-margin
  leverage clamp are unchanged. `min_lot` is applied after the exposure
  clamp, so a sub-minimum size can be raised back above the remaining
  risk budget — pre-existing behaviour shared with the pct path. For the
  shared-equity replay, an unclamped fixed-risk
  quantity stays absolute-$ while a clamp-bound one is marked
  equity-proportional and re-scaled.
- **signal replay / live**: `risk_per_trade_pct` maps to
  `RiskConfig.max_risk_per_trade`; the exposure request maps to
  `RiskConfig.max_total_exposure`.
  Signal replay additionally applies the same aggregate free-margin rule
  as the native engine (#240/#347): total open notional — each position
  marked at its symbol's latest close — may not exceed
  `equity × max_leverage`, where `L` is the operator-declared
  `max_leverage`. **When `max_leverage` is unset the aggregate bound
  fails closed to 1× equity**: a multi-position replay with default
  settings (`max_notional_per_trade` ≤ 1, `max_positions` > 1) can
  therefore refuse or clamp later entries that earlier versions would
  have taken — there was no aggregate bound in replay before #387.
  This matches the native engine's unset-`max_leverage` → 1× behaviour.

## Fill prices feeding sizing and PnL (owner decision 2026-11-13)

Sizing is anchored on the price a position actually fills at (§ "entry"). The
exit side of that rule changed on 2026-11-13 and **supersedes** the earlier
"stop/TP always fill at the level" and "signal exits fill at the signal bar's
close" rules in any older text:

- a stop-loss / take-profit fills at its **level on an intrabar touch**, but at
  the bar's **open** when the bar opens beyond it (gap) — a gapped stop can
  therefore lose more than the sized risk (`risk_per_trade_pct` bounds the
  *planned* loss at the stop level, not a gap), exactly as at an MT5/live broker;
- a signal exit fills at the **next bar's open**, like an entry; a signal on the
  last bar is closed by the end-of-data close and flagged.

Details and per-engine mapping: `docs/cross-engine-backtest-contract.md` §1,
§3 and §5.

## Sizing equity vs reporting equity

Sizing draws on **realized cash** (`initial_capital` + closed-trade net PnL;
`TradeEngine._sizing_equity`): unrealized profit is not spendable and a paper
drawdown does not shrink new positions. The **reporting** equity curve that
feeds drawdown / Sharpe / volatility is separate and **mark-to-market** on the
native engine, so an open position's drawdown is reported even though it never
changes a position size. See `docs/cross-engine-backtest-contract.md` §4b.
