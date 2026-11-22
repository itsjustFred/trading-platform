"""#278: one lot-grid floor for backtest sizing and live order quantities.

``shared.lot_grid.floor_qty_to_step`` is the single floor every site uses:
the native engine's ``_floor_to_lot_grid`` (fixed-risk sizing), the daemon's
exit step floor, and the Alpaca/cTrader exit and entry floors. These tests
pin the edge cases the issue names -- ``0.3`` on a ``0.1`` grid and float
dust -- and prove the engine and the live paths cannot disagree by a step.
"""
import math

import pytest

from shared.lot_grid import floor_qty_to_step


@pytest.mark.parametrize(
    ("qty", "step", "expected"),
    [
        # The issue's headline edge: a plain `math.floor(0.3 / 0.1)` is 2.
        (0.3, 0.1, 0.3),
        (0.1 + 0.2, 0.1, 0.3),
        (3 * 0.1, 0.1, 0.3),
        (0.30000000000000004, 0.1, 0.3),
        (0.9, 0.3, 0.9),
        (0.37, 0.1, 0.3),
        (0.49, 0.1, 0.4),
        # Float dust of a whole quantity is not a lost unit.
        (2.9999999999999996, 1.0, 3.0),
        (0.30000000000000004, 0.1, 0.3),
        # A `k * step` product whose float quotient lands an ulp low still
        # counts as k steps (the #151 non-idempotent fixture).
        (25602 * 0.01, 0.01, 256.02),
        (math.nextafter(25602 * 0.01, 0.0), 0.01, 256.02),
        # Tiny steps stay exact in decimal.
        (10 / 3, 1e-9, 3.333333333),
        (1 / 6, 1e-9, 0.166666666),
        # Below one step -> 0.0; a genuine fraction floors, never rounds.
        (0.125, 1.0, 0.0),
        (0.09, 0.1, 0.0),
        (0.3336, 0.001, 0.333),
        (0.5, 0.01, 0.5),
    ],
)
def test_floor_qty_to_step_edges(qty, step, expected) -> None:
    result = floor_qty_to_step(qty, step)
    assert result == pytest.approx(expected, abs=1e-15)
    # The noise band can return a grid value a hair above `qty`, never more
    # than 1e-6 of a step -- below every broker's quantity resolution.
    assert result - qty <= 1e-6 * step + 1e-15


def test_noise_band_never_rounds_a_real_fraction_up() -> None:
    # The 1e-6-of-a-step band is pinned from ABOVE as well as below: a real
    # fraction well clear of the band must floor DOWN, never round up. On
    # the live entry path a wider band would size an order one step above
    # the risk budget.
    assert floor_qty_to_step(0.1999, 0.1) == 0.1
    for k in (1, 2, 7):
        step = 0.1
        qty = (k + 1) * step - 1e-4 * step
        assert floor_qty_to_step(qty, step) == pytest.approx(k * step)


def test_floor_qty_to_step_returns_the_decimal_grid_value() -> None:
    # The decimal product is returned, not `k * step` re-evaluated in float:
    # `3 * 0.1 == 0.30000000000000004`, the floor must give the clean 0.3.
    assert floor_qty_to_step(0.3, 0.1) == 0.3
    assert floor_qty_to_step(0.9, 0.3) == 0.9
    assert floor_qty_to_step(99.9, 0.1) == 99.9


@pytest.mark.parametrize("qty", [float("nan"), float("inf"), -0.1])
def test_floor_qty_to_step_refuses_an_unusable_qty(qty) -> None:
    with pytest.raises(ValueError):
        floor_qty_to_step(qty, 0.1)


@pytest.mark.parametrize("step", [0.0, -1.0, float("nan"), float("inf")])
def test_floor_qty_to_step_refuses_an_unusable_step(step) -> None:
    with pytest.raises(ValueError, match="step must be finite and positive"):
        floor_qty_to_step(1.0, step)


# (qty, step) pairs covering grid products, ulp-low products, genuine
# fractions and the noise-band edge -- the engine floor and every live floor
# must return the same value on all of them.
_PARITY_CASES = [
    (0.3, 0.1),
    (0.1 + 0.2, 0.1),
    (0.37, 0.1),
    (0.49, 0.1),
    (0.9, 0.3),
    (3 * 0.3, 0.3),
    (0.8999999999999999, 0.3),
    (25602 * 0.01, 0.01),
    (math.nextafter(25602 * 0.01, 0.0), 0.01),
    (3 * 0.01, 0.01),
    (2.9999999999999996, 1.0),
    (0.125, 1.0),
    (10 / 3, 1e-9),
    (0.3336, 0.001),
    (5.0, 0.1),
    (99.95, 0.1),
]


def test_exit_signals_keeps_the_round_exit_qty_down_alias() -> None:
    # Open PRs (#329, #369, #350, #367) still import the pre-#278 name from
    # ``live_runner.exit_signals``; the alias keeps them working on an
    # unprotected main until they have landed and migrated. Remove it in a
    # follow-up once they are in.
    from live_runner import exit_signals

    assert exit_signals.round_exit_qty_down is floor_qty_to_step


def test_engine_floor_is_the_shared_helper() -> None:
    # The native engine's `_floor_to_lot_grid` delegates to the shared floor,
    # so a capped backtest size and a live order on the same quantity/step
    # cannot disagree by one step (#278).
    from runner.engine_pkg import trading_engine

    assert trading_engine.floor_qty_to_step is floor_qty_to_step
    for qty, step in _PARITY_CASES:
        assert trading_engine._floor_to_lot_grid(qty, step) == floor_qty_to_step(qty, step)


def test_live_floors_are_the_shared_helper() -> None:
    # The daemon's exit step floor and both adapters' exit floors call the
    # same function; the Alpaca whole-share entry floor and the cTrader
    # qty->volume entry floor go through it as well.
    from live_runner import alpaca_adapter, ctrader_adapter, daemon

    for module in (daemon, alpaca_adapter, ctrader_adapter):
        assert module.floor_qty_to_step is floor_qty_to_step
    for qty, _step in _PARITY_CASES:
        assert alpaca_adapter._floor_to_whole_shares(qty) == floor_qty_to_step(qty, 1.0)
        expected_volume = int(floor_qty_to_step(qty, 1.0))
        if expected_volume <= 0:
            with pytest.raises(ValueError):
                ctrader_adapter._qty_to_broker_volume(qty, 1.0)
        else:
            assert ctrader_adapter._qty_to_broker_volume(qty, 1.0) == expected_volume
