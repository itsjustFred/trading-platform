"""Deterministic StockKit Discord account routing."""
from __future__ import annotations

import re
from collections.abc import Mapping

from shared.models import LiveDeploymentConfig

# Fallback account numbers, used only when the accounts-env file does not name a
# block for the route. These are Alpaca *account numbers*, not local account ids.
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

# ALPACA_ACCOUNT_NAME_ACC{N} holds a human label such as "Nitro" or "RocketFuel".
_INDEXED_BLOCK = re.compile(r"^ALPACA_ACCOUNT_NAME_ACC(?P<index>\d+)$")


def _normalize_account_label(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())


def expected_account_number_for_route(
    route: str,
    accounts_env_values: Mapping[str, str] | None = None,
) -> str:
    """Resolve the Alpaca account *number* a StockKit route must trade on.

    Resolution order:

    1. An explicit ``STOCKKIT_ACCOUNT_NUMBER_<ROUTE>`` entry in accounts-env.
    2. The ``ALPACA_ACCOUNT_NUMBER_ACC{N}`` whose ``ALPACA_ACCOUNT_NAME_ACC{N}``
       label matches the route. This keeps routing correct when paper accounts
       are re-created, which silently invalidates any hardcoded number.
    3. The static fallback table.

    Raises ``ValueError`` when step 2 is ambiguous (several blocks share the label but
    name different account numbers); that must never resolve by file order.
    """
    normalized = normalize_stockkit_route(route)
    if not normalized:
        return ""
    env = accounts_env_values or {}

    explicit = str(env.get(f"STOCKKIT_ACCOUNT_NUMBER_{normalized.upper()}", "")).strip()
    if explicit:
        return explicit

    labelled: dict[str, str] = {}
    for key, value in env.items():
        match = _INDEXED_BLOCK.match(str(key))
        if match is None:
            continue
        if _normalize_account_label(value) != _normalize_account_label(normalized):
            continue
        number = str(env.get(f"ALPACA_ACCOUNT_NUMBER_ACC{match.group('index')}", "")).strip()
        if number:
            labelled[str(key)] = number
    if len(set(labelled.values())) > 1:
        # Two blocks carry the same label but name different accounts. Picking the first
        # in file order would route real orders to whichever block happens to come first,
        # and falling through to the static table would hide the misconfiguration. Refuse.
        raise ValueError(
            f"Ambiguous {normalized.title()} route: {len(labelled)} ALPACA_ACCOUNT_NAME_ACC{{N}} "
            f"blocks carry that label ({', '.join(sorted(labelled))}) and name different "
            "account numbers; give each account a unique label or set "
            f"STOCKKIT_ACCOUNT_NUMBER_{normalized.upper()}"
        )
    if labelled:
        return next(iter(labelled.values()))

    return STOCKKIT_ACCOUNT_ROUTES.get(normalized, "")


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


#: The explicit StockKit marking on a deployment (#121 decision 5). Present means
#: "this deployment is a StockKit bot and must route to that bot's account";
#: absent means a normal Discord bot.
STOCKKIT_ROUTE_CONFIG_KEY = "stockkit_route"


def is_stockkit_marked(deployment: LiveDeploymentConfig) -> bool:
    """True when the deployment carries an explicit ``stockkit_route``, whatever
    its value. A marked-but-empty value is still marked, and is rejected."""
    config = deployment.signal_config if isinstance(deployment.signal_config, dict) else {}
    return STOCKKIT_ROUTE_CONFIG_KEY in config


def resolve_deployment_stockkit_route(deployment: LiveDeploymentConfig) -> str:
    """The StockKit route a deployment must be validated against, or ``""``.

    An explicitly marked deployment must name a known route: ``normalize_stockkit_route``
    only knows fixed aliases, so a bot such as ``rocketfuel_shortsonly`` or
    ``nitro_v2`` normalised to ``""`` and skipped the account check entirely. A
    marked route that is unknown, blank or not a string raises ``ValueError``, and so
    does one that contradicts the route inferred from the bot's own identity.

    An unmarked deployment keeps the inferred route, so a deployment whose bot name
    is a known alias is still validated (stricter than "unmarked means unchecked",
    and what every existing StockKit deployment relies on until it is marked).
    """
    inferred = infer_stockkit_route(
        signal_source_id=deployment.signal_source_id,
        signal_config=deployment.signal_config,
        settings=deployment.settings,
    )
    if not is_stockkit_marked(deployment):
        return inferred
    raw = deployment.signal_config[STOCKKIT_ROUTE_CONFIG_KEY]
    marked = normalize_stockkit_route(raw) if isinstance(raw, str) else ""
    if not marked:
        raise ValueError(
            f"Deployment {deployment.id} is marked as StockKit with "
            f"{STOCKKIT_ROUTE_CONFIG_KEY}={raw!r}, which is not a known route "
            f"(expected one of {sorted(STOCKKIT_ACCOUNT_ROUTES)}); refusing to "
            "skip the broker-account check"
        )
    if inferred and inferred != marked:
        raise ValueError(
            f"Deployment {deployment.id} is marked {STOCKKIT_ROUTE_CONFIG_KEY}="
            f"{marked!r} but its bot identity says {inferred!r}; refusing an "
            "ambiguous StockKit route"
        )
    return marked


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


def validate_stockkit_deployment_route(
    deployment: LiveDeploymentConfig,
    *,
    account_broker_login: str | None = None,
    accounts_env_values: Mapping[str, str] | None = None,
) -> None:
    """Fail closed on Discord deployment config and StockKit account routing.

    This is the single chokepoint every Discord deployment passes through (DB
    create, viewer API, daemon sync, smoke script), so the config sanity check
    lives here to guarantee it cannot be bypassed by a new call site.

    ``account_broker_login`` is the broker-reported account number for
    ``deployment.account_id``. It must be supplied: comparing the local account
    id against an Alpaca account number compares two different identifier spaces
    and would reject every deployment, including correctly routed ones.
    """
    validate_discord_deployment_signal_config(deployment)
    if str(deployment.signal_source or "").lower() != "discord":
        if is_stockkit_marked(deployment):
            raise ValueError(
                f"Deployment {deployment.id} is marked {STOCKKIT_ROUTE_CONFIG_KEY} but "
                "is not a Discord deployment"
            )
        return
    route = resolve_deployment_stockkit_route(deployment)
    if not route:
        return
    expected_number = expected_account_number_for_route(route, accounts_env_values)
    if not expected_number:
        # Nothing to compare against: fail closed rather than wave it through.
        raise ValueError(
            f"No Alpaca account number is configured for the {route.title()} route; "
            "set STOCKKIT_ACCOUNT_NUMBER_%s or label an ALPACA_ACCOUNT_NAME_ACC{N} block"
            % route.upper()
        )
    actual_number = str(account_broker_login or "").strip()
    if actual_number != expected_number:
        raise ValueError(
            f"{route.title()} Discord signals must route to Alpaca account "
            f"{expected_number}; deployment {deployment.account_id} is on "
            f"{actual_number or 'an account with no reported number'}"
        )
