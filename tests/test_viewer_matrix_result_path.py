"""Containment tests for the viewer matrix result path helper (#155).

``_matrix_result_path`` must only ever hand out files inside
``MATRIX_RESULTS_DIR``, and the file an endpoint serves must be the file that
check approved. All current callers pass hard-coded names, so these tests
guard the helper for future callers that might take user input. Every rejected
form must answer 400, and no accepted form may resolve outside the results
directory (symlinks included), neither when it is checked nor when it is
opened.
"""

from __future__ import annotations

import gc
import os
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
    (tmp_path / "secret.csv").write_bytes(b"secret\n")
    (root / "matrix_matrix.csv").write_bytes(b"ok\n")
    return root


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:  # Windows without symlink privilege
        pytest.skip(f"symlinks unavailable on this platform: {exc}")


def _descriptors_pointing_at(path: Path) -> int | None:
    """Open descriptors that point at ``path``, or None where that cannot be seen.

    The file is named rather than counted, so unrelated descriptors the process
    holds -- a portal, a log handle -- do not decide the answer.
    """
    for listing in (Path("/proc/self/fd"), Path("/dev/fd")):
        if not listing.is_dir():
            continue
        pointing = 0
        for entry in listing.iterdir():
            try:
                target = os.readlink(entry)
            except OSError:  # closed between listing and reading it
                continue
            if Path(target) == path:
                pointing += 1
        return pointing
    return None


def _check_then_swap(monkeypatch, swap, *, after: str = "_matrix_result_path") -> None:
    """Rename the file inside the window between two steps of the request.

    Checking a path, opening it and streaming it are separate steps, so anything
    renamed between two of them would have been followed. This reproduces that
    window at ``after``.
    """
    original = getattr(viewer_app_module, after)

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        swap()
        return result

    monkeypatch.setattr(viewer_app_module, after, wrapper)


def _assert_rejected(filename: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        viewer_app_module._matrix_result_path(filename)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "Invalid matrix filename"


def test_plain_filename_resolves_inside_results_dir(results_dir):
    path = viewer_app_module._matrix_result_path("matrix_matrix.csv")

    assert path.is_relative_to(results_dir.resolve())
    assert path.name == "matrix_matrix.csv"
    # The returned path is the resolved one, so a caller opening it cannot be
    # handed a path that still has to be resolved (and could resolve outside).
    assert path == (results_dir / "matrix_matrix.csv").resolve()
    assert path == path.resolve()
    assert path.read_text(encoding="utf-8") == "ok\n"


def test_surrounding_whitespace_is_stripped(results_dir):
    path = viewer_app_module._matrix_result_path("  matrix_matrix.csv  ")

    assert path.name == "matrix_matrix.csv"


def test_returned_path_is_resolved_through_a_symlinked_results_dir(
    results_dir, tmp_path, monkeypatch
):
    real = tmp_path / "real_results"
    real.mkdir()
    (real / "matrix_matrix.csv").write_bytes(b"ok\n")
    linked = tmp_path / "linked_results"
    _symlink_or_skip(linked, real, target_is_directory=True)
    monkeypatch.setattr(viewer_app_module, "MATRIX_RESULTS_DIR", linked)

    path = viewer_app_module._matrix_result_path("matrix_matrix.csv")

    assert path == real / "matrix_matrix.csv"
    assert path == path.resolve()


def test_matrix_csv_endpoint_serves_the_contained_file(results_dir):
    response = TestClient(viewer_app_module.app).get("/api/backtests/matrix/matrix.csv")

    assert response.status_code == 200
    assert response.text == "ok\n"
    assert response.headers["content-disposition"] == 'attachment; filename="matrix_matrix.csv"'
    # Identity encoding keeps the middleware from replacing the headers it set.
    identity = TestClient(viewer_app_module.app).get(
        "/api/backtests/matrix/matrix.csv", headers={"accept-encoding": "identity"}
    )
    assert identity.headers["content-length"] == str(len("ok\n"))


def test_matrix_csv_is_served_byte_for_byte(results_dir):
    """CRLF line endings and a 0x1A byte survive unchanged (no text-mode read on Windows)."""
    payload = b"a,b\r\n1,2\r\n\x1a3,4\r\n"
    (results_dir / "matrix_matrix.csv").write_bytes(payload)

    response = TestClient(viewer_app_module.app).get(
        "/api/backtests/matrix/matrix.csv", headers={"accept-encoding": "identity"}
    )

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-length"] == str(len(payload))


def test_daily_equity_csv_endpoint_serves_the_contained_file(results_dir):
    (results_dir / "matrix_daily_equity.csv").write_bytes(b"equity\n")

    response = TestClient(viewer_app_module.app).get("/api/backtests/matrix/daily-equity.csv")

    assert response.status_code == 200
    assert response.text == "equity\n"
    assert response.headers["content-disposition"] == (
        'attachment; filename="matrix_daily_equity.csv"'
    )


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


@pytest.mark.parametrize("filename", [".", " . ", "./", "./."])
def test_directory_itself_is_rejected(results_dir, filename):
    # The results directory is inside the results directory, so containment alone
    # would hand it out; a caller then serving it would act on a directory.
    _assert_rejected(filename)


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
    (outside_dir / "secret.csv").write_bytes(b"secret\n")
    _symlink_or_skip(results_dir / "linked", outside_dir, target_is_directory=True)

    _assert_rejected("linked/secret.csv")


def test_symlink_that_stays_inside_results_dir_is_accepted(results_dir):
    _symlink_or_skip(results_dir / "alias.csv", results_dir / "matrix_matrix.csv")

    path = viewer_app_module._matrix_result_path("alias.csv")

    assert path.is_relative_to(results_dir.resolve())


def test_directory_inside_results_dir_is_not_served(results_dir):
    # Containment accepts a name that resolves to a directory inside the results
    # directory, so the endpoint has to refuse it itself: a file response built
    # for a directory fails when it opens instead of answering the caller.
    (results_dir / "matrix_matrix.csv").unlink()
    (results_dir / "matrix_matrix.csv").mkdir()

    response = TestClient(viewer_app_module.app, raise_server_exceptions=False).get(
        "/api/backtests/matrix/matrix.csv"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Matrix CSV not found"


def test_fifo_under_the_name_answers_instead_of_stalling(results_dir):
    # A special file under the expected name is refused, and refused now: opening
    # it without O_NONBLOCK would block the request, and with it the event loop
    # every other request shares.
    expected_name = results_dir / "matrix_matrix.csv"
    expected_name.unlink()
    try:
        os.mkfifo(expected_name)
    except (AttributeError, OSError, NotImplementedError) as exc:  # POSIX only
        pytest.skip(f"FIFOs are unavailable on this platform: {exc}")

    response = TestClient(viewer_app_module.app, raise_server_exceptions=False).get(
        "/api/backtests/matrix/matrix.csv"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Matrix CSV not found"


def test_matrix_csv_endpoint_does_not_serve_symlink_escape(results_dir, tmp_path):
    # Fixed-name route: a symlink planted under the expected name must not leak
    # a file from outside the results directory.
    target = results_dir / "matrix_matrix.csv"
    target.unlink()
    _symlink_or_skip(target, tmp_path / "secret.csv")

    response = TestClient(viewer_app_module.app).get("/api/backtests/matrix/matrix.csv")

    assert response.status_code == 400
    assert "secret" not in response.text


def test_symlink_swapped_in_after_the_check_is_not_served(results_dir, tmp_path, monkeypatch):
    # Containment is checked on one walk and the file is opened on another, so a
    # final component replaced by a symlink between the two must be refused
    # rather than followed: it points outside the results directory now.
    expected_name = results_dir / "matrix_matrix.csv"

    def swap() -> None:
        expected_name.unlink()
        expected_name.symlink_to(tmp_path / "secret.csv")

    _check_then_swap(monkeypatch, swap)

    response = TestClient(viewer_app_module.app, raise_server_exceptions=False).get(
        "/api/backtests/matrix/matrix.csv"
    )

    assert response.status_code == 400
    assert "secret" not in response.text


def test_symlink_swapped_in_after_the_check_is_refused_even_inside_results_dir(
    results_dir, monkeypatch
):
    # `O_NOFOLLOW` refuses the open itself wherever the platform provides it, so
    # a name that changed under the check is refused even when it now points at
    # a file that is still inside the results directory. Where the platform has
    # no `O_NOFOLLOW`, the containment re-check is what answers, and it lets an
    # inside target through -- so this pins the stricter answer only there.
    if not getattr(os, "O_NOFOLLOW", 0):
        pytest.skip("O_NOFOLLOW is not available on this platform")
    inside = results_dir / "other.csv"
    inside.write_bytes(b"inside\n")

    def swap() -> None:
        (results_dir / "matrix_matrix.csv").unlink()
        _symlink_or_skip(results_dir / "matrix_matrix.csv", inside)

    _check_then_swap(monkeypatch, swap)

    response = TestClient(viewer_app_module.app, raise_server_exceptions=False).get(
        "/api/backtests/matrix/matrix.csv"
    )

    assert response.status_code == 400


def test_served_bytes_come_from_the_file_that_was_checked(results_dir, monkeypatch):
    # The promise of the checked descriptor: the response streams what the check
    # opened. A replacement published under the same name between the check and
    # the response's own open must not reach the client -- the PR's central claim,
    # and the one a re-open by path breaks.
    expected_name = results_dir / "matrix_matrix.csv"
    expected_name.write_bytes(b"ok\n")

    def swap() -> None:
        staged = results_dir / "matrix_matrix.csv.staged"
        staged.write_bytes(b"secret\n")
        os.replace(staged, expected_name)  # a new inode under the checked name

    _check_then_swap(monkeypatch, swap, after="_checked_matrix_result")

    response = TestClient(viewer_app_module.app, raise_server_exceptions=False).get(
        "/api/backtests/matrix/matrix.csv"
    )

    assert response.status_code == 200
    assert response.text == "ok\n"


def test_refusal_does_not_leak_the_descriptor_it_opened(results_dir):
    # A refused request must not leave the descriptor behind: the viewer serves
    # these endpoints for as long as it runs, and 400/404 are answerable at will.
    expected_name = results_dir / "matrix_matrix.csv"
    expected_name.unlink()
    expected_name.mkdir()

    response = TestClient(viewer_app_module.app, raise_server_exceptions=False).get(
        "/api/backtests/matrix/matrix.csv"
    )
    gc.collect()

    leaked = _descriptors_pointing_at(expected_name)
    if leaked is None:
        pytest.skip("open descriptors cannot be listed on this platform")
    assert response.status_code == 404
    assert leaked == 0


def test_directory_component_swapped_in_after_the_check_is_not_served(
    results_dir, tmp_path, monkeypatch
):
    # A swapped *directory* component is the same hole one level up: the checked
    # path is inside the results directory, but by the time it is opened that
    # component points elsewhere, so the descriptor has to be checked again.
    inside = results_dir / "sub"
    inside.mkdir()
    (inside / "matrix_matrix.csv").write_bytes(b"inside\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "matrix_matrix.csv").write_bytes(b"secret\n")

    def swap() -> None:
        (inside / "matrix_matrix.csv").unlink()
        inside.rmdir()
        _symlink_or_skip(inside, outside, target_is_directory=True)

    _check_then_swap(monkeypatch, swap)

    with pytest.raises(HTTPException) as excinfo:
        viewer_app_module._checked_matrix_result(
            "sub/matrix_matrix.csv", not_found_detail="Matrix CSV not found"
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "Invalid matrix filename"
