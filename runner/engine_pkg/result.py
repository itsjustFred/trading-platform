"""Result assembly for the native engine.

Builds the universal ``BacktestResult`` (meta + trades + equity curve +
indicators + drawings) from the engine's accumulated state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from runner.engine_pkg._time import to_iso_utc
from shared.models import BacktestMeta, BacktestResult, IndicatorSeries


def build_indicator_series(indicators: dict[str, Any], data: pd.DataFrame) -> list[IndicatorSeries]:
    """Convert strategy indicator output into viewer IndicatorSeries payloads."""
    points_time = [to_iso_utc(ts) for ts in data["timestamp"]]
    output: list[IndicatorSeries] = []

    for name, value in (indicators or {}).items():
        if isinstance(value, tuple) and len(value) == 2:
            series, pane = value
            pane_type = str(pane)
        else:
            series = value
            pane_type = "overlay"

        if not isinstance(series, pd.Series):
            continue
        numeric = pd.to_numeric(series.reindex(data.index), errors="coerce")
        data_points = [
            {"time": points_time[idx], "value": float(val)}
            for idx, val in enumerate(numeric)
            if pd.notna(val)
        ]
        if not data_points:
            continue
        output.append(IndicatorSeries(name=str(name), type=pane_type, data=data_points, style={}))

    return output


def build_result(
    *,
    config,
    symbol: str,
    closed_trades: list,
    equity_curve: list,
    indicator_series: list[IndicatorSeries],
    drawings: list[dict[str, Any]],
) -> BacktestResult:
    """Assemble the native engine's BacktestResult."""
    created_at = datetime.now(timezone.utc).isoformat()
    meta = BacktestMeta(
        id=f"native-{config.strategy_name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        created_at=created_at,
        engine="native",
        strategies=[config.strategy_name],
        backtest_start=config.start_date,
        backtest_end=config.end_date,
        data={
            "symbols": [symbol],
            "timeframes": [config.timeframe],
            "source": "ohlcv",
        },
        initial_capital=config.initial_capital,
        risk_per_trade_pct=config.risk_per_trade_pct,
        max_position_exposure_pct=config.max_position_exposure_pct,
        max_leverage=config.max_leverage,
        modeling_resolution=config.modeling_resolution,
        signal_exit_fill=config.signal_exit_fill,
        allow_fallback_stop=config.allow_fallback_stop,
        reject_missing_stop=config.reject_missing_stop,
        require_explicit_column_roles=config.require_explicit_column_roles,
        parameters=config.parameters,
    )
    return BacktestResult(
        meta=meta,
        trades=closed_trades,
        equity_curve=equity_curve,
        indicators=indicator_series,
        drawings=drawings,
    )
