from __future__ import annotations

import re
from pathlib import Path

from live_runner.credential_env import (
    discord_bot_env_key,
    load_accounts_env_values,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_ENV = PROJECT_ROOT / ".accounts.env"

_TOKEN_PATTERN = re.compile(r"^DISCORD_BOT_(?P<name>[A-Z0-9_]+)_TOKEN$")


def discover_discord_bots_from_env() -> list[dict[str, str]]:
    # File-first precedence: .accounts.env wins over the process environment.
    env_values = load_accounts_env_values(DEFAULT_ACCOUNTS_ENV)

    discovered: list[dict[str, str]] = []
    for key in env_values:
        token_match = _TOKEN_PATTERN.match(key)
        if token_match is None:
            continue
        bot_name = token_match.group("name")
        # Through the helper even though `bot_name` is already canonical, because
        # it came from a regex here rather than from the canonicaliser. Spelling the
        # prefix out in two places is how the copies drift; this one is a
        # round-trip today and a divergence the moment the pattern is widened.
        token_env_key = discord_bot_env_key(bot_name, "TOKEN")
        channel_key = discord_bot_env_key(bot_name, "CHANNEL_ID")
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
