"""Focused tests for newer viewer data-management features."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

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


@pytest.fixture
def viewer_client(monkeypatch, tmp_path):
    import viewer.main as viewer_app_module

    legacy_module = viewer_app_module.legacy_viewer

    for module in (viewer_app_module, legacy_module):
        monkeypatch.setattr(module, "DATA_DIR", tmp_path)
        monkeypatch.setattr(module, "BACKTESTS_DIR", tmp_path / "backtests")
        monkeypatch.setattr(module, "LOGS_DIR", tmp_path / "logs")
        monkeypatch.setattr(module, "TASK_LOGS_DIR", tmp_path / "logs" / "tasks")
        monkeypatch.setattr(module, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")
        monkeypatch.setattr(module, "PORTFOLIOS_DIR", tmp_path / "portfolios")

    monkeypatch.setattr(viewer_app_module, "list_backtest_ids", lambda: [])
    monkeypatch.setattr(viewer_app_module, "load_backtest_result", lambda _backtest_id: None)
    monkeypatch.setattr(viewer_app_module, "save_result_to_sqlite", lambda _result: None)
    monkeypatch.setattr(viewer_app_module, "migrate_json_to_sqlite", lambda: {"migrated": 0, "failed": 0, "total": 0})
    viewer_app_module.DATA_TASKS.clear()
    return TestClient(viewer_app_module.app)


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
    assert data["genesis"] == {
        "EURUSD": {
            "d1": "1999-01-01",
            "h4": "2003-05-04",
            "s1": "2003-05-04",
        },
        "XAUUSD": {
            "d1": "1999-01-01",
            "h4": "2003-05-04",
            "s1": "2003-05-04",
        },
    }
    assert data["lower_bound"] == "2003-05-04"


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
            "start": "2024-01-02",
            "end": "2024-02-09",
            "source": "test_source",
        }
    ]


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
            "start": "2024-03-01",
            "end": "2024-03-03",
            "source": "test_source",
            "last_updated": cached_last_updated,
        }
    ]


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

    monkeypatch.setattr(viewer_app_module, "_start_dukascopy_task", fake_start)

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
    assert "ask" in captured["cmd"]
    assert "--merge-tick" in captured["cmd"]
    assert "--merge-s1" in captured["cmd"]
    assert "--batch-size" in captured["cmd"]
    assert "7" in captured["cmd"]
    assert "--pause-ms" in captured["cmd"]
    assert "2500" in captured["cmd"]
    assert captured["total_items"] == 3
    assert captured["symbols"] == ["EURUSD"]
    assert captured["timeframes"] == ["tick", "s1", "m1"]
    assert captured["batch_size"] == 7
    assert captured["pause_ms"] == 2500


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
    assert tasks[0]["id"] == task_id
    assert tasks[0]["progress"]["completed"] == 1
    assert tasks[0]["can_pause"] is True

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


def test_data_task_logs_endpoint_returns_empty_logs_when_file_missing(viewer_client, tmp_path):
    import viewer.main as viewer_app_module

    task_id = "missing-log"
    viewer_app_module.DATA_TASKS[task_id] = {
        "status": "running",
        "log_file": str(tmp_path / "logs" / "tasks" / f"task_{task_id}.log"),
    }

    resp = viewer_client.get(f"/api/data/tasks/{task_id}/logs")

    assert resp.status_code == 200
    assert resp.json() == {"logs": "", "status": "running"}


def test_data_task_logs_endpoint_returns_full_logs(viewer_client, tmp_path):
    import viewer.main as viewer_app_module

    task_id = "completed-log"
    log_file = tmp_path / "logs" / "tasks" / f"task_{task_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("[2026-03-04 10:00:00] Starting download...\nDone.\n")
    viewer_app_module.DATA_TASKS[task_id] = {
        "status": "completed",
        "log_file": str(log_file),
    }

    resp = viewer_client.get(f"/api/data/tasks/{task_id}/logs")

    assert resp.status_code == 200
    assert resp.json() == {
        "logs": "[2026-03-04 10:00:00] Starting download...\nDone.\n",
        "status": "completed",
    }


def test_start_data_task_writes_task_logs_and_refreshes_catalog(viewer_client, monkeypatch, tmp_path):
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
    job_id = resp.json()["job_id"]

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
        "top_trials": [{"number": 0, "value": 1.2345, "params": {"window": 10}}],
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


def test_backtests_list_uses_persisted_runner_stats(viewer_client, tmp_path):
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)

    result = BacktestResult(
        meta=BacktestMeta(
            id="cached-stats-demo",
            created_at="2026-03-07T15:00:00Z",
            engine="vectorbt",
            strategies=["Cached Stats Demo"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            initial_capital=100000.0,
            data={"symbols": ["AAPL"], "timeframes": ["1d"], "source": "test_source"},
        ),
        trades=[],
        equity_curve=_equity_curve_from_returns([0.01, -0.02, 0.015]),
        stats={
            "total_pnl": 1234.56,
            "sharpe_ratio": 7.777,
            "cagr": 0.4321,
            "max_drawdown_pct": -0.2345,
            "p_value": 0.012345,
            "psr": 0.88,
        },
    )
    result.save(backtests_dir / "cached-stats-demo.trades.json")

    resp = viewer_client.get("/api/backtests")

    assert resp.status_code == 200
    payload = resp.json()
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
    assert item["cagr"] == 43.21
    assert item["max_drawdown_pct"] == 23.45
    assert item["net_profit"] == 1234.56
    assert item["net_profit_pct"] == 1.23
    assert item["psr"] == 0.88
    assert item["p_value"] == 0.012345
    assert item["equity_mini"] == pytest.approx([100000.0, 101000.0, 98980.0, 100464.7])


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
            "strategies": [
                {
                    "backtest_id": "portfolio-source",
                    "risk_per_trade_pct": 1.0,
                    "max_positions": 5,
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
    assert detail_resp.json()["combined_stats"]["p_value"] == pytest.approx(
        payload["combined_stats"]["p_value"],
        abs=1e-6,
    )


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
        "model": viewer_app_module.DEFAULT_MODEL_NAME,
        "symbols": None,
        "timeframe": None,
        "start": None,
        "end": None,
        "capital": 100000.0,
    }

    assert captured["launch_task_id"] == captured["task_id"]
    assert captured["launch_payload"] == captured["payload"]


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
