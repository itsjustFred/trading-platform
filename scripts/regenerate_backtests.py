"""Regenerate the backtest corpus from the strategies directory.

For every strategy file in the strategies directory this script picks a
symbol/timeframe from the local OHLCV parquet index, runs the matching backtest
engine and writes the result to SQLite plus ``DATA_DIR/backtests/*.trades.json``.

The previous corpus is destroyed in the process, so the script is guarded:

- ``--dry-run`` is the default: prints which strategy would run on which
  symbol/timeframe and touches neither SQLite nor the JSON files.
- A confirmation prompt is required unless ``--yes`` is passed.
- Before the first deletion, every SQLite backtest row set and every
  ``*.trades.json`` file is copied into a backup directory outside
  ``DATA_DIR/backtests``. If the run produces **no** successful regeneration, the
  previous corpus is restored automatically, so a failing batch can never leave
  the repository without historical results. Use
  ``scripts.regenerate_backtests.restore_backups`` to restore a backup manually.

Usage:
    python -m scripts.regenerate_backtests                     # dry run (default)
    python -m scripts.regenerate_backtests --yes               # regenerate, with backup
    python -m scripts.regenerate_backtests --yes --strategy X  # limit to one strategy
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd
import pyarrow.parquet as pq
from dotenv import load_dotenv
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.engine import make_url

from runner.cli import get_engine
from runner.engines.base import BacktestConfig, resolve_strategy_parameters
from runner.stats import compute_stats_for_result
from shared.database import (
    BacktestRecord,
    DrawingRecord,
    EquityPoint,
    IndicatorRecord,
    TradeRecord,
    get_session,
    init_db,
    save_result_to_sqlite,
)
from shared.models import StrategyType
from shared.strategy_registry import (
    DEFAULT_STRATEGIES_DIR,
    detect_strategy_type,
    iter_strategy_files,
    load_strategy_meta,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

_data_dir_env = os.environ.get("DATA_DIR") or os.environ.get("TRADING_DATA_DIR")
DATA_DIR = (
    Path(_data_dir_env).expanduser().resolve()
    if _data_dir_env
    else (PROJECT_ROOT / "data").resolve()
)
BACKTESTS_DIR = DATA_DIR / "backtests"
TIMEFRAME_PRIORITY = ["1h", "4h", "1d", "15m", "5m", "30m", "1m"]
TIMEFRAME_PATTERN = re.compile(r"^(?:tick|s1|mn1|\d+[smhdw])$", re.IGNORECASE)

_SQLITE_BACKUP_TABLES = (
    "backtests",
    "trades",
    "equity_points",
    "drawings",
    "indicators",
)


def _looks_like_timeframe(value: str) -> bool:
    return bool(TIMEFRAME_PATTERN.match(str(value or "").strip().lower()))


def _parse_symbol_timeframe(parquet_path: Path, ohlcv_root: Path) -> tuple[str, str] | None:
    try:
        rel = parquet_path.relative_to(ohlcv_root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 4:
        return None

    stem = parquet_path.stem
    if len(parts) >= 5:
        symbol = parts[-2]
        timeframe = stem if _looks_like_timeframe(stem) else "1d"
        return symbol.upper(), timeframe.lower()

    if _looks_like_timeframe(stem):
        symbol = parts[-2]
        timeframe = stem
        return symbol.upper(), timeframe.lower()

    return stem.upper(), "1d"


def _build_symbol_index(data_dir: Path) -> dict[str, dict[str, list[Path]]]:
    ohlcv_root = data_dir / "ohlcv"
    index: dict[str, dict[str, list[Path]]] = {}
    if not ohlcv_root.exists():
        return index

    for parquet_path in sorted(ohlcv_root.rglob("*.parquet")):
        parsed = _parse_symbol_timeframe(parquet_path, ohlcv_root)
        if parsed is None:
            continue
        symbol, timeframe = parsed
        index.setdefault(symbol, {}).setdefault(timeframe, []).append(parquet_path)
    return index


ASSET_CLASS_CANDIDATES: list[tuple[tuple[str, ...], list[str]]] = [
    (("forex", "metal"), ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"]),
    (("equit", "stock"), ["SPY", "AAPL", "QQQ"]),
    (("etf",), ["SPY", "QQQ", "GLD"]),
    (("index", "future"), ["US500", "SPY", "VIX"]),
    (("multi",), ["SPY", "QQQ", "EURUSD", "XAUUSD"]),
]
DEFAULT_CANDIDATE_SYMBOLS = ["EURUSD", "XAUUSD", "SPY", "AAPL"]


def _asset_class_tokens(meta: dict[str, Any]) -> list[str]:
    """Normalize the strategy's ``asset_class`` tags into lowercase tokens."""
    tags = meta.get("tags", {}) if isinstance(meta, dict) else {}
    raw_assets = tags.get("asset_class", []) if isinstance(tags, dict) else []
    if isinstance(raw_assets, str):
        return [raw_assets.lower()]
    if isinstance(raw_assets, list):
        return [str(item).lower() for item in raw_assets]
    return []


def _candidate_symbols(meta: dict[str, Any]) -> list[str]:
    assets = _asset_class_tokens(meta)
    ordered: list[str] = []
    for tokens, symbols in ASSET_CLASS_CANDIDATES:
        if any(token in asset for asset in assets for token in tokens):
            ordered.extend(symbols)
    if not ordered:
        ordered.extend(DEFAULT_CANDIDATE_SYMBOLS)

    seen: set[str] = set()
    return [symbol for symbol in ordered if not (symbol in seen or seen.add(symbol))]


def _select_symbol_timeframe(
    meta: dict[str, Any], symbol_index: dict[str, dict[str, list[Path]]]
) -> tuple[str, str, Path] | None:
    for symbol in _candidate_symbols(meta):
        timeframe_map = symbol_index.get(symbol)
        if not timeframe_map:
            continue
        for timeframe in TIMEFRAME_PRIORITY:
            if timeframe in timeframe_map and timeframe_map[timeframe]:
                return symbol, timeframe, timeframe_map[timeframe][0]
        first_timeframe = min(timeframe_map)
        return symbol, first_timeframe, timeframe_map[first_timeframe][0]

    if not symbol_index:
        return None

    fallback_symbol = min(symbol_index)
    timeframe_map = symbol_index[fallback_symbol]
    if not timeframe_map:
        return None
    first_timeframe = min(timeframe_map)
    return fallback_symbol, first_timeframe, timeframe_map[first_timeframe][0]


def _select_timestamp_column(columns: list[str]) -> str | None:
    lowered = {column.lower(): column for column in columns}
    for candidate in ("timestamp", "datetime", "date", "time"):
        if candidate in lowered:
            return lowered[candidate]
    return None


def _read_parquet_date_range(parquet_path: Path) -> tuple[str, str]:
    parquet_file = pq.ParquetFile(parquet_path)
    metadata = parquet_file.metadata
    if metadata is None or metadata.num_rows == 0 or metadata.num_row_groups == 0:
        raise RuntimeError(f"File {parquet_path} has no rows")

    columns = list(parquet_file.schema_arrow.names)
    timestamp_column = _select_timestamp_column(columns)
    if timestamp_column is None:
        raise RuntimeError(f"No timestamp-like column in {parquet_path}")

    column_index = columns.index(timestamp_column)
    first_stats = metadata.row_group(0).column(column_index).statistics
    last_stats = metadata.row_group(metadata.num_row_groups - 1).column(column_index).statistics

    start_value = (
        first_stats.min
        if (first_stats is not None and getattr(first_stats, "has_min_max", False))
        else None
    )
    end_value = (
        last_stats.max
        if (last_stats is not None and getattr(last_stats, "has_min_max", False))
        else None
    )

    if start_value is None:
        first_group = parquet_file.read_row_group(0, columns=[timestamp_column])
        if first_group.num_rows == 0:
            raise RuntimeError(f"Could not determine start timestamp for {parquet_path}")
        start_value = first_group.column(0)[0].as_py()
    if end_value is None:
        last_group = parquet_file.read_row_group(
            metadata.num_row_groups - 1,
            columns=[timestamp_column],
        )
        if last_group.num_rows == 0:
            raise RuntimeError(f"Could not determine end timestamp for {parquet_path}")
        end_value = last_group.column(0)[last_group.num_rows - 1].as_py()

    start_ts = pd.to_datetime(start_value, utc=True, errors="coerce")
    end_ts = pd.to_datetime(end_value, utc=True, errors="coerce")
    if pd.isna(start_ts) or pd.isna(end_ts):
        raise RuntimeError(f"Could not parse timestamp range for {parquet_path}")
    return start_ts.date().isoformat(), end_ts.date().isoformat()


@dataclass
class RegenerationPlan:
    """What a regeneration run would do, without touching anything."""

    entries: list[dict[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    sqlite_backtests: int = 0
    json_backtests: int = 0
    no_data: bool = False

    @property
    def runnable(self) -> int:
        return len(self.entries)


@dataclass
class RegenerationOutcome:
    successes: int = 0
    skipped: int = 0
    failures: int = 0
    failure_messages: list[str] = field(default_factory=list)
    backup_dir: Path | None = None
    restored: bool = False


TRADES_JSON_GLOB = "*.trades.json"


def _existing_db_path() -> Path:
    """Resolve the SQLite database file backing the active engine.

    The engine is created at import time from the resolved DATA_DIR, so tests that
    swap ``shared.database.ENGINE`` need the path derived from the engine URL
    rather than the module-level ``DB_PATH`` constant. The URL is parsed with
    SQLAlchemy's own parser instead of string surgery — URL renderings differ
    between versions (relative ``sqlite:///x.db`` vs absolute
    ``sqlite:////abs/x.db``), and mis-deriving the path would silently back up
    and restore the wrong database file.
    """
    from shared.database import DB_PATH, ENGINE

    database = make_url(str(ENGINE.url)).database
    if database:
        engine_path = Path(database)
        if engine_path.is_absolute():
            return engine_path
    return DB_PATH


def _dump_sqlite_backup(db_path: Path, backup_dir: Path) -> Path | None:
    if not db_path.exists():
        return None
    destination = backup_dir / db_path.name
    shutil.copy2(db_path, destination)
    return destination


def _restore_sqlite_backup(db_path: Path, backup_file: Path) -> None:
    """Restore backtest tables from a backup database file.

    Only backtest-related tables are overwritten, so unrelated tables written
    during the failed run (live executions, background tasks, ...) survive.
    """
    if not backup_file.exists():
        return
    init_db()
    target = sqlite3.connect(db_path)
    source = sqlite3.connect(backup_file)
    try:
        for table in _SQLITE_BACKUP_TABLES:
            target.execute(f'DELETE FROM "{table}"')
            source_rows = _read_table_rows(source, (table,))
            table_rows = source_rows.get(table, [])
            if not table_rows:
                continue
            columns = list(table_rows[0])
            placeholders = ", ".join("?" for _ in columns)
            quoted = ", ".join(f'"{column}"' for column in columns)
            statement = f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})'
            target.executemany(
                statement,
                [tuple(row[column] for column in columns) for row in table_rows],
            )
        target.commit()
    finally:
        source.close()
        target.close()


def _read_table_rows(source: sqlite3.Connection, tables: Iterable[str]) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    for table in tables:
        try:
            cursor = source.execute(f'SELECT * FROM "{table}"')
            columns = [description[0] for description in cursor.description]
            rows[table] = [dict(zip(columns, row)) for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            rows[table] = []
    return rows


def _backup_json_files(backtests_dir: Path, backup_dir: Path) -> int:
    if not backtests_dir.exists():
        return 0
    copied = 0
    for path in backtests_dir.glob(TRADES_JSON_GLOB):
        destination = backup_dir / "backtests" / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied += 1
    return copied


def _restore_json_backups(backtests_dir: Path, backup_dir: Path) -> int:
    source_dir = backup_dir / "backtests"
    if not source_dir.exists():
        return 0
    backtests_dir.mkdir(parents=True, exist_ok=True)
    restored = 0
    for path in source_dir.glob(TRADES_JSON_GLOB):
        shutil.copy2(path, backtests_dir / path.name)
        restored += 1
    return restored


def restore_backups(backup_dir: Path, *, data_dir: Path | None = None) -> tuple[int, int]:
    """Restore a regeneration backup into ``data_dir``. Returns (sqlite, json) counts."""
    resolved_backup_dir = _validate_backup_dir(backup_dir)
    resolved_data_dir = data_dir if data_dir is not None else DATA_DIR
    db_path = _existing_db_path()
    sqlite_restored = 0
    json_restored = _restore_json_backups(resolved_data_dir / "backtests", resolved_backup_dir)
    backup_db = resolved_backup_dir / db_path.name
    if backup_db.exists():
        _restore_sqlite_backup(db_path, backup_db)
        sqlite_restored = 1
    return sqlite_restored, json_restored


def _purge_sqlite_backtests() -> None:
    with get_session() as session:
        session.exec(delete(TradeRecord))
        session.exec(delete(EquityPoint))
        session.exec(delete(DrawingRecord))
        session.exec(delete(IndicatorRecord))
        session.exec(delete(BacktestRecord))
        session.commit()


def _purge_json_backtests(backtests_dir: Path) -> int:
    if not backtests_dir.exists():
        return 0
    deleted = 0
    for path in backtests_dir.glob(TRADES_JSON_GLOB):
        path.unlink()
        deleted += 1
    return deleted


def _strategy_paths(strategies: list[str] | None) -> list[Path]:
    if strategies:
        return [Path(entry) for entry in strategies]
    return iter_strategy_files(DEFAULT_STRATEGIES_DIR)


def plan_regeneration(
    *,
    strategies: list[str] | None = None,
    data_dir: Path | None = None,
) -> RegenerationPlan:
    """Dry-run helper: describe the planned runs and the corpus at risk.

    Must be called *before* ``_purge_sqlite_backtests()``/``_purge_json_backtests()``
    for the reported ``sqlite_backtests``/``json_backtests`` counts to describe the
    live corpus — counting them afterwards always yields 0.
    """
    resolved_data_dir = data_dir if data_dir is not None else DATA_DIR
    backtests_dir = resolved_data_dir / "backtests"
    plan = RegenerationPlan(
        sqlite_backtests=_count_sqlite_backtests(),
        json_backtests=len(list(backtests_dir.glob(TRADES_JSON_GLOB)))
        if backtests_dir.exists()
        else 0,
    )

    symbol_index = _build_symbol_index(resolved_data_dir)
    if not symbol_index:
        plan.no_data = True
        plan.skipped.append(f"No OHLCV data found under {resolved_data_dir / 'ohlcv'}")
        return plan

    for strategy_path in _strategy_paths(strategies):
        strategy_type = detect_strategy_type(strategy_path, DEFAULT_STRATEGIES_DIR)
        if strategy_type == StrategyType.SIGNAL_PROCESSOR:
            plan.skipped.append(f"{strategy_path.name}: skipped signal_processor strategy")
            continue

        meta = load_strategy_meta(strategy_path, DEFAULT_STRATEGIES_DIR)
        selection = _select_symbol_timeframe(meta, symbol_index)
        if selection is None:
            plan.skipped.append(f"{strategy_path.name}: no available symbol/timeframe in DATA_DIR")
            continue

        symbol, timeframe, parquet_path = selection
        try:
            start_date, end_date = _read_parquet_date_range(parquet_path)
        except (OSError, ValueError, RuntimeError) as exc:
            plan.skipped.append(f"{strategy_path.name}: could not determine date range ({exc})")
            continue

        engine_name = "pynecore" if strategy_path.stem.startswith("pynecore_") else "vectorbt"
        plan.entries.append(
            {
                "strategy": strategy_path.stem,
                "engine": engine_name,
                "symbol": symbol,
                "timeframe": timeframe,
                "start": start_date,
                "end": end_date,
            }
        )
    return plan


def _count_sqlite_backtests() -> int:
    from shared.database import list_backtest_ids

    return len(list_backtest_ids())


def _run_strategy(
    entry: dict[str, str],
    strategy_path: Path,
    *,
    data_dir: Path,
    backtests_dir: Path,
) -> bool:
    """Run one planned strategy entry. Returns True on success."""
    engine_name = entry["engine"]
    strategy_name = entry["strategy"]
    engine = get_engine(engine_name)
    parameters = resolve_strategy_parameters(strategy_path, {})

    try:
        config = BacktestConfig(
            strategy_name=strategy_name,
            symbols=[entry["symbol"]],
            timeframe=entry["timeframe"],
            start_date=entry["start"],
            end_date=entry["end"],
            initial_capital=100_000.0,
            modeling_resolution="open_close",
            data_dir=str(data_dir),
            parameters=parameters,
        )
    except ValidationError as exc:
        raise BacktestRunError(f"{strategy_name}: invalid config ({exc})") from exc

    validation_errors = engine.validate_config(config)
    if validation_errors:
        raise BacktestRunError(
            f"{strategy_name}: validation failed ({'; '.join(validation_errors)})"
        )

    try:
        result = engine.run(config, str(strategy_path))
        result.stats = {**(result.stats or {}), **compute_stats_for_result(result)}
        save_result_to_sqlite(result)
        output_name = f"{result.meta.id}.trades.json"
        result.save(str(backtests_dir / output_name))
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        ImportError,
        ArithmeticError,
    ) as exc:
        raise BacktestRunError(f"{strategy_name}: run failed ({exc})") from exc

    print(
        f"✅ {strategy_name} ({engine_name}) symbol={entry['symbol']} "
        f"timeframe={entry['timeframe']} rows={len(result.trades)} id={result.meta.id}"
    )
    return True


class BacktestRunError(Exception):
    """A single strategy regeneration failed."""


def _confirm_regeneration(_input: Callable[[str], str]) -> None:
    """Abort unless the operator explicitly confirms the destructive purge."""
    answer = _input(
        "Regenerating deletes ALL existing SQLite backtests and *.trades.json files. "
        "Continue? [y/N] "
    )
    if answer.strip().lower() not in {"y", "yes"}:
        raise SystemExit("Aborted — the existing backtest corpus is unchanged.")


def _validate_backup_dir(backup_dir: Path) -> Path:
    """Validate a backup directory before it is used for writes.

    ``--backup-dir`` arrives from the command line (or from an LLM-driven caller),
    so it is resolved and checked before anything touches the file system: it must
    be an absolute path that is not the repository root, not ``DATA_DIR`` itself,
    and not inside the live ``DATA_DIR/backtests`` corpus it is supposed to back up.
    """
    resolved = backup_dir.expanduser().resolve()
    if not resolved.is_absolute():
        raise ValueError(f"Backup directory must be an absolute path: {resolved}")
    if resolved == Path.cwd() or resolved == DATA_DIR:
        raise ValueError(f"Refusing to use {resolved} as backup directory")
    if resolved == BACKTESTS_DIR or BACKTESTS_DIR in resolved.parents:
        raise ValueError(
            f"Backup directory {resolved} must live outside {BACKTESTS_DIR} — a snapshot "
            "inside the corpus would be deleted by the purge it precedes."
        )
    return resolved


def _snapshot_corpus(resolved_backup_dir: Path) -> None:
    """Copy the SQLite backtest tables and JSON artifacts into ``resolved_backup_dir``."""
    resolved_backup_dir.mkdir(parents=True, exist_ok=True)
    db_path = _existing_db_path()
    _dump_sqlite_backup(db_path, resolved_backup_dir)
    json_backed_up = _backup_json_files(BACKTESTS_DIR, resolved_backup_dir)
    print(f"Backup snapshot: {resolved_backup_dir} ({json_backed_up} JSON file(s))")


def _execute_runs(
    strategies: list[str] | None, plan: RegenerationPlan
) -> tuple[int, int, list[str]]:
    """Run the planned strategies, returning (successes, skipped, failures)."""
    failures: list[str] = list(plan.skipped)
    successes = 0
    skipped = 0

    if plan.no_data:
        skipped += len(plan.skipped)
        for message in plan.skipped:
            print(f"  - {message}")
        return successes, skipped, failures

    strategy_paths = _strategy_paths(strategies)
    for strategy_path, entry in zip(strategy_paths, plan.entries, strict=False):
        strategy_type = detect_strategy_type(strategy_path, DEFAULT_STRATEGIES_DIR)
        if strategy_type == StrategyType.SIGNAL_PROCESSOR:
            skipped += 1
            continue
        try:
            _run_strategy(entry, strategy_path, data_dir=DATA_DIR, backtests_dir=BACKTESTS_DIR)
            successes += 1
        except BacktestRunError as exc:
            failures.append(str(exc))
        except (OSError, RuntimeError, ValueError, KeyError, ImportError) as exc:
            failures.append(f"{strategy_path.stem}: run failed ({exc})")
    return successes, skipped, failures


def _restore_empty_run(
    resolved_backup_dir: Path, outcome: RegenerationOutcome
) -> RegenerationOutcome:
    """Put the previous corpus back when not a single strategy regenerated."""
    restored_sqlite, restored_json = restore_backups(resolved_backup_dir, data_dir=DATA_DIR)
    outcome.restored = True
    print(
        "⚠️  No strategy regenerated successfully — previous corpus restored from backup "
        f"(sqlite={restored_sqlite}, json={restored_json})."
    )
    print(f"Backup kept for inspection: {resolved_backup_dir}")
    return outcome


def regenerate(
    *,
    dry_run: bool,
    assume_yes: bool,
    strategies: list[str] | None = None,
    backup_dir: Path | None = None,
    _input: Callable[[str], str] = input,
) -> RegenerationOutcome | RegenerationPlan:
    """Plan or execute the corpus regeneration.

    ``dry_run=True`` returns a :class:`RegenerationPlan` without touching anything.
    ``dry_run=False`` requires ``assume_yes=True`` or a positive interactive answer;
    otherwise it raises ``SystemExit`` without destroying anything.
    """
    if dry_run:
        return plan_regeneration(strategies=strategies)

    if not assume_yes:
        _confirm_regeneration(_input)

    outcome = RegenerationOutcome()
    resolved_backup_dir = _validate_backup_dir(
        backup_dir if backup_dir is not None else _default_backup_dir()
    )
    _snapshot_corpus(resolved_backup_dir)
    outcome.backup_dir = resolved_backup_dir

    _purge_sqlite_backtests()
    BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)
    purged_json = _purge_json_backtests(BACKTESTS_DIR)
    print(f"Purged existing corpus: sqlite backtests + {purged_json} JSON file(s)")

    plan = plan_regeneration(strategies=strategies)
    successes, skipped, failures = _execute_runs(strategies, plan)

    outcome.successes = successes
    outcome.skipped = skipped
    outcome.failures = len(failures)
    outcome.failure_messages = failures

    print(
        "Regeneration summary:",
        f"successes={successes}",
        f"skipped={skipped}",
        f"failures={len(failures)}",
    )
    for message in failures:
        print(f"  - {message}")

    if successes == 0:
        return _restore_empty_run(resolved_backup_dir, outcome)

    return outcome


def _default_backup_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return DATA_DIR / f"backtests-regeneration-backup-{stamp}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate backtests (dry run unless --yes is passed)."
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Print the plan without deleting or running anything (default).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Run for real (still writes a restorable backup snapshot first).",
    )
    parser.add_argument(
        "--strategy",
        action="append",
        default=[],
        dest="strategies",
        help="Limit regeneration to these strategy paths (repeatable).",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help=(
            "Directory for the backup snapshot "
            "(default: DATA_DIR/backtests-regeneration-backup-<timestamp>)."
        ),
    )
    args = parser.parse_args(argv)

    print(f"Regenerating backtests with DATA_DIR={DATA_DIR}")

    if not args.yes:
        plan = plan_regeneration(strategies=args.strategies or None)
        print(f"Dry run (default): {plan.runnable} strategy run(s) would be executed:")
        for entry in plan.entries:
            print(
                f"  - {entry['strategy']} engine={entry['engine']} symbol={entry['symbol']} "
                f"timeframe={entry['timeframe']} {entry['start']}..{entry['end']}"
            )
        for message in plan.skipped:
            print(f"  ! skipped: {message}")
        print(
            f"At risk: sqlite_backtests={plan.sqlite_backtests} "
            f"json_backtests={plan.json_backtests}. Pass --yes to regenerate."
        )
        return 0

    regenerate(
        dry_run=False,
        assume_yes=True,
        strategies=args.strategies or None,
        backup_dir=args.backup_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
