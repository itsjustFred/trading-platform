from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_ENV = PROJECT_ROOT / ".accounts.env"

_TOKEN_PATTERN = re.compile(r"^DISCORD_BOT_(?P<name>[A-Z0-9_]+)_TOKEN$")


def discover_discord_bots_from_env() -> list[dict[str, str]]:
    env_values: dict[str, str] = {}
    if DEFAULT_ACCOUNTS_ENV.exists():
        loaded = dotenv_values(DEFAULT_ACCOUNTS_ENV)
        for key, value in loaded.items():
            if key and isinstance(value, str):
                env_values[key] = value.strip()
    for key, value in os.environ.items():
        if key.startswith("DISCORD_BOT_"):
            env_values[key] = value.strip()

    discovered: list[dict[str, str]] = []
    for key in env_values:
        token_match = _TOKEN_PATTERN.match(key)
        if token_match is None:
            continue
        bot_name = token_match.group("name")
        token_env_key = f"DISCORD_BOT_{bot_name}_TOKEN"
        channel_key = f"DISCORD_BOT_{bot_name}_CHANNEL_ID"
        channel_id = str(env_values.get(channel_key, "")).strip()
        if not channel_id:
            continue
        discovered.append(
            {
                "name": bot_name,
                "token_env_key": token_env_key,
                "channel_id": channel_id,
            }
        )
    return discovered
