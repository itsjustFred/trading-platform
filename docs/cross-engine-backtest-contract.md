# Cross-Engine Backtest Contract

Status: contract for the backtest engine adapters (`runner/engines/`), shared
primitives in `runner/engines/contract.py`, conformance tests in
`tests/test_engine_contract.py`.

Every engine adapter — `NativeAdapter` (+ `TradeEngine`),
`VectorbtAdapter`, `PyneCoreAdapter`, `SignalReplayAdapter`, `LeanAdapter` —
must satisfy the facets below. The conformance suite pins them on a
deterministic bundled fixture, so a divergence shows up as a failing test, not
as a silent parity drift between engines.

Decision Records referenced below are from Cleanup 16-A (2026-08-31) and the
parent Cleanup 16 record.

## 1. Fills

### Market entry

- **Convention: next-bar-open.** A signal produced on bar *N* executes on bar
  *N+1*, filling at bar *N+1*'s **open**.
- Native: orders created on the signal bar are filled by
  `_fill_pending_orders` only on a later bar (`i > created_index`), at
  `bar_open`. The same creation-index guard applies to the native engine's
  LIMIT/STOP/STOP_LIMIT pending-order branches (#288): a pending order may
  never fill on the bar whose close produced its signal.
  `ExecutionModelSettings.market_order_timing = "next_bar_open"` and
  `entry_price_side = "open"` document this on every native trade
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
- Fill price: the protective **level itself**, not the bar close. When a
  trailing stop is active, native reports reason `trailing_stop`; otherwise
  `stop_loss`. Native names take-profit exits after the level name (`tp`,
  `tp1`, …); vectorbt and signal replay use `take_profit`. Consumers must
  treat both spellings as the same event class.
- Vectorbt has no bar-loop position model: protective levels are resolved on
  the entry bar and enforced through the intrabar walk (§3). Without intrabar
  data, exits happen on the scheduled signal exit and the levels are recorded
  in `trade.meta` (`sl_price`, `tp_price`).

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

**Signal exit fill convention (A-M1/D4).** A signal exit (not a protective
level exit) fills at the **trigger bar's close** by default (exception: the
`supertrend_flip` leg, filled at the next bar's open, see below); the convention
is one place — `_prepare_next_open_execution_data` keeps the exit leg
unshifted in vectorbt, and `BacktestConfig.signal_exit_fill` (validated to
`close`/`open`) selects `open` to fill at the exit bar's open instead for
MT5-parity runs. Stop/TP exits fill at the level in every engine (§1).

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
- Native additionally supports `fixed_risk_amount` sizing with MT5 tick
  metadata (`tick_size`, `tick_value`, `lot_step`, `min_lot`, `max_lot`);
  the result is still capped by `max_leverage` unless
  `cap_fixed_risk_by_leverage = false`. With `lot_step > 0`, `max_lot` is
  floored to `lot_step` once, so an off-grid `max_lot` cannot yield an
  off-grid size; the documented exception is no grid at all — with
  `lot_step` absent, null or zero the native engine has no grid, so
  `max_lot` applies raw and an off-grid maximum stays off-grid. Because the
  leverage/exposure cap is a raw equity/price ratio, a size that the cap
  reduces is re-floored to `lot_step` afterwards (Issue #151). With a grid
  configured, a final size within the grid snap band (relative 1e-9) below
  `min_lot` is snapped up to `min_lot` — that size came out of the grid
  floor an ulp low (`3 * 0.3 == 0.8999999999999999 < 0.9`) and the broker
  accepts it as `min_lot`. Without a grid there is no such float product,
  so the exception does not apply: a capped size below `min_lot` is a cap
  the configured minimum exceeds, and raising it would push the size above
  the cap. Any other final size below `min_lot` is **skipped** (size 0, no
  trade, with a WARNING log) — it is never raised back up to `min_lot`,
  since the caps are what make it unaffordable. A size that no cap reduces
  is not re-floored. `min_lot` is deliberately honoured **off-grid**: the
  MT5 parity reference (`scripts/regenerate_mt5_parity_artifacts.py`,
  `_round_mt5_lots`, .mq5 Z.391-393) applies `max(floored, min_lot)` without
  a second grid floor (MT5 `MathMax`), so the engine reproduces that rule
  rather than correcting it; a venue that requires volumes to be exact step
  multiples would reject such a size, and a config for one must set
  `min_lot` to a grid multiple. `max_lot` is floored to the grid instead;
  with `min_lot == max_lot == 0.15` and `lot_step = 0.1` that combination is
  fail-closed — the cap becomes `0.1`, which is below `min_lot`, and the
  trade is skipped although the same config without `max_lot` trades `0.15`.
  Non-finite values for the sizing parameters are refused with a `ValueError`
  naming the parameter: `inf` would crash the grid floor, and `nan` defeats
  every guard on the sizing path. `fixed_risk_amount` is resolved — and thereby
  validated — **before any stop-dependent branch**, so a `nan`, `-inf` or
  negative amount is refused on **every** path: with a stop, without a stop,
  and with an unusable stop distance. `nan > 0`, `-inf > 0` and a negative
  amount all compare False, so a value checked after a branch is chosen is
  checked too late: on the stopful and the stopless path the trade was then
  sized by `risk_per_trade_pct` instead — a backtest under a risk rule the
  config does not declare, with no error at all (the unusable-distance path
  raised, but named the distance, never the amount). A configured
  `tick_value`/`tick_size` of zero or less is refused as well — `0 or 1.0`
  used to substitute `1.0` and silently drop that leg of the conversion,
  oversizing the order — and so is a negative `lot_step`/`min_lot`/`max_lot`,
  which the `> 0` gates used to discard. Those are parameters of the
  fixed-risk conversion, so they are resolved with that branch, before the
  risk-distance gate: an unusable stop distance is refused too (no risk
  distance means no size), and reporting which of the two is the config error
  must not depend on the bar data. An absent key, an explicit `null`, and
  `0.0` for `fixed_risk_amount` ("off", size from `risk_per_trade_pct`) keep
  their previous meaning. The order's `equity_proportional_sizing` flag reads
  the same way, so a `0.0` order is **now** re-scaled by the shared-equity
  replay like a percentage-sized one: the flag used to read the key's presence
  instead, and stopped the re-scale for a percentage-sized position (Issue
  #170).
- **Equity sizing** (no risk config): `qty = equity / entry_price`, capped by
  `max_leverage`.
- Without an explicit stop, engines fall back to a documented default stop
  distance (native/risk engine: `fallback_stop_pct = 2%`; vectorbt: 2% of
  entry) — the fallback must be visible in the trade meta, not silent.
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

## 5. Execution-timing models (documented divergence)

Three execution-timing models coexist. They are **documented, per-engine
behavior**, not a bug to be fixed by silently changing one engine:

| Engine | Model | Meaning |
|---|---|---|
| native / vectorbt | next-bar-open | Signal on bar *N* fills at bar *N+1* open; protective exits evaluated per bar (vectorbt: per intrabar sub-bar). |
| signal-replay | same-bar-close | External signal payloads fill at the signal bar's close; slippage/delay recorded in `trade.meta`. The `supertrend_flip` leg is the exception: it fills at the next bar's open (see below). |
| pynecore | post-hoc restatement | Pine execution produces exit prices that are then *recontained* to the recorded protective levels. |

**`supertrend_flip` exit (signal-replay, #168).** The flip leg is a signal exit
decided at the close of the first red bar after activation and filled at the
OPEN of the next bar, so a flip on the last bar closes as `end_of_data` at that
bar's close. This is the only replay exit that is not a level and not a
same-bar-close fill. The run-level `signal_exit_fill` stays `close` for replay
(the replay has no `open` model); each flip trade records
`meta["supertrend_flip"]["flip_exit_fill"] = "next_bar_open"`.

**Pynecore restatement containment.** `_normalize_trade_exits` tightens a
level-violating exit leg to the recorded protective level:

- a LONG exit below `sl_price` is restated to `sl_price` with reason
  `stop_loss` (mirrored for SHORT);
- a LONG exit above `tp_price` is restated to `tp_price` with reason
  `take_profit` (mirrored for SHORT);
- an exit inside the levels is left untouched.

Since A-H4 (D3) the adapter additionally runs `_apply_fill_parity` before
sizing: non-level exit legs (signal/time exits) are re-priced to the fill
convention the other engines use — the configured `signal_exit_fill`
(default `close`: the trigger bar's close; `open`: the next bar's open) —
when the recorded Pine price disagrees with that convention. Level exits
(`stop_loss`/`take_profit`) keep their recorded level. This re-pricing is a
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
