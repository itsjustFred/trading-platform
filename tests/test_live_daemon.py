import ast
import asyncio
import inspect
import logging
import textwrap
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from decimal import Decimal
from uuid import uuid4 as uuid4, UUID

import live_runner.daemon as daemon
import shared.database as database
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
    """A non-paper Alpaca adapter must not be registered, and this has to be visible.

    The assertion used to be `adapter.paper is False` on a value the test itself had
    just set, so it passed whether or not `_ensure_adapter_for_active_account`
    refused anything. The registry was passed as a literal `{}`, so the one thing
    worth asserting - that the adapter was not put in it - could not be observed.

    Production is correct: the paper check at the construction site returns before
    `active_adapters[account_id] = adapter`. This now proves it, and the companion
    case below proves the pair can fail in the other direction, so neither test
    passes merely because the function returned early for any reason.
    """
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
    active_adapters: dict = {}

    asyncio.run(
        daemon._ensure_adapter_for_active_account(
            "acc-paper",
            {"acc-paper": account},
            active_adapters,
            accounts_env_values={},
        )
    )

    assert "acc-paper" not in active_adapters, (
        "a non-paper Alpaca adapter was registered; the refusal happens at the "
        "construction site, before the assignment"
    )
    assert active_adapters == {}


def test_ensure_adapter_registers_a_paper_alpaca_adapter(monkeypatch):
    """The other half of the pair, so the refusal test above is not vacuous.

    Without this, a change that made `_ensure_adapter_for_active_account` return
    early for *every* account would keep the refusal test green.
    """
    account = LiveAccountConfig(
        id="acc-paper",
        broker="alpaca",
        name="Paper",
        is_demo=True,
    )
    adapter = SimpleNamespace(paper=True, connect=None, disconnect=None)
    monkeypatch.setattr(
        daemon, "_create_adapter_for_account", lambda *_args, **_kwargs: adapter
    )
    active_adapters: dict = {}

    async def _connect() -> None:
        return None

    adapter.connect = _connect
    monkeypatch.setattr(daemon, "_adapter_is_ready", lambda _a: True)

    asyncio.run(
        daemon._ensure_adapter_for_active_account(
            "acc-paper",
            {"acc-paper": account},
            active_adapters,
            accounts_env_values={},
        )
    )

    assert active_adapters.get("acc-paper") is adapter, (
        "a paper Alpaca adapter must be registered, otherwise the refusal test "
        "proves nothing"
    )


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
    strategy_name: str = "fx_breakout",
) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        strategy_name=strategy_name,
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


def _patch_dispatch_guards(monkeypatch, wait_impl):
    """Satisfy every guard `_dispatch_discord_trade_signal` checks *before* the
    candle-wait branch these tests are about.

    Without them `_deployment_is_currently_active` reads the real database, the
    synthetic deployment has no row, it returns False, and the loop `continue`s
    before the branch under test. Both tests here were in exactly that state: they
    asserted on a submission list that could never be filled, so the branch they
    named was dead code as far as the suite was concerned.

    The stub signature matters too. The real call passes `trade_signal`
    *positionally* (daemon.py:2031), so the keyword-only stub these tests used
    would raise TypeError the moment that line ran - which is the proof it never
    did.
    """
    monkeypatch.setattr(daemon, "_deployment_is_currently_active", lambda d: True)
    monkeypatch.setattr(
        daemon, "_deployment_matches_stockkit_route", lambda *a, **k: True
    )
    monkeypatch.setattr(
        daemon, "_deployment_covers_discord_symbol", lambda *a, **k: True
    )
    monkeypatch.setattr(
        daemon, "_deployment_accepts_signal_author", lambda *a, **k: True
    )
    monkeypatch.setattr(daemon, "_deployment_trusted_author_ids", lambda d: {"a"})
    monkeypatch.setattr(daemon, "_deployment_wait_for_candle_close", wait_impl)


def test_dispatch_drops_only_the_misconfigured_deployment_on_a_typo_bool(
    monkeypatch,
) -> None:
    """A typo'd wait boolean skips that deployment and no other.

    This test asserted the *opposite* of the codebase's recorded decision, and only
    passed because it never executed. Its docstring said the signal "must NOT fall
    through to a sibling deployment on the same channel", while the code it names
    carries a comment explaining that `return` there was a real bug: one deployment's
    config typo silently stopped every *later* deployment on the channel, each still
    reporting ACTIVE and connected. A sibling test 100 lines down asserts the correct
    behaviour, with all four guards patched and a correctly-shaped stub.

    So the fix is to make this test assert what the code is supposed to do: the
    misconfigured deployment is skipped, its sibling still trades, and the typo never
    becomes an immediate market order. Fail-closed without fail-over.
    """
    submitted: list[str] = []
    bad = _deployment_with_signal_config(
        {"wait_candle_close": "ture"}, strategy_name="typo_deployment"
    )
    good = _deployment_with_signal_config(
        {"wait_candle_close": "true"},
        settings={"signal_processor": "other"},
        strategy_name="healthy_deployment",
    )

    def _wait(deployment, _env):
        if deployment.strategy_name == "typo_deployment":
            raise ValueError("wait_for_candle_close must be a boolean")
        return True

    _patch_dispatch_guards(monkeypatch, _wait)

    # Positional `_signal`, matching the real call site.
    async def _record_submit(_account_id, _adapter, deployment, _signal, **_kwargs):
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

    assert submitted == ["healthy_deployment"], (
        f"expected only the healthy deployment to trade, got {submitted}. A config "
        f"typo must skip its own deployment and must not stop the others - that was "
        f"the bug `continue` was chosen over `return` to fix."
    )


def test_dispatch_typo_bool_does_not_raise_to_listener_loop(monkeypatch) -> None:
    bad = _deployment_with_signal_config({"wait_candle_close": "ture"})

    def _wait(_deployment, _env):
        raise ValueError("wait_for_candle_close must be a boolean")

    _patch_dispatch_guards(monkeypatch, _wait)

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
        # Merge resolution: the fixture used `action="buy"`, but the conversion
        # site only evaluates `allow_short` for a sell/short, so the typo was
        # never parsed and the assertion was vacuous. Use the action the check
        # actually applies to.
        action="sell",
        symbol="EURUSD",
        entry_price=1.1,
        stop_loss=1.0,
        take_profit=1.2,
        timeframe="15m",
        # `signal_is_fresh` is fail-closed on a missing `received_at`, and the
        # real Discord dispatch path always stamps one at receipt. Without it
        # this fixture is discarded as stale before any gate below runs.
        metadata={"received_at": datetime.now(timezone.utc).isoformat()},
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
        # Merge resolution: `signal_is_fresh` is fail-closed on a missing
        # `received_at`, and the real Discord dispatch path always stamps one at
        # receipt. Without it these fixtures are discarded as stale before any
        # gate below runs, which would make the assertions vacuous.
        metadata={"received_at": datetime.now(timezone.utc).isoformat()},
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
        # Merge resolution: `signal_is_fresh` is fail-closed on a missing
        # `received_at`, and the real Discord dispatch path always stamps one at
        # receipt. Without it these fixtures are discarded as stale before any
        # gate below runs, which would make the assertions vacuous.
        metadata={"received_at": datetime.now(timezone.utc).isoformat()},
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
        # Merge resolution: `signal_is_fresh` is fail-closed on a missing
        # `received_at`, and the real Discord dispatch path always stamps one at
        # receipt. Without it these fixtures are discarded as stale before any
        # gate below runs, which would make the assertions vacuous.
        metadata={"received_at": datetime.now(timezone.utc).isoformat()},
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


def test_a_slow_account_does_not_prevent_the_others_being_attempted(monkeypatch) -> None:
    """Formerly `test_connect_active_accounts_runs_concurrently`, which required simultaneous flight.

    That test used an Event barrier: each account's connect waited until the other's had started, so
    it could only pass if both were in flight at the same instant. Simultaneous connection is precisely
    what the broker refuses - cTrader authenticates against the application, so several handshakes on
    one credential set produce `(15, 'Deferred')`, a request reaching the socket with nothing coming
    back. Measured against five demo accounts: all five queried at once failed for every account after
    the first, and all five succeeded when spread out.

    So the accounts are now staggered by `CONNECT_STAGGER_SECONDS`. What still has to hold - and what
    this test now asserts - is that a slow or hanging account does not stop the others being
    *attempted*. The connects remain gathered with `return_exceptions=True`, so an exception on one
    account cannot fail the tick, and the stagger delays a start rather than serialising completion.
    """
    attempted: list[str] = []

    async def _run() -> None:
        release = asyncio.Event()

        async def _ensure(account_id, *_args, **_kwargs):
            attempted.append(account_id)
            if account_id == "slow":
                # The account that never finishes on its own.
                await release.wait()
            return account_id

        monkeypatch.setattr(daemon, "_ensure_adapter_for_active_account", _ensure)
        # No pacing delay, so the assertion is about isolation and not about timing.
        monkeypatch.setattr(daemon, "_connect_stagger_seconds", lambda: 0.0)

        task = asyncio.ensure_future(
            daemon._connect_active_accounts(
                {"slow", "fast"}, {}, {}, accounts_env_values={}
            )
        )
        # 'fast' must be attempted even though 'slow' is still hanging.
        for _ in range(200):
            if "fast" in attempted:
                break
            await asyncio.sleep(0.01)
        release.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(_run())
    assert set(attempted) == {"slow", "fast"}, (
        f"both accounts must be attempted despite one hanging, got {attempted}"
    )



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


def test_discord_poll_does_not_claim_live_signal_before_deployment_routing(
    isolated_db, monkeypatch
):
    """The execution adapter is the sole authority for the persisted order intent.

    Persisting at the source layer creates a canonical Discord signal with an empty
    deployment id. The cTrader adapter then mistakes the first routed delivery for
    crash recovery and fail-closes instead of submitting the first broker order.

    **`isolated_db` is load-bearing, and its absence was a real defect in this test.**
    Without it the assertion below reads the *production* database, so the test asserted
    "the live_signals table is empty everywhere" rather than "this poll persisted
    nothing". It passed only while production happened to hold no signal rows; once it
    held seven - real rows from April to 2026-09-25, unrelated to this code - the test
    failed with no change to any of its subject.

    Worse than a false failure: had the regression this test guards actually been
    reintroduced, the poll would have **written a Discord execution intent into the
    production database** in the course of asserting that it must not. A test that
    verifies a no-write property by touching the real store is not a safe place for that
    property to be checked.
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

    source._fetch_messages = _fetch

    asyncio.run(source._poll_once("channel-1"))

    queued = source._queue.get_nowait()
    assert queued.metadata["discord_message_id"] == "discord-123"

    # The property itself, checked rather than trusted to a tripwire.
    #
    # This test used to patch `save_live_signal` onto the source module with
    # `raising=False`, as a guard for a hypothetical future re-import. That patch
    # could never fire: the module does not import the name, so `raising=False`
    # simply *created* an attribute nothing reads. The only true no-op patch of its
    # kind in the suite, and the comment above it described an intent the code did
    # not implement.
    #
    # So the effect is asserted instead. `list_live_signals_for_replay` reads the
    # table the removed helper wrote to, so an empty result is proof no execution
    # intent was persisted - and it holds regardless of how a future regression
    # chooses to import the writer, which is the point a name-based patch cannot
    # offer.
    from shared.database import list_live_signals_for_replay

    assert list_live_signals_for_replay() == [], (
        "polling persisted an execution intent; the execution adapter must be the "
        "sole authority for a persisted order"
    )


def test_stockkit_route_guard_rejects_mismatched_signal_metadata() -> None:
    # account_id is a local UUID; the Alpaca account number is broker identity
    # and is supplied separately, because comparing a UUID against an account
    # number is comparing two different identifier spaces.
    ramjet_number = "PA3T0MQAGQ51"
    deployment = LiveDeploymentConfig(
        strategy_name="discord::ramjet",
        account_id="59d94c55-f6d3-4c38-ba4d-ec1e22abdbcf",
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="discord",
        signal_source_type="discord",
        signal_source_id="RAMJET",
        signal_config={"signal_processor": "ramjet"},
    )
    route_env = {"ALPACA_ACCOUNT_NAME_ACC2": "RamJet", "ALPACA_ACCOUNT_NUMBER_ACC2": ramjet_number}

    assert daemon._deployment_matches_stockkit_route(
        deployment,
        {"discord_bot_name": "RAMJET"},
        account_broker_login=ramjet_number,
        accounts_env_values=route_env,
    )
    assert not daemon._deployment_matches_stockkit_route(
        deployment,
        {"discord_bot_name": "NITRO"},
        account_broker_login=ramjet_number,
        accounts_env_values=route_env,
    )
    # Correctly routed account, but the deployment sits on the wrong broker
    # account: must be refused rather than dispatched.
    assert not daemon._deployment_matches_stockkit_route(
        deployment,
        {"discord_bot_name": "RAMJET"},
        account_broker_login="PA-SOME-OTHER-ACCOUNT",
        accounts_env_values=route_env,
    )


# ── Cleanup 15-A: PAUSE must stop live trading from stale snapshots ───────────


class _SignalSubmittingAdapter:
    def __init__(self) -> None:
        self.submitted: list[object] = []

    async def connect(self) -> None:
        return None

    async def submit_order(self, signal, *, qty: float | None = None) -> str:
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
        metadata={
            "discord_channel_id": "channel-1",
            # Fail-closed freshness (B2): real ingestion always stamps
            # received_at; fixtures must carry a fresh stamp to dispatch.
            "received_at": datetime.now(timezone.utc).isoformat(),
        },
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

    class _FastAdapter:
        _connected = True

        async def get_balance(self):
            completions.append("fast")
            return 100_000.0, 100_000.0

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
# ─── STOPPED semantics: cancel resting orders and flatten ────────────────────


class _RecordingAdapter:
    """Minimal adapter that records cancel/flatten calls."""

    def __init__(
        self,
        *,
        supports_cancel: bool = True,
        supports_flat: bool = True,
        fail_cancel: bool = False,
        orphan_sweep: int | None = 0,
    ) -> None:
        self._supports_cancel = supports_cancel
        self._supports_flat = supports_flat
        self._fail_cancel = fail_cancel
        self._orphan_sweep = orphan_sweep
        self.cancel_calls = 0
        self.flatten_calls = 0
        self.orphan_calls = 0
        self.call_order: list[str] = []

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
        self.call_order.append("cancel")
        if self._fail_cancel:
            raise RuntimeError("broker rejected cancel")
        return 2

    async def close_all_positions(self) -> int:
        if not self._supports_flat:
            raise NotImplementedError("no flatten support")
        self.flatten_calls += 1
        self.call_order.append("flatten")
        return 1

    async def cancel_orphaned_protections(self) -> int:
        self.orphan_calls += 1
        self.call_order.append("orphan_sweep")
        if self._orphan_sweep is None:
            raise NotImplementedError("cannot identify orphans")
        return self._orphan_sweep


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


def test_stopped_deployment_sweeps_protections_orphaned_by_the_flatten():
    """The blanket cancel leaves a live position's stop alone, so the flatten is
    what orphans it. Nothing else re-cancels afterwards, so the protection would
    rest on a flat account forever unless the sweep runs -- and it must run AFTER
    the flatten, while the position is already gone."""
    adapter = _RecordingAdapter(orphan_sweep=3)
    enforced: set[str] = set()

    asyncio.run(
        daemon._enforce_stopped_deployments(
            [_stopped_deployment()], {"acc-1": adapter}, already_enforced=enforced
        )
    )

    assert adapter.orphan_calls == 1, (
        "the protection detached by the flatten is never removed by any other "
        "call, so the account keeps a resting order nobody can explain"
    )
    assert adapter.call_order.index("orphan_sweep") > adapter.call_order.index(
        "flatten"
    ), "sweeping before the flatten would only re-cancel live protections"
    assert "44444444-4444-4444-8444-444444444444" in enforced, (
        "an unswept orphan is a cleanliness problem, not a safety one: the "
        "positions are already closed, so it must not block the record"
    )


def test_stopped_deployment_is_still_enforced_when_the_orphan_sweep_is_unsupported():
    adapter = _RecordingAdapter(orphan_sweep=None)
    enforced: set[str] = set()

    asyncio.run(
        daemon._enforce_stopped_deployments(
            [_stopped_deployment()], {"acc-1": adapter}, already_enforced=enforced
        )
    )

    assert adapter.orphan_calls == 1
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


def test_stopped_deployment_not_marked_enforced_when_adapter_cannot_cancel(monkeypatch):
    """An adapter that cannot cancel must NOT be reported as stopped-flat.

    "Cannot cancel" is UNKNOWN, not zero. The ledger check is `is not None`, so
    a `0` substituted for "unsupported" recorded the deployment as enforced
    while its resting entry orders were still live at the broker -- skipping
    the flatten for a deployment the operator had stopped.
    """
    adapter = _RecordingAdapter(supports_cancel=False)
    enforced: set[str] = set()

    async def _run() -> None:
        await daemon._enforce_stopped_deployments(
            [_stopped_deployment()], {"acc-1": adapter}, already_enforced=enforced
        )

    asyncio.run(_run())

    assert enforced == set(), "unsupported cancel is not a successful cancel"
    assert adapter.cancel_calls == 0
    assert adapter.flatten_calls == 0, (
        "must not proceed to flatten once the cancel step is known-unknown"
    )

    # The same adapter, once it does support cancel, is enforced and recorded.
    supported = _RecordingAdapter()
    asyncio.run(
        daemon._enforce_stopped_deployments(
            [_stopped_deployment()], {"acc-1": supported}, already_enforced=enforced
        )
    )
    assert "44444444-4444-4444-8444-444444444444" in enforced


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


def test_stopped_deployment_without_adapter_is_retried_not_marked_enforced(monkeypatch):
    """No adapter does NOT mean "nothing is open".

    `active_adapters` is empty for every account right after a daemon restart, so
    recording a stopped deployment as enforced here would permanently skip the
    cancel/flatten for a deployment that really does still hold positions. It must
    stay out of the ledger so the next sync retries once a connection exists.
    """
    enforced: set[str] = set()

    async def _run() -> None:
        await daemon._enforce_stopped_deployments(
            [_stopped_deployment()], {}, already_enforced=enforced
        )

    asyncio.run(_run())

    assert "44444444-4444-4444-8444-444444444444" not in enforced

    # Once an adapter exists, the stop is enforced and only then recorded.
    adapter = _RecordingAdapter()
    asyncio.run(
        daemon._enforce_stopped_deployments(
            [_stopped_deployment()], {"acc-1": adapter}, already_enforced=enforced
        )
    )
    assert adapter.cancel_calls == 1
    assert adapter.flatten_calls == 1
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


def test_stopped_deployment_refuses_to_flatten_a_shared_active_account():
    """cancel/flatten are account-wide.

    If another deployment on the same account is still ACTIVE, flattening would
    liquidate its positions as collateral damage. The stop must be refused and
    left un-enforced so it is retried, never reported as honoured.
    """
    from shared.models import LiveDeploymentConfig as _Config

    stopped = _stopped_deployment()
    other = _Config(
        id="66666666-6666-4666-8666-666666666666",
        strategy_name="other",
        account_id=stopped.account_id,
        symbols=["EURUSD"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="discord",
    )
    adapter = _RecordingAdapter()
    enforced: set[str] = set()

    async def _run() -> None:
        await daemon._enforce_stopped_deployments(
            [stopped, other], {"acc-1": adapter}, already_enforced=enforced
        )

    asyncio.run(_run())

    assert adapter.cancel_calls == 0, "must not cancel orders on a shared account"
    assert adapter.flatten_calls == 0, "must not flatten another deployment's positions"
    assert enforced == set(), "a refused stop must be retried, not marked honoured"


def test_stopped_deployment_flattens_when_the_account_is_exclusively_stopped():
    """The guard must not block the ordinary case where the stopped deployment is
    the only one on its account."""
    adapter = _RecordingAdapter()
    enforced: set[str] = set()

    async def _run() -> None:
        await daemon._enforce_stopped_deployments(
            [_stopped_deployment()], {"acc-1": adapter}, already_enforced=enforced
        )

    asyncio.run(_run())

    assert adapter.cancel_calls == 1
    assert adapter.flatten_calls == 1
    assert "44444444-4444-4444-8444-444444444444" in enforced


def test_auth_scheme_is_keyed_like_the_token_for_a_deployment_without_an_id():
    """Token and auth scheme must resolve against the same service name.

    `_resolve_account_discord_token` falls back to `signal_config["service_name"]`
    when `signal_source_id` is empty. Keying the scheme off `signal_source_id`
    alone meant such a deployment found its token but ignored the per-bot
    `DISCORD_BOT_<NAME>_AUTH_SCHEME` override, sending a `Bot` header for an
    account token and getting a 401.
    """
    from shared.models import LiveDeploymentConfig as _Config

    deployment = _Config(
        id="77777777-7777-4777-8777-777777777777",
        strategy_name="stockkit",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="PAUSED",
        signal_source="discord",
        signal_source_id="",
        signal_config={"service_name": "ramjet"},
    )
    values = {
        "DISCORD_BOT_RAMJET_TOKEN": "tok",
        "DISCORD_BOT_RAMJET_AUTH_SCHEME": "account",
        "DISCORD_AUTH_SCHEME": "bot",
    }
    token, scheme = daemon._resolve_account_discord_credentials(values, deployment)
    assert token == "tok"
    assert scheme == "account", (
        "the per-bot auth scheme must win over the global default, as it does for the token"
    )


def test_account_ids_needing_stopped_enforcement_covers_stopped_only_accounts():
    """A STOPPED-only account must be queued for an adapter.

    The adapter is what performs cancel/flatten, so without this the stop was
    logged and retried forever and never actually enforced.
    """
    stopped = _stopped_deployment()
    assert daemon._account_ids_needing_stopped_enforcement([stopped], set()) == {
        stopped.account_id
    }


def test_already_enforced_stopped_accounts_do_not_keep_an_adapter_alive():
    """Once flattened, the account is released so the adapter is not kept open."""
    stopped = _stopped_deployment()
    assert (
        daemon._account_ids_needing_stopped_enforcement(
            [stopped], {str(stopped.id)}
        )
        == set()
    )


def test_active_deployments_are_never_included_in_enforcement_accounts():
    """Only STOPPED deployments need enforcement; an ACTIVE one must not be
    connected on this basis, or it could be flattened by mistake."""
    from shared.models import LiveDeploymentConfig as _Config

    active = _Config(
        id="88888888-8888-4888-8888-888888888888",
        strategy_name="stockkit",
        account_id="acc-active",
        symbols=["EURUSD"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="discord",
    )
    assert daemon._account_ids_needing_stopped_enforcement([active], set()) == set()


def test_global_token_fallback_uses_the_global_auth_scheme(monkeypatch):
    """A global token is configured with the global scheme, not a per-bot one.

    Finding the token under `DISCORD_BOT_TOKEN` while resolving the scheme from
    the deployment's per-bot key sent an account header for a bot token, which
    is a 401.
    """
    from shared.models import LiveDeploymentConfig as _Config

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "global-token")
    monkeypatch.setenv("DISCORD_AUTH_SCHEME", "bot")
    deployment = _Config(
        id="99999999-9999-4999-8999-999999999999",
        strategy_name="stockkit",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="PAUSED",
        signal_source="discord",
        signal_source_id="",
        signal_config={"service_name": "ramjet"},
    )
    values = {
        "DISCORD_BOT_RAMJET_AUTH_SCHEME": "account",  # per-bot override
    }
    token, scheme = daemon._resolve_account_discord_credentials(values, deployment)
    assert token == "global-token", "must fall back to the global token"
    assert scheme == "bot", (
        f"a global token must use the global scheme, got {scheme!r}"
    )


def test_per_bot_token_uses_the_per_bot_auth_scheme(monkeypatch):
    from shared.models import LiveDeploymentConfig as _Config

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "global-token")
    monkeypatch.setenv("DISCORD_AUTH_SCHEME", "bot")
    deployment = _Config(
        id="99999999-9999-4999-8999-999999999999",
        strategy_name="stockkit",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="PAUSED",
        signal_source="discord",
        signal_source_id="",
        signal_config={"service_name": "ramjet"},
    )
    values = {
        "DISCORD_BOT_RAMJET_TOKEN": "ramjet-account-token",
        "DISCORD_BOT_RAMJET_AUTH_SCHEME": "account",
        "DISCORD_AUTH_SCHEME": "bot",
    }
    token, scheme = daemon._resolve_account_discord_credentials(values, deployment)
    assert token == "ramjet-account-token"
    assert scheme == "account", f"a per-bot token must use its per-bot scheme, got {scheme!r}"


def test_per_bot_scheme_falls_back_to_the_global_scheme(monkeypatch):
    from shared.models import LiveDeploymentConfig as _Config

    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    deployment = _Config(
        id="99999999-9999-4999-8999-999999999999",
        strategy_name="stockkit",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="PAUSED",
        signal_source="discord",
        signal_source_id="",
        signal_config={"service_name": "rocketfuel"},
    )
    values = {
        "DISCORD_BOT_ROCKETFUEL_TOKEN": "tok",
        "DISCORD_AUTH_SCHEME": "account",
    }
    token, scheme = daemon._resolve_account_discord_credentials(values, deployment)
    assert token == "tok"
    assert scheme == "account", "the global scheme applies when no per-bot override exists"


def test_stopped_deployment_refuses_to_flatten_an_account_with_a_paused_sibling():
    """PAUSED still owns positions, so it must block a wholesale flatten.

    Pausing drains and blocks new entries; it does not liquidate. Treating only
    ACTIVE as position-owning meant stopping one deployment would have closed a
    paused sibling's positions as collateral damage.
    """
    from shared.models import LiveDeploymentConfig as _Config

    stopped = _stopped_deployment()
    paused = _Config(
        id="aaaaaaaa-1111-4111-8111-111111111111",
        strategy_name="sibling",
        account_id=stopped.account_id,
        symbols=["EURUSD"],
        timeframe="1h",
        status="PAUSED",
        signal_source="internal",
    )
    adapter = _RecordingAdapter()
    enforced: set[str] = set()

    async def _run() -> None:
        await daemon._enforce_stopped_deployments(
            [stopped, paused], {"acc-1": adapter}, already_enforced=enforced
        )

    asyncio.run(_run())
    assert adapter.flatten_calls == 0, "must not flatten a paused sibling's positions"
    assert enforced == set(), "the stop stays un-enforced and is retried"


# ─── The demo-only gate itself ────────────────────────────────────────────────
#
# `_deployment_account_is_demo` is the last thing standing between a parsed
# Discord signal and a real order on a live-money account, and the fixture above
# stubs it out for every other test in this file. So the gate body itself needs
# its own tests: an unverified account must be refused, loudly, every time.

# Captured at import time, before the autouse fixture replaces the module
# attribute: the tests below must exercise the real gate, not the stub.
_REAL_DEPLOYMENT_ACCOUNT_IS_DEMO = daemon._deployment_account_is_demo

_DAEMON_LOGGER = logging.getLogger("trading.live-runner")


@contextmanager
def _daemon_log_records(*, level: int = logging.ERROR):
    """Collect records emitted by the daemon logger.

    `shared.logging.get_logger` sets `propagate = False`, so caplog (which
    hooks the root logger) only sees these records once some earlier test has
    happened to attach a handler to this logger. Attach our own for the
    duration of the block, as the Discord ingestion tests do, so the assertion
    cannot depend on test ordering.
    """
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= level:
                records.append(record)

    handler = _Capture(level=level)
    _DAEMON_LOGGER.addHandler(handler)
    try:
        yield records
    finally:
        _DAEMON_LOGGER.removeHandler(handler)


def _live_account(*, is_demo: bool) -> LiveAccountConfig:
    return LiveAccountConfig(
        id="acc-live", broker="ctrader", name="Live", is_demo=is_demo
    )


def _live_account_row(
    *,
    is_demo: bool,
    broker_login: str | None = None,
    ctid: str | None = None,
    verified_ago_seconds: float | None = 0.0,
    verified_as_broker_login: str | None = None,
    stored_fingerprint: str | None = None,
):
    """A stored account row, exactly as the database hands it to the gate.

    Built as a real ``LiveAccountModel`` and run through the real verdict
    function, so these tests exercise the production decision (demo flag,
    verification age, broker identity) instead of replacing it.
    """
    from datetime import timedelta

    import shared.database as database

    row = database.LiveAccountModel(
        id="acc-live",
        broker="ctrader",
        name="Live",
        is_demo=is_demo,
        broker_login=broker_login,
        ctid=ctid,
    )
    if verified_ago_seconds is not None:
        row.demo_verified_at = database._utcnow_naive() - timedelta(
            seconds=verified_ago_seconds
        )
        row.identity_fingerprint = database.live_account_identity_fingerprint(
            "ctrader",
            ctid=ctid,
            # What the broker confirmed at verification time, which is not
            # necessarily what the row says now.
            broker_login=(
                broker_login if verified_as_broker_login is None else verified_as_broker_login
            ),
        )
    if stored_fingerprint is not None:
        row.identity_fingerprint = stored_fingerprint
    return row


def _raise_account_lookup(_account_id: str) -> str:
    raise RuntimeError("database is unreachable")


@pytest.mark.parametrize(
    ("account_row", "expected_message"),
    [
        pytest.param(
            lambda: None,
            "account is unknown",
            id="account-row-missing",
        ),
        pytest.param(
            lambda: _live_account_row(is_demo=False),
            "account is non-demo and not verified as demo",
            id="account-is-not-demo",
        ),
        pytest.param(
            lambda: _live_account_row(is_demo=True, verified_ago_seconds=None),
            "account has no broker demo verification on record",
            id="never-verified-against-the-broker",
        ),
        pytest.param(
            lambda: _live_account_row(
                is_demo=True,
                verified_ago_seconds=database.DEMO_VERIFICATION_TTL_SECONDS + 60,
            ),
            "account demo verification is older than",
            id="verification-aged-out",
        ),
        pytest.param(
            lambda: _live_account_row(
                is_demo=True,
                broker_login="PA-LIVE-9",
                verified_as_broker_login="PA-DEMO-1",
            ),
            "account broker identity changed since it was verified as demo",
            id="credential-re-pointed-at-a-live-account",
        ),
        pytest.param(
            lambda: _live_account_row(is_demo=True, stored_fingerprint=""),
            "account has no broker identity fingerprint on record",
            id="fingerprint-missing",
        ),
    ],
)
def test_demo_gate_refuses_anything_it_cannot_verify_as_demo(
    monkeypatch, account_row, expected_message
) -> None:
    """Every unverifiable account is refused, and every refusal is logged.

    "I could not check" is not "it is fine": a missing row, an account the
    operator never marked as demo, a verification that has aged out, a
    credential re-pointed at another account, and a database error all have to
    fail closed, otherwise a real order routes to a live-money account.
    """
    from shared.database import live_account_verification_block_reason

    looked_up: list[str] = []

    def _account_verdict(account_id: str) -> str | None:
        looked_up.append(account_id)
        return live_account_verification_block_reason(account_row())

    monkeypatch.setattr(daemon, "live_account_submission_block_reason", _account_verdict)

    with _daemon_log_records() as records:
        assert _REAL_DEPLOYMENT_ACCOUNT_IS_DEMO("acc-live") is False

    assert looked_up == ["acc-live"], "the verdict must come from the stored row"
    assert records, "a refused dispatch must be logged, not silently dropped"
    assert any(expected_message in record.getMessage() for record in records), (
        f"the refusal must say why: {[r.getMessage() for r in records]}"
    )
    named = [getattr(record, "data", {}).get("account_id") for record in records]
    assert named == ["acc-live"], f"every refusal must name the account, got {named}"


def test_demo_gate_refuses_when_the_verdict_cannot_be_reached(monkeypatch) -> None:
    """An unreadable database is a refusal, not a pass.

    The gate must not treat "I could not check" as "it is fine": that is the
    difference between a missing row and a database that is down, and both have
    to stop the order.
    """
    monkeypatch.setattr(daemon, "live_account_submission_block_reason", _raise_account_lookup)

    with _daemon_log_records() as records:
        assert _REAL_DEPLOYMENT_ACCOUNT_IS_DEMO("acc-live") is False

    assert records, "an unreachable database must be logged, not silently dropped"
    assert any("verification failed" in record.getMessage() for record in records)
    named = [getattr(record, "data", {}).get("account_id") for record in records]
    assert named == ["acc-live"], f"every refusal must name the account, got {named}"


def test_demo_gate_allows_a_verified_demo_account(monkeypatch) -> None:
    """The control for the refusals: a verified demo account still trades.

    Without this, a gate hard-wired to refuse everything would pass them.
    """
    from shared.database import live_account_verification_block_reason

    def _account_verdict(account_id: str) -> str | None:
        return live_account_verification_block_reason(
            _live_account_row(is_demo=True, broker_login="PA-DEMO-1")
        )

    monkeypatch.setattr(daemon, "live_account_submission_block_reason", _account_verdict)

    with _daemon_log_records() as records:
        assert _REAL_DEPLOYMENT_ACCOUNT_IS_DEMO("acc-live") is True

    assert records == [], f"a verified demo account must not log an error: {records}"


def test_submission_path_runs_the_real_demo_gate_and_refuses_a_live_account(
    monkeypatch,
) -> None:
    """End to end: the real gate, a live-money row, and no broker call.

    The other submission tests here stub the gate out, so the wiring between
    `_deployment_account_is_demo` and `_submit_normalized_discord_signal` is
    only pinned when the real one runs.
    """
    deployment = _deployment_with_signal_config({})
    released: list[str] = []
    submitted: list[object] = []

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
        # `signal_is_fresh` is fail-closed on a missing `received_at`, and the
        # real Discord dispatch path always stamps one at receipt. Without it
        # this fixture is discarded as stale before any gate below runs.
        metadata={"received_at": datetime.now(timezone.utc).isoformat()},
    )

    monkeypatch.setattr(daemon, "_deployment_is_currently_active", lambda _deployment: True)
    # The real gate, not the autouse stub.
    monkeypatch.setattr(
        daemon, "_deployment_account_is_demo", _REAL_DEPLOYMENT_ACCOUNT_IS_DEMO
    )
    # The storage seam the real gate reads, not the gate itself: a live-money row
    # and the production verdict computed from it.
    monkeypatch.setattr(
        daemon,
        "live_account_submission_block_reason",
        lambda account_id: database.live_account_verification_block_reason(
            _live_account_row(is_demo=False)
        ),
    )
    monkeypatch.setattr(
        daemon, "release_live_deployment_submission", lambda token: released.append(token)
    )

    async def _record_submit(_adapter, _deployment_arg, live_signal):
        submitted.append(live_signal)

    monkeypatch.setattr(daemon, "submit_order_with_risk", _record_submit)

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

    assert submitted == [], "a live-money account must never reach the broker"
    assert released == [], (
        "the refusal happens before the submission claim is taken, so nothing "
        f"is released: {released}"
    )


# ─── The gate and the stop-enforcement log, against a real database ───────────
#
# The tests above drive the gate through a stubbed storage seam. These drive it
# through the real one, because the properties that matter here are properties
# of the stored row: an aged-out verification, a fingerprint that no longer
# matches, and an account that can never be given an adapter at all.


def test_dispatch_gate_refuses_against_the_real_stored_row(isolated_db, monkeypatch) -> None:
    """The real gate, the real verdict function, a real sandboxed row.

    Nothing is stubbed below `daemon._deployment_account_is_demo`, so this is the
    wiring that decides whether a real order reaches a real account.
    """
    database_module, _engine, _data_dir = isolated_db
    database_module.ensure_live_account(
        "acc-live", "alpaca", is_demo=True, broker_login="PA-DEMO-1"
    )
    database_module.record_live_account_demo_verification(
        "acc-live", is_demo=True, broker_login="PA-DEMO-1"
    )
    monkeypatch.setattr(
        daemon, "_deployment_account_is_demo", _REAL_DEPLOYMENT_ACCOUNT_IS_DEMO
    )

    assert _REAL_DEPLOYMENT_ACCOUNT_IS_DEMO("acc-live") is True

    # Move the credential to a live-money account behind the gate's back, the
    # way a rotated secret does: the row changes, the verification does not.
    with database_module.get_session() as session:
        row = session.get(database_module.LiveAccountModel, "acc-live")
        row.broker_login = "PA-LIVE-9"
        session.add(row)
        session.commit()

    with _daemon_log_records() as records:
        assert _REAL_DEPLOYMENT_ACCOUNT_IS_DEMO("acc-live") is False
    assert any("identity changed" in record.getMessage() for record in records), (
        f"the refusal must name the reason: {[r.getMessage() for r in records]}"
    )


def test_dispatch_gate_refuses_an_unknown_account_against_the_real_database(
    isolated_db, monkeypatch
) -> None:
    """No row at all is a refusal, not a pass."""
    database_module, _engine, _data_dir = isolated_db
    monkeypatch.setattr(
        daemon, "_deployment_account_is_demo", _REAL_DEPLOYMENT_ACCOUNT_IS_DEMO
    )
    with _daemon_log_records() as records:
        assert _REAL_DEPLOYMENT_ACCOUNT_IS_DEMO("never-configured") is False
    assert any("account is unknown" in record.getMessage() for record in records)


def test_stopped_deployment_on_a_non_demo_account_logs_a_terminal_error(
    isolated_db,
) -> None:
    """"Will retry" is a lie for an account that can never get an adapter.

    `_ensure_adapter_for_active_account` permanently refuses to build one for a
    non-demo account, so the cancel/flatten for a STOPPED deployment can never
    run. That is an operator-action state — the positions have to be closed by
    hand at the broker — and it was being logged at INFO as a retry.
    """
    database_module, _engine, _data_dir = isolated_db
    database_module.ensure_live_account("acc-1", "alpaca", is_demo=False)
    enforced: set[str] = set()

    with _daemon_log_records(level=logging.INFO) as records:
        asyncio.run(
            daemon._enforce_stopped_deployments(
                [_stopped_deployment()],
                {},
                already_enforced=enforced,
                account_lookup={
                    "acc-1": LiveAccountConfig(id="acc-1", broker="alpaca", is_demo=False)
                },
            )
        )

    terminal = [r for r in records if "can never get an adapter" in r.getMessage()]
    assert terminal, f"expected a terminal error, got {[r.getMessage() for r in records]}"
    assert all(record.levelno == logging.ERROR for record in terminal)
    assert "non-demo" in terminal[0].getMessage()
    # Still not recorded as enforced: it was not actually flattened.
    assert "44444444-4444-4444-8444-444444444444" not in enforced


def test_stopped_deployment_without_an_adapter_stays_an_info_retry(
    isolated_db,
) -> None:
    """The control: a merely-unconnected account is still a retry, not terminal.

    `active_adapters` is empty for every account right after a restart, so this
    must not be escalated to an error or the log would cry wolf on every boot.
    """
    database_module, _engine, _data_dir = isolated_db
    database_module.ensure_live_account("acc-1", "alpaca", is_demo=True)
    database_module.record_live_account_demo_verification("acc-1", is_demo=True)
    enforced: set[str] = set()

    with _daemon_log_records(level=logging.INFO) as records:
        asyncio.run(
            daemon._enforce_stopped_deployments(
                [_stopped_deployment()],
                {},
                already_enforced=enforced,
                account_lookup={
                    "acc-1": LiveAccountConfig(id="acc-1", broker="alpaca", is_demo=True)
                },
            )
        )

    retries = [r for r in records if "will retry cancel/flatten" in r.getMessage()]
    assert retries, f"expected a retry, got {[r.getMessage() for r in records]}"
    assert all(record.levelno == logging.INFO for record in retries)
    assert not [r for r in records if "can never get an adapter" in r.getMessage()]


class TestOneDeploymentsConfigErrorDoesNotSilenceTheOthers:
    """A config typo on one deployment must not silence the rest of the channel.

    `_dispatch_discord_trade_signal` loops over every deployment on a channel.
    One deployment's malformed boolean used to `return` out of the whole function
    from inside that loop, abandoning every *later* deployment as well.

    The surrounding comment already said the intent - "must skip the deployment,
    not kill the listener loop or other deployments on the same account" - so the
    code contradicted its own stated invariant. The reason it stayed invisible:
    each deployment still reported ACTIVE, its Discord service still showed
    connected, and no signal was ever *rejected*. The deployments just quietly
    stopped trading, which looks exactly like a strategy that stopped firing.

    Every sibling check in the same loop uses `continue`. This one did not.
    """

    @staticmethod
    def _deployment(name: str):
        from shared.models import LiveDeploymentConfig

        return LiveDeploymentConfig(
            strategy_name=name,
            account_id="acc-1",
            symbols=["AAPL"],
            timeframe="1m",
            status="ACTIVE",
            signal_source_type="discord",
            signal_source_id="rocketfuel",
        )

    def test_a_broken_deployment_does_not_stop_the_next_one(
        self, monkeypatch
    ):
        import asyncio

        import live_runner.daemon as daemon
        from risk.models import TradeSignal

        monkeypatch.setattr(daemon, "_deployment_is_currently_active", lambda d: True)
        monkeypatch.setattr(
            daemon, "_deployment_matches_stockkit_route", lambda *a, **k: True
        )
        monkeypatch.setattr(
            daemon, "_deployment_covers_discord_symbol", lambda *a, **k: True
        )
        monkeypatch.setattr(
            daemon, "_deployment_accepts_signal_author", lambda *a, **k: True
        )
        monkeypatch.setattr(daemon, "_deployment_trusted_author_ids", lambda d: {"a"})

        def _wait(deployment, _env):
            if deployment.strategy_name == "BROKEN":
                raise ValueError("wait_for_candle_close must be a boolean")
            return True

        monkeypatch.setattr(daemon, "_deployment_wait_for_candle_close", _wait)

        dispatched: list[str] = []

        async def _submit(_account_id, _adapter, deployment, _signal, **_kwargs):
            dispatched.append(deployment.strategy_name)

        monkeypatch.setattr(daemon, "_submit_normalized_discord_signal", _submit)

        signal = TradeSignal(
            action="buy", symbol="AAPL", entry_price=100.0, stop_loss=None,
            take_profit=None, timeframe="1m", metadata={},
        )

        asyncio.run(
            daemon._dispatch_discord_trade_signal(
                "acc-1",
                None,
                [self._deployment("BROKEN"), self._deployment("HEALTHY")],
                signal,
                accounts_env_values={},
            )
        )

        assert "HEALTHY" in dispatched, (
            "a broken deployment's config error silently silenced every later "
            f"deployment on the channel; dispatched={dispatched}"
        )
        assert "BROKEN" not in dispatched

    def test_every_sibling_refusal_in_that_loop_uses_continue(
        self, monkeypatch
    ):
        """A structural guard, so the bug cannot be reintroduced by a new branch.

        The failure was one `return` among four `continue`s in the same loop, and
        it is invisible in review because the function is long. This reads the
        loop body and asserts no early return appears inside it.
        """
        import inspect

        import live_runner.daemon as daemon

        # An AST walk, not a line-prefix scan.
        #
        # The previous version compared stripped line text, which is defeatable
        # twice over. Verified by mutation: changing the offending `continue` to
        # `raise RuntimeError(...)` left this test **green**, and a `raise` ends the
        # sibling deployments exactly as a `return` does - so the guard did not guard
        # the thing it names. The scan also ran `lines[start + 1:]` to the end of the
        # *function* rather than the end of the loop, so it over-scanned and would
        # have flagged a perfectly legal `return` after the loop.
        #
        # Now it finds the real `for` node and rejects any Return, Raise or Break at
        # any depth inside its body, which is what "a sibling refusal must not end the
        # loop" actually means. Nested function definitions are excluded: a `return`
        # inside one of those is that function's, not the loop's. This is the same
        # technique the refresher-wiring check uses, and the reason to prefer it is
        # that one was written after a substring search passed for the wrong reason.
        import ast

        tree = ast.parse(inspect.getsource(daemon._dispatch_discord_trade_signal))
        loops = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.For)
            and any(
                isinstance(inner, ast.Name) and inner.id == "deployment"
                for inner in ast.walk(node.target)
            )
        ]
        assert len(loops) == 1, (
            f"expected exactly one per-deployment loop, found {len(loops)}; the "
            f"guard cannot tell which one it is protecting"
        )
        loop = loops[0]
        offenders: list[str] = []
        # A nested definition's returns are its own, not the loop's.
        skipped: set[int] = set()
        for node in ast.walk(loop):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                skipped.update(id(n) for n in ast.walk(node))
        for node in ast.walk(loop):
            if id(node) in skipped:
                continue
            if isinstance(node, (ast.Return, ast.Raise)):
                offenders.append(f"{type(node).__name__} at line {node.lineno}")
            elif isinstance(node, ast.Break):
                offenders.append(f"Break at line {node.lineno}")
        assert not offenders, (
            f"a return/raise/break inside the per-deployment loop ends the sibling "
            f"deployments after it, which is the bug this guard exists for: "
            f"{offenders}"
        )


class TestStoppedDiscordServiceStatus:
    """A cancelled listener must not leave `failed` standing as its status.

    `ServiceStatus.STOPPED` was validated everywhere and written nowhere, so a
    service that had failed kept claiming failure after the daemon cancelled its
    listener. That record then described a process that no longer existed - the
    mirror image of treating a 200 as a healthy service. A stale claim is not
    safer than a missing one, because it is what an operator decides by.
    """

    def test_a_failed_service_stays_failed_so_startup_recovery_can_retry_it(
        self, monkeypatch
    ) -> None:
        """A FAILED service must remain FAILED when its listener is cancelled.

        `_recover_failed_services` never resurrects PAUSED/STOPPED, because those
        encode operator intent, while FAILED is system state that is safe to retry
        on the next daemon start. So writing STOPPED here silently disabled
        recovery: a service that failed on an expired token would stay dead across
        every restart, forever.

        It also hid the failure. a5a76ad writes FAILED to make a misconfigured
        author allowlist visible, and within one 60s sync tick that FAILED was
        overwritten with STOPPED - which reads as a clean, intentional stop.
        """
        updates: list[tuple[str, object]] = []

        monkeypatch.setattr(
            daemon,
            "update_service_status",
            lambda service_id, status, **_kw: updates.append((service_id, status)),
        )
        monkeypatch.setattr(
            daemon,
            "list_services",
            lambda **_kwargs: [
                SimpleNamespace(
                    id="service-1",
                    name="TEST_BOT",
                    status=daemon.ServiceStatus.FAILED,
                )
            ],
        )

        asyncio.run(
            daemon._stop_discord_bot_service(
                "service-1",
                active_discord_bots={},
                active_discord_bot_tasks={},
            )
        )

        assert updates == []

    def test_a_paused_service_is_never_relabelled_stopped(self, monkeypatch) -> None:
        """The caller's `paused` flag, which the row check does not cover.

        `_record_discord_service_stopped` implements *"a PAUSED service stays
        PAUSED: that is operator intent and must survive"* in **two independent
        places**: the caller's `paused` flag at line 2518, and the `RUNNING` row
        check at line 2537. The row check is pinned - a FAILED service must stay
        FAILED, per the test above - but the flag was not, and dropping
        `if paused: return` left all 584 collected tests green.

        The two are not redundant. The flag is the caller's snapshot of intent; the
        row is what the database says now. They disagree whenever the snapshot is
        taken before a pause lands, and the flag is the one that carries the
        operator's decision.

        Deliberately constructed so the row says RUNNING while the flag says
        paused, because that is the only state in which the two guards disagree and
        therefore the only one in which dropping the flag changes anything.
        """
        updates: list[tuple[str, object]] = []

        monkeypatch.setattr(
            daemon,
            "update_service_status",
            lambda service_id, status, **_kw: updates.append((service_id, status)),
        )
        monkeypatch.setattr(
            daemon,
            "list_services",
            lambda **_kwargs: [
                SimpleNamespace(
                    id="service-1",
                    name="TEST_BOT",
                    status=daemon.ServiceStatus.RUNNING,
                )
            ],
        )

        asyncio.run(
            daemon._stop_discord_bot_service(
                "service-1",
                active_discord_bots={},
                active_discord_bot_tasks={},
                paused=True,
            )
        )

        assert updates == [], (
            "a service the caller reported as PAUSED was written STOPPED; operator "
            f"intent was overwritten: {updates}"
        )

    def test_the_paused_flag_still_lets_a_running_service_be_recorded_stopped(
        self, monkeypatch
    ) -> None:
        """The converse, so the guard cannot be satisfied by never writing STOPPED."""
        updates: list[tuple[str, object]] = []

        monkeypatch.setattr(
            daemon,
            "update_service_status",
            lambda service_id, status, **_kw: updates.append((service_id, status)),
        )
        monkeypatch.setattr(
            daemon,
            "list_services",
            lambda **_kwargs: [
                SimpleNamespace(
                    id="service-1",
                    name="TEST_BOT",
                    status=daemon.ServiceStatus.RUNNING,
                )
            ],
        )

        asyncio.run(
            daemon._stop_discord_bot_service(
                "service-1",
                active_discord_bots={},
                active_discord_bot_tasks={},
                paused=False,
            )
        )

        assert updates == [("service-1", daemon.ServiceStatus.STOPPED)]

    def test_stopping_a_running_service_records_stopped(self, monkeypatch) -> None:
        updates: list[tuple[str, object]] = []

        def _capture_update(service_id: str, status: object, **_kwargs) -> None:
            updates.append((service_id, status))

        monkeypatch.setattr(daemon, "update_service_status", _capture_update)
        monkeypatch.setattr(
            daemon,
            "list_services",
            lambda **_kwargs: [
                SimpleNamespace(
                    id="service-1",
                    name="TEST_BOT",
                    status=daemon.ServiceStatus.RUNNING,
                )
            ],
        )

        asyncio.run(
            daemon._stop_discord_bot_service(
                "service-1",
                active_discord_bots={},
                active_discord_bot_tasks={},
            )
        )

        assert updates == [("service-1", daemon.ServiceStatus.STOPPED)]

    def test_a_paused_service_stays_paused_because_that_is_operator_intent(
        self, monkeypatch
    ) -> None:
        """Pausing is a decision. Overwriting it with STOPPED erases intent."""
        updates: list[tuple[str, object]] = []

        monkeypatch.setattr(
            daemon,
            "update_service_status",
            lambda service_id, status, **_kw: updates.append((service_id, status)),
        )
        monkeypatch.setattr(
            daemon,
            "list_services",
            lambda **_kwargs: [
                SimpleNamespace(
                    id="service-1",
                    name="TEST_BOT",
                    status=daemon.ServiceStatus.PAUSED,
                )
            ],
        )

        asyncio.run(
            daemon._stop_discord_bot_service(
                "service-1",
                active_discord_bots={},
                active_discord_bot_tasks={},
                paused=True,
            )
        )

        assert updates == []

    def test_a_bookkeeping_failure_never_blocks_the_stop(self, monkeypatch) -> None:
        """A status write must not be able to leak a cancelled listener task.

        The stop path cancels first and records second on purpose: if recording
        raised, the task would already be gone but the caller would see an
        exception, and callers treat that as "the stop did not happen".
        """
        def _explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("database is down")

        monkeypatch.setattr(daemon, "update_service_status", _explode)
        monkeypatch.setattr(
            daemon,
            "list_services",
            lambda **_kwargs: [
                SimpleNamespace(
                    id="service-1",
                    name="TEST_BOT",
                    status=daemon.ServiceStatus.RUNNING,
                )
            ],
        )

        # Must not raise, even though the database is unreachable.
        asyncio.run(
            daemon._stop_discord_bot_service(
                "service-1",
                active_discord_bots={},
                active_discord_bot_tasks={},
            )
        )

    def test_a_task_that_died_with_an_exception_still_completes_the_stop(
        self, monkeypatch
    ) -> None:
        """`await task` re-raises, and that must not skip the cleanup.

        A task can finish with an exception between the cleanup and reconfigure
        awaits. Awaiting it then re-raises, which used to skip the rest of the
        function: the source was never popped, the auth blocks were never
        cleared, and the status was never recorded - exactly the stale record
        this whole change exists to remove.
        """
        updates: list[tuple[str, object]] = []

        class _DiedTask:
            def done(self) -> bool:
                return False

            def cancel(self) -> None:
                return None

            def __await__(self):
                # A generator, not `async def`. `__await__` must return an
                # iterator, so an `async def __await__` hands the awaiter a
                # coroutine and the failure becomes
                # `TypeError: __await__() returned a coroutine` - measured, not
                # assumed. The double then simulated a task that raised the wrong
                # exception entirely, while naming "listener died" in its body.
                # The cleanup path does `await task` (daemon.py:2524), so this was
                # one await away from masking the very failure it claims to model.
                if False:  # pragma: no cover - makes this a generator function
                    yield
                raise RuntimeError("listener died")

            def __iter__(self):
                return self.__await__()

        class _Source:
            is_paused = False
            disconnected = False

            async def disconnect(self, failed: bool = True) -> None:
                self.disconnected = True

        source = _Source()
        monkeypatch.setattr(
            daemon,
            "update_service_status",
            lambda service_id, status, **_kw: updates.append((service_id, status)),
        )
        monkeypatch.setattr(
            daemon,
            "list_services",
            lambda **_kwargs: [
                SimpleNamespace(
                    id="service-1",
                    name="TEST_BOT",
                    status=daemon.ServiceStatus.RUNNING,
                )
            ],
        )
        daemon._DISCORD_SERVICE_AUTH_BLOCKED_IDS.add("service-1")
        daemon._DISCORD_SERVICE_AUTH_BLOCKED_REASONS["service-1"] = "auth failed"

        bots = {"service-1": source}
        asyncio.run(
            daemon._stop_discord_bot_service(
                "service-1",
                active_discord_bots=bots,
                active_discord_bot_tasks={"service-1": _DiedTask()},
            )
        )

        # The source is still released, the auth block is cleared, and the status
        # is recorded - all three, despite the task raising.
        assert bots == {}
        assert "service-1" not in daemon._DISCORD_SERVICE_AUTH_BLOCKED_IDS
        assert updates == [("service-1", daemon.ServiceStatus.STOPPED)]



class TestDiscordDispatchIsolation:
    def test_one_failing_dispatch_does_not_end_the_batch(self, monkeypatch) -> None:
        """A raise must skip the deployment, not the listener loop.

        The dispatch was awaited bare inside the `async for` over the source's
        signals, so any exception it did not catch internally - a broker timeout
        from `get_market_data` - propagated out, disconnected the source, and sent
        the outer retry loop round again. That rebuilds the source with empty
        seen-id sets and priming then marks the window as already-seen, so every
        *other* deployment on the account permanently lost the rest of that poll
        batch. Silently, which is the worst part.

        e7a4a40 fixed the same class of bug for a `return`, and the structural test
        it added only forbids `return` inside the loop. It cannot see a raise, so
        the invariant was never actually enforced.
        """
        source = inspect.getsource(daemon._run_discord_signal_listener_for_account)
        tree = ast.parse(inspect.cleandoc(source))

        # The dispatch must sit inside a try that catches Exception but
        # re-raises CancelledError. A bare `try/finally` does not count: that
        # disconnects the source on the way out and loses the batch, which is the
        # bug. Nor does a handler that swallows CancelledError - the listener
        # would never actually stop.
        guarding = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            calls = [
                child
                for stmt in node.body
                for child in ast.walk(stmt)
                if isinstance(child, ast.Call)
                and getattr(child.func, "id", "") == "_dispatch_discord_trade_signal"
            ]
            if not calls:
                continue
            # `asyncio.CancelledError` parses as an ast.Attribute, not a Name, so
            # reading only `.id` silently misses the handler it is written in.
            names = {
                getattr(h.type, "id", None) or getattr(h.type, "attr", None)
                for h in node.handlers
            }
            guarding.append("Exception" in names and "CancelledError" in names)

        assert guarding, "the dispatch call was not found to inspect"
        assert any(guarding), (
            "the dispatch is still awaited without an Exception guard: one "
            "deployment tearing down the account-wide listener loses the batch "
            "for every other deployment, silently"
        )

    def test_the_deployment_path_reports_through_a_service_row(self, monkeypatch) -> None:
        """A wrong allowlist was invisible on the path all three deployments use.

        `_set_service_status` returns immediately when `service_id` is empty, so
        the deployment listener built with no service id could not fail the
        service however wrong its allowlist was. a5a76ad's streak alert had
        nowhere to write, and the only trace was a per-message WARNING - the
        silent shape the whole streak feature exists to remove, on the only path
        the live deployments actually run.
        """
        source = ast.unparse(
            ast.parse(inspect.cleandoc(inspect.getsource(daemon._run_discord_signal_listener_for_account)))
        )
        assert "service_id=_service_id_for_discord_deployment(" in source, (
            "the deployment listener is still built without a service_id, so it "
            "cannot report a mis-set author allowlist"
        )

    def test_a_deployment_with_no_matching_service_reports_nowhere(self, monkeypatch) -> None:
        """No unique match means no observability, which is the safe direction.

        Attaching this listener to some other bot's status because it happened to
        be the only discord service would be worse than staying quiet: the wrong
        row would report a failure that belongs to a different bot.
        """
        from types import SimpleNamespace as _SN

        deployment = _SN(signal_config={"service_name": "NITRO"}, signal_source_id="nitro")

        def _two_services(**_kwargs):
            return [
                _SN(id="s1", name="NITRO"),
                _SN(id="s2", name="NITRO"),
            ]

        monkeypatch.setattr(daemon, "list_services", _two_services)
        assert daemon._service_id_for_discord_deployment(deployment) is None

        def _one_service(**_kwargs):
            return [_SN(id="s1", name="nitro")]

        monkeypatch.setattr(daemon, "list_services", _one_service)
        # Canonicalised both ways, so a lowercase row still matches.
        assert daemon._service_id_for_discord_deployment(deployment) == "s1"

        def _no_services(**_kwargs):
            return []

        monkeypatch.setattr(daemon, "list_services", _no_services)
        assert daemon._service_id_for_discord_deployment(deployment) is None


def test_the_daemon_cancels_every_task_it_started(monkeypatch):
    """The comment said "cancel EVERYTHING"; the list had drifted from the gather.

    `heartbeat_task` was awaited in the happy-path gather but absent from the
    finally block's cancel list, so when any sibling raised, the heartbeat kept
    running against a daemon that was already unwinding - writing heartbeats for
    a process on its way down.

    The same shape as the PAUSED floor and the allowlist drift: the rule is stated
    once and implemented by a list that nobody re-checks. Found by the CodeRabbit
    review, not by a test.
    """
    import ast
    import textwrap

    # `inspect.cleandoc` is for docstrings. It strips the first line's indentation
    # unconditionally, which for a function body silently removes one level from
    # every subsequent line and produced a tree with no `create_task` at all -
    # the test then failed on its own precondition while the fix was present.
    # `textwrap.dedent` is the right tool and is the reason the first version of
    # this test was a no-op.
    source = textwrap.dedent(inspect.getsource(daemon.run_daemon))
    tree = ast.parse(source)

    created = {
        node.targets[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
        # `asyncio.create_task` parses as an ast.Attribute, not a Name - matching
        # only `.id` finds nothing, which is why this test failed its own
        # precondition while the fix it was written for was present.
        and (getattr(node.value.func, "attr", None) or getattr(node.value.func, "id", None))
        == "create_task"
    }
    assert "heartbeat_task" in created, "precondition: the heartbeat task exists"

    # Every task created must appear in the finally block's cancel list.
    # `for task in (...)` - the target is a single Name, not a tuple. Unpacking is
    # a loop target, so the tuple is `node.iter`.
    cancelled: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.iter, ast.Tuple):
            continue
        for elt in node.iter.elts:
            if isinstance(elt, ast.Name):
                cancelled.add(elt.id)

    missing = created - cancelled
    assert not missing, (
        f"started but never cancelled on shutdown: {sorted(missing)}; a task that "
        "outlives the daemon keeps running against a process that is unwinding"
    )


class TestPersistedStoppedEnforcementLedger:
    """STOPPED enforcement must survive a process, not just a tick.

    The ledger was a set in daemon memory, so every restart re-ran cancel and
    flatten for deployments that were already stopped: re-sending cancel orders
    and re-closing positions that are already flat. Harmless per event, wrong in
    aggregate, and invisible because nothing recorded that it had happened.
    """

    def test_the_ledger_survives_a_restart(self, isolated_db_sandbox) -> None:
        database_module, _engine, _data_dir = isolated_db_sandbox
        import uuid as _uuid

        database_module.ensure_live_account("acc-stop", "alpaca", is_demo=True)
        did = str(_uuid.uuid4())
        database_module.create_live_deployment(
            database_module.LiveDeploymentConfig(
                id=_uuid.UUID(did),
                strategy_name="trend_following/trend_following.py",
                account_id="acc-stop",
                symbols=["EURUSD"],
                timeframe="M15",
                status="STOPPED",
                signal_source_type="discord",
            )
        )

        assert database_module.list_stopped_enforced_deployment_ids() == set()

        database_module.mark_live_deployment_stopped_enforced(did)

        # A fresh process rebuilds the ledger from here, so the second stop of
        # the same deployment is not repeated.
        assert did in database_module.list_stopped_enforced_deployment_ids(), (
            "the enforcement ledger did not survive, so a restart would flatten "
            "this deployment's account a second time"
        )

    def test_leaving_stopped_clears_the_stamp(self, isolated_db_sandbox) -> None:
        """The opposite error matters too: a stale stamp skips a real stop.

        Without clearing, a deployment that was stopped, enforced, then paused,
        then stopped again would be skipped as "already done" from the first
        stop - leaving orders and positions open. That is worse than repeating
        work, so both directions are pinned.
        """
        database_module, _engine, _data_dir = isolated_db_sandbox
        import uuid as _uuid

        database_module.ensure_live_account("acc-stop2", "alpaca", is_demo=True)
        did = str(_uuid.uuid4())
        database_module.create_live_deployment(
            database_module.LiveDeploymentConfig(
                id=_uuid.UUID(did),
                strategy_name="trend_following/trend_following.py",
                account_id="acc-stop2",
                symbols=["EURUSD"],
                timeframe="M15",
                status="STOPPED",
                signal_source_type="discord",
            )
        )
        database_module.mark_live_deployment_stopped_enforced(did)

        database_module.clear_live_deployment_stopped_enforced(did)

        assert database_module.list_stopped_enforced_deployment_ids() == set(), (
            "a stale stamp survived leaving STOPPED, so stopping again would be "
            "skipped and positions would stay open"
        )

    def test_a_legacy_table_gains_the_column(self, isolated_db_sandbox) -> None:
        """Model field without an ALTER works on a fresh DB and nowhere else."""
        import sqlite3 as _sqlite3
        from pathlib import Path

        _database_module, engine, _data_dir = isolated_db_sandbox
        db_path = Path(str(engine.url).replace("sqlite:///", ""))

        with _sqlite3.connect(str(db_path)) as con:
            con.execute("ALTER TABLE live_deployments RENAME TO ld_new")
            con.execute(
                "CREATE TABLE live_deployments (id TEXT PRIMARY KEY,"
                " strategy_name TEXT, account_id TEXT, symbols_json TEXT,"
                " timeframe TEXT, status TEXT, signal_source TEXT,"
                " signal_source_type TEXT, signal_source_id TEXT,"
                " symbol_filter_json TEXT, signal_config_json TEXT,"
                " settings_json TEXT, created_at DATETIME, updated_at DATETIME)"
            )
            con.execute("DROP TABLE ld_new")

        _database_module._migrate_schema()

        with _sqlite3.connect(str(db_path)) as con:
            cols = {r[1] for r in con.execute("PRAGMA table_info('live_deployments')")}
        assert "stopped_enforced_at" in cols, (
            "no ALTER for stopped_enforced_at, so the column would be missing in "
            "every existing install while every model-driven test passed"
        )


def test_the_daemon_reads_and_writes_the_ledger_rather_than_a_local_set(
    isolated_db_sandbox,
):
    """The DB functions passing is not the same as the daemon using them.

    Two mutations survived the first version of these pins: seeding the ledger
    empty again, and replacing the clear with a no-op. Both left every test green,
    because the tests exercised the database helpers directly and never the daemon
    code that calls them - the same "the test cannot reach the code under test"
    trap, and the reason the first pins were worth nothing on their own.

    So this asserts the wiring structurally: the enforcement loop must stamp
    through the persisting call, must clear through the other one, and the ledger
    must be seeded from the database rather than initialised empty.
    """
    source = textwrap.dedent(inspect.getsource(daemon._enforce_stopped_deployments))
    assert "mark_live_deployment_stopped_enforced" in source, (
        "enforcement records the id in a local set but not in the database, so a "
        "restart re-flattens the account"
    )
    assert "clear_live_deployment_stopped_enforced" in source, (
        "the stamp is never cleared, so stopping a deployment a second time is "
        "skipped as already done and positions stay open"
    )

    runner = textwrap.dedent(inspect.getsource(daemon.sync_deployments_loop))
    assert "list_stopped_enforced_deployment_ids" in runner, (
        "the ledger starts empty on every process, which is the defect itself"
    )


def test_the_daemon_writes_the_ledger_to_the_database_not_just_its_own_set(isolated_db):
    """The behavioural half of the static pin, and the half that matters.

    `test_the_daemon_reads_and_writes_the_ledger_rather_than_a_local_set` reads the
    source and asserts the identifier is mentioned. That is the weakest possible
    check: it passes if the name appears anywhere, so it cannot tell a working call
    from one that never executes. Confirmed by mutation - replacing the daemon's
    stamp with a no-op, keeping the in-memory set update, fails that test and
    nothing else. 95 other tests in this file pass, because every one of them
    asserts the local set.

    The defect that pin exists for is real and specific: the ledger is a set in
    daemon memory, so every restart re-ran cancel and flatten for deployments
    already stopped. That only stops happening if the write lands in the
    database, which is the thing to observe.

    So: drive the real enforcement path against a real (isolated) database, then
    read the ledger back through the database function rather than the argument
    the daemon was handed. A restart is simulated by asking a *fresh* reader,
    exactly as `sync_deployments_loop` does at startup.
    """
    import uuid as _uuid

    database_module, _engine, _data_dir = isolated_db
    # "acc-1" is what _stopped_deployment() returns; using a different id here
    # made the daemon find no adapter and - correctly - decline to stamp the
    # ledger, so the first version of this test failed for the wrong reason.
    database_module.ensure_live_account("acc-1", "alpaca", is_demo=True)
    did = "44444444-4444-4444-8444-444444444444"
    database_module.create_live_deployment(
        database_module.LiveDeploymentConfig(
            id=_uuid.UUID(did),
            strategy_name="trend_following/trend_following.py",
            account_id="acc-1",
            symbols=["EURUSD"],
            timeframe="M15",
            status="STOPPED",
            signal_source_type="discord",
        )
    )
    adapter = _RecordingAdapter()
    enforced: set[str] = set()

    asyncio.run(
        daemon._enforce_stopped_deployments(
            [_stopped_deployment()], {"acc-1": adapter}, already_enforced=enforced
        )
    )

    # The daemon's own set is not the evidence. This is, because a restarted
    # process would see nothing else.
    assert did in database_module.list_stopped_enforced_deployment_ids(), (
        "the daemon updated its in-memory set but never wrote the ledger, so the "
        "next process starts empty and re-flattens an account that is already "
        "stopped - the exact defect this column was added to prevent"
    )

    # And a fresh reader must not re-do the work, which is the whole point.
    assert did in database_module.list_stopped_enforced_deployment_ids()
    second = _RecordingAdapter()
    asyncio.run(
        daemon._enforce_stopped_deployments(
            [_stopped_deployment()],
            {"acc-1": second},
            already_enforced=database_module.list_stopped_enforced_deployment_ids(),
        )
    )
    assert second.flatten_calls == 0, (
        "a restarted daemon re-flattened despite the ledger, so the persisted "
        "state is not being read back"
    )


def test_every_task_the_daemon_starts_is_actually_cancelled_when_it_crashes(monkeypatch):
    """The behavioural half of the AST pin: the right task really is cancelled.

    `test_the_daemon_cancels_every_task_it_started` walks the AST and compares
    two *name sets* - the names assigned from `create_task`, and the names in the
    finally block's cancel tuple. That catches a task nobody added to the list.
    It cannot catch the list being right and the behaviour being wrong.

    Measured: replacing `task.cancel()` with a cancel of a different variable,
    leaving every name in the tuple exactly where it was, passes that pin and
    passes all 97 tests in this file. A heartbeat loop that outlives the daemon
    keeps writing heartbeats for a process that is already unwinding, which is
    the failure the pin's own docstring describes.

    So this drives the real `run_daemon`, makes one loop raise, and then checks
    what actually happened to the tasks it started - not what the source says
    about them.
    """
    created: dict[str, asyncio.Task] = {}
    real_create_task = asyncio.create_task

    def _recording_create_task(coro, *, name=None, **kwargs):
        task = real_create_task(coro, name=name, **kwargs)
        if name:
            created[name] = task
        return task

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("sibling loop crashed")

    async def _sleep_briefly(*_args, **_kwargs):
        # Bounded, not forever. `run_daemon` gathers the tasks it started before
        # its finally block runs, so a daemon that fails to cancel them blocks
        # inside its own gather and the test would die on a 180s pytest-timeout
        # instead of naming the task. With a short sleep, a daemon that does not
        # cancel lets them *complete*, and the test says so precisely: done but
        # not cancelled.
        await asyncio.sleep(0.25)

    class _StubScheduler:
        async def start(self):
            return None

    monkeypatch.setattr(asyncio, "create_task", _recording_create_task)
    monkeypatch.setattr(daemon, "sync_deployments_loop", _boom)
    monkeypatch.setattr(daemon, "sync_services_loop", _sleep_briefly)
    monkeypatch.setattr(daemon, "_poll_balances", _sleep_briefly)
    monkeypatch.setattr(daemon, "_daemon_heartbeat_loop", _sleep_briefly)
    monkeypatch.setattr(daemon, "_discover_and_register_discord_services", lambda **_k: None)
    monkeypatch.setattr(daemon, "_recover_failed_services", lambda: None)
    monkeypatch.setattr(daemon, "ServiceScheduler", _StubScheduler)
    monkeypatch.setattr(daemon, "capture_exception", lambda *_a, **_k: None)

    observed: dict[str, tuple[bool, bool]] = {}

    async def _drive():
        try:
            with pytest.raises(RuntimeError, match="sibling loop crashed"):
                await daemon.run_daemon()
            # A cancel that was never awaited has not landed yet, so give the
            # loop a turn before reading the state.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            for name, task in created.items():
                observed[name] = (task.done(), task.cancelled())
        finally:
            # Clean up whatever the daemon failed to stop, so `asyncio.run` does
            # not block on a forever-sleeping task. Without this the test would
            # fail on a 180s pytest-timeout instead of saying what is wrong.
            for task in created.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*created.values(), return_exceptions=True)

    asyncio.run(_drive())

    for name in (
        "service-sync-loop",
        "balance-poller",
        "daemon-heartbeat",
    ):
        assert name in created, f"precondition: {name} was never created"
        done, cancelled = observed.get(name, (False, False))
        assert done, (
            f"{name} was still pending after the daemon unwound; a task that "
            "outlives its daemon keeps running against a dead process"
        )
        assert cancelled, (
            f"{name} finished some other way. A loop that was told to stop must "
            "be cancelled, not left to complete on its own schedule"
        )


def test_an_unreadable_position_quantity_counts_as_open_not_as_flat() -> None:
    """This count is a risk limit, so an unreadable row must not read as zero.

    `_count_open_broker_positions` becomes `current_positions`, and multiplied by
    `max_risk_per_trade` becomes `current_exposure`, in `submit_order_with_risk`. It
    used to do `except (TypeError, ValueError): position_qty = 0.0`, and NaN failed
    the `abs(qty) > 0` comparison the same way. So a non-numeric string, `None`,
    `""`, `"NaN"` and a real NaN each counted as **flat**, and one unreadable row
    among several under-reported both `max_positions` and `max_total_exposure` — the
    engine then sized the next entry as if the account had room it did not.

    The asymmetry that gave it away is in this same module: the exit-only path uses
    `_coerce_live_float`, which raises on exactly these values rather than
    defaulting. Two readers of one broker payload, one refusing and one assuming flat.

    Counting them as open is the fail-closed direction: it blocks an entry rather
    than permitting one. Refusing outright was rejected because one malformed row
    would stop the deployment trading entirely, which is a worse failure than the
    one being prevented.
    """
    for bad in ("abc", None, "", "NaN", float("nan"), float("inf"), "1e400", "not-a-number"):
        assert daemon._count_open_broker_positions([{"qty": bad}]) == 1, (
            f"qty={bad!r} counted as flat, so the risk limits were under-reported "
            f"by one open position"
        )

    # The ordinary cases must be untouched, or the guard is just noise.
    assert daemon._count_open_broker_positions([{"qty": "10"}]) == 1
    assert daemon._count_open_broker_positions([{"qty": 10.0}]) == 1
    assert daemon._count_open_broker_positions([{"qty": "-3"}]) == 1
    assert daemon._count_open_broker_positions([{"qty": "0"}]) == 0
    assert daemon._count_open_broker_positions([{"qty": 0}]) == 0
    assert daemon._count_open_broker_positions([{"qty": 0.0}]) == 0

    # A mixed list counts the readable ones and the unreadable one alike.
    assert (
        daemon._count_open_broker_positions(
            [{"qty": "5"}, {"qty": "0"}, {"qty": "junk"}, {"qty": "2"}]
        )
        == 3
    ), "an unreadable row among several must not be dropped from the count"


def test_an_unreadable_quantity_is_logged_rather_than_silently_assumed_flat(
    caplog,
) -> None:
    """A silent default is how this hid. The fallback has to be visible."""
    import logging

    with caplog.at_level(logging.WARNING, logger="live-runner"):
        daemon._count_open_broker_positions([{"qty": "junk"}])

    messages = [record.getMessage() for record in caplog.records]
    assert any("unreadable quantity" in message for message in messages), (
        f"an unreadable quantity was substituted without a word: {messages}"
    )


class TestEnvIdListAndServiceConfigString:
    """Two small parsers the daemon leans on for allowlists and ids. Two survivors.

    Neither looks important, which is why both went unmeasured - and both end up in a place
    where a wrong answer is **silently** wrong: an id allowlist, and a service config lookup.

    **`_split_env_id_list` fails closed on a wrong type.** A non-string input yields an *empty*
    set, which for a trusted-author allowlist means "nobody is trusted". The alternative -
    coercing with `str(raw)` and splitting - turns a mis-typed config into a one-element
    "allowlist" containing `12345` or `True`: a value that looks configured, matches nothing,
    and is indistinguishable from a real id in a log line.

    That asymmetry is the whole point of the type check, so it is asserted on every wrong type
    rather than just one, including the falsy ones - `0` and `False` are the inputs most likely
    to arrive from a `JSON` config that meant `false`.

    **`_service_config_string` stringifies non-strings, and that is load-bearing.** A Discord
    channel id written as a JSON *number* arrives as an `int`, and the real function turns it
    into `"1234567890"`. Refusing it would mean a channel id that is perfectly valid in the
    config file simply does not resolve. So the stringification is pinned in both directions -
    numbers and booleans become their text, a `None` becomes empty - with the reasoning that
    the alternative failure is "configured but silently inactive".
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("a", {"a"}),
            ("a,b", {"a", "b"}),
            ("a b\tc", {"a", "b", "c"}),
            (" a , b ,, c ", {"a", "b", "c"}),
            ("a,,b", {"a", "b"}),
            ("12345", {"12345"}),
        ],
    )
    def test_an_id_list_splits_on_commas_and_whitespace(self, raw, expected) -> None:
        """Both separators, repeated separators, and surrounding padding.

        The id list arrives as one env string, so `a, b  c` and `a,b,c` must agree - otherwise
        the same allowlist written two ways authorises different people.
        """
        from live_runner.daemon import _split_env_id_list

        assert _split_env_id_list(raw) == expected

    @pytest.mark.parametrize(
        "raw", [None, "", "   ", 12345, 0, True, False, ["a", "b"], {"a"}, object()]
    )
    def test_a_wrong_typed_id_list_is_empty_rather_than_coerced(self, raw) -> None:
        """A mis-typed config yields *nobody*, not a one-element allowlist.

        The distinction is the safety property: an empty allowlist authorises nothing, while a
        coerced one contains a nonsense value that looks configured and matches nothing. Both
        refuse in practice, but only one is legible in a log.
        """
        from live_runner.daemon import _split_env_id_list

        assert _split_env_id_list(raw) == set(), (
            f"{raw!r} produced {_split_env_id_list(raw)!r} instead of an empty set"
        )

    def test_a_numeric_config_value_is_stringified_because_ids_arrive_as_numbers(self) -> None:
        """A JSON channel id arrives as an `int`, and refusing it would be the worse bug."""
        from live_runner.daemon import _service_config_string

        config = {"channel_id": 1234567890, "flag": True, "off": False, "ratio": 1.5}
        assert _service_config_string(config, "channel_id") == "1234567890"
        assert _service_config_string(config, "flag") == "True"
        assert _service_config_string(config, "off") == "False"
        assert _service_config_string(config, "ratio") == "1.5"

    def test_an_absent_or_null_config_value_is_empty(self) -> None:
        """And the two cases that must *not* become a string: absent, and explicitly null.

        `None` becoming `"None"` would be the same class of bug as the env-file `str(None)`
        this sweep already found, so it is asserted rather than assumed.
        """
        from live_runner.daemon import _service_config_string

        assert _service_config_string({}, "missing") == ""
        assert _service_config_string({"k": None}, "k") == ""
        assert _service_config_string({"k": "  padded  "}, "k") == "padded"

    def test_the_timeframe_fallback_is_unaffected_by_a_blank_override_KNOWN_EQUIVALENT(self) -> None:
        """**A verified equivalent mutant**, proved across the shape space.

        `_deployment_signal_timeframe` reads:

            configured_tf = str(configured.get("timeframe") or deployment.timeframe).strip()

        The `or` runs **before** the `strip`, so a whitespace-only override is truthy, survives
        the `or`, and is only then stripped to empty. Moving the fallback inside the `str()`
        - the obvious-looking "simplification" - therefore changes nothing, because the
        subsequent `if configured_tf:` already handles the empty result.

        Verified across every combination that matters, including the one where both sides are
        blank:

        | deployment | `signal_config` | result |
        |---|---|---|
        | `1h` | absent | `1h` |
        | `1h` | `5m` | `5m` |
        | `1h` | `"   "` | `1h` |
        | `1h` | `""` / `None` | `1h` |
        | `""` | `"5m"` | `5m` |
        | `""` | `"   "` / absent / `None` | `""` |

        Recorded because the *behaviour* is what matters - a blank override falls back to the
        deployment's own timeframe, and an entirely blank one yields an empty string rather
        than inventing one - and because the equivalence is a property of the statement
        **order**, which a future edit could change.
        """
        from live_runner.daemon import _deployment_signal_timeframe

        def _deployment(timeframe, signal_config):
            from uuid import uuid4

            from shared.models import LiveDeploymentConfig

            return LiveDeploymentConfig(
                id=str(uuid4()), strategy_name="s", account_id="a", symbols=["X"],
                timeframe=timeframe, status="ACTIVE", signal_config=signal_config,
            )

        for timeframe, config, expected in [
            ("1h", {}, "1h"),
            ("1h", {"timeframe": "5m"}, "5m"),
            ("1h", {"timeframe": "   "}, "1h"),
            ("1h", {"timeframe": ""}, "1h"),
            ("1h", {"timeframe": None}, "1h"),
            ("", {"timeframe": "5m"}, "5m"),
            ("", {"timeframe": "   "}, ""),
            ("", {}, ""),
            ("", {"timeframe": None}, ""),
        ]:
            deployment = _deployment(timeframe, config)
            resolved = _deployment_signal_timeframe(deployment)
            assert resolved == expected, (
                f"deployment={timeframe!r} config={config!r} resolved to {resolved!r}, "
                f"expected {expected!r}"
            )

    def test_a_signal_config_override_beats_the_deployment_timeframe(self) -> None:
        """The precedence itself, so the fallback test above has a working baseline."""
        from uuid import uuid4

        from live_runner.daemon import _deployment_signal_timeframe
        from shared.models import LiveDeploymentConfig

        deployment = LiveDeploymentConfig(
            id=str(uuid4()), strategy_name="s", account_id="a", symbols=["X"],
            timeframe="1h", status="ACTIVE", signal_config={"timeframe": "15m"},
        )
        assert _deployment_signal_timeframe(deployment) == "15m"


class TestNonDemoWarningAndAccountGrouping:
    """The live-money warning, the account grouping, and the auth-block ledger.

    Eight mutations survived here, and the first group is the one that matters most: the
    warning that names ACTIVE deployments pointing at accounts **not verified as demo**.

    **A missing account counts as non-demo, and that is the fail-loud choice.** The lookup is
    `getattr(account_lookup.get(id), "is_demo", False)`, so an account that is not in the map
    resolves to `False` and is therefore an offender. Treating an unknown account as *demo*
    would be the dangerous direction - a deployment whose account failed to load would look
    verified and the warning would stay silent. Asserted explicitly, because it is the kind of
    behaviour that looks like a bug until you read the docstring.

    **Only `ACTIVE` counts.** `PAUSED` and `STOPPED` deployments on a non-demo account are not
    named, which is the difference between "this is trading real money now" and "this is
    configured for real money but is not running". A warning that fired for paused deployments
    would be one an operator learns to ignore, and then it would not be doing its job when it
    matters. So the status gate is asserted in both directions, and the *absence* of a warning
    is asserted for the paused case - a test that only checked the positive would let the
    inversion through.

    The grouping functions are simpler but the same shape: **keep every deployment per account**.
    A grouping that kept only the last would silently drop a deployment from the set the
    adapter works on - and the surviving deployments would still be there, so nothing would
    look wrong.

    Two of the eleven mutations turned out to be **already covered** once applied properly -
    `APPLY-FAILED` had hidden them because the same two-line clear exists in two places. That
    is the third time this pass that treating an `APPLY-FAILED` as "not a verdict" turned a
    false gap into a real measurement.
    """

    @staticmethod
    def _deployment(status="ACTIVE", account_id="a1", **kwargs):
        from uuid import uuid4

        from shared.models import LiveDeploymentConfig

        return LiveDeploymentConfig(
            id=str(uuid4()),
            strategy_name="s",
            account_id=account_id,
            symbols=["X"],
            timeframe="1h",
            status=status,
            **kwargs,
        )

    @staticmethod
    def _account(account_id="a1", *, is_demo=False):
        from shared.models import LiveAccountConfig

        return LiveAccountConfig(
            id=account_id, name="n", broker="ctrader", is_demo=is_demo
        )

    @staticmethod
    def _warned(deployments, account_lookup):
        """Run the warning with a capture handler and report whether anything was logged.

        The handler is used rather than asserting on absence of output, because "no warning" is
        a claim about a log line and `caplog` cannot express it as a positive assertion.
        """
        import logging

        from live_runner import daemon

        class _Capture(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []

            def emit(self, record):
                # `extra={"data": ...}` lands on the record as a plain attribute, so expose
                # it under the name the test reads rather than reaching into `record.data`.
                record.deployment_ids = (getattr(record, "data", {}) or {}).get(
                    "deployment_ids", []
                )
                self.records.append(record)

        handler = _Capture()
        daemon.logger.addHandler(handler)
        try:
            daemon._warn_on_active_non_demo_deployments(deployments, account_lookup)
        finally:
            daemon.logger.removeHandler(handler)
        return [r for r in handler.records if "not verified as demo" in r.getMessage()]

    def test_an_active_deployment_on_a_non_demo_account_is_named(self) -> None:
        """The case the warning exists for."""
        warnings = self._warned(
            [self._deployment()], {"a1": self._account(is_demo=False)}
        )
        assert warnings, "an ACTIVE deployment on a non-demo account was not named"

    def test_a_demo_account_is_not_named(self) -> None:
        """And the negative, so the warning is not simply always-firing."""
        assert self._warned([self._deployment()], {"a1": self._account(is_demo=True)}) == []

    def test_a_missing_account_counts_as_non_demo(self) -> None:
        """**Fail-loud, and the reason to assert it.**

        An account absent from the lookup resolves to `is_demo=False` and is therefore an
        offender. The opposite - treating an unknown account as verified - would mean a
        deployment whose account failed to load looks fine and the warning stays silent.
        """
        assert self._warned([self._deployment()], {}), (
            "a deployment pointing at an account that is not in the lookup was not named; "
            "an unverified account must not read as a verified one"
        )
        assert self._warned([self._deployment()], {"other": self._account()})

    def test_only_an_active_deployment_is_named(self) -> None:
        """`PAUSED` and `STOPPED` are not "trading real money now".

        Asserted as the *absence* of a warning, which is the half that matters: a warning that
        also fired for paused deployments would be one an operator learns to ignore, and then
        it would not be doing its job on the deployment that does need it.
        """
        non_demo = {"a1": self._account(is_demo=False)}
        for status in ("PAUSED", "STOPPED"):
            assert self._warned([self._deployment(status=status)], non_demo) == [], (
                f"a {status} deployment on a non-demo account was named; the warning is "
                "meant to be about deployments that are actually running"
            )
        # ...and ACTIVE still is, on the same accounts, so the loop above is not vacuous.
        assert self._warned([self._deployment(status="ACTIVE")], non_demo)

    def test_the_warning_names_every_offending_deployment(self) -> None:
        """All of them, so an operator can act on the whole list in one pass."""
        deployments = [
            self._deployment(account_id="a1"),
            self._deployment(account_id="a2"),
            self._deployment(account_id="a3"),
            self._deployment(status="PAUSED", account_id="a4"),
        ]
        accounts = {
            "a1": self._account("a1", is_demo=False),
            "a2": self._account("a2", is_demo=True),
            "a3": self._account("a3", is_demo=False),
            "a4": self._account("a4", is_demo=False),
        }
        warnings = self._warned(deployments, accounts)
        assert len(warnings) == 1, warnings
        named = set(warnings[0].deployment_ids)
        expected = {d.id for d in (deployments[0], deployments[2])}
        assert named == expected, f"named {sorted(named)}, expected {sorted(expected)}"

    def test_grouping_keeps_every_deployment_for_an_account(self) -> None:
        """Two deployments on one account must both survive the grouping.

        A grouping that kept only the last would drop a deployment from the set the adapter
        works on - and the surviving ones would still look right, so nothing would report a
        miss.
        """
        from live_runner.daemon import _group_deployments_by_account

        deployments = [
            self._deployment(account_id="a1"),
            self._deployment(account_id="a1"),
            self._deployment(account_id="a2"),
        ]
        grouped = _group_deployments_by_account(deployments)
        assert set(grouped) == {"a1", "a2"}, sorted(grouped)
        assert len(grouped["a1"]) == 2, len(grouped["a1"])
        assert len(grouped["a2"]) == 1
        assert {d.id for d in grouped["a1"]} == {deployments[0].id, deployments[1].id}

    def test_only_active_deployments_contribute_an_account_id(self) -> None:
        """The same status gate as the warning, on the set of accounts the runner acts on."""
        from live_runner.daemon import _deployment_active_account_ids

        assert _deployment_active_account_ids(
            [self._deployment(status="ACTIVE", account_id="a1")]
        ) == {"a1"}
        for status in ("PAUSED", "STOPPED"):
            assert _deployment_active_account_ids(
                [self._deployment(status=status, account_id="a1")]
            ) == set(), f"a {status} deployment contributed an account id"

    def test_a_blank_block_reason_becomes_a_named_fallback(self) -> None:
        """The ledger's reason is the only diagnostic the operator gets.

        A terminal auth failure with a blank message stores a named fallback rather than an
        empty string, so "the account is blocked" and "the account is blocked, because..." are
        both answerable from the log.
        """
        from live_runner import daemon

        account_id = "acct-blank-reason"
        try:
            daemon._block_ctrader_account(
                account_id, daemon.CTraderAuthTerminalError("   ")
            )
            assert account_id in daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS
            reason = daemon._CTRADER_AUTH_BLOCKED_REASONS.get(account_id)
            assert reason == "cTrader authorization failed", repr(reason)
        finally:
            daemon._clear_ctrader_blocked_state(account_id)

    def test_clearing_the_block_removes_both_halves_of_the_ledger(self) -> None:
        """The account and its reason must go together.

        A reason left behind after the account is unblocked would be reported to the operator
        as the current cause of a block that no longer exists - so the two halves are asserted
        as one, and clearing twice is a no-op rather than an error.
        """
        from live_runner import daemon

        account_id = "acct-clear-roundtrip"
        try:
            daemon._block_ctrader_account(
                account_id, daemon.CTraderAuthTerminalError("not authorized")
            )
            assert account_id in daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS
            assert account_id in daemon._CTRADER_AUTH_BLOCKED_REASONS

            daemon._clear_ctrader_blocked_state(account_id)
            assert account_id not in daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS
            assert account_id not in daemon._CTRADER_AUTH_BLOCKED_REASONS, (
                "the account was unblocked but its stale reason remains, so an operator "
                "would be told the current cause of a block that no longer exists"
            )

            # And clearing an account that was never blocked is harmless.
            daemon._clear_ctrader_blocked_state(account_id)
            daemon._clear_ctrader_blocked_state("never-seen-at-all")
        finally:
            daemon._CTRADER_AUTH_BLOCKED_ACCOUNTS.discard(account_id)
            daemon._CTRADER_AUTH_BLOCKED_REASONS.pop(account_id, None)


class TestAdapterAvailabilityAndTaskLifecycle:
    """The retryable-vs-terminal decision, and the task bookkeeping around it.

    Six mutations survived. `_adapter_never_available_reason` is the one that matters: its
    return value is the **difference between "not connected yet" (retryable, INFO) and "can
    never connect" (terminal, ERROR)**, so a wrong answer does not just log the wrong thing - it
    decides whether the daemon keeps trying.

    **Every branch returns a reason rather than `None`, and all three are asserted.** `None` means
    "still may", so a branch returning `None` for a terminal condition converts a permanent
    refusal into an infinite retry loop that will never succeed.

    The three terminal conditions, in the order the function checks them:

    | condition | why it is terminal |
    |---|---|
    | no lookup supplied | *absence of evidence is not evidence of a retryable condition* |
    | account absent from the lookup | no adapter is ever built for it |
    | account is non-demo | no adapter is ever built for it |

    The first is the least obvious and the most worth pinning. "We could not check" is treated as
    "cannot connect" rather than "try again" - which is the conservative direction, and the
    opposite choice would retry forever on a lookup that was never going to arrive.

    **The account id is stringified before the lookup.** A caller passing an `int` finds a
    `str`-keyed lookup, which is what the daemon actually holds. Without the `str()` it would miss
    and report the account as permanently absent - a **terminal** verdict from what is really a
    type mismatch. That is the worst direction for this particular mistake, which is why it is
    asserted with an int key rather than a string one.

    The task helpers are the same shape: bookkeeping whose failure is silence. A finished task left
    in the dict leaks; a running task for a deactivated account that is popped without cancelling
    keeps running; and **calling `.exception()` on a cancelled task raises `CancelledError`**,
    so the `if task.cancelled(): continue` guard is what stops a routine cancellation from
    crashing the cleanup sweep.
    """

    @staticmethod
    def _account(account_id="a1", *, is_demo=True):
        from shared.models import LiveAccountConfig

        return LiveAccountConfig(
            id=account_id, name="n", broker="alpaca", is_demo=is_demo
        )

    def test_an_absent_lookup_is_terminal_not_retryable(self) -> None:
        """**"We could not check" is "cannot connect", not "try again".**

        The docstring's own reasoning, pinned because the opposite is the intuitive reading:
        a missing lookup looks like a transient state, so returning `None` would retry forever on
        something that was never going to arrive.
        """
        from live_runner.daemon import _adapter_never_available_reason

        reason = _adapter_never_available_reason(None, "a1")
        assert reason is not None, (
            "a missing lookup was reported as retryable, so the daemon would retry forever "
            "for an account it can never build an adapter for"
        )
        assert "not available" in reason, reason

    def test_an_account_missing_from_the_lookup_is_terminal(self) -> None:
        """No adapter is ever built for it, so no amount of retrying helps."""
        from live_runner.daemon import _adapter_never_available_reason

        reason = _adapter_never_available_reason({}, "a1")
        assert reason is not None, "an absent account was reported as retryable"
        assert "not in the account lookup" in reason, reason

    def test_a_non_demo_account_is_terminal(self) -> None:
        """Already covered through the dispatch path; pinned here for the reason it gives."""
        from live_runner.daemon import _adapter_never_available_reason

        reason = _adapter_never_available_reason(
            {"a1": self._account(is_demo=False)}, "a1"
        )
        assert reason is not None
        assert "non-demo" in reason, reason

    def test_a_demo_account_in_the_lookup_is_still_may(self) -> None:
        """`None`, and it is the only `None` - asserted so the other three are not always-firing."""
        from live_runner.daemon import _adapter_never_available_reason

        assert _adapter_never_available_reason(
            {"a1": self._account(is_demo=True)}, "a1"
        ) is None

    def test_a_non_string_account_id_still_finds_a_string_keyed_lookup(self) -> None:
        """**The `str()` is load-bearing, and the direction it fails in is the bad one.**

        The daemon's lookup is keyed by string. A caller passing an `int` must still find it -
        and without the `str()` it would miss, and report the account as *permanently absent*.
        That converts a type mismatch into a terminal verdict, which is the worst possible
        direction for this particular function.
        """
        from live_runner.daemon import _adapter_never_available_reason

        assert _adapter_never_available_reason({"1": self._account("1")}, 1) is None, (
            "an int account id did not find a string-keyed lookup, so it would be reported "
            "as permanently absent"
        )
        # And an int-keyed lookup still works, because the id is normalised first.
        assert (
            _adapter_never_available_reason({1: self._account("1")}, 1) is not None
        ), "an int-keyed lookup unexpectedly matched; the normalisation changed shape"

    def test_an_inactive_account_listener_is_cancelled_not_merely_forgotten(self) -> None:
        """Popping without cancelling leaves the coroutine running with no handle on it.

        Both halves are asserted, because the mutation removes only the `cancel()` and leaves the
        pop - so a test checking the dict alone would pass.

        **The assertions run inside the coroutine, deliberately.** Asserting after
        `asyncio.run` returns is a bug I made first: the loop shutdown cancels outstanding tasks,
        so a pending task reads as `cancelled()` whether or not the code under test cancelled
        it. That would have made this test pass for the wrong reason - and the sibling test
        caught it, because an *active* account's task came back cancelled too.
        """
        import asyncio

        from live_runner.daemon import _cancel_inactive_tasks

        async def scenario():
            live = asyncio.create_task(asyncio.sleep(60))
            await asyncio.sleep(0)
            tasks = {"a1": live}
            _cancel_inactive_tasks(tasks, set())
            await asyncio.sleep(0)
            still_in_dict = "a1" in tasks
            was_cancelled = live.cancelled()
            live.cancel()
            return still_in_dict, was_cancelled

        still_in_dict, was_cancelled = asyncio.run(scenario())
        assert not still_in_dict, "the task was not removed from the dict"
        assert was_cancelled, (
            "the task was removed but not cancelled, so the listener is still running with "
            "nothing holding a handle to it"
        )

    def test_an_active_account_listener_is_untouched(self) -> None:
        """The other half of the branch, so the cancel above is not unconditional.

        Also asserted inside the coroutine, for the reason given on the test above: after the
        loop closes, every outstanding task reports as cancelled.
        """
        import asyncio

        from live_runner.daemon import _cancel_inactive_tasks

        async def scenario():
            live = asyncio.create_task(asyncio.sleep(60))
            await asyncio.sleep(0)
            tasks = {"a1": live}
            _cancel_inactive_tasks(tasks, {"a1"})
            await asyncio.sleep(0)
            still_in_dict = "a1" in tasks
            was_cancelled = live.cancelled()
            live.cancel()
            return still_in_dict, was_cancelled

        still_in_dict, was_cancelled = asyncio.run(scenario())
        assert still_in_dict, "an active account's listener was removed"
        assert not was_cancelled, "an active account's listener was cancelled"

    def test_a_finished_listener_is_cleaned_from_the_dict(self) -> None:
        """A finished task left in the dict is a leak, and nothing reports it."""
        import asyncio

        from live_runner.daemon import _cleanup_finished_listener_tasks

        async def scenario():
            done = asyncio.create_task(asyncio.sleep(0))
            await done
            running = asyncio.create_task(asyncio.sleep(60))
            await asyncio.sleep(0)
            tasks = {"done": done, "running": running}
            _cleanup_finished_listener_tasks(tasks)
            running.cancel()
            return tasks

        tasks = asyncio.run(scenario())
        assert "done" not in tasks, "a finished task was left in the dict"
        assert "running" in tasks, "a still-running task was cleaned up"

    def test_a_cancelled_listener_is_cleaned_without_reading_its_exception(self) -> None:
        """**`.exception()` on a cancelled task raises `CancelledError`.**

        Verified by direct call rather than assumed, because that is what makes the
        `if task.cancelled(): continue` guard load-bearing rather than tidy. Without it, a
        routine cancellation - which happens on every account deactivation - turns into a
        `CancelledError` escaping the cleanup sweep.
        """
        import asyncio

        from live_runner.daemon import _cleanup_finished_listener_tasks

        async def scenario():
            cancelled = asyncio.create_task(asyncio.sleep(60))
            await asyncio.sleep(0)
            cancelled.cancel()
            try:
                await cancelled
            except asyncio.CancelledError:
                pass
            tasks = {"cancelled": cancelled}
            # The real function must not raise here.
            _cleanup_finished_listener_tasks(tasks)
            # And the reason it can is visible directly.
            raised = False
            try:
                cancelled.exception()
            except BaseException:
                raised = True
            return tasks, raised

        tasks, raises = asyncio.run(scenario())
        assert "cancelled" not in tasks, "the cancelled task was not cleaned up"
        assert raises, (
            "task.exception() no longer raises on a cancelled task, so the guard this test "
            "relies on may be removable - re-check before deleting it"
        )

    def test_the_deployment_task_name_includes_its_kind(self) -> None:
        """Two kinds share an account, so the name has to carry the kind.

        Dropping the kind makes `deployment-listener-a1` and `deployment-stream-a1`
        identical, which is the collision a task name exists to prevent.
        """
        from live_runner.daemon import _deployment_task_name

        listener = _deployment_task_name("acc-1", "listener")
        stream = _deployment_task_name("acc-1", "stream")
        assert listener == "deployment-listener-acc-1", listener
        assert stream == "deployment-stream-acc-1", stream
        assert listener != stream, "two task kinds produced the same name"
        assert "acc-1" in listener, listener


class TestDeploymentIdentityHelpersAndAuthorAllowlist:
    """The name/id resolvers, and one allowlist guard that is a security boundary.

    Nine mutations survived, all in the "which value wins, and how is it shaped" family.

    **The allowlist's `Mapping` exclusion is the one that matters.** `_trusted_author_ids_from_config`
    accepts a list of ids or a single id string, and explicitly returns an empty set for a
    `Mapping` - *before* the generic iterable branch. That ordering is the whole guard, because
    **iterating a dict yields its keys**. Without it, a config shaped like
    `{"123456": true}` - a perfectly natural "flags per author" form - would authorise
    author `123456` from the key, silently turning a settings block into an allowlist.

    The `str(item).strip().lower()` also matters beyond tidiness: ids are compared
    case-insensitively, and a padded id that is not stripped simply never matches, so an
    allowlist entry would look configured and authorise nobody.

    **Casing is a resolver, not a convention.** `_deployment_signal_processor_name` lower-cases,
    so `"RocketFuel"` and `"rocketfuel"` resolve to the same processor. Forgetting the
    lower-case is a silent miss in the same family as the StockKit route normalisation, and
    in the same direction: no processor match, so no trade.

    The two name resolvers are precedence questions, asserted with both sources present
    because that is the only case that distinguishes the branches - which is the third time
    this session that a one-value test passed under either order.

    The service name is stripped, so a padded name resolves; and the task name keeps its
    `service-discord-` prefix, which is what stops a service task colliding with a
    deployment task of the same id.
    """

    @staticmethod
    def _deployment(signal_config=None, settings=None, source_id="svc-1"):
        from uuid import uuid4

        from shared.models import LiveDeploymentConfig

        kwargs = {}
        if signal_config is not None:
            kwargs["signal_config"] = signal_config
        if settings is not None:
            kwargs["settings"] = settings
        return LiveDeploymentConfig(
            id=str(uuid4()), strategy_name="s", account_id="a", symbols=["X"],
            timeframe="1h", status="ACTIVE", signal_source_id=source_id, **kwargs,
        )

    def test_a_mapping_never_becomes_a_set_of_trusted_authors(self) -> None:
        """**Iterating a dict yields its keys, so this guard is a security boundary.**

        A config shaped `{"123456": true}` is a natural "flags per author" block. Without
        the `Mapping` exclusion it would be read as an allowlist of the single author
        `123456` - a settings dict silently promoted to a trust decision.
        """
        from live_runner.daemon import _trusted_author_ids_from_config

        for mapping in ({"123456": True}, {"alice": "on"}, {"1": None}, {}):
            assert _trusted_author_ids_from_config(mapping) == set(), (
                f"{mapping!r} produced {_trusted_author_ids_from_config(mapping)!r}; a "
                "dict-of-flags config must not become an allowlist of its keys"
            )

    def test_author_ids_are_lower_cased_stripped_and_non_empty_only(self) -> None:
        """Case-insensitive comparison is the contract, so a padded or blank entry is a miss.

        **An alphabetic id in the single-string form is the case that distinguishes these.**
        My first version of this used only digits, and digits have no case - so removing the
        `.lower()` from the string branch changed nothing observable and the mutation
        survived a test that looked like it covered it. The list branch lower-cases
        separately, so a list assertion cannot see the string branch either.
        """
        from live_runner.daemon import _trusted_author_ids_from_config

        assert _trusted_author_ids_from_config("  ALICE  ") == {"alice"}, (
            "a single-string id is not lower-cased, so it will never match a "
            "case-insensitive comparison"
        )
        assert _trusted_author_ids_from_config("  123456  ") == {"123456"}
        assert _trusted_author_ids_from_config(["123", " 456 ", "ALICE"]) == {
            "123", "456", "alice"
        }
        # Blank entries are dropped rather than becoming an id that matches nothing.
        assert _trusted_author_ids_from_config(["", "  ", "123"]) == {"123"}
        for absent in (None, "", "   ", 5, b"12"):
            assert _trusted_author_ids_from_config(absent) == set(), repr(absent)

    def test_the_explicit_mapping_guard_is_redundant_with_the_tuple_exclusion(self) -> None:
        """**A verified equivalent mutant**, and the reason is a second path to the same answer.

        The function guards a `Mapping` twice:

            if isinstance(value, Mapping):
                return set()
            if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
                ...

        Adding `Mapping` to that tuple exclusion reaches the same result by a different
        route - the dict is skipped by the iterable branch and falls through to the final
        `return set()`. So dropping the explicit guard changes nothing for any input, which
        is what the mutation showed: with the guard replaced, `{"123456": True}` still
        returned an empty set.

        Verified by hand rather than assumed, because "equivalent" is a claim about all
        inputs and the two paths do converge here only because the fallthrough is an empty
        set. The explicit guard stays - it documents that a dict is *deliberately* not an
        allowlist rather than incidentally falling through - and the note is here so the
        next person to run this mutation does not re-derive it.
        """
        from live_runner.daemon import _trusted_author_ids_from_config

        for mapping in ({"123456": True}, {"alice": "on"}, {}):
            assert _trusted_author_ids_from_config(mapping) == set(), mapping

    def test_the_signal_processor_name_is_lower_cased(self) -> None:
        """`"RocketFuel"` and `"rocketfuel"` must resolve to the same processor.

        The processor name is compared against registered names, so a missing lower-case is a
        silent miss - no processor match, so no trade.
        """
        from live_runner.daemon import _deployment_signal_processor_name

        deployment = self._deployment(signal_config={"signal_processor": "RocketFuel"})
        assert _deployment_signal_processor_name(deployment) == "rocketfuel"
        from_settings = self._deployment(settings={"signal_processor": "NITRO"})
        assert _deployment_signal_processor_name(from_settings) == "nitro"
        assert _deployment_signal_processor_name(self._deployment()) == ""

    def test_the_signal_config_processor_wins_over_the_settings_one(self) -> None:
        """Precedence, asserted with both present because one value cannot distinguish it."""
        from live_runner.daemon import _deployment_signal_processor_name

        deployment = self._deployment(
            signal_config={"signal_processor": "FromConfig"},
            settings={"signal_processor": "FromSettings"},
        )
        assert _deployment_signal_processor_name(deployment) == "fromconfig", (
            "the settings-level processor overrode the signal_config one"
        )

    def test_the_signal_source_id_wins_for_the_service_name(self) -> None:
        """The source id is authoritative; the config name is a fallback."""
        from live_runner.daemon import _deployment_service_name

        deployment = self._deployment(
            signal_config={"service_name": "from-config"}, source_id="from-source"
        )
        assert _deployment_service_name(deployment) == "from-source", (
            "the config service_name overrode the deployment's signal_source_id"
        )

    def test_the_service_name_falls_back_to_config_and_is_stripped(self) -> None:
        """No source id means the config answers, and padding is removed either way."""
        from live_runner.daemon import _deployment_service_name

        assert (
            _deployment_service_name(
                self._deployment(signal_config={"service_name": "  cfg  "}, source_id="")
            )
            == "cfg"
        )
        assert _deployment_service_name(self._deployment(source_id="  svc  ")) == "svc"
        assert _deployment_service_name(self._deployment(source_id="")) == ""

    def test_the_service_task_name_keeps_its_namespace_prefix(self) -> None:
        """`service-discord-` is what stops a service task colliding with a deployment task.

        Dropped, `service-discord-1` and `deployment-listener-1` would still differ - but a
        service id and a deployment id could then produce the same name for different
        subsystems, and task names are what a human reads when something is stuck.
        """
        from live_runner.daemon import _service_task_name

        name = _service_task_name("bot-1")
        assert name == "service-discord-bot-1", name
        assert name.startswith("service-"), name
        assert "bot-1" in name, name


class TestBrokerLoginAndChannelResolution:
    """Two resolvers that read the database and are therefore uncovered until stubbed.

    Both are **identity** functions, and both are used to decide *which account* or *which
    channel* something belongs to. Both fail closed on an unknown input, which is the
    property worth pinning: an empty string here means "no match", so the caller's routing
    check rejects rather than proceeding against a guess.

    `_live_account_broker_login` prefers `broker_login` over `ctid`, and the preference is
    only observable on an account that carries both. That combination is the realistic
    Alpaca case - an Alpaca account that also has a legacy `ctid` - so it is the case
    asserted, not a convenience one.

    `_live_deployment_channel_ids` collects the channels live deployments actually read.
    A channel id is stripped, so a padded id still matches, and a blank one is dropped
    rather than collected - a collected `""` would group every unconfigured deployment
    into one phantom channel, and the grouping check is what prevents two bots sharing a
    token from being pointed at the same channel.
    """

    @staticmethod
    def _account(**fields):
        from types import SimpleNamespace

        return SimpleNamespace(**fields)

    def test_the_broker_login_is_preferred_over_the_ctid(self) -> None:
        """Both present, so the preference is observable. One value could not distinguish it."""
        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(
                daemon, "get_live_account",
                lambda _account_id: self._account(broker_login="PA35HB6T7815", ctid="ctid-1"),
            )
            assert daemon._live_account_broker_login("acc-1") == "PA35HB6T7815", (
                "the ctid overrode the broker_login for routing"
            )
        finally:
            monkey.undo()

    def test_the_ctid_is_used_when_no_broker_login_is_set(self) -> None:
        """cTrader's identifier lives in `ctid`; an account with only one of the two resolves."""
        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(
                daemon, "get_live_account",
                lambda _account_id: self._account(broker_login="", ctid="ctid-1"),
            )
            assert daemon._live_account_broker_login("acc-1") == "ctid-1"
        finally:
            monkey.undo()

    def test_an_unknown_or_unreadable_account_resolves_to_no_broker_login(self) -> None:
        """The fail-closed contract: absent account, and a database that refuses to answer."""
        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(daemon, "get_live_account", lambda _account_id: None)
            assert daemon._live_account_broker_login("acc-1") == ""

            def _raise(_account_id):
                raise RuntimeError("database is locked")

            monkey.setattr(daemon, "get_live_account", _raise)
            assert daemon._live_account_broker_login("acc-1") == "", (
                "a database error produced a broker identity, so routing would "
                "compare against a guess instead of failing closed"
            )
        finally:
            monkey.undo()

    @staticmethod
    def _rows(*configs):
        from types import SimpleNamespace

        return [SimpleNamespace(signal_config=c) for c in configs]

    def test_channel_ids_are_collected_stripped_and_deduped(self) -> None:
        """Stripped so a padded id still matches; deduped because a set is the contract."""
        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(
                daemon, "list_live_deployments",
                lambda: self._rows(
                    {"channel_id": " 111 "}, {"channel_id": "111"}, {"channel_id": "222"},
                ),
            )
            channels = daemon._live_deployment_channel_ids()
            assert channels == {"111", "222"}, channels
        finally:
            monkey.undo()

    def test_a_blank_or_missing_channel_id_is_not_collected(self) -> None:
        """A collected `""` would group every unconfigured deployment into a phantom channel."""
        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(
                daemon, "list_live_deployments",
                lambda: self._rows(
                    {"channel_id": ""}, {"channel_id": "   "}, {}, {"channel_id": None},
                    {"channel_id": "333"},
                ),
            )
            channels = daemon._live_deployment_channel_ids()
            assert channels == {"333"}, channels
            assert "" not in channels, channels
        finally:
            monkey.undo()

    def test_an_unreadable_deployment_table_yields_no_channels(self) -> None:
        """The same fail-closed direction: no rows readable means no channels claimed."""
        monkey = pytest.MonkeyPatch()
        try:
            def _raise():
                raise RuntimeError("database is locked")

            monkey.setattr(daemon, "list_live_deployments", _raise)
            assert daemon._live_deployment_channel_ids() == set()
        finally:
            monkey.undo()


class TestStatusObservationLedgerAndAuthLog:
    """The status-observation ledger, and the log line for a terminal balance-auth failure.

    Three mutations survived. The ledger is the interesting one, because its job is to make
    a *transition* visible, and the two things that make a transition visible are both
    easy to drop silently.

    **`previous_statuses.clear()` is load-bearing, and dropping it looks harmless.** Without
    it, a deployment removed from the database leaves its status behind. The removed-id
    log still fires - that is computed from a set difference *before* the clear - so the
    removal is still announced and the mutation looks like a no-op in the log output. What
    changes is later: if an id is re-added, its stale status is already in the ledger, so
    a deployment that comes back `ACTIVE` where it used to be `PAUSED` is only logged if
    the two happen to differ from the *stale* value rather than from nothing. Worse, the
    ledger grows without bound for every id ever seen. The test therefore asserts the
    ledger's *contents* after a removal, not merely that a line was logged.

    **An UNKNOWN status warns at `warning`, not `debug`.** The `elif status == "UNKNOWN"`
    arm is the only thing that tells an operator the database holds a status the daemon
    does not understand. The debug arm below it is for the ordinary PAUSED/STOPPED
    bookkeeping, which is genuinely uninteresting; an unrecognised status is not.

    The terminal balance-auth failure is logged at `warning` with the account, the adapter
    class and the error. It is a one-shot line, so anything lost is lost for the run - the
    whole reason it names the adapter is that an operator seeing "not authorized" needs to
    know *which* broker refused.
    """

    @staticmethod
    def _deployment(status="ACTIVE", tag=1, account_id="acc-1"):
        """`id` is a validated UUID, so the ledger keys below are built from a fixed int.

        The first version of this used readable names like "dep-1" and pydantic rejected
        them. Worth knowing: the ledger is keyed by `str(deployment.id)`, so ids in these
        tests must be UUID-shaped even though the assertions read them as opaque strings.
        """
        from uuid import UUID

        from shared.models import LiveDeploymentConfig

        return LiveDeploymentConfig(
            id=str(UUID(int=tag)), strategy_name="strat", account_id=account_id,
            symbols=["X"], timeframe="1h", status=status,
        )

    @staticmethod
    def _key(tag):
        from uuid import UUID

        return str(UUID(int=tag))

    def test_the_ledger_is_replaced_not_merged_so_removed_ids_do_not_survive(self) -> None:
        """**The surviving mutation.** Asserted on the ledger's contents, not on log output.

        Without the clear, a removed deployment's status lingers, so an id re-added later
        looks unchanged and its transition is never announced. The removal log still fires
        either way, which is exactly why this is easy to miss by reading the logs.
        """
        keep, gone = self._key(1), self._key(2)
        previous = {gone: "PAUSED", keep: "ACTIVE"}
        deployments = [self._deployment("ACTIVE", tag=1)]

        daemon._log_deployment_status_observations(deployments, previous)

        assert set(previous) == {keep}, previous
        assert previous == {keep: "ACTIVE"}, previous
        assert gone not in previous, "a removed deployment survived in the ledger"

    def test_a_re_added_deployment_with_a_changed_status_is_reported_as_a_transition(self, caplog) -> None:
        """The consequence of the stale entry, asserted end to end through the log."""
        import logging

        key = self._key(1)
        previous = {key: "PAUSED"}
        deployments = [self._deployment("ACTIVE", tag=1)]

        with caplog.at_level(logging.INFO):
            caplog.clear()
            daemon._log_deployment_status_observations(deployments, previous)

        transitions = [r for r in caplog.records if "status transition" in r.getMessage()]
        assert len(transitions) == 1, [r.getMessage() for r in caplog.records]
        data = transitions[0].__dict__.get("data") or getattr(transitions[0], "data", {})
        assert data["previous_status"] == "PAUSED", data
        assert data["status"] == "ACTIVE", data
        assert data["deployment_id"] == key, data

    def test_an_unchanged_status_is_not_reported_as_a_transition(self, caplog) -> None:
        """A warning that fires every poll trains the operator to ignore it."""
        import logging

        with caplog.at_level(logging.INFO):
            caplog.clear()
            daemon._log_deployment_status_observations(
                [self._deployment("PAUSED", tag=1)], {self._key(1): "PAUSED"}
            )
        assert not [r for r in caplog.records if "status transition" in r.getMessage()], (
            [r.getMessage() for r in caplog.records]
        )

    def test_an_unknown_status_warns_at_warning_level(self, caplog) -> None:
        """**The surviving mutation.** The only signal that a status is not understood.

        **Like the cTrader action allowlist, this arm is defence-in-depth that the model
        already enforces.** `LiveDeploymentConfig.status` is a validated
        `Literal["ACTIVE", "PAUSED", "STOPPED"]`, so a validated deployment cannot carry
        "WHATEVER" - the first version of this test learned that from a pydantic
        `literal_error`, exactly as the cTrader one did.

        `_normalize_deployment_status` maps anything outside those three to "UNKNOWN", so
        the arm is reachable only via a row that bypassed validation. It is kept and tested
        with `model_construct` rather than left as an unreachable line: the daemon's whole
        job here is noticing a status it does not understand, and an arm that cannot fire
        is not evidence of that.
        """
        import logging

        deployment = self._deployment("ACTIVE", tag=1).model_construct(
            id=self._key(1), status="WHATEVER", account_id="acc-1", strategy_name="strat"
        )
        with caplog.at_level(logging.DEBUG):
            caplog.clear()
            daemon._log_deployment_status_observations([deployment], {})
        unknown = [r for r in caplog.records if "unknown status" in r.getMessage()]
        assert len(unknown) == 1, [r.getMessage() for r in caplog.records]
        assert unknown[0].levelno == logging.WARNING, (
            f"an unrecognised status logged at {logging.getLevelName(unknown[0].levelno)}, "
            "which is the same level as the uninteresting paused/stopped bookkeeping"
        )
        data = unknown[0].__dict__.get("data") or getattr(unknown[0], "data", {})
        assert data["status"] == "WHATEVER", data
        assert data["deployment_id"] == self._key(1), data

    def test_paused_and_stopped_are_bookkeeping_not_warnings(self, caplog) -> None:
        """Both are ordinary, so they are reported at debug - deliberately uninteresting.

        **`caplog` cannot see this line, and the reason matters.** The
        `trading.live-runner` logger sets its own level to INFO and has
        `propagate=False`, so a DEBUG record is dropped *before* any handler and never
        reaches caplog regardless of `caplog.at_level(logging.DEBUG)`. My first version of
        this test asserted on the debug record via caplog and found zero of them, having
        silently proved nothing.

        Two consequences worth carrying: the negative assertion ("no warning or above")
        goes through caplog and is genuine, while the content of the debug line needs a
        handler attached to `daemon.logger` with its level lowered - done here. And in
        production this line is likewise invisible at INFO, which is the intent.
        """
        import logging

        with caplog.at_level(logging.DEBUG):
            caplog.clear()
            daemon._log_deployment_status_observations(
                [self._deployment("PAUSED", tag=1), self._deployment("STOPPED", tag=2)],
                {},
            )
        # Genuine via caplog: neither ordinary status escalates.
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            [r.getMessage() for r in caplog.records]
        )

        # The debug line's content, reached only by handling the logger directly.
        captured = []
        handler = logging.Handler()
        handler.emit = captured.append
        previous_level = daemon.logger.level
        daemon.logger.addHandler(handler)
        daemon.logger.setLevel(logging.DEBUG)
        try:
            daemon._log_deployment_status_observations(
                [self._deployment("PAUSED", tag=1), self._deployment("STOPPED", tag=2)], {}
            )
        finally:
            daemon.logger.removeHandler(handler)
            daemon.logger.setLevel(previous_level)

        debug = [r for r in captured if "non-active deployments" in r.getMessage()]
        assert len(debug) == 1, [r.getMessage() for r in captured]
        data = debug[0].__dict__.get("data") or getattr(debug[0], "data", {})
        assert data["paused_deployment_ids"] == [self._key(1)], data
        assert data["stopped_deployment_ids"] == [self._key(2)], data

    def test_a_terminal_balance_auth_failure_names_the_account_and_the_adapter(self, caplog) -> None:
        """**The surviving mutation.** One-shot line; anything lost is lost for the run."""
        import logging

        class _FakeAdapter:
            pass

        with caplog.at_level(logging.WARNING):
            caplog.clear()
            daemon._log_terminal_balance_auth_failure(
                "acc-9", _FakeAdapter(), RuntimeError("401 unauthorized")
            )
        records = [r for r in caplog.records if "not authorized" in r.getMessage().lower()]
        assert len(records) == 1, [r.getMessage() for r in caplog.records]
        assert records[0].levelno == logging.WARNING, records[0].levelname
        data = records[0].__dict__.get("data") or getattr(records[0], "data", {})
        assert data["account_id"] == "acc-9", data
        assert data["adapter"] == "_FakeAdapter", data
        assert "401 unauthorized" in data["error"], data
        assert "acc-9" in records[0].getMessage(), records[0].getMessage()


class TestTestSignalTriggerPriceFetch:
    """The price a manual signal trigger submits against, and what a missing one must do.

    The surviving mutation turned a missing symbol into `0.0`, and that is the finding:
    this price feeds `theoretical_price` into a real order-sizing path, so returning a
    number the broker never sent is worse than raising. `0.0` is a plausible value that
    flows onward as a real figure; a `KeyError` stops the trigger and names the symbol.

    **The stub has to patch the module the function imports from, not the calling module.**
    `_fetch_latest_price` does `from alpaca.data.historical import
    StockHistoricalDataClient` *inside* the function body, so setting an attribute on
    `live_runner.test_signal_trigger` has no effect - the local import shadows it. My
    first version did exactly that and made a **real network call** to
    data.alpaca.markets, which returned 401. The patch target is
    `alpaca.data.historical.StockHistoricalDataClient`.

    Worth stating plainly: that call went out. It was a read against a public market-data
    endpoint with placeholder credentials, it changed nothing, and it was not an account
    or an order - but a test that silently reaches the network is a test that can hang or
    pass for the wrong reason, so the target is pinned here.
    """

    @staticmethod
    def _stub(monkeypatch, response):
        import alpaca.data.historical as historical

        class _Client:
            def __init__(self, api_key, secret_key):
                self.credentials = (api_key, secret_key)

            def get_stock_latest_trade(self, request):
                self.request = request
                return response

        monkeypatch.setattr(historical, "StockHistoricalDataClient", _Client)

    def test_a_reported_price_is_returned(self, monkeypatch) -> None:
        from live_runner.test_signal_trigger import _fetch_latest_price

        class _Trade:
            price = 187.65

        self._stub(monkeypatch, {"AAPL": _Trade()})
        assert _fetch_latest_price("AAPL", "key", "secret") == 187.65

    def test_a_missing_symbol_raises_rather_than_reporting_zero(self, monkeypatch) -> None:
        """**The surviving mutation.** A `0.0` here becomes a real price downstream."""
        from live_runner.test_signal_trigger import _fetch_latest_price

        class _Trade:
            price = 187.65

        # AAPL absent: the broker did not know the symbol.
        self._stub(monkeypatch, {"MSFT": _Trade()})
        with pytest.raises(KeyError):
            _fetch_latest_price("AAPL", "key", "secret")

    def test_a_missing_price_raises_rather_than_reporting_zero(self, monkeypatch) -> None:
        """A trade with no price is a different absence, and it must be equally loud."""
        from live_runner.test_signal_trigger import _fetch_latest_price

        class _Trade:
            price = None

        self._stub(monkeypatch, {"AAPL": _Trade()})
        with pytest.raises((TypeError, ValueError)):
            _fetch_latest_price("AAPL", "key", "secret")

    def test_a_missing_alpaca_py_names_the_script_that_needed_it(self, monkeypatch) -> None:
        import builtins

        from live_runner.test_signal_trigger import _fetch_latest_price

        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name == "alpaca" or name.startswith("alpaca."):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        with pytest.raises(RuntimeError) as excinfo:
            _fetch_latest_price("AAPL", "key", "secret")
        message = str(excinfo.value)
        assert "alpaca-py" in message, message
        # It names the module that needed it, which is a different script from the adapter's.
        assert "test_signal_trigger" in message, message


def test_the_demo_gate_stringifies_the_account_id_before_looking_it_up(monkeypatch) -> None:
    """The account id is stringified before the verdict lookup, and that is load-bearing.

    The surviving mutation. Every other demo-gate test passes `"acc-live"`, so for them
    `str(account_id)` is a no-op and removing it changes nothing - which is how a coercion
    can sit in the most safety-critical function in the daemon untested.

    It matters because the verdict comes from the database, which is keyed by text. A caller
    passing a non-string - an int from a config file, a UUID object, a `Decimal` from a
    settings parse - would miss the row, and a missed row is an *unverifiable* account. The
    gate would still fail closed, so the damage is a refused order rather than a live one,
    but the refusal would carry a reason about an account that in fact exists, which is
    precisely the kind of misleading log line that costs an hour during an incident.

    Asserted with three non-string types and by observing what the lookup was handed, not by
    observing the verdict - the verdict is the same either way, which is the whole problem.
    """
    looked_up: list[object] = []

    def _verdict(account_id):
        looked_up.append(account_id)
        return "refused: never verified as demo"

    monkeypatch.setattr(daemon, "live_account_submission_block_reason", _verdict)

    for account_id in (12345, UUID(int=1), Decimal("7")):
        looked_up.clear()
        assert _REAL_DEPLOYMENT_ACCOUNT_IS_DEMO(account_id) is False
        assert len(looked_up) == 1, account_id
        assert isinstance(looked_up[0], str), (
            f"{type(account_id).__name__} was looked up as {type(looked_up[0]).__name__}, "
            "not str; the database is keyed by text so this misses the row"
        )
        assert looked_up[0] == str(account_id), looked_up
