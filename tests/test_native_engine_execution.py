from runner.engine import TradeEngine
from runner.engines.base import BacktestConfig
import pandas as pd


def _engine(params=None):
    return TradeEngine(
        BacktestConfig(
            strategy_name="test",
            symbols=["EURUSD"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            risk_per_trade_pct=0.01,
            parameters=dict(params or {}),
        ),
        symbol="EURUSD",
    )


def test_native_engine_reads_native_stop_and_take_profit_aliases():
    engine = _engine()
    row = pd.Series({"sl_price": 1.0950, "tp_price": 1.1100})

    stop_loss, take_profit_levels = engine._extract_levels(row)

    assert stop_loss == 1.0950
    assert take_profit_levels == [{"name": "tp", "price": 1.1100, "fraction": 1.0, "filled": False}]


def test_native_engine_fixed_risk_amount_sizes_from_actual_stop():
    engine = _engine({"fixed_risk_amount": 500.0})

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
    )

    assert quantity == 100.0

def test_native_engine_fixed_risk_amount_honors_mt5_tick_metadata():
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.1,
            "max_lot": 100.0,
            "cap_fixed_risk_by_leverage": False,
        }
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
    )

    assert quantity == 1.0
