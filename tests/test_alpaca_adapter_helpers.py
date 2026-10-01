"""Helper guards in `live_runner/alpaca_adapter.py` that nothing tested.

Found by mutation testing the adapter with `mutate.sh`. Two of the four surviving
mutations were real gaps:

- the **nil-UUID** guard in `_normalize_broker_order_id`, which is the only one of
  that function's four guards doing any work - a nil UUID parses successfully, so
  without it a broker reporting the nil UUID would be recorded as a real order id;
- the **refusal** of timeframes Alpaca does not serve. `4h` is a valid project
  timeframe (`VALID_TIMEFRAMES` has it, and the CLI alias map maps `h4` to it), so
  it is a configuration an operator can legitimately write - and the adapter has to
  refuse it *by name* rather than request a wire timeframe that does not exist.
  Adding `4h` to the local table survived the whole suite, which would have turned
  a named refusal into silently wrong market data.

The other two survivors are **equivalent mutants**, pinned here as documentation
rather than as gaps:

- dropping the `isinstance(value, bool)` guard, and
- dropping the `{"none", "null", "false"}` sentinel guard,

both of which the subsequent `UUID(...)` parse already rejects. Measured: `UUID()`
raises for `True`, `"none"`, `"null"` and `"false"`, so those two checks are
belt-and-braces that happen to be redundant today. They stay, because a future
relaxation of the parse would otherwise silently promote a boolean order id.

`ALPACA_PAPER`'s `.strip()` is the same story and lives in
`tests/test_alpaca_paper_blank_agreement.py`'s territory: `parse_bool_env` strips
internally, so the caller's strip is redundant. The *default* beside it is not
redundant, and it is pinned there - flipping `default=True` to `False`, which
would make an unset `ALPACA_PAPER` mean **live** trading, is caught.
"""

from __future__ import annotations

import pytest

from live_runner.alpaca_adapter import _alpaca_timeframe, _normalize_broker_order_id

REAL_ORDER_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
NIL_ORDER_ID = "00000000-0000-0000-0000-000000000000"


# --------------------------------------------------------------------------
# _normalize_broker_order_id
# --------------------------------------------------------------------------


def test_a_real_broker_order_id_is_normalised() -> None:
    """The positive case, so the nil guard cannot be satisfied by refusing everything."""
    assert _normalize_broker_order_id(REAL_ORDER_ID) == REAL_ORDER_ID


def test_a_nil_uuid_is_treated_as_no_order_id() -> None:
    """The one guard in this function that does work.

    A nil UUID *parses* - `UUID(NIL_ORDER_ID)` succeeds - so without the
    `uuid_value.int == 0` check it would be returned as a real order id. That
    matters for reconciliation: the platform would believe an order exists at the
    broker when the broker reported nothing.
    """
    assert _normalize_broker_order_id(NIL_ORDER_ID) is None


def test_a_nil_uuid_in_uppercase_and_braced_form_is_also_refused() -> None:
    """Same id, other spellings a broker or SDK might produce."""
    for variant in (NIL_ORDER_ID.upper(), "{%s}" % NIL_ORDER_ID, NIL_ORDER_ID.replace("-", "")):
        assert _normalize_broker_order_id(variant) is None, variant


def test_surrounding_whitespace_is_stripped() -> None:
    assert _normalize_broker_order_id("  %s  " % REAL_ORDER_ID) == REAL_ORDER_ID


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        True,
        False,
        "none",
        "null",
        "false",
        "None",
        "NULL",
        "not-a-uuid",
        "12345",
    ],
)
def test_anything_that_is_not_an_order_id_becomes_none(value: object) -> None:
    """Absent, blank, boolean, sentinel and malformed all mean "no order id"."""
    assert _normalize_broker_order_id(value) is None


# --------------------------------------------------------------------------
# _alpaca_timeframe
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wire", "expected_amount"),
    [("1m", 1), ("5m", 5), ("15m", 15), ("30m", 30), ("1h", 1), ("1d", 1)],
)
def test_a_supported_timeframe_maps_to_its_amount(wire: str, expected_amount: int) -> None:
    timeframe, normalized = _alpaca_timeframe(wire)
    assert normalized == wire
    assert timeframe.amount_value == expected_amount


@pytest.mark.parametrize("wire", ["4h", "1w", "1wk", "tick", "s1", "2h", "1wk "])
def test_a_timeframe_alpaca_cannot_serve_is_refused_by_name(wire: str) -> None:
    """Kills adding one of these to the local table.

    `4h` and `1w` are valid *project* timeframes - `VALID_TIMEFRAMES` has them and
    the CLI alias map maps `h4`/`w1` onto them - so an operator can legitimately
    configure one. The adapter cannot serve it, and the refusal has to name the
    problem rather than request a wire timeframe Alpaca does not have, which would
    return wrong or stale bars with no error.
    """
    with pytest.raises(ValueError, match="Unsupported Alpaca market-data timeframe"):
        _alpaca_timeframe(wire)


def test_the_timeframe_table_is_not_widened_past_what_alpaca_serves() -> None:
    """The set itself, so a new entry has to be a deliberate decision.

    Complements the refusals above: they prove the *current* entries raise, and
    this proves nothing was quietly added.
    """
    # Read the table out of the module rather than restating it, so this assertion
    # cannot itself become the second copy this repo keeps finding.
    served = _served_timeframes()
    assert served == {"1m", "5m", "15m", "30m", "1h", "1d"}, (
        "the set of Alpaca-servable timeframes changed; every project timeframe "
        "outside it must be refused by name, so adding one needs a matching reason"
    )


def _served_timeframes() -> set[str]:
    """The wire values `_alpaca_timeframe` accepts, read from the module itself.

    The values in that table are `TimeFrame` attribute references, so the dict
    cannot be `literal_eval`-ed; only the keys are extracted, which are literals.
    """
    import ast
    import inspect
    import pathlib

    source = pathlib.Path(inspect.getfile(_alpaca_timeframe)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_alpaca_timeframe":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "units" for t in inner.targets
            ):
                keys = inner.value
                return {
                    k.value
                    for k in keys.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    raise AssertionError("could not read the `units` table out of _alpaca_timeframe")


def test_an_uppercase_monthly_spelling_silently_becomes_one_minute() -> None:
    """A known hazard, pinned so it cannot change without being noticed.

    `_alpaca_timeframe` lower-cases its input, so `1M` resolves to **one minute**,
    not monthly. Alpaca does not serve monthly bars at all, so nothing servable is
    lost - but a caller using Pine-style `1M` for monthly would get one-minute
    bars with no error, and one-minute bars produce a different ATR, which means
    different stops.

    Not asserted as a defect and not changed: the canonical spelling everywhere in
    this repo is lower-case, and the callers pass already-normalised values. It is
    recorded here because the next person to widen that table should know that
    `M` is the one letter whose case changes the meaning rather than the casing.
    """
    timeframe, normalized = _alpaca_timeframe("1M")

    assert normalized == "1m"
    assert timeframe.amount_value == 1
    assert timeframe.unit_value == "Min"