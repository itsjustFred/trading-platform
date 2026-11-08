"""StockKit Discord routing: broker identity, not the local account id.

The routing check used to compare ``deployment.account_id`` (a local UUID) with
``STOCKKIT_ACCOUNT_ROUTES`` values (Alpaca account numbers), two different
identifier spaces, so every StockKit deployment was rejected including correctly
routed ones. It also trusted a hardcoded table that went stale whenever a paper
account was re-created. These pin the replacement contract.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from live_runner.stockkit_routing import (
    expected_account_number_for_route,
    infer_stockkit_route,
    normalize_stockkit_route,
    validate_discord_deployment_signal_config,
    validate_stockkit_deployment_route,
)
from shared.models import LiveDeploymentConfig

# Synthetic, clearly-fake identifiers: no real channel id or account number is
# needed to pin routing, and none belongs in the repository.
CHANNEL_NITRO = "100000000000000001"
CHANNEL_RAMJET = "100000000000000002"
CHANNEL_ROCKETFUEL = "100000000000000003"


def _deployment(
    *,
    route: str,
    channel_id: str = CHANNEL_NITRO,
    account_id: str | None = None,
    signal_source: str = "discord",
) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name=f"discord::{route}",
        account_id=account_id or str(uuid4()),
        symbols=["AAPL"],
        timeframe="1h",
        status="PAUSED",
        signal_source=signal_source,
        signal_config={"service_name": route, "channel_id": channel_id},
    )


# ─── Route inference: the right bot for the right channel ────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("nitro", "nitro"),
        ("Nitro", "nitro"),
        ("ramjet", "ramjet"),
        ("ram_jet", "ramjet"),
        ("ram-jet", "ramjet"),
        ("rocketfuel", "rocketfuel"),
        ("rocket_fuel", "rocketfuel"),
        ("rocketfuel_longsonly", "rocketfuel"),
        ("rocketfuel_longandshort", "rocketfuel"),
    ],
)
def test_route_spellings_normalize_to_one_bot(raw: str, expected: str) -> None:
    assert normalize_stockkit_route(raw) == expected


def test_unknown_route_is_not_silently_defaulted() -> None:
    """An unrecognised name must not become some other bot's route."""
    assert normalize_stockkit_route("some-other-bot") == ""
    assert infer_stockkit_route(signal_source_id="some-other-bot") == ""


def test_route_is_inferred_from_deployment_identity() -> None:
    deployment = _deployment(route="rocketfuel", channel_id=CHANNEL_ROCKETFUEL)
    assert (
        infer_stockkit_route(
            signal_source_id=deployment.signal_source_id,
            signal_config=deployment.signal_config,
            settings=deployment.settings,
        )
        == "rocketfuel"
    )


# ─── Account-number resolution ───────────────────────────────────────────────


def test_configured_label_beats_the_static_table() -> None:
    """Paper accounts get re-created, which invalidates hardcoded numbers."""
    env = {
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-SYNTHETIC-NITRO",
        "ALPACA_ACCOUNT_NAME_ACC2": "RocketFuel",
        "ALPACA_ACCOUNT_NUMBER_ACC2": "PA-SYNTHETIC-ROCKET",
    }
    assert expected_account_number_for_route("nitro", env) == "PA-SYNTHETIC-NITRO"
    assert expected_account_number_for_route("rocketfuel", env) == "PA-SYNTHETIC-ROCKET"


def test_explicit_override_beats_a_labelled_block() -> None:
    env = {
        "STOCKKIT_ACCOUNT_NUMBER_NITRO": "PA-EXPLICIT",
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-LABELLED",
    }
    assert expected_account_number_for_route("nitro", env) == "PA-EXPLICIT"


def test_unlabelled_route_falls_back_to_the_static_table() -> None:
    assert expected_account_number_for_route("nitro", {}) == "PA3E767TJ91D"


def test_unknown_route_has_no_expected_account() -> None:
    assert expected_account_number_for_route("not-a-bot", {}) == ""


# ─── Validation compares broker identity ─────────────────────────────────────


def test_correct_broker_identity_passes_even_though_the_local_id_is_unrelated() -> None:
    env = {
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-SYNTHETIC-NITRO",
    }
    deployment = _deployment(route="nitro", account_id=str(uuid4()))

    validate_stockkit_deployment_route(
        deployment,
        account_broker_login="PA-SYNTHETIC-NITRO",
        accounts_env_values=env,
    )


def test_a_different_broker_account_is_rejected() -> None:
    env = {
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-SYNTHETIC-NITRO",
    }
    deployment = _deployment(route="nitro")

    with pytest.raises(ValueError, match="must route to Alpaca account PA-SYNTHETIC-NITRO"):
        validate_stockkit_deployment_route(
            deployment,
            account_broker_login="PA-SOMEONE-ELSE",
            accounts_env_values=env,
        )


def test_an_unreported_broker_number_is_rejected_not_waved_through() -> None:
    env = {
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-SYNTHETIC-NITRO",
    }
    deployment = _deployment(route="nitro")

    with pytest.raises(ValueError, match="no reported number"):
        validate_stockkit_deployment_route(
            deployment,
            account_broker_login="",
            accounts_env_values=env,
        )


def test_no_configured_account_number_fails_closed(monkeypatch) -> None:
    import live_runner.stockkit_routing as routing

    monkeypatch.setattr(routing, "STOCKKIT_ACCOUNT_ROUTES", {})

    with pytest.raises(ValueError, match="No Alpaca account number is configured"):
        routing.validate_stockkit_deployment_route(
            _deployment(route="nitro"),
            account_broker_login="PA-ANYTHING",
            accounts_env_values={},
        )


def test_each_route_is_validated_against_its_own_account() -> None:
    """A deployment for one bot must not accept another bot's account."""
    env = {
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-SYNTHETIC-NITRO",
        "ALPACA_ACCOUNT_NAME_ACC2": "RamJet",
        "ALPACA_ACCOUNT_NUMBER_ACC2": "PA-SYNTHETIC-RAMJET",
    }

    validate_stockkit_deployment_route(
        _deployment(route="ramjet", channel_id=CHANNEL_RAMJET),
        account_broker_login="PA-SYNTHETIC-RAMJET",
        accounts_env_values=env,
    )
    with pytest.raises(ValueError, match="Ramjet Discord signals must route"):
        validate_stockkit_deployment_route(
            _deployment(route="ramjet", channel_id=CHANNEL_RAMJET),
            account_broker_login="PA-SYNTHETIC-NITRO",
            accounts_env_values=env,
        )


# ─── Channel config: a deployment that could never fire ──────────────────────


@pytest.mark.parametrize(
    "channel_id",
    [
        "",
        "REPLACE_ME_DISCORD_CHANNEL_ID",
        "123456789",
        "1234567890123456",
        # Length-plausible scaffolding: rejected only by the placeholder list,
        # not by the snowflake length check.
        "111111111111111111",
        "000000000000000000",
    ],
)
def test_impossible_channel_ids_are_rejected(channel_id: str) -> None:
    with pytest.raises(ValueError):
        validate_discord_deployment_signal_config(
            _deployment(route="nitro", channel_id=channel_id)
        )


def test_a_plausible_channel_id_is_accepted() -> None:
    validate_discord_deployment_signal_config(
        _deployment(route="nitro", channel_id=CHANNEL_NITRO)
    )


def test_non_discord_deployment_ignores_channel_config() -> None:
    validate_discord_deployment_signal_config(
        _deployment(route="nitro", channel_id="123", signal_source="internal")
    )


def test_duplicate_labels_naming_different_accounts_are_ambiguous() -> None:
    """File order must never pick the account real orders go to."""
    env = {
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-ONE",
        "ALPACA_ACCOUNT_NAME_ACC2": "nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC2": "PA-TWO",
    }
    with pytest.raises(ValueError, match="Ambiguous Nitro route"):
        expected_account_number_for_route("nitro", env)


def test_ambiguous_label_does_not_fall_back_to_the_static_table() -> None:
    env = {
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-ONE",
        "ALPACA_ACCOUNT_NAME_ACC2": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC2": "PA-TWO",
    }
    with pytest.raises(ValueError):
        expected_account_number_for_route("nitro", env)


def test_explicit_override_still_resolves_an_ambiguous_label() -> None:
    env = {
        "STOCKKIT_ACCOUNT_NUMBER_NITRO": "PA-EXPLICIT",
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-ONE",
        "ALPACA_ACCOUNT_NAME_ACC2": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC2": "PA-TWO",
    }
    assert expected_account_number_for_route("nitro", env) == "PA-EXPLICIT"


def test_duplicate_labels_naming_the_same_account_are_not_ambiguous() -> None:
    env = {
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-SAME",
        "ALPACA_ACCOUNT_NAME_ACC2": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC2": "PA-SAME",
    }
    assert expected_account_number_for_route("nitro", env) == "PA-SAME"


def test_an_unlabelled_duplicate_without_a_number_is_ignored() -> None:
    env = {
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-ONE",
        "ALPACA_ACCOUNT_NAME_ACC2": "Nitro",
    }
    assert expected_account_number_for_route("nitro", env) == "PA-ONE"


def test_an_unreadable_accounts_env_is_logged_not_swallowed(isolated_db, monkeypatch, caplog):
    """Route validation falls back to the static table, but never silently."""
    import logging
    from uuid import uuid4

    import live_runner.credential_env as credential_env
    from shared.models import LiveDeploymentConfig

    database = isolated_db[0]
    database.ensure_live_account("acc-1", "alpaca")

    def _boom(_path):
        raise OSError("accounts env unreadable")

    monkeypatch.setattr(credential_env, "load_accounts_env_values", _boom)
    deployment = LiveDeploymentConfig(
        id=uuid4(),
        strategy_name="plain",
        account_id="acc-1",
        symbols=["AAPL"],
        timeframe="1h",
        status="PAUSED",
        signal_source="internal",
    )

    with caplog.at_level(logging.WARNING):
        database.create_live_deployment(deployment)

    assert any(
        "accounts env" in record.getMessage() and "unreadable" in record.getMessage()
        for record in caplog.records
    )
