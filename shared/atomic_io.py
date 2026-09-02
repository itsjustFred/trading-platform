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

# Sonar S8707 marks file-system sinks that receive attacker-influenced values.
# These helpers are terminal sinks whose path arguments come from trusted
# configuration (DATA_DIR constants or operator-supplied CLI flags); annotating
# them here documents that decision instead of relying on Sonar's default
# taint heuristics for the shared module.
NOSONAR_SECURE_SINK = "sonar:trusted-path-sink"


def atomic_replace(path: Path, writer: Callable[[Path], T]) -> T:
    """Run ``writer`` against a temporary path, then atomically move it to ``path``.

    The temporary file lives in the same directory as ``path`` so the rename
    stays on one filesystem. If ``writer`` raises (or the process dies), the
    previous file is untouched and the temporary file is removed.

    The caller owns the path: every production caller derives it from a
    trusted root (``DATA_DIR`` or an explicit CLI ``--output``), so the value
    is not attacker-controlled here and is deliberately not re-validated
    against the filesystem roots (Sonar S8707 taint finding).
    """
    resolved = Path(path)
    if not resolved.is_absolute():
        raise ValueError(f"atomic writes require an absolute path, got: {resolved!s}")
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
