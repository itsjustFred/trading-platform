"""Tests for the FastAPI endpoints in viewer/main.py."""

import json
import os
import sys
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from shared.strategy_registry import DEFAULT_STRATEGIES_DIR, load_strategy_meta

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def client():
    """Create a test FastAPI app client."""
    import viewer.main as viewer_main_module

    return TestClient(viewer_main_module.app)


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
        # Use a known seeded strategy instead of conditionally relying on the
        # list endpoint, so the detail assertion always runs.
        resp2 = client.get("/api/strategies/example_sma")
        assert resp2.status_code == 200
        detail = resp2.json()
        assert "name" in detail
        assert detail.get("name") == "example_sma"

    def test_get_strategy_detail_includes_parameters(self, client):
        resp = client.get("/api/strategies")
        strategies = resp.json()
        target = next((item for item in strategies if item["slug"] == "example_sma"), None)
        assert target is not None

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
    def test_list_backtests(self, client):
        resp = client.get("/api/backtests")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "items" in data
        assert "total" in data
        assert isinstance(data["items"], list)

    def test_get_backtest_info(self, client):
        resp = client.get("/api/backtests")
        backtests = resp.json().get("items", [])
        if backtests:
            bt_id = backtests[0]["id"]
            resp2 = client.get(f"/api/backtest/{bt_id}/info")
            assert resp2.status_code == 200
            info = resp2.json()
            assert "strategy" in info or "engine" in info

    def test_get_backtest_stats(self, client):
        resp = client.get("/api/backtests")
        backtests = resp.json().get("items", [])
        if backtests:
            bt_id = backtests[0]["id"]
            resp2 = client.get(f"/api/backtest/{bt_id}/stats")
            assert resp2.status_code == 200

    def test_get_backtest_trades(self, client):
        resp = client.get("/api/backtests")
        backtests = resp.json().get("items", [])
        if backtests:
            bt_id = backtests[0]["id"]
            resp2 = client.get(f"/api/backtest/{bt_id}/trades")
            assert resp2.status_code == 200
            assert isinstance(resp2.json(), list)

    def test_get_backtest_equity(self, client):
        resp = client.get("/api/backtests")
        backtests = resp.json().get("items", [])
        if backtests:
            bt_id = backtests[0]["id"]
            resp2 = client.get(f"/api/backtest/{bt_id}/equity")
            assert resp2.status_code == 200

    def test_get_nonexistent_backtest(self, client):
        resp = client.get("/api/backtest/nonexistent_bt_12345/info")
        assert resp.status_code == 404


class TestPortfoliosAPI:
    def test_list_portfolios(self, client):
        resp = client.get("/api/portfolios")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_get_portfolio_detail(self, client):
        resp = client.get("/api/portfolios")
        portfolios = resp.json()
        if portfolios:
            pid = portfolios[0]["id"]
            resp2 = client.get(f"/api/portfolios/{pid}")
            assert resp2.status_code == 200


class TestLogsAPI:
    def test_list_logs(self, client):
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)


class TestBenchmarkAPI:
    def test_benchmark_valid_symbol(self, client):
        resp = client.get("/api/benchmark/AAPL")
        if resp.status_code == 200:
            data = resp.json()
            assert "symbol" in data
            assert "equity" in data
            assert isinstance(data["equity"], list)
        else:
            # Might be 404 if data not available
            assert resp.status_code == 404

    def test_benchmark_with_dates(self, client):
        resp = client.get("/api/benchmark/AAPL?start=2023-01-01&end=2024-01-01")
        assert resp.status_code in (200, 404)

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

    def test_symbols_has_content(self, client):
        resp = client.get("/api/symbols")
        data = resp.json()
        # Should have at least some symbols from our downloaded data
        assert len(data["symbols"]) > 0
        assert len(data["categories"]) > 0
