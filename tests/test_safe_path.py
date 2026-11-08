"""Focused tests for the caller-supplied path guard used by the operator scripts."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts._safe_path import UnsafePathError, reject_traversal, under_root


def test_reject_traversal_refuses_a_dotdot_segment() -> None:
    with pytest.raises(UnsafePathError, match="containing '\\.\\.'"):
        reject_traversal("../etc/passwd", field="input")


def test_under_root_allows_a_relative_path_below_the_root(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    resolved = under_root("sub/out.json", root=tmp_path, field="output")
    assert resolved == (tmp_path / "sub" / "out.json").resolve()


def test_under_root_allows_an_absolute_path_below_the_root(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    assert under_root(target, root=tmp_path, field="output") == target.resolve()


def test_under_root_refuses_an_absolute_path_outside_the_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.json"
    with pytest.raises(UnsafePathError, match="outside"):
        under_root(outside, root=tmp_path, field="output")


def test_under_root_refuses_a_relative_escape(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError, match="containing '\\.\\.'"):
        under_root("../outside.json", root=tmp_path, field="output")
