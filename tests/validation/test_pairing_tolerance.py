"""Fill-pairing tolerance: how close two quantities must be before they count as equal.

    Why this file exists

    2026-10-15, from the same mutation pass that found the realized-risk accessors unpinned:

        M21  `_quantity_tolerance`: `quantity * 1e-9` -> `quantity * 1e-3`   SURVIVED - 4,987 passed

    A six-order-of-magnitude loosening, and the suite stayed green. The tolerance is what decides two
    questions that matter:

    * `classify_position_fills` marks a position **partial** only when
      `exit_quantity < entry_quantity - tolerance`. Loosen the tolerance and a position that never
      fully exited is reported as **closed** - the validator's quantity reconciliation passes a
      position that is still open.
    * the pairing loop treats a residual at or below the tolerance as zero, so an exit of that size is
      allocated to a *new* position instead of closing the existing residual.

    Both are the "a fill is silently dropped" family this audit has met before, in a different guise.
    The binding constraint is the magnitude, not the idea: at `1e-3`, a 0.1% discrepancy is invisible,
    and 0.1% of a leveraged notional is not a rounding artefact.
"""

from __future__ import annotations

import pytest

from validation.importers._pairing import PositionFills, classify_position_fills


def _fills(entry_quantity: float, exit_quantity: float) -> PositionFills:
    return PositionFills(
        position_id="p-1",
        entries=[{"quantity": entry_quantity}],
        exits=[{"quantity": exit_quantity}],
    )


def test_an_exactly_closed_position_is_closed() -> None:
    result = classify_position_fills([_fills(100.0, 100.0)])
    assert result[0].status == "closed"
    assert result[0].residual_quantity == pytest.approx(0.0)


def test_a_position_that_exited_less_than_it_entered_is_partial() -> None:
    """0.05 short of the entry. A real quantity discrepancy, not float noise."""
    result = classify_position_fills([_fills(100.0, 99.95)])
    assert result[0].status == "partial", (
        "a position that exited 0.05 less than it entered is still holding that 0.05; reporting it "
        "as closed hides an unclosed position from the validator"
    )
    assert result[0].residual_quantity == pytest.approx(0.05)


def test_a_position_that_over_exited_is_still_closed_with_the_excess_recorded() -> None:
    result = classify_position_fills([_fills(100.0, 100.05)])
    assert result[0].status == "closed"
    assert result[0].residual_exit_quantity == pytest.approx(0.05)


def test_the_tolerance_is_float_noise_and_not_a_percentage_of_quantity() -> None:
    """The magnitude is the whole point, so it is asserted directly rather than inferred.

    A position is closed when the two quantities agree to within ~1e-9 relative. At `1e-3` the same
    assertion would be about a 0.1% band, which is a different contract entirely.
    """
    # 1e9 units: a 1e-9 relative tolerance is ~1.0 here, so a 0.5 gap is inside it and a 5.0 gap is not.
    exact = classify_position_fills([_fills(1_000_000_000.0, 1_000_000_000.5)])
    assert exact[0].status == "closed"

    outside = classify_position_fills([_fills(1_000_000_000.0, 1_000_000_000.0 - 5.0)])
    assert outside[0].status == "partial", (
        "a 5-unit gap on a billion-unit position is 5e-9 relative - noise, and must not be reported "
        "as an unclosed position; but it must also not be reported as noise if the band is wider"
    )
