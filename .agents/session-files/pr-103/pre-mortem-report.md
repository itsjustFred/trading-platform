# Pre-Mortem Report — mega-batch-trading-numbers (PR-103 scope)

**Scope:** NEW code on branch `mega-batch-trading-numbers` vs `worktree-issues-batch-2026-09-21`, focused on:
`live_runner/credential_env.py` (`resolve_env_value` absent-vs-empty),
`live_runner/daemon.py` (qty binding, unbounded-TP flag, idempotency key),
`live_runner/alpaca_adapter.py` + `ctrader_adapter.py` (qty-required submit),
`risk/engine.py` (ATR reject, trailing warn),
`shared/database.py` (idempotency column, auto dedupe key, locked path),
`runner/converters/lean_converter.py` (pairing state machine),
`runner/optimizer.py` (risk-adjusted objective, embargo),
`runner/backtest_grid.py` (`_existing_cell_matches_request`),
`data_tools/auto_download.py` (coverage gates),
`validation/comparison/matcher.py` (cost weights).
**Date:** 2026-09-24
**Method:** read all scoped files + diffs; one hypothesis verified by execution (see Real Bugs below). No code edited.

## REAL current bugs found (not fictional — please triage separately)

**R1 — `has_sufficient_segment_coverage` falsely rejects perfect daily data when a segment endpoint lands on a weekend.**
`data_tools/auto_download.py:542-582`. Expected bars are computed as
`int(trading_minutes // step) + 1` (`auto_download.py:569`), where trading minutes exclude
Sat/Sun but the unconditional `+1` endpoint bar is counted even when the endpoint itself is a
weekend day with no bar. Verified by execution with synthetic perfect FX daily data
(5 bars Mon 2025-07-28 → Fri 2025-08-01, zero gaps):
`has_sufficient_segment_coverage(symbol, 1d, Mon→Sun) == False` (5/6 ≈ 83% < 95%),
while `Mon→Fri == True`. A two-week perfect daily segment scores 10/11 ≈ 91% and also fails.
This gate predates the batch, but the batch **newly wires it into the optimizer hot path**
(`runner/optimizer.py:399-411`, per symbol × train/test segment), so daily-timeframe
optimizations whose split windows touch weekends can now spuriously abort with
"insufficient OHLCV coverage". Intraday timeframes are unaffected in practice
(e.g. 1h Mon→Sun scores 120/121 ≈ 99%); crypto is lenient (weekend bars raise density).

**R2 (minor, pre-existing) — duplicate `timedelta` import**, `live_runner/daemon.py:11`
(`from datetime import datetime, timedelta, timezone, timedelta`). Cosmetic/lint (F811-class);
harmless at runtime.

## Summary

Six post-mortems below. The batch's dominant theme is **fail-closed safety built out of
convention rather than types**: the unbounded-TP escape hatch is a bare string inside an
untrusted metadata dict (PM2); redelivery-vs-re-intent is indistinguishable in the idempotency
key (PM3); fill identity is assumed stable across three key schemes (PM4); the LEAN entry-quantity
freeze lives only in a comment above `nonlocal` state (PM5); grid artifact freshness assumes schema
completeness that the code itself documents as false (PM6); and credential precedence now hinges on
`None`-vs-empty distinctions in three places at once (PM1). The residual risks cluster at
**trust boundaries** (signal metadata crosses from untrusted payload to order path) and
**silent no-ops** (a skipped trade or skipped cell looks exactly like a quiet market). None of the
scenarios below is broken today; each needs only one small, review-passable edit to detonate.

## Post-Mortems

### 1. Blanked credential resurrects from stale process env after a "cleanup" of the presence check

**Severity:** High
**Component:** `live_runner/credential_env.py:resolve_env_value` (lines 87–103); `live_runner/daemon.py:_create_adapter_for_account` (~line 1480); `live_runner/{alpaca,ctrader}_adapter.py` constructors
**Fragility type:** load-bearing defaults / invisible invariants

#### What happened

An operator blanked `CTRADER_ACCESS_TOKEN` in `.accounts.env` to force a misbehaving account
offline. The daemon kept the account connected and kept submitting orders for another 40 minutes
on the revoked token, because credential resolution silently fell back to the stale
process-environment value instead of honoring the present-but-empty file entry.

#### The change that caused it

A developer "simplified" the presence test in `resolve_env_value` from
`if accounts_env_values is not None and key in accounts_env_values` to the more idiomatic
`if accounts_env_values.get(key)` (empty string is falsy → falls through to `os.environ`),
reasoning that "empty and absent mean the same thing." A companion cleanup in
`_create_adapter_for_account` reverted the `is not None` guard back to `or`, so an explicitly
passed empty mapping again fell through to process env. Both edits looked like dead-code removal;
the `test_credential_env.py` suite still passed because its present-but-empty case exercised
`load_accounts_env_values`, not the edited branch.

#### Why it broke

The whole file-first contract rests on a three-way distinction — **absent** (fill from environ)
vs **present-but-empty** (stay empty, never shadowed) vs **present-with-value** — spelled out in
the `resolve_env_value` docstring (lines 92–98) but enforced by nothing except the `key in`
test at line 100 and the `is not None` test in the daemon. The adapters compound it:
`resolve_env_value(env_values, "ALPACA_PAPER") or None` (alpaca_adapter.py:288) and the cTrader
equivalent deliberately map empty→None→default-True, so empty means "default" in one place and
"blanked, honor it" in another, with only comments recording which is which.

#### How it was caught

Live incident: the account kept trading after the operator believed it was cut off. No unit test
failed — the falsy/empty path for a *blanked secret* (as opposed to a blanked bool) had no
coverage. It surfaced in logs (`falling back to resolved-env credentials`) after the fact.

#### Hardening suggestions

1. Add a test that blanks a *secret* key in the file mapping while process env holds a stale
   value, and asserts the resolved value is empty (the mirror of the existing bool-default test).
2. Replace the bare `Mapping[str, str] | None` with a tiny resolver type whose methods are
   `value_or_absent()` — make absent-vs-empty unrepresentable as a bare falsy check.
3. Log at warning level whenever a present-but-empty file value suppresses a non-empty process-env
   value for a secret-shaped key, so the precedence decision is observable.

### 2. A stale `take_profit_policy` key rides along on an unrelated signal and opens a stop-only live position

**Severity:** High
**Component:** `live_runner/daemon.py:submit_order_with_risk` (lines 792–824); `live_runner/alpaca_adapter.py:_resolve_broker_sl_tp` (535–555), `_build_order_request` OTO branch (602–616); `live_runner/ctrader_adapter.py` unbounded guard (~1845) + `_resolve_relative_sl_tp` returning `(stop, 0)`
**Fragility type:** stringly-typed

#### What happened

A stop-only (no take-profit) live position was opened on EURUSD at full risk size outside the
RocketFuel strategy. The order carried no TP leg on either broker, and the daemon log showed the
"explicitly unbounded take profit" warning for a deployment that never opted into unbounded exits.

#### The change that caused it

A new internal replay/reconciliation tool copied raw signal dicts between deployments and
preserved unknown metadata keys verbatim ("don't drop fields we don't understand"). One replayed
payload contained `take_profit_policy: unbounded_exit_on_opposite_signal` left over from a
RocketFuel signal. The daemon's unbounded check —
`str(metadata.get("take_profit_policy") or "") == "unbounded_exit_on_opposite_signal"`
(daemon.py:792–795) — honored the ambient string with no provenance check, skipped the missing-TP
reject, popped `take_profit`, and both adapters obediently built stop-only orders (Alpaca OTO,
cTrader relative-TP 0). Every step behaved exactly as coded.

#### Why it broke

The unbounded escape hatch from the bounded-TP owner decision is a **bare magic string inside the
same untrusted metadata dict as the signal payload**, read independently in three places
(daemon, Alpaca adapter, cTrader adapter) with no shared constant and no record of *who set it*.
`preserved_metadata` (daemon.py:842–845) intentionally forwards non-sizing keys, so the flag
survives the sizing overlay by design. Nothing distinguishes "processor-declared policy" from
"stale/copied/forged payload key" — the trust boundary is documented in a comment ("set by the
originating processor") and enforced nowhere.

#### How it was caught

The daemon warning log ("Submitting live order with explicitly unbounded take profit") fired and
an operator noticed the deployment id didn't run RocketFuel. No test caught it: adapter tests set
the flag deliberately, and no test asserts that a flag arriving via an untrusted path is rejected
or stripped. A forged Discord-side value would behave identically.

#### Hardening suggestions

1. Promote the flag to a shared constant (single definition imported by daemon + both adapters)
   and validate/normalize it once at the daemon boundary: strip `take_profit_policy` from any
   metadata that did not come through `_apply_signal_processor` for the owning deployment.
2. Have `_trade_signal_to_live_signal` (not downstream readers) resolve and *remove* the raw key,
   replacing it with a typed internal field, so adapters never string-match payload content.
3. Add a test: signal without TP but with a smuggled `take_profit_policy` from a
   non-RocketFuel path must raise, not submit.

### 3. A legitimate strategy re-entry is swallowed as a "redelivery" — silent missed trade

**Severity:** High
**Component:** `live_runner/daemon.py:_trade_signal_to_live_signal` (lines 870–883); `shared/database.py:save_live_signal` (1505–1553), `_find_existing_live_signal_by_idempotency_key` (1468+)
**Fragility type:** invisible invariants

#### What happened

A strategy exited EURUSD on its opposite-signal rule, then re-entered at the same price with the
same levels minutes later. The second entry never reached the broker: `save_live_signal` returned
the first signal's canonical row, the adapter's `_recover_existing_signal_order` returned the old
broker order id, and the daemon logged success. The book was flat while the strategy believed it
was long; P&L diverged from the backtest by a full trend leg.

#### The change that caused it

A new internal signal source emitted `signal_timestamp` only at batch granularity (one timestamp
per scan, not per signal) to "reduce clock noise." Two genuinely distinct signals — same
deployment, symbol, action, entry price, SL/TP, timeframe, and now identical timestamp material —
hashed to the same 32-char `idempotency_key`
(`key_material` join at daemon.py:871–882; `repr(float(...))`/`str(...)` formatting is stable, so
identical inputs collide deterministically). The dedupe layer did exactly what it was built to do.

#### Why it broke

The key material encodes **signal content, not signal intent**: there is no nonce, sequence
number, or producer-issued id, so "same content redelivered" and "same content intended twice"
are indistinguishable by construction. The timestamp field is the only intent-carrying element,
and it is optional metadata (`or ""`), sourced from either `signal_timestamp` or `timestamp`
with no contract on which producers must set. `_reconcile_existing_live_signal`
(database.py:1480–1502) then merges silently, and the adapter recovery path returns success —
three layers that all look like correct dedupe.

#### How it was caught

Not by tests: no test submits two identical-content signals and expects two orders (that would
contradict the redelivery tests). It was caught by strategy-vs-broker position reconciliation
showing the strategy long and the broker flat, investigated the next day.

#### Hardening suggestions

1. Include a producer-supplied unique element in the key when one exists (e.g. prefer an explicit
   `signal_id`/`nonce` metadata field; document that content-identical re-entry *requires* a
   distinct timestamp or nonce) and log dedupe hits with the full key material at info level.
2. Add a test pinning the contract from both sides: exact redelivery → one order; re-entry with a
   distinct nonce/timestamp → two orders.
3. Emit a metric/counter on idempotency-dedupe hits per deployment so a re-entry-eating regime
   change is visible in monitoring, not just in P&L.

### 4. One fill counted twice (or a partial fill dropped) after a timestamp-precision change

**Severity:** High
**Component:** `shared/database.py:_auto_execution_dedupe_key` (1558–1575), `_insert_live_execution_locked` (1578–1602), `save_live_execution` (1604+), `save_live_execution_up_to_cumulative` (1607+)
**Fragility type:** coincidental correctness

#### What happened

After an alpaca-py upgrade changed fill-event timestamps from microsecond to millisecond ISO
strings, one partial fill was persisted as two execution rows (2× quantity, inflated exposure and
PnL), while on another account two distinct same-millisecond partials at the same price collapsed
into one row (missing quantity, understated exposure).

#### The change that caused it

The dependency upgrade altered timestamp *representation* (not time) — a change no application
test pins. Fills arriving without `execution_id` fall back to the content-derived auto key
`auto:{signal_id}:{price:.8f}:{qty:.8f}:{stamp}:{broker_order_id}`. The same economic fill now
keyed differently across the stream path (which prefers `alpaca:exec:{execution_id}` when present
and the auto key when absent) and the reconcile path (which keys
`alpaca:order:{id}:reconciled:{cumulative}`), so one fill landed under two keys. Conversely, two
real fills differing only below the `.8f` rounding or within the same millisecond shared one key,
and `_insert_live_execution_locked`'s select-check-insert "skipped" the second as a duplicate.

#### Why it broke

Fill identity is assumed to be a stable, unique 5-tuple, but that invariant holds only
**coincidentally**: three key schemes (stream execution-id, auto content key, reconcile
cumulative key) coexist, each assuming the others will never describe the same fill, and the auto
key's `.8f` float formatting plus raw timestamp string silently define uniqueness at a precision
no producer contract guarantees. The locked single-path insert (`_insert_live_execution_locked`)
fixes the interleaving race but cannot fix two paths legitimately disagreeing on identity.

#### How it was caught

Broker-vs-local cumulative quantity drift on the reconciled account; the double-persisted fill
showed two rows with different `dedupe_key` values for one `broker_order_id`. The dropped partial
was found by auditing fills against broker statements. Unit tests passed because each key scheme
is tested in isolation — no test feeds the *same* fill through stream, redelivery-without-id, and
reconcile paths and asserts exactly-once persistence.

#### Hardening suggestions

1. One canonical key builder shared by all three paths, keyed on broker-atomic identity
   (`broker_order_id` + broker fill sequence/execution id) with content fields as *tiebreakers*,
   never as the primary key; keep the auto key strictly as a last resort and tag it (`auto:`).
2. Add a cross-path exactly-once test: same fill via `_on_trade_update` (with and without
   `execution_id`) plus `reconcile_executions` must yield one row with the cumulative quantity.
3. Replace `.8f`-formatted floats in the key with exact decimal strings of the broker payload to
   avoid rounding collisions between distinct fills.

### 5. A well-meaning LEAN-converter refactor silently rewrites entry quantities on partial closes

**Severity:** Medium
**Component:** `runner/converters/lean_converter.py:_build_trades` (108–243), nested `_close_leg` (122–175) with `nonlocal open_qty/open_notional/...`
**Fragility type:** shared mutable state

#### What happened

Cross-engine validation against the MT5 reference started reporting systematic quantity mismatches
on strategies with scaling/partial exits: converted LEAN trades showed shrunken entry quantities
and shifted VWAPs, flipping several profit factors across the 1.0 line and invalidating a
strategy-selection decision.

#### The change that caused it

A developer refactored the nested `_close_leg` closure into a module-level helper taking explicit
state ("easier to unit test"), and in the process replaced the frozen-entry computation
`qty = abs(open_entry_qty)` (line 128) with the live remainder `abs(open_qty)` — after all, at
close time the remainder *equals* the entry size for full closes, which is every case in the
existing tests. The `open_notional` freeze (entry VWAP must not move on partial closes, lines
126–129) was dropped with it, since the helper's author read `open_notional` as "current
notional." All existing converter tests passed: they cover full open→close pairs, where the two
formulations coincide.

#### Why it broke

The pairing machine's critical invariant — **entry size/VWAP are frozen at open+scale-ins while
`open_qty` is the shrinking remainder** — is held only by the `open_entry_qty` vs `open_qty`
variable split plus a code comment, threaded through six `nonlocal` bindings shared between the
loop and the closure. The sign-decides-scale-in rule (lines 202–209, "Signs decide, not tags")
and the oversized-exit flip loop (215–240) make the state transitions subtle enough that the
wrong-but-plausible variable reads correctly on every simple path.

#### How it was caught

The validation `TradeMatcher` cost comparison (price/quantity terms) flagged qty drift on
partial-fill fixtures; full-close fixtures stayed green, which is why it looked like a data issue
for a week before the refactor was bisected. Honestly: without partial-close fixtures in
`test_converters.py`, this would have shipped silently into research results.

#### Hardening suggestions

1. Add partial-close and scale-in fixtures asserting entry `quantity` equals total opened size and
   `entry_price` equals opened VWAP (not remainder-derived), plus an oversized-exit flip case.
2. Replace the `nonlocal` closure with an explicit small `_OpenLeg` dataclass threaded through
   `_close_leg(open_leg, ...)` so the frozen-entry vs remainder fields are named, typed, and
   reviewable at the call site.
3. Assert the invariant at close time: `abs(sum(exit legs)) ≈ abs(open_entry_qty)` before
   emitting the trade; log-and-skip on violation instead of emitting a mis-sized trade.

### 6. Grid `--resume` sells risk-retuned cells as fresh — strategy picked on incomparable numbers

**Severity:** Medium
**Component:** `runner/backtest_grid.py:_existing_cell_matches_request` (395–439), `CellRequest` (442–464)
**Fragility type:** invisible invariants

#### What happened

After retuning `risk_pct` from 0.01 to 0.02, the team reran the strategy grid with `--resume` and
selected a "best" symbol/timeframe on the resulting matrix. The winner had never actually run at
the new risk: its artifact was from the old sizing, reused as `skipped_existing`. Capacity was
allocated on incomparable cells.

#### The change that caused it

No code change at all — an operator changed a CLI knob (`--risk-pct`) and reasonably expected
`--resume` to recompute what changed. The freshness check compares engine, strategy, symbol,
timeframe, resolved window, capital, and strategy params, but risk knobs (`risk_pct`,
`max_position_exposure_pct`/`max_position_pct`, `max_leverage`, `intrabar_resolution`) live in
`CellRequest` and **outside the artifact schema**, so they are invisible to the comparison. The
docstring even documents the gap (lines 405–406: "NOT covered — residual gap, documented"), but
`--resume` output still reports the cell as fresh with no warning.

#### Why it broke

Artifact↔request correspondence is assumed ("same run_id ⇒ same computation") but enforced only
for schema-carried fields; sizing semantics that change every number in the artifact
(`_run_backtest_in_process_body` threads `request.risk_pct`/`max_position_exposure_pct`/
`max_leverage` into `BacktestConfig`, lines 507–521) leave no fingerprint in the persisted JSON.
`_init_cell_result` (324–375) then re-reports the stale stats as current.

#### How it was caught

An audit noticed identical output-file hashes before and after the risk change; the matrix itself
gave no signal. Existing tests cover matching on engine/params/window/capital — none feeds two
requests differing *only* in risk knobs and asserts recompute.

#### Hardening suggestions

1. Persist the sizing fingerprint (`risk_pct`, `max_position_exposure_pct`, `max_leverage`,
   `intrabar_resolution`) into `meta.data` at save time in `_run_backtest_in_process_body` and
   compare it in `_existing_cell_matches_request` (treat missing fingerprint as stale → recompute).
2. Until then, log a warning on every `skipped_existing` stating which knobs were *not* verified.
3. Add the risk-knob-only-diff test: same artifact, `risk_pct` 0.01 vs 0.02 → must recompute.

## Themes and Recommendations

**T1 — Untrusted metadata flows straight into the order path.** PM2 is the sharp end, but the
pattern recurs: `submit_order_with_risk` reads `take_profit_policy`, `volume`/`ctrader_volume`,
`qty` hints out of `signal.metadata` (daemon.py:792–795, 829–841); the cTrader adapter honors
`ctrader_volume`/`volume` for exit-only flatten (ctrader_adapter.py:1053–1096) trusting the
daemon's `risk_sizing_mode`. Structural fix: split signal metadata into *payload* (untrusted,
never read by the order path) and *resolved order intent* (typed, daemon-constructed), and strip
or re-authorize policy keys at `_trade_signal_to_live_signal`.

**T2 —Grep-level duplication of safety-critical parsing.** Strict-bool parsing exists in four
places (`credential_env.parse_bool_env`, daemon `_parse_strict_bool_env`, both adapters'
`_parse_bool_env`); the unbounded-TP string is matched in three; the SL/TP metadata key tuples
(`stop_loss/sl_price/stop_price/resolved_stop_loss`) are repeated in daemon, both adapters, and
cTrader's resolver. Any future rename/edit must land identically everywhere. Structural fix:
single `live_runner/order_guards.py` (or similar) owning the TP-policy constant, the SL/TP key
tuples, and the fail-closed SL/TP resolution both adapters call.

**T3 — Optimizer objective: sign-dependent math + unit-dependent clamps.** `_risk_adjusted_metric_value`
(optimizer.py:82–120) is correct today but fragile twice over: (a) the `if value < 0: return value`
early exit (line 105) *looks* like a removable special case, yet removing it lets multiplicative
haircuts compress large losses toward zero so catastrophic params outrank mild losers; (b) the
drawdown clamp `min(max_dd, 1.0)` assumes `max_drawdown_pct` stays a fraction (stats.py publishes
negative fractions, e.g. −0.05; tests use −0.40) — if stats ever publish percent, every trial
scores 0. Likewise `min_trades_for_full_credit=30` / `drawdown_penalty_weight=1.0`
(OptimizationConfig lines 226–231) are load-bearing defaults that silently re-rank all raw-metric
studies when tweaked. The `embargo_days` default (1) plus R1 above compose: embargo shifts test
windows onto weekends, and the weekend-blind-tolerant gate can then reject them. Recommendations:
unit-pin the sign guard with a test that fails if it is removed (large-loss vs small-loss
ordering); assert drawdown units at the boundary (`abs(max_dd) <= 1.0` else raise); record
`penalty_terms` *and* raw values (already done — keep both); surface embargo-shortened test
windows in the payload.

**T4 — Matcher: greedy order survives, weights move conclusions.** `TradeMatcher.match`
(matcher.py:168–201) now picks minimum `_match_cost` per reference in order — better than
first-fit, but still order-dependent (not Hungarian-optimal) and now sensitive to
`time_weight/price_weight/quantity_weight` (defaults 1.0). A future validation-tuning PR that
"just reweights" can flip matched/missing counts and hence engine-parity verdicts, while
`tolerance_seconds=0` makes the time term raw seconds (denominator `max(0,1)`, matcher.py:88) —
harmless today since only exact matches qualify, but surprising if tolerance handling is later
relaxed. Recommendations: pin a golden-pairing fixture including near-tie cases; document that
weight changes require re-baselining parity thresholds.

**T5 — Fail-closed additions that widen the failure surface.** Each is defensible alone, but
together they convert more situations into hard errors: risk ATR reject (risk/engine.py:20–49;
configs with `default_sl_atr_multiplier=0` or garbage ATR that previously sized on the 2% fallback
now raise mid-submission), `_fail_on_internal_gaps` on *every* `ensure_backtest_data` call even
when nothing was downloaded (auto_download.py:170, 237 — segmented/monthly storage with >3-day
inter-file gaps now fails backtests that used to run), and qty-required submit (both adapters —
every direct caller, script, or future adapter must pass binding qty; `scripts/live_smoke.py` and
any out-of-tree caller break loudly). Recommendations: keep fail-closed, but add a preflight
mode that reports *all* would-fail conditions at once (ATR quality, gaps, qty plumbing) instead of
one-at-a-time runtime raises; and keep an explicit, grep-able registry of `submit_order` callers.

**T6 — Trailing-stop warn-only no-op + log-only safety events.** Unsupported trailing modes return
the stop unchanged with a warning (risk/engine.py:322–331); fallback-stop sizing logs at warning
(219–223); unbounded-TP submission logs at warning (daemon.py:809–819). A future dev adding a real
`supertrend_flip` mode may miss that the warn branch must become a code path, and log-only signals
for sizing-on-invented-distance are easy to normalize into noise. Recommendations: count these in
metrics (not just logs) and add a test asserting each warn path emits exactly once per trigger.
