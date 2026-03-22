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
            )
        ]

    monkeypatch.setattr(viewer_main_module, "discover_configured_accounts", _discover_accounts_stub)
    monkeypatch.setattr(viewer_main_module, "list_live_accounts", lambda: accounts)
    monkeypatch.setattr(viewer_main_module, "list_live_deployments", lambda: deployments)
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

    client = TestClient(viewer_main_module.app)

    accounts_resp = client.get("/api/live/accounts")
    assert accounts_resp.status_code == 200
    accounts_payload = accounts_resp.json()
    assert len(accounts_payload) == 1
    assert accounts_payload[0]["id"] == account_id
    assert accounts_payload[0]["is_demo"] is True
    assert accounts_payload[0]["balance"] == 10_000.0

    deployments_resp = client.get("/api/live/deployments")
    assert deployments_resp.status_code == 200
    deployments_payload = deployments_resp.json()
    assert len(deployments_payload) == 1
    assert deployments_payload[0]["status"] == "ACTIVE"

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
            "settings": {"risk_per_trade_pct": 0.01},
        },
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    assert created["id"] == deployment_id
    assert created["status"] == "ACTIVE"
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
