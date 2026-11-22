"""Guard: the committed synthetic MT5 XLSX fixtures must match their generator.

``scripts/generate_mt5_xlsx_fixtures.py`` is the only supported way to produce
``tests/fixtures/validation/mt5_xlsx/``. Nothing used to execute it, so a drift
between the generator and the committed workbooks went unnoticed: the shape tests
and the golden pins both read the *committed* ``.xlsx``, so neither of them can
tell that ``build_*`` would now emit different rows. A wrong deal profit would
then ride straight into the goldens (``generate()`` derives them from the
workbooks it writes) and into every validation result people act on.

These tests regenerate both workbooks into ``tmp_path`` and compare them, cell by
cell, against the committed files, then run ``generate()`` end to end and compare
the goldens and the README it writes. The generator is therefore covered by the
same suite that consumes its output.

The comparison is on cell content, not bytes: openpyxl stamps every save with a
fresh ``dcterms:modified`` (``openpyxl/writer/excel.py``) and zip entries carry
the write time, so two runs never produce identical bytes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

import scripts.generate_mt5_xlsx_fixtures as generator
from scripts.generate_mt5_xlsx_fixtures import _FIXED_TIME

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "validation" / "mt5_xlsx"
GOLDEN_DIR = FIXTURE_DIR / "goldens"
# Deliberately independent of the generator's ``_FIXTURES``: this list is the
# pin, and the registration test below fails loudly if the two ever disagree.
NAMES = ["hedged_partial_open", "position_id_shared"]

_BUILDERS = {
    "hedged_partial_open": generator.build_hedged_partial_open,
    "position_id_shared": generator.build_position_id_shared,
}


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
def test_regenerated_workbook_matches_committed_cells(name: str, tmp_path: Path) -> None:
    """Every cell of the regenerated workbook equals the committed fixture.

    This is the check that makes the README's "do not edit by hand" promise real.
    It covers the whole building half of the generator: ``_meta``, ``_balance``,
    the summary block, the orders grid and every deal row.
    """
    _BUILDERS[name](tmp_path / f"{name}.xlsx")
    committed = FIXTURE_DIR / f"{name}.xlsx"
    assert committed.exists(), f"{name}.xlsx is not committed"

    regenerated = tmp_path / f"{name}.xlsx"
    expected, actual = load_workbook(committed), load_workbook(regenerated)
    try:
        assert actual.sheetnames == expected.sheetnames
        assert actual.active.dimensions == expected.active.dimensions
        assert actual.active.title == expected.active.title
        # The document timestamps must stay fixed, so that the fixture content
        # does not depend on when it happened to be generated. ``modified`` is
        # asserted too: openpyxl stamps ``dcterms:modified`` with the save time
        # when it is not set, and only the fixed assignment keeps a re-run from
        # embedding the wall clock in the workbook's metadata.
        assert actual.properties.created == expected.properties.created == _FIXED_TIME
        assert actual.properties.modified == expected.properties.modified == _FIXED_TIME
        assert actual.properties.creator == expected.properties.creator
        assert actual.properties.creator, "the fixture must carry a creator"
        actual_grid = [list(row) for row in actual.active.iter_rows(values_only=True)]
        expected_grid = [list(row) for row in expected.active.iter_rows(values_only=True)]
    finally:
        expected.close()
        actual.close()
    assert actual_grid == expected_grid, (
        f"{name}.xlsx no longer matches scripts/generate_mt5_xlsx_fixtures.py; "
        "regenerate the fixture (and the goldens) instead of editing it"
    )


def test_generate_reproduces_committed_goldens_and_readme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``generate()`` re-derives the committed goldens and the README byte for byte.

    Reading the generator's own output (rather than a committed file) is what
    covers ``project_reference`` and ``generate()`` itself.
    """
    monkeypatch.setattr(generator, "FIXTURE_DIR", tmp_path)
    generator.generate()

    for name in NAMES:
        committed = GOLDEN_DIR / f"{name}.json"
        regenerated = tmp_path / "goldens" / f"{name}.json"
        assert committed.exists(), f"goldens/{name}.json is not committed"
        assert regenerated.read_text(encoding="utf-8") == committed.read_text(encoding="utf-8"), (
            f"goldens/{name}.json no longer matches the generator's projection; "
            "regenerate the fixture (and the goldens) instead of editing it"
        )

    regenerated_readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert regenerated_readme == (FIXTURE_DIR / "README.md").read_text(encoding="utf-8"), (
        "the fixture README no longer matches the generator; regenerate it"
    )
