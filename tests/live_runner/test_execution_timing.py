import sys

import pytest


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


class TestSignalTimestampParsingAndTimeframeRecognition:
    """The two functions the freshness and candle-wait gates both stand on.

    `_require_recognised_timeframe` already had coverage - accepting a broker-style label was
    killed on the first attempt - so this class is mostly about `_parse_signal_timestamp`, which
    had **none**. Six mutations survived.

    **A naive timestamp is labelled UTC, and that is an assumption, not a conversion.** Both
    the `datetime` branch and the string branch attach `timezone.utc` to a stamp that carries
    no offset. So a broker sending local-time bars has every timestamp read as UTC, and the
    resulting `isoformat()` then *claims* UTC - meaning nothing downstream can notice.

    That is why the assertions are on the serialised result rather than on the parse: a test
    asserting only "it parsed" would pass with the labelling removed on any machine, since
    the instant is identical either way. The difference is only visible in the output's
    offset.

    **A `Z` suffix is converted explicitly**, which is redundant on Python 3.11+ and required
    before it - so it is asserted on the result rather than on the mechanism, and the reason is
    recorded in the test so nobody "simplifies" it away.

    **A non-string, non-datetime value returns `None` rather than passing through.** `None` is
    the *load-bearing* value here: `signal_is_fresh` treats it as stale and refuses to trade. A
    truthy non-timestamp slipping through would have to survive a subtraction downstream to
    cause harm, and a truthy one raises instead - so the filter is what makes the refusal
    total rather than accidental. Asserted on a bool as well, because `True` is the input most
    likely to arrive from a JSON config that meant a flag.

    **And the root cause of the `1M` collision, pinned at its actual source.** `_require_recognised_timeframe`
    validates the *normalised* label, and `shared.constants.normalize_timeframe` lowercases
    before an alias lookup. `TIMEFRAME_ALIASES` holds only `m1`, `h1` and `d1` - there is no
    monthly alias - so `1M` becomes `1m` purely through the lowercasing and then matches a valid
    timeframe. The consequence is the same 60-second budget already recorded from the daemon
    side, so the note is cross-referenced rather than duplicated.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "2026-07-15T10:00:00Z",
            "2026-07-15T10:00:00+00:00",
            "  2026-07-15T10:00:00Z  ",
        ],
    )
    def test_a_string_stamp_parses_to_an_utc_datetime(self, text: str) -> None:
        """Padding is normal in config, and `Z` is what every broker sends.

        Only UTC-equivalent spellings here. A `+02:00` stamp is a different claim - that the
        offset is *preserved* rather than normalised away - and it is asserted separately, so
        a failure in one does not look like a failure in the other.
        """
        from live_runner.signal_sources.execution_timing import _parse_signal_timestamp

        parsed = _parse_signal_timestamp(text)
        assert parsed is not None, f"{text!r} did not parse"
        assert parsed.tzinfo is not None, f"{text!r} parsed to a naive datetime"
        assert parsed.utcoffset().total_seconds() == 0, parsed.utcoffset()

    def test_a_non_utc_string_stamp_keeps_its_offset(self) -> None:
        """The string branch preserves an explicit offset rather than rewriting it to UTC.

        `"2026-07-15T12:00:00+02:00"` is the same instant as `10:00Z`, and both are valid
        inputs. Asserted on the offset *and* the wall clock, because a normalisation that
        shifted the clock while fixing the offset would satisfy only one of the two.
        """
        from live_runner.signal_sources.execution_timing import _parse_signal_timestamp

        parsed = _parse_signal_timestamp("2026-07-15T12:00:00+02:00")
        assert parsed is not None
        assert parsed.utcoffset().total_seconds() == 7200, parsed.utcoffset()
        assert parsed.hour == 12, f"the wall clock was rewritten: {parsed.isoformat()}"

    def test_the_two_guards_around_the_parse_are_verified_equivalent_mutants(self) -> None:
        """**Two survivors, equivalent for two different reasons.** Recorded, not "fixed".

        **The `Z` conversion is redundant on this interpreter.** `datetime.fromisoformat` only
        learned to accept `Z` in Python 3.11, so on the 3.12 running here
        `fromisoformat("2026-07-15T10:00:00Z")` already returns an aware datetime and the
        explicit `.replace("Z", "+00:00")` changes nothing. On 3.10 or earlier the same call
        **raises** - and `Z` is the form every broker sends, so deleting the guard would pass
        CI today and fail on an older runtime.

        This is the same `Z`-on-3.11 finding as the market-data bar serialiser in
        `tests/test_market_data_stream_symbols.py`, in a second module. That is worth
        recording as a pattern rather than two coincidences: **`Z`-suffix handling is
        load-bearing by version, and this repo runs two copies of that conversion.**

        **The blank-string guard is redundant with the `except`.** `fromisoformat("")` raises
        `ValueError`, which the surrounding `except ValueError` already turns into `None`, so
        removing the explicit `if not text: return None` changes nothing.

        Neither is deleted. The first is portability, and the second is a cheap, clear
        short-circuit in front of a parse attempt. What is pinned is the *behaviour* both
        guards produce - blank in, `None` out, and `Z` in, aware datetime out - so a future
        edit that breaks either shows up as a failing test rather than as a difference nobody
        can see.
        """
        from datetime import datetime, timezone

        from live_runner.signal_sources.execution_timing import _parse_signal_timestamp

        # The behaviour both guards exist to produce.
        assert _parse_signal_timestamp("") is None
        assert _parse_signal_timestamp("   ") is None
        assert _parse_signal_timestamp("2026-07-15T10:00:00Z") == datetime(
            2026, 7, 15, 10, 0, tzinfo=timezone.utc
        )

        # And the premise of the `Z` note, asserted so it fails if the interpreter changes
        # in a way that would make the equivalence a false claim.
        if sys.version_info >= (3, 11):
            assert datetime.fromisoformat("2026-07-15T10:00:00Z") == datetime(
                2026, 7, 15, 10, 0, tzinfo=timezone.utc
            ), (
                "this interpreter no longer parses Z natively, so the Z conversion in "
                "_parse_signal_timestamp is no longer redundant - re-check this note"
            )
        else:  # pragma: no cover - documents the pre-3.11 behaviour
            with pytest.raises(ValueError):
                datetime.fromisoformat("2026-07-15T10:00:00Z")

    def test_a_naive_datetime_is_labelled_utc_rather_than_left_ambiguous(self) -> None:
        """**The assumption, asserted on the output's offset and not on the instant.**

        The instant is the same either way, so an assertion on the value alone would pass with
        the labelling removed. The offset is the only place the assumption is visible - and
        once written down, the `isoformat()` will *claim* UTC regardless, so a broker sending
        local time is undetectable from here.
        """
        from datetime import datetime

        from live_runner.signal_sources.execution_timing import _parse_signal_timestamp

        naive = datetime(2026, 7, 15, 10, 0, 0)
        parsed = _parse_signal_timestamp(naive)
        assert parsed is not None
        assert parsed.tzinfo is not None, "a naive datetime was returned naive"
        assert parsed.utcoffset().total_seconds() == 0

    def test_a_naive_iso_string_is_labelled_utc_too(self) -> None:
        """The string branch, which is a separate statement from the datetime branch.

        `"2026-07-15T10:00:00"` carries no offset at all, so labelling it is the same
        assumption arriving by a different route - and it is the route an ISO string from a
        broker actually takes.
        """
        from live_runner.signal_sources.execution_timing import _parse_signal_timestamp

        parsed = _parse_signal_timestamp("2026-07-15T10:00:00")
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.isoformat() == "2026-07-15T10:00:00+00:00", parsed.isoformat()

    def test_an_aware_datetime_keeps_its_own_offset(self) -> None:
        """A stamp that knows its offset must not be overwritten with UTC.

        A non-UTC offset is preserved rather than replaced, so the instant is unchanged - which
        is what "assume UTC only when told nothing" means.
        """
        from datetime import datetime, timedelta, timezone

        from live_runner.signal_sources.execution_timing import _parse_signal_timestamp

        plus_two = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        parsed = _parse_signal_timestamp(plus_two)
        assert parsed is not None
        assert parsed.utcoffset() == timedelta(hours=2)
        assert parsed.hour == 12, "the wall clock was rewritten, not just the offset"

    @pytest.mark.parametrize(
        "value", [None, "", "   ", "not-a-time", 12345, 0, True, False, [], {}, object()]
    )
    def test_anything_unparseable_returns_none(self, value) -> None:
        """`None` is the load-bearing answer, not a fallback.

        `signal_is_fresh` treats `None` as stale and **refuses to trade**, and a non-timestamp
        value that passed through would have to survive a subtraction downstream to cause harm -
        and would raise instead. So the filter is what makes the refusal total rather than
        accidental. `True` is included because it is the value most likely to arrive from a
        JSON config that meant a flag.
        """
        from live_runner.signal_sources.execution_timing import _parse_signal_timestamp

        assert _parse_signal_timestamp(value) is None, (
            f"{value!r} did not return None, so a signal carrying it would not be treated "
            "as unstamped"
        )

    def test_an_unparseable_stamp_makes_a_signal_stale_rather_than_traded(self) -> None:
        """The end-to-end claim, because the `None` above is only worth anything if used.

        Asserted through `signal_is_fresh` rather than only through the parser, so the contract
        is "an unstamped signal does not trade" and not merely "the parser returns None" - the
        same distinction that caught the vacuous assertion earlier in this sweep.
        """
        from datetime import datetime, timedelta, timezone

        from risk.models import TradeSignal
        from live_runner.signal_sources.execution_timing import signal_is_fresh

        now = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
        fresh_stamp = {"received_at": (now - timedelta(seconds=10)).isoformat()}
        for metadata, label in (
            ({}, "no stamp"),
            ({"received_at": "garbage"}, "garbage stamp"),
            ({"received_at": None}, "explicit null"),
            ({"received_at": True}, "a bool"),
        ):
            signal = TradeSignal(
                action="buy", symbol="EURUSD", entry_price=100.0, stop_loss=98.0,
                take_profit=104.0, timeframe="1m", metadata=metadata,
            )
            assert signal_is_fresh(signal, "1m", now=now) is False, (
                f"a signal with {label} was treated as fresh and would trade"
            )
        # And a genuinely recent one is fresh, so the refusals are not always-firing.
        signal = TradeSignal(
            action="buy", symbol="EURUSD", entry_price=100.0, stop_loss=98.0,
            take_profit=104.0, timeframe="1m", metadata=fresh_stamp,
        )
        assert signal_is_fresh(signal, "1m", now=now) is True

    @pytest.mark.parametrize("timeframe", ["m1", "h1", "d1", "M1", "H1", " 1H ", "5m", "1w"])
    def test_a_recognised_timeframe_is_accepted_through_its_alias(self, timeframe: str) -> None:
        """Aliases and casing resolve, because a broker-style label is normal input.

        `M1`/`H1` are accepted via `TIMEFRAME_ALIASES`; the padding and casing cases go through
        `normalize_timeframe`'s own strip-and-lower.

        no-raise: test_an_unrecognised_timeframe_is_refused_with_a_usable_message
        """
        from live_runner.signal_sources.execution_timing import _require_recognised_timeframe

        _require_recognised_timeframe(timeframe, "test")

    def test_tick_is_a_recognised_timeframe_even_though_it_has_no_budget(self) -> None:
        """`tick` resolves to *no budget*, which is a different answer from *no such timeframe*.

        The docstring draws exactly this line, and collapsing the two is what the guard exists
        to prevent - so both halves are asserted: `tick` passes the check, and its budget is
        separately `None`.
        """
        from live_runner.signal_sources.execution_timing import (
            _require_recognised_timeframe,
            _timeframe_seconds,
        )

        _require_recognised_timeframe("tick", "test")
        assert _timeframe_seconds("tick") is None

    @pytest.mark.parametrize("timeframe", ["banana", "M15", "D", "", "   ", None, "15"])
    def test_an_unrecognised_timeframe_is_refused_with_a_usable_message(self, timeframe) -> None:
        """Refused, and the message names the caller, the label and what was expected.

        A `D` label is a real broker spelling that this repo does not define, so it belongs
        here rather than being quietly mapped to `1d` - a mapping that would change what a
        strategy validated against.
        """
        from live_runner.signal_sources.execution_timing import _require_recognised_timeframe

        with pytest.raises(ValueError) as exc:
            _require_recognised_timeframe(timeframe, "wait_for_candle_close")
        message = str(exc.value)
        assert "wait_for_candle_close" in message, message
        assert "unrecognised live timeframe" in message, message
        assert "1m" in message, "the message should list what was expected"

    def test_the_monthly_spelling_collides_because_there_is_no_monthly_alias(self) -> None:
        """**The root cause, pinned where it actually lives.**

        `1M` is month on most platforms. Here it lowercases to `1m`, which is a valid
        timeframe with a **60-second** budget, so a monthly strategy's signals are refused
        almost immediately.

        The cause is not the daemon's own lowercasing - it is `normalize_timeframe`, which
        lowercases and then consults `TIMEFRAME_ALIASES`. That table holds exactly `m1`, `h1`
        and `d1`: there is no monthly entry, and no `1M` entry, so the lowercased form passes
        straight through as already-valid.

        Cross-referenced with the daemon-side note, which recorded the same 60-second outcome
        from the other end. Recorded here because *this* is the place a fix would go, and
        because the failure direction is the safe one - refusing valid signals rather than
        trading stale ones - which is why it can sit unnoticed.
        """
        from shared.constants import TIMEFRAME_ALIASES, normalize_timeframe
        from live_runner.signal_sources.execution_timing import (
            _require_recognised_timeframe,
            _timeframe_seconds,
        )

        assert "1m" not in TIMEFRAME_ALIASES, (
            "1m became an alias, so the lowercase of 1M no longer passes through unchanged - "
            "re-check the collision this test documents"
        )
        assert normalize_timeframe("1M") == "1m"
        _require_recognised_timeframe("1M", "test")
        assert _timeframe_seconds("1M") == 60, (
            "the monthly spelling now resolves to something other than a minute; if that was "
            "deliberate, drop the KNOWN annotation from the daemon-side test too"
        )
