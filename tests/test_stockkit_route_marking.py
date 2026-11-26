"""#121 decision 5: a StockKit deployment is marked explicitly, and fails closed.

``normalize_stockkit_route`` only knows fixed aliases, so a bot such as
``rocketfuel_shortsonly`` or ``nitro_v2`` normalised to ``""`` and skipped the
broker-account check entirely. A deployment now marks itself as StockKit with
``signal_config.stockkit_route``. A marked deployment whose route is unknown or
unresolvable is rejected at creation and at dispatch, and its service is marked
unhealthy. An unmarked deployment is a normal Discord bot.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from live_runner.stockkit_routing import validate_stockkit_deployment_route
from shared.models import LiveDeploymentConfig

CHANNEL = "100000000000000001"
ENV = {
    "ALPACA_ACCOUNT_NAME_ACC1": "RocketFuel",
    "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-SYNTHETIC-ROCKET",
    "ALPACA_ACCOUNT_NAME_ACC2": "Nitro",
    "ALPACA_ACCOUNT_NUMBER_ACC2": "PA-SYNTHETIC-NITRO",
}


def _deployment(
    *,
    stockkit_route: object = None,
    marked: bool = True,
    service_name: str = "rocketfuel_shortsonly",
    signal_source_id: str | None = None,
) -> LiveDeploymentConfig:
    signal_config: dict[str, object] = {"service_name": service_name, "channel_id": CHANNEL}
    if marked:
        signal_config["stockkit_route"] = stockkit_route
    from shared.models import STORED_ROW_CONTEXT

    # Discord ingestion is retired, so the model refuses the source on a new
    # deployment; these tests exercise the route validator that still guards
    # stored discord rows, so the fixture loads through the stored-row path.
    return LiveDeploymentConfig.model_validate(
        {
            "id": str(uuid4()),
            "strategy_name": f"discord::{service_name}",
            "account_id": str(uuid4()),
            "symbols": ["AAPL"],
            "timeframe": "1h",
            "status": "ACTIVE",
            "signal_source": "discord",
            "signal_source_type": "discord",
            "signal_source_id": signal_source_id,
            "signal_config": signal_config,
        },
        context={STORED_ROW_CONTEXT: True},
    )


@pytest.mark.parametrize(
    "route",
    ["rocketfuel_shortsonly", "nitro_v2", "", "   ", None, 7, ["nitro"]],
)
def test_a_marked_deployment_with_an_unknown_route_is_rejected(route) -> None:
    with pytest.raises(ValueError, match="stockkit_route"):
        validate_stockkit_deployment_route(
            _deployment(stockkit_route=route),
            account_broker_login="PA-SYNTHETIC-ROCKET",
            accounts_env_values=ENV,
        )


def test_a_marked_route_is_validated_against_its_account() -> None:
    """The marked route resolves even when the bot name is not an alias."""
    deployment = _deployment(stockkit_route="rocketfuel")

    validate_stockkit_deployment_route(
        deployment,
        account_broker_login="PA-SYNTHETIC-ROCKET",
        accounts_env_values=ENV,
    )
    with pytest.raises(ValueError, match="must route to Alpaca account PA-SYNTHETIC-ROCKET"):
        validate_stockkit_deployment_route(
            deployment,
            account_broker_login="PA-SYNTHETIC-NITRO",
            accounts_env_values=ENV,
        )


def test_a_marked_route_without_a_resolvable_account_is_rejected(monkeypatch) -> None:
    import live_runner.stockkit_routing as routing

    monkeypatch.setattr(routing, "STOCKKIT_ACCOUNT_ROUTES", {})
    with pytest.raises(ValueError, match="No Alpaca account number is configured"):
        routing.validate_stockkit_deployment_route(
            _deployment(stockkit_route="ramjet"),
            account_broker_login="PA-ANYTHING",
            accounts_env_values={},
        )


def test_a_marked_route_that_contradicts_the_bot_identity_is_rejected() -> None:
    """Marked "nitro" on a deployment whose service is RocketFuel: refuse."""
    with pytest.raises(ValueError, match="stockkit_route"):
        validate_stockkit_deployment_route(
            _deployment(stockkit_route="nitro", service_name="rocketfuel_longsonly"),
            account_broker_login="PA-SYNTHETIC-NITRO",
            accounts_env_values=ENV,
        )


def test_an_unmarked_unknown_bot_is_a_normal_discord_bot() -> None:
    validate_stockkit_deployment_route(
        _deployment(marked=False, service_name="nitro_v2"),
        account_broker_login="PA-WHATEVER",
        accounts_env_values=ENV,
    )


def test_creation_rejects_a_marked_unknown_route(isolated_db) -> None:
    database, _engine, _data_dir = isolated_db
    database.init_db()
    account_id = "acc-stockkit-121"
    with database.get_session() as session:
        session.add(
            database.LiveAccountModel(
                id=account_id,
                broker="alpaca",
                name=account_id,
                broker_login="PA-SYNTHETIC-ROCKET",
            )
        )
        session.commit()
    deployment = _deployment(stockkit_route="rocketfuel_shortsonly").model_copy(
        update={"account_id": account_id}
    )

    with pytest.raises(ValueError, match="stockkit_route"):
        database.create_live_deployment(deployment)
    assert database.list_live_deployments() == []
