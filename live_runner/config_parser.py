from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from live_runner.account_routing import validate_live_account_routes
from shared.logging import get_logger
from shared.models import LiveAccountConfig, LiveDeploymentConfig

logger = get_logger("live-runner")

_UNEXPANDED_ENV_PATTERN = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
# The existing pattern insists on the closing brace, so an *unclosed* reference is invisible to
# it. Measured: `${FOO` survived expansion, passed this guard, and delivered its literal to a
# Discord channel_id - which is the silent mute #81 exists to prevent.
#
# The scope is deliberately narrow. `${}`, `${1BAD}` and `${9}` are also malformed, but they were
# recorded as ambiguous - a config could plausibly contain them as text - and that decision is
# left alone. Requiring the identifier run to end at a boundary keeps a hyphen out of it too, so
# `${VAR-NAME}` is still not this pattern's business.
#
# **This catches two different faults, not one, and the error message says so.** It fires both
# when the closing brace is missing (`${FOO`) *and* when the brace is present but the name is
# not a valid identifier (`${a.b}`, `${FOO BAR}`). An earlier version of the comment here claimed
# the second case did not exist - "there is no text that is `${FOO`" - which was simply false:
# `${a.b}` is text, and it is refused. The pattern is named for the condition it actually detects
# rather than the one it was first written for.
#
# The trailing `[^\s-]*\}?` is what makes the quoted token *whole*. Without it the match stopped
# at the end of the identifier run, so `${a.b}` was reported as `${a` and `${order.price}` as
# `${order` - a truncated string that appears nowhere in the config, which is worse for an
# operator than quoting a bare `${`. Whitespace still ends the token (there is no way to know
# where a name stops inside `${FOO BAR}`), and `-` is still excluded so `${VAR-NAME}` remains
# out of scope, as the recorded decision requires.
_MALFORMED_BRACED_ENV_PATTERN = re.compile(
    r"\$\{[A-Za-z_][A-Za-z0-9_]*(?![A-Za-z0-9_-]*\})[^\s-]*\}?"
)

# A bare `$NAME` - the braced spelling's silent sibling. `_expand_env_value` expands this shape
# whenever the name resolves, so it cannot be caught after expansion: by then `costs $USD per
# month` has already become `costs dollars per month`, and the evidence is gone. This pattern
# exists so `assert_no_bare_env_references` can look at the payload *before* that happens.
#
# Only names that actually resolve are refused, which is what separates this from the
# indiscriminate gate the backlog weighed and rejected. `pnl in $GBP` (GBP unset), `$5` and
# `100$ per trade` have no identifier run and no resolvable name, so they stay literals - a gate
# that rejected those would reject legitimate free text.
_BARE_ENV_PATTERN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_env_value(value: str, env: Mapping[str, str]) -> str:
    """Expand ``$VAR``/``${VAR}`` in *value* using *env*, and say when it could not.

    The expansion behaviour is deliberately unchanged - an unresolved reference is left in
    place exactly as before - but it is now **recorded** instead of silent. That matters
    because the value left behind is the failure mode this exists for: a channel id of
    ``${DISCORD_BOT_X_CHANNEL_ID}`` matches no channel, and the notification is dropped with
    nothing anywhere saying the reference never resolved.

    Note what this deliberately does *not* do: it does not refuse the value. An unresolved
    ``$UNSET`` and a deliberate literal ``$USD`` in free text both survive as the same string,
    so nothing downstream can tell them apart - not this function, and not a report of which
    keys failed to resolve. Refusing either would break a legitimate config; accepting both is
    why the reference is logged rather than rejected.

    Resolution is tested with ``key in env`` rather than truthiness, so a variable that is
    set-but-empty is *resolved* (to "") and is not reported as missing.
    """
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
    unresolved: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2)
        if key not in env:
            unresolved.append(key)
            return match.group(0)
        return str(env[key])

    expanded = pattern.sub(_replace, value)
    if unresolved:
        # One line per value, not per occurrence: a config that repeats a reference should
        # not produce a log line per repetition.
        logger.warning(
            "config reference(s) not present in the environment and left unexpanded: %s",
            ", ".join(sorted(set(unresolved))),
        )
    return expanded


def expand_env_mapping(payload: Any, env: Mapping[str, str] | None = None) -> Any:
    """Recursively expand env references in a parsed config mapping (#81).

    Uses *env* (file-first overlay when the caller passes accounts-env
    values), falling back to ``os.environ``.
    """
    source: Mapping[str, str] = env if env is not None else os.environ
    if isinstance(payload, dict):
        return {key: expand_env_mapping(item, source) for key, item in payload.items()}
    if isinstance(payload, list):
        return [expand_env_mapping(item, source) for item in payload]
    if isinstance(payload, str):
        return _expand_env_value(payload, source)
    return payload


def assert_no_bare_env_references(
    payload: Any,
    env: Mapping[str, str],
    *,
    context: str = "live deployment config",
) -> None:
    """Refuse bare ``$NAME`` references whose name **resolves** in *env* (#81).

    The braced spelling is refused by :func:`assert_no_unexpanded_env`, but it is refused for the
    opposite reason: ``${VAR}`` never matches anything, so leaving it in place silently mutes the
    thing it names. A bare ``$NAME`` fails differently - it is indistinguishable from a currency
    amount or a price, so it is *legitimately ambiguous*, and that is why it used to be expanded
    quietly instead of refused.

    Quiet expansion is the actual defect. ``costs $USD per month`` with ``USD`` set becomes
    ``costs dollars per month``; a credential-shaped name in prose expands the secret into a
    config value. Neither errors, and neither leaves a trace once expansion has run.

    **Why resolution is the discriminator.** A name that is *not* in *env* cannot have been
    expanded, so it was never a reference to anything - treating it as one would reject currency
    amounts and prices, which are ordinary config text. A name that *is* in *env* resolved, and
    the author either meant the reference or meant the literal; the two are no longer
    distinguishable afterwards, so this refuses and says so rather than guessing.

    The residual ambiguity is stated rather than hidden: a bare ``$NAME`` that resolves is still
    ambiguous between a deliberate reference and a currency code that collides with a variable
    name (``USD`` is not in ``.accounts.env`` today). Adding ``USD=...`` would make this gate
    reject a config that is harmless today. That is a narrower and more explicable failure than a
    credential landing in prose, and an operator hitting it gets an error naming the exact token
    and field path.
    """
    found: list[tuple[str, str]] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                _walk(item, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                _walk(item, f"{path}[{index}]")
        elif isinstance(node, str):
            for name in _BARE_ENV_PATTERN.findall(node):
                if name in env:
                    found.append((f"${name}", path or "<root>"))

    _walk(payload, "")
    if found:
        described = ", ".join(
            f"{token!r} at {path}" for token, path in sorted(set(found))
        )
        raise ValueError(
            f"Bare environment reference(s) that resolve to a defined variable: {described} - in "
            f"{context}. A '$' followed by a name expands silently, so a currency amount or price "
            "in prose can be replaced by a variable's value with no error. Each of these names is "
            "defined in the environment, so it was expanded rather than treated as text. Use the "
            "braced spelling '${NAME}' where a reference is genuinely intended."
        )


def assert_no_unexpanded_env(payload: Any, *, context: str = "live deployment config") -> None:
    """Fail closed on unexpanded ``${VAR}`` references (#81).

    A literal ``"${DISCORD_BOT_X_CHANNEL_ID}"`` never matches a real channel
    — that is a startup error, never a silent mute.
    """
    # (token, field-path) rather than a bare token. Backlog item 4 deferred widening this guard
    # until the logging had been observed, on the stated ground that "the error message names the
    # reference but not the field it came from". That part is decidable, and it is fixed here rather
    # than used as a reason to wait: an operator holding a seven-deployment YAML cannot act on a
    # token alone, because the same variable can appear in several places and only one of them is
    # wrong.
    leftovers: list[tuple[str, str]] = []
    malformed: list[tuple[str, str]] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                _walk(item, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                _walk(item, f"{path}[{index}]")
        elif isinstance(node, str):
            for match in _UNEXPANDED_ENV_PATTERN.findall(node):
                leftovers.append((match, path or "<root>"))
            for match in _MALFORMED_BRACED_ENV_PATTERN.findall(node):
                malformed.append((match, path or "<root>"))

    def _describe(found: list[tuple[str, str]]) -> str:
        return ", ".join(f"{token!r} at {path}" for token, path in sorted(set(found)))

    _walk(payload, "")
    if malformed:
        raise ValueError(
            f"Malformed environment reference(s): {_describe(malformed)} - in {context}. A '$' "
            "followed by '{' is always meant to be a reference, and each of these is not a "
            "complete ${NAME}: either the closing brace is missing, or the name contains "
            "something other than letters, digits and underscores. Nothing was expanded, so "
            "setting the variable will not help until the reference itself is well-formed."
        )
    if leftovers:
        raise ValueError(
            f"Unexpanded environment reference(s): {_describe(leftovers)} - in {context}; set "
            "them via environment or .accounts.env instead of shipping a literal '${...}' there."
        )


def _validated_config_file(config_path: str | Path) -> Path:
    path = Path(config_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Live deployment config not found: {config_path}")
    return path


def parse_live_deployments_config(
    config_path: str | Path,
    *,
    env_values: Mapping[str, str] | None = None,
) -> tuple[list[LiveAccountConfig], list[LiveDeploymentConfig]]:
    path = _validated_config_file(config_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # #81: env expansion at parse time (centrally here); unexpanded refs
    # are a startup error, never a silent mute.
    #
    # The bare-reference gate runs BEFORE expansion, and the ordering is load-bearing rather than
    # stylistic: `_expand_env_value` substitutes a bare `$NAME` whenever the name resolves, so once
    # expansion has run there is nothing left to find. Checking afterwards would pass silently on
    # every input it exists to catch, including `costs $USD per month` arriving as
    # `costs dollars per month`. The same `env` mapping the expander will use is resolved here, so
    # "resolves" means the same thing on both sides of the call.
    assert_no_bare_env_references(
        payload,
        env_values if env_values is not None else os.environ,
    )
    payload = expand_env_mapping(payload, env_values)
    assert_no_unexpanded_env(payload)
    if not isinstance(payload, dict):
        raise ValueError(
            "Live deployment config must be a YAML mapping with 'accounts' and 'deployments'."
        )

    raw_accounts = payload.get("accounts", [])
    raw_deployments = payload.get("deployments", [])
    if not isinstance(raw_accounts, list):
        raise ValueError("Live deployment config field 'accounts' must be a list.")
    if not isinstance(raw_deployments, list):
        raise ValueError("Live deployment config field 'deployments' must be a list.")

    accounts = [LiveAccountConfig.model_validate(item) for item in raw_accounts]
    deployments = [LiveDeploymentConfig.model_validate(item) for item in raw_deployments]
    validate_live_account_routes(accounts)

    account_ids = {account.id for account in accounts}
    for deployment in deployments:
        if deployment.account_id not in account_ids:
            logger.warning(
                "Live deployment references unknown account_id",
                extra={
                    "data": {
                        "strategy_name": deployment.strategy_name,
                        "account_id": deployment.account_id,
                    }
                },
            )

    return accounts, deployments
