from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
from dotenv import load_dotenv
from pydantic import ValidationError
from sqlalchemy import delete

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


def _candidate_symbols(meta: dict[str, Any]) -> list[str]:
    tags = meta.get("tags", {}) if isinstance(meta, dict) else {}
    raw_assets = tags.get("asset_class", []) if isinstance(tags, dict) else []
    if isinstance(raw_assets, str):
        assets = [raw_assets.lower()]
    elif isinstance(raw_assets, list):
        assets = [str(item).lower() for item in raw_assets]
    else:
        assets = []

    ordered: list[str] = []

    if any("forex" in asset or "metal" in asset for asset in assets):
        ordered.extend(["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"])
    if any("equit" in asset or "stock" in asset for asset in assets):
        ordered.extend(["SPY", "AAPL", "QQQ"])
    if any("etf" in asset for asset in assets):
        ordered.extend(["SPY", "QQQ", "GLD"])
    if any("index" in asset or "future" in asset for asset in assets):
        ordered.extend(["US500", "SPY", "VIX"])
    if any("multi" in asset for asset in assets):
        ordered.extend(["SPY", "QQQ", "EURUSD", "XAUUSD"])
    if not ordered:
        ordered.extend(["EURUSD", "XAUUSD", "SPY", "AAPL"])

    deduped: list[str] = []
    seen: set[str] = set()
    for symbol in ordered:
        if symbol in seen:
            continue
        seen.add(symbol)
        deduped.append(symbol)
    return deduped


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
        first_timeframe = sorted(timeframe_map)[0]
        return symbol, first_timeframe, timeframe_map[first_timeframe][0]

    if not symbol_index:
        return None

    fallback_symbol = sorted(symbol_index)[0]
    timeframe_map = symbol_index[fallback_symbol]
    if not timeframe_map:
        return None
    first_timeframe = sorted(timeframe_map)[0]
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

    start_value = first_stats.min if (
        first_stats is not None and getattr(first_stats, "has_min_max", False)
    ) else None
    end_value = last_stats.max if (
        last_stats is not None and getattr(last_stats, "has_min_max", False)
    ) else None

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
    for path in backtests_dir.glob("*.trades.json"):
        path.unlink()
        deleted += 1
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Regenerate all backtests (destructive purge of existing data)."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the destructive purge of existing SQLite and JSON backtests.",
    )
    args = parser.parse_args()
    if not args.yes and os.environ.get("REGENERATE_BACKTESTS_CONFIRM") != "yes":
        raise SystemExit(
            "Refusing to purge backtests without confirmation. "
            "Pass --yes or set REGENERATE_BACKTESTS_CONFIRM=yes."
        )
    print(f"Regenerating backtests with DATA_DIR={DATA_DIR}")
    init_db()
    _purge_sqlite_backtests()
    BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)
    deleted_json = _purge_json_backtests(BACKTESTS_DIR)
    print(f"Purged SQLite backtest tables and removed {deleted_json} JSON backtest files.")

    symbol_index = _build_symbol_index(DATA_DIR)
    if not symbol_index:
        raise RuntimeError(f"No OHLCV data found under {DATA_DIR / 'ohlcv'}")

    successes = 0
    failures: list[str] = []
    skipped = 0

    strategy_paths = iter_strategy_files(DEFAULT_STRATEGIES_DIR)
    for strategy_path in strategy_paths:
        strategy_type = detect_strategy_type(strategy_path, DEFAULT_STRATEGIES_DIR)
        if strategy_type == StrategyType.SIGNAL_PROCESSOR:
            skipped += 1
            failures.append(f"{strategy_path.name}: skipped signal_processor strategy")
            continue

        meta = load_strategy_meta(strategy_path, DEFAULT_STRATEGIES_DIR)
        selection = _select_symbol_timeframe(meta, symbol_index)
        if selection is None:
            skipped += 1
            failures.append(f"{strategy_path.name}: no available symbol/timeframe in DATA_DIR")
            continue

        symbol, timeframe, parquet_path = selection
        try:
            start_date, end_date = _read_parquet_date_range(parquet_path)
        except (OSError, ValueError, RuntimeError) as exc:
            skipped += 1
            failures.append(f"{strategy_path.name}: could not determine date range ({exc})")
            continue

        engine_name = "pynecore" if strategy_path.stem.startswith("pynecore_") else "vectorbt"
        engine = get_engine(engine_name)
        strategy_name = strategy_path.stem
        parameters = resolve_strategy_parameters(strategy_path, {})

        try:
            config = BacktestConfig(
                strategy_name=strategy_name,
                symbols=[symbol],
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
                initial_capital=100_000.0,
                modeling_resolution="open_close",
                data_dir=str(DATA_DIR),
                parameters=parameters,
            )
        except ValidationError as exc:
            failures.append(f"{strategy_name}: invalid config ({exc})")
            continue

        validation_errors = engine.validate_config(config)
        if validation_errors:
            failures.append(f"{strategy_name}: validation failed ({'; '.join(validation_errors)})")
            continue

        try:
            result = engine.run(config, str(strategy_path))
            result.stats = {**(result.stats or {}), **compute_stats_for_result(result)}
            save_result_to_sqlite(result)
            output_name = f"{result.meta.id}.trades.json"
            result.save(str(BACKTESTS_DIR / output_name))
            successes += 1
            print(
                f"✅ {strategy_name} ({engine_name}) symbol={symbol} timeframe={timeframe} "
                f"rows={len(result.trades)} id={result.meta.id}"
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            ImportError,
            TimeoutError,
            ArithmeticError,
        ) as exc:
            failures.append(f"{strategy_name}: run failed ({exc})")

    print(
        "Regeneration summary:",
        f"strategies={len(strategy_paths)}",
        f"successes={successes}",
        f"skipped={skipped}",
        f"failures={len(failures)}",
    )
    if failures:
        print("Failures:")
        for message in failures:
            print(f"  - {message}")


if __name__ == "__main__":
    main()
