"""Shared builders for hand-repeated test objects (proposal S03).

Every ``LiveDeploymentConfig``/``LiveSignal``/``BacktestConfig``/OHLCV frame used
to be rebuilt by hand in dozens of test files, so each new required model field
broke dozens of tests at once. The builders here carry the required-field
defaults once; a test passes only the values it actually depends on as keyword
overrides, and assertions stay exactly where they were.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pandas as pd

from runner.engines.base import BacktestConfig
from shared.models import LiveDeploymentConfig, LiveSignal


def make_live_deployment(**overrides: Any) -> LiveDeploymentConfig:
    """A valid ``LiveDeploymentConfig``; ``overrides`` replace any field.

    Defaults cover only the model's required fields (``strategy_name``,
    ``account_id``, ``symbols``, ``timeframe``); everything else keeps the
    model's own default. ``id`` is a fresh UUID per call unless overridden.
    """
    fields: dict[str, Any] = {
        "id": uuid4(),
        "strategy_name": "test-strategy",
        "account_id": "test-account",
        "symbols": ["AAPL"],
        "timeframe": "1h",
    }
    fields.update(overrides)
    return LiveDeploymentConfig(**fields)


def make_live_signal(**overrides: Any) -> LiveSignal:
    """A valid ``LiveSignal``; ``overrides`` replace any field.

    Defaults cover the required fields; ``timestamp`` defaults to a fixed
    instant (2024-01-01 UTC) so a caller that forgets to set it cannot get a
    run-dependent value, and ``metadata`` defaults to an empty dict. ``id``
    is a fresh UUID per call.
    """
    fields: dict[str, Any] = {
        "id": uuid4(),
        "deployment_id": "test-deployment",
        "symbol": "AAPL",
        "action": "buy",
        "theoretical_price": 100.0,
        "timestamp": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "metadata": {},
    }
    fields.update(overrides)
    return LiveSignal(**fields)


def make_backtest_config(
    *, start_date: str, end_date: str, **overrides: Any
) -> BacktestConfig:
    """A valid ``BacktestConfig``; ``overrides`` replace any field.

    ``start_date``/``end_date`` are required keyword arguments: the window a
    backtest covers is the single most load-bearing choice, so every caller
    states it explicitly instead of inheriting a silent default. Defaults
    cover the remaining required fields (``strategy_name``, ``symbols``,
    ``timeframe``); every other field keeps the model's own default, including
    the deprecated-kwarg folding in ``BacktestConfig.__init__``.
    """
    fields: dict[str, Any] = {
        "strategy_name": "test-strategy",
        "symbols": ["AAPL"],
        "timeframe": "1h",
        "start_date": start_date,
        "end_date": end_date,
    }
    fields.update(overrides)
    return BacktestConfig(**fields)


def make_ohlcv(
    *,
    bars: Sequence[tuple[float, float, float, float]] | None = None,
    closes: Iterable[float] | None = None,
    periods: int | None = None,
    start_price: float = 100.0,
    step: float = 1.0,
    freq: str = "h",
    start: str = "2024-01-01",
    tz: str = "UTC",
    wick: float = 0.25,
    volume: float | None = 1000.0,
    spread_price: float | None = None,
    as_index: bool = False,
) -> pd.DataFrame:
    """A deterministic OHLCV frame for engine and strategy tests.

    Exactly one of ``bars`` (explicit ``(open, high, low, close)`` tuples),
    ``closes`` or ``periods`` selects the price series:

    - ``bars``: used verbatim (validated: ``high >= max(open, close)`` and
      ``low <= min(open, close)`` on every bar).
    - ``closes``: ``open == close``, ``high == close + wick``,
      ``low == close - wick``.
    - ``periods`` (with neither of the above): linear closes
      ``start_price + i * step``.

    ``freq``/``start``/``tz`` drive the ``pd.date_range`` index. The frame
    carries a ``timestamp`` column unless ``as_index`` is set, in which case
    the timestamps become the index instead (the shape
    ``market_features_from_frame``-style consumers expect). ``volume=None``
    omits the column; ``spread_price`` adds a constant ``spread_price``
    column.
    """
    if bars is not None:
        ohlc = [tuple(bar) for bar in bars]
    elif closes is not None:
        series = [float(close) for close in closes]
        ohlc = [(close, close + wick, close - wick, close) for close in series]
    elif periods is not None:
        series = [start_price + i * step for i in range(periods)]
        ohlc = [(close, close + wick, close - wick, close) for close in series]
    else:
        raise ValueError("make_ohlcv needs one of bars, closes or periods")

    for open_, high, low, close_ in ohlc:
        if high < max(open_, close_) or low > min(open_, close_):
            raise ValueError(
                f"make_ohlcv got a malformed bar (open={open_}, high={high}, "
                f"low={low}, close={close_}): high must be >= max(open, close) "
                "and low <= min(open, close)"
            )

    index = pd.date_range(start, periods=len(ohlc), freq=freq, tz=tz)
    data: dict[str, Any] = {
        "open": [bar[0] for bar in ohlc],
        "high": [bar[1] for bar in ohlc],
        "low": [bar[2] for bar in ohlc],
        "close": [bar[3] for bar in ohlc],
    }
    if volume is not None:
        data["volume"] = [float(volume)] * len(ohlc)
    if spread_price is not None:
        data["spread_price"] = [float(spread_price)] * len(ohlc)
    frame = pd.DataFrame(data)
    if as_index:
        return frame.set_axis(index)
    frame.insert(0, "timestamp", index)
    return frame
