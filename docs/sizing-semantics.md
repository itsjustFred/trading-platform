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

All exposure fractions are validated as finite values in the inclusive range
`0 < value <= 1`. `max_leverage` is a separate multiple of equity and is not a
fraction-valued exposure field: it is the broker maximum leverage for the
account/asset. An unset value means an unknown maximum, and the fallback is
per engine: native clamps to 1x (#347), while the vectorbt and pynecore
adapters resolve it through `runner.engines._shared.resolve_max_leverage`
to 2.0x.

**Pending #240:** that `<= 1` ceiling is the current validation limit, not a permanent
design rule. The maintainer decided on 2026-11-16 that leverage may be allowed up to the
broker maximum per account and asset — `RiskConfig.max_notional_per_trade` above 1×, with
the entry size at the stop keeping the exposure sane. No PR implements it yet, so the
range above still applies on main.

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
- **signal replay / live**: `risk_per_trade_pct` maps to
  `RiskConfig.max_risk_per_trade`; the exposure request maps to
  `RiskConfig.max_total_exposure`.
