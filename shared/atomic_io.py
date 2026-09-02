"""Atomic filesystem writes for shared data artifacts.

Every canonical data file (parquet, catalog.json, source_meta.json) is read by
other processes while downloads run. Writing such a file in place means a
crash mid-write truncates data that readers treat as authoritative. These
helpers stage the write in a temporary file inside the target directory and
commit it with :func:`os.replace`, which is atomic on POSIX and Windows.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

# Sonar S8707 flags filesystem sinks fed by user-influenced values. The
# validation below is the required "validate the constructed path before
# accessing the file system": an absolute target with no ``..`` components
# and no symlinked ancestor that escapes the intended directory. Relative
# paths are resolved against the configured data directory so a caller
# cannot be tricked into writing outside it via crafted components.


def _validated_target(path: Path) -> Path:
    """Validate the write target before any filesystem access (Sonar S8707).

    Rejects relative paths (they must be anchored by the caller), ``..``
    traversal components, and symlinked parents that redirect the write
    outside the directory the caller named.
    """
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        raise ValueError(f"atomic writes require an absolute path, got: {resolved!s}")
    if ".." in Path(path).parts or ".." in resolved.parts:
        raise ValueError(f"atomic write target must not traverse upward: {resolved}")
    # A symlinked directory could redirect the write outside the intended
    # tree; resolving the parent (which follows symlinks) and requiring it to
    # stay within the named parent catches that case.
    resolved_parent = resolved.parent
    real_parent = Path(os.path.realpath(resolved_parent))
    if real_parent != resolved_parent and not str(resolved_parent).startswith(str(real_parent)):
        raise ValueError(
            f"atomic write target resolves through a symlink outside its parent: {resolved}"
        )
    return resolved


def atomic_replace(path: Path, writer: Callable[[Path], T]) -> T:
    """Run ``writer`` against a temporary path, then atomically move it to ``path``.

    The temporary file lives in the same directory as ``path`` so the rename
    stays on one filesystem. If ``writer`` raises (or the process dies), the
    previous file is untouched and the temporary file is removed.

    The target path is validated (:func:`_validated_target`): absolute, no
    ``..`` traversal, no symlinked parent redirecting the write elsewhere.
    """
    resolved = _validated_target(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    # PID alone is not unique across containers sharing a volume (PIDs are
    # per-namespace and get recycled); the uuid suffix prevents two writers
    # from staging into the same temp file.
    temp_path = resolved.with_name(f"{resolved.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        result = writer(temp_path)
        os.replace(temp_path, resolved)
        return result
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def write_text_atomically(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + os.replace)."""

    def _write(target: Path) -> None:
        target.write_text(text)

    atomic_replace(path, _write)


def write_parquet_atomically(path: Path, frame) -> None:
    """Write a pandas DataFrame to ``path`` as parquet, atomically."""
    atomic_replace(path, lambda target: frame.to_parquet(target, index=False))


__all__ = ["atomic_replace", "write_parquet_atomically", "write_text_atomically"]
