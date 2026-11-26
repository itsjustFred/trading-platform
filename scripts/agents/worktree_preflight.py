#!/usr/bin/env python3
"""Preflight check for agent worktrees in this repo.

Agents run this first in any worktree::

    python scripts/agents/worktree_preflight.py

It fails (exit 1, with the fix in the message) when:

- it is run in the main checkout - the tree the live daemon runs from
  (detected via ``git rev-parse --git-dir`` vs ``--git-common-dir``, the same
  test that distinguishes a linked worktree);
- the branch base is more than ``--max-behind`` commits (default 20) behind
  ``origin/main`` - a stale worktree produces reviews and tests against code
  that no longer exists;
- the shared venv is missing packages the lockfile's extras require (what CI
  installs). The check is ``uv export`` + ``uv pip install --dry-run``, both
  read-only: it never modifies the venv. The venv checked is the first of
  ``$VIRTUAL_ENV``, the interpreter running this script, ``<worktree>/.venv``
  or the main worktree's ``.venv`` that exists.

Prints ``OK`` plus a short summary and exits 0 otherwise.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

_GIT_TIMEOUT = 30
_FETCH_TIMEOUT = 60
_UV_TIMEOUT = 180


def _git(args: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=timeout,
        check=False,
        env=env,
    )


def _git_out(args: list[str], cwd: Path) -> str | None:
    out = _git(args, cwd)
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _fail(message: str) -> NoReturn:
    print(f"PREFLIGHT FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def _find_repo_root(start: Path) -> Path:
    root = _git_out(["rev-parse", "--show-toplevel"], start)
    if root is None:
        _fail(f"{start} is not inside a git worktree; run this from your worktree root")
    return Path(root)


def _check_not_main_checkout(root: Path) -> None:
    git_dir = _git_out(["rev-parse", "--git-dir"], root)
    common_dir = _git_out(["rev-parse", "--git-common-dir"], root)
    if git_dir is None or common_dir is None:
        _fail("cannot determine git dirs; is this a healthy worktree?")
    same = Path(git_dir).resolve() == Path(common_dir).resolve()
    if same:
        _fail(
            "this is the main checkout the live daemon runs from. Create a "
            "worktree instead: `git worktree add ../wt-<name> -b <branch> origin/main`"
        )


def _check_freshness(root: Path, max_behind: int) -> tuple[str, int]:
    # Best effort: a stale origin/main ref is better than no check at all, and
    # fetch is read-only for the worktree.
    _git(["fetch", "--quiet", "origin", "main"], root, timeout=_FETCH_TIMEOUT)
    upstream = "origin/main"
    if _git_out(["rev-parse", "--verify", "--quiet", upstream], root) is None:
        upstream = "FETCH_HEAD"
        if _git_out(["rev-parse", "--verify", "--quiet", upstream], root) is None:
            _fail(
                "cannot resolve origin/main (or FETCH_HEAD); run "
                "`git fetch origin main` and retry"
            )
    count = _git_out(["rev-list", "--count", f"HEAD..{upstream}"], root)
    if count is None:
        _fail(f"cannot count commits behind {upstream}; is the branch healthy?")
    behind = int(count)
    branch = _git_out(["rev-parse", "--abbrev-ref", "HEAD"], root) or "?"
    if behind > max_behind:
        _fail(
            f"branch `{branch}` is {behind} commits behind {upstream} "
            f"(max {max_behind}). Rebase onto a fresh base: "
            f"`git fetch origin main && git rebase origin/main` (or ask for a new worktree)"
        )
    return branch, behind


def _venv_candidates(root: Path) -> list[Path]:
    candidates: list[Path] = []
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        candidates.append(Path(venv) / "bin" / "python")
    exe = Path(sys.executable)
    if (exe.parent.parent / "pyvenv.cfg").exists():
        candidates.append(exe)
    candidates.append(root / ".venv" / "bin" / "python")
    # The shared venv lives in the main worktree.
    listing = _git_out(["worktree", "list", "--porcelain"], root)
    if listing:
        for line in listing.splitlines():
            if line.startswith("worktree "):
                main_tree = Path(line[len("worktree ") :])
                if main_tree.resolve() != root.resolve():
                    candidates.append(main_tree / ".venv" / "bin" / "python")
                break
    seen: list[Path] = []
    for cand in candidates:
        if cand not in seen:
            seen.append(cand)
    return seen


def _check_venv(root: Path) -> str | None:
    """Return a description of the venv checked, or None when skipped."""
    if not (root / "uv.lock").exists():
        print("note: no uv.lock here; skipping venv check")
        return None
    uv = shutil.which("uv")
    if uv is None:
        print("note: `uv` not on PATH; skipping venv check")
        return None
    python = next((c for c in _venv_candidates(root) if c.exists()), None)
    if python is None:
        print("note: no virtualenv found; skipping venv check")
        return None

    # What CI installs: the lockfile with every extra, minus the project
    # itself (its editable install legitimately points at the main checkout).
    export = subprocess.run(
        [
            uv,
            "export",
            "--frozen",
            "--all-extras",
            "--no-hashes",
            "--no-emit-project",
            "--no-emit-workspace",
        ],
        capture_output=True,
        text=True,
        cwd=str(root),
        timeout=_UV_TIMEOUT,
        check=False,
    )
    if export.returncode != 0:
        print(f"note: `uv export` failed ({export.stderr.strip()[:200]}); skipping venv check")
        return None
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix="tp-preflight-reqs-", delete=False, encoding="utf-8"
    ) as reqs_file:
        reqs_file.write(export.stdout)
        reqs_path = reqs_file.name
    try:
        dry = subprocess.run(
            [uv, "pip", "install", "--dry-run", "--python", str(python), "-r", reqs_path],
            capture_output=True,
            text=True,
            timeout=_UV_TIMEOUT,
            check=False,
        )
    finally:
        Path(reqs_path).unlink(missing_ok=True)
    output = (dry.stdout + dry.stderr).strip()
    if "Would make no changes" in output:
        return str(python)
    if "Would install" in output or "Would download" in output or "Would uninstall" in output:
        _fail(
            f"the shared venv at {python} is out of sync with uv.lock "
            f"(--all-extras):\n{output}\nAsk the operator to run "
            "`uv sync --frozen --all-extras` in the main checkout - do NOT "
            "run `uv sync` yourself (it would hit the daemon's env)."
        )
    # Resolution errors (offline registry, yanked pin) are inconclusive, not
    # proof of a broken env: warn and let the git checks stand.
    print(f"note: could not verify the venv ({output[:200] or 'uv error'}); continuing")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--max-behind",
        type=int,
        default=20,
        help="fail when HEAD is more than N commits behind origin/main (default 20)",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="directory to check (default: current directory)",
    )
    args = parser.parse_args()

    start = Path(args.path).resolve()
    root = _find_repo_root(start)
    _check_not_main_checkout(root)
    branch, behind = _check_freshness(root, args.max_behind)
    venv = _check_venv(root)

    print(
        f"OK: worktree {root} on `{branch}`, {behind} commit(s) behind "
        f"origin/main"
        + (f", venv {venv} in sync" if venv else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
