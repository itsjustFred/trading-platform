# Cross-Engine Backtest Contract

> **Freeze notice (#318, 2026-11-21).** Per
> `docs/adr/0001-native-single-strategy-engine.md`, `native` is the single
> strategy engine going forward. `vectorbt`, `pynecore` and the LEAN *run*
> path are frozen (bug fixes only, no new features) and scheduled for
> removal; LEAN becomes import-only. This contract will shrink to native +
> signal replay once they are gone. Selecting a frozen engine emits a
> `DeprecationWarning`.

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
- Fill price (owner decision 2026-11-13, supersedes "always the level"): the
  protective **level** on an **intrabar touch** (the bar opens on the safe side
  and trades through the level later); the bar's **open** when the bar **opens
  beyond** the level (gap through a stop — the worse price — or through a
  take-profit in your favour — the price actually available). An open exactly at
  the level fills at that price either way. The gap is the first event of the
  bar, so it also beats a later same-bar touch of the other level and needs no
  intrabar data. The entry bar is never a gap (the position was opened at that
  very open). Shared primitives: `contract.level_gapped_at_open` /
  `contract.level_fill_price` / `contract.resolve_protective_exit`; the
  gap-first ordering (gap through the stop, then gap through the target, then
  the stop-first range test) is implemented in native `_check_stops` and in
  signal replay's `_check_protective_exit_bar` (#287). The frozen vectorbt
  intrabar walk keeps level fills for protective exits (documented divergence;
  its `exit_order_type="level"` signal exits do honour gap → open, §2); pynecore
  restates through its broker emulator + `_normalize_trade_exits` containment.
  SHORT exits buy the ask, so native adds the spread on top of the open on a
  gap fill exactly as on a level fill. A gap fill is a market fill, so a SHORT
  target gapped by less than the spread fills at `open + spread`, which can be
  worse than the target level — realistic. When a
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
`next_open` (default) / `close`; `close` is an explicit legacy opt-in that fills
on the *signal bar itself* (its close). The former `open` mode (fill at the
signal bar's own open) was **removed** (issue #153): an exit computed from bar
*N* cannot be filled at bar *N*'s open without lookahead, and its only
legitimate use (MT5 clock exits) is served by raising the signal one bar early
under `next_open` (e.g. `intraday_volatility_breakout` 1.2.0). `BacktestConfig`
validation rejects `open` with a `ValueError` carrying that migration message
(`contract.normalize_signal_exit_fill`).

Per engine (ADR `docs/adr/0001-native-single-strategy-engine.md` — only native
is under active development):

- native: the exit signal is parked and executed at the next bar's open inside
  `_check_stops`, **after** open-gap exits (which keep their stop/target reason)
  and **before** the bar's intrabar walk and before pending entries fill, so a
  reversal (exit + entry on one bar) closes the old position and opens the new
  one at the same open. SHORT exits pay the spread.
- vectorbt / pynecore (frozen): do **not** implement `next_open`. Their
  adapters map it to the legacy `close` fill with a warning
  (`_frozen_signal_exit_fill`) so a default run cannot silently claim a
  next-open model it did not simulate; explicit `close`/`open` keep working.
- **Clock exits (strategy authors).** A strategy that must be flat *at* a clock time
  T (end-of-day flatten, session close, forced exit) raises its exit signal on the
  bar BEFORE the one that opens at T - the first bar whose end reaches T - so the
  next-open fill lands at T's open. Derive it from the bar spacing plus the clock
  schedule only, never from a later bar (`shared.bar_timing.modal_bar_minutes` /
  `clock_exit_signal_mask`; PyneCore scripts use `timeframe.in_seconds()`), and add a
  first-bar-of-the-next-session safeguard for data gaps. Do not write a market
  `exit_price` for these exits.
- signal replay: external signals — entries and flip/exit-only exits alike — are
  executed when they arrive at the fill bar's close (§5), so "exits like
  entries" already holds; there is no separate next-open exit.

**A signal exit on the last bar** has no next bar to fill at. On native the
position is closed by the forced **end-of-data** close at that bar's close
with reason `end_of_data`, and the trade is flagged
`trade.meta["signal_exit_deferred_to_end_of_data"] = True` (PyneCore's ledger
drops a trade that never closes, signal replay closes end-of-data at the last
close). Stop/TP exits fill at the level or, on a gap, the open (§1).

**Records without `signal_exit_fill`** predate this decision: the old default
was `close` and the key was omitted when it held that default. Every reader —
`BacktestMeta.from_dict`, the SQLite record loader and the optimizer's
base-runtime reconstruction — reads a missing value as
`shared.models.LEGACY_SIGNAL_EXIT_FILL` (`close`), so a legacy result is never
re-run (or re-optimised) under a different exit model than its stored stats.

**Explicit exit order type (#243).** A strategy may mark a signal exit with an
exit-when `exit_order_type` series (signal column or indicator) taking `market`
or `level` (case-insensitive):

- `market` — the exit follows the `signal_exit_fill` convention (on native,
  the next bar's open); any `exit_price` on that bar is ignored for pricing.
- `level` — the exit is a resting-price order, **not** a market signal exit:
  it fills at the strategy's `exit_price` on the signal bar under the §1
  reachability/gap rules, in every engine that consumes the series, and is
  *not* deferred to the next open even when `signal_exit_fill="next_open"`.
  A `level` exit without a usable (positive, finite) `exit_price` falls back
  to the market fill, logs a warning once and flags the trade
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
  `meta["exit_level_gapped"]`.
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
  bar trades through it, including on a gap-through for native (§1),
  which is the per-bar trigger/fill-at-level behaviour `level` deliberately
  does not reproduce. This applies to every level fill, explicit `level`
  and vectorbt's legacy implicit `exit_price` alike.
- absent or unrecognized — the **legacy fallback** applies per engine:
  vectorbt treats a usable `exit_price` as a level fill; native fills at the
  convention price and ignores `exit_price` (an unrecognized non-empty value
  warns once). PyneCore does not consume the series — its exits are
  Pine-native orders, re-priced by `_apply_fill_parity` (§5).

Per-engine support for `exit_order_type` / `exit_price` on signal exits:

| engine | `exit_order_type` | `exit_price` without it |
|---|---|---|
| native (`TradeEngine`) | `market`/`level` honored; absent/unrecognized → market fill (next-open under the default) | ignored |
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
- **`entry` is the price the order actually fills at**, for every order type
  (MARKET: the next bar's open, plus the configured spread for a LONG;
  LIMIT/STOP: their trigger price). Pre-fill
  creation sizing is only a pending-order estimate and must never survive into
  the position: because the caps are evaluated against the fill price, a
  quantity derived from the signal bar's close sizes a position *beyond* the
  cap whenever the next open gaps (repro: 103.4k notional on 100k equity at a
  1x cap). Native's `_fill_pending_orders` re-sizes at the fill for every
  order type, the no-explicit-stop fallback included.
- **The sizing `equity` is realized cash in the native engine** (`initial_capital
  +` realized PnL, net of fees — Cleanup 16-D1 F21/D2; cash from a partial exit
  is reduced by the round-trip fees the open position still owes), never the
  mark-to-market reporting equity of the curve: an open position's paper profit
  is not spendable and a paper drawdown must not deflate new sizes. The native
  engine records that same figure as `equity_at_entry`. **Exception:**
  `SignalReplayAdapter` deliberately marks open positions to market when sizing
  (D9, `docs/cleanup-16d1-money-path-semantics.md`).
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
  is capped by shared `max_leverage`. **#347 exception:** a trade whose
  quantity was clamped by the free-margin leverage rule is rescaled by
  `shared_equity / equity_at_entry` like every other trade — an
  approximation that is not flagged in the trade meta.

## 4b. Reporting equity curve (mark-to-market)

Drawdown, Sharpe, Sortino, volatility and Calmar are computed from the
persisted `equity_curve`, so that curve must show the paper PnL of open
positions. **The native engine persists a mark-to-market (MTM) portfolio
curve** (`runner/engines/mtm_equity.py`, single- and multi-symbol). The frozen
vectorbt / pynecore / signal-replay adapters keep their realized-only curves
until they are removed (ADR `docs/adr/0001-native-single-strategy-engine.md`),
so cross-engine stat comparisons must account for the different curve basis:

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
- a NaN close is not an observation: the last valid close stays the mark;
- runs of unchanged equity with no open position at the running equity peak
  are thinned to their end points (lossless for the daily resampling every
  ratio uses and for drawdown); underwater flat bars are kept, so
  `max_drawdown_duration` is counted in bars of the price series;
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
*balance* drawdown of closed-trade net PnL replayed in exit order. When
positions never overlap it is shallower than or equal to the headline figure;
with concurrent positions (multi-symbol, pyramiding) it can be deeper, because
a realized loss can be offset on the equity curve by another position's
unrealized gain. Consumers must not assume an ordering between the two.

**Sizing is unaffected.** The MTM curve is reporting only: sizing keeps
drawing on realized cash (§4, `TradeEngine._sizing_equity`).

## 5. Execution-timing models (documented divergence)

Three execution-timing models coexist. They are **documented, per-engine
behavior**, not a bug to be fixed by silently changing one engine:

| Engine | Model | Meaning |
|---|---|---|
| native | next-bar-open | Signal on bar *N* — entry **and** market signal exit — fills at bar *N+1* open (2026-11-13); protective exits evaluated per bar, filling at the level on a touch and at the open on a gap. `exit_order_type="level"` exits fill at their level on the signal bar (§2). |
| vectorbt (frozen) | next-bar-open entries / signal-bar-close exits | Entries fill at bar *N+1* open; signal exits keep the legacy trigger-bar fill (`close`, or `open` opt-in) — `next_open` is mapped to `close` with a warning (§2). Protective exits fill at the level on a touch (no gap→open rule; frozen). |
| signal-replay | same-bar-close | External signal payloads fill at the signal bar's close (entries and signal exits alike); slippage/delay recorded in `trade.meta`. Protective exits fill at the level on a touch and at the open on a gap. The `supertrend_flip` leg is the exception: it fills at the next bar's open (see below). |
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
  `stop_loss` (mirrored for SHORT) — or to the exit bar's **open** when that
  bar opened beyond `sl_price` (gap, 2026-11-13);
- a LONG exit above `tp_price` is restated to `tp_price` with reason
  `take_profit` (mirrored for SHORT), likewise to the exit bar's open when that
  bar gapped through the target;
- an exit inside the levels is left untouched.

Since A-H4 (D3) the adapter additionally runs `_apply_fill_parity` before
sizing: non-level exit legs (signal/time exits) are re-priced to the fill
convention the other engines use — the configured `signal_exit_fill`
(default `next_open`; the Pine ledger's legs are
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

### The real-data slice is downloaded, not committed

The one real slice (600 EURUSD 15m bars: real gaps, a weekend, irregular ranges)
used to be a committed fixture under `tests/fixtures/differential/`. It is
generated on demand instead, because this repository is public and vendor market
data is licensed to be *queried*, not redistributed through a history anyone can
clone - and because a fixture inside the test tree reads as a source of truth.

```bash
# writes $DATA_DIR/differential_reference/eurusd_15m_slice.csv
python -m scripts.fetch_differential_slice
```

* `DIFFERENTIAL_REAL_SLICE_CSV` overrides that path (CI points it at a path it
  restores from `actions/cache`, keyed on the fetch script and `data_tools/`).
* `REQUIRE_REAL_DATA_SLICE=1` turns a missing slice into a test failure. CI sets
  it, so the real-data scenarios can never be skipped silently; locally a
  missing slice skips them with a message naming the script.
* A slice that is present but malformed (wrong row count, non-monotonic stamps,
  non-finite OHLC, `high < max(open, close)`, `low > min(open, close)`) always
  fails - see `data_tools/differential_slice.py`, which owns the window, the
  15m aggregation rules (the platform's own, via `data_tools/resample.py`) and
  the validation.
* The committed fixture's provenance was mislabelled: bar for bar it was an
  **MT5 export**, not Dukascopy bid data, so it could not be regenerated from any
  downloader. The slice is now fetched from Dukascopy's bid minute candles for
  the same window; the values therefore differ from the removed file (prices by
  ~0.0008 on average, volumes more, since Dukascopy reports float volumes where
  the MT5 export carried integer tick counts). The contract these tests check is
  engine-versus-reference on one frame, so a different quote series validates
  the same thing - and `tests/test_differential_slice_contract.py` pins the
  window, the rules and the CSV schema without a network.
