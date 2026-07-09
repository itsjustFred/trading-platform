"""Deterministic StockKit Discord account routing."""
from __future__ import annotations

from collections.abc import Mapping

from shared.models import LiveDeploymentConfig

STOCKKIT_ACCOUNT_ROUTES: dict[str, str] = {
    "nitro": "PA3E767TJ91D",
    "ramjet": "PA3T0MQAGQ51",
    "rocketfuel": "PA3EFBL4P7BU",
}

_ALIASES: dict[str, str] = {
    "nitro": "nitro",
    "ramjet": "ramjet",
    "ram_jet": "ramjet",
    "ram jet": "ramjet",
    "rocketfuel": "rocketfuel",
    "rocket_fuel": "rocketfuel",
    "rocket fuel": "rocketfuel",
    "rocketfuel_longsonly": "rocketfuel",
    "rocketfuel_longandshort": "rocketfuel",
}


def normalize_stockkit_route(value: object) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    return _ALIASES.get(text, "")


def infer_stockkit_route(
    *,
    signal_source_id: object = None,
    signal_config: Mapping[str, object] | None = None,
    settings: Mapping[str, object] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> str:
    """Infer the StockKit bot route from stable deployment/signal fields."""
    config = signal_config or {}
    runtime_settings = settings or {}
    signal_metadata = metadata or {}
    candidates = [
        signal_source_id,
        config.get("service_name"),
        config.get("signal_processor"),
        runtime_settings.get("signal_processor"),
        signal_metadata.get("discord_bot_name"),
        signal_metadata.get("signal_bot"),
        signal_metadata.get("source"),
    ]
    for candidate in candidates:
        route = normalize_stockkit_route(candidate)
        if route:
            return route
    return ""


def expected_account_for_stockkit_route(route: str) -> str:
    return STOCKKIT_ACCOUNT_ROUTES[route]


def validate_stockkit_deployment_route(deployment: LiveDeploymentConfig) -> None:
    """Fail closed when a known StockKit Discord bot targets the wrong account."""
    if str(deployment.signal_source or "").lower() != "discord":
        return
    route = infer_stockkit_route(
        signal_source_id=deployment.signal_source_id,
        signal_config=deployment.signal_config,
        settings=deployment.settings,
    )
    if not route:
        return
    expected_account = expected_account_for_stockkit_route(route)
    if deployment.account_id != expected_account:
        raise ValueError(
            f"{route.title()} Discord signals must route to Alpaca account {expected_account}; "
            f"got {deployment.account_id}"
        )
