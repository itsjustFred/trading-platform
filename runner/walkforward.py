"""Walk-forward validation runner.

Runs rolling train/test windows and aggregates out-of-sample metrics.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from numbers import Real
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from runner.daily_equity import attach_daily_equity
from runner.engines.base import (
    BacktestConfig,
    EngineAdapter,
    resolve_legacy_max_position_kwarg,
    resolve_strategy_parameters,
)
from runner.portfolio_optimizer import optimize_portfolio_allocation
from runner.stats import merge_canonical_stats
from shared.atomic_io import write_text_atomically
from shared.constants import DATA_DIR, normalize_timeframe

WALKFORWARD_OUTPUT_DIR = DATA_DIR / "walkforward_results"


def _parse_boundary(value: str, *, is_end: bool = False) -> pd.Timestamp:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Walk-forward start/end must not be empty.")
    parsed = pd.to_datetime(raw, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"Invalid walk-forward boundary: {value!r}")
    timestamp = pd.Timestamp(parsed)
    if is_end and "T" not in raw and ":" not in raw:
        return timestamp + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return timestamp


def _date_text(value: pd.Timestamp) -> str:
    return value.isoformat()


def _float_or_none(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _metric(stats: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(stats.get(key))
        if value is not None:
            return value
    return None


def _build_windows(
    *,
    start: str,
    end: str,
    train_months: int,
    test_months: int,
    step_months: int,
    purge_days: int = 0,
    embargo_days: int = 0,
) -> list[dict[str, str]]:
    if train_months <= 0 or test_months <= 0 or step_months <= 0:
        raise ValueError("train_months, test_months and step_months must all be > 0.")
    if purge_days < 0 or embargo_days < 0:
        raise ValueError("purge_days and embargo_days must be >= 0.")

    cursor = _parse_boundary(start)
    final_end = _parse_boundary(end, is_end=True)
    if final_end <= cursor:
        raise ValueError("Walk-forward end must be after start.")
    windows: list[dict[str, str]] = []
    while True:
        train_start = cursor
        train_end = (
            train_start
            + pd.DateOffset(months=train_months)
            - pd.Timedelta(microseconds=1)
            - pd.Timedelta(days=purge_days)
        )
        test_start = (
            train_end
            + pd.Timedelta(microseconds=1)
            + pd.Timedelta(days=embargo_days)
        )
        test_end = test_start + pd.DateOffset(months=test_months) - pd.Timedelta(microseconds=1)
        if test_end > final_end or train_end <= train_start:
            break
        windows.append(
            {
                "train_start": _date_text(train_start),
                "train_end": _date_text(train_end),
                "test_start": _date_text(test_start),
                "test_end": _date_text(test_end),
            }
        )
        cursor = cursor + pd.DateOffset(months=step_months)
    if not windows:
        raise ValueError("No walk-forward folds fit inside the requested date range.")
    if step_months < test_months:
        raise ValueError("step_months must be >= test_months to avoid overlapping OOS folds.")
    return windows


def _run_fold_backtest(
    *,
    engine: EngineAdapter,
    strategy: str,
    strategy_name: str,
    symbols: list[str],
    timeframe: str,
    start: str,
    end: str,
    capital: float,
    risk_pct: float | None,
    max_position_exposure_pct: float | None,
    max_leverage: float | None,
    modeling_resolution: str,
    data_dir: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    config = BacktestConfig(
        strategy_name=strategy_name,
        symbols=symbols,
        timeframe=timeframe,
        start_date=start,
        end_date=end,
        initial_capital=capital,
        risk_per_trade_pct=risk_pct,
        max_position_exposure_pct=max_position_exposure_pct,
        max_leverage=max_leverage,
        modeling_resolution=modeling_resolution,
        data_dir=data_dir,
        parameters=parameters,
    )
    errors = engine.validate_config(config)
    if errors:
        raise ValueError("; ".join(errors))
    result = engine.run(config, strategy)
    if not result.equity_curve:
        attach_daily_equity(result, start=start, end=end)
    # Freshly computed canonical stats win over stale engine-provided stats.
    merge_canonical_stats(result)
    stats = dict(result.stats)
    stats["_equity_curve"] = [point.to_dict() for point in result.equity_curve]
    return stats


def _summary_from_folds(folds: list[dict[str, Any]]) -> dict[str, Any]:
    oos_sharpes = [
        value
        for value in (_float_or_none(fold.get("oos_sharpe")) for fold in folds)
        if value is not None
    ]
    drawdowns = [
        abs(value)
        for value in (_float_or_none(fold.get("oos_max_drawdown")) for fold in folds)
        if value is not None
    ]
    profitable = [value for value in oos_sharpes if value > 0]
    aggregate_sharpe, aggregate_drawdown = _aggregate_oos_metrics(folds)
    canonical_sharpe = (
        aggregate_sharpe
        if aggregate_sharpe is not None
        else (sum(oos_sharpes) / len(oos_sharpes) if oos_sharpes else None)
    )
    return {
        "fold_count": len(folds),
        "avg_oos_sharpe": (
            round(canonical_sharpe, 6) if canonical_sharpe is not None else None
        ),
        "mean_fold_oos_sharpe": (
            round(sum(oos_sharpes) / len(oos_sharpes), 6) if oos_sharpes else None
        ),
        "aggregate_oos_sharpe": (
            round(aggregate_sharpe, 6) if aggregate_sharpe is not None else None
        ),
        "aggregate_oos_max_drawdown": (
            round(aggregate_drawdown, 6) if aggregate_drawdown is not None else None
        ),
        "worst_oos_drawdown": round(max(drawdowns), 6) if drawdowns else None,
        "n_profitable_folds": len(profitable),
        "profitable_fold_pct": (
            round((len(profitable) / len(oos_sharpes)) * 100, 4) if oos_sharpes else 0.0
        ),
    }


def _parse_equity_timestamp(value: Any) -> pd.Timestamp | None:
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, Real):
            parsed = pd.to_datetime(value, unit="s", utc=True, errors="coerce")
        else:
            parsed = pd.to_datetime(value, utc=True, errors="coerce")
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed)


def _daily_close_series(curve: list[dict[str, Any]]) -> pd.Series:
    observations: list[tuple[pd.Timestamp, float]] = []
    for point in curve or []:
        try:
            timestamp = _parse_equity_timestamp(point["time"])
            equity = float(point["equity"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if timestamp is not None and math.isfinite(equity):
            observations.append((timestamp, equity))
    if not observations:
        return pd.Series(dtype=float)
    series = pd.Series(
        [equity for _, equity in observations],
        index=pd.DatetimeIndex([timestamp for timestamp, _ in observations]),
        dtype=float,
    ).sort_index(kind="stable")
    series = series[~series.index.duplicated(keep="last")]
    return series.resample("D").last().ffill().dropna()


def _aggregate_oos_metrics(folds: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    return_values: list[float] = []
    for fold in folds:
        curve = fold.get("oos_equity_curve")
        if not curve:
            continue
        daily_equity = _daily_close_series(curve)
        values = daily_equity.to_numpy(dtype=float)
        for start, end in zip(values, values[1:], strict=False):
            if start > 0 and math.isfinite(start) and math.isfinite(end):
                return_values.append(float(end / start - 1.0))
    if len(return_values) < 2:
        return None, None
    mean = sum(return_values) / len(return_values)
    variance = sum((value - mean) ** 2 for value in return_values) / len(return_values)
    std = math.sqrt(variance)
    sharpe = mean / std * math.sqrt(252.0) if std > 0 else 0.0
    running = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in return_values:
        running *= 1.0 + value
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, (peak - running) / peak)
    return sharpe, max_drawdown


def _write_payload(payload: dict[str, Any], output_dir: str | Path | None = None) -> Path:
    target_dir = Path(output_dir) if output_dir else WALKFORWARD_OUTPUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    out_path = target_dir / f"wf_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}.json"
    payload["output_path"] = str(out_path)
    write_text_atomically(out_path, json.dumps(payload, indent=2, ensure_ascii=False))
    return out_path


def _run_portfolio_walkforward(
    *,
    backtest_ids: list[str],
    train_months: int,
    test_months: int,
    step_months: int | None,
    n_trials: int,
    output_dir: str | Path | None,
    start: str = "",
    end: str = "",
    purge_days: int = 0,
    embargo_days: int = 0,
) -> dict[str, Any]:
    effective_step_months = test_months if step_months is None else step_months
    windows = None
    if (not start or not end) and (
        (step_months is not None and step_months != 1)
        or purge_days != 0
        or embargo_days != 0
    ):
        raise ValueError(
            "start and end are required when portfolio walk-forward shaping is configured"
        )
    if start and end:
        windows = _build_windows(
            start=start,
            end=end,
            train_months=train_months,
            test_months=test_months,
            step_months=effective_step_months,
            purge_days=purge_days,
            embargo_days=embargo_days,
        )
    if windows is None:
        windows = [None]

    folds: list[dict[str, Any]] = []
    for index, window in enumerate(windows, start=1):
        result = optimize_portfolio_allocation(
            backtest_ids=backtest_ids,
            n_trials=n_trials,
            train_months=train_months,
            test_months=test_months,
            start_date=window["train_start"] if window else None,
            end_date=window["test_end"] if window else None,
            train_start=window["train_start"] if window else None,
            train_end=window["train_end"] if window else None,
            test_start=window["test_start"] if window else None,
            test_end=window["test_end"] if window else None,
        )
        folds.append(
            {
                "fold": index,
                **(window or {}),
                "is_sharpe": _metric(result, "sharpe", "sharpe_ratio"),
                "oos_sharpe": result.get("oos_sharpe"),
                "oos_max_drawdown": result.get("oos_max_drawdown"),
                "oos_equity_curve": result.get("oos_equity_curve", []),
            }
        )

    payload = {
        "mode": "portfolio",
        "backtest_ids": backtest_ids,
        "config": {
            "start": start,
            "end": end,
            "train_months": train_months,
            "test_months": test_months,
            "step_months": effective_step_months,
            "purge_days": purge_days,
            "embargo_days": embargo_days,
            "n_trials": n_trials,
        },
        "folds": folds,
        "summary": _summary_from_folds(folds),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_payload(payload, output_dir)
    return payload


def run_walkforward(
    *,
    strategy: str = "",
    engine: EngineAdapter | None = None,
    engine_name: str = "vectorbt",
    symbols: list[str] | None = None,
    timeframe: str = "1d",
    start: str = "",
    end: str = "",
    capital: float = 100000.0,
    train_months: int = 12,
    test_months: int = 3,
    step_months: int | None = None,
    risk_pct: float | None = None,
    max_position_exposure_pct: float | None = None,
    max_leverage: float | None = None,
    modeling_resolution: str = "open_close",
    params: dict[str, Any] | None = None,
    data_dir: str = "",
    output_dir: str | Path | None = None,
    backtest_ids: list[str] | None = None,
    n_trials: int = 100,
    max_position_pct: float | None = None,
    purge_days: int = 0,
    embargo_days: int = 0,
) -> dict[str, Any]:
    """Run generic strategy walk-forward validation.

    ``backtest_ids`` keeps backward compatibility for portfolio-allocation walk-forward
    checks. New callers should pass ``strategy``, ``engine`` and market data settings.

    Sizing semantics (Cleanup 16-E): ``risk_pct`` is the maximum loss per trade;
    ``max_position_exposure_pct`` caps the notional of ONE position as a fraction
    of equity. ``max_position_pct`` is a deprecated alias of
    ``max_position_exposure_pct``.
    """
    if backtest_ids and not strategy:
        return _run_portfolio_walkforward(
            backtest_ids=backtest_ids,
            train_months=train_months,
            test_months=test_months,
            step_months=step_months,
            n_trials=n_trials,
            output_dir=output_dir,
            start=start,
            end=end,
            purge_days=purge_days,
            embargo_days=embargo_days,
        )

    if not strategy:
        raise ValueError("strategy is required for generic walk-forward validation.")
    if not symbols:
        raise ValueError("symbols are required for generic walk-forward validation.")
    if not start or not end:
        raise ValueError("start and end are required for generic walk-forward validation.")

    if engine is None:
        from runner.cli import get_engine

        engine = get_engine(engine_name)
    max_position_exposure_pct = resolve_legacy_max_position_kwarg(
        max_position_exposure_pct,
        max_position_pct,
        caller="run_walkforward",
    )
    normalized_timeframe = normalize_timeframe(timeframe)
    strategy_name = Path(strategy).stem
    parameters = resolve_strategy_parameters(strategy, params or {})
    effective_step_months = test_months if step_months is None else step_months
    windows = _build_windows(
        start=start,
        end=end,
        train_months=train_months,
        test_months=test_months,
        step_months=effective_step_months,
        purge_days=purge_days,
        embargo_days=embargo_days,
    )

    folds: list[dict[str, Any]] = []
    for index, window in enumerate(windows, start=1):
        # One shared kwargs block for both folds of a window: train and test
        # folds must always receive IDENTICAL sizing/context parameters — a
        # parameter added here must not silently reach only one of the two
        # calls (otherwise OOS folds run with different sizing than IS).
        fold_kwargs = {
            "engine": engine,
            "strategy": strategy,
            "strategy_name": strategy_name,
            "symbols": symbols,
            "timeframe": normalized_timeframe,
            "capital": capital,
            "risk_pct": risk_pct,
            "max_position_exposure_pct": max_position_exposure_pct,
            "max_leverage": max_leverage,
            "modeling_resolution": modeling_resolution,
            "data_dir": data_dir,
            "parameters": parameters,
        }
        train_stats = _run_fold_backtest(
            start=window["train_start"], end=window["train_end"], **fold_kwargs
        )
        test_stats = _run_fold_backtest(
            start=window["test_start"], end=window["test_end"], **fold_kwargs
        )
        oos_curve = test_stats.pop("_equity_curve", [])
        train_stats.pop("_equity_curve", None)
        folds.append(
            {
                "fold": index,
                **window,
                "is_sharpe": _metric(train_stats, "sharpe_ratio", "sharpe"),
                "oos_sharpe": _metric(test_stats, "sharpe_ratio", "sharpe"),
                "oos_max_drawdown": _metric(test_stats, "max_drawdown_pct"),
                "is_stats": train_stats,
                "oos_stats": test_stats,
                "oos_equity_curve": oos_curve,
            }
        )

    payload = {
        "mode": "strategy",
        "strategy": strategy,
        "engine": engine.name,
        "symbols": symbols,
        "timeframe": normalized_timeframe,
        "config": {
            "start": start,
            "end": end,
            "capital": capital,
            "train_months": train_months,
            "test_months": test_months,
            "step_months": effective_step_months,
            "purge_days": purge_days,
            "embargo_days": embargo_days,
            "risk_pct": risk_pct,
            "max_position_exposure_pct": max_position_exposure_pct,
            "max_leverage": max_leverage,
            "modeling_resolution": modeling_resolution,
            "parameters": parameters,
        },
        "folds": folds,
        "summary": _summary_from_folds(folds),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_payload(payload, output_dir)
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walk-Forward Strategy Validation")
    parser.add_argument("--strategy", default="")
    parser.add_argument("--backtest-ids", nargs="*", default=[])
    parser.add_argument("--symbols", default="")
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--train-months", type=int, default=12)
    parser.add_argument("--test-months", type=int, default=3)
    parser.add_argument("--step-months", type=int, default=1)
    parser.add_argument("--trials", type=int, default=100)
    args = parser.parse_args()

    result = run_walkforward(
        strategy=args.strategy,
        backtest_ids=args.backtest_ids,
        symbols=[item.strip() for item in args.symbols.split(",") if item.strip()],
        timeframe=args.timeframe,
        start=args.start,
        end=args.end,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        n_trials=args.trials,
    )
    print(json.dumps(result, indent=2))
