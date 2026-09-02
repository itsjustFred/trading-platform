"""Native TradeEngine — public surface (compatibility shim).

The implementation is split by responsibility:
- ``runner/engine_pkg/trading_engine.py``  — bar simulation loop, order
  lifecycle, risk sizing, position accounting
- ``runner/engine_pkg/execution_model.py`` — execution-model assumptions
  recorded with each trade
- ``runner/engine_pkg/orders.py``          — Order/Position dataclasses, OrderType
- ``runner/engine_pkg/result.py``          — indicator-series presentation and
  BacktestResult assembly

``from runner.engine import TradeEngine`` keeps working for all callers.
"""

from __future__ import annotations

from runner.engine_pkg.execution_model import ExecutionModelSettings
from runner.engine_pkg.orders import Order, OrderType, Position
from runner.engine_pkg.trading_engine import TradeEngine

__all__ = [
    "ExecutionModelSettings",
    "Order",
    "OrderType",
    "Position",
    "TradeEngine",
]
