"""Cleanup 18-C — trade context endpoint regression tests.

The trade viewer window is a `_TradeWindow` (ohlcv + df + bounds). Slicing the
auto-detected indicators to that window must read the window's records; an
out-of-window indicator point must be dropped instead of raising NameError.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from shared.models import BacktestMeta, BacktestResult, IndicatorSeries, Trade, TradeExit

sys.path.insert(0, str(Path(__file__).parent.parent))


def _trade(trade_id: int = 1) -> Trade:
    return Trade(
        id=trade_id,
        symbol="X",
        direction="LONG",
        entry_time="2021-01-04T00:00:00Z",
        entry_price=100.0,
        quantity=10,
        exits=[
            TradeExit(
                time="2021-01-05T00:00:00Z",
                price=101.0,
                quantity=10,
                pnl=10.0,
            )
        ],
        pnl=10.0,
    )


def _result() -> BacktestResult:
    return BacktestResult(
        meta=BacktestMeta(
            id="trade-context-demo",
            created_at="2026-01-01T00:00:00Z",
            engine="vectorbt",
            strategies=["Demo"],
            backtest_start="2021-01-01",
            backtest_end="2021-02-01",
            initial_capital=100000.0,
        ),
        trades=[_trade()],
        indicators=[
            IndicatorSeries(
                name="SMA(20)",
                type="overlay",
                data=[
                    {"time": "2021-01-03T00:00:00Z", "value": 99.0},
                    {"time": "2021-01-04T00:00:00Z", "value": 100.0},
                    {"time": "2021-01-06T00:00:00Z", "value": 101.0},
                ],
            )
        ],
    )


def _ohlcv_df() -> pd.DataFrame:
    timestamps = pd.to_datetime(
        [
            "2021-01-04T00:00:00Z",
            "2021-01-04T01:00:00Z",
            "2021-01-04T02:00:00Z",
            "2021-01-04T03:00:00Z",
            "2021-01-04T04:00:00Z",
        ],
        utc=True,
    )
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0] * 5,
            "high": [101.0] * 5,
            "low": [99.0] * 5,
            "close": [100.5] * 5,
            "volume": [1000.0] * 5,
        }
    )


def test_trade_context_slices_indicators_to_the_window(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    result = _result()
    monkeypatch.setattr(viewer_app_module, "_load_result", lambda _backtest_id: result)
    monkeypatch.setattr(
        viewer_app_module, "_load_symbol_ohlcv_with_details", lambda *_args, **_kwargs: (_ohlcv_df(), None)
    )

    response = viewer_client.get("/api/backtest/trade-context-demo/trade/1")

    assert response.status_code == 200, response.text
    payload = response.json()

    ohlcv = payload["ohlcv"]
    assert ohlcv, "trade window must include OHLCV rows"
    assert payload["indicators"], "result-declared indicators must be served"

    window_start = ohlcv[0]["time"]
    window_end = ohlcv[-1]["time"]
    for indicator in payload["indicators"]:
        points = indicator["data"]
        assert points, "window slice must keep the in-window points"
        assert all(window_start <= point["time"] <= window_end for point in points), (
            "indicator points must be sliced to the trade window"
        )


def test_trade_context_endpoint_still_serves_symbol_and_totals(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    result = _result()
    monkeypatch.setattr(viewer_app_module, "_load_result", lambda _backtest_id: result)
    monkeypatch.setattr(
        viewer_app_module, "_load_symbol_ohlcv_with_details", lambda *_args, **_kwargs: (_ohlcv_df(), None)
    )

    response = viewer_client.get("/api/backtest/trade-context-demo/trade/1")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["symbol"] == "X"
    assert payload["total_trades"] == 1
    assert payload["trade"]["nr"] == 1
    assert payload["drawings"] == []
