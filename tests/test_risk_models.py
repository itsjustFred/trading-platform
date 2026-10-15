"""Validation contracts on `risk/models.py` - the live signal and decision objects.

Found by the `platform-pr-verify` mutation pass, not by reading: deleting the
`None` guard in `_optional_finite_positive` and dropping the integer branch from
`_parse_bool` both **survived** the whole suite. Neither helper was named in any
test file.

Both matter because they guard the objects a trade is built from:

- `_optional_finite_positive` validates `TradeSignal.stop_loss` and
  `TradeSignal.take_profit`. A signal with **no stop** is legitimate here - the
  ATR fallback and the stop-less backtest work in this repo both depend on it - so
  the `None` guard is load-bearing, and removing it turns an accepted stop-less
  signal into a raised `ValueError`.
- `_parse_bool` validates `RiskDecision.accepted`, and an integer `1`/`0` reaches
  it from JSON and from numpy scalars. Removing that branch makes a decision built
  with `accepted=1` fail to construct.

The "must be refused" half is covered elsewhere for the validation models; what
was missing here is that these two coercions are *optional* at all.
"""

from __future__ import annotations

import math

import pytest

from risk.models import RiskDecision, SizedOrder, TradeSignal


def _signal(**overrides) -> TradeSignal:
    fields = {
        "action": "buy",
        "symbol": "aapl",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "timeframe": "5Min",
    }
    fields.update(overrides)
    return TradeSignal(**fields)


def _decision(**overrides) -> RiskDecision:
    fields = {
        "signal_id": "sig-1",
        "accepted": True,
        "quantity": 10.0,
        "risk_amount": 50.0,
        "risk_percent": 0.005,
        "risk_distance": 5.0,
        "stop_price_used": 95.0,
        "take_profit_used": 110.0,
    }
    fields.update(overrides)
    return RiskDecision(**fields)


# --------------------------------------------------------------------------
# An absent optional is an absence, not an error
# --------------------------------------------------------------------------


def test_a_signal_without_a_stop_is_accepted() -> None:
    """The stop is optional. Kills deleting the `None` guard."""
    signal = _signal(stop_loss=None)
    assert signal.stop_loss is None


def test_a_signal_without_a_take_profit_is_accepted() -> None:
    signal = _signal(take_profit=None)
    assert signal.take_profit is None


def test_a_signal_with_neither_level_is_accepted() -> None:
    signal = _signal(stop_loss=None, take_profit=None)
    assert signal.stop_loss is None and signal.take_profit is None


def test_a_decision_with_no_prices_used_is_accepted() -> None:
    """A rejected decision has no prices, and constructing one must still work."""
    decision = _decision(
        accepted=False,
        rejection_reason="below minimum size",
        risk_distance=None,
        stop_price_used=None,
        take_profit_used=None,
    )
    assert decision.stop_price_used is None
    assert decision.accepted is False


def test_a_sized_order_may_carry_no_ratio() -> None:
    """`risk_reward_ratio` is optional, and 0 is a real ratio rather than an absence."""
    order = SizedOrder(
        signal=_signal(stop_loss=None),
        position_size=10.0,
        risk_amount=50.0,
        risk_percent=0.005,
        risk_reward_ratio=None,
    )
    assert order.risk_reward_ratio is None
    zero = SizedOrder(
        signal=_signal(),
        position_size=10.0,
        risk_amount=50.0,
        risk_percent=0.005,
        risk_reward_ratio=0.0,
    )
    assert zero.risk_reward_ratio == 0


# --------------------------------------------------------------------------
# ...but a present one is still validated
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1.0, True])
def test_a_present_but_non_positive_stop_is_refused(bad: object) -> None:
    """Optional, yes - but if it is there it must be a positive number."""
    with pytest.raises(ValueError, match="stop_loss must be"):
        _signal(stop_loss=bad)


def test_a_present_but_non_positive_take_profit_is_refused() -> None:
    with pytest.raises(ValueError, match="take_profit must be"):
        _signal(take_profit=0)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_non_finite_stop_is_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="stop_loss must be finite"):
        _signal(stop_loss=bad)


# --------------------------------------------------------------------------
# `accepted` is a boolean, and integers reach it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), (False, False), (1, True), (0, False), ("true", True), ("0", False)],
)
def test_the_accepted_flag_accepts_booleans_and_integers(raw: object, expected: bool) -> None:
    """Kills dropping the integer branch. `1`/`0` arrive from JSON and numpy."""
    assert _decision(accepted=raw).accepted is expected


@pytest.mark.parametrize("bad", ["maybe", 2, -1, None, 1.5])
def test_a_non_boolean_accepted_flag_is_refused(bad: object) -> None:
    with pytest.raises(ValueError, match="accepted must be a boolean"):
        _decision(accepted=bad)


# --------------------------------------------------------------------------
# `quantity` and `risk_amount` are counts, so zero is legal for them
# --------------------------------------------------------------------------


def test_a_zero_quantity_decision_is_accepted() -> None:
    """`quantity` is non-negative, not positive - a rejection carries quantity 0."""
    assert _decision(quantity=0).quantity == 0


@pytest.mark.parametrize("bad", [-1, True, math.nan])
def test_a_negative_or_non_numeric_quantity_is_refused(bad: object) -> None:
    with pytest.raises(ValueError, match="quantity must be"):
        _decision(quantity=bad)


@pytest.mark.parametrize("bad", [1.01, -0.01])
def test_a_risk_percent_outside_zero_to_one_is_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="risk_percent must be between 0 and 1"):
        _decision(risk_percent=bad)


def test_a_boolean_risk_percent_is_refused_as_non_numeric() -> None:
    """`True` is 1.0, which is inside [0, 1] - so the bool guard is the only thing
    that catches it, and it reports the *type* problem rather than the range one."""
    with pytest.raises(ValueError, match="risk_percent must be numeric"):
        _decision(risk_percent=True)


def test_the_fraction_bounds_are_inclusive() -> None:
    """0 and 1 are both valid fractions; the boundary is part of the contract."""
    assert _decision(risk_percent=0).risk_percent == 0
    assert _decision(risk_percent=1).risk_percent == 1


# --- the realized-risk accessors (#84) ----------------------------------------
#
# This file's docstring already records two mutations that survived here. Two more joined them on
# 2026-10-15, found by scanning `risk/` - a package the money-path gate added at L202 did not cover,
# because that gate only looked at `live_runner`.
#
#     M11  realized_risk_amount ignores the realized figure, always returns the policy one   SURVIVED
#     M12  realized_risk_percent returns the policy percent, not realized/balance          SURVIVED
#
# The docstring of `realized_risk_amount` says consumers "must read this, never the policy figure, or
# they overstate risk on every capped fill". These are the accessors that distinction depends on, and
# both are written into live order metadata by `size_live_signal_for_submission` - so a regression here
# reports the wrong risk for every capped fill without any test noticing.
def _order_with_realized(realized: float, policy_amount: float, policy_percent: float) -> SizedOrder:
    return SizedOrder(
        signal=_signal(stop_loss=95.0),
        position_size=0.5,
        risk_amount=policy_amount,
        risk_percent=policy_percent,
        risk_reward_ratio=None,
        decision=_decision(metadata={"realized_risk_amount": realized}),
    )


def test_realized_risk_amount_prefers_the_realized_figure_over_the_policy_one() -> None:
    """The policy figure is 8x the truth on this order. Reading the wrong one overstates risk."""
    from risk.models import realized_risk_amount

    order = _order_with_realized(realized=12.5, policy_amount=100.0, policy_percent=0.01)
    assert realized_risk_amount(order) == pytest.approx(12.5)


def test_realized_risk_amount_falls_back_to_the_policy_figure_when_there_is_no_realized_one() -> None:
    from risk.models import realized_risk_amount

    order = SizedOrder(
        signal=_signal(stop_loss=95.0),
        position_size=0.5,
        risk_amount=100.0,
        risk_percent=0.01,
        risk_reward_ratio=None,
        decision=_decision(metadata={}),
    )
    assert realized_risk_amount(order) == pytest.approx(100.0)


def test_realized_risk_percent_is_realized_over_balance_not_the_policy_percent() -> None:
    """The engine reports no realized percent, so it is derived here - 12.5/1000, not the policy 1%."""
    from risk.models import realized_risk_percent

    order = _order_with_realized(realized=12.5, policy_amount=100.0, policy_percent=0.01)
    assert realized_risk_percent(order, 1_000.0) == pytest.approx(0.0125)
    assert realized_risk_percent(order, 1_000.0) != pytest.approx(0.01)


def test_realized_risk_percent_falls_back_to_policy_on_an_unknown_balance() -> None:
    from risk.models import realized_risk_percent

    order = _order_with_realized(realized=12.5, policy_amount=100.0, policy_percent=0.01)
    assert realized_risk_percent(order, 0.0) == pytest.approx(0.01)
