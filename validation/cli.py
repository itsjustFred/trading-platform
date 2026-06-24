"""CLI entrypoint for strategy validation report generation.

Compares a reference backtest result (external framework) with a platform
backtest result and produces a JSON or Markdown validation report.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from validation.comparison.comparison import TradeComparisonConfig
from validation.comparison.comparison import TradeComparator
from validation.comparison.matcher import TradeMatcher
from validation.importers.mt5_html import MT5HTMLImporter
from validation.importers.mt5_xlsx import MT5XLSXImporter
from validation.reference import ValidationReference
from validation.reporting.json_report import generate_json_report, generate_markdown_report

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )


def _load_validation_reference(path: Path, importer_name: str) -> ValidationReference:
    """Load a ValidationReference from a file path.

    Supports JSON, XLSX and HTML.  JSON is parsed directly as a
    ``ValidationReference``.  XLSX and HTML are dispatched to the MT5
    importers.
    """
    suffix = path.suffix.lower()

    if suffix == ".json":
        text = path.read_text(encoding="utf-8")
        return ValidationReference.model_validate_json(text)

    if importer_name == "auto":
        if suffix == ".xlsx":
            importer_name = "mt5_xlsx"
        elif suffix == ".html":
            importer_name = "mt5_html"
        else:
            raise ValueError(
                f"Cannot auto-detect importer for file '{path}' (unknown extension '{suffix}'). "
                "Use --importer to specify explicitly."
            )

    if importer_name == "mt5_xlsx":
        importer = MT5XLSXImporter()
        return importer.load(path)
    if importer_name == "mt5_html":
        importer = MT5HTMLImporter()
        return importer.load(path)

    raise ValueError(f"Unknown importer: {importer_name}")


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validation.cli",
        description="Generate a strategy validation report by comparing a reference "
                    "backtest result with the platform backtest result.",
    )
    parser.add_argument(
        "--reference", "-r",
        required=True,
        help="Path to the reference file (JSON, XLSX or HTML) or a ValidationReference JSON.",
    )
    parser.add_argument(
        "--platform", "-p",
        required=True,
        help="Path to the platform file (JSON, XLSX or HTML) or a ValidationReference JSON.",
    )
    parser.add_argument(
        "--format", "-f",
        choices=["json", "markdown"],
        default="markdown",
        help="Output format (default: markdown).",
    )
    parser.add_argument(
        "--output", "-o",
        default="-",
        help="Output file path.  Use '-' for stdout (default: '-').",
    )
    parser.add_argument(
        "--importer", "-i",
        choices=["auto", "mt5_xlsx", "mt5_html"],
        default="auto",
        help="Importer to use for non-JSON files (default: auto).",
    )
    parser.add_argument(
        "--time-tolerance", "-t",
        type=float,
        default=0.0,
        help="Time tolerance in seconds for trade matching (default: 0).",
    )
    parser.add_argument(
        "--price-tolerance",
        type=float,
        default=0.001,
        help="Relative price tolerance (default: 0.001).",
    )
    parser.add_argument(
        "--volume-tolerance",
        type=float,
        default=0.001,
        help="Relative volume tolerance (default: 0.001).",
    )
    parser.add_argument(
        "--pnl-absolute-tolerance",
        type=float,
        default=0.01,
        help="Absolute PnL tolerance (default: 0.01).",
    )
    parser.add_argument(
        "--stats-tolerance",
        type=float,
        default=0.001,
        help="Statistics relative tolerance (default: 0.001).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint.

    Returns 0 when validation passes, 1 when validation fails, and 2 on
    argument or I/O errors.
    """
    _setup_logging()
    parser = _build_argument_parser()
    args = parser.parse_args(argv)

    ref_path = Path(args.reference)
    platform_path = Path(args.platform)

    # --- load reference --------------------------------------------------
    try:
        reference = _load_validation_reference(ref_path, args.importer)
    except FileNotFoundError as exc:
        logger.error(str(exc))
        return 2
    except Exception as exc:
        logger.error("Failed to load reference '%s': %s", ref_path, exc)
        return 2

    # --- load platform ---------------------------------------------------
    try:
        platform = _load_validation_reference(platform_path, args.importer)
    except FileNotFoundError as exc:
        logger.error(str(exc))
        return 2
    except Exception as exc:
        logger.error("Failed to load platform '%s': %s", platform_path, exc)
        return 2

    # --- build comparator config -----------------------------------------
    config = TradeComparisonConfig(
        entry_time_seconds=args.time_tolerance,
        exit_time_seconds=args.time_tolerance,
        entry_price_pct=args.price_tolerance,
        exit_price_pct=args.price_tolerance,
        volume_pct=args.volume_tolerance,
        position_size_pct=args.volume_tolerance,
        pnl_pct=args.stats_tolerance,
        pnl_abs=args.pnl_absolute_tolerance,
    )

    # --- generate report -------------------------------------------------
    try:
        if args.format == "json":
            output = generate_json_report(
                reference,
                platform,
                time_tolerance_seconds=int(args.time_tolerance),
                trade_comparison_config=config,
                stats_abs_tol=args.pnl_absolute_tolerance,
                stats_rel_tol=args.stats_tolerance,
            )
        else:
            output = generate_markdown_report(
                reference,
                platform,
                time_tolerance_seconds=int(args.time_tolerance),
                trade_comparison_config=config,
                stats_abs_tol=args.pnl_absolute_tolerance,
                stats_rel_tol=args.stats_tolerance,
            )
    except Exception as exc:
        logger.error("Report generation failed: %s", exc)
        return 2

    # --- write output ----------------------------------------------------
    try:
        if args.output == "-":
            sys.stdout.write(output)
            if not output.endswith("\n"):
                sys.stdout.write("\n")
        else:
            out_path = Path(args.output)
            out_path.write_text(output, encoding="utf-8")
    except Exception as exc:
        logger.error("Failed to write output: %s", exc)
        return 2

    # --- exit code -------------------------------------------------------
    # Re-parse JSON output to determine pass/fail.
    try:
        if args.format == "json":
            report_data: dict[str, Any] = json.loads(output)
            passed = bool(report_data.get("passed", False))
        else:
            # For markdown we generate JSON internally but don't have it here.
            # We compute the overall pass/fail by running the pipeline again
            # via generate_json_report to get the passed flag.
            json_output = generate_json_report(
                reference,
                platform,
                time_tolerance_seconds=int(args.time_tolerance),
                trade_comparison_config=config,
                stats_abs_tol=args.pnl_absolute_tolerance,
                stats_rel_tol=args.stats_tolerance,
            )
            report_data = json.loads(json_output)
            passed = bool(report_data.get("passed", False))
    except Exception as exc:
        logger.error("Failed to determine report status: %s", exc)
        return 2

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
