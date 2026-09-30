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
    """Read the tuple passed at a call site, from the AST.

    A tuple literal is read directly. A bare name is resolved to the module-level
    constant it names, so a call site that passes the shared definition is recognised
    rather than rejected - while a call site passing a literal of its own is still
    visible, and still shows up in the comparison below as a value that has to match.
    """
    if isinstance(node, ast.Name):
        resolved = getattr(ctrader_adapter, node.id, None)
        if isinstance(resolved, tuple) and resolved and all(
            isinstance(item, str) for item in resolved
        ):
            return tuple(resolved)
        raise AssertionError(
            f"call site passes a bare name {node.id!r} that is not a string tuple"
        )
    if not isinstance(node, ast.Tuple):
        raise AssertionError(f"expected a tuple, found {type(node).__name__}")
    values = []
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            raise AssertionError("expected string literals only")
        values.append(element.value)
    return tuple(values)


def _alpaca_aliases() -> dict[str, tuple[str, ...]]:
    """The alias tuples Alpaca resolves stop-loss and take-profit from.

    Read from the module rather than from the source, because these are no longer
    literals: all three paths now import one definition from `shared.models`. The
    previous version required a module-level tuple literal and failed to collect once
    they became references - which is a useful reminder that a source-reading pin also
    pins the *shape* of the code, not just the rule.

    Reading the value is strictly better for this file's purpose. The docstring's
    worry was that restating the tuples would let the test pass on the day the two
    sides diverge; taking the value from the module cannot do that, because if the two
    sides disagree the values differ. Only cTrader's copy had to be read from the AST,
    and it no longer needs to be.
    """
    found = {
        name: tuple(getattr(alpaca_adapter, name))
        for name in ("_SL_METADATA_KEYS", "_TP_METADATA_KEYS")
    }
    assert all(found.values()), "the Alpaca alias constants resolved to nothing"
    return found


def _ctrader_aliases() -> list[tuple[str, ...]]:
    """The tuples cTrader resolves stop-loss and take-profit from, read from the calls.

    The call sites are read rather than the module attribute because what matters here
    is what is *passed* at each call: a module constant that is correct while one call
    site passes a literal of its own is exactly the divergence this file exists to
    catch, and reading the module would not see it.
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
    assert found, "cTrader no longer resolves stop-loss or take-profit through a tuple"
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

# No distinctness assertion here: `SENTINEL` is built by a comprehension over
# ALL_ALIAS_NAMES with strictly increasing offsets, so distinctness holds for every
# possible value of that set. The check I first wrote beside it was a tautology that
# could never fail - which is worse than no check, because it reads like a guard. An
# adversarial review caught it. The real protection is structural: the values come
# from the union itself, so an alias that appears in the source cannot be missing here.
assert all(value > 0 for value in SENTINEL.values()), (
    f"a sentinel must parse as a plausible price, or the matrix exercises a parse "
    f"failure instead of a precedence difference: {SENTINEL}"
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
    """One alias omitted at a time pins the precedence order.

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
            f"with {name!r} omitted and the rest populated, the two adapters "
            f"disagree: "
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


def test_all_three_paths_share_one_alias_definition() -> None:
    """One definition, not N equal copies.

    The list existed in three places: a module constant in `alpaca_adapter`, inline
    literals at cTrader's two call sites, and a fourth tuple in `risk/live.py`. The
    comparison above would have passed for as long as the copies were equal, which is
    exactly the state that hides the hazard.

    Asserted as identity. Equality is what the original pin proved, and it is the
    weaker claim: two tuples that happen to agree today say nothing about tomorrow.
    This asserts there is nothing to disagree.
    """
    from risk import live as risk_live
    from shared.models import SL_METADATA_KEYS, TP_METADATA_KEYS

    assert alpaca_adapter._SL_METADATA_KEYS is SL_METADATA_KEYS
    assert alpaca_adapter._TP_METADATA_KEYS is TP_METADATA_KEYS
    assert risk_live._LIVE_SL_KEYS is SL_METADATA_KEYS
    assert risk_live._LIVE_TP_KEYS is TP_METADATA_KEYS

    # cTrader has no constant of its own, so check what its call sites pass.
    for node in ast.walk(ast.parse(CTRADER_SOURCE.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_first_metadata_float"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Name)
        ):
            assert getattr(ctrader_adapter, node.args[1].id) in (
                SL_METADATA_KEYS,
                TP_METADATA_KEYS,
            ), (
                f"cTrader resolves {node.args[1].id!r}, which is not the shared "
                f"definition; the alias list exists in more than one place again"
            )


def test_the_live_sizing_path_is_covered_by_the_bracket_matrix() -> None:
    """`risk/live.py` resolves the same keys, so it belongs in the union.

    Its copy was invisible to every assertion in this file: the matrix covered the two
    broker adapters, and a fourth copy in the sizing path could drift without anything
    here noticing. It shares the definition now, and this asserts it stays in the
    matrix so a future re-copy is a test failure rather than a silent fourth copy.
    """
    from risk import live as risk_live

    assert tuple(risk_live._LIVE_SL_KEYS) in set(CTRADER_CALL_TUPLES)
    assert tuple(risk_live._LIVE_TP_KEYS) in set(CTRADER_CALL_TUPLES)

