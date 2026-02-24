"""
Backtest Runner CLI — unified interface to run backtests on any engine.

Usage:
    python -m runner.cli run --engine vectorbt --strategy strategies/example_sma.py \\
        --symbols AAPL MSFT --timeframe 1h --start 2023-01-01 --end 2025-01-01

    python -m runner.cli run --engine lean --strategy path/to/lean_result.json \\
        --symbols XAUUSD --name my_strategy

    python -m runner.cli engines    # list available engines
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from runner.engines.base import BacktestConfig, EngineAdapter

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("TRADING_DATA_DIR", Path.home() / "data"))
BACKTESTS_DIR = DATA_DIR / "backtests"


def get_engine(name: str) -> EngineAdapter:
    """Get an engine adapter by name."""
    if name == "vectorbt":
        from runner.engines.vectorbt_adapter import VectorbtAdapter
        return VectorbtAdapter()
    elif name == "lean":
        from runner.engines.lean_adapter import LeanAdapter
        return LeanAdapter()
    else:
        raise ValueError(f"Unknown engine: {name}. Available: vectorbt, lean")


def cmd_run(args):
    """Run a backtest."""
    engine = get_engine(args.engine)

    config = BacktestConfig(
        strategy_name=args.name or Path(args.strategy).stem,
        symbols=args.symbols,
        timeframe=args.timeframe,
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
        data_dir=args.data_dir or "",
        parameters=json.loads(args.params) if args.params else {},
    )

    # Validate
    errors = engine.validate_config(config)
    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        sys.exit(1)

    logger.info("Running backtest: engine=%s, strategy=%s, symbols=%s",
                engine.name, config.strategy_name, config.symbols)

    result = engine.run(config, args.strategy)

    # Save result
    BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.output:
        output_path = args.output
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = str(BACKTESTS_DIR / f"{config.strategy_name}_{ts}.trades.json")

    result.save(output_path)
    logger.info("Result: %d trades, total PnL: $%.2f",
                len(result.trades), sum(t.pnl for t in result.trades))
    logger.info("Saved to: %s", output_path)


def cmd_engines(args):
    """List available engines."""
    engines = [
        ("vectorbt", "Python vectorized backtesting (simple signal-based strategies)"),
        ("lean", "QuantConnect LEAN (complex event-driven strategies)"),
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
    run_p.add_argument("--start", default="2023-01-01", help="Start date")
    run_p.add_argument("--end", default="2025-12-31", help="End date")
    run_p.add_argument("--capital", type=float, default=100000, help="Initial capital")
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
