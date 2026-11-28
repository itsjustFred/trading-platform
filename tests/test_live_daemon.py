import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

import live_runner.daemon as daemon
from live_runner.ctrader_adapter import CTraderAuthTerminalError
from shared.models import LiveAccountConfig, LiveDeploymentConfig


@dataclass(slots=True)
class _FailingAdapter:
    connect_calls: int = 0
    disconnect_calls: int = 0
    demo: bool = True

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


def test_ensure_adapter_for_active_account_blocks_ctrader_terminal_auth(monkeypatch):
    daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS.clear()
    daemon._CTRADER_AUTH_BLOCKED_REASONS.clear()

    @dataclass(slots=True)
    class _TerminalFailAdapter:
        connect_calls: int = 0
        disconnect_calls: int = 0
        demo: bool = True

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
        signal_source="internal",
        signal_config=signal_config,
        settings=settings or {},
    )

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
        active_adapters = {"acc-1": adapter}
        active_tasks = {"acc-1": sleeper}

        await daemon._rotate_changed_ctrader_adapters(
            {"acc-1"},
            active_adapters,
            active_tasks,
        )
        await asyncio.sleep(0)

        assert active_adapters == {}
        assert active_tasks == {}
        assert adapter.disconnect_calls == 1
        assert sleeper.cancelled()

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


def test_balance_polling_fans_out_across_accounts(monkeypatch) -> None:
    """B2: one slow broker must not serialize all balance snapshots."""
    import contextlib

    import live_runner.daemon as daemon_module

    completions: list[str] = []

    class _SlowAdapter:
        _connected = True

        async def get_balance(self):
            await asyncio.sleep(0.5)
            completions.append("slow")
            return 100_000.0, 100_000.0

        async def get_equity(self):
            return 100_000.0

    class _FastAdapter:
        _connected = True

        async def get_balance(self):
            completions.append("fast")
            return 100_000.0, 100_000.0

        async def get_equity(self):
            return 100_000.0

    monkeypatch.setattr(daemon_module, "save_live_balance", lambda _record: True)

    async def _one_tick() -> None:
        task = asyncio.create_task(
            daemon_module._poll_balances(
                {"slow-acc": _SlowAdapter(), "fast-acc": _FastAdapter()},
                interval_seconds=60.0,
            )
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_one_tick())
    # Gathered: fast completes while slow is still sleeping. Sequential
    # would order slow first (dict order) — or never reach fast at all.
    assert completions and completions[0] == "fast"


# ── ACTIVE deployments nothing can trade (#242) ──────────────────────────────


def _internal_deployment(
    status: str = "ACTIVE",
    *,
    strategy_name: str = "internal::ramjet",
    settings: dict | None = None,
) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id="22222222-2222-2222-2222-222222222222",
        strategy_name=strategy_name,
        account_id="acc-1",
        symbols=["AAPL"],
        timeframe="1h",
        status=status,
        signal_source="internal",
        signal_source_type="internal",
        signal_config={},
        settings=settings or {},
    )


def _stored_discord_deployment(status: str = "ACTIVE") -> LiveDeploymentConfig:
    """A discord deployment as the DB read path rehydrates it.

    Discord signal ingestion was removed, so the model refuses the source on a
    NEW deployment; stored rows still carry it and load through
    ``STORED_ROW_CONTEXT`` - the daemon then skips them with a retired-source
    warning.
    """
    from shared.models import STORED_ROW_CONTEXT

    return LiveDeploymentConfig.model_validate(
        {
            "id": "33333333-3333-3333-3333-333333333333",
            "strategy_name": "discord::nitro",
            "account_id": "acc-1",
            "symbols": ["AAPL"],
            "timeframe": "1h",
            "status": status,
            "signal_source": "discord",
            "signal_source_type": "discord",
            "signal_config": {"channel_id": "channel-1"},
        },
        context={STORED_ROW_CONTEXT: True},
    )


def test_active_internal_deployment_logs_error_naming_it(monkeypatch) -> None:
    errors: list[tuple] = []
    monkeypatch.setattr(
        daemon,
        "logger",
        SimpleNamespace(error=lambda msg, *args, **kwargs: errors.append((msg, args, kwargs))),
    )

    daemon._warn_untradeable_active_deployments([_internal_deployment()])

    assert len(errors) == 1
    msg, args, kwargs = errors[0]
    assert "cannot trade" in msg
    assert "22222222-2222-2222-2222-222222222222" in str(args)
    assert "internal" in str(args)
    data = kwargs["extra"]["data"]
    assert data["deployment_id"] == "22222222-2222-2222-2222-222222222222"
    assert data["signal_source"] == "internal"
    # No opt-in flag: the reason names that, not a missing runner (#348).
    assert "internal_runner_enabled" in data["untradeable_reason"]


def test_active_internal_deployment_opted_in_with_resolvable_strategy_is_not_warned(
    monkeypatch,
) -> None:
    """#348: an opted-in internal deployment whose strategy resolves trades."""
    errors: list[tuple] = []
    monkeypatch.setattr(
        daemon,
        "logger",
        SimpleNamespace(error=lambda msg, *args, **kwargs: errors.append((msg, args, kwargs))),
    )
    deployment = _internal_deployment(
        strategy_name="example_sma",
        settings={"internal_runner_enabled": True},
    )

    daemon._warn_untradeable_active_deployments([deployment])

    assert errors == []


def test_active_internal_deployment_opted_in_with_unresolvable_strategy_warns(
    monkeypatch,
) -> None:
    """Opted in but the strategy name resolves to nothing: still untradeable."""
    errors: list[tuple] = []
    monkeypatch.setattr(
        daemon,
        "logger",
        SimpleNamespace(error=lambda msg, *args, **kwargs: errors.append((msg, args, kwargs))),
    )
    deployment = _internal_deployment(
        strategy_name="risk_parity/risk_parity.py",
        settings={"internal_runner_enabled": True},
    )

    daemon._warn_untradeable_active_deployments([deployment])

    assert len(errors) == 1
    data = errors[0][2]["extra"]["data"]
    assert data["alert"] == "untradeable_active_deployment"
    assert "risk_parity/risk_parity.py" in data["untradeable_reason"]
    assert "cannot be resolved" in data["untradeable_reason"]


def test_active_internal_source_with_non_internal_source_type_warns(
    monkeypatch,
) -> None:
    """signal_source 'internal' + signal_source_type 'discord' is inert: the row
    matches no runner, so the untradeable warning names the mismatch."""
    errors: list[tuple] = []
    monkeypatch.setattr(
        daemon,
        "logger",
        SimpleNamespace(error=lambda msg, *args, **kwargs: errors.append((msg, args, kwargs))),
    )
    deployment = _internal_deployment(
        strategy_name="example_sma",
        settings={"internal_runner_enabled": True},
    )
    deployment.signal_source_type = "discord"

    daemon._warn_untradeable_active_deployments([deployment])

    assert len(errors) == 1
    assert "signal_source_type" in errors[0][2]["extra"]["data"]["untradeable_reason"]


def test_active_discord_and_non_active_deployments_are_not_warned(monkeypatch) -> None:
    """Discord rows are owned by the retired-source warning, not the
    untradeable-active alert, so this loop stays silent for them."""
    errors: list[tuple] = []
    monkeypatch.setattr(
        daemon,
        "logger",
        SimpleNamespace(error=lambda msg, *args, **kwargs: errors.append((msg, args, kwargs))),
    )

    daemon._warn_untradeable_active_deployments(
        [
            _internal_deployment("PAUSED"),
            _internal_deployment("STOPPED"),
            _stored_discord_deployment("ACTIVE"),
        ]
    )

    assert errors == []


def test_active_unknown_signal_source_is_reported_as_untradeable(monkeypatch) -> None:
    errors: list[tuple] = []
    monkeypatch.setattr(
        daemon,
        "logger",
        SimpleNamespace(error=lambda msg, *args, **kwargs: errors.append((msg, args, kwargs))),
    )
    unknown = _internal_deployment()
    unknown.signal_source = "webhook"

    daemon._warn_untradeable_active_deployments([unknown])

    assert len(errors) == 1
    assert "webhook" in str(errors[0][1])
    # #242 asks for an alert, not a log line. In this daemon the alert is the ERROR
    # record (Sentry's LoggingIntegration sends event_level=ERROR), so it is tagged.
    assert errors[0][2]["extra"]["data"]["alert"] == "untradeable_active_deployment"


def test_already_warned_reports_a_row_once_then_only_on_change(monkeypatch) -> None:
    """The suppression the sync loop relies on, pinned at the function itself.

    The loop tests drive it in situ; this is the contract in isolation, so a change
    to the dedupe key cannot pass the loop tests and break here.
    """
    errors: list[tuple] = []
    monkeypatch.setattr(
        daemon,
        "logger",
        SimpleNamespace(error=lambda msg, *args, **kwargs: errors.append((msg, args, kwargs))),
    )
    already_warned: dict[str, str] = {}
    deployment = _internal_deployment()

    daemon._warn_untradeable_active_deployments([deployment], already_warned)
    daemon._warn_untradeable_active_deployments([deployment], already_warned)

    assert len(errors) == 1
    reported_reason = errors[0][2]["extra"]["data"]["untradeable_reason"]
    assert already_warned == {str(deployment.id): reported_reason}

    # A different kind of untradeable (repointed to a source with no runner) is a
    # new condition, not a repeat of the one already reported.
    deployment.signal_source = "webhook"
    daemon._warn_untradeable_active_deployments([deployment], already_warned)
    daemon._warn_untradeable_active_deployments([deployment], already_warned)

    assert len(errors) == 2
    assert "webhook" in str(errors[1][1])


def test_already_warned_forgets_a_row_that_is_no_longer_untradeable(monkeypatch) -> None:
    """Stopping the row (or making it tradeable) must not silence a later repeat."""
    errors: list[tuple] = []
    monkeypatch.setattr(
        daemon,
        "logger",
        SimpleNamespace(error=lambda msg, *args, **kwargs: errors.append((msg, args, kwargs))),
    )
    already_warned: dict[str, str] = {}
    deployment = _internal_deployment()

    daemon._warn_untradeable_active_deployments([deployment], already_warned)

    deployment.status = "STOPPED"
    daemon._warn_untradeable_active_deployments([deployment], already_warned)

    assert already_warned == {}

    deployment.status = "ACTIVE"
    daemon._warn_untradeable_active_deployments([deployment], already_warned)

    assert len(errors) == 2


@dataclass
class _RecordingLogger:
    """The logger the daemon uses, keeping every level for an assertion."""

    errors: list[tuple] = field(default_factory=list)
    warnings: list[tuple] = field(default_factory=list)

    def error(self, msg, *args, **kwargs) -> None:
        self.errors.append((msg, args, kwargs))

    def warning(self, msg, *args, **kwargs) -> None:
        self.warnings.append((msg, args, kwargs))

    def info(self, msg, *args, **kwargs) -> None:
        pass

    def debug(self, msg, *args, **kwargs) -> None:
        pass


def _run_ticks_recorder(deployments, ticks: int, *, accounts=None, adapters=None, tasks=None):
    """Run the real deployment-sync loop for ``ticks`` ticks, returning the recorder.

    Only the DB read is stubbed (``_load_db_accounts_and_deployments``) and the
    enforcement ledger it seeds itself from; everything else -- the sync loop, the
    warning call inside it, the status observation -- is the code that ships. The
    loop is cancelled out of its sleep once ``ticks`` ticks have run.
    """
    recorder = _RecordingLogger()
    original_load = daemon._load_db_accounts_and_deployments
    original_sleep = daemon.asyncio.sleep
    original_logger = daemon.logger
    original_ledger = daemon.list_stopped_enforced_deployment_ids
    ticks_run = {"count": 0}

    async def _load_state():
        return list(accounts or []), deployments

    async def _sleep(_seconds: float) -> None:
        ticks_run["count"] += 1
        if ticks_run["count"] >= ticks:
            raise asyncio.CancelledError

    daemon._load_db_accounts_and_deployments = _load_state
    daemon.asyncio.sleep = _sleep
    daemon.logger = recorder
    daemon.list_stopped_enforced_deployment_ids = lambda: set()
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                daemon.sync_deployments_loop(
                    adapters if adapters is not None else {},
                    tasks if tasks is not None else {},
                    accounts_env=Path("/nonexistent/accounts.env"),
                    interval_seconds=0,
                )
            )
    finally:
        daemon._load_db_accounts_and_deployments = original_load
        daemon.asyncio.sleep = original_sleep
        daemon.logger = original_logger
        daemon.list_stopped_enforced_deployment_ids = original_ledger
    return recorder


def _run_ticks(deployments, ticks: int):
    return _run_ticks_recorder(deployments, ticks).errors


def _untradeable_alerts(errors):
    """Only the #242 alerts, not every ERROR the loop can log."""
    return [
        error
        for error in errors
        if error[2]["extra"]["data"].get("alert") == "untradeable_active_deployment"
    ]


def test_sync_loop_alerts_an_untradeable_active_deployment() -> None:
    """#242 blocker: the warning has to be reached from the loop, not only unit-tested.

    The rows the daemon trades come from this loop's DB read, so a helper nothing
    calls leaves an ACTIVE internal deployment looking live in the log for ever.
    """
    deployment = _internal_deployment()

    alerts = _untradeable_alerts(_run_ticks([deployment], ticks=2))

    assert len(alerts) == 1
    data = alerts[0][2]["extra"]["data"]
    assert data["deployment_id"] == str(deployment.id)
    assert data["account_id"] == "acc-1"
    assert data["signal_source"] == "internal"
    assert "internal_runner_enabled" in data["untradeable_reason"]


def test_the_same_untradeable_row_is_not_alerted_on_every_tick() -> None:
    """A loop that polls for days must not repeat a condition that has not changed."""
    assert len(_untradeable_alerts(_run_ticks([_internal_deployment()], ticks=3))) == 1


def test_a_stopped_or_paused_row_is_not_alerted() -> None:
    assert _untradeable_alerts(_run_ticks([_internal_deployment("PAUSED")], ticks=2)) == []
    assert _untradeable_alerts(_run_ticks([_internal_deployment("STOPPED")], ticks=2)) == []


def test_a_retired_discord_row_is_not_alerted_as_untradeable() -> None:
    assert _untradeable_alerts(_run_ticks([_stored_discord_deployment()], ticks=2)) == []


def test_an_opted_in_internal_deployment_with_a_resolvable_strategy_is_not_alerted() -> None:
    """#348: the internal strategy runner trades this row, so it is not untradeable."""
    deployment = _internal_deployment(
        strategy_name="example_sma",
        settings={"internal_runner_enabled": True},
    )
    assert _untradeable_alerts(_run_ticks([deployment], ticks=2)) == []


def test_activating_a_row_alerts_it_once() -> None:
    """A row that becomes untradeable is a new condition, not a repeat of an old one."""
    deployment = _internal_deployment("PAUSED")
    assert _untradeable_alerts(_run_ticks([deployment], ticks=2)) == []

    deployment.status = "ACTIVE"
    alerts = _untradeable_alerts(_run_ticks([deployment], ticks=3))

    assert len(alerts) == 1
    assert alerts[0][2]["extra"]["data"]["signal_source"] == "internal"


def test_repointing_a_row_alerts_the_change_and_the_stop() -> None:
    deployment = _internal_deployment()
    assert len(_untradeable_alerts(_run_ticks([deployment], ticks=2))) == 1

    # Repointed to the source the daemon runs: the alert stops, and the row stays
    # quiet afterwards rather than being re-reported a tick later.
    deployment.signal_source = "discord"
    deployment.signal_source_type = "discord"
    assert _untradeable_alerts(_run_ticks([deployment], ticks=2)) == []

    # Moved back to a source with no runner: alerted again, once.
    deployment.signal_source = "internal"
    deployment.signal_source_type = "internal"
    assert len(_untradeable_alerts(_run_ticks([deployment], ticks=2))) == 1


def test_retired_discord_warning_fires_once_per_deployment_per_process() -> None:
    """M3: one WARNING per stored discord deployment, not one per sync tick."""
    deployments = [
        _stored_discord_deployment("PAUSED"),
        _stored_discord_deployment("PAUSED").model_copy(
            update={"id": "44444444-4444-4444-4444-444444444444"}
        ),
    ]

    recorder = _run_ticks_recorder(deployments, ticks=3)

    retired_warnings = [
        w for w in recorder.warnings if "retired 'discord' signal source" in str(w[0])
    ]
    assert len(retired_warnings) == 2
    warned_ids = {
        w[2]["extra"]["data"]["deployment_id"] for w in retired_warnings
    }
    assert warned_ids == {str(d.id) for d in deployments}


class _SpyAdapter:
    """Records every broker-facing call the sync loop can make.

    The method names are the daemon's real mutation surface
    (``cancel_open_orders``, ``close_all_positions`` gated on
    ``supports_flatten``, ``cancel_orphaned_protections`` and the kill-switch
    flatten): a regression that routes a retired row into enforcement calls
    one of these and is caught here instead of raising AttributeError into a
    log line.
    """

    demo = True
    supports_flatten = True

    def __init__(self) -> None:
        self.mutations: list[str] = []
        self.listener_started = False

    async def listen_executions(self) -> None:
        self.listener_started = True

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def get_positions(self):
        return []

    async def working_order_ids(self):
        return set()

    async def submit_order(self, *args, **kwargs):
        self.mutations.append("submit_order")

    async def cancel_order(self, *args, **kwargs):
        self.mutations.append("cancel_order")

    async def flatten_positions(self, *args, **kwargs):
        self.mutations.append("flatten_positions")

    async def cancel_open_orders(self, *args, **kwargs):
        self.mutations.append("cancel_open_orders")

    async def close_all_positions(self, *args, **kwargs):
        self.mutations.append("close_all_positions")

    async def cancel_orphaned_protections(self, *args, **kwargs):
        self.mutations.append("cancel_orphaned_protections")

    async def cancel_working_orders(self, *args, **kwargs):
        self.mutations.append("cancel_working_orders")

    async def amend_protective_stop(self, *args, **kwargs):
        self.mutations.append("amend_protective_stop")


def test_paused_retired_discord_rows_get_no_broker_mutation_on_a_sync_tick() -> None:
    """B1 core + B3: three stored discord rows (the real shape: PAUSED,
    signal_source='discord', signal_source_type='discord', channel_id in
    signal_config) on a sync tick place no order, do no cancel/flatten - the
    account keeps its adapter and listener exactly as on main."""
    from shared.models import LiveAccountConfig

    deployments = [
        _stored_discord_deployment("PAUSED"),
        _stored_discord_deployment("PAUSED").model_copy(
            update={"id": "55555555-5555-5555-5555-555555555555"}
        ),
        _stored_discord_deployment("PAUSED").model_copy(
            update={"id": "66666666-6666-6666-6666-666666666666"}
        ),
    ]
    account = LiveAccountConfig(
        id="acc-1", broker="alpaca", name="Paper", broker_login="", is_demo=True
    )
    adapter = _SpyAdapter()
    adapters = {"acc-1": adapter}

    recorder = _run_ticks_recorder(
        deployments, ticks=2, accounts=[account], adapters=adapters, tasks={}
    )

    assert adapter.mutations == []
    # A failed enforcement attempt would surface as an ERROR log line even if
    # the spy missed the call; assert the loop stayed quiet too.
    failure_lines = [
        entry
        for entry in recorder.errors
        if any(
            token in str(entry[0]).lower()
            for token in ("failed", "flatten", "cancel", "close")
        )
    ]
    assert failure_lines == []
    # The adapter and its execution listener are retained (same as main): the
    # rows are recognised, listed and enforced like any other - they simply
    # have no signal source left to dispatch from.
    assert adapter.listener_started
