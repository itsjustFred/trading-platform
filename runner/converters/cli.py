"""
CLI to convert backtest results from various engines to the universal format.

Usage:
    python -m runner.converters.cli lean  path/to/lean_output.json -o result.json
    python -m runner.converters.cli stockkit path/to/trades.csv -o result.json --name ram_jet
    python -m runner.converters.cli list   # Show available converters
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from runner.converters.lean_converter import convert_lean_result
from runner.converters.stockkit_converter import convert_stockkit_trades

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

CONVERTERS = {
    "lean": {
        "description": "LEAN Engine JSON output",
        "extensions": [".json"],
        "convert": convert_lean_result,
    },
    "stockkit": {
        "description": "Stockkit AI Bot trade CSV (semicolon-separated)",
        "extensions": [".csv"],
        "convert": convert_stockkit_trades,
    },
}


def cmd_convert(args):
    """Convert a file using the specified engine converter."""
    engine = args.engine
    input_path = Path(args.input)

    if not input_path.exists():
        logger.error("Input file not found: %s", input_path)
        sys.exit(1)

    if engine not in CONVERTERS:
        logger.error("Unknown engine: %s. Use 'list' to see available converters.", engine)
        sys.exit(1)

    converter = CONVERTERS[engine]
    name = args.name

    if engine == "lean":
        result = converter["convert"](input_path, strategy_name=name)
    elif engine == "stockkit":
        result = converter["convert"](input_path, strategy_name=name)
    else:
        result = converter["convert"](input_path)

    # Update meta with symbols from trades
    if result.trades and not result.meta.data.get("symbols"):
        result.meta.data["symbols"] = sorted(set(t.symbol for t in result.trades))

    output = args.output or str(input_path.with_suffix(".trades.json"))
    result.save(output)
    logger.info(
        "Converted %d trades → %s (engine=%s, strategy=%s)",
        len(result.trades),
        output,
        engine,
        result.meta.strategies[0] if result.meta.strategies else "unknown",
    )


def cmd_list(args):
    """List available converters."""
    print("Available converters:\n")
    for name, info in CONVERTERS.items():
        exts = ", ".join(info["extensions"])
        print(f"  {name:12s}  {info['description']}  ({exts})")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Convert backtest results to the universal trade format",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Convert command (use engine name as subcommand)
    for engine_name, info in CONVERTERS.items():
        sub = subparsers.add_parser(engine_name, help=info["description"])
        sub.add_argument("input", help="Path to input file")
        sub.add_argument("-o", "--output", help="Output JSON path (default: <input>.trades.json)")
        sub.add_argument("-n", "--name", help="Strategy name override")
        sub.set_defaults(func=cmd_convert, engine=engine_name)

    # List command
    list_parser = subparsers.add_parser("list", help="Show available converters")
    list_parser.set_defaults(func=cmd_list)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
