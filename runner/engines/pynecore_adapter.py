"""
PyneCore Engine Adapter — runs Pine-style Python strategies via PyneCore.

Data pipeline: Parquet → CSV (temp) → PyneCore execution → Trade CSV → Universal Format.
If a strategy defines build_trade_context(df, params), the adapter enriches each
parsed trade with logic-relevant indicator values in trade.meta.
"""

from __future__ import annotations

import ast
import csv
import inspect
import logging
import math
import shutil
import subprocess
import sys
import tempfile
from datetime import date as _date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from runner.agent_process import build_backtest_child_env
from runner.approval_gate import require_generated_strategy_approval
from runner.codegen_safety import BANNED_CALL_NAMES, import_root
from runner.engines import contract
from runner.engines._drawings import _normalize_drawings
from runner.engines._shared import (
    resolve_bar_spread,
)
from runner.engines._shared import (
    resolve_max_leverage as _resolve_max_leverage,
)
from runner.engines._shared import (
    resolve_max_position_exposure_pct as _resolve_max_position_exposure_pct,
)
from runner.engines._shared import (
    serialize_meta_value as _serialize_meta_value,
)
from runner.engines.base import (
    BacktestConfig,
    EngineAdapter,
    ProgressCallback,
    resolve_strategy_parameters,
)
from runner.fees import resolve_fee_model
from runner.stats import compute_stats_for_result
from shared.constants import DATA_DIR
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit

logger = logging.getLogger(__name__)

IndicatorMap = dict[str, tuple[pd.Series, str]]

#: Exit reasons that represent level fills (stop/TP). Their prices are the
#: strategy's levels — not re-priced by the signal-exit convention, mirroring
#: native _close_position (stop fills) and vectorbt (intrabar stop/TP legs).
_LEVEL_EXIT_REASONS = frozenset({"stop_loss", "take_profit", "trailing_stop"})


def _effective_load_start(config: BacktestConfig) -> str:
    """A-H4 (D3): warmup_start extends the loaded window backwards for
    indicator warmup, mirroring the native adapter."""
    warmup_raw = str(getattr(config, "warmup_start", "") or "").strip()
    return warmup_raw or config.start_date


def _pyne_bar_spread(row: pd.Series, parameters: dict[str, Any] | None) -> float:
    """A-H4 (D3): per-bar spread surcharge mirroring native _bar_spread."""
    return resolve_bar_spread(row, (parameters or {}).get("spread_price_col"))


def _gate_warmup_trades(trades: list[Trade], config: BacktestConfig) -> list[Trade]:
    """A-H4 (D3): drop trades entered before start_date when warmup_start is
    set — warmup bars feed indicators only, mirroring native TradeEngine.run
    and the vectorbt warmup gate. No warmup configured: passthrough."""
    warmup_raw = str(getattr(config, "warmup_start", "") or "").strip()
    if not warmup_raw:
        return trades
    # A-M11 parity with native TradeEngine.run and the vectorbt gate: a
    # warmup window without a parseable start_date must fail loudly — the
    # previous passthrough silently traded the warmup bars here while the
    # other engines refused the run.
    trade_from = pd.to_datetime(config.start_date, utc=True, errors="coerce")
    if pd.isna(trade_from):
        raise ValueError(
            "warmup_start is set but start_date "
            f"({config.start_date!r}) is empty or unparseable — the "
            "warmup gate cannot derive trade_from"
        )
    kept: list[Trade] = []
    for trade in trades:
        entry_ts = _normalize_trade_timestamp(trade.entry_time)
        if entry_ts is not None and entry_ts < trade_from:
            continue
        kept.append(trade)
    return kept


def _apply_fill_parity(
    trades: list[Trade], df: pd.DataFrame, config: BacktestConfig, parameters: dict[str, Any]
) -> None:
    """A-H4 (D3): post-hoc fill parity with the native/vectorbt convention.

    The PyneCore engine reports fills without spread/ask awareness and
    without a signal-exit-fill convention. This re-prices deterministically
    from the loaded OHLCV frame, mirroring native semantics exactly:

    - LONG entries lift the ask (bar open + ``spread_price_col`` spread);
      SHORT entries hit the bid (unchanged). Only applied when the spread
      column is configured AND the entry bar is locatable — otherwise the
      engine price stands (default off = unchanged behavior).
    - Non-level exits (anything but stop_loss/take_profit/trailing_stop)
      follow ``signal_exit_fill``: "close" (default) fills at the exit bar's
      close, "open" at its open; SHORT open-exits pay the spread (ask).
      Level exits keep the engine price (stop-market fills at the level).
    - Touched legs get their pnl recomputed from the new prices so
      trade.pnl stays consistent ahead of risk sizing.

    Known limitation (documented, not silent): stop/TP TRIGGER TIMING stays
    engine-internal — PyneCore detects triggers on its own feed without an
    ask-side distinction, so ask_ohlc cannot move a stop earlier post-hoc.
    Price parity holds (stops fill at the level in every engine); only the
    ask-side early-trigger edge is native/vectorbt-only.
    """
    if not trades or df is None or df.empty or "timestamp" not in df.columns:
        return
    fill_at_open = str(getattr(config, "signal_exit_fill", "close") or "close").lower() == "open"
    timestamp_series = df["timestamp"]
    for trade in trades:
        entry_idx = _locate_trade_bar(timestamp_series, trade.entry_time)
        if entry_idx is not None and trade.direction == "LONG":
            spread = _pyne_bar_spread(df.iloc[entry_idx], parameters)
            if spread > 0:
                try:
                    base_open = float(df.iloc[entry_idx]["open"])
                except (KeyError, TypeError, ValueError):
                    base_open = None
                if base_open is not None:
                    trade.entry_price = round(base_open + spread, 6)
        if not trade.exits:
            continue
        for leg in trade.exits:
            reason = str(getattr(leg, "reason", "") or "")
            if reason in _LEVEL_EXIT_REASONS:
                continue
            exit_idx = _locate_trade_bar(timestamp_series, leg.time)
            if exit_idx is None:
                continue
            row = df.iloc[exit_idx]
            try:
                if fill_at_open:
                    base = float(row["open"])
                else:
                    base = float(row["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if fill_at_open and trade.direction == "SHORT":
                base += _pyne_bar_spread(row, parameters)
            leg.price = round(base, 6)
        try:
            total = 0.0
            for leg in trade.exits:
                qty = float(getattr(leg, "quantity", 0.0) or 0.0)
                if trade.direction == "LONG":
                    total += (float(leg.price) - float(trade.entry_price)) * qty
                else:
                    total += (float(trade.entry_price) - float(leg.price)) * qty
            trade.pnl = round(total, 2)
            if trade.exits:
                last = trade.exits[-1]
                last.pnl = round(
                    (
                        (float(last.price) - float(trade.entry_price))
                        if trade.direction == "LONG"
                        else (float(trade.entry_price) - float(last.price))
                    )
                    * float(getattr(last, "quantity", 0.0) or 0.0),
                    2,
                )
        except (TypeError, ValueError):
            continue


def _pyne_cli_end_bound(end_date: str) -> str:
    """Translate a config end date into the ``pyne`` CLI's ``--to`` bound.

    Conformance (Cleanup 16-A, F9): a date-only end boundary is inclusive of
    the whole end day. The adapter's own parquet loader enforces that via
    :func:`contract.apply_date_filter`, but the external ``pyne`` CLI applies
    its own window: a date-only ``--to`` value is parsed with
    ``datetime.fromisoformat`` into a **midnight** bound (bars stamped
    ``01:00``–``23:00`` of the end date are dropped from the replay). Passing
    the raw ``end_date`` through therefore re-introduces the exact
    midnight-truncation the F9 Decision Record removed everywhere else, and
    pyne's replay window becomes a strict subset of native/vectorbt's for the
    same configuration.

    A date-only end date is translated to the inclusive end-of-day instant
    (``"2024-01-01"`` → ``"2024-01-01T23:59:59.999999+00:00"``), which the CLI
    parses and applies as-is; a value that already carries a time component
    already states the intended bound and is passed through unchanged.

    Timezone note: the translated instant carries an explicit ``+00:00``
    offset, so the CLI's host-TZ handling (see
    :func:`_pyne_cli_start_bound`) can never shift it.
    """
    raw = str(end_date or "").strip()
    if not raw:
        return raw
    if "T" not in raw and ":" not in raw:
        return contract.parse_date_bound(raw, is_end=True).isoformat()
    return raw


def _pyne_cli_start_bound(start_date: str) -> str:
    """Translate a config start date into the ``pyne`` CLI's ``--from`` bound.

    Conformance (Cleanup 16-A, F9): naive timestamps are interpreted as UTC.
    The CLI, however, parses ``--from`` with ``datetime.fromisoformat`` and
    resolves the resulting NAIVE datetime through ``timestamp()`` — i.e. in
    the HOST timezone. A date-only ``--from 2024-01-01`` therefore lands at
    ``2024-01-01T05:00Z`` on an ``America/New_York`` host and silently drops
    the first five UTC bars of the day (verified against pynesys-pynecore
    6.8.14): the replay window would depend on where the adapter runs, not on
    the configuration.

    Translating the date-only start to an AWARE UTC start-of-day instant
    (``"2024-01-01"`` → ``"2024-01-01T00:00:00+00:00"``) makes the bound
    host-TZ independent; a value that already carries a time component
    already states the intended bound and is passed through unchanged.
    """
    raw = str(start_date or "").strip()
    if not raw:
        return raw
    if "T" not in raw and ":" not in raw:
        return contract.parse_date_bound(raw, is_end=False).isoformat()
    return raw


class PyneCoreAdapter(EngineAdapter):
    """Adapter for PyneCore-based Pine-style backtesting."""

    @property
    def name(self) -> str:
        return "pynecore"

    def run(
        self,
        config: BacktestConfig,
        strategy_path: str,
        progress_callback: ProgressCallback | None = None,
    ) -> BacktestResult:
        require_generated_strategy_approval(strategy_path)
        data_root = Path(config.data_dir) if config.data_dir else DATA_DIR
        resolved_parameters = resolve_strategy_parameters(strategy_path, config.parameters)
        # #84: missing fee/slippage config is a hard error, never silent gross.
        fee_model = resolve_fee_model(resolved_parameters)
        # Fail before any per-symbol work when the CLI is absent.
        _resolve_pyne_bin()
        all_trades: list[Trade] = []
        all_stats: dict[str, Any] = {}
        all_drawings: list[dict[str, Any]] = []
        asset_classes: dict[str, str] = {}
        trade_counter = 0
        total_bars = 0
        strategy_name, context_builder, drawings_builder = _load_strategy_artifacts(
            strategy_path, config.strategy_name
        )

        for symbol in config.symbols:
            # A-H4 (D3): load from warmup_start when set (indicator warmup,
            # mirroring the native adapter); trades are gated to start_date
            # after parsing.
            load_start = _effective_load_start(config)
            df = _load_ohlcv(
                data_root, symbol, config.timeframe, load_start, config.end_date
            )
            if df is None or df.empty:
                # Same rule as the native/vectorbt adapters (F24): a symbol with
                # no data is a failed run, not a silently skipped one.
                raise RuntimeError(
                    f"No OHLCV data for {symbol}/{config.timeframe} in "
                    f"{config.start_date}..{config.end_date} under {data_root}; "
                    "refusing to run a silent empty PyneCore backtest"
                )

            total_bars += len(df)
            trades, symbol_stats = self._run_pyne_for_symbol(
                config, strategy_path, symbol, df, context_builder, trade_counter
            )
            asset_class = _infer_asset_class(
                Path(config.data_dir) if config.data_dir else DATA_DIR,
                symbol,
                config.timeframe,
            )
            asset_classes[str(symbol).upper()] = asset_class
            trade_counter += len(trades)
            all_trades.extend(trades)
            # Merge, never overwrite (F11): `all_stats =` here would silently
            # keep only the last symbol's stats. `update` is load-bearing.
            all_stats.update(symbol_stats)

            all_drawings.extend(
                _build_drawings(
                    drawings_builder,
                    df,
                    resolved_parameters,
                    trades,
                    symbol,
                )
            )

        _apply_risk_sizing(
            all_trades,
            config,
            asset_class_by_symbol=asset_classes,
            fee_model=fee_model,
        )
        result = BacktestResult(
            meta=_pyne_meta(config, strategy_name, resolved_parameters, total_bars),
            trades=all_trades,
            equity_curve=_equity_curve_from_trades(config, all_trades),
            stats=all_stats,
            drawings=all_drawings,
        )
        # Conformance (Cleanup 16-A): every engine exposes the canonical
        # BacktestStats key set; the engine-specific strategy stats are merged
        # on top of the derived ones. Contract §6: engine keys may add, never
        # remove — canonical keys win a name collision.
        result.stats = contract.EngineStatsKeys.merge_over_canonical(
            compute_stats_for_result(result), result.stats
        )
        return result

    def _run_pyne_for_symbol(
        self,
        config: BacktestConfig,
        strategy_path: str,
        symbol: str,
        df: Any,
        context_builder: Any,
        trade_counter: int,
    ) -> tuple[list[Trade], dict[str, Any]]:
        """Run the pyne CLI for one symbol and parse its trade/stats output."""
        resolved_parameters = resolve_strategy_parameters(strategy_path, config.parameters)
        trade_context = _build_trade_context(context_builder, df, resolved_parameters)
        trades: list[Trade] = []
        stats: dict[str, Any] = {}

        with tempfile.TemporaryDirectory(prefix="pynecore_") as tmpdir:
            tmpdir_path = Path(tmpdir)
            workdir_path = tmpdir_path / "workdir"
            workdir_path.mkdir()
            strategy_copy_path = tmpdir_path / Path(strategy_path).name
            strategy_copy_path.write_text(Path(strategy_path).read_text())

            csv_path = tmpdir_path / f"{symbol}_{config.timeframe}.csv"
            df[["timestamp", "open", "high", "low", "close", "volume"]].to_csv(
                csv_path,
                index=False,
            )

            trade_csv = tmpdir_path / "trades.csv"
            stats_csv = tmpdir_path / "stats.csv"

            self._run_pyne_cli(
                config,
                symbol,
                strategy_copy_path,
                csv_path,
                trade_csv,
                stats_csv,
                workdir_path,
                tmpdir_path,
                len(df),
            )

            if trade_csv.exists():
                trades = _parse_trade_csv(trade_csv, symbol, trade_counter)
                if trade_context:
                    _enrich_trades_with_context(trades, trade_context, df)
                _normalize_trade_exits(trades)
                # A-H4 (D3): warmup gate + fill parity before sizing so the
                # resized quantities/pnl build on parity prices.
                trades = _gate_warmup_trades(trades, config)
                _apply_fill_parity(trades, df, config, resolved_parameters)


            if stats_csv.exists():
                stats.update(_parse_stats_csv(stats_csv))

        return trades, stats

    def _run_pyne_cli(
        self,
        config: BacktestConfig,
        symbol: str,
        strategy_copy_path: Path,
        csv_path: Path,
        trade_csv: Path,
        stats_csv: Path,
        workdir_path: Path,
        tmpdir_path: Path,
        bar_count: int,
    ) -> None:
        """Invoke the pyne CLI once; a non-zero exit or timeout raises instead
        of degrading into an empty success."""
        pyne_bin = _resolve_pyne_bin()
        cmd = [
            pyne_bin,
            "--workdir",
            str(workdir_path),
            "run",
            str(strategy_copy_path),
            str(csv_path),
            "--trade",
            str(trade_csv),
            "--strat",
            str(stats_csv),
        ]
        if config.start_date:
            # A-H4 (D3): the CLI window starts at the warmup date when set
            # (the CSV already carries the extended frame); strategy-time
            # gating to start_date happens in _gate_warmup_trades.
            cmd.extend(["--from", _pyne_cli_start_bound(_effective_load_start(config))])
        if config.end_date:
            cmd.extend(["--to", _pyne_cli_end_bound(config.end_date)])

        logger.info(
            "Running PyneCore: %s on %s (%d bars)", config.strategy_name, symbol, bar_count
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(tmpdir_path),
                env=build_backtest_child_env(),
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            logger.exception(
                "PyneCore run for %s timed out after %s seconds", symbol, exc.timeout
            )
            raise RuntimeError(
                f"PyneCore run for {symbol} timed out after {exc.timeout} seconds"
            ) from exc

        if result.returncode != 0:
            logger.error(
                "PyneCore failed for %s (exit code %d): %s",
                symbol,
                result.returncode,
                (result.stderr or result.stdout or "")[-500:],
            )
            raise RuntimeError(
                f"PyneCore run failed for {config.strategy_name}/{symbol} "
                f"(exit code {result.returncode}): "
                f"{(result.stderr or result.stdout or '')[-2000:]}"
            )

    def validate_config(self, config: BacktestConfig) -> list[str]:
        errors = super().validate_config(config)
        try:
            import pynecore  # noqa: F401
        except ImportError:
            errors.append(
                "PyneCore not installed. Run: pip install pynesys-pynecore"
            )
        return errors


def _resolve_pyne_bin() -> str:
    """Locate the pyne CLI; a missing binary is a loud failure, never a skip.

    Module-level so a run fails before any temp dir is built for its first
    symbol (the pre-F11 behaviour ran the whole grid, then skipped every cell).
    """
    pyne_bin = shutil.which("pyne")
    if not pyne_bin:
        venv_bin = Path(sys.executable).parent / "pyne"
        if venv_bin.exists():
            return str(venv_bin)
        raise RuntimeError(
            "PyneCore CLI ('pyne') not found on PATH. "
            "Install with: pip install pynesys-pynecore"
        )
    return pyne_bin


def _apply_fee_model(all_trades: list[Trade], fee_model) -> None:
    """Price pyne-binary fills through the explicit fee model (#84).

    The external binary reports gross fills; fees/net are attached here so
    stats and equity never see silent gross.
    """
    for trade in all_trades:
        exit_price = trade.exits[-1].price if trade.exits else trade.entry_price
        fees = round(
            float(fee_model.fee_for_trade(trade.entry_price, exit_price, trade.quantity)),
            2,
        )
        trade.fees = fees
        trade.net_pnl = round(float(trade.pnl) - fees, 2)


def _equity_curve_from_trades(config: BacktestConfig, all_trades: list[Trade]) -> list[EquityPoint]:
    """Build a net, exit-timed equity curve from merged trades."""
    equity = float(config.initial_capital)
    events: list[tuple[str, float]] = []
    for trade in all_trades:
        exits = sorted(trade.exits or [], key=lambda item: str(item.time))
        if not exits:
            events.append((str(trade.entry_time), float(trade.net_pnl or 0.0)))
            continue
        gross_total = sum(float(leg.pnl or 0.0) for leg in exits)
        net_total = (
            float(trade.net_pnl)
            if trade.net_pnl is not None
            else float(trade.pnl) - float(trade.fees or 0.0)
        )
        fee_delta = net_total - gross_total
        for leg in exits:
            gross_leg = float(leg.pnl or 0.0)
            share = gross_leg / gross_total if gross_total else 1.0 / len(exits)
            events.append((str(leg.time), gross_leg + fee_delta * share))

    if not events:
        return []
    start = pd.to_datetime(config.start_date, utc=True, errors="coerce")
    if pd.isna(start):
        start = pd.to_datetime(events[0][0], utc=True)
    curve = [EquityPoint(time=pd.Timestamp(start).isoformat(), equity=round(equity, 2))]
    for timestamp, pnl in sorted(events, key=lambda item: (pd.Timestamp(item[0]), item[0])):
        equity += pnl
        point = EquityPoint(time=pd.Timestamp(timestamp).isoformat(), equity=round(equity, 2))
        if curve[-1].time == point.time:
            curve[-1] = point
        else:
            curve.append(point)
    return curve


def _pyne_meta(
    config: BacktestConfig,
    strategy_name: str,
    resolved_parameters: dict[str, Any],
    total_bars: int,
) -> BacktestMeta:
    """Assemble the BacktestMeta for a pyne run."""
    duration_days = 0
    try:
        d_start = _date.fromisoformat(config.start_date)
        d_end = _date.fromisoformat(config.end_date)
        duration_days = (d_end - d_start).days
    except (ValueError, TypeError):
        pass

    now = datetime.now(timezone.utc)
    return BacktestMeta(
        id=f"pyne-{config.strategy_name}-{now.strftime('%Y%m%d-%H%M%S')}",
        created_at=now.isoformat(),
        engine="pynecore",
        strategies=[strategy_name],
        backtest_start=config.start_date,
        backtest_end=config.end_date,
        duration_days=duration_days,
        total_bars=total_bars,
        data={
            "timeframes": [config.timeframe],
            "symbols": config.symbols,
            "universe": "",
            "source": "yfinance",
        },
        initial_capital=config.initial_capital,
        risk_per_trade_pct=config.risk_per_trade_pct,
        max_position_exposure_pct=config.max_position_exposure_pct,
        max_leverage=config.max_leverage,
        modeling_resolution=config.modeling_resolution,
        signal_exit_fill=config.signal_exit_fill,
        allow_fallback_stop=config.allow_fallback_stop,
        reject_missing_stop=config.reject_missing_stop,
        require_explicit_column_roles=config.require_explicit_column_roles,
        parameters=resolved_parameters,
    )


def _load_strategy_artifacts(path: str, fallback_name: str) -> tuple[str, Any, Any]:
    source_path = Path(path).resolve()
    source = source_path.read_text()
    tree = ast.parse(source, filename=str(source_path))

    strategy_name = fallback_name

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "STRATEGY_META":
                    try:
                        strategy_meta = ast.literal_eval(node.value)
                        if isinstance(strategy_meta, dict):
                            strategy_name = str(strategy_meta.get("name", strategy_name))
                    except (ValueError, SyntaxError):
                        pass
    helpers = _load_strategy_helper_namespace(source_path, source)
    return strategy_name, helpers.get("build_trade_context"), helpers.get("build_drawings")


def _load_strategy_helper_namespace(strategy_path: Path, source: str) -> dict[str, Any]:
    require_generated_strategy_approval(strategy_path)
    namespace: dict[str, Any] = {}

    helper_path = strategy_path.with_name(f"{strategy_path.stem}_context.py")
    if helper_path.exists():
        namespace.update(
            _compile_helper_namespace(
                helper_path,
                helper_path.read_text(),
                strict_structure=True,
            )
        )

    source_namespace = _compile_helper_namespace(
        strategy_path,
        source,
        function_names={"build_trade_context", "build_drawings"},
        # The main strategy file is a PyneCore script executed out-of-process
        # by the ``pyne`` CLI; its ``pynecore`` imports are the engine contract
        # and are never needed by the in-process helper namespace, so they are
        # dropped here instead of failing the screen. Every other import is
        # still screened against the whitelist (a helper that really needs
        # ``pynecore`` fails loudly with NameError at call time).
        ignored_roots=frozenset({"pynecore"}),
    )
    namespace.update({key: value for key, value in source_namespace.items() if value is not None})
    return namespace


# Trust boundary for helper code executed in-process by the runner (see
# _compile_helper_namespace): this import whitelist is the real gate — every
# entry transitively widens what helper code can reach. The banned-call list
# below is only a second fence, not a sandbox. Additions here need a
# security-review and a test in tests/test_pynecore_helper_containment.py.
# "__future__" is the compiler-flag pseudo-module (inert at runtime, no
# attribute surface) so helper code may opt into postponed annotations.
HELPER_ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "pandas",
        "numpy",
        "math",
        "statistics",
        "decimal",
        "datetime",
        "typing",
        "collections",
        "itertools",
        "functools",
        "dataclasses",
    }
)

HELPER_ALLOWED_FUNCTION_NAMES = frozenset({"build_trade_context", "build_drawings"})

# Shared with runner.codegen_safety (single source of truth, pinned by test).
HELPER_BANNED_CALL_NAMES = BANNED_CALL_NAMES


def _reject_helper_import(node: ast.Import | ast.ImportFrom, source_path: Path) -> None:
    # Check EVERY alias: `import numpy, os` must not slip through on numpy's
    # root alone (import_root only returns the first alias for ast.Import).
    if isinstance(node, ast.Import):
        roots = [alias.name.split(".")[0] for alias in node.names]
    else:
        roots = [import_root(node)]
    for root in roots:
        if root not in HELPER_ALLOWED_IMPORT_ROOTS:
            raise RuntimeError(
                f"Import '{root}' is not allowed in strategy helper code "
                f"({source_path.name}). Allowed: "
                + ", ".join(sorted(HELPER_ALLOWED_IMPORT_ROOTS))
            )


def _reject_helper_call(node: ast.Call, source_path: Path) -> None:
    func = node.func
    if not isinstance(func, ast.Name) or func.id not in HELPER_BANNED_CALL_NAMES:
        return
    raise RuntimeError(
        f"Call to '{func.id}()' is not allowed in strategy helper code ({source_path.name})."
    )


def _reject_dunder_attribute(node: ast.Attribute, source_path: Path) -> None:
    if node.attr.startswith("__") and node.attr.endswith("__"):
        raise RuntimeError(
            f"Dunder attribute access '{node.attr}' is not allowed in "
            f"strategy helper code ({source_path.name})."
        )


def _strip_ignored_import(
    node: ast.Import | ast.ImportFrom,
    ignored_roots: frozenset[str] | None,
) -> ast.Import | ast.ImportFrom | None:
    """Drop engine-contract imports that the helper namespace never needs.

    Only whole roots listed in ``ignored_roots`` are removed; any other alias
    on the same statement is kept and still screened. ``import pynecore, os``
    therefore still fails on ``os`` instead of slipping through.
    """
    if not ignored_roots:
        return node
    if isinstance(node, ast.ImportFrom):
        if import_root(node) in ignored_roots:
            return None
        return node
    kept = [alias for alias in node.names if alias.name.split(".")[0] not in ignored_roots]
    if not kept:
        return None
    if len(kept) == len(node.names):
        return node
    return ast.Import(names=kept)


def _screen_helper_node(node: ast.AST, source_path: Path) -> None:
    """Apply every helper screening rule to one AST node."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        _reject_helper_import(node, source_path)
    elif isinstance(node, ast.Call):
        _reject_helper_call(node, source_path)
    elif isinstance(node, ast.Attribute):
        _reject_dunder_attribute(node, source_path)


def _helper_screen_nodes(nodes: list[ast.stmt], source_path: Path) -> None:
    """Reject helper constructs the runner process must not execute."""
    for node in nodes:
        for inner in ast.walk(node):
            _screen_helper_node(inner, source_path)


def _compile_helper_namespace(
    source_path: Path,
    source: str,
    *,
    function_names: set[str] | None = None,
    strict_structure: bool = False,
    ignored_roots: frozenset[str] | None = None,
) -> dict[str, Any]:
    require_generated_strategy_approval(source_path)
    tree = ast.parse(source, filename=str(source_path))
    helper_nodes: list[ast.stmt] = []
    allowed_names = function_names or {"build_trade_context", "build_drawings"}
    for position, node in enumerate(tree.body):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            stripped = _strip_ignored_import(node, ignored_roots)
            if stripped is not None:
                helper_nodes.append(stripped)
        elif isinstance(node, ast.FunctionDef) and node.name in allowed_names:
            helper_nodes.append(node)
        elif strict_structure and not (
            position == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
        ):
            # A leading string constant is the module docstring — inert, so it
            # is dropped from the compiled module but does not fail the screen.
            raise RuntimeError(
                f"Only imports and helper functions ({', '.join(sorted(allowed_names))}) "
                f"are allowed in strategy helper code ({source_path.name}); found "
                f"{type(node).__name__} at line {getattr(node, 'lineno', 0)}."
            )

    if not helper_nodes:
        return {}

    _helper_screen_nodes(helper_nodes, source_path)

    helper_module = ast.Module(body=helper_nodes, type_ignores=[])
    ast.fix_missing_locations(helper_module)
    namespace: dict[str, Any] = {}
    exec(compile(helper_module, str(source_path), "exec"), namespace)
    return {name: namespace.get(name) for name in allowed_names}


def _normalize_trade_context_timeframe(name: str, explicit_when: str) -> str:
    if explicit_when == "exit":
        return "exit"
    if explicit_when == "entry" and not (
        name.lower().startswith("exit_") or name.lower().endswith("_at_exit")
    ):
        return explicit_when
    lowered = name.lower()
    if lowered.startswith("exit_") or lowered.endswith("_at_exit"):
        return "exit"
    return "entry"


def _trade_context_meta_key(name: str) -> str:
    lowered = name.lower()
    if lowered == "exit_reason":
        return "exit_reason"
    if lowered.startswith("exit_"):
        return name[5:]
    return name


def _coerce_series(value: Any, index: pd.Index) -> pd.Series:
    if isinstance(value, pd.Series):
        series = value.copy()
        if len(series) != len(index):
            raise ValueError(
                f"Trade context series length mismatch: expected {len(index)}, got {len(series)}"
            )
        series.index = index
        return series

    try:
        series = pd.Series(value, index=index)
    except ValueError as exc:
        raise ValueError(f"Could not align trade context series: {exc}") from exc

    if len(series) != len(index):
        raise ValueError(
            f"Trade context series length mismatch: expected {len(index)}, got {len(series)}"
        )
    return series


def _normalize_trade_context(raw_context: Any, index: pd.Index) -> IndicatorMap:
    if raw_context is None:
        return {}
    if not isinstance(raw_context, dict):
        raise TypeError("build_trade_context() must return a dict")

    normalized: IndicatorMap = {}
    for name, raw_value in raw_context.items():
        if raw_value is None:
            continue

        when = ""
        value = raw_value
        if isinstance(raw_value, dict) and "series" in raw_value:
            when = str(raw_value.get("when", "entry")).lower()
            value = raw_value["series"]
        elif (
            isinstance(raw_value, tuple)
            and len(raw_value) == 2
            and isinstance(raw_value[0], str)
            and raw_value[0].lower() in {"entry", "exit"}
        ):
            when = raw_value[0].lower()
            value = raw_value[1]

        normalized[str(name)] = (_coerce_series(value, index), when)
    return normalized


def _build_trade_context(context_builder: Any, df: pd.DataFrame, params: dict) -> IndicatorMap:
    if context_builder is None:
        return {}
    return _normalize_trade_context(context_builder(df.copy(), params or {}), df.index)


def _build_drawings(
    drawings_builder: Any,
    df: pd.DataFrame,
    params: dict,
    trades: list[Trade],
    symbol: str,
) -> list[dict[str, Any]]:
    if drawings_builder is None:
        return []
    raw_drawings = _invoke_strategy_helper(
        drawings_builder,
        df.copy(),
        params or {},
        trades,
        symbol,
    )
    return _normalize_drawings(raw_drawings, symbol=symbol)


def _invoke_strategy_helper(
    builder: Any,
    df: pd.DataFrame,
    params: dict,
    trades: list[Trade],
    symbol: str,
) -> Any:
    signature = inspect.signature(builder)
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    positional_index = 0
    values_by_name = {
        "df": df,
        "dataframe": df,
        "params": params,
        "trades": trades,
        "symbol": symbol,
    }

    for parameter in signature.parameters.values():
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            if positional_index == 0:
                args.append(df)
            elif parameter.name in values_by_name:
                args.append(values_by_name[parameter.name])
            positional_index += 1
        elif parameter.kind == inspect.Parameter.KEYWORD_ONLY and parameter.name in values_by_name:
            kwargs[parameter.name] = values_by_name[parameter.name]

    return builder(*args, **kwargs)


def _normalize_trade_timestamp(value: str) -> pd.Timestamp | None:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return pd.Timestamp(ts)


def _locate_trade_bar(timestamp_series: pd.Series, raw_time: str) -> int | None:
    trade_ts = _normalize_trade_timestamp(raw_time)
    if trade_ts is None:
        return None

    series = pd.to_datetime(timestamp_series, utc=True, errors="coerce")
    matches = series == trade_ts
    if matches.any():
        return int(matches.idxmax())

    diffs = (series - trade_ts).abs()
    diffs = diffs[diffs.notna()]
    if diffs.empty:
        return None
    nearest_idx = diffs.idxmin()
    if diffs.loc[nearest_idx] <= pd.Timedelta(seconds=1):
        return int(nearest_idx)
    return None


def _finite_meta_value(meta: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = meta.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _promote_directional_price_meta(meta: dict[str, Any], direction: str, key_prefix: str) -> None:
    long_key = f"{key_prefix}_long"
    short_key = f"{key_prefix}_short"
    selected_key = long_key if direction == "LONG" else short_key
    if key_prefix not in meta and selected_key in meta:
        meta[key_prefix] = meta[selected_key]
    meta.pop(long_key, None)
    meta.pop(short_key, None)


def _enrich_trades_with_context(
    trades: list[Trade], trade_context: IndicatorMap, df: pd.DataFrame
) -> None:
    if not trades or not trade_context or "timestamp" not in df.columns:
        return

    timestamp_series = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    for trade in trades:
        entry_idx = _locate_trade_bar(timestamp_series, trade.entry_time)
        exit_idx = _locate_trade_bar(timestamp_series, trade.last_exit_time)
        meta = dict(trade.meta or {})

        for name, (series, explicit_when) in trade_context.items():
            when = _normalize_trade_context_timeframe(name, explicit_when)
            sample_idx = exit_idx if when == "exit" else entry_idx
            if sample_idx is None:
                continue
            serialized = _serialize_meta_value(series.iloc[sample_idx])
            if serialized is None:
                continue
            meta[_trade_context_meta_key(name)] = serialized

        _promote_directional_price_meta(meta, trade.direction, "sl_price")
        _promote_directional_price_meta(meta, trade.direction, "tp_price")
        _materialize_exit_price_levels(meta, trade)
        trade.stop_loss = _finite_meta_value(
            meta,
            ("sl_price_at_exit", "sl_price"),
        )
        trade.take_profit = _finite_meta_value(
            meta,
            ("tp_price_at_exit", "tp_price"),
        )
        trade.meta = meta or None
        if trade.exits:
            inferred_reason = None
            explicit_reason = meta.get("exit_reason") if meta else None
            if explicit_reason is not None:
                inferred_reason = str(explicit_reason)
            elif meta:
                inferred_reason = _infer_exit_reason_from_meta(trade.exits[-1].price, meta)
            if inferred_reason:
                trade.exits[-1].reason = inferred_reason


def _load_ohlcv(
    data_root: Path,
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame | None:
    """Find and load OHLCV parquet for a symbol."""
    ohlcv_root = data_root / "ohlcv"
    matches = sorted(
        ohlcv_root.rglob(f"**/{symbol}/{timeframe}.parquet"),
        key=lambda path: path.as_posix(),
    )
    if not matches:
        return None

    df = pd.read_parquet(matches[0])
    df.columns = [c.lower() for c in df.columns]
    df = contract.normalize_ohlcv_frame(df)

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        # Conformance (Cleanup 16-A, F9): shared date-filter convention — a
        # date-only end boundary is inclusive of the full day.
        df = contract.apply_date_filter(df, start=start_date, end=end_date)

    return df.reset_index(drop=True)


def _infer_asset_class(data_root: Path, symbol: str, timeframe: str) -> str:
    matches = sorted(
        (data_root / "ohlcv").rglob(f"**/{symbol}/{timeframe}.parquet"),
        key=lambda path: path.as_posix(),
    )
    if not matches:
        return "unknown"
    parts = matches[0].parts
    try:
        ohlcv_idx = parts.index("ohlcv")
        return parts[ohlcv_idx + 2].lower()
    except (ValueError, IndexError):
        return "unknown"


def _normalize_trade_exits(trades: list[Trade]) -> None:
    for trade in trades:
        if not trade.exits or not trade.meta:
            continue

        sl_price = trade.meta.get("sl_price_at_exit", trade.meta.get("sl_price"))
        tp_price = trade.meta.get("tp_price_at_exit", trade.meta.get("tp_price"))

        for exit_leg in trade.exits:
            if trade.direction == "LONG":
                if sl_price is not None and exit_leg.price < float(sl_price):
                    exit_leg.price = round(float(sl_price), 6)
                    exit_leg.reason = "stop_loss"
                elif tp_price is not None and exit_leg.price > float(tp_price):
                    exit_leg.price = round(float(tp_price), 6)
                    exit_leg.reason = "take_profit"
            else:
                if sl_price is not None and exit_leg.price > float(sl_price):
                    exit_leg.price = round(float(sl_price), 6)
                    exit_leg.reason = "stop_loss"
                elif tp_price is not None and exit_leg.price < float(tp_price):
                    exit_leg.price = round(float(tp_price), 6)
                    exit_leg.reason = "take_profit"


def _materialize_exit_price_levels(meta: dict[str, Any], trade: Trade) -> None:
    sl_offset = meta.get("sl_offset_at_exit")
    if sl_offset is not None:
        try:
            offset = float(sl_offset)
        except (TypeError, ValueError):
            offset = 0.0
        if offset > 0:
            if trade.direction == "LONG":
                sl_exit = trade.entry_price - offset
            else:
                sl_exit = trade.entry_price + offset
            meta.setdefault("sl_price_at_exit", round(sl_exit, 6))
            meta.setdefault("trailing_stop_at_exit", round(sl_exit, 6))

    tp_offset = meta.get("tp_offset_at_exit")
    if tp_offset is not None:
        try:
            offset = float(tp_offset)
        except (TypeError, ValueError):
            offset = 0.0
        if offset > 0:
            if trade.direction == "LONG":
                tp_exit = trade.entry_price + offset
            else:
                tp_exit = trade.entry_price - offset
            meta.setdefault("tp_price_at_exit", round(tp_exit, 6))


def _apply_risk_sizing(
    trades: list[Trade],
    config: BacktestConfig,
    asset_class: str = "unknown",
    *,
    asset_class_by_symbol: dict[str, str] | None = None,
    fee_model: Any | None = None,
) -> None:
    if not trades:
        return
    if config.risk_per_trade_pct is None or config.risk_per_trade_pct <= 0:
        raise ValueError(
            "Risk sizing is not configured (risk_per_trade_pct unset); "
            "refusing unsized PyneCore trades"
        )
    risk_pct = float(config.risk_per_trade_pct)
    if not math.isfinite(risk_pct) or not 0 < risk_pct <= 1:
        raise ValueError("PyneCore risk_per_trade_pct must be finite and between 0 and 1")
    equity = float(config.initial_capital)
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("PyneCore initial capital must be finite and > 0")
    max_leverage = _resolve_max_leverage(config.max_leverage)
    if not math.isfinite(max_leverage) or max_leverage <= 0:
        raise ValueError("PyneCore max_leverage must be finite and > 0")

    class_by_symbol = {
        str(symbol).upper(): str(value).lower()
        for symbol, value in (asset_class_by_symbol or {}).items()
    }

    def _time(value: object, field_name: str) -> pd.Timestamp:
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(parsed):
            raise ValueError(f"PyneCore {field_name} must be a valid timestamp")
        return pd.Timestamp(parsed)

    ordered: list[tuple[Trade, pd.Timestamp, pd.Timestamp]] = []
    for trade in trades:
        entry_time = _time(trade.entry_time, "entry_time")
        exit_time = entry_time
        if trade.exits:
            exit_time = max(
                _time(leg.time, "exit_time") for leg in trade.exits
            )
        ordered.append((trade, entry_time, exit_time))
    ordered.sort(key=lambda item: (item[1], str(item[0].entry_time), int(item[0].id or 0)))
    by_exit = sorted(
        ordered,
        key=lambda item: (item[2], str(item[0].entry_time), int(item[0].id or 0)),
    )

    open_ledger: dict[int, tuple[pd.Timestamp, float, float, float]] = {}
    released = 0
    rejected_trade_ids: set[int] = set()

    def _release_through(cutoff: pd.Timestamp) -> None:
        nonlocal released, equity
        while released < len(by_exit):
            trade, _entry_time, exit_time = by_exit[released]
            if exit_time > cutoff:
                break
            key = int(trade.id)
            booked = open_ledger.pop(key, None)
            if booked is not None:
                equity += booked[3]
            released += 1

    for trade, entry_time, _exit_time in ordered:
        _release_through(entry_time)
        symbol_class = class_by_symbol.get(str(trade.symbol).upper(), asset_class)
        max_position_exposure_pct = _resolve_max_position_exposure_pct(
            config.max_position_exposure_pct, symbol_class
        )
        if not 0 < max_position_exposure_pct <= 1:
            raise ValueError("PyneCore position exposure must be between 0 and 1")

        meta = dict(trade.meta or {})
        sl_price = meta.get("sl_price")
        if sl_price is None:
            if config.reject_missing_stop or not config.allow_fallback_stop:
                rejected_trade_ids.add(int(trade.id))
                logger.warning(
                    "Rejecting PyneCore trade %s: no stop_loss and fallback sizing is disabled",
                    trade.id,
                )
                continue
            direction_sign = 1.0 if str(trade.direction).upper() == "LONG" else -1.0
            stop_distance = float(trade.entry_price) * 0.02
            sl_price = float(trade.entry_price) - direction_sign * stop_distance
            meta["sl_price"] = sl_price
            meta["stop_source"] = "fallback_percent"
        entry_price = float(trade.entry_price)
        stop_distance = abs(entry_price - float(sl_price))
        if not math.isfinite(entry_price) or entry_price <= 0:
            raise ValueError("PyneCore entry price must be finite and > 0")
        if not math.isfinite(stop_distance) or stop_distance <= 0:
            raise ValueError("PyneCore stop distance must be finite and > 0")

        old_qty = float(trade.quantity or 0.0)
        if not math.isfinite(old_qty) or old_qty <= 0:
            raise ValueError("PyneCore trade quantity must be finite and > 0")
        risk_amount = equity * risk_pct
        required_notional = risk_amount / stop_distance * entry_price
        position_notional = equity * max_position_exposure_pct
        open_notional = sum(item[1] for item in open_ledger.values())
        aggregate_notional = max(0.0, equity * max_leverage - open_notional)
        final_notional = min(required_notional, position_notional, aggregate_notional)
        new_qty = round(final_notional / entry_price, 4)
        scale = new_qty / old_qty

        for leg in trade.exits:
            leg.quantity = round(float(leg.quantity or 0.0) * scale, 4)
            if str(trade.direction).upper() == "LONG":
                leg.pnl = round((float(leg.price) - entry_price) * leg.quantity, 2)
            else:
                leg.pnl = round((entry_price - float(leg.price)) * leg.quantity, 2)
        trade.quantity = new_qty
        trade.pnl = round(sum(float(leg.pnl or 0.0) for leg in trade.exits), 2)
        if fee_model is not None:
            exit_price = float(trade.exits[-1].price) if trade.exits else entry_price
            trade.fees = round(
                float(fee_model.fee_for_trade(entry_price, exit_price, new_qty)),
                2,
            )
        elif trade.fees is not None:
            trade.fees = round(float(trade.fees) * scale, 2)
        trade.net_pnl = round(float(trade.pnl) - float(trade.fees or 0.0), 2)
        trade.meta = meta

        open_notional += new_qty * entry_price
        open_risk = sum(item[2] for item in open_ledger.values()) + new_qty * stop_distance
        if new_qty > 0:
            open_ledger[int(trade.id)] = (
                _exit_time,
                new_qty * entry_price,
                new_qty * stop_distance,
                float(trade.net_pnl),
            )
        meta["risk_math"] = {
            "equity_at_entry": round(equity, 2),
            "position_size": round(new_qty, 4),
            "open_positions": len(open_ledger),
            "open_notional": round(open_notional, 2),
            "open_notional_pct": round(open_notional / equity, 6) if equity > 0 else None,
            "open_risk": round(open_risk, 2),
            "open_risk_pct": round(open_risk / equity, 6) if equity > 0 else None,
        }
        trade.meta = meta

    if rejected_trade_ids:
        trades[:] = [trade for trade in trades if int(trade.id) not in rejected_trade_ids]


def _infer_exit_reason_from_meta(exit_price: float, meta: dict[str, Any]) -> str | None:
    checks = [
        ("trailing_stop_at_exit", "trailing_stop"),
        ("sl_price_at_exit", "stop_loss"),
        ("tp_price_at_exit", "take_profit"),
        ("sl_price", "stop_loss"),
        ("tp_price", "take_profit"),
    ]
    for key, reason in checks:
        value = meta.get(key)
        if value is None:
            continue
        try:
            target = float(value)
        except (TypeError, ValueError):
            continue
        if target == 0:
            continue
        if abs(exit_price - target) / abs(target) <= 0.001:
            return reason
    return None


def _initial_exit_reason(raw_signal: str) -> str | None:
    """Seed an exit reason from the broker ledger's Signal column.

    ``strategy.close(..., comment="...")`` / ``strategy.exit`` comments surface
    here verbatim (e.g. ``time_exit``, ``trailing_stop``), as do emulator
    events (``Margin call``). The generic ``Close entry(s) order ...`` text
    carries no strategy intent and stays None so context/meta inference can
    still classify the exit. Normalised to snake_case to match the
    vectorbt/native reason vocabulary.
    """
    signal = (raw_signal or "").strip()
    if not signal or signal.lower().startswith("close entry(s) order"):
        return None
    return "_".join(signal.lower().split())


def _parse_trade_csv(csv_path: Path, symbol: str, id_offset: int) -> list[Trade]:
    trades: list[Trade] = []
    entries: dict[int, dict[str, Any]] = {}
    exits: dict[int, list[TradeExit]] = {}

    with open(csv_path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            trade_num = int(row["Trade #"])
            trade_type = row["Type"].strip().lower()

            if "entry" in trade_type:
                entry = entries.setdefault(
                    trade_num,
                    {
                        "time": row["Date/Time"],
                        "price": 0.0,
                        "contracts": 0.0,
                        "direction": "LONG" if "long" in trade_type else "SHORT",
                    },
                )
                quantity = float(row["Contracts"])
                entry["contracts"] += quantity
                entry["price"] = (
                    (
                        entry["price"] * (entry["contracts"] - quantity)
                        + float(row["Price USD"]) * quantity
                    )
                    / entry["contracts"]
                    if entry["contracts"]
                    else float(row["Price USD"])
                )
            elif "exit" in trade_type and trade_num in entries:
                exits.setdefault(trade_num, []).append(
                    TradeExit(
                        time=row["Date/Time"],
                        price=round(float(row["Price USD"]), 6),
                        quantity=round(float(row["Contracts"]), 4),
                        pnl=round(float(row["Profit USD"]), 2),
                        reason=_initial_exit_reason(row.get("Signal", "")),
                    )
                )

    for trade_num, entry in entries.items():
        trade_exits = sorted(exits.get(trade_num, []), key=lambda item: str(item.time))
        if not trade_exits:
            continue
        trades.append(
            Trade(
                id=id_offset + trade_num,
                symbol=symbol,
                direction=str(entry["direction"]),
                entry_time=str(entry["time"]),
                entry_price=round(float(entry["price"]), 6),
                quantity=round(float(entry["contracts"]), 4),
                exits=trade_exits,
                pnl=round(sum(float(leg.pnl or 0.0) for leg in trade_exits), 2),
                tags=["pynecore"],
            )
        )

    return trades


def _parse_stats_csv(csv_path: Path) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    with open(csv_path, "r") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for row in reader:
            if len(row) >= 2 and row[0].strip():
                key = row[0].strip().lower().replace(" ", "_")
                val = row[1].strip()
                if val:
                    try:
                        stats[key] = float(val)
                    except ValueError:
                        stats[key] = val
    return stats
