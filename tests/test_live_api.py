from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from shared.models import (
    LiveAccount,
    LiveAccountConfig,
    LiveBalanceRecord,
    LiveDeploymentConfig,
    Service,
    ServiceStatus,
    ServiceType,
)

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_live_endpoints_return_expected_payloads(monkeypatch):
    import viewer.main as viewer_main_module

    account_id = "PA3T0MQAGQ51"
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
    # Creating a deployment now requires a KNOWN account: the endpoint refuses an
    # unknown one rather than binding a deployment to an arbitrary id. Register
    # the account the test uses so the create call exercises that gate honestly
    # instead of tripping over it.
    monkeypatch.setattr(
        viewer_main_module,
        "get_live_account",
        lambda requested: next(
            (account for account in accounts if str(account.id) == str(requested)), None
        ),
    )
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
    # The single buy execution never closes a round trip, so no trade counts.
    assert stats_payload["total_trades"] == 0
    assert stats_payload["winning_trades"] == 0
    assert stats_payload["losing_trades"] == 0
    assert stats_payload["win_rate"] == 0.0
    assert stats_payload["total_pnl"] == 0.0
    assert stats_payload["max_drawdown"] == 0.0
    assert stats_payload["max_drawdown_pct"] == 0.0
    assert stats_payload["current_balance"] == 10_000.0
    assert stats_payload["current_equity"] == 10_010.0


def test_live_account_discovery_persists_demo_identity(monkeypatch):
    import asyncio

    import viewer.main as viewer_main_module

    discovered = [
        {
            "id": "acc-demo",
            "name": "Demo",
            "broker": "alpaca",
            "is_demo": True,
            "broker_login": "login-1",
            "ctid": None,
        }
    ]
    persisted: list[LiveAccountConfig] = []
    monkeypatch.setattr(
        viewer_main_module,
        "_cached_live_accounts_payload",
        lambda _force_refresh=False: discovered,
    )
    monkeypatch.setattr(
        viewer_main_module,
        "upsert_live_accounts",
        lambda accounts: persisted.extend(accounts),
    )

    result = asyncio.run(viewer_main_module.get_live_accounts())

    assert result == discovered
    assert persisted[0].is_demo is True
    assert persisted[0].broker_login == "login-1"


def test_live_deployments_crud_endpoints(monkeypatch):
    import viewer.main as viewer_main_module

    # Local account id is a UUID; the Alpaca account number is broker identity.
    account_id = "59d94c55-f6d3-4c38-ba4d-ec1e22abdbcf"
    ramjet_account_number = "PA3T0MQAGQ51"
    _ACCOUNT_ROW = LiveAccountConfig(
        id=account_id,
        broker="alpaca",
        broker_login=ramjet_account_number,
        is_demo=True,
    )
    deployment_id = "44444444-4444-4444-4444-444444444444"
    created_deployment = {
        "id": deployment_id,
        "strategy_name": "my_live_strategy",
        "account_id": account_id,
        "symbols": ["AAPL", "MSFT"],
        "symbol_filter": ["AAPL", "MSFT"],
        "timeframe": "1h",
        "status": "ACTIVE",
        "signal_source_type": "discord",
        "signal_source_id": "RAMJET",
        "signal_source": "discord",
        "signal_config": {"channel_id": "1431465140905185452", "wait_candle_close": True},
    }

    monkeypatch.setattr(viewer_main_module, "_seed_default_live_accounts", lambda: None)
    monkeypatch.setattr(
        viewer_main_module,
        "list_services",
        lambda *args, **kwargs: [
            Service(
                id="88888888-8888-8888-8888-888888888888",
                name="RAMJET",
                type=ServiceType.DISCORD_BOT,
                status=ServiceStatus.RUNNING,
                config={"token_env_key": "DISCORD_BOT_RAMJET_TOKEN", "channel_id": "1431465140905185452"},
                created_at=datetime.now(timezone.utc),
            )
        ],
    )
    monkeypatch.setattr(
        viewer_main_module,
        "_ensure_live_account_for_deployment",
        lambda _account_id: {
            "id": _account_id,
            "broker": "alpaca",
            "name": "Alpaca Paper",
            # Broker-reported identity. Routing is validated against this, not
            # against the local account id, which is a different id space.
            "broker_login": ramjet_account_number,
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
            signal_source_type=deployment.signal_source_type,
            signal_source_id=deployment.signal_source_id,
            symbol_filter=deployment.symbol_filter,
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
            signal_source_type=created_deployment["signal_source_type"],
            signal_source_id=created_deployment["signal_source_id"],
            symbol_filter=created_deployment["symbol_filter"],
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
            "signal_source_type": "discord",
            "signal_source_id": "RAMJET",
            "symbol_filter": ["AAPL", "MSFT"],
            "signal_source": "discord",
            "signal_config": {"channel_id": "1431465140905185452", "wait_candle_close": True},
            "settings": {"risk_per_trade_pct": 0.01},
        },
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    assert created["id"] == deployment_id
    assert created["status"] == "ACTIVE"
    assert created["signal_source_type"] == "discord"
    assert created["signal_source_id"] == "RAMJET"
    assert created["symbol_filter"] == ["AAPL", "MSFT"]
    assert created["signal_source"] == "discord"
    assert created["signal_config"]["channel_id"] == "1431465140905185452"
    assert created["settings"]["risk_per_trade_pct"] == 0.01

    update_resp = client.put(
        f"/api/live/deployments/{deployment_id}/status",
        # Confirmed as the real client does; the refusal path has its own tests.
        json={
            "status": "PAUSED",
            "confirmation": {"confirmed_deployment_id": deployment_id},
        },
    )
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["id"] == deployment_id
    assert updated["status"] == "PAUSED"

    delete_resp = client.delete(
        f"/api/live/deployments/{deployment_id}"
        f"?confirm_deployment_id={deployment_id}"
    )
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


def test_live_dashboard_endpoint_returns_telemetry(monkeypatch):
    import viewer.main as viewer_main_module

    account = LiveAccountConfig(id="acct-1", broker="alpaca", name="Account One")
    deployment = LiveDeploymentConfig(
        id="55555555-5555-5555-5555-555555555555",
        strategy_name="live_strategy",
        account_id="acct-1",
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
    )

    monkeypatch.setattr(viewer_main_module, "_seed_default_live_accounts", lambda: None)
    monkeypatch.setattr(viewer_main_module, "list_live_accounts", lambda: [account])
    monkeypatch.setattr(viewer_main_module, "list_live_deployments", lambda: [deployment])
    monkeypatch.setattr(
        viewer_main_module,
        "_live_account_stats_payload",
        lambda _account_id: {
            "account_id": _account_id,
            "current_equity": 10000.0,
            "current_balance": 9900.0,
            "total_trades": 1,
            "total_pnl": 25.0,
        },
    )

    async def _fake_trades(_account_id):
        return {"account_id": _account_id, "deployment_ids": [str(deployment.id)], "trades": []}

    monkeypatch.setattr(viewer_main_module, "_live_trades_payload", _fake_trades)

    client = TestClient(viewer_main_module.app)
    response = client.get("/api/live/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capital"]["total_equity"] == 10000.0
    assert payload["active_strategies"][0]["strategy_name"] == "live_strategy"
    assert payload["active_strategies"][0]["capital_allocation"] == 1.0


def test_ctrader_top5_endpoint_dry_run(monkeypatch):
    import viewer.main as viewer_main_module

    monkeypatch.setattr(viewer_main_module, "_seed_default_live_accounts", lambda: None)
    monkeypatch.setattr(
        viewer_main_module,
        "list_live_accounts",
        lambda: [LiveAccountConfig(id="ct-1", broker="ctrader", name="cTrader")],
    )
    monkeypatch.setattr(
        viewer_main_module,
        "list_backtest_metadata_rows",
        lambda: [
            {
                "id": "bt-1",
                "strategy_name": "fx_breakout",
                "symbols": ["EURUSD"],
                "timeframe": "15m",
                "sharpe_ratio": 1.4,
                "cagr": 0.2,
                "psr": 0.8,
                "max_drawdown": -5,
                "trade_count": 11,
                # C5: deployment shortlist requires OOS evidence.
                "oos_sharpe": 0.9,
            }
        ],
    )

    client = TestClient(viewer_main_module.app)
    response = client.post("/api/live/deployments/ctrader/top5", json={"dry_run": True})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "planned"
    assert payload["deployments"][0]["account_id"] == "ct-1"
    assert payload["deployments"][0]["signal_config"]["source_backtest_id"] == "bt-1"


def test_discord_stockkit_deployment_rejects_wrong_account(monkeypatch):
    import viewer.main as viewer_main_module

    monkeypatch.setattr(viewer_main_module, "_seed_default_live_accounts", lambda: None)
    monkeypatch.setattr(
        viewer_main_module,
        "list_services",
        lambda *args, **kwargs: [
            Service(
                id="88888888-8888-8888-8888-888888888888",
                name="RAMJET",
                type=ServiceType.DISCORD_BOT,
                status=ServiceStatus.RUNNING,
                config={"token_env_key": "DISCORD_BOT_RAMJET_TOKEN", "channel_id": "1431465140905185452"},
                created_at=datetime.now(timezone.utc),
            )
        ],
    )
    monkeypatch.setattr(
        viewer_main_module,
        "_ensure_live_account_for_deployment",
        lambda _account_id: {"id": _account_id, "broker": "alpaca", "name": "Wrong"},
    )

    client = TestClient(viewer_main_module.app)
    response = client.post(
        "/api/live/deployments",
        json={
            "strategy_name": "my_live_strategy",
            "account_id": "wrong-account",
            "symbols": ["AAPL"],
            "timeframe": "1h",
            "signal_source_type": "discord",
            "signal_source_id": "RAMJET",
            "signal_source": "discord",
            "signal_config": {"channel_id": "1431465140905185452", "signal_processor": "ramjet"},
        },
    )

    assert response.status_code == 400
    assert "PA3T0MQAGQ51" in response.json()["detail"]


def test_services_crud_and_logs_endpoints(monkeypatch):
    import viewer.main as viewer_main_module

    service_id = "77777777-7777-7777-7777-777777777777"
    services: dict[str, Service] = {}

    def _list_services(*, service_type=None):
        payload = list(services.values())
        if service_type is None:
            return payload
        return [service for service in payload if service.type == service_type]

    def _create_service(service: Service):
        created = service.model_copy(update={"id": service_id})
        services[service_id] = created
        return created

    def _get_service(value: str):
        return services.get(value)

    # Mirrors the real signature, including `respect_operator_intent`: a double
    # that does not accept it fails loudly rather than hiding the new contract.
    def _update_service_status(
        value: str, status, *, last_activity=None, respect_operator_intent=True
    ):
        existing = services.get(value)
        if existing is None:
            return None
        updated = existing.model_copy(
            update={
                "status": ServiceStatus(status),
                "last_activity": last_activity or existing.last_activity,
            }
        )
        services[value] = updated
        return updated

    def _delete_service(value: str):
        return services.pop(value, None) is not None

    monkeypatch.setattr(viewer_main_module, "list_services", _list_services)
    monkeypatch.setattr(viewer_main_module, "create_service", _create_service)
    monkeypatch.setattr(viewer_main_module, "get_service", _get_service)
    monkeypatch.setattr(viewer_main_module, "update_service_status", _update_service_status)
    monkeypatch.setattr(viewer_main_module, "delete_service", _delete_service)
    monkeypatch.setattr(viewer_main_module.legacy_viewer, "_available_log_files", lambda: [])
    monkeypatch.setattr(
        viewer_main_module.legacy_viewer,
        "_collect_log_entries",
        lambda _paths, **_kwargs: [
            {
                "line": "Started Discord bot RAMJET",
                "level": "info",
                "path": "live-runner.log",
                "line_number": 1,
                "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            },
            {
                "line": "Discovered 3 Discord bots: RAMJET, ROCKETFUEL, NITRO",
                "level": "info",
                "path": "live-runner.log",
                "line_number": 2,
                "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            },
        ],
    )

    client = TestClient(viewer_main_module.app)

    create_response = client.post(
        "/api/services",
        json={
            "name": "RAMJET",
            "type": "discord_bot",
            "status": "running",
            "config": {"token_env_key": "DISCORD_BOT_RAMJET_TOKEN", "channel_id": "1431465140905185452"},
        },
    )
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["id"] == service_id
    assert created["name"] == "RAMJET"
    assert created["type"] == "discord_bot"
    assert created["status"] == "running"

    list_response = client.get("/api/services")
    assert list_response.status_code == 200
    listed = list_response.json()
    assert len(listed) == 1
    assert listed[0]["name"] == "RAMJET"

    detail_response = client.get(f"/api/services/{service_id}")
    assert detail_response.status_code == 200
    assert detail_response.json()["id"] == service_id

    status_response = client.put(
        f"/api/services/{service_id}/status",
        json={"status": "paused"},
    )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "paused"

    logs_response = client.get(f"/api/services/{service_id}/logs")
    assert logs_response.status_code == 200
    logs_payload = logs_response.json()
    assert logs_payload["name"] == "service:RAMJET"
    assert logs_payload["total_lines"] >= 1

    delete_response = client.delete(f"/api/services/{service_id}")
    assert delete_response.status_code == 200
    assert delete_response.json()["status"] == "deleted"


def test_verification_evidence_endpoints(monkeypatch):
    import viewer.main as viewer_main_module

    record = {
        "id": "evidence-1",
        "run_id": "task:abc",
        "component": "scheduler",
        "flow_name": "weekly refresh",
        "category": "service",
        "status": "success",
        "started_at": "2026-04-19T10:00:00+00:00",
        "finished_at": "2026-04-19T10:00:05+00:00",
        "duration_seconds": 5.0,
        "git_sha": "deadbeef",
        "environment": "local",
        "mode": "thread",
        "metadata": {"task_id": "abc"},
        "error_summary": None,
        "evidence_ref": "/tmp/task.log",
        "manual_validation_required": True,
        "source": "scheduler",
        "created_at": "2026-04-19T10:00:05+00:00",
        "updated_at": "2026-04-19T10:00:05+00:00",
    }
    summary = {
        "component": "scheduler",
        "flow_name": "weekly refresh",
        "category": "service",
        "run_count": 3,
        "success_count": 2,
        "failed_count": 1,
        "partial_count": 0,
        "skipped_count": 0,
        "last_status": "success",
        "last_run_at": "2026-04-19T10:00:05+00:00",
        "last_success_at": "2026-04-19T10:00:05+00:00",
        "last_error": "",
        "latest_git_sha": "deadbeef",
        "latest_environment": "local",
        "latest_mode": "thread",
        "manual_validation_required": True,
    }

    captured: dict[str, object] = {}

    def _list_records(**_kwargs):
        return [record]

    def _list_summary(**_kwargs):
        return [summary]

    def _upsert(**kwargs):
        captured.update(kwargs)
        created = dict(record)
        created.update(
            {
                "component": kwargs["component"],
                "flow_name": kwargs["flow_name"],
                "category": kwargs["category"],
                "status": kwargs["status"],
                "metadata": dict(kwargs.get("metadata") or {}),
                "source": kwargs.get("source", ""),
                "manual_validation_required": bool(kwargs.get("manual_validation_required")),
            }
        )
        return created

    monkeypatch.setattr(viewer_main_module, "list_verification_evidence_records", _list_records)
    monkeypatch.setattr(viewer_main_module, "list_verification_evidence_summary", _list_summary)
    monkeypatch.setattr(viewer_main_module, "upsert_verification_evidence", _upsert)

    client = TestClient(viewer_main_module.app)

    list_response = client.get("/api/verification/evidence?component=scheduler&limit=10")
    assert list_response.status_code == 200
    listed = list_response.json()
    assert len(listed) == 1
    assert listed[0]["component"] == "scheduler"
    assert listed[0]["status"] == "success"

    summary_response = client.get("/api/verification/evidence/summary?component=scheduler")
    assert summary_response.status_code == 200
    summaries = summary_response.json()
    assert len(summaries) == 1
    assert summaries[0]["run_count"] == 3
    assert summaries[0]["failed_count"] == 1

    create_response = client.post(
        "/api/verification/evidence",
        json={
            "component": "discord",
            "flow_name": "auth-check",
            "category": "discord",
            "status": "partial",
            "metadata": {"bot": "RAMJET"},
            "manual_validation_required": True,
            "source": "manual-validation",
            "run_id": "discord:auth-check:1",
        },
    )
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["component"] == "discord"
    assert created["flow_name"] == "auth-check"
    assert created["status"] == "partial"
    assert created["metadata"]["bot"] == "RAMJET"
    assert captured["run_id"] == "discord:auth-check:1"


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
    # Two closed round trips (one win, one loss); the trailing open buy does
    # not count, and win rate is 1/2 closed trades, not 1/5 executions.
    assert payload["total_trades"] == 2
    assert payload["winning_trades"] == 1
    assert payload["losing_trades"] == 1
    assert payload["win_rate"] == 50.0
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


def test_live_accounts_websocket_streams_snapshot_and_update(monkeypatch):
    import viewer.main as viewer_main_module

    snapshots = iter(
        [
            {
                "accounts": [
                    {
                        "id": "acc-1",
                        "broker": "alpaca",
                        "name": "Primary",
                        "balance": 10_000.0,
                        "equity": 10_000.0,
                        "is_demo": True,
                    }
                ]
            },
            {
                "accounts": [
                    {
                        "id": "acc-1",
                        "broker": "alpaca",
                        "name": "Primary",
                        "balance": 10_015.5,
                        "equity": 10_020.0,
                        "is_demo": True,
                    }
                ]
            },
        ]
    )
    latest_payload = {
        "accounts": [
            {
                "id": "acc-1",
                "broker": "alpaca",
                "name": "Primary",
                "balance": 10_015.5,
                "equity": 10_020.0,
                "is_demo": True,
            }
        ]
    }

    def _next_payload() -> dict[str, object]:
        nonlocal latest_payload
        try:
            latest_payload = next(snapshots)
        except StopIteration:
            pass
        return latest_payload

    monkeypatch.setattr(viewer_main_module, "_live_accounts_stream_payload", _next_payload)
    monkeypatch.setattr(viewer_main_module, "_LIVE_WS_ACCOUNTS_POLL_SECONDS", 0.05)
    monkeypatch.setattr(viewer_main_module, "_LIVE_WS_HEARTBEAT_SECONDS", 5.0)

    client = TestClient(viewer_main_module.app)
    with client.websocket_connect("/ws/live/accounts") as websocket:
        first = websocket.receive_json()
        second = websocket.receive_json()

    assert first["type"] == "snapshot"
    assert first["topic"] == "live.accounts"
    assert first["data"]["accounts"][0]["balance"] == 10_000.0

    assert second["type"] == "update"
    assert second["topic"] == "live.accounts"
    assert second["data"]["accounts"][0]["equity"] == 10_020.0


def test_live_signals_websocket_streams_account_payload(monkeypatch):
    import viewer.main as viewer_main_module

    calls: list[tuple[str, int]] = []

    async def _signals_payload(account_id: str, *, limit: int = 200) -> dict[str, object]:
        calls.append((account_id, limit))
        return {
            "account_id": account_id,
            "deployment_ids": ["dep-1"],
            "signals": [
                {
                    "signal_id": "sig-1",
                    "deployment_id": "dep-1",
                    "symbol": "AAPL",
                    "action": "buy",
                    "status": "Pending",
                    "source": "discord",
                    "execution_id": None,
                    "signal_timestamp": "2026-01-01T10:00:00+00:00",
                    "signal_time": 1767261600.0,
                    "execution_timestamp": "",
                    "execution_time": 0.0,
                    "theoretical_price": 100.0,
                    "actual_price": None,
                    "filled_qty": None,
                    "commission": None,
                    "broker_order_id": None,
                    "signal_metadata": {},
                    "raw_message": None,
                    "waiting_for_candle_close": False,
                    "waiting_countdown_seconds": None,
                }
            ],
        }

    monkeypatch.setattr(viewer_main_module, "_live_signals_payload", _signals_payload)

    client = TestClient(viewer_main_module.app)
    with client.websocket_connect("/ws/live/test-account/signals") as websocket:
        snapshot = websocket.receive_json()

    assert snapshot["type"] == "snapshot"
    assert snapshot["topic"] == "live.signals.test-account"
    assert snapshot["data"]["account_id"] == "test-account"
    assert calls[0] == ("test-account", 200)


def test_live_positions_websocket_streams_account_payload(monkeypatch):
    import viewer.main as viewer_main_module

    calls: list[str] = []

    async def _positions_payload(account_id: str) -> dict[str, object]:
        calls.append(account_id)
        return {
            "account_id": account_id,
            "positions": [
                {
                    "symbol": "AAPL",
                    "qty": 1.0,
                    "entry_price": 100.0,
                    "current_price": 101.0,
                    "unrealized_pnl": 1.0,
                }
            ],
        }

    monkeypatch.setattr(viewer_main_module, "_live_positions_payload", _positions_payload)

    client = TestClient(viewer_main_module.app)
    with client.websocket_connect("/ws/live/test-account/positions") as websocket:
        snapshot = websocket.receive_json()

    assert snapshot["type"] == "snapshot"
    assert snapshot["topic"] == "live.positions.test-account"
    assert snapshot["data"]["account_id"] == "test-account"
    assert calls[0] == "test-account"


def test_live_executions_websocket_streams_account_payload(monkeypatch):
    import viewer.main as viewer_main_module

    calls: list[str] = []

    async def _executions_payload(account_id: str) -> dict[str, object]:
        calls.append(account_id)
        return {
            "account_id": account_id,
            "deployment_ids": ["dep-1"],
            "trades": [
                {
                    "signal_id": "sig-1",
                    "execution_id": "exe-1",
                    "deployment_id": "dep-1",
                    "account_id": account_id,
                    "symbol": "AAPL",
                    "action": "buy",
                    "theoretical_price": 100.0,
                    "actual_price": 100.1,
                    "slippage": 0.1,
                    "slippage_bps": 10.0,
                    "filled_qty": 1.0,
                    "commission": 0.01,
                    "signal_timestamp": "2026-01-01T10:00:00+00:00",
                    "signal_time": 1767261600.0,
                    "execution_timestamp": "2026-01-01T10:00:01+00:00",
                    "execution_time": 1767261601.0,
                    "broker_order_id": "order-1",
                    "signal_metadata": {},
                }
            ],
        }

    monkeypatch.setattr(viewer_main_module, "_live_trades_payload", _executions_payload)

    client = TestClient(viewer_main_module.app)
    with client.websocket_connect("/ws/live/test-account/executions") as websocket:
        snapshot = websocket.receive_json()

    assert snapshot["type"] == "snapshot"
    assert snapshot["topic"] == "live.executions.test-account"
    assert snapshot["data"]["account_id"] == "test-account"
    assert calls[0] == "test-account"


def test_live_executions_websocket_streams_snapshot_and_update(monkeypatch):
    import viewer.main as viewer_main_module

    snapshots = iter(
        [
            {
                "account_id": "test-account",
                "deployment_ids": ["dep-1"],
                "trades": [
                    {
                        "signal_id": "sig-1",
                        "execution_id": "exe-1",
                        "deployment_id": "dep-1",
                        "account_id": "test-account",
                        "symbol": "AAPL",
                        "action": "buy",
                        "theoretical_price": 100.0,
                        "actual_price": 100.0,
                        "slippage": 0.0,
                        "slippage_bps": 0.0,
                        "filled_qty": 1.0,
                        "commission": 0.01,
                        "signal_timestamp": "2026-01-01T10:00:00+00:00",
                        "signal_time": 1767261600.0,
                        "execution_timestamp": "2026-01-01T10:00:01+00:00",
                        "execution_time": 1767261601.0,
                        "broker_order_id": "order-1",
                        "signal_metadata": {},
                    }
                ],
            },
            {
                "account_id": "test-account",
                "deployment_ids": ["dep-1"],
                "trades": [
                    {
                        "signal_id": "sig-1",
                        "execution_id": "exe-2",
                        "deployment_id": "dep-1",
                        "account_id": "test-account",
                        "symbol": "AAPL",
                        "action": "buy",
                        "theoretical_price": 100.0,
                        "actual_price": 100.2,
                        "slippage": 0.2,
                        "slippage_bps": 20.0,
                        "filled_qty": 1.0,
                        "commission": 0.01,
                        "signal_timestamp": "2026-01-01T10:00:00+00:00",
                        "signal_time": 1767261600.0,
                        "execution_timestamp": "2026-01-01T10:00:02+00:00",
                        "execution_time": 1767261602.0,
                        "broker_order_id": "order-2",
                        "signal_metadata": {},
                    }
                ],
            },
        ]
    )
    latest_payload = {
        "account_id": "test-account",
        "deployment_ids": ["dep-1"],
        "trades": [],
    }

    async def _next_payload(account_id: str) -> dict[str, object]:
        nonlocal latest_payload
        assert account_id == "test-account"
        try:
            latest_payload = next(snapshots)
        except StopIteration:
            pass
        return latest_payload

    monkeypatch.setattr(viewer_main_module, "_live_trades_payload", _next_payload)
    monkeypatch.setattr(viewer_main_module, "_LIVE_WS_EXECUTIONS_POLL_SECONDS", 0.05)
    monkeypatch.setattr(viewer_main_module, "_LIVE_WS_HEARTBEAT_SECONDS", 5.0)

    client = TestClient(viewer_main_module.app)
    with client.websocket_connect("/ws/live/test-account/executions") as websocket:
        first = websocket.receive_json()
        second = websocket.receive_json()

    assert first["type"] == "snapshot"
    assert first["topic"] == "live.executions.test-account"
    assert first["data"]["trades"][0]["execution_id"] == "exe-1"

    assert second["type"] == "update"
    assert second["topic"] == "live.executions.test-account"
    assert second["data"]["trades"][0]["execution_id"] == "exe-2"


def test_live_accounts_websocket_ping_pong(monkeypatch):
    import viewer.main as viewer_main_module

    monkeypatch.setattr(
        viewer_main_module,
        "_live_accounts_stream_payload",
        lambda: {
            "accounts": [
                {
                    "id": "acc-1",
                    "broker": "alpaca",
                    "name": "Primary",
                    "balance": 10_000.0,
                    "equity": 10_000.0,
                    "is_demo": True,
                }
            ]
        },
    )
    monkeypatch.setattr(viewer_main_module, "_LIVE_WS_ACCOUNTS_POLL_SECONDS", 5.0)
    monkeypatch.setattr(viewer_main_module, "_LIVE_WS_HEARTBEAT_SECONDS", 10.0)

    client = TestClient(viewer_main_module.app)
    with client.websocket_connect("/ws/live/accounts") as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "snapshot"
        websocket.send_text('{"type":"ping"}')
        pong = websocket.receive_json()

    assert pong["type"] == "pong"
    assert pong["topic"] == "live.accounts"


# ─── A read must not disarm an account ─────────────────────────────────────────
#
# GET /api/live/accounts persists what discovery learned. It used to derive
# `is_demo` with `bool(item.get("is_demo", False))`, so a discovery response
# that simply did not mention the flag overwrote a broker-verified True with
# False while leaving `demo_verified_at` fresh. The account was then unfixably
# non-demo: no adapter is ever built for it, so its open positions were never
# flattened and no later read could put the flag back.


def _verified_demo_account(database, account_id: str = "acc-1") -> None:
    database.ensure_live_account(account_id, "alpaca", is_demo=True, broker_login="PA-DEMO-1")
    database.record_live_account_demo_verification(
        account_id, is_demo=True, broker_login="PA-DEMO-1"
    )


def _call_get_live_accounts(monkeypatch, discovered: list[dict]) -> list[dict]:
    import asyncio

    import viewer.main as viewer_main_module

    monkeypatch.setattr(
        viewer_main_module, "_cached_live_accounts_payload", lambda *a, **k: discovered
    )
    return asyncio.run(viewer_main_module.get_live_accounts())


def test_get_live_accounts_does_not_demote_a_verified_demo_account(
    isolated_db, monkeypatch
) -> None:
    """The proven sequence: a GET whose payload omits is_demo changes nothing.

    Not even for one request. A page view is a read; it has no business
    disarming an account that has positions open at the broker.
    """
    database = isolated_db[0]
    _verified_demo_account(database)

    _call_get_live_accounts(monkeypatch, [{"id": "acc-1", "broker": "alpaca", "name": "acc-1"}])

    with database.get_session() as session:
        row = session.get(database.LiveAccountModel, "acc-1")
        assert row.is_demo is True, "a read must not demote a verified demo account"
    assert database.live_account_submission_block_reason("acc-1") is None, (
        "and the account must still be submittable afterwards"
    )


def test_get_live_accounts_keeps_reporting_a_non_boolean_is_demo_as_unasserted(
    isolated_db, monkeypatch
) -> None:
    """A payload that asserts nothing must not become a False.

    `is_demo="yes"` is not a broker confirmation, so it is treated exactly like
    an absent field rather than coerced into a demotion.
    """
    database = isolated_db[0]
    _verified_demo_account(database)

    _call_get_live_accounts(
        monkeypatch,
        [{"id": "acc-1", "broker": "alpaca", "name": "acc-1", "is_demo": "yes"}],
    )

    with database.get_session() as session:
        assert session.get(database.LiveAccountModel, "acc-1").is_demo is True


def test_get_live_accounts_still_applies_an_explicitly_asserted_flag(
    isolated_db, monkeypatch
) -> None:
    """The control: a real broker assertion still reaches the stored row.

    Without this, "ignore the payload" would pass the demotion test while
    quietly breaking every account that the broker reports as non-demo.
    """
    database = isolated_db[0]
    _verified_demo_account(database)

    _call_get_live_accounts(
        monkeypatch,
        [
            {
                "id": "acc-1",
                "broker": "alpaca",
                "name": "acc-1",
                "is_demo": False,
                "is_demo_asserted": True,
            }
        ],
    )

    with database.get_session() as session:
        row = session.get(database.LiveAccountModel, "acc-1")
        assert row.is_demo is False
        # A demotion drops the verification it was based on, so the row never
        # reads as "verified as a non-demo account".
        assert row.demo_verified_at is None
        assert row.identity_fingerprint is None


def test_get_live_accounts_still_creates_unknown_accounts(isolated_db, monkeypatch) -> None:
    """The control for the other direction: new accounts still get a row.

    Persistence exists so an account seen in discovery can be picked as a
    deployment target once verified. A new row is unverified, so it cannot be
    armed until discovery confirms it.
    """
    database = isolated_db[0]

    _call_get_live_accounts(
        monkeypatch,
        [{"id": "acc-new", "broker": "alpaca", "name": "new", "is_demo": True}],
    )

    with database.get_session() as session:
        row = session.get(database.LiveAccountModel, "acc-new")
        assert row is not None
        assert row.demo_verified_at is None
    assert database.live_account_submission_block_reason("acc-new") is not None


def test_discovered_payload_without_is_demo_records_no_verification(
    isolated_db, monkeypatch
) -> None:
    """Discovery that does not report the flag must not stamp a verification.

    Recording `is_demo=False` for a source that never said so would invent a
    "the broker confirms this is live" answer, and stamp a fresh verification
    onto it besides. No assertion means no verification.
    """
    import viewer.main as viewer_main_module

    unasserted_source = [{"id": "acc-1", "broker": "ctrader", "name": "One"}]
    monkeypatch.setattr(
        viewer_main_module, "_run_coroutine_sync", lambda _coro: list(unasserted_source)
    )

    payload = viewer_main_module._discover_live_accounts_payload()

    # Reported to the client as not asserted, not as a False the broker never gave.
    assert payload[0]["is_demo_asserted"] is False
    # And nothing recorded against the account row.
    recorded: list[str] = []
    # The recorder moved to `live_runner.account_discovery` so the daemon and the
    # viewer share one definition rather than two that can drift - which is how
    # the TTL came to be refreshed by only one of them. Patching the module
    # attribute the viewer actually calls, not the name it used to define.
    monkeypatch.setattr(
        viewer_main_module,
        "record_discovered_account_verification",
        lambda account_id, **kwargs: recorded.append(f"{account_id}:{kwargs['is_demo']}"),
    )
    viewer_main_module._discover_live_accounts_payload()
    assert recorded == ["acc-1:None"], "nothing asserted means nothing recorded"


def test_arming_an_internal_source_deployment_is_refused_by_the_api(
    viewer_client, monkeypatch, isolated_db_sandbox
):
    """End-to-end, through the real endpoint.

    The database gate already refuses this, but a refusal the operator sees as a
    bare 400 after a pointless broker round-trip is not a control they can act
    on. This asserts the API names the real reason, and that the refusal is
    audited like every other refused live control.

    Also asserts the broker is never contacted: no account verification can make
    a deployment without an evaluator capable of trading, so spending a
    round-trip on it would only hide the actual cause.
    """
    database_module, _engine, _data_dir = isolated_db_sandbox
    database_module.ensure_live_account("acc-int", "ctrader", is_demo=True)
    database_module.record_live_account_demo_verification(
        "acc-int", is_demo=True, ctid="46736452"
    )

    import viewer.main as viewer_main_module

    broker_calls: list[str] = []

    async def _must_not_reverify(account_id: str) -> bool:
        broker_calls.append(account_id)
        return True

    monkeypatch.setattr(
        viewer_main_module, "_reverify_account_against_broker", _must_not_reverify
    )

    import uuid as _uuid

    deployment_id = str(_uuid.uuid4())
    created = database_module.create_live_deployment(
        database_module.LiveDeploymentConfig(
            id=_uuid.UUID(deployment_id),
            strategy_name="trend_following/trend_following.py",
            account_id="acc-int",
            symbols=["EURUSD"],
            timeframe="M15",
            status="PAUSED",
            signal_source_type="internal",
        )
    )
    assert created.status == "PAUSED"

    client = TestClient(viewer_main_module.app)
    response = client.put(
        f"/api/live/deployments/{deployment_id}/status",
        # Confirmed, so the 409 below is the internal-source refusal and not the
        # confirmation precondition.
        json={
            "status": "ACTIVE",
            "confirmation": {"confirmed_deployment_id": deployment_id},
        },
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "internal" in detail
    assert "no live evaluator" in detail
    # The broker was never contacted: the refusal is not a broker problem.
    assert broker_calls == []
    # And the row is untouched.
    assert database_module.get_live_deployment(deployment_id).status == "PAUSED"

    # The refusal is on the audit record, so "who tried to arm this" is
    # answerable after the fact.
    from shared.database import list_live_control_audit

    entries = [
        entry
        for entry in list_live_control_audit()
        if entry.get("target_id") == deployment_id and not entry.get("succeeded")
    ]
    assert entries, "the refused arming must be audited"
    assert "internal" in (entries[0].get("detail") or {}).get("reason", "")


def test_a_submission_claim_expires_while_the_first_submit_is_still_running(
    isolated_db_sandbox,
):
    """A 30s TTL is shorter than a slow broker submit, and expiry deletes the row.

    `claim_live_deployment_submission` deletes claims whose `expires_at` has
    passed before checking for a conflict, so a second dispatcher gets a clean
    claim while the first is still inside its submission. The `finally` that
    releases the claim does not help, because the two orders are placed before
    either returns.

    Nothing enforces the relationship in the other direction: the claimed region
    in `daemon.py` spans three or four sequential broker round trips, so it is
    not bounded by anything but luck. This is not a claim that the current TTL is
    too short - it may be right for a market order - it is a pin on the shape, so
    that shortening the TTL or adding a reaper grace period cannot quietly turn
    into a double-order.
    """
    database_module, _engine, _data_dir = isolated_db_sandbox
    database_module.ensure_live_account("acc-ttl", "ctrader", is_demo=True)
    database_module.record_live_account_demo_verification(
        "acc-ttl", is_demo=True, ctid="46736452"
    )
    # The claim reads the live deployment row, so it must exist and be ACTIVE.
    # A nonexistent id would be refused for an unrelated reason and the test
    # would pass for the wrong one.
    import uuid as _uuid

    deployment_id = str(_uuid.uuid4())
    created = database_module.create_live_deployment(
        database_module.LiveDeploymentConfig(
            id=_uuid.UUID(deployment_id),
            strategy_name="trend_following/trend_following.py",
            account_id="acc-ttl",
            symbols=["EURUSD"],
            timeframe="M15",
            status="ACTIVE",
            signal_source_type="discord",
        )
    )
    assert created.status == "ACTIVE", (
        f"precondition failed: the claim is refused unless the deployment is "
        f"ACTIVE, and this one is {created.status}"
    )

    # The TTL is set by the *first* claim. A later caller's ttl_seconds only sizes
    # the lease it would take, so it cannot shorten the wait - which is why the
    # first claim here uses a short TTL and the second a long one.
    first = database_module.claim_live_deployment_submission(
        deployment_id, "token-first", ttl_seconds=0.3
    )
    assert first is True, "the first claim should succeed"

    # Still inside the TTL: a second dispatcher must be refused.
    assert (
        database_module.claim_live_deployment_submission(
            deployment_id, "token-second", ttl_seconds=30.0
        )
        is False
    ), "a live claim was not honoured, so the atomicity guarantee is already gone"

    # Past the TTL the row is deleted, so the second claim succeeds. That is what
    # makes the TTL load-bearing: whatever it is set to is the real upper bound on
    # a submission, and nothing else in the code enforces one.
    import time

    time.sleep(0.5)
    assert database_module.claim_live_deployment_submission(
        deployment_id, "token-second", ttl_seconds=30.0
    ) is True, (
        "an expired claim is not reclaimable, so a dispatcher that died "
        "mid-submission would block the deployment forever"
    )


def test_a_paused_service_cannot_be_moved_by_the_process(isolated_db_sandbox):
    """PAUSED is a floor, enforced where no caller can bypass it.

    There were six unguarded `update_service_status` call sites in the daemon and
    one guarded copy in the signal source. A rule enforced by eight call sites is
    not a rule: it had been applied to one of them, and the rest kept the hole.

    The failure was invisible. A FAILED write landed on a PAUSED row, and
    `_recover_failed_services` then reads FAILED rather than PAUSED and resets the
    service to RUNNING on the next daemon start - so the process undid a decision
    the operator had just made, silently, across a restart.
    """
    database_module, _engine, _data_dir = isolated_db_sandbox
    service = database_module.create_service(
        database_module.Service(
            name="TEST_BOT",
            type=database_module.ServiceType.DISCORD_BOT,
            status=database_module.ServiceStatus.RUNNING,
            config={},
        )
    )

    paused = database_module.update_service_status(
        str(service.id), database_module.ServiceStatus.PAUSED
    )
    assert paused.status == database_module.ServiceStatus.PAUSED

    # Nothing the process observes afterwards may move it off PAUSED.
    for status in (
        database_module.ServiceStatus.RUNNING,
        database_module.ServiceStatus.FAILED,
        database_module.ServiceStatus.STOPPED,
    ):
        after = database_module.update_service_status(str(service.id), status)
        assert after.status == database_module.ServiceStatus.PAUSED, (
            f"the process moved a PAUSED service to {status}; an operator pause "
            "must survive anything the process observes"
        )

    # And the recovery path must therefore never pick it up.
    assert not any(
        item.id == service.id
        for item in database_module.list_services(
            service_type=database_module.ServiceType.DISCORD_BOT
        )
        if item.status == database_module.ServiceStatus.FAILED
    ), "a PAUSED service became FAILED, so startup recovery would resurrect it"


def test_the_api_is_the_one_caller_allowed_to_clear_a_pause(isolated_db_sandbox):
    """`respect_operator_intent=False` exists so a person can clear their pause.

    Without it the guard would be unfalsifiable in the other direction: an
    operator could never resume a service they had paused. The endpoint opts out
    explicitly, so the exemption is visible at the call site rather than implied.
    """
    database_module, _engine, _data_dir = isolated_db_sandbox
    service = database_module.create_service(
        database_module.Service(
            name="TEST_BOT",
            type=database_module.ServiceType.DISCORD_BOT,
            status=database_module.ServiceStatus.RUNNING,
            config={},
        )
    )
    database_module.update_service_status(
        str(service.id), database_module.ServiceStatus.PAUSED
    )

    resumed = database_module.update_service_status(
        str(service.id),
        database_module.ServiceStatus.RUNNING,
        respect_operator_intent=False,
    )

    assert resumed.status == database_module.ServiceStatus.RUNNING, (
        "an operator cannot resume a service they paused, so the guard is a "
        "trap rather than a safety"
    )


def test_every_stockkit_route_call_site_resolves_configured_accounts():
    """Four call sites, one rule, and the arguments were not shared.

    `a182e50` moved expected-account resolution out of a hardcoded table into
    configuration and wired it into three of four callers. The viewer API - the
    path an operator walks to create a Discord deployment - kept resolving
    `STOCKKIT_ACCOUNT_ROUTES`, the table the same commit describes as matching
    none of the configured paper accounts. A correctly routed deployment was
    refused, and the refusal named a broker account the deployment was not on.

    Asserted by inspecting the source rather than by driving the endpoint: the
    defect is an *argument* that one call site omitted, and an HTTP-level test
    has to satisfy every other precondition of that endpoint before it reaches
    the line in question. That is the "unreachable code" trap again, one level up
    from the fixtures - the assertion has to be about the thing that is wrong.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if {"tests", ".venv", "node_modules"} & set(path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "validate_stockkit_deployment_route(" not in text:
            continue
        if "def validate_stockkit_deployment_route" in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", "") != "validate_stockkit_deployment_route":
                continue
            names = {kw.arg for kw in node.keywords}
            if "accounts_env_values" not in names:
                offenders.append(
                    f"{path.relative_to(root)}:{node.lineno} "
                    "(validated routing without the configured accounts)"
                )

    assert not offenders, (
        "call sites that resolve the stale static account table instead of the "
        "configured one:\n  " + "\n  ".join(offenders)
    )


def test_a_confirmation_naming_a_different_deployment_is_refused(
    isolated_db_sandbox,
):
    """The echo check stops an operator confirming the wrong row.

    Every existing test passes the *correct* deployment id, so the mismatch branch
    was never reached - confirmed by mutation: replacing the echoed value with the
    requested one left all 8 confirmation tests green. The guard exists so that
    arming or stopping a deployment cannot be aimed at the wrong one by a stale
    tab, a mis-read id, or a UI that dropped the field.

    The refusal is a 409, is audited like every other refused live control, and
    leaves the row untouched.
    """
    database_module, _engine, _data_dir = isolated_db_sandbox
    database_module.ensure_live_account("acc-echo", "alpaca", is_demo=True)
    database_module.record_live_account_demo_verification(
        "acc-echo", is_demo=True, ctid="PA_ECHO"
    )

    import uuid as _uuid

    from fastapi.testclient import TestClient

    import viewer.main as viewer_main_module

    ids = []
    for _ in range(2):
        did = str(_uuid.uuid4())
        database_module.create_live_deployment(
            database_module.LiveDeploymentConfig(
                id=_uuid.UUID(did),
                strategy_name="trend_following/trend_following.py",
                account_id="acc-echo",
                symbols=["EURUSD"],
                timeframe="M15",
                status="PAUSED",
                signal_source_type="internal",
            )
        )
        ids.append(did)

    client = TestClient(viewer_main_module.app)
    response = client.put(
        f"/api/live/deployments/{ids[0]}/status",
        json={
            "status": "ACTIVE",
            # Names the OTHER deployment: exactly the stale-tab mistake.
            "confirmation": {"confirmed_deployment_id": ids[1]},
        },
    )

    assert response.status_code == 409, (
        "a confirmation naming a different deployment was accepted, so an "
        "operator can arm the wrong row from a stale tab"
    )
    detail = response.json()["detail"]
    assert "does not match" in detail or "different deployment" in detail
    assert database_module.get_live_deployment(ids[0]).status == "PAUSED", (
        "the deployment was armed despite a mismatched confirmation"
    )


def test_an_absent_confirmation_is_refused(isolated_db_sandbox):
    """Omitting the field entirely must not arm anything.

    A missing `confirmed_deployment_id` stringifies to "", which is not the
    deployment id, so the same branch refuses it. Worth pinning because "" is the
    value that makes an `if not confirmed` shortcut look reasonable, and that
    shortcut would be a different behaviour from this one.
    """
    database_module, _engine, _data_dir = isolated_db_sandbox
    database_module.ensure_live_account("acc-noecho", "alpaca", is_demo=True)
    database_module.record_live_account_demo_verification(
        "acc-noecho", is_demo=True, ctid="PA_NOECHO"
    )

    import uuid as _uuid

    from fastapi.testclient import TestClient

    import viewer.main as viewer_main_module

    did = str(_uuid.uuid4())
    database_module.create_live_deployment(
        database_module.LiveDeploymentConfig(
            id=_uuid.UUID(did),
            strategy_name="trend_following/trend_following.py",
            account_id="acc-noecho",
            symbols=["EURUSD"],
            timeframe="M15",
            status="PAUSED",
            signal_source_type="internal",
        )
    )

    client = TestClient(viewer_main_module.app)
    response = client.put(
        f"/api/live/deployments/{did}/status",
        json={"status": "ACTIVE", "confirmation": {}},
    )

    assert response.status_code in (409, 422), (
        f"a missing confirmation was accepted (status {response.status_code})"
    )
    assert database_module.get_live_deployment(did).status == "PAUSED"


def test_a_failed_service_records_that_it_was_actually_tried(isolated_db_sandbox):
    """`last_activity` only advances on success, so it cannot answer this.

    Found by restarting the daemon rather than reasoning about it: all three
    Discord bots were re-discovered, restarted and re-authenticated, and
    `last_activity` did not move by a second while `updated_at` did. A service
    failing for days therefore shows the last time it *worked*, and reads as "the
    process is not trying" when it is.

    Two questions, two columns: `last_activity` is when this last worked,
    `last_attempt_at` is when anyone last tried.
    """
    database_module, _engine, _data_dir = isolated_db_sandbox
    service = database_module.create_service(
        database_module.Service(
            name="TEST_BOT",
            type=database_module.ServiceType.DISCORD_BOT,
            status=database_module.ServiceStatus.RUNNING,
            config={},
        )
    )
    assert service.last_attempt_at is None

    database_module.update_service_status(
        str(service.id), database_module.ServiceStatus.FAILED
    )
    after = database_module.get_service(str(service.id))

    assert after.status == database_module.ServiceStatus.FAILED
    assert after.last_attempt_at is not None, (
        "a failed attempt left no record that it was tried, so the service is "
        "indistinguishable from one nobody is attempting"
    )
    # And last_activity is deliberately untouched: it means "last success".
    assert after.last_activity is None


def test_a_legacy_services_table_gains_the_column(isolated_db_sandbox):
    """The migration has to reach an existing install, not just a fresh schema.

    Every operator running this has a database created before the column existed,
    and a model field with no ALTER leaves the column absent in production while
    the tests - which build from the model - all pass.
    """
    import sqlite3 as _sqlite3
    from pathlib import Path

    database_module, engine, _data_dir = isolated_db_sandbox
    db_path = Path(str(engine.url).replace("sqlite:///", ""))

    # Rebuild `services` in the pre-column shape, then re-run the migration.
    with _sqlite3.connect(str(db_path)) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info('services')")}
        assert "last_attempt_at" in cols, "precondition: the fresh schema has it"
        con.execute("ALTER TABLE services RENAME TO services_new")
        con.execute(
            "CREATE TABLE services (id TEXT PRIMARY KEY, name TEXT, type TEXT,"
            " status TEXT, config TEXT, last_activity DATETIME,"
            " created_at DATETIME, updated_at DATETIME)"
        )
        con.execute("DROP TABLE services_new")

    database_module._migrate_schema()

    with _sqlite3.connect(str(db_path)) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info('services')")}
    assert "last_attempt_at" in cols, (
        "the migration did not add the column to a legacy services table, so it "
        "would be missing in every existing install"
    )


def test_the_migrations_apply_to_a_population_shaped_like_production(isolated_db_sandbox):
    """A fresh schema proves nothing. Production is a populated, pre-column table.

    Both columns this branch adds were only ever exercised against a database
    built from the current model, which is the shape a *new* install has. Every
    operator running this has a populated table without them, and the failure that
    matters is silent: the column is missing, reads return `None`, and the
    enforcement ledger is empty on every restart - the defect the column was added
    to fix, reappearing.

    So this rebuilds both tables in their pre-column shape with rows in them, and
    asserts the migration brings them up and the rows survive.
    """
    import sqlite3 as _sqlite3
    from pathlib import Path

    database_module, engine, _data_dir = isolated_db_sandbox
    db_path = Path(str(engine.url).replace("sqlite:///", ""))

    database_module.ensure_live_account("acc-mig", "alpaca", is_demo=True)
    import uuid as _uuid

    did = str(_uuid.uuid4())
    database_module.create_live_deployment(
        database_module.LiveDeploymentConfig(
            id=_uuid.UUID(did),
            strategy_name="trend_following/trend_following.py",
            account_id="acc-mig",
            symbols=["EURUSD"],
            timeframe="M15",
            status="STOPPED",
            signal_source_type="discord",
        )
    )

    # The pre-column shape is derived from the models minus the columns this
    # branch adds, rather than hand-written. The hand-written version used
    # `symbols_json`, `symbol_filter_json` and `signal_config_json`, and the
    # production table has `symbols`, `symbol_filter` and `signal_config` - so
    # the fixture described a schema that has never existed, while claiming to
    # be production-shaped. Deriving it keeps the fixture honest and keeps it
    # correct when the next column is added.
    from sqlmodel import SQLModel

    added_by_this_branch = {"last_attempt_at", "stopped_enforced_at"}

    with _sqlite3.connect(str(db_path)) as con:
        for table in ("services", "live_deployments"):
            declared = [
                c.name
                for c in SQLModel.metadata.tables[table].columns
                if c.name not in added_by_this_branch
            ]
            present = [r[1] for r in con.execute(f"PRAGMA table_info('{table}')")]
            keep = [c for c in declared if c in present]
            assert keep, f"{table} fixture would have no columns to carry over"
            con.execute(f"ALTER TABLE {table} RENAME TO {table}_pre_column")
            cols = ", ".join(
                f"{c} TEXT PRIMARY KEY" if c == "id" else f"{c} TEXT" for c in declared
            )
            con.execute(f"CREATE TABLE {table} ({cols})")
            # Copy the rows across. The previous version dropped the populated
            # table and started empty, so the rows it claimed to protect were
            # gone before the migration ran and nothing could have detected it.
            con.execute(
                f"INSERT INTO {table} ({', '.join(keep)}) "
                f"SELECT {', '.join(keep)} FROM {table}_pre_column"
            )
            con.execute(f"DROP TABLE {table}_pre_column")

    database_module._migrate_schema()

    with _sqlite3.connect(str(db_path)) as con:
        svc = {r[1] for r in con.execute("PRAGMA table_info('services')")}
        dep = {r[1] for r in con.execute("PRAGMA table_info('live_deployments')")}
    assert "last_attempt_at" in svc, (
        "services.last_attempt_at missing after migration: a failed attempt would "
        "again leave no trace that anything tried"
    )
    assert "stopped_enforced_at" in dep, (
        "live_deployments.stopped_enforced_at missing after migration: the "
        "enforcement ledger would be empty on every restart"
    )

    # And the general form of the same check, so this test does not become a
    # monument to the two columns someone remembered. Asserting a hand-listed
    # pair certifies the schema only as far as the list goes: add a third column,
    # forget its migration, and this test still passes while production reads
    # None from it - the exact silent failure it exists to prevent.
    with _sqlite3.connect(str(db_path)) as con:
        absent: dict[str, list[str]] = {}
        for table in SQLModel.metadata.sorted_tables:
            actual = {r[1] for r in con.execute(f"PRAGMA table_info('{table.name}')")}
            if not actual:
                absent[table.name] = sorted(c.name for c in table.columns)
                continue
            for col in table.columns:
                if col.name not in actual:
                    absent.setdefault(table.name, []).append(col.name)
    assert not absent, (
        "declared columns absent from the migrated schema, so their migration "
        f"is missing or incomplete: {absent}"
    )

    # The half the docstring promised and never checked: a migration that
    # rebuilds a table must not take the operator's rows with it. These rows
    # carry the PAUSED/STOPPED state that stops a deployment trading, so losing
    # them is not a cosmetic failure - it is the migration silently re-arming
    # something the operator deliberately turned off.
    with _sqlite3.connect(str(db_path)) as con:
        survived = con.execute(
            "SELECT id, status FROM live_deployments WHERE id = ?", (did,)
        ).fetchone()
        total = con.execute("SELECT count(*) FROM live_deployments").fetchone()[0]
    assert survived is not None, (
        "the deployment row did not survive the migration; a migration that "
        "rebuilds a table and drops its rows would silently re-arm whatever "
        "the operator had PAUSED"
    )
    assert survived[1] == "STOPPED", (
        f"the row survived but its status changed to {survived[1]!r}; the "
        "operator's stop state must not be rewritten by a migration"
    )
    assert total == 1, f"expected exactly the one seeded deployment, found {total}"


def test_a_refused_status_change_is_still_recorded_as_an_attempt(isolated_db_sandbox):
    """Refusing to un-pause is an attempt, and the attempt has to survive.

    `last_attempt_at` was added to answer "when did anything last try to change
    this service?", which is how an operator discovers something repeatedly
    fighting a pause they set. But the refusal path returned from inside
    `get_session` without committing, so the pending write was rolled back: the
    column was empty for exactly the attempts worth noticing, while the returned
    object still carried the timestamp - so a caller serialising the return value
    would report a time that is not in the database.

    Asserted against a re-read from the database, not against the return value.
    The return value passing is what let this look correct in the first place.
    """
    database_module, _engine, _data_dir = isolated_db_sandbox
    service = database_module.create_service(
        database_module.Service(
            name="TEST_BOT",
            type=database_module.ServiceType.DISCORD_BOT,
            status=database_module.ServiceStatus.RUNNING,
            config={},
        )
    )
    database_module.update_service_status(
        str(service.id), database_module.ServiceStatus.PAUSED
    )

    def _persisted_attempt() -> object:
        # A second, independent session, because the one under test still holds
        # the write in its own open transaction - a read there sees the
        # uncommitted row and cannot see this bug at all.
        with database_module.get_session() as verify:
            return verify.execute(
                database_module.text(
                    "SELECT last_attempt_at FROM services WHERE id = :sid"
                ),
                {"sid": str(service.id)},
            ).scalar_one_or_none()

    after_pause = _persisted_attempt()
    assert after_pause is not None

    refused = database_module.update_service_status(
        str(service.id), database_module.ServiceStatus.RUNNING
    )

    assert refused.status == database_module.ServiceStatus.PAUSED, (
        "the pause was cleared; the guard is the safety and must not weaken"
    )

    # The value must ADVANCE, not merely exist. Asserting "is not None" passed
    # with the fix reverted, because the earlier pause had already set the
    # column - the test was satisfied by the wrong write. Verified by mutation.
    after_refusal = _persisted_attempt()
    assert after_refusal is not None and after_refusal > after_pause, (
        f"the refused attempt was rolled back: last_attempt_at stayed at "
        f"{after_pause} instead of advancing, so the column is empty for exactly "
        f"the attempts an operator needs to see"
    )


def test_a_live_control_audit_row_records_who_asked(monkeypatch):
    """`X-Operator` has to reach the audit row, not just the helper.

    `_live_control_actor` reads the header and has always been tested - with a
    fake request object, directly. But all three live-control endpoints passed
    `request=None` and none of them declared a `Request` parameter, so the header
    could never arrive: every row in `live_control_audit` said
    `actor = "local-operator"`, including the refused attempts, which are the
    more interesting half. The audit trail was an action log with no attribution,
    which is the one thing it exists to provide.

    Driven through the real HTTP route, because the gap was in the wiring between
    the endpoint and the helper, and a helper-only test cannot see wiring.
    """
    import viewer.main as viewer_main_module

    recorded: list[dict] = []

    async def _capture_audit(
        *,
        action: str,
        target_type: str,
        target_id: str,
        request=None,
        succeeded: bool = True,
        detail: dict | None = None,
    ) -> None:
        # Mirrors the real signature exactly. A first attempt guessed
        # `deployment_id` positionally and missed `target_type`/`target_id`, so
        # the stub raised TypeError instead of recording anything - a test that
        # fails on its own harness, not on the wiring it exists to check.
        recorded.append(
            {
                "action": action,
                "target_id": target_id,
                "succeeded": succeeded,
                "actor": viewer_main_module._live_control_actor(request),
            }
        )

    monkeypatch.setattr(viewer_main_module, "_audit_live_control", _capture_audit)
    monkeypatch.setattr(
        viewer_main_module,
        "_ensure_live_account_for_deployment",
        lambda _account_id: {
            "id": _account_id,
            "broker": "alpaca",
            "name": "Alpaca Paper",
            "broker_login": "PAPER",
        },
    )
    # A real UUID: LiveDeploymentConfig validates the id, and a non-UUID 400s
    # before the audit path is reached, which is a confusing way to fail a test
    # about attribution.
    deployment_id = "77777777-7777-7777-7777-777777777777"
    monkeypatch.setattr(
        viewer_main_module,
        "update_live_deployment_status",
        lambda _deployment_id, _status: _deployment_config(_deployment_id, "PAUSED"),
    )

    client = TestClient(viewer_main_module.app)
    resp = client.put(
        f"/api/live/deployments/{deployment_id}/status",
        headers={"X-Operator": "fred-from-the-console"},
        json={
            "status": "PAUSED",
            "confirmation": {"confirmed_deployment_id": deployment_id},
        },
    )
    assert resp.status_code == 200, resp.text

    assert recorded, "the audit helper was never called, so this proves nothing"
    assert recorded[0]["actor"] == "fred-from-the-console", (
        f"the declared operator was dropped: {recorded[0]['actor']!r}. The header "
        "never reached the helper, so every audit row claimed 'local-operator'."
    )

    # And the default still holds when nobody declares themselves.
    recorded.clear()
    client.put(
        f"/api/live/deployments/{deployment_id}/status",
        json={
            "status": "PAUSED",
            "confirmation": {"confirmed_deployment_id": deployment_id},
        },
    )
    assert recorded[0]["actor"] == "local-operator", (
        "an undeclared operator must stay 'local-operator', not become empty"
    )


def _deployment_config(deployment_id: str, status: str):
    return LiveDeploymentConfig(
        id=deployment_id,
        strategy_name="s",
        account_id="acc",
        symbols=["AAPL"],
        timeframe="1h",
        status=status,
        signal_source_type="discord",
        signal_source_id="RAMJET",
    )


def test_the_daemon_refreshes_its_own_demo_verification(
    isolated_db_sandbox, monkeypatch
):
    """The process that enforces the TTL has to be the one that refreshes it.

    `DEMO_VERIFICATION_TTL_SECONDS` is checked by the daemon before every broker
    submission, but the only writer of the stamp was the viewer's account
    discovery endpoint - a different systemd unit. Nothing about running headless
    should change whether the process that places orders is allowed to place them,
    so a browser tab being open was an invisible precondition for live trading.

    Asserted through the refresher the daemon's loop calls, with the broker
    conversation stubbed: the daemon must be able to renew the stamp on its own,
    and the renewal must be the thing the TTL gate reads.
    """
    import asyncio

    from live_runner.account_discovery import refresh_account_demo_verifications

    database_module_module, _engine, _data_dir = isolated_db_sandbox
    database_module_module.upsert_live_accounts(
        [
            database_module_module.LiveAccountConfig(
                id="acct-ttl",
                broker="alpaca",
                name="Alpaca Paper",
                broker_login="PAPER-1",
                is_demo=True,
            )
        ]
    )
    account_id = "acct-ttl"

    # Stale on arrival: no broker has been asked yet.
    assert database_module_module.live_account_submission_block_reason(account_id) == (
        "account has no broker demo verification on record"
    )
    assert database_module_module.live_account_verification_needs_refresh(account_id)

    async def _fake_discovery():
        return [
            {
                "id": account_id,
                "broker": "alpaca",
                "name": "Alpaca Paper",
                "is_demo": True,
                "broker_login": "PAPER-1",
                "ctid": "ctid-ttl",
            }
        ]

    import live_runner.account_discovery as discovery_module

    monkeypatch.setattr(
        discovery_module, "discover_configured_accounts", _fake_discovery
    )

    written = asyncio.run(refresh_account_demo_verifications())

    assert written == 1, f"the refresh wrote {written} verifications, expected 1"
    assert not database_module_module.live_account_verification_needs_refresh(
        account_id
    ), "the stamp is still stale after a successful broker round-trip"
    assert (
        database_module_module.live_account_submission_block_reason(account_id) is None
    ), "the account is still refused for a reason unrelated to freshness"
    assert database_module_module.live_account_submission_block_reason(account_id) is None


def test_a_broker_that_stops_reporting_demo_leaves_the_account_refused(
    isolated_db_sandbox, monkeypatch
):
    """Fail-closed on the refresh path too, not just on the gate.

    A refresh that discovers the account with no `is_demo` flag must not clear a
    stamp that has expired - the absence of an answer is not an answer. If the
    refresher wrote `False`, or stamped freshness from nothing, a broker outage
    would look like a passing check.
    """
    import asyncio

    from live_runner.account_discovery import refresh_account_demo_verifications

    database_module_module, _engine, _data_dir = isolated_db_sandbox
    database_module_module.upsert_live_accounts([
        database_module_module.LiveAccountConfig(
            id="acct-quiet",
            broker="alpaca",
            name="Alpaca Paper",
            broker_login="PAPER-2",
            is_demo=True,
        )
    ])
    account_id = "acct-quiet"

    async def _silent_discovery():
        # No is_demo key at all: the source did not assert anything.
        return [
            {
                "id": account_id,
                "broker": "alpaca",
                "name": "Alpaca Paper",
                "broker_login": "PAPER-2",
            }
        ]

    import live_runner.account_discovery as discovery_module

    monkeypatch.setattr(
        discovery_module, "discover_configured_accounts", _silent_discovery
    )

    written = asyncio.run(refresh_account_demo_verifications())

    assert written == 0, (
        "a discovery that reported no is_demo flag recorded a verification; an "
        "absent answer must not become a fresh one"
    )
    assert database_module_module.live_account_submission_block_reason(account_id) == (
        "account has no broker demo verification on record"
    )


def test_a_refresh_failure_leaves_the_account_as_it_was(isolated_db_sandbox, monkeypatch):
    """A broker error must not raise out of the daemon's loop, nor clear a stamp.

    The refresher runs inside the daemon's main `while True`. Letting an exception
    escape would kill the loop that places and flattens orders, and clearing the
    stamp on failure would trade a stale-but-true record for a missing one.
    """
    import asyncio

    from live_runner.account_discovery import refresh_account_demo_verifications

    database_module_module, _engine, _data_dir = isolated_db_sandbox
    database_module_module.upsert_live_accounts([
        database_module_module.LiveAccountConfig(
            id="acct-boom",
            broker="alpaca",
            name="Alpaca Paper",
            broker_login="PAPER-3",
            is_demo=True,
        )
    ])
    database_module_module.record_live_account_demo_verification(
        "acct-boom",
        is_demo=True,
        ctid="ctid-boom",
        broker_login="PAPER-3",
    )

    async def _explode():
        raise RuntimeError("broker unreachable")

    import live_runner.account_discovery as discovery_module

    monkeypatch.setattr(discovery_module, "discover_configured_accounts", _explode)

    written = asyncio.run(refresh_account_demo_verifications())

    assert written == 0
    # The earlier verification stands - it is simply approaching its TTL.
    assert not database_module_module.live_account_verification_needs_refresh(
        "acct-boom"
    ), "a failed refresh discarded a still-valid verification"


def test_the_daemon_loop_refreshes_stale_verifications_and_skips_fresh_ones(
    isolated_db_sandbox, monkeypatch
):
    """The decision is pinned, not just the refresher.

    The first version of this fix tested `refresh_account_demo_verifications` and
    nothing caught deleting its call from the loop - the whole suite stayed green,
    which is precisely the "correct guard, nothing calls it" shape the change
    exists to fix. Repeating that mistake inside the fix would have been worse
    than the original bug, so the *decision* is exercised here: no stale account
    means no broker conversation, and a stale one means exactly one.

    Then the wiring itself: the loop body has to call this helper. A source check,
    because running the daemon's main loop is not a test.
    """
    import asyncio

    from live_runner import daemon as daemon_module

    database_module_module, _engine, _data_dir = isolated_db_sandbox
    database_module_module.upsert_live_accounts(
        [
            database_module_module.LiveAccountConfig(
                id="acct-wire",
                broker="alpaca",
                name="Alpaca Paper",
                broker_login="PAPER-9",
                is_demo=True,
            )
        ]
    )

    calls: list[int] = []

    async def _counting_refresh() -> int:
        calls.append(1)
        return 1

    monkeypatch.setattr(
        daemon_module, "refresh_account_demo_verifications", _counting_refresh
    )

    from types import SimpleNamespace

    stale = [SimpleNamespace(id="acct-wire")]
    written = asyncio.run(daemon_module._refresh_stale_account_verifications(stale))
    assert written == 1, "a stale account did not trigger a refresh"
    assert len(calls) == 1

    # Verified and fresh: nothing to do, so no broker round-trip this tick.
    database_module_module.record_live_account_demo_verification(
        "acct-wire", is_demo=True, ctid="ctid-9", broker_login="PAPER-9"
    )
    written = asyncio.run(daemon_module._refresh_stale_account_verifications(stale))
    assert written == 0, "a freshly verified account triggered a needless refresh"
    assert len(calls) == 1, "the broker was asked again with nothing stale to fix"

    # And the loop actually calls it. A test of the helper alone would pass with
    # the call site deleted, which is the bug this whole change is about.
    from pathlib import Path

    source = Path(daemon_module.__file__).read_text(encoding="utf-8")
    assert "_refresh_stale_account_verifications(accounts)" in source, (
        "the daemon's main loop no longer calls the refresher; every dispatch "
        "would be refused once the TTL expires, with nothing in the loop to renew it"
    )


def test_re_pointing_a_deployment_requires_a_verified_account_not_just_a_demo_flag(
    isolated_db_sandbox, monkeypatch
):
    """The docstring promised a *verified* demo account; the check read a flag.

    `update_live_deployment_account` is one of three ways to put a deployment in
    front of a broker account, and the only one that did not run the full
    activation verdict. It checked `bool(account.is_demo)` and nothing else, so
    both halves of `_activation_block_reason` were skipped: the freshness window
    and the identity fingerprint. An operator could re-point - including an
    ACTIVE deployment with `allow_active` - at an account whose verification had
    expired hours ago, or whose credentials had been rotated since.

    The daemon's per-dispatch gate still refuses the resulting orders, so nothing
    reaches a non-demo broker. The real cost is quieter: a silently mis-armed
    deployment, and a `succeeded=True` audit row for a state the documented gate
    should have refused.

    Two arms, because the two halves fail differently: an expired stamp is caught
    by age, a rotated credential by the fingerprint, and a check that only ever
    read the flag caught neither.
    """
    database_module_module, _engine, _data_dir = isolated_db_sandbox
    database_module_module.upsert_live_accounts(
        [
            database_module_module.LiveAccountConfig(
                id="acct-fresh",
                broker="alpaca",
                name="Alpaca Paper",
                broker_login="PAPER-A",
                is_demo=True,
            ),
            database_module_module.LiveAccountConfig(
                id="acct-stale",
                broker="alpaca",
                name="Alpaca Paper",
                broker_login="PAPER-B",
                is_demo=True,
            ),
            database_module_module.LiveAccountConfig(
                id="acct-rotated",
                broker="alpaca",
                name="Alpaca Paper",
                broker_login="PAPER-C",
                is_demo=True,
            ),
        ]
    )
    for account_id, login in (
        ("acct-fresh", "PAPER-A"),
        ("acct-stale", "PAPER-B"),
        ("acct-rotated", "PAPER-C"),
    ):
        database_module_module.record_live_account_demo_verification(
            account_id, is_demo=True, ctid=f"ctid-{account_id}", broker_login=login
        )

    created = database_module_module.create_live_deployment(
        database_module_module.LiveDeploymentConfig(
            id="11111111-2222-3333-4444-555555555555",
            strategy_name="s",
            account_id="acct-fresh",
            symbols=["AAPL"],
            timeframe="1h",
            status="PAUSED",
            signal_source_type="discord",
            signal_source_id="RAMJET",
        )
    )
    deployment_id = str(created.id)

    # The happy path still works, so this is a gate and not a lockout.
    ok = database_module_module.update_live_deployment_account(
        deployment_id, "acct-fresh"
    )
    assert ok is not None and ok.account_id == "acct-fresh"

    # Arm 1: the verification aged out. Only the is_demo flag is still true.
    with database_module_module.get_session() as session:
        record = session.get(database_module_module.LiveAccountModel, "acct-stale")
        record.demo_verified_at = record.demo_verified_at.replace(year=2020)
        session.add(record)
        session.commit()
    assert database_module_module.live_account_submission_block_reason("acct-stale")
    with pytest.raises(ValueError, match="older than"):
        database_module_module.update_live_deployment_account(
            deployment_id, "acct-stale"
        )

    # Arm 2: the credentials were rotated, so the fingerprint no longer matches
    # even though the stamp is fresh and the flag is set.
    with database_module_module.get_session() as session:
        record = session.get(database_module_module.LiveAccountModel, "acct-rotated")
        record.broker_login = "PAPER-ROTATED"
        session.add(record)
        session.commit()
    reason = database_module_module.live_account_submission_block_reason(
        "acct-rotated"
    )
    assert reason, "the fixture must actually be a mismatch, or this arm is vacuous"
    with pytest.raises(ValueError):
        database_module_module.update_live_deployment_account(
            deployment_id, "acct-rotated"
        )


def test_the_refresher_understands_what_discovery_actually_returns(
    isolated_db_sandbox, monkeypatch
):
    """The refresher must consume the real return type, not a convenient one.

    `discover_configured_accounts` returns `list[LiveAccount]` - pydantic models.
    The first version of the refresher copied the viewer's `isinstance(account,
    dict)` guard, which is correct there because *the viewer's* payload really is
    dicts, and wrong here: it rejected every real account, so the function
    returned 0 forever and the daemon never renewed the stamp. The TTL it was
    written to refresh expired 900s after the last viewer-side discovery and
    every dispatch was refused again - the exact failure it was committed to fix,
    still in place.

    **The tests missed it because they stubbed dicts** - the shape the real
    function never returns. So this test builds the objects the real function
    returns, from the real class, and the guard asserts the refresher
    understood them.

    The dict branch is still covered, deliberately: it is now a fallback rather
    than the only path, and the skip counter plus the log guard exist so a future
    shape change cannot go silent again.
    """
    import asyncio

    from live_runner.account_discovery import refresh_account_demo_verifications
    from shared.models import LiveAccount

    database_module_module, _engine, _data_dir = isolated_db_sandbox
    database_module_module.upsert_live_accounts(
        [
            database_module_module.LiveAccountConfig(
                id="acct-real",
                broker="alpaca",
                name="Alpaca Paper",
                broker_login="PAPER-REAL",
                is_demo=True,
            )
        ]
    )

    async def _real_shaped_discovery():
        # Exactly what discover_configured_accounts returns: LiveAccount models.
        return [
            LiveAccount(
                id="acct-real",
                name="Alpaca Paper",
                broker="alpaca",
                balance=0.0,
                is_demo=True,
                broker_login="PAPER-REAL",
                ctid="ctid-real",
            )
        ]

    import live_runner.account_discovery as discovery_module

    monkeypatch.setattr(
        discovery_module, "discover_configured_accounts", _real_shaped_discovery
    )

    written = asyncio.run(refresh_account_demo_verifications())

    assert written == 1, (
        f"the refresher wrote {written} verifications from a LiveAccount list. "
        f"discover_configured_accounts returns models, not dicts - a dict-only "
        f"guard makes this function a permanent no-op and the daemon's TTL goes "
        f"unrefreshed."
    )
    assert database_module_module.live_account_submission_block_reason(
        "acct-real"
    ) is None, "the account is still refused after a successful re-verification"
    assert database_module_module.get_live_account("acct-real").ctid == "ctid-real"
