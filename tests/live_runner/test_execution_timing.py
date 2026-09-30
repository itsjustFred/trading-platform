

def test_an_unrecognised_live_timeframe_is_refused_rather_than_silently_skipped():
    """A broker-style label must not quietly delete the candle-close wait.

    `wait_for_candle_close` returned immediately when it could not size the
    timeframe. "Returned immediately" means *no wait* - so an operator who asked
    for candle-close entries on `M15` (a MetaTrader-style label) got immediate
    market orders mid-candle, at a different price than the strategy was validated
    against, with nothing logged and nothing refused. `signal_is_fresh` has the same
    shape but is left alone: it is a secondary gate in front of
    `_validate_signal_age_before_wait`, which raises and has a documented
    `max(300.0, 2 * budget)` fallback. Here there is no compensating gate, so
    skipping is silent rather than redundant.

    The refusal is a raise rather than a return, because returning *is* the defect.

    `tick` is the case that must keep working: it is a recognised label with no
    meaningful budget, so "no wait" is the correct answer there.
    """
    import asyncio

    import pytest

    from live_runner.signal_sources.execution_timing import (
        _timeframe_seconds,
        wait_for_candle_close,
    )
    from risk import TradeSignal

    def _signal(tf: str) -> TradeSignal:
        return TradeSignal(
            action="buy",
            symbol="EURUSD",
            entry_price=1.1,
            stop_loss=None,
            take_profit=None,
            timeframe=tf,
            metadata={},
        )

    # `H1` is deliberately absent: `normalize_timeframe` canonicalises it to `1h`,
    # so it is a *valid* timeframe in broker notation. Listing it here made the test
    # wait out a real hour, which is what the 180s timeout on the first run was.
    # `""` is absent too, and for a better reason: `TradeSignal` refuses an empty
    # timeframe at construction ("timeframe is required"), so it can never reach
    # the wait at all. It is already refused - just upstream of here.
    for bad in ("M15", "D", "5min", "m5", "daily", "1x", "15"):
        with pytest.raises(ValueError, match="unrecognised live timeframe"):
            asyncio.run(wait_for_candle_close(_signal(bad), bad))

    # `tick` resolves to no budget but is a real timeframe, so it must still pass.
    asyncio.run(wait_for_candle_close(_signal("tick"), "tick"))

    # A normal timeframe must not have become stricter either. Checked through the
    # budget lookup rather than by calling the coroutine: `wait_for_candle_close`
    # genuinely sleeps until the next close, so invoking it for "15m" here hung the
    # test for the remainder of the candle (a 180s pytest-timeout, hit on the first
    # run). The refusal path returns before any sleeping, which is why the
    # unrecognised cases above are safe to call directly.
    for good in ("15m", "1h", "1d", "s1", "H1"):  # H1 canonicalises to 1h
        assert _timeframe_seconds(good) is not None, (
            f"{good} is a valid timeframe and must still resolve to a budget"
        )
    for bad in ("M15", "D", "5min", "m5", "daily", "", "1x", "15"):
        assert _timeframe_seconds(bad) is None, (
            f"{bad!r} unexpectedly resolved to a budget, so the refusal above is "
            f"not the only thing distinguishing it from a valid timeframe"
        )


def test_signal_is_fresh_stays_permissive_for_unbudgeted_timeframes():
    """The companion that was deliberately NOT changed, so the choice is visible.

    An audit proposed making this fail closed too. It is left alone on purpose: it
    sits in front of `_validate_signal_age_before_wait`, which raises for a missing,
    invalid or stale stamp and falls back to `max(300.0, 2 * budget)` for a
    timeframe it cannot size. Adding a second, weaker refusal in front of a
    stronger one would change a documented decision for no safety gain - and this
    is exactly the kind of "defect" that is a design choice with a compensating
    control, which is why it is pinned here rather than left implicit.
    """
    from live_runner.signal_sources.execution_timing import signal_is_fresh
    from risk import TradeSignal

    unstamped = TradeSignal(
        action="buy",
        symbol="EURUSD",
        entry_price=1.1,
        stop_loss=None,
        take_profit=None,
        timeframe="15m",
        metadata={},
    )

    # A budgeted timeframe refuses an unstamped signal - the fail-closed case.
    assert signal_is_fresh(unstamped, "15m") is False
    # An unbudgeted one defers to the primary gate, as documented.
    assert signal_is_fresh(unstamped, "tick") is True
