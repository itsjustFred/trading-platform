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


def test_strategy_folder_auxiliary_dirs_are_not_strategies(tmp_path: Path) -> None:
    """``mt5/``, ``reference/`` and ``_template/`` never load as strategies."""
    from shared.strategy_registry import iter_strategy_entries, iter_strategy_files

    strategies_dir = tmp_path / "strategies"
    strategy_dir = strategies_dir / "foldered_strategy"
    strategy_dir.mkdir(parents=True)
    (strategy_dir / "foldered_strategy.py").write_text(
        "STRATEGY_META = {'name': 'Folded Strategy'}\n",
        encoding="utf-8",
    )
    (strategy_dir / "SOURCE.md").write_text("# source\n", encoding="utf-8")
    (strategy_dir / "__init__.py").write_text("", encoding="utf-8")

    mt5_dir = strategy_dir / "mt5"
    mt5_dir.mkdir()
    (mt5_dir / "mt5.py").write_text(
        "STRATEGY_META = {'name': 'MT5 Helper'}\n",
        encoding="utf-8",
    )
    reference_dir = strategy_dir / "reference"
    reference_dir.mkdir()
    (reference_dir / "reference.py").write_text(
        "STRATEGY_META = {'name': 'Reference Helper'}\n",
        encoding="utf-8",
    )
    template_dir = strategies_dir / "_template"
    template_dir.mkdir()
    (template_dir / "_template.py").write_text(
        "STRATEGY_META = {'name': 'Template'}\n",
        encoding="utf-8",
    )

    assert iter_strategy_files(strategies_dir) == [strategy_dir / "foldered_strategy.py"]
    entries = iter_strategy_entries(strategies_dir)
    assert [entry.slug for entry in entries] == ["foldered_strategy"]
    assert entries[0].display_name == "Folded Strategy"


def test_real_strategies_tree_nested_dirs_are_declared_auxiliary() -> None:
    """Every directory nested inside a strategy folder must be ignored by name.

    ``iter_strategy_files`` enumerates any ``<dir>/<dir>.py`` pair outside
    ``_STRATEGY_IGNORED_DIRS``; a new auxiliary folder (``data/``,
    ``fixtures/``, …) added without extending the set would be silently queued
    as a runnable strategy. This pins the reserved names against the real
    tree so adding one fails here instead of in a backtest queue.
    """
    from shared.strategy_registry import _STRATEGY_IGNORED_DIRS, DEFAULT_STRATEGIES_DIR

    undeclared = sorted(
        dirpath.name
        for dirpath in DEFAULT_STRATEGIES_DIR.rglob("*")
        if dirpath.is_dir()
        and len(dirpath.relative_to(DEFAULT_STRATEGIES_DIR).parts) >= 2
        and not dirpath.name.startswith((".", "__"))
        and dirpath.name not in _STRATEGY_IGNORED_DIRS
    )

    assert undeclared == []


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
    from shared.models import StrategyType
    from shared.strategy_registry import detect_strategy_type, load_strategy_meta

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


def test_vectorbt_only_signal_keys_detects_dict_and_subscript(tmp_path: Path) -> None:
    """``entry_stop`` counts as a dict-literal key or subscript index only."""
    from shared.strategy_registry import vectorbt_only_signal_keys

    strategy_dir = tmp_path / "stop_strategy"
    strategy_dir.mkdir()
    (strategy_dir / "stop_strategy.py").write_text(
        "import pandas as pd\n"
        "def generate_signals(df, params):\n"
        "    indicators = {'stop_loss': pd.Series([1.0]), 'entry_stop': pd.Series([2.0])}\n"
        "    indicators['entry_stop'] = indicators['entry_stop'] * 2\n"
        "    return pd.Series([True]), pd.Series([False]), indicators\n",
        encoding="utf-8",
    )

    assert vectorbt_only_signal_keys(strategy_dir / "stop_strategy.py") == frozenset(
        {"entry_stop"}
    )


def test_vectorbt_only_signal_keys_covers_call_publication_forms(tmp_path: Path) -> None:
    """``dict(entry_stop=...)`` and ``setdefault("entry_stop", ...)`` also count."""
    from shared.strategy_registry import vectorbt_only_signal_keys

    strategy_dir = tmp_path / "call_forms"
    strategy_dir.mkdir()
    (strategy_dir / "call_forms.py").write_text(
        "def generate_signals(df, params):\n"
        "    indicators = dict(entry_stop=1.0, stop_loss=2.0)\n"
        "    indicators.setdefault('entry_stop', 3.0)\n"
        "    return None, None, indicators\n",
        encoding="utf-8",
    )

    assert vectorbt_only_signal_keys(strategy_dir / "call_forms.py") == frozenset(
        {"entry_stop"}
    )


def test_vectorbt_only_signal_keys_ignores_mentions_and_other_keys(tmp_path: Path) -> None:
    """A STRATEGY_META note or variable name mentioning the key must not match."""
    from shared.strategy_registry import vectorbt_only_signal_keys

    strategy_dir = tmp_path / "mention_strategy"
    strategy_dir.mkdir()
    (strategy_dir / "mention_strategy.py").write_text(
        "STRATEGY_META = {'name': 'Mention', 'notes': 'no entry_stop fills here'}\n"
        "entry_stop_distance = 1.5\n"
        "def generate_signals(df, params):\n"
        "    indicators = {'stop_distance': entry_stop_distance, 'exit_reason': 'x'}\n"
        "    return None, None, indicators\n",
        encoding="utf-8",
    )

    assert vectorbt_only_signal_keys(strategy_dir / "mention_strategy.py") == frozenset()


def test_vectorbt_only_signal_keys_real_registry() -> None:
    """The shipped registry: only us100_scalper emits a vectorbt-only key."""
    from shared.strategy_registry import (
        VECTORBT_ONLY_SIGNAL_KEYS,
        iter_strategy_files,
        strategy_slug_for_path,
        vectorbt_only_signal_keys,
    )

    # exit_reason is a replaceable label, not a fill-semantics input — adding
    # it would exclude nearly every strategy (pre-mortem finding, PR #447).
    assert "exit_reason" not in VECTORBT_ONLY_SIGNAL_KEYS

    strategies_dir = Path(__file__).resolve().parents[1] / "strategies"
    hits = {
        strategy_slug_for_path(path, strategies_dir)
        for path in iter_strategy_files(strategies_dir)
        if vectorbt_only_signal_keys(path)
    }

    assert hits == {"us100_scalper"}


def test_atr_sizing_keys_without_stop_flags_atr_only(tmp_path: Path) -> None:
    """``atr*`` keys with no stop_loss/sl_price are the vectorbt ATR fallback."""
    from shared.strategy_registry import atr_sizing_keys_without_stop

    strategy_dir = tmp_path / "atr_only"
    strategy_dir.mkdir()
    (strategy_dir / "atr_only.py").write_text(
        "import pandas as pd\n"
        "def generate_signals(df, params):\n"
        "    indicators = {'atr': pd.Series([1.0]), 'atr_value': pd.Series([2.0])}\n"
        "    return pd.Series([True]), pd.Series([False]), indicators\n",
        encoding="utf-8",
    )

    assert atr_sizing_keys_without_stop(strategy_dir / "atr_only.py") == frozenset(
        {"atr", "atr_value"}
    )


def test_atr_sizing_keys_without_stop_respects_stop_keys(tmp_path: Path) -> None:
    """A stop_loss/sl_price key keeps vectorbt off the ATR fallback."""
    from shared.strategy_registry import atr_sizing_keys_without_stop

    strategy_dir = tmp_path / "atr_with_stop"
    strategy_dir.mkdir()
    for name, stop_key in (
        ("with_stop_loss", "stop_loss"),
        ("with_sl_price", "sl_price"),
        ("with_sizing_sl_price", "sizing_sl_price"),
        ("with_sizing_sl", "sizing_sl"),
    ):
        (strategy_dir / f"{name}.py").write_text(
            "def generate_signals(df, params):\n"
            f"    indicators = {{'atr': 1.0, '{stop_key}': 2.0}}\n"
            "    return None, None, indicators\n",
            encoding="utf-8",
        )
        assert atr_sizing_keys_without_stop(strategy_dir / f"{name}.py") == frozenset()


def test_atr_sizing_keys_without_stop_real_registry() -> None:
    """No shipped strategy relies on the vectorbt ATR fallback today."""
    from shared.strategy_registry import (
        atr_sizing_keys_without_stop,
        iter_strategy_files,
    )

    strategies_dir = Path(__file__).resolve().parents[1] / "strategies"
    hits = {
        path.name
        for path in iter_strategy_files(strategies_dir)
        if atr_sizing_keys_without_stop(path)
    }

    assert hits == set()


def test_stop_level_keys_match_vectorbt_level_aliases() -> None:
    """``_STOP_LEVEL_SIGNAL_KEYS`` must cover every stop spelling vectorbt honours.

    The adapter resolves sizing distance from ``sizing_stop``/``stop_loss``
    (``_LEVEL_ALIASES``) before falling back to ``atr*`` — a missing sizing
    spelling here would wrongly exclude a strategy that has a real stop.
    """
    from runner.engines.vectorbt_adapter import _LEVEL_ALIASES
    from shared.strategy_registry import _STOP_LEVEL_SIGNAL_KEYS

    expected = frozenset(
        alias
        for kind in ("stop_loss", "sizing_stop")
        for alias in _LEVEL_ALIASES[kind]
    )
    assert _STOP_LEVEL_SIGNAL_KEYS == expected


def test_vectorbt_only_keys_cover_execution_key_diff() -> None:
    """Every vectorbt execution key native ignores must be registered or excused.

    ``_EXECUTION_SIGNAL_KEYS`` (the adapter's ``require_explicit_roles``
    allowlist) minus the keys the native engine reads must equal
    ``VECTORBT_ONLY_SIGNAL_KEYS`` — adding a new vectorbt-only execution key
    without registering it fails here. ``atr`` and ``exit_reason`` are the two
    documented exceptions: ``atr*`` is handled conditionally by
    ``atr_sizing_keys_without_stop`` (it only matters with no stop key), and
    ``exit_reason`` is a label native replaces with its own reasons.
    """
    from runner.engines.vectorbt_adapter import _EXECUTION_SIGNAL_KEYS
    from shared.strategy_registry import VECTORBT_ONLY_SIGNAL_KEYS

    # Signal keys the native engine reads (pinned literal — the native-read
    # set is not mechanically derivable): entries/exits frame columns,
    # _extract_levels stop/tp spellings, _resolve_direction, row.get
    # stop_distance, row.get exit_price/exit_order_type in trading_engine.py.
    native_read_keys = frozenset(
        {
            "entries",
            "exits",
            "stop_loss",
            "sl_price",
            "take_profit",
            "tp_price",
            "direction",
            "stop_distance",
            "exit_price",
            "exit_order_type",
        }
    )
    conditional_or_label_keys = frozenset({"atr", "exit_reason"})

    unhandled = (
        _EXECUTION_SIGNAL_KEYS - native_read_keys - conditional_or_label_keys
    )
    assert unhandled == VECTORBT_ONLY_SIGNAL_KEYS


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
