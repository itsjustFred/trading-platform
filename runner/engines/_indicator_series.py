"""Indicator-series presentation for the viewer layer.

Builds the viewer-facing ``IndicatorSeries`` payloads (points, pane/type
styling, supertrend splitting) from adapter-side indicator maps. This is
presentation logic — it belongs beside the viewer contract, not inside a
specific engine adapter.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from shared.models import IndicatorSeries

IndicatorMap = dict[str, tuple[pd.Series, str]]

PANEL_INDICATOR_HINTS = ("rsi", "macd", "signal", "hist", "atr", "osc", "stoch", "cci", "adx")
HIDDEN_DISPLAY_HINTS = (
    "sl_price",
    "stop_loss",
    "tp_price",
    "take_profit",
    "exit_price",
    "exit_reason",
    "direction",
)


def _infer_indicator_display_type(name: str) -> str:
    lowered = name.lower()
    if any(hint in lowered for hint in PANEL_INDICATOR_HINTS):
        return "panel"
    return "overlay"


def _infer_indicator_pane(name: str) -> str | None:
    lowered = name.lower()
    if "macd" in lowered or "signal" in lowered or "hist" in lowered:
        return "macd"
    if "rsi" in lowered:
        return "rsi"
    if "atr" in lowered:
        return "atr"
    return None


def _is_hidden_indicator(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in HIDDEN_DISPLAY_HINTS)


def _series_points(
    timestamps: pd.Series,
    values: pd.Series,
    *,
    include_gaps: bool = False,
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for idx, raw_time in timestamps.items():
        if idx not in values.index:
            continue
        value = values.loc[idx]
        timestamp = pd.to_datetime(raw_time, utc=True, errors="coerce")
        if pd.isna(timestamp):
            continue

        point: dict[str, Any] = {"time": pd.Timestamp(timestamp).isoformat()}
        if pd.isna(value):
            if include_gaps:
                point["value"] = None
                points.append(point)
            continue

        point["value"] = round(float(value), 6)
        points.append(point)

    return points


def _supertrend_component(name: str) -> str | None:
    lowered = name.lower()
    if "supertrend" not in lowered:
        return None

    if any(token in lowered for token in ("_down", "down_", "upper", "bear")):
        return "down"
    if any(token in lowered for token in ("_up", "up_", "lower", "bull")):
        return "up"
    if "trend" in lowered:
        return "trend"
    return None


def _build_supertrend_series(
    timestamps: pd.Series,
    indicators: IndicatorMap,
) -> tuple[list[IndicatorSeries], set[str]]:
    trend_series: pd.Series | None = None
    up_series: pd.Series | None = None
    down_series: pd.Series | None = None
    consumed: set[str] = set()

    for name, (series, _explicit_when) in indicators.items():
        component = _supertrend_component(name)
        if component is None:
            continue
        if component == "trend" and trend_series is None:
            trend_series = pd.to_numeric(series, errors="coerce")
            consumed.add(name)
            continue
        if component == "up" and up_series is None:
            up_series = pd.to_numeric(series, errors="coerce")
            consumed.add(name)
            continue
        if component == "down" and down_series is None:
            down_series = pd.to_numeric(series, errors="coerce")
            consumed.add(name)

    if up_series is None and down_series is None:
        return [], set()

    supertrend_indicators: list[IndicatorSeries] = []

    if up_series is not None:
        split_up = up_series.copy()
        if trend_series is not None:
            split_up = split_up.where(trend_series > 0)
        points = _series_points(timestamps, split_up, include_gaps=True)
        if points:
            supertrend_indicators.append(
                IndicatorSeries(
                    name="supertrend_up",
                    type="overlay",
                    data=points,
                    style={"color": "#22c55e", "pane": "price"},
                )
            )

    if down_series is not None:
        split_down = down_series.copy()
        if trend_series is not None:
            split_down = split_down.where(trend_series < 0)
        points = _series_points(timestamps, split_down, include_gaps=True)
        if points:
            supertrend_indicators.append(
                IndicatorSeries(
                    name="supertrend_down",
                    type="overlay",
                    data=points,
                    style={"color": "#ef4444", "pane": "price"},
                )
            )

    return supertrend_indicators, consumed


def _build_indicator_series(df: pd.DataFrame, indicators: IndicatorMap) -> list[IndicatorSeries]:
    if not indicators or "timestamp" not in df.columns:
        return []

    timestamps = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    output: list[IndicatorSeries] = []
    supertrend_series, consumed = _build_supertrend_series(timestamps, indicators)
    output.extend(supertrend_series)

    for name, (series, _explicit_when) in indicators.items():
        if name in consumed or _is_hidden_indicator(name):
            continue

        numeric = pd.to_numeric(series, errors="coerce")
        if numeric.isna().all():
            continue

        points = _series_points(timestamps, numeric)
        if not points:
            continue

        display_type = _infer_indicator_display_type(name)
        style: dict[str, Any] = {}
        pane = _infer_indicator_pane(name)
        if pane is not None:
            style["pane"] = pane

        output.append(
            IndicatorSeries(
                name=name,
                type=display_type,
                data=points,
                style=style,
            )
        )

    return output
