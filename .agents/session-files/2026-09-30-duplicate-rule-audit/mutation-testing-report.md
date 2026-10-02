# Mutation testing report — duplicate-rule audit session

Scope: production files changed by this session, per the `platform-pr-verify`
pipeline (step 2). Test files are out of scope by the method.

- `risk/models.py`
- `data_tools/downloaders/alpaca_dl.py`
- `data_tools/downloaders/huggingface_dl.py`

Pre-flight: clean tree, baseline green (`3758 passed / 87 skipped`, exit 0).
Every mutation was reverted and the revert verified by checksum before the next
one. No mutation was ever left in place.

## Results

| #   | File                  | Mutation                                       | Result   | Diagnostic | Notes |
|-----|-----------------------|------------------------------------------------|----------|------------|-------|
| M1  | `risk/models.py`      | `_finite_positive`: `> 0` becomes `>= 0`        | Killed  | Clear      | `test_a_present_but_non_positive_stop_is_refused` |
| M2  | `risk/models.py`      | `_finite_fraction`: `<= 1.0` becomes `< 1.0`    | Killed  | Clear      | fraction of exactly 1.0 refused |
| M3  | `risk/models.py`      | `_finite_nonnegative`: `< 0` becomes `<= 0`     | Killed  | Clear      | over-strict zero |
| M4  | `risk/models.py`      | delete the `bool` guard in `_finite_float`      | Killed  | Clear      | the fix this session made; `entry_price=True` returns to 1.0 |
| M5  | `risk/models.py`      | `_finite_float`: `isfinite` becomes a NaN check  | Killed  | Clear      | `inf` slips through |
| **M6** | `risk/models.py`    | `_optional_finite_positive`: delete `None` guard| **Survived → fixed** | — | guard is load-bearing |
| **M7** | `risk/models.py`    | `_parse_bool`: drop the `int {0,1}` branch      | **Survived → fixed** | — | `accepted=1` rejected |
| M8  | `risk/models.py`      | `_finite_float`: accept `inf`                   | Killed  | Clear      | re-confirmed after M6/M7 fix |
| D1  | `alpaca_dl.py`        | `_to_utc_timestamp`: `raise` → `coerce`         | Killed  | Clear      | body-equality pin across the three downloaders |
| D2  | `alpaca_dl.py`        | drop the empty-`DatetimeIndex` guard            | Killed  | Clear      | body-equality pin |
| D3  | `alpaca_dl.py`        | `_normalize_supported_timeframe`: delete refusal| Killed  | Clear      | |
| D4  | `alpaca_dl.py`        | drop `.strip().lower()` from normalisation      | Killed  | Clear      | caught by the alias-coverage pin's padded/upper-case cases |
| D5  | `huggingface_dl.py`   | `_to_utc_timestamp`: `raise` → `coerce`         | Killed  | Clear      | |
| D6  | `huggingface_dl.py`   | reintroduce the dead second lookup               | Killed  | Clear      | proves the pin catches a *resurrected* dead branch, not just a new one |

**Score: 14/14 killed** after adding tests for the two survivors (12/14 before).

## The two survivors, and why they were real

Neither helper was named in **any** test file in the repository before this pass.
Both guard the objects a live trade is built from:

- **`_optional_finite_positive`** validates `TradeSignal.stop_loss` and
  `TradeSignal.take_profit`. A signal with **no stop** is legitimate — the ATR
  fallback and the stop-less backtest work in this repo both rely on it — so the
  `None` guard is load-bearing. Deleting it turns an accepted stop-less signal
  into a raised `ValueError`, i.e. it removes a supported position shape rather
  than adding a stricter check.

- **`_parse_bool`** validates `RiskDecision.accepted`. Integers `0`/`1` reach it
  from JSON and from numpy scalars. Deleting that branch makes a decision built
  with `accepted=1` fail to construct.

Fixes: `tests/test_risk_models.py` (31 tests) — absent optionals accepted,
present-but-invalid optionals still refused, integer booleans accepted, the
inclusive fraction bounds pinned, and a bool `risk_percent` documented as the
type error it is rather than a range error.

## Two tests I wrote were wrong, and the code was right

Worth recording, because the instinct on a failing new test is to suspect the
new test:

- `SizedOrder` has no `stop_price_used`/`take_profit_used` fields; I invented
  them. The real optional there is `risk_reward_ratio`, and `0.0` is a real ratio
  rather than an absence — so both are now asserted.
- `risk_percent=True` does **not** raise the range error. `_finite_fraction`
  delegates to `_finite_float`, whose `bool` guard fires first, so the message is
  `risk_percent must be numeric`. `True` is `1.0`, which is *inside* `[0, 1]` —
  which is exactly why the bool guard is the only thing that catches it, and why
  the message is the useful one. Split into its own test rather than loosening the
  regex.

## Diagnostic quality

All killed mutations produced a **clear** failure: the test name states the
behaviour and the assertion names the field. No cascading failures were observed,
which indicates the risk-model coercions have focused tests rather than only being
exercised incidentally through large integration tests.

## The harness was itself wrong once, so it was replaced

Worth its own section, because it is the finding that matters most in this
report.

This report claims two mutations survived. **One of them did not.** An independent
reviewer found that deleting the `_optional_finite_positive` `None` guard makes
**177 tests fail** without `tests/test_risk_models.py`, including pre-existing ones in
`tests/live_runner/` and `tests/validation/test_risk_sizing.py`. Re-running the mutation
against *the exact selection used here* fails too — 36 failures, exit 1.

So this report's claim that it "survived the whole suite" was wrong. It survived a
*scoped selection*, and the scope was chosen after the fact rather than derived.

The worse half: when the same harness was replayed with the same arguments it then
reported **KILLED**. The mutation had applied and the suite really did fail, yet an
earlier invocation of that harness printed "no test failed". That is a harness that
can emit an unverified verdict, and it is the same failure as everything else this
session has been about — a clean answer from a run that was not clean.

The `_parse_bool` survivor is real and unaffected: exactly two failures, both from the
new tests.

### `mutate.sh`

Rather than trust the ad-hoc harness to keep being careful, the paths by which it can
lie are now closed:

| exit | meaning |
|---|---|
| 0 | applied, suite ran, reverted, and the verdict is **backed by a named failing test** (or SURVIVED, which needs no attribution) |
| 2 | mutation text not uniquely present — **nothing was mutated**, never "survived" |
| 3 | applied but the file did not change — aborted |
| 4 | **no verdict** — usage error, missing path, nothing collected, a file failed to collect, **or pytest exited non-zero without any test failing** |
| 5 | verdict disagreed with `--expect` |
| 6 | revert failed — working tree dirty |
| 7 | KILLED-UNATTRIBUTED: a test failed, but not the one that should have. **Wrong**, not merely unverified. Never masked by `--expect` |
| 8 | KILLED with no `--detector`: some test **did** fail, but nothing proves it was this mutation. **Unverified**. Never masked by `--expect` |

### A kill requires a failing test

The row for rc=4 was widened on 2026-10-02, and it is the most consequential line in the
table. For most of its life the harness decided `KILLED` from `rc != 0` alone.
`FAILURES` — the count of failing test ids — was computed two lines above the verdict and
used **only for the log line**, which is why that survived three rounds of auditing this
harness.

Measured, against a suite reporting `1 passed, 1 error` — the test passed, an autouse
fixture's teardown raised:

```
teardown error, not a test failure    KILLED   rc=1 collected=1 failed=0 attr=unknown
```

`failed=0`. Nothing was caught, and the printed word said KILLED. A non-zero pytest exit
means "something went wrong", and besides a test failing it can be: a fixture teardown
error, a warning promoted to an error by config (`-W error` exits non-zero with every test
passing), a plugin session-finish hook returning non-zero, or an xdist plugin failing after
the tests ran. All of those exit 1 with no failing test id.

This matters more here than a misread would suggest. A mutation reported KILLED becomes a
**committed test**; one reported SURVIVED becomes a **new test to write**. A wrong verdict
does not merely mislead — it writes the wrong code.

Exit 2 and exit 4 are the two that matter. A mutation that silently failed to apply,
and a suite that never collected anything, both look like a verdict to a reader — and
the *first version of this script made exactly the exit-4 mistake*, calling pytest's
usage-error exit 4 a "KILLED". That was caught by its own self-test, which is the
argument for writing the self-test at all.

Self-tested on five paths: real kill, real kill with a case-insensitive `--expect`,
nonexistent suite path (infra), absent mutation text (apply-failed), and a
deliberately wrong expectation (mismatch).

## Every verdict in this report re-verified through `mutate.sh`

The table above was produced by the ad-hoc harness, and that harness was later shown to be able
to print a verdict it had not verified. So all 14 were re-run through the replacement, which
refuses to report a verdict it cannot prove.

| # | earlier | re-verified | note |
|---|---|---|---|
| M1 | Killed | **Killed** (2 failures) | agrees |
| M2 | Killed | **Killed** (1) | agrees |
| M3 | Killed | **Killed** (3) | agrees |
| M4 | Killed | **Killed** (5) | agrees |
| M5 | Killed | **Killed** (7) | agrees |
| **M6** | **Survived** | **Killed (41 failures)** | **the earlier verdict was false** |
| M7 | Survived | Killed (2) | the survivor status was **true at the time**; the new tests now kill it |
| M8 | Killed | **Killed** (7) | agrees (M5 and M8 are the same mutation, listed twice) |
| D1–D6 | Killed | **Killed** (1–2 each) | all agree |

**The M6/M7 split is the substantive result.** Both were reported as survivors. Only one was:
`_parse_bool`'s integer branch genuinely had no coverage, and `tests/test_risk_models.py` now
kills it. The `_optional_finite_positive` guard was already covered by 41 failures once the new
file was in the suite — the "survivor" was the harness, not the test suite.

## The replacement harness found a bug in itself, twice

Both worth recording, because each was caught by a check rather than by care:

1. It called pytest's **usage-error exit (4) a "KILLED"** — a suite that never collected anything
   reported as a mutation caught. Now exit 4 is INFRA with no verdict.
2. On **D6** it refused a mutation that had applied correctly, because the replacement was a
   *superstring* of the original (`x = f(a)` → `x = f(a) or g()`), so "old text still present"
   was trivially true. The guard now skips that case and asserts absence only when the
   replacement does not contain the original.

The second is the more interesting one: the guard was correct in principle and wrong in a case
that the previous, unchecked harness had silently allowed. The two bugs are opposite — one too
permissive, one too strict — and both were found by running the thing rather than by reading it.

## Method notes

- The first attempt at this pass used a bash array with `:` as a delimiter while
  the descriptions also contained `:`, so every label printed empty; and `out`
  was assigned inside a function, so it never reached the caller and no test names
  were captured. Both were invisible in the output — the run *looked* fine and
  carried no diagnostics. Worth noting because the same class of error (a green
  summary with nothing verified in it) is what this whole session has been about.
- One mutation was reported `NOT-APPLIED` (M4's first form) because the guard text
  appears twice in the file — once in `_finite_float`, once in `_parse_bool`. The
  unapplied mutation was re-expressed with its following line to disambiguate
  rather than being reported as a pass.

## The whole ledger re-verified under the new rule (2026-10-02)

Because the kill rule changed, every verdict above is suspect until its evidence is
re-derived — and no historical verdict recorded the evidence needed, because the harness
did not compute it for the verdict. `reverify_ledger.py` re-applies all 14 mutations,
reconstructed from the descriptions in this report, and requires each to come back
**rc=0 (killed AND attributed) with `failed >= 1`**. Two passes each: the first proves a
kill exists, the second sets `--detector` to the test the first found, which is the only
value a caller may trust.

| # | this report said | re-verified at `c7afca1` | failing tests |
|---|---|---|---|
| M1 | Killed (2) | KILLED + attributed | 2 |
| M2 | Killed (1) | KILLED + attributed | 1 |
| M3 | Killed (3) | KILLED + attributed | 5 |
| M4 | Killed (5) | KILLED + attributed | 3 |
| M5 | Killed (7) | KILLED + attributed | 4 |
| M6 | Killed (41) | KILLED + attributed | **51** |
| M7 | Killed (2) | KILLED + attributed | 2 |
| M8 | Killed (7) | KILLED + attributed | 4 |
| D1–D6 | Killed (1–2 each) | KILLED + attributed | 1, 1, 1, 2, 1, 1 |

**All 14 hold.** No verdict in this report was a non-failure exit wearing a KILLED label.
M6's count rose from 41 to 51 because the re-verification suite is wider than the one used
here; that is a selection difference, not a change in the code.

Two things worth recording about the re-verification itself:

- **M6's anchor appears in two helpers**, so the first attempt reported ANCHOR-MISS rather
  than a pass. Re-expressing it with the following line disambiguated it — the same fix this
  report already describes for M4, and for the same reason: a mutation that cannot be
  applied is not a pass, it is an absence of evidence.
- **`reverify_ledger.py` was run by the gate suite and made it 94 minutes long**, which is
  why it is now opt-in behind `FULL_GATES=1`. A gate nobody runs is not a gate — the same
  argument as the check that a gate's own test file must exist. It had failed for a second
  reason too: leaving `reverify_ledger.py` uncommitted made the forward worktree dirty, which
  claim-check correctly flags.

The ledger is now **checked by running it** rather than trusted because it once said "14/14
killed".
