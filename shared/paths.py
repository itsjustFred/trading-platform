"""Path containment in one place: join a caller-supplied path beneath a base.

A viewer route that turns a request value into a file path, and a script that
writes to a path an operator (or a runbook an LLM wrote) supplied, both need the
same answer to the same question: *does this value, once joined and resolved,
still name something inside the directory I meant?* Hand-written answers drifted
apart -- one checked the lexical string, another the resolved path, a third
served a path it had not checked -- and each hardening round found an alias the
previous guard missed. :func:`safe_join` is the one implementation.

What it refuses, and why:

* an **absolute** ``user_path``, because "absolute" is not the same as
  "intended": pathlib silently drops the base when joining one. Callers that
  must accept an operator-supplied absolute path (the viewer's data-file routes
  take one from the client) opt in with ``allow_absolute=True``; containment is
  then still checked against ``base``.
* **``..`` segments** in any position, even ones that would resolve back inside
  the base. A path whose written form climbs is a path whose author was
  reasoning about a directory they did not name; refusing is cheaper than
  guessing which parts were meant.
* **NUL bytes**, empty/blank paths, and ``.``: they either raise inside the
  filesystem layer or name the base itself, which is a directory, not a file.
* a result **equal to ``base``**, unless the caller passes ``allow_base=True``
  because it really does mean the directory.

Backslashes are *not* special here. On POSIX a backslash is an ordinary
filename character, so ``..\\x`` is a plain file name that cannot escape the
base; on Windows the same string is two parts, ``..`` and ``x``, and the ``..``
rule refuses it. The parts are what decide, which is why the check is written
in terms of :class:`pathlib.PurePath.parts` rather than string prefixes. Callers
that need the *same* input rejected identically on every platform (the matrix
and asset-id routes refuse separators outright) keep that as their own naming
rule before calling here.

Symlinks: the candidate is resolved, so a symlink inside ``base`` that points
outside it resolves outside and is refused. A symlink that stays inside is
accepted -- it names the same tree the caller confined itself to. Callers that
write to or delete the result refuse symlinks outright with
``refuse_symlinks=True``: a swap between check and use is then not a question of
where the link pointed.

Rejections raise :class:`PathContainmentError`, a ``ValueError``, so callers
that already translate ``ValueError`` into their own error keep working.
"""

from __future__ import annotations

import os
from pathlib import Path


class PathContainmentError(ValueError):
    """Raised when a caller-supplied path does not stay inside its base directory."""


def _refuse_symlinked_components(base: Path, candidate: Path) -> None:
    """Raise if any component of ``candidate`` below ``base`` is a symlink."""
    current = base
    for part in candidate.parts:
        if part in {".", "..", ""}:
            continue
        current = current / part
        if current.is_symlink():
            raise PathContainmentError(
                f"refusing a path that runs through a symlink: {current}"
            )


def safe_join(
    base: str | os.PathLike[str],
    user_path: str | os.PathLike[str],
    *,
    allow_absolute: bool = False,
    allow_base: bool = False,
    refuse_symlinks: bool = False,
) -> Path:
    """Resolve ``user_path`` beneath ``base``, refusing anything that leaves it.

    ``base`` is resolved once, so a symlinked or relative base compares
    correctly against the resolved candidate. The returned path is absolute,
    symlink-free in its final form (``resolve()`` followed it), and the same
    path the containment check approved -- callers must use it rather than
    joining again.
    """
    if user_path is None or isinstance(user_path, bytes):
        raise PathContainmentError(f"path must be text, not {type(user_path).__name__}")
    raw = os.fspath(user_path)
    if isinstance(raw, bytes):  # pragma: no cover - os.fspath on a bytes path
        raise PathContainmentError("path must be text, not bytes")
    if "\x00" in raw:
        raise PathContainmentError(f"refusing a path containing a NUL byte: {raw!r}")
    if not raw.strip():
        raise PathContainmentError("refusing an empty path")

    try:
        candidate = Path(raw)
    except (OSError, ValueError) as exc:
        raise PathContainmentError(f"refusing an unparseable path: {raw!r}") from exc

    if candidate.is_absolute() and not allow_absolute:
        raise PathContainmentError(f"refusing an absolute path: {raw!r}")
    if ".." in candidate.parts:
        raise PathContainmentError(f"refusing a path containing '..': {raw!r}")

    base_path = Path(base)
    resolved_base = base_path.resolve()
    if refuse_symlinks:
        # Checked on the *unresolved* base: ``resolved_base`` has already
        # followed a symlinked base away, so it could never report one.
        if base_path.is_symlink():
            raise PathContainmentError(f"refusing a symlinked base directory: {base_path}")
        # An absolute candidate is already checked against ``resolved_base``
        # below; walking its components from the anchor is what keeps the
        # symlink refusal honest for it too.
        walk = candidate
        if candidate.is_absolute():
            walk = candidate.relative_to(candidate.anchor)
        _refuse_symlinked_components(resolved_base, walk)
    joined = candidate if candidate.is_absolute() else resolved_base / candidate
    try:
        resolved = joined.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathContainmentError(f"refusing a path that cannot be resolved: {raw!r}") from exc

    if resolved == resolved_base:
        if not allow_base:
            raise PathContainmentError(f"refusing the base directory itself: {raw!r}")
    elif not resolved.is_relative_to(resolved_base):
        raise PathContainmentError(
            f"refusing a path outside {resolved_base}: {raw!r} resolves to {resolved}"
        )
    return resolved
