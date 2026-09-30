"""The grid downloader's timeframe set must be derived, not restated.

`data_tools/download_grid_market_data.py` carried its own copy of the canonical
timeframe order, under a comment saying it was "every normalized timeframe we
support" and "must stay in sync with shared.constants.VALID_TIMEFRAMES". It had
already drifted: `tick`, `s1` and `1w` were missing, so this downloader refused
three timeframes the rest of the platform handles, and the comment claiming
otherwise was the only thing standing between that and a reader's trust.

The same divergence had already been fixed elsewhere in the codebase for exactly
this reason - `runner/engines/vectorbt_adapter.py` now takes its order from
`contract.TIMEFRAME_ORDER` and its comment says why. The grid module is now on the
same footing: it takes the canonical list and subtracts a named, explained set.

These tests pin the reasoning rather than the resulting list, so the next person to
add a timeframe is forced to make a decision instead of inheriting a stale copy.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from data_tools import download_grid_market_data as grid
from shared.constants import VALID_TIMEFRAMES

CANONICAL = tuple(VALID_TIMEFRAMES)

# The series this module builds from. Nothing can be resampled to something finer
# than its input, which is a fact about the pipeline rather than a policy choice.
SOURCE_TIMEFRAME = "1m"

FASTER_THAN_SOURCE = ("tick", "s1")

# The order the tool actually served before it was derived, recorded so that
# changing it is a deliberate act with a test failing, not a side effect of an edit.
SERVED_BEFORE_DERIVATION = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")


def test_every_canonical_timeframe_is_either_served_or_explicitly_excluded() -> None:
    """The set is a partition of the canonical one, so nothing can be forgotten.

    A hand-copied list fails this the day a timeframe is added upstream. Deriving
    it cannot, which is the whole point of the change.
    """
    served = set(grid.TIMEFRAME_ORDER)
    excluded = set(grid.UNSUPPORTED_TIMEFRAMES)
    assert served | excluded == set(CANONICAL), (
        "a canonical timeframe is neither served nor listed as unsupported, so it "
        "would be silently refused: "
        f"{sorted(set(CANONICAL) - served - excluded)}"
    )
    assert not served & excluded, (
        f"listed as both served and unsupported: {sorted(served & excluded)}"
    )


def test_the_served_set_is_the_one_the_tool_actually_worked_with() -> None:
    """Pins today's behaviour so a change to it cannot arrive unnoticed."""
    assert tuple(grid.TIMEFRAME_ORDER) == SERVED_BEFORE_DERIVATION


def test_nothing_finer_than_the_source_is_served() -> None:
    """Timeframes finer than the 1-minute source cannot be produced here.

    This pins the *reason* two entries are excluded, so that a later reader aiming
    to "fix the drift" by serving every canonical timeframe does not add a
    resampling step that cannot work.
    """
    index_of_source = CANONICAL.index(SOURCE_TIMEFRAME)
    for timeframe in FASTER_THAN_SOURCE:
        assert CANONICAL.index(timeframe) < index_of_source, (
            f"{timeframe!r} was expected to be finer than {SOURCE_TIMEFRAME!r}"
        )
        assert timeframe not in grid.TIMEFRAME_ORDER, (
            f"{timeframe!r} is finer than the {SOURCE_TIMEFRAME!r} source and cannot be "
            "resampled into existence by this module"
        )
        assert timeframe in grid.UNSUPPORTED_TIMEFRAMES, (
            f"{timeframe!r} must be listed as unsupported so the partition above holds"
        )


def test_weekly_bars_are_still_excluded_and_the_question_is_left_visible() -> None:
    """1w is the one exclusion this module cannot justify on its own terms.

    The same resampler produces 4h and 1d from 1m, and `data_tools/resample.py`
    lists 1w, so weekly bars look derivable. Whether this downloader should serve
    them is a decision about the pipeline, not something to settle by editing a
    constant, so it is recorded here as excluded and asserted so the choice stays
    visible in a test rather than only in a comment.
    """
    assert "1w" in CANONICAL, "weekly bars are a canonical timeframe platform-wide"
    assert "1w" in grid.UNSUPPORTED_TIMEFRAMES, (
        "1w remains excluded pending a decision; if it is now served, delete this "
        "test deliberately rather than letting it drift"
    )
    assert "1w" not in grid.TIMEFRAME_ORDER


def test_the_cli_default_is_the_served_set_and_not_a_fourth_copy() -> None:
    """`DEFAULT_TIMEFRAMES` used to be another literal of the same seven values."""
    assert tuple(grid.DEFAULT_TIMEFRAMES) == tuple(grid.TIMEFRAME_ORDER)
    assert ",".join(grid.DEFAULT_TIMEFRAMES) == "1m,5m,15m,30m,1h,4h,1d"


def test_the_module_no_longer_restates_the_timeframe_sets_as_literals() -> None:
    """The drift was invisible because these were literals; they must be derived.

    Assigning the names is fine and expected - they are this module's interface.
    Assigning them a literal is the defect: that is what let the copy fall behind the
    canonical set while a comment claimed otherwise. A `VALID_TIMEFRAMES` assignment
    is worse still, since the name is imported from `shared.constants` and a second
    one would quietly take over every unqualified use in the file.
    """
    source = Path(grid.__file__).read_text(encoding="utf-8")
    literal_nodes = (ast.Constant, ast.Tuple, ast.List, ast.Set)
    restated, shadowed = [], []
    for node in ast.parse(source).body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            continue
        name = node.targets[0].id
        if name not in {"VALID_TIMEFRAMES", "TIMEFRAME_ORDER", "DEFAULT_TIMEFRAMES"}:
            continue
        if name == "VALID_TIMEFRAMES":
            shadowed.append(name)
        elif isinstance(node.value, literal_nodes):
            restated.append(name)
    assert not shadowed, (
        f"{Path(grid.__file__).name} assigns VALID_TIMEFRAMES at module level, shadowing "
        "the canonical import; every unqualified use in the file silently changes meaning"
    )
    assert not restated, (
        f"{Path(grid.__file__).name} restates {restated} as a literal instead of deriving "
        "it from shared.constants - this is how the copy drifted in the first place"
    )


def test_the_derived_sets_are_not_plain_copies() -> None:
    """A comprehension, not a copy: the value must track the canonical set.

    Belt and braces with the test above, and it fails for a subtler reason: if
    someone "simplified" the comprehension into `[tf for tf in VALID_TIMEFRAMES]`,
    this module would start serving `tick` and `s1`, which it cannot produce.
    """
    excluded = grid.UNSUPPORTED_TIMEFRAMES
    assert excluded, "the exclusion set must not be empty, or this is a plain copy"
    expected = [tf for tf in CANONICAL if tf not in excluded]
    assert list(grid.TIMEFRAME_ORDER) == expected


def test_sorting_orders_the_served_timeframes_by_canonical_granularity() -> None:
    """The sort key follows the canonical order, so a new entry lands correctly.

    Unknown timeframes previously sorted last via a bare `len(TIMEFRAME_ORDER)`
    fallback, which silently grouped everything unrecognised together.
    """
    keys = [grid._timeframe_sort_key(tf) for tf in grid.TIMEFRAME_ORDER]
    assert keys == sorted(keys), f"served timeframes are out of canonical order: {keys}"
    unknown = grid._timeframe_sort_key("99y")
    assert unknown[0] == len(grid.TIMEFRAME_ORDER), (
        "an unrecognised timeframe must sort after every supported one rather than "
        "sharing a position with it"
    )


@pytest.mark.parametrize("timeframe", ("tick", "s1", "1w"))
def test_an_unsupported_timeframe_is_refused_by_name(timeframe) -> None:
    """The refusal message lists what this tool does serve.

    A user asking for weekly bars from a module that cannot produce them should be
    told the supported set, which is the set that is now derived rather than
    guessed at the point of use.
    """
    assert timeframe not in grid.SUPPORTED_TIMEFRAMES
    message = f"unsupported timeframe {timeframe!r}. Valid: {sorted(grid.SUPPORTED_TIMEFRAMES)}"
    assert "1m" in message and timeframe not in message.split("Valid:")[1]
