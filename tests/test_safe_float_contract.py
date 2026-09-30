"""One numeric-coercion rule, three implementations, and no test for any of them.

`_safe_float` is written out in `live_runner/account_discovery.py`, `viewer/core.py` and
`viewer/main.py`. The three bodies are identical, and an identical-function-body sweep
found them - which is worth recording, because identical bodies are exactly the state
that hides a divergence. Nothing related them, and nothing tested them.

They are deliberately not unified. `live_runner` importing from `viewer` would be the
wrong dependency, and the viewer pair already share a module. What is missing is not a
shared definition but a pinned contract, so that if one copy loses the part that is not
obvious, it fails.

The part that is not obvious is `math.isfinite`. `float("nan")` does not raise, so
without that check a NaN sails straight through and poisons whatever consumes it - a
balance, an exposure figure, a metric. Every one of these three is on a path where a NaN
would be silently wrong rather than loud.

`viewer/main.py::_optional_float` is the deliberate counter-example and is pinned here
too: an *absent* metric must stay absent rather than becoming the `0.0` its sibling
substitutes, which is the rule the notes record as a real fix.
"""

from __future__ import annotations

import math

import pytest

from live_runner.account_discovery import _safe_float as discovery_safe_float
from viewer.core import _safe_float as core_safe_float
from viewer.main import _optional_float, _safe_float as main_safe_float

# keyword-only in account_discovery, positional in the viewer pair - the divergence the
# call sites have to respect, so the caller is adapted rather than the contract weakened
IMPLS = {
    "live_runner.account_discovery": lambda v, d: discovery_safe_float(v, default=d),
    "viewer.core": lambda v, d: core_safe_float(v, d),
    "viewer.main": lambda v, d: main_safe_float(v, d),
}


@pytest.mark.parametrize("name", sorted(IMPLS))
@pytest.mark.parametrize(
    "raw",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        "nan",
        "inf",
    ],
)
def test_non_finite_input_becomes_the_default_not_itself(name: str, raw: object) -> None:
    """`float("nan")` does not raise, so nothing but this guard stops it.

    The failing case is `float("nan")` rather than `"abc"`: an unparseable value raises
    inside `float()` and the `except` already handles it. NaN is the value that gets
    through every other branch.
    """
    result = IMPLS[name](raw, 0.0)
    assert math.isfinite(result), f"{name} returned {result!r} for {raw!r}"
    assert result == 0.0, f"{name} substituted {result!r} instead of the default"


@pytest.mark.parametrize("name", sorted(IMPLS))
@pytest.mark.parametrize("raw", [None, "", "abc", [], {}, object()])
def test_unparseable_input_becomes_the_default(name: str, raw: object) -> None:
    assert IMPLS[name](raw, -1.0) == -1.0


@pytest.mark.parametrize("name", sorted(IMPLS))
def test_a_real_number_passes_through_unchanged(name: str) -> None:
    for raw, expected in ((1.5, 1.5), ("1.5", 1.5), (2, 2.0), ("-3", -3.0), (0, 0.0)):
        assert IMPLS[name](raw, 99.0) == expected, f"{name} mangled {raw!r}"


def test_the_three_copies_agree() -> None:
    """The point of the sweep result: they agree, and nothing was holding them there.

    Asserted over the values that decide each branch, so a copy that loses `isfinite`
    fails here as well as in the test above - and the failure names which copy.
    """
    probes: list[object] = [float("nan"), float("inf"), None, "", "abc", 1.5, "1.5", 0]
    for raw in probes:
        results = {name: impl(raw, 0.0) for name, impl in IMPLS.items()}
        assert len(set(results.values())) == 1, (
            f"_safe_float copies disagree on {raw!r}: {results}"
        )


def test_an_absent_metric_stays_absent_rather_than_becoming_zero() -> None:
    """`_optional_float` is the deliberate exception, and it is a rule worth pinning.

    `_safe_float` substitutes a default, which for an unmeasured metric is a fabricated
    number - the notes record that being fixed, and that `shared.models` now owns the
    convention. The sibling here must keep letting `None` through, or the fix silently
    comes back in this one file.
    """
    assert _optional_float(None) is None
    assert _optional_float("") is None
    assert _optional_float(float("nan")) is None
    assert _optional_float("abc") is None
    assert _optional_float("1.5") == 1.5
