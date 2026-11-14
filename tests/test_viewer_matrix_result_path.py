"""Containment tests for the viewer matrix result path helper (#155).

``_matrix_result_path`` must only ever hand out files inside
``MATRIX_RESULTS_DIR``. All current callers pass hard-coded names, so these
tests guard the helper for future callers that might take user input.
Every rejected form must answer 400, and no accepted form may resolve outside
the results directory (symlinks included).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import viewer.main as viewer_app_module  # noqa: E402


@pytest.fixture
def results_dir(monkeypatch, tmp_path) -> Path:
    """Point the matrix results dir at an isolated tmp tree with a sibling secret."""
    root = tmp_path / "matrix_runner"
    root.mkdir()
    monkeypatch.setattr(viewer_app_module, "MATRIX_RESULTS_DIR", root)
    (tmp_path / "secret.csv").write_text("secret\n", encoding="utf-8")
    (root / "matrix_matrix.csv").write_text("ok\n", encoding="utf-8")
    return root


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:  # Windows without symlink privilege
        pytest.skip(f"symlinks unavailable on this platform: {exc}")


def _assert_rejected(filename: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        viewer_app_module._matrix_result_path(filename)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "Invalid matrix filename"


def test_plain_filename_resolves_inside_results_dir(results_dir):
    path = viewer_app_module._matrix_result_path("matrix_matrix.csv")

    assert path.is_relative_to(results_dir.resolve())
    assert path.name == "matrix_matrix.csv"
    assert path.read_text(encoding="utf-8") == "ok\n"


def test_surrounding_whitespace_is_stripped(results_dir):
    path = viewer_app_module._matrix_result_path("  matrix_matrix.csv  ")

    assert path.name == "matrix_matrix.csv"


@pytest.mark.parametrize("filename", ["", "   ", None])
def test_empty_filename_is_rejected(results_dir, filename):
    _assert_rejected(filename)


@pytest.mark.parametrize(
    "filename",
    [
        "..",
        "../secret.csv",
        "../../secret.csv",
        "sub/../../secret.csv",
        "./../secret.csv",
    ],
)
def test_traversal_is_rejected(results_dir, filename):
    _assert_rejected(filename)


def test_absolute_posix_path_is_rejected(results_dir, tmp_path):
    _assert_rejected(str(tmp_path / "secret.csv"))
    _assert_rejected("/etc/passwd")


@pytest.mark.parametrize(
    "filename",
    [
        "..\\secret.csv",
        "sub\\matrix_matrix.csv",
        "sub\\..\\..\\secret.csv",
        "\\\\server\\share\\matrix_matrix.csv",
        "\\matrix_matrix.csv",
        "C:\\secret.csv",
        "C:/secret.csv",
        "C:secret.csv",
        "matrix_matrix.csv:stream",
    ],
)
def test_windows_style_forms_are_rejected(results_dir, filename):
    _assert_rejected(filename)


def test_symlink_to_file_outside_results_dir_is_rejected(results_dir, tmp_path):
    _symlink_or_skip(results_dir / "escape.csv", tmp_path / "secret.csv")

    _assert_rejected("escape.csv")


def test_symlinked_directory_outside_results_dir_is_rejected(results_dir, tmp_path):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.csv").write_text("secret\n", encoding="utf-8")
    _symlink_or_skip(results_dir / "linked", outside_dir, target_is_directory=True)

    _assert_rejected("linked/secret.csv")


def test_symlink_that_stays_inside_results_dir_is_accepted(results_dir):
    _symlink_or_skip(results_dir / "alias.csv", results_dir / "matrix_matrix.csv")

    path = viewer_app_module._matrix_result_path("alias.csv")

    assert path.is_relative_to(results_dir.resolve())


def test_matrix_csv_endpoint_does_not_serve_symlink_escape(results_dir, tmp_path):
    # Fixed-name route: a symlink planted under the expected name must not leak
    # a file from outside the results directory.
    target = results_dir / "matrix_matrix.csv"
    target.unlink()
    _symlink_or_skip(target, tmp_path / "secret.csv")

    response = TestClient(viewer_app_module.app).get("/api/backtests/matrix/matrix.csv")

    assert response.status_code == 400
    assert "secret" not in response.text
