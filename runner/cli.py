"""
Backtest Runner CLI — unified interface to run backtests on any engine.

Usage:
    python -m runner.cli run --engine vectorbt --strategy strategies/example_sma/example_sma.py \\
        --symbols AAPL MSFT --timeframe 1h --start 2023-01-01 --end 2025-01-01

    python -m runner.converters.cli lean path/to/lean_result.json \\
        --name my_strategy

    python -m runner.cli engines    # list available engines
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from data_tools.auto_download import ensure_backtest_data
from runner.approval_gate import require_generated_strategy_approval
from runner.backtest_grid import DEFAULT_OUTPUT_DIR, parse_csv_list, parse_params, run_backtest_grid
from runner.backtest_matrix import MATRIX_OUTPUT_DIR, run_backtest_matrix
from runner.daily_equity import attach_daily_equity
from runner.engines.base import BacktestConfig, EngineAdapter, resolve_strategy_parameters
from runner.stats import merge_canonical_stats
from runner.walkforward import run_walkforward
from shared.constants import DATA_DIR, normalize_timeframe
from shared.database import save_result_to_sqlite
from shared.logging import get_logger
from shared.models import StrategyType
from shared.sentry import initialize_sentry
from shared.strategy_registry import detect_strategy_type

initialize_sentry("runner")
logger = get_logger("runner")

BACKTESTS_DIR = DATA_DIR / "backtests"

HELP_INITIAL_CAPITAL = "Initial capital"
HELP_OVERRIDE_DATA_DIR = "Override data directory"
HELP_COMMA_SEPARATED_SYMBOLS = "Comma-separated symbols"
HELP_RISK_PER_TRADE = "Risk per trade"


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
    # The run already trusts this explicit path (cmd_run would import it via
    # load_strategy_module); contain the metadata read to the file's own
    # directory so user-selected paths outside the strategy tree still work.
    return detect_strategy_type(resolved, resolved.parent)


def _detect_date_range(symbols: list[str], timeframe: str, data_dir: str = "") -> tuple[str, str]:
    """Auto-detect date range from available OHLCV data files.

    For multiple symbols, returns the intersection (common period).
    Raises ValueError when no data is found or the symbols' covered periods
    are disjoint — fabricated ranges must never flow into backtests.

    Single-symbol callers with a plain ``data_dir`` may use the cheaper
    per-symbol facet in ``runner.backtest_grid._detect_date_range``; both share
    the same loud-failure contract (F19) over the same parquet layout.
    """
    ohlcv_root = Path(data_dir) / "ohlcv" if data_dir else DATA_DIR / "ohlcv"
    normalized_timeframe = normalize_timeframe(timeframe)
    starts, ends = [], []

    if not symbols:
        raise ValueError(
            "No symbols requested; refusing to fabricate a backtest date range"
        )

    for symbol in symbols:
        matches = sorted(
            ohlcv_root.rglob(f"**/{symbol}/{normalized_timeframe}.parquet"),
            key=lambda path: path.as_posix(),
        )
        if not matches:
            # Try without exact timeframe match — find any parquet for symbol
            matches = sorted(
                ohlcv_root.rglob(f"**/{symbol}/*.parquet"),
                key=lambda path: path.as_posix(),
            )
        if not matches:
            raise ValueError(
                f"No OHLCV data found for {symbol}/{normalized_timeframe} under "
                f"{ohlcv_root}; refusing to fabricate a backtest date range"
            )
        try:
            import pandas as pd

            df = pd.read_parquet(matches[0])
            df.columns = [c.lower() for c in df.columns]
            if "timestamp" in df.columns:
                ts = pd.to_datetime(df["timestamp"])
                starts.append(ts.min().strftime("%Y-%m-%d"))
                ends.append(ts.max().strftime("%Y-%m-%d"))
            else:
                logger.warning(
                    "No timestamp column in %s; symbol %s cannot contribute a date range",
                    matches[0], symbol,
                )
        except Exception as e:
            logger.warning("Could not read date range from %s: %s", matches[0], e)

    # A symbol whose file could not be read must not silently shrink the
    # intersection — every requested symbol has to contribute a range.
    if len(starts) != len(symbols) or len(ends) != len(symbols):
        raise ValueError(
            f"OHLCV data for {symbols} under {ohlcv_root} has no readable "
            "timestamps for every symbol; refusing to fabricate a backtest "
            "date range"
        )

    # Intersection: latest start, earliest end
    detected_start, detected_end = max(starts), min(ends)
    if detected_start > detected_end:
        raise ValueError(
            f"Symbols {symbols} have disjoint data coverage "
            f"({detected_start} > {detected_end}); refusing to backtest an "
            "empty intersection"
        )
    return detected_start, detected_end


def cmd_run(args):
    """Run a backtest."""
    strategy_type = _resolve_strategy_type(args.strategy)
    if strategy_type == StrategyType.SIGNAL_PROCESSOR:
        raise ValueError(
            "Detected signal_processor strategy. "
            "runner.cli run currently supports market_data strategies only."
        )

    require_generated_strategy_approval(args.strategy)
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
    # #84: explicit fee declaration rides into engine params (hard error
    # downstream when absent).
    resolved_parameters = {**resolved_parameters, **_fee_params_from_args(args)}

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
            max_position_exposure_pct=getattr(args, "max_position_exposure_pct", None),
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
    merge_canonical_stats(result)

    # Save result
    _emit_progress(progress_callback, 95.0, "Saving results...")
    BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.output:
        output_path = args.output
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = str(BACKTESTS_DIR / f"{config.strategy_name}_{ts}.trades.json")

    if not getattr(args, "keep_indicators", False):
        # Default is indicator-stripped output (F3): strip BEFORE saving so the
        # serialized JSON omits indicators. The flag exists only on the
        # grid/matrix commands, which opt in explicitly.
        result.indicators = []
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
    params = {**params, **_fee_params_from_args(args)}
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
        max_position_exposure_pct=args.max_position_exposure_pct,
        max_leverage=args.max_leverage,
        modeling_resolution=args.intrabar_resolution,
        params=params,
        data_dir=args.data_dir or "",
        output_dir=args.output_dir,
        purge_days=getattr(args, "purge_days", 0),
        embargo_days=getattr(args, "embargo_days", 0),
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
        max_position_exposure_pct=args.max_position_exposure_pct,
        max_leverage=args.max_leverage,
        intrabar_resolution=args.intrabar_resolution,
        params={**parse_params(args.params), **_fee_params_from_args(args)},
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
        max_position_exposure_pct=args.max_position_exposure_pct,
        max_leverage=args.max_leverage,
        intrabar_resolution=args.intrabar_resolution,
        params={**parse_params(args.params), **_fee_params_from_args(args)},
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


def _add_sizing_flags(parser: argparse.ArgumentParser, *, risk_default: float | None) -> None:
    """Add the sizing flags shared by all subcommands (Cleanup 16-E semantics).

    - ``--risk-pct`` is the maximum loss per trade (fraction of equity).
    - ``--max-position-exposure-pct`` caps the notional of ONE position as a
      fraction of equity (position-size semantics). On the native engine this
      value is applied as the risk engine's aggregate
      ``RiskConfig.max_total_exposure`` (documented divergence — F7, see
      ``docs/sizing-semantics.md``).
    ``--max-position-pct`` is a deprecated alias of
    ``--max-position-exposure-pct`` kept so old invocations keep working.
    """
    parser.add_argument(
        "--risk-pct",
        type=float,
        default=risk_default,
        help="Max loss per trade as a fraction of equity (decimal, e.g. 0.01 for 1%%)",
    )
    parser.add_argument(
        "--max-position-exposure-pct",
        action=_SizingAliasAction,
        dest="max_position_exposure_pct",
        type=float,
        default=None,
        help=(
            "Per-position notional cap as a fraction of equity (e.g. 1.0 = 100%%). "
            "Caps ONE position's exposure — not a leverage grant; "
            "--max-leverage controls leverage separately."
        ),
    )
    parser.add_argument(
        "--max-position-pct",
        action=_SizingAliasAction,
        dest="max_position_exposure_pct",
        type=float,
        default=None,
        help="Deprecated alias of --max-position-exposure-pct",
    )
    parser.add_argument(
        "--max-leverage",
        type=float,
        default=None,
        help="Max notional / equity allowed with leverage",
    )


def _add_fee_flags(parser: argparse.ArgumentParser) -> None:
    """Explicit fee/slippage declaration flags (#84: no silent gross)."""
    parser.add_argument(
        "--fee-rate",
        type=float,
        default=None,
        help="Fee as fraction of notional per side, e.g. 0.0005",
    )
    parser.add_argument(
        "--fee-flat-per-trade",
        type=float,
        default=None,
        help="Flat fee surcharge per closed trade",
    )
    parser.add_argument(
        "--fees-zero",
        action="store_true",
        help="Explicitly declare a no-cost run (conscious opt-in, not an omission)",
    )


def _fee_params_from_args(args) -> dict:
    """Merge CLI fee flags into engine params, failing closed (#84)."""
    params: dict = {}
    if getattr(args, "fee_rate", None) is not None:
        params["fee_rate"] = float(args.fee_rate)
    if getattr(args, "fee_flat_per_trade", None) is not None:
        params["fee_flat_per_trade"] = float(args.fee_flat_per_trade)
    if getattr(args, "fees_zero", False):
        if "fee_rate" in params or "fee_flat_per_trade" in params:
            raise ValueError("--fees-zero conflicts with --fee-rate/--fee-flat-per-trade")
        params["fees"] = "zero"
    return params


class _SizingAliasAction(argparse.Action):
    """Store the sizing value; warn when the deprecated spelling was used.

    Both ``--max-position-exposure-pct`` and its deprecated alias
    ``--max-position-pct`` store the same destination, so a conflict between
    the two spellings is rejected outright.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        previous = getattr(namespace, self.dest, None)
        if previous is not None and values is not None and previous != values:
            parser.error(
                f"{option_string} conflicts with an already-provided sizing flag"
            )
        if option_string == "--max-position-pct":
            warnings.warn(
                "--max-position-pct is deprecated; use --max-position-exposure-pct "
                "(same per-position notional/equity semantics). The alias will be "
                "removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )
        setattr(namespace, self.dest, values)


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


def build_parser() -> argparse.ArgumentParser:
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
    _add_sizing_flags(run_p, risk_default=None)
    _add_fee_flags(run_p)
    run_p.add_argument(
        "--intrabar-resolution",
        default="open_close",
        help="Intrabar exit modeling: main_timeframe, open_close, or explicit finer timeframe",
    )
    run_p.add_argument("--name", "-n", help="Strategy name (default: filename)")
    run_p.add_argument("--output", "-o", help="Output file path")
    run_p.add_argument("--result-id", help="Persisted BacktestMeta.id override")
    run_p.add_argument(
        "--skip-sqlite", action="store_true", help="Do not persist this run to SQLite"
    )
    run_p.add_argument("--params", help="Parameters JSON, e.g. '{\"fast\": 10, \"slow\": 30}'")
    run_p.add_argument("--data-dir", help=HELP_OVERRIDE_DATA_DIR)
    run_p.add_argument(
        "--keep-indicators",
        action="store_true",
        help=(
            "Keep indicator arrays in the output JSON (grids pass this when "
            "--keep-indicators is set)"
        ),
    )
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
    wf_p.add_argument("--symbols", required=True, help=HELP_COMMA_SEPARATED_SYMBOLS)
    wf_p.add_argument("--timeframe", "-tf", default="1d", help="Data timeframe")
    wf_p.add_argument("--start", required=True, help="Walk-forward start date")
    wf_p.add_argument("--end", required=True, help="Walk-forward end date")
    wf_p.add_argument("--capital", type=float, default=100000, help=HELP_INITIAL_CAPITAL)
    wf_p.add_argument("--train-months", type=int, default=12, help="Training window in months")
    wf_p.add_argument(
        "--test-months", type=int, default=3, help="Out-of-sample test window in months"
    )
    wf_p.add_argument(
        "--step-months",
        type=int,
        default=None,
        help="Rolling step in months; defaults to the test window",
    )
    wf_p.add_argument(
        "--purge-days", type=int, default=0, help="Days purged before each test fold"
    )
    wf_p.add_argument(
        "--embargo-days", type=int, default=0, help="Days embargoed between train and test"
    )
    wf_p.add_argument("--output-dir", default="", help="Override output directory")
    _add_sizing_flags(wf_p, risk_default=None)
    _add_fee_flags(wf_p)
    wf_p.add_argument("--intrabar-resolution", default="open_close")
    wf_p.add_argument("--params", default="", help="Strategy params JSON")
    wf_p.add_argument("--data-dir", default="", help=HELP_OVERRIDE_DATA_DIR)
    wf_p.set_defaults(func=cmd_walkforward)

    # Backtest grid command
    grid_p = subparsers.add_parser("grid", help="Run one strategy across a symbol/timeframe grid")
    grid_p.add_argument("--strategy", "-s", required=True, help="Path to strategy file")
    grid_p.add_argument(
        "--symbols",
        default="XAUUSD,SP500,NAS100,EURUSD,USDJPY,OIL",
        help=HELP_COMMA_SEPARATED_SYMBOLS,
    )
    grid_p.add_argument(
        "--timeframes",
        default="1m,5m,15m,1h,4h,1d",
        help="Comma-separated timeframes",
    )
    grid_p.add_argument("--start", default="2020-01-01", help="Start date")
    grid_p.add_argument("--end", default="", help="End date, empty = auto-detect")
    grid_p.add_argument("--engine", default="vectorbt", help="Backtest engine")
    grid_p.add_argument("--capital", type=float, default=100000, help="Initial capital")
    _add_sizing_flags(grid_p, risk_default=0.01)
    _add_fee_flags(grid_p)
    grid_p.add_argument("--intrabar-resolution", default="open_close")
    grid_p.add_argument("--params", default="", help='Strategy params JSON')
    grid_p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    grid_p.add_argument("--data-dir", default="", help=HELP_OVERRIDE_DATA_DIR)
    grid_p.add_argument("--max-workers", type=int, default=1, help="Parallel grid cells")
    grid_p.add_argument("--timeout-seconds", type=int, default=1800, help="Timeout per grid cell")
    grid_p.add_argument(
        "--resume", action="store_true", help="Skip cells with existing output JSON"
    )
    grid_p.add_argument(
        "--subprocess",
        action="store_true",
        help="Run each grid cell via runner.cli subprocess",
    )
    grid_p.add_argument(
        "--skip-sqlite", action="store_true", help="Do not persist grid cells to SQLite"
    )
    grid_p.add_argument(
        "--keep-indicators",
        action="store_true",
        help="Keep indicator arrays in grid output JSONs",
    )
    grid_p.set_defaults(func=cmd_grid)

    # Backtest matrix command
    matrix_p = subparsers.add_parser(
        "matrix", help="Run all strategies across a symbol/timeframe grid"
    )
    matrix_p.add_argument(
        "--strategy",
        "-s",
        action="append",
        default=[],
        help="Strategy slug/name/path to include; repeatable. Default: all.",
    )
    matrix_p.add_argument("--strategies-dir", type=Path, default=Path("strategies"))
    matrix_p.add_argument(
        "--symbols",
        default="XAUUSD,SP500,NAS100,EURUSD,USDJPY,OIL",
        help=HELP_COMMA_SEPARATED_SYMBOLS,
    )
    matrix_p.add_argument(
        "--timeframes",
        default="1m,5m,15m,1h,4h,1d",
        help="Comma-separated timeframes",
    )
    matrix_p.add_argument("--start", default="2020-01-01", help="Start date")
    matrix_p.add_argument("--end", default="", help="End date, empty = auto-detect")
    matrix_p.add_argument(
        "--engine",
        default="",
        help="Override engine. Empty uses each strategy's detected engine.",
    )
    matrix_p.add_argument("--capital", type=float, default=100000, help="Initial capital")
    _add_sizing_flags(matrix_p, risk_default=0.01)
    _add_fee_flags(matrix_p)
    matrix_p.add_argument("--intrabar-resolution", default="open_close")
    matrix_p.add_argument("--params", default="", help='Strategy params JSON')
    matrix_p.add_argument("--output-dir", type=Path, default=MATRIX_OUTPUT_DIR)
    matrix_p.add_argument("--data-dir", default="", help=HELP_OVERRIDE_DATA_DIR)
    matrix_p.add_argument(
        "--max-workers", type=int, default=1, help="Parallel grid cells per strategy"
    )
    matrix_p.add_argument(
        "--timeout-seconds", type=int, default=1800, help="Timeout per grid cell"
    )
    matrix_p.add_argument(
        "--resume",
        action="store_true",
        help="Skip/rehydrate cells with existing output JSON",
    )
    matrix_p.add_argument(
        "--subprocess",
        action="store_true",
        help="Run each grid cell via runner.cli subprocess",
    )
    matrix_p.add_argument(
        "--skip-sqlite", action="store_true", help="Do not persist grid cells to SQLite"
    )
    matrix_p.add_argument(
        "--keep-indicators",
        action="store_true",
        help="Keep indicator arrays in grid output JSONs",
    )
    matrix_p.set_defaults(func=cmd_matrix)

    return parser


if __name__ == "__main__":
    main()
