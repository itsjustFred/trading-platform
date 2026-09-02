#!/usr/bin/env python3
"""Run a strategy backtest over a symbol × timeframe grid.

The runner intentionally delegates each cell to ``runner.cli run`` so every
backtest uses the same validation, statistics calculation, risk sizing, output
format, and SQLite persistence path as a normal manual backtest.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from pydantic import ValidationError

from data_tools.auto_download import ensure_backtest_data
from runner.daily_equity import attach_daily_equity
from runner.engines.base import BacktestConfig, resolve_strategy_parameters
from runner.stats import merge_canonical_stats
from shared.constants import DATA_DIR, normalize_timeframe
from shared.database import save_result_to_sqlite
from shared.logging import get_logger
from shared.models import BacktestResult, StrategyType
from shared.strategy_registry import detect_strategy_type

GRID_SYMBOLS = ["XAUUSD", "SP500", "NAS100", "EURUSD", "USDJPY", "OIL"]
GRID_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]
GRID_START = "2020-01-01"
DEFAULT_OUTPUT_DIR = DATA_DIR / "backtests" / "grid_runner"
logger = get_logger("runner.grid")
TIMEFRAME_ALIASES = {
    "m1": "1m",
    "1min": "1m",
    "min1": "1m",
    "m5": "5m",
    "m15": "15m",
    "m30": "30m",
    "h1": "1h",
    "hour1": "1h",
    "h4": "4h",
    "hour4": "4h",
    "d1": "1d",
    "daily": "1d",
    "day": "1d",
}


def parse_csv_list(raw: str | Iterable[str]) -> list[str]:
    """Parse comma-separated CLI input while accepting already-split values."""
    if isinstance(raw, str):
        return [item.strip().upper() for item in raw.split(",") if item.strip()]
    values: list[str] = []
    for item in raw:
        values.extend(parse_csv_list(str(item)))
    return values


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def normalize_timeframe_value(value: str) -> str:
    lowered = str(value or "").strip().lower()
    return normalize_timeframe(TIMEFRAME_ALIASES.get(lowered, lowered))


def sanitize(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_") or "item"


def parse_params(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    params = json.loads(raw)
    if not isinstance(params, dict):
        raise ValueError("--params must decode to a JSON object")
    return params


def output_path_for(output_dir: Path, run_id: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{run_id}.trades.json"


def backtest_id_from_output_path(path: Path) -> str:
    name = path.name
    if name.endswith(".trades.json"):
        return name[: -len(".trades.json")]
    return path.stem


def read_backtest_stats(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    stats = payload.get("stats", {}) if isinstance(payload, dict) else {}
    trades = payload.get("trades", []) if isinstance(payload, dict) else []
    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    backtest_id = str(meta.get("id") or "").strip() if isinstance(meta, dict) else ""
    return {
        "backtest_id": backtest_id or backtest_id_from_output_path(path),
        "trade_data_available": isinstance(trades, list),
        "total_trades": len(trades) if isinstance(trades, list) else 0,
        "total_pnl": stats.get("total_pnl"),
        "net_profit_pct": stats.get("net_profit_pct", stats.get("total_return")),
        "sharpe": stats.get("sharpe", stats.get("sharpe_ratio")),
        "cagr": stats.get("cagr"),
        "max_drawdown_pct": stats.get("max_drawdown_pct"),
        "win_rate": stats.get("win_rate"),
        "daily_return_count": stats.get("daily_return_count", 0),
    }


def build_run_id(strategy: str, symbol: str, timeframe: str) -> str:
    strategy_slug = sanitize(Path(strategy).stem)
    return f"grid_{strategy_slug}_{symbol.lower()}_{timeframe}"


def build_cli_command(
    *,
    strategy: str,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    engine: str,
    capital: float,
    risk_pct: float | None,
    max_position_pct: float | None,
    max_leverage: float | None,
    intrabar_resolution: str,
    params: dict[str, Any] | None,
    output_path: Path,
    data_dir: str,
    save_sqlite: bool = True,
    result_id: str = "",
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "runner.cli",
        "run",
        "--engine",
        engine,
        "--strategy",
        strategy,
        "--symbols",
        symbol,
        "--timeframe",
        timeframe,
        "--start",
        start,
        "--capital",
        str(capital),
        "--intrabar-resolution",
        intrabar_resolution,
        "--output",
        str(output_path),
    ]
    if end:
        cmd.extend(["--end", end])
    if risk_pct is not None:
        cmd.extend(["--risk-pct", str(risk_pct)])
    if max_position_pct is not None:
        cmd.extend(["--max-position-pct", str(max_position_pct)])
    if max_leverage is not None:
        cmd.extend(["--max-leverage", str(max_leverage)])
    if params:
        cmd.extend(["--params", json.dumps(params, separators=(",", ":"))])
    if data_dir:
        cmd.extend(["--data-dir", data_dir])
    if not save_sqlite:
        cmd.append("--skip-sqlite")
    if result_id:
        cmd.extend(["--result-id", result_id])
    return cmd


def get_engine(name: str):
    """Create an engine adapter without importing runner.cli."""
    if name == "native":
        from runner.engines.native_adapter import NativeAdapter

        return NativeAdapter()
    if name == "vectorbt":
        from runner.engines.vectorbt_adapter import VectorbtAdapter

        return VectorbtAdapter()
    if name == "lean":
        from runner.engines.lean_adapter import LeanAdapter

        return LeanAdapter()
    if name == "pynecore":
        from runner.engines.pynecore_adapter import PyneCoreAdapter

        return PyneCoreAdapter()
    raise ValueError(f"Unknown engine: {name}")


def _detect_date_range(symbol: str, timeframe: str, data_dir: str) -> tuple[str, str]:
    root = Path(data_dir) / "ohlcv" if data_dir else DATA_DIR / "ohlcv"
    matches = list(root.rglob(f"**/{symbol}/{timeframe}.parquet"))
    if not matches:
        matches = list(root.rglob(f"**/{symbol}/*.parquet"))
    if not matches:
        raise ValueError(
            f"No OHLCV data found for {symbol}/{timeframe} under {root}; "
            "refusing to fabricate a backtest date range"
        )
    df = pd.read_parquet(matches[0], columns=["timestamp"])
    ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").dropna()
    if ts.empty:
        raise ValueError(
            f"OHLCV data for {symbol}/{timeframe} under {root} has no valid "
            "timestamps; refusing to fabricate a backtest date range"
        )
    return ts.min().strftime("%Y-%m-%d"), ts.max().strftime("%Y-%m-%d")


def _resolve_dates_for_equity(
    symbol: str,
    timeframe: str,
    data_dir: str,
    start: str,
    end: str,
) -> tuple[str, str]:
    """Resolve start/end dates for equity curve attachment."""
    start_date = start
    end_date = end
    normalized_timeframe = normalize_timeframe_value(timeframe)
    if not start_date or not end_date:
        detected_start, detected_end = _detect_date_range(symbol, normalized_timeframe, data_dir)
        start_date = start_date or detected_start
        end_date = end_date or detected_end
    return start_date, end_date


def _init_cell_result(
    *,
    strategy: str,
    symbol: str,
    timeframe: str,
    data_dir: str,
    start: str,
    end: str,
    output_dir: Path,
    skip_existing: bool,
) -> tuple[dict[str, Any], BacktestResult | None]:
    """Build the shared cell record and honor the skip-existing shortcut.

    Returns ``(cell, skipped)``; ``skipped`` is a fully-processed
    ``BacktestResult`` when the existing output was reused, else ``None``.
    The resolved window is computed once here so the reported cell window,
    the CLI command and ``attach_daily_equity`` all see the same dates.
    """
    run_id = build_run_id(strategy, symbol, timeframe)
    path = output_path_for(output_dir, run_id)
    resolved_start, resolved_end = _resolve_dates_for_equity(
        symbol, timeframe, data_dir, start, end
    )
    result: dict[str, Any] = {
        "run_id": run_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "start": resolved_start,
        "end": resolved_end,
        "output": str(path),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }

    if skip_existing and path.exists():
        try:
            backtest_result = BacktestResult.load(path)
            attach_daily_equity(backtest_result, start=resolved_start, end=resolved_end)
            merge_canonical_stats(backtest_result)
            backtest_result.save(path)
            result.update(read_backtest_stats(path))
            result.update({"status": "skipped_existing", "finished_at": datetime.now(timezone.utc).isoformat()})
            return result, backtest_result
        except (json.JSONDecodeError, ValidationError, OSError, ValueError) as exc:
            result.update({"status": "failed", "error": f"existing output is invalid: {exc}"})
            return result, None

    return result, None


def run_one_backtest_in_process(
    *,
    strategy: str,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    engine: str,
    capital: float,
    risk_pct: float | None,
    max_position_pct: float | None,
    max_leverage: float | None,
    intrabar_resolution: str,
    params: dict[str, Any] | None,
    output_dir: Path,
    data_dir: str,
    skip_existing: bool,
    save_sqlite: bool = True,
    keep_indicators: bool = False,
) -> dict[str, Any]:
    result, _skipped = _init_cell_result(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        data_dir=data_dir,
        start=start,
        end=end,
        output_dir=output_dir,
        skip_existing=skip_existing,
    )
    if result["status"] != "pending":
        return result

    run_id = result["run_id"]
    path = Path(result["output"])
    resolved_start, resolved_end = result["start"], result["end"]

    try:
        strategy_type = detect_strategy_type(Path(strategy).resolve())
        if strategy_type == StrategyType.SIGNAL_PROCESSOR:
            raise ValueError(
                "Detected signal_processor strategy. runner.cli run currently supports market_data strategies only."
            )

        resolved_parameters = resolve_strategy_parameters(strategy, dict(params or {}))
        normalized_timeframe = normalize_timeframe_value(timeframe)
        start_date = start
        end_date = end
        if not start_date or not end_date:
            detected_start, detected_end = _detect_date_range(symbol, normalized_timeframe, data_dir)
            start_date = start_date or detected_start
            end_date = end_date or detected_end

        downloaded = ensure_backtest_data(
            symbols=[symbol],
            timeframe=normalized_timeframe,
            start=start_date,
            end=end_date,
            data_dir=data_dir,
            modeling_resolution=intrabar_resolution or "open_close",
            log_callback=logger.info,
        )
        if downloaded:
            result["auto_downloaded_data"] = downloaded

        config = BacktestConfig(
            strategy_name=Path(strategy).stem,
            symbols=[symbol],
            timeframe=normalized_timeframe,
            start_date=start_date,
            end_date=end_date,
            initial_capital=capital,
            risk_per_trade_pct=risk_pct,
            max_position_pct=max_position_pct,
            max_leverage=max_leverage,
            modeling_resolution=intrabar_resolution or "open_close",
            data_dir=data_dir,
            parameters=resolved_parameters,
        )
        engine_adapter = get_engine(engine)
        errors = engine_adapter.validate_config(config)
        if errors:
            raise ValueError("; ".join(errors))

        logger.info("Running grid cell: engine=%s strategy=%s symbol=%s timeframe=%s", engine, strategy, symbol, timeframe)
        backtest_result = engine_adapter.run(config, strategy)
        backtest_result.meta.id = run_id
        if not keep_indicators:
            backtest_result.indicators = []
        # Use resolved dates for daily equity attachment
        attach_daily_equity(backtest_result, start=start_date, end=end_date)
        merge_canonical_stats(backtest_result)
        output_dir.mkdir(parents=True, exist_ok=True)
        backtest_result.save(path)
        if save_sqlite:
            try:
                save_result_to_sqlite(backtest_result)
            except Exception as exc:
                logger.warning("Could not persist grid cell to SQLite: %s", exc)

        result.update(read_backtest_stats(path))
        result.update({"status": "ok", "returncode": 0})
    except ValidationError as exc:
        result.update({"status": "failed", "returncode": 2, "error": str(exc)})
    except Exception as exc:
        result.update({"status": "failed", "returncode": 1, "error": f"{exc}\n{traceback.format_exc()[-4000:]}"})

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result


def run_one_backtest(
    *,
    strategy: str,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    engine: str,
    capital: float,
    risk_pct: float | None,
    max_position_pct: float | None,
    max_leverage: float | None,
    intrabar_resolution: str,
    params: dict[str, Any] | None,
    output_dir: Path,
    data_dir: str,
    timeout_seconds: int,
    skip_existing: bool,
    use_subprocess: bool = False,
    save_sqlite: bool = True,
    keep_indicators: bool = False,
) -> dict[str, Any]:
    if not use_subprocess:
        return run_one_backtest_in_process(
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            engine=engine,
            capital=capital,
            risk_pct=risk_pct,
            max_position_pct=max_position_pct,
            max_leverage=max_leverage,
            intrabar_resolution=intrabar_resolution,
            params=params,
            output_dir=output_dir,
            data_dir=data_dir,
            skip_existing=skip_existing,
            save_sqlite=save_sqlite,
            keep_indicators=keep_indicators,
        )

    result, _skipped = _init_cell_result(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        data_dir=data_dir,
        start=start,
        end=end,
        output_dir=output_dir,
        skip_existing=skip_existing,
    )
    if result["status"] != "pending":
        return result

    run_id = result["run_id"]
    path = Path(result["output"])
    resolved_start, resolved_end = result["start"], result["end"]

    cmd = build_cli_command(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        start=resolved_start,
        end=resolved_end,
        engine=engine,
        capital=capital,
        risk_pct=risk_pct,
        max_position_pct=max_position_pct,
        max_leverage=max_leverage,
        intrabar_resolution=intrabar_resolution,
        params=params,
        output_path=path,
        data_dir=data_dir,
        save_sqlite=save_sqlite,
        result_id=run_id,
    )
    result["cmd"] = " ".join(cmd)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            env=env,
        )
        result["returncode"] = proc.returncode
        result["finished_at"] = datetime.now(timezone.utc).isoformat()

        if proc.returncode == 0 and path.exists():
            result.update(read_backtest_stats(path))
            result["status"] = "ok"
        else:
            result["status"] = "failed"
            result["error"] = proc.stdout[-4000:] if proc.stdout else "no output captured"
    except subprocess.TimeoutExpired:
        result["status"] = "failed"
        result["returncode"] = -1
        result["error_class"] = "timeout"
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["error"] = f"subprocess cell timed out after {timeout_seconds}s"
        return result
    except OSError as exc:
        result["status"] = "failed"
        result["returncode"] = -1
        result["error_class"] = "subprocess_error"
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["error"] = f"subprocess could not be launched: {exc}"
        return result
    return result


def write_matrix_csv(output_dir: Path, rows: list[dict[str, Any]]) -> Path:
    path = output_dir / "grid_matrix.csv"
    fieldnames = [
        "symbol",
        "timeframe",
        "status",
        "backtest_id",
        "trade_data_available",
        "total_trades",
        "total_pnl",
        "net_profit_pct",
        "sharpe",
        "cagr",
        "max_drawdown_pct",
        "win_rate",
        "daily_return_count",
        "output",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    return path


def run_backtest_grid(
    *,
    strategy: str,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    start: str = GRID_START,
    end: str = "",
    engine: str = "vectorbt",
    capital: float = 100_000.0,
    risk_pct: float | None = 0.01,
    max_position_pct: float | None = None,
    max_leverage: float | None = None,
    intrabar_resolution: str = "open_close",
    params: dict[str, Any] | None = None,
    output_dir: Path | None = None,
    data_dir: str = "",
    max_workers: int = 1,
    timeout_seconds: int = 1800,
    skip_existing: bool = False,
    use_subprocess: bool = False,
    save_sqlite: bool = True,
    keep_indicators: bool = False,
) -> dict[str, Any]:
    """Run one strategy across every symbol/timeframe combination."""
    normalized_symbols = [normalize_symbol(symbol) for symbol in (symbols or GRID_SYMBOLS)]
    normalized_timeframes = [normalize_timeframe_value(tf) for tf in (timeframes or GRID_TIMEFRAMES)]
    if not normalized_symbols:
        raise ValueError("At least one symbol is required")
    if not normalized_timeframes:
        raise ValueError("At least one timeframe is required")

    output_dir = output_dir or DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    params = dict(params or {})

    summary: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "symbols": normalized_symbols,
        "timeframes": normalized_timeframes,
        "start": start,
        "end": end,
        "engine": engine,
        "capital": capital,
        "risk_pct": risk_pct,
        "max_position_pct": max_position_pct,
        "max_leverage": max_leverage,
        "intrabar_resolution": intrabar_resolution,
        "params": params,
        "output_dir": str(output_dir),
        "data_dir": data_dir,
        "max_workers": max_workers,
        "skip_existing": skip_existing,
        "use_subprocess": use_subprocess,
        "save_sqlite": save_sqlite,
        "runs": [],
    }

    tasks = [
        {
            "strategy": strategy,
            "symbol": symbol,
            "timeframe": timeframe,
            "start": start,
            "end": end,
            "engine": engine,
            "capital": capital,
            "risk_pct": risk_pct,
            "max_position_pct": max_position_pct,
            "max_leverage": max_leverage,
            "intrabar_resolution": intrabar_resolution,
            "params": params,
            "output_dir": output_dir,
            "data_dir": data_dir,
            "timeout_seconds": timeout_seconds,
            "skip_existing": skip_existing,
            "use_subprocess": use_subprocess,
            "save_sqlite": save_sqlite,
        }
        for symbol in normalized_symbols
        for timeframe in normalized_timeframes
    ]

    rows: list[dict[str, Any]] = []
    if max_workers and max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(run_one_backtest, **task) for task in tasks]
            for future in as_completed(futures):
                rows.append(future.result())
    else:
        rows = [run_one_backtest(**task) for task in tasks]

    rows.sort(key=lambda row: (row.get("symbol", ""), row.get("timeframe", "")))
    summary["runs"] = rows
    summary["completed_at"] = datetime.now(timezone.utc).isoformat()
    summary["requested"] = len(tasks)
    summary["ok"] = sum(1 for row in rows if row.get("status") == "ok")
    summary["failed"] = sum(1 for row in rows if row.get("status") == "failed")
    summary["skipped_existing"] = sum(1 for row in rows if row.get("status") == "skipped_existing")
    summary["matrix_csv"] = str(write_matrix_csv(output_dir, rows))

    summary_path = output_dir / "grid_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a strategy backtest grid")
    parser.add_argument("--strategy", "-s", required=True, help="Path to strategy file")
    parser.add_argument("--symbols", default=",".join(GRID_SYMBOLS), help="Comma-separated symbols")
    parser.add_argument("--timeframes", default=",".join(GRID_TIMEFRAMES), help="Comma-separated timeframes")
    parser.add_argument("--start", default=GRID_START, help="Start date, default 2020-01-01")
    parser.add_argument("--end", default="", help="End date, empty lets runner.cli auto-detect")
    parser.add_argument("--engine", default="vectorbt", help="Backtest engine")
    parser.add_argument("--capital", type=float, default=100_000.0, help="Initial capital")
    parser.add_argument("--risk-pct", type=float, default=0.01, help="Risk per trade, e.g. 0.01")
    parser.add_argument("--max-position-pct", type=float, default=None)
    parser.add_argument("--max-leverage", type=float, default=None)
    parser.add_argument("--intrabar-resolution", default="open_close")
    parser.add_argument("--params", default="", help='Strategy params JSON, e.g. \'{"fast": 10}\'')
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--data-dir", default="", help="Override data directory")
    parser.add_argument("--max-workers", type=int, default=1, help="Parallel backtest cells")
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="Timeout per cell")
    parser.add_argument("--resume", action="store_true", help="Skip cells with existing output JSON")
    parser.add_argument("--subprocess", action="store_true", help="Run each grid cell via runner.cli subprocess instead of in-process")
    parser.add_argument("--skip-sqlite", action="store_true", help="Do not persist grid cells to SQLite")
    args = parser.parse_args()

    summary = run_backtest_grid(
        strategy=args.strategy,
        symbols=parse_csv_list(args.symbols),
        timeframes=parse_csv_list(args.timeframes),
        start=args.start,
        end=args.end,
        engine=args.engine,
        capital=args.capital,
        risk_pct=args.risk_pct,
        max_position_pct=args.max_position_pct,
        max_leverage=args.max_leverage,
        intrabar_resolution=args.intrabar_resolution,
        params=parse_params(args.params),
        output_dir=args.output_dir,
        data_dir=args.data_dir,
        max_workers=args.max_workers,
        timeout_seconds=args.timeout_seconds,
        skip_existing=args.resume,
        use_subprocess=args.subprocess,
        save_sqlite=not args.skip_sqlite,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if summary["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
