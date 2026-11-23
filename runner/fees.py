"""Pluggable fee/slippage cost models for backtest engines (#84).

Every engine must price fills through an explicit fee model — there is no
silent gross default. Callers declare costs consciously:

- ``NotionalFeeModel``: fraction of notional per side + flat per-trade
  surcharge (matches the ``fee_rate``/``fee_flat_per_trade`` engine params).

Every model prices the round trip *and* each leg, additively
(``fee_for_trade == entry_fee + exit_fee``): a partly-closed position has
already paid some of its legs, and an engine may only hold already-paid
costs out of spendable cash if it can price a single leg.
- ``ZeroFeeModel``: explicit declaration that this run prices no costs
  (e.g. a reference-parity run). Passing it is a conscious opt-in, not an
  omission.

``resolve_fee_model`` maps engine params to a model and raises when no fee
configuration is present — missing fee/slippage config is a hard error,
never silent gross.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol


class FeeModel(Protocol):
    """Prices the cost of one trade, leg by leg.

    A model must be able to price a single leg as well as the whole round
    trip, because a partly-closed position has already paid some legs: an
    engine that holds already-paid costs out of spendable cash (see
    ``TradeEngine._refresh_sizing_equity``) cannot do that from the
    round-trip price alone. The two prices must be additive::

        fee_for_trade(e, x, q) == entry_fee(e, q) + exit_fee(x, q)

    ``entry_fee`` carries the per-trade surcharge (the trade, and with it its
    surcharge, exists from the moment it is opened); ``exit_fee`` does not.
    """

    def fee_for_trade(self, entry_price: float, exit_price: float, quantity: float) -> float:
        """Return the total fee for a round trip (entry + exit legs)."""
        ...

    def entry_fee(self, entry_price: float, quantity: float) -> float:
        """Return the fee of the ENTRY leg, incl. the per-trade surcharge."""
        ...

    def exit_fee(self, exit_price: float, quantity: float) -> float:
        """Return the fee of one EXIT leg, excl. the per-trade surcharge."""
        ...


@dataclass(frozen=True)
class ZeroFeeModel:
    """Explicit no-cost declaration (conscious opt-in, not an omission)."""

    def fee_for_trade(self, entry_price: float, exit_price: float, quantity: float) -> float:
        return 0.0

    def entry_fee(self, entry_price: float, quantity: float) -> float:
        return 0.0

    def exit_fee(self, exit_price: float, quantity: float) -> float:
        return 0.0


@dataclass(frozen=True)
class NotionalFeeModel:
    """Fraction of notional per side + flat per-trade surcharge."""

    rate: float = 0.0
    flat_per_trade: float = 0.0

    def __post_init__(self) -> None:
        try:
            rate = float(self.rate)
            flat = float(self.flat_per_trade)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Fee rate and flat surcharge must be finite and non-negative."
            ) from exc
        if (
            not math.isfinite(rate)
            or not math.isfinite(flat)
            or rate < 0
            or flat < 0
        ):
            raise ValueError(
                "Fee rate and flat surcharge must be finite and non-negative."
            )
        object.__setattr__(self, "rate", rate)
        object.__setattr__(self, "flat_per_trade", flat)

    def fee_for_trade(self, entry_price: float, exit_price: float, quantity: float) -> float:
        return self.entry_fee(entry_price, quantity) + self.exit_fee(exit_price, quantity)

    def entry_fee(self, entry_price: float, quantity: float) -> float:
        return abs(float(entry_price)) * abs(float(quantity)) * self.rate + self.flat_per_trade

    def exit_fee(self, exit_price: float, quantity: float) -> float:
        return abs(float(exit_price)) * abs(float(quantity)) * self.rate


def resolve_fee_model(params: dict[str, Any] | None) -> FeeModel:
    """Resolve a fee model from engine params, failing closed (#84).

    Accepted (in order):
    1. ``params["fee_model"]`` — a ready-made :class:`FeeModel`.
    2. ``fee_rate`` / ``fee_flat_per_trade`` keys — built into a
       :class:`NotionalFeeModel`.
    3. ``params["fees"] == "zero"`` — explicit :class:`ZeroFeeModel` opt-in.

    Anything else raises ``ValueError``: a run without a declared cost model
    must not silently report gross numbers.
    """
    params = params or {}
    model = params.get("fee_model")
    if model is not None:
        missing = [
            name
            for name in ("fee_for_trade", "entry_fee", "exit_fee")
            if not callable(getattr(model, name, None))
        ]
        if missing:
            raise ValueError(
                f"params['fee_model'] must implement {', '.join(missing)}() "
                "(a round-trip price plus per-leg prices; see FeeModel)."
            )
        return model
    if "fee_rate" in params or "fee_flat_per_trade" in params:
        try:
            rate = float(params.get("fee_rate", 0.0) or 0.0)
            flat = float(params.get("fee_flat_per_trade", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid fee params: {exc}") from exc
        return NotionalFeeModel(rate=rate, flat_per_trade=flat)
    if str(params.get("fees", "")).strip().lower() == "zero":
        return ZeroFeeModel()
    raise ValueError(
        "Missing fee/slippage config: pass fee_rate/fee_flat_per_trade, "
        "an explicit fee_model, or fees='zero' to declare a no-cost run."
    )
