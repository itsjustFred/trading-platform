from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import optuna
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from runner.cli import _detect_date_range
from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter
from runner.stats import compute_stats_for_result
from shared.constants import DATA_DIR, OPTIMIZATIONS_DIR
from shared.models import BacktestResult
from shared.strategy_registry import find_strategy_entry

BACKTESTS_DIR = DATA_DIR / "backtests"
TARGET_METRIC_ALIASES = {
    "sharpe": "sharpe_ratio",
    "sharpe_ratio": "sharpe_ratio",
    "sortino": "sortino_ratio",
    "sortino_ratio": "sortino_ratio",
    "calmar": "calmar_ratio",
    "calmar_ratio": "calmar_ratio",
    "net_profit": "total_pnl",
    "total_pnl": "total_pnl",
    "cagr": "cagr",
    "win_rate": "win_rate",
    "profit_factor": "profit_factor",
    "psr": "psr",
    "p_value": "p_value",
    "total_return": "total_return",
}

ProgressCallback = Callable[[float, str], None]


class OptimizationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_backtest_id: str
    target_metric: str
    n_trials: int = Field(default=100, gt=0)
    train_split_pct: float = Field(default=0.7, gt=0.0, le=1.0)
    parameter_ranges: dict[str, dict[str, Any]] = Field(default_factory=dict, min_length=1)

    @field_validator("base_backtest_id", "target_metric")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("Value must not be empty")
        return cleaned

    @field_validator("target_metric")
    @classmethod
    def _normalize_target_metric(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("parameter_ranges")
    @classmethod
    def _validate_parameter_ranges(
        cls, value: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        if not value:
            raise ValueError("parameter_ranges must not be empty")

        normalized: dict[str, dict[str, Any]] = {}
        for name, spec in value.items():
            if not isinstance(spec, dict):
                raise ValueError(f"Parameter range for {name!r} must be an object")
            param_name = str(name or "").strip()
            if not param_name:
                raise ValueError("Parameter names must not be empty")
            normalized[param_name] = dict(spec)
            kind = str(spec.get("type", "")).strip().lower()
            if kind not in {"int", "float", "categorical"}:
                raise ValueError(
                    f"Parameter {param_name!r} has unsupported type {spec.get('type')!r}"
                )
            if kind in {"int", "float"}:
                if "min" not in spec or "max" not in spec:
                    raise ValueError(
                        f"Parameter {param_name!r} requires 'min' and 'max' boundaries"
                    )
            if kind == "categorical":
                choices = spec.get("choices")
                if not isinstance(choices, list) or not choices:
                    raise ValueError(
                        f"Parameter {param_name!r} requires a non-empty 'choices' list"
                    )
        return normalized


def run_optimization(
    config: OptimizationConfig,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    base_result = _load_base_backtest(config.base_backtest_id)
    if base_result.meta.engine != "vectorbt":
        raise ValueError(
            "Optimization currently supports vectorbt base backtests only, "
            f"got {base_result.meta.engine!r}"
        )

    symbols = list(base_result.meta.data.get("symbols") or [])
    if not symbols:
        raise ValueError("Base backtest metadata does not contain any symbols")

    timeframes = list(base_result.meta.data.get("timeframes") or [])
    timeframe = str(timeframes[0] if timeframes else "1h")
    backtest_start = str(base_result.meta.backtest_start or "").strip()
    backtest_end = str(base_result.meta.backtest_end or "").strip()
    if not backtest_start or not backtest_end:
        detected_start, detected_end = _detect_date_range(symbols, timeframe)
        backtest_start = backtest_start or detected_start
        backtest_end = backtest_end or detected_end

    train_period, test_period = _split_backtest_period(
        backtest_start,
        backtest_end,
        config.train_split_pct,
    )
    strategy_identifier, strategy_path, strategy_slug = _resolve_strategy_reference(base_result)

    base_parameters = dict(base_result.meta.parameters or {})
    initial_capital = float(base_result.meta.initial_capital or 100_000.0)

    _emit_progress(progress_callback, 0.0, "Starting Optuna optimization.")

    def objective(trial: optuna.Trial) -> float:
        trial_params = {
            param_name: _suggest_parameter(trial, param_name, spec)
            for param_name, spec in config.parameter_ranges.items()
        }
        combined_params = dict(base_parameters)
        combined_params.update(trial_params)

        train_result = _run_vectorbt_backtest(
            BacktestConfig(
                strategy_name=strategy_slug,
                symbols=symbols,
                timeframe=timeframe,
                start_date=train_period["start"],
                end_date=train_period["end"],
                initial_capital=initial_capital,
                parameters=combined_params,
            ),
            strategy_path,
        )
        train_stats = _canonical_stats(train_result)
        trial.set_user_attr("train_stats", _json_safe(train_stats))
        trial.set_user_attr("effective_parameters", _json_safe(combined_params))
        trial.set_user_attr("trade_count", len(train_result.trades))

        if not train_result.trades:
            trial.set_user_attr("penalty_applied", True)
            trial.set_user_attr("metric_name", _metric_key(config.target_metric, train_stats))
            trial.set_user_attr("metric_value", 0.0)
            return 0.0

        metric_value = _metric_value(train_stats, config.target_metric)
        trial.set_user_attr("penalty_applied", False)
        trial.set_user_attr("metric_name", _metric_key(config.target_metric, train_stats))
        trial.set_user_attr("metric_value", metric_value)
        return metric_value

    def on_trial_complete(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        completed_trials = len(study.trials)
        percent = completed_trials / max(config.n_trials, 1) * 100.0
        has_numeric_value = trial.value is not None and math.isfinite(float(trial.value))
        metric_display = f"{trial.value:.6f}" if has_numeric_value else "n/a"
        prefix = "✅" if trial.state == optuna.trial.TrialState.COMPLETE else "❌"
        message = (
            f"{prefix} Trial {completed_trials}/{config.n_trials} "
            f"({config.target_metric}={metric_display}) params={trial.params}"
        )
        _emit_progress(progress_callback, percent, message)

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=config.n_trials, callbacks=[on_trial_complete])

    best_trial = study.best_trial
    best_params = dict(study.best_params)
    effective_best_params = dict(base_parameters)
    effective_best_params.update(best_params)
    best_train_stats = dict(best_trial.user_attrs.get("train_stats") or {})

    test_result = _run_vectorbt_backtest(
        BacktestConfig(
            strategy_name=strategy_slug,
            symbols=symbols,
            timeframe=timeframe,
            start_date=test_period["start"],
            end_date=test_period["end"],
            initial_capital=initial_capital,
            parameters=effective_best_params,
        ),
        strategy_path,
    )
    test_stats = _canonical_stats(test_result)

    now = datetime.now(timezone.utc)
    optimization_id = f"opt_{config.base_backtest_id}_{now.strftime('%Y%m%d_%H%M%S')}"
    output_path = OPTIMIZATIONS_DIR / f"{optimization_id}.json"
    payload = {
        "id": optimization_id,
        "file": output_path.name,
        "created_at": now.isoformat(),
        "engine": "vectorbt",
        "base_backtest_id": config.base_backtest_id,
        "strategy": {
            "identifier": strategy_identifier,
            "slug": strategy_slug,
            "path": str(strategy_path),
        },
        "symbols": symbols,
        "timeframe": timeframe,
        "target_metric": config.target_metric,
        "n_trials": config.n_trials,
        "train_split_pct": config.train_split_pct,
        "parameter_ranges": _json_safe(config.parameter_ranges),
        "base_parameters": _json_safe(base_parameters),
        "best_params": _json_safe(best_params),
        "effective_best_parameters": _json_safe(effective_best_params),
        "best_value": _safe_float_or_none(study.best_value),
        "train_period": train_period,
        "test_period": test_period,
        "train_stats": _json_safe(best_train_stats),
        "test_stats": _json_safe(test_stats),
        "top_trials": _top_trials_payload(study.trials, limit=10),
    }

    OPTIMIZATIONS_DIR.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(_json_safe(payload), indent=2), encoding="utf-8")
    _emit_progress(progress_callback, 100.0, f"Optimization finished: {output_path.name}")
    return payload


def _load_base_backtest(backtest_id: str) -> BacktestResult:
    path = BACKTESTS_DIR / f"{backtest_id}.trades.json"
    if not path.exists():
        raise FileNotFoundError(f"Base backtest {backtest_id!r} not found at {path}")
    return BacktestResult.load(path)


def _resolve_strategy_reference(result: BacktestResult) -> tuple[str, Path, str]:
    strategy_identifier = str((result.meta.strategies or [""])[0]).strip()
    if not strategy_identifier:
        raise ValueError("Base backtest metadata does not contain a strategy identifier")

    entry = find_strategy_entry(strategy_identifier)
    if entry is None:
        raise FileNotFoundError(
            f"Could not resolve strategy file for base backtest strategy {strategy_identifier!r}"
        )
    return strategy_identifier, entry.file_path, entry.slug


def _split_backtest_period(
    start_value: str,
    end_value: str,
    train_split_pct: float,
) -> tuple[dict[str, str], dict[str, str]]:
    start_ts = _parse_period_boundary(start_value, is_end=False)
    end_ts = _parse_period_boundary(end_value, is_end=True)
    if end_ts <= start_ts:
        raise ValueError(
            f"Base backtest period is invalid: start={start_value!r}, end={end_value!r}"
        )

    split_ts = start_ts + ((end_ts - start_ts) * train_split_pct)
    if split_ts <= start_ts:
        split_ts = start_ts + pd.Timedelta(microseconds=1)
    if split_ts >= end_ts:
        split_ts = end_ts - pd.Timedelta(microseconds=1)
    if split_ts <= start_ts or split_ts >= end_ts:
        raise ValueError(
            "train_split_pct produces an empty train or test period for the base backtest range"
        )

    train_period = {"start": start_ts.isoformat(), "end": split_ts.isoformat()}
    test_period = {
        "start": (split_ts + pd.Timedelta(microseconds=1)).isoformat(),
        "end": end_ts.isoformat(),
    }
    return train_period, test_period


def _parse_period_boundary(value: str, *, is_end: bool) -> pd.Timestamp:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Backtest period values must not be empty")

    parsed = pd.to_datetime(raw, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"Invalid backtest period value: {value!r}")

    timestamp = pd.Timestamp(parsed)
    if is_end and "T" not in raw and ":" not in raw:
        return timestamp + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return timestamp


def _suggest_parameter(trial: optuna.Trial, name: str, spec: dict[str, Any]) -> Any:
    kind = str(spec.get("type", "")).strip().lower()
    if kind == "int":
        return trial.suggest_int(
            name,
            int(spec["min"]),
            int(spec["max"]),
            step=int(spec.get("step", 1)),
        )
    if kind == "float":
        kwargs: dict[str, Any] = {
            "low": float(spec["min"]),
            "high": float(spec["max"]),
        }
        if "step" in spec and spec.get("step") is not None:
            kwargs["step"] = float(spec["step"])
        if "log" in spec and spec.get("log") is not None:
            kwargs["log"] = bool(spec["log"])
        return trial.suggest_float(name, **kwargs)
    if kind == "categorical":
        return trial.suggest_categorical(name, list(spec["choices"]))
    raise ValueError(f"Unsupported parameter type for {name!r}: {spec.get('type')!r}")


def _run_vectorbt_backtest(config: BacktestConfig, strategy_path: Path) -> BacktestResult:
    result = VectorbtAdapter().run(config, str(strategy_path))
    result.stats = {**result.stats, **compute_stats_for_result(result)}
    return result


def _canonical_stats(result: BacktestResult) -> dict[str, Any]:
    return {**result.stats, **compute_stats_for_result(result)}


def _metric_key(target_metric: str, stats: dict[str, Any]) -> str:
    canonical = TARGET_METRIC_ALIASES.get(target_metric, target_metric)
    if canonical not in stats:
        raise ValueError(
            f"Target metric {target_metric!r} is not available. "
            f"Known metrics: {sorted(stats.keys())}"
        )
    return canonical


def _metric_value(stats: dict[str, Any], target_metric: str) -> float:
    metric_key = _metric_key(target_metric, stats)
    value = stats.get(metric_key)
    if value is None:
        raise ValueError(f"Target metric {target_metric!r} resolved to None")
    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        raise ValueError(f"Target metric {target_metric!r} is not finite: {value!r}")
    return numeric_value


def _top_trials_payload(
    trials: list[optuna.trial.FrozenTrial],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    completed_trials = [
        trial
        for trial in trials
        if trial.state == optuna.trial.TrialState.COMPLETE and trial.value is not None
    ]
    ordered_trials = sorted(completed_trials, key=lambda trial: float(trial.value), reverse=True)
    payload: list[dict[str, Any]] = []
    for trial in ordered_trials[:limit]:
        payload.append(
            {
                "number": trial.number,
                "state": trial.state.name,
                "value": _safe_float_or_none(trial.value),
                "params": _json_safe(dict(trial.params)),
                "effective_parameters": _json_safe(
                    dict(trial.user_attrs.get("effective_parameters") or {})
                ),
                "trade_count": int(trial.user_attrs.get("trade_count", 0) or 0),
                "penalty_applied": bool(trial.user_attrs.get("penalty_applied", False)),
                "train_stats": _json_safe(dict(trial.user_attrs.get("train_stats") or {})),
            }
        )
    return payload


def _emit_progress(
    callback: ProgressCallback | None,
    percent: float,
    message: str,
) -> None:
    if callback is None:
        return
    callback(round(max(0.0, min(100.0, percent)), 2), message)


def _safe_float_or_none(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return round(numeric, 6)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, (int, str, bool)) or value is None:
        return value
    item_method = getattr(value, "item", None)
    if callable(item_method):
        return _json_safe(item_method())
    return str(value)
