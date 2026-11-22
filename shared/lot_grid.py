"""The one lot-grid floor shared by backtest sizing and live order quantities (#278).

Quantities are floored to the broker lot grid in the native engine's fixed-risk
sizing and in the live exit/entry paths. Before #278 each site carried its own
tolerance (`math.floor(x / step + 1e-12)`, a Decimal floor with a different
noise band, `floor(x + 1e-9)`, an integer ``//``), so at the edge -- ``0.3`` on
a ``0.1`` grid, or a ``k * step`` product an ulp low -- a backtest size and the
live order built from it could disagree by one step.

:func:`floor_qty_to_step` is the promoted ``round_exit_qty_down`` (the most
careful of the old floors): exact decimal arithmetic on the shortest
round-trip representations, so ``0.3 / 0.1`` is exactly ``3`` and a step like
``1e-9`` is exact, with a noise band of 1e-6 of a step that absorbs binary
float error (``0.1 + 0.2``, ``k * step`` landing an ulp low, ledger sums read
back from SQLite REAL columns).
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

#: Float noise allowance when flooring to a step, as a fraction of one step. A
#: ledger sum of 2.9999999999999996 is a whole 3, not 2 plus dust; anything this
#: close to the next step is below every broker's quantity resolution.
_STEP_NOISE = Decimal("1e-6")


def floor_qty_to_step(qty: float, step: float) -> float:
    """``qty`` rounded DOWN to a whole number of ``step``.

    An order quantity must never exceed the size it was computed from, so it
    is floored, never rounded to nearest. Computed in decimal so a step like
    1e-9 is exact and ``k * step`` products an ulp low still count as ``k``
    steps. The noise band can return a value up to 1e-6 of a step ABOVE
    ``qty`` -- float dust, below every broker's quantity resolution. Returns
    0.0 when less than one step remains; raises ``ValueError`` for an
    unusable step or quantity.
    """
    try:
        step_value = Decimal(repr(float(step)))
        qty_value = Decimal(repr(float(qty)))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError(f"Unusable order quantity {qty!r} or step {step!r}") from exc
    if not step_value.is_finite() or step_value <= 0:
        raise ValueError(f"Order quantity step must be finite and positive, got {step!r}")
    if not qty_value.is_finite() or qty_value < 0:
        raise ValueError(f"Order quantity must be finite and non-negative, got {qty!r}")
    steps = math.floor(qty_value / step_value + _STEP_NOISE)
    return float(Decimal(steps) * step_value)
