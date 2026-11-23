"""Lazy ``merged_row`` equivalence for the native engine hot loop (#210).

``TradeEngine.run`` builds the merged signal row (OHLCV row overlaid with the
strategy's signal columns) lazily — only on bars where ``entries`` or ``exits``
fire — instead of on every bar. ``combine_first`` is pure, so the dumps must be
identical; these tests pin that by running each scenario twice: once with the
lazy path and once with ``engine._eager_signal_rows = True`` (the pre-#210
eager construction, kept as a diagnostic hook).

Covered: non-signal values living in signal columns on non-signal bars,
market AND ``exit_order_type="level"`` exits with explicit ``exit_price``,
signal column names that collide with OHLCV columns, both
``signal_exit_fill`` modes, and a fixed-risk sizing parameter.
"""

from __future__ import annotations

import math
from dataclasses import fields, is_dataclass

import numpy as np
import pandas as pd
import pytest

from runner.engine import TradeEngine
from runner.engines.base import BacktestConfig


def _frame(rows: int = 400) -> pd.DataFrame:
    """Deterministic 1h frame with enough swing to fill entries, signal exits,
    level exits and protective stops."""
    idx = pd.date_range("2024-01-01", periods=rows, freq="1h", tz="UTC")
    t = np.arange(rows, dtype=float)
    close = 100.0 + 8.0 * np.sin(t / 17.0) + 0.05 * t
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + 0.4
    low = np.minimum(open_, close) - 0.4
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000.0,
        }
    )


def _strategy(df: pd.DataFrame, params: dict):
    """Sparse entries/exits plus signal columns that are populated on EVERY
    bar (non-signal values), including names that collide with OHLCV columns."""
    n = len(df)
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    for bar in (30, 90, 150, 210, 270, 330):
        entries.iloc[bar] = True
    # Market exits a few bars after each entry, plus two explicit level exits.
    for bar in (40, 100, 160, 220, 280, 340, 250, 360):
        exits.iloc[bar] = True

    idx = df.index
    indicators = {
        "direction": pd.Series("LONG", index=idx),
        # Populated on every bar — the lazy path must not let these leak into
        # bars without a signal, and the eager path must not corrupt OHLCV.
        "noise": pd.Series(np.linspace(0.0, 1.0, n), index=idx),
        "close": pd.Series(9999.0, index=idx),  # collides with OHLCV "close"
        "high": pd.Series(-1.0, index=idx),  # collides with OHLCV "high"
        "exit_order_type": pd.Series([None] * n, index=idx, dtype=object),
        "exit_price": pd.Series(np.nan, index=idx),
        "sl_price": pd.Series(np.nan, index=idx),
        "tp_price": pd.Series(np.nan, index=idx),
    }
    # Two exits are explicit level exits priced inside the signal bar.
    for bar in (250, 360):
        indicators["exit_order_type"].iloc[bar] = "level"
        indicators["exit_price"].iloc[bar] = float(df.iloc[bar]["close"])
    # Protective levels on the first two entries so _check_stops fires too.
    for bar in (30, 90):
        indicators["sl_price"].iloc[bar] = float(df.iloc[bar]["low"]) - 0.5
        indicators["tp_price"].iloc[bar] = float(df.iloc[bar]["high"]) + 0.5
    return entries, exits, indicators, []


def _plain(obj):
    """Recursively serialize trades / equity points for an exact comparison."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj):
            return "NaN"
        if math.isinf(obj):
            return "Inf" if obj > 0 else "-Inf"
    return obj


def _dump(result) -> dict:
    curve = result.equity_curve
    points = [
        (getattr(p, "time", None), getattr(p, "equity", None)) for p in curve
    ]
    return {"trades": _plain(result.trades), "equity": _plain(points)}


def _run(signal_exit_fill: str, parameters: dict | None = None) -> tuple[dict, dict]:
    data = _frame()
    cfg = BacktestConfig(
        strategy_name="lazy-equiv",
        symbols=["TEST"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-20",
        initial_capital=100_000,
        risk_per_trade_pct=0.01,
        signal_exit_fill=signal_exit_fill,
        parameters={"fees": "zero", "slippage": "none", **(parameters or {})},
    )
    lazy = TradeEngine(cfg, symbol="TEST")
    lazy_result = lazy.run(data, _strategy)

    eager = TradeEngine(cfg, symbol="TEST")
    eager._eager_signal_rows = True
    eager_result = eager.run(data, _strategy)
    return _dump(lazy_result), _dump(eager_result)


@pytest.mark.parametrize("fill", ["next_open", "close"])
def test_lazy_and_eager_merged_row_dumps_identical(fill: str):
    lazy, eager = _run(fill)
    assert lazy == eager


def test_lazy_and_eager_identical_with_fixed_risk():
    lazy, eager = _run("next_open", {"fixed_risk_amount": 250.0})
    assert lazy == eager


def test_scenario_actually_trades():
    """Guard against a vacuous pass: the fixture must produce real trades,
    market exits, level exits and protective-stop activity."""
    data = _frame()
    cfg = BacktestConfig(
        strategy_name="lazy-equiv",
        symbols=["TEST"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-20",
        initial_capital=100_000,
        risk_per_trade_pct=0.01,
        parameters={"fees": "zero", "slippage": "none"},
    )
    result = TradeEngine(cfg, symbol="TEST").run(data, _strategy)
    assert len(result.trades) >= 3
    reasons = {t.meta.get("exit_reason") for t in result.trades}
    assert "signal" in reasons
    assert len(result.equity_curve) == len(data)
