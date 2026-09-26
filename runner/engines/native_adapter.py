from __future__ import annotations

import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from runner.approval_gate import require_generated_strategy_approval
from runner.engine import TradeEngine
from runner.engines import contract
from runner.engines._drawings import _normalize_drawings
from runner.engines.base import (
    BacktestConfig,
    EngineAdapter,
    ProgressCallback,
    resolve_strategy_parameters,
)
from shared.constants import DATA_DIR
from shared.models import BacktestMeta, BacktestResult, EquityPoint

TIMEFRAME_ORDER = contract.TIMEFRAME_ORDER

# Conformance (Cleanup 16-A, F8): the intrabar-resolution convention (spelling
# table + base→intrabar mapping) lives in ONE place, contract.py, and both the
# native and vectorbt loaders resolve through it — a private per-adapter copy
# is how the "m1" alias ended up honored in one engine and dropped in the
# other.
ONE_LOWER_SPELLINGS = contract.ONE_LOWER_SPELLINGS


def _load_strategy_module(path: str):
    require_generated_strategy_approval(path)
    resolved = Path(path).resolve()
    spec = importlib.util.spec_from_file_location("strategy", str(resolved))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# Merge resolution: the branch's data-source-aware selection wins — routing a
# run to a named feed is behaviour the deterministic `sorted(rglob())[0]` cannot
# express, and every caller now threads `data_source` through. main's
# determinism intent is preserved by `contract.select_ohlcv_path`, which returns
# `min(candidates, key=rank)` with `str(path)` as the final tiebreaker, so the
# choice is a total order rather than filesystem order.
def _load_ohlcv(
    data_root: Path,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    *,
    data_source: str = "",
) -> pd.DataFrame | None:
    selected_path, _ = contract.select_ohlcv_path(
        data_root, symbol, timeframe, data_source=data_source
    )
    if selected_path is None:
        return None

    df = pd.read_parquet(selected_path)
    df.attrs["ohlcv_source_path"] = str(selected_path)
    df.columns = [str(col).lower() for col in df.columns]
    df = contract.normalize_ohlcv_frame(df)
    if "timestamp" not in df.columns:
        return None

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
    # Conformance (Cleanup 16-A): shared date-filter convention (F9) — a
    # date-only end boundary is inclusive of the full day.
    df = contract.apply_date_filter(df, start=start, end=end)
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
    *,
    data_source: str = "",
) -> pd.DataFrame | None:
    # Conformance (Cleanup 16-A, F8): resolve the spelling through the shared
    # resolver so native and vectorbt always agree on which intrabar frame a
    # configuration requests ("1-lower" → next-lower granularity, "m1" → "1m",
    # unknown spellings → no intrabar walk).
    selected = contract.resolve_intrabar_timeframe(base_timeframe, modeling_resolution)
    if not selected:
        return None
    return _load_ohlcv(
        data_root,
        symbol,
        selected,
        start,
        end,
        data_source=data_source,
    )


class NativeAdapter(EngineAdapter):
    @property
    def name(self) -> str:
        return "native"

    def run(
        self,
        config: BacktestConfig,
        strategy_path: str,
        progress_callback: ProgressCallback | None = None,
    ) -> BacktestResult:
        require_generated_strategy_approval(strategy_path)

        def emit_progress(percent: float, message: str) -> None:
            if progress_callback is None:
                return
            bounded = max(0.0, min(100.0, float(percent)))
            progress_callback(round(bounded, 2), message)

        emit_progress(5.0, "Loading market data...")
        strategy_module = _load_strategy_module(strategy_path)
        if not hasattr(strategy_module, "generate_signals"):
            raise ValueError(f"Strategy {strategy_path} must define generate_signals")

        data_root = Path(config.data_dir) if config.data_dir else DATA_DIR
        resolved_parameters = resolve_strategy_parameters(strategy_path, config.parameters)

        all_trades = []
        all_data_paths: list[str] = []
        all_drawings: list[dict[str, Any]] = []
        used_timeframes = [config.timeframe]
        symbols_total = max(len(config.symbols), 1)
        total_loaded_bars = 0
        single_symbol_curve: list[EquityPoint] = []

        for symbol_index, symbol in enumerate(config.symbols):
            # warmup_start extends the
            # loaded window backwards for indicator warmup; the engine gates
            # trading to start_date..end_date.
            warmup_raw = str(getattr(config, "warmup_start", "") or "").strip()
            load_start = warmup_raw or config.start_date
            data = _load_ohlcv(
                data_root,
                symbol,
                config.timeframe,
                load_start,
                config.end_date,
                data_source=config.data_source,
            )
            if data is None or data.empty:
                raise RuntimeError(
                    f"No OHLCV data for {symbol}/{config.timeframe} in "
                    f"{config.start_date}..{config.end_date} under {data_root}; "
                    "refusing to run a silent empty backtest"
                )
            source_path = str(data.attrs.get("ohlcv_source_path") or "")
            if source_path:
                all_data_paths.append(source_path)
            total_loaded_bars += len(data)

            symbol_start = 10.0 + (80.0 * symbol_index / symbols_total)
            symbol_end = 10.0 + (80.0 * (symbol_index + 1) / symbols_total)
            emit_progress(
                symbol_start,
                f"Data loaded: {len(data)} bars"
                if symbols_total == 1
                else f"Data loaded: {symbol} ({len(data)} bars)",
            )

            intrabar = _load_intrabar_data(
                data_root,
                symbol,
                config.timeframe,
                config.modeling_resolution,
                load_start,
                config.end_date,
                data_source=config.data_source,
            )
            if intrabar is not None and not intrabar.empty and config.modeling_resolution not in used_timeframes:
                used_timeframes.append(config.modeling_resolution)

            symbol_config = config.model_copy(update={"symbols": [symbol], "parameters": resolved_parameters})
            engine = TradeEngine(
                symbol_config,
                symbol=symbol,
                intrabar_data=intrabar,
                progress_callback=progress_callback,
                progress_start=symbol_start,
                progress_end=symbol_end,
            )
            symbol_result = engine.run(data, strategy_module.generate_signals)

            all_trades.extend(symbol_result.trades)
            if len(config.symbols) == 1:
                single_symbol_curve = list(symbol_result.equity_curve)
            # Normalize exactly as the vectorbt adapter does. The native engine
            # stores the strategy's drawings verbatim, so a strategy that puts a
            # pandas Timestamp in `time` (the obvious thing to write) produced a
            # payload that raised "Object of type Timestamp is not JSON
            # serializable" the moment the result was persisted.
            all_drawings.extend(
                _normalize_drawings(symbol_result.drawings, symbol=symbol)
            )

        all_trades.sort(key=lambda trade: (trade.entry_time, trade.id))
        for index, trade in enumerate(all_trades, start=1):
            trade.id = index

        # A-M4 (D5): multi-symbol runs replay all trades onto ONE running
        # portfolio equity (shared sizing basis, summed exposure) via the
        # same helper the vectorbt adapter uses. Per-symbol engines still
        # generate signals independently and size from full capital in pass
        # 1; the replay re-scales quantities chronologically from shared
        # equity (position size is linear in equity). Single-symbol runs keep
        # the legacy path bit-identical (no replay).
        # Merge resolution — the two sides described DIFFERENT curves and the
        # union keeps both, because each is load-bearing:
        #
        #   * Multi-symbol (main + branch agree): realized-PnL portfolio curve
        #     over EXIT time (Cleanup 16-D1, F5). Per-symbol engines each
        #     compound sizing from full capital; summing their per-bar equities
        #     would multiply capital by the symbol count. The aggregate replays
        #     closed PnL once per portfolio, credited at exit — documented in
        #     the cross-engine contract.
        #   * Single symbol (branch): the engine's per-bar mark-to-market
        #     curve, so open-position risk is represented instead of being
        #     invisible between exits. D2 of the money-path decision record
        #     designates the engine's `self.equity` (initial + realized +
        #     unrealized) as the mark-to-market REPORTING equity for the curve;
        #     a realized-only replay cannot express an intra-trade drawdown,
        #     which silently understates every curve-derived statistic (max
        #     drawdown, Sharpe, Calmar).
        #   * main's leading initial-capital point is kept in both cases, so
        #     the curve always spans the requested window even when the first
        #     data bar sits after start_date. When the engine's first bar lands
        #     on that same stamp the point is replaced, not duplicated (see
        #     the same-stamp collapse below).
        equity_curve: list[EquityPoint] = [
            EquityPoint(
                time=f"{config.start_date}T00:00:00+00:00"
                if len(config.start_date) == 10
                else config.start_date,
                equity=round(config.initial_capital, 2),
            )
        ]
        shared_summary: dict[str, Any] = {}
        if len(config.symbols) > 1 and all_trades:
            from runner.engines._shared import replay_shared_equity

            all_trades, replayed_curve, shared_summary = replay_shared_equity(
                all_trades,
                config.initial_capital,
                start_time=equity_curve[0].time,
            )
            all_trades.sort(key=lambda trade: (trade.entry_time, trade.id))
            for index, trade in enumerate(all_trades, start=1):
                trade.id = index
            equity_curve = replayed_curve
        elif single_symbol_curve:
            # Single symbol: the engine's per-bar mark-to-market curve, with
            # main's leading initial-capital point kept as the window anchor.
            # The engine stamps one point per traded bar, so the merge prepends
            # the anchor and collapses a same-stamp collision (first data bar
            # exactly at start_date) rather than emitting a duplicate point.
            for point in single_symbol_curve:
                if equity_curve[-1].time == point.time:
                    equity_curve[-1] = point
                else:
                    equity_curve.append(point)
        else:
            running = config.initial_capital
            for trade in sorted(all_trades, key=lambda item: item.last_exit_time):
                # net_pnl is authoritative when present; gross-minus-fees is
                # the fallback for engines that do not split the two. Crediting
                # net keeps the curve reconcilable with the trades' net PnL.
                running += (
                    float(trade.net_pnl)
                    if trade.net_pnl is not None
                    else float(trade.pnl) - float(trade.fees or 0.0)
                )
                point = EquityPoint(time=trade.last_exit_time, equity=round(running, 2))
                if equity_curve[-1].time == point.time:
                    equity_curve[-1] = point
                else:
                    equity_curve.append(point)
        emit_progress(90.0, "Calculating statistics...")

        now = datetime.now(timezone.utc)
        duration_days = 0
        try:
            duration_days = (date.fromisoformat(config.end_date) - date.fromisoformat(config.start_date)).days
        except Exception:
            duration_days = 0

        strategy_name = config.strategy_name
        if hasattr(strategy_module, "STRATEGY_META") and isinstance(strategy_module.STRATEGY_META, dict):
            strategy_name = strategy_module.STRATEGY_META.get("name", strategy_name)
        path_metadata = [contract.ohlcv_path_metadata(path) for path in all_data_paths]
        selected_sources = sorted({item["source"] for item in path_metadata if item["source"]})
        selected_asset_classes = sorted(
            {item["asset_class"] for item in path_metadata if item["asset_class"]}
        )

        meta = BacktestMeta(
            id=f"native-{config.strategy_name}-{now.strftime('%Y%m%d-%H%M%S')}",
            created_at=now.isoformat(),
            engine="native",
            strategies=[strategy_name],
            backtest_start=config.start_date,
            backtest_end=config.end_date,
            duration_days=duration_days,
            total_bars=total_loaded_bars,
            data={
                "timeframes": used_timeframes,
                "symbols": config.symbols,
                "universe": "",
                "source": ",".join(selected_sources) or "ohlcv",
                "asset_classes": selected_asset_classes,
                "data_files": all_data_paths,
                "requested_data_source": config.data_source,
                # A-M4 (D5): shared-equity replay summary for multi-symbol
                # runs (empty for single-symbol legacy path).
                "shared_equity": shared_summary,
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

        return BacktestResult(
            meta=meta,
            trades=all_trades,
            equity_curve=equity_curve,
            drawings=all_drawings,
        )
