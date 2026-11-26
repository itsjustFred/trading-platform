"""Tests for ``shared.constants.ensure_path_within_workspace``.

The helper guards operator-facing CLI output paths. Its contract has two
halves: a relative path means what ``Path.resolve()`` says it means (relative
to the process working directory), and the resolved path must stay inside the
project workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.constants import PROJECT_ROOT, ensure_path_within_workspace


def test_relative_path_is_anchored_at_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative ``--output-dir`` is relative to cwd, not to the repo root.

    Anchoring it at the repository root instead would silently move an
    operator's output when the tool is run from a subdirectory, and would make
    a relative path outside the repository pass instead of being refused.
    """
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="escapes the project workspace"):
        ensure_path_within_workspace(Path("reports"))


def test_relative_path_inside_the_workspace_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(PROJECT_ROOT)
    assert ensure_path_within_workspace(Path("reports")) == (PROJECT_ROOT / "reports").resolve()


def test_absolute_path_inside_the_workspace_is_accepted() -> None:
    target = PROJECT_ROOT / "reports" / "out.json"
    assert ensure_path_within_workspace(target) == target


def test_absolute_path_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes the project workspace"):
        ensure_path_within_workspace(tmp_path / "out.json")


def test_the_workspace_root_itself_is_accepted() -> None:
    assert ensure_path_within_workspace(PROJECT_ROOT) == PROJECT_ROOT
