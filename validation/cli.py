"""CLI entrypoint for strategy validation report generation.

Compares a reference backtest result (external framework) with a platform
backtest result and produces a JSON or Markdown validation report.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import tempfile
from pathlib import Path

from validation.comparison.comparison import TradeComparisonConfig
from validation.comparison.delta_report import TradeDeltaReport
from validation.comparison.matcher import MatchResult, TradeMatcher
from validation.importers.mt5_html import MT5HTMLImporter
from validation.importers.mt5_xlsx import MT5XLSXImporter
from validation.importers.quantconnect_result import QuantConnectResultImporter
from validation.readiness.models import ReadinessConfig, ReadinessReport
from validation.readiness.validator import ReadinessValidator
from validation.reference import ValidationReference
from validation.regression.runner import RegressionConfig, run_regression
from validation.reporting.json_report import (
    ValidationReport,
    _build_trade_delta_report,
    _build_validation_report,
    generate_markdown_report,
    serialize_validation_report,
)
from validation.risk.comparison import (
    RiskComparator,
    RiskComparisonConfig,
    RiskComparisonReport,
)
from validation.stats.calculator import compute_trade_statistics

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )


def _load_validation_reference(
    path: Path,
    importer_name: str,
    timeframe: str | None = None,
) -> ValidationReference:
    """Load a ValidationReference from a file path.

    Supports JSON, XLSX and HTML.  JSON is parsed directly as a
    ``ValidationReference``.  XLSX and HTML are dispatched to the MT5
    importers.
    """
    suffix = path.suffix.lower()

    if suffix == ".json" and importer_name == "quantconnect":
        importer = QuantConnectResultImporter()
        if timeframe:
            return importer.load(path, timeframe=timeframe)
        return importer.load(path)
    if suffix == ".json":
        text = path.read_text(encoding="utf-8")
        try:
            return ValidationReference.model_validate_json(text)
        except ValueError:
            if importer_name != "auto":
                raise
            return QuantConnectResultImporter().load(path)

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
        choices=["auto", "mt5_xlsx", "mt5_html", "quantconnect"],
        default="auto",
        help="Importer to use for non-canonical files (default: auto).",
    )
    parser.add_argument(
        "--timeframe",
        help="Explicit source timeframe for importers that cannot infer it safely.",
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
    parser.add_argument(
        "--readiness-golden",
        "--regression-golden",
        dest="readiness_golden",
        help="Optional golden reference JSON; enables the full readiness gate.",
    )
    return parser


def _write_atomic(path: Path, output: str) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(output)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _initial_capital(reference: ValidationReference) -> float:
    metadata = reference.raw_metadata.get("parsed_meta", {})
    value = metadata.get("initial_capital") if isinstance(metadata, dict) else None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 100_000.0
    return number if math.isfinite(number) and number > 0.0 else 100_000.0


def _risk_report(
    reference: ValidationReference,
    platform: ValidationReference,
    time_tolerance: float,
    match_result: MatchResult,
) -> RiskComparisonReport:
    config = RiskComparisonConfig(
        time_tolerance_seconds=int(time_tolerance),
        account_balance=_initial_capital(reference),
    )
    return RiskComparator(config).compare(
        reference,
        platform,
        match_result=match_result,
    )


def _readiness_report(
    reference: ValidationReference,
    platform: ValidationReference,
    trade_delta_report: TradeDeltaReport,
    risk_report: RiskComparisonReport,
    golden_path: str,
    trade_config: TradeComparisonConfig,
    time_tolerance: float,
) -> ReadinessReport:
    regression_config = RegressionConfig(
        trade_config=trade_config,
        match_time_tolerance_seconds=int(time_tolerance),
    )
    regression = run_regression(
        golden_path,
        current_data=platform,
        config=regression_config,
    )
    return ReadinessValidator.validate(
        report=trade_delta_report,
        regression=regression,
        risk_report=risk_report,
        stats=(
            compute_trade_statistics(reference.trades),
            compute_trade_statistics(platform.trades),
        ),
        config=ReadinessConfig(),
    )


def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint.

    Returns 0 when validation passes, 1 when validation fails, and 2 on
    argument or I/O errors.
    """
    _setup_logging()
    parser = _build_argument_parser()
    args = parser.parse_args(argv)

    # Tolerances are magnitudes; a negative tolerance is meaningless and would
    # silently accept (or reject) every comparison.
    _tolerances = {
        "--time-tolerance": args.time_tolerance,
        "--price-tolerance": args.price_tolerance,
        "--volume-tolerance": args.volume_tolerance,
        "--pnl-absolute-tolerance": args.pnl_absolute_tolerance,
        "--stats-tolerance": args.stats_tolerance,
    }
    invalid = {
        name: value
        for name, value in _tolerances.items()
        if not math.isfinite(value) or value < 0
    }
    if invalid:
        for name, value in invalid.items():
            logger.error("%s must be finite and >= 0 (got %s)", name, value)
        return 2

    ref_path = Path(args.reference)
    platform_path = Path(args.platform)

    # --- load reference --------------------------------------------------
    try:
        reference = _load_validation_reference(
            ref_path,
            args.importer,
            timeframe=args.timeframe,
        )
    except FileNotFoundError as exc:
        logger.error(str(exc))
        return 2
    except Exception as exc:
        logger.error("Failed to load reference '%s': %s", ref_path, exc)
        return 2

    # --- load platform ---------------------------------------------------
    try:
        platform = _load_validation_reference(
            platform_path,
            args.importer,
            timeframe=args.timeframe,
        )
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

    try:
        match_result = TradeMatcher.match(
            reference.trades,
            platform.trades,
            time_tolerance_seconds=args.time_tolerance,
        )
        platform_bt = platform.to_backtest_result()
        trade_delta_report = _build_trade_delta_report(
            reference,
            platform_bt,
            time_tolerance_seconds=args.time_tolerance,
            trade_comparison_config=config,
            match_result=match_result,
        )
        risk_report = _risk_report(
            reference,
            platform,
            args.time_tolerance,
            match_result,
        )
        readiness = None
        if args.readiness_golden:
            readiness = _readiness_report(
                reference,
                platform,
                trade_delta_report,
                risk_report,
                args.readiness_golden,
                config,
                args.time_tolerance,
            )
        report: ValidationReport = _build_validation_report(
            reference,
            platform,
            trade_delta_report=trade_delta_report,
            risk_delta_report=risk_report,
            readiness=readiness,
            time_tolerance_seconds=args.time_tolerance,
            trade_comparison_config=config,
            stats_abs_tol=args.pnl_absolute_tolerance,
            stats_rel_tol=args.stats_tolerance,
        )
        if args.format == "json":
            output = serialize_validation_report(report)
        else:
            output = generate_markdown_report(report)
    except Exception as exc:
        logger.error("Report generation failed: %s", exc)
        return 2

    try:
        if args.output == "-":
            sys.stdout.write(output)
            if not output.endswith("\n"):
                sys.stdout.write("\n")
        else:
            _write_atomic(Path(args.output), output)
    except Exception as exc:
        logger.error("Failed to write output: %s", exc)
        return 2

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
