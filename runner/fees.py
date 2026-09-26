"""Pluggable fee/slippage cost models for backtest engines (#84).

Every engine must price fills through an explicit fee model — there is no
silent gross default. Callers declare costs consciously:

- ``NotionalFeeModel``: fraction of notional per side + flat per-trade
  surcharge (matches the ``fee_rate``/``fee_flat_per_trade`` engine params).
- ``ZeroFeeModel``: explicit declaration that this run prices no costs
  (e.g. a reference-parity run). Passing it is a conscious opt-in, not an
  omission.

``resolve_fee_model`` maps engine params to a model and raises when no fee
configuration is present — missing fee/slippage config is a hard error,
never silent gross.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class FeeModel(Protocol):
    """Prices the round-trip cost of one closed trade."""

    def fee_for_trade(self, entry_price: float, exit_price: float, quantity: float) -> float:
        """Return the total fee for a round trip (entry + exit legs)."""
        ...


@dataclass(frozen=True)
class ZeroFeeModel:
    """Explicit no-cost declaration (conscious opt-in, not an omission)."""

    def fee_for_trade(self, entry_price: float, exit_price: float, quantity: float) -> float:
        return 0.0


@dataclass(frozen=True)
class NotionalFeeModel:
    """Fraction of notional per side + flat per-trade surcharge."""

    rate: float = 0.0
    flat_per_trade: float = 0.0

    def __post_init__(self) -> None:
        if self.rate < 0 or self.flat_per_trade < 0:
            raise ValueError("Fee rate and flat surcharge must be non-negative.")

    def fee_for_trade(self, entry_price: float, exit_price: float, quantity: float) -> float:
        notional = (abs(float(entry_price)) + abs(float(exit_price))) * abs(float(quantity))
        return notional * self.rate + self.flat_per_trade


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
        if not hasattr(model, "fee_for_trade"):
            raise ValueError("params['fee_model'] must implement fee_for_trade().")
        return model
    if "fee_rate" in params or "fee_flat_per_trade" in params:
        try:
            rate = max(0.0, float(params.get("fee_rate", 0.0) or 0.0))
            flat = max(0.0, float(params.get("fee_flat_per_trade", 0.0) or 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid fee params: {exc}") from exc
        return NotionalFeeModel(rate=rate, flat_per_trade=flat)
    if str(params.get("fees", "")).strip().lower() == "zero":
        return ZeroFeeModel()
    raise ValueError(
        "Missing fee/slippage config: pass fee_rate/fee_flat_per_trade, "
        "an explicit fee_model, or fees='zero' to declare a no-cost run."
    )
