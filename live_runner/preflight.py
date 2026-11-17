from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import traceback
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from live_runner.account_routing import ctrader_account_identifier
from live_runner.alpaca_adapter import AlpacaLiveAdapter, resolve_alpaca_credentials
from live_runner.config_parser import parse_live_deployments_config
from live_runner.credential_env import overlay_process_env, read_accounts_env_file
from live_runner.ctrader_adapter import DEFAULT_ACCOUNTS_ENV, CTraderLiveAdapter
from shared.logging import get_logger, route_console_logs_to_stderr
from shared.models import LiveAccountConfig, LiveDeploymentConfig

logger = get_logger("live-runner")


@dataclass(frozen=True)
class LivePreflightAccountResult:
    account_id: str
    broker: str
    ok: bool
    paper: bool | None = None
    demo: bool | None = None
    connected: bool | None = None
    balance: float | None = None
    equity: float | None = None
    positions_count: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LivePreflightResult:
    ok: bool
    config_path: str
    accounts_env_path: str
    accounts: list[LivePreflightAccountResult]
    deployment_errors: list[str] = field(default_factory=list)
    deployment_warnings: list[str] = field(default_factory=list)


def _accounts_env_views(accounts_env: Path) -> tuple[dict[str, str], dict[str, str]]:
    """The accounts env file read once, as two views of the same values.

    The first is the credentials view: file values only, so a key the file does
    not define is "not configured" and must stay a failed preflight rather than
    silently borrowing the process environment. A present-but-empty value stays
    defined by the file, so a key an operator blanked on purpose is never
    shadowed by a stale process-env value. The second is the ``${VAR}``
    expansion view: the same file, with the process environment filling only
    the keys the file does not define. Both use the shared resolver's rules
    (``live_runner.credential_env``), and an unreadable file warns and continues.
    """
    file_values = read_accounts_env_file(accounts_env)
    return file_values, overlay_process_env(file_values)


class _LookupRecorder(Mapping[str, str]):
    """Expansion view that remembers every key the config looked up.

    Expansion looks up exactly the keys the parsed config references, so the
    recorded set is the redaction set. Recording lookups instead of re-scanning
    the raw text is exact: a hex-escaped reference in a quoted YAML scalar
    expands to a reference the raw text never spells, and a comment can name a
    key that never expands.
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = values
        self.looked_up: set[str] = set()

    def __getitem__(self, key: str) -> str:
        self.looked_up.add(key)
        return self._values[key]

    def get(self, key: str, default: object = None) -> object:
        self.looked_up.add(key)
        return self._values.get(key, default)

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


_TRUE_BOOL_TOKENS = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_BOOL_TOKENS = frozenset({"0", "false", "no", "n", "off"})


def _parse_bool_env(name: str, value: str | None, *, default: bool) -> bool:
    """Strictly parse a boolean env var.

    Missing value -> default. Recognized true/false tokens (case-insensitive,
    stripped) parse accordingly. Anything else raises so a malformed value can
    never implicitly select live trading.
    """
    if value is None:
        return default
    token = value.strip().lower()
    if token in _TRUE_BOOL_TOKENS:
        return True
    if token in _FALSE_BOOL_TOKENS:
        return False
    valid = ", ".join(sorted(_TRUE_BOOL_TOKENS | _FALSE_BOOL_TOKENS))
    raise ValueError(
        f"Invalid boolean value for {name}: {value!r}. Valid values are: {valid}."
    )


def _resolve_ctrader_credentials(
    env_values: Mapping[str, str],
) -> tuple[str, str, str, str, bool] | None:
    app_id = str(env_values.get("CTRADER_APP_ID", "")).strip()
    app_secret = str(env_values.get("CTRADER_APP_SECRET", "")).strip()
    access_token = str(env_values.get("CTRADER_ACCESS_TOKEN", "")).strip()
    refresh_token = str(env_values.get("CTRADER_REFRESH_TOKEN", "")).strip()
    if not app_id or not app_secret or not access_token:
        return None
    demo = _parse_bool_env("CTRADER_DEMO", env_values.get("CTRADER_DEMO"), default=True)
    return app_id, app_secret, access_token, refresh_token, demo


def _run_broker_connectivity_check(
    adapter: AlpacaLiveAdapter | CTraderLiveAdapter,
    *,
    broker_label: str,
    errors: list[str],
) -> tuple[bool, float | None, float | None, int | None]:
    connected = False
    balance: float | None = None
    equity: float | None = None
    positions_count: int | None = None

    async def _check() -> tuple[float | None, float | None, int]:
        nonlocal connected
        try:
            await adapter.connect()
            fetched_balance, fetched_equity = await adapter.get_balance()
            positions = await adapter.get_positions()
            connected = True
            return fetched_balance, fetched_equity, len(positions)
        finally:
            with contextlib.suppress(Exception):
                await adapter.disconnect()

    try:
        balance, equity, positions_count = asyncio.run(_check())
    except Exception as exc:
        errors.append(f"{broker_label} connectivity check failed: {type(exc).__name__}: {exc}")
    return connected, balance, equity, positions_count


def _preflight_alpaca_account(
    account: LiveAccountConfig,
    *,
    env_values: Mapping[str, str],
    check_connectivity: bool,
    errors: list[str],
    warnings: list[str],
) -> tuple[bool | None, bool | None, float | None, float | None, int | None]:
    credentials = resolve_alpaca_credentials(
        account_id=account.id,
        broker_login=account.broker_login,
        env=env_values,
    )
    paper: bool | None = None
    connected: bool | None = None
    balance: float | None = None
    equity: float | None = None
    positions_count: int | None = None
    if credentials is None:
        errors.append(
            "Missing Alpaca credentials for account. Configure default "
            "ALPACA_API_KEY/ALPACA_SECRET_KEY or a matching ACC{N} block."
        )
    else:
        api_key, secret_key, paper = credentials
        if not paper:
            warnings.append("Alpaca account resolves to live mode, not paper mode.")
        if check_connectivity:
            adapter = AlpacaLiveAdapter(
                api_key=api_key,
                secret_key=secret_key,
                paper=paper,
                account_id=account.id,
            )
            connected, balance, equity, positions_count = _run_broker_connectivity_check(
                adapter,
                broker_label="Alpaca",
                errors=errors,
            )
    return paper, connected, balance, equity, positions_count


def _ctrader_preflight_credentials(
    env_values: Mapping[str, str],
    errors: list[str],
) -> tuple[str, str, str, str, bool] | None:
    try:
        return _resolve_ctrader_credentials(env_values)
    except ValueError as exc:
        # The boolean error quotes the CTRADER_DEMO value it rejected, which may be a
        # mistyped secret. ``env_values`` is the credentials view, so it is the
        # redaction set for every file/explicit-env value.
        errors.append(_redact_values(str(exc), env_values))
        return None


def _validate_ctrader_account_identifier(account: LiveAccountConfig, errors: list[str]) -> str:
    account_identifier = ctrader_account_identifier(account)
    try:
        if int(account_identifier) <= 0:
            errors.append(
                "cTrader account id must be a positive numeric cTID trader account id."
            )
    except ValueError:
        errors.append("cTrader account id must be numeric via ctid, broker_login, or id.")
    return account_identifier


def _preflight_ctrader_account(
    account: LiveAccountConfig,
    *,
    env_values: Mapping[str, str],
    check_connectivity: bool,
    accounts_env_path: Path,
    errors: list[str],
    warnings: list[str],
) -> tuple[bool | None, bool | None, float | None, float | None, int | None]:
    credentials = _ctrader_preflight_credentials(env_values, errors)
    demo: bool | None = None
    connected: bool | None = None
    balance: float | None = None
    equity: float | None = None
    positions_count: int | None = None
    if credentials is None:
        if not errors:
            errors.append(
                "Missing cTrader credentials. Configure CTRADER_APP_ID, "
                "CTRADER_APP_SECRET, and CTRADER_ACCESS_TOKEN."
            )
    else:
        _, _, _, refresh_token, demo = credentials
        if not demo:
            warnings.append("cTrader account resolves to live mode, not demo mode.")
        if not refresh_token:
            warnings.append("CTRADER_REFRESH_TOKEN is missing; token refresh cannot be verified.")
    account_identifier = _validate_ctrader_account_identifier(account, errors)
    if check_connectivity and credentials is not None and not errors:
        app_id, app_secret, access_token, refresh_token, demo = credentials
        logger.info(
            "cTrader preflight using accounts file for credential read/refresh",
            extra={
                "data": {
                    "account_id": account.id,
                    "accounts_env_path": str(accounts_env_path),
                    "refresh_target_path": str(accounts_env_path),
                }
            },
        )
        adapter = CTraderLiveAdapter(
            app_id=app_id,
            app_secret=app_secret,
            access_token=access_token,
            refresh_token=refresh_token,
            account_id=account_identifier,
            demo=demo,
            account_config_id=account.id,
            accounts_env_path=accounts_env_path,
        )
        connected, balance, equity, positions_count = _run_broker_connectivity_check(
            adapter,
            broker_label="cTrader",
            errors=errors,
        )
    return demo, connected, balance, equity, positions_count


def _preflight_account(
    account: LiveAccountConfig,
    *,
    env_values: Mapping[str, str],
    check_connectivity: bool,
    accounts_env_path: Path,
) -> LivePreflightAccountResult:
    errors: list[str] = []
    warnings: list[str] = []
    paper: bool | None = None
    demo: bool | None = None
    connected: bool | None = None
    balance: float | None = None
    equity: float | None = None
    positions_count: int | None = None

    if account.broker == "alpaca":
        (
            paper,
            connected,
            balance,
            equity,
            positions_count,
        ) = _preflight_alpaca_account(
            account,
            env_values=env_values,
            check_connectivity=check_connectivity,
            errors=errors,
            warnings=warnings,
        )

    elif account.broker == "ctrader":
        (
            demo,
            connected,
            balance,
            equity,
            positions_count,
        ) = _preflight_ctrader_account(
            account,
            env_values=env_values,
            check_connectivity=check_connectivity,
            accounts_env_path=accounts_env_path,
            errors=errors,
            warnings=warnings,
        )

    return LivePreflightAccountResult(
        account_id=account.id,
        broker=account.broker,
        ok=not errors,
        paper=paper,
        demo=demo,
        connected=connected,
        balance=balance,
        equity=equity,
        positions_count=positions_count,
        errors=errors,
        warnings=warnings,
    )


def _validate_deployments(
    accounts: Sequence[LiveAccountConfig],
    deployments: Sequence[LiveDeploymentConfig],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    account_ids = {account.id for account in accounts}

    for deployment in deployments:
        label = f"{deployment.strategy_name} ({deployment.account_id})"
        if deployment.account_id not in account_ids:
            errors.append(f"{label}: references unknown account_id.")
        if not deployment.symbols:
            errors.append(f"{label}: has no symbols configured.")
        if not str(deployment.timeframe).strip():
            errors.append(f"{label}: has no timeframe configured.")
        if deployment.signal_source == "discord":
            channel_id = str(deployment.signal_config.get("channel_id") or "").strip()
            if not channel_id:
                warnings.append(f"{label}: Discord deployment has no channel_id in signal_config.")

    return errors, warnings


# A resolved value this short is treated as a non-secret: redacting it would mangle
# unrelated words in the message far more often than it would protect anything.
_MIN_REDACTABLE_VALUE_LENGTH = 3


def _redaction_values(
    *,
    env_values: Mapping[str, str],
    expansion_env: Mapping[str, str],
    referenced: Iterable[str] | None,
) -> dict[str, str]:
    """Values a config error message must never print, keyed by their name.

    That is every accounts-file value plus the expansion value of each key the
    config referenced (``referenced``, recorded during expansion). Process-env
    keys the config never names are left out: they cannot reach the message
    through the config, and redacting the whole environment would mangle
    unrelated text. ``referenced=None`` means the config text is unknown (it
    could not be read), so every expansion value counts as referenced.
    """
    values = dict(env_values)
    if referenced is None:
        values.update(expansion_env)
        return values
    for key in referenced:
        if key in expansion_env:
            values[key] = expansion_env[key]
    return values


def _redact_values(message: str, values: Mapping[str, str]) -> str:
    """Rewrite each redactable value in *message* back to its ``${KEY}`` reference.

    A value is matched both raw and in its ``repr`` form (the text between the
    quotes), because exception text often quotes a value with ``!r``, which
    escapes backslashes and quotes. One pass over a longest-first alternation: a
    value that prefixes a longer redacted value cannot cut it short, and inserted
    references are never rescanned.
    """
    by_value: dict[str, str] = {}
    for key, value in values.items():
        for form in (value, repr(value)[1:-1]):
            if len(form) >= _MIN_REDACTABLE_VALUE_LENGTH:
                by_value.setdefault(form, key)
    if not by_value:
        return message
    pattern = re.compile(
        "|".join(re.escape(value) for value in sorted(by_value, key=len, reverse=True))
    )
    return pattern.sub(lambda match: f"${{{by_value[match.group(0)]}}}", message)


def _yaml_error_summary(exc: yaml.YAMLError) -> str:
    """The problem and position of a YAML syntax error, never its source snippet.

    PyYAML's ``str()`` appends the raw line the operator wrote, and that line can
    hold a literal secret, so only the problem and its 1-based position are kept.
    """
    name = type(exc).__name__
    problem = getattr(exc, "problem", None)
    mark = getattr(exc, "problem_mark", None)
    if not isinstance(problem, str) or mark is None:
        return name
    return f"{name}: {problem} (line {mark.line + 1}, column {mark.column + 1})"


def _config_error_message(exc: Exception, *, redact: Mapping[str, str]) -> str:
    """Value-free text for a deployment-config parse failure.

    ``${VAR}`` references expand before validation, so ``str(exc)`` can quote a
    value that only lives in the accounts env file or the process environment,
    or an explicit ``env`` mapping: pydantic echoes the offending input, and the
    cTrader routing check names the resolved identifier. That text is printed as
    JSON by ``main()`` and written to the warning log below, so a config error
    would leak a credential into operator output. Report the message type and
    the field instead, and rewrite any raw value that still slipped through back
    to the ``${KEY}`` reference - the same name-only reporting
    ``assert_no_unexpanded_env`` uses, so the operator learns which reference
    to fix rather than being shown the secret. ``redact`` maps each key to the
    value that must not be printed (see ``_redaction_values``).

    Not every parse failure is a ``ValueError``: a YAML syntax error, or a
    ``TypeError``/``KeyError`` from a route check, reaches here too. Those are
    typed by name and redacted the same way.
    """
    if isinstance(exc, ValidationError):
        details: list[str] = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc") or ())
            detail = str(error.get("msg") or error.get("type") or "invalid")
            details.append(f"{location}: {detail}" if location else detail)
        count = len(details)
        plural = "" if count == 1 else "s"
        # ``title`` is pydantic's model name; fall back to the exception type so a
        # pydantic release without it cannot turn a config error into a crash.
        title = getattr(exc, "title", None) or type(exc).__name__
        message = f"{title} ({count} validation error{plural}): " + "; ".join(details)
    elif isinstance(exc, yaml.YAMLError):
        message = _yaml_error_summary(exc)
    elif isinstance(exc, ValueError):
        message = str(exc)
    else:
        message = f"{type(exc).__name__}: {exc}"
    return _redact_values(message, redact)


def run_live_preflight(
    config_path: str | Path,
    *,
    accounts_env_path: str | Path = DEFAULT_ACCOUNTS_ENV,
    env: Mapping[str, str] | None = None,
    check_connectivity: bool = False,
) -> LivePreflightResult:
    config = Path(config_path)
    accounts_env = Path(accounts_env_path)
    # ${VAR} references in the YAML resolve exactly like credentials: the accounts
    # env file first, the process environment only for keys the file does not
    # define. An explicit ``env`` mapping is used as-is (hermetic callers). The
    # file is read once; both views come from that single read.
    if env is not None:
        env_values = dict(env)
        expansion_env: Mapping[str, str] = env_values
    else:
        env_values, expansion_env = _accounts_env_views(accounts_env)
    # The lookup recorder hands the parse the same values, and afterwards holds
    # the exact set of keys the config referenced: the redaction set.
    lookups = _LookupRecorder(expansion_env)
    try:
        accounts, deployments = parse_live_deployments_config(config, env_values=lookups)
    except OSError:
        # A missing or unreadable config names only its path, and the message is
        # not a config-text one; it reaches main()'s crash document with a
        # traceback on stderr, as before.
        raise
    except Exception as exc:
        # B3: invalid account definitions (e.g. non-routable ctrader ids)
        # report as a failed preflight, never as an unhandled crash. The text is
        # value-free: expanded values can be credentials (#199), and so can a
        # literal line a YAML syntax error would echo.
        message = _config_error_message(
            exc,
            redact=_redaction_values(
                env_values=env_values,
                expansion_env=expansion_env,
                referenced=lookups.looked_up,
            ),
        )
        logger.warning(
            "Live deployment config invalid",
            extra={"data": {"config_path": str(config), "error": message}},
        )
        return LivePreflightResult(
            ok=False,
            config_path=str(config),
            accounts_env_path=str(accounts_env),
            accounts=[],
            deployment_errors=[message],
        )
    logger.info(
        "Running live preflight",
        extra={
            "data": {
                "config_path": str(config),
                "accounts_env_path": str(accounts_env),
                "check_connectivity": check_connectivity,
            }
        },
    )

    account_results = [
        _preflight_account(
            account,
            env_values=env_values,
            check_connectivity=check_connectivity,
            accounts_env_path=accounts_env,
        )
        for account in accounts
    ]
    deployment_errors, deployment_warnings = _validate_deployments(accounts, deployments)

    ok = all(result.ok for result in account_results) and not deployment_errors
    return LivePreflightResult(
        ok=ok,
        config_path=str(config),
        accounts_env_path=str(accounts_env),
        accounts=account_results,
        deployment_errors=deployment_errors,
        deployment_warnings=deployment_warnings,
    )


def _result_to_jsonable(result: LivePreflightResult) -> dict[str, Any]:
    return asdict(result)


def _crash_redaction(accounts_env: Path) -> dict[str, str] | None:
    """The values an unhandled failure must not print, or ``None`` if unknowable.

    The config is not read again here, so its references are unknown and every
    expansion value counts as referenced. ``None`` means the accounts env file
    could not be resolved at all, so no redaction set exists.
    """
    try:
        env_values, expansion_env = _accounts_env_views(accounts_env)
    except Exception:
        return None
    return _redaction_values(env_values=env_values, expansion_env=expansion_env, referenced=None)


def _crash_message(exc: Exception, *, redact: Mapping[str, str] | None) -> str:
    """``Type: message`` for an unhandled failure, with expanded values redacted.

    Without a redaction set the message keeps only the exception type.
    """
    if redact is None:
        return type(exc).__name__
    return _redact_values(f"{type(exc).__name__}: {exc}", redact)


def _crash_traceback(exc: Exception, *, redact: Mapping[str, str] | None) -> str | None:
    """The full traceback of an unhandled failure, redacted like ``_crash_message``.

    Operators need the traceback, but its exception text can quote an expanded
    value, so the whole formatted text goes through the same redaction. Without a
    redaction set there is no safe text, so there is no traceback at all.
    """
    if redact is None:
        return None
    return _redact_values("".join(traceback.format_exception(exc)), redact)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run non-ordering live trading preflight checks.")
    parser.add_argument(
        "--config",
        default="live_deployments.yaml",
        help="Path to live deployments YAML.",
    )
    parser.add_argument(
        "--accounts-env",
        default=str(DEFAULT_ACCOUNTS_ENV),
        help="Path to .accounts.env-style credential file.",
    )
    parser.add_argument(
        "--check-connectivity",
        action="store_true",
        help="Connect to brokers and read balance/positions without placing orders.",
    )
    args = parser.parse_args(argv)

    # stdout carries exactly one JSON result document; log lines go to stderr.
    route_console_logs_to_stderr()
    try:
        result = run_live_preflight(
            args.config,
            accounts_env_path=args.accounts_env,
            check_connectivity=args.check_connectivity,
        )
    except Exception as exc:
        # An unhandled failure (e.g. missing or malformed config) must still honour
        # the stdout contract: a failed result document, traceback on stderr. Both
        # the traceback and the document are redacted, so an expanded value that
        # reached the exception text is printed nowhere.
        redact = _crash_redaction(Path(args.accounts_env))
        crash_traceback = _crash_traceback(exc, redact=redact)
        crash_log = "Live preflight crashed"
        if crash_traceback is not None:
            crash_log = f"{crash_log}\n{crash_traceback}"
        logger.error(
            crash_log,
            extra={"data": {"config_path": str(args.config)}},
        )
        result = LivePreflightResult(
            ok=False,
            config_path=str(args.config),
            accounts_env_path=str(args.accounts_env),
            accounts=[],
            deployment_errors=[_crash_message(exc, redact=redact)],
        )
    print(json.dumps(_result_to_jsonable(result), indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
