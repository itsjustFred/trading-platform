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
