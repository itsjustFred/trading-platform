"""``shared.money`` — the single strict parser for broker money fields (#280).

The live path used to copy a ``_to_float(..., default=0.0)`` coercion that
turned an unreadable quantity, price or balance into ``0.0`` — reading as
"flat" or "no stop". ``parse_money`` raises on anything unreadable;
``parse_optional`` tolerates only absence (``None``), never garbage.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from shared.money import MoneyParseError, parse_money, parse_optional


def test_parse_failures_are_money_parse_errors() -> None:
    # MoneyParseError is a ValueError (every existing handler keeps working)
    # but distinct, so reconcile_executions can skip parse failures without
    # also swallowing programming errors or database ValueErrors.
    with pytest.raises(MoneyParseError):
        parse_money("junk")
    assert issubclass(MoneyParseError, ValueError)


@pytest.mark.parametrize("value", [0, 0.0, -1.5, "12.5", " 3 ", Decimal("2.5")])
def test_parse_money_reads_valid_values(value) -> None:
    assert parse_money(value) == pytest.approx(float(value))


@pytest.mark.parametrize(
    "value",
    [None, "junk", "", "nan", float("nan"), float("inf"), -float("inf"), True, False, object()],
)
def test_parse_money_raises_on_unreadable_values(value) -> None:
    with pytest.raises(ValueError):
        parse_money(value)


def test_parse_optional_returns_none_only_for_absence() -> None:
    assert parse_optional(None) is None
    assert parse_optional(1.5) == 1.5
    assert parse_optional("2.5") == 2.5


@pytest.mark.parametrize("value", ["junk", float("nan"), True])
def test_parse_optional_still_raises_on_garbage(value) -> None:
    """"The broker sent something" must never degrade to "the broker sent nothing"."""
    with pytest.raises(ValueError):
        parse_optional(value)
