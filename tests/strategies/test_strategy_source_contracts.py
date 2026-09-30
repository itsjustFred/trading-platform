"""Guards on strategy source and defaults that need no market data.

These live outside the data-gated modules on purpose.

`tests/strategies/conftest.py` installs an `autouse`, module-scoped fixture that skips
an entire module when its `TEST_SYMBOL`/`TEST_TIMEFRAME` dataset is absent. That is
right for regression tests that genuinely backtest over real bars, and wrong for checks
that only read a source file or call a function with synthetic input.

Two such checks were trapped in it and had never run in any environment lacking the
data:

- `test_generate_signals_has_parameter_defaults` parses a strategy file. It cannot
  need market data; it did not run anyway.
- `test_threshold_defaults_match_source` asserted four exact substrings of a call
  expression. It did not run either.

Both are ordinary-looking skips in a suite reporting `94 skipped`, which is how they
stayed invisible. A skip reason is not evidence that the thing being skipped needed the
thing it skipped for.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# No TEST_SYMBOL / TEST_TIMEFRAME here on purpose: declaring them is what opts a module
# into the data gate. These checks must run everywhere.
CUSTOM_SWINGS_DIR = PROJECT_ROOT / "strategies" / "custom_swings_supertrend"
STAT_ARB_DIR = PROJECT_ROOT / "strategies" / "stat_arb_pairs"

# Every default `stat_arb_pairs.generate_signals` reads, so a new one cannot be added
# undocumented. Kept separate from the tunable thresholds below because the load-bearing
# test perturbs these arithmetically, which does not apply to a boolean.
STAT_ARB_ALL_DEFAULTS = {
    "lookback": 60,
    "entry_zscore": 2.0,
    "exit_zscore": 0.5,
    "stop_zscore": 4.0,
    "use_atr_stop": False,
    "atr_period": 14,
    "atr_stop_mult": 2.0,
}

# The tunable thresholds, and the ones that must keep applying when params is empty.
STAT_ARB_DEFAULTS = {
    "lookback": 60,
    "entry_zscore": 2.0,
    "exit_zscore": 0.5,
    "stop_zscore": 4.0,
}


# These strategies are **git-tracked files**. A missing or unimportable one is a
# defect, not an environment condition - and this file originally called
# `pytest.skip` in all three of these places, which is how six of its seven guards
# could be made to vanish in silence. Measured: renaming
# `strategies/stat_arb_pairs` away gave **6 skipped, 1 passed, exit 0**. The pass
# count looked fine and six guards provided no evidence at all.
#
# So every one of these is a hard failure. This is the same defect this branch has
# been closing all session - a check that stops being evidence without saying so -
# found in code written to *fix* that defect, which is the part worth recording.
STRATEGY_SOURCE_MISSING = (
    "a git-tracked strategy source is missing or unimportable; these guards exist to "
    "pin that source, so its absence is the failure they were written to report. "
    "A skip here would make them pass by not running."
)


def _strategy_source(directory: Path) -> Path:
    """Resolve the strategy module inside a strategy directory."""
    matches = sorted(directory.glob("*.py"))
    if not matches:
        raise AssertionError(f"{STRATEGY_SOURCE_MISSING} No source under {directory}")
    preferred = directory / f"{directory.name}.py"
    if preferred in matches:
        return preferred
    return matches[0]


def _load_strategy(path: Path, module_name: str):
    if not path.exists():
        candidates = sorted(path.parent.glob(f"{path.stem}.py"))
        if not candidates:
            raise AssertionError(f"{STRATEGY_SOURCE_MISSING} Not found: {path}")
        path = candidates[0]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"{STRATEGY_SOURCE_MISSING} Not importable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _synthetic_ohlcv(rows: int = 400, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, rows))))
    # A deliberate spike, so the z-score reaches extremes and the level-triggered
    # branches are reachable at all. On a pure random walk the tails barely move.
    close.iloc[rows // 2 :] *= 0.6
    return pd.DataFrame(
        {
            "close": close.to_numpy(),
            "high": close * 1.005,
            "low": close * 0.995,
            "open": close.shift(1).fillna(close.iloc[0]).to_numpy(),
            "volume": 1_000_000.0,
        }
    )


def test_generate_signals_has_parameter_defaults() -> None:
    """The optimisation UI is generated from the signature, so defaults must exist.

    Originally in `test_custom_swings_supertrend.py`, where it never ran: that module
    declares `TEST_SYMBOL = "XAUUSD"` and `TEST_TIMEFRAME = "1h"`, so the data gate
    skipped all 8 of its tests, including this one, in every environment without that
    dataset. It parses a source file. It does not need the dataset.
    """
    src = _strategy_source(CUSTOM_SWINGS_DIR).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "generate_signals"
        ),
        None,
    )
    assert fn is not None, "generate_signals Funktion nicht gefunden!"
    n_defaults = len(fn.args.defaults) + len(fn.args.kw_defaults)
    assert n_defaults > 0, (
        "generate_signals hat keine Keyword-Argumente mit Default-Werten — "
        "der AST-Parser kann kein Optimierungs-UI generieren!"
    )


def _perturbed(name: str, value: float) -> float:
    """A value that makes the parameter's branch *more* reachable, not less.

    The first attempt scaled by 1.5 and added 1, which moved `stop_zscore` to 7.0 and
    left the condition `z <= -7.0` unreachable on any random walk - so the test failed
    for a reason that had nothing to do with the parameter being wired up. Perturbing
    downward keeps the branch in play: every one of these thresholds is a "did the score
    reach this level" test, so loosening it is the direction that can actually fire.
    """
    if name == "lookback":
        # A shorter window changes every rolling statistic, so any multiple works.
        return max(5.0, value / 4.0)
    return max(0.05, value / 4.0)


def _assert_outputs_identical(left, right, obj: str) -> None:
    """Compare two `generate_signals` results, whatever container it returns.

    The strategy returns ``(entries, exits, indicators, drawings)`` where indicators is a
    dict of Series here, not a DataFrame, so a blanket ``assert_frame_equal`` is the
    wrong tool. Asserting on the exact container is the same mistake as pinning the
    exact call spelling.
    """
    assert len(left) == len(right), f"{obj}: arity changed {len(left)} -> {len(right)}"
    for index, (lhs, rhs) in enumerate(zip(left, right)):
        if isinstance(lhs, pd.DataFrame):
            pd.testing.assert_frame_equal(lhs, rhs, check_exact=False)
        elif isinstance(lhs, pd.Series):
            pd.testing.assert_series_equal(lhs, rhs, check_exact=False)
        elif isinstance(lhs, dict):
            assert lhs.keys() == rhs.keys(), (
                f"{obj}: indicator keys diverged at position {index}"
            )
            for key in lhs:
                pd.testing.assert_series_equal(
                    lhs[key], rhs[key], check_exact=False, obj=f"{obj}: {key}"
                )
        else:
            assert lhs == rhs, f"{obj}: position {index} diverged"


def test_stat_arb_documented_defaults_match_the_source_literals() -> None:
    """The documented defaults must be the literals in the code.

    This exists because the behavioural test above is not sufficient on its own, and
    mutation testing is what showed it. Shifting `stop_zscore` from 4.0 to 3.9 left the
    behavioural pair **green**: the generated series puts almost no mass in the narrow
    band `(-4.0, -3.9]`, so both settings produce identical entries, exits and reasons.
    A behavioural equivalence test can only detect a change the data exercises.

    The four substring assertions this file replaced *did* catch that mutation, by
    accident of spelling. So the value check is kept, but on the literal itself rather
    than on the surrounding call syntax - a shifted default is a changed default whether
    or not the refactored code still reads the same way.

    Read from the AST, so reformatting the call cannot break it, and a default hoisted
    to a module constant is reported as a mismatch to be resolved deliberately rather
    than silently passing.
    """
    source = _strategy_source(STAT_ARB_DIR).read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "generate_signals"
        ),
        None,
    )
    assert fn is not None, "generate_signals not found"

    literals: dict[str, float] = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Name)
            and func.value.id == "params"
        ):
            continue
        if len(node.args) != 2 or not isinstance(node.args[0], ast.Constant):
            continue
        value = node.args[1]
        if isinstance(value, ast.Constant) and isinstance(value.value, (int, float)):
            literals[node.args[0].value] = value.value

    assert literals == STAT_ARB_ALL_DEFAULTS, (
        "the defaults in the source no longer match the documented set; "
        f"source={literals} documented={STAT_ARB_ALL_DEFAULTS}"
    )


def test_stat_arb_documented_defaults_are_the_defaults_actually_used() -> None:
    """The empty-params path must produce the documented thresholds.

    This replaces four substring assertions of the form
    `assert 'params.get("lookback", 60)' in src`, which pinned the *spelling* of a call
    expression rather than the value it produces. Any correct refactor — hoisting the
    literal to a module constant, switching to `params.get("lookback", DEFAULT_LOOKBACK)`
    — would have failed them, and they would equally have passed against a comment
    containing the same text. They also never ran: `test_stat_arb_pairs.py` declares
    `TEST_SYMBOL = "AAPL"`, so the data gate skipped them.

    What actually matters is the property behind them: with no params, the strategy must
    behave identically to being handed the documented defaults explicitly. That is
    checked here by running both and comparing, on synthetic bars — so it needs no
    dataset and cannot pass by accident.
    """
    module = _load_strategy(
        STAT_ARB_DIR / "stat_arb_pairs.py", "_ungated_stat_arb_pairs"
    )
    df = _synthetic_ohlcv()

    out_empty = module.generate_signals(df, {})
    out_explicit = module.generate_signals(df, STAT_ARB_DEFAULTS)

    _assert_outputs_identical(
        out_empty,
        out_explicit,
        (
            "generate_signals(df, {}) diverged from generate_signals(df, "
            f"{STAT_ARB_DEFAULTS}); the documented defaults are not the ones in effect"
        ),
    )


@pytest.mark.parametrize(
    "name,value",
    sorted(STAT_ARB_DEFAULTS.items()),
)
def test_stat_arb_default_is_actually_load_bearing(name: str, value: float) -> None:
    """Each documented default must still influence the output.

    Without this, the test above would pass even if `generate_signals` ignored `params`
    entirely and hardcoded values that happen to coincide - which is exactly the state
    the four substring assertions were pretending to rule out. Overriding one default
    must change the result; if it does not, the default is not wired up.
    """
    module = _load_strategy(
        STAT_ARB_DIR / "stat_arb_pairs.py", "_ungated_stat_arb_pairs_load_bearing"
    )
    df = _synthetic_ohlcv()

    baseline = module.generate_signals(df, {})
    perturbed = dict(STAT_ARB_DEFAULTS)
    perturbed[name] = _perturbed(name, value)
    changed = module.generate_signals(df, perturbed)

    try:
        _assert_outputs_identical(baseline, changed, "identical")
    except AssertionError:
        return
    raise AssertionError(
        f"overriding {name} did not change the output at all, so its default is not "
        f"load bearing: generate_signals(df, {{}}) == generate_signals(df, "
        f"{{{name}: {perturbed[name]}}})"
    )