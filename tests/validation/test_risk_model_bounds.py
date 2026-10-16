"""The validation layer's own risk bounds: a validator that accepts nonsense certifies nonsense.

    Why this file exists

    2026-10-15. `validation/` is the independent checker - the thing whose job is to refuse a backtest
    whose risk model cannot be trusted. Mutation:

        M20  drop `_validate_optional_fraction(self.risk_percent, ...)` from RiskModelConfig and
             TradeRiskProfile                                                 SURVIVED - 4,987 passed

    `_validate_optional_fraction` requires `0 <= value <= 1`. Without it both classes accept any
    number, which is two distinct failures:

    * **Above 1** - `risk_percent=1.5` means risking 150% of the account's equity on one trade. The
      validator is the component that is supposed to notice that.
    * **NaN / inf** - `_validate_optional_fraction` rejects non-finite values, and the reason matters
      more than it looks. Every downstream comparison against a NaN is False, so a NaN risk figure does
      not raise; it makes the checks that would have caught the risk *silently pass*. A validator
      poisoned into always agreeing is worse than a validator that is obviously absent, because the
      absence is visible on the page.

    These tests are about the models refusing bad input. They are deliberately not about the reports:
    the report tests already cover what a delta looks like, and none of them could have detected a
    NaN reaching the comparison, because a NaN does not produce a delta to display.
"""

from __future__ import annotations

import pytest

from validation.risk.models import RiskModelConfig, TradeRiskProfile


def _profile(**overrides) -> TradeRiskProfile:
    fields = {
        "trade_id": 1,
        "direction": "LONG",
        "entry_price": 100.0,
        "stop_loss": 99.0,
        "take_profit": 102.0,
        "position_size": 1.0,
        "risk_amount": 100.0,
        "risk_percent": 0.01,
        "stop_distance": 1.0,
        "risk_reward_ratio": 2.0,
    }
    fields.update(overrides)
    return TradeRiskProfile(**fields)


# --- the boundary itself -------------------------------------------------------


def test_the_exact_upper_boundary_is_still_accepted() -> None:
    """Risking exactly 100% of equity is allowed. The bound is inclusive, and this pins that."""
    config = RiskModelConfig(risk_percent=1.0)
    assert config.risk_percent == pytest.approx(1.0)
    profile = _profile(risk_percent=1.0)
    assert profile.risk_percent == pytest.approx(1.0)
    # Zero is the other end of the inclusive bound, and it is a legitimate "no risk" figure.
    assert RiskModelConfig(risk_percent=0.0).risk_percent == pytest.approx(0.0)


# --- above the boundary: M20 ---------------------------------------------------


@pytest.mark.parametrize("bad", [1.000001, 1.5, 2.0, 100.0])
def test_risk_above_one_is_refused_by_the_risk_config(bad: float) -> None:
    with pytest.raises(ValueError, match="risk_percent must be finite and between 0 and 1"):
        RiskModelConfig(risk_percent=bad)


@pytest.mark.parametrize("bad", [1.000001, 1.5, 2.0, 100.0])
def test_risk_above_one_is_refused_by_the_trade_profile(bad: float) -> None:
    with pytest.raises(ValueError, match="risk_percent must be finite and between 0 and 1"):
        _profile(risk_percent=bad)


# --- non-finite: the failure mode that does not raise on its own ----------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_risk_percent_is_refused(bad: float) -> None:
    """A NaN would not raise downstream; it would make every check that reads it agree."""
    with pytest.raises(ValueError, match="risk_percent must be finite and between 0 and 1"):
        RiskModelConfig(risk_percent=bad)
    with pytest.raises(ValueError, match="risk_percent must be finite and between 0 and 1"):
        _profile(risk_percent=bad)


def test_absent_risk_percent_is_allowed_where_the_field_is_optional() -> None:
    """The bound applies to a value that is present. Absent is a different state, not a violation.

    `RiskModelConfig.risk_percent` is optional; `TradeRiskProfile.risk_percent` is required, so a
    profile cannot be built without one - asserted below rather than left implicit.
    """
    assert RiskModelConfig(risk_percent=None).risk_percent is None
    with pytest.raises(Exception):
        _profile(risk_percent=None)
