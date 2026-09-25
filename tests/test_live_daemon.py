import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import live_runner.daemon as daemon
import live_runner.signal_sources.discord_source as discord_source_module
from live_runner.ctrader_adapter import CTraderAuthTerminalError
from live_runner.signal_sources.discord_source import (
    DiscordAuthenticationError,
    DiscordSignalSource,
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


@pytest.fixture(autouse=True)
def _stub_submission_claims(monkeypatch):
    monkeypatch.setattr(
        daemon, "claim_live_deployment_submission", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        daemon, "release_live_deployment_submission", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(daemon, "_deployment_account_is_demo", lambda _account_id: True)


def test_ensure_adapter_for_active_account_keeps_adapter_after_connect_failure(monkeypatch):
    adapter = _FailingAdapter()
    account = LiveAccountConfig(
        id="acc-1",
        broker="ctrader",
        name="Demo",
        is_demo=True,
        broker_login="",
        ctid="",
    )

    def _factory(_account, *, accounts_env_values, accounts_env_path=None):
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


def test_ensure_adapter_refuses_non_demo_account(monkeypatch):
    account = LiveAccountConfig(
        id="acc-live",
        broker="alpaca",
        name="Live",
        is_demo=False,
    )
    called = False

    def _factory(*_args, **_kwargs):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(daemon, "_create_adapter_for_account", _factory)

    asyncio.run(
        daemon._ensure_adapter_for_active_account(
            "acc-live",
            {"acc-live": account},
            {},
            accounts_env_values={},
        )
    )

    assert called is False


def test_ensure_adapter_refuses_non_paper_alpaca_adapter(monkeypatch):
    account = LiveAccountConfig(
        id="acc-paper",
        broker="alpaca",
        name="Paper",
        is_demo=True,
    )
    adapter = SimpleNamespace(paper=False)
    monkeypatch.setattr(
        daemon, "_create_adapter_for_account", lambda *_args, **_kwargs: adapter
    )

    asyncio.run(
        daemon._ensure_adapter_for_active_account(
            "acc-paper",
            {"acc-paper": account},
            {},
            accounts_env_values={},
        )
    )

    assert adapter.paper is False


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
        is_demo=True,
        broker_login="",
        ctid="",
    )

    def _factory(_account, *, accounts_env_values, accounts_env_path=None):
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


def test_disconnect_inactive_adapters_clears_stale_ctrader_block() -> None:
    daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS.clear()
    daemon._CTRADER_AUTH_BLOCKED_REASONS.clear()
    daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS.add("drop")
    daemon._CTRADER_AUTH_BLOCKED_REASONS["drop"] = "old auth failure"
    drop_adapter = _TrackingAdapter()
    active_adapters = {"drop": drop_adapter}

    asyncio.run(daemon._disconnect_inactive_adapters(active_adapters, set()))

    assert active_adapters == {}
    assert drop_adapter.disconnect_calls == 1
    assert "drop" not in daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS
    assert "drop" not in daemon._CTRADER_AUTH_BLOCKED_REASONS


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


def test_resolve_ctrader_credentials_rejects_invalid_demo_value() -> None:
    values = {
        "CTRADER_APP_ID": "app",
        "CTRADER_APP_SECRET": "secret",
        "CTRADER_ACCESS_TOKEN": "token",
        "CTRADER_DEMO": "tru",
    }

    with pytest.raises(ValueError, match="CTRADER_DEMO"):
        daemon._resolve_ctrader_credentials(values)


def test_ctrader_signature_safe_survives_invalid_bool_config() -> None:
    """A typo'd CTRADER_DEMO must not abort the deployment sync iteration: the
    signature degrades to a stable error value instead of raising."""
    values = {
        "CTRADER_APP_ID": "app",
        "CTRADER_APP_SECRET": "secret",
        "CTRADER_ACCESS_TOKEN": "token",
        "CTRADER_DEMO": "tru",
    }
    assert daemon._ctrader_credentials_signature_safe(values) == "invalid-credentials"
    assert (
        daemon._ctrader_credentials_signature_safe({**values, "CTRADER_DEMO": "true"})
        != "invalid-credentials"
    )


def test_refresh_ctrader_blocked_state_survives_invalid_bool_config() -> None:
    """The sync loop calls the signature per cTrader account every cycle; a
    typo in the shared env must keep the loop alive for all accounts."""
    accounts = [
        LiveAccountConfig(id="acc-1", broker="ctrader", name="Broker One"),
        LiveAccountConfig(id="acc-2", broker="ctrader", name="Broker Two"),
    ]
    daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS.clear()
    daemon._CTRADER_AUTH_BLOCKED_REASONS.clear()
    daemon._CTRADER_ACCOUNT_CREDENTIAL_SIGNATURES.clear()
    try:
        assert daemon._refresh_ctrader_blocked_state_for_accounts(accounts, {}) == set()
        typo_env = {
            "CTRADER_APP_ID": "app",
            "CTRADER_APP_SECRET": "secret",
            "CTRADER_ACCESS_TOKEN": "token",
            "CTRADER_DEMO": "ture",
        }
        # Must not raise; signature flips to the stable error value once.
        assert daemon._refresh_ctrader_blocked_state_for_accounts(accounts, typo_env) == {
            "acc-1",
            "acc-2",
        }
        # Stable across cycles: no rotation churn while the typo persists.
        assert daemon._refresh_ctrader_blocked_state_for_accounts(accounts, typo_env) == set()
    finally:
        daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS.clear()
        daemon._CTRADER_AUTH_BLOCKED_REASONS.clear()
        daemon._CTRADER_ACCOUNT_CREDENTIAL_SIGNATURES.clear()


def test_wait_for_candle_close_defaults_true_when_env_is_missing() -> None:
    deployment = LiveDeploymentConfig(
        strategy_name="fx_breakout",
        account_id="ctrader-demo",
        symbols=["EURUSD"],
        timeframe="15m",
        signal_source="discord",
        signal_config={},
    )

    assert daemon._deployment_wait_for_candle_close(deployment, {}) is True

def _deployment_with_signal_config(
    signal_config: dict[str, object],
    *,
    settings: dict[str, object] | None = None,
) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        strategy_name="fx_breakout",
        account_id="ctrader-demo",
        symbols=["EURUSD"],
        timeframe="15m",
        signal_source="discord",
        signal_config=signal_config,
        settings=settings or {},
    )

def test_wait_for_candle_close_typo_raises_instead_of_silently_disabling() -> None:
    """A typo'd boolean must not silently disable candle-close waiting (which
    forces immediate market orders); the strict parser rejects it."""
    deployment = _deployment_with_signal_config({"wait_candle_close": "ture"})

    with pytest.raises(ValueError, match="wait_candle_close"):
        daemon._deployment_wait_for_candle_close(deployment, {})

    deployment = _deployment_with_signal_config({"wait_for_candle_close": "eanbled"})

    with pytest.raises(ValueError, match="wait_for_candle_close"):
        daemon._deployment_wait_for_candle_close(deployment, {})

def test_wait_for_candle_close_env_typo_raises() -> None:
    deployment = _deployment_with_signal_config({})

    with pytest.raises(ValueError, match="DISCORD_WAIT_FOR_CANDLE_CLOSE"):
        daemon._deployment_wait_for_candle_close(
            deployment, {"DISCORD_WAIT_FOR_CANDLE_CLOSE": "yess"}
        )

def test_wait_for_candle_close_env_value_with_whitespace_is_recognized() -> None:
    deployment = _deployment_with_signal_config({})

    assert (
        daemon._deployment_wait_for_candle_close(
            deployment, {"DISCORD_WAIT_FOR_CANDLE_CLOSE": " false "}
        )
        is False
    )

def test_wait_for_candle_close_accepts_recognized_tokens() -> None:
    for raw, expected in (("true", True), ("0", False), ("Yes", True), ("off", False)):
        deployment = _deployment_with_signal_config({"wait_candle_close": raw})
        assert daemon._deployment_wait_for_candle_close(deployment, {}) is expected

def test_allow_short_typo_raises_instead_of_silently_disabling() -> None:
    """A typo'd allow_short must not silently turn shorts into exit-only."""
    deployment = _deployment_with_signal_config(
        {"allow_short": "eanbled"},
        settings={"allow_short": True},
    )

    with pytest.raises(ValueError, match="allow_short"):
        daemon._deployment_allows_short(deployment)

def test_allow_short_recognized_tokens() -> None:
    enabled = _deployment_with_signal_config({"allow_short": "true"})
    assert daemon._deployment_allows_short(enabled) is True
    disabled = _deployment_with_signal_config(
        {},
        settings={"allow_short": "false"},
    )
    assert daemon._deployment_allows_short(disabled) is False


def test_ensure_adapter_skips_account_with_invalid_bool_config(monkeypatch) -> None:
    """A typo'd ALPACA_PAPER_ACC{N} raises ValueError inside adapter creation.
    The account must be skipped with an error log — not propagated, which would
    abort the whole deployment sync iteration every cycle."""
    daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS.clear()

    account = LiveAccountConfig(
        id="acc-typo",
        broker="alpaca",
        name="Typo",
    )

    def _raise_typo(_account, **_kwargs):
        raise ValueError("Invalid boolean value for ALPACA_PAPER_ACC1: 'ture'.")

    monkeypatch.setattr(daemon, "_create_adapter_for_account", _raise_typo)
    active_adapters: dict[str, object] = {}

    # Must not raise.
    asyncio.run(
        daemon._ensure_adapter_for_active_account(
            "acc-typo",
            {"acc-typo": account},
            active_adapters,
            accounts_env_values={},
        )
    )

    assert "acc-typo" not in active_adapters


def test_ctrader_blocked_state_refresh_survives_demo_bool_typo() -> None:
    """A typo'd CTRADER_DEMO must not abort the whole deployment sync
    iteration: the signature refresh skips the account with a warning, so
    healthy accounts in the same iteration keep being served."""
    daemon._CTRADER_ACCOUNT_CREDENTIAL_SIGNATURES.clear()
    accounts = [
        LiveAccountConfig(id="acc-ctrader", broker="ctrader", name="Demo"),
        LiveAccountConfig(id="acc-alpaca", broker="alpaca", name="Paper"),
    ]
    env_values = {
        "CTRADER_APP_ID": "app",
        "CTRADER_APP_SECRET": "secret",
        "CTRADER_ACCESS_TOKEN": "token",
        "CTRADER_DEMO": "ture",
    }

    # Must not raise.
    changed = daemon._refresh_ctrader_blocked_state_for_accounts(accounts, env_values)

    assert changed == set()
    # The account is recorded with the invalid signature marker so a later fix
    # of the typo still triggers the credential-change path.
    assert "acc-ctrader" in daemon._CTRADER_ACCOUNT_CREDENTIAL_SIGNATURES


def _signal_with_metadata(metadata: dict[str, object] | None = None):
    from risk import TradeSignal

    return TradeSignal(
        action="buy",
        symbol="EURUSD",
        entry_price=1.1,
        stop_loss=None,
        take_profit=None,
        timeframe="15m",
        metadata=metadata or {},
    )


def test_dispatch_drops_signal_when_matched_deployment_has_typo_bool(monkeypatch) -> None:
    """A typo'd wait boolean drops the signal for the matched deployment. It must
    NOT fall through to a sibling deployment on the same channel: that would leak
    one strategy's signal into another strategy. Fail-closed beats fail-over."""
    submitted: list[str] = []
    bad = _deployment_with_signal_config({"wait_candle_close": "ture"})
    good = _deployment_with_signal_config(
        {"wait_candle_close": "true"},
        settings={"signal_processor": "other"},
    )

    async def _record_submit(
        account_id, adapter, deployment, *, trade_signal, signal_timeframe, wait_enabled
    ):
        submitted.append(deployment.strategy_name)

    monkeypatch.setattr(daemon, "_submit_normalized_discord_signal", _record_submit)

    asyncio.run(
        daemon._dispatch_discord_trade_signal(
            "acc-1",
            adapter=object(),
            deployments_for_channel=[bad, good],
            trade_signal=_signal_with_metadata({"discord_channel_id": "chan"}),
            accounts_env_values={},
        )
    )

    assert submitted == []


def test_dispatch_typo_bool_does_not_raise_to_listener_loop(monkeypatch) -> None:
    bad = _deployment_with_signal_config({"wait_candle_close": "ture"})

    async def _fail_submit(*args, **kwargs):
        raise AssertionError("submission must never be attempted for a bad config")

    monkeypatch.setattr(daemon, "_submit_normalized_discord_signal", _fail_submit)

    # Must not raise; the listener loop keeps running.
    asyncio.run(
        daemon._dispatch_discord_trade_signal(
            "acc-1",
            adapter=object(),
            deployments_for_channel=[bad],
            trade_signal=_signal_with_metadata({}),
            accounts_env_values={},
        )
    )


def test_submit_skips_deployment_with_typo_allow_short(monkeypatch) -> None:
    """A typo'd allow_short skips the deployment at signal-conversion time."""
    deployment = _deployment_with_signal_config(
        {"allow_short": "eanbled"},
        settings={"allow_short": True},
    )

    class _NoSubmitAdapter:
        async def get_positions(self):
            raise AssertionError("must not be reached")

        async def get_balance(self):
            raise AssertionError("must not be reached")

    from risk import TradeSignal

    trade_signal = TradeSignal(
        action="short",
        symbol="EURUSD",
        entry_price=1.1,
        stop_loss=None,
        take_profit=None,
        timeframe="15m",
        metadata={},
    )

    submitted: list[object] = []

    def _fail_submit_with_risk(adapter, deployment_arg, live_signal):
        submitted.append(live_signal)
        raise AssertionError("submission must never be attempted for a bad config")

    monkeypatch.setattr(daemon, "submit_order_with_risk", _fail_submit_with_risk)
    monkeypatch.setattr(daemon, "_deployment_is_currently_active", lambda _deployment: True)

    asyncio.run(
        daemon._submit_normalized_discord_signal(
            "acc-1",
            _NoSubmitAdapter(),
            deployment,
            trade_signal,
            signal_timeframe="15m",
            wait_enabled=False,
        )
    )

    assert submitted == []


def test_submission_claim_blocks_order_after_pause_race(monkeypatch) -> None:
    deployment = _deployment_with_signal_config({})
    released: list[str] = []

    class _NoSubmitAdapter:
        async def get_positions(self):
            raise AssertionError("must not be reached")

        async def get_balance(self):
            raise AssertionError("must not be reached")

    from risk import TradeSignal

    trade_signal = TradeSignal(
        action="buy",
        symbol="EURUSD",
        entry_price=1.1,
        stop_loss=1.0,
        take_profit=1.2,
        timeframe="15m",
        metadata={},
    )

    monkeypatch.setattr(daemon, "_deployment_is_currently_active", lambda _deployment: True)
    monkeypatch.setattr(daemon, "claim_live_deployment_submission", lambda *_args: False)
    monkeypatch.setattr(
        daemon, "release_live_deployment_submission", lambda token: released.append(token)
    )

    async def _unexpected_submit(*_args, **_kwargs):
        raise AssertionError("order submission must lose the pause race")

    monkeypatch.setattr(daemon, "submit_order_with_risk", _unexpected_submit)

    asyncio.run(
        daemon._submit_normalized_discord_signal(
            "acc-1",
            _NoSubmitAdapter(),
            deployment,
            trade_signal,
            signal_timeframe="15m",
            wait_enabled=False,
        )
    )

    assert released == []


def test_submission_path_refuses_non_demo_account(monkeypatch) -> None:
    deployment = _deployment_with_signal_config({})
    released: list[str] = []

    class _NoSubmitAdapter:
        async def get_positions(self):
            raise AssertionError("must not be reached")

        async def get_balance(self):
            raise AssertionError("must not be reached")

    from risk import TradeSignal

    trade_signal = TradeSignal(
        action="buy",
        symbol="EURUSD",
        entry_price=1.1,
        stop_loss=1.0,
        take_profit=1.2,
        timeframe="15m",
        metadata={},
    )

    monkeypatch.setattr(daemon, "_deployment_is_currently_active", lambda _deployment: True)
    monkeypatch.setattr(daemon, "_deployment_account_is_demo", lambda _account_id: False)
    monkeypatch.setattr(
        daemon, "release_live_deployment_submission", lambda token: released.append(token)
    )

    async def _unexpected_submit(*_args, **_kwargs):
        raise AssertionError("non-demo account must not submit")

    monkeypatch.setattr(daemon, "submit_order_with_risk", _unexpected_submit)

    asyncio.run(
        daemon._submit_normalized_discord_signal(
            "acc-1",
            _NoSubmitAdapter(),
            deployment,
            trade_signal,
            signal_timeframe="15m",
            wait_enabled=False,
        )
    )

    assert released == []


def test_submission_claim_releases_after_submit_failure(monkeypatch) -> None:
    deployment = _deployment_with_signal_config({})
    released: list[str] = []

    class _NoSubmitAdapter:
        async def get_positions(self):
            raise AssertionError("must not be reached")

        async def get_balance(self):
            raise AssertionError("must not be reached")

    from risk import TradeSignal

    trade_signal = TradeSignal(
        action="buy",
        symbol="EURUSD",
        entry_price=1.1,
        stop_loss=1.0,
        take_profit=1.2,
        timeframe="15m",
        metadata={},
    )

    monkeypatch.setattr(daemon, "_deployment_is_currently_active", lambda _deployment: True)

    async def _failing_submit(*_args, **_kwargs):
        raise RuntimeError("broker rejected")

    monkeypatch.setattr(daemon, "submit_order_with_risk", _failing_submit)
    monkeypatch.setattr(
        daemon,
        "release_live_deployment_submission",
        lambda token: released.append(token) or True,
    )

    asyncio.run(
        daemon._submit_normalized_discord_signal(
            "acc-1",
            _NoSubmitAdapter(),
            deployment,
            trade_signal,
            signal_timeframe="15m",
            wait_enabled=False,
        )
    )

    assert len(released) == 1


def test_ctrader_auth_block_clears_for_all_accounts_when_shared_credentials_change() -> None:
    accounts = [
        LiveAccountConfig(id="acc-1", broker="ctrader", name="Broker One"),
        LiveAccountConfig(id="acc-2", broker="ctrader", name="Broker Two"),
    ]
    original = {
        "CTRADER_APP_ID": "app",
        "CTRADER_APP_SECRET": "secret",
        "CTRADER_ACCESS_TOKEN": "token",
        "DISCORD_BOT_TOKEN": "old",
    }
    daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS.clear()
    daemon._CTRADER_AUTH_BLOCKED_REASONS.clear()
    daemon._CTRADER_ACCOUNT_CREDENTIAL_SIGNATURES.clear()
    assert daemon._refresh_ctrader_blocked_state_for_accounts(accounts, original) == set()
    daemon._block_ctrader_account("acc-1", CTraderAuthTerminalError("bad token"))
    daemon._block_ctrader_account("acc-2", CTraderAuthTerminalError("bad token"))

    unrelated_change = {**original, "DISCORD_BOT_TOKEN": "new"}
    assert daemon._refresh_ctrader_blocked_state_for_accounts(accounts, unrelated_change) == set()
    assert daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS == {"acc-1", "acc-2"}

    credential_change = {**unrelated_change, "CTRADER_ACCESS_TOKEN": "new-token"}
    assert daemon._refresh_ctrader_blocked_state_for_accounts(
        accounts, credential_change
    ) == {"acc-1", "acc-2"}
    assert daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS == set()


def test_ctrader_deferred_error_does_not_block_account() -> None:
    daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS.clear()
    daemon._CTRADER_AUTH_BLOCKED_REASONS.clear()

    daemon._block_ctrader_account("acc-1", RuntimeError("(15, 'Deferred')"))

    assert "acc-1" not in daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS
    assert "acc-1" not in daemon._CTRADER_AUTH_BLOCKED_REASONS


def test_ctrader_transient_error_does_not_block_account() -> None:
    daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS.clear()
    daemon._CTRADER_AUTH_BLOCKED_REASONS.clear()

    daemon._block_ctrader_account("acc-1", RuntimeError("Connection timeout"))

    assert "acc-1" not in daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS
    assert "acc-1" not in daemon._CTRADER_AUTH_BLOCKED_REASONS


def test_rotate_changed_ctrader_adapters_disconnects_and_cancels_tasks() -> None:
    async def _run() -> None:
        adapter = _TrackingAdapter()
        sleeper = asyncio.create_task(asyncio.sleep(60))
        discord_sleeper = asyncio.create_task(asyncio.sleep(60))
        active_adapters = {"acc-1": adapter}
        active_tasks = {"acc-1": sleeper}
        active_discord_tasks = {"acc-1": discord_sleeper}

        await daemon._rotate_changed_ctrader_adapters(
            {"acc-1"},
            active_adapters,
            active_tasks,
            active_discord_tasks,
        )
        await asyncio.sleep(0)

        assert active_adapters == {}
        assert active_tasks == {}
        assert active_discord_tasks == {}
        assert adapter.disconnect_calls == 1
        assert sleeper.cancelled()
        assert discord_sleeper.cancelled()

    asyncio.run(_run())


def test_rotate_changed_ctrader_adapters_awaits_listener_cancellation_cleanup() -> None:
    async def _run() -> None:
        listener_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup_finished = asyncio.Event()
        disconnect_started = asyncio.Event()

        async def _listener() -> None:
            listener_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
                cleanup_finished.set()

        @dataclass(slots=True)
        class _BarrierAdapter:
            async def disconnect(self) -> None:
                disconnect_started.set()

        listener = asyncio.create_task(_listener())
        await listener_started.wait()
        rotation = asyncio.create_task(
            daemon._rotate_changed_ctrader_adapters(
                {"acc-1"},
                {"acc-1": _BarrierAdapter()},
                {"acc-1": listener},
                {},
            )
        )

        await asyncio.wait_for(cleanup_started.wait(), timeout=0.1)
        await asyncio.sleep(0)
        assert disconnect_started.is_set() is False

        release_cleanup.set()
        await asyncio.wait_for(rotation, timeout=0.1)
        assert cleanup_finished.is_set() is True
        assert disconnect_started.is_set() is True

    asyncio.run(_run())


def test_connect_active_accounts_runs_concurrently(monkeypatch) -> None:
    started: list[str] = []

    async def _run() -> None:
        both_started = asyncio.Event()

        async def _ensure(account_id, *_args, **_kwargs):
            started.append(account_id)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()

        monkeypatch.setattr(daemon, "_ensure_adapter_for_active_account", _ensure)

        await asyncio.wait_for(
            daemon._connect_active_accounts(
                {"slow", "fast"},
                {},
                {},
                accounts_env_values={},
            ),
            timeout=0.1,
        )

    asyncio.run(_run())

    assert set(started) == {"slow", "fast"}


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


def test_discord_poll_does_not_claim_live_signal_before_deployment_routing(monkeypatch):
    """The execution adapter is the sole authority for the persisted order intent.

    Persisting at the source layer creates a canonical Discord signal with an empty
    deployment id. The cTrader adapter then mistakes the first routed delivery for
    crash recovery and fail-closes instead of submitting the first broker order.
    """
    source = DiscordSignalSource(
        bot_name="ramjet",
        token="test-token",
        channel_ids={"channel-1"},
        timeframe="1h",
        trusted_author_ids={"author-1"},
    )
    message = {
        "id": "discord-123",
        "channel_id": "channel-1",
        "timestamp": "2026-07-15T10:00:00Z",
        "content": "RAM JET BUY AAPL @ 200.00",
        "author": {"id": "author-1", "bot": True},
    }

    async def _fetch(_channel_id):
        return [message]

    def _forbid_source_persistence(*_args, **_kwargs):
        pytest.fail("Discord source must not persist an execution intent before routing")

    # Keep the guard effective if a future regression restores the former
    # module-level import used by the removed source persistence helper.
    monkeypatch.setattr(
        discord_source_module,
        "save_live_signal",
        _forbid_source_persistence,
        raising=False,
    )
    source._fetch_messages = _fetch

    asyncio.run(source._poll_once("channel-1"))

    queued = source._queue.get_nowait()
    assert queued.metadata["discord_message_id"] == "discord-123"


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


# ── Cleanup 15-A: PAUSE must stop live trading from stale snapshots ───────────


class _SignalSubmittingAdapter:
    def __init__(self) -> None:
        self.submitted: list[object] = []

    async def connect(self) -> None:
        return None

    async def submit_order(self, signal, *, qty=None) -> str:
        self.submitted.append(signal)
        return "order-1"

    async def get_balance(self):
        return 100_000.0, 100_000.0

    async def get_positions(self):
        return []


def _discord_deployment(status: str = "ACTIVE") -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id="11111111-1111-1111-1111-111111111111",
        strategy_name="discord::ramjet",
        account_id="acc-1",
        symbols=["AAPL"],
        timeframe="1h",
        status=status,
        signal_source="discord",
        signal_source_type="discord",
        signal_config={"channel_id": "channel-1"},
    )


def test_active_discord_deployments_exclude_paused_and_stopped() -> None:
    deployments = [
        _discord_deployment("ACTIVE"),
        _discord_deployment("PAUSED"),
        _discord_deployment("STOPPED"),
    ]

    active = daemon._active_discord_deployments_for_account("acc-1", deployments)

    assert [deployment.status for deployment in active] == ["ACTIVE"]


def _trade_signal() -> object:
    from risk import TradeSignal

    return TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=200.0,
        stop_loss=195.0,
        take_profit=210.0,
        timeframe="1h",
        metadata={"discord_channel_id": "channel-1"},
    )


def test_paused_deployment_snapshot_is_rejected_at_dispatch(monkeypatch) -> None:
    """A stale snapshot must not trade: PAUSE is checked against the DB at dispatch."""
    adapter = _SignalSubmittingAdapter()
    stale = _discord_deployment("PAUSED")

    def _forbid_refresh(_deployment_id: str):
        pytest.fail("paused deployment must be rejected before any DB refresh")

    monkeypatch.setattr(daemon, "get_live_deployment", _forbid_refresh)
    consulted: list[str] = []
    monkeypatch.setattr(
        daemon,
        "_deployment_is_currently_active",
        lambda deployment: consulted.append(str(deployment.id)) or False,
    )

    async def _no_wait(_signal, _timeframe):
        return None

    monkeypatch.setattr(daemon, "wait_for_candle_close", _no_wait)

    asyncio.run(
        daemon._dispatch_discord_trade_signal(
            "acc-1",
            adapter,
            [stale],
            _trade_signal(),
            accounts_env_values={},
        )
    )

    assert adapter.submitted == []
    assert consulted == [str(stale.id)]


def test_active_deployment_trades_after_status_recheck(monkeypatch) -> None:
    adapter = _SignalSubmittingAdapter()
    deployment = _discord_deployment("ACTIVE")

    monkeypatch.setattr(
        daemon,
        "get_live_deployment",
        lambda deployment_id: _discord_deployment("ACTIVE"),
    )

    async def _no_wait(_signal, _timeframe):
        return None

    monkeypatch.setattr(daemon, "wait_for_candle_close", _no_wait)

    asyncio.run(
        daemon._dispatch_discord_trade_signal(
            "acc-1",
            adapter,
            [deployment],
            _trade_signal(),
            accounts_env_values={},
        )
    )

    assert len(adapter.submitted) == 1


def test_deployment_paused_between_snapshot_and_dispatch_is_not_traded(
    monkeypatch,
) -> None:
    """Snapshot says ACTIVE but the DB now says PAUSED: refuse to trade."""
    adapter = _SignalSubmittingAdapter()
    deployment = _discord_deployment("ACTIVE")

    monkeypatch.setattr(
        daemon,
        "get_live_deployment",
        lambda deployment_id: _discord_deployment("PAUSED"),
    )

    async def _no_wait(_signal, _timeframe):
        return None

    monkeypatch.setattr(daemon, "wait_for_candle_close", _no_wait)

    asyncio.run(
        daemon._dispatch_discord_trade_signal(
            "acc-1",
            adapter,
            [deployment],
            _trade_signal(),
            accounts_env_values={},
        )
    )

    assert adapter.submitted == []


def test_stale_deployment_removed_from_db_is_not_traded(monkeypatch) -> None:
    adapter = _SignalSubmittingAdapter()
    deployment = _discord_deployment("ACTIVE")

    monkeypatch.setattr(daemon, "get_live_deployment", lambda _deployment_id: None)

    async def _no_wait(_signal, _timeframe):
        return None

    monkeypatch.setattr(daemon, "wait_for_candle_close", _no_wait)

    asyncio.run(
        daemon._dispatch_discord_trade_signal(
            "acc-1",
            adapter,
            [deployment],
            _trade_signal(),
            accounts_env_values={},
        )
    )

    assert adapter.submitted == []


def test_deployment_with_unknown_status_is_not_traded(monkeypatch) -> None:
    """Deny-by-default: a status outside ACTIVE/PAUSED/STOPPED must not trade.

    Pins that _normalize_deployment_status's UNKNOWN fallback keeps
    _deployment_is_currently_active fail-closed when a new status value
    appears upstream before live_runner learns about it.
    """
    adapter = _SignalSubmittingAdapter()
    deployment = _discord_deployment("ACTIVE")

    monkeypatch.setattr(
        daemon,
        "get_live_deployment",
        lambda deployment_id: _discord_deployment("MAINTENANCE"),
    )

    async def _no_wait(_signal, _timeframe):
        return None

    monkeypatch.setattr(daemon, "wait_for_candle_close", _no_wait)

    asyncio.run(
        daemon._dispatch_discord_trade_signal(
            "acc-1",
            adapter,
            [deployment],
            _trade_signal(),
            accounts_env_values={},
        )
    )

    assert adapter.submitted == []


def test_deployment_paused_during_candle_close_wait_is_not_traded(monkeypatch) -> None:
    """The pre-wait recheck passes, the deployment is paused during the
    candle-close wait, and the post-wait recheck must refuse to trade."""
    adapter = _SignalSubmittingAdapter()
    deployment = _discord_deployment("ACTIVE")
    statuses = iter(["ACTIVE", "PAUSED"])

    def _status_after_each_recheck(_deployment_id):
        return _discord_deployment(next(statuses))

    monkeypatch.setattr(daemon, "get_live_deployment", _status_after_each_recheck)

    async def _no_wait(_signal, _timeframe):
        return None

    monkeypatch.setattr(daemon, "wait_for_candle_close", _no_wait)

    asyncio.run(
        daemon._dispatch_discord_trade_signal(
            "acc-1",
            adapter,
            [deployment],
            _trade_signal(),
            accounts_env_values={},
        )
    )

    assert adapter.submitted == []


# ─── STOPPED semantics: cancel resting orders and flatten ────────────────────


class _RecordingAdapter:
    """Minimal adapter that records cancel/flatten calls."""

    def __init__(
        self,
        *,
        supports_cancel: bool = True,
        supports_flat: bool = True,
        fail_cancel: bool = False,
    ) -> None:
        self._supports_cancel = supports_cancel
        self._supports_flat = supports_flat
        self._fail_cancel = fail_cancel
        self.cancel_calls = 0
        self.flatten_calls = 0

    @property
    def supports_cancel_orders(self) -> bool:
        return self._supports_cancel

    @property
    def supports_flatten(self) -> bool:
        return self._supports_flat

    async def cancel_open_orders(self) -> int:
        if not self._supports_cancel:
            raise NotImplementedError("no cancel support")
        self.cancel_calls += 1
        if self._fail_cancel:
            raise RuntimeError("broker rejected cancel")
        return 2

    async def close_all_positions(self) -> int:
        if not self._supports_flat:
            raise NotImplementedError("no flatten support")
        self.flatten_calls += 1
        return 1


def _stopped_deployment(
    deployment_id: str = "44444444-4444-4444-8444-444444444444",
) -> LiveDeploymentConfig:
    from shared.models import LiveDeploymentConfig as _Config

    return _Config(
        id=deployment_id,
        strategy_name="demo",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="STOPPED",
        signal_source="internal",
    )


def test_stopped_deployment_cancels_and_flattens_once(monkeypatch):
    adapter = _RecordingAdapter()
    enforced: set[str] = set()

    async def _run() -> None:
        deployments = [_stopped_deployment()]
        for _ in range(3):
            await daemon._enforce_stopped_deployments(
                deployments, {"acc-1": adapter}, already_enforced=enforced
            )

    asyncio.run(_run())

    assert adapter.cancel_calls == 1, "cancel must happen once per stop transition"
    assert adapter.flatten_calls == 1, "flatten must happen once per stop transition"
    assert "44444444-4444-4444-8444-444444444444" in enforced


def test_stopped_deployment_re_enforces_after_reactivation(monkeypatch):
    """A second STOPPED transition must enforce the controls again."""
    adapter = _RecordingAdapter()
    enforced: set[str] = set()

    async def _run() -> None:
        await daemon._enforce_stopped_deployments(
            [_stopped_deployment()], {"acc-1": adapter}, already_enforced=enforced
        )
        # Re-activated: the deployment is no longer stopped, so the record clears.
        await daemon._enforce_stopped_deployments(
            [], {"acc-1": adapter}, already_enforced=enforced
        )
        # Stopped again.
        await daemon._enforce_stopped_deployments(
            [_stopped_deployment()], {"acc-1": adapter}, already_enforced=enforced
        )

    asyncio.run(_run())

    assert adapter.cancel_calls == 2
    assert adapter.flatten_calls == 2


def test_stopped_deployment_not_marked_enforced_when_adapter_cannot_flatten(monkeypatch):
    """An adapter that cannot flatten must NOT be reported as stopped-flat."""
    adapter = _RecordingAdapter(supports_flat=False)
    enforced: set[str] = set()

    async def _run() -> None:
        await daemon._enforce_stopped_deployments(
            [_stopped_deployment()], {"acc-1": adapter}, already_enforced=enforced
        )

    asyncio.run(_run())

    assert enforced == set(), "must retry next sync instead of claiming success"
    assert adapter.cancel_calls == 1


def test_stopped_deployment_not_marked_enforced_when_cancel_fails(monkeypatch):
    adapter = _RecordingAdapter(fail_cancel=True)
    enforced: set[str] = set()

    async def _run() -> None:
        await daemon._enforce_stopped_deployments(
            [_stopped_deployment()], {"acc-1": adapter}, already_enforced=enforced
        )

    asyncio.run(_run())

    assert enforced == set()
    assert adapter.flatten_calls == 0, "must not flatten when cancel failed"


def test_stopped_deployment_without_adapter_is_a_noop(monkeypatch):
    enforced: set[str] = set()

    async def _run() -> None:
        await daemon._enforce_stopped_deployments(
            [_stopped_deployment()], {}, already_enforced=enforced
        )

    asyncio.run(_run())

    assert "44444444-4444-4444-8444-444444444444" in enforced


def test_paused_deployment_is_never_flattened(monkeypatch):
    """PAUSE must never liquidate: only STOPPED triggers cancel/flatten."""
    from shared.models import LiveDeploymentConfig as _Config

    adapter = _RecordingAdapter()
    enforced: set[str] = set()
    paused = _Config(
        id="55555555-5555-4555-8555-555555555555",
        strategy_name="demo",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="PAUSED",
        signal_source="internal",
    )

    async def _run() -> None:
        await daemon._enforce_stopped_deployments(
            [paused], {"acc-1": adapter}, already_enforced=enforced
        )

    asyncio.run(_run())

    assert adapter.cancel_calls == 0
    assert adapter.flatten_calls == 0
    assert enforced == set()
