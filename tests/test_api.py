"""Tests for the FastAPI endpoints in viewer/main.py.

Every detail endpoint test runs against a seeded, isolated store
(tmp DATA_DIR + isolated SQLite). A missing store or empty list fails the
test instead of silently skipping the assertions (Cleanup 17-B, tests-a C1).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shared.models import (
    BacktestMeta,
    BacktestResult,
    EquityPoint,
    Trade,
    TradeExit,
)
from shared.strategy_registry import DEFAULT_STRATEGIES_DIR, load_strategy_meta

sys.path.insert(0, str(Path(__file__).parent.parent))


SEED_BACKTEST_ID = "seed-example-sma-20240101"


def _seed_benchmark_ohlcv(data_dir: Path) -> None:
    """Write a deterministic AAPL/1d parquet so /api/benchmark has real data.

    Without this the benchmark endpoint 404s for every test in this module,
    so a 200-with-body branch could never be exercised — the conditional
    "200 or 404" contract tests passed vacuously. The legacy viewer also
    caches loaded frames per (symbol, timeframe), so the cache is cleared
    for the seeded symbol.
    """
    import pandas as pd

    import viewer.core as viewer_core

    symbol_dir = data_dir / "ohlcv" / "AAPL"
    symbol_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=30, freq="1D", tz="UTC"),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000.0,
        }
    )
    frame.to_parquet(symbol_dir / "1d.parquet", index=False)
    with viewer_core._OHLCV_DF_CACHE_LOCK:
        viewer_core._OHLCV_DF_CACHE.pop(("AAPL", "1d"), None)


def _seeded_backtest_result() -> BacktestResult:
    """A minimal, fully valid backtest result used as the seeded store row."""
    entry_time = "2024-01-02T14:30:00+00:00"
    exit_time = "2024-01-03T15:00:00+00:00"
    return BacktestResult(
        meta=BacktestMeta(
            id=SEED_BACKTEST_ID,
            created_at="2024-01-03T15:30:00+00:00",
            engine="vectorbt",
            strategies=["example_sma"],
            backtest_start="2024-01-01",
            backtest_end="2024-02-01",
            total_bars=22,
            data={"symbols": ["AAPL"], "timeframes": ["1d"]},
            initial_capital=100_000.0,
        ),
        trades=[
            Trade(
                id=1,
                symbol="AAPL",
                direction="LONG",
                entry_time=entry_time,
                entry_price=100.0,
                quantity=10.0,
                exits=[
                    TradeExit(
                        time=exit_time,
                        price=102.0,
                        quantity=10.0,
                        pnl=20.0,
                        reason="signal",
                    )
                ],
                pnl=20.0,
            )
        ],
        equity_curve=[EquityPoint(time=entry_time, equity=100_000.0)],
        stats={"total_trades": 1, "total_pnl": 20.0, "win_rate": 1.0},
    )


@pytest.fixture
def isolated_viewer(monkeypatch, tmp_path):
    """viewer app bound to a tmp data dir with one seeded backtest.

    Mirrors the isolation pattern from tests/test_viewer_features.py: both
    viewer.main and viewer.core (legacy) are pointed at tmp_path and the
    real SQLite engine is redirected to an isolated file so tests never
    touch the developer's data directory.
    """
    import shared.database as database
    import viewer.main as viewer_main_module

    legacy_module = viewer_main_module.legacy_viewer

    for module in (viewer_main_module, legacy_module):
        monkeypatch.setattr(module, "DATA_DIR", tmp_path)
        monkeypatch.setattr(module, "BACKTESTS_DIR", tmp_path / "backtests")
        monkeypatch.setattr(module, "LOGS_DIR", tmp_path / "logs")
        monkeypatch.setattr(module, "TASK_LOGS_DIR", tmp_path / "logs" / "tasks")
        monkeypatch.setattr(module, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")
        monkeypatch.setattr(module, "PORTFOLIOS_DIR", tmp_path / "portfolios")

    engine = database.create_engine(
        f"sqlite:///{(tmp_path / 'viewer-tests.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(database, "ENGINE", engine)
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)

    result = _seeded_backtest_result()
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    result.save(backtests_dir / f"{SEED_BACKTEST_ID}.trades.json")
    database.save_result_to_sqlite(result)
    _seed_benchmark_ohlcv(tmp_path)
    viewer_main_module._BACKTEST_META_CACHE.clear()

    yield viewer_main_module

    engine.dispose()
    viewer_main_module._BACKTEST_META_CACHE.clear()


@pytest.fixture
def client(isolated_viewer):
    """Create a test FastAPI app client on the isolated viewer."""
    return TestClient(isolated_viewer.app)


class TestStrategiesAPI:
    def test_list_strategies(self, client):
        resp = client.get("/api/strategies")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        # Each strategy should have name and file
        for s in data[:3]:
            assert "name" in s
            assert "file" in s

    def test_get_strategy_detail(self, client):
        resp = client.get("/api/strategies")
        strategies = resp.json()
        assert strategies, "Strategy registry must not be empty"
        name = strategies[0]["name"]
        resp2 = client.get(f"/api/strategies/{name}")
        assert resp2.status_code == 200
        detail = resp2.json()
        assert "name" in detail

    def test_get_strategy_detail_includes_parameters(self, client):
        resp = client.get("/api/strategies")
        strategies = resp.json()
        assert strategies, "Strategy registry must not be empty"
        target = next((item for item in strategies if item["slug"] == "example_sma"), None)
        assert target is not None, (
            f"example_sma missing from strategy list: "
            f"{[item.get('slug') for item in strategies]}"
        )

        detail_resp = client.get("/api/strategies/example_sma")
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        expected_parameters = load_strategy_meta(
            DEFAULT_STRATEGIES_DIR / "example_sma" / "example_sma.py"
        ).get("parameters")
        assert isinstance(expected_parameters, dict)
        assert detail["parameters"] == expected_parameters
        assert detail["parameters"] == {"fast_length": 9, "slow_length": 21}
        assert all(isinstance(key, str) for key in detail["parameters"])
        assert all(isinstance(value, int) for value in detail["parameters"].values())

    def test_get_nonexistent_strategy(self, client):
        resp = client.get("/api/strategies/nonexistent_strategy_xyz")
        assert resp.status_code == 404


class TestBacktestsAPI:
    def test_list_backtests_contains_seeded_record(self, client):
        resp = client.get("/api/backtests")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "items" in data
        assert "total" in data
        assert isinstance(data["items"], list)
        assert data["total"] >= 1, "Seeded backtest must be listed"
        assert any(item["id"] == SEED_BACKTEST_ID for item in data["items"]), (
            f"Seeded backtest {SEED_BACKTEST_ID} missing from list: "
            f"{[item.get('id') for item in data['items']]}"
        )

    def test_get_backtest_info(self, client):
        resp2 = client.get(f"/api/backtest/{SEED_BACKTEST_ID}/info")
        assert resp2.status_code == 200
        info = resp2.json()
        assert info["id"] == SEED_BACKTEST_ID
        assert info["strategy"] == "example_sma"
        assert "engine" in info
        assert info["trade_count"] == 1

    def test_get_backtest_stats(self, client):
        resp2 = client.get(f"/api/backtest/{SEED_BACKTEST_ID}/stats")
        assert resp2.status_code == 200
        stats = resp2.json()
        assert stats["total_trades"] == 1
        assert stats["net_profit"] == 20.0

    def test_get_backtest_trades(self, client):
        resp2 = client.get(f"/api/backtest/{SEED_BACKTEST_ID}/trades")
        assert resp2.status_code == 200
        trades = resp2.json()
        assert isinstance(trades, list)
        assert len(trades) == 1
        trade = trades[0]
        assert trade["symbol"] == "AAPL"
        assert trade["direction"] == "LONG"
        assert trade["quantity"] == 10.0
        assert trade["pnl"] == 20.0

    def test_get_backtest_equity(self, client):
        resp = client.get(f"/api/backtest/{SEED_BACKTEST_ID}/equity")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload, "Equity preview must not be empty for a seeded backtest"

    def test_get_backtest_detail(self, client):
        resp = client.get(f"/api/backtests/{SEED_BACKTEST_ID}")
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["meta"]["id"] == SEED_BACKTEST_ID
        assert len(detail["trades"]) == 1

    def test_get_nonexistent_backtest(self, client):
        resp = client.get("/api/backtest/nonexistent_bt_12345/info")
        assert resp.status_code == 404


class TestPortfoliosAPI:
    def test_list_portfolios_is_empty_but_wellformed(self, client):
        """Isolated store: the list is well-formed AND empty (no bleed-in)."""
        resp = client.get("/api/portfolios")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data == [], (
            f"Isolated store must not leak portfolios from other tests: {data}"
        )

    def test_get_nonexistent_portfolio(self, client):
        resp = client.get("/api/portfolios/nonexistent_portfolio_xyz")
        assert resp.status_code == 404

    def test_get_portfolio_detail(self, client):
        create_resp = client.post(
            "/api/portfolios",
            json={"name": "seed-portfolio", "account_capital": 100_000.0},
        )
        assert create_resp.status_code == 201, create_resp.text
        portfolio_id = create_resp.json()["id"]

        resp2 = client.get(f"/api/portfolios/{portfolio_id}")
        assert resp2.status_code == 200
        detail = resp2.json()
        assert detail["id"] == portfolio_id
        assert detail["name"] == "seed-portfolio"


class TestLogsAPI:
    def test_list_logs(self, client):
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)


class TestBenchmarkAPI:
    def test_benchmark_returns_payload_for_seeded_data(self, client):
        """The seeded AAPL/1d parquet must produce a 200 with a real payload.

        Previously this test accepted "200 or 404"; on the isolated store the
        endpoint always 404'd (no local OHLCV), so the 200-with-body branch
        was never executed and the test asserted nothing.
        """
        resp = client.get("/api/benchmark/AAPL")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["symbol"] == "AAPL"
        assert isinstance(data["equity"], list)
        assert data["equity"], "Benchmark equity must not be empty for seeded data"

    def test_benchmark_with_date_window_outside_data_is_empty_or_404(self, client):
        """A window entirely before the seeded data yields 404 or empty equity."""
        resp = client.get("/api/benchmark/AAPL?start=2023-01-01&end=2023-06-01")
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            assert isinstance(resp.json().get("equity"), list)

    def test_benchmark_nonexistent(self, client):
        resp = client.get("/api/benchmark/ZZZZZ_FAKE")
        assert resp.status_code == 404


class TestSymbolsAPI:
    def test_list_symbols(self, client):
        resp = client.get("/api/symbols")
        assert resp.status_code == 200
        data = resp.json()
        assert "symbols" in data
        assert "categories" in data
        assert isinstance(data["symbols"], list)
        assert isinstance(data["categories"], dict)
