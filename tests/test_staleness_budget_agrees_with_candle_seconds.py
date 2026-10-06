"""Two spellings of one timeframe must not buy two staleness policies.

`_timeframe_age_budget_seconds` used to lower-case its input and look the label up in a private dict.
That works for the canonical spellings and misses the aliases, so `M1`/`m1` fell through to the
300.0s default while `1m` hit its own entry.

The daemon's caller computes `max(300.0, 2.0 * budget)`, so the same one-minute candle was allowed a
600s effective staleness budget under `M1` and 300s under `1m` - a 2x wider window on how old a
signal may be and still be traded. Meanwhile `execution_timing._timeframe_seconds`, the other half of
the same signal path, routes through `normalize_timeframe` and correctly resolved `M1` to 60s. So the
two halves disagreed about what `M1` means.

This is not the M1/monthly coin-flip in backlog item 3, which is still undecided. That is about whether
`M1` should be accepted at all. This is narrower and decidable: given that `M1` is accepted and
normalised to `1m` everywhere else, its staleness budget must equal `1m`'s.
"""

from __future__ import annotations

import pytest

from live_runner.daemon import _timeframe_age_budget_seconds
from shared.constants import normalize_timeframe


@pytest.mark.parametrize("alias", ["M1", "m1"])
def test_an_alias_gets_the_same_budget_as_its_canonical_spelling(alias: str) -> None:
    canonical = normalize_timeframe(alias)
    assert canonical == "1m", (
        f"{alias} now normalises to {canonical!r}, so this test's premise needs re-deriving - if the "
        f"alias no longer means 1m there is nothing to compare against"
    )
    assert _timeframe_age_budget_seconds(alias) == _timeframe_age_budget_seconds(canonical), (
        f"{alias} and {canonical} are the same timeframe but receive different staleness budgets, so "
        f"a spelling typo silently widens how old a signal may be and still be traded"
    )


def test_every_alias_normalising_to_a_known_timeframe_gets_that_timeframes_budget() -> None:
    """The general form of the bug: any alias, not just the two M1 spellings."""
    checked = 0
    for label in ("M1", "m1", "1m", "5m", "m5", "15m", "1h", "h1", "1d", "d1", "1w"):
        canonical = normalize_timeframe(label)
        known = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
        if canonical not in known:
            continue
        checked += 1
        assert _timeframe_age_budget_seconds(label) == _timeframe_age_budget_seconds(canonical), (
            f"{label} normalises to {canonical} but the two budgets differ"
        )
    assert checked >= 8, f"only {checked} labels were comparable; the alias table has changed"


def test_the_budget_matches_the_candle_seconds_the_other_half_of_the_path_uses() -> None:
    """The disagreement this fixes, stated as the cross-check that would have caught it."""
    from live_runner.signal_sources.execution_timing import _timeframe_seconds

    for label in ("1m", "M1", "m1", "5m", "1h"):
        candle_seconds = _timeframe_seconds(label)
        if candle_seconds is None:
            continue
        budget = _timeframe_age_budget_seconds(label)
        # The caller allows max(300, 2*budget); the candle is what the signal is timed against.
        assert budget == float(candle_seconds) or budget >= float(candle_seconds), (
            f"{label}: staleness budget {budget}s is below the candle {candle_seconds}s, so a signal "
            f"older than its own candle could be refused before it can be acted on"
        )


def test_a_monthly_alias_still_falls_to_the_default_rather_than_a_minute_budget() -> None:
    """MN1/1mo are refused at config load, so this path is unreachable - and stays unchanged.

    Pinned deliberately: if someone later routes them through a monthly entry, this test is the
    reminder that their config-load refusal and this lookup have to be decided together.
    """
    for label in ("MN1", "1mo"):
        assert _timeframe_age_budget_seconds(label) == 300.0, (
            f"{label} now resolves to a real budget, so the config-load refusal in shared.models and "
            f"this default need re-deciding together"
        )
