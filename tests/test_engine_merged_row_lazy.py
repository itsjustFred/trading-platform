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
from tests.builders import make_backtest_config


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
    # Market exits a few bars after each entry, plus exit-only bars while flat.
    # Bars 35/95 carry exit_order_type="level" + exit_price (set below) — they
    # sit inside the positions opened by entries 30/90.
    for bar in (35, 40, 95, 100, 160, 220, 250, 280, 340, 360):
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
    # Two exits are explicit level exits priced inside the signal bar — placed
    # INSIDE open positions (entry 30 fills ~31, market exit is 40; entry 90
    # fills ~91, market exit is 100) so the level path is really exercised. A
    # level exit while flat would never read merged_row's exit_order_type and
    # could not catch a stale-row regression.
    for bar in (35, 95):
        indicators["exit_order_type"].iloc[bar] = "level"
        indicators["exit_price"].iloc[bar] = float(df.iloc[bar]["close"])
    # Protective levels on two LATER entries (150, 210) so _check_stops fires
    # too — kept off entries 30/90 so those positions survive to their level
    # exits at 35/95.
    for bar in (150, 210):
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
    cfg = make_backtest_config(
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
    market exits, level exits INSIDE open positions and protective-stop
    activity — the defect class this file exists for is an exit-only bar
    reusing a stale ``merged_row``."""
    data = _frame()
    cfg = make_backtest_config(
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

    level_prices = {35: float(data.iloc[35]["close"]), 95: float(data.iloc[95]["close"])}
    level_fills = [
        ex
        for t in result.trades
        for ex in t.exits
        if ex.reason == "signal" and ex.price in level_prices.values()
    ]
    # At least one signal-bar level exit filled at its explicit exit_price
    # while a position was open (the level exits sit inside entries 30/90).
    assert level_fills, "no level exit filled at its exit_price"

    protective = [
        ex
        for t in result.trades
        for ex in t.exits
        if ex.reason in {"stop_loss", "take_profit", "tp"}
    ]
    assert protective, "no protective stop/TP exit fired"


def _intrabar_frame() -> pd.DataFrame:
    """6 hourly bars; bar 2 trades through BOTH the stop (95) and the
    take-profit (105) so the same-bar ambiguity needs intrabar chronology."""
    idx = pd.date_range("2026-01-01", periods=6, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
            "high": [100.2, 100.2, 106.0, 100.2, 100.2, 100.2],
            "low": [99.8, 99.8, 94.0, 99.8, 99.8, 99.8],
            "close": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
            "volume": 1000.0,
        }
    )


def _intrabar_for_bar2() -> pd.DataFrame:
    """15m sub-bars of the 02:00 bar: the rally through tp (105) happens
    BEFORE the dip through the stop (95), so the chronological first event
    is the take-profit."""
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2026-01-01 02:00", periods=4, freq="15min", tz="UTC"
            ),
            "open": [100.0, 100.2, 105.5, 96.0],
            "high": [100.4, 106.0, 105.8, 96.0],
            "low": [99.6, 100.0, 94.5, 95.5],
            "close": [100.2, 105.5, 96.0, 95.8],
            "volume": [100.0] * 4,
        }
    )


def _intrabar_strategy(df: pd.DataFrame, params: dict):
    entries = pd.Series(False, index=df.index)
    entries.iloc[0] = True
    exits = pd.Series(False, index=df.index)
    exits.iloc[5] = True
    idx = df.index
    indicators = {
        "direction": pd.Series("LONG", index=idx),
        "sl_price": pd.Series(np.nan, index=idx),
        "tp_price": pd.Series(np.nan, index=idx),
    }
    indicators["sl_price"].iloc[0] = 95.0
    indicators["tp_price"].iloc[0] = 105.0
    return entries, exits, indicators, []


def test_intrabar_next_time_isolation():
    """Pin the ``next_time`` optimisation separately from the lazy
    ``merged_row`` one: ``_intrabar_window`` bounds sub-bars by
    ``[bar_time, next_time)``, so a wrong ``next_time`` empties the window
    and the stop-first fallback fires instead of the chronological tp.

    Lazy and eager must agree AND the run must really resolve the ambiguity
    via intrabar data (a tp fill at 105, not the stop-first 95)."""
    data = _intrabar_frame()
    intrabar = _intrabar_for_bar2()
    cfg = make_backtest_config(
        strategy_name="lazy-equiv-intrabar",
        symbols=["TEST"],
        timeframe="1h",
        modeling_resolution="15m",
        start_date="2026-01-01",
        end_date="2026-01-01",
        initial_capital=100_000,
        risk_per_trade_pct=0.01,
        parameters={"fees": "zero", "slippage": "none"},
    )
    lazy = TradeEngine(cfg, symbol="TEST", intrabar_data=intrabar)
    lazy_result = lazy.run(data, _intrabar_strategy)
    eager = TradeEngine(cfg, symbol="TEST", intrabar_data=intrabar)
    eager._eager_signal_rows = True
    eager_result = eager.run(data, _intrabar_strategy)

    assert _dump(lazy_result) == _dump(eager_result)
    assert len(lazy_result.trades) == 1
    exits = lazy_result.trades[0].exits
    assert [ex.price for ex in exits] == pytest.approx([105.0]), (
        "the intrabar chronology must resolve the same-bar stop/tp "
        "ambiguity to the take-profit, not the stop-first fallback"
    )
