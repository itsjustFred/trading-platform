# Pre-Mortem Report — PR #107

**Scope:** `runner/daily_equity.py`, `runner/stats.py` (changed production files of
`fix/golden-accounting-oracle`, diff vs `main`)
**Date:** 2026-10-30

## Summary

Five post-mortems, concentrated on three fragilities introduced or sharpened by this PR:
the *two-views-one-number* invariant between `EquityPoint.equity` and
`daily_returns[i]["equity"]`, the *cent quantisation happens at the wrong layer*
invariant (rounding a displayed value while accumulating a raw one), and the
*window bookkeeping is spread across four locals* invariant in `build_daily_equity_curve`.
The dominant theme is that PR #107 added several implicit relationships between numbers
that no type or assertion enforces — the tests pin today's values, not the relationships.

---

### 1. Daily return percentages silently diverge from the curve after a rounding change

**Severity:** High
**Component:** `runner/daily_equity.py` — `build_daily_equity_curve` (the
`daily_returns.append` block and the `daily_points.append` block, ~lines 477–497)
**Fragility type:** Invisible invariants

#### What happened

A windowed re-report of a live account showed `stats["daily_returns"]` summing to a
daily-factor product that disagreed with the curve's endpoint by 0.4 % over 90 days. The
account owner reconciled both series by hand and could not tell which one the broker
statement matched.

#### The change that caused it

A developer fixing a "returns look stale" complaint changes the `return` numerator from
the raw `running_equity - previous` to the newly introduced cent-quantised value, writing
`"return": (closed_equity - previous) / previous`. It reads as more consistent — both
fields on the row now derive from `closed_equity` — and passes review because every
existing assertion uses `pytest.approx`.

#### Why it broke

`closed_equity = round(running_equity, 2)` and `previous` is *not* quantised on the first
iteration (`previous = starting_equity`, which comes from `_window_starting_equity` and is
only rounded when it lands in `daily_points[0]`, not when it seeds `running_equity` at
line 469). So the rounded numerator is differenced against an unrounded base, and the
error is signed and systematic for accounts whose balance settles on x.xx5 boundaries: it
never averages out. Currently the `return` field is the *only* unquantised value on the
row, which makes it the obvious target for exactly this "consistency" edit.

#### How it was caught

Not by the tests. `test_daily_return_calculation` and the window rebasing tests all
assert with `pytest.approx`, which absorbs the sub-cent difference at 100 000 capital.
It surfaced only because a human compared the compounded product of the daily factors
against the broker statement. No test asserts that the product of `1 + return` over the
window reconstructs the curve's endpoint.

#### Hardening suggestions

Add one invariant test that does not depend on tolerance luck: build a curve with
sub-cent daily PnL, compound `1 + daily["return"]` across `daily_returns`, and assert the
result matches `daily_points[-1].equity / daily_points[0].equity`. That pins the
*relationship* between the two fields rather than their individual values, so any future
edit that quantises one side without the other fails loudly.

---

### 2. `starting_equity` is quantised for display but not for accumulation

**Severity:** Medium
**Component:** `runner/daily_equity.py` — `build_daily_equity_curve`, line 469
(`running_equity = starting_equity`) versus `daily_points[0]` (~line 467)
**Fragility type:** Coincidental correctness

#### What happened

A windowed backtest starting mid-history reported a first-day return that implied an
opening balance 0.4 cents away from the opening point displayed directly above it in the
equity chart. Support could not reproduce it on accounts whose opening balance happened
to be a whole number of cents.

#### The change that caused it

A developer adds cent quantisation to `starting_equity` where it enters the curve, to
match the closes: `daily_points = [EquityPoint(time=inception.isoformat(),
equity=round(starting_equity, 2))]` becomes the *only* place `starting_equity` is rounded,
while `running_equity = starting_equity` (line 469) keeps the raw value. The motivation is
reasonable — the displayed point should not carry more precision than the rest of the
curve.

#### Why it broke

`_window_starting_equity` reconstructs the opening balance from
`initial_capital + _net_pnl_before(...)` and, on the reconciliation path, returns
`reconciled_equity + (starting_equity - initial_capital - net_through_reconciled)` — a
raw float that is generally not a whole number of cents. The first day's `return` is then
`(running_equity - previous) / previous` with `previous` holding that raw value, while
`daily_points[0].equity` holds the rounded one. The two disagree by up to half a cent, and
the disagreement is invisible whenever the test account's opening balance is round.

#### How it was caught

Not caught. Every existing test uses `make_result(initial_capital=100000.0)` with integer
PnL, so `_window_starting_equity` returns a value that is already a whole cent and the
round is a no-op. `test_window_starting_equity_reconciles_within_one_cent` deliberately
adds a fractional engine point (100_500.005) but asserts only on the raw return value, not
on the curve point built from it.

#### Hardening suggestions

Quantise once, at the boundary: set `starting_equity = round(float(starting_equity), 2)`
immediately after the finite check (near line 419), so both the inception point and
`running_equity` derive from the same quantised value. Then add an assertion-style test
with a fractional `starting_equity` (e.g. `100_000.005`) that pins
`daily_points[0].equity == round(returns[0]["equity"] - pnl, 2)`, which only holds when
both sides share one quantisation point.

---

### 3. The `end_bound` guard is defeated by reordering the two normalisation passes

**Severity:** Medium
**Component:** `runner/daily_equity.py` — `_strictly_increasing_curve`, coalescing loop
(lines 191–203) and shift loop (lines 205–220)
**Fragility type:** Implicit ordering dependencies

#### What happened

A window whose end instant fell a few microseconds after a day close published a curve
whose last point sat one microsecond **after** the requested end. Downstream reporting
that filtered `time <= end` dropped the final close, so the window appeared to end a day
early and the reported window return was short by the last day's PnL.

#### The change that caused it

A developer notices that the coalescing pass and the shift pass both parse
`point.time` and merges them into one loop to save a pass: parse once, and when the
timestamp collides with the previous one, replace the last element of `normalized`
instead of maintaining a separate `coalesced` list. The result is strictly increasing and
all existing tests pass — the motivation is a clean performance/readability win on a hot
path.

#### Why it broke

The `end_bound` check at line 216 lives *inside* the shift branch of the **second** loop,
and it only runs when `current <= previous` where `previous` starts at `inception`. In the
merged loop the collision is resolved by replacement, not by shifting, so the branch that
contains the guard is never taken for adjacent duplicates — exactly the input the guard
was added for (the guard is currently reachable only through the
`inception`-equals-first-point path). The guard is load-bearing for the "never overshoot
the window" contract but its reachability depends entirely on the two passes staying
separate.

#### How it was caught

Not by the pre-existing suite — the guard had *zero* coverage before this PR's
verification pass (a surviving mutant proved it). It is only reachable at all because this
PR added two tests that call `_strictly_increasing_curve` directly with an `end_bound`.
A merged-loop refactor would still pass those two tests (they exercise the
`inception`-equals-point path) while breaking the multi-point window path.

#### Hardening suggestions

Add a test that drives the guard through the *public* entry point rather than the private
helper: construct a result whose end instant lands exactly one microsecond short of a day
close, call `attach_daily_equity(result, end=...)`, and assert every published
`point.time <= end_bound`. That test survives the refactor described above because it
does not care which loop performs the shift.

---

### 4. The one-cent preservation gate is compared against a rounded ledger endpoint

**Severity:** Low
**Component:** `runner/daily_equity.py` — `_preserves_engine_curve`, line ~364
(`daily_final = float(daily_points[-1].equity)`) and the `abs(...) <= 0.01` return
**Fragility type:** Coincidental correctness

#### What happened

A user reported that a run flipped between "engine curve preserved" and "engine curve
rebuilt" across two executions with byte-identical inputs. The two curves have different
interior points, so the viewer chart visibly changed between runs.

#### The change that caused it

A developer refactors the endpoint comparison to stop relying on the caller passing
`daily_points` at all, and instead recomputes the ledger endpoint inside
`_preserves_engine_curve` from `trades + initial_capital` — reading the raw accumulated
balance rather than the rounded curve point. This looks like a decoupling improvement: the
function no longer needs its `daily_points` parameter for anything but the length check.

#### Why it broke

`daily_points[-1].equity` is `closed_equity = round(running_equity, 2)`, so the current
gate compares the engine endpoint against a **quantised** ledger value. Recomputing from
trades yields the raw float, which differs by up to half a cent — precisely the order of
the 0.01 tolerance. An engine curve whose true endpoint sits at 100_500.005 is preserved
today (|100_500.005 − 100_500.00| = 0.005 ≤ 0.01) but rejected after the refactor
(|100_500.005 − 100_500.0049…| is still under 0.01, but a curve at 100_500.0051 flips).
The `drift=0.005 → preserved=True` parametrisation in
`test_endpoint_drift_is_judged_by_an_absolute_one_cent` sits exactly on that cliff.

#### How it was caught

Partly — the existing parametrised test (`0.005 → True`, `0.015 → False`) would likely
catch a gross version of this change, but it passes `initial_capital=1e12` and integer
PnL, where the raw and rounded endpoints coincide except for the injected `drift`. A
float-noise variant of the refactor slips through because the test's own arithmetic is
exact.

#### Hardening suggestions

Make the dependency explicit rather than incidental: compute `daily_final` from the
quantised value and say so — `daily_final = round(float(daily_points[-1].equity), 2)` —
with a comment stating that both sides of the tolerance must be quantised so the
comparison is decided by real drift, not float noise. Add a parametrised case at
`drift=0.0049` and `drift=0.0051` alongside the existing `0.005` case to pin the cliff
from both sides.

---

### 5. `effective_start` / `effective_end` recomputed in three places can drift apart

**Severity:** Medium
**Component:** `runner/daily_equity.py` — `attach_daily_equity` lines ~546–551
(`effective_start`, `effective_end`, `scoped_result`, `_window_starting_equity`) and the
`preserved_curve` call at ~line 586
**Fragility type:** Shotgun surgery / invisible invariants

#### What happened

After a follow-up commit added a per-run override for the backtest end, windowed runs
began reporting `daily_returns` for a wider period than the published equity curve. The
matrix CSV's `equity` column and its `daily_return` column described different windows for
the same `run_id`, and a downstream comparison against the benchmark silently used the
longer period.

#### The change that caused it

A developer threads a new optional override through the path and updates the two call
sites that obviously need it — `_scoped_view(result, effective_start, effective_end)` and
`build_daily_equity_curve(..., start=effective_start, end=effective_end, ...)` — but misses
`_windowed_curve(result, effective_start, effective_end)` at line 586. Nothing in the code
says the four locals must stay identical; they are computed once at 546–547 and then
*referenced* in three separate call sites that are ~40 lines apart with an early return
between them.

#### Why it broke

The PR deliberately introduced the "resolve the window once" rule (the comment at lines
542–545 and the pre-mortem reference in the `preserved_curve` comment) precisely because
this class of bug already happened once. But the rule is enforced only by a comment. The
`if not daily_points:` early return at line 560 splits the function in two, so an editor
working on the second half has no visual cue that `effective_start`/`effective_end` were
derived from `start_bound or _parse_ts(result.meta.backtest_start)` and must not be
recomputed from the raw `start_bound`/`end_bound` out-parameters.

#### How it was caught

Partly: `test_window_scopes_preserved_engine_curve` covers the explicit-window case and
`test_fallback_scopes_existing_curve_to_the_requested_window` covers the fallback path. But
no test covers the **meta-derived** window on the preserved-curve path — a regression that
passes `start_bound`/`end_bound` instead of the effective values is only distinguishable
when the window comes from `meta.backtest_start`/`backtest_end` alone, which none of the
preserved-curve tests exercise.

#### Hardening suggestions

Bundle the two locals into one value at the point of resolution — a small frozen
`_Window` dataclass (or a `(start, end)` tuple named `window`) — and pass `window.start` /
`window.end` at all three call sites instead of two loose locals. Add a test that sets
`backtest_start`/`backtest_end` in meta (no explicit args) with a dense engine curve that
would otherwise be preserved, and assert the published `result.equity_curve` stays within
`[window.start, window.end]` while `daily_returns` covers the same days.

---

## Themes and Recommendations

Two structural themes run through all five.

**Cent quantisation happens at the wrong layer, repeatedly.** Post-mortems 1, 2 and 4 are
all the same bug in different clothing: a raw accumulated float and a rounded display
value coexist in the same function, and each new "consistency" edit has a fresh
opportunity to pair the wrong two. The fix is to quantise **once, at the boundary** — round
`starting_equity` right after its finite check, derive `closed_equity` once per day, and
compute the preservation gate from quantised values on both sides — so there is exactly one
quantised number per instant and no raw twin to be accidentally compared against. A single
`_cents(value: float) -> float` helper would make the quantisation points greppable and
reviewable in one place.

**Window/curve bookkeeping is carried in loose locals with a comment as the only contract.**
Post-mortems 3 and 5 both stem from invariants that the PR states in prose ("resolve the
window once", "never overshoot the window end") but does not encode in a type or an
assertion. The `_Window` bundle in #5 and the public-entry-point window test in #3 are the
two structural changes that would convert both prose contracts into enforced ones. Neither
extends the PR's scope: both are local to `daily_equity.py`.

No live bugs were found while reading; each scenario above is a plausible future edit, and
the "How it was caught" sections are the honest answer to whether today's suite would stop
it.

## Pre-Mortem Cycle 2 — window starting-equity basis

**Scope:** `runner/daily_equity.py` — `WindowStartingEquity`,
`_window_starting_equity`, `attach_daily_equity` (the basis/warning change)
**Date:** 2026-11-01

### Summary

The change makes a previously silent accounting decision visible: a warning when the
engine starting point cannot be reconciled with the realized net ledger, and
`meta.data["window_start_basis"]` in the report. Four fragilities follow from that:
the warning can drift out of its once-per-run slot, its level can be quieted back to
invisibility, the warning and the basis field can drift apart, and the field can go
stale across a re-attach.

### 1. The warning becomes a log flood

**Severity:** Medium
**Component:** `runner/daily_equity.py` — `_window_starting_equity`, the `logger.warning` branch
**Fragility type:** Implicit ordering dependencies

#### What happened

A walkforward run of 240 windows produced 240 identical warnings, and a large grid
produced thousands. Operators started filtering `runner.daily_equity` out of the log
pipeline, which also hid the real divergences.

#### The change that caused it

The warning was moved into `build_daily_equity_curve` ("closer to where the equity is
used") and then into the per-day loop, because a developer wanted to know *which* day
was affected.

#### Why it broke

The once-per-run granularity is a property of the call site — `_window_starting_equity`
runs once per `attach_daily_equity`, while `build_daily_equity_curve` runs per day. The
docstring states the constraint in prose; nothing enforces it.

#### How it was caught

`test_unreconciled_start_warns_once_and_reports_the_ledger_basis` asserts exactly one
WARNING record for one attach, so the per-day move fails immediately.

#### Hardening suggestions

- Keep the "exactly one" assertion (present) rather than "at least one".
- Keep the docstring sentence naming the granularity (present).

### 2. The warning and the basis field drift apart

**Severity:** High
**Component:** `runner/daily_equity.py` — `attach_daily_equity`, basis assignment vs the warning
**Fragility type:** Invisible invariants

#### What happened

A report carried `window_start_basis="net_ledger"` for a run whose window contained no
trades, so the published curve was the engine's own curve. A reconciliation report then
compared the engine curve against a ledger base it had never been computed from.

#### The change that caused it

An earlier cut of this change set the field only where the daily build is published and
dropped it on the engine-curve fallback. Both directions are plausible edits: "only
report what was published" versus "always report the base the warning refers to".

#### Why it broke

The field and the warning are two halves of one fact: *the engine starting point
disagreed with the ledger and the ledger value was used for the window base*. Publishing
one without the other is what misleads — a warning with no basis, or a basis with no
warning.

#### How it was caught

Spec review on the changed diff (the spec asks for the field so the basis "appears in
the report"; an absent field contradicts the normal-path contract). Now pinned by
`test_engine_curve_fallback_still_reports_the_ledger_basis` (warning + field together)
and `test_reconciled_start_reports_the_engine_basis_without_warning` (neither).

#### Hardening suggestions

- Set the field on every path that computed a window base, immediately next to the
  computation (done), so no branch can publish one half.
- Keep the pair asserted in both directions (done).

### 3. Someone quiets the warning back to debug

**Severity:** High
**Component:** `runner/daily_equity.py` — `logger.warning` call
**Fragility type:** Load-bearing defaults

#### What happened

After a noisy week the level was lowered to `debug` "to reduce log volume", which is
exactly where this code started. The broker/engine divergence became invisible again.

#### The change that caused it

A one-word edit that looks like a logging preference, not an accounting decision.

#### Why it broke

The visibility of the fallback is the entire point of the change; the log level is the
only thing carrying it.

#### How it was caught

The test captures the module logger at WARNING and asserts `levelno == logging.WARNING`;
lowering the level makes the record disappear from the capture and the test fails.

#### Hardening suggestions

- Keep the level assertion, not just the message assertion (present).
- The `extra={"data": {...}}` payload also carries the fact to structured log consumers
  independent of the message text (present).

### 4. A stale basis survives a re-attach

**Severity:** Medium
**Component:** `runner/daily_equity.py` — `attach_daily_equity`, stale-field cleanup
**Fragility type:** Semantic coupling through shared mutable state

#### What happened

`backtest_grid` re-attaches to an existing result (`skip_existing`). The second attach
returned early on an empty result, but the report still carried the previous attach's
`window_start_basis="net_ledger"`.

#### The change that caused it

A future edit adds an early return before the pop, or moves the empty-result branch above
the cleanup, mirroring how `daily_return_count` had to be cleaned up in two places.

#### Why it broke

`result.meta.data` is shared mutable state across attaches; every field needs an
explicit cleanup on each early-return path, and the paths are not centralised.

#### How it was caught

`test_empty_result_clears_stale_daily_fields` pre-seeds both a stale count and a stale
basis and asserts both are gone.

#### Hardening suggestions

- Consider one `_clear_daily_meta(result)` helper if a third field is added; today the
  two pops are cheaper than the indirection.

### Themes and Recommendations

**Observability is now part of the contract.** The basis field and the warning are the
first things in this module that a human reads rather than a statistic consumes. Both
are one edit away from being wrong in a way no value assertion would catch, so the tests
assert *counts, levels and pairing*, not just presence.

**Report fields must describe a fact completely.** Post-mortem 2 generalises: a field and
the warning that explains it belong to one fact and must be written on the same paths.
Whenever a field is added next to `equity_frequency`/`daily_return_count`, check all three
early-return paths in `attach_daily_equity` and the two publish branches.
