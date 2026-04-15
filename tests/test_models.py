"""Tests for shared.models — universal trade format."""

import json
import tempfile
from pathlib import Path

from shared.models import (
    BacktestMeta,
    BacktestResult,
    DrawingSchema,
    EquityPoint,
    ExitLeg,
    SignalExecutionSchema,
    SignalOrder,
    StrategyMeta,
    Trade,
    TradeExit,
    create_triangle_definition,
    parse_signal_order_shorthand,
    place_triangle_instance,
)


def _make_simple_trade() -> Trade:
    return Trade(
        id=1,
        symbol="AAPL",
        direction="LONG",
        entry_time="2021-03-15T10:30:00Z",
        entry_price=121.50,
        quantity=82,
        exits=[
            TradeExit(time="2021-03-18T15:45:00Z", price=124.80, quantity=82, reason="rsi_target", fees=1.64)
        ],
        pnl=270.60,
        pnl_pct=2.72,
        fees=1.64,
        net_pnl=268.96,
    )


def _make_partial_exit_trade() -> Trade:
    return Trade(
        id=7,
        symbol="CRWD",
        direction="LONG",
        entry_time="2026-02-23T14:35:02Z",
        entry_price=376.37,
        quantity=138,
        exits=[
            TradeExit(time="2026-02-23T15:20:00Z", price=387.22, quantity=69, reason="tp1", fees=0.82, tags=["tp1"]),
            TradeExit(time="2026-02-23T16:45:00Z", price=382.10, quantity=69, reason="supertrend_flip", fees=0.82, tags=["runner"]),
        ],
        pnl=1143.67,
        pnl_pct=2.20,
        fees=1.64,
        net_pnl=1142.03,
        tags=["tp1_reached"],
        meta={"sl_price": 369.14, "tp1_price": 387.22},
    )


def _make_result() -> BacktestResult:
    return BacktestResult(
        meta=BacktestMeta(
            id="test-run-20260224",
            created_at="2026-02-24T14:30:22Z",
            engine="vectorbt",
            strategies=["rsi_mean_reversion"],
            backtest_start="2021-01-01",
            backtest_end="2025-06-30",
            initial_capital=100_000.0,
            data={
                "timeframes": ["1h"],
                "symbols": ["AAPL", "CRWD"],
                "universe": "",
                "source": "yfinance",
            },
            parameters={"rsi_period": 2},
        ),
        trades=[_make_simple_trade(), _make_partial_exit_trade()],
        equity_curve=[
            EquityPoint(time="2021-01-01", equity=100_000.0),
            EquityPoint(time="2021-01-02", equity=100_150.30),
        ],
        stats={"total_trades": 2, "win_rate": 100.0},
        drawings=[
            {
                "type": "marker",
                "time": "2021-03-15T10:30:00Z",
                "position": "belowBar",
                "shape": "arrowUp",
                "color": "#3fb950",
                "text": "Buy",
            },
            {
                "type": "line",
                "start_time": "2021-03-15T10:30:00Z",
                "end_time": "2021-03-18T15:45:00Z",
                "val1": 121.5,
                "val2": 124.8,
                "color": "#58a6ff",
            },
        ],
    )


class TestTradeExit:
    def test_roundtrip(self):
        e = TradeExit(time="2021-03-18T15:45:00Z", price=124.80, quantity=82, reason="tp1", fees=1.0)
        d = e.to_dict()
        e2 = TradeExit.from_dict(d)
        assert e2.time == e.time
        assert e2.price == e.price
        assert e2.quantity == e.quantity
        assert e2.reason == e.reason
        assert e2.fees == e.fees

    def test_minimal(self):
        e = TradeExit(time="2021-01-01T00:00:00Z", price=100.0, quantity=10)
        d = e.to_dict()
        assert "reason" not in d
        assert "fees" not in d
        assert "tags" not in d


class TestTrade:
    def test_simple_roundtrip(self):
        t = _make_simple_trade()
        d = t.to_dict()
        t2 = Trade.from_dict(d)
        assert t2.id == t.id
        assert t2.symbol == t.symbol
        assert t2.pnl == t.pnl
        assert len(t2.exits) == 1
        assert t2.exits[0].reason == "rsi_target"

    def test_partial_exit_roundtrip(self):
        t = _make_partial_exit_trade()
        d = t.to_dict()
        t2 = Trade.from_dict(d)
        assert len(t2.exits) == 2
        assert t2.exits[0].reason == "tp1"
        assert t2.exits[1].reason == "supertrend_flip"
        assert t2.tags == ["tp1_reached"]
        assert t2.meta["sl_price"] == 369.14

    def test_is_winner(self):
        t = _make_simple_trade()
        assert t.is_winner is True
        t.pnl = -50.0
        assert t.is_winner is False

    def test_last_exit_time(self):
        t = _make_partial_exit_trade()
        assert t.last_exit_time == "2026-02-23T16:45:00Z"

    def test_direction_sign(self):
        t = _make_simple_trade()
        assert t.direction_sign == 1
        t.direction = "SHORT"
        assert t.direction_sign == -1

    def test_optional_fields_omitted_when_none(self):
        t = Trade(
            id=1, symbol="X", direction="LONG",
            entry_time="2021-01-01T00:00:00Z", entry_price=100.0,
            quantity=10,
            exits=[TradeExit(time="2021-01-02T00:00:00Z", price=105.0, quantity=10)],
            pnl=50.0,
        )
        d = t.to_dict()
        assert "pnl_pct" not in d
        assert "fees" not in d
        assert "r_multiple" not in d
        assert "tags" not in d
        assert "meta" not in d


class TestBacktestResult:
    def test_json_roundtrip(self):
        r = _make_result()
        json_str = r.to_json()
        r2 = BacktestResult.from_json(json_str)
        assert r2.meta.id == r.meta.id
        assert r2.meta.strategies == ["rsi_mean_reversion"]
        assert len(r2.trades) == 2
        assert len(r2.equity_curve) == 2
        assert r2.stats["total_trades"] == 2
        assert len(r2.drawings) == 2

    def test_file_roundtrip(self):
        r = _make_result()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_result.json"
            r.save(path)
            r2 = BacktestResult.load(path)
            assert r2.meta.id == r.meta.id
            assert len(r2.trades) == 2

    def test_filename_generation(self):
        r = _make_result()
        assert r.filename == "rsi_mean_reversion_vectorbt_20260224-143022.json"

    def test_meta_serialization(self):
        r = _make_result()
        d = r.to_dict()
        assert d["meta"]["backtest_start"] == "2021-01-01"
        assert d["meta"]["backtest_end"] == "2025-06-30"
        assert d["meta"]["strategies"] == ["rsi_mean_reversion"]
        assert d["meta"]["data"]["timeframes"] == ["1h"]
        assert d["drawings"][0]["type"] == "marker"


class TestStrategyMeta:
    def test_roundtrip(self):
        sm = StrategyMeta(
            name="Test Strategy",
            version="1.0.0",
            tags={"type": ["momentum"], "complexity": "simple"},
        )
        d = sm.to_dict()
        sm2 = StrategyMeta.from_dict(d)
        assert sm2.name == sm.name
        assert sm2.version == sm.version
        assert sm2.tags == sm.tags

    def test_minimal(self):
        sm = StrategyMeta(name="Minimal")
        d = sm.to_dict()
        assert d["name"] == "Minimal"
        assert d["version"] == "1.0.0"
        assert "tags" not in d


class TestSignalExecutionSchema:
    def test_roundtrip_with_limit_order_and_partial_legs(self):
        schema = SignalExecutionSchema(
            direction="LONG",
            entry=SignalOrder(
                action="OPEN",
                direction="LONG",
                order_type="LIMIT",
                size_model="PCT_POSITION",
                size_value=0.5,
                limit_price=101.25,
                label="long_limit_50",
            ),
            tp_legs=[
                ExitLeg(kind="TP", quantity_pct=0.5, price=110.0, priority=10, label="tp1"),
                ExitLeg(kind="TP", quantity_pct=0.5, price=115.0, priority=20, label="tp2"),
            ],
            sl_leg=ExitLeg(kind="SL", quantity_pct=1.0, price=96.0, priority=5, label="sl"),
            meta={"signal_schema_version": "2.0"},
        )

        payload = schema.to_dict()
        restored = SignalExecutionSchema.from_dict(payload)

        assert restored.direction == "LONG"
        assert restored.entry is not None
        assert restored.entry.order_type == "LIMIT"
        assert restored.entry.size_model == "PCT_POSITION"
        assert restored.entry.size_value == 0.5
        assert len(restored.tp_legs) == 2
        assert restored.tp_legs[0].label == "tp1"
        assert restored.sl_leg is not None
        assert restored.sl_leg.kind == "SL"

    def test_invalid_order_requirements_raise(self):
        try:
            SignalOrder(
                action="OPEN",
                direction="LONG",
                order_type="LIMIT",
                size_model="PCT_POSITION",
                size_value=0.5,
            )
            assert False, "Expected ValueError for missing limit_price"
        except ValueError as exc:
            assert "limit_price" in str(exc)


class TestDrawingSchemaV2:
    def test_triangle_definition_and_placement_helpers(self):
        definition = create_triangle_definition(
            "triangle_normal_md",
            default_size="md",
            line_style="dashed",
            line_width=2,
            color="#58a6ff",
        )
        instance = place_triangle_instance(
            "triangle_normal_md",
            x=1234567890,
            y=101.5,
            rotation=45.0,
            size="lg",
        )

        schema = DrawingSchema(definitions=[definition], instances=[instance])
        payload = schema.to_dict()
        restored = DrawingSchema.from_dict(payload)

        assert restored.definitions[0].type == "triangle"
        assert restored.definitions[0].style["line_style"] == "dashed"
        assert restored.instances[0].x == 1234567890.0
        assert restored.instances[0].y == 101.5
        assert restored.instances[0].rotation == 45.0
        assert restored.instances[0].size == "lg"

    def test_triangle_size_validation(self):
        try:
            place_triangle_instance("triangle_normal_md", x=1, y=2, size="xl")
            assert False, "Expected ValueError for invalid triangle size"
        except ValueError as exc:
            assert "size" in str(exc)


class TestSignalOrderShorthand:
    def test_parse_long_limit_percent(self):
        order = parse_signal_order_shorthand("Long(Limit, 50, 101.25)")
        assert order.direction == "LONG"
        assert order.order_type == "LIMIT"
        assert order.size_model == "PCT_POSITION"
        assert order.size_value == 0.5
        assert order.limit_price == 101.25

    def test_parse_short_market_percent_symbol(self):
        order = parse_signal_order_shorthand("Short(Market, 25%)")
        assert order.direction == "SHORT"
        assert order.order_type == "MARKET"
        assert order.size_value == 0.25

    def test_parse_stop_limit_requires_two_prices(self):
        order = parse_signal_order_shorthand("Long(StopLimit, 40, 99.5, 99.8)")
        assert order.order_type == "STOP_LIMIT"
        assert order.stop_price == 99.5
        assert order.limit_price == 99.8

    def test_parse_invalid_format_raises(self):
        try:
            parse_signal_order_shorthand("Long-Limit-50")
            assert False, "Expected ValueError for invalid shorthand format"
        except ValueError as exc:
            assert "Invalid shorthand" in str(exc)
