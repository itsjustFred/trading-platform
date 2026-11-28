"""Table tests for :func:`shared.paths.safe_join` (proposal G26).

The table is the contract: every row is an input a viewer route or a script can
receive, and the expected outcome. ``..`` is refused even when it would resolve
back inside the base, because the written form is what a caller reasoned about;
the escape rows pin the cases that used to need a hand-written check each.
"""

from __future__ import annotations

import os
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
        unquote("%2e%2e/x"),
    ],
)
def test_escape_patterns_are_refused(base: Path, raw: str) -> None:
    with pytest.raises(PathContainmentError):
        safe_join(base, raw)


def test_windows_style_dotdot_is_a_filename_on_posix(base: Path) -> None:
    """A backslash is an ordinary character on POSIX, a separator on Windows.

    On POSIX ``..\\x`` is one file name that cannot leave ``base``, so it is
    accepted as such; on Windows the same string is ``..`` plus ``x`` and the
    ``..`` rule refuses it. Either way the parts decide, not the string.
    """
    if os.name == "nt":
        with pytest.raises(PathContainmentError):
            safe_join(base, "..\\x")
    else:
        assert safe_join(base, "..\\x") == (base / "..\\x").resolve()
        # A backslash-separated subdirectory is a single file name on POSIX.
        assert safe_join(base, "sub\\file.csv") == (base / "sub\\file.csv").resolve()


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


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:  # Windows without symlink privilege
        pytest.skip(f"symlinks unavailable on this platform: {exc}")


def test_symlink_pointing_outside_the_base_is_refused(base: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.csv"
    outside.write_text("secret", encoding="utf-8")
    _symlink_or_skip(base / "escape.csv", outside)
    _symlink_or_skip(base / "escape-dir", tmp_path, target_is_directory=True)

    with pytest.raises(PathContainmentError):
        safe_join(base, "escape.csv")
    with pytest.raises(PathContainmentError):
        safe_join(base, "escape-dir/secret.csv")


def test_symlink_staying_inside_the_base_is_accepted(base: Path) -> None:
    _symlink_or_skip(base / "alias.csv", base / "sub" / "file.csv")

    assert safe_join(base, "alias.csv") == (base / "sub" / "file.csv").resolve()


def test_refuse_symlinks_rejects_any_symlink_below_the_base(base: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.csv"
    outside.write_text("secret", encoding="utf-8")
    _symlink_or_skip(base / "alias.csv", base / "sub" / "file.csv")
    _symlink_or_skip(base / "escape.csv", outside)

    with pytest.raises(PathContainmentError):
        safe_join(base, "alias.csv", refuse_symlinks=True)
    with pytest.raises(PathContainmentError):
        safe_join(base, "escape.csv", refuse_symlinks=True)
    # A plain file still passes.
    assert safe_join(base, "sub/file.csv", refuse_symlinks=True) == (
        base / "sub" / "file.csv"
    ).resolve()


def test_refuse_symlinks_accepts_a_symlinked_base(tmp_path: Path) -> None:
    """A symlinked base (and its ancestors) is a deployment shape, not an escape.

    Production reaches the data directory through a symlink (``~/data`` ->
    another mount), so refusing the base would refuse every normal id under it.
    """
    real = tmp_path / "real"
    real.mkdir()
    (real / "file.csv").write_text("x", encoding="utf-8")
    linked = tmp_path / "linked"
    _symlink_or_skip(linked, real, target_is_directory=True)

    assert safe_join(linked, "file.csv", refuse_symlinks=True) == (real / "file.csv").resolve()


def test_refuse_symlinks_rejects_a_symlinked_directory_below_the_base(
    base: Path, tmp_path: Path
) -> None:
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "file.csv").write_text("secret", encoding="utf-8")
    _symlink_or_skip(base / "linked-dir", outside_dir, target_is_directory=True)
    _symlink_or_skip(base / "linked-inside", base / "sub", target_is_directory=True)

    with pytest.raises(PathContainmentError):
        safe_join(base, "linked-dir/file.csv", refuse_symlinks=True)
    # Even a link that stays inside the base is refused: the caller writes
    # through this path and a swap must not redirect it.
    with pytest.raises(PathContainmentError):
        safe_join(base, "linked-inside/file.csv", refuse_symlinks=True)


def test_refuse_symlinks_checks_an_absolute_path_below_the_base(
    base: Path, tmp_path: Path
) -> None:
    """``allow_absolute`` + ``refuse_symlinks`` must inspect the real components.

    The walk is lexical and has to map an absolute candidate onto the base
    first; walking the candidate's anchor-relative parts from the base would
    check a path that does not exist and miss a planted symlink.
    """
    outside = tmp_path / "secret.csv"
    outside.write_text("secret", encoding="utf-8")
    _symlink_or_skip(base / "escape.csv", outside)

    with pytest.raises(PathContainmentError):
        safe_join(base, base / "escape.csv", allow_absolute=True, refuse_symlinks=True)
    assert safe_join(
        base, base / "sub" / "file.csv", allow_absolute=True, refuse_symlinks=True
    ) == (base / "sub" / "file.csv").resolve()


def test_refuse_symlinks_maps_an_absolute_path_under_a_symlinked_base(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    (real / "sub" / "file.csv").write_text("x", encoding="utf-8")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    _symlink_or_skip(real / "linked-dir", outside_dir, target_is_directory=True)
    linked = tmp_path / "linked"
    _symlink_or_skip(linked, real, target_is_directory=True)

    # Spelled through the resolved base and through the base as given.
    for candidate in (real / "sub" / "file.csv", linked / "sub" / "file.csv"):
        assert safe_join(
            linked, candidate, allow_absolute=True, refuse_symlinks=True
        ) == (real / "sub" / "file.csv").resolve()

    # And a symlink planted below the base is still caught for an absolute path.
    with pytest.raises(PathContainmentError):
        safe_join(
            linked,
            real / "linked-dir" / "file.csv",
            allow_absolute=True,
            refuse_symlinks=True,
        )


def test_a_symlinked_base_compares_against_its_resolved_target(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    (real / "sub" / "file.csv").write_text("x", encoding="utf-8")
    linked = tmp_path / "linked"
    _symlink_or_skip(linked, real, target_is_directory=True)

    assert safe_join(linked, "sub/file.csv") == (real / "sub" / "file.csv").resolve()


def test_missing_leaf_inside_the_base_is_returned(base: Path) -> None:
    assert safe_join(base, "does-not-exist.json") == (base / "does-not-exist.json").resolve()


def test_error_is_a_value_error(base: Path) -> None:
    with pytest.raises(ValueError):
        safe_join(base, "../x")
