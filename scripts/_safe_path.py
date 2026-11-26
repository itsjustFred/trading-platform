"""Validate a caller-supplied path before a script writes to it.

The scripts that take an output path (or derive one from file content) on the command line are
driven by an operator, and by runbooks that quote a path an LLM produced. A path is not inherently
unsafe - the point is to make the *intent* explicit and the failure legible: refuse a traversal
segment, refuse a path that escapes its stated root, and hand back one normalized ``Path`` rather
than letting each caller re-derive it.

Deliberately small, and not a second implementation: the containment decision is
:func:`shared.paths.safe_join`, the same helper the viewer routes use. This module keeps the
script-facing names and the ``field``-prefixed message, so a runbook author sees which argument was
wrong instead of a half-written file. ``reject_traversal`` stays a pure lexical check because it
has no root to confine a path to.
"""

from __future__ import annotations

from pathlib import Path

from shared.paths import PathContainmentError, safe_join


class UnsafePathError(ValueError):
    """Raised when a path contains a traversal segment or escapes its root."""


def reject_traversal(raw: str | Path, *, field: str) -> Path:
    """Return `raw` as a normalized `Path`, refusing any ``..`` segment.

    Relative or absolute, the answer is the same: a path whose *written* form contains ``..`` is a
    path whose author was reasoning about a directory they were not willing to name. Refusing is
    cheaper than guessing which parts were meant.
    """
    text = str(raw)
    parts = Path(text).parts
    if ".." in parts:
        raise UnsafePathError(
            f"{field}: refusing a path containing '..' ({text!r}); name the directory directly"
        )
    return Path(text)


def under_root(raw: str | Path, *, root: Path, field: str) -> Path:
    """Return `raw` resolved beneath `root`, refusing anything that lands outside it.

    `root` is normally ``Path.cwd()`` or the script's own project root, so a runbook that writes
    into the repository cannot be talked into writing anywhere else by a path it was handed. An
    absolute `raw` is checked against `root` too rather than trusted: "absolute" is not the same as
    "intended". Containment is :func:`shared.paths.safe_join`, so the path is resolved and a symlink
    inside `root` that points outside it is refused as well.
    """
    try:
        return safe_join(root, raw, allow_absolute=True)
    except PathContainmentError as exc:
        raise UnsafePathError(f"{field}: {exc}") from None
