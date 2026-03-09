"""Tests for the Flask API endpoints in viewer/app.py."""

import json
import os
import sys
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def app():
    """Create a test Flask app."""
    from viewer.app import app as flask_app
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


class TestStrategiesAPI:
    def test_list_strategies(self, client):
        resp = client.get("/api/strategies")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) > 0
        # Each strategy should have name and file
        for s in data[:3]:
            assert "name" in s
            assert "file" in s

    def test_get_strategy_detail(self, client):
        # First get list of strategies
        resp = client.get("/api/strategies")
        strategies = resp.get_json()
        if strategies:
            name = strategies[0]["name"]
            resp2 = client.get(f"/api/strategies/{name}")
            assert resp2.status_code == 200
            detail = resp2.get_json()
            assert "name" in detail

    def test_get_strategy_detail_includes_parameters(self, client):
        resp = client.get("/api/strategies")
        strategies = resp.get_json()
        target = next((item for item in strategies if item["slug"] == "example_sma"), None)
        assert target is not None

        detail_resp = client.get("/api/strategies/example_sma")
        assert detail_resp.status_code == 200
        detail = detail_resp.get_json()
        assert detail["parameters"] == {"fast": 10, "slow": 30}

    def test_get_nonexistent_strategy(self, client):
        resp = client.get("/api/strategies/nonexistent_strategy_xyz")
        assert resp.status_code == 404


class TestBacktestsAPI:
    def test_list_backtests(self, client):
        resp = client.get("/api/backtests")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_get_backtest_info(self, client):
        resp = client.get("/api/backtests")
        backtests = resp.get_json()
        if backtests:
            bt_id = backtests[0]["id"]
            resp2 = client.get(f"/api/backtest/{bt_id}/info")
            assert resp2.status_code == 200
            info = resp2.get_json()
            assert "strategy" in info or "engine" in info

    def test_get_backtest_stats(self, client):
        resp = client.get("/api/backtests")
        backtests = resp.get_json()
        if backtests:
            bt_id = backtests[0]["id"]
            resp2 = client.get(f"/api/backtest/{bt_id}/stats")
            assert resp2.status_code == 200

    def test_get_backtest_trades(self, client):
        resp = client.get("/api/backtests")
        backtests = resp.get_json()
        if backtests:
            bt_id = backtests[0]["id"]
            resp2 = client.get(f"/api/backtest/{bt_id}/trades")
            assert resp2.status_code == 200
            assert isinstance(resp2.get_json(), list)

    def test_get_backtest_equity(self, client):
        resp = client.get("/api/backtests")
        backtests = resp.get_json()
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
        data = resp.get_json()
        assert isinstance(data, list)

    def test_get_portfolio_detail(self, client):
        resp = client.get("/api/portfolios")
        portfolios = resp.get_json()
        if portfolios:
            pid = portfolios[0]["id"]
            resp2 = client.get(f"/api/portfolios/{pid}")
            assert resp2.status_code == 200


class TestLogsAPI:
    def test_list_logs(self, client):
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)


class TestBenchmarkAPI:
    def test_benchmark_valid_symbol(self, client):
        resp = client.get("/api/benchmark/AAPL")
        if resp.status_code == 200:
            data = resp.get_json()
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
        data = resp.get_json()
        assert "symbols" in data
        assert "categories" in data
        assert isinstance(data["symbols"], list)
        assert isinstance(data["categories"], dict)

    def test_symbols_has_content(self, client):
        resp = client.get("/api/symbols")
        data = resp.get_json()
        # Should have at least some symbols from our downloaded data
        assert len(data["symbols"]) > 0
        assert len(data["categories"]) > 0
