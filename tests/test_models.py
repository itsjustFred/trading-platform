"""Tests for shared.models — universal trade format."""

import json
import tempfile
from pathlib import Path

from shared.models import (
    BacktestMeta,
    BacktestResult,
    EquityPoint,
    StrategyMeta,
    Trade,
    TradeExit,
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
