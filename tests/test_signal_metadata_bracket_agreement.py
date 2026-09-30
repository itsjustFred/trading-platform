"""Both live adapters must read a signal's stop-loss and take-profit the same way.

`_resolve_optional_float` in `live_runner/alpaca_adapter.py` and
`_first_metadata_float` in `live_runner/ctrader_adapter.py` are the same rule
written twice: walk a tuple of alias names, skip absent keys and null values, take
the first that parses, and raise rather than invent a number. Neither was named in
a single test file, in either adapter.

They read the bracket around a real order, so the interesting question is not
whether each works but whether they agree. They resolve the same signal metadata
for two different brokers, and the alias lists are duplicated as data: a module
constant in Alpaca, and inlined literals at the cTrader call site. That is the
shape that drifts. Adding an alias to one broker leaves the other resolving by a
different rule, so the same signal would be bracketed differently depending on
which account it routed to - and where the two aliases hold *different* values,
precedence alone decides which stop the position gets.

The tests below read the alias tuples out of the source rather than restating them,
so an alias added to one side is picked up here and fails until the other side
agrees. Restating the tuples would let this file pass on the day the two diverge,
which is the exact failure it exists to catch.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from live_runner import alpaca_adapter, ctrader_adapter

REPO_ROOT = Path(__file__).resolve().parents[1]
ALPACA_SOURCE = REPO_ROOT / "live_runner" / "alpaca_adapter.py"
CTRADER_SOURCE = REPO_ROOT / "live_runner" / "ctrader_adapter.py"


def _literal_tuple(node: ast.AST) -> tuple[str, ...]:
    """Read a tuple of string literals straight out of the AST."""
    if not isinstance(node, ast.Tuple):
        raise AssertionError(f"expected a tuple literal, found {type(node).__name__}")
    values = []
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            raise AssertionError("expected string literals only")
        values.append(element.value)
    return tuple(values)


def _alpaca_aliases() -> dict[str, tuple[str, ...]]:
    """The alias tuples Alpaca declares as module constants."""
    found: dict[str, tuple[str, ...]] = {}
    for node in ast.parse(ALPACA_SOURCE.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "_SL_METADATA_KEYS",
                    "_TP_METADATA_KEYS",
                }:
                    found[target.id] = _literal_tuple(node.value)
    assert found, "the Alpaca alias constants are no longer module-level tuples"
    return found


def _ctrader_aliases() -> list[tuple[str, ...]]:
    """The alias tuples cTrader passes inline at its two call sites.

    Read from the calls rather than from constants, because there are no constants
    to read - the tuples are literals in the call, and that duplication is half of
    what this file is about.
    """
    found: list[tuple[str, ...]] = []
    tree = ast.parse(CTRADER_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_first_metadata_float"
            and len(node.args) >= 2
        ):
            found.append(_literal_tuple(node.args[1]))
    assert found, "cTrader no longer resolves stop-loss or take-profit through a literal tuple"
    return found


ALPACA_ALIASES = _alpaca_aliases()
CTRADER_CALL_TUPLES = _ctrader_aliases()

# The union drives the matrix, so an alias that exists on only one side still gets
# exercised - that is the case that must fail.
ALL_ALIAS_NAMES = sorted(
    set(ALPACA_ALIASES["_SL_METADATA_KEYS"])
    | set(ALPACA_ALIASES["_TP_METADATA_KEYS"])
    | {name for tup in CTRADER_CALL_TUPLES for name in tup}
)

RESOLVERS = {
    "alpaca": alpaca_adapter._resolve_optional_float,
    "ctrader": ctrader_adapter._first_metadata_float,
}

# A value that parses to something no real price is, so a wrong precedence shows up
# as a mismatch rather than as a plausible number.
#
# Built from ALL_ALIAS_NAMES rather than written out by hand, and distinct per name
# by construction. The hand-written version happened to cover all six aliases, which
# is the problem: it was correct by coincidence and nothing would have said so when
# a seventh alias appeared. An alias with no entry here resolves as absent, so a
# precedence error involving it becomes invisible rather than failing.
SENTINEL = {
    name: 100.0 + 10.0 * position for position, name in enumerate(ALL_ALIAS_NAMES)
}

assert len(set(SENTINEL.values())) == len(ALL_ALIAS_NAMES), (
    f"sentinel values must be distinct or precedence cannot be told apart: {SENTINEL}"
)


def _resolve(resolver, metadata, keys):
    try:
        return ("ok", resolver(metadata, keys))
    except Exception as exc:  # noqa: BLE001 - the type is the observation
        return ("raised", type(exc).__name__)


def test_both_adapters_declare_the_same_alias_tuples() -> None:
    """The alias lists are the duplicated data, so they are compared as data.

    Alpaca holds them in module constants and cTrader inlines them at the call
    site. Any divergence here means the two brokers resolve the same metadata by
    different rules, which no per-value test below would necessarily catch if the
    extra alias is simply never exercised by a signal in production.
    """
    assert sorted(ALPACA_ALIASES.values()) == sorted(CTRADER_CALL_TUPLES), (
        "the adapters resolve stop-loss / take-profit from different alias lists:\n"
        f"  alpaca:  {sorted(ALPACA_ALIASES.values())}\n"
        f"  ctrader: {sorted(CTRADER_CALL_TUPLES)}"
    )


@pytest.mark.parametrize("name", ALL_ALIAS_NAMES)
def test_a_single_alias_resolves_to_the_same_number_in_both_adapters(name) -> None:
    """One alias present, one number under it: the two must return the same float."""
    for keys in (ALPACA_ALIASES["_SL_METADATA_KEYS"], ALPACA_ALIASES["_TP_METADATA_KEYS"]):
        outcomes = {broker: _resolve(fn, {name: "123.5"}, keys) for broker, fn in RESOLVERS.items()}
        assert outcomes["alpaca"] == outcomes["ctrader"], (
            f"alias {name!r} resolves differently: {outcomes}. The same signal would be "
            "bracketed one way for Alpaca and another for cTrader."
        )


@pytest.mark.parametrize("name", ALL_ALIAS_NAMES)
def test_precedence_over_conflicting_aliases_matches(name) -> None:
    """Every alias populated with a distinct number pins the precedence order.

    The order of the alias tuple decides the stop, so a reorder on one side is a
    silent change to where a position gets stopped out - and both adapters reading
    the same metadata must land on the same one.
    """
    # `name` is omitted, so each parametrised case is a *different* scenario: with
    # every alias present the winner is the first key in the tuple and the rest are
    # never consulted, which is one fact repeated six times. Dropping one alias at a
    # time is what actually walks the order - a reorder on either side moves which
    # of the remaining values wins.
    metadata = {k: v for k, v in SENTINEL.items() if k != name}
    for keys in (ALPACA_ALIASES["_SL_METADATA_KEYS"], ALPACA_ALIASES["_TP_METADATA_KEYS"]):
        outcomes = {broker: _resolve(fn, metadata, keys) for broker, fn in RESOLVERS.items()}
        assert outcomes["alpaca"] == outcomes["ctrader"], (
            f"with every alias populated, {name!r} present, the two adapters disagree: "
            f"{outcomes}. Precedence is what decides the stop price."
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"stop_loss": None},
        {"stop_loss": None, "sl_price": "1.5"},
        {"stop_loss": "abc"},
        {"stop_loss": "abc", "sl_price": "1.5"},
        {"stop_loss": ""},
        {"stop_loss": float("nan")},
        {"stop_loss": float("inf")},
        {"stop_loss": "1e400"},
    ],
    ids=["empty", "null", "null-then-value", "unparseable", "unparseable-then-value",
         "empty-string", "nan", "inf", "overflow"],
)
def test_both_adapters_treat_odd_metadata_the_same_way(metadata) -> None:
    """Absent, null, unparseable, and the float edge cases must match exactly.

    NaN and infinity matter here specifically: they parse successfully, so they
    become a stop price that is not a price. Both adapters must at least agree on
    what they do with them.
    """
    for keys in (ALPACA_ALIASES["_SL_METADATA_KEYS"], ALPACA_ALIASES["_TP_METADATA_KEYS"]):
        outcomes = {broker: _resolve(fn, metadata, keys) for broker, fn in RESOLVERS.items()}
        assert outcomes["alpaca"] == outcomes["ctrader"], (
            f"metadata {metadata!r} resolves differently: {outcomes}"
        )


def test_a_malformed_bracket_value_is_refused_rather_than_dropped() -> None:
    """An unparseable stop must raise, not quietly become "no stop configured".

    `None` is how both adapters spell "missing", and the submission paths treat a
    missing bracket as a refusal - but only because the parse error is raised
    before it can be mistaken for an absent key. If it were swallowed, the caller
    would see `None` and could not tell a malformed signal from a signal that never
    carried a stop at all.
    """
    for broker, fn in RESOLVERS.items():
        with pytest.raises(ValueError) as excinfo:
            fn({"stop_loss": "not-a-number"}, ("stop_loss",))
        assert "stop_loss" in str(excinfo.value), (
            f"{broker} raised without naming the offending key: {excinfo.value}"
        )
