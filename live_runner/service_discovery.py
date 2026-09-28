from __future__ import annotations

import re
from pathlib import Path

from live_runner.credential_env import load_accounts_env_values

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


def validate_discord_channel_grouping(
    discovered: list[dict[str, str]],
    *,
    deployment_channels: set[str] | None = None,
) -> list[str]:
    """Report incoherent (token, channel) groupings. Returns a list of problems.

    The grouping is inferred from naming alone: any ``DISCORD_BOT_<NAME>_TOKEN``
    is paired with ``DISCORD_BOT_<NAME>_CHANNEL_ID``. Nothing checks that the
    resulting pairs make sense, and the failure modes are not cosmetic:

    - **Two tokens on one channel** means the same Discord message is read and
      processed twice. Depending on which path wins that is a duplicated signal
      and potentially a duplicated order, and the per-broker duplicate-signal
      recovery is a second line of defence at the wrong layer to rely on.
    - **A channel with no deployment** means a service connects, authenticates and
      reads messages that nothing will ever trade.
    - **A deployment with no service** means signals silently never arrive.

    None of these raise. Discovery runs at daemon start-up and a bot that is
    merely idle should not stop the platform booting; the caller decides what to
    do with the list.
    """
    problems: list[str] = []
    by_channel: dict[str, list[str]] = {}
    for item in discovered or []:
        name = str(item.get("name") or "").strip().upper()
        channel_id = str(item.get("channel_id") or "").strip()
        if not name or not channel_id:
            continue
        by_channel.setdefault(channel_id, []).append(name)

    for channel_id, names in sorted(by_channel.items()):
        if len(names) > 1:
            problems.append(
                f"channel {channel_id} is claimed by {len(names)} Discord tokens "
                f"({', '.join(sorted(names))}); every message in it would be "
                f"processed more than once"
            )

    if deployment_channels is not None:
        configured = {str(value).strip() for value in deployment_channels if str(value).strip()}
        for channel_id in sorted(set(by_channel) - configured):
            problems.append(
                f"channel {channel_id} has a Discord service but no live deployment "
                f"reads it; it will authenticate and ingest for nothing"
            )
        for channel_id in sorted(configured - set(by_channel)):
            problems.append(
                f"channel {channel_id} has a live deployment but no Discord service, "
                f"so its signals will never arrive"
            )
    return problems
