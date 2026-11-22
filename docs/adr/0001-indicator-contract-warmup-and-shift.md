# ADR 0001: Shared indicator contract — warm-up NaN, never 0; no internal shift

- Status: Accepted (maintainer decision on issue #317, 2026-11-21)
- Date: 2026-11-22
- Scope: `shared/indicators/` and every consumer (strategies, signal bots,
  replay/trailing paths, viewer, strategy-IR codegen)

## Context

Before `shared/indicators/` (issue #317) every strategy, signal bot and the
viewer carried private copies of ATR, RSI, EMA and Supertrend. The copies
disagreed on the details that decide trading behaviour:

- **Warm-up**: some copies emitted `0.0` before enough bars existed
  (`custom_swings_supertrend._calc_atr`), some emitted values from bar 0
  (`ewm` without `min_periods`), some emitted NaN. A zero ATR produces
  zero-width stops — a fabricated price level, not "unknown".
- **Shifting**: `.shift(1)` was applied ad hoc at call sites, so it was
  ambiguous whether a value at bar `t` was computable from bars `<= t`
  or already peeked at bar `t+1`.
- **Naming**: "ATR" meant four different smoothers (Wilder-RMA with a
  TR[0] seed, Wilder-RMA with an SMA seed, SMA — MT5 `iATR` — and EMA
  span), so identical parameter names hid different stop distances.

## Decision

All shared indicators in `shared/indicators/core.py` obey:

1. **Warm-up bars are `NaN`, never `0`.** A consumer that needs a number
   during warm-up must say so explicitly (a named preset such as
   `rsi_wilder_masked`/`ema_first_seed` with `min_periods=0`, or an
   explicit `fillna` at the call site). `0.0` is never a stand-in for
   "not yet computable" because it silently produces zero-width stops
   and zero-width Supertrend bands.
2. **No internal shift.** The value at bar `t` uses bars `<= t` only.
   Callers that act on the *next* bar apply `lagged(x)` explicitly; the
   shift is visible at the call site, never hidden inside the indicator.
3. **Smoothing and seeding are required, named parameters.** `smoothing`
   (`"sma"` / `"wilder"` / `"ema"`) and `seed` (`"first"` / `"sma"`) have
   no defaults; parity variants live in `shared/indicators/presets.py`
   under explicit names (`atr_qc_wilder`, `atr_mt5_iatr`,
   `atr_ema_span`, `supertrend_ramjet`, ...). New code picks a preset or
   passes the parameters explicitly — it does not invent a third meaning
   of "ATR".
4. **Parity presets preserve legacy edge cases deliberately.** Where a
   legacy copy emitted early or filled warm-up (e.g. the live bots'
   EMA-span ATR emitting from bar 0), the preset reproduces it via an
   explicit `min_periods=0` and says so in its docstring. Those are
   frozen parity paths, not patterns for new code.

## Consequences

- Strategies and bots import `shared.indicators` instead of carrying
  private recursions; fixes land once.
- Tests can rely on uniform warm-up semantics: `NaN` means "not ready",
  and consumers must gate on `NaN`/`ready` rather than on `!= 0`.
- Characterisation tests (`tests/indicators/`) pin every preset against
  the implementation it replaced — a live copy where one remains, or a
  frozen reference where P1 deleted the copy — so preset names are
  evidence, not marketing.
- Parity fixes that intentionally change outputs (Pine `ta.atr` = RMA,
  SMA-seeded EMAs, the `pine_supertrend_dema` band condition, zero-fill
  warm-ups) are separate, golden-moving PRs (phase P3) — they are *not*
  smuggled into dedup swaps.

## Alternatives considered

- **pandas_ta / ta-lib / `ta`**: rejected — no library exposes all four
  ATR meanings and seed variants explicitly; pandas_ta was already
  undeclared and unimportable in supported environments; ta-lib adds a
  C build that breaks the Linux/Windows/macOS portability rule.
- **Keep per-strategy copies**: rejected — five divergent Supertrend
  implementations already fed live, replay, trailing and the viewer.
