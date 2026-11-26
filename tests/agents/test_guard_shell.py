"""Tests for ``scripts/agents/guard_shell.py`` - the Claude Code PreToolUse guard.

The guard is a shell-command parser, so the tests are table-driven: each case
is a command line plus the directory it would run in, and the expectation is
whether the hook blocks it. The checkout fixtures are real temp git repos - a
"main" checkout (``.git`` directory) and a linked worktree (``.git`` file) -
because the main-checkout rule set keys off exactly that distinction.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.agents import guard_shell

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "agents" / "guard_shell.py"


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
        env=_child_env(repo),
    )


def _child_env(home: Path) -> dict[str, str]:
    """Minimal environment for git/python children: no inherited credentials."""
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "TMPDIR": str(home),
    }


@pytest.fixture
def checkouts(tmp_path: Path) -> dict[str, Path]:
    """A main checkout and a linked worktree of the same repo."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-b", "main")
    (main / "README.md").write_text("hello\n", encoding="utf-8")
    (main / "src").mkdir()
    (main / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(main, "add", "README.md", "src/app.py")
    _git(main, "commit", "-m", "init")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", str(wt), "-b", "feature")
    return {"main": main, "wt": wt, "plain": tmp_path / "plain"}


# (command, which checkout, expect_blocked)
# which: "wt" = linked worktree, "main" = main checkout, "plain" = not a repo
CASES: list[tuple[str, str, bool]] = [
    # --- git stash: blocked everywhere -------------------------------------
    ("git stash", "wt", True),
    ("git stash push -m wip", "wt", True),
    ("git stash pop", "wt", True),
    ("git stash list", "wt", True),
    ("git -C /some/dir stash", "wt", True),
    # --- git checkout pathspec restore --------------------------------------
    ("git checkout -- .", "wt", True),
    ("git checkout -- README.md", "wt", True),
    ("git checkout .", "wt", True),
    ("git checkout README.md", "wt", True),  # existing path = worktree restore
    ("git checkout -- '*.py'", "wt", True),
    ("git checkout HEAD -- src/app.py", "wt", True),
    ("git checkout main", "wt", False),  # branch switch is fine in a worktree
    ("git checkout -b newbranch", "wt", False),
    ("git checkout --detach HEAD", "wt", False),
    # --- git restore ---------------------------------------------------------
    ("git restore .", "wt", True),
    ("git restore -- .", "wt", True),
    ("git restore src/", "wt", True),
    ("git restore --worktree README.md", "wt", True),
    ("git restore README.md", "wt", True),
    ("git restore --staged .", "wt", True),
    ("git restore --staged src/", "wt", True),
    ("git restore --staged README.md", "wt", False),
    ("git restore --staged src/app.py", "wt", False),
    ("git restore -S README.md", "wt", False),
    # --- git add -------------------------------------------------------------
    ("git add -A", "wt", True),
    ("git add --all", "wt", True),
    ("git add -u", "wt", True),
    ("git add --update", "wt", True),
    ("git add .", "wt", True),
    ("git add ./", "wt", True),
    ("git add -- .", "wt", True),
    ("git add README.md", "wt", False),
    ("git add src/app.py README.md", "wt", False),
    ("git add -p", "wt", False),
    # --- reset / clean --------------------------------------------------------
    ("git reset --hard", "wt", True),
    ("git reset --hard HEAD~1", "wt", True),
    ("git reset --soft HEAD~1", "wt", False),
    ("git reset HEAD~1", "wt", False),
    ("git clean -f", "wt", True),
    ("git clean -fdx", "wt", True),
    ("git clean --force", "wt", True),
    ("git clean -nd", "wt", False),
    ("git clean -n", "wt", False),
    # --- process kills ---------------------------------------------------------
    ("pkill -f pytest", "wt", True),
    ("pkill pytest", "wt", True),
    ("killall node", "wt", True),
    ("pgrep -af pytest", "wt", False),
    ("kill 1234", "wt", False),
    ("ps aux | grep pytest", "wt", False),
    # --- ruff format ------------------------------------------------------------
    ("ruff format", "wt", True),
    ("ruff format .", "wt", True),
    ("ruff format src/", "wt", True),
    ("ruff format '*.py'", "wt", True),
    ("ruff format README.md", "wt", False),  # exists but not a .py? still a file
    ("ruff check src/app.py", "wt", False),
    ("ruff format --check src/app.py", "wt", False),
    # --- pushes ------------------------------------------------------------------
    ("git push origin main", "wt", True),
    ("git push origin master", "wt", True),
    ("git push origin HEAD:main", "wt", True),
    ("git push origin :main", "wt", True),
    ("git push origin +feature:main", "wt", True),
    ("git push --force origin feature", "wt", True),
    ("git push -f origin feature", "wt", True),
    ("git push --force-with-lease origin feature", "wt", True),
    ("git push origin +feature", "wt", True),
    ("git push origin feature", "wt", False),
    ("git push origin feature:feature", "wt", False),
    ("git push origin v1.0.0", "wt", False),
    ("git push", "wt", False),  # current branch is `feature`, not main
    ("git push", "main", True),  # bare push on `main` pushes main
    ("git push origin", "main", True),  # remote-only push uses current branch
    ("git push origin HEAD", "main", True),
    # --- compound / wrapped commands ----------------------------------------------
    ("cd src && git stash", "wt", True),
    ("git status; git stash", "wt", True),
    ("git status && git add -A", "wt", True),
    ("echo ok | git stash", "wt", True),
    ("(git stash)", "wt", True),
    ("((git stash))", "wt", True),
    ("bash -c 'git stash'", "wt", True),
    ("sh -c \"git add -A\"", "wt", True),
    ("bash -lc 'git reset --hard'", "wt", True),
    ("eval 'git clean -fd'", "wt", True),
    ("sudo git stash", "wt", True),
    ("env FOO=1 git stash", "wt", True),
    ("FOO=1 BAR=2 git stash", "wt", True),
    ("timeout 30 git stash", "wt", True),
    ("nice -n 5 git stash", "wt", True),
    ("nohup git stash", "wt", True),
    ("git stash > /tmp/fix.patch", "wt", True),
    ("git stash 2>&1 | tee log", "wt", True),
    ("git -C src stash", "wt", True),
    ("uv run ruff format", "wt", True),
    ("uvx ruff format .", "wt", True),
    ("python -m pip --version", "wt", False),
    # --- things that must NOT block -------------------------------------------------
    ("echo 'git stash'", "wt", False),
    ("echo \"git stash\"", "wt", False),
    ("grep -rn stash scripts/", "wt", False),
    ("git log --grep=stash --oneline", "wt", False),
    ("git commit -m 'fix: describe stash handling'", "wt", False),
    ("git status", "wt", False),
    ("git diff", "wt", False),
    ("git fetch origin", "wt", False),
    ("ls -la | grep git", "wt", False),
    ("cat > /tmp/note.txt <<'EOF'\nsome text\ngit stash is forbidden\nEOF", "wt", False),
    # --- main checkout only ---------------------------------------------------------
    ("git pull", "main", True),
    ("git checkout main", "main", True),
    ("git checkout -b x", "main", True),
    ("git switch -c x", "main", True),
    ("git commit -m x", "main", True),
    ("git merge feature", "main", True),
    ("git rebase main", "main", True),
    ("git cherry-pick abc123", "main", True),
    ("git revert HEAD", "main", True),
    ("uv sync", "main", True),
    ("uv sync --all-extras", "main", True),
    ("uv pip install foo", "main", True),
    ("uv pip uninstall foo", "main", True),
    ("uv run pytest", "main", True),
    ("uv run --no-sync pytest", "main", False),
    ("uv pip list", "main", False),
    ("pip install foo", "main", True),
    ("pip3 install foo", "main", True),
    ("python -m pip install foo", "main", True),
    ("npm install", "main", True),
    ("npm ci", "main", True),
    ("npm test", "main", False),
    ("ln -s /tmp/x node_modules", "main", True),
    ("ln -sfn target ./node_modules", "main", True),
    ("ln -s target node_modules", "main", True),
    ("ln target hardlink", "main", False),
    ("git fetch origin", "main", False),
    ("git status", "main", False),
    ("git push origin feature", "main", False),
    ("uv pip check", "main", False),
    # --- outside any checkout: only the global rules apply ----------------------
    ("git commit -m x", "plain", False),
    ("git pull", "plain", False),
    ("uv sync", "plain", False),
    ("pip install foo", "plain", False),
    ("git stash", "plain", True),
    ("git add -A", "plain", True),
    # --- main-checkout rules do NOT fire in a worktree -------------------------------
    ("git pull", "wt", False),
    ("git commit -m x", "wt", False),
    ("git merge feature", "wt", False),
    ("git checkout -b x", "wt", False),
    ("uv sync", "wt", False),
    ("uv run pytest", "wt", False),
    ("pip install foo", "wt", False),
    ("npm install", "wt", False),
    ("ln -s /tmp/x node_modules", "wt", False),
    # --- operator override ------------------------------------------------------------
    ("TP_MAIN_CHECKOUT_OK=1 git pull", "main", False),
    ("TP_MAIN_CHECKOUT_OK=1 git commit -m x", "main", False),
    ("TP_MAIN_CHECKOUT_OK=1 uv sync", "main", False),
    ("env TP_MAIN_CHECKOUT_OK=1 git pull", "main", False),
    # ...but global rules still apply under the override
    ("TP_MAIN_CHECKOUT_OK=1 git stash", "main", True),
    ("TP_MAIN_CHECKOUT_OK=1 git add -A", "main", True),
    # --- cwd tracking -------------------------------------------------------------------
    ("cd {main} && git commit -m x", "wt", True),  # placeholder, resolved below
    ("cd src && git commit -m x", "wt", False),
    ("cd /nonexistent-dir-xyz && git commit -m x", "main", True),  # cd fails, stays in main
]


def _resolve_case(command: str, checkouts: dict[str, Path]) -> str:
    return command.replace("{main}", str(checkouts["main"]))


@pytest.mark.parametrize(
    "command,which,expected",
    [(cmd, w, exp) for cmd, w, exp in CASES],
    ids=[f"{w}:{cmd[:48]}" for cmd, w, _ in CASES],
)
def test_guard_command_table(
    command: str, which: str, expected: bool, checkouts: dict[str, Path]
) -> None:
    cwd = checkouts[which]
    cwd.mkdir(exist_ok=True)
    command = _resolve_case(command, checkouts)
    verdict = guard_shell.check_command(command, cwd)
    assert (verdict is not None) == expected, (
        f"command {command!r} in {which}: expected "
        f"{'block' if expected else 'allow'}, got {verdict!r}"
    )
    if expected:
        assert "blocked:" in verdict


# A few cases where the expectation depends on the tracked cwd rather than the
# hook cwd, kept separate for readability.
def test_cd_into_main_checkout_blocks(checkouts: dict[str, Path]) -> None:
    cmd = f"cd {checkouts['main']} && git commit -m x"
    assert guard_shell.check_command(cmd, checkouts["wt"]) is not None


def test_cd_within_worktree_allows_commit(checkouts: dict[str, Path]) -> None:
    assert guard_shell.check_command("cd src && git commit -m x", checkouts["wt"]) is None


def test_git_dash_C_into_main_blocks(checkouts: dict[str, Path]) -> None:
    cmd = f"git -C {checkouts['main']} commit -m x"
    assert guard_shell.check_command(cmd, checkouts["wt"]) is not None


def test_subshell_cd_does_not_leak(checkouts: dict[str, Path]) -> None:
    """A `cd` inside `( )` must not change the cwd of later commands."""
    cmd = f"(cd {checkouts['main']}); git status"
    assert guard_shell.check_command(cmd, checkouts["wt"]) is None


# --- hook contract: stdin JSON -> exit code ------------------------------------


def _run_hook(payload: dict, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=_child_env(cwd),
        timeout=30,
    )


def test_hook_blocks_with_exit_2_and_stderr(checkouts: dict[str, Path]) -> None:
    result = _run_hook(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git stash"},
            "cwd": str(checkouts["wt"]),
        },
        checkouts["wt"],
    )
    assert result.returncode == 2
    assert "blocked:" in result.stderr


def test_hook_allows_with_exit_0(checkouts: dict[str, Path]) -> None:
    result = _run_hook(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "cwd": str(checkouts["wt"]),
        },
        checkouts["wt"],
    )
    assert result.returncode == 0
    assert result.stderr == ""


def test_hook_ignores_non_bash_tools(checkouts: dict[str, Path]) -> None:
    result = _run_hook(
        {"tool_name": "Read", "tool_input": {"command": "git stash"}, "cwd": str(checkouts["wt"])},
        checkouts["wt"],
    )
    assert result.returncode == 0


def test_hook_tolerates_bad_stdin(checkouts: dict[str, Path]) -> None:
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input="not json{{{",
        capture_output=True,
        text=True,
        cwd=str(checkouts["wt"]),
        env=_child_env(checkouts["wt"]),
        timeout=30,
    )
    assert result.returncode == 0
