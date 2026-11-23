"""Market-data bar payloads fail closed on unreadable money fields (#280).

A bar whose open/high/low/close/volume cannot be read must never be stored as
zeros: a strategy reading a 0.0 close trades on a price the broker never sent.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from live_runner.market_data_stream import (
    LiveMarketDataStreamService,
    _serialize_bar_payload,
)


def _bar(**overrides):
    bar = {
        "symbol": "AAPL",
        "timestamp": datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1000,
    }
    bar.update(overrides)
    return SimpleNamespace(**bar)


def test_serialize_bar_payload_reads_valid_money_fields() -> None:
    payload = _serialize_bar_payload(_bar())

    assert payload["open"] == 100.0
    assert payload["high"] == 101.0
    assert payload["low"] == 99.0
    assert payload["close"] == 100.5
    assert payload["volume"] == 1000.0


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_serialize_bar_payload_raises_on_an_unreadable_money_field(field: str) -> None:
    with pytest.raises(ValueError, match="unreadable money value"):
        _serialize_bar_payload(_bar(**{field: "junk"}))


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_on_bar_drops_a_bar_with_an_unreadable_money_field(field: str) -> None:
    service = LiveMarketDataStreamService()

    asyncio.run(service._on_bar(_bar(**{field: "junk"})))

    assert service._bars == {} or all(not queue for queue in service._bars.values())


def test_on_bar_stores_a_readable_bar() -> None:
    service = LiveMarketDataStreamService()

    asyncio.run(service._on_bar(_bar()))

    payload = asyncio.run(service.get_quote_payload("AAPL"))
    assert payload["latest"] is not None
    assert payload["latest"]["close"] == 100.5
