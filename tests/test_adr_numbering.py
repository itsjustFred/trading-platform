"""Guards for the ADR directory: unique numbers, and no dangling ADR paths.

Two ADRs once shared the number 0001, so "ADR 0001" was ambiguous. These
tests keep every number unique and make sure every ``docs/adr/<file>.md``
path quoted in the code or docs still points at an existing ADR.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = PROJECT_ROOT / "docs" / "adr"
ADR_NAME = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
ADR_REFERENCE = re.compile(r"docs/adr/([A-Za-z0-9_.-]+\.md)")
SCANNED_DIRS = (
    "docs", "runner", "live_runner", "risk", "shared", "scripts", "strategies", "tests", "viewer",
)
SCANNED_SUFFIXES = {".py", ".md"}


def _adr_files() -> list[Path]:
    return sorted(p for p in ADR_DIR.iterdir() if p.is_file() and p.suffix == ".md")


def test_adr_files_follow_the_naming_scheme() -> None:
    bad = [p.name for p in _adr_files() if not ADR_NAME.match(p.name)]
    assert not bad, f"ADR files must be named NNNN-kebab-title.md: {bad}"


def test_adr_numbers_are_unique() -> None:
    by_number: dict[str, list[str]] = defaultdict(list)
    for path in _adr_files():
        match = ADR_NAME.match(path.name)
        if match:
            by_number[match.group(1)].append(path.name)
    duplicates = {num: names for num, names in by_number.items() if len(names) > 1}
    assert not duplicates, f"ADR numbers must be unique: {duplicates}"


def test_every_referenced_adr_path_exists() -> None:
    existing = {p.name for p in _adr_files()}
    dangling: list[str] = []
    for top in SCANNED_DIRS:
        root = PROJECT_ROOT / top
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix not in SCANNED_SUFFIXES or not path.is_file():
                continue
            if any(part in {"node_modules", "__pycache__", ".venv"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            dangling.extend(
                f"{rel}: docs/adr/{name}"
                for name in ADR_REFERENCE.findall(text)
                if name not in existing
            )
    assert not dangling, f"References to missing ADRs: {dangling}"
