from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from runner.daily_equity import build_daily_equity_curve
from runner.engines._drawings import _normalize_drawing_time
from runner.engines.base import BacktestConfig
from runner.engines.pynecore_adapter import _parse_trade_csv
from runner.engines.signal_replay_adapter import ReplaySignal, SignalReplayEngine
from runner.engines.vectorbt_adapter import (
    _normalize_signal_output,
    _prepare_next_open_execution_data,
    _size_trade,
)
from runner.stats import compute_stats
from runner.walkforward import _build_windows
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit


def _config(tmp_path: Path, **overrides: object) -> BacktestConfig:
    values: dict[str, object] = {
        "strategy_name": "regression",
        "symbols": ["X"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-01-03",
        "initial_capital": 10_000.0,
        "risk_per_trade_pct": 0.01,
        "data_dir": str(tmp_path),
    }
    values.update(overrides)
    return BacktestConfig(**values)


def test_vectorbt_missing_stop_rejection_is_explicit(tmp_path: Path) -> None:
    record = _size_trade(
        config=_config(tmp_path, reject_missing_stop=True),
        equity=10_000.0,
        entry_price=100.0,
        asset_class="equities",
        stop_loss_series=None,
        atr_series=None,
        entry_idx=0,
        symbol="X",
        trade_counter=1,
    )
    assert record["rejected"] is True
    assert record["rejection_reason"] == "missing_stop"
    assert record["qty"] == 0.0


def test_explicit_roles_support_noncanonical_dataframe_columns() -> None:
    index = pd.RangeIndex(3)
    output = pd.DataFrame(
        {
            "open_signal": [True, False, False],
            "close_signal": [False, False, True],
        },
        index=index,
    )
    signals, indicators, drawings = _normalize_signal_output(
        output,
        index,
        symbol="X",
        column_roles={"open_signal": "entry", "close_signal": "exit"},
    )
    prepared, _ = _prepare_next_open_execution_data(
        signals,
        indicators,
        {"open_signal": "entry", "close_signal": "exit"},
        require_explicit_roles=True,
    )
    assert prepared["entries"].tolist() == [False, True, False]
    assert prepared["exits"].tolist() == [False, False, True]
    assert drawings == []


def test_replay_uses_the_declared_finest_timeframe() -> None:
    timestamp = pd.Timestamp("2024-01-01T00:00:00Z")
    signals = [
        ReplaySignal(timestamp, "X", "buy", 100.0, timeframe="5m"),
        ReplaySignal(timestamp, "Y", "buy", 100.0, timeframe="1h"),
    ]
    assert SignalReplayEngine._infer_replay_timeframe(signals, "1d") == "5m"


def test_drawing_epoch_units_and_invalid_values() -> None:
    assert _normalize_drawing_time(1_700_000_000, field_name="time", drawing_index=0).startswith(
        "2023-11-14"
    )
    assert _normalize_drawing_time(
        1_700_000_000_000, field_name="time", drawing_index=0
    ).startswith("2023-11-14")
    with pytest.raises(ValueError):
        _normalize_drawing_time("not-a-time", field_name="time", drawing_index=0)


def test_pynecore_parser_groups_partial_exits(tmp_path: Path) -> None:
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        "Trade #,Bar Index,Type,Signal,Date/Time,Price USD,Contracts,Profit USD,"
        "Profit %,Cumulative profit USD,Cumulative profit %,Run-up USD,Run-up %,"
        "Drawdown USD,Drawdown %\n"
        "1,0,Entry long,,2024-01-01T00:00:00Z,100,2,0,0,0,0,0,0,0,0\n"
        "1,1,Exit long,first,2024-01-01T01:00:00Z,101,1,1,0,1,0,0,0,0,0\n"
        "1,2,Exit long,second,2024-01-01T02:00:00Z,102,1,2,0,3,0,0,0,0,0\n",
        encoding="utf-8",
    )
    trades = _parse_trade_csv(csv_path, "X", 0)
    assert len(trades) == 1
    assert len(trades[0].exits) == 2
    assert trades[0].quantity == 2.0
    assert trades[0].pnl == 3.0


def test_daily_curve_has_explicit_initial_point_and_preserves_nonpositive_returns() -> None:
    result = BacktestResult(
        meta=BacktestMeta(
            id="daily",
            created_at="2024-01-01T00:00:00Z",
            engine="test",
            strategies=["test"],
            initial_capital=100.0,
            backtest_start="2024-01-01",
            backtest_end="2024-01-03",
        ),
        trades=[
            Trade(
                id=1,
                symbol="X",
                direction="LONG",
                entry_time="2024-01-01T01:00:00Z",
                entry_price=100.0,
                quantity=1.0,
                exits=[TradeExit(time="2024-01-01T02:00:00Z", price=110.0, quantity=1.0, pnl=10.0)],
                pnl=10.0,
            ),
            Trade(
                id=2,
                symbol="X",
                direction="LONG",
                entry_time="2024-01-02T01:00:00Z",
                entry_price=100.0,
                quantity=1.0,
                exits=[TradeExit(time="2024-01-02T02:00:00Z", price=0.0, quantity=1.0, pnl=-200.0)],
                pnl=-200.0,
            ),
        ],
    )
    points, returns = build_daily_equity_curve(result)
    assert points[0].time == "2024-01-01T00:00:00+00:00"
    assert points[0].equity == 100.0
    assert returns[-1]["return"] is None
    stats = compute_stats([], initial_capital=100.0, equity_curve=points)
    assert stats["duplicate_equity_timestamps"] == 0
    assert (
        "p_value" not in stats["undefined_metrics"]
        or stats["equity_return_frequency"] == "daily"
    )


def test_stats_reports_duplicate_equity_timestamps() -> None:
    curve = [
        EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100.0),
        EquityPoint(time="2024-01-01T00:00:00+00:00", equity=101.0),
        EquityPoint(time="2024-01-02T00:00:00+00:00", equity=102.0),
    ]
    stats = compute_stats([], initial_capital=100.0, equity_curve=curve)
    assert stats["duplicate_equity_timestamps"] == 1


def test_walkforward_purge_and_embargo_boundaries() -> None:
    windows = _build_windows(
        start="2024-01-01",
        end="2024-06-30",
        train_months=2,
        test_months=1,
        step_months=1,
        purge_days=2,
        embargo_days=3,
    )
    assert len(windows) >= 2
    first = pd.Timestamp(windows[0]["train_end"])
    nominal_end = pd.Timestamp("2024-03-01T23:59:59.999999+00:00")
    assert nominal_end - first >= pd.Timedelta(days=2)
    test_start = pd.Timestamp(windows[0]["test_start"])
    assert test_start - first >= pd.Timedelta(days=3)
