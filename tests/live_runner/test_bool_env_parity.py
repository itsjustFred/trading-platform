"""One strict parser for the money path's boolean env vars (G08).

``CTRADER_DEMO``, ``ALPACA_PAPER`` and ``allow_short`` decide whether the
platform trades real money, so a typo'd value has to raise. The parser that
does it used to live in four places in ``live_runner``; it now lives once, in
:func:`live_runner.credential_env.parse_bool_env`. This suite pins it from the
caller side, so the merge cannot be undone silently:

* every recognized token, both defaults and the exact rejection message of the
  canonical parser - characterized before the copies were replaced, so the
  merge had to be a no-op;
* every former caller - preflight and daemon credential resolution, both
  adapter constructors, both Alpaca credential readers,
  ``_deployment_allows_short`` and ``_deployment_wait_for_candle_close`` -
  rejecting ``"ture"`` with the canonical message;
* ``loss_breaker._env_bool``, the one copy that stays: same tokens, its own
  (shorter) operator-facing message, pinned as a maintainer decision.
``risk.models._parse_bool`` and ``shared.sentry._parse_bool`` are pinned in
their own test files - they do not parse env vars.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from live_runner import alpaca_adapter, ctrader_adapter, daemon, loss_breaker, preflight
from live_runner.credential_env import parse_bool_env
from live_runner.ctrader_adapter import CTraderLiveAdapter
from shared.models import LiveDeploymentConfig

LIVE_RUNNER_DIR = Path(__file__).resolve().parents[2] / "live_runner"

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


# The one parser every live_runner caller now uses. The four copies that used
# to sit beside it (`ctrader_adapter`, `preflight`, `daemon` and the alpaca
# passthrough) were byte-identical and are gone; the guard at the end of this
# module is what keeps them from coming back.
CANONICAL_PARSERS = [
    BoolParser(
        "credential_env.parse_bool_env",
        "CTRADER_DEMO",
        lambda name, raw, default: parse_bool_env(name, raw, default=default),
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

ALL_PARSERS = CANONICAL_PARSERS + VARIANT_PARSERS

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
    alpaca_adapter.default_alpaca_credentials(
        {
            "ALPACA_API_KEY": "key",
            "ALPACA_SECRET_KEY": "secret",
            "ALPACA_PAPER": "ture",
        }
    )


def _alpaca_credential_block() -> None:
    alpaca_adapter.alpaca_credential_block(
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


# ─── the merge must not be undone by a fresh copy ────────────────────────────


@pytest.mark.parametrize(
    "module",
    [ctrader_adapter, preflight, daemon, alpaca_adapter],
    ids=["ctrader_adapter", "preflight", "daemon", "alpaca_adapter"],
)
def test_no_live_runner_module_keeps_a_private_bool_env_parser(
    module: ModuleType,
) -> None:
    """Every former copy is gone and must not come back under its own name.

    A private copy is how the daemon's lenient variant appeared in the first
    place: it accepted every unrecognized token as ``False``, so a typo'd
    ``CTRADER_DEMO`` would have disabled demo trading silently.
    """

    for name in ("_parse_bool_env", "_parse_strict_bool_env", "_env_bool"):
        assert not hasattr(module, name), f"{module.__name__} still defines {name}"
    for name in ("_TRUE_BOOL_TOKENS", "_FALSE_BOOL_TOKENS"):
        assert not hasattr(module, name), f"{module.__name__} still defines {name}"


# Every site in ``live_runner`` that still spells out its own boolean token
# vocabulary. A site that is not on this list fails the scan below, so a fresh
# copy cannot be added quietly - not under a new name either.
BOOLEAN_VOCABULARY_SITES: dict[str, str] = {
    "credential_env.TRUE_BOOL_TOKENS": (
        "the canonical parser's own vocabulary - this is the single definition"
    ),
    "credential_env.FALSE_BOOL_TOKENS": (
        "the canonical parser's own vocabulary - this is the single definition"
    ),
    "loss_breaker._env_bool": (
        "env tokens, deliberately unmerged: same accepted tokens as the "
        "canonical parser but its own shorter operator-facing message "
        "(maintainer decision, PR #457)"
    ),
    "broker_reconciliation._SETTING_TRUE": (
        "deployment SETTINGS, not env vars; fails closed to a safe default"
    ),
    "broker_reconciliation._SETTING_FALSE": (
        "deployment SETTINGS, not env vars; fails closed to a safe default"
    ),
    "internal_strategy_runner._internal_runner_opted_in": (
        "deployment SETTINGS, explicitly lenient by maintainer decision of "
        "2026-11-22 (anything but an unmistakable truthy value stays inert)"
    ),
    "signal_archive._ARCHIVE_DISABLED_VALUES": (
        "env kill switch with the opposite polarity - enabled unless "
        "explicitly disabled; not a money-path gate"
    ),
}

# A vocabulary of boolean tokens is at least three of them: ``{"0", "1"}`` also
# describes channel ids, marker sets and flags that have nothing to do with a
# boolean parser.
_MIN_VOCABULARY_TOKENS = 3

BOOLEAN_TOKEN_UNIVERSE = frozenset(
    {"0", "1", "true", "false", "yes", "no", "y", "n", "on", "off"}
)


def _boolean_vocabulary_sites() -> set[str]:
    """``module.name`` for every live_runner site that spells out boolean tokens.

    Matched on the AST, not on the text: a renamed or reformatted copy still
    carries its token set, and a comment mentioning the tokens does not.
    """

    found: set[str] = set()
    # rglob, not glob: the signal_sources subpackage carries its own modules,
    # and a copy hidden there must fail the scan as surely as one at the top.
    for path in sorted(LIVE_RUNNER_DIR.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = path.stem
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            tokens = _boolean_tokens(node.value)
            if len(tokens) < _MIN_VOCABULARY_TOKENS:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.add(f"{module}.{target.id}")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            tokens = _boolean_tokens(node)
            if len(tokens) < _MIN_VOCABULARY_TOKENS:
                continue
            found.add(f"{module}.{node.name}")
    return found


def _boolean_tokens(node: ast.AST) -> set[str]:
    """The distinct boolean tokens that appear as string constants under *node*."""

    return {
        constant.value.strip().lower()
        for constant in ast.walk(node)
        if isinstance(constant, ast.Constant)
        and isinstance(constant.value, str)
        and constant.value.strip().lower() in BOOLEAN_TOKEN_UNIVERSE
    }


def test_every_boolean_token_vocabulary_in_live_runner_is_declared() -> None:
    """A fresh copy anywhere in ``live_runner`` lands in this list first.

    Pre-mortem finding on PR #457: the four-module check above cannot see a
    parser added under a new name in a fifth module. This scan is what fails
    then - and a stale allowlist fails too, so the list cannot rot silently.
    """

    observed = _boolean_vocabulary_sites()
    missing = sorted(observed - BOOLEAN_VOCABULARY_SITES.keys())
    stale = sorted(BOOLEAN_VOCABULARY_SITES.keys() - observed)
    assert not missing, (
        "A boolean token vocabulary exists outside the canonical parser. "
        f"New sites: {missing}. Route it through "
        "live_runner.credential_env.parse_bool_env, or add it to "
        "BOOLEAN_VOCABULARY_SITES with the reason it is a different domain."
    )
    assert not stale, (
        f"Sites removed: {stale}. Drop them from BOOLEAN_VOCABULARY_SITES, or "
        "restore the parser they guarded."
    )
