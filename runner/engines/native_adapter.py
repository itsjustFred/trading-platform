from __future__ import annotations

import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from runner.engine import TradeEngine
from runner.engines.base import BacktestConfig, EngineAdapter, resolve_strategy_parameters
from shared.constants import DATA_DIR
from shared.models import BacktestMeta, BacktestResult, EquityPoint

TIMEFRAME_ORDER = ["tick", "s1", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]


def _load_strategy_module(path: str):
    resolved = Path(path).resolve()
    spec = importlib.util.spec_from_file_location("strategy", str(resolved))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_ohlcv(data_root: Path, symbol: str, timeframe: str, start: str, end: str) -> pd.DataFrame | None:
    matches = list((data_root / "ohlcv").rglob(f"**/{symbol}/{timeframe}.parquet"))
    if not matches:
        return None

    df = pd.read_parquet(matches[0])
    df.columns = [str(col).lower() for col in df.columns]
    if "timestamp" not in df.columns:
        return None

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
    if start:
        df = df[df["timestamp"] >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df["timestamp"] <= pd.Timestamp(end, tz="UTC")]
    return df.reset_index(drop=True)


def _is_finer(candidate: str, base: str) -> bool:
    try:
        return TIMEFRAME_ORDER.index(candidate) < TIMEFRAME_ORDER.index(base)
    except ValueError:
        return False


def _load_intrabar_data(
    data_root: Path,
    symbol: str,
    base_timeframe: str,
    modeling_resolution: str,
    start: str,
    end: str,
) -> pd.DataFrame | None:
    selected = (modeling_resolution or "").strip().lower()
    if selected in {"", "main", "main_timeframe", "open_close", base_timeframe.lower()}:
        return None
    if not _is_finer(selected, base_timeframe.lower()):
        return None
    return _load_ohlcv(data_root, symbol, selected, start, end)


class NativeAdapter(EngineAdapter):
    @property
    def name(self) -> str:
        return "native"

    def run(self, config: BacktestConfig, strategy_path: str) -> BacktestResult:
        strategy_module = _load_strategy_module(strategy_path)
        if not hasattr(strategy_module, "generate_signals"):
            raise ValueError(f"Strategy {strategy_path} must define generate_signals")

        data_root = Path(config.data_dir) if config.data_dir else DATA_DIR
        resolved_parameters = resolve_strategy_parameters(strategy_path, config.parameters)

        all_trades = []
        all_drawings: list[dict[str, Any]] = []
        used_timeframes = [config.timeframe]

        for symbol in config.symbols:
            data = _load_ohlcv(data_root, symbol, config.timeframe, config.start_date, config.end_date)
            if data is None or data.empty:
                continue

            intrabar = _load_intrabar_data(
                data_root,
                symbol,
                config.timeframe,
                config.modeling_resolution,
                config.start_date,
                config.end_date,
            )
            if intrabar is not None and not intrabar.empty and config.modeling_resolution not in used_timeframes:
                used_timeframes.append(config.modeling_resolution)

            symbol_config = config.model_copy(update={"symbols": [symbol], "parameters": resolved_parameters})
            engine = TradeEngine(symbol_config, symbol=symbol, intrabar_data=intrabar)
            symbol_result = engine.run(data, strategy_module.generate_signals)

            all_trades.extend(symbol_result.trades)
            for drawing in symbol_result.drawings:
                payload = dict(drawing)
                payload.setdefault("symbol", symbol)
                all_drawings.append(payload)

        all_trades.sort(key=lambda trade: (trade.entry_time, trade.id))
        for index, trade in enumerate(all_trades, start=1):
            trade.id = index

        equity_curve: list[EquityPoint] = []
        running = config.initial_capital
        for trade in sorted(all_trades, key=lambda item: item.last_exit_time):
            running += trade.pnl
            equity_curve.append(EquityPoint(time=trade.last_exit_time, equity=round(running, 2)))

        now = datetime.now(timezone.utc)
        duration_days = 0
        try:
            duration_days = (date.fromisoformat(config.end_date) - date.fromisoformat(config.start_date)).days
        except Exception:
            duration_days = 0

        strategy_name = config.strategy_name
        if hasattr(strategy_module, "STRATEGY_META") and isinstance(strategy_module.STRATEGY_META, dict):
            strategy_name = strategy_module.STRATEGY_META.get("name", strategy_name)

        meta = BacktestMeta(
            id=f"native-{config.strategy_name}-{now.strftime('%Y%m%d-%H%M%S')}",
            created_at=now.isoformat(),
            engine="native",
            strategies=[strategy_name],
            backtest_start=config.start_date,
            backtest_end=config.end_date,
            duration_days=duration_days,
            total_bars=0,
            data={
                "timeframes": used_timeframes,
                "symbols": config.symbols,
                "universe": "",
                "source": "ohlcv",
            },
            initial_capital=config.initial_capital,
            parameters=resolved_parameters,
        )

        return BacktestResult(
            meta=meta,
            trades=all_trades,
            equity_curve=equity_curve,
            drawings=all_drawings,
        )
