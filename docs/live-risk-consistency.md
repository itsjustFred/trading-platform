# Live Risk Consistency

## Problem

Position sizing in backtests and in live trading is built on the same `RiskEngine`.
Live has exactly **one** sizing entry point (#125):
`live_runner.daemon.size_live_signal_for_submission()`. The second entry point that
used to exist, `risk.live.size_live_signal()`, had no production caller and has been
removed.

| Module | Purpose | Differences |
|--------|---------|-------------|
| `risk/engine.py` | Backtest risk engine | Complete `RiskEngine` class with a configurable `RiskConfig` |
| `risk/live.py` | Live levels and gate rules | `resolve_live_levels()` (the only entry/SL/TP resolver), `entry_gate_settings()`, `entry_deviation_sl_fraction()`; no sizing |
| `live_runner/daemon.py` | Live sizing and submission | `size_live_signal_for_submission()` (the only live sizing entry point) / `prepare_order_with_risk()`: sizing with the deployment's `RiskConfig` through `RiskEngine`, levels from `resolve_live_levels()`, entry-deviation gate |

## Critical Differences

### 1. Stop-Direction Enforcement

**Backtest (`risk/engine.py`):**
```python
enforce_stop_direction = bool(signal.metadata.get("enforce_stop_direction", False))
```
Default: `False` (not enforced)

**Live (`live_runner/daemon.py`, `size_live_signal_for_submission`):**
```python
"enforce_stop_direction": True,  # hardcoded
```
Live always enforces that the stop loss points in the right direction.

### 2. RiskConfig Defaults

Both paths use the same defaults from `RiskConfig`:
- `max_risk_per_trade: 0.01` (1%)
- `max_total_exposure: 0.05` (5%)
- `max_positions: 5`
- `default_sl_atr_multiplier: 2.0`
- `fallback_stop_pct: 0.02` (2%)

### 3. Sizing Formula

Both paths use the same formula:
```
position_size = risk_amount / (risk_distance * pip_value)
```

Where:
- `risk_amount = balance * risk_percent`
- `risk_percent = min(max_risk_per_trade, max_total_exposure - current_exposure)`
- `position_size = min(position_size, max_notional_per_trade * balance / (entry_price * pip_value))`
  (notional ceiling, `max_notional_per_trade` as a fraction of the balance, default 0.20).
  The notional of one position in account currency is `position_size * entry_price * pip_value`
  (#146: without `* pip_value` the ceiling was 1000x too loose for cTrader FX).
- `pip_value` is the P&L of **one** `position_size` unit per price move of 1.0, in
  account currency (not per pip). US equities in USD on a USD account: 1.0; cTrader EURUSD
  on a USD account: 1000 (one qty = 0.01 lot = 1,000 EUR).

## Units Contract (Cleanup 20-A)

All risk limits are fractions of the account balance (fraction of balance):

| Quantity | Unit |
|----------|------|
| `max_risk_per_trade` | Fraction of the balance (maximum loss per trade) |
| `max_total_exposure` / `current_exposure` | Fraction of the balance (aggregate open risk) |
| `max_notional_per_trade` | Fraction of the balance (notional ceiling per position) |
| `SizedOrder.position_size` / `qty` | Account-currency units of the symbol (shares/lots) |

Trust boundary: signal metadata is **untrusted** publisher payload. It may only supply
trading descriptors (entry price, stop, take profit, ATR, timeframe). Never read from
metadata: `qty`/`explicit_qty` (quantity override), `max_risk_per_trade`,
`max_total_exposure`, `max_positions`, `current_positions`, `current_exposure`,
`pip_value` (#125: a payload pip value scaled the quantity directly) and
`max_entry_deviation_sl_fraction`. The pip value comes from the broker
(`adapter.get_pip_value()`, #146) or from a trusted caller; there is no implicit 1.0 in
the live path any more.
Risk policy comes exclusively from deployment/runtime config (`RiskConfig`), state
(`current_positions`, `current_exposure`) from the portfolio/broker source of truth. The
former `qty`/`explicit_qty` bypass around the whole risk engine is removed;
`size_live_signal_for_submission` sizes every live signal through `RiskEngine`.
State fail-closed (#263): a broker position whose `qty` is unreadable (not numeric,
`nan`/`inf`, missing, or Alpaca's `qty_unparseable` marker) is no longer counted as 0 -
that would read as "the account is flat", and the next entry would be sized against an
empty book although `max_positions` or `max_total_exposure` are already full. The count
raises `UnreadableBrokerPositionQtyError`, the entry is refused (nothing goes to the
broker) and an operator gets an ERROR alert. Exit orders are not affected: they size
against the deployment's own ledger, not against the count.

## Broker Pip Value (#146)

`prepare_order_with_risk()` fetches `adapter.get_pip_value(symbol)` before sizing
(`LivePipValue`: `value = units_per_qty * conversion_rate`):

- **cTrader:** from `ProtoOASymbol.lotSize` (in cents of the base currency; EURUSD
  10,000,000 = 100,000 EUR), i.e. `units_per_qty = lotSize / 100 / 100`, plus the
  conversion of the quote currency into the account currency along the broker chain
  (`ProtoOASymbolsForConversionReq`) with live quotes. Per hop the side that makes the
  rate **higher** is taken (ask when multiplying, bid when dividing): a higher pip value
  sizes smaller, so the spread can only push the loss at the SL below the budget. That
  holds only for the conversion quote at sizing time: cross-currency rates can move until
  the stop, and submission checks only the symbol scale, not the conversion rate. The SL
  budget is therefore not a hard loss limit in account currency. An explicit
  `pip_value` override (`source="caller"`) carries no `units_per_qty`; cTrader refuses it
  because the scale would not be verifiable at submission. Every use of the override
  writes a warning to the log (`Caller-supplied pip_value override in use`), since it
  bypasses the broker lookup including its freshness check.
  The cTrader volume divisor (`CTRADER_VOLUME_PER_BASE_UNIT = 100`) has only been
  verified on the demo symbol classes it was verified on (EURUSD/FX, XAUUSD, US500); for
  other classes it is unverified.
  Only a broker-sent `lotSize` counts; the static fallback table (in base units, 100x
  below the broker cents for FX/XAU, #184) is never used for sizing. If the scale changes
  between sizing and submission (e.g. a reconnect with a failed props fetch), the adapter
  refuses the order (`pip_value_units_per_qty`).
- **Alpaca:** 1.0 (shares/coins in USD on a USD account). A non-USD account or a pair not
  quoted in USD (`ETH/BTC`, `BTC/USDT`) is refused.

Fail closed (entry refused, reason in the log `Pip value unavailable`): adapter without
`get_pip_value`, error or timeout (`PIP_VALUE_FETCH_TIMEOUT_SECONDS` = 60 s,
`live_runner/daemon.py:1223`), a value that is not finite and positive,
`value != units_per_qty * conversion_rate`, a rate other than 1 without quotes, or a
conversion quote that violates the entry gate's freshness rule (missing/invalid time,
future, older than `max_entry_quote_age_seconds`). Exit-only orders need no pip value.

Worst-case wait before an entry is submitted: the daemon bounds both broker fetches it
makes on the entry path — 60 s for the pip value (`PIP_VALUE_FETCH_TIMEOUT_SECONDS`,
`live_runner/daemon.py:1223`) and 20 s for the entry-gate quote
(`ENTRY_QUOTE_FETCH_TIMEOUT_SECONDS` = 20.0, `live_runner/daemon.py:1086`). Pip value and
sizing run before the entry-deviation gate, so an entry that times out on both waits about
80 s before it is refused. The cTrader quote timeout of 5 s
(`quote_timeout_seconds = 5.0`, `live_runner/ctrader_adapter.py:849`) is an
adapter-internal bound inside that 20 s.

Persisted on the signal: `pip_value`, `pip_value_source`, `pip_value_units_per_qty`,
`pip_value_conversion_rate`, `pip_value_quote_asset`, `pip_value_account_asset`,
`pip_value_conversion_symbols`, `pip_value_conversion_quote_times`,
`pip_value_conversion_quote_ages_seconds`. Payload values under these names are always
overwritten.

Consequence of the corrected notional cap: `RiskConfig` accepts
`max_notional_per_trade <= 1` (no leverage; `risk/config.py:62-63`). On a 100-USD account
0.01 lot EURUSD (~1,100 USD notional) is already above that cap, so such entries are
refused (below `minVolume`).
**Pending #240:** the maintainer decided on 2026-11-16 that leverage may be allowed up to
the broker maximum per account and asset, with the entry size (risk at the stop) keeping
the exposure sane. No PR implements it yet, so the 1× ceiling is a current validation
limit and not a permanent design rule.

## Single Level Resolver (#125)

`risk.live.resolve_live_levels()` is the only source for the entry, SL and TP of a live
signal. The daemon sizes **and** submits from it, so quantity and broker levels can never
drift apart:

- **Entry:** metadata `entry_price`, otherwise `theoretical_price`. The signal's
  `theoretical_price` passed to the adapter is set to this entry (cTrader computes
  relative SL/TP distances from it).
- **SL:** first set alias of `stop_loss`, `sl_price`, `stop_price`,
  `resolved_stop_loss`; if it is missing, the risk engine sets the fallback stop, and
  exactly that one goes to the broker. A payload `resolved_stop_loss` can no longer
  overwrite the sized stop.
- **TP:** first set alias of `take_profit`, `tp_price`. A missing TP is not a resolver
  error; a malformed TP (non-numeric, boolean, non-finite, `<= 0`) is refused. Submission
  rule C1 stands: without a TP only with an explicit
  `take_profit_policy="unbounded_exit_on_opposite_signal"`.

## Entry Deviation Gate (#125)

Before the market order is submitted, `prepare_order_with_risk()` compares the entry with
the current broker quote (`adapter.get_quote()`): **ask for buys, bid for sells**.

```
deviation = (entry - quote) / (entry - stop)
```

as a fraction of the SL distance, with sign: positive means **toward the SL**, negative
**away from the SL**. Refused (rules in this evaluation order; the name lands as
`entry_gate_rejection_rule` in the log and in the error message). First the quote must be
**current**: an after-hours "latest" quote can be hours old, and no price check may rest
on it.

| Rule | Condition |
|------|-----------|
| `quote_time_missing` | Quote without timestamp |
| `quote_time_invalid` | Timestamp is not a timezone-aware `datetime` |
| `quote_time_in_future` | Timestamp more than 2 s (clock-skew tolerance, inclusive) in the future |
| `quote_stale` | Quote older than `max_entry_quote_age_seconds` (exactly at the limit is allowed) |
| `quote_at_or_beyond_stop_loss` | Buy: ask `<=` SL. Sell: bid `>=` SL |
| `quote_at_or_beyond_take_profit` | only with a TP. Buy: ask `>=` TP. Sell: bid `<=` TP |
| `deviation_toward_stop_loss` | `deviation > max_entry_deviation_sl_fraction` (setup possibly invalid) |
| `deviation_away_from_stop_loss` | `-deviation > max_entry_deviation_sl_fraction` (chasing: with a fixed SL the real risk is larger than what was sized) |

Both limits come per deployment from `settings`, never from signal metadata, and are
validated by `risk.live.entry_gate_settings()`: when the deployment is created
(`shared.database.create_live_deployment` and the API endpoint, 400) and again on dispatch
(fail-closed, before the quote fetch):

| Setting | Default | Valid |
|---------|---------|-------|
| `max_entry_deviation_sl_fraction` | **0.25** | `0 <= x <= 1` (fraction of the SL distance; `25` meant as 25 % is an error; `0` allows only a quote exactly at the entry) |
| `max_entry_quote_age_seconds` | **10** | finite, `> 0` |

The quote fetch itself is bounded in the daemon at 20 s
(`ENTRY_QUOTE_FETCH_TIMEOUT_SECONDS`); a timeout refuses the entry. The comparison has a
tolerance of `1e-9`, so float rounding at the boundary does not decide.
Without a TP (explicit unbounded policy) only the TP rule drops out. The quote is a
**gate only**, never a sizing input: quantity and levels stay those of the signal.

Fail-closed: an adapter without quote support, a fetch error, a timeout, and missing,
non-positive, non-finite, crossed (bid > ask) or non-current quotes refuse the entry and
are logged. Exit-only orders do not go through the gate.

Sources: Alpaca `get_stock_latest_quote` (IEX feed, like the bars), the timestamp is that
of the quote itself, never `now()`; cTrader briefly subscribes to the symbol's spot
stream, takes the first complete bid/ask and unsubscribes again (timeout
`quote_timeout_seconds`, default 5 s). The timestamp is the broker time of the spot event,
otherwise the local receive time (`timestamp_source="receive"`; a spot event is pushed
live, so its receive time bounds its age).

Persisted on the signal metadata: `entry_quote_bid`, `entry_quote_ask`,
`entry_quote_side`, `entry_quote_price`, `entry_quote_time` (ISO-8601),
`entry_quote_time_source` (`broker` / `receive`), `entry_quote_age_seconds`,
`max_entry_quote_age_seconds`, `entry_quote_source`,
`entry_deviation_sl_fraction` (with sign), `entry_deviation_direction`
(`toward_stop_loss` / `away_from_stop_loss` / `none`),
`max_entry_deviation_sl_fraction`, `entry_gate_rejection_rule` (`None` for accepted
signals; refused ones are not persisted yet, only logged).
On entries, payload values under these keys are overwritten; on exits (which run no gate)
they are removed. A schema for quote snapshots (of refused signals too) is #134.

## Kill Switch (#300)

Before every entry `prepare_order_with_risk()` refuses the order when the account is
halted (`live_runner.kill_switch.refuse_entry_while_halted`). The check sits **after** the
exit branch, so it never blocks closing a position, and **before** any broker access: a
halted account costs no round trip.

The state lives in `live_kill_switch` (one row per scope: `global` plus one per live
account) and is re-read on every entry and on every tick - no process cache, or a restart
would lift the halt. The strictest mode wins (`entries` < `orders` < `flatten`); an account
scope can never soften a global halt.

Fail closed deliberately means the cheap side here: an unreadable state or a mode that
cannot be interpreted refuses entries and does not touch the broker. An empty read cannot
be told apart from "nothing is halted" - the one answer a kill switch must never produce
by accident.

Set, lift and read: `scripts/live_kill_switch.py` (CLI) and `/api/live/kill-switch`
(viewer); operating details in `docs/live-deploy-runbook.md`.

## Commission Sign (#184 finding 14)

`live_executions.commission` is a **non-negative cost value** on **all** brokers (fee =
positive, known-null = `0.0`; #285). Net P&L is
`gross - commission` everywhere (viewer, `net_pnl`, Alpaca CFEE backfill from #143).
Adapters normalize before storing: cTrader reports `ProtoOADeal.commission` as a signed
cash flow (fee negative) and is negated via `base_adapter.commission_from_signed_cash_flow`; Alpaca reports
no commission (known null `0.0` or NULL, see #124) and backfills CFEE as a positive cost.
Since #285 the value is guarded on the write side: `LiveExecution.commission` validates
through `shared.models.validate_commission_cost` (`mode="before"`; `bool` and numeric
strings are refused too) and raises `ValidationError` on a negative or non-finite value;
at the persistence threshold `LiveExecutionModel.from_live_execution` checks again and
raises `ValueError`. Only this funnel is guarded - a direct `LiveExecutionModel(...)` or
`session.add` does not validate. `commission_from_signed_cash_flow` is the only permitted
conversion of the sign convention; its negative result (i.e. a cTrader rebate) is stored
by the adapter as `commission=NULL` ("fee unknown") plus an ERROR log so the fill is not
lost - how a rebate should actually be represented is an open maintainer decision.
`apply_live_execution_commissions` (Alpaca CFEE) refuses negative values as well. The read
path (`LiveExecutionModel.to_live_execution`) builds a `LiveExecution` and so validates
again; it currently has no production callers - the net-P&L readers consume the raw
`live_executions` rows directly.
Already-stored cTrader rows (before this change) carried the raw negative sign; `init_db`
converts them once via migration (`_migrate_ctrader_commission_to_cost`, `abs` of the
stored value) into a positive cost. Only accounts with broker `ctrader` are affected;
NULL (unknown) and `0.0` stay. The migration is recorded in `schema_data_migrations` and
runs exactly once: since #285 no negative value can be written any more, but a second
`abs` would needlessly touch an already converted value. A cTrader rebate stored before
the change (raw positive) cannot be distinguished from a new fee and stays positive.
`commission_from_signed_cash_flow` refuses non-finite values with `ValueError` (no silent
`0.0`).
Deployment note: stop the daemon before the first start of the new build (see
`docs/live-deploy-runbook.md`); an old build that still writes a raw negative fee after
the migration record is no longer converted.

## Broker Grid (Alpaca)

Alpaca accepts bracket/OTO entries only in whole shares and with SL/TP on the price grid
(0.01 from 1 USD, 0.0001 below that). `AlpacaLiveAdapter` normalizes every entry before it
is persisted: the risk-sized `qty` is rounded **down** to whole shares (below 1 share → refused),
and SL and TP are rounded **toward the entry**. The actual risk therefore never exceeds the
sized budget. Exit-only orders (simple) stay unchanged.

Refused before persistence in addition (ValueError, no signal row, no order): SL/TP on the
wrong side of the entry, SL/TP within one tick of the entry, a missing/invalid SL/TP level,
extended-hours brackets and crypto symbols (Alpaca has no bracket/OTO orders for crypto).

Crypto consequence on main: every Alpaca crypto entry is refused before submission —
`AlpacaLiveAdapter._normalize_bracket_entry` classifies the symbol and raises for
`classify_alpaca_symbol(symbol) == "crypto"`
(`live_runner/alpaca_adapter.py:1268-1273`). Even a plain (non-bracket) crypto entry as
proposed in #241 would be refused today by the entry-deviation gate, because that gate
needs a quote and `AlpacaLiveAdapter.get_quote` requests only
`StockLatestQuoteRequest(feed="iex")` (`live_runner/alpaca_adapter.py:1751`) — there is no
crypto quote path. Crypto bars themselves are available (`get_market_data` uses
`CryptoBarsRequest`), so the blockers are order submission and the gate quote, not market
data. **Pending #241** (BTC on Alpaca as a plain order plus a separately managed stop) has
to change both.

## Broker Grid (cTrader)

cTrader receives SL/TP as relative distances in 1/100000 of the price. The grid is
`10 ** max(0, 5 - price_digits)` of the symbol. Both distances are rounded **down toward
the entry** (`floor`), so the actual risk never exceeds the sized budget: a stop of
1.6 ticks goes out as 1 tick. A stop below one tick is refused (ValueError before
sending); a take profit below one tick is sent as one tick (never 0).

## Implicit Divergence

The `enforce_stop_direction` difference means that:

1. **Backtest**: a trade whose stop sits on the wrong side of the entry is accepted and
   sized from the absolute `|entry − stop|` distance — the stop stays where the signal put
   it. Nothing flips or drops it, so the backtest simulates a trade whose risk
   characteristics differ from the signal's intent.
2. **Live**: such a trade is refused with `position_size=0` (rejection reason
   `bad_stop_direction`).

This prevents unrealistic P&L in live trading, but creates a divergence between backtest
and reality: the same signal is traded in the backtest and refused live.

## Recommendation

The current approach is intentional: live trading should validate more strictly than
backtests. This documentation now exists for it.

## Tests

The three cases this file used to list as a wish list are pinned on main as follows:

| Case (former wish list) | Pinned by |
|---|---|
| Identical inputs → identical output between `RiskEngine.calculate_position_size` and the live path `size_live_signal_for_submission` | `tests/test_risk_consistency.py::test_risk_engine_and_live_size_match_for_valid_signals` (parametrized buy/sell) |
| Both paths use the same `RiskConfig` defaults | `tests/test_risk_consistency.py::test_risk_engine_and_live_use_same_defaults` |
| Strict stop-direction check enforced in the live path | `tests/test_risk_consistency.py::test_live_size_rejects_wrong_stop_direction`; the backtest side of the same divergence: `tests/test_risk_consistency.py::test_backtest_engine_allows_wrong_stop_direction` |
| Fallback stop used for sizing (percent fallback, no ATR in metadata) | `tests/test_live_sizing_single_resolver.py::test_daemon_fallback_stop_is_submitted_and_used_for_sizing` and `tests/test_live_sizing_single_resolver.py::test_gate_measures_against_the_fallback_stop_sizing_used` |

Not pinned as a backtest-vs-live parity case: an ATR-based fallback stop computed in both
paths. The fallback itself is one shared `RiskEngine` function
(`risk/engine.py::_fallback_stop_loss`: ATR × `default_sl_atr_multiplier`, otherwise
`entry_price * fallback_stop_pct`) and is covered on the engine side by
`tests/test_live_execution_safety.py::test_fallback_stop_is_below_entry_for_buys_and_above_for_sells`
(ATR 1.5) and
`tests/validation/test_risk_sizing.py::test_calculate_trade_risk_none_stop_fallback_uses_meta_atr`,
but no test feeds a signal with ATR metadata through the live sizing entry point
`size_live_signal_for_submission`.