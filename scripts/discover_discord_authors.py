"""Print the Discord author ids seen in each bot's channel.

`DISCORD_BOT_<NAME>_TRUSTED_AUTHOR_IDS` is an operator decision, and the mapping
from author id to bot is not recorded anywhere in this repository. Guessing it is
not safe: a wrong value makes that bot refuse its real signal author, and it then
stops trading while still reporting itself connected.

So read the value instead of guessing it. This is read-only: it fetches recent
messages through the same path the listener uses, builds no signal, mutates no
state and submits nothing. Tokens are never printed.

    python scripts/discover_discord_authors.py
    python scripts/discover_discord_authors.py --pages 5
    python scripts/discover_discord_authors.py --json

Review the output before pasting it. A channel can carry other traffic, so the
loudest author is not automatically the signal bot - the `is_bot` column and the
message count are both there to make that judgement visible.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - script entry point
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_runner import service_discovery
from live_runner.credential_env import load_accounts_env_values
from live_runner.service_discovery import DEFAULT_ACCOUNTS_ENV
from live_runner.signal_sources.discord_source import DiscordSignalSource


async def _observe(bot: dict, pages: int) -> dict:
    name = str(bot.get("name") or "")
    source = DiscordSignalSource(
        bot_name=name,
        token=str(bot.get("token") or ""),
        channel_id=str(bot.get("channel_id") or ""),
        service_id=None,
        # Required by the constructor, and irrelevant here: nothing is parsed
        # into a signal, so no message is ever admitted or refused.
        trusted_author_ids=["*"],
    )
    channel_id = str(bot.get("channel_id") or "")
    # One unreadable channel must not abort the whole report. The ingest path
    # already treats 401/403 as per-channel; a discovery tool that dies on the
    # first bad token says nothing about the other two bots, which is the
    # opposite of what it was run for.
    try:
        rows = await source.observe_channel_authors(channel_id, pages=pages)
        error = None
    except Exception as exc:
        rows = []
        error = f"{type(exc).__name__}: {exc}"
    return {
        "bot": name,
        "channel_id": channel_id,
        "authors": rows,
        "error": error,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pages",
        type=int,
        default=3,
        help="pages of 20 recent messages per channel (default: 3)",
    )
    parser.add_argument(
        "--accounts-env",
        type=Path,
        default=DEFAULT_ACCOUNTS_ENV,
        help="path to .accounts.env (default: the project file)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    # `discover_discord_bots_from_env` reads DEFAULT_ACCOUNTS_ENV itself, so the
    # flag has to be applied to the module attribute rather than merely passed
    # around - otherwise --accounts-env is silently ignored and the tool reports
    # "no bots configured" against the wrong file.
    service_discovery.DEFAULT_ACCOUNTS_ENV = args.accounts_env
    load_accounts_env_values(args.accounts_env)
    bots = service_discovery.discover_discord_bots_from_env()
    if not bots:
        print(
            f"No Discord bots configured in {args.accounts_env}",
            file=sys.stderr,
        )
        return 1

    results = [asyncio.run(_observe(bot, args.pages)) for bot in bots]

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
        return 0

    failed = 0
    for entry in results:
        print(f"\n{entry['bot']}  (channel {entry['channel_id']})")
        if entry["error"]:
            failed += 1
            print(f"  could not read this channel: {entry['error']}")
            print("  the token may be expired, revoked, or lack channel access")
            continue
        if not entry["authors"]:
            print("  no messages readable - check the token's channel access")
            continue
        print(f"  {'author id':22} {'messages':>9}  {'bot?':5} username")
        for row in entry["authors"]:
            print(
                f"  {row['author_id']:22} {row['message_count']:>9}  "
                f"{'yes' if row['is_bot'] else 'no':5} {row['username']}"
            )
        ids = ",".join(row["author_id"] for row in entry["authors"])
        print("\n  # review the above, then set:")
        print(f"  DISCORD_BOT_{entry['bot'].upper()}_TRUSTED_AUTHOR_IDS={ids}")
    if failed:
        print(
            f"\n{failed} of {len(results)} channels could not be read; those values "
            "cannot be discovered until the credentials work.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
