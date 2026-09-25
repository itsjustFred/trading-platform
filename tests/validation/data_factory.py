"""Shared factories for the validation test suite.

Kept dependency-free (only ``shared.models``) so every validation test module
can import the same trade/reference/result builders instead of carrying
per-file copies.
"""

from __future__ import annotations

from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit
from validation.reference import ValidationReference


def make_trade(
    trade_id: int = 1,
    symbol: str = "EURUSD",
    direction: str = "LONG",
    entry_time: str = "2024-01-01T10:00:00Z",
    exit_time: str = "2024-01-01T11:00:00Z",
    entry_price: float = 1.1000,
    exit_price: float = 1.1050,
    quantity: float = 1000.0,
    position_size: float | None = None,
    pnl: float = 50.0,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> Trade:
    """Build a two-point round-trip trade with optional SL/TP metadata."""
    meta: dict[str, object] = {}
    if stop_loss is not None:
        meta["stop_loss"] = stop_loss
    if take_profit is not None:
        meta["take_profit"] = take_profit
    return Trade(
        id=trade_id,
        symbol=symbol,
        direction=direction,
        entry_time=entry_time,
        entry_price=entry_price,
        quantity=quantity,
        exits=[TradeExit(time=exit_time, price=exit_price, quantity=quantity)],
        pnl=pnl,
        position_size=position_size,
        meta=meta,
    )


def make_reference(
    trades: list[Trade], *, strategy_name: str = "test_strategy"
) -> ValidationReference:
    """Build an ``allow_empty`` reference with no stats/metadata attached."""
    return ValidationReference(
        strategy_name=strategy_name,
        source_framework="unknown",
        symbol="EURUSD",
        timeframe="1h",
        settings={},
        trades=trades,
        stats={},
        raw_metadata={},
        allow_empty=True,
    )


def make_passing_risk_evidence():
    from validation.reporting.json_report import StatDelta

    return [
        StatDelta(
            field="risk_position_size",
            expected=1.0,
            actual=1.0,
            passed=True,
            explanation="risk position size matches",
        )
    ]


def make_platform_result(trades: list[Trade]) -> BacktestResult:
    """Build a bare platform-side backtest result."""
    meta = BacktestMeta(
        id="platform_test",
        created_at="2024-01-01T00:00:00Z",
        engine="test",
        strategies=["test"],
    )
    return BacktestResult(meta=meta, trades=trades, stats={})
