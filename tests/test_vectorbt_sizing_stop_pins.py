"""The vectorbt adapter's sizing-only protective level (A-M3): it must never arm an exit.

    Why this file exists

    2026-10-15, from the `runner/` mutation queue. One mutation, against the full suite:

        M44  `_find_sizing_stop_series` resolves `kind="stop_loss"` instead of `kind="sizing_stop"`
             SURVIVED - 5,056 passed, exit 0

    The function it mutates states the rule in its own docstring:

        def _find_sizing_stop_series(...):
            \"\"\"Sizing-only protective level (A-M3): never arms exit detection.\"\"\"

    **A sizing stop and a stop-loss are different things.** A strategy that publishes `sizing_stop` is
    saying "size the position against this level" - it is sizing input, not an instruction to exit.
    Collapsing the two lookup kinds makes a sizing level arm exit detection, so the backtest exits at a
    price the strategy never asked to exit at. Every affected trade changes, and the direction is wrong
    in a way that reads as plausible: exits still happen, at a level that is at least a real price.

    Two other mutations in the same batch were killed by existing tests and needed nothing here:
    `resolve_max_position_exposure_pct` losing its 20% non-equity default, and `resolve_max_leverage`
    defaulting to 20x instead of unlevered.

    The test pins the distinction at the level where it is made - which *series* each lookup resolves -
    because a test of the end-to-end backtest result would have to construct a strategy that both
    publishes a sizing stop and would exit differently, which is a much larger fixture to keep honest.
"""

from __future__ import annotations

import pandas as pd
import pytest

from runner.engines.vectorbt_adapter import (
    _find_entry_stop_series,
    _find_sizing_stop_series,
    _find_stop_loss_series,
)


def _signals(**columns: pd.Series) -> pd.DataFrame:
    index = pd.RangeIndex(3)
    return pd.DataFrame({name: series for name, series in columns.items()}, index=index)


# `stop_loss` is read straight off the signals frame when the column is there. `sizing_stop` is NOT:
# it has no column shortcut and is resolved through the indicator map, where the value is a
# (series, when) pair and `when` must be "entry" for a sizing level. Reading it as a column is how my
# first version of this fixture returned None for a strategy that plainly had one.
_SIZING_INDICATORS = {"sizing_stop": (pd.Series([10.0, 11.0, 12.0]), "entry")}
_STOP_LOSS_ONLY = {"stop_loss": pd.Series([9.0, 10.0, 11.0])}
_EMPTY_FRAME = pd.DataFrame(index=pd.RangeIndex(3))


def test_a_sizing_stop_resolves_for_sizing():
    assert _find_sizing_stop_series(_signals(), _SIZING_INDICATORS) is not None


def test_a_sizing_stop_does_not_resolve_as_a_stop_loss():
    """A-M3. If this returns the series, a sizing level is arming exit detection."""
    assert _find_stop_loss_series(_signals(), _SIZING_INDICATORS) is None


def test_a_real_stop_loss_resolves_for_exit_detection():
    assert _find_stop_loss_series(_signals(**_STOP_LOSS_ONLY), {}) is not None


def test_a_real_stop_loss_does_not_double_as_a_sizing_stop():
    """The two lookups are distinct in both directions, not one falling back to the other."""
    assert _find_sizing_stop_series(_signals(**_STOP_LOSS_ONLY), {}) is None


def test_a_sizing_stop_does_not_resolve_as_an_entry_stop():
    """`entry_stop` is a third, separate concept; the same collapse would be just as wrong here."""
    assert _find_entry_stop_series(_signals(), _SIZING_INDICATORS) is None


def test_an_empty_strategy_resolves_neither():
    assert _find_sizing_stop_series(_EMPTY_FRAME, {}) is None
    assert _find_stop_loss_series(_EMPTY_FRAME, {}) is None


def test_the_two_lookups_disagree_when_a_strategy_publishes_both():
    """The case the mutation is most dangerous in: both levels present, different values."""
    frame = _signals(stop_loss=pd.Series([9.0, 10.0, 11.0]))
    sizing = _find_sizing_stop_series(frame, _SIZING_INDICATORS)
    stop = _find_stop_loss_series(frame, _SIZING_INDICATORS)
    assert sizing is not None and stop is not None
    assert float(sizing.iloc[0]) == pytest.approx(10.0)
    assert float(stop.iloc[0]) == pytest.approx(9.0)
