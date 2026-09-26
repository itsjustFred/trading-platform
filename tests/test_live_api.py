from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

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


def test_live_deployments_crud_endpoints(monkeypatch):
    import viewer.main as viewer_main_module

    account_id = "PA3T0MQAGQ51"
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
        "signal_config": {"channel_id": "123", "wait_candle_close": True},
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
                config={"token_env_key": "DISCORD_BOT_RAMJET_TOKEN", "channel_id": "123"},
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
            "signal_config": {"channel_id": "123", "wait_candle_close": True},
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
                config={"token_env_key": "DISCORD_BOT_RAMJET_TOKEN", "channel_id": "123"},
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
            "signal_config": {"channel_id": "123", "signal_processor": "ramjet"},
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

    def _update_service_status(value: str, status, *, last_activity=None):
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
        lambda _paths: [
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
            "config": {"token_env_key": "DISCORD_BOT_RAMJET_TOKEN", "channel_id": "123"},
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
