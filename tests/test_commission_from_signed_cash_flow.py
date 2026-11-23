"""``base_adapter.commission_from_signed_cash_flow``: the live commission sign convention (#205)."""

from __future__ import annotations

import math

import pytest

from live_runner.base_adapter import commission_from_signed_cash_flow


def test_a_broker_charge_becomes_a_positive_cost() -> None:
    assert commission_from_signed_cash_flow(-2.5) == 2.5


def test_a_broker_rebate_becomes_a_negative_cost() -> None:
    assert commission_from_signed_cash_flow(2.5) == -2.5


@pytest.mark.parametrize("zero", [0, 0.0, -0.0])
def test_zero_stays_a_positive_zero(zero: float) -> None:
    result = commission_from_signed_cash_flow(zero)

    assert result == 0.0
    assert str(result) == "0.0", "a known zero must never be stored as -0.0"


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_non_finite_cash_flow_is_refused_not_stored_as_a_free_fill(bad: float) -> None:
    # 0.0 is a KNOWN zero fee; garbage must not silently become one.
    with pytest.raises(ValueError, match="finite"):
        commission_from_signed_cash_flow(bad)
