from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from shared.models import LiveAccount, LiveAccountConfig, LiveBalanceRecord, LiveDeploymentConfig

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_live_endpoints_return_expected_payloads(monkeypatch):
    import viewer.main as viewer_main_module

    account_id = "alpaca-paper"
    deployment_id = "33333333-3333-3333-3333-333333333333"
    signal_id = "11111111-1111-1111-1111-111111111111"

    accounts = [LiveAccountConfig(id=account_id, broker="alpaca", name="Alpaca Paper")]
    deployments = [
        LiveDeploymentConfig(
            id=deployment_id,
            strategy_name="us_equities_momentum",
            account_id=account_id,
            symbols=["AAPL", "MSFT"],
            timeframe="1h",
            status="ACTIVE",
            signal_source="internal",
            signal_config={},
        )
    ]
    balance = LiveBalanceRecord(
        id=uuid4(),
        account_id=account_id,
        timestamp=datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
        balance=10_000.0,
        equity=10_010.0,
    )

    monkeypatch.setattr(viewer_main_module, "_seed_default_live_accounts", lambda: None)
    async def _discover_accounts_stub():
        return [
            LiveAccount(
                id=account_id,
                name="Alpaca Paper",
                broker="alpaca",
                balance=10_000.0,
                is_demo=True,
                broker_login=account_id,
            )
        ]

    monkeypatch.setattr(viewer_main_module, "discover_configured_accounts", _discover_accounts_stub)
    monkeypatch.setattr(viewer_main_module, "list_live_accounts", lambda: accounts)
    monkeypatch.setattr(viewer_main_module, "list_live_deployments", lambda: deployments)
    monkeypatch.setattr(
        viewer_main_module,
        "load_live_account_stats_inputs",
        lambda _account_id: (
            [balance] if _account_id == account_id else [],
            [
                {
                    "signal_id": signal_id,
                    "execution_id": "22222222-2222-2222-2222-222222222222",
                    "deployment_id": deployment_id,
                    "symbol": "AAPL",
                    "action": "buy",
                    "theoretical_price": 100.0,
                    "actual_price": 100.25,
                    "slippage": 0.25,
                    "slippage_bps": 25.0,
                    "filled_qty": 1.0,
                    "commission": 0.01,
                    "signal_timestamp": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                    "execution_timestamp": datetime(2026, 1, 1, 12, 0, 2, tzinfo=timezone.utc),
                    "broker_order_id": "broker-123",
                    "signal_metadata": {"qty": 1},
                }
            ]
            if _account_id == account_id
            else [],
        ),
    )
    monkeypatch.setattr(
        viewer_main_module,
        "list_live_deployments_for_account",
        lambda _account_id: deployments if _account_id == account_id else [],
    )
    monkeypatch.setattr(viewer_main_module, "list_live_balances", lambda _account_id: [balance])
    monkeypatch.setattr(
        viewer_main_module,
        "list_live_signal_executions_for_deployments",
        lambda _deployment_ids: [
            {
                "signal_id": signal_id,
                "execution_id": "22222222-2222-2222-2222-222222222222",
                "deployment_id": deployment_id,
                "symbol": "AAPL",
                "action": "buy",
                "theoretical_price": 100.0,
                "actual_price": 100.25,
                "slippage": 0.25,
                "slippage_bps": 25.0,
                "filled_qty": 1.0,
                "commission": 0.01,
                "signal_timestamp": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                "execution_timestamp": datetime(2026, 1, 1, 12, 0, 2, tzinfo=timezone.utc),
                "broker_order_id": "broker-123",
                "signal_metadata": {"qty": 1},
            }
        ],
    )
    monkeypatch.setattr(
        viewer_main_module,
        "_deployment_ids_for_account",
        lambda _account_id, _deployments: [_account_id, deployment_id],
    )
    monkeypatch.setattr(
        viewer_main_module,
        "load_live_account_stats_inputs",
        lambda _account_id: (
            [balance],
            [
                {
                    "signal_id": signal_id,
                    "execution_id": "22222222-2222-2222-2222-222222222222",
                    "deployment_id": deployment_id,
                    "symbol": "AAPL",
                    "action": "buy",
                    "theoretical_price": 100.0,
                    "actual_price": 100.25,
                    "filled_qty": 1.0,
                    "commission": 0.01,
                    "signal_timestamp": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                    "execution_timestamp": datetime(2026, 1, 1, 12, 0, 2, tzinfo=timezone.utc),
                    "broker_order_id": "broker-123",
                    "signal_metadata": {"qty": 1},
                }
            ],
        ),
    )

    client = TestClient(viewer_main_module.app)

    accounts_resp = client.get("/api/live/accounts")
    assert accounts_resp.status_code == 200
    accounts_payload = accounts_resp.json()
    assert len(accounts_payload) == 1
    assert accounts_payload[0]["id"] == account_id
    assert accounts_payload[0]["is_demo"] is True
    assert accounts_payload[0]["balance"] == 10_000.0
    assert accounts_payload[0]["broker_login"] == account_id
    assert accounts_payload[0]["ctid"] is None

    deployments_resp = client.get("/api/live/deployments")
    assert deployments_resp.status_code == 200
    deployments_payload = deployments_resp.json()
    assert len(deployments_payload) == 1
    assert deployments_payload[0]["status"] == "ACTIVE"
    assert deployments_payload[0]["signal_source"] == "internal"
    assert deployments_payload[0]["signal_config"] == {}

    balances_resp = client.get(f"/api/live/balances/{account_id}")
    assert balances_resp.status_code == 200
    balances_payload = balances_resp.json()
    assert balances_payload["account_id"] == account_id
    assert len(balances_payload["series"]) >= 1
    first_balance = balances_payload["series"][-1]
    assert first_balance["balance"] == 10_000.0
    assert first_balance["equity"] == 10_010.0

    trades_resp = client.get(f"/api/live/trades/{account_id}")
    assert trades_resp.status_code == 200
    trades_payload = trades_resp.json()
    assert trades_payload["account_id"] == account_id
    assert deployment_id in trades_payload["deployment_ids"]
    assert len(trades_payload["trades"]) >= 1
    first_trade = trades_payload["trades"][0]
    assert first_trade["signal_id"] == signal_id
    assert first_trade["actual_price"] == 100.25
    assert first_trade["theoretical_price"] == 100.0
    assert first_trade["slippage"] == 0.25

    stats_resp = client.get(f"/api/live/stats/{account_id}")
    assert stats_resp.status_code == 200
    stats_payload = stats_resp.json()
    assert stats_payload["account_id"] == account_id
    assert stats_payload["total_trades"] == 1
    assert stats_payload["winning_trades"] == 0
    assert stats_payload["losing_trades"] == 0
    assert stats_payload["win_rate"] == 0.0
    assert stats_payload["total_pnl"] == 0.0
    assert stats_payload["max_drawdown"] == 0.0
    assert stats_payload["max_drawdown_pct"] == 0.0
    assert stats_payload["current_balance"] == 10_000.0
    assert stats_payload["current_equity"] == 10_010.0


def test_live_deployments_crud_endpoints(monkeypatch):
    import viewer.main as viewer_main_module

    account_id = "alpaca-paper"
    deployment_id = "44444444-4444-4444-4444-444444444444"
    created_deployment = {
        "id": deployment_id,
        "strategy_name": "my_live_strategy",
        "account_id": account_id,
        "symbols": ["AAPL", "MSFT"],
        "timeframe": "1h",
        "status": "ACTIVE",
        "signal_source": "discord",
        "signal_config": {"channel_id": "123", "wait_candle_close": True},
    }

    monkeypatch.setattr(viewer_main_module, "_seed_default_live_accounts", lambda: None)
    monkeypatch.setattr(
        viewer_main_module,
        "_ensure_live_account_for_deployment",
        lambda _account_id: {
            "id": _account_id,
            "broker": "alpaca",
            "name": "Alpaca Paper",
        },
    )
    monkeypatch.setattr(
        viewer_main_module,
        "create_live_deployment",
        lambda deployment: LiveDeploymentConfig(
            id=created_deployment["id"],
            strategy_name=deployment.strategy_name,
            account_id=deployment.account_id,
            symbols=deployment.symbols,
            timeframe=deployment.timeframe,
            status="ACTIVE",
            signal_source=deployment.signal_source,
            signal_config=deployment.signal_config,
            settings=deployment.settings,
        ),
    )
    monkeypatch.setattr(
        viewer_main_module,
        "update_live_deployment_status",
        lambda _deployment_id, _status: LiveDeploymentConfig(
            id=_deployment_id,
            strategy_name=created_deployment["strategy_name"],
            account_id=created_deployment["account_id"],
            symbols=created_deployment["symbols"],
            timeframe=created_deployment["timeframe"],
            status=_status,
            signal_source=created_deployment["signal_source"],
            signal_config=created_deployment["signal_config"],
        ),
    )
    monkeypatch.setattr(viewer_main_module, "delete_live_deployment", lambda _deployment_id: True)

    client = TestClient(viewer_main_module.app)

    create_resp = client.post(
        "/api/live/deployments",
        json={
            "strategy_name": "my_live_strategy",
            "account_id": account_id,
            "symbols": ["AAPL", "MSFT"],
            "timeframe": "1h",
            "signal_source": "discord",
            "signal_config": {"channel_id": "123", "wait_candle_close": True},
            "settings": {"risk_per_trade_pct": 0.01},
        },
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    assert created["id"] == deployment_id
    assert created["status"] == "ACTIVE"
    assert created["signal_source"] == "discord"
    assert created["signal_config"]["channel_id"] == "123"
    assert created["settings"]["risk_per_trade_pct"] == 0.01

    update_resp = client.put(
        f"/api/live/deployments/{deployment_id}/status",
        json={"status": "PAUSED"},
    )
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["id"] == deployment_id
    assert updated["status"] == "PAUSED"

    delete_resp = client.delete(f"/api/live/deployments/{deployment_id}")
    assert delete_resp.status_code == 200
    deleted = delete_resp.json()
    assert deleted["status"] == "deleted"
    assert deleted["id"] == deployment_id


def test_live_deployments_returns_empty_array(monkeypatch):
    import viewer.main as viewer_main_module

    monkeypatch.setattr(viewer_main_module, "list_live_deployments", lambda: [])

    client = TestClient(viewer_main_module.app)
    response = client.get("/api/live/deployments")
    assert response.status_code == 200
    assert response.json() == []


def test_live_stats_endpoint_returns_defaults_for_missing_data(monkeypatch):
    import viewer.main as viewer_main_module

    monkeypatch.setattr(
        viewer_main_module,
        "load_live_account_stats_inputs",
        lambda _account_id: ([], []),
    )

    client = TestClient(viewer_main_module.app)
    response = client.get("/api/live/stats/unknown-account")

    assert response.status_code == 200
    payload = response.json()
    assert payload["account_id"] == "unknown-account"
    assert payload["total_trades"] == 0
    assert payload["winning_trades"] == 0
    assert payload["losing_trades"] == 0
    assert payload["win_rate"] == 0.0
    assert payload["total_pnl"] == 0.0
    assert payload["current_balance"] == 0.0
    assert payload["current_equity"] == 0.0
    assert payload["max_drawdown"] == 0.0
    assert payload["max_drawdown_pct"] == 0.0


def test_live_stats_endpoint_uses_realized_pnl_and_balance_drawdown(monkeypatch):
    import viewer.main as viewer_main_module

    account_id = "acc-1"
    balances = [
        LiveBalanceRecord(
            id=uuid4(),
            account_id=account_id,
            timestamp=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
            balance=1000.0,
            equity=1000.0,
        ),
        LiveBalanceRecord(
            id=uuid4(),
            account_id=account_id,
            timestamp=datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc),
            balance=1100.0,
            equity=1110.0,
        ),
        LiveBalanceRecord(
            id=uuid4(),
            account_id=account_id,
            timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            balance=880.0,
            equity=900.0,
        ),
        LiveBalanceRecord(
            id=uuid4(),
            account_id=account_id,
            timestamp=datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc),
            balance=950.0,
            equity=960.0,
        ),
    ]

    execution_rows = [
        {
            "symbol": "AAPL",
            "action": "buy",
            "actual_price": 100.0,
            "filled_qty": 1.0,
            "execution_timestamp": datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc),
        },
        {
            "symbol": "AAPL",
            "action": "sell",
            "actual_price": 110.0,
            "filled_qty": 1.0,
            "execution_timestamp": datetime(2026, 1, 1, 10, 10, tzinfo=timezone.utc),
        },
        {
            "symbol": "AAPL",
            "action": "buy",
            "actual_price": 100.0,
            "filled_qty": 1.0,
            "execution_timestamp": datetime(2026, 1, 1, 10, 15, tzinfo=timezone.utc),
        },
        {
            "symbol": "AAPL",
            "action": "sell",
            "actual_price": 90.0,
            "filled_qty": 1.0,
            "execution_timestamp": datetime(2026, 1, 1, 10, 20, tzinfo=timezone.utc),
        },
        {
            "symbol": "AAPL",
            "action": "buy",
            "actual_price": 105.0,
            "filled_qty": 1.0,
            "execution_timestamp": datetime(2026, 1, 1, 10, 25, tzinfo=timezone.utc),
        },
    ]
    monkeypatch.setattr(
        viewer_main_module,
        "load_live_account_stats_inputs",
        lambda _account_id: (balances, execution_rows),
    )

    client = TestClient(viewer_main_module.app)
    response = client.get(f"/api/live/stats/{account_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["account_id"] == account_id
    assert payload["total_trades"] == 5
    assert payload["winning_trades"] == 1
    assert payload["losing_trades"] == 1
    assert payload["win_rate"] == 20.0
    assert payload["total_pnl"] == 0.0
    assert payload["current_balance"] == 950.0
    assert payload["current_equity"] == 960.0
    assert payload["max_drawdown"] == 220.0
    assert payload["max_drawdown_pct"] == 20.0


def test_live_quote_and_positions_endpoints(monkeypatch):
    import viewer.main as viewer_main_module

    account_id = "alpaca-paper"
    monkeypatch.setattr(viewer_main_module, "_seed_default_live_accounts", lambda: None)
    monkeypatch.setattr(
        viewer_main_module,
        "get_live_account",
        lambda _account_id: (
            LiveAccountConfig(
                id=account_id,
                broker="alpaca",
                name="Alpaca Paper",
                broker_login="paper-login",
            )
            if _account_id == account_id
            else None
        ),
    )
    monkeypatch.setattr(
        viewer_main_module,
        "resolve_alpaca_credentials",
        lambda **kwargs: ("api-key", "secret-key", True),
    )

    class _FakeAlpacaAdapter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def get_positions(self):
            return [
                {
                    "symbol": "AAPL",
                    "qty": 2.0,
                    "entry_price": 100.0,
                    "current_price": 101.5,
                    "unrealized_pnl": 3.0,
                }
            ]

    class _FakeMarketDataService:
        async def get_quote_payload(self, symbol: str):
            return {
                "symbol": symbol.upper(),
                "latest": {
                    "symbol": symbol.upper(),
                    "timestamp": "2026-01-01T10:00:00+00:00",
                    "time": 1767261600.0,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.5,
                    "close": 100.8,
                    "volume": 1234.0,
                },
                "bars": [
                    {
                        "symbol": symbol.upper(),
                        "timestamp": "2026-01-01T10:00:00+00:00",
                        "time": 1767261600.0,
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.5,
                        "close": 100.8,
                        "volume": 1234.0,
                    }
                ],
            }

    monkeypatch.setattr(viewer_main_module, "AlpacaLiveAdapter", _FakeAlpacaAdapter)
    monkeypatch.setattr(viewer_main_module, "_live_market_data_service", _FakeMarketDataService())

    client = TestClient(viewer_main_module.app)

    quote_resp = client.get("/api/live/quotes/AAPL")
    assert quote_resp.status_code == 200
    quote_payload = quote_resp.json()
    assert quote_payload["symbol"] == "AAPL"
    assert quote_payload["latest"]["close"] == 100.8

    positions_resp = client.get(f"/api/live/positions/{account_id}")
    assert positions_resp.status_code == 200
    positions_payload = positions_resp.json()
    assert positions_payload["account_id"] == account_id
    assert positions_payload["positions"] == [
        {
            "symbol": "AAPL",
            "qty": 2.0,
            "entry_price": 100.0,
            "current_price": 101.5,
            "unrealized_pnl": 3.0,
        }
    ]


def test_live_quote_endpoint_returns_404_when_no_data(monkeypatch):
    import viewer.main as viewer_main_module

    class _FakeMarketDataService:
        async def get_quote_payload(self, symbol: str):
            return {"symbol": symbol.upper(), "latest": None, "bars": []}

    monkeypatch.setattr(viewer_main_module, "_live_market_data_service", _FakeMarketDataService())

    client = TestClient(viewer_main_module.app)
    response = client.get("/api/live/quotes/TSLA")
    assert response.status_code == 404


def test_live_signals_endpoint_returns_signal_log(monkeypatch):
    import viewer.main as viewer_main_module

    account_id = "alpaca-paper"
    deployment_id = "55555555-5555-5555-5555-555555555555"
    now = datetime.now(timezone.utc)

    monkeypatch.setattr(viewer_main_module, "_seed_default_live_accounts", lambda: None)
    monkeypatch.setattr(
        viewer_main_module,
        "list_live_deployments_for_account",
        lambda _account_id: (
            [
                LiveDeploymentConfig(
                    id=deployment_id,
                    strategy_name="discord_scalper",
                    account_id=account_id,
                    symbols=["AAPL"],
                    timeframe="1m",
                    status="ACTIVE",
                    signal_source="discord",
                    signal_config={"wait_candle_close": True},
                    settings={},
                )
            ]
            if _account_id == account_id
            else []
        ),
    )
    monkeypatch.setattr(
        viewer_main_module,
        "_deployment_ids_for_account",
        lambda _account_id, _deployments: [_account_id, deployment_id],
    )
    monkeypatch.setattr(
        viewer_main_module,
        "list_live_signals_for_deployments",
        lambda _deployment_ids, limit=200: [
            {
                "signal_id": "sig-1",
                "deployment_id": deployment_id,
                "symbol": "AAPL",
                "action": "buy",
                "source": "discord",
                "theoretical_price": 150.25,
                "signal_timestamp": now,
                "signal_metadata": {
                    "source": "discord",
                    "timeframe": "1m",
                    "wait_for_candle_close": True,
                    "discord_content": "BUY AAPL @ 150.25",
                },
                "execution_id": None,
                "execution_timestamp": None,
                "actual_price": None,
                "filled_qty": None,
                "commission": None,
                "broker_order_id": None,
            }
        ],
    )

    client = TestClient(viewer_main_module.app)
    response = client.get(f"/api/live/signals/{account_id}?limit=50")
    assert response.status_code == 200

    payload = response.json()
    assert payload["account_id"] == account_id
    assert deployment_id in payload["deployment_ids"]
    assert len(payload["signals"]) == 1

    signal = payload["signals"][0]
    assert signal["signal_id"] == "sig-1"
    assert signal["symbol"] == "AAPL"
    assert signal["source"] == "discord"
    assert signal["status"] in {"Waiting", "Pending"}
    assert signal["waiting_for_candle_close"] is True
    assert signal["raw_message"] == "BUY AAPL @ 150.25"
    assert signal["execution_id"] is None
