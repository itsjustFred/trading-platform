"""Regression pins for the synthetic MT5 XLSX fixtures.

``tests/fixtures/validation/mt5_xlsx/goldens/*.json`` is the importer output
projected onto the checked fields (see ``scripts/generate_mt5_xlsx_fixtures.py``).
The golden only catches *changes*. Whether the values are right is decided by
the hand-computed expectations in ``test_mt5_xlsx_fixture_shapes.py``, so a
golden must never be regenerated to make a shape test pass.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

import scripts.generate_mt5_xlsx_fixtures as generator
from scripts.generate_mt5_xlsx_fixtures import project_reference
from validation.importers.mt5_xlsx import MT5XLSXImporter

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "validation" / "mt5_xlsx"
GOLDEN_DIR = FIXTURE_DIR / "goldens"
# This list is deliberately independent of the generator's ``_FIXTURES``: it is
# the pin. ``test_generator_registers_every_fixture`` fails loudly when a
# fixture is added to (or dropped from) the generator, so a new workbook is
# never generated but left unpinned, and a dropped one never disappears
# silently from both lists at once.
NAMES = ["hedged_partial_open", "position_id_shared"]


def _assert_close(actual: Any, expected: Any, path: str) -> None:
    if isinstance(expected, float) or (isinstance(actual, float) and isinstance(expected, int)):
        assert actual is not None, f"{path}: expected {expected}, got None"
        assert math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9), (
            f"{path}: {actual} != {expected}"
        )
    elif isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: not a dict"
        assert set(actual) == set(expected), f"{path}: keys differ: {set(actual) ^ set(expected)}"
        for key in expected:
            _assert_close(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: not a list"
        assert len(actual) == len(expected), f"{path}: length {len(actual)} != {len(expected)}"
        # Length equality is asserted above; ``strict`` makes the pairing explicit.
        for index, (a, e) in enumerate(zip(actual, expected, strict=True)):
            _assert_close(a, e, f"{path}[{index}]")
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def test_generator_registers_every_fixture() -> None:
    """Every committed fixture is generated, and only those.

    Without this, unregistering a fixture surfaces as a ``FileNotFoundError``
    on the missing golden (or, if the list were imported from the generator,
    not at all: the pin would vanish with the registration).
    """
    assert set(generator._FIXTURES) == set(NAMES), (
        "scripts/generate_mt5_xlsx_fixtures.py registers "
        f"{sorted(generator._FIXTURES)}, the tests pin {sorted(NAMES)}; "
        "update the generator and the test lists in the same commit"
    )


@pytest.mark.parametrize("name", NAMES)
def test_importer_output_matches_golden(name: str) -> None:
    golden = json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))
    actual = project_reference(MT5XLSXImporter().load(FIXTURE_DIR / f"{name}.xlsx"))
    _assert_close(actual, golden, name)
