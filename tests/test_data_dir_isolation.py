"""The suite must never touch the repo's ``data/`` directory.

In the main checkout that directory is the live platform's database and market data, so a test that
resolves ``DATA_DIR`` to it (or hardcodes ``data/``) silently mutates production state. ``conftest``
points DATA_DIR at a per-process temp dir and, at session end, fails the run if the repo ``data/``
changed. These tests pin both halves.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import tests.conftest as suite_conftest
from tests.conftest import REPO_DATA_DIR, diff_snapshots, snapshot_tree


def _is_under(path: Path, root: Path) -> bool:
    return path.resolve() == root or root in path.resolve().parents


def test_env_data_dir_is_not_the_repo_data_dir() -> None:
    for var in ("DATA_DIR", "TRADING_DATA_DIR"):
        resolved = Path(os.environ[var]).resolve()
        assert not _is_under(resolved, REPO_DATA_DIR), f"{var} points into the repo data dir"


def test_platform_constants_resolve_outside_the_repo_data_dir() -> None:
    import shared.constants as constants
    import shared.database as database

    for name, path in (
        ("constants.DATA_DIR", constants.DATA_DIR),
        ("constants.OPTIMIZATIONS_DIR", constants.OPTIMIZATIONS_DIR),
        ("database.DATA_DIR", database.DATA_DIR),
        ("database.DB_PATH", database.DB_PATH),
    ):
        assert not _is_under(Path(path), REPO_DATA_DIR), f"{name} points into the repo data dir"


def test_env_data_dir_is_the_conftest_session_dir() -> None:
    """Forced, not ``setdefault``: a production DATA_DIR exported in the shell must not survive."""
    assert Path(os.environ["DATA_DIR"]).resolve() == suite_conftest.TEST_DATA_DIR


def test_sessionfinish_fails_the_run_when_the_repo_data_dir_changed(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    fake_repo_data = tmp_path / "data"
    fake_repo_data.mkdir()
    monkeypatch.setattr(suite_conftest, "REPO_DATA_DIR", fake_repo_data)
    monkeypatch.setattr(suite_conftest, "_REPO_DATA_BEFORE", snapshot_tree(fake_repo_data))

    class _Config:  # no ``workerinput`` attribute: behaves like the controller
        pass

    class _Session:
        config = _Config()
        exitstatus = pytest.ExitCode.OK

    session = _Session()
    suite_conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == pytest.ExitCode.OK

    (fake_repo_data / "backtests.db").write_text("written by a test")
    suite_conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert "created: backtests.db" in capsys.readouterr().err


def test_snapshot_diff_reports_created_modified_and_deleted(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("a")
    (tmp_path / "change.txt").write_text("a")
    (tmp_path / "gone.txt").write_text("a")
    before = snapshot_tree(tmp_path)

    (tmp_path / "change.txt").write_text("longer")
    (tmp_path / "gone.txt").unlink()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "new.txt").write_text("x")

    assert diff_snapshots(before, snapshot_tree(tmp_path)) == [
        "created: sub",
        "created: sub/new.txt",
        "deleted: gone.txt",
        "modified: change.txt",
    ]


def test_snapshot_of_an_unchanged_or_missing_tree_is_clean(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("a")
    assert diff_snapshots(snapshot_tree(tmp_path), snapshot_tree(tmp_path)) == []
    assert snapshot_tree(tmp_path / "missing") == {}
