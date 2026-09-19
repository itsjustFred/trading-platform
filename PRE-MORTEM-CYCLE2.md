# Pre-Mortem Cycle 2 — Follow-up Fix Commits (PR #74)

Scope: `b8162bf` (us100-scalper TZ + frame-end flatten), `2e064d4` (pynecore-orb day-reset
flatten), `7cf181d` (risk-parity reference note). Cycle 1 findings (PM1–PM4 fixed,
PM5–PM10 recorded as follow-ups) are documented in the PR body; this cycle covers only
the fragility introduced or touched by the three new commits.

---

### Same-bar entry + frame-end flatten books a phantom round-trip loss

**Severity:** Medium
**Component:** `strategies/us100_scalper/us100_scalper.py` — `generate_signals`, frame-end
block (lines 187–195) interacting with the entry block (lines 172–185)
**Fragility type:** Coincidental correctness / non-atomic compound operation

#### What happened

A backtest frame that ends on the very bar where the daily buy-stop is touched books an
entry **and** a `day_reset_flatten` exit on the same bar index. The exit price is the bar
close while the engine fills the entry at the stop level (`entry_stop`, D2 strict), so the
synthetic trade carries the full entry→close distance as PnL even though the position
existed for zero bars. Reproduced: entry at bar 15 (touch 130.5 ≥ stop 130), flatten exit
on bar 15 at close 100.0 → a −30-point round trip for a position that never held.

#### The change that caused it

A developer adds the frame-end flatten (this PR, B-H1 edge) after the main loop without
excluding the bar the entry fired on. It passes review: for every realistic frame the
close is near the stop level, so the phantom loss is small and the trade count is
unchanged.

#### Why it broke

The flatten block only checks `in_trade`, which the entry block sets `True` on the same
bar it fills. Nothing distinguishes "position carried over from an earlier bar" from
"position opened on the final bar". The engine-side same-bar machinery
(`resolve_same_bar_exit` in `runner/engines/contract.py`) can order stop vs. close on that
bar, but the strategy-level flatten unconditionally claims the close.

#### How it was caught

Only by a targeted repro; no existing test constructs an entry-on-last-bar frame. In a
live/regen run it would surface as a small number of trades with `holding_bars == 0` and
large negative PnL — easily mistaken for a genuine stop-out.

#### Hardening suggestions

1. In the frame-end block, skip when the entry fired on the last bar
   (`if in_trade and not entries.iloc[last]:`) — or book the flatten at the entry
   price for a zero-PnL round trip, matching "never held" semantics.
2. Add a regression test: frame ending on the touch bar asserts either no exit or a
   zero-distance exit.
3. Alternatively assert in the block comment why same-bar entry+flatten is intended, so
   a future editor does not "fix" it silently in either direction.

---

### `day_reset_flatten` reason string is an unenforced cross-layer contract

**Severity:** Low
**Component:** `strategies/us100_scalper/us100_scalper.py` (lines 139, 195),
`strategies/pynecore_orb/pynecore_orb.py` (line 96),
`strategies/pynecore_orb/pynecore_orb_context.py` (line 77), engines
**Fragility type:** Stringly-typed contract

#### What happened

A cleanup renames the reason literal in one of the four producer sites (e.g. to
`"session_reset"`). No test fails, no type checker complains; downstream consumers that
group exits by reason silently start reporting a new bucket, and cross-engine parity
comparisons diverge on reason-only diffs.

#### The change that caused it

A developer consolidating exit-reason vocabulary renames the string in the two scalper
sites and misses the ORB script/mirror pair (or vice versa).

#### Why it broke

The literal is duplicated across four files in two layers (strategy scripts and the
pure-pandas mirror) with no shared constant and no test asserting the set of allowed
exit reasons. The engines treat it as an opaque marker today, but any future consumer
(e.g. stats grouping, audit filters) makes the spelling load-bearing.

#### How it was caught

Currently nothing — the string appears only in strategy outputs and test assertions that
each pin their own site. A rename would surface only in manual parity reviews.

#### Hardening suggestions

1. Add a test asserting the exact set of exit-reason literals produced by each
   strategy module (`test_us100_scalper.py`, `test_pynecore_orb.py`) so a rename in one
   site fails loudly.
2. Or define the literal once per strategy pair (script + context import from a shared
   module constant) — the context mirror already imports nothing from the script by
   design, so the test is the cheaper guard.

---

### ORB context flatten marker assumes the prior bar exists and is same-session

**Severity:** Low
**Component:** `strategies/pynecore_orb/pynecore_orb_context.py` — `build_trade_context`
day-change branch (lines 70–85)
**Fragility type:** Invisible invariant

#### What happened

A refactor changes the frame to start exactly at a session boundary (first row is the
first bar of a new day) while a position is somehow marked open — or, more plausibly, a
future edit introduces a second state reset (e.g. weekend gap handling) that also writes
`exit_reason.iloc[i - 1]`. When `i == 0`, `iloc[-1]` silently writes the **last** row of
the frame; a second writer on the same bar makes the reason depend on branch order.

#### The change that caused it

A developer adds weekend/holiday gap handling next to the day-change branch and reuses
the same `iloc[i - 1]` write pattern.

#### Why it broke

The write at line 77 relies on two unenforced invariants: the day-change branch can never
run at `i == 0` (`cur_day` starts as `None` and `position` as `0`, so the marker is
skipped), and only one writer ever targets `i - 1`. Neither is asserted or commented as a
constraint; the guard `if position != 0` is what currently keeps `i == 0` safe, and that
coupling is invisible.

#### How it was caught

Silently — a wrong-row write produces a plausible-looking `exit_reason` on the wrong bar,
which the enrichment step (`_enrich_trades_with_context` in
`runner/engines/pynecore_adapter.py`) then samples at the located exit bar. No test
constructs a first-bar-is-day-change frame with an open position.

#### Hardening suggestions

1. Guard explicitly: `if position != 0 and i > 0:` with a comment naming the invariant
   ("marker must land on the prior session's last bar; i == 0 has no prior session").
2. Add a test with a frame whose first row is a day change (multi-day frame starting
   mid-session is the current coverage; the boundary-start frame is not).

---

## Verdict

- **PM11 (same-bar entry + flatten):** confirmed by repro; hardening fix applied in this
  cycle (skip the frame-end flatten when the entry fired on the final bar; zero-distance
  semantics documented in the block comment).
- **PM12 / PM13:** accepted as-is for this PR (documentation-level hardening only, no
  scope extension); candidates for the follow-up ticket list alongside PM5–PM10.
