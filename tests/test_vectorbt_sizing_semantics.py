"""Vectorbt adapter sizing/equity-curve regressions (Cleanup 16-D1).

Covers:

- F35 — the leverage branch must honor a conservative ``max_leverage`` as an
  independent cap (no silent override toward position-cap semantics, no
  identical if/elif arms).
- F6 — ``meta.total_bars`` must be the loaded bar count, not the trade count.
- F5 — the portfolio equity curve credits closed PnL once per portfolio at
  EXIT time (realized-PnL semantics), instead of crediting per-symbol
  compounded equity at ENTRY time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter, _resolve_max_leverage


STRATEGY = """
import pandas as pd

STRATEGY_META = {"name": "vbt_sizing_sma", "version": "1.0",
                 "parameters": {"fast_length": 3, "slow_length": 10}}

def generate_signals(df, params):
    close = df["close"]
    fast = close.rolling(int(params.get("fast_length", 3))).mean()
    slow = close.rolling(int(params.get("slow_length", 10))).mean()
    entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    exits = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    indicators = {
        "direction": pd.Series("LONG", index=df.index),
        "stop_loss": pd.Series([df["close"].iloc[0] * 0.95] * len(df), index=df.index),
    }
    return entries, exits, indicators
"""


def _deterministic_frame(*, periods: int = 300, start: float = 100.0) -> pd.DataFrame:
    closes: list[float] = []
    price = start
    for cycle in range(3):
        for _ in range(30):
            price += 0.5
            closes.append(price)
        for _ in range(20):
            closes.append(price)
        for _ in range(30):
            price -= 0.5
            closes.append(price)
        for _ in range(20):
            closes.append(price)
    closes = closes[:periods]
    timestamps = pd.date_range("2024-01-01", periods=len(closes), freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": closes,
            "high": [c + 0.25 for c in closes],
            "low": [c - 0.25 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        }
    )


def _write_ohlcv(data_dir: Path, symbol: str, frame: pd.DataFrame) -> None:
    symbol_dir = data_dir / "ohlcv" / "test_source" / "equities" / symbol
    symbol_dir.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(symbol_dir / "1h.parquet", index=False)


def _config(tmp_path: Path, **overrides: Any) -> BacktestConfig:
    payload: dict[str, Any] = {
        "strategy_name": "vbt_sizing_sma",
        "symbols": ["VSZ"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "initial_capital": 100_000,
        "modeling_resolution": "main_timeframe",
        "data_dir": str(tmp_path),
        "parameters": {"fast_length": 3, "slow_length": 10},
    }
    payload.update(overrides)
    return BacktestConfig(**payload)


@pytest.fixture()
def sizing_env(tmp_path: Path):
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    strategy_path = strategy_dir / "vbt_sizing_sma.py"
    strategy_path.write_text(STRATEGY)
    _write_ohlcv(tmp_path, "VSZ", _deterministic_frame())
    return tmp_path, strategy_path


# ─── F35: max_leverage is an independent cap ─────────────────────────────────


class TestLeverageSemantics:
    def test_resolver_keeps_conservative_leverage(self):
        assert _resolve_max_leverage(1.0) == 1.0
        assert _resolve_max_leverage(0.5) == 0.5

    def test_conservative_max_leverage_binds_notional(self, sizing_env):
        """max_leverage=1 must cap the notional at 1x equity — no override."""
        tmp_path, strategy_path = sizing_env
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.05,
            max_position_pct=1.0,  # position cap allows full equity
            max_leverage=1.0,      # leverage forbids more than that
        )
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades, "fixture must produce trades"
        for trade in result.trades:
            notional = trade.entry_price * trade.quantity
            sizing_equity = trade.meta["risk_math"]["equity_at_entry"]
            assert notional <= sizing_equity * 1.0 + 1e-6, (
                "max_leverage=1 must bind the notional to equity even when "
                "max_position_pct would allow more (silent leverage override)"
            )

    def test_risk_math_records_binding_caps(self, sizing_env):
        tmp_path, strategy_path = sizing_env
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.05,
            max_position_pct=1.0,
            max_leverage=1.0,
        )
        result = VectorbtAdapter().run(config, str(strategy_path))
        risk_math = result.trades[0].meta["risk_math"]
        assert risk_math["max_leverage"] == 1.0
        assert risk_math["max_leverage_notional"] == pytest.approx(100_000.0)


# ─── F6: total_bars is the bar count ────────────────────────────────────────


class TestTotalBarsIsBarCount:
    def test_meta_total_bars_counts_bars_not_trades(self, sizing_env):
        tmp_path, strategy_path = sizing_env
        config = _config(tmp_path)
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.meta.total_bars == 300
        assert result.meta.total_bars != len(result.trades)


# ─── F5: portfolio equity curve semantics ───────────────────────────────────


class TestPortfolioEquityCurveSemantics:
    def test_curve_credits_pnl_at_exit_not_entry(self, sizing_env):
        """Every curve point at a trade's ENTRY time must show initial capital.

        Regression (F5): the old curve credited PnL at entry_time while the
        simulator credited it on close — the persisted path described an
        equity trajectory the backtest never traded.
        """
        tmp_path, strategy_path = sizing_env
        config = _config(tmp_path)
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades and result.equity_curve

        equity_by_time = {point.time: point.equity for point in result.equity_curve}
        initial = config.initial_capital
        for trade in result.trades:
            entry_time = str(trade.entry_time)
            # Entry timestamps that carry a curve point must still be at
            # realized-only capital (nothing has closed at entry time).
            if entry_time in equity_by_time:
                assert equity_by_time[entry_time] == pytest.approx(initial, rel=1e-9), (
                    "PnL must be credited at EXIT time; an entry-time point "
                    "carrying profit describes an equity path that was never traded"
                )

    def test_curve_is_monotone_in_realized_pnl_cumsum(self, sizing_env):
        tmp_path, strategy_path = sizing_env
        config = _config(tmp_path)
        result = VectorbtAdapter().run(config, str(strategy_path))

        exits = [str(trade.last_exit_time) for trade in result.trades]
        assert exits == sorted(exits), "curve points must follow exit order"
        running = config.initial_capital
        for trade in sorted(result.trades, key=lambda item: item.last_exit_time):
            running += trade.pnl
            # Every trade's exit lands exactly on the running realized equity.
            matching = [
                point.equity
                for point in result.equity_curve
                if point.time == str(trade.last_exit_time)
            ]
            assert matching, "each exit time must have an equity point"
            assert matching[-1] == pytest.approx(round(running, 2), rel=1e-6)
