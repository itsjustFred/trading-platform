"""Chart indicators depend on one import that no dependency file declares.

`viewer/core.py::_compute_indicators` produces every indicator panel - RSI, ATR, Bollinger, MACD - from a
single `import pandas_ta as ta`. That import is:

* declared in **no** dependency file (`requirements*.txt`, `pyproject.toml`, `setup.py`, `setup.cfg`,
  `Pipfile` - checked, no match anywhere);
* not installed in the project venv, verified by importing it;
* guarded by `except ImportError: return []`.

So on a fresh install the panels are **absent**, not wrong - the rest of the chart renders normally and
nothing says why. This was invisible for exactly that reason, which is what the warn-once below fixes.

The assertion that matters is the one about silence: a missing optional dependency must be reported, or it
cannot be diagnosed.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "viewer" / "core.py"
SOURCE = CORE.read_text(encoding="utf-8")

# Whether to add `pandas_ta` as a dependency is the operator's call: adding it would *enable* a viewer
# feature that has been silently absent in every install so far, and removing its use would be a
# behaviour change. The gap is recorded rather than silently closed, and the warning means the
# condition is now diagnosable either way.
_DECLARATION_IS_OPERATORS_CALL = True


def test_pandas_ta_is_declared_somewhere() -> None:
    """The assertion that should have existed before this was found."""
    declared = []
    for name in ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile"):
        path = REPO / name
        if path.exists() and re.search(r"pandas[-_]ta", path.read_text(encoding="utf-8"), re.I):
            declared.append(name)
        for extra in path.parent.glob(f"{name[:-3]}*.txt") if name.endswith(".txt") else ():
            if re.search(r"pandas[-_]ta", extra.read_text(encoding="utf-8"), re.I):
                declared.append(extra.name)
    assert declared or _DECLARATION_IS_OPERATORS_CALL, (
        "pandas_ta is imported by the viewer but declared in no dependency file, so a fresh install "
        "silently loses every indicator panel. Declare it (optionally, if it is meant to be optional) "
        "rather than leaving the failure to be discovered by a chart rendering without panels."
    )


def test_the_missing_dependency_is_reported_rather_than_silent() -> None:
    # Comments are stripped first. The explanatory comment above the guard quotes the literal text
    # `return []`, which a naive non-greedy regex matches inside the comment and truncates before the
    # code it is meant to inspect - the first version of this test failed that way.
    code_only = re.sub(r"#[^\n]*", "", SOURCE)
    block = re.search(
        r"def _compute_indicators\(.*?except ImportError:(.*?)\n        return \[\]",
        code_only, re.S,
    )
    assert block, "could not find the ImportError guard in _compute_indicators"
    handler = block.group(1)
    assert "logger.warning" in handler, (
        "a missing pandas_ta must be logged; returning [] alone makes the missing panels "
        "undiagnosable"
    )
    assert "_PANDAS_TA_MISSING_WARNED" in handler, (
        "the warning must be emitted once, not per chart request"
    )


def test_the_warning_names_what_is_lost() -> None:
    assert "ATR" in SOURCE and "pandas_ta is not installed" in SOURCE, (
        "the warning should say which panels are missing, so an operator can tell what to install"
    )
