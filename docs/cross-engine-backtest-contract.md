# Cross-Engine Backtest Contract

Status: contract for the backtest engine adapters (`runner/engines/`), shared
primitives in `runner/engines/contract.py`, conformance tests in
`tests/test_engine_contract.py`; the 2026-11-13 fill semantics are pinned per
engine in `tests/test_realistic_fills.py`.

Every engine adapter — `NativeAdapter` (+ `TradeEngine`),
`VectorbtAdapter`, `PyneCoreAdapter`, `SignalReplayAdapter`, `LeanAdapter` —
must satisfy the facets below. The conformance suite pins them on a
deterministic bundled fixture, so a divergence shows up as a failing test, not
as a silent parity drift between engines.

Decision Records referenced below are from Cleanup 16-A (2026-08-31) and the
parent Cleanup 16 record.

> **Owner decision 2026-11-13 (realism / parity with MT5 and live brokers)
> supersedes two earlier fill rules.** (A) A stop-loss/take-profit fills at the
> *level* only when the bar touches it intrabar; a bar that **opens beyond** the
> level (a gap) fills at the bar's **open**. (B) A signal exit fills at the
> **next bar's open**, like an entry — not at the signal bar's close
> (D4 / A-M1 "Fill-zum-Trigger-Close" is superseded). Wherever the old text
> below is marked *superseded*, this decision wins. See §1 (Protective exits,
> Signal exits) and §5.

## 1. Fills

### Market entry

- **Convention: next-bar-open.** A signal produced on bar *N* executes on bar
  *N+1*, filling at bar *N+1*'s **open**.
- Native: orders created on the signal bar are filled by
  `_fill_pending_orders` only on a later bar (`i > created_index`), at
  `bar_open`. `ExecutionModelSettings.market_order_timing = "next_bar_open"`
  and `entry_price_side = "open"` document this on every native trade
  (`trade.meta["execution_model"]`, `trade.meta["entry_fill"]`).
- Vectorbt: strategy output is shifted one bar by
  `_prepare_next_open_execution_data` and entries fill at the execution bar's
  open (`_resolve_entry_price`).
- Signal replay (documented exception, see §5): signals from external bots
  fill **same-bar at the signal bar's close**, because the replay works from
  signal payloads that arrive with a bar already closed. `trade.meta`
  records `signal_timestamp`, `fill_timestamp`, `fill_delay_seconds` and
  `slippage_bps` so the difference stays auditable.

### Protective exits (stop-loss / take-profit)

- Trigger sides: a LONG stop triggers when `low <= stop`; a LONG take-profit
  triggers when `high >= take_profit`; mirrored for SHORT. Native records this
  as `stop_trigger_side = "bar_low_high"` and
  `take_profit_trigger_side = "bar_high_low"`.
- Fill price (owner decision 2026-11-13, supersedes "always the level"): the
  protective **level** on an **intrabar touch** (the bar opens on the safe side
  and trades through the level later); the bar's **open** when the bar **opens
  beyond** the level (gap through a stop — the worse price — or through a
  take-profit in your favour — the price actually available). An open exactly at
  the level fills at that price either way. The gap is the first event of the
  bar, so it also beats a later same-bar touch of the other level and needs no
  intrabar data. The entry bar is never a gap (the position was opened at that
  very open). Shared primitives: `contract.level_gapped_at_open` /
  `contract.level_fill_price`. SHORT exits buy the ask, so the native engine
  adds the spread on top of the open exactly as it does on a level fill. When a
  trailing stop is active, native reports reason `trailing_stop`; otherwise
  `stop_loss`. Native names take-profit exits after the level name (`tp`,
  `tp1`, …); vectorbt and signal replay use `take_profit`. Consumers must
  treat both spellings as the same event class.
- Vectorbt has no bar-loop position model: protective levels are resolved on
  the entry bar and enforced through the intrabar walk (§3), where the gap rule
  applies per **sub-bar** (a sub-bar that opens beyond the level fills at its
  open; the entry bar's first sub-bar is exempt). Without intrabar data, exits
  happen on the scheduled signal exit and the levels are recorded in
  `trade.meta` (`sl_price`, `tp_price`) — vectorbt therefore cannot express the
  gap rule without intrabar data; it does not enforce the levels at all there
  (pre-existing, documented limitation).
- Signal replay applies the gap rule in `_check_protective_exit_bar`
  (bar open beyond the level → fill at the open). PyneCore's own broker
  emulator fills gaps at the open; the adapter's containment
  (`_normalize_trade_exits`, §5) restates a level-violating leg to the level, or
  to the exit bar's open when that bar gapped through the level.

## 2. End-date boundary

Decision Record (Cleanup 16-A): **a date-only end boundary is inclusive of the
full day.**

- `start_date="2024-01-15"` keeps bars at/after `2024-01-15T00:00:00Z`.
- `end_date="2024-01-15"` keeps bars up to `2024-01-15T23:59:59.999999Z`.
- A value with an explicit time component is used as-is; naive timestamps are
  interpreted as UTC.

The rules are implemented once in
`runner.engines.contract.parse_date_bound` / `contract.apply_date_filter` and
consumed by the adapters (native, pynecore, vectorbt and signal-replay all
filter through the shared helper). A date-only end bound must never drop the
bars of the end date itself.

**Warmup gate (A-M11, A-H4/D3).** `warmup_start` (default `""`) names a
period whose bars feed indicator warm-up but must produce NO trades. All
adapters gate trades before the warm-up boundary; an explicit `start_date`
inside the warm-up span is a hard error (`ValueError`), never a silent
no-gate. Engines agree on the boundary semantics (pinned by cross-engine
conformance tests).

**Signal exit fill convention (owner decision 2026-11-13; supersedes
A-M1/D4 "Fill-zum-Trigger-Close").** A signal exit (not a protective level
exit) fills at the **next bar's open**, exactly like an entry: a signal on bar
*N* exits at bar *N+1*'s open. `BacktestConfig.signal_exit_fill` is validated to
`next_open` (default) / `close` / `open`; the last two are explicit legacy
opt-ins that fill on the *signal bar itself* (its close / its open — `open`
serves MT5 clock exits whose signal bar *is* the exit minute). The convention is
implemented once per engine:

- native: the exit signal is parked and executed at the next bar's open inside
  `_check_stops`, **after** open-gap exits (which keep their stop/target reason)
  and **before** the bar's intrabar walk and before pending entries fill, so a
  reversal (exit + entry on one bar) closes the old position and opens the new
  one at the same open. SHORT exits pay the spread.
- vectorbt: `_prepare_next_open_execution_data` shifts the exit leg with the
  entry leg; the intrabar walk for a signal exit ends at the exit bar's open.
- PyneCore: Pine fills at the next open natively; `_apply_fill_parity` prices a
  non-level ledger leg at its own (fill) bar's open under `next_open`.
- **Explicit `exit_price` = a LEVEL.** A strategy-supplied `exit_price` is a
  stop/limit/target level, never a market-at-close request. Only vectorbt reads
  the column (native, PyneCore and signal replay ignore it and always fill a
  signal exit at the next open). Under `next_open` the vectorbt engine therefore
  drops an `exit_price` that equals the *signal bar's own close* (a market-close
  hack, which would otherwise book the next bar's timestamp at the previous
  bar's close), fills at the exit bar's open, logs a warning once and flags the
  trade `meta["exit_price_ignored_market_close"]`. Real levels are honoured;
  the legacy `close`/`open` opt-ins keep the explicit price.
- **Clock exits (strategy authors).** A strategy that must be flat *at* a clock time
  T (end-of-day flatten, session close, forced exit) raises its exit signal on the
  bar BEFORE the one that opens at T - the first bar whose end reaches T - so the
  next-open fill lands at T's open. Derive it from the bar spacing plus the clock
  schedule only, never from a later bar (`shared.bar_timing.modal_bar_minutes` /
  `clock_exit_signal_mask`; PyneCore scripts use `timeframe.in_seconds()`), and add a
  first-bar-of-the-next-session safeguard for data gaps. Do not write a market
  `exit_price` for these exits.
- Signal replay: external signals — entries and flip/exit-only exits alike — are
  executed when they arrive at the fill bar's close (§5), so "exits like
  entries" already holds; there is no separate next-open exit.

**A signal exit on the last bar** has no next bar to fill at. The position is
closed by the forced **end-of-data** close at that bar's close with reason
`end_of_data`, and the trade is flagged
`trade.meta["signal_exit_deferred_to_end_of_data"] = True` (native and vectorbt;
PyneCore's ledger drops a trade that never closes, signal replay closes
end-of-data at the last close). Stop/TP exits fill at the level or, on a gap,
the open (§1).

**Pynecore CLI bounds.** Pynecore additionally hands each symbol's frame to
the external `pyne` CLI (`--from`/`--to`). The CLI parses those values with
`datetime.fromisoformat` and resolves a NAIVE result through `timestamp()` —
i.e. in the **host timezone** — and a date-only `--to` lands on that day's
midnight (verified against pynesys-pynecore 6.8.14: `--to 2024-01-01` replays
only the `00:00` bar; under `TZ=America/New_York` a date-only `--from
2024-01-01` lands on `05:00Z` and drops the first five UTC bars of the day).
The adapter therefore translates BOTH bounds to timezone-aware UTC instants
before dispatch — `_pyne_cli_end_bound` (date-only end → inclusive
end-of-day) and `_pyne_cli_start_bound` (date-only start → UTC start-of-day) —
so pyne replays the same window as the other engines **regardless of the host
timezone**. Both translations live in `runner/engines/pynecore_adapter.py`
and are pinned by `TestPyneCoreCliEndOfDayBoundary`, including a real-CLI run
under `TZ=America/New_York`.

## 3. Same-bar stop/take-profit priority

Decision Record (Cleanup 16-A):

- Same-bar SL/TP priority is **stop-first everywhere when intrabar data is
  unavailable**;
- with intrabar data enabled and available, the **actual chronological event**
  is used;
- if intrabar data is missing, fall back to stop-first **and flag the
  result**.

Implementation:

- `runner.engines.contract.resolve_same_bar_exit()` is the single
  classification point. Callers that walk intrabar data invoke it per
  sub-bar in chronological order, so the first event seen is the event that
  happened; within one bar/step that hits both levels, the helper's
  stop-first tie-break applies. It only accepts the engine-wide direction
  spelling `"LONG"`/`"SHORT"`.
- Native: `_check_stops` uses the intrabar walk when
  `modeling_resolution` requests a finer timeframe; otherwise (and whenever
  the walk cannot order the events) it applies stop-first and sets
  `trade.meta["same_bar_priority"]` via `flag_missing_intrabar`:
  `{"same_bar_priority": "stop_first_fallback", "intrabar_resolution": …,
  "intrabar_available": …, "reason": …}`. The flag is present ONLY when the
  fallback was used (never a `null` placeholder), and `reason` distinguishes
  `"intrabar_data_missing"` (no sub-bars for the bar) from
  `"intrabar_unresolved"` (sub-bars exist but touch neither level).
- Vectorbt: `_match_intrabar_exit_event` routes through the same helper per
  sub-bar. The intrabar resolution itself is honored as requested (`5m`,
  `15m`, `1h`, `1-lower`, `m1`, …); only unknown spellings fall back to
  open/close modeling. The spelling convention lives in exactly ONE place —
  `runner.engines.contract.normalize_intrabar_resolution` /
  `resolve_intrabar_timeframe` — and BOTH vectorbt and native resolve through
  it (`BacktestConfig` additionally canonicalizes alias spellings such as
  `m1`/`M1` at the config boundary, so no adapter can receive a spelling it
  might interpret differently). The same configuration therefore yields the
  same intrabar frame — and the same exits — on every engine (pinned by
  `test_native_and_vectorbt_resolve_the_same_intrabar_spellings` and
  `test_spellings_pinned_against_one_shared_table`). Since D4 (A-M1) the
  vectorbt signal exit fires on the trigger bar itself, so vectorbt faces
  the same-bar ambiguity on its main frame exactly like native; its
  intrabar walk covers the trigger bar's own sub-bars (window through the
  trigger bar's close) and a trigger-bar stop/TP therefore fills at the
  level in both engines.
- Signal replay: `_process_protective_exits` walks bars chronologically and
  classifies each through the shared helper.
- Configuring `same_bar_order = "take_profit_first"` is **not** part of the
  shared contract: the shared helper has no tp-first mode, and the native
  setting is audit metadata only — stop-first is contractual and no engine
  implements a tp-first mode (`trade.meta["execution_model"]["same_bar_order"]`
  stays `"stop_first"` regardless of the configuration).

## 4. Sizing

- **Risk-based sizing** (`risk_per_trade_pct` set): position quantity is
  derived from the *actual* stop distance,
  `qty = (equity × risk_per_trade_pct) / |entry − stop|`, capped by the
  max-position notional (`equity × max_position_pct`) and the max-leverage
  notional (`equity × max_leverage`). When the risk target cannot be met
  inside the caps, the position is filled up to the cap (never beyond) —
  vectorbt records the decision in `trade.meta["risk_math"]`
  (`sizing_mode`, `risk_target_usd`, `stop_distance`, `required_notional`,
  `final_notional`, `capped_by_max_position`, `capped_by_max_leverage`).
- **`entry` is the price the order actually fills at**, for every order type
  (MARKET: the next bar's open; LIMIT/STOP: their trigger price). Pre-fill
  creation sizing is only a pending-order estimate and must never survive into
  the position: because the caps are evaluated against the fill price, a
  quantity derived from the signal bar's close sizes a position *beyond* the
  cap whenever the next open gaps (repro: 103.4k notional on 100k equity at a
  1x cap). Native's `_fill_pending_orders` re-sizes at the fill for every
  order type, the no-explicit-stop fallback included.
- **The sizing `equity` is realized cash** (`initial_capital +` closed-trade
  PnL, net of fees — Cleanup 16-D1 F21/D2), never the mark-to-market
  reporting equity of the curve: an open position's paper profit is not
  spendable and a paper drawdown must not deflate new sizes.
- Native additionally supports `fixed_risk_amount` sizing with MT5 tick
  metadata (`tick_size`, `tick_value`, `lot_step`, `min_lot`, `max_lot`);
  the result is still capped by `max_leverage` unless
  `cap_fixed_risk_by_leverage = false`.
- **Equity sizing** (no risk config): `qty = equity / entry_price`, capped by
  `max_leverage`.
- Without an explicit stop, engines fall back to a documented default stop
  distance — **2% of the fill price** (native/risk engine:
  `fallback_stop_pct = 2%`, applied to the entry the risk engine is handed;
  vectorbt: `entry_price * 0.02`) — the fallback must be visible in the trade
  meta, not silent. It is a real fill-time risk input, so a stopless order is
  sized from it like any other order (this supersedes the 16-D1 stopless-MARKET
  exception recorded in `docs/cleanup-16d1-money-path-semantics.md` D4).
- **Multi-symbol shared equity (D5, A-M4):** per-symbol engines size each
  trade from the full starting capital; on multi-symbol runs the adapters
  replay all trades onto ONE running portfolio equity
  (`runner.engines._shared.replay_shared_equity`, wired in the native and
  vectorbt adapters): quantities are re-scaled onto the shared equity,
  PnL/fees re-priced from the scaled quantities, and open exposure is summed
  across symbols into `trade.meta["portfolio"]`. Trades flagged
  `equity_proportional_sizing=False` (native fixed-amount sizing is
  absolute-$) keep their quantity; exposure is still summed. Single-symbol
  runs skip the replay (identity). PyneCore performs the equivalent merged
  sizing pass after parsing all symbols: entries are processed in timestamp
  order against one realized-equity ledger, and the aggregate open notional
  is capped by shared `max_leverage`.

## 4b. Reporting equity curve (mark-to-market)

Drawdown, Sharpe, Sortino, volatility and Calmar are computed from the
persisted `equity_curve`, so that curve must show the paper PnL of open
positions. **Every engine persists a mark-to-market (MTM) portfolio curve**
(`runner/engines/mtm_equity.py`, native / vectorbt / pynecore / signal-replay,
single- and multi-symbol alike):

- one observation per bar of the traded symbols plus every entry / exit-leg
  time, starting at `start_date` with the initial capital; the observation at
  `t` uses the close of the bar stamped `t`;
- equity = `initial_capital` + realized exit legs (gross) + unrealized PnL of
  the still-open quantity marked at the last close; fees are recognised when
  the trade fully closes, so from the final exit on a trade contributes exactly
  its `net_pnl` and the last point equals `initial_capital + Σ net_pnl`
  (total return is identical to the old realized-only curve);
- multi-symbol runs build ONE portfolio curve from the shared-equity-replayed
  trades; per-symbol paths are never summed;
- runs of unchanged equity with no open position are thinned to their end
  points (lossless for the daily resampling every ratio uses);
- `meta.data["equity_curve_basis"]` is `mark_to_market`
  (`mark_to_market_partial` + `equity_curve_unmarked_symbols` when a traded
  symbol had no bars to mark against) or `realized_exit_timed` for artifacts
  whose PnL cannot be re-derived from bars (MT5-parity trades priced with tick
  values);
- `stats.daily_returns` / `meta.data["daily_equity_curve"]` are derived from
  the same curve (`runner/daily_equity.py`), never from the closed-trade ledger
  next to an MTM curve.

**Headline vs balance drawdown (MT5 reports both).** `max_drawdown_pct` is the
*equity* drawdown on the MTM curve. `max_drawdown_realized_pct` (extras) is the
*balance* drawdown of closed-trade net PnL replayed in exit order; it is always
shallower than or equal to the headline figure.

**Sizing is unaffected.** The MTM curve is reporting only: sizing keeps
drawing on realized cash (§4, `TradeEngine._sizing_equity`).

## 5. Execution-timing models (documented divergence)

Three execution-timing models coexist. They are **documented, per-engine
behavior**, not a bug to be fixed by silently changing one engine:

| Engine | Model | Meaning |
|---|---|---|
| native / vectorbt | next-bar-open | Signal on bar *N* — entry **and** signal exit — fills at bar *N+1* open (2026-11-13); protective exits evaluated per bar (vectorbt: per intrabar sub-bar), filling at the level on a touch and at the open on a gap. |
| signal-replay | same-bar-close | External signal payloads fill at the signal bar's close (entries and signal exits alike); slippage/delay recorded in `trade.meta`; protective exits fill at the level on a touch and at the open on a gap. |
| pynecore | post-hoc restatement | Pine execution produces exit prices that are then *recontained* to the recorded protective levels. |

**Pynecore restatement containment.** `_normalize_trade_exits` tightens a
level-violating exit leg to the recorded protective level:

- a LONG exit below `sl_price` is restated to `sl_price` with reason
  `stop_loss` (mirrored for SHORT) — or to the exit bar's **open** when that
  bar opened beyond `sl_price` (gap, 2026-11-13);
- a LONG exit above `tp_price` is restated to `tp_price` with reason
  `take_profit` (mirrored for SHORT), likewise to the exit bar's open when that
  bar gapped through the target;
- an exit inside the levels is left untouched.

Since A-H4 (D3) the adapter additionally runs `_apply_fill_parity` before
sizing: non-level exit legs (signal/time exits) are re-priced to the fill
convention the other engines use — the configured `signal_exit_fill`
(default `next_open`, equal to `open` for the Pine ledger whose legs are
stamped on the fill bar: that bar's open; legacy `close`: that bar's close) —
when the recorded Pine price disagrees with that convention. Level exits
(`stop_loss`/`take_profit`) keep their recorded price. This re-pricing is a
documented divergence from the raw Pine ledger, applied so cross-engine
comparisons measure strategy edge instead of engine artifacts.

The restatement never invents prices beyond the recorded levels and never
rewrites entry legs. Conformance tests pin the containment as a property:
for any exit price, the restated price is either the original price or
exactly the violated level.

## 6. Stats keys

Every engine's `BacktestResult.stats` must be able to expose the canonical
`BacktestStats` key set (see `EngineStatsKeys.REQUIRED` in
`runner/engines/contract.py`): `total_trades`, `winners`, `losers`,
`win_rate`, `profit_factor`, `total_pnl`, `avg_winner`, `avg_loser`,
`max_consecutive_wins`, `max_consecutive_losses`, `max_drawdown_pct`,
`sharpe_ratio`, `sortino_ratio`, `calmar_ratio`, `total_return`, `p_value`
(plus the optional `avg_holding_bars`, `psr`, `max_drawdown_duration`).

Engines satisfy this by routing through `runner.stats.compute_stats` (derived
from trades + equity curve) rather than emitting a bespoke key set. Engine-
specific stats (pynecore strategy stats) are merged **on top of** the
canonical ones via `EngineStatsKeys.merge_over_canonical`; they may add keys
but never remove or overwrite them — a same-named engine key loses the
collision, so consumers always read the canonical value.

## 7. `total_bars`

Decision Record (Cleanup 16-A, F6): **`meta.total_bars` is the number of bars
of market data loaded for the backtest, summed across symbols — it is not the
trade count.**

- native: `total_loaded_bars` (sum of `len(data)` per symbol).
- pynecore: `total_bars` (sum of `len(df)` per symbol).
- vectorbt: bar count loaded by `_process_symbol` per symbol (previously it
  persisted the trade count — fixed under F6).
- signal-replay: no parquet-driven bar count is loaded by the adapter itself
  (frames are provided by the caller); `total_bars` stays `0` there, which is
  honest — it must not be repurposed as a trade count.

## 8. Conformance tests

`tests/test_engine_contract.py` pins the facets above on a deterministic
bundled fixture (module `tests/strategies/contract_sma.py`, no network, no
`DATA_DIR` dependence). The suite covers: the shared policy helpers, the
end-date boundary per engine, stop-first fallback (with flag), chronological
intrabar resolution (native + vectorbt), sizing from the actual stop (including
cap behavior), the canonical stats key set per engine, `total_bars` semantics,
market-entry fill parity, signal-replay's documented same-bar-close exception,
and the pynecore restatement containment property.

`tests/test_differential_reference_strategies.py` complements the conformance
suite with differential tests: three textbook strategies (SMA crossover with
all-in sizing, Donchian breakout with ATR stop/target and fixed-risk sizing,
RSI mean reversion with a time exit) are run on the native and vectorbt engines
and compared trade by trade with small independent reference simulators written
in the test module (synthetic edge-case series and one real EURUSD 15m slice,
zero and non-zero costs). Known engine deviations are recorded there as
`xfail(strict=True)` with the exact location and reproducer.
