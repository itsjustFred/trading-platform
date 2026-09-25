from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, NoReturn

import optuna
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from data_tools.auto_download import ensure_backtest_data, has_ohlcv_bars_in_range
from runner.cli import _detect_date_range
from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter
from runner.risk_parity_optimizer import select_best_risk_parity_universe
from runner.stats import compute_stats_for_result
from shared.constants import DATA_DIR, OPTIMIZATIONS_DIR
from shared.database import load_backtest_result
from shared.models import BacktestResult
from shared.strategy_registry import find_strategy_entry

logger = logging.getLogger(__name__)

# Optuna param name for the internal symbol-universe selection. Namespaced so it
# can never collide with (and be filtered away alongside) a real strategy
# parameter that happens to be called "symbol_universe".
_INTERNAL_SYMBOL_UNIVERSE_PARAM = "__internal_symbol_universe"

# Upper bound on the number of symbol-subset universes we enumerate. Without a
# cap the candidate set grows as 2**n and both memory and the Optuna categorical
# search space explode for large symbol lists.
MAX_CANDIDATE_SYMBOL_UNIVERSES = 256

# No-trade trials must rank below every honest trial (including losing ones),
# otherwise Optuna is steered toward parameter regions that avoid trading.
# A finite, very negative floor keeps the study maximizable while making the
# trial strictly worse than any real metric value.
NO_TRADE_PENALTY = -1_000_000.0

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

#: Marker value recorded on no-trade trials. Purely informational (the trial
#: objective returns the dynamic penalty from :func:`_no_trade_penalty_value`);
#: kept for the ``metric_value`` user attr so downstream consumers see an
#: explicitly non-competitive value instead of a plausible-looking 0.0.
_NO_TRADE_PENALTY = -1e9


def _no_trade_penalty_value(completed_trading_values: list[float]) -> float:
    """Return a no-trade trial value that can never win the study.

    The penalty sits strictly below the worst value of the trading trials
    completed so far (with a safety margin). For the very first trial there
    is nothing to undercut yet, so a large finite negative constant is used —
    it is still strictly below any plausible metric value (Sharpe, Sortino,
    Calmar, profit factor and total return are all above −1e6 for real
    backtests), so an honest trial can never lose against it.
    """
    if not completed_trading_values:
        return _NO_TRADE_PENALTY
    return min(float(value) for value in completed_trading_values) - 1.0


def _best_trial_or_raise(study: optuna.Study) -> optuna.trial.FrozenTrial:
    """Return the best completed TRADING trial, raising when none exist.

    ``study.best_trial`` raises an opaque Optuna ValueError when every trial
    failed (e.g. bad parameter ranges) — surfacing that as a plain message
    keeps the failure distinguishable from "no valid parameter region".

    No-trade (penalized) trials are excluded from selection (Cleanup 16-D1,
    AC 2): a study whose completed trials are ALL no-trade trials must not
    hand back a no-trade parameter set as "best".
    """
    completed = study.get_trials(
        deepcopy=False, states=(optuna.trial.TrialState.COMPLETE,)
    )
    if not completed:
        raise RuntimeError(
            "Optimization produced no completed trials — every trial failed. "
            "Check parameter_ranges/target_metric before retrying."
        )
    trading_trials = [
        trial
        for trial in completed
        if not bool(trial.user_attrs.get("penalty_applied", False))
    ]
    if not trading_trials:
        raise RuntimeError(
            "Every completed optimization trial was a no-trade trial — no "
            "parameter region produced trades. Widen parameter_ranges or "
            "relax position-sizing constraints before retrying."
        )
    return max(trading_trials, key=lambda trial: float(trial.value or 0.0))


class OptimizationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_backtest_id: str
    target_metric: str
    n_trials: int = Field(default=100, gt=0)
    train_split_pct: float = Field(default=0.7, ge=0.1, le=0.95)
    parameter_ranges: dict[str, dict[str, Any]] = Field(default_factory=dict)
    optimization_mode: str = "parameter"
    symbol_universes: list[list[str]] = Field(default_factory=list)

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

    @field_validator("optimization_mode")
    @classmethod
    def _normalize_optimization_mode(cls, value: str) -> str:
        normalized = str(value or "parameter").strip().lower()
        if normalized not in {"parameter", "risk_parity_universe", "risk_parity_covariance"}:
            raise ValueError(
                "optimization_mode must be 'parameter', 'risk_parity_universe', "
                "or 'risk_parity_covariance'"
            )
        return normalized

    @field_validator("symbol_universes")
    @classmethod
    def _normalize_symbol_universes(cls, value: list[list[str]]) -> list[list[str]]:
        normalized: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()
        for raw_universe in value or []:
            if not isinstance(raw_universe, list):
                raise ValueError("symbol_universes must be a list of symbol lists")
            universe = []
            for raw_symbol in raw_universe:
                symbol = str(raw_symbol or "").strip().upper()
                if symbol and symbol not in universe:
                    universe.append(symbol)
            if len(universe) < 2:
                raise ValueError("Each symbol universe must contain at least two symbols")
            key = tuple(universe)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(universe)
        return normalized

    @field_validator("parameter_ranges")
    @classmethod
    def _validate_parameter_ranges(
        cls, value: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
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

    @model_validator(mode="after")
    def _validate_optimization_surface(self) -> "OptimizationConfig":
        if (
            not self.parameter_ranges
            and not self.symbol_universes
            and self.optimization_mode not in {"risk_parity_universe", "risk_parity_covariance"}
        ):
            raise ValueError(
                "Configure at least one parameter range or symbol universe for optimization"
            )
        return self


def _execution_context_from_result(result: BacktestResult) -> dict[str, Any]:
    raw = result.meta.data.get("execution_context")
    if not isinstance(raw, dict) or not raw:
        raise ValueError(
            "Base backtest has no persisted execution_context; rerun the base "
            "backtest before optimizing so data, risk, and modeling settings "
            "cannot be silently replaced with defaults."
        )
    context = dict(raw)
    risk = context.get("risk_config")
    if not isinstance(risk, dict):
        raise ValueError("Base backtest execution_context is missing risk_config")
    required = {
        "engine",
        "data_dir",
        "data_source",
        "symbols",
        "timeframe",
        "initial_capital",
    }
    missing = sorted(key for key in required if key not in context)
    if missing:
        raise ValueError(
            "Base backtest execution_context is missing: " + ", ".join(missing)
        )
    if str(context.get("engine")) != str(result.meta.engine):
        raise ValueError(
            "Base backtest execution_context engine does not match artifact engine"
        )
    context["risk_config"] = dict(risk)
    return context


def _context_fingerprint(context: dict[str, Any], strategy_path: Path) -> str:
    payload = {
        "context": context,
        "strategy_sha256": hashlib.sha256(strategy_path.read_bytes()).hexdigest(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _optimizer_backtest_config(
    *,
    strategy_slug: str,
    symbols: list[str],
    timeframe: str,
    start_date: str,
    end_date: str,
    initial_capital: float,
    parameters: dict[str, Any],
    context: dict[str, Any],
) -> BacktestConfig:
    risk = dict(context.get("risk_config") or {})
    return BacktestConfig(
        strategy_name=strategy_slug,
        symbols=list(symbols),
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        risk_per_trade_pct=risk.get("risk_per_trade_pct"),
        max_position_exposure_pct=risk.get("max_position_exposure_pct"),
        max_leverage=risk.get("max_leverage"),
        modeling_resolution=str(risk.get("modeling_resolution") or "open_close"),
        signal_exit_fill=str(risk.get("signal_exit_fill") or "close"),
        warmup_start=str(risk.get("warmup_start") or ""),
        data_dir=str(context.get("data_dir") or ""),
        data_source=str(context.get("data_source") or ""),
        parameters=dict(parameters),
    )


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
    execution_context = _execution_context_from_result(base_result)
    context_fingerprint = _context_fingerprint(execution_context, strategy_path)
    initial_capital = float(base_result.meta.initial_capital or 100_000.0)
    candidate_universes = _candidate_symbol_universes(
        configured_universes=config.symbol_universes,
        base_symbols=symbols,
        strategy_slug=strategy_slug,
        optimization_mode=config.optimization_mode,
    )

    if config.optimization_mode == "risk_parity_covariance":
        return _run_risk_parity_covariance_optimization(
            config=config,
            base_result=base_result,
            strategy_identifier=strategy_identifier,
            strategy_path=strategy_path,
            strategy_slug=strategy_slug,
            symbols=symbols,
            candidate_universes=candidate_universes,
            timeframe=timeframe,
            initial_capital=initial_capital,
            train_period=train_period,
            test_period=test_period,
            base_parameters=base_parameters,
            execution_context=execution_context,
            context_fingerprint=context_fingerprint,
            progress_callback=progress_callback,
        )

    required_symbols = list(
        dict.fromkeys(
            symbol
            for universe in [symbols, *candidate_universes]
            for symbol in universe
        )
    )
    data_dir = str(execution_context.get("data_dir") or "")
    ensure_backtest_data(
        symbols=required_symbols,
        timeframe=timeframe,
        start=backtest_start,
        end=backtest_end,
        data_dir=data_dir,
    )
    for segment_name, period in (("train", train_period), ("test", test_period)):
        for symbol in required_symbols:
            if not has_ohlcv_bars_in_range(
                symbol=symbol,
                timeframe=timeframe,
                start=period["start"],
                end=period["end"],
                data_dir=data_dir,
            ):
                raise ValueError(
                    f"Optimization {segment_name} segment has no OHLCV bars for "
                    f"{symbol} {timeframe} ({period['start']} -> {period['end']})"
                )

    _emit_progress(progress_callback, 0.0, "Starting Optuna optimization.")

    last_trial_exception: BaseException | None = None

    def _record_failure(trial: optuna.Trial, exc: BaseException) -> NoReturn:
        nonlocal last_trial_exception
        last_trial_exception = exc
        trial.set_user_attr("failure_reason", f"{type(exc).__name__}: {exc}")
        raise exc

    study = optuna.create_study(direction="maximize")

    def objective(trial: optuna.Trial) -> float:
        completed_trading_values = [
            float(t.value)
            for t in study.get_trials(deepcopy=False, states=(optuna.trial.TrialState.COMPLETE,))
            if t.value is not None and not bool(t.user_attrs.get("penalty_applied", False))
        ]
        trial_symbols = _trial_symbols(trial, symbols, candidate_universes)
        trial_params = {
            param_name: _suggest_parameter(trial, param_name, spec)
            for param_name, spec in config.parameter_ranges.items()
        }
        combined_params = dict(base_parameters)
        combined_params.update(trial_params)

        try:
            train_result = _run_vectorbt_backtest(
                _optimizer_backtest_config(
                    strategy_slug=strategy_slug,
                    symbols=trial_symbols,
                    timeframe=timeframe,
                    start_date=train_period["start"],
                    end_date=train_period["end"],
                    initial_capital=initial_capital,
                    parameters=combined_params,
                    context=execution_context,
                ),
                strategy_path,
            )
        except (ValueError, RuntimeError, KeyError) as exc:
            return _record_failure(trial, exc)
        train_stats = _canonical_stats(train_result)
        trial.set_user_attr("train_stats", _json_safe(train_stats))
        trial.set_user_attr("effective_parameters", _json_safe(combined_params))
        trial.set_user_attr("symbols", _json_safe(trial_symbols))
        trial.set_user_attr("execution_context_fingerprint", context_fingerprint)
        trial.set_user_attr("trade_count", len(train_result.trades))

        if not train_result.trades:
            # No-trade trials (Cleanup 16-D1, AC 2): a trial that never trades
            # must never win the study. Under direction="maximize" the previous
            # penalty value 0.0 BEAT every honest trial with negative Sharpe,
            # steering the search toward parameter regions that avoid trading.
            # The explicit penalty is strictly below the value of the worst
            # trading trial observed so far, and the marker stays on the trial
            # for downstream validation.
            trial.set_user_attr("penalty_applied", True)
            trial.set_user_attr("metric_name", _metric_key(config.target_metric, train_stats))
            penalty = _no_trade_penalty_value(completed_trading_values)
            trial.set_user_attr("metric_value", float(penalty))
            return penalty

        metric_value = _metric_value(train_stats, config.target_metric)
        # The NO_TRADE_PENALTY floor only steers Optuna correctly while every
        # honest metric value ranks above it. A metric that reaches the floor
        # (e.g. a raw-PnL target on a large account) would silently invert the
        # no-trade steering, so record the failure loudly instead.
        if metric_value <= NO_TRADE_PENALTY:
            _record_failure(
                trial,
                ValueError(
                    f"{config.target_metric}={metric_value} is at or below the "
                    f"no-trade penalty floor ({NO_TRADE_PENALTY}); the floor only "
                    "holds for metrics bounded above it. Choose a different "
                    "target_metric or raise NO_TRADE_PENALTY's magnitude."
                ),
            )
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

    study.optimize(
        objective,
        n_trials=config.n_trials,
        callbacks=[on_trial_complete],
        catch=(ValueError, RuntimeError, KeyError),
    )

    completed_trials = study.get_trials(
        deepcopy=False,
        states=(optuna.trial.TrialState.COMPLETE,),
    )
    if not completed_trials:
        failure_summary = "; ".join(
            f"trial {trial.number}: {trial.state.name}"
            + (f" ({trial.user_attrs.get('failure_reason')})" if trial.user_attrs.get("failure_reason") else "")
            for trial in study.get_trials(deepcopy=False)[-5:]
        )
        raise RuntimeError(
            f"No optimization trial completed successfully "
            f"({config.n_trials} trials attempted); refusing to report a best "
            f"parameter set from failed backtests. Last trials: {failure_summary}"
        ) from last_trial_exception

    best_trial = _best_trial_or_raise(study)
    best_params = {
        key: value
        for key, value in dict(best_trial.params).items()
        if key != _INTERNAL_SYMBOL_UNIVERSE_PARAM
    }
    best_symbols = list(best_trial.user_attrs.get("symbols") or symbols)
    effective_best_params = dict(base_parameters)
    effective_best_params.update(best_params)
    best_train_stats = dict(best_trial.user_attrs.get("train_stats") or {})
    best_value = best_trial.value

    test_result = _run_vectorbt_backtest(
        _optimizer_backtest_config(
            strategy_slug=strategy_slug,
            symbols=best_symbols,
            timeframe=timeframe,
            start_date=test_period["start"],
            end_date=test_period["end"],
            initial_capital=initial_capital,
            parameters=effective_best_params,
            context=execution_context,
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
        "best_symbols": best_symbols,
        "timeframe": timeframe,
        "target_metric": config.target_metric,
        "n_trials": config.n_trials,
        "train_split_pct": config.train_split_pct,
        "optimization_mode": config.optimization_mode,
        "parameter_ranges": _json_safe(config.parameter_ranges),
        "symbol_universes": _json_safe(candidate_universes),
        "base_parameters": _json_safe(base_parameters),
        "execution_context": _json_safe(execution_context),
        "execution_context_fingerprint": context_fingerprint,
        "data_files": _json_safe(base_result.meta.data.get("data_files") or []),
        "best_params": _json_safe(best_params),
        "effective_best_parameters": _json_safe(effective_best_params),
        "best_value": _safe_float_or_none(best_value),
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
    if path.exists():
        return BacktestResult.load(path)
    stored = load_backtest_result(str(backtest_id))
    if stored is not None:
        return stored
    raise FileNotFoundError(f"Base backtest {backtest_id!r} not found at {path}")


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

    test_start_ts = split_ts + pd.Timedelta(microseconds=1)
    if split_ts <= start_ts or test_start_ts >= end_ts:
        raise ValueError(
            "train_split_pct produces an empty train or test period for the base backtest range"
        )

    train_period = {"start": start_ts.isoformat(), "end": split_ts.isoformat()}
    test_period = {
        "start": test_start_ts.isoformat(),
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
    ensure_backtest_data(
        symbols=config.symbols,
        timeframe=config.timeframe,
        start=config.start_date,
        end=config.end_date,
        data_dir=config.data_dir,
        modeling_resolution=config.modeling_resolution,
        log_callback=lambda message: None,
    )
    result = VectorbtAdapter().run(config, str(strategy_path))
    result.stats = {**result.stats, **compute_stats_for_result(result)}
    return result


def _candidate_symbol_universes(
    *,
    configured_universes: list[list[str]],
    base_symbols: list[str],
    strategy_slug: str,
    optimization_mode: str,
) -> list[list[str]]:
    if configured_universes:
        return [list(universe) for universe in configured_universes]
    if (
        optimization_mode not in {"risk_parity_universe", "risk_parity_covariance"}
        and strategy_slug != "risk_parity"
    ):
        return []

    symbols = []
    for raw_symbol in base_symbols:
        symbol = str(raw_symbol or "").strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    if len(symbols) < 2:
        return []

    universes: list[list[str]] = []
    for size in range(2, len(symbols) + 1):
        for combo in combinations(symbols, size):
            universes.append(list(combo))
            if len(universes) >= MAX_CANDIDATE_SYMBOL_UNIVERSES:
                logger.warning(
                    "Symbol-universe combinations capped at %d (from %d base "
                    "symbols); not all subsets will be explored.",
                    MAX_CANDIDATE_SYMBOL_UNIVERSES,
                    len(symbols),
                )
                return universes
    return universes


def _trial_symbols(
    trial: optuna.Trial,
    base_symbols: list[str],
    candidate_universes: list[list[str]],
) -> list[str]:
    if not candidate_universes:
        return list(base_symbols)
    universe_index = trial.suggest_categorical(
        _INTERNAL_SYMBOL_UNIVERSE_PARAM, list(range(len(candidate_universes)))
    )
    return list(candidate_universes[int(universe_index)])


def _run_risk_parity_covariance_optimization(
    *,
    config: OptimizationConfig,
    base_result: BacktestResult,
    strategy_identifier: str,
    strategy_path: Path,
    strategy_slug: str,
    symbols: list[str],
    candidate_universes: list[list[str]],
    timeframe: str,
    initial_capital: float,
    train_period: dict[str, str],
    test_period: dict[str, str],
    base_parameters: dict[str, Any],
    execution_context: dict[str, Any],
    context_fingerprint: str,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    _emit_progress(progress_callback, 5.0, "Loading asset return history for risk parity.")
    # Covariance input precedence (Cleanup 16-D1, F30): risk-parity weights
    # express ASSET co-movement, so the returns frame must come from market
    # data (asset prices). The trade-PnL pseudo-equity frame measures
    # strategy-trade overlap, not asset correlation, and is only a
    # last-resort fallback — the previous order of preference let two
    # traded symbols permanently block the market-data path.
    returns_frame = _asset_returns_frame_from_market_data(
        symbols=symbols,
        timeframe=timeframe,
        start=train_period["start"],
        end=train_period["end"],
        data_dir=str(execution_context.get("data_dir") or ""),
    )
    if returns_frame.empty or len(returns_frame.columns) < 2:
        returns_frame = _asset_returns_frame_from_base_result(base_result)
    if returns_frame.empty or len(returns_frame.columns) < 2:
        raise ValueError(
            "Risk parity covariance optimization requires at least two asset return series"
        )

    _emit_progress(progress_callback, 35.0, "Solving covariance risk parity weights.")
    selection = select_best_risk_parity_universe(
        returns_frame,
        candidate_universes or None,
    )
    best_symbols = list(selection["best_symbols"])
    effective_best_params = dict(base_parameters)
    effective_best_params["asset_weights"] = dict(selection["best_weights"])
    effective_best_params["risk_parity_weights"] = dict(selection["best_weights"])

    _emit_progress(
        progress_callback, 70.0, "Running OOS backtest for selected risk parity universe."
    )
    test_result = _run_vectorbt_backtest(
        _optimizer_backtest_config(
            strategy_slug=strategy_slug,
            symbols=best_symbols,
            timeframe=timeframe,
            start_date=test_period["start"],
            end_date=test_period["end"],
            initial_capital=initial_capital,
            parameters=effective_best_params,
            context=execution_context,
        ),
        strategy_path,
    )
    test_stats = _canonical_stats(test_result)

    now = datetime.now(timezone.utc)
    optimization_id = f"rp_cov_{config.base_backtest_id}_{now.strftime('%Y%m%d_%H%M%S')}"
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
        "best_symbols": best_symbols,
        "timeframe": timeframe,
        "target_metric": "risk_parity_score",
        "n_trials": 0,
        "train_split_pct": config.train_split_pct,
        "optimization_mode": config.optimization_mode,
        "parameter_ranges": _json_safe(config.parameter_ranges),
        "symbol_universes": _json_safe(candidate_universes),
        "base_parameters": _json_safe(base_parameters),
        "execution_context": _json_safe(execution_context),
        "execution_context_fingerprint": context_fingerprint,
        "data_files": _json_safe(base_result.meta.data.get("data_files") or []),
        "best_params": {},
        "effective_best_parameters": _json_safe(effective_best_params),
        "best_value": _safe_float_or_none(selection["best_score"]),
        "train_period": train_period,
        "test_period": test_period,
        "train_stats": {
            "risk_parity_score": selection["best_score"],
            "risk_parity_sharpe": selection["best_sharpe"],
            "risk_parity_volatility": selection["best_volatility"],
            "diversification_ratio": selection["best_diversification_ratio"],
            "avg_abs_correlation": selection["best_avg_abs_correlation"],
        },
        "test_stats": _json_safe(test_stats),
        "risk_parity": _json_safe(selection),
        "top_trials": [],
    }

    OPTIMIZATIONS_DIR.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(_json_safe(payload), indent=2), encoding="utf-8")
    _emit_progress(
        progress_callback, 100.0, f"Risk parity optimization finished: {output_path.name}"
    )
    return payload


def _asset_returns_frame_from_base_result(result: BacktestResult) -> pd.DataFrame:
    by_symbol: dict[str, list[tuple[pd.Timestamp, float]]] = {}
    for trade in result.trades:
        symbol = str(trade.symbol or "").strip().upper()
        if not symbol:
            continue
        timestamp = pd.to_datetime(trade.entry_time, utc=True, errors="coerce")
        if pd.isna(timestamp):
            continue
        by_symbol.setdefault(symbol, []).append((pd.Timestamp(timestamp), float(trade.pnl or 0.0)))
    series: dict[str, pd.Series] = {}
    for symbol, rows in by_symbol.items():
        if len(rows) < 2:
            continue
        frame = pd.DataFrame(rows, columns=["time", "pnl"]).sort_values("time")
        equity = float(result.meta.initial_capital or 100_000.0) + frame["pnl"].cumsum()
        returns = equity.pct_change().fillna(0.0)
        series[symbol] = pd.Series(returns.to_numpy(), index=frame["time"])
    if len(series) < 2:
        return pd.DataFrame()
    return pd.DataFrame(series).sort_index().fillna(0.0)


def _asset_returns_frame_from_market_data(
    *,
    symbols: list[str],
    timeframe: str,
    start: str,
    end: str,
    data_dir: str,
) -> pd.DataFrame:
    ensure_backtest_data(
        symbols=symbols,
        timeframe=timeframe,
        start=start,
        end=end,
        data_dir=data_dir,
        modeling_resolution="open_close",
        log_callback=lambda message: None,
    )
    root = Path(data_dir) / "ohlcv" if data_dir else DATA_DIR / "ohlcv"
    series: dict[str, pd.Series] = {}
    start_ts = pd.to_datetime(start, utc=True, errors="coerce")
    end_ts = pd.to_datetime(end, utc=True, errors="coerce")
    for symbol in symbols:
        path = next(root.rglob(f"**/{symbol}/{timeframe}.parquet"), None)
        if path is None:
            continue
        frame = pd.read_parquet(path)
        if "timestamp" not in frame.columns or "close" not in frame.columns:
            continue
        timestamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        close = pd.to_numeric(frame["close"], errors="coerce")
        asset = pd.DataFrame({"time": timestamps, "close": close}).dropna()
        if not pd.isna(start_ts):
            asset = asset[asset["time"] >= start_ts]
        if not pd.isna(end_ts):
            asset = asset[asset["time"] <= end_ts]
        if len(asset) < 3:
            continue
        asset = asset.sort_values("time").drop_duplicates("time", keep="last")
        series[str(symbol).upper()] = asset.set_index("time")["close"].pct_change().fillna(0.0)
    if len(series) < 2:
        return pd.DataFrame()
    return pd.DataFrame(series).sort_index().fillna(0.0)


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
                "symbols": _json_safe(list(trial.user_attrs.get("symbols") or [])),
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
