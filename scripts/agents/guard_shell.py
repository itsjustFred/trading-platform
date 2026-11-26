#!/usr/bin/env python3
"""Claude Code PreToolUse guard for shell commands in this repo.

Registered for the ``Bash`` tool in ``.claude/settings.json``. Reads the hook
JSON on stdin (``tool_name``, ``tool_input.command``, ``cwd``) and exits 2 with
a message on stderr to block the call; anything else exits 0.

Two rule sets:

- everywhere: commands that have repeatedly destroyed agent work or swept
  unrelated changes into a diff (``git stash``, ``git checkout --``,
  ``git restore`` of trees, ``git add -A``/``.``, ``git reset --hard``,
  ``git clean -f``, ``pkill``/``killall``, ``ruff format`` without explicit
  files, force pushes and pushes to ``main``/``master``);
- in the main checkout only (the tree the live daemon runs from, detected the
  same way ``git rev-parse --git-dir`` vs ``--git-common-dir`` distinguishes a
  linked worktree: a worktree has a ``.git`` *file*, the main checkout a
  ``.git`` *directory*): history- and environment-mutating commands
  (``git pull``/``checkout``/``switch``/``commit``/``merge``/``rebase`` and
  friends, ``uv sync``/``uv run``/``uv pip`` mutations, ``uv venv``,
  ``pip install``, ``npm install``/``ci``, ``node_modules`` symlinks). The
  human/operator override is the env prefix ``TP_MAIN_CHECKOUT_OK=1``.

The parser is deliberately approximate: it splits on shell control operators,
tracks ``cd``/``pushd`` and subshell parentheses, unwraps common prefixes
(``env``, ``sudo``, ``timeout``, ``uv run``, ``python -m``) and recurses into
``sh -c '...'``/``bash -c '...'``/``eval``. It errs toward evaluating more of
a command, never less - but only clear matches are blocked.

Stdlib only, Python 3.12+, no repo imports: the hook must stay fast and must
not depend on the checkout it is guarding.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath

_MAIN_OK_MARKER = "TP_MAIN_CHECKOUT_OK=1"
_MAX_DEPTH = 6
_MAX_SEGMENTS = 400

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_GLOB_CHARS = frozenset("*?[]")
_CTRL_CHARS = frozenset("()&|;")
_REDIRECT_CHARS = frozenset("<>&")

_HEREDOC_RE = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _basename(token: str) -> str:
    name = PurePosixPath(token).name
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _strip_heredocs(command: str) -> str:
    """Remove ``<<``/``<<-`` heredoc bodies so their text is not parsed as commands.

    Line-oriented and approximate: a ``<<word`` inside quotes still counts as
    a heredoc opener (rare, and the safe direction is to drop the body rather
    than execute-check it).
    """
    lines = command.splitlines(keepends=True)
    out: list[str] = []
    pending: list[str] = []
    for line in lines:
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        out.append(line)
        pending.extend(match.group(2) for match in _HEREDOC_RE.finditer(line))
    return "".join(out)


def _tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(_strip_heredocs(command), posix=True, punctuation_chars="();<>|&")
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        # Unbalanced quotes: fall back to a naive split so obvious matches
        # are still caught.
        return _strip_heredocs(command).split()


def _op_kind(tok: str) -> str | None:
    """'ctrl' for control-operator runs, 'redir' for redirections, else None."""
    if tok and all(ch in _CTRL_CHARS for ch in tok):
        return "ctrl"
    if tok and any(ch in "<>" for ch in tok) and all(
        ch in _REDIRECT_CHARS or ch.isdigit() for ch in tok
    ):
        return "redir"
    return None


def _is_main_checkout(cwd: Path) -> bool:
    """True when ``cwd`` sits in a checkout whose ``.git`` is a directory.

    Equivalent to ``git rev-parse --git-dir == --git-common-dir`` for the
    layouts this repo uses: a linked worktree (``git worktree add``) and a
    submodule carry a ``.git`` *file* pointing at the real gitdir, while the
    main checkout carries a ``.git`` directory. Anything without a ``.git``
    entry is not a checkout and gets the global rules only.
    """
    for directory in (cwd, *cwd.parents):
        dotgit = directory / ".git"
        try:
            if dotgit.is_file():
                return False
            if dotgit.is_dir():
                return True
        except OSError:
            continue
    return False


def _resolve(base: Path, arg: str) -> Path:
    p = Path(arg)
    return p if p.is_absolute() else (base / p)


# ---------------------------------------------------------------------------
# option scanning helpers
# ---------------------------------------------------------------------------


def _skip_value(
    tokens: list[str], i: int, tok: str, value_opts: set[str], value_longs: set[str]
) -> int:
    """Index after ``tok`` at ``i``, consuming a separate value token when needed."""
    if tok.startswith("--"):
        name = tok.split("=", 1)[0]
        if name in value_longs and "=" not in tok:
            return i + 2
        return i + 1
    # short-option cluster: a value-taking option consumes the rest of the
    # cluster or, when it is last, the next token
    cluster = tok[1:]
    for j, ch in enumerate(cluster):
        if f"-{ch}" in value_opts:
            return i + 1 if j < len(cluster) - 1 else i + 2
    return i + 1


def _skip_options(
    argv: list[str], start: int, value_opts: set[str], value_longs: set[str]
) -> int:
    """Index of the first non-option token at/after ``start`` (after ``--`` too)."""
    i = start
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return i + 1
        if tok.startswith("-") and tok != "-":
            i = _skip_value(argv, i, tok, value_opts, value_longs)
            continue
        return i
    return i


def _positional_args(
    argv: list[str], start: int, value_opts: set[str], value_longs: set[str]
) -> list[str]:
    """Non-option arguments after ``start``, honouring ``--`` and value opts."""
    out: list[str] = []
    i = start
    end_opts = False
    while i < len(argv):
        tok = argv[i]
        if not end_opts and tok == "--":
            end_opts = True
            i += 1
            continue
        if not end_opts and tok.startswith("-") and tok != "-":
            i = _skip_value(argv, i, tok, value_opts, value_longs)
            continue
        out.append(tok)
        i += 1
    return out


# ---------------------------------------------------------------------------
# git rules
# ---------------------------------------------------------------------------

_GIT_VALUE_LONGS = {
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--super-prefix",
    "--config-env",
}


def _git_parts(argv: list[str]) -> tuple[str | None, list[str], str | None]:
    """Split ``git [global opts] <subcommand> [args]``.

    Returns (subcommand, args, ``-C`` directory). Unknown global options are
    skipped as flags; the subcommand is the first non-option token.
    """
    args = argv[1:]
    chdir: str | None = None
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "-C" and i + 1 < len(args):
            chdir = args[i + 1]
            i += 2
            continue
        if tok.startswith("-C") and len(tok) > 2 and not tok.startswith("--"):
            chdir = tok[2:]
            i += 1
            continue
        if tok == "-c" and i + 1 < len(args):
            i += 2
            continue
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            if name in _GIT_VALUE_LONGS and "=" not in tok:
                i += 2
            else:
                i += 1
            continue
        if tok.startswith("-") and tok != "-":
            i += 1
            continue
        break
    if i >= len(args):
        return None, [], chdir
    return args[i].strip("`"), args[i + 1 :], chdir


def _looks_like_path_restore(arg: str, cwd: Path) -> bool:
    if arg in (".", "..", "*") or arg.endswith("/") or arg.startswith(":("):
        return True
    if any(ch in _GLOB_CHARS for ch in arg):
        return True
    try:
        return _resolve(cwd, arg).exists()
    except OSError:
        return False


def _check_git_checkout(rest: list[str], cwd: Path) -> str | None:
    """Block ``git checkout`` used as a worktree restore.

    ``git checkout -- <path>`` and ``git checkout <existing-path>`` both
    discard uncommitted changes; branch checkouts leave the pathspec empty.
    """
    pathspecs = _positional_args(
        rest,
        0,
        value_opts={"-b", "-B"},
        value_longs={"--orphan", "--conflict", "--pathspec-from-file"},
    )
    for spec in pathspecs:
        if _looks_like_path_restore(spec, cwd):
            return (
                "blocked: `git checkout` with a pathspec discards uncommitted "
                "work. Save it first with `git diff > /tmp/fix.patch` (or "
                "commit it on your branch), then restore explicitly if you "
                "really mean it."
            )
    return None


def _check_git_restore(rest: list[str], cwd: Path) -> str | None:
    """Allow only ``git restore --staged <explicit files>``.

    Anything that touches the worktree, or names ``.``, ``..``, a glob or a
    directory, can wipe uncommitted fixes in one shot.
    """
    staged = False
    pathspecs: list[str] = []
    i = 0
    end_opts = False
    while i < len(rest):
        tok = rest[i]
        if end_opts:
            pathspecs.append(tok)
            i += 1
            continue
        if tok == "--":
            end_opts = True
            i += 1
            continue
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            if name == "--staged":
                staged = True
                i += 1
                continue
            if name in {"--source", "--pathspec-from-file"} and "=" not in tok:
                i += 2
                continue
            i += 1
            continue
        if tok.startswith("-") and tok != "-":
            cluster = tok[1:]
            j = 0
            while j < len(cluster):
                ch = cluster[j]
                if ch == "S":
                    staged = True
                elif ch == "s":  # --source takes a value
                    if j == len(cluster) - 1:
                        i += 1  # the value is the next token
                    break
                j += 1
            i += 1
            continue
        pathspecs.append(tok)
        i += 1

    def explicit_file(spec: str) -> bool:
        if spec in (".", "..", "*") or spec.endswith("/") or spec.startswith(":("):
            return False
        if any(ch in _GLOB_CHARS for ch in spec):
            return False
        try:
            return not _resolve(cwd, spec).is_dir()
        except OSError:
            return False

    if staged and pathspecs and all(explicit_file(spec) for spec in pathspecs):
        return None
    return (
        "blocked: `git restore` can discard uncommitted work. Only "
        "`git restore --staged <explicit files>` is allowed; for worktree "
        "changes save `git diff > /tmp/fix.patch` first."
    )


def _check_git_add(rest: list[str]) -> str | None:
    for tok in rest:
        if tok == "--":
            break
        if tok in ("-A", "--all", "--update", "-u") or (
            tok.startswith("-")
            and not tok.startswith("--")
            and len(tok) > 1
            and ("A" in tok[1:] or "u" in tok[1:])
        ):
            return (
                "blocked: `git add` with -A/--all/-u sweeps unrelated changes "
                "into the diff. Stage explicit paths: `git add <file>...`."
            )
    pathspecs = _positional_args(
        rest, 0, value_opts=set(), value_longs={"--chmod", "--pathspec-from-file"}
    )
    if any(spec in (".", "./", "..", "*") for spec in pathspecs):
        return (
            "blocked: `git add .` stages everything, including unrelated "
            "changes. Stage explicit paths: `git add <file>...`."
        )
    return None


def _check_git_clean(rest: list[str]) -> str | None:
    for tok in rest:
        if tok == "--":
            break
        if tok == "--force" or (
            tok.startswith("-") and not tok.startswith("--") and "f" in tok[1:]
        ):
            return (
                "blocked: `git clean -f` deletes untracked files "
                "irreversibly. List them with `git clean -nd` and remove "
                "files by explicit path."
            )
    return None


def _check_git_reset(rest: list[str]) -> str | None:
    if "--hard" in rest:
        return (
            "blocked: `git reset --hard` discards uncommitted work. Commit "
            "or `git diff > /tmp/fix.patch` first; `git reset --soft/--mixed` "
            "is fine."
        )
    return None


def _current_branch(cwd: Path) -> str | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


_PUSH_VALUE_LONGS = {
    "--repo",
    "--receive-pack",
    "--exec",
    "--push-option",
}
_PUSH_VALUE_OPTS = {"-o"}


def _check_git_push(rest: list[str], cwd: Path) -> str | None:
    force = False
    positionals: list[str] = []
    i = 0
    end_opts = False
    while i < len(rest):
        tok = rest[i]
        if end_opts:
            positionals.append(tok)
            i += 1
            continue
        if tok == "--":
            end_opts = True
            i += 1
            continue
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            if name.startswith("--force"):
                force = True
            if name in _PUSH_VALUE_LONGS and "=" not in tok:
                i += 2
            else:
                i += 1
            continue
        if tok.startswith("-") and tok != "-":
            cluster = tok[1:]
            if "f" in cluster:
                force = True
            i = _skip_value(rest, i, tok, _PUSH_VALUE_OPTS, _PUSH_VALUE_LONGS)
            continue
        positionals.append(tok)
        i += 1

    if force:
        return (
            "blocked: force pushes are not allowed (including "
            "--force-with-lease and `+refspec`). Ask the maintainer to push "
            "instead."
        )

    refspecs = positionals[1:] if len(positionals) > 1 else []
    ambiguous_head = len(positionals) <= 1  # `git push`, `git push origin`
    for spec in refspecs:
        if spec.startswith("+"):
            return (
                "blocked: a `+` refspec forces the update. Force pushes are "
                "not allowed; ask the maintainer to push instead."
            )
        dst = spec.rsplit(":", 1)[-1] if ":" in spec else spec
        dst = dst.removeprefix("refs/heads/")
        if dst in ("main", "master"):
            return (
                "blocked: pushing to `main`/`master` is not allowed. Push "
                "your branch and open a PR instead."
            )
        if spec == "HEAD":
            ambiguous_head = True

    if ambiguous_head:
        branch = _current_branch(cwd)
        if branch in ("main", "master"):
            return (
                "blocked: this pushes the local `main`/`master` branch. "
                "Push your work branch instead."
            )
    return None


_MAIN_GIT_BLOCKED = {
    "pull": "`git pull` in the main checkout can move the tree the live daemon runs from",
    "checkout": "`git checkout` in the main checkout switches the live daemon's tree",
    "switch": "`git switch` in the main checkout switches the live daemon's tree",
    "commit": "`git commit` in the main checkout commits on the daemon's branch",
    "merge": "`git merge` in the main checkout changes the daemon's tree",
    "rebase": "`git rebase` in the main checkout rewrites the daemon's branch",
    "cherry-pick": "`git cherry-pick` in the main checkout changes the daemon's tree",
    "revert": "`git revert` in the main checkout changes the daemon's tree",
    "am": "`git am` in the main checkout changes the daemon's tree",
}

_MAIN_HINT = (
    "this is the main checkout the live daemon runs from - work in a git "
    "worktree (`git worktree add ...`)"
)


def _check_git(argv: list[str], cwd: Path, main_ok: bool) -> str | None:
    sub, rest, chdir = _git_parts(argv)
    if sub is None:
        return None
    git_cwd = _resolve(cwd, chdir) if chdir else cwd
    is_main = not main_ok and _is_main_checkout(git_cwd)

    if sub == "stash":
        return (
            "blocked: `git stash` is shared across worktrees and holds the "
            "maintainer's work. Commit on your branch or save `git diff > "
            "/tmp/fix.patch` instead."
        )
    if sub == "checkout":
        verdict = _check_git_checkout(rest, git_cwd)
        if verdict:
            return verdict
    elif sub == "restore":
        return _check_git_restore(rest, git_cwd)
    elif sub == "add":
        verdict = _check_git_add(rest)
        if verdict:
            return verdict
    elif sub == "clean":
        verdict = _check_git_clean(rest)
        if verdict:
            return verdict
    elif sub == "reset":
        verdict = _check_git_reset(rest)
        if verdict:
            return verdict
    elif sub == "push":
        return _check_git_push(rest, git_cwd)

    if is_main and sub in _MAIN_GIT_BLOCKED:
        return f"blocked: {_MAIN_GIT_BLOCKED[sub]}; {_MAIN_HINT}."
    return None


# ---------------------------------------------------------------------------
# non-git rules
# ---------------------------------------------------------------------------


def _check_pkill(argv: list[str]) -> str | None:
    """Block ``pkill``/``killall`` outright.

    On this shared box even a name-only match kills other agents' test runs,
    and ``-f`` additionally matches the agent's own shell command line.
    """
    if _basename(argv[0]) in ("pkill", "killall"):
        return (
            "blocked: pkill/killall can kill this shell or other agents' "
            "processes on the shared box. Find the PID with `pgrep -af "
            "<name>` or `ps`, then `kill <pid>`."
        )
    return None


_RUFF_VALUE_OPTS = {"-o"}
_RUFF_VALUE_LONGS = {
    "--config",
    "--exclude",
    "--extend-exclude",
    "--line-length",
    "--indent-width",
    "--target-version",
    "--select",
    "--ignore",
    "--extend-select",
    "--extend-ignore",
    "--per-file-ignores",
    "--output-format",
    "--stdin-filename",
    "--required-version",
    "--extension",
    "--config-file",
}


def _check_ruff(argv: list[str], cwd: Path) -> str | None:
    if _basename(argv[0]) != "ruff":
        return None
    args = argv[1:]
    # Locate the `format` subcommand past ruff's own global options.
    idx = _skip_options(args, 0, _RUFF_VALUE_OPTS, _RUFF_VALUE_LONGS)
    if idx >= len(args) or args[idx] != "format":
        return None
    files = _positional_args(args[idx + 1 :], 0, _RUFF_VALUE_OPTS, _RUFF_VALUE_LONGS)
    explicit = bool(files) and all(
        not any(ch in _GLOB_CHARS for ch in f)
        and f not in (".", "..")
        and not f.endswith("/")
        and not f.startswith(":(")
        and _resolve(cwd, f).is_file()
        for f in files
    )
    if not explicit:
        return (
            "blocked: `ruff format` without explicit file arguments "
            "reformats the whole tree and pulls unrelated lines into the "
            "diff. Pass the files you changed: `ruff format <file>...`."
        )
    return None


_UV_GLOBAL_VALUE_OPTS = {"-p", "-c"}
_UV_GLOBAL_VALUE_LONGS = {
    "--directory",
    "--project",
    "--config-file",
    "--cache-dir",
    "--python",
    "--index-url",
    "--default-index",
    "--index",
    "--keyring-provider",
    "--python-preference",
    "--exclude-newer",
    "--link-mode",
    "--config-setting",
    "--extra",
    "--group",
    "--package",
    "--torch-backend",
    "--python-install-dir",
    "--cert",
    "--client-cert",
    "--allow-insecure-host",
    "--index-strategy",
    "--resolution",
    "--build-constraint",
    "--constraint",
    "--overrides",
    "--env-file",
}

_UV_RUN_VALUE_OPTS = {"-w", "-p", "-c"}
_UV_RUN_VALUE_LONGS = _UV_GLOBAL_VALUE_LONGS | {
    "--with",
    "--with-editable",
    "--from",
    "--python-platform",
    "--python-version",
    "--script",
}


def _uv_command_index(args: list[str]) -> int:
    """Index of uv's subcommand, past global options."""
    return _skip_options(args, 0, _UV_GLOBAL_VALUE_OPTS, _UV_GLOBAL_VALUE_LONGS)


def _uv_inner_argv(argv: list[str]) -> list[str] | None:
    """For `uv run ...`, `uv tool run ...`, `uvx ...`: the wrapped command."""
    name = _basename(argv[0])
    args = argv[1:]
    if name == "uvx":
        start = 0
    elif name == "uv":
        idx = _uv_command_index(args)
        if idx >= len(args):
            return None
        if args[idx] == "run":
            start = idx + 1
        elif args[idx] == "tool" and idx + 1 < len(args) and args[idx + 1] == "run":
            start = idx + 2
        else:
            return None
    else:
        return None
    inner = args[start:]
    idx = _skip_options(inner, 0, _UV_RUN_VALUE_OPTS, _UV_RUN_VALUE_LONGS)
    return inner[idx:] if idx < len(inner) else None


_PIP_MUTATING = {"install", "uninstall", "sync", "wheel"}
_NPM_MUTATING = {"install", "ci", "i"}
_UV_READONLY_PIP = {"list", "check", "freeze", "show", "tree", "compile"}
_UV_ENV_BLOCKED = {"sync", "add", "remove", "venv", "publish"}

_PIP_GLOBAL_VALUE_OPTS: set[str] = set()
_PIP_GLOBAL_VALUE_LONGS = {
    "--proxy",
    "--cert",
    "--client-cert",
    "--cache-dir",
    "--src",
    "--exists-action",
    "--trusted-host",
    "--index-url",
    "--extra-index-url",
    "--python",
}

_NPM_GLOBAL_VALUE_LONGS = {
    "--prefix",
    "--cache",
    "--userconfig",
    "--globalconfig",
    "--loglevel",
    "--registry",
}

_UV_PIP_VALUE_OPTS = {"-p", "-f", "-c"}
_UV_PIP_VALUE_LONGS = _UV_GLOBAL_VALUE_LONGS | {
    "--target",
    "--prefix",
    "--src",
    "--reinstall",
    "--reinstall-package",
    "--upgrade-package",
    "--exclude-newer-package",
}


def _check_main_env_command(argv: list[str]) -> str | None:
    """Commands that mutate the shared venv or the daemon's checkout."""
    name = _basename(argv[0])
    args = argv[1:]

    if name == "uv":
        idx = _uv_command_index(args)
        if idx >= len(args):
            return None
        sub = args[idx]
        rest = args[idx + 1 :]
        if sub == "pip":
            j = _skip_options(rest, 0, _UV_PIP_VALUE_OPTS, _UV_PIP_VALUE_LONGS)
            pip_sub = rest[j] if j < len(rest) else ""
            if not pip_sub or "--dry-run" in rest or "--check" in rest:
                return None
            if pip_sub not in _UV_READONLY_PIP:
                return (
                    f"blocked: `uv pip {pip_sub}` changes the shared venv; "
                    f"{_MAIN_HINT}."
                )
            return None
        if sub == "run":
            if "--no-sync" in rest or "--no-project" in rest:
                return None
            return (
                "blocked: `uv run` syncs the project env (which can drop "
                f"extras the live daemon needs); {_MAIN_HINT}."
            )
        if sub in _UV_ENV_BLOCKED:
            return (
                f"blocked: `uv {sub}` changes the shared venv or the "
                f"daemon's checkout; {_MAIN_HINT}."
            )
        return None

    if name in ("pip", "pip3"):
        idx = _skip_options(args, 0, _PIP_GLOBAL_VALUE_OPTS, _PIP_GLOBAL_VALUE_LONGS)
        sub = args[idx] if idx < len(args) else ""
        if sub in _PIP_MUTATING:
            return f"blocked: `pip {sub}` changes the shared venv; {_MAIN_HINT}."
        return None

    if name == "npm":
        idx = _skip_options(args, 0, set(), _NPM_GLOBAL_VALUE_LONGS)
        sub = args[idx] if idx < len(args) else ""
        if sub in _NPM_MUTATING:
            return f"blocked: `npm {sub}` changes the shared node_modules; {_MAIN_HINT}."
        return None

    if name == "ln":
        opts = [a for a in args if a.startswith("-")]
        symlink = any(
            (o.startswith("-") and not o.startswith("--") and "s" in o[1:])
            or o == "--symbolic"
            for o in opts
        )
        if symlink:
            for a in args:
                if not a.startswith("-") and PurePosixPath(a.rstrip("/")).name == "node_modules":
                    return (
                        "blocked: symlinking `node_modules` into the main "
                        f"checkout changes the daemon's tree; {_MAIN_HINT}."
                    )
    return None


# ---------------------------------------------------------------------------
# wrapper commands and the evaluator
# ---------------------------------------------------------------------------

# name -> (short value opts, long value opts, positional args to skip)
_WRAPPER_OPTS: dict[str, tuple[set[str], set[str], int]] = {
    "env": ({"-u", "-C", "-S"}, {"--unset", "--chdir", "--split-string", "--argv0"}, 0),
    "sudo": (
        {"-u", "-g", "-h", "-p", "-C", "-T", "-r", "-t", "-U", "-R"},
        {
            "--user",
            "--group",
            "--host",
            "--prompt",
            "--chdir",
            "--role",
            "--type",
            "--other-user",
            "--close-from",
        },
        0,
    ),
    "doas": ({"-u"}, {"--user"}, 0),
    "nice": ({"-n"}, {"--adjustment"}, 0),
    "nohup": (set(), set(), 0),
    "time": (set(), set(), 0),
    "timeout": ({"-k", "-s"}, {"--kill-after", "--signal"}, 1),
    "stdbuf": ({"-o", "-e", "-i"}, set(), 0),
    "ionice": ({"-c", "-n", "-p"}, set(), 0),
    "taskset": (set(), set(), 1),
    "chrt": ({"-p", "-m"}, set(), 1),
    "command": (set(), set(), 0),
    "exec": (set(), set(), 0),
}

_SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}
_CD_BUILTINS = {"cd", "pushd"}

_PY_VALUE_OPTS = {"-W", "-X"}
_PY_VALUE_LONGS = {"--check-hash-based-pycs"}


def _strip_assignments(argv: list[str]) -> tuple[list[str], bool]:
    """Remove leading ``NAME=value`` tokens; True if the override was set."""
    override = False
    i = 0
    while i < len(argv) and _ASSIGN_RE.match(argv[i]):
        if argv[i] == _MAIN_OK_MARKER:
            override = True
        i += 1
    return argv[i:], override


def _eval_argv(
    argv: list[str], cwd: Path, depth: int, main_ok: bool
) -> str | None:
    if depth > _MAX_DEPTH or not argv:
        return None
    argv, seg_override = _strip_assignments(argv)
    main_ok = main_ok or seg_override
    if not argv:
        return None
    name = _basename(argv[0]).strip("`")
    args = argv[1:]

    # Shells running a command string: `bash -c '...'`, `sh -c ...`.
    if name in _SHELLS:
        i = 0
        while i < len(args):
            tok = args[i]
            if tok == "-c" or (
                tok.startswith("-")
                and not tok.startswith("--")
                and len(tok) > 1
                and "c" in tok[1:]
            ):
                if i + 1 < len(args):
                    return _eval_command_string(args[i + 1], cwd, depth + 1, main_ok)
                return None
            if tok == "--":
                break
            if tok.startswith("-"):
                if tok in ("-o", "+o") and i + 1 < len(args):
                    i += 2
                else:
                    i += 1
                continue
            break
        return None

    if name == "eval":
        return _eval_command_string(" ".join(args), cwd, depth + 1, main_ok)

    # `python -m pip install` / `python -m ruff format` reach the same rules.
    if name == "python" or name.startswith("python"):
        m_idx = None
        i = 0
        while i < len(args):
            tok = args[i]
            if tok == "-m" and i + 1 < len(args):
                m_idx = i
                break
            if tok == "-c" or (not tok.startswith("-") and tok != "-"):
                break  # `-c` code or a script path ends option parsing
            i = _skip_value(args, i, tok, _PY_VALUE_OPTS, _PY_VALUE_LONGS)
        if m_idx is not None:
            module = args[m_idx + 1]
            if module == "pip":
                return _check_simple(["pip", *args[m_idx + 2 :]], cwd, depth, main_ok)
            if module == "ruff":
                return _check_simple(["ruff", *args[m_idx + 2 :]], cwd, depth, main_ok)
        return None

    inner = _uv_inner_argv(argv)
    if inner is not None and name in ("uv", "uvx"):
        # `uv run`/`uvx` get their own main-checkout verdict first (uv run
        # auto-syncs the env), then the wrapped command is checked too.
        verdict = _check_simple(argv, cwd, depth, main_ok)
        if verdict:
            return verdict
        return _eval_argv(inner, cwd, depth + 1, main_ok)

    spec = _WRAPPER_OPTS.get(name)
    if spec is not None:
        value_opts, value_longs, skip_pos = spec
        if name == "env":
            # `env -S 'cmd args'` carries a whole command line as one token.
            for j, tok in enumerate(args):
                if tok in ("-S", "--split-string") and j + 1 < len(args):
                    return _eval_command_string(args[j + 1], cwd, depth + 1, main_ok)
                if tok.startswith("--split-string="):
                    return _eval_command_string(
                        tok.split("=", 1)[1], cwd, depth + 1, main_ok
                    )
        idx = _skip_options(args, 0, value_opts, value_longs)
        rest = args[idx + skip_pos :] if idx + skip_pos <= len(args) else []
        # `env` also takes NAME=value assignments before the command.
        while rest and _ASSIGN_RE.match(rest[0]):
            if rest[0] == _MAIN_OK_MARKER:
                main_ok = True
            rest = rest[1:]
        if rest:
            return _eval_argv([rest[0], *rest[1:]], cwd, depth + 1, main_ok)
        return None

    return _check_simple(argv, cwd, depth, main_ok)


def _cd_target(argv: list[str], cwd: Path) -> Path | None:
    """Directory a ``cd``/``pushd`` segment moves to, or None to keep cwd."""
    args = argv[1:]
    idx = _skip_options(args, 0, set(), set())
    if idx >= len(args):
        return Path.home()
    target = args[idx]
    if target == "-":
        return None  # OLDPWD is not tracked
    resolved = _resolve(cwd, target)
    try:
        return resolved if resolved.is_dir() else None
    except OSError:
        return None


def _check_simple(argv: list[str], cwd: Path, depth: int, main_ok: bool) -> str | None:
    verdict = _check_pkill(argv)
    if verdict:
        return verdict
    verdict = _check_ruff(argv, cwd)
    if verdict:
        return verdict
    if _basename(argv[0]).strip("`") == "git":
        return _check_git(argv, cwd, main_ok)
    if not main_ok and _is_main_checkout(cwd):
        return _check_main_env_command(argv)
    return None


def _eval_tokens(
    tokens: list[str], cwd: Path, depth: int, main_ok: bool
) -> str | None:
    """Walk the token stream, tracking cwd across ``cd`` and subshell parens."""
    cwd = Path(cwd)
    paren_stack: list[Path] = []
    current: list[str] = []
    segments = 0
    i = 0

    def flush() -> str | None:
        nonlocal current, cwd, segments
        if not current:
            return None
        seg, current = current, []
        segments += 1
        if segments > _MAX_SEGMENTS:
            return None
        bare = _strip_assignments(seg)[0]
        if bare and _basename(bare[0]).strip("`") in _CD_BUILTINS:
            target = _cd_target(bare, cwd)
            if target is not None:
                cwd = target
            return None
        return _eval_argv(seg, cwd, depth, main_ok)

    while i < len(tokens):
        tok = tokens[i]
        kind = _op_kind(tok)
        if kind == "ctrl":
            verdict = flush()
            if verdict:
                return verdict
            for ch in tok:
                if ch == "(":
                    paren_stack.append(cwd)
                elif ch == ")" and paren_stack:
                    cwd = paren_stack.pop()
            i += 1
            continue
        if kind == "redir":
            verdict = flush()
            if verdict:
                return verdict
            i += 2  # skip the redirect target
            continue
        current.append(tok)
        i += 1
    return flush()


def _eval_command_string(command: str, cwd: Path, depth: int, main_ok: bool) -> str | None:
    return _eval_tokens(_tokenize(command), cwd, depth, main_ok)


def check_command(command: str, cwd: Path) -> str | None:
    """Return the block message for ``command`` run in ``cwd``, or None."""
    if not command.strip():
        return None
    # The human/operator override is a property of the whole command line.
    return _eval_tokens(_tokenize(command), cwd, 0, main_ok=_MAIN_OK_MARKER in command)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str):
        return 0
    cwd_raw = payload.get("cwd") or os.getcwd()
    try:
        cwd = Path(cwd_raw).resolve()
    except OSError:
        cwd = Path(os.getcwd())
    verdict = check_command(command, cwd)
    if verdict:
        print(verdict, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
