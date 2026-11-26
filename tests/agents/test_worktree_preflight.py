"""Tests for ``scripts/agents/worktree_preflight.py``.

Uses real temp git repos: a "main" checkout, a bare ``origin``, and a linked
worktree - the same layout the script distinguishes via
``git rev-parse --git-dir`` vs ``--git-common-dir``. The venv check is only
exercised for its graceful-skip paths here (a temp repo has no ``uv.lock`` /
``pyproject.toml``); the real sync check runs against the actual repo.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO_ROOT / "scripts" / "agents" / "worktree_preflight.py"


def _env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "TMPDIR": str(home),
    }


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            *args,
        ],
        check=True,
        capture_output=True,
        env=_env(repo),
    )


def _commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add {name}")


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Path]:
    """main checkout + bare origin + linked worktree on `feature`."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-b", "main")
    _commit(main, "f.txt", "one\n")

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        check=True,
        capture_output=True,
        env=_env(tmp_path),
    )
    _git(main, "remote", "add", "origin", str(origin))
    _git(main, "push", "origin", "main")
    _git(main, "fetch", "origin")

    wt = tmp_path / "wt"
    _git(main, "worktree", "add", str(wt), "-b", "feature")
    return {"main": main, "wt": wt, "tmp": tmp_path}


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PREFLIGHT), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=_env(cwd),
        timeout=120,
    )


def test_fails_in_the_main_checkout(repo: dict[str, Path]) -> None:
    result = _run(cwd=repo["main"])
    assert result.returncode == 1
    assert "main checkout" in result.stderr
    assert "worktree add" in result.stderr


def test_passes_in_a_fresh_worktree(repo: dict[str, Path]) -> None:
    result = _run(cwd=repo["wt"])
    assert result.returncode == 0, result.stderr
    assert "OK:" in result.stdout
    assert "0 commit(s) behind" in result.stdout


def test_fails_outside_a_git_repo(repo: dict[str, Path]) -> None:
    plain = repo["tmp"] / "plain"
    plain.mkdir()
    result = _run(cwd=plain)
    assert result.returncode == 1
    assert "not inside a git worktree" in result.stderr


def test_fails_when_too_far_behind(repo: dict[str, Path]) -> None:
    for i in range(25):
        _commit(repo["main"], f"c{i}.txt", f"{i}\n")
    _git(repo["main"], "push", "origin", "main")
    result = _run(cwd=repo["wt"])
    assert result.returncode == 1
    assert "25 commits behind" in result.stderr
    assert "rebase" in result.stderr


def test_max_behind_is_configurable(repo: dict[str, Path]) -> None:
    for i in range(3):
        _commit(repo["main"], f"d{i}.txt", f"{i}\n")
    _git(repo["main"], "push", "origin", "main")
    assert _run("--max-behind", "2", cwd=repo["wt"]).returncode == 1
    assert _run("--max-behind", "3", cwd=repo["wt"]).returncode == 0


def test_venv_check_skips_without_uv_lock(repo: dict[str, Path]) -> None:
    result = _run(cwd=repo["wt"])
    assert result.returncode == 0
    assert "skipping venv check" in result.stdout


def test_venv_check_degrades_without_pyproject(repo: dict[str, Path]) -> None:
    """A uv.lock but no pyproject makes `uv export` fail: warn, don't fail."""
    (repo["wt"] / "uv.lock").write_text("", encoding="utf-8")
    result = _run(cwd=repo["wt"])
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    assert "uv export" in out or "skipping venv check" in out
