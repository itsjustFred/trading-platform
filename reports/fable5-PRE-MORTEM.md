# Pre-Mortem: Trading Platform Live Execution

Fictional post-mortems for fragilities found in the current codebase. Each incident is
plausible given the code as written today. Focus areas: non-atomic order operations,
open orders surviving a crash, partial-fill invariants, and corrupt/missing state on startup.

---

## Incident 1 — The position nobody knew about

**Severity: Critical | Component: `live_runner/daemon.py`, `live_runner/alpaca_adapter.py` | Type: Non-atomic order op + missing recovery**

At 14:31 UTC a Discord signal triggered `submit_order_with_risk()`. The signal row was
written to SQLite, `TradingClient.submit_order()` returned, and the daemon process was
OOM-killed before the fill event arrived on the trade-update stream. The market order
filled at the broker 400ms later.

On restart the daemon performed **no reconciliation of any kind**: there is no code path
anywhere in `live_runner/` that queries broker order history or open positions at startup.
The fill was never persisted. The `live_executions` table showed no position; the broker
showed a 200-share long. Three days later the strategy emitted a second buy for the same
symbol. The risk engine — fed `current_exposure=0.0` and `current_positions=0` defaults —
sized it as if the account were flat. Exposure doubled silently.

**Contributing factor:** `broker_order_id` is returned by `submit_order()` and *logged*,
but never written to the database. Even manual reconciliation from the DB alone was
impossible; the operator had to grep log files.

**What would have prevented it:**
- Startup reconciliation: pull broker orders/fills since last persisted execution
  (the `client_order_id = signal.id` mapping already exists broker-side on both adapters).
- Persist `broker_order_id` on the signal row at submit time, before returning.
- Feed real broker positions into `submit_order_with_risk(current_exposure=...)`.

---

## Incident 2 — Partial fills double the books

**Severity: Critical | Component: `live_runner/alpaca_adapter.py::_on_trade_update` | Type: Invariant breaks on partial fill**

A 5,000-share order filled in three partials. For each `partial_fill` event the adapter
read `filled_qty` via fallback from `order.filled_qty` — which in alpaca-py is the
**cumulative** filled quantity — and inserted a fresh `LiveExecution` row (1,000 / 3,500 /
5,000). Downstream, `load_live_account_stats_inputs()` joins all execution rows per signal;
anything summing `filled_qty` computed a 9,500-share position from a 5,000-share order.
Live Sharpe and slippage stats drifted from broker truth and the divergence was only
noticed when the equity panel disagreed with the broker statement by 19%.

**Aggravator:** there is no idempotency key. `LiveExecutionModel` has a random UUID primary
key and no unique constraint on `(broker_order_id, timestamp, filled_qty)`. When the
Alpaca stream reconnects and replays an event, the same fill is inserted twice. The same
applies to cTrader if an execution event is delivered again after `listen_executions`
retries.

**What would have prevented it:** persist per-event delta quantity (event `qty`, not
`order.filled_qty`), plus a uniqueness constraint or upsert keyed on broker execution id.

---

## Incident 3 — Restart resurrects what the operator paused

**Severity: High | Component: `live_runner/daemon.py::_set_all_services_running` | Type: Corrupt-state-on-startup by design**

An operator paused a misbehaving Discord signal bot at 09:00 (`status=PAUSED`). At 11:30 a
routine server reboot restarted the daemon. `run_daemon()` unconditionally calls
`_set_all_services_running()`, which flips **every** service — paused, failed, stopped —
back to RUNNING. The paused bot reconnected and resumed feeding signals into a live
account for six hours before anyone noticed. The operator's explicit risk decision was
silently undone by a restart.

**What would have prevented it:** persist and respect operator intent across restarts;
only auto-start services that were RUNNING when the process stopped.

---

## Incident 4 — The 2-dollar stop on a 100,000-dollar asset

**Severity: High | Component: `risk/engine.py::_fallback_stop_loss` | Type: Load-bearing default**

A Discord signal for BTCUSD arrived with no stop-loss and no ATR in metadata. The fallback
computed `atr_distance = pip_value * default_sl_atr_multiplier = 1.0 * 2.0` — an **absolute
distance of $2.00** on a $100,000 asset. Risk distance 2.0 → `position_size =
(balance * 1%) / 2.0`. On a $100k account that is 500 BTC of intended size. The order was
only stopped because the broker rejected the buying power, i.e. the last line of defense
was the counterparty, not the risk engine. On a smaller-priced asset with the same bug the
order would have gone through, with a stop 2 ticks from entry that whipsaws instantly.

The `entry_price * 0.02` guard exists, but only triggers when `atr_distance <= 0` — never
for this case. Mutation testing confirmed the fallback's *direction* is also untested
(stop above entry on a buy goes undetected).

**What would have prevented it:** make the fallback proportional to price (percent-based),
not pip-based; reject signals without SL/ATR in live mode; test fallback direction and
magnitude.

---

## Incident 5 — Token rotation kills the fill listener, forever, silently

**Severity: High | Component: `live_runner/ctrader_adapter.py::listen_executions`, `daemon.py::_start_missing_tasks` | Type: Zombie task after recovery**

A cTrader access token expired. The adapter marked a terminal auth failure and
`listen_executions()` entered its `while True: await asyncio.sleep(60)` parking loop. The
operator rotated the token in `.accounts.env`; the daemon correctly cleared the blocked
account and built a **new** adapter, which authenticated fine. Orders flowed again.

But `_start_missing_tasks()` keys listener tasks by `account_id` and skips accounts whose
task is "not done". The old task — bound to the dead adapter object — sleeps forever and
never completes. The new adapter's `listen_executions()` was never started. Orders were
submitted and filled for two weeks with **zero fills persisted**, and nothing logged after
the first hour. Discovered only during a manual broker statement audit.

**What would have prevented it:** make terminal auth failure *terminate* the listener task
(raise/return) instead of parking; or on adapter replacement, cancel the old listener task
explicitly.

---

## Incident 6 — The minimum lot that ignored the risk engine

**Severity: High | Component: `live_runner/ctrader_adapter.py::_resolve_order_volume` | Type: Silent clamp overrides risk sizing**

The risk engine sized a trade at qty 0.07. `_resolve_order_volume()` computed
`max(int(0.07 * 1000), 1000) = 1000` — silently submitting ~14x the intended size. Worse:
a signal with no qty/volume metadata at all defaults to `qty = 1.0` and trades anyway.
The risk engine's output is a suggestion; the volume floor is the real sizing authority,
and it never logs that it overrode anything.

**What would have prevented it:** reject orders where the exchange minimum exceeds the
risk-derived size (or require explicit operator opt-in); never default a missing qty to a
tradable value.

---

## Incident 7 — The deployment status that wasn't

**Severity: Medium | Component: `live_runner/daemon.py::_normalize_deployment_status` | Type: Missing test, fail-open adjacent**

A buggy API client wrote status `"stopped "` (trailing space survives `.upper()` only;
hypothetical variants: `"DISABLED"`, `""` after a failed migration). Today unknown statuses
normalize to `"UNKNOWN"` and are excluded from the active set — correct, fail-closed. But
mutation testing showed that flipping this to fail-open (`return "ACTIVE"`) passes the
entire test suite. The single line that decides whether a live account trades after a DB
glitch has no test pinning its behavior. One refactor away from trading stopped
deployments.

---

## Incident 8 — Queue full, fills dropped

**Severity: Medium | Component: `live_runner/ctrader_adapter.py::_on_message` | Type: Backpressure as data loss**

During a volatile session with the SQLite file on a slow disk, `_persist_execution_event`
(one event per loop iteration, serialized through `asyncio.to_thread`) fell behind. The
bounded queue (5,000) filled and `_on_message` began **dropping execution events with a
warning log**. Additionally, `disconnect()` replaces `_execution_queue` with a fresh queue,
discarding any not-yet-persisted events. Fills lost both ways; books diverged from broker.

**What would have prevented it:** persist-before-ack semantics or a durable journal for
incoming execution events; drain the queue on disconnect.
