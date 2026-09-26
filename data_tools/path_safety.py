from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from typing import Iterable

_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "COM¹",
        "COM²",
        "COM³",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
        "LPT¹",
        "LPT²",
        "LPT³",
        "CLOCK$",
    }
)
_WINDOWS_FORBIDDEN_CHARACTERS = frozenset('<>:"/\\|?*')
_GLOB_CHARACTERS = frozenset("*?[]{}")


def validate_dataset_component(value: object, *, name: str = "dataset component") -> str:
    """Validate one portable dataset path component."""
    if value is None:
        raise ValueError(f"{name} must not be empty")
    component = str(value)
    if not component or component != component.strip():
        raise ValueError(f"{name} must not be empty or padded: {component!r}")
    if component in {".", ".."}:
        raise ValueError(f"{name} must not be a traversal component: {component!r}")
    if any(character in _WINDOWS_FORBIDDEN_CHARACTERS for character in component):
        raise ValueError(f"{name} contains a path or glob character: {component!r}")
    if any(character in _GLOB_CHARACTERS for character in component):
        raise ValueError(f"{name} contains a glob character: {component!r}")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in component):
        raise ValueError(f"{name} contains a control character: {component!r}")
    if component.endswith((".", " ")):
        raise ValueError(f"{name} must not end with a dot or space: {component!r}")
    reserved_stem = component.split(".", 1)[0].rstrip(" .").upper()
    if reserved_stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"{name} uses a Windows-reserved name: {component!r}")
    return component


def validate_dataset_components(
    values: Iterable[object], *, name: str = "dataset component"
) -> tuple[str, ...]:
    """Validate and normalize a sequence of dataset path components."""
    return tuple(validate_dataset_component(value, name=name) for value in values)


def _root_path(root: Path | str) -> Path:
    try:
        raw = Path(root).expanduser()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"could not resolve dataset root: {exc}") from exc
    raw_parts = {part for part in raw.parts}
    raw_parts.update(part for part in str(raw).replace("\\", "/").split("/") if part)
    if ".." in raw_parts:
        raise ValueError(f"dataset root must not traverse upward: {raw}")
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    return Path(os.path.abspath(os.path.normpath(raw)))


def _is_within(path: Path, root: Path) -> bool:
    try:
        return path == root or root in path.parents
    except ValueError:
        return False


def build_dataset_path(root: Path | str, *components: object) -> Path:
    """Build a canonical dataset path and reject component or symlink escapes."""
    if not components:
        raise ValueError("at least one dataset path component is required")
    validated = validate_dataset_components(components)
    base = _root_path(root)
    try:
        base_real = base.resolve(strict=False)
        candidate = base_real.joinpath(*validated)
        candidate_real = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"could not resolve dataset path: {exc}") from exc
    if not _is_within(candidate_real, base_real):
        raise ValueError(f"dataset path escapes its root through a symlink: {candidate_real}")
    return candidate_real


def ensure_dataset_path_within(root: Path | str, path: Path | str) -> Path:
    """Resolve an existing or prospective path and enforce root containment."""
    base = _root_path(root)
    try:
        candidate = Path(path).expanduser()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"could not resolve dataset path: {exc}") from exc
    if not candidate.is_absolute():
        candidate = base / candidate
    raw_parts = {part for part in candidate.parts}
    raw_parts.update(part for part in str(candidate).replace("\\", "/").split("/") if part)
    if ".." in raw_parts:
        raise ValueError(f"dataset path must not traverse upward: {candidate}")
    try:
        base_real = base.resolve(strict=False)
        candidate_real = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"could not resolve dataset path: {exc}") from exc
    if not _is_within(candidate_real, base_real):
        raise ValueError(f"dataset path escapes its root: {candidate_real}")
    return candidate_real


__all__ = [
    "build_dataset_path",
    "ensure_dataset_path_within",
    "validate_dataset_component",
    "validate_dataset_components",
]
