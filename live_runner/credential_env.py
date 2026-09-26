"""Shared credential/env resolution for live trading components.

Two rules live here so every live_runner entry point resolves configuration
identically:

- **Strict booleans.** A typo'd value raises instead of silently selecting a
  default that may flip paper -> live or disable a safety behaviour.
- **File-first precedence.** `.accounts.env` wins over `os.environ`; the
  process environment only fills keys the file does not define. Refreshed
  credentials are persisted to the file, so the file must stay the source of
  truth — otherwise a stale process-env copy would shadow the fresh value.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

from shared.logging import get_logger

logger = get_logger("live-runner")

TRUE_BOOL_TOKENS = frozenset({"1", "true", "yes", "y", "on"})
FALSE_BOOL_TOKENS = frozenset({"0", "false", "no", "n", "off"})

DISCORD_BOT_ENV_PREFIX = "DISCORD_BOT_"


def discord_bot_env_canonical_name(name: str) -> str:
    """Canonicalise a bot/service name for use in a ``DISCORD_BOT_<NAME>_`` key.

    Per-bot credentials are matched by ``^DISCORD_BOT_[A-Z0-9_]+_...`` in
    :mod:`live_runner.service_discovery`, so a name that keeps its spaces or
    punctuation can never be configured at all. Every per-bot key (token, auth
    scheme, ...) must be built through this helper, otherwise the keys for two
    credentials of the same bot silently diverge.
    """
    return re.sub(r"[^A-Z0-9_]+", "_", str(name or "").strip().upper())


def discord_bot_env_key(name: str, suffix: str) -> str:
    """Build the per-bot env key for a credential, e.g. ``_TOKEN``/``_AUTH_SCHEME``."""
    canonical = discord_bot_env_canonical_name(name)
    if not canonical:
        return ""
    return f"{DISCORD_BOT_ENV_PREFIX}{canonical}_{suffix}"


def resolve_credential_value(
    accounts_env_values: Mapping[str, str],
    key: str,
    *,
    env_key: str | None = None,
) -> str:
    """Resolve one credential with the file-first precedence rule of this module.

    ``.accounts.env`` wins over the process environment; the environment only
    fills keys the file does not define.
    """
    if not key:
        return ""
    resolved = str(accounts_env_values.get(key) or "").strip()
    if resolved:
        return resolved
    return str(os.environ.get(env_key or key, "") or "").strip()


def parse_bool_env(name: str, value: str | None, *, default: bool) -> bool:
    """Strictly parse a boolean config value.

    Missing value -> default. Recognized true/false tokens (case-insensitive,
    stripped) parse accordingly. Anything else raises so a malformed value can
    never implicitly select live trading or disable a safety behaviour.
    """
    if value is None:
        return default
    token = value.strip().lower()
    if token in TRUE_BOOL_TOKENS:
        return True
    if token in FALSE_BOOL_TOKENS:
        return False
    valid = ", ".join(sorted(TRUE_BOOL_TOKENS | FALSE_BOOL_TOKENS))
    raise ValueError(f"Invalid boolean value for {name}: {value!r}. Valid values are: {valid}.")


def load_accounts_env_values(
    accounts_env: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Load `.accounts.env` and overlay the process environment file-first.

    The file wins for every key it defines; the environment only fills keys
    the file does not define. A present-but-empty file value stays empty (it
    is still "defined" by the file and must not be shadowed).
    """
    env_source = os.environ if environ is None else environ
    values: dict[str, str] = {}
    try:
        loaded = dotenv_values(accounts_env)
    except FileNotFoundError:
        # No file is the normal single-machine setup: env-only credentials.
        loaded = {}
    except OSError as exc:
        # A file that exists but cannot be read is a config error. Discovery
        # decides from this result which accounts exist, so a silent empty
        # result would look like "no accounts configured" instead of a
        # filesystem problem.
        logger.warning(
            "Could not read accounts env file; continuing with process env only",
            extra={"data": {"accounts_env": str(accounts_env), "error": str(exc)}},
        )
        loaded = {}
    for key, value in loaded.items():
        if key and isinstance(value, str):
            values[key] = value.strip()
    for key, value in env_source.items():
        if key in values:
            continue
        if isinstance(value, str):
            values[key] = value.strip()
    return values


def resolve_env_value(
    accounts_env_values: Mapping[str, str] | None,
    key: str,
    default: str = "",
) -> str:
    """Resolve one key file-first (C1: credential-precedence source of truth).

    A key DEFINED in `.accounts.env` wins even when its value is empty
    (present-but-empty stays empty); `os.environ` only fills keys ABSENT
    from the file. This closes the `values.get(k) or os.environ.get(k)`
    hole where a stale process-env value shadowed a fresh (possibly
    intentionally blanked) file value.
    """
    if accounts_env_values is not None and key in accounts_env_values:
        value = accounts_env_values[key]
        return value.strip() if isinstance(value, str) else default
    return str(os.environ.get(key, default)).strip()


def accounts_env_mtime(accounts_env: Path | str) -> int | None:
    try:
        return Path(accounts_env).stat().st_mtime_ns
    except OSError:
        return None
