"""Acceptance tests for #123: Trade.direction must be LONG or SHORT, never guessed.

A direction outside VALID_DIRECTIONS used to fall through ``direction_sign``
as -1, silently flipping the PnL sign of every consumer. The contract is now
strict: no case normalisation, invalid values raise at construction and at
deserialisation, and a trade without exits has no exit time.
"""

from __future__ import annotations

import pytest

from shared.models import VALID_DIRECTIONS, Trade, TradeExit


def _trade(direction: str, *, exits: list[TradeExit] | None = None) -> Trade:
    return Trade(
        id=1,
        symbol="AAPL",
        direction=direction,
        entry_time="2024-01-02T14:30:00Z",
        entry_price=100.0,
        quantity=10.0,
        exits=(
            exits
            if exits is not None
            else [TradeExit(time="2024-01-02T15:30:00Z", price=101.0, quantity=10.0)]
        ),
        pnl=10.0,
    )


def _trade_dict(direction: object) -> dict:
    return {
        "id": 1,
        "symbol": "AAPL",
        "direction": direction,
        "entry_time": "2024-01-02T14:30:00Z",
        "entry_price": 100.0,
        "quantity": 10.0,
        "exits": [{"time": "2024-01-02T15:30:00Z", "price": 101.0, "quantity": 10.0}],
        "pnl": 10.0,
    }


def test_valid_directions_constant_is_long_and_short() -> None:
    assert VALID_DIRECTIONS == ("LONG", "SHORT")


@pytest.mark.parametrize(("direction", "sign"), [("LONG", 1), ("SHORT", -1)])
def test_direction_sign_for_valid_directions(direction: str, sign: int) -> None:
    assert _trade(direction).direction_sign == sign


@pytest.mark.parametrize("direction", ["long", "short", "Long", "", "BUY", "SELL", "LONGG", " LONG"])
def test_constructing_trade_with_invalid_direction_raises(direction: str) -> None:
    with pytest.raises(ValueError, match="direction"):
        _trade(direction)


@pytest.mark.parametrize("direction", ["long", "", "BUY", None, 1])
def test_from_dict_with_invalid_direction_raises(direction: object) -> None:
    with pytest.raises(ValueError, match="direction"):
        Trade.from_dict(_trade_dict(direction))


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_from_dict_round_trip_keeps_valid_direction(direction: str) -> None:
    trade = Trade.from_dict(_trade_dict(direction))
    assert trade.direction == direction
    assert Trade.from_dict(trade.to_dict()).direction == direction


def test_direction_sign_never_defaults_to_short_after_mutation() -> None:
    """Even a field mutated after construction must not be read as SHORT."""
    trade = _trade("LONG")
    trade.direction = "long"
    with pytest.raises(ValueError, match="direction"):
        _ = trade.direction_sign


def test_last_exit_time_is_the_final_exit() -> None:
    trade = _trade(
        "LONG",
        exits=[
            TradeExit(time="2024-01-02T15:00:00Z", price=101.0, quantity=5.0),
            TradeExit(time="2024-01-02T16:00:00Z", price=102.0, quantity=5.0),
        ],
    )
    assert trade.last_exit_time == "2024-01-02T16:00:00Z"


def test_trade_without_exits_has_no_exit_time() -> None:
    """An open/malformed trade must not look like a zero-duration closed trade."""
    trade = _trade("LONG", exits=[])
    with pytest.raises(ValueError, match="exit"):
        _ = trade.last_exit_time
