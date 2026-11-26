"""Table tests for :func:`shared.paths.safe_join` (proposal G26).

The table is the contract: every row is an input a viewer route or a script can
receive, and the expected outcome. ``..`` is refused even when it would resolve
back inside the base, because the written form is what a caller reasoned about;
the escape rows pin the cases that used to need a hand-written check each.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import pytest

from shared.paths import PathContainmentError, safe_join


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "file.csv").write_text("x", encoding="utf-8")
    (root / "a..b.json").write_text("{}", encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("file.csv", "file.csv"),
        ("sub/file.csv", "sub/file.csv"),
        ("./file.csv", "file.csv"),
        ("a..b.json", "a..b.json"),
        ("...", "..."),
        # A URL-encoded dot segment is a literal file name: this helper does
        # not decode. A caller that decodes before calling hands it ".." and
        # the next table refuses it.
        ("%2e%2e/x", "%2e%2e/x"),
    ],
)
def test_relative_paths_inside_the_base_are_accepted(base: Path, raw: str, expected: str) -> None:
    assert safe_join(base, raw) == (base / expected).resolve()


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        ".",
        "..",
        "../x",
        "../../etc/passwd",
        "a/../../x",
        # Climbing and coming back is still refused: the written form climbed.
        "sub/../file.csv",
        "/etc/passwd",
        "//etc/passwd",
        "x\x00y",
        # Windows-style separators are refused on every platform: on POSIX a
        # backslash is an ordinary filename character, so "..\\x" would reach
        # the filesystem as a single file name here but climb on Windows.
        "..\\x",
        "sub\\file.csv",
        "C:\\temp\\x",
        unquote("%2e%2e/x"),
    ],
)
def test_escape_patterns_are_refused(base: Path, raw: str) -> None:
    with pytest.raises(PathContainmentError):
        safe_join(base, raw)


def test_non_text_inputs_are_refused(base: Path) -> None:
    with pytest.raises(PathContainmentError):
        safe_join(base, None)  # type: ignore[arg-type]
    with pytest.raises(PathContainmentError):
        safe_join(base, b"file.csv")  # type: ignore[arg-type]


def test_base_itself_is_refused_unless_the_caller_means_it(base: Path) -> None:
    with pytest.raises(PathContainmentError):
        safe_join(base, ".")
    assert safe_join(base, ".", allow_base=True) == base.resolve()


def test_absolute_paths_are_refused_unless_the_caller_opts_in(base: Path) -> None:
    inside = base / "sub" / "file.csv"
    outside = base.parent / "outside.csv"
    outside.write_text("x", encoding="utf-8")

    with pytest.raises(PathContainmentError):
        safe_join(base, inside)
    assert safe_join(base, inside, allow_absolute=True) == inside.resolve()
    with pytest.raises(PathContainmentError):
        safe_join(base, outside, allow_absolute=True)


def test_symlink_pointing_outside_the_base_is_refused(base: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.csv"
    outside.write_text("secret", encoding="utf-8")
    (base / "escape.csv").symlink_to(outside)
    (base / "escape-dir").symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(PathContainmentError):
        safe_join(base, "escape.csv")
    with pytest.raises(PathContainmentError):
        safe_join(base, "escape-dir/secret.csv")


def test_symlink_staying_inside_the_base_is_accepted(base: Path) -> None:
    (base / "alias.csv").symlink_to(base / "sub" / "file.csv")

    assert safe_join(base, "alias.csv") == (base / "sub" / "file.csv").resolve()


def test_refuse_symlinks_rejects_any_symlink_below_the_base(base: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.csv"
    outside.write_text("secret", encoding="utf-8")
    (base / "alias.csv").symlink_to(base / "sub" / "file.csv")
    (base / "escape.csv").symlink_to(outside)

    with pytest.raises(PathContainmentError):
        safe_join(base, "alias.csv", refuse_symlinks=True)
    with pytest.raises(PathContainmentError):
        safe_join(base, "escape.csv", refuse_symlinks=True)
    # A plain file still passes.
    assert safe_join(base, "sub/file.csv", refuse_symlinks=True) == (
        base / "sub" / "file.csv"
    ).resolve()


def test_refuse_symlinks_rejects_a_symlinked_base(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "file.csv").write_text("x", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    # Default: a symlinked base is resolved, so the result is inside it.
    assert safe_join(linked, "file.csv") == (real / "file.csv").resolve()
    with pytest.raises(PathContainmentError):
        safe_join(linked, "file.csv", refuse_symlinks=True)


def test_a_symlinked_base_compares_against_its_resolved_target(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    (real / "sub" / "file.csv").write_text("x", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    assert safe_join(linked, "sub/file.csv") == (real / "sub" / "file.csv").resolve()


def test_missing_leaf_inside_the_base_is_returned(base: Path) -> None:
    assert safe_join(base, "does-not-exist.json") == (base / "does-not-exist.json").resolve()


def test_error_is_a_value_error(base: Path) -> None:
    with pytest.raises(ValueError):
        safe_join(base, "../x")
