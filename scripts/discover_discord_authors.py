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
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - script entry point
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_runner import service_discovery
from live_runner.credential_env import (
    discord_bot_env_key,
    load_accounts_env_values,
    resolve_env_value,
)
from live_runner.service_discovery import DEFAULT_ACCOUNTS_ENV
from live_runner.signal_sources.discord_source import DiscordSignalSource


async def _observe(
    bot: dict,
    pages: int,
    *,
    env_values: dict[str, str] | None = None,
) -> dict:
    name = str(bot.get("name") or "")
    channel_id = str(bot.get("channel_id") or "")
    values = env_values if env_values is not None else {}
    # `discover_discord_bots_from_env` returns the *key* the token lives under,
    # never the token itself. Reading `bot["token"]` (which does not exist) built
    # every source with an empty credential, so the tool could only ever report
    # 401s.
    token = resolve_env_value(values, str(bot.get("token_env_key") or ""))
    # The scheme belongs to the credential: a bot token sent raw is a 401, and
    # so is an account token sent with a "Bot " prefix. Resolved from the same
    # per-bot scope the daemon uses, not guessed here.
    from live_runner.daemon import _resolve_discord_auth_scheme

    try:
        auth_scheme = _resolve_discord_auth_scheme(name, values)
        source = DiscordSignalSource(
            bot_name=name,
            token=token,
            channel_id=channel_id,
            service_id=None,
            # Required by the constructor, and irrelevant here: nothing is parsed
            # into a signal, so no message is ever admitted or refused.
            trusted_author_ids=["*"],
            auth_scheme=auth_scheme,
        )
        # One unreadable channel must not abort the whole report. The ingest path
        # already treats 401/403 as per-channel; a discovery tool that dies on the
        # first bad token says nothing about the other two bots, which is the
        # opposite of what it was run for.
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


def _write_record(
    results: list[dict[str, object]],
    out: Path | None,
) -> None:
    """Persist what was observed, so a later session does not have to rediscover it.

    The credential outage on 2026-09-26 cost the whole author allowlist, and the
    only place it had ever existed was this tool's stdout: terminal scrollback,
    gone by the next session. Every channel would have to be re-read by hand with
    no way to check the new answer against the old one.

    So the observation gets written down. Author ids, counts and channel ids only
    - no message content, no tokens - and the default path is gitignored, because
    an author id is a small secret: it is the ingestion allowlist.
    """
    if out is None:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bots": results,
    }
    out.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"recorded to {out}", file=sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    """Separate so a test can read the tool's own defaults.

    The gitignore check has to know where the record lands, and hardcoding that
    path in the test meant it kept passing after the default moved to a committed
    location.
    """
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
    parser.add_argument(
        "--out",
        type=Path,
        # Default is inside the gitignored data dir: an author id IS the
        # ingestion allowlist, so it must never be committed by accident.
        default=Path("data/discord_authors.json"),
        help="where to record what was observed (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # `discover_discord_bots_from_env` reads DEFAULT_ACCOUNTS_ENV itself, so the
    # flag has to be applied to the module attribute rather than merely passed
    # around - otherwise --accounts-env is silently ignored and the tool reports
    # "no bots configured" against the wrong file.
    service_discovery.DEFAULT_ACCOUNTS_ENV = args.accounts_env
    env_values = load_accounts_env_values(args.accounts_env)
    bots = service_discovery.discover_discord_bots_from_env()
    if not bots:
        print(
            f"No Discord bots configured in {args.accounts_env}",
            file=sys.stderr,
        )
        return 1

    results = [
        asyncio.run(_observe(bot, args.pages, env_values=env_values)) for bot in bots
    ]

    # Recorded before the format branch, not inside the --json arm. --out has a
    # default, so the record was silently skipped for the default text output -
    # and the text output is what the operator actually runs. The whole reason
    # this file exists is that the author allowlist used to live only in terminal
    # scrollback, which the 2026-09-26 credential outage destroyed. Writing the
    # record only in the mode nobody uses defeats that.
    _write_record(results, args.out)

    # Counted once, here, and reported in both formats. --json returned a flat 0
    # no matter what it had just printed, so a caller reading the exit status saw
    # success next to a JSON body full of `error` fields. An allowlist silently
    # missing channels is the failure this tool exists to prevent, and returning
    # 0 for it made that failure invisible to any wrapper.
    failed = sum(1 for entry in results if entry["error"])

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
        return 1 if failed else 0

    for entry in results:
        print(f"\n{entry['bot']}  (channel {entry['channel_id']})")
        if entry["error"]:
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
        # Through the helper. This is the variable the operator is told to set, so
        # a key built any other way emits something the daemon will never read -
        # and the bot then falls back to the shared list, which is exactly the
        # cross-bot trust the per-bot variable exists to prevent.
        print(
            f"  {discord_bot_env_key(entry['bot'], 'TRUSTED_AUTHOR_IDS')}={ids}"
        )
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
