"""Static strategy registry tests (Cleanup 20-D).

Listing/looking up strategies must not execute strategy files: the registry
derives metadata by parsing, and only explicit load entry points used for
actual backtests may import a module.
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest


@pytest.fixture
def no_strategy_imports(monkeypatch):
    """Block exec/import of strategy files for the duration of a test."""

    original_import = builtins.__import__
    blocked: list[str] = []

    def _guard(name, globals=None, locals=None, fromlist=(), level=0):
        # importlib used by the registry goes through __import__ too; block
        # any import whose name matches a strategy module naming pattern.
        if name.startswith("strategy_"):
            blocked.append(name)
            raise AssertionError(f"strategy module import attempted: {name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _guard)
    return blocked


def test_iter_strategy_entries_does_not_execute_strategy_files(
    tmp_path: Path, no_strategy_imports
) -> None:
    """Listing strategies parses instead of importing."""
    from shared.strategy_registry import iter_strategy_entries

    strategy_dir = tmp_path / "example_strategy"
    strategy_dir.mkdir()
    (strategy_dir / "example_strategy.py").write_text(
        "STRATEGY_META = {'name': 'Example Strategy', 'tags': {'type': ['trend']}}\n",
        encoding="utf-8",
    )

    entries = iter_strategy_entries(strategy_dir)

    assert len(entries) == 1
    assert entries[0].slug == "example_strategy"
    assert entries[0].display_name == "Example Strategy"
    assert not no_strategy_imports


def test_find_strategy_entry_does_not_execute_strategy_files(
    tmp_path: Path, no_strategy_imports
) -> None:
    """Lookup by identifier parses instead of importing."""
    from shared.strategy_registry import find_strategy_entry

    strategy_dir = tmp_path / "lookup_strategy"
    strategy_dir.mkdir()
    (strategy_dir / "lookup_strategy.py").write_text(
        "STRATEGY_META = {'name': 'Lookup Strategy'}\n",
        encoding="utf-8",
    )

    entry = find_strategy_entry("lookup_strategy", strategy_dir)

    assert entry is not None
    assert entry.display_name == "Lookup Strategy"
    assert not no_strategy_imports


def test_load_strategy_meta_is_static(tmp_path: Path) -> None:
    """load_strategy_meta returns parsed STRATEGY_META without importing."""
    from shared.strategy_registry import load_strategy_meta

    strategy_dir = tmp_path / "static_meta"
    strategy_dir.mkdir()
    (strategy_dir / "static_meta.py").write_text(
        "STRATEGY_META = {'name': 'Static Meta', 'engine': 'native'}\n",
        encoding="utf-8",
    )

    meta = load_strategy_meta(strategy_dir / "static_meta.py", strategy_dir)

    assert meta == {"name": "Static Meta", "engine": "native"}


def test_load_strategy_meta_without_literal(tmp_path: Path) -> None:
    """Dynamic STRATEGY_META yields {} and an explicit-load error is not raised."""
    from shared.strategy_registry import load_strategy_meta

    strategy_dir = tmp_path / "dynamic_meta"
    strategy_dir.mkdir()
    (strategy_dir / "dynamic_meta.py").write_text(
        "import os\nSTRATEGY_META = {'name': os.environ.get('X', 'y')}\n",
        encoding="utf-8",
    )

    assert load_strategy_meta(strategy_dir / "dynamic_meta.py", strategy_dir) == {}


def test_detect_strategy_type_is_static(tmp_path: Path) -> None:
    """Type detection reads STRATEGY_META statically; falls back to MARKET_DATA."""
    from shared.models import StrategyType
    from shared.strategy_registry import detect_strategy_type

    strategy_dir = tmp_path / "typed_strategy"
    strategy_dir.mkdir()
    (strategy_dir / "typed_strategy.py").write_text(
        "STRATEGY_META = {'name': 'Typed', 'strategy_type': 'signal_processor'}\n",
        encoding="utf-8",
    )

    detected = detect_strategy_type(strategy_dir / "typed_strategy.py", strategy_dir)
    assert detected == StrategyType.SIGNAL_PROCESSOR

    # A signal-processor *class* is detected statically from the AST class shape
    # (name + process_signal method) — the file is still never executed.
    class_dir = tmp_path / "class_strategy"
    class_dir.mkdir()
    (class_dir / "class_strategy.py").write_text(
        "class ExampleSignalProcessor:\n"
        "    def process_signal(self, raw_signal, market_data):\n"
        "        return raw_signal\n",
        encoding="utf-8",
    )
    assert detect_strategy_type(class_dir / "class_strategy.py", class_dir) == (
        StrategyType.SIGNAL_PROCESSOR
    )

    # A class without the process_signal method is not a signal processor.
    bare_dir = tmp_path / "bare_strategy"
    bare_dir.mkdir()
    (bare_dir / "bare_strategy.py").write_text(
        "class ExampleSignalProcessor:\n"
        "    def run(self):\n"
        "        return None\n",
        encoding="utf-8",
    )
    assert detect_strategy_type(bare_dir / "bare_strategy.py", bare_dir) == (
        StrategyType.MARKET_DATA
    )


def test_strategy_reads_are_contained_to_strategies_dir(tmp_path: Path) -> None:
    """Path traversal / outside files are never read by listing-time lookups (S8707)."""
    from shared.strategy_registry import detect_strategy_type, load_strategy_meta
    from shared.models import StrategyType

    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    (strategy_dir / "contained.py").write_text(
        "STRATEGY_META = {'name': 'Contained', 'strategy_type': 'signal_processor'}\n",
        encoding="utf-8",
    )

    # A file outside strategies_dir is not read, even when pointed at directly.
    outside = tmp_path / "outside.py"
    outside.write_text(
        "STRATEGY_META = {'name': 'Outside', 'strategy_type': 'signal_processor'}\n",
        encoding="utf-8",
    )
    assert load_strategy_meta(outside, strategy_dir) == {}
    assert detect_strategy_type(outside, strategy_dir) == StrategyType.MARKET_DATA

    # A traversal path that resolves outside is rejected as well.
    traversal = strategy_dir / ".." / "outside.py"
    assert load_strategy_meta(traversal, strategy_dir) == {}
    assert detect_strategy_type(traversal, strategy_dir) == StrategyType.MARKET_DATA

    # A symlink escaping the tree is rejected; a contained symlink is followed.
    escaping_link = strategy_dir / "escape.py"
    escaping_link.symlink_to(outside)
    assert load_strategy_meta(escaping_link, strategy_dir) == {}

    linked_name = strategy_dir / "linked.py"
    linked_name.symlink_to(strategy_dir / "contained.py")
    assert load_strategy_meta(linked_name, strategy_dir) == {
        "name": "Contained",
        "strategy_type": "signal_processor",
    }


def test_iter_strategy_entries_skips_dynamic_meta(tmp_path: Path) -> None:
    """Strategies with non-literal STRATEGY_META are skipped, not imported."""
    from shared.strategy_registry import iter_strategy_entries

    dynamic_dir = tmp_path / "dynamic_strategy"
    dynamic_dir.mkdir()
    (dynamic_dir / "dynamic_strategy.py").write_text(
        "import os\nSTRATEGY_META = {'name': os.environ.get('STRATEGY_NAME', 'never')}\n",
        encoding="utf-8",
    )

    assert iter_strategy_entries(dynamic_dir) == []


def test_load_strategy_module_remains_explicit(tmp_path: Path) -> None:
    """The explicit load entry point still exists for real backtest runs."""
    from shared.strategy_registry import load_strategy_module

    strategy_dir = tmp_path / "explicit_strategy"
    strategy_dir.mkdir()
    (strategy_dir / "explicit_strategy.py").write_text(
        "MARKER = 'loaded'\nSTRATEGY_META = {'name': 'Explicit'}\n",
        encoding="utf-8",
    )

    module = load_strategy_module(strategy_dir / "explicit_strategy.py", strategy_dir)
    assert module.MARKER == "loaded"
