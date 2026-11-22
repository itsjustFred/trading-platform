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
- Fill price: the protective **level itself**, not the bar close — with one
  owner-decided exception (2026-11-13, realism / MT5 + live-broker parity): a
  bar that **opens beyond** the level gapped through it and fills at the
  bar's **open**, the price actually available; an open exactly at the level
  fills at that price either way, and intrabar touches keep the level fill.
  The open is the bar's first event, so a gap also beats a later same-bar
  touch of the other level. The shared helpers are
  `contract.level_gapped_at_open` / `contract.level_fill_price` /
  `contract.resolve_protective_exit`. On main today this is implemented for
  the signal-replay `supertrend_flip` stop/TP1 exits (#287); the remaining
  engine paths adopt it with #208. When a
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
MT5-parity runs. Stop/TP exits fill at the level in every engine (§1),
except that a bar opening beyond the level fills at its open (gap rule,
§1).

**Explicit exit order type (#243).** A strategy may mark a signal exit with an
exit-when `exit_order_type` series (signal column or indicator) taking `market`
or `level` (case-insensitive):

- `market` — the exit fills at the signal-exit convention price
  (`signal_exit_fill`); any `exit_price` on that bar is ignored for pricing.
- `level` — the exit fills at the strategy's `exit_price`. A `level` exit
  without a usable (positive, finite) `exit_price` falls back to the market
  fill, logs a warning once and flags the trade
  `meta["exit_level_missing_price"]`.
- a `level` outside the exit bar's traded range is never booked at the
  level (`contract.exit_level_disposition`, evaluated on the ask range for
  a SHORT exit when `ask_ohlc` data is enabled). On the *untraded* side —
  above the bar's high for a LONG exit, below the low for a SHORT exit —
  the order could never have filled: it falls back to the market fill,
  warns once and flags `meta["exit_level_unreachable"]`. On the
  *marketable* side — below the low for a LONG exit, above the high for a
  SHORT exit — the resting order was marketable at the open, so under the
  gap-through-level convention it fills at the bar's **open** and flags
  `meta["exit_level_gapped"]`. (This is `level`-exit semantics only:
  protective stop/TP legs fill at their level even on a gap-through —
  gap → open for them lands with #208.)
  Direction-specific, like every open fill: a LONG exit sells the bid at
  the raw open, while a SHORT exit buys the ask at `open + spread` when a
  spread is configured. With explicit `ask_high`/`ask_low` columns but no
  `spread_price_col`, reachability is still judged on the ask range while
  the fill stays the raw open (spread 0) — the same convention every
  open fill already follows. `level` has **limit-order semantics**: an
  untraded-side level is "unreachable" and falls back to the market fill.
  A strategy whose `exit_price` is really a *stop* (e.g. a sell stop
  above the whole bar after a gap down) should express it through the
  `stop_loss` legs instead — a stop leg fills at its level whenever the
  bar trades through it, including on a gap-through (gap → open for
  protective exits lands with #208), which is the per-bar
  trigger/fill-at-level behaviour `level` deliberately does not
  reproduce. This applies to every level fill, explicit `level`
  and vectorbt's legacy implicit `exit_price` alike.
- absent or unrecognized — the **legacy fallback** applies per engine:
  vectorbt treats a usable `exit_price` as a level fill; native fills at the
  convention price and ignores `exit_price` (an unrecognized non-empty value
  warns once). PyneCore does not consume the series — its exits are
  Pine-native orders, re-priced by `_apply_fill_parity` (§5).

Per-engine support for `exit_order_type` / `exit_price` on signal exits:

| engine | `exit_order_type` | `exit_price` without it |
|---|---|---|
| native (`TradeEngine`) | `market`/`level` honored; absent/unrecognized → market fill | ignored |
| vectorbt | `market`/`level` honored; absent/unrecognized → legacy rule | usable value → level fill (legacy) |
| pynecore | not consumed — Pine-native orders, `_apply_fill_parity` (§5) | n/a |
| signal_replay | not consumed — replays recorded fills | n/a |
| lean | not consumed | n/a |
| live (`live_runner/`) | **not consumed** — every signal exit is a market order, so a strategy emitting `level` diverges between backtest and live | n/a |

The order type is evaluated only on a real exit signal in every consumer: the
forced end-of-data close fills at the convention price and ignores
`exit_price`/`exit_order_type` entirely, matching native's `end_of_data`
close. The parsing lives in `runner.engines.contract`
(`normalize_exit_order_type`, `usable_exit_price`, `exit_level_disposition`) —
native and vectorbt read the same spelling table; the other engines listed
above do not consume the series at all.

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
  unavailable** — with one qualifier: a bar that *opens* beyond a level is not
  a same-bar ambiguity at all. The open is the bar's first event, so a gap
  through a level fills at the open and is checked before the stop-first
  tie-break (`resolve_protective_exit`, owner decision 2026-11-13, #287).
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

- **Sizing basis (native, vectorbt, pynecore):** the `equity` every sizing
  rule below denominates is **realized cash** — `initial_capital` plus the net
  PnL of closed trade legs. The mark-to-market equity that feeds the equity
  curve is never a sizing input: an open position's paper profit is not
  spendable and an unrealized drawdown is not a realized loss. The one
  documented exception is the native free-margin leverage check (#347),
  which reads mark-to-market equity and used margin by design (MT5-style
  margin) — see `docs/sizing-semantics.md`. Native holds it
  in `TradeEngine._sizing_equity` (decision record D2,
  `docs/cleanup-16d1-money-path-semantics.md`); vectorbt accumulates it inline
  and records it as `trade.meta["risk_math"]["equity_at_entry"]`; pynecore walks
  the same realized ledger. A partial exit's proceeds enter the basis as soon
  as the leg closes, less the fee model's cost for the legs that position has
  already incurred (entry leg on its original quantity, plus each closed exit
  leg) — never the exit leg its still-open remainder has not taken. The basis
  is therefore *net realized cash*, not "`initial_capital` until the whole
  trade closes" and not gross proceeds. MT5 is consistent with this basis but
  cannot decide it: its EA risks a fixed cash amount and reads no account value
  at all, and the repo's MT5 sizing code and the vectorbt runner each hold at
  most one position, so they never size while a position is open.
  **Documented per-engine divergence:** signal replay sizes on realized cash
  *plus* the mark-to-market PnL of open positions (decision record D9 / F39,
  pinned by `tests/test_portfolio_sharpe_annualization.py::TestReplaySizingEquity`)
  — that engine's entries must react to an open drawdown, and its divergence
  from the realized-only engines is a recorded decision, not an oversight.
  Do not "align" one side without revisiting the corresponding decision record.
- **Risk-based sizing** (`risk_per_trade_pct` set): position quantity is
  derived from the *actual* stop distance,
  `qty = (equity × risk_per_trade_pct) / |entry − stop|`, capped by the
  max-position notional (`equity × max_position_pct`) and the max-leverage
  notional (`equity × max_leverage`). When the risk target cannot be met
  inside the caps, the position is filled up to the cap (never beyond) —
  vectorbt records the decision in `trade.meta["risk_math"]`
  (`sizing_mode`, `risk_target_usd`, `stop_distance`, `required_notional`,
  `final_notional`, `capped_by_max_position`, `capped_by_max_leverage`).
  **Leverage-cap basis divergence (#347):** vectorbt holds at most one
  position, so its free margin always equals equity and its
  `equity × max_leverage` cap already *is* the MT5 rule. PyneCore keeps
  overlapping trades (`open_ledger`) and caps
  `equity × max_leverage − open_notional` on *realized* equity — a
  different rule; it is frozen and slated for deletion (#318). The native
  engine sizes against **free margin** — mark-to-market equity minus the
  margin open positions already use — and clamps the quantity (floored to
  `lot_step` when the leverage cap binds) instead of refusing; an unknown
  broker maximum means 1x.
- Native additionally supports `fixed_risk_amount` sizing with MT5 tick
  metadata (`tick_size`, `tick_value`, `lot_step`, `min_lot`, `max_lot`);
  the result is still capped by `max_leverage` (free margin, #347) unless
  `cap_fixed_risk_by_leverage = false`. With `lot_step > 0`, `max_lot` is
  floored to `lot_step` once, so an off-grid `max_lot` cannot yield an
  off-grid size; the documented exception is no grid at all — with
  `lot_step` absent, null or zero the native engine has no grid, so
  `max_lot` applies raw and an off-grid maximum stays off-grid. Because the
  leverage/exposure cap is a raw equity/price ratio, a size that the cap
  reduces is re-floored to `lot_step` afterwards (Issue #151). Every grid
  floor — `max_lot`, the uncapped size and the capped re-floor — is
  `shared.lot_grid.floor_qty_to_step` (#278), the same decimal floor the
  live exit/entry paths use, so a backtest size and the live order built
  from it cannot disagree by one step at the edge (`0.3` on a `0.1` grid).
  With a grid
  configured, a final size within the `min_lot` snap band (relative 1e-9)
  below `min_lot` is snapped up to `min_lot`. Since the decimal grid floor
  returns exact products, that can only happen when `min_lot` itself is off
  the grid a hair above a grid multiple, and the broker accepts the size as
  `min_lot`. Without a grid there is no floored product to sit in the band,
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
  is capped by shared `max_leverage`. **#347 exception:** a trade whose
  quantity was clamped by the free-margin leverage rule is rescaled by
  `shared_equity / equity_at_entry` like every other trade — an
  approximation that is not flagged in the trade meta.

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
bar's close. This is the only replay *signal* exit that is not a level fill and
not a same-bar-close fill (the level exits themselves can fill at the open on a
gap — see the §1 gap rule below). The run-level `signal_exit_fill` stays `close` for replay
(the replay has no `open` model); each flip trade records
`meta["supertrend_flip"]["flip_exit_fill"] = "next_bar_open"`.

The workflow's level exits — the stop before and after TP1, and the TP1
tranche — follow the §1 gap rule (#287): a bar
that opens beyond the level fills at the open (`contract.resolve_protective_exit`,
gap checked before the stop-first same-bar policy), an intrabar touch at the
level. One qualification inside the TP1 bar: a bar that gaps through TP1
fills the tranche at the open, but the remainder's pre-TP1 stop — still
working live until the breakeven amendment takes effect on the next bar —
is checked against the rest of that bar's range, so a later same-bar trade
through it stops the remainder at the stop level. The entry bar is never
evaluated (the fill bar's close is the entry and
`last_checked_at` starts there), so no `allow_gap=False` exemption is needed.

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
