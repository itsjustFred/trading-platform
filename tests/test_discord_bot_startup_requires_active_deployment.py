"""A Discord bot may only start if its deployment is ACTIVE.

Bot discovery reads `.accounts.env`, so it finds a token for every configured bot whether or not that
bot is meant to be running. Discovery and deployment state were separate systems and only one of them
was consulted - so `discord::nitro`, `discord::ramjet` and `discord::rocketfuel` are all PAUSED and
their bots still started on every daemon boot.

Starting a bot is a connection. The operator's standing rule is that those three stay failed and no
restart may be used to "test" them, so a restart that deploys code is also a Discord contact - which is
how this became load-bearing rather than cosmetic.

The gate reads deployment status, and fails **closed**: an unreadable deployment set starts nothing,
because failing open would connect to a source the operator disabled.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from live_runner import daemon


def _deployment(name: str, status: str, source_type: str = "discord") -> SimpleNamespace:
    return SimpleNamespace(
        signal_source_id=name,
        signal_source_type=source_type,
        status=status,
        signal_config={"service_name": name},
        strategy_name=f"{source_type}::{name.lower()}",
    )


@pytest.fixture
def deployments(monkeypatch):
    def _install(rows):
        monkeypatch.setattr(daemon, "list_live_deployments", lambda: rows)

    return _install


def test_only_an_active_discord_deployment_may_start(deployments):
    deployments([
        _deployment("NITRO", "ACTIVE"),
        _deployment("RAMJET", "PAUSED"),
        _deployment("ROCKETFUEL", "STOPPED"),
    ])

    assert daemon._active_discord_service_names() == {"NITRO"}, (
        "a paused or stopped deployment must not authorise its bot to start - starting a bot is a "
        "connection, which is a stronger condition than 'do not trade this'"
    )


def test_the_three_disabled_bots_authorise_nothing(deployments):
    """The exact production shape, and the reason the gate exists."""
    deployments([
        _deployment("NITRO", "PAUSED"),
        _deployment("RAMJET", "PAUSED"),
        _deployment("ROCKETFUEL", "PAUSED"),
    ])

    assert daemon._active_discord_service_names() == set()


def test_internal_deployments_never_authorise_a_bot(deployments):
    """An ACTIVE internal deployment is not a Discord source, so it authorises nothing."""
    deployments([_deployment("XAUUSD-STRAT", "ACTIVE", source_type="internal")])

    assert daemon._active_discord_service_names() == set()


def test_an_unreadable_deployment_set_fails_closed(monkeypatch):
    """Failing open would connect to a disabled source, which is the thing the gate prevents."""
    def _boom():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(daemon, "list_live_deployments", _boom)

    assert daemon._active_discord_service_names() == set(), (
        "an unreadable deployment set must start no bots; failing open here is exactly the bug the "
        "gate exists to stop"
    )


def test_a_deployment_with_no_service_name_authorises_nothing(deployments):
    deployments([SimpleNamespace(signal_source_id="", signal_source_type="discord",
                                 status="ACTIVE", signal_config={})])

    assert daemon._active_discord_service_names() == set()


def test_discovery_still_finds_the_bots_it_only_filters_at_startup(monkeypatch, deployments):
    """Discovery is unchanged; only startup is gated.

    The bots must still be *discovered*, because that is what drives the log line an operator reads to
    confirm the gate is working. Filtering discovery instead would hide the fact that credentials are
    still present.
    """
    deployments([_deployment("NITRO", "PAUSED")])
    monkeypatch.setattr(
        daemon,
        "discover_discord_bots_from_env",
        lambda: [{"name": "NITRO", "token_env_key": "DISCORD_BOT_NITRO_TOKEN", "channel_id": "1"}],
    )
    monkeypatch.setattr(daemon, "validate_discord_channel_grouping", lambda *a, **k: [])
    monkeypatch.setattr(daemon, "list_services", lambda **k: [])
    created = []
    monkeypatch.setattr(
        daemon, "_register_discord_service", lambda **kw: created.append(kw), raising=False
    )
    monkeypatch.setattr(daemon, "ServiceType", SimpleNamespace(DISCORD_BOT="discord_bot"))

    daemon._discover_and_register_discord_services(log_summary=True)

    assert created == [], (
        "a PAUSED deployment must not produce a registered bot service - if this fails the bot was "
        "started, which is the Discord contact this gate exists to prevent"
    )
