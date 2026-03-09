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
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from runner.engines.base import BacktestConfig, EngineAdapter, resolve_strategy_parameters
from runner.stats import compute_stats_for_result

from shared.constants import DATA_DIR
from shared.logging import get_logger

logger = get_logger("runner")

BACKTESTS_DIR = DATA_DIR / "backtests"


def get_engine(name: str) -> EngineAdapter:
    """Get an engine adapter by name."""
    if name == "vectorbt":
        from runner.engines.vectorbt_adapter import VectorbtAdapter

        return VectorbtAdapter()
    elif name == "lean":
        from runner.engines.lean_adapter import LeanAdapter

        return LeanAdapter()
    elif name == "pynecore":
        from runner.engines.pynecore_adapter import PyneCoreAdapter

        return PyneCoreAdapter()
    else:
        raise ValueError(f"Unknown engine: {name}. Available: vectorbt, lean, pynecore")


def _detect_date_range(symbols: list[str], timeframe: str, data_dir: str = "") -> tuple[str, str]:
    """Auto-detect date range from available OHLCV data files.

    For multiple symbols, returns the intersection (common period).
    """
    ohlcv_root = Path(data_dir) / "ohlcv" if data_dir else DATA_DIR / "ohlcv"
    starts, ends = [], []

    for symbol in symbols:
        matches = list(ohlcv_root.rglob(f"**/{symbol}/{timeframe}.parquet"))
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
    engine = get_engine(args.engine)
    parameter_overrides = json.loads(args.params) if args.params else {}
    if not isinstance(parameter_overrides, dict):
        raise ValueError("--params must decode to a JSON object")
    resolved_parameters = resolve_strategy_parameters(args.strategy, parameter_overrides)

    # Auto-detect date range if not provided
    start = args.start
    end = args.end
    if not start or not end:
        detected_start, detected_end = _detect_date_range(
            args.symbols, args.timeframe, args.data_dir or ""
        )
        if not start:
            start = detected_start
        if not end:
            end = detected_end
        logger.info("Auto-detected date range: %s to %s", start, end)

    config = BacktestConfig(
        strategy_name=args.name or Path(args.strategy).stem,
        symbols=args.symbols,
        timeframe=args.timeframe,
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

    result = engine.run(config, args.strategy)
    result.stats = {**result.stats, **compute_stats_for_result(result)}

    # Save result
    BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.output:
        output_path = args.output
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = str(BACKTESTS_DIR / f"{config.strategy_name}_{ts}.trades.json")

    result.save(output_path)
    logger.info(
        "Result: %d trades, total PnL: $%.2f", len(result.trades), sum(t.pnl for t in result.trades)
    )
    logger.info("Saved to: %s", output_path)


def cmd_engines(args):
    """List available engines."""
    engines = [
        ("vectorbt", "Python vectorized backtesting (simple signal-based strategies)"),
        ("lean", "QuantConnect LEAN (complex event-driven strategies)"),
        ("pynecore", "PyneCore Pine-style Python strategies (indicator-rich)"),
    ]
    print("\nAvailable engines:\n")
    for name, desc in engines:
        print(f"  {name:12s}  {desc}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Backtest Runner")
    subparsers = parser.add_subparsers(dest="command")

    # Run command
    run_p = subparsers.add_parser("run", help="Run a backtest")
    run_p.add_argument("--engine", "-e", required=True, help="Engine: vectorbt, lean")
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
        help="Intrabar exit modeling: open_close, 1-lower, 1m, s1, tick",
    )
    run_p.add_argument("--name", "-n", help="Strategy name (default: filename)")
    run_p.add_argument("--output", "-o", help="Output file path")
    run_p.add_argument("--params", help='Parameters JSON, e.g. \'{"fast": 10, "slow": 30}\'')
    run_p.add_argument("--data-dir", help="Override data directory")
    run_p.set_defaults(func=cmd_run)

    # Engines command
    eng_p = subparsers.add_parser("engines", help="List available engines")
    eng_p.set_defaults(func=cmd_engines)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
