"""The repository modules a signal-bot processor executes are bound by the promotion (#165)."""

from __future__ import annotations

from pathlib import Path

import pytest

from live_runner.promotion import (
    PromotionTargetError,
    repository_import_closure,
)


def _write(root: Path, relative: str, text: str = "") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_absolute_imports_bind_the_module_and_its_parent_packages(tmp_path: Path) -> None:
    _write(tmp_path, "pkg_c/__init__.py", "")
    _write(tmp_path, "pkg_c/leaf.py", "VALUE = 1\n")
    start = _write(tmp_path, "pkg_a/processor.py", "from pkg_c.leaf import VALUE\n")
    closure = repository_import_closure(start, repo_root=tmp_path)
    # Importing pkg_c.leaf executes pkg_c/__init__.py first, so both are bound.
    assert set(closure) == {"pkg_c/__init__.py", "pkg_c/leaf.py"}


def test_the_closure_is_transitive_and_follows_import_module_aliases(tmp_path: Path) -> None:
    _write(tmp_path, "pkg_b/processor.py", "from pkg_d import deep\n")
    _write(tmp_path, "pkg_d/deep.py", "X = 1\n")
    _write(tmp_path, "pkg_d/__init__.py", "")
    start = _write(tmp_path, "pkg_a/processor.py", "from pkg_b import processor as p\n")
    closure = repository_import_closure(start, repo_root=tmp_path)
    assert set(closure) == {"pkg_b/processor.py", "pkg_d/__init__.py", "pkg_d/deep.py"}


def test_relative_imports_resolve_against_the_importing_package(tmp_path: Path) -> None:
    _write(tmp_path, "pkg_a/__init__.py", "")
    _write(tmp_path, "pkg_a/helper.py", "X = 1\n")
    _write(tmp_path, "pkg_a/sub/__init__.py", "")
    _write(tmp_path, "pkg_a/sub/other.py", "Y = 2\n")
    start = _write(
        tmp_path,
        "pkg_a/sub/processor.py",
        "from . import other\nfrom .. import helper\n",
    )
    closure = repository_import_closure(start, repo_root=tmp_path)
    assert set(closure) == {
        "pkg_a/__init__.py",
        "pkg_a/helper.py",
        "pkg_a/sub/__init__.py",
        "pkg_a/sub/other.py",
    }


def test_a_relative_import_above_the_repository_root_is_refused(tmp_path: Path) -> None:
    start = _write(tmp_path, "pkg_a/processor.py", "from ...... import nothing\n")
    with pytest.raises(PromotionTargetError, match="above the repository root"):
        repository_import_closure(start, repo_root=tmp_path)


def test_standard_library_and_third_party_imports_are_not_bound(tmp_path: Path) -> None:
    start = _write(
        tmp_path,
        "pkg_a/processor.py",
        "import json\nfrom typing import Any\nimport numpy\n",
    )
    assert repository_import_closure(start, repo_root=tmp_path) == {}


def test_a_changed_module_changes_its_digest(tmp_path: Path) -> None:
    _write(tmp_path, "pkg_b/mod.py", "X = 1\n")
    start = _write(tmp_path, "pkg_a/processor.py", "from pkg_b import mod\n")
    before = repository_import_closure(start, repo_root=tmp_path)["pkg_b/mod.py"]
    _write(tmp_path, "pkg_b/mod.py", "X = 2\n")
    after = repository_import_closure(start, repo_root=tmp_path)["pkg_b/mod.py"]
    assert before != after


def test_the_promoted_file_itself_is_not_part_of_its_own_closure(tmp_path: Path) -> None:
    start = _write(tmp_path, "pkg_a/processor.py", "import pkg_a.processor\n")
    assert repository_import_closure(start, repo_root=tmp_path) == {}


def test_the_start_modules_parent_package_inits_are_bound(tmp_path: Path) -> None:
    _write(tmp_path, "top/__init__.py", "")
    _write(tmp_path, "top/inner/__init__.py", "")
    start = _write(tmp_path, "top/inner/processor.py", "")
    closure = repository_import_closure(start, repo_root=tmp_path)
    assert set(closure) == {"top/__init__.py", "top/inner/__init__.py"}


def test_a_start_parent_init_is_walked_for_its_own_imports(tmp_path: Path) -> None:
    _write(tmp_path, "top/__init__.py", "from helper import X\n")
    _write(tmp_path, "helper.py", "X = 1\n")
    start = _write(tmp_path, "top/processor.py", "")
    closure = repository_import_closure(start, repo_root=tmp_path)
    assert set(closure) == {"top/__init__.py", "helper.py"}


def test_ramjet_closure_contains_its_package_inits() -> None:
    from signal_bots.registry import get_signal_processor_module_path

    closure = repository_import_closure(get_signal_processor_module_path("ramjet"))
    assert "signal_bots/__init__.py" in closure
    assert "signal_bots/ramjet/__init__.py" in closure


def test_a_module_symlinked_outside_the_repository_is_refused(tmp_path: Path) -> None:
    """An imported module that resolves outside the repo cannot be bound: refuse, fail closed."""
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leaf.py").write_text("VALUE = 1\n", encoding="utf-8")
    start = _write(repo, "pkg_a/processor.py", "from pkg_c.leaf import VALUE\n")
    _write(repo, "pkg_c/__init__.py")
    (repo / "pkg_c" / "leaf.py").symlink_to(outside / "leaf.py")

    with pytest.raises(PromotionTargetError, match="outside the repository"):
        repository_import_closure(start, repo_root=repo)
