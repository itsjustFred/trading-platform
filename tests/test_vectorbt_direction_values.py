"""#123: the vectorbt adapter must not guess a trade side from an unknown value."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from runner.engines.vectorbt_adapter import _normalize_direction_value, _resolve_direction


@pytest.mark.parametrize("value", ["LONG", "long", 1, 1.0])
def test_long_spellings(value) -> None:
    assert _normalize_direction_value(value) == "LONG"


@pytest.mark.parametrize("value", ["SHORT", "short", -1, -1.0])
def test_short_spellings(value) -> None:
    assert _normalize_direction_value(value) == "SHORT"


@pytest.mark.parametrize("value", [math.nan, None, 0, "", "BUY", "SELL", "Long ", True, False])
def test_unknown_values_raise_instead_of_defaulting_to_long(value) -> None:
    with pytest.raises(ValueError, match="direction"):
        _normalize_direction_value(value)


def test_missing_direction_at_entry_bar_raises() -> None:
    signals = pd.DataFrame({"direction": pd.Series(["LONG", None, "SHORT"], dtype="object")})
    assert _resolve_direction(signals, None, 0) == "LONG"
    assert _resolve_direction(signals, None, 2) == "SHORT"
    with pytest.raises(ValueError, match="direction"):
        _resolve_direction(signals, None, 1)


def test_no_direction_information_stays_long_only() -> None:
    """A strategy without any direction column is long-only by contract."""
    assert _resolve_direction(pd.DataFrame({"entry": [True]}), None, 0) == "LONG"
