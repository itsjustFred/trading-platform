import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import live_runner.daemon as daemon
from live_runner.ctrader_adapter import CTraderAuthTerminalError
from live_runner.signal_sources.discord_source import (
    DiscordAuthenticationError,
    _is_discord_auth_error,
)
from shared.models import LiveAccountConfig, LiveDeploymentConfig


@dataclass(slots=True)
class _FailingAdapter:
    connect_calls: int = 0
    disconnect_calls: int = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        raise RuntimeError("connect failed")

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


@dataclass(slots=True)
class _TrackingAdapter:
    disconnect_calls: int = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


def test_ensure_adapter_for_active_account_keeps_adapter_after_connect_failure(monkeypatch):
    adapter = _FailingAdapter()
    account = LiveAccountConfig(
        id="acc-1",
        broker="ctrader",
        name="Demo",
        broker_login="",
        ctid="",
    )

    def _factory(_account, *, accounts_env_values):
        return adapter

    monkeypatch.setattr(daemon, "_create_adapter_for_account", _factory)
    active_adapters: dict[str, object] = {}

    asyncio.run(
        daemon._ensure_adapter_for_active_account(
            "acc-1",
            {"acc-1": account},
            active_adapters,
            accounts_env_values={},
        )
    )
    asyncio.run(
        daemon._ensure_adapter_for_active_account(
            "acc-1",
            {"acc-1": account},
            active_adapters,
            accounts_env_values={},
        )
    )

    assert adapter.connect_calls == 1
    assert adapter.disconnect_calls == 0
    assert active_adapters == {"acc-1": adapter}


def test_ensure_adapter_for_active_account_blocks_ctrader_terminal_auth(monkeypatch):
    daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS.clear()
    daemon._CTRADER_AUTH_BLOCKED_REASONS.clear()

    @dataclass(slots=True)
    class _TerminalFailAdapter:
        connect_calls: int = 0
        disconnect_calls: int = 0

        async def connect(self) -> None:
            self.connect_calls += 1
            raise CTraderAuthTerminalError(
                "Trading account is not authorized",
                error_code="TRADING_ACCOUNT_IS_NOT_AUTHORIZED",
            )

        async def disconnect(self) -> None:
            self.disconnect_calls += 1

    adapter = _TerminalFailAdapter()
    account = LiveAccountConfig(
        id="acc-ctrader",
        broker="ctrader",
        name="Demo",
        broker_login="",
        ctid="",
    )

    def _factory(_account, *, accounts_env_values):
        return adapter

    monkeypatch.setattr(daemon, "_create_adapter_for_account", _factory)
    active_adapters: dict[str, object] = {}

    asyncio.run(
        daemon._ensure_adapter_for_active_account(
            "acc-ctrader",
            {"acc-ctrader": account},
            active_adapters,
            accounts_env_values={},
        )
    )

    assert adapter.connect_calls == 1
    assert adapter.disconnect_calls == 1
    assert active_adapters == {}
    assert "acc-ctrader" in daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS
    assert "not authorized" in daemon._CTRADER_AUTH_BLOCKED_REASONS["acc-ctrader"].lower()


def test_disconnect_inactive_adapters_removes_and_disconnects() -> None:
    keep_adapter = _TrackingAdapter()
    drop_adapter = _TrackingAdapter()
    active_adapters = {"keep": keep_adapter, "drop": drop_adapter}

    asyncio.run(daemon._disconnect_inactive_adapters(active_adapters, {"keep"}))

    assert active_adapters == {"keep": keep_adapter}
    assert keep_adapter.disconnect_calls == 0
    assert drop_adapter.disconnect_calls == 1


def test_resolve_ctrader_credentials_defaults_demo_true_when_unset() -> None:
    values = {
        "CTRADER_APP_ID": "app",
        "CTRADER_APP_SECRET": "secret",
        "CTRADER_ACCESS_TOKEN": "token",
    }
    credentials = daemon._resolve_ctrader_credentials(values)
    assert credentials == ("app", "secret", "token", "", True)


def test_resolve_ctrader_credentials_includes_refresh_token() -> None:
    values = {
        "CTRADER_APP_ID": "app",
        "CTRADER_APP_SECRET": "secret",
        "CTRADER_ACCESS_TOKEN": "token",
        "CTRADER_REFRESH_TOKEN": "refresh",
        "CTRADER_DEMO": "false",
    }
    credentials = daemon._resolve_ctrader_credentials(values)
    assert credentials == ("app", "secret", "token", "refresh", False)


def test_discord_auth_error_detector_matches_improper_token_message() -> None:
    assert _is_discord_auth_error(RuntimeError("Improper token has been passed"))


def test_ctrader_pending_deferred_error_detector() -> None:
    assert daemon._is_ctrader_pending_deferred_error(RuntimeError("(15, 'Deferred')"))
    assert not daemon._is_ctrader_pending_deferred_error(RuntimeError("Connection timeout"))


def test_discord_listener_retries_stop_on_auth_error(monkeypatch) -> None:
    async def _raise_auth_error(*_args, **_kwargs):
        raise DiscordAuthenticationError("Discord bot TEST auth failed: Invalid token")

    monkeypatch.setattr(
        daemon,
        "_run_discord_signal_listener_for_account",
        _raise_auth_error,
    )
    daemon._DISCORD_AUTH_BLOCKED_ACCOUNTS.clear()
    daemon._DISCORD_AUTH_BLOCKED_REASONS.clear()

    asyncio.run(
        daemon._run_discord_listener_with_retries(
            "acc-1",
            object(),
            {},
            accounts_env_values={},
        )
    )

    assert "acc-1" in daemon._DISCORD_AUTH_BLOCKED_ACCOUNTS
    assert "Invalid token" in daemon._DISCORD_AUTH_BLOCKED_REASONS["acc-1"]


def test_discord_listener_retries_stop_on_missing_token(monkeypatch) -> None:
    async def _raise_missing_token(*_args, **_kwargs):
        raise ValueError("Missing DISCORD_BOT_TOKEN")

    monkeypatch.setattr(
        daemon,
        "_run_discord_signal_listener_for_account",
        _raise_missing_token,
    )
    daemon._DISCORD_AUTH_BLOCKED_ACCOUNTS.clear()
    daemon._DISCORD_AUTH_BLOCKED_REASONS.clear()

    asyncio.run(
        daemon._run_discord_listener_with_retries(
            "acc-2",
            object(),
            {},
            accounts_env_values={},
        )
    )

    assert "acc-2" in daemon._DISCORD_AUTH_BLOCKED_ACCOUNTS
    assert "Missing DISCORD_BOT_TOKEN" in daemon._DISCORD_AUTH_BLOCKED_REASONS["acc-2"]


def test_discord_bot_listener_stops_on_terminal_auth_message(monkeypatch) -> None:
    class _InvalidTokenSource:
        is_paused = False

        async def listen_signals(self):
            if False:
                yield None
            raise RuntimeError("Improper token has been passed.")

    updates: list[tuple[str, object]] = []
    daemon._DISCORD_SERVICE_AUTH_BLOCKED_IDS.clear()
    daemon._DISCORD_SERVICE_AUTH_BLOCKED_REASONS.clear()

    def _capture_update(service_id: str, status: object, **_kwargs) -> None:
        updates.append((service_id, status))

    monkeypatch.setattr(daemon, "update_service_status", _capture_update)

    service = SimpleNamespace(id="service-1", name="TEST_BOT")
    asyncio.run(daemon._run_discord_bot_service_listener(service, _InvalidTokenSource()))

    assert updates
    assert updates[-1] == ("service-1", daemon.ServiceStatus.FAILED)
    assert "service-1" in daemon._DISCORD_SERVICE_AUTH_BLOCKED_IDS
    assert "improper token" in daemon._DISCORD_SERVICE_AUTH_BLOCKED_REASONS["service-1"].lower()


def test_start_missing_discord_tasks_skips_blocked_accounts() -> None:
    daemon._DISCORD_AUTH_BLOCKED_ACCOUNTS.clear()
    daemon._DISCORD_AUTH_BLOCKED_REASONS.clear()
    daemon._DISCORD_AUTH_BLOCKED_ACCOUNTS.add("blocked-acc")
    daemon._DISCORD_AUTH_BLOCKED_REASONS["blocked-acc"] = "auth failed"

    discord_tasks: dict[str, asyncio.Task[None]] = {}
    daemon._start_missing_discord_tasks(
        adapters={"blocked-acc": object()},
        discord_tasks=discord_tasks,
        active_discord_account_ids={"blocked-acc"},
        deployments_by_account={},
        accounts_env_values={},
    )

    assert discord_tasks == {}


def test_start_missing_tasks_skips_ctrader_auth_blocked_accounts() -> None:
    daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS.clear()
    daemon._CTRADER_AUTH_BLOCKED_REASONS.clear()
    daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS.add("blocked-ctrader")
    daemon._CTRADER_AUTH_BLOCKED_REASONS["blocked-ctrader"] = "not authorized"

    active_tasks: dict[str, asyncio.Task[None]] = {}
    daemon._start_missing_tasks(
        adapters={"blocked-ctrader": object()},
        active_tasks=active_tasks,
        active_account_ids={"blocked-ctrader"},
    )

    assert active_tasks == {}


def test_stockkit_route_guard_rejects_mismatched_signal_metadata() -> None:
    deployment = LiveDeploymentConfig(
        strategy_name="discord::ramjet",
        account_id="PA3T0MQAGQ51",
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="discord",
        signal_source_type="discord",
        signal_source_id="RAMJET",
        signal_config={"signal_processor": "ramjet"},
    )

    assert daemon._deployment_matches_stockkit_route(
        deployment,
        {"discord_bot_name": "RAMJET"},
    )
    assert not daemon._deployment_matches_stockkit_route(
        deployment,
        {"discord_bot_name": "NITRO"},
    )
