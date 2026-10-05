"""Every spelling of "monthly", and which of them this platform can act on.

Item 8 is the `1M` collision: monthly is spelled several ways, this system has no monthly timeframe
in `VALID_TIMEFRAMES`, and one of those spellings means *minutes* here. The original guard caught a
single family (`\\d+M`) and let the other two through to fail much later, at execution time, with a
generic "unrecognised label" message.

The split that matters is not spelling, it is whether the spelling is **ambiguous**:

  * `1M`/`5M`/`60M`  monthly on TradingView and web platforms. Unambiguous - refuse.
  * `MN1`/`mn1`      MetaTrader's monthly convention, and the only spelling that is never minutes.
                     Unambiguous - refuse.
  * `1mo`/`12mo`     pandas/TV "mo". Unambiguous - refuse.
  * `M1`/`m1`        **monthly on TradingView, 1-minute on MetaTrader.** A real coin-flip. Today it
#                     resolves to 60 seconds - the MetaTrader reading, chosen silently.
#                     Refusing it blocks an operator who means one minute; allowing it
#                     keeps a monthly typo tradingable at 1/100th the intended size.
                     blocks an operator who means one minute; allowing it keeps a monthly typo
                     tradingable at 1/100th the intended size. That is a trading decision, so it is
                     pinned here as OPEN rather than resolved.

These tests also pin the spellings that must keep working. The `\\d+M` branch is case-SENSITIVE on
purpose: with an IGNORECASE flag it would match `1m` and refuse every minute deployment on the
platform, which is the worst available outcome for a guard meant to help.
"""

from __future__ import annotations

import pytest

from live_runner.signal_sources.execution_timing import _timeframe_seconds
from shared.models import _reject_month_timeframe_collision as guard

UNAMBIGUOUS_MONTHLY = ["1M", "5M", "60M", "MN1", "mn1", "MN12", "1mo", "1MO", "12mo"]

# Everything that must still load. `2h`/`3m` are here deliberately: the resolver accepts arbitrary
# `<count><unit>`, so a curated allowlist would have been the wrong guard and would have narrowed
# configs that work today.
MUST_STILL_LOAD = [
    "1m", "5m", "15m", "30m", "1h", "2h", "4h", "3m", "1d", "1w", "tick", "s1",
]


@pytest.mark.parametrize("spelling", UNAMBIGUOUS_MONTHLY)
def test_unambiguous_monthly_spellings_are_refused(spelling: str) -> None:
    with pytest.raises(ValueError, match="monthly candle"):
        guard(spelling)


@pytest.mark.parametrize("spelling", MUST_STILL_LOAD)
def test_no_legitimate_timeframe_is_collateral_damage(spelling: str) -> None:
    """The failure mode that matters: an IGNORECASE flag here would refuse every `1m`."""
    assert guard(spelling) == spelling


@pytest.mark.parametrize("spelling", ["M1", "m1"])
def test_the_m1_coin_flip_is_still_open_and_still_resolving_to_one_minute(spelling: str) -> None:
    """Pinned as UNDECIDED, deliberately.

#    If this test is ever changed to expect a refusal, that is the operator's decision
#    landing - not a cleanup, and it should arrive with a note in notes/backlog.md
#    saying which reading won.
    cleanup, and it should come with a note in notes/backlog.md saying which reading won.
    """
    assert guard(spelling) == spelling, (
        "M1/m1 is monthly on TradingView and 1-minute on MetaTrader; refusing it is a trading "
        "decision and must not happen as a side effect"
    )
    assert _timeframe_seconds(spelling) == 60, (
        "this test also documents that the resolver currently picks the MetaTrader reading silently"
    )


@pytest.mark.parametrize("spelling", ["1mo", "mn1"])
def test_the_newly_refused_spellings_were_caught_late_at_execution_time(
    spelling: str,
) -> None:
    """Why widening the guard mattered: these resolved to nothing, so they failed late.

    `_timeframe_seconds` returns None, so the deployment loaded, persisted, and only refused when a
    signal arrived via `_require_recognised_timeframe`. Fail-closed, but late.
    """
    assert _timeframe_seconds(spelling) is None


def test_the_error_names_the_family_specific_alternative() -> None:
    """`candidate[:-1]` is right for `1M` and wrong for `MN1` (it yields the string "MN")."""
    with pytest.raises(ValueError) as one_m:
        guard("1M")
    with pytest.raises(ValueError) as mn1:
        guard("MN1")
    assert "1m" in str(one_m.value), "1M should be told to write 1m"
    assert "1d or 1w" in str(mn1.value), (
        "MN1 has no minutes reading to offer, so it must be pointed at 1d/1w"
    )
    assert "MN" not in str(mn1.value).split("Write")[0].replace("MN1", ""), (
        "the suggestion must not be derived by chopping characters off the input"
    )
