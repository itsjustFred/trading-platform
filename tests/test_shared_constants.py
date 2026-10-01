"""`shared/constants.py` guards that nothing tested.

Found by mutation testing `shared/constants.py` with `mutate.sh`: deleting the
`is_relative_to` check in `ensure_path_within_workspace` — and dropping its
`.expanduser()` — both **survived** every selected test.

That function is a security guard. Its own docstring names the reason it exists:
it guards "operator-facing CLI tools against LLM-supplied arguments that escape
the repository (python:S8707)". It has exactly one production caller,
`scripts/run_risk_parity_backtest.py:698`, on an operator-supplied
`--output-dir`. And it had **no test reference anywhere in the repository**, so
removing the containment check entirely changed nothing observable.

The `.expanduser()` half is the subtler one. Without it, a `~` path is not
expanded, so `~/.ssh/id_rsa` resolves to `<project>/~/.ssh/id_rsa` — which is
*inside* `PROJECT_ROOT`, so the containment check passes and the path is
**accepted**. The guard stops refusing things it refuses today. It is not an
escape, but it is a refusal that silently becomes an acceptance, which is
exactly the direction a security guard should never move on its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.constants import PROJECT_ROOT, ensure_path_within_workspace


def test_a_path_inside_the_workspace_is_accepted(tmp_path: Path) -> None:
    """The positive case, so the guard cannot be satisfied by refusing everything."""
    inside = PROJECT_ROOT / "data" / "results.parquet"
    assert ensure_path_within_workspace(inside) == inside.resolve()


def test_resolution_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same absolute path resolves identically from any directory.

    Written with an absolute path on purpose: `tests/test_trade_engine.py` pins
    that no test may resolve a *repo-relative* path against the working
    directory, and this file was caught by that pin when it did exactly that.
    Changing the cwd here is what makes the assertion about CWD-independence,
    rather than about the path spelling.
    """
    target = PROJECT_ROOT / "data" / "results.parquet"

    monkeypatch.chdir(tmp_path)
    from_outside = ensure_path_within_workspace(target)
    monkeypatch.chdir(PROJECT_ROOT)
    from_inside = ensure_path_within_workspace(target)

    assert from_outside == from_inside == target.resolve()


@pytest.mark.parametrize(
    "candidate",
    [
        "/etc/passwd",
        "/tmp/anything",
        "../../../etc/passwd",
        "data/../../../../etc/passwd",
    ],
)
def test_a_path_escaping_the_workspace_is_refused(candidate: str) -> None:
    """The load-bearing case. Absolute and traversal escapes both raise."""
    with pytest.raises(ValueError, match="escapes the project workspace"):
        ensure_path_within_workspace(Path(candidate))


def test_a_home_relative_path_is_refused() -> None:
    """Catches a missing `.expanduser()`.

    Unexpanded, `~` is an ordinary directory name, so the path resolves to
    `<project>/~/.ssh/id_rsa` - inside `PROJECT_ROOT`, and therefore *accepted*.
    Expanded, it is the real home directory, and refused.
    """
    with pytest.raises(ValueError, match="escapes the project workspace"):
        ensure_path_within_workspace(Path("~/.ssh/id_rsa"))


def test_the_returned_path_is_fully_resolved() -> None:
    """`..` and symlink-style segments are collapsed before the containment check."""
    resolved = ensure_path_within_workspace(PROJECT_ROOT / "data" / ".." / "data" / "x")
    assert ".." not in resolved.parts
    assert resolved.is_absolute()


def test_the_project_root_itself_is_accepted() -> None:
    """The boundary case: the root is inside itself, so it must not be refused.

    Uses an absolute path so this file never resolves a repo-relative path
    against the working directory, which `tests/test_trade_engine.py` pins against
    across the whole suite.
    """
    assert ensure_path_within_workspace(PROJECT_ROOT) == PROJECT_ROOT.resolve()