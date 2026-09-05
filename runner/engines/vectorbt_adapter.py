"""
vectorbt Engine Adapter — runs Python strategies using vectorized signals.

Preferred strategy contract:
    def generate_signals(df: pd.DataFrame, params: dict):
        return entries, exits, indicators_dict[, drawings]

Legacy compatibility:
    def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame
        DataFrame must contain 'entries' and 'exits' columns.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

import pandas as pd

from runner.engines import contract
from runner.engines._drawings import DrawingList, _normalize_drawings
from runner.engines._indicator_series import _build_indicator_series
from runner.engines._shared import (
    resolve_max_leverage as _resolve_max_leverage,
    resolve_max_position_pct as _resolve_max_position_pct,
    serialize_meta_value as _shared_serialize_meta_value,
)
from runner.engines.base import (
    BacktestConfig,
    EngineAdapter,
    ProgressCallback,
    resolve_strategy_parameters,
)
from runner.engines.contract import resolve_same_bar_exit
from shared.constants import DATA_DIR
from shared.models import BacktestMeta, BacktestResult, EquityPoint, IndicatorSeries, Trade, TradeExit

logger = logging.getLogger(__name__)

_RISK_WARNING_KEYS: set[tuple[str, str, str]] = set()


class _SizeRecord(TypedDict):
    """Per-trade position-sizing record produced by ``_size_trade``.

    These keys are re-serialized into ``trade.meta["risk_math"]`` (minus the
    qty/bookkeeping fields) and are part of the viewer contract — see
    ``frontend/src/api/types.ts`` (``TradeRiskMath``). Renaming or dropping a
    key here without updating the ``risk_math`` assembly breaks the chart
    silently; the key-set contract test guards this (pre-mortem PM-2).
    """

    sizing_mode: str
    risk_target_usd: float | None
    stop_distance: float | None
    max_position_pct: float | None
    max_leverage: float | None
    required_notional: float | None
    cap_notional: float | None
    leverage_notional_limit: float | None
    final_notional: float | None
    capped_by_max_position: bool
    capped_by_max_leverage: bool
    qty: float


def _warn_once(key: tuple[str, str, str], message: str, *args: Any) -> None:
    if key in _RISK_WARNING_KEYS:
        return
    _RISK_WARNING_KEYS.add(key)
    logger.warning(message, *args)


IndicatorMap = dict[str, tuple[pd.Series, str]]
TIMEFRAME_ORDER = ["tick", "s1", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]
TIMEFRAME_TO_DURATION = {
    "s1": pd.Timedelta(seconds=1),
    "1m": pd.Timedelta(minutes=1),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "1d": pd.Timedelta(days=1),
    "1w": pd.Timedelta(weeks=1),
}

MAX_PARALLEL_WORKERS = 4


def _uses_kwargs_style(func) -> bool:
    """Return True if generate_signals uses keyword-argument style (not params dict)."""
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return False
    params = list(sig.parameters.values())
    if len(params) < 2:
        return False
    # Skip first arg (df/data). If second arg is named 'params' → dict style.
    if params[1].name == "params":
        return False
    # If any param beyond the first has a non-empty default → kwargs style
    return any(
        p.default is not inspect.Parameter.empty
        for p in params[1:]
    )


def _call_generate_signals(strategy_mod, df: pd.DataFrame, parameters: dict):
    """Call generate_signals with dict-style or kwargs-style depending on signature."""
    if _uses_kwargs_style(strategy_mod.generate_signals):
        # Filter to only keys the function actually accepts
        sig = inspect.signature(strategy_mod.generate_signals)
        accepted = {p.name for p in list(sig.parameters.values())[1:]}
        filtered = {k: v for k, v in parameters.items() if k in accepted}
        return strategy_mod.generate_signals(df, **filtered)
    return strategy_mod.generate_signals(df, parameters)


def _size_trade(
    *,
    config: BacktestConfig,
    equity: float,
    entry_price: float,
    asset_class: str,
    stop_loss_series: pd.Series | None,
    atr_series: pd.Series | None,
    entry_idx: int,
    symbol: str,
    trade_counter: int,
) -> _SizeRecord:
    """Size one trade and return the full risk-math record.

    Mirrors the legacy in-loop sizing logic exactly: risk_pct sizing when
    configured, otherwise full-equity sizing. Warnings are emitted once per
    (symbol, timeframe, reason).
    """
    risk_target_usd: float | None = None
    stop_distance: float | None = None
    max_position_pct: float | None = None
    max_leverage: float | None = None
    required_notional: float | None = None
    cap_notional: float | None = None
    leverage_notional_limit: float | None = None
    final_notional: float | None = None
    capped_by_max_position = False
    capped_by_max_leverage = False
    sizing_mode = "full_equity"

    if config.risk_per_trade_pct is not None and config.risk_per_trade_pct > 0:
        sizing_mode = "risk_pct"
        max_position_pct = _resolve_max_position_pct(config.max_position_pct, asset_class)
        max_leverage = _resolve_max_leverage(config.max_leverage)
        # D10 (F7/F35): max_position_pct is the per-position notional cap; it is
        # an INDEPENDENT cap, not an implicit leverage floor — a conservative
        # max_leverage < max_position_pct is honored as configured.

        risk_target_usd = equity * config.risk_per_trade_pct
        stop_distance = _resolve_stop_distance(
            entry_idx,
            entry_price,
            stop_loss_series=stop_loss_series,
            atr_series=atr_series,
        )

        if not stop_distance or stop_distance <= 0:
            stop_distance = entry_price * 0.02
            _warn_once(
                (symbol, config.timeframe, "fallback_stop"),
                "Risk sizing fallback for %s/%s: no stop_loss/ATR, using %.2f%% default stop",
                symbol,
                config.timeframe,
                2.0,
            )

        base_qty = risk_target_usd / stop_distance
        required_notional = base_qty * entry_price
        cap_notional = equity * max_position_pct
        leverage_notional_limit = equity * max_leverage
        capped_by_max_position = required_notional > cap_notional
        capped_by_max_leverage = required_notional > leverage_notional_limit

        # D10: the binding notional is min(position cap, leverage limit).
        binding_notional = min(cap_notional, leverage_notional_limit)
        if required_notional <= binding_notional:
            final_notional = required_notional
        else:
            final_notional = binding_notional
            achievable_risk = (final_notional / entry_price) * stop_distance
            if capped_by_max_leverage and not capped_by_max_position:
                logger.info(
                    "Leverage bound notional for %s trade %s: required_notional=%.2f exceeds leverage limit=%.2f (max_leverage=%.2fx)",
                    symbol,
                    trade_counter,
                    required_notional,
                    leverage_notional_limit,
                    max_leverage,
                )
            _warn_once(
                (symbol, config.timeframe, "risk_underfilled"),
                "Risk target underfilled for %s/%s: cap=%.2f, max_leverage=%.2fx",
                symbol,
                config.timeframe,
                cap_notional,
                max_leverage,
            )

        qty = final_notional / entry_price
    else:
        qty = equity / entry_price
        final_notional = qty * entry_price

    return _SizeRecord(
        sizing_mode=sizing_mode,
        risk_target_usd=risk_target_usd,
        stop_distance=stop_distance,
        max_position_pct=max_position_pct,
        max_leverage=max_leverage,
        required_notional=required_notional,
        cap_notional=cap_notional,
        leverage_notional_limit=leverage_notional_limit,
        final_notional=final_notional,
        capped_by_max_position=capped_by_max_position,
        capped_by_max_leverage=capped_by_max_leverage,
        qty=qty,
    )


def _process_symbol(
    symbol: str,
    config: BacktestConfig,
    strategy_path: str,
    resolved_parameters: dict[str, Any],
    data_root: Path,
) -> tuple[list[Trade], list[str], DrawingList, list[IndicatorSeries], int]:
    """Process a single symbol: load data, generate signals, extract trades.

    Returns (trades_with_temp_ids, used_intrabar_timeframes, drawings,
    indicator_series, loaded_bar_count). Runs in its own process when
    parallelized.
    """
    strategy_mod = _load_strategy_module(strategy_path)
    df, data_path = _load_ohlcv(
        data_root, symbol, config.timeframe, config.start_date, config.end_date
    )
    if df is None or df.empty:
        raise RuntimeError(
            f"No OHLCV data for {symbol}/{config.timeframe} in "
            f"{config.start_date}..{config.end_date} under {data_root}; "
            "refusing to run a silent empty backtest"
        )

    asset_class = _infer_asset_class_from_path(data_path)
    intrabar_df, intrabar_timeframe = _load_intrabar_ohlcv(
        data_root,
        symbol,
        config.timeframe,
        config.modeling_resolution,
        config.start_date,
        config.end_date,
    )
    extra_timeframes = [intrabar_timeframe] if intrabar_timeframe else []

    signal_output = _call_generate_signals(strategy_mod, df, resolved_parameters)
    signals, indicators, drawings = _normalize_signal_output(signal_output, df.index, symbol=symbol)
    indicator_series = _build_indicator_series(df, indicators)
    execution_signals, execution_indicators = _prepare_next_open_execution_data(signals, indicators)
    entries = execution_signals["entries"]
    exits = execution_signals["exits"]
    stop_loss_series = _find_stop_loss_series(execution_signals, execution_indicators)
    take_profit_series = _find_take_profit_series(execution_signals, execution_indicators)
    atr_series = _find_atr_series(execution_signals, execution_indicators)
    direction_series = _find_direction_series(execution_signals, execution_indicators)
    exit_price_series = _find_exit_price_series(execution_signals, execution_indicators)
    exit_reason_series = _find_exit_reason_series(execution_signals, execution_indicators)

    trades: list[Trade] = []
    in_trade = False
    entry_idx = None
    equity = config.initial_capital
    trade_counter = 0

    for i in range(len(df)):
        if not in_trade and bool(entries.iloc[i]):
            in_trade = True
            entry_idx = i
        elif in_trade and bool(exits.iloc[i]):
            in_trade = False
            trade_counter += 1

            entry_row = df.iloc[entry_idx]
            exit_row = df.iloc[i]
            direction = _resolve_direction(execution_signals, direction_series, entry_idx)
            entry_price = _resolve_entry_price(entry_row)
            exit_price = _resolve_exit_price(exit_row, i, exit_price_series)
            exit_reason = _resolve_exit_reason(i, exit_reason_series)
            entry_ts = str(entry_row.get("timestamp", entry_row.name))
            exit_ts = str(exit_row.get("timestamp", exit_row.name))
            trade_meta = _extract_trade_meta(execution_signals, execution_indicators, entry_idx, i)
            stop_price = _resolve_level_value(stop_loss_series, entry_idx)
            risk_target_usd: float | None = None
            stop_distance: float | None = None
            max_position_pct: float | None = None
            max_leverage: float | None = None
            required_notional: float | None = None
            cap_notional: float | None = None
            leverage_notional_limit: float | None = None
            final_notional: float | None = None
            capped_by_max_position = False
            capped_by_max_leverage = False

            intrabar_exit = _resolve_intrabar_exit(
                intrabar_df=intrabar_df,
                intrabar_timeframe=intrabar_timeframe,
                base_timeframe=config.timeframe,
                direction=direction,
                entry_time=entry_ts,
                scheduled_exit_time=exit_ts,
                stop_price=_resolve_level_value(stop_loss_series, entry_idx),
                take_profit_price=_resolve_level_value(take_profit_series, entry_idx),
            )
            if intrabar_exit is not None:
                exit_price = intrabar_exit["price"]
                exit_ts = intrabar_exit["time"]
                exit_reason = intrabar_exit["reason"]
                trade_meta["modeling_resolution"] = intrabar_exit["resolution"]

            sizing = _size_trade(
                config=config,
                equity=equity,
                entry_price=entry_price,
                asset_class=asset_class,
                stop_loss_series=stop_loss_series,
                atr_series=atr_series,
                entry_idx=entry_idx,
                symbol=symbol,
                trade_counter=trade_counter,
            )
            qty = sizing["qty"]
            sizing_mode = sizing["sizing_mode"]
            risk_target_usd = sizing["risk_target_usd"]
            stop_distance = sizing["stop_distance"]
            max_position_pct = sizing["max_position_pct"]
            max_leverage = sizing["max_leverage"]
            required_notional = sizing["required_notional"]
            cap_notional = sizing["cap_notional"]
            leverage_notional_limit = sizing["leverage_notional_limit"]
            final_notional = sizing["final_notional"]
            capped_by_max_position = sizing["capped_by_max_position"]
            capped_by_max_leverage = sizing["capped_by_max_leverage"]

            if stop_price is None and stop_distance is not None and stop_distance > 0:
                stop_price = (
                    entry_price - stop_distance
                    if direction == "LONG"
                    else entry_price + stop_distance
                )

            if stop_price is None:
                sl_from_meta = trade_meta.get("sl_price")
                if isinstance(sl_from_meta, (int, float)):
                    stop_price = float(sl_from_meta)

            if stop_distance is None and stop_price is not None:
                stop_distance = abs(entry_price - stop_price)

            trade_meta["risk_math"] = {
                "sizing_mode": sizing_mode,
                "equity_at_entry": round(equity, 2),
                "risk_target_usd": round(risk_target_usd, 2) if risk_target_usd is not None else None,
                "entry_price": round(entry_price, 6),
                "sl_price": round(stop_price, 6) if stop_price is not None else None,
                "stop_distance": round(stop_distance, 6) if stop_distance is not None else None,
                "position_size": round(qty, 4),
                "required_notional": (
                    round(required_notional, 2) if required_notional is not None else None
                ),
                "final_notional": round(final_notional, 2) if final_notional is not None else None,
                "max_position_notional": (
                    round(cap_notional, 2) if cap_notional is not None else None
                ),
                "max_leverage_notional": (
                    round(leverage_notional_limit, 2)
                    if leverage_notional_limit is not None
                    else None
                ),
                "max_position_pct": round(max_position_pct, 6) if max_position_pct is not None else None,
                "max_leverage": round(max_leverage, 6) if max_leverage is not None else None,
                "capped_by_max_position": capped_by_max_position,
                "capped_by_max_leverage": capped_by_max_leverage,
            }

            if direction == "LONG":
                pnl = (exit_price - entry_price) * qty
            else:
                pnl = (entry_price - exit_price) * qty

            trade = Trade(
                id=trade_counter,  # temporary; renumbered after merge
                symbol=symbol,
                direction=direction,
                entry_time=entry_ts,
                entry_price=round(entry_price, 6),
                quantity=round(qty, 4),
                exits=[
                    TradeExit(
                        time=exit_ts,
                        price=round(exit_price, 6),
                        quantity=round(qty, 4),
                        pnl=round(pnl, 2),
                        reason=exit_reason,
                    )
                ],
                pnl=round(pnl, 2),
                holding_bars=i - entry_idx,
                tags=["vectorbt"],
                meta=trade_meta,
            )
            trades.append(trade)
            equity += pnl

    return trades, extra_timeframes, drawings, indicator_series, len(df)


class VectorbtAdapter(EngineAdapter):
    """Adapter for vectorbt-style vectorized backtesting."""

    @property
    def name(self) -> str:
        return "vectorbt"

    def run(
        self,
        config: BacktestConfig,
        strategy_path: str,
        progress_callback: ProgressCallback | None = None,
    ) -> BacktestResult:
        strategy_mod = _load_strategy_module(strategy_path)
        if not hasattr(strategy_mod, "generate_signals"):
            raise ValueError(f"Strategy {strategy_path} must define generate_signals(df, params)")

        data_root = Path(config.data_dir) if config.data_dir else DATA_DIR
        resolved_parameters = resolve_strategy_parameters(strategy_path, config.parameters)
        all_trades: list[Trade] = []
        all_drawings: DrawingList = []
        all_indicators: list[IndicatorSeries] = []
        used_timeframes = [config.timeframe]
        total_loaded_bars = 0

        num_symbols = len(config.symbols)
        use_parallel = num_symbols > 1

        if use_parallel:
            max_workers = min(num_symbols, MAX_PARALLEL_WORKERS)
            symbol_errors: list[tuple[str, Exception]] = []
            logger.info(
                "Parallel backtest: %d symbols across %d workers", num_symbols, max_workers
            )
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        _process_symbol, sym, config, strategy_path, resolved_parameters, data_root
                    ): sym
                    for sym in config.symbols
                }
                for future in as_completed(futures):
                    sym = futures[future]
                    try:
                        trades, extra_tfs, drawings, indicator_series, loaded_bars = future.result()
                        total_loaded_bars += loaded_bars
                        all_trades.extend(trades)
                        all_drawings.extend(drawings)
                        all_indicators.extend(indicator_series)
                        for tf in extra_tfs:
                            if tf not in used_timeframes:
                                used_timeframes.append(tf)
                    except Exception as exc:
                        logger.exception("Failed processing symbol %s", sym)
                        symbol_errors.append((sym, exc))
            if symbol_errors:
                failed_symbols = ", ".join(symbol for symbol, _ in symbol_errors)
                raise RuntimeError(
                    f"Vectorbt backtest failed for symbol(s): {failed_symbols}"
                ) from symbol_errors[0][1]
        else:
            for symbol in config.symbols:
                trades, extra_tfs, drawings, indicator_series, loaded_bars = _process_symbol(
                    symbol, config, strategy_path, resolved_parameters, data_root
                )
                total_loaded_bars += loaded_bars
                all_trades.extend(trades)
                all_drawings.extend(drawings)
                all_indicators.extend(indicator_series)
                for tf in extra_tfs:
                    if tf not in used_timeframes:
                        used_timeframes.append(tf)

        # Renumber trade IDs sequentially after merge
        all_trades.sort(key=lambda t: t.entry_time)
        for idx, trade in enumerate(all_trades, start=1):
            trade.id = idx

        # Build equity curve from sorted trades (Cleanup 16-D1, F5).
        # Semantics decision: the persisted curve is a REALIZED-PnL portfolio
        # curve over EXIT time. Each symbol's own sizing compounds from the
        # shared initial capital inside `_process_symbol`; summing those
        # per-symbol paths into one "portfolio" equity would multiply capital
        # by the symbol count, so the aggregate curve here intentionally
        # replays only closed PnL, once per portfolio, credited at the trade's
        # exit — and therefore replayed in EXIT order (native/pynecore do the
        # same). Drawdown/Sharpe computed on this curve describe the realized
        # portfolio, not an intraday mark-to-market path — the limitation is
        # documented in the cross-engine contract.
        all_equity: list[EquityPoint] = []
        equity = config.initial_capital
        for trade in sorted(all_trades, key=lambda item: item.last_exit_time):
            equity += trade.pnl
            all_equity.append(
                EquityPoint(
                    time=trade.last_exit_time or trade.entry_time,
                    equity=round(equity, 2),
                )
            )

        now = datetime.now(timezone.utc)
        if hasattr(strategy_mod, "STRATEGY_META"):
            strategy_name = strategy_mod.STRATEGY_META.get("name", config.strategy_name)
            strategy_names = [strategy_name]
        else:
            strategy_names = [config.strategy_name]

        duration_days = 0
        try:
            from datetime import date as _date

            d_start = _date.fromisoformat(config.start_date)
            d_end = _date.fromisoformat(config.end_date)
            duration_days = (d_end - d_start).days
        except (ValueError, TypeError):
            pass

        meta = BacktestMeta(
            id=f"vbt-{config.strategy_name}-{now.strftime('%Y%m%d-%H%M%S')}",
            created_at=now.isoformat(),
            engine="vectorbt",
            strategies=strategy_names,
            backtest_start=config.start_date,
            backtest_end=config.end_date,
            duration_days=duration_days,
            # Conformance (Cleanup 16-A, F6): total_bars is the loaded
            # main-timeframe bar count summed across symbols — the same
            # semantic as the native and pynecore adapters — NOT the trade
            # count it degenerated to previously.
            total_bars=total_loaded_bars,
            data={
                "timeframes": used_timeframes,
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
            indicators=all_indicators,
            drawings=all_drawings,
        )


def _load_strategy_module(path: str):
    p = Path(path).resolve()
    spec = importlib.util.spec_from_file_location("strategy", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _normalize_signal_output(
    output: Any,
    index: pd.Index,
    *,
    symbol: str,
) -> tuple[pd.DataFrame, IndicatorMap, DrawingList]:
    if isinstance(output, pd.DataFrame):
        if "entries" not in output.columns or "exits" not in output.columns:
            raise ValueError("Strategy DataFrame output must include 'entries' and 'exits' columns")
        signals = output.copy()
        signals.index = index
        signals["entries"] = _coerce_boolean_series(signals["entries"], index)
        signals["exits"] = _coerce_boolean_series(signals["exits"], index)
        return signals, {}, []

    if isinstance(output, (tuple, list)):
        raw_drawings: Any = []
        if len(output) == 4:
            raw_entries, raw_exits, raw_indicators, raw_drawings = output
        elif len(output) == 3:
            raw_entries, raw_exits, raw_indicators = output
        elif len(output) == 2:
            raw_entries, raw_exits = output
            raw_indicators = {}
        else:
            raise ValueError(
                "Tuple/list strategy output must be (entries, exits), "
                "(entries, exits, indicators_dict), or "
                "(entries, exits, indicators_dict, drawings)"
            )

        signals = pd.DataFrame(index=index)
        signals["entries"] = _coerce_boolean_series(raw_entries, index)
        signals["exits"] = _coerce_boolean_series(raw_exits, index)
        return (
            signals,
            _normalize_indicator_map(raw_indicators, index),
            _normalize_drawings(raw_drawings, symbol=symbol),
        )

    raise TypeError(
        "generate_signals() must return a pandas DataFrame or a tuple/list of "
        "(entries, exits, indicators_dict[, drawings])"
    )


def _coerce_series(value: Any, index: pd.Index) -> pd.Series:
    if isinstance(value, pd.Series):
        series = value.copy()
        if len(series) != len(index):
            raise ValueError(
                f"Indicator series length mismatch: expected {len(index)}, got {len(series)}"
            )
        series.index = index
        return series

    try:
        series = pd.Series(value, index=index)
    except ValueError as exc:
        raise ValueError(f"Could not align series to DataFrame index: {exc}") from exc

    if len(series) != len(index):
        raise ValueError(
            f"Indicator series length mismatch: expected {len(index)}, got {len(series)}"
        )
    return series


def _coerce_boolean_series(value: Any, index: pd.Index) -> pd.Series:
    return _coerce_series(value, index).fillna(False).astype(bool)


def _prepare_next_open_execution_data(
    signals: pd.DataFrame,
    indicators: IndicatorMap,
) -> tuple[pd.DataFrame, IndicatorMap]:
    """Shift strategy output by one bar so signal bars execute on the next bar open."""
    execution_signals = signals.shift(1)
    execution_signals.index = signals.index
    execution_signals["entries"] = _coerce_boolean_series(execution_signals["entries"], signals.index)
    shifted_exits = _coerce_boolean_series(execution_signals["exits"], signals.index)
    original_exits = _coerce_boolean_series(signals["exits"], signals.index)
    if len(shifted_exits) > 0 and bool(original_exits.iloc[-1]):
        shifted_exits.iloc[-1] = True
    execution_signals["exits"] = shifted_exits

    execution_indicators: IndicatorMap = {}
    for name, (series, explicit_when) in indicators.items():
        shifted_series = series.shift(1)
        shifted_series.index = signals.index
        execution_indicators[name] = (shifted_series, explicit_when)

    return execution_signals, execution_indicators


def _normalize_indicator_map(indicators: Any, index: pd.Index) -> IndicatorMap:
    if indicators is None:
        return {}
    if not isinstance(indicators, dict):
        raise TypeError("indicators_dict must be a dict mapping indicator names to aligned series")

    normalized: IndicatorMap = {}
    for name, raw_value in indicators.items():
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


def _infer_indicator_when(name: str, explicit_when: str) -> str:
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


def _indicator_meta_key(name: str) -> str:
    lowered = name.lower()
    if lowered in {"exit_price", "exit_reason"}:
        return lowered
    if lowered.startswith("exit_"):
        return name[5:]
    if lowered == "stop_loss":
        return "sl_price"
    if lowered in {"take_profit", "tp_price"}:
        return "tp_price"
    return name


def _find_indicator_series(
    signals: pd.DataFrame, indicators: IndicatorMap, *, kind: str
) -> pd.Series | None:
    if kind == "direction" and "direction" in signals.columns:
        return signals["direction"]
    if kind == "stop_loss" and "stop_loss" in signals.columns:
        return signals["stop_loss"]
    if kind == "take_profit" and "take_profit" in signals.columns:
        return signals["take_profit"]
    if kind == "take_profit" and "tp_price" in signals.columns:
        return signals["tp_price"]
    if kind == "exit_price" and "exit_price" in signals.columns:
        return signals["exit_price"]
    if kind == "exit_reason" and "exit_reason" in signals.columns:
        return signals["exit_reason"]
    if kind == "atr":
        for column in signals.columns:
            if str(column).lower().startswith("atr"):
                return signals[column]

    desired_when = "exit" if kind in {"exit_price", "exit_reason"} else "entry"
    for name, (series, explicit_when) in indicators.items():
        when = _infer_indicator_when(name, explicit_when)
        if when != desired_when:
            continue

        lowered = name.lower()
        if kind == "direction" and lowered == "direction":
            return series
        if kind == "stop_loss" and lowered in {"stop_loss", "sl_price"}:
            return series
        if kind == "take_profit" and lowered in {"take_profit", "tp_price"}:
            return series
        if kind == "exit_price" and lowered == "exit_price":
            return series
        if kind == "exit_reason" and lowered == "exit_reason":
            return series
        if kind == "atr" and lowered.startswith("atr"):
            return series
    return None


def _find_stop_loss_series(signals: pd.DataFrame, indicators: IndicatorMap) -> pd.Series | None:
    return _find_indicator_series(signals, indicators, kind="stop_loss")


def _find_take_profit_series(signals: pd.DataFrame, indicators: IndicatorMap) -> pd.Series | None:
    return _find_indicator_series(signals, indicators, kind="take_profit")


def _find_atr_series(signals: pd.DataFrame, indicators: IndicatorMap) -> pd.Series | None:
    return _find_indicator_series(signals, indicators, kind="atr")


def _find_direction_series(signals: pd.DataFrame, indicators: IndicatorMap) -> pd.Series | None:
    return _find_indicator_series(signals, indicators, kind="direction")


def _find_exit_price_series(signals: pd.DataFrame, indicators: IndicatorMap) -> pd.Series | None:
    return _find_indicator_series(signals, indicators, kind="exit_price")


def _find_exit_reason_series(signals: pd.DataFrame, indicators: IndicatorMap) -> pd.Series | None:
    return _find_indicator_series(signals, indicators, kind="exit_reason")


def _normalize_direction_value(value: Any) -> str:
    if value in ("SHORT", "short", -1):
        return "SHORT"
    return "LONG"


def _resolve_direction(
    signals: pd.DataFrame, direction_series: pd.Series | None, entry_idx: int
) -> str:
    if "direction" in signals.columns:
        return _normalize_direction_value(signals["direction"].iloc[entry_idx])
    if direction_series is not None:
        return _normalize_direction_value(direction_series.iloc[entry_idx])
    return "LONG"


def _resolve_exit_price(
    exit_row: pd.Series, exit_idx: int, exit_price_series: pd.Series | None
) -> float:
    if exit_price_series is not None:
        exit_price = exit_price_series.iloc[exit_idx]
        if pd.notna(exit_price) and float(exit_price) > 0:
            return float(exit_price)
    open_value = exit_row.get("open")
    if open_value is not None and pd.notna(open_value):
        return float(open_value)
    return float(exit_row["close"])


def _resolve_entry_price(entry_row: pd.Series) -> float:
    open_value = entry_row.get("open")
    if open_value is not None and pd.notna(open_value):
        return float(open_value)
    return float(entry_row["close"])


def _resolve_exit_reason(exit_idx: int, exit_reason_series: pd.Series | None) -> str | None:
    if exit_reason_series is None:
        return None
    exit_reason = _serialize_meta_value(exit_reason_series.iloc[exit_idx])
    if exit_reason is None:
        return None
    return str(exit_reason)


def _resolve_level_value(series: pd.Series | None, idx: int) -> float | None:
    if series is None:
        return None
    value = series.iloc[idx]
    if pd.isna(value):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    return numeric


def _resolve_stop_distance(
    entry_idx: int,
    entry_price: float,
    *,
    stop_loss_series: pd.Series | None,
    atr_series: pd.Series | None,
) -> float | None:
    if stop_loss_series is not None:
        sl = stop_loss_series.iloc[entry_idx]
        if pd.notna(sl) and float(sl) > 0:
            return abs(entry_price - float(sl))

    if atr_series is not None:
        atr = atr_series.iloc[entry_idx]
        if pd.notna(atr) and float(atr) > 0:
            return float(atr)

    return None


def _load_ohlcv(
    data_root: Path,
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame | None, Path | None]:
    """Find and load OHLCV parquet for a symbol."""
    ohlcv_root = data_root / "ohlcv"
    matches = list(ohlcv_root.rglob(f"**/{symbol}/{timeframe}.parquet"))
    if not matches:
        return None, None

    selected_path = matches[0]
    df = pd.read_parquet(selected_path)
    df.columns = [c.lower() for c in df.columns]

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        # Conformance (Cleanup 16-A, F9): shared date-filter convention — a
        # date-only end boundary is inclusive of the full day.
        df = contract.apply_date_filter(df, start=start_date, end=end_date)

    return df.reset_index(drop=True), selected_path


def _normalize_intrabar_resolution(value: str | None) -> str:
    # Conformance (Cleanup 16-A): the spelling convention lives in ONE place
    # (contract.normalize_intrabar_resolution); this delegation keeps the
    # module's private name for existing callers while guaranteeing vectorbt
    # and the native engine can never normalize differently.
    return contract.normalize_intrabar_resolution(value)


def _resolve_intrabar_timeframe(base_timeframe: str, intrabar_resolution: str | None) -> str | None:
    # Conformance (Cleanup 16-A, F8): same single-source resolver as native —
    # the same configuration must load the same intrabar frame everywhere.
    return contract.resolve_intrabar_timeframe(base_timeframe, intrabar_resolution)


def _load_intrabar_ohlcv(
    data_root: Path,
    symbol: str,
    base_timeframe: str,
    intrabar_resolution: str | None,
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame | None, str | None]:
    target_timeframe = _resolve_intrabar_timeframe(base_timeframe, intrabar_resolution)
    if not target_timeframe:
        return None, None

    intrabar_df, _ = _load_ohlcv(data_root, symbol, target_timeframe, start_date, end_date)
    if intrabar_df is None or intrabar_df.empty:
        logger.info(
            "Intrabar data unavailable for %s/%s at %s; falling back to open/close exits",
            symbol,
            base_timeframe,
            target_timeframe,
        )
        return None, None
    return intrabar_df, target_timeframe


def _resolve_intrabar_exit(
    *,
    intrabar_df: pd.DataFrame | None,
    intrabar_timeframe: str | None,
    base_timeframe: str,
    direction: str,
    entry_time: str,
    scheduled_exit_time: str,
    stop_price: float | None,
    take_profit_price: float | None,
) -> dict[str, Any] | None:
    if intrabar_df is None or intrabar_df.empty or intrabar_timeframe is None:
        return None
    if stop_price is None and take_profit_price is None:
        return None

    entry_ts = pd.to_datetime(entry_time, utc=True, errors="coerce")
    exit_ts = pd.to_datetime(scheduled_exit_time, utc=True, errors="coerce")

    if pd.isna(entry_ts) or pd.isna(exit_ts):
        return None

    effective_start = entry_ts
    effective_end = exit_ts
    if effective_end <= effective_start:
        return None

    timestamps = pd.to_datetime(intrabar_df["timestamp"], utc=True, errors="coerce")
    # The upper bound is EXCLUSIVE because vectorbt schedules exits at the
    # next bar's open (F10 execution-timing models, contract doc §3) — the
    # exit bar's own sub-bars are outside the resolution window. If exits are
    # ever moved onto the signal bar, this slice must become inclusive or the
    # intrabar resolution silently degrades to main-frame fills.
    window = intrabar_df.loc[(timestamps >= effective_start) & (timestamps < effective_end)]
    if window.empty:
        return None

    for row in window.itertuples(index=False):
        timestamp = pd.to_datetime(getattr(row, "timestamp"), utc=True, errors="coerce")
        if pd.isna(timestamp):
            continue
        high = float(getattr(row, "high"))
        low = float(getattr(row, "low"))
        event = _match_intrabar_exit_event(
            direction=direction,
            high=high,
            low=low,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
        )
        if event is None:
            continue
        event["time"] = timestamp.isoformat()
        event["resolution"] = intrabar_timeframe
        return event

    return None


def _match_intrabar_exit_event(
    *,
    direction: str,
    high: float,
    low: float,
    stop_price: float | None,
    take_profit_price: float | None,
) -> dict[str, Any] | None:
    """Conformance (Cleanup 16-A, F8): shared same-bar classification.

    Called per intrabar step in chronological order, so the first event seen
    by the caller is the actual chronological event; within a single step the
    stop-first tie-break applies (Decision Record, Cleanup 16-A).
    """
    event_kind = resolve_same_bar_exit(
        direction=direction,
        high=high,
        low=low,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
    )
    if event_kind == "stop":
        return {"price": float(stop_price), "reason": "stop_loss"}
    if event_kind == "tp":
        return {"price": float(take_profit_price), "reason": "take_profit"}
    return None


def _infer_asset_class_from_path(data_path: Path | None) -> str:
    if data_path is None:
        return "unknown"
    parts = data_path.parts
    try:
        ohlcv_idx = parts.index("ohlcv")
        return parts[ohlcv_idx + 2].lower()
    except (ValueError, IndexError):
        return "unknown"


def _extract_trade_meta(
    signals: pd.DataFrame,
    indicators: IndicatorMap,
    entry_idx: int,
    exit_idx: int,
) -> dict[str, Any]:
    trade_meta: dict[str, Any] = {}

    for column in signals.columns:
        col_name = str(column)
        if col_name.startswith("meta_"):
            serialized = _serialize_meta_value(signals.iloc[entry_idx][column])
            if serialized is not None:
                trade_meta[col_name[5:]] = serialized
        elif col_name.startswith("exit_meta_"):
            serialized = _serialize_meta_value(signals.iloc[exit_idx][column])
            if serialized is not None:
                trade_meta[col_name[10:]] = serialized

    if "stop_loss" in signals.columns:
        serialized = _serialize_meta_value(signals["stop_loss"].iloc[entry_idx])
        if serialized is not None:
            trade_meta.setdefault("sl_price", serialized)

    for name, (series, explicit_when) in indicators.items():
        when = _infer_indicator_when(name, explicit_when)
        sample_idx = exit_idx if when == "exit" else entry_idx
        meta_key = _indicator_meta_key(name)
        serialized = _serialize_meta_value(series.iloc[sample_idx])
        if serialized is None:
            continue
        trade_meta[meta_key] = serialized
        if meta_key == "sl_price":
            trade_meta.setdefault("sl_price", serialized)

    return trade_meta


def _serialize_meta_value(value: Any) -> Any | None:
    """Deprecated shim: use runner.engines._shared.serialize_meta_value."""
    return _shared_serialize_meta_value(value)
