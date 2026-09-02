"""Walk-forward validation runner.

Runs rolling train/test windows and aggregates out-of-sample metrics.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
) -> list[dict[str, str]]:
    if train_months <= 0 or test_months <= 0 or step_months <= 0:
        raise ValueError("train_months, test_months and step_months must all be > 0.")

    cursor = _parse_boundary(start)
    final_end = _parse_boundary(end, is_end=True)
    windows: list[dict[str, str]] = []
    while True:
        train_start = cursor
        train_end = train_start + pd.DateOffset(months=train_months) - pd.Timedelta(microseconds=1)
        test_start = train_end + pd.Timedelta(microseconds=1)
        test_end = test_start + pd.DateOffset(months=test_months) - pd.Timedelta(microseconds=1)
        if test_end > final_end:
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
    return dict(result.stats)


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
    return {
        "fold_count": len(folds),
        "avg_oos_sharpe": round(sum(oos_sharpes) / len(oos_sharpes), 6) if oos_sharpes else None,
        "worst_oos_drawdown": round(max(drawdowns), 6) if drawdowns else None,
        "n_profitable_folds": len(profitable),
        "profitable_fold_pct": round((len(profitable) / len(oos_sharpes)) * 100, 4) if oos_sharpes else 0.0,
    }


def _write_payload(payload: dict[str, Any], output_dir: str | Path | None = None) -> Path:
    target_dir = Path(output_dir) if output_dir else WALKFORWARD_OUTPUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    out_path = target_dir / f"wf_{now.strftime('%Y%m%d_%H%M%S')}.json"
    payload["output_path"] = str(out_path)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def _run_portfolio_walkforward(
    *,
    backtest_ids: list[str],
    train_months: int,
    test_months: int,
    step_months: int,
    n_trials: int,
    output_dir: str | Path | None,
) -> dict[str, Any]:
    result = optimize_portfolio_allocation(
        backtest_ids=backtest_ids,
        n_trials=n_trials,
        train_months=train_months,
        test_months=test_months,
    )
    fold = {
        "fold": 1,
        "is_sharpe": _metric(result, "sharpe", "sharpe_ratio"),
        "oos_sharpe": result.get("oos_sharpe"),
        "oos_max_drawdown": result.get("oos_max_drawdown"),
    }
    payload = {
        "mode": "portfolio",
        "backtest_ids": backtest_ids,
        "config": {
            "train_months": train_months,
            "test_months": test_months,
            "step_months": step_months,
            "n_trials": n_trials,
        },
        "folds": [fold],
        "summary": _summary_from_folds([fold]),
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
    step_months: int = 1,
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
    windows = _build_windows(
        start=start,
        end=end,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
    )

    folds: list[dict[str, Any]] = []
    for index, window in enumerate(windows, start=1):
        train_stats = _run_fold_backtest(
            engine=engine,
            strategy=strategy,
            strategy_name=strategy_name,
            symbols=symbols,
            timeframe=normalized_timeframe,
            start=window["train_start"],
            end=window["train_end"],
            capital=capital,
            risk_pct=risk_pct,
            max_position_exposure_pct=max_position_exposure_pct,
            max_leverage=max_leverage,
            modeling_resolution=modeling_resolution,
            data_dir=data_dir,
            parameters=parameters,
        )
        test_stats = _run_fold_backtest(
            engine=engine,
            strategy=strategy,
            strategy_name=strategy_name,
            symbols=symbols,
            timeframe=normalized_timeframe,
            start=window["test_start"],
            end=window["test_end"],
            capital=capital,
            risk_pct=risk_pct,
            max_position_exposure_pct=max_position_exposure_pct,
            max_leverage=max_leverage,
            modeling_resolution=modeling_resolution,
            data_dir=data_dir,
            parameters=parameters,
        )
        folds.append(
            {
                "fold": index,
                **window,
                "is_sharpe": _metric(train_stats, "sharpe_ratio", "sharpe"),
                "oos_sharpe": _metric(test_stats, "sharpe_ratio", "sharpe"),
                "oos_max_drawdown": _metric(test_stats, "max_drawdown_pct"),
                "is_stats": train_stats,
                "oos_stats": test_stats,
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
            "step_months": step_months,
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
