# Code Review Report — Trading Platform (Live Execution, Risk, Recovery)

Date: 2026-06-10
Method: The Fool (failure-mode/attack pass) → Pre-Mortem (`PRE-MORTEM.md`) → Mutation testing.
Test runner confirmed: `.venv/Scripts/python.exe -m pytest` (Windows venv via WSL interop);
baseline 19 passed on risk/daemon suites. Two pre-existing collection errors
(`tests/e2e/lab/test_conversion.py`, `tests/test_account_discovery.py`) and one pre-existing
failure (`tests/test_api.py::TestSymbolsAPI::test_symbols_has_content`) were excluded from
mutation verdicts.

Legend: **[BUG]** = incorrect behavior reachable in production. **[FRAGILITY]** = currently
correct or fail-closed, but one change/edge away from incorrect, with no test pinning it.

---

## Critical

### C1 [BUG] — Recovery: No crash/restart reconciliation exists at all
`live_runner/` (whole package), `live_runner/alpaca_adapter.py::submit_order`, `ctrader_adapter.py::submit_order`

- No code queries broker open orders, positions, or historical fills on startup. Verified by
  search: no reconcile/backfill path anywhere in `live_runner/`.
- Fill persistence is **stream-only**. Any fill occurring while the daemon is down, between
  submit and stream-event arrival, or during a stream gap is permanently lost.
- `broker_order_id` is returned and logged but **never persisted** at submit time, so the DB
  alone cannot be reconciled after the fact. The `client_order_id = signal.id` convention
  exists on both brokers, so a reconciler is buildable — it just doesn't exist.
- Consequence: position/balance invariants silently diverge from broker truth after any
  crash. Given "silent failures are worse than crashes", this is the top issue.
- See Pre-Mortem Incident 1.

### C2 [BUG] — Risk: Exposure and position caps are dead code in the live path
`live_runner/daemon.py::submit_order_with_risk` (default `current_exposure=0.0`), Discord listener call site, `risk/live.py`

- The Discord listener calls `submit_order_with_risk(adapter, deployment, live_signal)` with
  no exposure argument; `current_positions`/`pending_positions` metadata default to 0.
- Therefore `max_total_exposure` and `max_positions` never bind in production: every signal
  is sized as if the account were flat. Sequential signals can stack unbounded risk.
- `adapter.get_positions()` exists (Alpaca) and is already used for exit-only sizing — the
  data is available, just not wired into sizing.
- Agrees with GPT-5.5 Critical #2, and extends it: **`max_positions` is equally dead**, not
  only `max_total_exposure`.

### C3 [BUG] — Live Execution: Partial fills persist cumulative quantities; no idempotency
`live_runner/alpaca_adapter.py::_on_trade_update`, `shared/database.py::save_live_execution`

- `filled_qty` falls back to `order.filled_qty`, which is cumulative in alpaca-py. Each
  partial-fill event inserts a new `LiveExecution` row with the running total; consumers
  joining/summing executions (`load_live_account_stats_inputs`,
  `list_live_signal_executions_for_deployments`) overcount.
- No unique constraint or natural key on executions: stream reconnect/replay inserts
  duplicate rows. `save_live_execution` is a blind `INSERT` with a fresh UUID.
- cTrader path has the same dedupe gap (`_persist_execution_event` per event, no key).
- See Pre-Mortem Incident 2.

---

## High

### H1 [BUG] — Recovery: Startup force-resurrects PAUSED/FAILED services
`live_runner/daemon.py::_set_all_services_running`, called unconditionally in `run_daemon()`

Every service is flipped to RUNNING on daemon start, discarding operator pause/stop
decisions. A reboot silently re-enables a signal bot an operator deliberately paused.
See Pre-Mortem Incident 3.

### H2 [BUG] — Risk: Pip-based fallback stop produces absurd sizes on high-priced assets
`risk/engine.py::_fallback_stop_loss`

With no SL and no ATR, the stop distance becomes `pip_value * multiplier` — an **absolute**
distance (default $2.00) independent of price. On expensive assets this yields tiny risk
distance → enormous `position_size`. The `entry_price * 0.02` guard only fires when the
distance is `<= 0`, never here. Mutation testing additionally showed the fallback's
*direction* (stop below entry for buys) is untested. See Pre-Mortem Incident 4.

### H3 [BUG] — Live Execution: cTrader volume floor silently overrides risk sizing; missing qty defaults to tradable size
`live_runner/ctrader_adapter.py::_resolve_order_volume`

- `max(int(qty * 1000), 1000)` clamps any risk-sized quantity up to a minimum volume with no
  log and no rejection — the risk engine's output is not authoritative.
- A signal with no `qty`/`volume`/`ctrader_volume` metadata defaults to `qty = 1.0` and
  **trades anyway** instead of being rejected.
- See Pre-Mortem Incident 6.

### H4 [BUG] — Recovery: Zombie execution listener after cTrader credential rotation
`live_runner/ctrader_adapter.py::listen_executions` (infinite `sleep(60)` park loop), `daemon.py::_start_missing_tasks`

After a terminal auth failure the listener task parks forever instead of terminating. When
credentials are rotated, the daemon builds a fresh adapter (orders work again), but
`_start_missing_tasks` sees the old not-done task keyed by `account_id` and never starts a
listener on the new adapter. Result: orders execute, **fills are never persisted**, no
ongoing error logs. See Pre-Mortem Incident 5.

### H5 [BUG] — Live Execution: cTrader fill→signal mapping is in-memory only
`live_runner/ctrader_adapter.py` (`_order_id_to_signal_id` dict)

The orderId→signal mapping lives in a dict that is lost on disconnect/restart (and
`disconnect()` also discards the pending `_execution_queue`). Fills whose events lack
`clientOrderId` after a reconnect are dropped with only a warning. Combined with C1, fills
around any connection blip are unrecoverable. Queue-full events are also dropped
(see Pre-Mortem Incident 8).

### H6 [FRAGILITY] — Risk: Synthetic market data fed to live signal processors
`live_runner/daemon.py::_build_processor_market_data`

When `market_data` is absent, a fabricated linear OHLCV ramp (±0.3% around entry) is fed to
signal processors that may compute ATR/volatility/trend filters. Approvals based on
invented context are a textbook silent failure. GPT-5.5 rated this Medium; given the
project's stated principle, **High** is appropriate. Live mode should reject instead.

---

## Medium

### M1 [FRAGILITY] — Recovery: Unknown deployment status is fail-closed but unpinned
`live_runner/daemon.py::_normalize_deployment_status`

Current behavior (unknown → `"UNKNOWN"` → not traded) is correct. Mutation testing flipped
it to fail-open (`"ACTIVE"`) and **the whole suite passed**. The single line deciding
whether a live account trades after a corrupt status has zero test coverage.
See Pre-Mortem Incident 7.

### M2 [FRAGILITY] — Risk: Two divergent sizing paths between daemon and adapters
`live_runner/daemon.py::size_live_signal_for_submission` vs `risk/live.py::size_live_signal` (called again inside each adapter's `submit_order`)

The daemon sizes with deployment `RiskConfig` and writes `explicit_qty`; the adapter then
re-runs `size_live_signal`, which takes the explicit-qty fast path and **skips all engine
guards** (stop-direction, max positions, exposure). Today the daemon path is safe because it
sized first, but any caller that submits a signal carrying `qty`/`explicit_qty` metadata
bypasses the risk engine entirely. Direct adapter calls also use metadata-derived
`RiskConfig` defaults rather than deployment settings. One risk authority should exist.

### M3 [FRAGILITY] — Live Execution: Candle-close wait trades on stale prices
`live_runner/signal_sources/execution_timing.py::wait_for_candle_close`, daemon Discord listener

The daemon can wait up to a full timeframe (hours on 1h+) between signal and submission,
then sizes off the original `entry_price`/`theoretical_price` with no re-quote and no
staleness bound. Stop distance and risk amount are computed against a price that may no
longer exist. Add a max-staleness check or re-fetch the price at submit.

### M4 [FRAGILITY] — Live Execution: First-match `break` drops signals for co-deployed strategies
`live_runner/daemon.py::_run_discord_signal_listener_for_account`

When multiple ACTIVE deployments share a channel and symbol on one account, only the first
deployment in list order receives the signal (`break` after first successful submit).
Whether this is intended routing or silent signal loss is undocumented; for a portfolio
of negatively-correlated strategies sharing a feed, this drops trades silently.

### M5 [FRAGILITY] — State Persistence: signal-before-submit ordering is good, but the write is the only atomicity
`live_runner/alpaca_adapter.py`, `ctrader_adapter.py`, `shared/database.py`

`save_live_signal` (idempotent `merge`) before broker submit is the right order: a crash
leaves a signal with no execution, not vice versa. But there is no order-state machine
(SUBMITTED/ACKED/FILLED/FAILED) — a persisted signal whose submit failed is
indistinguishable from one whose fill was lost (C1). Schema migration is hand-rolled
`PRAGMA`/`ALTER` without versioning; live trading state shares one SQLite file with
backtest bulk writes (WAL + 30s busy timeout mitigates, doesn't eliminate, contention).

---

## Low

### L1 [FRAGILITY] — `_adapter_is_ready` trusts absent attributes
`live_runner/daemon.py::_adapter_is_ready`: `getattr(..., None)` means an adapter without
`_connected`/`_authenticated` attributes is "ready". Fail-open duck typing on the gate that
starts execution listeners.

### L2 [FRAGILITY] — Terminal-error classification by substring
`daemon.py::_is_terminal_ctrader_runtime_error` matches `"not authorized"` anywhere in the
message. A transient error mentioning authorization permanently blocks an account until
credentials are touched; conversely, broker wording changes silently demote terminal errors
to retry loops.

### L3 [FRAGILITY] — Alpaca balance defaults to 0.0 on parse failure
`alpaca_adapter.py::get_balance`: unparseable `cash`/`equity` becomes `0.0` instead of an
error; `submit_order` would then raise on sizing (`balance <= 0`), which is fine, but the
balance poller persists a fake `0.0` equity snapshot, corrupting the live stats series.

### L4 — Test suite hygiene
Pre-existing: 2 collection errors and 1 failing API test (see header). These mask real
regressions in `-k`-filtered runs and should be fixed or quarantined.

---

## Mutation Testing Results

Runner: `.venv/Scripts/python.exe -m pytest ... --ignore=tests/e2e --ignore=tests/test_account_discovery.py`

| # | Target (focus area) | Mutation | Result |
|---|---|---|---|
| 1 | `risk/engine.py` sizing (risk calc) | drop `* pip` from `risk_amount / (risk_distance * pip)` | **SURVIVED** (13 passed) — every test uses `pip_value=1.0` |
| 2 | `risk/engine.py` fallback SL (risk calc) | stop placed *above* entry for buys | **SURVIVED** — fallback test only asserts `is not None` and `size > 0` |
| 3 | `alpaca_adapter.py` order guard (execution) | `qty <= 0` → `qty < 0` (allow zero-qty submit) | **SURVIVED** (45 passed) |
| 4 | `shared/database.py::save_live_execution` (persistence) | remove `session.commit()` (fills silently rolled back) | **SURVIVED** (65 passed; 1 pre-existing unrelated failure) |
| 5 | `daemon.py::_normalize_deployment_status` (recovery read) | unknown status → `"ACTIVE"` (fail-open) | **SURVIVED** (32 passed) |

**Score: 0/5 killed.** Contrast with GPT-5.5's 2/2 kills: their mutations targeted the
well-tested arithmetic core (`max_positions` comparison, `min`→`max`). The arithmetic is
covered; the **boundaries are not** — pip units, fallback geometry, submit guards, the
persistence commit, and the trade/don't-trade recovery decision are all unpinned.

Highest-value tests to add, in order:
1. Fill persistence round-trip: partial fills with delta quantities, duplicate-event
   idempotency, commit actually durable (kills #3, #4, C3).
2. Sizing with `pip_value != 1.0` and a no-SL/no-ATR signal on a high-priced asset
   (kills #1, #2, H2).
3. `_normalize_deployment_status` and `_adapter_is_ready` fail-closed contracts (kills #5).
4. Exposure integration test: existing position consumes the cap, next signal blocked (C2 —
   currently impossible to test meaningfully because the wiring doesn't exist).

---

## Systemic Themes

1. **The risk engine is a library, not an authority.** Correct in isolation, but every
   production caller can starve it of inputs (exposure=0), override its output (cTrader
   volume floor), or bypass it (explicit-qty fast path). Risk checks need to live at the
   single choke point before broker submit, fed from broker truth, not caller-supplied
   metadata.

2. **Persistence is write-only telemetry, not a system of record.** Nothing ever reads the
   live tables to make decisions (no reconciliation, no recovery, no exposure computation).
   That's why C1–C3 are invisible in operation: the data that would expose drift is never
   consulted.

3. **Fail-open defaults at boundaries.** Missing qty trades at default size; missing market
   data is fabricated; absent adapter attributes mean "ready"; restart resurrects paused
   services. For real money, every one of these should fail closed and loud.

4. **Recovery is the untested half of the system.** Tests cover the steady state. Every
   crash/restart/rotation path examined (C1, H1, H4, H5) loses or corrupts state silently.

5. **Test suite measures arithmetic, not behavior.** 0/5 boundary mutations killed. The
   suite would not notice if fills stopped persisting or stopped deployments started
   trading.

---

## Agreement / Disagreement with the GPT-5.5 Review

**Agree (verified in source):**
- Critical #2 (live exposure default `0.0`) — confirmed and extended (C2: `max_positions`
  also dead).
- Synthetic market data finding — confirmed, but I rate it **High**, not Medium (H6), given
  the project's "silent failures worse than crashes" principle.
- Their test-gap analysis ("live exposure integration not covered") — confirmed by my
  mutation results.

**Not contradicted, not independently verified (outside my focus areas):**
- vectorbt multi-symbol capital accounting, equity stamped at entry time, `total_bars`,
  optimizer robustness, comparison endpoint N>2 — all plausible from a skim; I did not
  re-derive them. Their priority #2/#3 ordering looks reasonable for the backtest side.

**Disagree / corrections:**
1. **"Mutation testing killed the key RiskEngine mutations, which is encouraging"** — this
   conclusion is too optimistic. Their two mutations hit the arithmetic core where the four
   existing tests live. Five mutations on the guard/boundary logic (this review) all
   survived. The mutation evidence supports the *opposite* headline: the risk path's
   safety-relevant behavior is effectively untested.
2. **Recovery is absent from their review entirely.** The single largest exposure for real
   money — no startup reconciliation, unpersisted `broker_order_id`, fills lost across
   restarts (C1), paused services resurrected (H1), the zombie listener after credential
   rotation (H4) — none of it appears. Their "Recommended Priority" list should have C1 at
   or near the top.
3. **Partial-fill accounting (C3) is missed**, despite "position/balance invariants" being a
   natural review target for a live platform; their review treats execution persistence as
   working.
4. Minor: their Medium on logging portability is real but cosmetic next to the above; I
   would not spend the next engineering day there.

## Recommended Priority

1. C1 — persist `broker_order_id` at submit + build startup/periodic broker reconciliation.
2. C3 — per-event fill deltas + idempotent execution writes (do together with C1).
3. C2 — wire real broker exposure/position count into sizing.
4. H2/H3 — fail-closed sizing fallbacks (reject no-SL signals; reject below-minimum volume).
5. H1/H4 — restart semantics: respect persisted service status; terminate (don't park)
   listeners on terminal auth failure.
6. Add the four test groups from the mutation section to pin all of the above.
