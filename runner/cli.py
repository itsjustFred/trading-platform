"""
Backtest Runner CLI — unified interface to run backtests on any engine.

Usage:
    python -m runner.cli run --engine vectorbt --strategy strategies/example_sma/example_sma.py \\
        --symbols AAPL MSFT --timeframe 1h --start 2023-01-01 --end 2025-01-01

    python -m runner.cli run --engine lean --strategy path/to/lean_result.json \\
        --symbols XAUUSD --name my_strategy

    python -m runner.cli engines    # list available engines
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from data_tools.auto_download import ensure_backtest_data
from runner.backtest_grid import DEFAULT_OUTPUT_DIR, parse_csv_list, parse_params, run_backtest_grid
from runner.backtest_matrix import MATRIX_OUTPUT_DIR, run_backtest_matrix
from runner.daily_equity import attach_daily_equity
from runner.engines.base import BacktestConfig, EngineAdapter, resolve_strategy_parameters
from runner.stats import compute_stats_for_result
from runner.walkforward import run_walkforward
from shared.constants import DATA_DIR, normalize_timeframe
from shared.database import save_result_to_sqlite
from shared.logging import get_logger
from shared.models import StrategyType
from shared.strategy_registry import detect_strategy_type

logger = get_logger("runner")

BACKTESTS_DIR = DATA_DIR / "backtests"


def _emit_progress(
    callback: Callable[[float, str], None] | None,
    percent: float,
    message: str,
) -> None:
    if callback is None:
        return
    bounded = max(0.0, min(100.0, float(percent)))
    callback(round(bounded, 2), str(message))


def get_engine(name: str) -> EngineAdapter:
    """Get an engine adapter by name."""
    if name == "native":
        from runner.engines.native_adapter import NativeAdapter

        return NativeAdapter()
    elif name == "vectorbt":
        from runner.engines.vectorbt_adapter import VectorbtAdapter

        return VectorbtAdapter()
    elif name == "lean":
        from runner.engines.lean_adapter import LeanAdapter

        return LeanAdapter()
    elif name == "pynecore":
        from runner.engines.pynecore_adapter import PyneCoreAdapter

        return PyneCoreAdapter()
    else:
        raise ValueError(f"Unknown engine: {name}. Available: native, vectorbt, lean, pynecore")


def _resolve_strategy_type(strategy_path: str) -> StrategyType:
    resolved = Path(strategy_path).resolve()
    if not resolved.exists():
        return StrategyType.MARKET_DATA
    return detect_strategy_type(resolved)


def _detect_date_range(symbols: list[str], timeframe: str, data_dir: str = "") -> tuple[str, str]:
    """Auto-detect date range from available OHLCV data files.

    For multiple symbols, returns the intersection (common period).
    """
    ohlcv_root = Path(data_dir) / "ohlcv" if data_dir else DATA_DIR / "ohlcv"
    normalized_timeframe = normalize_timeframe(timeframe)
    starts, ends = [], []

    for symbol in symbols:
        matches = list(ohlcv_root.rglob(f"**/{symbol}/{normalized_timeframe}.parquet"))
        if not matches:
            # Try without exact timeframe match — find any parquet for symbol
            matches = list(ohlcv_root.rglob(f"**/{symbol}/*.parquet"))
        if not matches:
            continue
        try:
            import pandas as pd

            df = pd.read_parquet(matches[0])
            df.columns = [c.lower() for c in df.columns]
            if "timestamp" in df.columns:
                ts = pd.to_datetime(df["timestamp"])
                starts.append(ts.min().strftime("%Y-%m-%d"))
                ends.append(ts.max().strftime("%Y-%m-%d"))
        except Exception as e:
            logger.warning("Could not read date range from %s: %s", matches[0], e)

    if starts and ends:
        # Intersection: latest start, earliest end
        return max(starts), min(ends)
    return "2023-01-01", "2025-12-31"  # fallback


def cmd_run(args):
    """Run a backtest."""
    strategy_type = _resolve_strategy_type(args.strategy)
    if strategy_type == StrategyType.SIGNAL_PROCESSOR:
        raise ValueError(
            "Detected signal_processor strategy. "
            "runner.cli run currently supports market_data strategies only."
        )

    engine = get_engine(args.engine)
    logger.info("Detected strategy type: %s", strategy_type.value)
    progress_callback: Callable[[float, str], None] | None = None
    if bool(getattr(args, "progress_stdout", False)):
        def _stdout_progress(percent: float, message: str) -> None:
            print(f"PROGRESS:{percent:.2f}:{message}", flush=True)

        progress_callback = _stdout_progress

    parameter_overrides = json.loads(args.params) if args.params else {}
    if not isinstance(parameter_overrides, dict):
        raise ValueError("--params must decode to a JSON object")
    resolved_parameters = resolve_strategy_parameters(args.strategy, parameter_overrides)

    normalized_timeframe = normalize_timeframe(args.timeframe)

    # Auto-detect date range if not provided
    start = args.start
    end = args.end
    if not start or not end:
        detected_start, detected_end = _detect_date_range(
            args.symbols, normalized_timeframe, args.data_dir or ""
        )
        if not start:
            start = detected_start
        if not end:
            end = detected_end
        logger.info("Auto-detected date range: %s to %s", start, end)

    downloaded = ensure_backtest_data(
        symbols=args.symbols,
        timeframe=normalized_timeframe,
        start=start,
        end=end,
        data_dir=args.data_dir or "",
        modeling_resolution=getattr(args, "intrabar_resolution", "open_close") or "open_close",
        progress_callback=progress_callback,
        log_callback=logger.info,
    )
    if downloaded:
        logger.info("Auto-downloaded %d missing OHLCV dataset(s)", len(downloaded))

    try:
        config = BacktestConfig(
            strategy_name=args.name or Path(args.strategy).stem,
            symbols=args.symbols,
            timeframe=normalized_timeframe,
            start_date=start,
            end_date=end,
            initial_capital=args.capital,
            risk_per_trade_pct=getattr(args, "risk_pct", None),
            max_position_pct=getattr(args, "max_position_pct", None),
            max_leverage=getattr(args, "max_leverage", None),
            modeling_resolution=getattr(args, "intrabar_resolution", "open_close") or "open_close",
            data_dir=args.data_dir or "",
            parameters=resolved_parameters,
        )
    except ValidationError as exc:
        for err in exc.errors():
            logger.error("Config error: %s — %s", err["loc"], err["msg"])
        sys.exit(1)

    # Validate
    errors = engine.validate_config(config)
    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        sys.exit(1)

    logger.info(
        "Running backtest: engine=%s, strategy=%s, symbols=%s",
        engine.name,
        config.strategy_name,
        config.symbols,
    )

    if engine.name != "native":
        _emit_progress(progress_callback, 5.0, "Loading market data...")
    if progress_callback is None:
        result = engine.run(config, args.strategy)
    else:
        result = engine.run(config, args.strategy, progress_callback=progress_callback)
    result_id = str(getattr(args, "result_id", "") or "").strip()
    if result_id:
        result.meta.id = result_id
    if engine.name != "native":
        _emit_progress(progress_callback, 90.0, "Calculating statistics...")
    attach_daily_equity(result, start=start, end=end)
    result.stats = {**result.stats, **compute_stats_for_result(result)}

    # Save result
    _emit_progress(progress_callback, 95.0, "Saving results...")
    BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.output:
        output_path = args.output
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = str(BACKTESTS_DIR / f"{config.strategy_name}_{ts}.trades.json")

    result.save(output_path)
    if not getattr(args, "skip_sqlite", False):
        try:
            save_result_to_sqlite(result)
        except Exception as exc:
            logger.warning("Could not persist backtest to SQLite: %s", exc)
    logger.info(
        "Result: %d trades, total PnL: $%.2f", len(result.trades), sum(t.pnl for t in result.trades)
    )
    logger.info("Saved to: %s", output_path)
    _emit_progress(progress_callback, 100.0, f"Completed - {len(result.trades)} trades found")


def cmd_engines(args):
    """List available engines."""
    engines = [
        ("native", "Event-based bar-by-bar simulation engine (default)"),
        ("vectorbt", "Python vectorized backtesting (simple signal-based strategies)"),
        ("lean", "QuantConnect LEAN (complex event-driven strategies)"),
        ("pynecore", "PyneCore Pine-style Python strategies (indicator-rich strategies)"),
    ]
    print("\nAvailable engines:\n")
    for name, desc in engines:
        print(f"  {name:12s}  {desc}")
    print()


def cmd_walkforward(args):
    """Run walk-forward validation for a strategy."""
    symbols = parse_csv_list(args.symbols)
    params = parse_params(args.params)
    result = run_walkforward(
        strategy=args.strategy,
        engine=get_engine(args.engine),
        engine_name=args.engine,
        symbols=symbols,
        timeframe=normalize_timeframe(args.timeframe),
        start=args.start,
        end=args.end,
        capital=args.capital,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        risk_pct=args.risk_pct,
        max_position_pct=args.max_position_pct,
        max_leverage=args.max_leverage,
        modeling_resolution=args.intrabar_resolution,
        params=params,
        data_dir=args.data_dir or "",
        output_dir=args.output_dir,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_grid(args):
    """Run a strategy over a symbol/timeframe grid."""
    result = run_backtest_grid(
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
        data_dir=args.data_dir or "",
        max_workers=args.max_workers,
        timeout_seconds=args.timeout_seconds,
        skip_existing=args.resume,
        use_subprocess=args.subprocess,
        save_sqlite=not args.skip_sqlite,
        keep_indicators=args.keep_indicators,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["failed"]:
        raise SystemExit(1)


def cmd_matrix(args):
    """Run every strategy over the symbol/timeframe grid."""
    result = run_backtest_matrix(
        strategy_identifiers=args.strategy,
        symbols=parse_csv_list(args.symbols),
        timeframes=parse_csv_list(args.timeframes),
        start=args.start,
        end=args.end,
        engine=args.engine or "",
        capital=args.capital,
        risk_pct=args.risk_pct,
        max_position_pct=args.max_position_pct,
        max_leverage=args.max_leverage,
        intrabar_resolution=args.intrabar_resolution,
        params=parse_params(args.params),
        output_dir=args.output_dir,
        data_dir=args.data_dir or "",
        max_workers=args.max_workers,
        timeout_seconds=args.timeout_seconds,
        skip_existing=args.resume,
        use_subprocess=args.subprocess,
        save_sqlite=not args.skip_sqlite,
        strategies_dir=args.strategies_dir,
        keep_indicators=args.keep_indicators,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["failed_cells"]:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description="Backtest Runner")
    subparsers = parser.add_subparsers(dest="command")

    # Run command
    run_p = subparsers.add_parser("run", help="Run a backtest")
    run_p.add_argument(
        "--engine",
        "-e",
        required=True,
        help="Engine: native, vectorbt, lean, pynecore",
    )
    run_p.add_argument("--strategy", "-s", required=True, help="Path to strategy file")
    run_p.add_argument("--symbols", nargs="+", default=[], help="Symbols to trade")
    run_p.add_argument("--timeframe", "-tf", default="1h", help="Data timeframe")
    run_p.add_argument("--start", default="", help="Start date (empty = auto from data)")
    run_p.add_argument("--end", default="", help="End date (empty = auto from data)")
    run_p.add_argument("--capital", type=float, default=100000, help="Initial capital")
    run_p.add_argument(
        "--risk-pct", type=float, default=None, help="Risk per trade (decimal, e.g. 0.01 for 1%%)"
    )
    run_p.add_argument(
        "--max-position-pct",
        type=float,
        default=None,
        help="Max position notional / equity (e.g. 1.0 = 100%%)",
    )
    run_p.add_argument(
        "--max-leverage",
        type=float,
        default=None,
        help="Max notional / equity allowed with leverage",
    )
    run_p.add_argument(
        "--intrabar-resolution",
        default="open_close",
        help="Intrabar exit modeling: main_timeframe, open_close, or explicit finer timeframe",
    )
    run_p.add_argument("--name", "-n", help="Strategy name (default: filename)")
    run_p.add_argument("--output", "-o", help="Output file path")
    run_p.add_argument("--result-id", help="Persisted BacktestMeta.id override")
    run_p.add_argument("--skip-sqlite", action="store_true", help="Do not persist this run to SQLite")
    run_p.add_argument("--params", help="Parameters JSON, e.g. '{\"fast\": 10, \"slow\": 30}'")
    run_p.add_argument("--data-dir", help="Override data directory")
    run_p.add_argument(
        "--progress-stdout",
        action="store_true",
        help="Emit progress updates on stdout as PROGRESS:<percent>:<message>",
    )
    run_p.set_defaults(func=cmd_run)

    # Engines command
    eng_p = subparsers.add_parser("engines", help="List available engines")
    eng_p.set_defaults(func=cmd_engines)

    # Walk-forward validation command
    wf_p = subparsers.add_parser("walkforward", help="Run walk-forward validation")
    wf_p.add_argument("--strategy", "-s", required=True, help="Strategy name or path")
    wf_p.add_argument("--engine", default="vectorbt", help="Backtest engine")
    wf_p.add_argument("--symbols", required=True, help="Comma-separated symbols")
    wf_p.add_argument("--timeframe", "-tf", default="1d", help="Data timeframe")
    wf_p.add_argument("--start", required=True, help="Walk-forward start date")
    wf_p.add_argument("--end", required=True, help="Walk-forward end date")
    wf_p.add_argument("--capital", type=float, default=100000, help="Initial capital")
    wf_p.add_argument("--train-months", type=int, default=12, help="Training window in months")
    wf_p.add_argument("--test-months", type=int, default=3, help="Out-of-sample test window in months")
    wf_p.add_argument("--step-months", type=int, default=1, help="Rolling step in months")
    wf_p.add_argument("--output-dir", default="", help="Override output directory")
    wf_p.add_argument("--risk-pct", type=float, default=None, help="Risk per trade")
    wf_p.add_argument("--max-position-pct", type=float, default=None)
    wf_p.add_argument("--max-leverage", type=float, default=None)
    wf_p.add_argument("--intrabar-resolution", default="open_close")
    wf_p.add_argument("--params", default="", help="Strategy params JSON")
    wf_p.add_argument("--data-dir", default="", help="Override data directory")
    wf_p.set_defaults(func=cmd_walkforward)

    # Backtest grid command
    grid_p = subparsers.add_parser("grid", help="Run one strategy across a symbol/timeframe grid")
    grid_p.add_argument("--strategy", "-s", required=True, help="Path to strategy file")
    grid_p.add_argument("--symbols", default="XAUUSD,SP500,NAS100,EURUSD,USDJPY,OIL", help="Comma-separated symbols")
    grid_p.add_argument("--timeframes", default="1m,5m,15m,1h,4h,1d", help="Comma-separated timeframes")
    grid_p.add_argument("--start", default="2020-01-01", help="Start date")
    grid_p.add_argument("--end", default="", help="End date, empty = auto-detect")
    grid_p.add_argument("--engine", default="vectorbt", help="Backtest engine")
    grid_p.add_argument("--capital", type=float, default=100000, help="Initial capital")
    grid_p.add_argument("--risk-pct", type=float, default=0.01, help="Risk per trade")
    grid_p.add_argument("--max-position-pct", type=float, default=None)
    grid_p.add_argument("--max-leverage", type=float, default=None)
    grid_p.add_argument("--intrabar-resolution", default="open_close")
    grid_p.add_argument("--params", default="", help='Strategy params JSON')
    grid_p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    grid_p.add_argument("--data-dir", default="", help="Override data directory")
    grid_p.add_argument("--max-workers", type=int, default=1, help="Parallel grid cells")
    grid_p.add_argument("--timeout-seconds", type=int, default=1800, help="Timeout per grid cell")
    grid_p.add_argument("--resume", action="store_true", help="Skip cells with existing output JSON")
    grid_p.add_argument("--subprocess", action="store_true", help="Run each grid cell via runner.cli subprocess")
    grid_p.add_argument("--skip-sqlite", action="store_true", help="Do not persist grid cells to SQLite")
    grid_p.add_argument("--keep-indicators", action="store_true", help="Keep indicator arrays in grid output JSONs")
    grid_p.set_defaults(func=cmd_grid)

    # Backtest matrix command
    matrix_p = subparsers.add_parser("matrix", help="Run all strategies across a symbol/timeframe grid")
    matrix_p.add_argument("--strategy", "-s", action="append", default=[], help="Strategy slug/name/path to include; repeatable. Default: all.")
    matrix_p.add_argument("--strategies-dir", type=Path, default=Path("strategies"))
    matrix_p.add_argument("--symbols", default="XAUUSD,SP500,NAS100,EURUSD,USDJPY,OIL", help="Comma-separated symbols")
    matrix_p.add_argument("--timeframes", default="1m,5m,15m,1h,4h,1d", help="Comma-separated timeframes")
    matrix_p.add_argument("--start", default="2020-01-01", help="Start date")
    matrix_p.add_argument("--end", default="", help="End date, empty = auto-detect")
    matrix_p.add_argument("--engine", default="", help="Override engine. Empty uses each strategy's detected engine.")
    matrix_p.add_argument("--capital", type=float, default=100000, help="Initial capital")
    matrix_p.add_argument("--risk-pct", type=float, default=0.01, help="Risk per trade")
    matrix_p.add_argument("--max-position-pct", type=float, default=None)
    matrix_p.add_argument("--max-leverage", type=float, default=None)
    matrix_p.add_argument("--intrabar-resolution", default="open_close")
    matrix_p.add_argument("--params", default="", help='Strategy params JSON')
    matrix_p.add_argument("--output-dir", type=Path, default=MATRIX_OUTPUT_DIR)
    matrix_p.add_argument("--data-dir", default="", help="Override data directory")
    matrix_p.add_argument("--max-workers", type=int, default=1, help="Parallel grid cells per strategy")
    matrix_p.add_argument("--timeout-seconds", type=int, default=1800, help="Timeout per grid cell")
    matrix_p.add_argument("--resume", action="store_true", help="Skip/rehydrate cells with existing output JSON")
    matrix_p.add_argument("--subprocess", action="store_true", help="Run each grid cell via runner.cli subprocess")
    matrix_p.add_argument("--skip-sqlite", action="store_true", help="Do not persist grid cells to SQLite")
    matrix_p.add_argument("--keep-indicators", action="store_true", help="Keep indicator arrays in grid output JSONs")
    matrix_p.set_defaults(func=cmd_matrix)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
