# Pre-Mortem Report — PR #115 (P7 Data/Validation)

**Scope:** production files changed by the PR —
`shared/market_calendar.py`, `shared/constants.py`,
`data_tools/{auto_download,validate,resample,dolthub_manager,download_grid_market_data}.py`,
`runner/backtest_grid.py` (alias map only),
`validation/readiness/{models,validator}.py`, `validation/importers/mt5_xlsx.py`,
`validation/comparison/__init__.py` (import order only).
**Date:** 2026-10-30
**Method:** read the changed code and its callers; every scenario below is a small
edit a maintainer could plausibly make, not a rewrite.

## Summary

The slice replaces constants with derived facts — a calendar instead of a length
budget, a derived timeframe set instead of three literals, one alias map instead
of two. That is the right shape, and it moves the fragility: the code now depends
on *tables and lookups agreeing* rather than on numbers being big enough. Five of
the six post-mortems below are the same failure mode in different clothes: a new
entry in one table that is not reflected in another, with the disagreement
manifesting as either a silently excused data hole or a spurious coverage
failure.

One genuine migration hazard is called out separately under **Real risks** — it
is not fictional.

## Post-Mortems

### 1. A new downloader makes every US-equity holiday week fail coverage

**Severity:** High
**Component:** `data_tools/auto_download.py::_uses_us_equity_calendar`, `US_EQUITY_CALENDAR_SOURCES`
**Fragility type:** Stringly-typed contract / invisible invariant

#### What happened

After adding a `polygon` downloader for US equities, every backtest whose data
came from it failed with `OHLCV coverage has internal gaps ... gap_days: 3.73`,
for the week of every market holiday. Re-downloading did not help.

#### The change that caused it

A new downloader was added and its files landed under
`ohlcv/polygon/equity/AAPL/1m.parquet`, matching the existing layout. Nobody
touched `US_EQUITY_CALENDAR_SOURCES` because nothing pointed at it.

#### Why it broke

`_uses_us_equity_calendar` requires **both** the asset class and the source to be
in the allowlists, and `sources.issubset(US_EQUITY_CALENDAR_SOURCES)` is false for
`polygon`. The gate is deliberately fail-closed, so the holiday closure is
reported as a data hole. The failure is loud, but it points at the data rather
than at the missing table entry, and the fix is a one-word edit in a frozenset
forty lines away from the symptom.

#### How it was caught

Not by tests: the tests name `alpaca`/`dukascopy` explicitly. It surfaces on the
first holiday week after the downloader lands, as a coverage failure with a
plausible-looking 3.73-day gap.

#### Hardening suggestions

- Derive the source set from the layout instead of listing it: treat
  `ohlcv/<source>/<asset_class>/...` as US-equity when `asset_class` is in
  `US_EQUITY_ASSET_CLASSES`, and use the source list only to *exclude* known
  non-US venues (dukascopy).
- If the allowlist stays, put it next to the downloader registry so adding a
  downloader is one edit.

### 2. The dead `allow_long_weekend` parameter gets a body again

**Severity:** Medium
**Component:** `data_tools/auto_download.py::scaled_gap_threshold_days`
**Fragility type:** Load-bearing default / speculative generality

#### What happened

A later change reintroduced a flat long-weekend floor inside
`scaled_gap_threshold_days` under `if allow_long_weekend:`, and 1-minute data with
a 3.6-day hole passed the gate again — the exact defect this PR removed.

#### The change that caused it

Someone reading the signature saw a parameter that is accepted and ignored,
assumed it was a leftover, and "restored" its behaviour to make it meaningful.

#### Why it broke

The parameter is documented as kept for call-site clarity, but a parameter that
does nothing is a standing invitation to give it a body. The real allowance lives
in `_gap_is_market_closure`/`closure_hole_allowance_seconds`, applied per hole.

#### How it was caught

Only if the 3.6-day regression test still exists and still runs; the parameter's
docstring is the only other signal.

#### Hardening suggestions

- Remove the parameter and update the three call sites; the per-hole allowance
  needs no flag. (Deferred here because it changes a public signature in a
  verification cycle, and Sonar already flags it as unused.)

### 3. A deeper dataset directory silently loses its calendar metadata

**Severity:** Medium
**Component:** `data_tools/validate.py::_dataset_metadata`
**Fragility type:** Assumptions baked into data transformations

#### What happened

A downloader started writing `ohlcv/dukascopy/forex/EURUSD/2024/1h.parquet`. The
canonical key became `forex/EURUSD/2024/1h` instead of
`dukascopy/forex/EURUSD/1h`, so `_dataset_metadata` returned `{}` and validation
fell back to the observed-session heuristic. Coverage warnings stopped appearing
for those files, and a real two-day hole was reported as `ok`.

#### The change that caused it

A per-year directory was added for a segmented dataset — a reasonable layout
change that no test constrains.

#### Why it broke

The key is reconstructed from positional parts (`path.parts[-4:-1]` plus the
stem). Any layout with an extra level shifts every component and produces a key
that looks plausible but matches nothing. The lookup is a silent `dict.get`.

#### How it was caught

Not at all: a missing key is indistinguishable from "no metadata was ever
written", which is a supported state.

#### Hardening suggestions

- Build the key from the `ohlcv` root: `path.relative_to(root).with_suffix("")`
  is layout-independent and fails loudly if the path is outside the root.
- Log at debug when a `source_meta.json` exists but no key matched, so the
  fallback is observable.

### 4. The closure allowance stops following the calendar when a session is added

**Severity:** Medium
**Component:** `shared/market_calendar.py` (`_HOLIDAY_RULES`, `session_open_utc`), `data_tools/auto_download.py::closure_hole_allowance_seconds`
**Fragility type:** Implicit ordering / invisible invariant

#### What happened

A half-day calendar was introduced for early closes (13:00). The coverage
allowance kept using the 16:00 model, so the measured hole for an early-close
Friday grew by three hours relative to the allowance and legitimate long weekends
around Thanksgiving started failing coverage.

#### The change that caused it

Someone added early-close support to `market_calendar` and changed
`session_close_utc` for those days, without re-deriving
`closure_hole_allowance_seconds`.

#### Why it broke

The allowance is `close(start) → open(end)` plus two session tails. The tails
were sized to absorb the *current* three-hour error; once the calendar models
early closes, the error changes sign and the tails are the wrong size. Nothing
ties the tail width to the modelled session.

#### How it was caught

Only by the holiday-week coverage tests, and only if the fixture dates include an
early close.

#### Hardening suggestions

- Express the tail as a derived quantity (e.g. `max(0, model_close - real_close)`)
  once half-days are modelled, instead of `2.0 * session_span_seconds`.
- Add an early-close case to the allowance tests now, so the assumption is at
  least visible.

### 5. `US_EQUITY_ASSET_CLASSES` gains a class the calendar cannot speak for

**Severity:** Medium
**Component:** `data_tools/auto_download.py::_uses_us_equity_calendar`
**Fragility type:** Stringly-typed contract

#### What happened

`crypto` was added to `US_EQUITY_ASSET_CLASSES` by someone who read the name as
"assets we have a calendar for". 24/7 crypto series then had their weekend gaps
excused as market closures, and a dead crypto feed passed coverage.

#### The change that caused it

A one-line addition to a frozenset whose name does not say "venues the NYSE
calendar models".

#### Why it broke

The set is a *proxy* for "the US equity calendar describes this venue". Its name
describes a taxonomy, not a calendar relationship, and the two only coincide
today.

#### How it was caught

`test_a_four_day_fx_outage_is_not_excused_by_the_us_equity_calendar` covers the
symbol path, not a newly added asset class.

#### Hardening suggestions**

- Rename to `US_EQUITY_CALENDAR_ASSET_CLASSES` and document the rule at the
  definition ("the NYSE regular session describes this venue's trading week").
- Add a test that iterates the set and asserts each member's symbol family is
  refused when it is not a US equity.

### 6. The once-per-value warning cache outlives a config change

**Severity:** Low
**Component:** `data_tools/validate.py::_MALFORMED_SESSION_WARNED`
**Fragility type:** Semantic coupling through shared mutable state

#### What happened

A long-running viewer process validated a file with a typo'd session, the
operator fixed the config, and the corrected value produced no new warning —
while the old value stayed in the module-level set for the process lifetime.

#### The change that caused it

The memo was added to stop one warning per gap. Nobody added an eviction path.

#### Why it broke

`_MALFORMED_SESSION_WARNED` is process-global and keyed by `repr(value)`. It is
only cleared by `reset_malformed_session_warnings()`, which only tests call.

#### How it was caught

It would not be; the warning is advisory.

#### Hardening suggestions**

- Key the set by `(path, value)` or clear it per validation run, so a fixed config
  warns again on the next run.
- Bound it (an LRU) so a long-lived process cannot grow it without limit.

## Real risks (not fictional)

**R1 — Weekly relabelling collides with stored history.** `data_tools/resample.py`
now labels weekly bars on Monday (`W-MON`) instead of Sunday. Any existing
`1w.parquet` written under the old rule still holds Sunday-stamped bars, and
`merge_resampled_history` unions on timestamp, so a re-download keeps *both* the
old Sunday bar and the new Monday bar for the same week. A migration (drop or
re-label existing weekly files) is needed before the next weekly re-download; it
is deliberately out of this slice's scope and should be its own change.

**R2 — Two calendars.** `data_tools/auto_download._nyse_holidays` (used by
`validate.py`) and `shared.market_calendar.holidays` both encode the US equity
closure rules. They agree today, including the New Year exception, but nothing
asserts that. Collapsing them is the obvious follow-up.

## Themes and Recommendations

Every post-mortem above is a table that must agree with another table, or a
constant that must move when a fact moves. The structural fix is to derive the
dependent value at the point of use rather than restate it — which is exactly
what this PR did for the timeframe sets. The two remaining restatements are the
source allowlist (1) and the session-tail width (4); both are cheap to derive and
should be derived before the next venue or calendar feature lands.

---
Model: deepseek-v4.1-flash, Harness: T3 Code (ACP, 2026-10-30)
