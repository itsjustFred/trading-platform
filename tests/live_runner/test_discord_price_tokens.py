"""Price-token details found while implementing #121."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from live_runner.signal_sources import discord_source as ds


def _parse(content: str):
    message = SimpleNamespace(
        id="900000000000000123",
        content=content,
        channel=SimpleNamespace(id="100000000000000121"),
        author=SimpleNamespace(id="bot-121", bot=True),
        created_at=datetime.now(timezone.utc),
    )
    return ds._parse_trade_signal_from_message(message, default_timeframe="1h")


def test_a_comma_after_the_price_is_a_separator_not_a_thousands_group() -> None:
    """`\\d[\\d,]*` swallowed the comma in "@ 100," and the strict thousands check
    then dropped a well-formed signal."""
    assert ds._extract_price("BUY AAPL @ 100, now") == pytest.approx(100.0)
    assert ds._extract_price("Price: $1,234.56, RSI 29") == pytest.approx(1234.56)
    # An internal comma is still validated, never shortened to a smaller price.
    assert ds._extract_price("Price: $12,34") is None


def test_a_clock_time_after_at_is_not_a_price() -> None:
    assert ds._extract_price("BUY AAPL at 10:30 @ 190.5") == pytest.approx(190.5)
    assert ds._extract_price("BUY AAPL at 9am @ 190.5") == pytest.approx(190.5)


def test_a_percentage_level_is_not_a_price_level() -> None:
    levels = ds._extract_trade_levels("BUY AAPL @ 100 stop 5%")
    assert levels is not None
    assert levels.stop_loss is None
    assert levels.entry_price == pytest.approx(100.0)


@pytest.mark.parametrize(
    "content",
    [
        "BUY AAPL @ 100 TP 90",
        "BUY AAPL @ 100 SL 105",
        "SELL AAPL @ 100 TP 110",
        "SELL AAPL @ 100 SL 95",
    ],
)
def test_levels_on_the_wrong_side_of_the_entry_are_rejected(content: str) -> None:
    assert _parse(content) is None


def test_levels_on_the_right_side_are_kept_for_a_sell() -> None:
    signal = _parse("SELL AAPL @ 100 TP 90 SL 105")
    assert signal is not None
    assert signal.action == "sell"
    assert signal.take_profit == pytest.approx(90.0)
    assert signal.stop_loss == pytest.approx(105.0)


def test_a_bare_english_word_is_not_an_exit_ticker() -> None:
    """"close it out" must not close Gartner (IT) or ON Semiconductor (ON)."""
    assert _parse("CLOSE IT OUT @ 100") is None
    signal = _parse("Close $ON @ 10")
    assert signal is not None
    assert signal.symbol == "ON"
    assert signal.metadata["signal_intent"] == "exit"


def test_prose_close_is_not_an_exit_directive() -> None:
    signal = _parse("BUY AAPL @ 100, strong into the close")
    assert signal is not None
    assert signal.action == "buy"
    assert "signal_intent" not in signal.metadata


@pytest.mark.parametrize(
    "content",
    [
        "BUY AAPL, RSI now at 28, $190.50",
        "BUY AAPL, RSI is at 28, $190.50",
        "BUY AAPL, RSI is currently at 28, $190.50",
        "BUY AAPL, VIX still sitting at 18, $190.50",
        "BUY AAPL, RSI(14) just at 28, $190.50",
    ],
)
def test_an_indicator_reading_with_filler_words_is_not_the_entry_price(content: str) -> None:
    """ "RSI now at 28" is still an oscillator reading: sizing off 28 for a
    190.50 stock would be a 7x-wrong entry (CodeRabbit finding on #142)."""
    assert ds._extract_price(content) == pytest.approx(190.50)
    signal = _parse(content)
    assert signal is not None
    assert signal.entry_price == pytest.approx(190.50)


@pytest.mark.parametrize(
    ("content", "price"),
    [
        # The indicator is mentioned, but the words before "at" are the order
        # itself, not filler: the price after "at" is the real entry.
        ("RSI oversold: BUY AAPL at 190.50", 190.50),
        ("RSI BUY AAPL at 190.50", 190.50),
        ("VIX is high, now BUY AAPL at 190.50", 190.50),
    ],
)
def test_an_indicator_mention_does_not_hide_the_real_at_price(content: str, price: float) -> None:
    assert ds._extract_price(content) == pytest.approx(price)
