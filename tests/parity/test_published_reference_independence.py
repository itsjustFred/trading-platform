"""The published-strategy references must stay independent of the platform."""
from __future__ import annotations

import ast
from pathlib import Path

REFERENCE = Path(__file__).with_name("ref_published.py")
PLATFORM = {"runner", "strategies", "risk", "shared", "data_tools", "tests"}


def test_reference_has_no_platform_imports():
    tree = ast.parse(REFERENCE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & PLATFORM, imported & PLATFORM
    assert imported <= {"__future__", "dataclasses"}, imported
