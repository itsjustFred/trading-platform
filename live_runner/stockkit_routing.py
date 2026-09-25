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


# Obvious scaffolding values that must never be treated as a real Discord
# channel. A deployment carrying one of these looks configured but can never
# match a real message, so it would sit silent forever.
_PLACEHOLDER_CHANNEL_IDS = frozenset(
    {
        "0",
        "1",
        "123",
        "1234",
        "12345",
        "123456",
        "1234567",
        "12345678",
        "123456789",
        "987654321",
        "000000000000000000",
        "111111111111111111",
        "222222222222222222",
    }
)

# Snowflake ids issued by Discord are strictly increasing integers well above
# the 17-19 digit range. Anything shorter is not a real channel id.
_MIN_PLAUSIBLE_CHANNEL_ID_LENGTH = 17


def validate_discord_deployment_signal_config(
    deployment: LiveDeploymentConfig,
) -> None:
    """Fail closed on Discord deployment config that could never fire.

    Catches the two silent-failure modes seen in real data:

    * a placeholder channel id (scaffolding that was never filled in), and
    * a channel id that is not a syntactically plausible Discord snowflake.
    """
    if str(deployment.signal_source or "").lower() != "discord":
        return

    config = deployment.signal_config if isinstance(deployment.signal_config, dict) else {}
    raw_channel = config.get("channel_id")
    if raw_channel is None:
        return

    channel_id = str(raw_channel).strip()
    if not channel_id:
        raise ValueError("Discord deployment has an empty channel_id")
    if not channel_id.isdigit():
        raise ValueError(
            f"Discord deployment channel_id '{channel_id}' is not numeric"
        )
    if channel_id in _PLACEHOLDER_CHANNEL_IDS or len(channel_id) < _MIN_PLAUSIBLE_CHANNEL_ID_LENGTH:
        raise ValueError(
            f"Discord deployment channel_id '{channel_id}' looks like a placeholder; "
            "it can never match a real Discord channel, so this deployment would "
            "silently never receive signals"
        )


def validate_stockkit_deployment_route(deployment: LiveDeploymentConfig) -> None:
    """Fail closed on Discord deployment config and StockKit account routing.

    This is the single chokepoint every Discord deployment passes through (DB
    create, viewer API, daemon sync, smoke script), so the config sanity check
    lives here to guarantee it cannot be bypassed by a new call site.
    """
    validate_discord_deployment_signal_config(deployment)
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
