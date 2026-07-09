from datetime import datetime, timezone
from types import SimpleNamespace

from live_runner.signal_sources.discord_source import _parse_trade_signal_from_message


def _message(content: str, channel_id: str = "123"):
    return SimpleNamespace(
        content=content,
        id="msg-1",
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id="author-1"),
        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )


def test_discord_parser_parses_ramjet_oversold_buy() -> None:
    signal = _parse_trade_signal_from_message(
        _message("**$AAPL** is now oversold — buy signal\nPrice: $190.50"),
        default_timeframe="1h",
    )

    assert signal is not None
    assert signal.symbol == "AAPL"
    assert signal.action == "buy"
    assert signal.entry_price == 190.5
    assert signal.metadata["discord_signal_type"] == "ram_jet_oversold_buy"
    assert signal.metadata["discord_channel_id"] == "123"


def test_discord_parser_parses_rocketfuel_momentum_reversal_sell() -> None:
    signal = _parse_trade_signal_from_message(
        _message("Rocket Fuel momentum reversal for **$TSLA** @ 180.25"),
        default_timeframe="1h",
    )

    assert signal is not None
    assert signal.symbol == "TSLA"
    assert signal.action == "sell"
    assert signal.entry_price == 180.25
    assert signal.metadata["discord_signal_type"] == "momentum_reversal"
