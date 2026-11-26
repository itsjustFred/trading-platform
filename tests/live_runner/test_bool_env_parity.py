"""Characterization of every strict boolean parser on the live path (G08).

``CTRADER_DEMO``, ``ALPACA_PAPER`` and ``allow_short`` decide whether the
platform trades real money, so every parser that reads them has to reject the
same typo'd value with the same operator-facing message. That contract was
implemented several times in ``live_runner``; these tests pin each copy's
behaviour BEFORE the copies are replaced by
:func:`live_runner.credential_env.parse_bool_env`, so the merge can only be a
no-op.

The parsers that stay deliberately unmerged (a different error type, a
different operator-facing message, or a value domain that is not an env var)
are pinned here too, so a later attempt to merge them has the same baseline.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pytest

from live_runner import alpaca_adapter, ctrader_adapter, daemon, loss_breaker, preflight
from live_runner.credential_env import parse_bool_env
from live_runner.ctrader_adapter import CTraderLiveAdapter
from shared.models import LiveDeploymentConfig

CANONICAL_VALID_LISTING = "0, 1, false, n, no, off, on, true, y, yes"
# ``loss_breaker._env_bool`` accepts exactly the canonical tokens but names
# only eight of the ten in its error text, so an operator who typed ``y`` (an
# accepted token) is told it is not valid. Kept as-is for now; see the PR.
LOSS_BREAKER_VALID_LISTING = "0, 1, true, false, yes, no, on, off"

# (raw env value, expected result) for every token the parsers recognize.
RECOGNIZED_TOKENS: list[tuple[str, bool]] = [
    ("1", True),
    ("0", False),
    ("true", True),
    ("false", False),
    ("TRUE", True),
    ("FALSE", False),
    ("Yes", True),
    ("No", False),
    ("on", True),
    ("off", False),
    ("y", True),
    ("n", False),
    (" true ", True),
    ("\tOff\n", False),
]

# Values that must raise instead of falling back to a default.
REJECTED_VALUES = ["", "   ", "ture", "tru", "flase", "maybe", "2", "TRUEISH", "yes,no"]


@dataclass(frozen=True)
class BoolParser:
    """One parser, the variable it is fed, and its message for a bad value."""

    label: str
    variable: str
    parse: Callable[[str, str | None, bool], bool]
    error_text: Callable[[str, str], str]


def _canonical_error(name: str, raw: str) -> str:
    return (
        f"Invalid boolean value for {name}: {raw!r}. "
        f"Valid values are: {CANONICAL_VALID_LISTING}."
    )


def _loss_breaker_error(name: str, raw: str) -> str:
    return (
        f"Invalid boolean value for {name}: {raw!r}. "
        f"Valid values are: {LOSS_BREAKER_VALID_LISTING}."
    )


def _loss_breaker_bool(key: str, raw: str | None, default: bool) -> bool:
    """``loss_breaker._env_bool`` through the global key it falls back to.

    The full ``LOSS_BREAKER_<SUFFIX>`` key is what the other parsers are handed
    as well, and a missing value is an empty mapping rather than a
    ``{key: None}`` entry - "the variable is not configured" as the lookup
    sees it.
    """
    suffix = key.removeprefix(loss_breaker._ENV_PREFIX)  # noqa: SLF001
    env_values: Mapping[str, str] = {} if raw is None else {key: raw}
    return loss_breaker._env_bool(env_values, None, suffix, default=default)  # noqa: SLF001


SHARED_PARSERS = [
    BoolParser(
        "credential_env.parse_bool_env",
        "CTRADER_DEMO",
        lambda name, raw, default: parse_bool_env(name, raw, default=default),
        _canonical_error,
    ),
    BoolParser(
        "ctrader_adapter._parse_bool_env",
        "CTRADER_DEMO",
        lambda name, raw, default: ctrader_adapter._parse_bool_env(  # noqa: SLF001
            name, raw, default=default
        ),
        _canonical_error,
    ),
    BoolParser(
        "preflight._parse_bool_env",
        "CTRADER_DEMO",
        lambda name, raw, default: preflight._parse_bool_env(name, raw, default=default),  # noqa: SLF001
        _canonical_error,
    ),
    BoolParser(
        "alpaca_adapter._parse_bool_env",
        "ALPACA_PAPER",
        lambda name, raw, default: alpaca_adapter._parse_bool_env(  # noqa: SLF001
            name, raw, default=default
        ),
        _canonical_error,
    ),
]

VARIANT_PARSERS = [
    BoolParser(
        "loss_breaker._env_bool",
        "LOSS_BREAKER_ENABLED",
        _loss_breaker_bool,
        _loss_breaker_error,
    ),
]

ALL_PARSERS = SHARED_PARSERS + VARIANT_PARSERS

_PARSER_IDS = [parser.label for parser in ALL_PARSERS]


@pytest.mark.parametrize("parser", ALL_PARSERS, ids=_PARSER_IDS)
@pytest.mark.parametrize(
    "raw,expected",
    RECOGNIZED_TOKENS,
    ids=[f"{raw!r}->{expected}" for raw, expected in RECOGNIZED_TOKENS],
)
def test_recognized_tokens_parse_identically(
    parser: BoolParser, raw: str, expected: bool
) -> None:
    assert parser.parse(parser.variable, raw, True) is expected


@pytest.mark.parametrize("parser", ALL_PARSERS, ids=_PARSER_IDS)
@pytest.mark.parametrize("default", [True, False], ids=["default-true", "default-false"])
def test_missing_value_returns_the_default(parser: BoolParser, default: bool) -> None:
    assert parser.parse(parser.variable, None, default) is default


@pytest.mark.parametrize("parser", ALL_PARSERS, ids=_PARSER_IDS)
@pytest.mark.parametrize("raw", REJECTED_VALUES, ids=[repr(raw) for raw in REJECTED_VALUES])
def test_unrecognized_value_raises_with_its_exact_message(
    parser: BoolParser, raw: str
) -> None:
    with pytest.raises(ValueError) as excinfo:
        parser.parse(parser.variable, raw, True)

    assert str(excinfo.value) == parser.error_text(parser.variable, raw)


@pytest.mark.parametrize("parser", ALL_PARSERS, ids=_PARSER_IDS)
def test_the_error_names_the_variable_and_keeps_the_raw_value(parser: BoolParser) -> None:
    """The raw (unstripped) value is echoed, so a pasted value is recognizable."""

    with pytest.raises(ValueError) as excinfo:
        parser.parse(parser.variable, "  ture ", True)

    message = str(excinfo.value)
    assert message == parser.error_text(parser.variable, "  ture ")
    assert parser.variable in message
    assert "'  ture '" in message


# ─── the caller level: a typo must reach the operator, not a silent default ──

_CTRADER_ENV: dict[str, str] = {
    "CTRADER_APP_ID": "app",
    "CTRADER_APP_SECRET": "secret",
    "CTRADER_ACCESS_TOKEN": "token",
    "CTRADER_DEMO": "ture",
}


def _deployment_with_signal_config(
    signal_config: dict[str, object],
    *,
    settings: dict[str, object] | None = None,
) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        strategy_name="fx_breakout",
        account_id="ctrader-demo",
        symbols=["EURUSD"],
        timeframe="15m",
        signal_source="discord",
        signal_config=signal_config,
        settings=settings or {},
    )


def _preflight_ctrader_credentials() -> None:
    preflight._resolve_ctrader_credentials(_CTRADER_ENV)  # noqa: SLF001


def _daemon_ctrader_credentials() -> None:
    daemon._resolve_ctrader_credentials(_CTRADER_ENV)


def _ctrader_adapter_constructor() -> None:
    CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="10105152",
        env_values={"CTRADER_DEMO": "ture"},
    )


def _alpaca_default_credentials() -> None:
    alpaca_adapter._default_alpaca_credentials(  # noqa: SLF001
        {
            "ALPACA_API_KEY": "key",
            "ALPACA_SECRET_KEY": "secret",
            "ALPACA_PAPER": "ture",
        }
    )


def _alpaca_credential_block() -> None:
    alpaca_adapter._alpaca_credential_block(  # noqa: SLF001
        {
            "ALPACA_API_KEY_ACC1": "key",
            "ALPACA_SECRET_KEY_ACC1": "secret",
            "ALPACA_PAPER_ACC1": "ture",
        },
        1,
    )


def _alpaca_adapter_constructor() -> None:
    alpaca_adapter.AlpacaLiveAdapter(
        api_key="key",
        secret_key="secret",
        env_values={"ALPACA_PAPER": "ture"},
    )


def _daemon_allow_short() -> None:
    daemon._deployment_allows_short(
        _deployment_with_signal_config(
            {"allow_short": "ture"},
            settings={"allow_short": True},
        )
    )


def _daemon_wait_for_candle_close() -> None:
    daemon._deployment_wait_for_candle_close(
        _deployment_with_signal_config({"wait_candle_close": "ture"}), {}
    )


def _daemon_wait_for_candle_close_env() -> None:
    daemon._deployment_wait_for_candle_close(
        _deployment_with_signal_config({}), {"DISCORD_WAIT_FOR_CANDLE_CLOSE": "ture"}
    )


# (the caller, the variable it names, the message it must raise)
CALLERS: list[tuple[Callable[[], None], str]] = [
    (_preflight_ctrader_credentials, "CTRADER_DEMO"),
    (_daemon_ctrader_credentials, "CTRADER_DEMO"),
    (_ctrader_adapter_constructor, "CTRADER_DEMO"),
    (_alpaca_default_credentials, "ALPACA_PAPER"),
    (_alpaca_credential_block, "ALPACA_PAPER_ACC1"),
    (_alpaca_adapter_constructor, "ALPACA_PAPER"),
    (_daemon_allow_short, "allow_short"),
    (_daemon_wait_for_candle_close, "wait_candle_close"),
    (_daemon_wait_for_candle_close_env, "DISCORD_WAIT_FOR_CANDLE_CLOSE"),
]


@pytest.mark.parametrize(
    "caller,variable",
    CALLERS,
    ids=[variable for _, variable in CALLERS],
)
def test_every_former_caller_rejects_the_same_typo_with_the_same_message(
    caller: Callable[[], None], variable: str
) -> None:
    """``"ture"`` must raise, not fall back to a default that may flip to live."""

    with pytest.raises(ValueError) as excinfo:
        caller()

    assert str(excinfo.value) == _canonical_error(variable, "ture")


def test_loss_breaker_rejects_the_same_typo_with_its_own_message() -> None:
    """The one unmerged variant: same tokens, a shorter (incomplete) listing."""

    with pytest.raises(ValueError) as excinfo:
        loss_breaker.account_loss_breaker_limits(
            "acc-1", env_values={"LOSS_BREAKER_ENABLED": "ture"}
        )

    assert str(excinfo.value) == _loss_breaker_error("LOSS_BREAKER_ENABLED", "ture")
