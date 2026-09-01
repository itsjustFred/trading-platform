"""Helpers for discovering and editing strategy assets.

Listing/looking up strategies never executes strategy files: metadata is
derived by parsing the module AST. Importing a strategy is reserved for the
explicit :func:`load_strategy_module` entry point used by real backtest runs.
"""

from __future__ import annotations

import ast
import importlib.util
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.models import StrategyType

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STRATEGIES_DIR = PROJECT_ROOT / "strategies"


@dataclass(frozen=True)
class StrategyEntry:
    slug: str
    file_path: Path
    relative_file: str
    directory: Path
    documentation_path: Path | None
    meta: dict[str, Any]

    @property
    def display_name(self) -> str:
        return str(self.meta.get("name") or self.slug)

    @property
    def engine(self) -> str:
        configured = str(self.meta.get("engine") or "").strip().lower()
        if configured:
            return configured
        return "pynecore" if self.file_path.stem.startswith("pynecore_") else "vectorbt"

    @property
    def created_at(self) -> str:
        try:
            return datetime.fromtimestamp(
                self.file_path.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        except OSError:
            return ""


def iter_strategy_files(strategies_dir: Path = DEFAULT_STRATEGIES_DIR) -> list[Path]:
    if not strategies_dir.exists():
        return []

    files: list[Path] = []
    for path in strategies_dir.rglob("*.py"):
        if path.name.startswith("__") or path.stem.endswith("_context"):
            continue
        if path.parent != strategies_dir and path.parent.name != path.stem:
            continue
        files.append(path)
    return sorted(files)


def _relative_strategy_path(path: Path, strategies_dir: Path) -> Path | None:
    try:
        return path.resolve().relative_to(strategies_dir.resolve())
    except ValueError:
        return None


def strategy_slug_for_path(path: Path, strategies_dir: Path = DEFAULT_STRATEGIES_DIR) -> str:
    relative = _relative_strategy_path(path, strategies_dir)
    if relative and len(relative.parts) >= 2 and relative.parts[-1] == f"{relative.parts[-2]}.py":
        return relative.parts[-2]
    if path.parent.name == path.stem:
        return path.parent.name
    return path.stem


def strategy_relative_file(path: Path, strategies_dir: Path = DEFAULT_STRATEGIES_DIR) -> str:
    relative = _relative_strategy_path(path, strategies_dir)
    return relative.as_posix() if relative else path.name


def strategy_documentation_path(
    path: Path, strategies_dir: Path = DEFAULT_STRATEGIES_DIR
) -> Path | None:
    readme_path = path.parent / "README.md"
    if readme_path.exists():
        return readme_path

    legacy_md = path.with_suffix(".md")
    if legacy_md.exists():
        return legacy_md

    folder_md = path.parent / f"{path.stem}.md"
    if path.parent != strategies_dir and folder_md.exists():
        return folder_md
    return None


def load_strategy_module(path: Path, strategies_dir: Path = DEFAULT_STRATEGIES_DIR):
    """Explicitly import a strategy module (executes the file).

    Only real backtest/engine runs should call this: listing or looking up
    strategies uses :func:`load_strategy_meta`, which parses instead.
    """
    module_name = f"strategy_{strategy_slug_for_path(path, strategies_dir).replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load strategy module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _strategy_meta_literal(source: str) -> dict[str, Any] | None:
    """Return the STRATEGY_META literal parsed from source, or None.

    Never executes the file: the assignment is located in the module AST and
    decoded with ``ast.literal_eval``. Dynamic (non-literal) metadata returns
    None so callers can skip the strategy instead of importing it.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "STRATEGY_META" for target in targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            return None
        return dict(value) if isinstance(value, dict) else None
    return None


def load_strategy_meta(path: Path, strategies_dir: Path = DEFAULT_STRATEGIES_DIR) -> dict[str, Any]:
    """Read a strategy's STRATEGY_META without executing the strategy file."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    meta = _strategy_meta_literal(source)
    return dict(meta) if isinstance(meta, dict) else {}


def _strategy_type_from_meta(meta: dict[str, Any]) -> StrategyType | None:
    candidates: list[Any] = [
        meta.get("strategy_type"),
        meta.get("type"),
    ]

    tags = meta.get("tags")
    if isinstance(tags, dict):
        candidates.extend(
            [
                tags.get("strategy_type"),
                tags.get("type"),
            ]
        )

    normalized_tokens: set[str] = set()
    for candidate in candidates:
        if isinstance(candidate, (list, tuple, set)):
            normalized_tokens.update(_identifier_key(str(item)) for item in candidate)
        elif candidate is not None:
            normalized_tokens.add(_identifier_key(str(candidate)))

    if "signal processor" in normalized_tokens:
        return StrategyType.SIGNAL_PROCESSOR
    if "market data" in normalized_tokens:
        return StrategyType.MARKET_DATA
    return None


def detect_strategy_type(
    path: Path,
    strategies_dir: Path = DEFAULT_STRATEGIES_DIR,
) -> StrategyType:
    """Detect the strategy type from parsed metadata, without executing the file.

    Signal processors must declare themselves via STRATEGY_META
    (``strategy_type``/``type``, optionally under ``tags``). Executing strategy
    files just to sniff a class name is intentionally not done here.
    """
    meta = load_strategy_meta(path, strategies_dir)
    detected = _strategy_type_from_meta(meta)
    if detected is not None:
        return detected
    return StrategyType.MARKET_DATA


def iter_strategy_entries(strategies_dir: Path = DEFAULT_STRATEGIES_DIR) -> list[StrategyEntry]:
    entries: list[StrategyEntry] = []
    for path in iter_strategy_files(strategies_dir):
        meta = load_strategy_meta(path, strategies_dir)
        if not meta:
            continue
        entries.append(
            StrategyEntry(
                slug=strategy_slug_for_path(path, strategies_dir),
                file_path=path,
                relative_file=strategy_relative_file(path, strategies_dir),
                directory=path.parent,
                documentation_path=strategy_documentation_path(path, strategies_dir),
                meta=meta,
            )
        )
    return entries


def _normalized_identifier(value: str) -> list[str]:
    return "".join(ch.lower() if ch.isalnum() else " " for ch in value).split()


def _identifier_key(value: str) -> str:
    return " ".join(_normalized_identifier(value))


def strategy_identifier_aliases(entry: StrategyEntry) -> set[str]:
    aliases = {
        entry.slug,
        entry.relative_file,
        entry.file_path.name,
        entry.file_path.stem,
        entry.display_name,
    }
    return {_identifier_key(alias) for alias in aliases if alias}


def matches_strategy_identifier(entry: StrategyEntry, identifier: str) -> bool:
    if not identifier:
        return False
    return _identifier_key(identifier) in strategy_identifier_aliases(entry)


def find_strategy_entry(
    identifier: str, strategies_dir: Path = DEFAULT_STRATEGIES_DIR
) -> StrategyEntry | None:
    for entry in iter_strategy_entries(strategies_dir):
        if matches_strategy_identifier(entry, identifier):
            return entry
    return None


def read_strategy_documentation(entry: StrategyEntry) -> str:
    if not entry.documentation_path or not entry.documentation_path.exists():
        return ""
    return entry.documentation_path.read_text(encoding="utf-8")


def update_strategy_display_name(
    strategy_file: Path, new_name: str, strategies_dir: Path = DEFAULT_STRATEGIES_DIR
) -> dict[str, Any]:
    source = strategy_file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(strategy_file))
    line_offsets = _line_offsets(source)

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "STRATEGY_META"
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Dict):
            break
        for key, value in zip(node.value.keys, node.value.values, strict=False):
            if not isinstance(key, ast.Constant) or key.value != "name":
                continue
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            start = _offset_from_position(line_offsets, value.lineno, value.col_offset)
            end = _offset_from_position(line_offsets, value.end_lineno, value.end_col_offset)
            updated = source[:start] + repr(new_name) + source[end:]
            strategy_file.write_text(updated, encoding="utf-8")
            entry = find_strategy_entry(
                strategy_slug_for_path(strategy_file, strategies_dir),
                strategies_dir,
            )
            return entry.meta if entry else {"name": new_name}
        break
    raise ValueError(f"Could not update STRATEGY_META.name in {strategy_file}")


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    running = 0
    for line in source.splitlines(keepends=True):
        running += len(line)
        offsets.append(running)
    return offsets


def _offset_from_position(offsets: list[int], lineno: int | None, col: int | None) -> int:
    if lineno is None or col is None:
        raise ValueError("Missing AST source positions")
    return offsets[lineno - 1] + col
