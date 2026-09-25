"""Focused tests for newer viewer data-management features."""

from __future__ import annotations

import json
import logging
import os
import sys
from io import StringIO
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from starlette.middleware.gzip import GZipMiddleware
from starlette.websockets import WebSocketDisconnect

from shared.models import (
    BacktestMeta,
    BacktestResult,
    EquityPoint,
    IndicatorSeries,
    Trade,
    TradeExit,
)

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_get_dukascopy_genesis(viewer_client, tmp_path):
    (tmp_path / "dukascopy_genesis.json").write_text(json.dumps({
        "EURUSD": {
            "d1": "1999-01-01",
            "h4": "2003-05-04",
            "s1": "2003-05-04",
            "tick": "2003-05-04",
        },
        "XAUUSD": {
            "d1": "1999-01-01",
            "h4": "2003-05-04",
            "s1": "2003-05-04",
            "tick": "2003-05-04",
        },
    }))

    resp = viewer_client.get("/api/data/dukascopy/genesis?symbols=EURUSD,XAUUSD,GBPUSD&timeframes=d1,sec,4h")

    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "dukascopy"
    assert data["coverage"] == {
        "EURUSD": {
            "d1": {"available": True, "start": "1999-01-01"},
            "s1": {"available": True, "start": "2003-05-04"},
            "h4": {"available": True, "start": "2003-05-04"},
        },
        "XAUUSD": {
            "d1": {"available": True, "start": "1999-01-01"},
            "s1": {"available": True, "start": "2003-05-04"},
            "h4": {"available": True, "start": "2003-05-04"},
        },
        "GBPUSD": {
            "d1": {"available": False, "start": None},
            "s1": {"available": False, "start": None},
            "h4": {"available": False, "start": None},
        },
    }


def test_grid_strategy_endpoint_returns_rows_and_trade_samples(
    viewer_client,
    monkeypatch,
    tmp_path,
):
    import viewer.main as viewer_app_module

    overview_path = tmp_path / "matrix-grid-overview.json"
    overview_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-23T00:00:00Z",
                "source": "test",
                "counts": {
                    "strategies_done": 1,
                    "strategies_blocked": 0,
                    "rows": 1,
                    "ok_rows": 1,
                    "failed_rows": 0,
                    "symbols": ["SPY"],
                },
                "strategy_summaries": [{"strategy": "example_sma", "ok": 1, "requested": 1}],
                "blocked_strategies": [],
                "rows": [
                    {
                        "strategy": "example_sma",
                        "status": "ok",
                        "symbol": "SPY",
                        "timeframe": "1d",
                        "run_id": "grid_example_sma_spy_1d",
                        "backtest_id": "bt_grid_spy_1d",
                        "total_trades": 1,
                        "sharpe": 1.25,
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(viewer_app_module, "MATRIX_GRID_OVERVIEW_PATH", overview_path)

    result = BacktestResult(
        meta=BacktestMeta(
            id="bt_grid_spy_1d",
            created_at="2026-06-23T00:00:00+00:00",
            engine="native",
            strategies=["example_sma"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-05",
            data={"symbols": ["SPY"], "timeframes": ["1d"]},
        ),
        trades=[
            Trade(
                id=1,
                symbol="SPY",
                direction="LONG",
                entry_time="2024-01-02T00:00:00+00:00",
                entry_price=100.0,
                quantity=2.0,
                exits=[
                    TradeExit(
                        time="2024-01-03T00:00:00+00:00",
                        price=105.0,
                        quantity=2.0,
                        pnl=10.0,
                    )
                ],
                pnl=10.0,
            )
        ],
        equity_curve=[],
        stats={},
    )
    monkeypatch.setattr(viewer_app_module, "load_backtest_result", lambda backtest_id: result if backtest_id == "bt_grid_spy_1d" else None)

    resp = viewer_client.get("/api/grid/example_sma?trades_limit=2")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["selected_strategy"] == "example_sma"
    assert payload["counts"]["rows"] == 1
    row = payload["rows"][0]
    assert row["backtest_id"] == "bt_grid_spy_1d"
    assert row["backtest_url"] == "/backtests/bt_grid_spy_1d"
    assert row["trades_url"] == "/api/backtest/bt_grid_spy_1d/trades"
    assert row["trade_sample_count"] == 1
    assert row["trades"][0]["symbol"] == "SPY"


def test_grid_strategy_endpoint_returns_404_for_unknown_strategy(
    viewer_client,
    monkeypatch,
    tmp_path,
):
    import viewer.main as viewer_app_module

    overview_path = tmp_path / "matrix-grid-overview.json"
    overview_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-23T00:00:00Z",
                "source": "test",
                "counts": {"rows": 0, "ok_rows": 0, "failed_rows": 0, "symbols": []},
                "strategy_summaries": [],
                "blocked_strategies": [],
                "rows": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(viewer_app_module, "MATRIX_GRID_OVERVIEW_PATH", overview_path)

    resp = viewer_client.get("/api/grid/missing_strategy")

    assert resp.status_code == 404


def test_get_dukascopy_genesis_returns_missing_timeframes_as_unavailable(
    viewer_client,
    tmp_path,
):
    (tmp_path / "dukascopy_genesis.json").write_text(json.dumps({
        "EURUSD": {
            "d1": "1999-01-01",
            "tick": "2003-05-04",
        },
    }))

    resp = viewer_client.get("/api/data/dukascopy/genesis?symbols=EURUSD&timeframes=tick,4h,m1")
    assert resp.status_code == 200

    coverage = resp.json()["coverage"]["EURUSD"]
    assert coverage["tick"] == {"available": True, "start": "2003-05-04"}
    assert coverage["h4"] == {"available": False, "start": None}
    assert coverage["m1"] == {"available": False, "start": None}


def test_data_check_endpoint_reports_availability_from_catalog(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(
        viewer_app_module,
        "_cached_catalog_ohlcv_entries",
        lambda: [
            {
                "symbol": "AAPL",
                "timeframe": "1d",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "path": "/tmp/aapl.parquet",
                "source": "test",
                "size_bytes": 1,
            }
        ],
    )

    ok_resp = viewer_client.get(
        "/api/data/check?symbol=AAPL&timeframe=1d&start=2024-03-01&end=2024-06-01"
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json() == {"available": True, "missing_range": None}

    missing_resp = viewer_client.get(
        "/api/data/check?symbol=AAPL&timeframe=1d&start=2023-12-01&end=2024-02-01"
    )
    assert missing_resp.status_code == 200
    missing_payload = missing_resp.json()
    assert missing_payload["available"] is False
    assert missing_payload["missing_range"] == {"start": "2023-12-01", "end": "2024-01-01"}

    bad_resp = viewer_client.get("/api/data/check?timeframe=1d")
    assert bad_resp.status_code == 400
    assert bad_resp.json()["detail"] == "symbol is required"


def test_backtest_boundary_gap_tolerance_allows_small_leading_gap():
    import viewer.main as viewer_app_module

    allowed, reason = viewer_app_module._allow_small_backtest_boundary_gap(
        requested_start="2008-01-01",
        requested_end="2008-01-02",
        available_start="2008-01-02",
        available_end="2008-01-03",
    )

    assert allowed is True
    assert reason == "leading gap of 1 day(s)"


def test_backtest_boundary_gap_tolerance_rejects_large_gap():
    import viewer.main as viewer_app_module

    allowed, reason = viewer_app_module._allow_small_backtest_boundary_gap(
        requested_start="2008-01-01",
        requested_end="2008-01-02",
        available_start="2008-02-01",
        available_end="2008-02-10",
    )

    assert allowed is False
    assert reason is None


def test_viewer_app_enables_gzip_middleware(viewer_client):
    import viewer.main as viewer_app_module

    assert any(
        middleware.cls is GZipMiddleware and middleware.kwargs.get("minimum_size") == 500
        for middleware in viewer_app_module.app.user_middleware
    )


def test_static_spa_middleware_returns_404_for_missing_js_asset(viewer_client):
    resp = viewer_client.get("/assets/missing-liveviewer-chunk.js")
    assert resp.status_code == 404


def test_data_download_rejects_dates_before_genesis(viewer_client, tmp_path):
    (tmp_path / "dukascopy_genesis.json").write_text(json.dumps({
        "EURUSD": {
            "d1": "1999-01-01",
            "s1": "2003-05-04",
        },
        "XAUUSD": {
            "d1": "1999-01-01",
            "s1": "2003-05-04",
        },
    }))

    resp = viewer_client.post(
        "/api/data/download",
        json={
            "source": "dukascopy",
            "symbols": "EURUSD,XAUUSD",
            "timeframes": "d1,sec",
            "start": "2000-01-01",
            "end": "2000-01-02",
            "price_type": "bid",
        },
    )

    assert resp.status_code == 400
    payload = resp.json()
    message = payload.get("error") or payload.get("detail", "")
    assert "precedes Dukascopy genesis" in message
    assert "EURUSD/s1 >= 2003-05-04" in message
    assert "XAUUSD/s1 >= 2003-05-04" in message


def test_trade_detail_includes_meta(viewer_client, tmp_path):
    backtests_dir = tmp_path / "backtests"
    ohlcv_dir = tmp_path / "ohlcv" / "test_source" / "equities" / "AAPL"
    backtests_dir.mkdir()
    ohlcv_dir.mkdir(parents=True)

    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01T09:00:00Z", periods=6, freq="h", tz="UTC"),
        "open": [100, 101, 102, 103, 104, 105],
        "high": [101, 102, 103, 104, 105, 106],
        "low": [99, 100, 101, 102, 103, 104],
        "close": [100.5, 101.5, 102.5, 103.5, 104.5, 105.5],
        "volume": [10, 11, 12, 13, 14, 15],
    })
    df.to_parquet(ohlcv_dir / "1h.parquet", index=False)

    result = BacktestResult(
        meta=BacktestMeta(
            id="meta-demo",
            created_at="2026-03-07T15:00:00Z",
            engine="vectorbt",
            strategies=["SMA Crossover"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            initial_capital=100000.0,
            data={
                "timeframes": ["1h"],
                "symbols": ["AAPL"],
                "universe": "",
                "source": "test_source",
            },
        ),
        trades=[
            Trade(
                id=1,
                symbol="AAPL",
                direction="LONG",
                entry_time="2024-01-01T10:00:00+00:00",
                entry_price=101.5,
                quantity=10,
                exits=[
                    TradeExit(
                        time="2024-01-01T12:00:00+00:00",
                        price=103.5,
                        quantity=10,
                        pnl=20.0,
                    )
                ],
                pnl=20.0,
                meta={"SMA_Fast": 150.2, "Signal_Strength": "High"},
            )
        ],
        drawings=[
            {
                "type": "marker",
                "time": "2024-01-01T10:00:00+00:00",
                "position": "belowBar",
                "shape": "arrowUp",
                "color": "#3fb950",
                "text": "SMA Cross",
            },
            {
                "type": "line",
                "start_time": "2024-01-01T10:00:00+00:00",
                "end_time": "2024-01-01T12:00:00+00:00",
                "val1": 101.5,
                "val2": 103.5,
                "color": "#58a6ff",
            },
        ],
    )
    result.save(backtests_dir / "meta-demo.trades.json")

    resp = viewer_client.get("/api/backtest/meta-demo/trade/1")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["trade"]["meta"] == {
        "SMA_Fast": 150.2,
        "Signal_Strength": "High",
    }
    assert len(payload["ohlcv"]) > 0
    assert payload["drawings"][0]["type"] == "marker"
    assert payload["drawings"][0]["time"] == pytest.approx(1704103200.0)
    assert payload["drawings"][1]["type"] == "line"
    assert payload["drawings"][1]["start_time"] == pytest.approx(1704103200.0)


def test_trade_detail_handles_invalid_entry_time(viewer_client, tmp_path):
    backtests_dir = tmp_path / "backtests"
    ohlcv_dir = tmp_path / "ohlcv" / "test_source" / "equities" / "AAPL"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    ohlcv_dir.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01T09:00:00Z", periods=8, freq="h", tz="UTC"),
        "open": [100, 101, 102, 103, 104, 105, 106, 107],
        "high": [101, 102, 103, 104, 105, 106, 107, 108],
        "low": [99, 100, 101, 102, 103, 104, 105, 106],
        "close": [100.5, 101.5, 102.5, 103.5, 104.5, 105.5, 106.5, 107.5],
        "volume": [10, 11, 12, 13, 14, 15, 16, 17],
    })
    df.to_parquet(ohlcv_dir / "1h.parquet", index=False)

    result = BacktestResult(
        meta=BacktestMeta(
            id="invalid-entry-time-demo",
            created_at="2026-03-07T15:00:00Z",
            engine="vectorbt",
            strategies=["Malformed Timestamp Strategy"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            initial_capital=100000.0,
            data={"timeframes": ["1h"], "symbols": ["AAPL"], "source": "test_source"},
        ),
        trades=[
            Trade(
                id=1,
                symbol="AAPL",
                direction="LONG",
                entry_time="not-a-timestamp",
                entry_price=101.5,
                quantity=1,
                exits=[
                    TradeExit(
                        time="2024-01-01T12:00:00+00:00",
                        price=103.5,
                        quantity=1,
                        pnl=2.0,
                    )
                ],
                pnl=2.0,
            )
        ],
    )
    result.save(backtests_dir / "invalid-entry-time-demo.trades.json")

    resp = viewer_client.get("/api/backtest/invalid-entry-time-demo/trade/1")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["symbol"] == "AAPL"
    assert isinstance(payload["ohlcv"], list)
    assert payload["trade"]["nr"] == 1


def test_backtest_replay_endpoint_returns_full_chart_payload(viewer_client, tmp_path):
    backtests_dir = tmp_path / "backtests"
    ohlcv_dir = tmp_path / "ohlcv" / "test_source" / "equities" / "AAPL"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    ohlcv_dir.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01T00:00:00Z", periods=6, freq="h", tz="UTC"),
        "open": [100, 101, 102, 103, 104, 105],
        "high": [101, 102, 103, 104, 105, 106],
        "low": [99, 100, 101, 102, 103, 104],
        "close": [100.5, 101.5, 102.5, 103.5, 104.5, 105.5],
        "volume": [10, 11, 12, 13, 14, 15],
    })
    df.to_parquet(ohlcv_dir / "1h.parquet", index=False)

    result = BacktestResult(
        meta=BacktestMeta(
            id="replay-demo",
            created_at="2026-03-07T15:00:00Z",
            engine="vectorbt",
            strategies=["Replay Demo"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-02",
            initial_capital=100000.0,
            data={
                "timeframes": ["1h"],
                "symbols": ["AAPL"],
                "universe": "",
                "source": "test_source",
            },
        ),
        trades=[
            Trade(
                id=1,
                symbol="AAPL",
                direction="LONG",
                entry_time="2024-01-01T01:00:00+00:00",
                entry_price=101.5,
                quantity=1,
                exits=[
                    TradeExit(
                        time="2024-01-01T04:00:00+00:00",
                        price=104.5,
                        quantity=1,
                        pnl=3.0,
                    )
                ],
                pnl=3.0,
            )
        ],
        indicators=[
            IndicatorSeries(
                name="supertrend_up",
                type="overlay",
                data=[
                    {"time": "2024-01-01T00:00:00+00:00", "value": None},
                    {"time": "2024-01-01T01:00:00+00:00", "value": 100.8},
                ],
                style={"color": "#22c55e"},
            )
        ],
        drawings=[
            {
                "type": "line_with_breaks",
                "symbol": "AAPL",
                "data": [
                    {"time": "2024-01-01T00:00:00+00:00", "value": None},
                    {"time": "2024-01-01T02:00:00+00:00", "value": 101.2},
                ],
            }
        ],
    )
    result.save(backtests_dir / "replay-demo.trades.json")

    resp = viewer_client.get("/api/backtest/replay-demo/replay")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total_trades"] == 1
    assert payload["primary_symbol"] == "AAPL"
    assert "AAPL" in payload["ohlcv_by_symbol"]
    assert payload["trades"][0]["nr"] == 1
    supertrend = next(ind for ind in payload["indicators"] if ind["name"] == "supertrend_up")
    assert supertrend["data"][0]["value"] is None
    assert payload["drawings"][0]["type"] == "line_with_breaks"


def test_backtest_replay_endpoint_limits_ohlcv_to_trade_windows(viewer_client, tmp_path):
    backtests_dir = tmp_path / "backtests"
    ohlcv_dir = tmp_path / "ohlcv" / "test_source" / "equities" / "AAPL"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    ohlcv_dir.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame({
        "timestamp": pd.date_range("2021-01-01T00:00:00Z", periods=16050, freq="h", tz="UTC"),
        "open": [100 + (idx * 0.01) for idx in range(16050)],
        "high": [101 + (idx * 0.01) for idx in range(16050)],
        "low": [99 + (idx * 0.01) for idx in range(16050)],
        "close": [100.5 + (idx * 0.01) for idx in range(16050)],
        "volume": [1000 + (idx % 100) for idx in range(16050)],
    })
    df.to_parquet(ohlcv_dir / "1h.parquet", index=False)

    first_entry = df["timestamp"].iloc[120]
    first_exit = df["timestamp"].iloc[160]
    second_entry = df["timestamp"].iloc[-220]
    second_exit = df["timestamp"].iloc[-180]
    midpoint = df["timestamp"].iloc[len(df) // 2]

    result = BacktestResult(
        meta=BacktestMeta(
            id="replay-window-demo",
            created_at="2026-03-07T15:00:00Z",
            engine="vectorbt",
            strategies=["Replay Window Demo"],
            backtest_start="2021-01-01",
            backtest_end="2024-12-31",
            initial_capital=100000.0,
            data={
                "timeframes": ["1h"],
                "symbols": ["AAPL"],
                "universe": "",
                "source": "test_source",
            },
        ),
        trades=[
            Trade(
                id=1,
                symbol="AAPL",
                direction="LONG",
                entry_time=first_entry.isoformat(),
                entry_price=101.5,
                quantity=1,
                exits=[
                    TradeExit(
                        time=first_exit.isoformat(),
                        price=104.5,
                        quantity=1,
                        pnl=3.0,
                    )
                ],
                pnl=3.0,
            ),
            Trade(
                id=2,
                symbol="AAPL",
                direction="LONG",
                entry_time=second_entry.isoformat(),
                entry_price=201.5,
                quantity=1,
                exits=[
                    TradeExit(
                        time=second_exit.isoformat(),
                        price=204.5,
                        quantity=1,
                        pnl=3.0,
                    )
                ],
                pnl=3.0,
            ),
        ],
        indicators=[
            IndicatorSeries(
                name="window_indicator",
                type="overlay",
                data=[
                    {"time": first_entry.isoformat(), "value": 100.0},
                    {"time": midpoint.isoformat(), "value": 150.0},
                    {"time": second_entry.isoformat(), "value": 200.0},
                ],
                style={"color": "#22c55e"},
            )
        ],
    )
    result.save(backtests_dir / "replay-window-demo.trades.json")

    resp = viewer_client.get("/api/backtest/replay-window-demo/replay")

    assert resp.status_code == 200
    payload = resp.json()
    bars = payload["ohlcv_by_symbol"]["AAPL"]
    bar_times = {round(float(bar["time"]), 6) for bar in bars}
    first_entry_unix = round(pd.Timestamp(first_entry).timestamp(), 6)
    second_entry_unix = round(pd.Timestamp(second_entry).timestamp(), 6)
    midpoint_unix = round(pd.Timestamp(midpoint).timestamp(), 6)

    assert len(bars) < 5000
    assert first_entry_unix in bar_times
    assert second_entry_unix in bar_times
    assert midpoint_unix not in bar_times

    indicator = next(ind for ind in payload["indicators"] if ind["name"] == "window_indicator")
    indicator_times = {round(float(point["time"]), 6) for point in indicator["data"]}
    assert first_entry_unix in indicator_times
    assert second_entry_unix in indicator_times
    assert midpoint_unix not in indicator_times


def test_data_overview_and_category_use_catalog_date_ranges(viewer_client, tmp_path):
    catalog = {
        "ohlcv": [
            {
                "path": str(tmp_path / "ohlcv" / "test_source" / "etf" / "SPY" / "1d.parquet"),
                "symbol": "SPY",
                "timeframe": "1d",
                "source": "test_source",
                "size_bytes": 1024,
                "start": "2024-01-02",
                "end": "2024-02-09",
            }
        ]
    }
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))

    overview_resp = viewer_client.get("/api/data/overview")

    assert overview_resp.status_code == 200
    categories = {
        category["name"]: category
        for category in overview_resp.json()["categories"]
    }
    assert categories["ETFs"]["start"] == "2024-01-02"
    assert categories["ETFs"]["end"] == "2024-02-09"

    details_resp = viewer_client.get("/api/data/category/ETFs")

    assert details_resp.status_code == 200
    assert details_resp.json()["symbols"] == [
        {
            "symbol": "SPY",
            "timeframe": "1d",
            "path": str(tmp_path / "ohlcv" / "test_source" / "etf" / "SPY" / "1d.parquet"),
            "size_mb": 0.0,
            "row_count": 0,
            "file_count": 1,
            "files": [str(tmp_path / "ohlcv" / "test_source" / "etf" / "SPY" / "1d.parquet")],
            "sources": ["test_source"],
            "start": "2024-01-02",
            "end": "2024-02-09",
            "source": "test_source",
        }
    ]


def test_data_category_strips_price_payload_fields(viewer_client, tmp_path):
    catalog = {
        "ohlcv": [
            {
                "path": str(tmp_path / "ohlcv" / "test_source" / "equity" / "US" / "AAPL" / "1d.parquet"),
                "symbol": "AAPL",
                "timeframe": "1d",
                "source": "test_source",
                "size_bytes": 1024,
                "start": "2024-03-01",
                "end": "2024-03-03",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1234,
            }
        ]
    }
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))

    details_resp = viewer_client.get("/api/data/category/Equities")

    assert details_resp.status_code == 200
    payload = details_resp.json()["symbols"][0]
    assert "open" not in payload
    assert "high" not in payload
    assert "low" not in payload
    assert "close" not in payload
    assert "volume" not in payload
    assert payload["symbol"] == "AAPL"
    assert payload["timeframe"] == "1d"


def test_data_endpoints_read_fast_date_ranges_without_catalog(viewer_client, tmp_path):
    ohlcv_dir = tmp_path / "ohlcv" / "test_source" / "equity" / "US" / "AAPL"
    ohlcv_dir.mkdir(parents=True)

    df = pd.DataFrame({
        "timestamp": pd.to_datetime(
            ["2024-03-01T09:30:00Z", "2024-03-02T09:30:00Z", "2024-03-03T09:30:00Z"],
            utc=True,
        ),
        "open": [100.0, 101.0, 102.0],
        "high": [101.0, 102.0, 103.0],
        "low": [99.0, 100.0, 101.0],
        "close": [100.5, 101.5, 102.5],
        "volume": [10, 11, 12],
    })
    df.to_parquet(ohlcv_dir / "1d.parquet", index=False)

    overview_resp = viewer_client.get("/api/data/overview")

    assert overview_resp.status_code == 200
    categories = {
        category["name"]: category
        for category in overview_resp.json()["categories"]
    }
    assert categories["Equities"]["start"] == "2024-03-01"
    assert categories["Equities"]["end"] == "2024-03-03"

    details_resp = viewer_client.get("/api/data/category/Equities")

    assert details_resp.status_code == 200
    assert details_resp.json()["symbols"] == [
        {
            "symbol": "AAPL",
            "timeframe": "1d",
            "path": str(ohlcv_dir / "1d.parquet"),
            "size_mb": pytest.approx((ohlcv_dir / "1d.parquet").stat().st_size / (1024 * 1024), rel=0, abs=0.01),
            "row_count": 3,
            "file_count": 1,
            "files": [str(ohlcv_dir / "1d.parquet")],
            "sources": ["test_source"],
            "start": "2024-03-01",
            "end": "2024-03-03",
            "source": "test_source",
        }
    ]


def test_data_endpoints_reuse_catalog_summary_for_unchanged_files(
    viewer_client, monkeypatch, tmp_path
):
    import viewer.main as viewer_app_module

    ohlcv_dir = tmp_path / "ohlcv" / "test_source" / "equity" / "US" / "AAPL"
    ohlcv_dir.mkdir(parents=True)

    df = pd.DataFrame({
        "timestamp": pd.to_datetime(
            ["2024-03-01T09:30:00Z", "2024-03-02T09:30:00Z", "2024-03-03T09:30:00Z"],
            utc=True,
        ),
        "open": [100.0, 101.0, 102.0],
        "high": [101.0, 102.0, 103.0],
        "low": [99.0, 100.0, 101.0],
        "close": [100.5, 101.5, 102.5],
        "volume": [10, 11, 12],
    })
    parquet_path = ohlcv_dir / "1d.parquet"
    df.to_parquet(parquet_path, index=False)

    cached_last_updated = pd.Timestamp.fromtimestamp(
        parquet_path.stat().st_mtime + 60, tz="UTC"
    ).isoformat()
    catalog = {
        "ohlcv": [
            {
                "path": str(parquet_path),
                "symbol": "AAPL",
                "timeframe": "1d",
                "source": "test_source",
                "size_bytes": parquet_path.stat().st_size,
                "rows": 3,
                "start": "2024-03-01",
                "end": "2024-03-03",
                "last_updated": cached_last_updated,
            }
        ]
    }
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))

    def fail_if_parquet_read(_: Path) -> dict[str, object]:
        raise AssertionError("_read_parquet_summary should not be called for unchanged files")

    monkeypatch.setattr(viewer_app_module, "_read_parquet_summary", fail_if_parquet_read)

    overview_resp = viewer_client.get("/api/data/overview")

    assert overview_resp.status_code == 200
    categories = {
        category["name"]: category
        for category in overview_resp.json()["categories"]
    }
    assert categories["Equities"]["start"] == "2024-03-01"
    assert categories["Equities"]["end"] == "2024-03-03"

    details_resp = viewer_client.get("/api/data/category/Equities")

    assert details_resp.status_code == 200
    assert details_resp.json()["symbols"] == [
        {
            "symbol": "AAPL",
            "timeframe": "1d",
            "path": str(parquet_path),
            "size_mb": pytest.approx(parquet_path.stat().st_size / (1024 * 1024), rel=0, abs=0.01),
            "row_count": 3,
            "file_count": 1,
            "files": [str(parquet_path)],
            "sources": ["test_source"],
            "start": "2024-03-01",
            "end": "2024-03-03",
            "source": "test_source",
            "last_updated": cached_last_updated,
        }
    ]


def test_data_endpoints_group_segmented_catalog_entries(viewer_client, tmp_path):
    catalog = {
        "ohlcv": [
            {
                "path": str(tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "tick_2024_01.parquet"),
                "symbol": "EURUSD",
                "timeframe": "tick",
                "source": "dukascopy",
                "size_bytes": 1024,
                "start": "2024-01-01",
                "end": "2024-01-31",
                "segment_count": 2,
                "segments": [
                    {
                        "path": str(
                            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "tick_2024_01.parquet"
                        ),
                        "segment": "2024_01",
                        "start": "2024-01-01",
                        "end": "2024-01-31",
                        "rows": 100,
                        "size_bytes": 1024,
                    },
                    {
                        "path": str(
                            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "tick_2024_02.parquet"
                        ),
                        "segment": "2024_02",
                        "start": "2024-02-01",
                        "end": "2024-02-29",
                        "rows": 120,
                        "size_bytes": 2048,
                    },
                ],
            }
        ]
    }
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))

    overview_resp = viewer_client.get("/api/data/overview")

    assert overview_resp.status_code == 200
    categories = {
        category["name"]: category
        for category in overview_resp.json()["categories"]
    }
    assert categories["Forex"]["symbol_count"] == 1
    assert categories["Forex"]["file_count"] == 1
    assert categories["Forex"]["start"] == "2024-01-01"
    assert categories["Forex"]["end"] == "2024-02-29"

    details_resp = viewer_client.get("/api/data/category/Forex")

    assert details_resp.status_code == 200
    symbols = details_resp.json()["symbols"]
    assert len(symbols) == 1
    assert symbols[0]["symbol"] == "EURUSD"
    assert symbols[0]["timeframe"] == "tick"
    assert symbols[0]["start"] == "2024-01-01"
    assert symbols[0]["end"] == "2024-02-29"
    assert symbols[0]["source"] == "dukascopy"
    assert symbols[0]["segment_count"] == 2
    assert [segment["segment"] for segment in symbols[0]["segments"]] == ["2024_01", "2024_02"]


def test_data_endpoints_merge_same_symbol_timeframe_across_sources(viewer_client, tmp_path):
    catalog = {
        "ohlcv": [
            {
                "path": str(tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "tick_2024_01.parquet"),
                "symbol": "EURUSD",
                "timeframe": "tick",
                "source": "dukascopy",
                "asset_class": "forex",
                "size_bytes": 1024,
                "start": "2024-01-01",
                "end": "2024-01-31",
                "segment_count": 1,
                "segments": [
                    {
                        "path": str(
                            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "tick_2024_01.parquet"
                        ),
                        "segment": "2024_01",
                        "start": "2024-01-01",
                        "end": "2024-01-31",
                        "rows": 100,
                        "size_bytes": 1024,
                    }
                ],
            },
            {
                "path": str(tmp_path / "ohlcv" / "histdata" / "forex" / "EURUSD" / "tick_2024_02.parquet"),
                "symbol": "EURUSD",
                "timeframe": "tick",
                "source": "histdata",
                "asset_class": "forex",
                "size_bytes": 2048,
                "start": "2024-02-01",
                "end": "2024-02-29",
                "segment_count": 1,
                "segments": [
                    {
                        "path": str(
                            tmp_path / "ohlcv" / "histdata" / "forex" / "EURUSD" / "tick_2024_02.parquet"
                        ),
                        "segment": "2024_02",
                        "start": "2024-02-01",
                        "end": "2024-02-29",
                        "rows": 120,
                        "size_bytes": 2048,
                    }
                ],
            },
        ]
    }
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))

    overview_resp = viewer_client.get("/api/data/overview")
    assert overview_resp.status_code == 200
    categories = {
        category["name"]: category
        for category in overview_resp.json()["categories"]
    }
    assert categories["Forex"]["symbol_count"] == 1
    assert categories["Forex"]["file_count"] == 1
    assert categories["Forex"]["start"] == "2024-01-01"
    assert categories["Forex"]["end"] == "2024-02-29"

    details_resp = viewer_client.get("/api/data/category/Forex")
    assert details_resp.status_code == 200
    symbols = details_resp.json()["symbols"]
    assert len(symbols) == 1

    merged = symbols[0]
    assert merged["symbol"] == "EURUSD"
    assert merged["timeframe"] == "tick"
    assert merged["source"] == "mixed"
    assert merged["start"] == "2024-01-01"
    assert merged["end"] == "2024-02-29"
    assert merged["segment_count"] == 2
    assert {segment["segment"] for segment in merged["segments"]} == {"2024_01", "2024_02"}


def test_data_endpoints_group_symbol_prefixed_segmented_files(viewer_client, tmp_path):
    symbol_dir = tmp_path / "ohlcv" / "dukascopy" / "forex"
    symbol_dir.mkdir(parents=True)

    jan = pd.DataFrame({
        "timestamp": pd.to_datetime(
            ["2024-01-01T00:00:00Z", "2024-01-31T23:59:59Z"],
            utc=True,
        ),
        "bid": [2000.1, 2010.2],
        "ask": [2000.3, 2010.4],
    })
    feb = pd.DataFrame({
        "timestamp": pd.to_datetime(
            ["2024-02-01T00:00:00Z", "2024-02-29T23:59:59Z"],
            utc=True,
        ),
        "bid": [2020.1, 2030.2],
        "ask": [2020.3, 2030.4],
    })

    jan_path = symbol_dir / "XAUUSD_tick_2024_01.parquet"
    feb_path = symbol_dir / "XAUUSD_tick_2024_02.parquet"
    jan.to_parquet(jan_path, index=False)
    feb.to_parquet(feb_path, index=False)

    overview_resp = viewer_client.get("/api/data/overview")
    assert overview_resp.status_code == 200
    categories = {
        category["name"]: category
        for category in overview_resp.json()["categories"]
    }
    assert categories["Forex"]["symbol_count"] == 1
    assert categories["Forex"]["file_count"] == 1
    assert categories["Forex"]["start"] == "2024-01-01"
    assert categories["Forex"]["end"] == "2024-02-29"

    details_resp = viewer_client.get("/api/data/category/Forex")
    assert details_resp.status_code == 200
    symbols = details_resp.json()["symbols"]
    assert len(symbols) == 1
    assert symbols[0]["symbol"] == "XAUUSD"
    assert symbols[0]["timeframe"] == "tick"
    assert symbols[0]["source"] == "dukascopy"
    assert symbols[0]["segment_count"] == 2
    assert symbols[0]["start"] == "2024-01-01"
    assert symbols[0]["end"] == "2024-02-29"
    assert [Path(segment["path"]).name for segment in symbols[0]["segments"]] == [
        "XAUUSD_tick_2024_01.parquet",
        "XAUUSD_tick_2024_02.parquet",
    ]
    assert [segment["segment"] for segment in symbols[0]["segments"]] == ["2024_01", "2024_02"]


def test_data_category_includes_warning_message_from_catalog(viewer_client, tmp_path):
    catalog = {
        "ohlcv": [
            {
                "path": str(tmp_path / "ohlcv" / "dolthub" / "equity" / "US" / "MSFT" / "1d.parquet"),
                "symbol": "MSFT",
                "timeframe": "1d",
                "source": "dolthub",
                "size_bytes": 2048,
                "start": "2024-04-01",
                "end": "2024-04-02",
                "validation": {
                    "status": "warning",
                    "warnings": ["2 duplicate timestamps"],
                    "gaps": 3,
                },
            }
        ]
    }
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))

    resp = viewer_client.get("/api/data/category/Equities")

    assert resp.status_code == 200
    assert resp.json()["symbols"] == [
        {
            "symbol": "MSFT",
            "timeframe": "1d",
            "path": str(tmp_path / "ohlcv" / "dolthub" / "equity" / "US" / "MSFT" / "1d.parquet"),
            "size_mb": 0.0,
            "row_count": 0,
            "file_count": 1,
            "files": [str(tmp_path / "ohlcv" / "dolthub" / "equity" / "US" / "MSFT" / "1d.parquet")],
            "sources": ["dolthub"],
            "start": "2024-04-01",
            "end": "2024-04-02",
            "source": "dolthub",
            "validation": {
                "status": "warning",
                "warnings": ["2 duplicate timestamps"],
                "gaps": 3,
            },
            "warning_message": "2 duplicate timestamps | 3 data gaps detected",
            "validation_message": "2 duplicate timestamps | 3 data gaps detected",
        }
    ]


def test_dolthub_exports_appear_as_equities(viewer_client, tmp_path):
    ohlcv_dir = tmp_path / "ohlcv" / "dolthub" / "equity" / "US"
    ohlcv_dir.mkdir(parents=True)

    df = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-04-01", "2024-04-02"], utc=True),
        "open": [150.0, 151.0],
        "high": [151.0, 152.0],
        "low": [149.0, 150.0],
        "close": [150.5, 151.5],
        "volume": [1000, 1100],
    })
    df.to_parquet(ohlcv_dir / "MSFT.parquet", index=False)

    overview_resp = viewer_client.get("/api/data/overview")

    assert overview_resp.status_code == 200
    categories = {
        category["name"]: category
        for category in overview_resp.json()["categories"]
    }
    assert categories["Equities"]["symbol_count"] == 1
    assert categories["Equities"]["start"] == "2024-04-01"
    assert categories["Equities"]["end"] == "2024-04-02"

    details_resp = viewer_client.get("/api/data/category/Equities")

    assert details_resp.status_code == 200
    assert details_resp.json()["symbols"] == [
        {
            "symbol": "MSFT",
            "timeframe": "1d",
            "path": str(ohlcv_dir / "MSFT.parquet"),
            "size_mb": pytest.approx((ohlcv_dir / "MSFT.parquet").stat().st_size / (1024 * 1024), rel=0, abs=0.01),
            "row_count": 2,
            "file_count": 1,
            "files": [str(ohlcv_dir / "MSFT.parquet")],
            "sources": ["dolthub"],
            "start": "2024-04-01",
            "end": "2024-04-02",
            "source": "dolthub",
        }
    ]


def test_dolthub_repo_entries_are_marked_ok(viewer_client, tmp_path):
    repo_dir = tmp_path / "dolthub" / "stocks"
    repo_dir.mkdir(parents=True)

    resp = viewer_client.get("/api/data/category/Dolthub%20Repos")

    assert resp.status_code == 200
    symbols = resp.json()["symbols"]
    assert len(symbols) == 1
    assert symbols[0]["symbol"] == "stocks"
    assert symbols[0]["timeframe"] == "repo"
    assert symbols[0]["path"] == str(repo_dir)
    assert symbols[0]["size_mb"] == 0.0
    assert symbols[0]["start"] is None
    assert symbols[0]["end"] is None
    assert symbols[0]["source"] == "dolthub"
    assert symbols[0]["repo"] is True
    assert symbols[0]["last_updated"] is not None
    assert symbols[0]["validation"] == {"status": "ok"}
    assert symbols[0]["warning_message"] == ""
    assert symbols[0]["validation_message"] == ""


def test_data_download_passes_price_type_and_merge_flags(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    captured: dict[str, object] = {}

    def fake_start(task_id: str, cmd: list[str], **kwargs) -> dict:
        captured["task_id"] = task_id
        captured["cmd"] = cmd
        captured.update(kwargs)
        return {"task_id": task_id, "status": "starting"}

    monkeypatch.setattr(viewer_app_module, "_start_jforex_task", fake_start)

    resp = viewer_client.post(
        "/api/data/download",
        json={
            "source": "dukascopy",
            "symbols": "EURUSD",
            "timeframes": "tick,sec,m1",
            "price_type": "ask",
            "merge_tick": True,
            "merge_s1": True,
            "batch_size": 7,
            "pause_ms": 2500,
        },
    )

    assert resp.status_code == 200
    assert captured["cmd"].count("--price-type") == 1
    assert "data_tools.downloaders.jforex_dl" in captured["cmd"]
    assert "ask" in captured["cmd"]
    assert "--query-timeout-seconds" in captured["cmd"]
    assert captured["source_label"] == "dukascopy"
    assert captured["allow_native_fallback"] is True
    assert captured["total_items"] == 3
    assert captured["symbols"] == ["EURUSD"]
    assert captured["timeframes"] == ["tick", "s1", "m1"]
    assert captured["batch_size"] == 7
    assert captured["pause_ms"] == 2500


def test_data_download_coalesces_identical_active_request(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    starts: list[str] = []
    real_thread = threading.Thread

    class _NoStartThread:
        def __init__(self, *args, **kwargs):
            self.target = kwargs.get("target")
            intercept = False
            for frame_info in traceback.extract_stack():
                if frame_info.filename.endswith("viewer/core.py") and frame_info.name in {
                    "_start_data_task",
                    "_start_dukascopy_task",
                    "_start_jforex_task",
                }:
                    intercept = True
                    break
            if not intercept:
                self._real = real_thread(*args, **kwargs)

        def start(self) -> None:
            if hasattr(self, "_real"):
                self._real.start()
                return
            starts.append("started")

    monkeypatch.setattr(viewer_app_module.legacy_viewer.threading, "Thread", _NoStartThread)
    payload = {
        "source": "dukascopy",
        "symbols": "EURUSD",
        "timeframes": "m1",
        "start": "2026-01-01",
        "end": "2026-01-02",
        "price_type": "bid",
    }

    first = viewer_client.post("/api/data/download", json=payload)
    second = viewer_client.post(
        "/api/data/download",
        json={**payload, "symbols": " eurusd ", "timeframes": "M1"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(starts) == 1
    assert second.json() == {
        "task_id": first.json()["task_id"],
        "status": "starting",
        "coalesced": True,
    }


def test_data_download_does_not_apply_recovery_metadata_to_coalesced_registration(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    class _NoStartThread:
        def __init__(self, *args, **kwargs):
            self.target = kwargs.get("target")

        def start(self) -> None:
            # Recovery metadata is tested without launching a worker thread.
            pass

    recovery_task_ids: list[str] = []
    monkeypatch.setattr(viewer_app_module.legacy_viewer.threading, "Thread", _NoStartThread)
    monkeypatch.setattr(
        viewer_app_module,
        "_apply_recovery_data_metadata",
        lambda task_id, **kwargs: recovery_task_ids.append(task_id),
    )
    payload = {
        "source": "yfinance",
        "symbols": "AAPL",
        "timeframes": "m1",
        "start": "2026-01-01",
        "end": "2026-01-02",
    }

    first = viewer_client.post("/api/data/download", json=payload)
    second = viewer_client.post("/api/data/download", json=payload)

    assert first.status_code == second.status_code == 200
    assert second.json()["coalesced"] is True
    assert second.json()["task_id"] == first.json()["task_id"]
    assert recovery_task_ids == [first.json()["task_id"]]


def test_data_download_coalesces_equivalent_timeframe_aliases(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    starts = 0

    class _NoStartThread:
        def __init__(self, *args, **kwargs):
            self.target = kwargs.get("target")

        def start(self) -> None:
            nonlocal starts
            starts += 1

    monkeypatch.setattr(viewer_app_module.legacy_viewer.threading, "Thread", _NoStartThread)
    payload = {
        "source": "yfinance",
        "symbols": "AAPL",
        "start": "2026-01-01",
        "end": "2026-01-02",
    }

    first = viewer_client.post(
        "/api/data/download", json={**payload, "timeframes": "1m"}
    )
    second = viewer_client.post(
        "/api/data/download", json={**payload, "timeframes": "m1"}
    )

    assert first.status_code == second.status_code == 200
    assert starts == 1
    assert second.json()["task_id"] == first.json()["task_id"]
    assert second.json()["coalesced"] is True


def test_download_registry_coalesces_concurrent_identical_requests(viewer_client):
    import viewer.main as viewer_app_module

    request_key = "concurrent-request"
    barrier = threading.Barrier(2)
    results: list[dict | None] = []
    results_lock = threading.Lock()

    def register(index: int) -> None:
        barrier.wait()
        record = {"id": f"task-{index:03d}", "status": "starting"}
        existing = viewer_app_module.legacy_viewer._register_download_task(
            record, request_key
        )
        with results_lock:
            results.append(existing)

    workers = [threading.Thread(target=register, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert sum(result is None for result in results) == 1
    assert sum(result is not None for result in results) == 1
    assert len(viewer_app_module.legacy_viewer.DATA_TASKS) == 1


def test_singleflight_starter_failure_leaves_failed_record_for_coalesced_id(
    viewer_client,
):
    import viewer.main as viewer_app_module

    entered = threading.Event()
    release = threading.Event()
    starter_errors: list[BaseException] = []

    def failing_starter(task_id: str) -> dict:
        entered.set()
        assert release.wait(timeout=2)
        raise RuntimeError("worker launch failed")

    def launch() -> None:
        try:
            viewer_app_module.legacy_viewer._start_singleflight_download(
                "task-primary", "same-request", failing_starter
            )
        except RuntimeError as exc:  # pragma: no cover - asserted below
            starter_errors.append(exc)

    worker = threading.Thread(target=launch)
    worker.start()
    assert entered.wait(timeout=2)

    coalesced = viewer_app_module.legacy_viewer._start_singleflight_download(
        "task-coalesced",
        "same-request",
        lambda _task_id: {"task_id": _task_id, "status": "running"},
    )
    assert coalesced == {
        "task_id": "task-primary",
        "status": "starting",
        "coalesced": True,
    }

    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert [str(error) for error in starter_errors] == ["worker launch failed"]

    failed = viewer_app_module.legacy_viewer.DATA_TASKS["task-primary"]
    assert failed["status"] == "failed"
    assert failed["error"] == "worker launch failed"
    assert "download_request_key" not in failed

    retry = viewer_app_module.legacy_viewer._start_singleflight_download(
        "task-retry",
        "same-request",
        lambda task_id: {"task_id": task_id, "status": "running"},
    )
    assert retry == {"task_id": "task-retry", "status": "running"}


def test_data_download_does_not_coalesce_different_options_or_finished_task(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    starts: list[str] = []

    class _NoStartThread:
        def __init__(self, *args, **kwargs):
            # This test double intentionally ignores Thread construction arguments.
            pass

        def start(self) -> None:
            starts.append("started")

    monkeypatch.setattr(viewer_app_module.legacy_viewer.threading, "Thread", _NoStartThread)
    payload = {
        "source": "dukascopy",
        "symbols": "EURUSD",
        "timeframes": "m1",
        "start": "2026-01-01",
        "end": "2026-01-02",
        "price_type": "bid",
    }

    first = viewer_client.post("/api/data/download", json=payload)
    different = viewer_client.post(
        "/api/data/download", json={**payload, "price_type": "ask"}
    )
    viewer_app_module._mark_task_finished(
        first.json()["task_id"], status="completed", returncode=0
    )
    retried = viewer_client.post("/api/data/download", json=payload)

    assert first.status_code == different.status_code == retried.status_code == 200
    assert len(starts) == 3
    assert different.json()["task_id"] != first.json()["task_id"]
    assert retried.json()["task_id"] != first.json()["task_id"]
    assert "coalesced" not in different.json()
    assert "coalesced" not in retried.json()


@pytest.mark.parametrize(
    ("source", "irrelevant_options"),
    [
        (
            "yfinance",
            {
                "price_type": "ask",
                "merge_tick": True,
                "batch_size": 23,
                "pause_ms": 4000,
                "query_timeout_seconds": 1200,
            },
        ),
    ],
)
def test_data_download_coalesces_options_irrelevant_to_source(
    viewer_client, monkeypatch, source, irrelevant_options
):
    import viewer.main as viewer_app_module

    starts = 0

    class _NoStartThread:
        def __init__(self, *args, **kwargs):
            # This test double intentionally ignores Thread construction arguments.
            pass

        def start(self) -> None:
            nonlocal starts
            starts += 1

    monkeypatch.setattr(viewer_app_module.legacy_viewer.threading, "Thread", _NoStartThread)
    payload = {
        "source": source,
        "symbols": "EURUSD",
        "timeframes": "m1",
        "start": "2026-01-01",
        "end": "2026-01-02",
    }

    first = viewer_client.post("/api/data/download", json=payload)
    second = viewer_client.post(
        "/api/data/download", json={**payload, **irrelevant_options}
    )

    assert first.status_code == second.status_code == 200
    assert starts == 1
    assert second.json()["task_id"] == first.json()["task_id"]
    assert second.json()["coalesced"] is True


@pytest.mark.parametrize(
    ("option", "different_value"),
    [
        ("offer_side", "ask"),
        ("merge_tick", True),
        ("merge_s1", True),
        ("batch_size", 23),
        ("pause_ms", 4000),
        ("query_timeout_seconds", 1200),
    ],
)
def test_jforex_request_key_includes_execution_options(option, different_value):
    import viewer.main as viewer_app_module

    identity = {
        "source": "jforex",
        "symbols": ["EURUSD"],
        "timeframes": ["m1"],
        "start": "2026-01-01",
        "end": "2026-01-02",
        "offer_side": "bid",
        "merge_tick": False,
        "merge_s1": False,
        "batch_size": 10,
        "pause_ms": 1000,
        "query_timeout_seconds": 900,
    }

    baseline_key = viewer_app_module._download_request_key_for_source(identity)
    different_key = viewer_app_module._download_request_key_for_source(
        {**identity, option: different_value}
    )

    assert different_key != baseline_key


def test_data_download_coalesces_dolthub_requests_with_unused_fields(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    starts = 0

    class _NoStartThread:
        def __init__(self, *args, **kwargs):
            # This test double intentionally ignores Thread construction arguments.
            pass

        def start(self) -> None:
            nonlocal starts
            starts += 1

    monkeypatch.setattr(viewer_app_module.legacy_viewer.threading, "Thread", _NoStartThread)

    first = viewer_client.post(
        "/api/data/download",
        json={
            "source": "dolthub",
            "symbols": "AAPL",
            "timeframes": "1d",
            "start": "2025-01-01",
            "end": "2025-01-31",
        },
    )
    second = viewer_client.post(
        "/api/data/download",
        json={
            "source": "dolthub",
            "symbols": "MSFT",
            "timeframes": "1h",
            "start": "2026-06-01",
            "end": "2026-06-30",
        },
    )

    assert first.status_code == second.status_code == 200
    assert starts == 1
    assert second.json()["task_id"] == first.json()["task_id"]
    assert second.json()["coalesced"] is True


def test_data_download_thread_start_failure_allows_retry(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    starts = 0
    real_thread = threading.Thread

    class _FailThenStartThread:
        def __init__(self, *args, **kwargs):
            # Only intercept threads launched by the viewer task machinery; let
            # other libraries (e.g. sentry) create their own real threads.
            intercept = False
            for frame_info in traceback.extract_stack():
                if frame_info.filename.endswith("viewer/core.py") and frame_info.name in {
                    "_start_data_task",
                    "_start_dukascopy_task",
                    "_start_jforex_task",
                }:
                    intercept = True
                    break
            if intercept:
                self._intercepted = True
            else:
                self._intercepted = False
                self._real = real_thread(*args, **kwargs)

        def start(self) -> None:
            nonlocal starts
            if not self._intercepted:
                self._real.start()
                return
            starts += 1
            if starts == 1:
                raise RuntimeError("thread unavailable")

    monkeypatch.setattr(
        viewer_app_module.legacy_viewer.threading, "Thread", _FailThenStartThread
    )
    payload = {
        "source": "dukascopy",
        "symbols": "EURUSD",
        "timeframes": "m1",
        "start": "2026-01-01",
        "end": "2026-01-02",
    }

    with pytest.raises(RuntimeError, match="thread unavailable"):
        viewer_client.post("/api/data/download", json=payload)
    retry = viewer_client.post("/api/data/download", json=payload)

    assert retry.status_code == 200
    assert starts == 2
    assert "coalesced" not in retry.json()


def test_data_download_thread_construction_failure_allows_retry(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    constructions = 0
    real_thread = threading.Thread

    class _FailThenConstructThread:
        def __init__(self, *args, **kwargs):
            nonlocal constructions
            intercept = False
            for frame_info in traceback.extract_stack():
                if frame_info.filename.endswith("viewer/core.py") and frame_info.name in {
                    "_start_data_task",
                    "_start_dukascopy_task",
                    "_start_jforex_task",
                }:
                    intercept = True
                    break
            if not intercept:
                self._real = real_thread(*args, **kwargs)
                return
            constructions += 1
            if constructions == 1:
                raise ValueError("thread construction unavailable")

        def start(self) -> None:
            # A successful retry only needs to return without launching a real thread.
            if hasattr(self, "_real"):
                self._real.start()

    monkeypatch.setattr(
        viewer_app_module.legacy_viewer.threading, "Thread", _FailThenConstructThread
    )
    payload = {
        "source": "yfinance",
        "symbols": "AAPL",
        "timeframes": "1d",
        "start": "2026-01-01",
        "end": "2026-01-02",
    }

    with pytest.raises(ValueError, match="thread construction unavailable"):
        viewer_client.post("/api/data/download", json=payload)
    retry = viewer_client.post("/api/data/download", json=payload)

    assert retry.status_code == 200
    assert constructions == 2
    assert "coalesced" not in retry.json()


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_data_download_releases_request_key_when_task_finishes(
    viewer_client, monkeypatch, status
):
    import viewer.main as viewer_app_module

    class _NoStartThread:
        def __init__(self, *args, **kwargs):
            # This test double intentionally ignores Thread construction arguments.
            pass

        def start(self) -> None:
            # The lifecycle transition is driven explicitly by the test.
            pass

    monkeypatch.setattr(viewer_app_module.legacy_viewer.threading, "Thread", _NoStartThread)
    payload = {
        "source": "dukascopy",
        "symbols": "EURUSD",
        "timeframes": "m1",
        "start": "2026-01-01",
        "end": "2026-01-02",
    }
    response = viewer_client.post("/api/data/download", json=payload)
    task_id = response.json()["task_id"]

    assert "download_request_key" in viewer_app_module.DATA_TASKS[task_id]

    viewer_app_module._mark_task_finished(
        task_id,
        status=status,
        returncode=0 if status == "completed" else 1,
    )

    assert "download_request_key" not in viewer_app_module.DATA_TASKS[task_id]

    retry = viewer_client.post("/api/data/download", json=payload)

    assert retry.status_code == 200
    assert retry.json()["task_id"] != task_id
    assert "coalesced" not in retry.json()


def test_data_download_discards_persisted_record_for_coalesced_request(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    persisted: set[str] = set()
    deleted: set[str] = set()

    class _NoStartThread:
        def __init__(self, *args, **kwargs):
            # This test double intentionally ignores Thread construction arguments.
            pass

        def start(self) -> None:
            # Persistence behavior is tested without launching a worker thread.
            pass

    monkeypatch.setattr(viewer_app_module.legacy_viewer.threading, "Thread", _NoStartThread)
    monkeypatch.setattr(
        viewer_app_module,
        "_persist_task_snapshot",
        lambda task_id, _task: persisted.add(task_id),
    )
    monkeypatch.setattr(
        viewer_app_module,
        "delete_background_task_record",
        lambda task_id: deleted.add(task_id) or True,
    )
    payload = {
        "source": "dukascopy",
        "symbols": "EURUSD",
        "timeframes": "m1",
        "start": "2026-01-01",
        "end": "2026-01-02",
    }

    first = viewer_client.post("/api/data/download", json=payload)
    second = viewer_client.post("/api/data/download", json=payload)

    first_id = first.json()["task_id"]
    assert second.json()["task_id"] == first_id
    assert persisted == {first_id}
    assert len(deleted) == 1
    assert first_id not in deleted
    assert deleted.isdisjoint(persisted)


def test_coalesced_cleanup_state_updates_are_serialized(tmp_path, monkeypatch):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(viewer_app_module, "DATA_DIR", tmp_path)
    first_write_started = threading.Event()
    release_first_write = threading.Event()
    write_calls: list[dict] = []
    real_write = viewer_app_module._write_coalesced_cleanup_state

    def blocking_write(state):
        write_calls.append(dict(state))
        if len(write_calls) == 1:
            first_write_started.set()
            assert release_first_write.wait(timeout=2)
        real_write(state)

    monkeypatch.setattr(viewer_app_module, "_write_coalesced_cleanup_state", blocking_write)
    first = threading.Thread(
        target=viewer_app_module._update_coalesced_cleanup_state,
        args=("cleanup-0001",),
        kwargs={"attempts": 1, "exhausted": False},
    )
    second = threading.Thread(
        target=viewer_app_module._update_coalesced_cleanup_state,
        args=("cleanup-0002",),
        kwargs={"attempts": 1, "exhausted": False},
    )
    first.start()
    assert first_write_started.wait(timeout=2)
    second.start()
    time.sleep(0.05)
    assert len(write_calls) == 1
    release_first_write.set()
    first.join(timeout=2)
    second.join(timeout=2)

    state = viewer_app_module._load_coalesced_cleanup_state()
    assert set(state) == {"cleanup-0001", "cleanup-0002"}


def test_coalesced_cleanup_failure_persists_retryable_marker(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    persisted_markers: list[dict] = []
    delete_attempts: list[str] = []

    with viewer_app_module.DATA_TASKS_LOCK:
        viewer_app_module.DATA_TASKS["task-provisional"] = {
            "id": "task-provisional",
            "kind": "data",
            "source": "yfinance",
            "status": "starting",
        }

    def fail_delete(task_id: str) -> bool:
        delete_attempts.append(task_id)
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(viewer_app_module, "delete_background_task_record", fail_delete)
    monkeypatch.setattr(
        viewer_app_module,
        "upsert_background_task_record",
        lambda **record: persisted_markers.append(record),
    )

    viewer_app_module._discard_coalesced_download_registration("task-provisional")

    assert delete_attempts == ["task-provisional"]
    assert "task-provisional" not in viewer_app_module.DATA_TASKS
    assert persisted_markers == [
        {
            "task_id": "task-provisional",
            "task_type": "data",
            "status": "cleanup_pending",
            "config_json": {
                "coalesced_cleanup_pending": True,
                "coalesced_cleanup_attempts": 1,
            },
            "progress_message": "Pending cleanup of coalesced provisional task",
            "source": "yfinance",
        }
    ]

    monkeypatch.setattr(
        viewer_app_module,
        "list_background_task_records",
        lambda: [
            {
                "id": "task-provisional",
                "type": "data",
                "status": "cleanup_pending",
                "config_json": {"coalesced_cleanup_pending": True},
                "source": "yfinance",
            }
        ],
    )
    monkeypatch.setattr(
        viewer_app_module,
        "delete_background_task_record",
        lambda task_id: delete_attempts.append(task_id) or True,
    )
    with viewer_app_module.DATA_TASKS_LOCK:
        viewer_app_module.DATA_TASKS["task-provisional"] = {
            "id": "task-provisional",
            "kind": "data",
            "source": "yfinance",
            "status": "running",
        }

    viewer_app_module._hydrate_task_cache_once(force=True)

    assert delete_attempts == ["task-provisional", "task-provisional"]
    assert "task-provisional" not in viewer_app_module.DATA_TASKS


def test_coalesced_cleanup_marker_failure_retries_from_memory(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    task_id = "task-provisional"
    with viewer_app_module.DATA_TASKS_LOCK:
        viewer_app_module.DATA_TASKS[task_id] = {
            "id": task_id,
            "kind": "data",
            "source": "yfinance",
            "status": "starting",
        }

    delete_attempts: list[str] = []

    def fail_delete(value: str) -> bool:
        delete_attempts.append(value)
        raise RuntimeError("database unavailable")

    def fail_upsert(**_record) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(viewer_app_module, "delete_background_task_record", fail_delete)
    monkeypatch.setattr(
        viewer_app_module,
        "upsert_background_task_record",
        fail_upsert,
    )

    viewer_app_module._discard_coalesced_download_registration(task_id)

    assert delete_attempts == [task_id]
    assert task_id not in viewer_app_module.DATA_TASKS
    assert task_id in viewer_app_module._COALESCED_CLEANUP_PENDING

    monkeypatch.setattr(
        viewer_app_module,
        "list_background_task_records",
        lambda: [
            {
                "id": task_id,
                "type": "data",
                "status": "starting",
                "config_json": {},
                "source": "yfinance",
            }
        ],
    )
    monkeypatch.setattr(
        viewer_app_module,
        "delete_background_task_record",
        lambda value: delete_attempts.append(value) or True,
    )
    viewer_app_module._COALESCED_CLEANUP_PENDING.clear()
    viewer_app_module._COALESCED_CLEANUP_ORDER.clear()
    viewer_app_module._COALESCED_CLEANUP_ATTEMPTS.clear()

    viewer_app_module._hydrate_task_cache_once(force=True)

    assert delete_attempts == [task_id, task_id]
    assert task_id not in viewer_app_module.DATA_TASKS
    assert task_id not in viewer_app_module._COALESCED_CLEANUP_PENDING


def test_coalesced_cleanup_exhaustion_is_terminal_across_restart(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    task_id = "task-exhausted-cleanup"
    delete_attempts: list[str] = []
    persisted: list[dict] = []

    def fail_delete(value: str) -> bool:
        delete_attempts.append(value)
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(viewer_app_module, "delete_background_task_record", fail_delete)
    monkeypatch.setattr(
        viewer_app_module,
        "upsert_background_task_record",
        lambda **record: persisted.append(record),
    )

    viewer_app_module._discard_coalesced_download_registration(task_id)
    for _ in range(viewer_app_module._COALESCED_CLEANUP_MAX_ATTEMPTS - 1):
        viewer_app_module._retry_coalesced_cleanup(task_id)

    terminal = [record for record in persisted if record.get("status") == "cleanup_exhausted"]
    assert terminal
    assert terminal[-1]["config_json"]["coalesced_cleanup_exhausted"] is True
    assert task_id in viewer_app_module._COALESCED_CLEANUP_EXHAUSTED

    with viewer_app_module.DATA_TASKS_LOCK:
        viewer_app_module.DATA_TASKS.clear()
    viewer_app_module._COALESCED_CLEANUP_PENDING.clear()
    viewer_app_module._COALESCED_CLEANUP_ORDER.clear()
    viewer_app_module._COALESCED_CLEANUP_ATTEMPTS.clear()
    viewer_app_module._COALESCED_CLEANUP_EXHAUSTED.clear()
    delete_attempts.clear()
    monkeypatch.setattr(
        viewer_app_module,
        "list_background_task_records",
        lambda: [
            {
                "id": task_id,
                "type": "data",
                "status": "cleanup_pending",
                "config_json": {"coalesced_cleanup_pending": True},
            }
        ],
    )

    viewer_app_module._hydrate_task_cache_once(force=True)

    assert delete_attempts == []
    assert task_id not in viewer_app_module.DATA_TASKS


def test_coalesced_cleanup_exhausted_memory_is_bounded(monkeypatch):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(viewer_app_module, "_COALESCED_CLEANUP_MAX_ENTRIES", 2)
    viewer_app_module._COALESCED_CLEANUP_EXHAUSTED.clear()
    for index in range(3):
        viewer_app_module._remember_coalesced_cleanup_exhausted(f"exhausted-{index}")

    assert len(viewer_app_module._COALESCED_CLEANUP_EXHAUSTED) == 2
    viewer_app_module._COALESCED_CLEANUP_EXHAUSTED.clear()


def test_hydrated_cache_retries_pending_coalesced_cleanup(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    task_id = "task-provisional-hydrated"
    deleted: list[str] = []
    with viewer_app_module.DATA_TASKS_LOCK:
        viewer_app_module.DATA_TASKS[task_id] = {
            "id": task_id,
            "kind": "data",
            "status": "starting",
        }
        viewer_app_module._COALESCED_CLEANUP_PENDING.add(task_id)
    monkeypatch.setattr(viewer_app_module, "_TASK_CACHE_HYDRATED", True)
    monkeypatch.setattr(
        viewer_app_module,
        "delete_background_task_record",
        lambda deleted_task_id: deleted.append(deleted_task_id) or True,
    )

    assert viewer_app_module._hydrate_task_cache_once() == 0
    assert deleted == [task_id]
    assert task_id not in viewer_app_module.DATA_TASKS
    assert task_id not in viewer_app_module._COALESCED_CLEANUP_PENDING


def test_task_lookup_retries_pending_coalesced_cleanup_without_hydrating(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    cleanup_record = {
        "id": "task-provisional",
        "type": "data",
        "status": "cleanup_pending",
        "config_json": {"coalesced_cleanup_pending": True},
        "source": "yfinance",
    }
    delete_attempts: list[str] = []

    monkeypatch.setattr(
        viewer_app_module,
        "get_background_task_record",
        lambda _task_id: cleanup_record,
    )
    monkeypatch.setattr(
        viewer_app_module,
        "delete_background_task_record",
        lambda task_id: delete_attempts.append(task_id) or True,
    )

    assert viewer_app_module._get_task("task-provisional") is None
    assert delete_attempts == ["task-provisional"]
    assert "task-provisional" not in viewer_app_module.DATA_TASKS


def test_task_lookup_evicts_cached_task_when_persisted_cleanup_marker_appears(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    task_id = "task-stale"
    with viewer_app_module.DATA_TASKS_LOCK:
        viewer_app_module.DATA_TASKS[task_id] = {
            "id": task_id,
            "kind": "data",
            "source": "yfinance",
            "status": "running",
        }

    monkeypatch.setattr(
        viewer_app_module,
        "get_background_task_record",
        lambda _task_id: {
            "id": task_id,
            "type": "data",
            "status": "cleanup_pending",
            "config_json": {"coalesced_cleanup_pending": True},
        },
    )
    delete_attempts: list[str] = []
    monkeypatch.setattr(
        viewer_app_module,
        "delete_background_task_record",
        lambda value: delete_attempts.append(value) or True,
    )

    assert viewer_app_module._get_task(task_id) is None
    assert delete_attempts == [task_id]
    assert task_id not in viewer_app_module.DATA_TASKS


def test_task_snapshot_hides_cached_task_when_cleanup_marker_appears(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    task_id = "task-stale-snapshot"
    with viewer_app_module.DATA_TASKS_LOCK:
        viewer_app_module.DATA_TASKS[task_id] = {
            "id": task_id,
            "kind": "data",
            "source": "yfinance",
            "status": "running",
        }

    monkeypatch.setattr(
        viewer_app_module,
        "get_background_task_record",
        lambda _task_id: {
            "id": task_id,
            "type": "data",
            "status": "cleanup_pending",
            "config_json": {"coalesced_cleanup_pending": True},
        },
    )
    monkeypatch.setattr(
        viewer_app_module,
        "delete_background_task_record",
        lambda _task_id: True,
    )

    assert viewer_app_module._snapshot_task(task_id) is None
    assert task_id not in viewer_app_module.DATA_TASKS


def test_data_download_accepts_jforex_source(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    captured: dict[str, object] = {}

    def fake_start(task_id: str, cmd: list[str], **kwargs) -> dict:
        captured["task_id"] = task_id
        captured["cmd"] = cmd
        captured.update(kwargs)
        return {"task_id": task_id, "status": "starting"}

    monkeypatch.setattr(viewer_app_module, "_start_jforex_task", fake_start)

    resp = viewer_client.post(
        "/api/data/download",
        json={
            "source": "jforex",
            "symbols": "EURUSD",
            "timeframes": "h1",
            "price_type": "bid",
            "query_timeout_seconds": 1200,
        },
    )

    assert resp.status_code == 200
    assert "data_tools.downloaders.jforex_dl" in captured["cmd"]
    assert "--query-timeout-seconds" in captured["cmd"]
    assert "1200" in captured["cmd"]
    assert captured["source_label"] == "jforex"
    assert captured["allow_native_fallback"] is False


def test_data_download_rejects_invalid_query_timeout_for_jforex(viewer_client):
    response = viewer_client.post(
        "/api/data/download",
        json={
            "source": "jforex",
            "symbols": "EURUSD",
            "timeframes": "h1",
            "query_timeout_seconds": 10,
        },
    )
    too_long = viewer_client.post(
        "/api/data/download",
        json={
            "source": "jforex",
            "symbols": "EURUSD",
            "timeframes": "h1",
            "query_timeout_seconds": 7201,
        },
    )
    assert response.status_code == 400
    assert too_long.status_code == 400
    message = response.json().get("detail", "")
    assert "query_timeout_seconds" in message


def test_data_download_fallback_flag_respects_source(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    captured: list[dict[str, object]] = []

    def fake_start(task_id: str, cmd: list[str], **kwargs) -> dict:
        payload = {"task_id": task_id, "cmd": cmd}
        payload.update(kwargs)
        captured.append(payload)
        return {"task_id": task_id, "status": "starting"}

    monkeypatch.setattr(viewer_app_module, "_start_jforex_task", fake_start)

    dukascopy_resp = viewer_client.post(
        "/api/data/download",
        json={
            "source": "dukascopy",
            "symbols": "EURUSD",
            "timeframes": "h1",
            "price_type": "bid",
        },
    )
    assert dukascopy_resp.status_code == 200

    jforex_resp = viewer_client.post(
        "/api/data/download",
        json={
            "source": "jforex",
            "symbols": "EURUSD",
            "timeframes": "h1",
            "price_type": "bid",
        },
    )
    assert jforex_resp.status_code == 200

    assert len(captured) == 2
    assert captured[0]["source_label"] == "dukascopy"
    assert captured[0]["allow_native_fallback"] is True
    assert captured[1]["source_label"] == "jforex"
    assert captured[1]["allow_native_fallback"] is False


def test_data_download_builds_yfinance_command_with_timeframes_and_dates(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    captured: dict[str, object] = {}

    def fake_start(task_id: str, cmd: list[str], **kwargs) -> dict:
        captured["task_id"] = task_id
        captured["cmd"] = cmd
        captured.update(kwargs)
        return {"task_id": task_id, "status": "starting"}

    monkeypatch.setattr(viewer_app_module, "_start_data_task", fake_start)

    resp = viewer_client.post(
        "/api/data/download",
        json={
            "source": "yfinance",
            "symbols": "AAPL,MSFT",
            "timeframes": "1h,1d,1w",
            "start": "2026-01-01",
            "end": "2026-02-01",
        },
    )

    assert resp.status_code == 200
    cmd = captured["cmd"]
    assert "data_tools.downloaders.yfinance_dl" in cmd
    assert "--symbols" in cmd
    assert "--timeframes" in cmd
    assert "--start" in cmd
    assert "--end" in cmd
    assert "AAPL,MSFT" in cmd
    assert "1h,1d,1w" in cmd
    assert captured["source"] == "yfinance"
    assert captured["total_items"] == 6


def test_data_download_builds_huggingface_and_alpaca_commands(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    captured: list[dict[str, object]] = []

    def fake_start(task_id: str, cmd: list[str], **kwargs) -> dict:
        payload = {"task_id": task_id, "cmd": cmd}
        payload.update(kwargs)
        captured.append(payload)
        return {"task_id": task_id, "status": "starting"}

    monkeypatch.setattr(viewer_app_module, "_start_data_task", fake_start)

    hf_resp = viewer_client.post(
        "/api/data/download",
        json={
            "source": "huggingface",
            "symbols": "AAPL",
            "timeframes": "1m",
            "start": "2025-06-01",
            "end": "2025-07-15",
        },
    )
    assert hf_resp.status_code == 200

    alpaca_resp = viewer_client.post(
        "/api/data/download",
        json={
            "source": "alpaca",
            "symbols": "AAPL",
            "timeframes": "1h",
            "start": "2025-07-15",
            "end": "2025-08-01",
        },
    )
    assert alpaca_resp.status_code == 200

    assert len(captured) == 2
    hf_cmd = captured[0]["cmd"]
    alpaca_cmd = captured[1]["cmd"]

    assert "data_tools.downloaders.huggingface_dl" in hf_cmd
    assert "--symbols" in hf_cmd
    assert "--timeframes" in hf_cmd
    assert "--start" in hf_cmd
    assert "--end" in hf_cmd
    assert captured[0]["source"] == "huggingface"

    assert "data_tools.downloaders.alpaca_dl" in alpaca_cmd
    assert "--symbols" in alpaca_cmd
    assert "--timeframes" in alpaca_cmd
    assert "--start" in alpaca_cmd
    assert "--end" in alpaca_cmd
    assert captured[1]["source"] == "alpaca"


def test_auto_download_source_prefers_yfinance_for_non_forex_symbols(viewer_client):
    import viewer.main as viewer_app_module

    assert viewer_app_module._auto_download_source("AAPL", "1d") == "yfinance"
    assert viewer_app_module._auto_download_source("AAPL", "1h") == "huggingface"
    assert viewer_app_module._auto_download_source("SP500", "1m") == "dukascopy"
    assert viewer_app_module._auto_download_source("NAS100", "15m") == "dukascopy"
    assert viewer_app_module._auto_download_source("US100", "15m") == "dukascopy"
    assert viewer_app_module._auto_download_source("BTC/USD", "1h") == "dukascopy"
    assert viewer_app_module._auto_download_source("EURUSD", "1h") == "dukascopy"


def test_auto_download_source_plan_splits_us_intraday_range(viewer_client):
    import viewer.main as viewer_app_module

    split_plan = viewer_app_module._auto_download_source_plan(
        symbol="AAPL",
        timeframe="1h",
        start="2025-06-20",
        end="2025-08-05",
    )
    assert split_plan == [
        {"source": "huggingface", "start": "2025-06-20", "end": "2025-07-31"},
        {"source": "alpaca", "start": "2025-07-01", "end": "2025-08-05"},
    ]

    alpaca_only_plan = viewer_app_module._auto_download_source_plan(
        symbol="AAPL",
        timeframe="1h",
        start="2025-08-01",
        end="2025-08-15",
    )
    assert alpaca_only_plan == [
        {"source": "alpaca", "start": "2025-08-01", "end": "2025-08-15"}
    ]


def test_download_missing_ohlcv_data_falls_back_to_yfinance_when_alpaca_credentials_missing(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    calls: list[dict[str, str | None]] = []

    class _FailingAlpacaDownloader:
        def download(self, **_kwargs):
            raise RuntimeError(
                "Missing Alpaca credentials. Set ALPACA_API_KEY/APCA_API_KEY_ID and "
                "ALPACA_SECRET_KEY/APCA_API_SECRET_KEY."
            )

    class _RecordingYFinanceDownloader:
        def download(self, **kwargs):
            calls.append({key: kwargs.get(key) for key in ("symbol", "timeframe", "start", "end")})

    monkeypatch.setitem(
        sys.modules,
        "data_tools.downloaders.alpaca_dl",
        SimpleNamespace(AlpacaDownloader=_FailingAlpacaDownloader),
    )
    monkeypatch.setitem(
        sys.modules,
        "data_tools.downloaders.yfinance_dl",
        SimpleNamespace(YFinanceDownloader=_RecordingYFinanceDownloader),
    )

    viewer_app_module._download_missing_ohlcv_data(
        symbol="AAPL",
        timeframe="1m",
        start="2026-03-31",
        end="2026-04-02",
        source="alpaca",
    )

    assert calls == [
        {
            "symbol": "AAPL",
            "timeframe": "1m",
            "start": "2026-03-31",
            "end": "2026-04-02",
        }
    ]


def test_data_cache_invalidate_endpoint_returns_stats(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(
        viewer_app_module.cache_manager,
        "invalidate_cache",
        lambda *_args, **_kwargs: 2,
    )
    monkeypatch.setattr(
        viewer_app_module.cache_manager,
        "get_cache_stats",
        lambda: {"entries": 0, "invalidations": 2},
    )

    resp = viewer_client.post("/api/data/cache/invalidate?reason=manual")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["removed_entries"] == 2
    assert payload["reason"] == "manual"
    assert payload["cache_stats"]["entries"] == 0


def test_data_overview_uses_cached_catalog_entries(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(
        viewer_app_module,
        "_cached_catalog_ohlcv_entries",
        lambda: [
            {
                "path": "/tmp/ohlcv/test_source/etf/SPY/1d.parquet",
                "symbol": "SPY",
                "timeframe": "1d",
                "source": "test_source",
                "size_bytes": 1024,
                "start": "2024-01-02",
                "end": "2024-02-09",
            }
        ],
    )
    monkeypatch.setattr(
        viewer_app_module.cache_manager,
        "get_overview_metadata",
        lambda *_args, **_kwargs: {"summary": {"total_datasets": 1}},
    )

    response = viewer_client.get("/api/data/overview")
    assert response.status_code == 200
    categories = {item["name"]: item for item in response.json()["categories"]}
    assert categories["ETFs"]["file_count"] == 1
    assert categories["ETFs"]["start"] == "2024-01-02"
    assert categories["ETFs"]["end"] == "2024-02-09"


def test_data_views_normalize_timeframe_aliases_in_catalog(viewer_client, tmp_path):
    catalog = {
        "ohlcv": [
            {
                "path": str(tmp_path / "ohlcv" / "test_source" / "forex" / "EURUSD" / "m1.parquet"),
                "symbol": "EURUSD",
                "timeframe": "m1",
                "source": "test_source",
                "size_bytes": 1000,
                "start": "2024-01-01",
                "end": "2024-01-10",
            },
            {
                "path": str(tmp_path / "ohlcv" / "test_source" / "forex" / "EURUSD" / "1m.parquet"),
                "symbol": "EURUSD",
                "timeframe": "1m",
                "source": "test_source",
                "size_bytes": 1200,
                "start": "2024-01-11",
                "end": "2024-01-20",
            },
        ]
    }
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))

    overview_resp = viewer_client.get("/api/data/overview")
    assert overview_resp.status_code == 200
    categories = {item["name"]: item for item in overview_resp.json()["categories"]}
    assert categories["Forex"]["file_count"] == 1
    assert categories["Forex"]["start"] == "2024-01-01"
    assert categories["Forex"]["end"] == "2024-01-20"

    details_resp = viewer_client.get("/api/data/category/Forex")
    assert details_resp.status_code == 200
    symbols = details_resp.json()["symbols"]
    assert len(symbols) == 1
    assert symbols[0]["symbol"] == "EURUSD"
    assert symbols[0]["timeframe"] == "1m"
    assert symbols[0]["file_count"] == 2


def test_list_symbols_reuses_precomputed_data_views(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    viewer_app_module._invalidate_data_views_cache()

    monkeypatch.setattr(
        viewer_app_module,
        "_cached_catalog_ohlcv_entries",
        lambda: [
            {
                "path": "/tmp/ohlcv/test_source/equity/US/AAPL/1d.parquet",
                "symbol": "AAPL",
                "timeframe": "1d",
                "source": "test_source",
                "asset_class": "equity",
                "size_bytes": 2048,
                "start": "2024-01-01",
                "end": "2024-02-01",
            },
            {
                "path": "/tmp/ohlcv/test_source/equity/US/AAPL/1h.parquet",
                "symbol": "AAPL",
                "timeframe": "1h",
                "source": "test_source",
                "asset_class": "equity",
                "size_bytes": 1024,
                "start": "2024-01-01",
                "end": "2024-02-01",
            },
        ],
    )

    response_1 = viewer_client.get("/api/symbols")
    assert response_1.status_code == 200
    payload_1 = response_1.json()
    assert payload_1["symbols"] == ["AAPL"]
    assert payload_1["categories"]["equity"] == ["AAPL"]

    monkeypatch.setattr(
        viewer_app_module,
        "_cached_catalog_ohlcv_entries",
        lambda: (_ for _ in ()).throw(
            AssertionError("_cached_catalog_ohlcv_entries should not run when data views are cached")
        ),
    )

    response_2 = viewer_client.get("/api/symbols")
    assert response_2.status_code == 200
    assert response_2.json() == payload_1


def test_data_tasks_list_and_controls(viewer_client, tmp_path):
    import viewer.main as viewer_app_module

    task_id = "controllable-task"
    log_file = tmp_path / "logs" / "tasks" / f"task_{task_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("✅ step complete\n", encoding="utf-8")
    viewer_app_module.DATA_TASKS[task_id] = viewer_app_module._create_task_record(
        task_id,
        name="Download EURUSD tick",
        description="Dukascopy download for EURUSD tick data.",
        kind="data",
        source="dukascopy",
        total_items=1,
        log_file=log_file,
        control_mode="thread",
        supports_control=True,
    )
    viewer_app_module._set_task_status(task_id, "running")

    list_resp = viewer_client.get("/api/data/tasks")
    assert list_resp.status_code == 200
    tasks = list_resp.json()["tasks"]
    listed = next(item for item in tasks if item["id"] == task_id)
    assert listed["progress"]["completed"] == 1
    assert listed["can_pause"] is True
    assert listed["category"] == "download"
    assert isinstance(listed["elapsed_seconds"], int)
    assert listed["eta_seconds"] is None

    pause_resp = viewer_client.post(f"/api/data/tasks/{task_id}/pause")
    assert pause_resp.status_code == 200
    assert viewer_app_module.DATA_TASKS[task_id]["pause_event"].is_set()
    assert pause_resp.json()["status"] == "paused"

    resume_resp = viewer_client.post(f"/api/data/tasks/{task_id}/resume")
    assert resume_resp.status_code == 200
    assert not viewer_app_module.DATA_TASKS[task_id]["pause_event"].is_set()
    assert resume_resp.json()["status"] == "running"

    cancel_resp = viewer_client.post(f"/api/data/tasks/{task_id}/cancel")
    assert cancel_resp.status_code == 200
    assert viewer_app_module.DATA_TASKS[task_id]["stop_event"].is_set()
    assert cancel_resp.json()["status"] == "cancelled"


def test_tasks_alias_returns_enriched_fields(viewer_client, tmp_path):
    import viewer.main as viewer_app_module

    task_id = "alias-fields-task"
    log_file = tmp_path / "logs" / "tasks" / f"task_{task_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    viewer_app_module.DATA_TASKS[task_id] = viewer_app_module._create_task_record(
        task_id,
        name="Optimize SMA window",
        description="Optimization task",
        kind="optimization",
        source="optuna",
        total_items=4,
        log_file=log_file,
        control_mode="thread",
        supports_control=False,
    )
    viewer_app_module._set_task_status(task_id, "running")
    viewer_app_module._update_task_progress(
        task_id,
        percent=25.0,
        current="Trial 1/4",
        total=4,
    )

    response = viewer_client.get("/api/tasks")
    assert response.status_code == 200
    listed = next(item for item in response.json()["tasks"] if item["id"] == task_id)
    assert "created_at" in listed
    assert "started_at" in listed
    assert "completed_at" in listed
    assert "progress_percent" in listed
    assert "progress_message" in listed
    assert "eta_seconds" in listed
    assert "elapsed_seconds" in listed
    assert "category" in listed
    assert "error_message" in listed
    assert listed["progress_percent"] == 25.0
    assert listed["progress_message"] == "Trial 1/4"
    assert listed["category"] == "optimization"
    assert listed["error_message"] is None


def test_data_tasks_running_eta_is_exposed(viewer_client, tmp_path):
    import viewer.main as viewer_app_module

    task_id = "running-eta-task"
    log_file = tmp_path / "logs" / "tasks" / f"task_{task_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    viewer_app_module.DATA_TASKS[task_id] = viewer_app_module._create_task_record(
        task_id,
        name="Download GBPUSD h1",
        description="Long-running download.",
        kind="data",
        source="dukascopy",
        total_items=10,
        log_file=log_file,
        control_mode="thread",
        supports_control=True,
    )
    viewer_app_module._set_task_status(task_id, "running")
    viewer_app_module._update_task_progress(
        task_id,
        percent=40.0,
        current="Downloading chunk 4/10",
        total=10,
    )
    viewer_app_module._update_task_fields(
        task_id,
        started_at="2026-03-27T10:00:00+00:00",
    )

    list_resp = viewer_client.get("/api/data/tasks")
    assert list_resp.status_code == 200
    listed = next(item for item in list_resp.json()["tasks"] if item["id"] == task_id)
    assert listed["status"] == "running"
    assert listed["category"] == "download"
    assert listed["elapsed_seconds"] is not None
    assert listed["eta_seconds"] is not None
    assert listed["eta_seconds"] >= 0


def test_failed_task_can_restart(viewer_client, tmp_path):
    import viewer.main as viewer_app_module

    task_id = "failed-task"
    log_file = tmp_path / "logs" / "tasks" / f"task_{task_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    viewer_app_module.DATA_TASKS[task_id] = viewer_app_module._create_task_record(
        task_id,
        name="Download EURUSD",
        description="Should be retryable.",
        kind="data",
        source="dukascopy",
        total_items=2,
        log_file=log_file,
        control_mode="thread",
        supports_control=True,
    )
    viewer_app_module._mark_task_finished(task_id, status="failed", returncode=1, error="network")

    list_resp = viewer_client.get("/api/data/tasks")
    assert list_resp.status_code == 200
    listed = next(item for item in list_resp.json()["tasks"] if item["id"] == task_id)
    assert listed["status"] == "failed"
    assert listed["can_restart"] is True

    restart_resp = viewer_client.post(f"/api/data/tasks/{task_id}/restart")
    assert restart_resp.status_code == 200
    restarted = restart_resp.json()
    assert restarted["status"] == "manual_required"
    assert restarted["can_restart"] is False


def test_failed_jforex_task_restart_marks_interrupted(viewer_client, tmp_path):
    import viewer.main as viewer_app_module

    task_id = "failed-jforex"
    log_file = tmp_path / "logs" / "tasks" / f"task_{task_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    viewer_app_module.DATA_TASKS[task_id] = viewer_app_module._create_task_record(
        task_id,
        name="JForex download",
        description="Manual restart required.",
        kind="data",
        source="jforex",
        total_items=1,
        log_file=log_file,
        control_mode="thread",
        supports_control=True,
    )
    viewer_app_module._update_task_fields(task_id, downloader="jforex")
    viewer_app_module._mark_task_finished(
        task_id, status="failed", returncode=1, error="jforex error"
    )

    restart_resp = viewer_client.post(f"/api/data/tasks/{task_id}/restart")
    assert restart_resp.status_code == 200
    payload = restart_resp.json()
    assert payload["status"] == "manual_required"
    assert payload["can_restart"] is False
    assert payload["progress"]["failed"] >= 1


def test_data_task_delete_removes_finished_task(viewer_client, tmp_path):
    import viewer.main as viewer_app_module

    task_id = "finished-task"
    log_file = tmp_path / "logs" / "tasks" / f"task_{task_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("Done.\n")
    viewer_app_module.DATA_TASKS[task_id] = viewer_app_module._create_task_record(
        task_id,
        name="Completed download",
        description="Finished task",
        kind="data",
        source="dukascopy",
        total_items=1,
        log_file=log_file,
        control_mode="thread",
        supports_control=True,
    )
    viewer_app_module._set_task_status(task_id, "completed")

    resp = viewer_client.delete(f"/api/data/tasks/{task_id}")

    assert resp.status_code == 200
    assert resp.json() == {"task_id": task_id, "status": "deleted"}
    assert task_id not in viewer_app_module.DATA_TASKS


def test_data_task_delete_rejects_active_task(viewer_client, tmp_path):
    import viewer.main as viewer_app_module

    task_id = "active-task"
    log_file = tmp_path / "logs" / "tasks" / f"task_{task_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    viewer_app_module.DATA_TASKS[task_id] = viewer_app_module._create_task_record(
        task_id,
        name="Running download",
        description="Still active",
        kind="data",
        source="dukascopy",
        total_items=1,
        log_file=log_file,
        control_mode="thread",
        supports_control=True,
    )
    viewer_app_module._set_task_status(task_id, "running")

    resp = viewer_client.delete(f"/api/data/tasks/{task_id}")

    assert resp.status_code == 409
    message = resp.json().get("error") or resp.json().get("detail", "")
    assert "can be removed" in message
    assert task_id in viewer_app_module.DATA_TASKS


def test_data_task_logs_endpoint_returns_empty_logs_when_file_missing(
    viewer_client, tmp_path, monkeypatch
):
    import viewer.main as viewer_app_module

    task_id = "missing-log"
    viewer_app_module.DATA_TASKS[task_id] = {
        "status": "running",
        "log_file": str(tmp_path / "logs" / "tasks" / f"task_{task_id}.log"),
    }
    monkeypatch.setattr(
        viewer_app_module,
        "get_background_task_record",
        lambda _task_id: {"id": task_id, "type": "data", "status": "running", "config_json": {}},
    )

    resp = viewer_client.get(f"/api/data/tasks/{task_id}/logs")

    assert resp.status_code == 200
    assert resp.json() == {"logs": "", "status": "running"}


def test_data_task_logs_endpoint_returns_full_logs(viewer_client, tmp_path, monkeypatch):
    import viewer.main as viewer_app_module

    task_id = "completed-log"
    log_file = tmp_path / "logs" / "tasks" / f"task_{task_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("[2026-03-04 10:00:00] Starting download...\nDone.\n")
    viewer_app_module.DATA_TASKS[task_id] = {
        "status": "completed",
        "log_file": str(log_file),
    }
    monkeypatch.setattr(
        viewer_app_module,
        "get_background_task_record",
        lambda _task_id: {"id": task_id, "type": "data", "status": "completed", "config_json": {}},
    )

    resp = viewer_client.get(f"/api/data/tasks/{task_id}/logs")

    assert resp.status_code == 200
    assert resp.json() == {
        "logs": "[2026-03-04 10:00:00] Starting download...\nDone.\n",
        "status": "completed",
    }


def test_data_task_logs_endpoint_bounds_and_redacts_output(
    viewer_client, tmp_path, monkeypatch
):
    import viewer.main as viewer_app_module

    task_id = "bounded-log"
    log_file = tmp_path / "logs" / "tasks" / f"task_{task_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        ("x" * 70000) + "Authorization: Bearer endpoint-secret\n",
        encoding="utf-8",
    )
    viewer_app_module.DATA_TASKS[task_id] = {
        "status": "running",
        "log_file": str(log_file),
    }
    monkeypatch.setattr(
        viewer_app_module,
        "get_background_task_record",
        lambda _task_id: {"id": task_id, "type": "data", "status": "running", "config_json": {}},
    )

    response = viewer_client.get(f"/api/data/tasks/{task_id}/logs")

    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs.encode("utf-8")) <= 64 * 1024
    assert "endpoint-secret" not in logs
    assert "Bearer [Filtered]" in logs


def test_task_log_writer_redacts_before_persisting():
    from viewer import core

    handle = StringIO()
    core._write_task_log_line(
        handle,
        "task-redact",
        "secret_key=task-secret Authorization: Basic task-auth-secret",
        stream="stdout",
    )

    persisted = handle.getvalue()
    assert "task-secret" not in persisted
    assert "task-auth-secret" not in persisted
    assert "Basic [Filtered]" in persisted


def test_service_payload_redacts_structured_secrets():
    import viewer.main as viewer_app_module

    service = SimpleNamespace(
        id="service-redact",
        name="Service",
        type=SimpleNamespace(value="scheduled_job"),
        status=SimpleNamespace(value="running"),
        config={
            "secret_key": "service-secret",
            "app_secret": "service-app-secret",
            "safe": "visible",
        },
        last_activity=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    payload = viewer_app_module._serialize_service_payload(service)

    assert payload["config"]["secret_key"] == "[Filtered]"
    assert payload["config"]["app_secret"] == "[Filtered]"
    assert payload["config"]["safe"] == "visible"


def test_data_task_logs_rejects_syntactically_valid_unknown_task(viewer_client, tmp_path):
    task_id = "unknown-log"
    log_file = tmp_path / "logs" / "tasks" / f"task_{task_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("secret\n")

    response = viewer_client.get(f"/api/data/tasks/{task_id}/logs")

    assert response.status_code == 404
    assert "secret" not in response.text


def test_task_log_websocket_rejects_unknown_task_identity(viewer_client, tmp_path):
    task_id = "unknown-ws-log"
    log_file = tmp_path / "logs" / "tasks" / f"task_{task_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("secret\n")

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with viewer_client.websocket_connect(f"/ws/tasks/{task_id}") as websocket:
            websocket.receive_text()

    assert exc_info.value.code == 1008


def test_start_data_task_writes_task_logs_and_refreshes_catalog(
    viewer_client, monkeypatch, tmp_path
):
    import viewer.main as viewer_app_module

    refresh_calls: list[str] = []
    routed_logs: list[str] = []

    def fake_refresh() -> None:
        refresh_calls.append("refreshed")

    def fake_info(message: str, *args, **kwargs) -> None:
        routed_logs.append(message % args if args else message)

    monkeypatch.setattr(viewer_app_module.legacy_viewer, "_refresh_catalog", fake_refresh)
    monkeypatch.setattr(viewer_app_module.legacy_viewer.logger, "info", fake_info)

    task_id = "streamed-task"
    viewer_app_module._start_data_task(
        task_id,
        [
            sys.executable,
            "-u",
            "-c",
            "import sys; print('stdout line'); print('stderr line', file=sys.stderr)",
        ],
        total_items=1,
    )

    for _ in range(50):
        task = viewer_app_module.DATA_TASKS[task_id]
        if task["status"] in {"completed", "failed"}:
            break
        time.sleep(0.1)

    task = viewer_app_module.DATA_TASKS[task_id]
    assert task["status"] == "completed"
    assert Path(task["log_file"]).parent == tmp_path / "logs" / "tasks"
    assert task["pause_event"].is_set() is False
    assert task["stop_event"].is_set() is False
    assert task["progress"] == 100.0
    log_text = Path(task["log_file"]).read_text()
    assert "Starting command:" in log_text
    assert "stdout line" in log_text
    assert "stderr line" in log_text
    assert "Refreshing catalog..." in log_text
    assert refresh_calls == ["refreshed"]
    assert any("stdout line" in line for line in routed_logs)
    assert any("stderr line" in line for line in routed_logs)
    thread = task.get("thread")
    if isinstance(thread, threading.Thread):
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_optimization_run_starts_background_task(viewer_client, monkeypatch, tmp_path):
    import viewer.main as viewer_app_module

    captured: dict[str, object] = {}

    def fake_execute(config, progress_callback=None):
        captured["config"] = config
        output_path = tmp_path / "optimizations" / "opt_base-demo_20260311_100000.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": "opt_base-demo_20260311_100000",
            "file": output_path.name,
            "created_at": "2026-03-11T10:00:00+00:00",
            "base_backtest_id": config.base_backtest_id,
            "target_metric": config.target_metric,
            "best_params": {"window": 10},
            "effective_best_parameters": {"window": 10, "fixed": 3},
            "train_period": {
                "start": "2024-01-01T00:00:00+00:00",
                "end": "2024-01-07T23:59:59+00:00",
            },
            "test_period": {
                "start": "2024-01-08T00:00:00+00:00",
                "end": "2024-01-10T23:59:59+00:00",
            },
            "train_stats": {"sharpe_ratio": 1.5},
            "test_stats": {"sharpe_ratio": 1.1},
            "top_trials": [],
        }
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        if progress_callback is not None:
            progress_callback(50.0, "✅ Trial 1/2 (sharpe=1.500000) params={'window': 10}")
            progress_callback(100.0, f"Optimization finished: {output_path.name}")
        return payload

    monkeypatch.setattr(viewer_app_module, "execute_optimization", fake_execute)

    resp = viewer_client.post(
        "/api/optimizations/run",
        json={
            "base_backtest_id": "base-demo",
            "target_metric": "sharpe",
            "n_trials": 2,
            "train_split_pct": 0.7,
            "parameter_ranges": {
                "window": {"type": "int", "min": 5, "max": 10, "step": 1},
            },
        },
    )

    assert resp.status_code == 202
    run_payload = resp.json()
    assert run_payload["task_id"] == run_payload["job_id"]
    job_id = run_payload["job_id"]

    for _ in range(50):
        task = viewer_app_module.DATA_TASKS[job_id]
        if task["status"] in {"completed", "failed"}:
            break
        time.sleep(0.05)

    task = viewer_app_module.DATA_TASKS[job_id]
    assert task["status"] == "completed"
    assert task["kind"] == "optimization"
    assert task["optimization_id"] == "opt_base-demo_20260311_100000"
    assert task["optimization_file"] == "opt_base-demo_20260311_100000.json"
    assert task["progress"] == 100.0
    assert captured["config"].base_backtest_id == "base-demo"

    log_text = Path(task["log_file"]).read_text(encoding="utf-8")
    assert "Starting optimization for base-demo" in log_text
    assert "✅ Trial 1/2" in log_text
    assert "Optimization saved to opt_base-demo_20260311_100000.json" in log_text


def test_optimization_run_rejects_train_split_above_max_before_queueing(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(
        viewer_app_module.threading,
        "Thread",
        lambda *args, **kwargs: SimpleNamespace(start=lambda: None),
    )

    response = viewer_client.post(
        "/api/optimizations/run",
        json={
            "base_backtest_id": "base-demo",
            "target_metric": "sharpe",
            "n_trials": 2,
            "train_split_pct": 0.96,
            "parameter_ranges": {
                "window": {"type": "int", "min": 5, "max": 10, "step": 1},
            },
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "Invalid optimization payload"
    assert response.json()["detail"]["details"][0]["loc"] == ["train_split_pct"]
    assert viewer_app_module.DATA_TASKS == {}


def test_backtest_run_registers_task_manager_progress(viewer_client, monkeypatch, tmp_path):
    import viewer.main as viewer_app_module

    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    strategy_file = strategies_dir / "demo_strategy.py"
    strategy_file.write_text("class DemoStrategy:\n    pass\n", encoding="utf-8")

    monkeypatch.setattr(viewer_app_module, "STRATEGIES_DIR", strategies_dir)
    monkeypatch.setattr(viewer_app_module.legacy_viewer, "STRATEGIES_DIR", strategies_dir)

    expected_output = tmp_path / "backtests" / "demo_20260101_120000.trades.json"
    expected_output.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(viewer_app_module, "_dolthub_pull", lambda: None)
    monkeypatch.setattr(viewer_app_module, "_ensure_backtest_data_availability", lambda **kwargs: [])

    class _FakeProc:
        def __init__(self, lines: list[str]):
            self.stdout = iter(lines)
            self.pid = 4242
            self._killed = False

        def wait(self, timeout=None):
            return 0

        def poll(self):
            # The fake child "exits" immediately so the fill-bar watchdog
            # thread terminates instead of blocking pytest shutdown.
            return 0

        def kill(self):
            self._killed = True

    def fake_popen(cmd, *args, **kwargs):
        assert cmd[:4] == [sys.executable, "-u", "-m", "runner.cli"]
        assert "--progress-stdout" in cmd
        return _FakeProc(
            [
                '{"message":"Auto-detected date range: 2024-01-01 to 2024-12-31"}\n',
                "PROGRESS:5.00:Loading market data...\n",
                '{"message":"Running backtest: engine=native, strategy=demo, symbols=[\\"AAPL\\"]"}\n',
                "PROGRESS:25.00:Running strategy: 2500/10000 bars (25%)\n",
                '{"message":"Result: 5 trades, total PnL: $123.45"}\n',
                "PROGRESS:95.00:Saving results...\n",
                json.dumps({"message": f"Saved to: {expected_output}"}) + "\n",
                "PROGRESS:100.00:Completed - 5 trades found\n",
            ]
        )

    monkeypatch.setattr(viewer_app_module.subprocess, "Popen", fake_popen)

    response = viewer_client.post(
        "/api/backtests/run",
        json={
            "strategy": "demo_strategy.py",
            "engine": "native",
            "symbols": ["AAPL"],
            "timeframe": "1d",
            "capital": 100000,
            "parameters": {"window": 10},
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["task_id"] == payload["job_id"]
    assert payload["status"] == "queued"
    task_id = payload["task_id"]

    for _ in range(50):
        task = viewer_app_module.DATA_TASKS[task_id]
        if task["status"] in {"completed", "failed"}:
            break
        time.sleep(0.05)

    task = viewer_app_module.DATA_TASKS[task_id]
    assert task["kind"] == "backtest"
    assert task["status"] == "completed"
    assert task["progress"] == 100.0
    assert task["result"]["output_path"] == str(expected_output)
    assert task["result"]["backtest_id"] == expected_output.stem.replace(".trades", "")

    list_resp = viewer_client.get("/api/data/tasks")
    assert list_resp.status_code == 200
    listed_tasks = list_resp.json()["tasks"]
    listed = next(item for item in listed_tasks if item["id"] == task_id)
    assert listed["kind"] == "backtest"
    assert listed["progress"]["percent"] == 100.0
    assert listed["progress"]["current"]

    status_resp = viewer_client.get(f"/api/backtests/run/{task_id}/status")
    assert status_resp.status_code == 200
    status_payload = status_resp.json()
    assert status_payload["task_id"] == task_id
    assert status_payload["job_id"] == task_id
    assert status_payload["status"] == "completed"
    assert status_payload["result"]["output_path"] == str(expected_output)

    log_text = Path(task["log_file"]).read_text(encoding="utf-8")
    assert "Loading market data..." in log_text
    assert "Running backtest... 45%" in log_text
    assert "Running backtest... 58%" in log_text
    assert "Running backtest... " in log_text
    assert "Processing trades..." in log_text
    assert "Saving results..." in log_text
    assert "Completed - " in log_text


def test_backtest_run_signals_registers_task_manager_progress(viewer_client, monkeypatch, tmp_path):
    import viewer.main as viewer_app_module

    replay_query_args: dict[str, object] = {}
    adapter_init_kwargs: dict[str, object] = {}

    monkeypatch.setattr(
        viewer_app_module,
        "list_live_deployments",
        lambda: [
            SimpleNamespace(
                id="dep-1",
                signal_source="discord",
                account_id="acct-1",
            )
        ],
    )

    def _fake_replay_query(**kwargs):
        replay_query_args.update(kwargs)
        return [
            {
                "signal_id": "sig-1",
                "deployment_id": "dep-1",
                "symbol": "AAPL",
                "action": "buy",
                "source": "discord",
                "timestamp": datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
                "entry_price": 100.0,
                "stop_loss": 98.0,
                "take_profit": 104.0,
                "timeframe": "1h",
                "metadata": {"source": "discord"},
            }
        ]

    monkeypatch.setattr(viewer_app_module, "list_live_signals_for_replay", _fake_replay_query)
    monkeypatch.setattr(viewer_app_module, "_ensure_backtest_data_availability", lambda **_kwargs: [])
    monkeypatch.setattr(viewer_app_module, "_upsert_backtest_meta_cache", lambda _backtest_id: None)

    monkeypatch.setattr(
        viewer_app_module.legacy_viewer,
        "_load_ohlcv_df",
        lambda symbol, timeframe: pd.DataFrame(
            {
                "timestamp": [
                    "2024-01-01T12:00:00+00:00",
                    "2024-01-01T13:00:00+00:00",
                    "2024-01-01T14:00:00+00:00",
                ],
                "open": [100.0, 101.0, 102.0],
                "high": [101.0, 102.0, 103.0],
                "low": [99.0, 100.0, 101.0],
                "close": [100.5, 101.5, 102.5],
                "volume": [1000, 1100, 1200],
            }
        ),
    )

    class _FakeSignalReplayAdapter:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            adapter_init_kwargs.update(kwargs)

        def run(self, **kwargs):
            progress_callback = kwargs.get("progress_callback")
            if callable(progress_callback):
                progress_callback(50.0, "halfway")
                progress_callback(100.0, "done")
            return BacktestResult(
                meta=BacktestMeta(
                    id="signal-replay-unit",
                    created_at="2026-03-12T12:00:00Z",
                    engine="signal-replay",
                    strategies=["Signal Replay"],
                    backtest_start="2024-01-01",
                    backtest_end="2024-01-02",
                    data={"symbols": ["AAPL"], "timeframes": ["1h"], "source": "discord"},
                    initial_capital=100000.0,
                    tags=["signal-replay"],
                    notes="Signal Replay",
                ),
                trades=[],
                equity_curve=[],
            )

    monkeypatch.setattr(viewer_app_module, "SignalReplayAdapter", _FakeSignalReplayAdapter)

    response = viewer_client.post(
        "/api/backtests/run-signals",
        json={
            "signal_source": "discord",
            "signal_processor": "ramjet",
            "account_id": "acct-1",
            "start_date": "2024-01-01",
            "end_date": "2024-01-02",
            "timeframe": "1h",
            "auto_download": False,
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["task_id"] == payload["job_id"]
    assert payload["status"] == "queued"
    task_id = payload["task_id"]

    for _ in range(50):
        task = viewer_app_module.DATA_TASKS[task_id]
        if task["status"] in {"completed", "failed"}:
            break
        time.sleep(0.05)

    task = viewer_app_module.DATA_TASKS[task_id]
    assert task["kind"] == "backtest"
    assert task["status"] == "completed"
    assert task["result"]["backtest_id"] == "signal-replay-unit"
    assert task["result"]["trade_count"] == 0
    assert task["progress"] == 100.0

    assert replay_query_args["deployment_ids"] == ["dep-1"]
    assert replay_query_args["source"] == "discord"
    assert replay_query_args["limit"] == 20000
    assert adapter_init_kwargs["signal_processor_name"] == "ramjet"


def test_backtest_run_from_signals_csv_forwards_to_replay(viewer_client, monkeypatch, tmp_path):
    import viewer.main as viewer_app_module

    csv_path = tmp_path / "ohlcv" / "signals.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "\n".join(
            [
                "Symbol;Signal Price;Datum;Zeit;Signal Type",
                "AAPL;173,79;2026-02-23;09:32:00-05:00;BUY",
                "TSLA;250,10;2026-02-23;10:07:00-05:00;SELL",
            ]
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    async def _fake_run_backtest_signals(payload):
        captured["payload"] = payload
        return {"task_id": "sigcsv01", "job_id": "sigcsv01", "status": "queued"}

    monkeypatch.setattr(viewer_app_module, "run_backtest_signals", _fake_run_backtest_signals)

    response = viewer_client.post(
        "/api/backtests/run-from-signals",
        json={
            "signal_source": "csv",
            "filepath": str(csv_path),
            "signal_processor": "ramjet",
            "start_date": "2026-02-23",
            "end_date": "2026-02-24",
            "auto_download": True,
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    forwarded = captured["payload"]
    assert forwarded is not None
    assert forwarded.timeframe == "1m"
    assert forwarded.signal_processor == "ramjet"
    assert forwarded.signal_source is None
    assert len(forwarded.signals) == 2
    assert str(forwarded.signals[0].source).startswith("csv:")


def test_backtest_run_from_signals_csv_date_only_end_is_inclusive(viewer_client, monkeypatch, tmp_path):
    import viewer.main as viewer_app_module

    csv_path = tmp_path / "ohlcv" / "signals.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "\n".join(
            [
                "Symbol;Signal Price;Datum;Zeit;Signal Type",
                "AAPL;173,79;2026-02-23;09:32:00-05:00;BUY",
            ]
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    async def _fake_run_backtest_signals(payload):
        captured["payload"] = payload
        return {"task_id": "sigcsv02", "job_id": "sigcsv02", "status": "queued"}

    monkeypatch.setattr(viewer_app_module, "run_backtest_signals", _fake_run_backtest_signals)

    response = viewer_client.post(
        "/api/backtests/run-from-signals",
        json={
            "signal_source": "csv",
            "filepath": str(csv_path),
            "signal_processor": "ramjet",
            "start_date": "2026-02-23",
            "end_date": "2026-02-23",
        },
    )

    assert response.status_code == 202
    forwarded = captured["payload"]
    assert forwarded is not None
    assert len(forwarded.signals) == 1
    assert forwarded.signals[0].symbol == "AAPL"


def test_thread_bound_task_log_handler_emits_without_shadowing_handle(viewer_client, tmp_path):
    import viewer.main as viewer_app_module

    task_id = "optulog1"
    log_file = tmp_path / "logs" / "tasks" / f"task_{task_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("optuna.test-handler")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate

    with log_file.open("a", encoding="utf-8", buffering=1) as handle:
        handler = viewer_app_module._ThreadBoundTaskLogHandler(
            task_id=task_id,
            handle=handle,
            source="optimization",
        )
        logger.handlers = []
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(handler)
        try:
            logger.info("optuna logger smoke test")
        finally:
            logger.removeHandler(handler)
            logger.handlers = original_handlers
            logger.setLevel(original_level)
            logger.propagate = original_propagate

    assert "optuna logger smoke test" in log_file.read_text(encoding="utf-8")


def test_signal_processors_endpoint_exposes_registry(viewer_client):
    response = viewer_client.get("/api/signal-processors")
    assert response.status_code == 200
    payload = response.json()
    ids = {item["id"] for item in payload}
    assert ids == {"ramjet", "rocketfuel_longsonly", "rocketfuel_longandshort", "nitro"}


def test_optimizations_list_and_detail_endpoints(viewer_client, tmp_path):
    optimizations_dir = tmp_path / "optimizations"
    optimizations_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": "opt_base-demo_20260311_100000",
        "file": "opt_base-demo_20260311_100000.json",
        "created_at": "2026-03-11T10:00:00+00:00",
        "base_backtest_id": "base-demo",
        "target_metric": "sharpe",
        "n_trials": 25,
        "train_split_pct": 0.7,
        "best_value": 1.2345,
        "best_params": {"window": 10},
        "effective_best_parameters": {"window": 10, "fixed": 3},
        "symbols": ["SPY", "TLT", "GLD"],
        "best_symbols": ["SPY", "TLT"],
        "optimization_mode": "risk_parity_universe",
        "symbol_universes": [["SPY", "TLT"], ["SPY", "GLD"]],
        "train_period": {
            "start": "2024-01-01T00:00:00+00:00",
            "end": "2024-01-07T23:59:59+00:00",
        },
        "test_period": {
            "start": "2024-01-08T00:00:00+00:00",
            "end": "2024-01-10T23:59:59+00:00",
        },
        "train_stats": {"sharpe_ratio": 1.2345},
        "test_stats": {"sharpe_ratio": 0.9876},
        "top_trials": [
            {"number": 0, "value": 1.2345, "params": {"window": 10}, "symbols": ["SPY", "TLT"]}
        ],
    }
    (optimizations_dir / "opt_base-demo_20260311_100000.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    list_resp = viewer_client.get("/api/optimizations")
    assert list_resp.status_code == 200
    listed = list_resp.json()
    assert listed == [
        {
            "id": "opt_base-demo_20260311_100000",
            "filename": "opt_base-demo_20260311_100000.json",
            "created_at": "2026-03-11T10:00:00+00:00",
            "base_backtest_id": "base-demo",
            "target_metric": "sharpe",
            "n_trials": 25,
            "train_split_pct": 0.7,
            "best_value": 1.2345,
            "best_params": {"window": 10},
            "effective_best_parameters": {"window": 10, "fixed": 3},
            "symbols": ["SPY", "TLT", "GLD"],
            "best_symbols": ["SPY", "TLT"],
            "optimization_mode": "risk_parity_universe",
            "symbol_universes": [["SPY", "TLT"], ["SPY", "GLD"]],
            "train_period": {
                "start": "2024-01-01T00:00:00+00:00",
                "end": "2024-01-07T23:59:59+00:00",
            },
            "test_period": {
                "start": "2024-01-08T00:00:00+00:00",
                "end": "2024-01-10T23:59:59+00:00",
            },
            "train_stats": {"sharpe_ratio": 1.2345},
            "test_stats": {"sharpe_ratio": 0.9876},
        }
    ]

    detail_resp = viewer_client.get("/api/optimizations/opt_base-demo_20260311_100000")
    assert detail_resp.status_code == 200
    assert detail_resp.json() == payload


def _equity_curve_from_returns(
    returns: list[float],
    *,
    initial_capital: float = 100000.0,
    start: str = "2024-01-01T00:00:00+00:00",
) -> list[EquityPoint]:
    start_ts = pd.Timestamp(start)
    points = [EquityPoint(time=start_ts.isoformat(), equity=initial_capital)]
    equity = initial_capital
    for offset, daily_return in enumerate(returns, start=1):
        equity *= 1 + daily_return
        points.append(
            EquityPoint(
                time=(start_ts + pd.Timedelta(days=offset)).isoformat(),
                equity=equity,
            )
        )
    return points


def test_backtest_stats_endpoint_includes_p_value(viewer_client, tmp_path):
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)

    result = BacktestResult(
        meta=BacktestMeta(
            id="pvalue-demo",
            created_at="2026-03-07T15:00:00Z",
            engine="vectorbt",
            strategies=["PValue Demo"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            initial_capital=100000.0,
            duration_days=30,
            data={"symbols": ["AAPL"], "timeframes": ["1d"], "source": "test_source"},
        ),
        trades=[],
        equity_curve=_equity_curve_from_returns(
            [0.010, 0.012, 0.009, 0.014, 0.011, 0.013, 0.008, 0.012, 0.010, 0.011, 0.009, 0.013]
        ),
    )
    result.save(backtests_dir / "pvalue-demo.trades.json")

    resp = viewer_client.get("/api/backtest/pvalue-demo/stats")

    assert resp.status_code == 200
    payload = resp.json()
    assert 0.0 <= payload["p_value"] <= 1.0
    assert payload["p_value"] < 0.05


def _close_prices_from_returns(returns: list[float], *, start_price: float = 100.0) -> list[float]:
    closes = [start_price]
    for daily_return in returns:
        closes.append(closes[-1] * (1 + daily_return))
    return closes


def test_backtest_capm_endpoint_returns_regression_payload(viewer_client, tmp_path):
    backtests_dir = tmp_path / "backtests"
    ohlcv_dir = tmp_path / "ohlcv" / "test_source" / "equities" / "SPY"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    ohlcv_dir.mkdir(parents=True, exist_ok=True)

    market_returns = [0.012, -0.008, 0.01, 0.003, -0.004, 0.009, 0.001, -0.002]
    alpha_daily = 0.0002
    beta = 0.8
    strategy_returns = [alpha_daily + beta * value for value in market_returns]

    benchmark_df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01T00:00:00Z", periods=len(market_returns) + 1, freq="D"),
            "open": _close_prices_from_returns(market_returns),
            "high": _close_prices_from_returns(market_returns),
            "low": _close_prices_from_returns(market_returns),
            "close": _close_prices_from_returns(market_returns),
            "volume": [1000] * (len(market_returns) + 1),
        }
    )
    benchmark_df.to_parquet(ohlcv_dir / "1d.parquet", index=False)

    result = BacktestResult(
        meta=BacktestMeta(
            id="capm-demo",
            created_at="2026-03-18T19:00:00Z",
            engine="vectorbt",
            strategies=["CAPM Demo"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-09",
            initial_capital=100000.0,
            duration_days=8,
            data={"symbols": ["AAPL"], "timeframes": ["1d"], "source": "test_source"},
        ),
        trades=[],
        equity_curve=_equity_curve_from_returns(strategy_returns),
    )
    result.save(backtests_dir / "capm-demo.trades.json")

    resp = viewer_client.get("/api/backtest/capm-demo/capm?benchmark=SPY")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["benchmark_symbol"] == "SPY"
    assert payload["beta"] == pytest.approx(beta, rel=1e-6)
    assert payload["alpha"] == pytest.approx(alpha_daily * 252, rel=1e-6)
    assert payload["r_squared"] == pytest.approx(1.0, rel=1e-6)
    assert payload["p_value"] < 1e-6
    assert len(payload["scatter_data"]) == len(market_returns)
    assert payload["regression_line"]["slope"] == pytest.approx(beta, rel=1e-6)
    assert payload["regression_line"]["intercept"] == pytest.approx(alpha_daily, rel=1e-6)


def test_backtest_capm_endpoint_uses_first_symbol_when_benchmark_missing(viewer_client, tmp_path):
    backtests_dir = tmp_path / "backtests"
    ohlcv_dir = tmp_path / "ohlcv" / "test_source" / "equities" / "SPY"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    ohlcv_dir.mkdir(parents=True, exist_ok=True)

    market_returns = [0.01, -0.004, 0.006, 0.003, -0.002]
    closes = _close_prices_from_returns(market_returns)
    benchmark_df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01T00:00:00Z", periods=len(closes), freq="D"),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000] * len(closes),
        }
    )
    benchmark_df.to_parquet(ohlcv_dir / "1d.parquet", index=False)

    result = BacktestResult(
        meta=BacktestMeta(
            id="capm-default-benchmark",
            created_at="2026-03-18T19:00:00Z",
            engine="vectorbt",
            strategies=["CAPM Default Benchmark"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-06",
            initial_capital=100000.0,
            duration_days=5,
            data={"symbols": ["SPY"], "timeframes": ["1d"], "source": "test_source"},
        ),
        trades=[],
        equity_curve=_equity_curve_from_returns([0.008, -0.002, 0.004, 0.002, -0.001]),
    )
    result.save(backtests_dir / "capm-default-benchmark.trades.json")

    resp = viewer_client.get("/api/backtest/capm-default-benchmark/capm")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["benchmark_symbol"] == "SPY"
    assert "alpha" in payload
    assert "beta" in payload
    assert "r_squared" in payload
    assert "p_value" in payload


def test_backtests_list_uses_persisted_runner_stats(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(
        viewer_app_module,
        "list_backtest_metadata_rows",
        lambda: [
            {
                "id": "cached-stats-demo",
                "engine": "vectorbt",
                "strategy_name": "Cached Stats Demo",
                "symbols": ["AAPL"],
                "timeframe": "1d",
                "start_date": "2024-01-01",
                "end_date": "2024-01-31",
                "total_return": 0.012345,
                "cagr": 0.0345,
                "sharpe_ratio": 7.777,
                "psr": 0.8123,
                "p_value": 0.012345,
                "max_drawdown": -0.2345,
                "trade_count": 0,
                "created_at": "2026-03-07T15:00:00Z",
            }
        ],
    )
    with viewer_app_module._BACKTEST_META_CACHE_LOCK:
        viewer_app_module._BACKTEST_META_CACHE.clear()

    resp = viewer_client.get("/api/backtests")

    assert resp.status_code == 200
    payload = resp.json()["items"]
    assert len(payload) == 1

    item = payload[0]
    assert item["id"] == "cached-stats-demo"
    assert item["filename"] == "cached-stats-demo.trades.json"
    assert item["title"] == "Cached Stats Demo"
    assert item["strategy"] == "Cached Stats Demo"
    assert item["strategy_slug"] == "Cached Stats Demo"
    assert item["strategy_display_name"] == "Cached Stats Demo"
    assert item["engine"] == "vectorbt"
    assert item["symbols"] == ["AAPL"]
    assert item["timeframes"] == ["1d"]
    assert item["trade_count"] == 0
    assert item["start"] == "2024-01-01"
    assert item["end"] == "2024-01-31"
    assert item["created_at"] == "2026-03-07T15:00:00Z"
    assert item["sharpe"] == 7.777
    assert item["sharpe_ratio"] == 7.777
    assert item["cagr"] == 3.45
    assert item["max_drawdown_pct"] == 23.45
    assert item["net_profit"] == 0.0
    assert item["net_profit_pct"] == 1.23
    assert item["psr"] == pytest.approx(0.8123, abs=1e-4)
    assert item["p_value"] == pytest.approx(0.012345, abs=1e-6)
    assert item["equity_mini"] == []


def test_backtests_list_filters_and_sorts_new_stat_fields(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(
        viewer_app_module,
        "list_backtest_metadata_rows",
        lambda: [
            {
                "id": "bt-a",
                "engine": "vectorbt",
                "strategy_name": "A",
                "symbols": ["AAPL"],
                "timeframe": "1d",
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
                "total_return": 0.10,
                "cagr": 0.08,
                "sharpe_ratio": -0.5,
                "psr": 0.40,
                "p_value": 0.20,
                "max_drawdown": -0.10,
                "trade_count": 5,
                "created_at": "2026-03-07T15:00:00Z",
            },
            {
                "id": "bt-b",
                "engine": "vectorbt",
                "strategy_name": "B",
                "symbols": ["MSFT"],
                "timeframe": "1d",
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
                "total_return": 0.20,
                "cagr": 0.15,
                "sharpe_ratio": 0.2,
                "psr": 0.80,
                "p_value": 0.03,
                "max_drawdown": -0.15,
                "trade_count": 10,
                "created_at": "2026-03-08T15:00:00Z",
            },
        ],
    )
    with viewer_app_module._BACKTEST_META_CACHE_LOCK:
        viewer_app_module._BACKTEST_META_CACHE.clear()

    resp = viewer_client.get(
        "/api/backtests?sort=psr&order=desc&sharpe_min=-1&sharpe_max=0&psr_min=0.3&p_value_max=0.5"
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == "bt-a"


def test_backtests_all_returns_cached_metadata_payload(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(
        viewer_app_module,
        "list_backtest_metadata_rows",
        lambda: [
            {
                "id": "bt-old",
                "engine": "vectorbt",
                "strategy_name": "Old Strategy",
                "symbols": ["AAPL"],
                "timeframe": "1d",
                "start_date": "2024-01-01",
                "end_date": "2024-06-30",
                "total_return": 0.05,
                "cagr": 0.03,
                "sharpe_ratio": 0.8,
                "psr": 0.6,
                "p_value": 0.2,
                "max_drawdown": -0.1,
                "win_rate": 0.55,
                "trade_count": 14,
                "created_at": "2026-03-01T10:00:00Z",
                "tags": ["baseline"],
            },
            {
                "id": "bt-new",
                "engine": "vectorbt",
                "strategy_name": "New Strategy",
                "symbols": ["MSFT", "NVDA"],
                "timeframe": "4h",
                "start_date": "2024-07-01",
                "end_date": "2024-12-31",
                "total_return": 0.21,
                "cagr": 0.18,
                "sharpe_ratio": 1.45,
                "psr": 0.92,
                "p_value": 0.02,
                "max_drawdown": -0.08,
                "win_rate": 72.5,
                "trade_count": 33,
                "created_at": "2026-03-09T10:00:00Z",
                "tags": ["signal-replay", "source:discord"],
            },
        ],
    )
    with viewer_app_module._BACKTEST_META_CACHE_LOCK:
        viewer_app_module._BACKTEST_META_CACHE.clear()
    with viewer_app_module._BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        viewer_app_module._BACKTEST_EQUITY_PREVIEW_CACHE.clear()

    resp = viewer_client.get("/api/backtests/all")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2

    assert rows[0]["id"] == "bt-new"
    assert rows[0]["strategy_name"] == "New Strategy"
    assert rows[0]["symbols"] == ["MSFT", "NVDA"]
    assert rows[0]["timeframe"] == "4h"
    assert rows[0]["total_return"] == pytest.approx(0.21)
    assert rows[0]["sharpe_ratio"] == pytest.approx(1.45)
    assert rows[0]["psr"] == pytest.approx(0.92)
    assert rows[0]["p_value"] == pytest.approx(0.02)
    assert rows[0]["cagr"] == pytest.approx(0.18)
    assert rows[0]["max_drawdown"] == pytest.approx(0.08)
    assert rows[0]["win_rate"] == pytest.approx(0.725)
    assert rows[0]["trade_count"] == 33
    assert rows[0]["start_date"] == "2024-07-01"
    assert rows[0]["end_date"] == "2024-12-31"
    assert rows[0]["created_at"] == "2026-03-09T10:00:00Z"
    assert rows[0]["tags"] == ["signal-replay", "source:discord"]


def test_backtests_all_can_include_equity_preview(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(
        viewer_app_module,
        "list_backtest_metadata_rows",
        lambda: [
            {
                "id": "with-preview",
                "engine": "vectorbt",
                "strategy_name": "Preview Strategy",
                "symbols": ["AAPL"],
                "timeframe": "1h",
                "start_date": "2024-01-01",
                "end_date": "2024-01-07",
                "total_return": 0.11,
                "cagr": 0.08,
                "sharpe_ratio": 1.2,
                "psr": 0.9,
                "p_value": 0.03,
                "max_drawdown": -0.05,
                "win_rate": 0.61,
                "trade_count": 12,
                "created_at": "2026-03-12T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        viewer_app_module,
        "load_backtest_equity_preview_map",
        lambda backtest_ids, limit=50: {"with-preview": [100000.0, 100120.0, 100180.0]},
    )
    with viewer_app_module._BACKTEST_META_CACHE_LOCK:
        viewer_app_module._BACKTEST_META_CACHE.clear()
    with viewer_app_module._BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        viewer_app_module._BACKTEST_EQUITY_PREVIEW_CACHE.clear()

    response = viewer_client.get("/api/backtests/all?include_equity_preview=true&preview_limit=50")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["id"] == "with-preview"
    assert rows[0]["equity_preview"] == [100000.0, 100120.0, 100180.0]


def test_backtests_all_uses_cached_equity_preview_only(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(
        viewer_app_module,
        "list_backtest_metadata_rows",
        lambda: [
            {
                "id": "cache-preview",
                "engine": "vectorbt",
                "strategy_name": "Cache Strategy",
                "symbols": ["AAPL"],
                "timeframe": "1h",
                "start_date": "2024-01-01",
                "end_date": "2024-01-02",
                "total_return": 0.04,
                "cagr": 0.03,
                "sharpe_ratio": 0.9,
                "psr": 0.7,
                "p_value": 0.07,
                "max_drawdown": -0.03,
                "win_rate": 0.52,
                "trade_count": 9,
                "created_at": "2026-03-15T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        viewer_app_module,
        "load_backtest_equity_preview_map",
        lambda backtest_ids, limit=50: {"cache-preview": [100000.0, 100050.0]},
    )

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("Per-item equity preview DB loader should not be called for /api/backtests/all")

    monkeypatch.setattr(viewer_app_module, "load_backtest_equity_preview_points", _fail_if_called)

    with viewer_app_module._BACKTEST_META_CACHE_LOCK:
        viewer_app_module._BACKTEST_META_CACHE.clear()
    with viewer_app_module._BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        viewer_app_module._BACKTEST_EQUITY_PREVIEW_CACHE.clear()

    response = viewer_client.get("/api/backtests/all?include_equity_preview=true&preview_limit=50")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["equity_preview"] == [100000.0, 100050.0]


def test_backtests_all_uses_cached_preview_fallback_limit(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(
        viewer_app_module,
        "list_backtest_metadata_rows",
        lambda: [
            {
                "id": "fallback-preview",
                "engine": "vectorbt",
                "strategy_name": "Fallback Strategy",
                "symbols": ["AAPL"],
                "timeframe": "1h",
                "start_date": "2024-01-01",
                "end_date": "2024-01-02",
                "total_return": 0.04,
                "cagr": 0.03,
                "sharpe_ratio": 0.9,
                "psr": 0.7,
                "p_value": 0.07,
                "max_drawdown": -0.03,
                "win_rate": 0.52,
                "trade_count": 9,
                "created_at": "2026-03-15T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        viewer_app_module,
        "load_backtest_equity_preview_map",
        lambda backtest_ids, limit=50: {"fallback-preview": [100000.0, 100040.0, 100080.0]},
    )

    with viewer_app_module._BACKTEST_META_CACHE_LOCK:
        viewer_app_module._BACKTEST_META_CACHE.clear()
    with viewer_app_module._BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        viewer_app_module._BACKTEST_EQUITY_PREVIEW_CACHE.clear()

    response = viewer_client.get("/api/backtests/all?include_equity_preview=true&preview_limit=80")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["equity_preview"] == [100000.0, 100040.0, 100080.0]


def test_backtest_equity_preview_endpoint_uses_cache(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    calls = {"count": 0}

    monkeypatch.setattr(
        viewer_app_module,
        "list_backtest_metadata_rows",
        lambda: [
            {
                "id": "preview-demo",
                "engine": "vectorbt",
                "strategy_name": "Preview Demo",
                "symbols": ["AAPL"],
                "timeframe": "1d",
                "start_date": "2024-01-01",
                "end_date": "2024-01-31",
                "total_return": 0.01,
                "cagr": 0.01,
                "sharpe_ratio": 0.1,
                "psr": 0.2,
                "p_value": 0.9,
                "max_drawdown": -0.02,
                "win_rate": 0.5,
                "trade_count": 2,
                "created_at": "2026-03-10T10:00:00Z",
            }
        ],
    )

    def _fake_preview_loader(backtest_id: str, limit: int = 80):
        calls["count"] += 1
        assert backtest_id == "preview-demo"
        assert limit == 80
        return [
            EquityPoint(time="2024-01-01T00:00:00Z", equity=100000.0),
            EquityPoint(time="2024-01-02T00:00:00Z", equity=100250.0),
            EquityPoint(time="2024-01-03T00:00:00Z", equity=100180.0),
        ]

    monkeypatch.setattr(viewer_app_module, "load_backtest_equity_preview_points", _fake_preview_loader)
    with viewer_app_module._BACKTEST_META_CACHE_LOCK:
        viewer_app_module._BACKTEST_META_CACHE.clear()
    with viewer_app_module._BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        viewer_app_module._BACKTEST_EQUITY_PREVIEW_CACHE.clear()

    first = viewer_client.get("/api/backtests/preview-demo/equity-preview?limit=80")
    second = viewer_client.get("/api/backtests/preview-demo/equity-preview?limit=80")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == [100000.0, 100250.0, 100180.0]
    assert second.json() == [100000.0, 100250.0, 100180.0]
    assert calls["count"] == 1


def test_strategies_endpoint_includes_latest_equity_preview(viewer_client, monkeypatch, tmp_path):
    import viewer.main as viewer_app_module

    strategy_file = tmp_path / "demo_strategy.py"
    strategy_file.write_text("STRATEGY_META = {}", encoding="utf-8")
    entry = viewer_app_module.StrategyEntry(
        slug="demo-strategy",
        file_path=strategy_file,
        relative_file="demo_strategy.py",
        directory=tmp_path,
        documentation_path=None,
        meta={
            "name": "Demo Strategy",
            "version": "1.0.0",
            "source": "tests",
            "tags": {"type": ["momentum"], "asset_class": ["equity"], "complexity": "medium"},
            "parameters": {"lookback": 20},
        },
    )

    warmed_ids: dict[str, list[str]] = {}

    monkeypatch.setattr(viewer_app_module, "_safe_strategy_entries", lambda: [entry])
    monkeypatch.setattr(
        viewer_app_module,
        "_snapshot_backtest_meta_cache",
        lambda: [
            {
                "id": "demo-old",
                "strategy_name": "Demo Strategy",
                "created_at": "2026-03-09T10:00:00Z",
                "sharpe_ratio": 2.0,
            },
            {
                "id": "demo-latest",
                "strategy_name": "Demo Strategy",
                "created_at": "2026-03-12T12:30:00Z",
                "sharpe_ratio": 1.2,
            },
        ],
    )
    monkeypatch.setattr(
        viewer_app_module,
        "_bulk_warm_backtest_equity_preview",
        lambda backtest_ids, limit=50: warmed_ids.setdefault("ids", list(backtest_ids)),
    )
    monkeypatch.setattr(
        viewer_app_module,
        "_cached_backtest_equity_preview_only",
        lambda backtest_id, limit=50: [100000.0, 100200.0, 100350.0] if backtest_id == "demo-latest" else [],
    )
    with viewer_app_module._STRATEGIES_CACHE_LOCK:
        viewer_app_module._STRATEGIES_CACHE["payload"] = []
        viewer_app_module._STRATEGIES_CACHE["expires_at"] = 0.0

    response = viewer_client.get("/api/strategies")
    assert response.status_code == 200
    payload = response.json()
    row = next(item for item in payload if item.get("slug") == "demo-strategy")
    assert row["backtest_count"] == 2
    assert row["best_sharpe"] == pytest.approx(2.0)
    assert row["equity_preview"] == [100000.0, 100200.0, 100350.0]
    assert warmed_ids["ids"] == ["demo-latest"]


def test_backtests_cache_refreshes_when_sqlite_changes(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    call_count = {"value": 0}

    def _list_rows():
        call_count["value"] += 1
        if call_count["value"] == 1:
            return [
                {
                    "id": "cache-mtime-demo",
                    "engine": "vectorbt",
                    "strategy_name": "Cache Mtime Demo",
                    "symbols": ["AAPL"],
                    "timeframe": "1d",
                    "start_date": "2024-01-01",
                    "end_date": "2024-01-31",
                    "total_return": 0.01,
                    "cagr": 0.01,
                    "sharpe_ratio": 1.0,
                    "psr": 0.5,
                    "p_value": 0.2,
                    "max_drawdown": -0.02,
                    "win_rate": 0.5,
                    "trade_count": 10,
                    "created_at": "2026-03-10T10:00:00Z",
                }
            ]
        return [
            {
                "id": "cache-mtime-demo",
                "engine": "vectorbt",
                "strategy_name": "Cache Mtime Demo",
                "symbols": ["AAPL"],
                "timeframe": "1d",
                "start_date": "2024-01-01",
                "end_date": "2024-01-31",
                "total_return": 0.15,
                "cagr": 0.12,
                "sharpe_ratio": 1.0,
                "psr": 0.5,
                "p_value": 0.2,
                "max_drawdown": -0.02,
                "win_rate": 0.5,
                "trade_count": 10,
                "created_at": "2026-03-10T10:00:00Z",
            }
        ]

    class _FakeStat:
        def __init__(self, mtime_ns: int):
            self.st_mtime_ns = mtime_ns

    mtime_state = {"value": 100}

    class _FakeDbPath:
        @staticmethod
        def stat():
            return _FakeStat(mtime_state["value"])

    monkeypatch.setattr(viewer_app_module, "list_backtest_metadata_rows", _list_rows)
    monkeypatch.setattr(viewer_app_module, "BACKTEST_DB_PATH", _FakeDbPath())
    monkeypatch.setattr(
        viewer_app_module,
        "load_backtest_equity_preview_map",
        lambda backtest_ids, limit=50: {"cache-mtime-demo": [100000.0, 100050.0]},
    )

    with viewer_app_module._BACKTEST_META_CACHE_LOCK:
        viewer_app_module._BACKTEST_META_CACHE.clear()
        viewer_app_module._BACKTEST_META_CACHE_SOURCE_MTIME_NS = 0
    with viewer_app_module._BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        viewer_app_module._BACKTEST_EQUITY_PREVIEW_CACHE.clear()

    first = viewer_client.get("/api/backtests")
    assert first.status_code == 200
    assert first.json()["items"][0]["cagr"] == pytest.approx(1.0)
    assert call_count["value"] == 1

    mtime_state["value"] = 200
    second = viewer_client.get("/api/backtests")
    assert second.status_code == 200
    assert second.json()["items"][0]["cagr"] == pytest.approx(12.0)
    assert call_count["value"] == 2


def test_backtests_bulk_delete_and_single_delete(viewer_client, tmp_path):
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)

    result_a = BacktestResult(
        meta=BacktestMeta(
            id="delete-a",
            created_at="2026-03-09T09:00:00Z",
            engine="vectorbt",
            strategies=["demo"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            initial_capital=100000.0,
            data={"symbols": ["AAPL"], "timeframes": ["1d"], "source": "test_source"},
        ),
        trades=[],
        equity_curve=[],
    )
    result_b = BacktestResult(
        meta=BacktestMeta(
            id="delete-b",
            created_at="2026-03-09T09:10:00Z",
            engine="vectorbt",
            strategies=["demo"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            initial_capital=100000.0,
            data={"symbols": ["AAPL"], "timeframes": ["1d"], "source": "test_source"},
        ),
        trades=[],
        equity_curve=[],
    )
    result_a.save(backtests_dir / "delete-a.trades.json")
    result_b.save(backtests_dir / "delete-b.trades.json")

    bulk_resp = viewer_client.post("/api/backtests/delete-bulk", json={"ids": ["delete-a", "delete-b"]})
    assert bulk_resp.status_code == 200
    assert bulk_resp.json()["deleted"] == 2
    assert not (backtests_dir / "delete-a.trades.json").exists()
    assert not (backtests_dir / "delete-b.trades.json").exists()

    result_c = BacktestResult(
        meta=BacktestMeta(
            id="delete-c",
            created_at="2026-03-09T09:20:00Z",
            engine="vectorbt",
            strategies=["demo"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            initial_capital=100000.0,
            data={"symbols": ["AAPL"], "timeframes": ["1d"], "source": "test_source"},
        ),
        trades=[],
        equity_curve=[],
    )
    result_c.save(backtests_dir / "delete-c.trades.json")
    single_resp = viewer_client.delete("/api/backtests/delete-c")
    assert single_resp.status_code == 200
    assert single_resp.json()["status"] == "deleted"
    assert not (backtests_dir / "delete-c.trades.json").exists()


def test_backtest_delete_database_failure_restores_json_and_reports_failed(
    viewer_client, monkeypatch, tmp_path
):
    import viewer.main as viewer_app_module

    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    artifact = backtests_dir / "delete-failure.trades.json"
    artifact.write_text("{}", encoding="utf-8")

    def fail_delete(_backtest_id: str) -> bool:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(viewer_app_module, "delete_backtest", fail_delete)
    response = viewer_client.post(
        "/api/backtests/delete-bulk", json={"ids": ["delete-failure"]}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] == 0
    assert payload["items"][0]["status"] == "failed"
    assert payload["items"][0]["database"] == "failed"
    assert payload["items"][0]["artifact"] == "restored"
    assert artifact.exists()
    assert not (backtests_dir / ".delete-failure.trades.json.delete-staged").exists()


def test_backtest_delete_cleanup_failure_is_recoverable_and_truthful(
    viewer_client, monkeypatch, tmp_path
):
    import viewer.main as viewer_app_module

    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    artifact = backtests_dir / "delete-pending.trades.json"
    artifact.write_text("{}", encoding="utf-8")
    database_deletes: list[str] = []
    original_unlink = Path.unlink

    def delete_from_db(backtest_id: str) -> bool:
        database_deletes.append(backtest_id)
        return True

    def fail_staged_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.name.endswith(".delete-staged"):
            raise OSError("artifact cleanup failed")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(viewer_app_module, "delete_backtest", delete_from_db)
    monkeypatch.setattr(Path, "unlink", fail_staged_unlink)
    response = viewer_client.post(
        "/api/backtests/delete-bulk", json={"ids": ["delete-pending"]}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] == 0
    assert payload["items"][0]["status"] == "cleanup_pending"
    assert payload["items"][0]["database"] == "deleted"
    assert payload["items"][0]["artifact"] == "staged"
    assert not artifact.exists()
    staged = backtests_dir / ".delete-pending.trades.json.delete-staged"
    journal = backtests_dir / ".delete-pending.trades.json.delete-journal.json"
    assert staged.exists()
    assert journal.exists()

    monkeypatch.setattr(Path, "unlink", original_unlink)
    viewer_app_module._recover_maintenance_deletes()

    assert database_deletes == ["delete-pending"]
    assert not staged.exists()
    assert not journal.exists()


def test_portfolio_delete_database_failure_restores_json_and_reports_failed(
    viewer_client, monkeypatch, tmp_path
):
    import viewer.main as viewer_app_module

    portfolios_dir = tmp_path / "portfolios"
    portfolios_dir.mkdir(parents=True, exist_ok=True)
    artifact = portfolios_dir / "delete-failure.json"
    artifact.write_text("{}", encoding="utf-8")

    def fail_delete(_portfolio_id: str) -> bool:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(viewer_app_module, "delete_portfolio_record", fail_delete)
    response = viewer_client.post(
        "/api/portfolios/delete-bulk", json={"ids": ["delete-failure"]}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] == 0
    assert payload["items"][0]["status"] == "failed"
    assert payload["items"][0]["database"] == "failed"
    assert payload["items"][0]["artifact"] == "restored"
    assert artifact.exists()


def test_create_portfolio_persists_p_value_and_psr(viewer_client, tmp_path):
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)

    result = BacktestResult(
        meta=BacktestMeta(
            id="portfolio-source",
            created_at="2026-03-07T15:00:00Z",
            engine="vectorbt",
            strategies=["Portfolio Signal"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            initial_capital=100000.0,
            duration_days=30,
            data={"symbols": ["AAPL"], "timeframes": ["1d"], "source": "test_source"},
        ),
        trades=[
            Trade(
                id=1,
                symbol="AAPL",
                direction="LONG",
                entry_time="2024-01-02T00:00:00+00:00",
                entry_price=100.0,
                quantity=10.0,
                exits=[
                    TradeExit(
                        time="2024-01-10T00:00:00+00:00",
                        price=105.0,
                        quantity=10.0,
                        pnl=50.0,
                    )
                ],
                pnl=50.0,
            )
        ],
        equity_curve=_equity_curve_from_returns(
            [0.010, 0.012, 0.009, 0.014, 0.011, 0.013, 0.008, 0.012, 0.010, 0.011, 0.009, 0.013]
        ),
    )
    result.save(backtests_dir / "portfolio-source.trades.json")

    create_resp = viewer_client.post(
        "/api/portfolios",
        json={
            "name": "Signal Basket",
            "account_capital": 100000,
            "components": [
                {
                    "strategy_name": "Portfolio Signal",
                    "symbol": "AAPL",
                    "timeframe": "1d",
                    "weight": 1.0,
                    "risk_config": {"max_positions": 5},
                    "settings": {},
                    "backtest_id": "portfolio-source",
                }
            ],
        },
    )

    assert create_resp.status_code == 201
    payload = create_resp.json()
    assert payload["combined_stats"]["p_value"] < 0.05
    assert "psr" in payload["combined_stats"]

    list_resp = viewer_client.get("/api/portfolios")
    assert list_resp.status_code == 200
    listed = list_resp.json()
    assert listed[0]["p_value"] == pytest.approx(payload["combined_stats"]["p_value"], abs=1e-6)

    detail_resp = viewer_client.get(f"/api/portfolios/{payload['id']}")
    assert detail_resp.status_code == 200
    detail_payload = detail_resp.json()
    assert detail_payload["combined_stats"]["p_value"] == pytest.approx(
        payload["combined_stats"]["p_value"],
        abs=1e-6,
    )
    assert len(detail_payload.get("components", [])) == 1

    contributions_resp = viewer_client.get(f"/api/portfolios/{payload['id']}/contributions?group_by=strategy")
    assert contributions_resp.status_code == 200
    contributions = contributions_resp.json()
    assert contributions
    assert contributions[0]["group"] == "strategy"
    assert contributions[0]["trade_count"] >= 1

    trades_resp = viewer_client.get(
        f"/api/portfolios/{payload['id']}/trades?strategy=Portfolio Signal&symbol=AAPL&timeframe=1d"
    )
    assert trades_resp.status_code == 200
    trades_payload = trades_resp.json()
    assert trades_payload
    assert trades_payload[0]["strategy_name"] == "Portfolio Signal"
    assert trades_payload[0]["symbol"] == "AAPL"
    assert trades_payload[0]["timeframe"] == "1d"


def test_create_portfolio_allows_empty_components(viewer_client):
    response = viewer_client.post(
        "/api/portfolios",
        json={
            "name": "Empty Portfolio",
            "account_capital": 50000.0,
            "components": [],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["name"] == "Empty Portfolio"
    assert payload["components"] == []
    assert payload["trades"] == []
    assert payload["combined_stats"]["total_return"] == pytest.approx(0.0, abs=1e-9)


def test_portfolio_contributions_fallback_to_backtest_metrics(viewer_client, monkeypatch, tmp_path):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(viewer_app_module, "list_portfolio_records", lambda: [])
    with viewer_app_module._PORTFOLIO_META_CACHE_LOCK:
        viewer_app_module._PORTFOLIO_META_CACHE.clear()
        viewer_app_module._PORTFOLIO_META_CACHE_WARMED = False

    portfolios_dir = tmp_path / "portfolios"
    portfolios_dir.mkdir(parents=True, exist_ok=True)
    (portfolios_dir / "portfolio-contrib-fallback.json").write_text(
        json.dumps(
            {
                "id": "portfolio-contrib-fallback",
                "name": "Contribution Fallback",
                "account_capital": 100000.0,
                "components": [
                    {
                        "component_id": "component-001",
                        "strategy_name": "Fallback Strat",
                        "symbol": "AAPL",
                        "timeframe": "1h",
                        "weight": 0.5,
                        "risk_config": {},
                        "settings": {},
                        "backtest_id": "fallback-backtest",
                        "return_contribution": 0.0,
                        "drawdown_contribution": 0.0,
                    }
                ],
                "trades": [{"component_id": "component-001", "pnl": 50.0}],
                "combined_stats": {"total_return": 0.2, "max_drawdown": 0.15, "net_profit": 50.0},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        viewer_app_module,
        "_load_result",
        lambda backtest_id: (SimpleNamespace(stats={}, trades=[]) if backtest_id == "fallback-backtest" else None),
    )
    monkeypatch.setattr(
        viewer_app_module.legacy_viewer,
        "_canonical_stats_from_result",
        lambda _result: {
            "total_return": 0.18,
            "max_drawdown": 0.12,
            "sharpe_ratio": 1.4,
            "psr": 0.88,
            "p_value": 0.03,
            "win_rate": 0.55,
            "cagr": 0.24,
        },
    )

    response = viewer_client.get("/api/portfolios/portfolio-contrib-fallback/contributions?group_by=strategy")
    assert response.status_code == 200
    rows = response.json()
    assert rows
    row = rows[0]
    assert row["group"] == "strategy"
    assert row["return_contribution"] == pytest.approx(0.09, abs=1e-9)
    assert row["drawdown_contribution"] == pytest.approx(0.06, abs=1e-9)
    assert row["cagr"] == pytest.approx(0.24, abs=1e-9)
    assert row["trade_count"] == 1


def test_remove_empty_portfolios_endpoint_removes_only_empty(viewer_client, monkeypatch, tmp_path):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(viewer_app_module, "list_portfolio_records", lambda: [])
    monkeypatch.setattr(viewer_app_module, "delete_portfolio_record", lambda _portfolio_id: False)

    portfolios_dir = tmp_path / "portfolios"
    portfolios_dir.mkdir(parents=True, exist_ok=True)
    (portfolios_dir / "portfolio-empty.json").write_text(
        json.dumps(
            {
                "id": "portfolio-empty",
                "name": "Empty Portfolio",
                "components": [],
                "trades": [],
                "combined_stats": {},
            }
        ),
        encoding="utf-8",
    )
    (portfolios_dir / "portfolio-keep.json").write_text(
        json.dumps(
            {
                "id": "portfolio-keep",
                "name": "Active Portfolio",
                "components": [
                    {
                        "component_id": "component-001",
                        "strategy_name": "Keep Strategy",
                        "symbol": "AAPL",
                        "timeframe": "1h",
                        "weight": 1.0,
                        "risk_config": {},
                        "settings": {},
                        "backtest_id": "",
                    }
                ],
                "trades": [],
                "combined_stats": {},
            }
        ),
        encoding="utf-8",
    )

    response = viewer_client.post("/api/portfolios/remove-empty")

    assert response.status_code == 200
    payload = response.json()
    assert payload["removed"] == 1
    assert payload["removed_ids"] == ["portfolio-empty"]
    assert payload["remaining"] >= 1
    assert not (portfolios_dir / "portfolio-empty.json").exists()
    assert (portfolios_dir / "portfolio-keep.json").exists()


def test_portfolio_trades_normalize_qty_and_side_fields(viewer_client, monkeypatch, tmp_path):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(viewer_app_module, "list_portfolio_records", lambda: [])

    portfolios_dir = tmp_path / "portfolios"
    portfolios_dir.mkdir(parents=True, exist_ok=True)
    (portfolios_dir / "portfolio-trade-fields.json").write_text(
        json.dumps(
            {
                "id": "portfolio-trade-fields",
                "name": "Trade Fields",
                "account_capital": 100000.0,
                "components": [
                    {
                        "component_id": "component-001",
                        "strategy_name": "Field Strategy",
                        "symbol": "AAPL",
                        "timeframe": "1h",
                        "weight": 1.0,
                        "risk_config": {},
                        "settings": {},
                        "backtest_id": "",
                    }
                ],
                "trades": [
                    {
                        "nr": 3,
                        "component_id": "component-001",
                        "strategy_name": "Field Strategy",
                        "symbol": "AAPL",
                        "timeframe": "1h",
                        "side": "short",
                        "qty": "2.5",
                        "entry": "100.5",
                        "entry_time": "2024-01-01T10:00:00Z",
                        "exits": [{"time": "2024-01-01T12:00:00Z", "price": "99.0", "qty": "2.5"}],
                        "pnl": "3.75",
                    }
                ],
                "combined_stats": {},
            }
        ),
        encoding="utf-8",
    )

    response = viewer_client.get("/api/portfolios/portfolio-trade-fields/trades")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["size"] == pytest.approx(2.5)
    assert payload[0]["entry_price"] == pytest.approx(100.5)
    assert payload[0]["exit_price"] == pytest.approx(99.0)
    assert payload[0]["side"] == "SELL"
    assert payload[0]["direction"] == "SHORT"
    assert payload[0]["symbol"] == "AAPL"


def test_portfolio_trades_enrich_missing_fields_from_backtest(viewer_client, monkeypatch, tmp_path):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(viewer_app_module, "list_portfolio_records", lambda: [])

    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    result = BacktestResult(
        meta=BacktestMeta(
            id="portfolio-enrich-source",
            created_at="2026-03-10T12:00:00Z",
            engine="vectorbt",
            strategies=["Enrich Strategy"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            initial_capital=100000.0,
            data={"symbols": ["AAPL"], "timeframes": ["1h"], "source": "test_source"},
        ),
        trades=[
            Trade(
                id=1,
                symbol="AAPL",
                direction="LONG",
                entry_time="2024-01-01T10:00:00Z",
                entry_price=100.0,
                quantity=2.0,
                exits=[
                    TradeExit(
                        time="2024-01-01T12:00:00Z",
                        price=103.0,
                        quantity=2.0,
                        pnl=6.0,
                    )
                ],
                pnl=6.0,
                pnl_pct=0.03,
            )
        ],
        equity_curve=[],
    )
    result.save(backtests_dir / "portfolio-enrich-source.trades.json")

    portfolios_dir = tmp_path / "portfolios"
    portfolios_dir.mkdir(parents=True, exist_ok=True)
    (portfolios_dir / "portfolio-enrich.json").write_text(
        json.dumps(
            {
                "id": "portfolio-enrich",
                "name": "Enrich Portfolio",
                "account_capital": 100000.0,
                "components": [
                    {
                        "component_id": "component-001",
                        "strategy_name": "Enrich Strategy",
                        "symbol": "AAPL",
                        "timeframe": "1h",
                        "weight": 1.0,
                        "risk_config": {},
                        "settings": {},
                        "backtest_id": "portfolio-enrich-source",
                    }
                ],
                "trades": [
                    {
                        "time": "2024-01-01T12:00:00Z",
                        "pnl": 6.0,
                        "strategy": "Enrich Strategy",
                        "symbol": "AAPL",
                        "direction": "LONG",
                        "backtest_id": "portfolio-enrich-source",
                        "component_id": "component-001",
                    }
                ],
                "combined_stats": {},
            }
        ),
        encoding="utf-8",
    )

    response = viewer_client.get("/api/portfolios/portfolio-enrich/trades")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["size"] == pytest.approx(2.0)
    assert payload[0]["entry_price"] == pytest.approx(100.0)
    assert payload[0]["exit_price"] == pytest.approx(103.0)
    assert payload[0]["pnl_pct"] == pytest.approx(0.03)
    assert payload[0]["entry_time_iso"] == "2024-01-01T10:00:00Z"
    assert payload[0]["exit_time_iso"] == "2024-01-01T12:00:00Z"


def test_agent_convert_endpoint_starts_background_agent(viewer_client, monkeypatch):
    import viewer.main as viewer_app_module

    captured: dict[str, object] = {}

    def fake_queue(task_id: str, payload: dict) -> dict:
        captured["task_id"] = task_id
        captured["payload"] = payload
        return {"job_id": task_id, "task_id": task_id, "status": "starting"}

    def fake_launch(task_id: str, payload: dict) -> None:
        captured["launch_task_id"] = task_id
        captured["launch_payload"] = payload

    monkeypatch.setattr(viewer_app_module, "_queue_agent_task", fake_queue)
    monkeypatch.setattr(viewer_app_module, "_launch_agent_task", fake_launch)

    resp = viewer_client.post(
        "/api/agent/convert",
        json={"code": "//@version=5\nstrategy('x')", "strategy_name": "Signal Forge"},
    )

    assert resp.status_code == 202
    assert resp.json() == {
        "job_id": captured["task_id"],
        "task_id": captured["task_id"],
        "status": "starting",
    }
    assert captured["payload"] == {
        "code": "//@version=5\nstrategy('x')",
        "strategy_name": "Signal Forge",
        "engine": None,
        "provider": viewer_app_module.DEFAULT_LLM_PROVIDER_NAME,
        "model": viewer_app_module.DEFAULT_MODEL_NAME,
        "symbols": None,
        "timeframe": None,
        "start": None,
        "end": None,
        "capital": 100000.0,
    }

    assert captured["launch_task_id"] == captured["task_id"]
    assert captured["launch_payload"] == captured["payload"]


def test_agent_convert_endpoint_rejects_unknown_provider(viewer_client):
    resp = viewer_client.post(
        "/api/agent/convert",
        json={
            "code": "//@version=5\nstrategy('x')",
            "provider": "unknown_provider",
        },
    )

    assert resp.status_code == 400
    assert "Unsupported provider" in str(resp.json().get("detail"))


def test_agent_convert_endpoint_rejects_copilot_cli_provider(viewer_client):
    resp = viewer_client.post(
        "/api/agent/convert",
        json={
            "code": "//@version=5\nstrategy('x')",
            "provider": "copilot_cli",
        },
    )

    assert resp.status_code == 400
    assert "Unsupported provider" in str(resp.json().get("detail"))


def test_equity_endpoint_rebuilds_curve_from_trades(viewer_client, tmp_path):
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)

    result = BacktestResult(
        meta=BacktestMeta(
            id="rebuild-equity",
            created_at="2026-03-09T09:00:00Z",
            engine="vectorbt",
            strategies=["short_term_reversal"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            initial_capital=100000.0,
            data={"symbols": ["AAPL"], "timeframes": ["1d"], "source": "test_source"},
        ),
        trades=[
            Trade(
                id=2,
                symbol="AAPL",
                direction="LONG",
                entry_time="2024-01-03T00:00:00+00:00",
                entry_price=101.0,
                quantity=10.0,
                exits=[
                    TradeExit(
                        time="2024-01-05T00:00:00+00:00",
                        price=103.5,
                        quantity=10.0,
                        pnl=250.0,
                    )
                ],
                pnl=250.0,
            ),
            Trade(
                id=1,
                symbol="AAPL",
                direction="LONG",
                entry_time="2024-01-01T00:00:00+00:00",
                entry_price=100.0,
                quantity=10.0,
                exits=[
                    TradeExit(
                        time="2024-01-02T00:00:00+00:00",
                        price=105.0,
                        quantity=10.0,
                        pnl=500.0,
                    )
                ],
                pnl=500.0,
            ),
            Trade(
                id=3,
                symbol="AAPL",
                direction="LONG",
                entry_time="2024-01-04T00:00:00+00:00",
                entry_price=103.0,
                quantity=10.0,
                exits=[
                    TradeExit(
                        time="2024-01-05T00:00:00+00:00",
                        price=101.0,
                        quantity=10.0,
                        pnl=-200.0,
                    )
                ],
                pnl=-200.0,
            ),
        ],
        equity_curve=[],
    )
    result.save(backtests_dir / "rebuild-equity.trades.json")

    resp = viewer_client.get("/api/backtest/rebuild-equity/equity")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload == [
        {"time": pytest.approx(pd.Timestamp("2024-01-01T00:00:00+00:00").timestamp()), "equity": 100000.0},
        {"time": pytest.approx(pd.Timestamp("2024-01-02T00:00:00+00:00").timestamp()), "equity": 100500.0},
        {"time": pytest.approx(pd.Timestamp("2024-01-05T00:00:00+00:00").timestamp()), "equity": 100550.0},
    ]


def test_strategy_detail_reads_markdown_and_patch_renames_strategy(
    viewer_client, monkeypatch, tmp_path
):
    import viewer.main as viewer_app_module

    strategies_root = tmp_path / "strategies"
    strategy_dir = strategies_root / "demo"
    strategy_dir.mkdir(parents=True, exist_ok=True)
    strategy_dir.joinpath("demo.py").write_text(
        """
STRATEGY_META = {
    "name": "Demo Strategy",
    "version": "1.0.0",
    "source_url": "https://example.com/demo-strategy",
    "notes": "Known issue: **gaps** can delay entry by one bar.",
    "tags": {"type": ["momentum"], "source": "test", "asset_class": ["equities"], "complexity": "simple"},
}

def generate_signals(df, params):
    return df["close"] > df["close"], df["close"] < df["close"], {"noop": df["close"]}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    strategy_dir.joinpath("README.md").write_text("# Demo Strategy\n\nThis is markdown.\n", encoding="utf-8")
    monkeypatch.setattr(viewer_app_module, "STRATEGIES_DIR", strategies_root)

    detail_resp = viewer_client.get("/api/strategies/demo")

    assert detail_resp.status_code == 200
    detail_payload = detail_resp.json()
    assert detail_payload["slug"] == "demo"
    assert detail_payload["name"] == "Demo Strategy"
    assert detail_payload["source_url"] == "https://example.com/demo-strategy"
    assert detail_payload["notes"] == "Known issue: **gaps** can delay entry by one bar."
    assert "This is markdown." in detail_payload["documentation_markdown"]

    patch_resp = viewer_client.patch("/api/strategies/demo", json={"name": "Renamed Strategy"})

    assert patch_resp.status_code == 200
    patch_payload = patch_resp.json()
    assert patch_payload["name"] == "Renamed Strategy"
    assert "Renamed Strategy" in strategy_dir.joinpath("demo.py").read_text(encoding="utf-8")


def test_backtest_patch_updates_title_in_meta(viewer_client, tmp_path):
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)

    result = BacktestResult(
        meta=BacktestMeta(
            id="rename-backtest",
            created_at="2026-03-09T09:00:00Z",
            engine="vectorbt",
            strategies=["short_term_reversal"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            initial_capital=100000.0,
            data={"symbols": ["AAPL"], "timeframes": ["1d"], "source": "test_source"},
        ),
        trades=[],
        equity_curve=[],
    )
    result.save(backtests_dir / "rename-backtest.trades.json")

    patch_resp = viewer_client.patch("/api/backtests/rename-backtest", json={"name": "Desk Run"})

    assert patch_resp.status_code == 200
    payload = patch_resp.json()
    assert payload["title"] == "Desk Run"
    assert payload["notes"] == "Desk Run"

    info_resp = viewer_client.get("/api/backtest/rename-backtest/info")
    assert info_resp.status_code == 200
    assert info_resp.json()["title"] == "Desk Run"


def test_logs_endpoint_supports_all_logs_and_multi_level_filters(viewer_client, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "viewer.log").write_text(
        '{"timestamp":"2026-03-08T10:00:00+00:00","level":"INFO","message":"viewer-start"}\n'
        '{"timestamp":"2026-03-08T10:02:00+00:00","level":"ERROR","message":"viewer-fail"}\n'
    )
    (logs_dir / "runner.log").write_text(
        '{"timestamp":"2026-03-08T10:01:00+00:00","level":"WARNING","message":"runner-warn"}\n'
    )

    listing = viewer_client.get("/api/logs")
    assert listing.status_code == 200
    assert listing.json()[0]["name"] == "ALL_LOGS"

    resp = viewer_client.get("/api/logs/ALL_LOGS?level=INFO,WARN")

    assert resp.status_code == 200
    assert resp.json()["lines"] == [
        '{"timestamp":"2026-03-08T10:00:00+00:00","level":"INFO","message":"viewer-start"}',
        '{"timestamp":"2026-03-08T10:01:00+00:00","level":"WARNING","message":"runner-warn"}',
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/api/logs/ALL_LOGS",
        "/api/services/service-log-test/logs",
    ],
)
def test_log_endpoints_cap_response_lines(viewer_client, monkeypatch, path):
    import viewer.main as viewer_app_module

    entries = [
        {
            "line": f"line-{index}",
            "timestamp": None,
            "level": "INFO",
            "path": "viewer.log",
            "line_number": index,
        }
        for index in range(2001)
    ]
    monkeypatch.setattr(
        viewer_app_module.legacy_viewer,
        "_collect_log_entries",
        lambda _paths: entries,
    )
    monkeypatch.setattr(
        viewer_app_module,
        "get_service",
        lambda _service_id: SimpleNamespace(
            id="service-log-test",
            name="service-log-test",
            config={},
            type=SimpleNamespace(value="runner"),
        ),
    )
    monkeypatch.setattr(
        viewer_app_module,
        "_entry_matches_service_log",
        lambda *_args, **_kwargs: True,
    )

    requested = viewer_client.get(f"{path}?lines=3")
    capped = viewer_client.get(f"{path}?lines=100000")

    assert requested.status_code == 200
    assert requested.json()["lines"] == ["line-1998", "line-1999", "line-2000"]
    assert capped.status_code == 200
    capped_lines = capped.json()["lines"]
    assert len(capped_lines) == 2000
    assert capped_lines[0] == "line-1"
    assert capped_lines[-1] == "line-2000"
    assert capped.json()["total_lines"] == 2001


def test_logs_endpoint_sorts_space_separated_timestamps(viewer_client, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "viewer.log").write_text(
        '{"timestamp":"2026-03-08 10:00:00","level":"INFO","message":"viewer-start"}\n'
        '{"timestamp":"2026-03-08 10:02:00","level":"ERROR","message":"viewer-fail"}\n'
    )
    (logs_dir / "runner.log").write_text(
        '{"timestamp":"2026-03-08 10:01:00","level":"WARNING","message":"runner-warn"}\n'
    )

    resp = viewer_client.get("/api/logs/ALL_LOGS")

    assert resp.status_code == 200
    assert resp.json()["lines"] == [
        '{"timestamp":"2026-03-08 10:00:00","level":"INFO","message":"viewer-start"}',
        '{"timestamp":"2026-03-08 10:01:00","level":"WARNING","message":"runner-warn"}',
        '{"timestamp":"2026-03-08 10:02:00","level":"ERROR","message":"viewer-fail"}',
    ]


def test_download_and_verify_checks_normalized_timeframe(monkeypatch):
    """Coverage verification must use the normalized timeframe, not the raw argument."""
    import viewer.main as viewer_app_module

    checked: list[str] = []

    def _fake_verify(**kwargs):
        checked.append(str(kwargs["timeframe"]))

    monkeypatch.setattr(viewer_app_module, "_build_ohlcv_availability_index", lambda: {})
    monkeypatch.setattr(
        viewer_app_module,
        "_data_check_result",
        lambda **kwargs: {"available": True},
    )
    monkeypatch.setattr(
        viewer_app_module, "_verify_missing_download_coverage", _fake_verify
    )
    monkeypatch.setattr(
        viewer_app_module,
        "_auto_download_source_plan",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        viewer_app_module, "_download_missing_ohlcv_data", lambda **kwargs: None
    )
    monkeypatch.setattr(
        viewer_app_module.cache_manager,
        "invalidate_cache",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(viewer_app_module, "_invalidate_data_views_cache", lambda: None)
    monkeypatch.setattr(viewer_app_module, "_refresh_catalog", lambda: None)

    viewer_app_module._download_and_verify_missing_data(
        [{"symbol": "SP500", "source": "dukascopy", "missing_range": None}],
        normalized_timeframe="1m",
        start="2024-01-01",
        end="2024-01-02",
    )

    assert checked == ["1m"]


def test_verify_missing_download_coverage_uses_given_timeframe(monkeypatch):
    import viewer.main as viewer_app_module

    checked: list[str] = []

    def _fake_data_check(*, symbol, timeframe, start, end, availability_index):
        checked.append(timeframe)
        return {"available": True}

    monkeypatch.setattr(viewer_app_module, "_build_ohlcv_availability_index", lambda: {})
    monkeypatch.setattr(viewer_app_module, "_data_check_result", _fake_data_check)

    viewer_app_module._verify_missing_download_coverage(
        symbol="SP500",
        timeframe="1m",
        start="2024-01-01",
        end="2024-01-02",
    )

    assert checked == ["1m"]


def test_resume_dispatches_backtest_recovery_to_bound_helper(monkeypatch):
    import viewer.main as viewer_app_module

    calls = []
    record = {
        "id": "task-recovery",
        "type": "backtest",
        "status": "interrupted",
        "config_json": {"task_spec": {"version": 1, "kind": "backtest", "operation": "run"}},
    }
    monkeypatch.setattr(viewer_app_module, "_LEGACY_HOOKS_INSTALLED", True)
    monkeypatch.setattr(
        viewer_app_module,
        "list_background_task_records",
        lambda **_kwargs: [record],
    )
    monkeypatch.setattr(
        viewer_app_module,
        "_resume_interrupted_backtest_task",
        lambda received_record, task_id, raw_config: calls.append(
            (received_record["id"], task_id, raw_config)
        ),
    )

    viewer_app_module._resume_interrupted_tasks()

    assert calls == [("task-recovery", "task-recovery", record["config_json"])]


def test_backtest_recovery_builds_request_from_typed_spec(monkeypatch):
    import viewer.main as viewer_app_module

    started = []
    persisted = []
    record = {
        "id": "task-recovery",
        "type": "backtest",
        "status": "interrupted",
        "config_json": {
            "task_spec": {
                "version": 1,
                "kind": "backtest",
                "operation": "run",
                "parameters": {
                    "strategy": "demo",
                    "engine": "vectorbt",
                    "symbols": ["AAPL"],
                    "timeframe": "1d",
                    "capital": 100000,
                },
            }
        },
    }
    monkeypatch.setattr(
        viewer_app_module,
        "_run_backtest_sync",
        lambda runner, request: started.append((runner, request)) or {"task_id": "task-new"},
    )
    monkeypatch.setattr(
        viewer_app_module,
        "upsert_background_task_record",
        lambda **kwargs: persisted.append(kwargs),
    )

    viewer_app_module._resume_interrupted_backtest_task(
        record,
        "task-recovery",
        record["config_json"],
    )

    assert len(started) == 1
    request = started[0][1]
    assert request.strategy == "demo"
    assert request.engine == "vectorbt"
    assert request.symbols == ["AAPL"]
    assert persisted[0]["status"] == "completed"


def test_internal_error_detail_is_generic_and_redacted():
    import viewer.main as viewer_app_module

    detail = viewer_app_module._internal_error_detail(
        RuntimeError("Authorization: Bearer api-secret"),
        "test operation",
    )

    assert "api-secret" not in detail
    assert detail.startswith("Internal server error (error_id=")


def test_data_recovery_rejects_persisted_command_without_typed_spec(monkeypatch):
    import viewer.main as viewer_app_module

    updates = []
    record = {
        "id": "task-recovery",
        "type": "data",
        "status": "interrupted",
        "config_json": {"command": [sys.executable, "-c", "raise SystemExit('executed')"]},
    }
    started = []
    monkeypatch.setattr(
        viewer_app_module,
        "upsert_background_task_record",
        lambda **kwargs: updates.append(kwargs),
    )
    monkeypatch.setattr(
        viewer_app_module,
        "_start_data_task",
        lambda *args, **kwargs: started.append((args, kwargs)),
    )

    viewer_app_module._resume_interrupted_data_task(record, "task-recovery", record["config_json"])

    assert started == []
    assert updates[0]["status"] == "manual_required"
    assert updates[0]["config_json"]["recovery_rejected"] == "missing typed data spec"
    assert updates[0]["config_json"]["recovery_mode"] == "manual"
    assert "command" not in updates[0]["config_json"]


def test_typed_only_unsupported_recovery_is_manual_and_not_executable(monkeypatch):
    import viewer.main as viewer_app_module

    updates: list[dict] = []
    started: list[tuple] = []
    record = {
        "id": "task-typed-only",
        "type": "data",
        "status": "interrupted",
        "config_json": {
            "command": [sys.executable, "-c", "raise SystemExit('executed')"],
            "task_spec": {
                "version": 1,
                "kind": "data",
                "operation": "harvest",
                "source": "dukascopy",
                "symbols": ["EURUSD"],
            },
        },
    }
    monkeypatch.setattr(
        viewer_app_module,
        "upsert_background_task_record",
        lambda **kwargs: updates.append(kwargs),
    )
    monkeypatch.setattr(
        viewer_app_module,
        "_start_data_task",
        lambda *args, **kwargs: started.append((args, kwargs)),
    )

    viewer_app_module._resume_interrupted_data_task(
        record, "task-typed-only", record["config_json"]
    )

    assert started == []
    assert updates[0]["status"] == "manual_required"
    assert updates[0]["config_json"]["recovery_mode"] == "manual"
    assert "command" not in updates[0]["config_json"]


def test_manual_recovery_typed_only_unsupported_is_not_executable(
    viewer_client, monkeypatch
):
    import viewer.main as viewer_app_module

    task_id = "manual-typed-only"
    viewer_app_module.DATA_TASKS[task_id] = {
        "id": task_id,
        "kind": "data",
        "status": "failed",
    }
    record = {
        "id": task_id,
        "type": "data",
        "status": "failed",
        "config_json": {
            "command": [sys.executable, "-c", "raise SystemExit('executed')"],
            "task_spec": {
                "version": 1,
                "kind": "data",
                "operation": "validate",
                "symbol": "EURUSD",
            },
        },
    }
    started: list[tuple] = []
    updates: list[dict] = []
    monkeypatch.setattr(
        viewer_app_module, "get_background_task_record", lambda _task_id: record
    )
    monkeypatch.setattr(
        viewer_app_module,
        "_start_data_task",
        lambda *args, **kwargs: started.append((args, kwargs)),
    )
    monkeypatch.setattr(
        viewer_app_module,
        "upsert_background_task_record",
        lambda **kwargs: updates.append(kwargs),
    )

    response = viewer_client.post(f"/api/data/tasks/{task_id}/restart")

    assert response.status_code == 200
    assert response.json()["status"] == "manual_required"
    assert started == []
    assert updates[-1]["config_json"]["recovery_mode"] == "manual"
    assert "command" not in updates[-1]["config_json"]


def test_typed_data_recovery_rebuilds_request_and_ignores_command(monkeypatch):
    import viewer.main as viewer_app_module

    started = []
    record = {
        "id": "task-recovery",
        "type": "data",
        "status": "interrupted",
        "name": "Download",
        "config_json": {
            "command": [sys.executable, "-c", "raise SystemExit('executed')"],
            "task_spec": {
                "version": 1,
                "kind": "data",
                "operation": "download",
                "source": "yfinance",
                "symbols": ["AAPL"],
                "timeframes": ["1d"],
            },
        },
    }
    monkeypatch.setattr(
        viewer_app_module,
        "_start_generic_download",
        lambda task_id, request, **kwargs: started.append((task_id, request, kwargs)),
    )

    viewer_app_module._resume_interrupted_data_task(record, "task-recovery", record["config_json"])

    assert len(started) == 1
    task_id, request, _ = started[0]
    assert task_id == "task-recovery"
    assert request.symbol_list == ["AAPL"]
    assert request.timeframe_list == ["1d"]


def test_terminal_task_retention_is_bounded(monkeypatch):
    from viewer import core

    core.DATA_TASKS.clear()
    monkeypatch.setattr(core, "MAX_TERMINAL_TASKS", 2)
    for index in range(3):
        core.DATA_TASKS[f"task-terminal-{index}"] = {
            "status": "failed",
            "updated_at": f"2026-01-01T00:00:0{index}",
        }
    core._prune_terminal_tasks_locked()

    assert set(core.DATA_TASKS) == {"task-terminal-1", "task-terminal-2"}
    core.DATA_TASKS.clear()


def test_singleflight_expired_reservation_does_not_block_new_work(monkeypatch):
    from viewer import core

    core.DATA_TASKS.clear()
    monkeypatch.setattr(core, "SINGLEFLIGHT_LEASE_SECONDS", 0.01)
    stale = {
        "id": "task-stale",
        "status": "running",
        "download_request_key": "same-request",
        "singleflight_lease_until": time.monotonic() - 1,
    }
    core.DATA_TASKS["task-stale"] = stale
    reservation = {"id": "task-new", "status": "starting"}

    existing = core._register_download_task(reservation, "same-request")

    assert existing is None
    assert stale["status"] == "failed"
    assert core.DATA_TASKS["task-new"]["download_request_key"] == "same-request"
    core.DATA_TASKS.clear()


def test_singleflight_lease_covers_max_admitted_query_timeout():
    from viewer import core

    now = 100.0
    task = {
        "status": "running",
        "download_request_key": "same-request",
        "singleflight_lease_until": now + core.SINGLEFLIGHT_LEASE_SECONDS,
    }

    assert core.SINGLEFLIGHT_LEASE_SECONDS > core.MAX_DATA_QUERY_TIMEOUT_SECONDS
    assert (
        core._singleflight_lease_expired(
            task,
            now + core.MAX_DATA_QUERY_TIMEOUT_SECONDS,
        )
        is False
    )


def test_task_log_read_is_bounded_and_redacted(tmp_path):
    from viewer import core

    log_path = tmp_path / "task-log.log"
    log_path.write_text(
        ("x" * 200) + "\nAuthorization: Bearer super-secret\n",
        encoding="utf-8",
    )

    content = core._read_task_log(log_path, max_bytes=64)

    assert len(content.encode("utf-8")) <= 64
    assert "super-secret" not in content
    assert "Bearer [Filtered]" in content


def test_general_log_collection_caps_lines_and_redacts_json(tmp_path):
    from viewer import core

    log_path = tmp_path / "viewer.log"
    log_path.write_text(
        "\n".join(
            json.dumps({"level": "INFO", "message": f"line-{index}", "api_key": "secret"})
            for index in range(2500)
        ),
        encoding="utf-8",
    )

    entries = core._collect_log_entries([log_path], max_bytes=1024 * 1024, max_lines=3)

    assert len(entries) == 3
    assert all("secret" not in entry["line"] for entry in entries)


def test_general_log_collection_spreads_line_budget_across_files(tmp_path):
    from viewer import core

    paths = []
    for name in ("viewer.log", "runner.log"):
        path = tmp_path / name
        path.write_text("\n".join(f"{name}:{index}" for index in range(8)), encoding="utf-8")
        paths.append(path)

    entries = core._collect_log_entries(paths, max_bytes=1024 * 1024, max_lines=4)

    assert len(entries) == 4
    assert {entry["path"] for entry in entries} == {"viewer.log", "runner.log"}
    assert all(sum(entry["path"] == path.name for entry in entries) == 2 for path in paths)


def test_ctrader_report_is_atomic_and_private(tmp_path, monkeypatch):
    import importlib.util

    script_path = Path(__file__).parents[1] / "scripts" / "verify_ctrader_order.py"
    spec = importlib.util.spec_from_file_location("verify_ctrader_order_test", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "REPORTS_DIR", tmp_path / "reports")

    output_path = module._write_report_for_run({"verified": True})

    assert output_path.exists()
    if os.name == "posix":
        assert output_path.stat().st_mode & 0o777 == 0o600
        assert output_path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(output_path.read_text(encoding="utf-8"))["verified"] is True


def test_core_task_id_boundaries_are_exact():
    from viewer import core

    assert core._normalized_task_id("a" * 7) == ""
    assert core._normalized_task_id("a" * 8) == "a" * 8
    assert core._normalized_task_id("a" * 64) == "a" * 64
    assert core._normalized_task_id("a" * 65) == ""
    core.DATA_TASKS["../escape"] = {"id": "../escape", "status": "running"}
    assert core._update_task_fields("../escape", status="failed") is None
    core.DATA_TASKS.pop("../escape", None)


def test_singleflight_lease_boundary_and_owner_authorization(monkeypatch):
    from viewer import core

    task = {
        "status": "running",
        "download_request_key": "request",
        "singleflight_lease_until": 100.0,
    }
    assert core._singleflight_lease_expired(task, 99.999) is False
    assert core._singleflight_lease_expired(task, 100.0) is True

    monkeypatch.setattr(core, "SINGLEFLIGHT_LEASE_SECONDS", 100.0)
    monkeypatch.setattr(core, "SINGLEFLIGHT_HEARTBEAT_SECONDS", 10.0)
    owner_token = "owner-token"
    task.update(
        {
            "owner_pid": core.os.getpid(),
            "owner_token": owner_token,
            "singleflight_heartbeat_at": 100.0,
            "singleflight_lease_until": 200.0,
        }
    )
    assert core._touch_singleflight_lease(task, 105.0, owner_token=owner_token) is False
    assert task["singleflight_lease_until"] == 200.0
    assert core._touch_singleflight_lease(task, 105.0, owner_token="wrong-token") is False
    assert task["singleflight_lease_until"] == 200.0
    task["owner_pid"] = core.os.getpid() + 1
    assert core._touch_singleflight_lease(task, 110.0, owner_token=owner_token) is False
    assert task["singleflight_lease_until"] == 200.0
    task["owner_pid"] = core.os.getpid()
    assert core._touch_singleflight_lease(task, 110.0, owner_token=owner_token) is True
    assert task["singleflight_lease_until"] == 210.0


def test_singleflight_requester_cannot_renew_existing_owner_lease(monkeypatch):
    from viewer import core

    core.DATA_TASKS.clear()
    monkeypatch.setattr(core, "SINGLEFLIGHT_LEASE_SECONDS", 100.0)
    monkeypatch.setattr(core, "SINGLEFLIGHT_HEARTBEAT_SECONDS", 10.0)
    now = time.monotonic()
    existing = {
        "id": "task-owned-reservation",
        "status": "running",
        "download_request_key": "same-request",
        "owner_pid": core.os.getpid(),
        "owner_token": "owner-token",
        "singleflight_owner_boot_id": core.SINGLEFLIGHT_BOOT_ID,
        "singleflight_heartbeat_at": now - 20,
        "singleflight_lease_until": now + 100,
    }
    core.DATA_TASKS[existing["id"]] = existing

    result = core._register_download_task(
        {"id": "task-new-owner", "status": "starting"},
        "same-request",
        owner_token="new-owner",
    )

    assert result is not None
    assert result["id"] == existing["id"]
    assert existing["singleflight_lease_until"] == now + 100
    core.DATA_TASKS.clear()


def test_singleflight_requester_invalidates_reservation_from_old_boot(monkeypatch):
    from viewer import core

    core.DATA_TASKS.clear()
    now = time.monotonic()
    existing = {
        "id": "task-old-boot-reservation",
        "status": "running",
        "download_request_key": "same-request",
        "owner_pid": core.os.getpid(),
        "owner_token": "old-owner",
        "singleflight_owner_boot_id": "old-boot",
        "singleflight_heartbeat_at": now,
        "singleflight_lease_until": now + 100,
    }
    core.DATA_TASKS[existing["id"]] = existing

    result = core._register_download_task(
        {"id": "task-new-boot-owner", "status": "starting"},
        "same-request",
        owner_token="new-owner",
    )

    assert result is None
    assert existing["status"] == "failed"
    assert "download_request_key" not in existing
    assert core.DATA_TASKS["task-new-boot-owner"]["download_request_key"] == "same-request"
    core.DATA_TASKS.clear()


def test_singleflight_reservation_is_invalidated_after_restart():
    import viewer.main as viewer_app_module

    task = viewer_app_module._build_task_from_record(
        {
            "id": "task-stale-reservation",
            "type": "data",
            "status": "starting",
            "config_json": {
                "download_request_key": "same-request",
                "owner_pid": viewer_app_module.os.getpid(),
                "owner_token": "old-owner",
                "singleflight_owner_boot_id": "old-boot",
            },
        }
    )

    assert task["status"] == "interrupted"
    assert "download_request_key" not in task
    assert "owner_token" not in task
    assert "invalidated by server restart" in task["error"]

    terminal = viewer_app_module._build_task_from_record(
        {
            "id": "task-terminal-reservation",
            "type": "data",
            "status": "completed",
            "config_json": {
                "download_request_key": "old-request",
                "owner_token": "old-owner",
            },
        }
    )

    assert terminal["status"] == "completed"
    assert "download_request_key" not in terminal
    assert "owner_token" not in terminal


def test_singleflight_runtime_state_is_in_persisted_task_config():
    import viewer.main as viewer_app_module

    config = viewer_app_module._task_config_payload(
        {
            "id": "task-persisted-reservation",
            "kind": "data",
            "status": "starting",
            "download_request_key": "same-request",
            "owner_pid": 123,
            "owner_thread_id": 456,
            "owner_token": "owner-token",
            "singleflight_owner_boot_id": "boot-id",
            "singleflight_lease_expires_at": 789.5,
        }
    )

    assert config["download_request_key"] == "same-request"
    assert config["owner_pid"] == 123
    assert config["owner_token"] == "owner-token"
    assert config["singleflight_owner_boot_id"] == "boot-id"
    assert config["singleflight_lease_expires_at"] == 789.5


def test_task_status_evidence_mapping_is_exhaustive_and_fail_closed():
    import viewer.main as viewer_app_module
    from shared import database
    from viewer import core

    assert core.TASK_STATUS_VALUES == database.TASK_STATUS_VALUES
    assert set(viewer_app_module._TASK_STATUS_WEIGHTS) == set(database.TASK_STATUS_VALUES)
    assert set(database._TASK_STATUS_TO_EVIDENCE_STATUS) == set(database.TASK_STATUS_VALUES)
    assert database._task_status_to_evidence_status("completed") == "success"
    assert database._task_status_to_evidence_status("cleanup_pending") == "partial"
    assert database._task_status_to_evidence_status("cleanup_exhausted") == "failed"
    assert database.is_final_task_status("cleanup_pending") is True
    assert database.is_final_task_status("mystery") is False
    with pytest.raises(ValueError, match="unknown task status"):
        database.normalize_task_status("mystery")


def test_unknown_persisted_task_status_requires_manual_review():
    import viewer.main as viewer_app_module

    task = viewer_app_module._build_task_from_record(
        {
            "id": "task-unknown",
            "type": "data",
            "status": "mystery",
            "config_json": {},
        }
    )

    assert task["status"] == "manual_required"
    assert task["recovery_mode"] == "manual"
    assert "Unknown persisted task status" in task["recovery_rejected"]


def test_task_snapshot_does_not_persist_executable_command_fields(monkeypatch):
    import viewer.main as viewer_app_module

    persisted: list[dict] = []
    monkeypatch.setattr(
        viewer_app_module,
        "upsert_background_task_record",
        lambda **kwargs: persisted.append(kwargs),
    )
    viewer_app_module._persist_task_snapshot(
        "task-command",
        {
            "id": "task-command",
            "kind": "data",
            "status": "running",
            "command": ["/bin/sh", "-c", "echo secret"],
            "task_spec": {
                "version": 1,
                "kind": "data",
                "operation": "download",
                "parameters": {"commandLine": "echo secret"},
            },
            "restart_config": {"executable": "/bin/sh"},
        },
    )

    config = persisted[-1]["config_json"]
    assert "command" not in config
    assert "commandLine" not in config["task_spec"]["parameters"]
    assert "executable" not in config["restart_config"]


def test_database_task_config_strips_nested_executable_fields():
    from shared.database import _strip_executable_command_fields

    sanitized = _strip_executable_command_fields(
        {
            "commandLine": "echo secret",
            "nested": {"argv": ["echo", "secret"], "safe": "value"},
            "items": [{"executable": "/bin/sh"}],
        }
    )

    assert sanitized == {"nested": {"safe": "value"}, "items": [{}]}


def test_recovery_spec_round_trip_uses_typed_data_fields():
    import viewer.main as viewer_app_module

    request = viewer_app_module._PreparedDownloadRequest(
        source="yfinance",
        symbols="AAPL",
        timeframes="1d,5m",
        start="2026-01-01",
        end="2026-01-02",
        offer_side="bid",
        merge_tick=True,
        merge_s1=False,
        batch_size=10,
        pause_ms=1000,
        query_timeout_seconds=900,
        symbol_list=["AAPL"],
        timeframe_list=["1d", "5m"],
    )
    spec = viewer_app_module._download_task_spec(request)
    parsed = viewer_app_module._parse_recovery_spec({"task_spec": spec}, "data")
    assert parsed is not None
    recovered = viewer_app_module._download_request_from_recovery_spec(parsed)
    assert recovered.symbol_list == request.symbol_list
    assert recovered.timeframe_list == request.timeframe_list
    assert recovered.merge_tick is True
    assert "command" not in spec

    backtest_spec = {
        "version": 1,
        "kind": "backtest",
        "operation": "run",
        "parameters": {
            "strategy": "demo",
            "engine": "native",
            "symbols": ["AAPL"],
            "timeframe": "1d",
            "capital": 100000.0,
        },
    }
    parsed_backtest = viewer_app_module._parse_recovery_spec(
        {"task_spec": backtest_spec},
        "backtest",
    )
    assert parsed_backtest is not None
    backtest_request = viewer_app_module._backtest_request_from_recovery_spec(parsed_backtest)
    assert backtest_request.strategy == "demo"
    assert backtest_request.symbols == ["AAPL"]
    assert backtest_request.timeframe == "1d"


def test_recovery_parser_rejects_nested_executable_fields():
    import viewer.main as viewer_app_module

    raw_config = {
        "task_spec": {
            "version": 1,
            "kind": "data",
            "operation": "download",
            "source": "yfinance",
            "symbols": ["AAPL"],
            "timeframes": ["1d"],
            "parameters": {"commandLine": ["/bin/sh", "-c", "echo secret"]},
        }
    }

    assert viewer_app_module._parse_recovery_spec(raw_config, "data") is None


def test_failed_backtest_recovery_persists_final_manual_record(monkeypatch):
    import viewer.main as viewer_app_module

    persisted: list[dict] = []
    record = {
        "id": "task-backtest-recovery",
        "type": "backtest",
        "status": "interrupted",
        "config_json": {
            "task_spec": {
                "version": 1,
                "kind": "backtest",
                "operation": "run",
                "parameters": {
                    "strategy": "demo",
                    "symbols": ["AAPL"],
                    "timeframe": "1d",
                },
            }
        },
    }
    def fail_recovery(*_args):
        raise RuntimeError("worker failed")

    monkeypatch.setattr(viewer_app_module, "_run_backtest_sync", fail_recovery)
    monkeypatch.setattr(viewer_app_module, "_persist_task_snapshot", lambda *_args: None)
    monkeypatch.setattr(
        viewer_app_module,
        "upsert_background_task_record",
        lambda **kwargs: persisted.append(kwargs),
    )
    viewer_app_module.DATA_TASKS.pop(record["id"], None)

    viewer_app_module._resume_interrupted_backtest_task(
        record,
        record["id"],
        record["config_json"],
    )

    assert persisted[-1]["status"] == "manual_required"
    assert persisted[-1]["config_json"]["recovery_mode"] == "manual"
    assert "command" not in persisted[-1]["config_json"]


def test_live_websocket_payload_redacts_nested_secrets_and_unknown_objects():
    import viewer.main as viewer_app_module

    class _Unknown:
        def __str__(self):
            return "opaque-secret"

    safe = viewer_app_module._redact_ws_message(
        {
            "type": "snapshot",
            "data": {
                "apiKey": "key-secret",
                "secret_key": "nested-secret-key",
                "headers": {"Authorization": "Digest digest-secret"},
                "opaque": "opaque-secret",
                "unknown": _Unknown(),
                "safe": "visible",
            },
        }
    )

    assert safe["data"]["apiKey"] == "[Filtered]"
    assert safe["data"]["secret_key"] == "[Filtered]"
    assert safe["data"]["headers"]["Authorization"] == "[Filtered]"
    assert safe["data"]["opaque"] == "[Filtered]"
    assert safe["data"]["unknown"] == "[Unserializable]"
    assert safe["data"]["safe"] == "visible"
