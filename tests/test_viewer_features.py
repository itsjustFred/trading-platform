"""Focused tests for newer viewer data-management features."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def viewer_client(monkeypatch, tmp_path):
    import viewer.app as viewer_app_module

    monkeypatch.setattr(viewer_app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(viewer_app_module, "BACKTESTS_DIR", tmp_path / "backtests")
    monkeypatch.setattr(viewer_app_module, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(viewer_app_module, "TASK_LOGS_DIR", tmp_path / "logs" / "tasks")
    viewer_app_module.DATA_TASKS.clear()
    viewer_app_module.app.config["TESTING"] = True
    return viewer_app_module.app.test_client()


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
    data = resp.get_json()
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
    payload = resp.get_json()
    assert "precedes Dukascopy genesis" in payload["error"]
    assert "EURUSD/s1 >= 2003-05-04" in payload["error"]
    assert "XAUUSD/s1 >= 2003-05-04" in payload["error"]


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
    )
    result.save(backtests_dir / "meta-demo.trades.json")

    resp = viewer_client.get("/api/backtest/meta-demo/trade/1")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["trade"]["meta"] == {
        "SMA_Fast": 150.2,
        "Signal_Strength": "High",
    }
    assert len(payload["ohlcv"]) > 0


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
        for category in overview_resp.get_json()["categories"]
    }
    assert categories["ETFs"]["start"] == "2024-01-02"
    assert categories["ETFs"]["end"] == "2024-02-09"

    details_resp = viewer_client.get("/api/data/category/ETFs")

    assert details_resp.status_code == 200
    assert details_resp.get_json()["symbols"] == [
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
        for category in overview_resp.get_json()["categories"]
    }
    assert categories["Equities"]["start"] == "2024-03-01"
    assert categories["Equities"]["end"] == "2024-03-03"

    details_resp = viewer_client.get("/api/data/category/Equities")

    assert details_resp.status_code == 200
    assert details_resp.get_json()["symbols"] == [
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
    assert resp.get_json()["symbols"] == [
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
        for category in overview_resp.get_json()["categories"]
    }
    assert categories["Equities"]["symbol_count"] == 1
    assert categories["Equities"]["start"] == "2024-04-01"
    assert categories["Equities"]["end"] == "2024-04-02"

    details_resp = viewer_client.get("/api/data/category/Equities")

    assert details_resp.status_code == 200
    assert details_resp.get_json()["symbols"] == [
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
    symbols = resp.get_json()["symbols"]
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
    import viewer.app as viewer_app_module

    captured: dict[str, object] = {}

    def fake_start(task_id: str, cmd: list[str], *, total_items: int = 0) -> dict:
        captured["task_id"] = task_id
        captured["cmd"] = cmd
        captured["total_items"] = total_items
        return {"task_id": task_id, "status": "starting"}

    monkeypatch.setattr(viewer_app_module, "_start_data_task", fake_start)

    resp = viewer_client.post(
        "/api/data/download",
        json={
            "source": "dukascopy",
            "symbols": "EURUSD",
            "timeframes": "tick,sec,m1",
            "price_type": "ask",
            "merge_tick": True,
            "merge_s1": True,
        },
    )

    assert resp.status_code == 200
    assert captured["cmd"].count("--price-type") == 1
    assert "ask" in captured["cmd"]
    assert "--merge-tick" in captured["cmd"]
    assert "--merge-s1" in captured["cmd"]
    assert captured["total_items"] == 3


def test_data_task_logs_endpoint_returns_empty_logs_when_file_missing(viewer_client, tmp_path):
    import viewer.app as viewer_app_module

    task_id = "missing-log"
    viewer_app_module.DATA_TASKS[task_id] = {
        "status": "running",
        "log_file": str(tmp_path / "logs" / "tasks" / f"task_{task_id}.log"),
    }

    resp = viewer_client.get(f"/api/data/tasks/{task_id}/logs")

    assert resp.status_code == 200
    assert resp.get_json() == {"logs": "", "status": "running"}


def test_data_task_logs_endpoint_returns_full_logs(viewer_client, tmp_path):
    import viewer.app as viewer_app_module

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
    assert resp.get_json() == {
        "logs": "[2026-03-04 10:00:00] Starting download...\nDone.\n",
        "status": "completed",
    }


def test_start_data_task_writes_task_logs_and_refreshes_catalog(viewer_client, monkeypatch, tmp_path):
    import viewer.app as viewer_app_module

    refresh_calls: list[str] = []
    routed_logs: list[str] = []

    def fake_refresh() -> None:
        refresh_calls.append("refreshed")

    def fake_info(message: str, *args, **kwargs) -> None:
        routed_logs.append(message % args if args else message)

    monkeypatch.setattr(viewer_app_module, "_refresh_catalog", fake_refresh)
    monkeypatch.setattr(viewer_app_module.logger, "info", fake_info)

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
    log_text = Path(task["log_file"]).read_text()
    assert "Starting command:" in log_text
    assert "stdout line" in log_text
    assert "stderr line" in log_text
    assert "Refreshing catalog..." in log_text
    assert refresh_calls == ["refreshed"]
    assert any("stdout line" in line for line in routed_logs)
    assert any("stderr line" in line for line in routed_logs)


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
    payload = resp.get_json()
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
    payload = resp.get_json()
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
    payload = create_resp.get_json()
    assert payload["combined_stats"]["p_value"] < 0.05
    assert "psr" in payload["combined_stats"]

    list_resp = viewer_client.get("/api/portfolios")
    assert list_resp.status_code == 200
    listed = list_resp.get_json()
    assert listed[0]["p_value"] == pytest.approx(payload["combined_stats"]["p_value"], abs=1e-6)

    detail_resp = viewer_client.get(f"/api/portfolios/{payload['id']}")
    assert detail_resp.status_code == 200
    assert detail_resp.get_json()["combined_stats"]["p_value"] == pytest.approx(
        payload["combined_stats"]["p_value"],
        abs=1e-6,
    )


def test_agent_convert_endpoint_starts_background_agent(viewer_client, monkeypatch):
    import viewer.app as viewer_app_module

    captured: dict[str, object] = {}

    def fake_start(task_id: str, payload: dict) -> dict:
        captured["task_id"] = task_id
        captured["payload"] = payload
        return {"task_id": task_id, "status": "starting"}

    monkeypatch.setattr(viewer_app_module, "_start_agent_task", fake_start)

    resp = viewer_client.post(
        "/api/agent/convert",
        json={"code": "//@version=5\nstrategy('x')", "strategy_name": "Signal Forge"},
    )

    assert resp.status_code == 200
    assert captured["payload"] == {
        "code": "//@version=5\nstrategy('x')",
        "strategy_name": "Signal Forge",
        "engine": None,
        "symbols": None,
        "timeframe": None,
        "start": None,
        "end": None,
        "capital": 100000.0,
    }


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
    payload = resp.get_json()
    assert payload == [
        {"time": pytest.approx(pd.Timestamp("2024-01-01T00:00:00+00:00").timestamp()), "equity": 100000.0},
        {"time": pytest.approx(pd.Timestamp("2024-01-02T00:00:00+00:00").timestamp()), "equity": 100500.0},
        {"time": pytest.approx(pd.Timestamp("2024-01-05T00:00:00+00:00").timestamp()), "equity": 100550.0},
    ]


def test_strategy_detail_reads_markdown_and_patch_renames_strategy(
    viewer_client, monkeypatch, tmp_path
):
    import viewer.app as viewer_app_module

    strategies_root = tmp_path / "strategies"
    strategy_dir = strategies_root / "demo"
    strategy_dir.mkdir(parents=True, exist_ok=True)
    strategy_dir.joinpath("demo.py").write_text(
        """
STRATEGY_META = {
    "name": "Demo Strategy",
    "version": "1.0.0",
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
    detail_payload = detail_resp.get_json()
    assert detail_payload["slug"] == "demo"
    assert detail_payload["name"] == "Demo Strategy"
    assert "This is markdown." in detail_payload["documentation_markdown"]

    patch_resp = viewer_client.patch("/api/strategies/demo", json={"name": "Renamed Strategy"})

    assert patch_resp.status_code == 200
    patch_payload = patch_resp.get_json()
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
    payload = patch_resp.get_json()
    assert payload["title"] == "Desk Run"
    assert payload["notes"] == "Desk Run"

    info_resp = viewer_client.get("/api/backtest/rename-backtest/info")
    assert info_resp.status_code == 200
    assert info_resp.get_json()["title"] == "Desk Run"


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
    assert listing.get_json()[0]["name"] == "ALL_LOGS"

    resp = viewer_client.get("/api/logs/ALL_LOGS?level=INFO,WARN")

    assert resp.status_code == 200
    assert resp.get_json()["lines"] == [
        '{"timestamp":"2026-03-08T10:00:00+00:00","level":"INFO","message":"viewer-start"}',
        '{"timestamp":"2026-03-08T10:01:00+00:00","level":"WARNING","message":"runner-warn"}',
    ]
