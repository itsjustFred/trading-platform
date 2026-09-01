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
from typing import Any

import pandas as pd

from runner.engines.base import (
    BacktestConfig,
    EngineAdapter,
    ProgressCallback,
    resolve_strategy_parameters,
)
from shared.constants import DATA_DIR
from shared.models import BacktestMeta, BacktestResult, EquityPoint, IndicatorSeries, Trade, TradeExit

logger = logging.getLogger(__name__)

_RISK_WARNING_KEYS: set[tuple[str, str, str]] = set()


def _warn_once(key: tuple[str, str, str], message: str, *args: Any) -> None:
    if key in _RISK_WARNING_KEYS:
        return
    _RISK_WARNING_KEYS.add(key)
    logger.warning(message, *args)


IndicatorMap = dict[str, tuple[pd.Series, str]]
DrawingList = list[dict[str, Any]]
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
PANEL_INDICATOR_HINTS = ("rsi", "macd", "signal", "hist", "atr", "osc", "stoch", "cci", "adx")
HIDDEN_DISPLAY_HINTS = (
    "sl_price",
    "stop_loss",
    "tp_price",
    "take_profit",
    "exit_price",
    "exit_reason",
    "direction",
)


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


def _process_symbol(
    symbol: str,
    config: BacktestConfig,
    strategy_path: str,
    resolved_parameters: dict[str, Any],
    data_root: Path,
) -> tuple[list[Trade], list[str], DrawingList, list[IndicatorSeries]]:
    """Process a single symbol: load data, generate signals, extract trades.

    Returns (trades_with_temp_ids, used_intrabar_timeframes, drawings).
    Runs in its own process when parallelized.
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
            sizing_mode = "full_equity"

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

            if config.risk_per_trade_pct is not None and config.risk_per_trade_pct > 0:
                sizing_mode = "risk_pct"
                max_position_pct = _resolve_max_position_pct(
                    config.max_position_pct, asset_class
                )
                max_leverage = _resolve_max_leverage(config.max_leverage)
                if max_leverage < max_position_pct:
                    max_leverage = max_position_pct

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

                if required_notional <= cap_notional:
                    final_notional = required_notional
                elif required_notional <= leverage_notional_limit:
                    final_notional = required_notional
                    logger.info(
                        "Leverage used for %s trade %s: required_notional=%.2f exceeds cap=%.2f (max_leverage=%.2fx)",
                        symbol,
                        trade_counter,
                        required_notional,
                        cap_notional,
                        max_leverage,
                    )
                else:
                    final_notional = leverage_notional_limit
                    achievable_risk = (final_notional / entry_price) * stop_distance
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

    return trades, extra_timeframes, drawings, indicator_series


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
                        trades, extra_tfs, drawings, indicator_series = future.result()
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
                trades, extra_tfs, drawings, indicator_series = _process_symbol(
                    symbol, config, strategy_path, resolved_parameters, data_root
                )
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

        # Build equity curve from sorted trades
        all_equity: list[EquityPoint] = []
        equity = config.initial_capital
        for trade in all_trades:
            equity += trade.pnl
            all_equity.append(EquityPoint(time=trade.entry_time, equity=round(equity, 2)))

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
            total_bars=sum(1 for _ in all_trades),
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


def _infer_indicator_display_type(name: str) -> str:
    lowered = name.lower()
    if any(hint in lowered for hint in PANEL_INDICATOR_HINTS):
        return "panel"
    return "overlay"


def _infer_indicator_pane(name: str) -> str | None:
    lowered = name.lower()
    if "macd" in lowered or "signal" in lowered or "hist" in lowered:
        return "macd"
    if "rsi" in lowered:
        return "rsi"
    if "atr" in lowered:
        return "atr"
    return None


def _is_hidden_indicator(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in HIDDEN_DISPLAY_HINTS)


def _series_points(
    timestamps: pd.Series,
    values: pd.Series,
    *,
    include_gaps: bool = False,
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for idx, raw_time in timestamps.items():
        if idx not in values.index:
            continue
        value = values.loc[idx]
        timestamp = pd.to_datetime(raw_time, utc=True, errors="coerce")
        if pd.isna(timestamp):
            continue

        point: dict[str, Any] = {"time": pd.Timestamp(timestamp).isoformat()}
        if pd.isna(value):
            if include_gaps:
                point["value"] = None
                points.append(point)
            continue

        point["value"] = round(float(value), 6)
        points.append(point)

    return points


def _supertrend_component(name: str) -> str | None:
    lowered = name.lower()
    if "supertrend" not in lowered:
        return None

    if any(token in lowered for token in ("_down", "down_", "upper", "bear")):
        return "down"
    if any(token in lowered for token in ("_up", "up_", "lower", "bull")):
        return "up"
    if "trend" in lowered:
        return "trend"
    return None


def _build_supertrend_series(
    timestamps: pd.Series,
    indicators: IndicatorMap,
) -> tuple[list[IndicatorSeries], set[str]]:
    trend_series: pd.Series | None = None
    up_series: pd.Series | None = None
    down_series: pd.Series | None = None
    consumed: set[str] = set()

    for name, (series, _explicit_when) in indicators.items():
        component = _supertrend_component(name)
        if component is None:
            continue
        if component == "trend" and trend_series is None:
            trend_series = pd.to_numeric(series, errors="coerce")
            consumed.add(name)
            continue
        if component == "up" and up_series is None:
            up_series = pd.to_numeric(series, errors="coerce")
            consumed.add(name)
            continue
        if component == "down" and down_series is None:
            down_series = pd.to_numeric(series, errors="coerce")
            consumed.add(name)
            continue

    if up_series is None and down_series is None:
        return [], set()

    supertrend_indicators: list[IndicatorSeries] = []

    if up_series is not None:
        split_up = up_series.copy()
        if trend_series is not None:
            split_up = split_up.where(trend_series > 0)
        points = _series_points(timestamps, split_up, include_gaps=True)
        if points:
            supertrend_indicators.append(
                IndicatorSeries(
                    name="supertrend_up",
                    type="overlay",
                    data=points,
                    style={"color": "#22c55e", "pane": "price"},
                )
            )

    if down_series is not None:
        split_down = down_series.copy()
        if trend_series is not None:
            split_down = split_down.where(trend_series < 0)
        points = _series_points(timestamps, split_down, include_gaps=True)
        if points:
            supertrend_indicators.append(
                IndicatorSeries(
                    name="supertrend_down",
                    type="overlay",
                    data=points,
                    style={"color": "#ef4444", "pane": "price"},
                )
            )

    return supertrend_indicators, consumed


def _build_indicator_series(df: pd.DataFrame, indicators: IndicatorMap) -> list[IndicatorSeries]:
    if not indicators or "timestamp" not in df.columns:
        return []

    timestamps = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    output: list[IndicatorSeries] = []
    supertrend_series, consumed = _build_supertrend_series(timestamps, indicators)
    output.extend(supertrend_series)

    for name, (series, _explicit_when) in indicators.items():
        if name in consumed or _is_hidden_indicator(name):
            continue

        numeric = pd.to_numeric(series, errors="coerce")
        if numeric.isna().all():
            continue

        points = _series_points(timestamps, numeric)
        if not points:
            continue

        display_type = _infer_indicator_display_type(name)
        style: dict[str, Any] = {}
        pane = _infer_indicator_pane(name)
        if pane is not None:
            style["pane"] = pane

        output.append(
            IndicatorSeries(
                name=name,
                type=display_type,
                data=points,
                style=style,
            )
        )

    return output


def _normalize_drawings(drawings: Any, *, symbol: str) -> DrawingList:
    if drawings is None:
        return []
    if not isinstance(drawings, list):
        raise TypeError("drawings must be a list of drawing dictionaries")

    normalized: DrawingList = []
    for idx, raw_drawing in enumerate(drawings, start=1):
        if not isinstance(raw_drawing, dict):
            raise TypeError(f"drawing #{idx} must be a dict")

        drawing_type = str(raw_drawing.get("type", "")).strip()
        if drawing_type not in {"marker", "line"}:
            raise ValueError(
                f"drawing #{idx} has unsupported type {drawing_type!r}; use 'marker' or 'line'"
            )

        normalized_drawing: dict[str, Any] = {}
        for key, value in raw_drawing.items():
            serialized = _serialize_meta_value(value)
            if serialized is not None:
                normalized_drawing[str(key)] = serialized

        normalized_drawing["type"] = drawing_type
        normalized_drawing.setdefault("symbol", symbol)

        if drawing_type == "marker":
            normalized_drawing["time"] = _normalize_drawing_time(
                raw_drawing.get("time"), field_name="time", drawing_index=idx
            )
        else:
            normalized_drawing["start_time"] = _normalize_drawing_time(
                raw_drawing.get("start_time"), field_name="start_time", drawing_index=idx
            )
            normalized_drawing["end_time"] = _normalize_drawing_time(
                raw_drawing.get("end_time"), field_name="end_time", drawing_index=idx
            )
            if "val1" not in raw_drawing or "val2" not in raw_drawing:
                raise ValueError(f"drawing #{idx} line drawings require val1 and val2")
            try:
                normalized_drawing["val1"] = float(raw_drawing["val1"])
                normalized_drawing["val2"] = float(raw_drawing["val2"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"drawing #{idx} line values must be numeric"
                ) from exc

        normalized.append(normalized_drawing)

    return normalized


def _normalize_drawing_time(value: Any, *, field_name: str, drawing_index: int) -> str:
    if value is None:
        raise ValueError(f"drawing #{drawing_index} missing required field {field_name!r}")

    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass

    if isinstance(value, pd.Timestamp):
        timestamp = value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")
        return timestamp.isoformat()

    if isinstance(value, datetime):
        timestamp = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).isoformat()

    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).isoformat()

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()

    if isinstance(value, str):
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.notna(parsed):
            return pd.Timestamp(parsed).isoformat()
        return value

    raise TypeError(
        f"drawing #{drawing_index} field {field_name!r} must be a timestamp-like value"
    )


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
        if start_date:
            df = df[df["timestamp"] >= _parse_date_bound(start_date, is_end=False)]
        if end_date:
            df = df[df["timestamp"] <= _parse_date_bound(end_date, is_end=True)]

    return df.reset_index(drop=True), selected_path


def _parse_date_bound(value: str, *, is_end: bool) -> pd.Timestamp:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Date filter value cannot be empty")

    parsed = pd.to_datetime(raw, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"Invalid date filter value: {value!r}")

    timestamp = pd.Timestamp(parsed)
    if is_end and "T" not in raw and ":" not in raw:
        return timestamp + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return timestamp


def _normalize_intrabar_resolution(value: str | None) -> str:
    lowered = str(value or "open_close").strip().lower()
    if lowered in {"", "open_close", "open-close", "open/close only", "open_close_only"}:
        return "open_close"
    if lowered in {"1-lower", "1 timeframe lower", "one-lower"}:
        return "1-lower"
    if lowered == "m1":
        return "1m"
    if lowered in {"1m", "s1", "tick"}:
        return lowered
    return "open_close"


def _resolve_intrabar_timeframe(base_timeframe: str, intrabar_resolution: str | None) -> str | None:
    normalized = _normalize_intrabar_resolution(intrabar_resolution)
    if normalized == "open_close":
        return None

    if normalized == "1-lower":
        try:
            base_index = TIMEFRAME_ORDER.index(base_timeframe)
        except ValueError:
            return None
        if base_index <= 0:
            return None
        return TIMEFRAME_ORDER[base_index - 1]

    try:
        base_index = TIMEFRAME_ORDER.index(base_timeframe)
        target_index = TIMEFRAME_ORDER.index(normalized)
    except ValueError:
        return None

    if target_index >= base_index:
        return None
    return normalized


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
    if direction == "LONG":
        stop_hit = stop_price is not None and low <= stop_price
        tp_hit = take_profit_price is not None and high >= take_profit_price
    else:
        stop_hit = stop_price is not None and high >= stop_price
        tp_hit = take_profit_price is not None and low <= take_profit_price

    if stop_hit:
        return {"price": float(stop_price), "reason": "stop_loss"}
    if tp_hit:
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


def _resolve_max_position_pct(explicit: float | None, asset_class: str) -> float:
    if explicit is not None and explicit > 0:
        return float(explicit)
    if asset_class in {"equity", "equities", "etf", "etfs", "stocks"}:
        return 1.0
    return 0.20


def _resolve_max_leverage(explicit: float | None) -> float:
    if explicit is not None and explicit > 0:
        return float(explicit)
    return 2.0
