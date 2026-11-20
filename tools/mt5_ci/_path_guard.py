"""Confine CLI paths to caller-approved roots.

Both ``convert_setfile.py`` and ``inspect_report.py`` take file paths from the
command line and open them. A crafted ``--input``/``--report``/``--json-out``
(``..``, an absolute path, a symlink) must not read or write outside the roots
the caller allows (SonarCloud S8707), so the value used at the file sink is
always the resolved path returned here, never the raw argument.

The CI job passes ``--root`` twice: the checkout and the portable terminal
directory, which sits outside the workspace.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


class PathOutsideRoots(ValueError):
    """Raised when a CLI path resolves outside every allowed root."""


def allowed_roots(raw_roots: Sequence[str]) -> tuple[Path, ...]:
    """Resolve the ``--root`` arguments, defaulting to the current directory."""
    if not raw_roots:
        return (Path.cwd().resolve(),)
    return tuple(Path(root).expanduser().resolve() for root in raw_roots)


def resolve_cli_path(
    raw: Path,
    roots: Sequence[Path],
    what: str,
    error: type[ValueError] = PathOutsideRoots,
) -> Path:
    """Resolve a CLI path and confine it to the allowed roots.

    ``error`` lets each tool raise its own error type, so its ``main`` keeps
    catching one class for both parse and path failures.
    """
    candidate = Path(raw).expanduser().resolve()
    if not any(candidate == root or root in candidate.parents for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise error(f"{what} {str(raw)!r} resolves to {candidate}, outside {allowed}")
    return candidate
