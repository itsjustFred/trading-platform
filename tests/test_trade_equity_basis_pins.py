"""The equity basis a trade was sized from: a per-unit coefficient's denominator.

    Why this file exists

    2026-10-15, from the `runner/` mutation queue. One mutation, against the full suite:

        M46  `_trade_equity_basis` falls back to 1.0 instead of `initial_capital`
             SURVIVED - 5,063 passed, exit 0

    The caller shows why this matters more than a helper's size suggests:

        basis = _trade_equity_basis(trade, initial_capital)
        if proportional and basis > 0:
            unit_coeff = orig_qty / basis
            new_qty = unit_coeff * shar

    `basis` is the denominator of a per-unit coefficient. With a 100k account, a 100-lot original
    position and a target equity ratio of 2.5x, the correct answer is `100 / 100_000 * 2.5 = 2.5`.
    With the fallback of 1.0 it is `100 / 1 * 2.5 = 250` — **a hundred times too large**, produced
    without an error, and in a backtest whose output looks entirely ordinary.

    Note the mutation is also *conservative-looking*: the `if basis > 0` guard still passes for 1.0, so
    the "fail closed" shape is preserved while the value is nonsense. A guard that checks `> 0` does not
    check that a fallback is plausible.

    The other two mutations in this batch were killed by existing tests and needed nothing: `stats.
    _net_trade_pnl` silently returning gross (the #84 rule for the fourth time), and the vectorbt stop
    distance returning the raw stop price instead of `|entry - stop|`.
"""

from __future__ import annotations

import pytest

from runner.engines._shared import _trade_equity_basis
from shared.models import Trade, TradeExit


def _trade(**meta) -> Trade:
    return Trade(
        id=1,
        symbol="EURUSD",
        direction="LONG",
        entry_time="2024-01-01T00:00:00Z",
        entry_price=100.0,
        quantity=100.0,
        exits=[TradeExit(time="2024-01-05T00:00:00Z", price=110.0, quantity=100.0, pnl=1000.0)],
        pnl=1000.0,
        meta=meta or None,
    )


def test_an_explicit_equity_at_entry_is_the_basis() -> None:
    assert _trade_equity_basis(_trade(equity_at_entry=50_000.0), 100_000.0) == pytest.approx(50_000.0)


def test_risk_math_equity_at_entry_is_used_when_there_is_no_explicit_one() -> None:
    trade = _trade(risk_math={"equity_at_entry": 40_000.0})
    assert _trade_equity_basis(trade, 100_000.0) == pytest.approx(40_000.0)


def test_the_explicit_value_wins_over_risk_math() -> None:
    trade = _trade(equity_at_entry=50_000.0, risk_math={"equity_at_entry": 40_000.0})
    assert _trade_equity_basis(trade, 100_000.0) == pytest.approx(50_000.0)


# --- the fallback, which is where the mutation lived -----------------------------


def test_with_no_recorded_equity_the_basis_is_the_initial_capital() -> None:
    """Not 1.0. The basis divides a quantity, so a fallback of 1 scales every position by the account
    size - which is exactly the failure the mutation introduced, silently."""
    assert _trade_equity_basis(_trade(), 100_000.0) == pytest.approx(100_000.0)
    assert _trade_equity_basis(_trade(), 25_000.0) == pytest.approx(25_000.0)


@pytest.mark.parametrize(
    "meta",
    [
        {"equity_at_entry": 0.0},
        {"equity_at_entry": -5.0},
        {"equity_at_entry": "not a number"},
        {"equity_at_entry": None},
        {"risk_math": {"equity_at_entry": 0.0}},
        {"risk_math": {"equity_at_entry": -1.0}},
        {"risk_math": "not a dict"},
        {"equity_at_entry": "junk", "risk_math": {"equity_at_entry": "also junk"}},
    ],
)
def test_an_unusable_recorded_equity_falls_back_to_the_initial_capital(meta: dict) -> None:
    assert _trade_equity_basis(_trade(**meta), 100_000.0) == pytest.approx(100_000.0)


def test_the_fallback_is_never_a_degenerate_denominator() -> None:
    """A basis of 0 is skipped and a basis of 1 is not plausible; assert the shape a caller relies on."""
    basis = _trade_equity_basis(_trade(), 100_000.0)
    assert basis > 1.0, "a per-unit coefficient divided by 1 is the original quantity, not a fraction"
