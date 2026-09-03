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
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

from shared.logging import get_logger

logger = get_logger("live-runner")

TRUE_BOOL_TOKENS = frozenset({"1", "true", "yes", "y", "on"})
FALSE_BOOL_TOKENS = frozenset({"0", "false", "no", "n", "off"})


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


def accounts_env_mtime(accounts_env: Path | str) -> int | None:
    try:
        return Path(accounts_env).stat().st_mtime_ns
    except OSError:
        return None
