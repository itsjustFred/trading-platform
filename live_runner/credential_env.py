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
    # A name made only of punctuation canonicalises to "_", which is not a name
    # an operator could ever have written in the variable. Returning a key for it
    # is worse than returning none: the lookup then misses silently and the bot
    # falls back to the shared author list, which looks like a lookup that
    # happened. Found by mutation testing, not by reading.
    if not canonical.strip("_"):
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

    This is the only definition. It was previously written out four times -
    here, in `ctrader_adapter.py`, `daemon.py` and `preflight.py` - as three
    private copies plus this one, along with two parallel token sets.

    The failure mode is the reason that mattered. The rule had already been got
    wrong twice before it was right, once per broker, and both times the same
    way: an `or None` collapsed a *blanked* value to "unset", so a value that
    should have been a loud parse error instead silently selected the default -
    and the default was demo mode. The notes record both.

    Four copies that agree today is the same hazard as every other duplicate
    this audit has found, and it is the one shape that has already produced two
    live bugs in this repository. Nothing related the copies, so a fix applied
    to one of them would have left the other three on the old behaviour - which
    is precisely how the second instance survived the first fix.
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


# Cache for `resolve_flag_env`, keyed on the accounts-env file's mtime. The daemon already uses
# this idiom for its own reload (`daemon.py`: load once, reload when the mtime changes), so it is
# not a new pattern - the same one, applied to a value read per call rather than per tick.
# `_UNSET` rather than `None` as the initial mtime: `accounts_env_mtime` returns `None` when
# the file does not exist, so a `None` sentinel compares equal on the very first call, the
# mapping is never loaded, and every lookup reads through an empty cache forever.
_UNSET: object = object()
_FLAG_ENV_CACHE: dict[str, object] = {"mtime": _UNSET, "values": None}


def resolve_flag_env(
    key: str,
    *,
    accounts_env: Path | str | None = None,
) -> str | None:
    """The value of a **runtime flag**, file-first, or `None` when the key is defined nowhere.

    `resolve_env_value` takes the already-loaded mapping, which is right for credentials: the caller
    has it, because it is the thing that owns the connection. It is wrong for a flag read from a
    helper that has no such mapping - the caller then has no way to supply one, and silently falls
    back to `os.environ` only.

    That is not hypothetical. `CTRADER_HONOUR_ZERO_MONEY_DIGITS` is the switch that implements
    backlog item 6, and it was read from the process environment alone while every other live flag -
    including `CTRADER_DEMO`, read a few lines away in the same constructor - is file-first. An
    operator who put the flag in `.accounts.env` beside `CTRADER_DEMO`, as the runbook tells them
    to, got the default with no error and no warning.

    **Returns `None` rather than `""` for an absent key**, because that distinction is the
    whole point for a boolean: `parse_bool_env(name, None, default=...)` treats absent as the
    default and present-but-blank as an error, and collapsing the two here would lose that.

    The file is read at most once per change, keyed on mtime, so calling this per invocation
    is cheap. The `accounts_env` path defaults to the same `<repo>/.accounts.env` the rest of
    the codebase uses, computed the same way rather than adding a seventh copy of it.
    """
    path = (
        Path(accounts_env)
        if accounts_env is not None
        else Path(__file__).resolve().parent.parent / ".accounts.env"
    )
    mtime = accounts_env_mtime(path)
    if _FLAG_ENV_CACHE["mtime"] != mtime:
        _FLAG_ENV_CACHE["mtime"] = mtime
        _FLAG_ENV_CACHE["values"] = load_accounts_env_values(path)
    values = _FLAG_ENV_CACHE["values"]
    if not isinstance(values, dict):  # unreachable via the branch above; narrows the type
        raise RuntimeError(f"accounts-env cache was not populated for {path}")
    if key in values:
        return resolve_env_value(values, key)
    return os.environ.get(key)


def reset_flag_env_cache() -> None:
    """Forget the cached accounts-env mapping. For tests."""
    _FLAG_ENV_CACHE["mtime"] = _UNSET
    _FLAG_ENV_CACHE["values"] = None


def accounts_env_mtime(accounts_env: Path | str) -> int | None:
    try:
        return Path(accounts_env).stat().st_mtime_ns
    except OSError:
        return None
