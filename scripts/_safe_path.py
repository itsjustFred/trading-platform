"""Validate a caller-supplied path before a script writes to it.

The four scripts that take an output path (or derive one from file content) on the command line are
driven by an operator, and by runbooks that quote a path an LLM produced. A path is not inherently
unsafe - the point is to make the *intent* explicit and the failure legible: refuse a traversal
segment, refuse a path that escapes its stated root, and hand back one normalized ``Path`` rather
than letting each caller re-derive it.

Deliberately small. This is not a sandbox: it does not resolve symlinks, does not confine to a jail,
and does not claim to. It rejects the two things that are actual mistakes rather than policy -
``..`` segments and a path that climbs out of the tree it was given - and it does so with a message
naming the offending path, so a runbook author sees the typo instead of a half-written file.
"""

from __future__ import annotations

from pathlib import Path


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

    `root` is resolved once by the caller - normally ``Path.cwd()`` or the script's own project
    root - so a runbook that writes into the repository cannot be talked into writing anywhere else
    by a path it was handed. An absolute `raw` is checked against `root` too rather than trusted:
    "absolute" is not the same as "intended".
    """
    candidate = reject_traversal(raw, field=field)
    resolved_root = root.resolve()
    resolved = (
        (resolved_root / candidate).resolve()
        if not candidate.is_absolute()
        else candidate.resolve()
    )
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise UnsafePathError(
            f"{field}: refusing to write outside {resolved_root} ({resolved}); pass a path under "
            "that directory"
        )
    return resolved
