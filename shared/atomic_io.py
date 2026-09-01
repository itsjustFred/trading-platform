"""Atomic filesystem writes for shared data artifacts.

Every canonical data file (parquet, catalog.json, source_meta.json) is read by
other processes while downloads run. Writing such a file in place means a
crash mid-write truncates data that readers treat as authoritative. These
helpers stage the write in a temporary file inside the target directory and
commit it with :func:`os.replace`, which is atomic on POSIX and Windows.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


def atomic_replace(path: Path, writer: Callable[[Path], T]) -> T:
    """Run ``writer`` against a temporary path, then atomically move it to ``path``.

    The temporary file lives in the same directory as ``path`` so the rename
    stays on one filesystem. If ``writer`` raises (or the process dies), the
    previous file is untouched and the temporary file is removed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        result = writer(temp_path)
        os.replace(temp_path, path)
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
