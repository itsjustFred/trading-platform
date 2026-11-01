"""The operator scripts write a caller-supplied path - and only that path.

Each sink now rebuilds its target from a constant root plus components that pass through
`os.path.basename`, which is what makes the confinement visible to a reader (and to a taint
analyzer) instead of hidden behind a helper. The rebuild must be the identity for every legitimate
path, so these cases pin both halves: a nested output lands exactly where it was asked for, and a
path that climbs out of the root is refused before anything is created.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from scripts._safe_path import UnsafePathError

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    path = REPO_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"script_{path.stem}", str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: `@dataclass` resolves its own module through `sys.modules`, so a
    # module that is executed without being registered cannot define one.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def harvester(tmp_path: Path, monkeypatch):
    module = _load("adaptive_harvester.py")
    monkeypatch.setattr(module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path / "repo")
    return module


def test_the_harvester_writes_a_nested_output_where_it_was_asked(harvester, tmp_path: Path) -> None:
    target = tmp_path / "data" / "reports" / "harvest.json"

    harvester._write_json_atomic(target, {"symbol": "EURUSD"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"symbol": "EURUSD"}
    assert not list(target.parent.glob("*.tmp")), "the atomic write must not leave its temp file"


def test_the_harvester_resolves_a_relative_output_under_the_data_dir(
    harvester, tmp_path: Path
) -> None:
    """A relative `--output` belongs to the data directory, not to the caller's cwd.

    That is the pre-existing contract of `_safe_write_path` (it tries DATA_DIR first); the rebuild
    must not change where the file lands.
    """
    harvester._write_json_atomic(Path("out/harvest.json"), {"symbol": "EURUSD"})

    assert json.loads((tmp_path / "data" / "out" / "harvest.json").read_text()) == {
        "symbol": "EURUSD"
    }


def test_the_harvester_refuses_a_traversal_output(harvester, tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        harvester._write_json_atomic(tmp_path / "data" / ".." / "escape.json", {})

    assert not (tmp_path / "escape.json").exists()


def test_the_harvester_refuses_an_output_outside_both_roots(harvester, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.json"

    with pytest.raises(UnsafePathError, match="outside"):
        harvester._write_json_atomic(outside, {})

    assert not outside.exists()


def test_the_backtest_rewrite_writes_a_nested_path_where_it_was_asked(tmp_path: Path) -> None:
    module = _load("recalculate_backtest_stats.py")
    root = tmp_path / "backtests"
    (root / "2024").mkdir(parents=True)
    target = root / "2024" / "result.trades.json"

    module._save_json_payload(target, {"stats": {"sharpe_ratio": 1.5}}, root=root)

    assert json.loads(target.read_text(encoding="utf-8")) == {"stats": {"sharpe_ratio": 1.5}}


def test_the_backtest_rewrite_refuses_a_path_outside_its_root(tmp_path: Path) -> None:
    module = _load("recalculate_backtest_stats.py")
    root = tmp_path / "backtests"
    root.mkdir()
    outside = tmp_path / "outside.json"

    with pytest.raises(UnsafePathError, match="outside"):
        module._save_json_payload(outside, {}, root=root)

    assert not outside.exists()
