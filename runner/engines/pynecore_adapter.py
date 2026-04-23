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
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, date as _date
from pathlib import Path
from typing import Any

import pandas as pd

from runner.engines.base import (
    BacktestConfig,
    EngineAdapter,
    ProgressCallback,
    resolve_strategy_parameters,
)
from runner.engines.vectorbt_adapter import (
    _normalize_drawings,
    _resolve_max_leverage,
    _resolve_max_position_pct,
)
from shared.constants import DATA_DIR
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit

logger = logging.getLogger(__name__)

IndicatorMap = dict[str, tuple[pd.Series, str]]


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
        data_root = Path(config.data_dir) if config.data_dir else DATA_DIR
        resolved_parameters = resolve_strategy_parameters(strategy_path, config.parameters)
        all_trades: list[Trade] = []
        all_equity: list[EquityPoint] = []
        all_stats: dict[str, Any] = {}
        all_drawings: list[dict[str, Any]] = []
        trade_counter = 0
        total_bars = 0
        strategy_name, context_builder, drawings_builder = _load_strategy_artifacts(
            strategy_path, config.strategy_name
        )

        for symbol in config.symbols:
            df = _load_ohlcv(
                data_root, symbol, config.timeframe, config.start_date, config.end_date
            )
            if df is None or df.empty:
                logger.warning("No data for %s/%s, skipping", symbol, config.timeframe)
                continue

            total_bars += len(df)
            trade_context = _build_trade_context(context_builder, df, resolved_parameters)
            asset_class = _infer_asset_class(data_root, symbol, config.timeframe)
            trades: list[Trade] = []

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

                pyne_bin = shutil.which("pyne")
                if not pyne_bin:
                    venv_bin = Path(sys.executable).parent / "pyne"
                    if venv_bin.exists():
                        pyne_bin = str(venv_bin)
                    else:
                        logger.error("pyne CLI not found in PATH")
                        continue

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
                    cmd.extend(["--from", config.start_date])
                if config.end_date:
                    cmd.extend(["--to", config.end_date])

                logger.info(
                    "Running PyneCore: %s on %s (%d bars)", config.strategy_name, symbol, len(df)
                )
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=str(tmpdir_path),
                    timeout=300,
                )

                if result.returncode != 0:
                    logger.error("PyneCore failed for %s: %s", symbol, result.stderr[-500:])
                    continue

                if trade_csv.exists():
                    trades = _parse_trade_csv(trade_csv, symbol, trade_counter)
                    if trade_context:
                        _enrich_trades_with_context(trades, trade_context, df)
                    _normalize_trade_exits(trades)
                    _apply_risk_sizing(trades, config, asset_class)
                    trade_counter += len(trades)
                    all_trades.extend(trades)

                if stats_csv.exists():
                    all_stats = _parse_stats_csv(stats_csv)

            symbol_drawings = _build_drawings(
                drawings_builder,
                df,
                resolved_parameters,
                trades,
                symbol,
            )
            all_drawings.extend(symbol_drawings)

        equity = config.initial_capital
        for trade in sorted(all_trades, key=lambda item: item.entry_time):
            equity += trade.pnl
            all_equity.append(EquityPoint(time=trade.entry_time, equity=round(equity, 2)))

        duration_days = 0
        try:
            d_start = _date.fromisoformat(config.start_date)
            d_end = _date.fromisoformat(config.end_date)
            duration_days = (d_end - d_start).days
        except (ValueError, TypeError):
            pass

        now = datetime.now(timezone.utc)
        meta = BacktestMeta(
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
            parameters=resolved_parameters,
        )

        return BacktestResult(
            meta=meta,
            trades=all_trades,
            equity_curve=all_equity,
            stats=all_stats,
            drawings=all_drawings,
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
    namespace: dict[str, Any] = {}

    helper_path = strategy_path.with_name(f"{strategy_path.stem}_context.py")
    if helper_path.exists():
        namespace.update(_compile_helper_namespace(helper_path, helper_path.read_text()))

    source_namespace = _compile_helper_namespace(
        strategy_path,
        source,
        function_names={"build_trade_context", "build_drawings"},
    )
    namespace.update({key: value for key, value in source_namespace.items() if value is not None})
    return namespace


def _compile_helper_namespace(
    source_path: Path,
    source: str,
    *,
    function_names: set[str] | None = None,
) -> dict[str, Any]:
    tree = ast.parse(source, filename=str(source_path))
    helper_nodes: list[ast.stmt] = []
    allowed_names = function_names or {"build_trade_context", "build_drawings"}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            helper_nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in allowed_names:
            helper_nodes.append(node)

    if not helper_nodes:
        return {}

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


def _serialize_meta_value(value: Any) -> Any | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()

    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass

    if isinstance(value, float):
        if pd.isna(value):
            return None
        return round(value, 6)
    if isinstance(value, (int, str, bool)):
        return value
    if pd.isna(value):
        return None
    return str(value)


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
    matches = list(ohlcv_root.rglob(f"**/{symbol}/{timeframe}.parquet"))
    if not matches:
        return None

    df = pd.read_parquet(matches[0])
    df.columns = [c.lower() for c in df.columns]

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        if start_date:
            df = df[df["timestamp"] >= start_date]
        if end_date:
            df = df[df["timestamp"] <= end_date]

    return df.reset_index(drop=True)


def _infer_asset_class(data_root: Path, symbol: str, timeframe: str) -> str:
    matches = list((data_root / "ohlcv").rglob(f"**/{symbol}/{timeframe}.parquet"))
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

        exit_leg = trade.exits[-1]
        sl_price = trade.meta.get("sl_price_at_exit", trade.meta.get("sl_price"))
        tp_price = trade.meta.get("tp_price_at_exit", trade.meta.get("tp_price"))

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


def _apply_risk_sizing(trades: list[Trade], config: BacktestConfig, asset_class: str) -> None:
    if not trades or config.risk_per_trade_pct is None or config.risk_per_trade_pct <= 0:
        return

    equity = config.initial_capital
    max_position_pct = _resolve_max_position_pct(config.max_position_pct, asset_class)
    max_leverage = max(_resolve_max_leverage(config.max_leverage), max_position_pct)

    for trade in sorted(trades, key=lambda item: item.entry_time):
        meta = trade.meta or {}
        sl_price = meta.get("sl_price")
        if sl_price is None:
            equity += trade.pnl
            continue

        stop_distance = abs(trade.entry_price - float(sl_price))
        if stop_distance <= 0:
            equity += trade.pnl
            continue

        risk_amount = equity * config.risk_per_trade_pct
        base_qty = risk_amount / stop_distance
        required_notional = base_qty * trade.entry_price
        cap_notional = equity * max_position_pct
        leverage_notional_limit = equity * max_leverage

        if required_notional <= cap_notional:
            final_notional = required_notional
        elif required_notional <= leverage_notional_limit:
            final_notional = required_notional
        else:
            final_notional = leverage_notional_limit

        qty = round(final_notional / trade.entry_price, 4)
        exit_price = trade.exits[-1].price
        if trade.direction == "LONG":
            pnl = round((exit_price - trade.entry_price) * qty, 2)
        else:
            pnl = round((trade.entry_price - exit_price) * qty, 2)

        trade.quantity = qty
        trade.pnl = pnl
        trade.exits[-1].quantity = qty
        trade.exits[-1].pnl = pnl
        equity += pnl


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


def _parse_trade_csv(csv_path: Path, symbol: str, id_offset: int) -> list[Trade]:
    trades: list[Trade] = []
    entries: dict[int, dict[str, Any]] = {}

    with open(csv_path, "r") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            trade_num = int(row["Trade #"])
            trade_type = row["Type"].strip().lower()

            if "entry" in trade_type:
                entries[trade_num] = {
                    "time": row["Date/Time"],
                    "price": float(row["Price USD"]),
                    "contracts": float(row["Contracts"]),
                    "direction": "LONG" if "long" in trade_type else "SHORT",
                }
            elif "exit" in trade_type and trade_num in entries:
                entry = entries[trade_num]
                exit_price = float(row["Price USD"])
                exit_time = row["Date/Time"]
                profit = float(row["Profit USD"])
                qty = float(row["Contracts"])

                trade = Trade(
                    id=id_offset + trade_num,
                    symbol=symbol,
                    direction=entry["direction"],
                    entry_time=entry["time"],
                    entry_price=round(entry["price"], 6),
                    quantity=round(qty, 4),
                    exits=[
                        TradeExit(
                            time=exit_time,
                            price=round(exit_price, 6),
                            quantity=round(qty, 4),
                            pnl=round(profit, 2),
                        )
                    ],
                    pnl=round(profit, 2),
                    tags=["pynecore"],
                )
                trades.append(trade)

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
