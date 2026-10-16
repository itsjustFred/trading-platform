"""MT5 HTML export parsing: a number that fails to parse becomes None, and None is invisible.

    Why this file exists

    2026-10-15, from the mutation sweep over `validation/`'s nineteen unexamined money functions:

        M24  drop `,` -> `.` conversion in _extract_parenthesized_number   SURVIVED - 5,004 passed

    The failure is a **silent drop**, not a wrong number, which is what makes it easy to miss. MT5 writes
    European-formatted decimals with a comma. Without the conversion:

        float("1,25")   -> ValueError   ->  the function returns None
        float("1.25")   -> 1.25

    So a parenthesised figure in a European-locale export becomes `None` rather than a value. Every
    consumer of that `None` is downstream of a parse, so nothing raises — the figure is simply absent
    from the import, and the resulting report looks like a broker that did not report it.

    My first description of this mutation was that `1,25` would "parse as 1". That was wrong, and the
    suite never contradicted me because nothing was pinned. Checking `float("1,25")` directly rather
    than assuming is what caught it — the same lesson as the cash/equity swap at 0a4e8f9, where reading
    the code closely rather than reasoning about it is what distinguished the two failure modes.

    The other three mutations in this batch were killed by existing tests and needed nothing:
    `_stop_distance` ignoring the entry price (3 tests), `enforce_minimum_position_size` not enforcing
    the minimum (6 tests), and the risk readiness check ignoring every failed trade comparison.
"""

from __future__ import annotations

import pytest

from validation.importers.mt5_html import MT5HTMLImporter


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("(1.25)", 1.25),        # already dotted - the baseline
        ("(1,25)", 1.25),        # European decimal comma
        ("(1 234,56)", 1234.56),  # European thousands separator AND decimal comma
        ("( 2,5 )", 2.5),        # whitespace is stripped before parsing
        ("(0,125)", 0.125),
    ],
)
def test_a_parenthesized_number_is_parsed_in_either_locale(text: str, expected: float) -> None:
    assert MT5HTMLImporter._extract_parenthesized_number(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    ["", None, "no parentheses here", "()", "(abc)", "(1,2,3)"],
)
def test_something_that_is_not_a_number_returns_none(text: object) -> None:
    """None is the documented "nothing here" answer, so it must be reserved for it.

    A European-formatted number must not land here: the caller cannot distinguish "MT5 did not report
    this" from "we failed to parse what it reported", and the first is a fact while the second is a
    defect.
    """
    assert MT5HTMLImporter._extract_parenthesized_number(text) is None  # type: ignore[arg-type]
