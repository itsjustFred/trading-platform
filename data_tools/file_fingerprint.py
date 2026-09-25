from __future__ import annotations

import filecmp
from pathlib import Path
from typing import Any

FileFingerprint = tuple[int, int]


def file_fingerprint(path: Path | str) -> FileFingerprint | None:
    """Return the exact portable stat fingerprint for a file."""
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return int(stat.st_mtime_ns), int(stat.st_size)


def fingerprint_to_json(fingerprint: FileFingerprint | None) -> dict[str, int] | None:
    if fingerprint is None:
        return None
    return {"mtime_ns": fingerprint[0], "size_bytes": fingerprint[1]}


def fingerprint_from_json(value: Any) -> FileFingerprint | None:
    if isinstance(value, dict):
        try:
            return int(value["mtime_ns"]), int(value["size_bytes"])
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    return None


def fingerprint_matches(path: Path | str, expected: Any) -> bool:
    expected_fingerprint = fingerprint_from_json(expected)
    return expected_fingerprint is not None and file_fingerprint(path) == expected_fingerprint


def files_have_same_content(left: Path | str, right: Path | str) -> bool:
    try:
        return filecmp.cmp(left, right, shallow=False)
    except OSError:
        return False


__all__ = [
    "FileFingerprint",
    "file_fingerprint",
    "fingerprint_from_json",
    "fingerprint_matches",
    "fingerprint_to_json",
    "files_have_same_content",
]
