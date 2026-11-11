from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, NoReturn
from uuid import uuid4

import optuna
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from data_tools.auto_download import ensure_backtest_data, has_sufficient_segment_coverage
from runner.cli import _detect_date_range
from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter
from runner.optimization_diagnostics import (
    ThreeWaySplit,
    three_way_split,
    walk_forward_train_validation_folds,
)
from runner.risk_parity_optimizer import select_best_risk_parity_universe
from runner.stats import compute_stats_for_result, daily_returns_moments
from runner.universe_provenance import (
    UNIVERSE_PROVENANCE_KEY,
    observe_symbol_coverage,
    read_universe_provenance,
    survivorship_bias_diagnostic,
    unknown_universe_provenance,
)
from shared.atomic_io import write_text_atomically
from shared.constants import DATA_DIR, OPTIMIZATIONS_DIR, normalize_timeframe
from shared.models import BacktestResult
from shared.strategy_registry import find_strategy_entry

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str], None]

# Optuna param name for the internal symbol-universe selection. Namespaced so it
# can never collide with (and be filtered away alongside) a real strategy
# parameter that happens to be called "symbol_universe".
_INTERNAL_SYMBOL_UNIVERSE_PARAM = "__internal_symbol_universe"

#: Prefix that marks an Optuna param as optimizer bookkeeping rather than a
#: strategy parameter. Params under it are stripped before the trial rows reach
#: the plateau / multiple-testing statistics, so an internal param can never be
#: picked as the axis a "plateau" is reported along, nor counted as a distinct
#: strategy configuration.
_INTERNAL_PARAM_PREFIX = "__internal_"

# Upper bound on the number of symbol-subset universes we enumerate. Without a
# cap the candidate set grows as 2**n and both memory and the Optuna categorical
# search space explode for large symbol lists.
MAX_CANDIDATE_SYMBOL_UNIVERSES = 256

# No-trade trials must rank below every honest trial (including losing ones),
# otherwise Optuna is steered toward parameter regions that avoid trading.
# A finite, very negative floor keeps maximize-studies steerable while making
# the trial strictly worse than any real metric value; minimize-studies
# (p_value) use the mirrored ceiling NO_TRADE_PENALTY_MINIMIZE.
NO_TRADE_PENALTY = -1_000_000.0
NO_TRADE_PENALTY_MINIMIZE = 1_000_000.0

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

#: Per-metric study direction. Everything ranks higher-is-better EXCEPT
#: ``p_value`` (a statistical significance p-value: smaller = more
#: significant). Maximizing it would select the most INSIGNIFICANT trials
#: (#82); the frontend already treats it as lower-is-favorable.
METRIC_DIRECTIONS: dict[str, str] = {
    metric: ("minimize" if metric == "p_value" else "maximize")
    for metric in set(TARGET_METRIC_ALIASES.values())
}

#: Raw (non-risk-adjusted) maximize metrics (C3). Targeting these without
#: trade-count/drawdown penalties systematically selects overfit lucky runs,
#: so the objective haircuts them by default (opt-out via config).
RAW_MAXIMIZE_METRICS = frozenset(
    {"total_pnl", "total_return", "profit_factor", "win_rate"}
)


def _risk_adjusted_metric_value(
    value: float,
    stats: dict[str, Any],
    *,
    metric_key: str,
    direction: str,
    min_trades_for_full_credit: int = 30,
    drawdown_penalty_weight: float = 1.0,
) -> tuple[float, dict[str, float]]:
    """Apply mandatory risk penalties to raw maximize targets (C3).

    Returns (adjusted_value, terms) with terms for transparency. Sparsity
    haircut scales credit by trades/``min_trades`` below the threshold; the
    drawdown haircut scales by ``1 - |max_drawdown_pct| * weight`` (floored
    at 0). Both haircuts multiply non-negative values, so the score keeps
    the metric's unit; LOSING values pass through raw (a multiplicative
    haircut would compress them toward zero and let losers outrank small
    winners). Non-raw metrics (sharpe/sortino/calmar/psr/cagr) and minimize
    studies pass through untouched.
    """
    terms = {"sparsity_factor": 1.0, "drawdown_factor": 1.0}
    if direction != "maximize" or metric_key not in RAW_MAXIMIZE_METRICS:
        return value, terms
    if value < 0:
        return value, terms
    try:
        trade_count = int(stats.get("total_trades", 0) or 0)
    except (TypeError, ValueError):
        trade_count = 0
    try:
        max_dd = abs(float(stats.get("max_drawdown_pct", 0.0) or 0.0))
    except (TypeError, ValueError):
        max_dd = 0.0
    if not math.isfinite(max_dd):
        max_dd = 0.0
    # Pre-mortem T3: stats publish drawdown as a FRACTION (e.g. 0.05, not
    # 5.0). Percent-scale input would silently zero every trial via the
    # min(max_dd, 1.0) clamp — fail loud instead.
    if max_dd > 1.0:
        raise ValueError(
            f"max_drawdown_pct {max_dd} looks percent-scaled; expected a fraction <= 1.0."
        )
    sparsity = max(
        0.0,
        min(1.0, trade_count / max(int(min_trades_for_full_credit), 1)),
    )
    drawdown_factor = max(0.0, 1.0 - min(max_dd, 1.0) * max(drawdown_penalty_weight, 0.0))
    terms = {"sparsity_factor": sparsity, "drawdown_factor": drawdown_factor}
    return value * sparsity * drawdown_factor, terms

#: Marker value recorded on no-trade trials. Purely informational (the trial
#: objective returns the dynamic penalty from :func:`_no_trade_penalty_value`);
#: kept for the ``metric_value`` user attr so downstream consumers see an
#: explicitly non-competitive value instead of a plausible-looking 0.0.
_NO_TRADE_PENALTY = -1e9

#: For minimize-direction metrics the penalty must sit strictly ABOVE every
#: plausible metric value, not below (p-values are in [0, 1]).
_NO_TRADE_PENALTY_MAXIMIZE_METRICS = 1e9


def _study_direction(target_metric: str) -> str:
    """Return the Optuna study direction for ``target_metric`` (#82).

    ``p_value`` is minimized (smaller = more significant); every other
    registered target metric is maximized. Unknown metrics fail loudly
    instead of silently defaulting to maximize.
    """
    canonical = TARGET_METRIC_ALIASES.get(target_metric, target_metric)
    direction = METRIC_DIRECTIONS.get(canonical)
    if direction is None:
        raise ValueError(
            f"Unknown target metric {target_metric!r}: no study direction is "
            "defined. Extend TARGET_METRIC_ALIASES/METRIC_DIRECTIONS explicitly."
        )
    return direction


def _no_trade_penalty_value(
    completed_trading_values: list[float],
    *,
    direction: str = "maximize",
) -> float:
    """Return a no-trade trial value that can never win the study.

    The penalty sits strictly beyond the worst value of the trading trials
    completed so far (with a safety margin). For the very first trial there
    is nothing to undercut yet, so a large finite constant is used — it is
    still strictly beyond any plausible metric value (Sharpe, Sortino,
    Calmar, profit factor and total return are all above −1e6 for real
    backtests; p-values are in [0, 1]), so an honest trial can never lose
    against it.
    """
    if direction == "minimize":
        if not completed_trading_values:
            return _NO_TRADE_PENALTY_MAXIMIZE_METRICS
        return max(float(value) for value in completed_trading_values) + 1.0
    if not completed_trading_values:
        return _NO_TRADE_PENALTY
    return min(float(value) for value in completed_trading_values) - 1.0


def _best_trial_or_raise(
    study: optuna.Study,
    *,
    direction: str = "maximize",
) -> optuna.trial.FrozenTrial:
    """Return the best completed TRADING trial, raising when none exist.

    ``study.best_trial`` raises an opaque Optuna ValueError when every trial
    failed (e.g. bad parameter ranges) — surfacing that as a plain message
    keeps the failure distinguishable from "no valid parameter region".

    No-trade (penalized) trials are excluded from selection (Cleanup 16-D1,
    AC 2): a study whose completed trials are ALL no-trade trials must not
    hand back a no-trade parameter set as "best".

    ``direction`` must match the study's direction (#82): minimize-studies
    (p_value) rank lower-is-better, so the best trading trial is the MINIMUM.
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
    if direction == "minimize":
        return min(trading_trials, key=lambda trial: float(trial.value or 0.0))
    return max(trading_trials, key=lambda trial: float(trial.value or 0.0))


class OptimizationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_backtest_id: str
    target_metric: str
    n_trials: int = Field(default=100, gt=0)
    # Three-way split: train / validation / test. Defaults give 60/20/20.
    # If validation_split_pct and test_split_pct are not provided, the remainder
    # 1 - train_split_pct is split equally between validation and test.
    # The bounds on train_split_pct are unchanged; the three fractions together
    # are validated in _validate_three_way_split.
    train_split_pct: float = Field(default=0.6, ge=0.1, le=0.95)
    validation_split_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    test_split_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    parameter_ranges: dict[str, dict[str, Any]] = Field(default_factory=dict)
    optimization_mode: str = "parameter"
    symbol_universes: list[list[str]] = Field(default_factory=list)
    # C3: risk-adjusted objective for raw targets (default on) + embargo
    # gap between train and test (default 1 day, no shared-boundary bars).
    risk_adjust_objective: bool = True
    min_trades_for_full_credit: int = Field(default=30, ge=1)
    drawdown_penalty_weight: float = Field(default=1.0, ge=0.0)
    embargo_days: int = Field(default=1, ge=0)
    # Fraction of the window dropped between train and test, on top of
    # `embargo_days`, so a trade open across the split cannot leak test
    # information into training. Default 0 keeps `embargo_days` authoritative.
    embargo_pct: float = Field(default=0.0, ge=0.0, le=0.4)
    # Number of sequential walk-forward folds reported as a robustness check.
    walk_forward_splits: int = Field(default=3, ge=1, le=20)
    # Share of trials, ranked by validation score, that get evaluated on test.
    # Default 0.10 means the top 10% of trials by validation metric.
    test_top_fraction: float = Field(default=0.10, gt=0.0, le=1.0)
    # Overfitting thresholds. A failed check flags the result; it does not
    # block the run (blocking happens at the promotion gate, #165).
    # max_train_validation_degradation: validation metric may be at most this
    # fraction worse than train (relative drop). Default 0.5 = 50% degradation.
    max_train_validation_degradation: float = Field(default=0.5, ge=0.0)
    # min_deflated_sharpe: probability that the selected trial's Sharpe is not
    # due to selection bias (Bailey & Lopez de Prado 2014). Default 0.95.
    min_deflated_sharpe: float = Field(default=0.95, gt=0.0, le=1.0)

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

    @model_validator(mode="after")
    def _validate_three_way_split(self) -> "OptimizationConfig":
        """Validate train/validation/test split fractions.

        If validation_split_pct and test_split_pct are not provided, the remainder
        1 - train_split_pct is split equally between validation and test.
        All three fractions must be > 0 and sum to 1 (tolerance 1e-9).
        """
        train = self.train_split_pct
        validation = self.validation_split_pct
        test = self.test_split_pct

        if validation is None and test is None:
            # Old config: split remainder equally between validation and test
            remainder = 1.0 - train
            if remainder <= 0:
                raise ValueError(
                    "train_split_pct must be < 1.0 to leave room for validation and test"
                )
            validation = remainder / 2.0
            test = remainder / 2.0
            # Store the derived values back for downstream use
            object.__setattr__(self, "validation_split_pct", validation)
            object.__setattr__(self, "test_split_pct", test)
        elif validation is None or test is None:
            raise ValueError(
                "Both validation_split_pct and test_split_pct must be provided together, "
                "or neither (to use the equal-split default)"
            )

        if not (validation > 0 and test > 0 and train > 0):
            raise ValueError(
                "train_split_pct, validation_split_pct, and test_split_pct must all be > 0"
            )

        total = train + validation + test
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"train_split_pct + validation_split_pct + test_split_pct must sum to 1.0, "
                f"got {total:.10f}"
            )
        return self


def _coverage_session_from_result(result: BacktestResult) -> Mapping[str, object] | None:
    data = result.meta.data if isinstance(result.meta.data, dict) else {}
    for key in ("session", "trading_session"):
        value = data.get(key)
        if isinstance(value, Mapping):
            return value
    start = data.get("session_start")
    end = data.get("session_end")
    if start is not None and end is not None:
        return {"start": start, "end": end}
    return None


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
    base_runtime = _runtime_config_from_base_result(base_result)
    coverage_session = _coverage_session_from_result(base_result)

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

    split = _split_backtest_period(
        backtest_start,
        backtest_end,
        train_split_pct=config.train_split_pct,
        validation_split_pct=config.validation_split_pct,
        test_split_pct=config.test_split_pct,
        embargo_days=config.embargo_days,
        embargo_pct=float(config.embargo_pct or 0.0),
    )
    train_period = split.train
    validation_period = split.validation
    test_period = split.test
    strategy_identifier, strategy_path, strategy_slug = _resolve_strategy_reference(base_result)

    base_parameters = dict(base_result.meta.parameters or {})
    # Universe provenance: how this run's symbol list was determined, and when.
    # A base artifact that predates the recording is reported as unknown rather
    # than assumed point-in-time — the whole point of the field is that a reader
    # can tell "we know" from "nobody said".
    universe_provenance = read_universe_provenance(
        base_result.meta
    ) or unknown_universe_provenance(
        f"base backtest {config.base_backtest_id!r} carries no "
        f"{UNIVERSE_PROVENANCE_KEY!r} record"
    )
    initial_capital = float(base_result.meta.initial_capital or 100_000.0)
    candidate_universes = _candidate_symbol_universes(
        configured_universes=config.symbol_universes,
        base_symbols=symbols,
        strategy_slug=strategy_slug,
        optimization_mode=config.optimization_mode,
    )

    if config.optimization_mode == "risk_parity_covariance":
        # This mode has no Optuna trial search: covariance is fitted on train
        # and the selected universe is reported on the test holdout. The
        # validation segment exists in `split` but plays no role here because
        # there is no trial selection to make.
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
            base_runtime=base_runtime,
            progress_callback=progress_callback,
        )

    required_symbols = list(
        dict.fromkeys(
            symbol
            for universe in [symbols, *candidate_universes]
            for symbol in universe
        )
    )
    data_dir = str(base_result.meta.data.get("data_dir") or "")
    ensure_backtest_data(
        symbols=required_symbols,
        timeframe=timeframe,
        start=backtest_start,
        end=backtest_end,
        data_dir=data_dir,
    )
    for segment_name, period in (
        ("train", train_period),
        ("validation", validation_period),
        ("test", test_period),
    ):
        for symbol in required_symbols:
            coverage_kwargs: dict[str, Any] = {
                "symbol": symbol,
                "timeframe": timeframe,
                "start": period["start"],
                "end": period["end"],
                "data_dir": data_dir,
            }
            if coverage_session is not None:
                coverage_kwargs["session"] = coverage_session
            if not has_sufficient_segment_coverage(**coverage_kwargs):
                raise ValueError(
                    f"Optimization {segment_name} segment has insufficient OHLCV "
                    f"coverage for {symbol} {timeframe} ({period['start']} -> "
                    f"{period['end']}: need >=95% of expected bars, no gap >3x "
                    "timeframe)"
                )

    _emit_progress(progress_callback, 0.0, "Starting Optuna optimization.")

    # Sub-periods of the train window, for the per-period stability view, and
    # the per-symbol data coverage the survivorship diagnostic reports on. Both
    # are read AFTER the data has been ensured, so what they see is the data
    # this study actually trades.
    stability_subperiods = _stability_subperiods(train_period, config, timeframe)
    stored_context = base_result.meta.data.get("execution_context")
    data_source = (
        str(stored_context.get("data_source") or "")
        if isinstance(stored_context, dict)
        else ""
    )
    universe_coverage = observe_symbol_coverage(
        required_symbols,
        timeframe=timeframe,
        data_dir=data_dir,
        data_source=data_source,
    )

    last_trial_exception: BaseException | None = None

    def _record_failure(trial: optuna.Trial, exc: BaseException) -> NoReturn:
        nonlocal last_trial_exception
        last_trial_exception = exc
        trial.set_user_attr("failure_reason", f"{type(exc).__name__}: {exc}")
        raise exc

    study = optuna.create_study(direction=_study_direction(config.target_metric))

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
                BacktestConfig(
                    strategy_name=strategy_slug,
                    symbols=trial_symbols,
                    timeframe=timeframe,
                    start_date=train_period["start"],
                    end_date=train_period["end"],
                    initial_capital=initial_capital,
                    parameters=combined_params,
                    **base_runtime,
                ),
                strategy_path,
            )
        except (ValueError, RuntimeError, KeyError) as exc:
            return _record_failure(trial, exc)
        train_stats = _canonical_stats(train_result)
        trial.set_user_attr("train_stats", _json_safe(train_stats))
        trial.set_user_attr("effective_parameters", _json_safe(combined_params))
        trial.set_user_attr("symbols", _json_safe(trial_symbols))
        trial.set_user_attr("trade_count", len(train_result.trades))
        trial.set_user_attr(
            SUBPERIOD_PNL_ATTR,
            _json_safe(_trial_subperiod_pnl(train_result, stability_subperiods)),
        )

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
            penalty = _no_trade_penalty_value(
                completed_trading_values,
                direction=_study_direction(config.target_metric),
            )
            trial.set_user_attr("metric_value", float(penalty))
            return penalty

        raw_metric_value = _metric_value(train_stats, config.target_metric)
        metric_value = raw_metric_value
        # C3: raw maximize targets (total_pnl/profit_factor/win_rate/...)
        # carry mandatory trade-count/drawdown haircuts so lucky sparse
        # runs cannot beat robust ones. Terms are recorded for transparency.
        metric_direction = _study_direction(config.target_metric)
        metric_key = _metric_key(config.target_metric, train_stats)
        if config.risk_adjust_objective:
            metric_value, penalty_terms = _risk_adjusted_metric_value(
                metric_value,
                train_stats,
                metric_key=metric_key,
                direction=metric_direction,
                min_trades_for_full_credit=config.min_trades_for_full_credit,
                drawdown_penalty_weight=config.drawdown_penalty_weight,
            )
            trial.set_user_attr("penalty_terms", penalty_terms)
        # The NO_TRADE_PENALTY floor only steers Optuna correctly while every
        # honest metric value ranks above it. A metric that reaches the floor
        # (e.g. a raw-PnL target on a large account) would silently invert the
        # no-trade steering, so record the failure loudly instead.
        if _study_direction(config.target_metric) == "minimize":
            # p_value and friends: the penalty is a CEILING above every
            # plausible metric value, so a metric at/above it would invert
            # the steering the same way the floor would for maximize.
            if metric_value >= NO_TRADE_PENALTY_MINIMIZE:
                _record_failure(
                    trial,
                    ValueError(
                        f"{config.target_metric}={metric_value} is at or above the "
                        f"no-trade penalty ceiling ({NO_TRADE_PENALTY_MINIMIZE}); the "
                        "ceiling only holds for metrics bounded below it. Choose a "
                        "different target_metric or raise the ceiling's magnitude."
                    ),
                )
        elif metric_value <= NO_TRADE_PENALTY:
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
        trial.set_user_attr("metric_name", metric_key)
        trial.set_user_attr("raw_metric_value", raw_metric_value)
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
            + (
                f" ({trial.user_attrs.get('failure_reason')})"
                if trial.user_attrs.get("failure_reason")
                else ""
            )
            for trial in study.get_trials(deepcopy=False)[-5:]
        )
        raise RuntimeError(
            f"No optimization trial completed successfully "
            f"({config.n_trials} trials attempted); refusing to report a best "
            f"parameter set from failed backtests. Last trials: {failure_summary}"
        ) from last_trial_exception

    best_trial = _best_trial_or_raise(study, direction=_study_direction(config.target_metric))
    # Every completed trial is evaluated on the VALIDATION segment, after the
    # search is over and before the winner is fixed. Nothing in the objective
    # or the search can see this window.
    _evaluate_trials_on_validation(
        trials=completed_trials,
        validation_period=validation_period,
        config=config,
        base_parameters=base_parameters,
        symbols=symbols,
        timeframe=timeframe,
        initial_capital=initial_capital,
        base_runtime=base_runtime,
        strategy_slug=strategy_slug,
        strategy_path=strategy_path,
        progress_callback=progress_callback,
    )
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
    best_metric_key = _metric_key(config.target_metric, best_train_stats)
    best_raw_value = _metric_value(best_train_stats, config.target_metric)
    best_value_is_risk_adjusted = bool(
        config.risk_adjust_objective
        and best_metric_key in RAW_MAXIMIZE_METRICS
        and best_raw_value >= 0
    )

    test_result = _run_vectorbt_backtest(
        BacktestConfig(
            strategy_name=strategy_slug,
            symbols=best_symbols,
            timeframe=timeframe,
            start_date=test_period["start"],
            end_date=test_period["end"],
            initial_capital=initial_capital,
            parameters=effective_best_params,
            **base_runtime,
        ),
        strategy_path,
    )
    test_stats = _canonical_stats(test_result)

    now = datetime.now(timezone.utc)
    optimization_id = (
        f"opt_{config.base_backtest_id}_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}"
    )
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
        "universe_provenance": _json_safe(universe_provenance),
        "best_params": _json_safe(best_params),
        "effective_best_parameters": _json_safe(effective_best_params),
        "best_value": _safe_float_or_none(best_value),
        "best_metric_key": best_metric_key,
        "best_raw_value": _safe_float_or_none(best_raw_value),
        "best_value_is_risk_adjusted": best_value_is_risk_adjusted,
        "oos_comparison": {
            "metric_key": best_metric_key,
            "in_sample_value": _safe_float_or_none(best_raw_value),
            "compatible": True,
        },
        "train_period": train_period,
        "test_period": test_period,
        "train_stats": _json_safe(best_train_stats),
        "test_stats": _json_safe(test_stats),
        "oos_status": "available",
        "top_trials": _top_trials_payload(
            study.trials,
            limit=10,
            direction=_study_direction(config.target_metric),
        ),
        "diagnostics": _json_safe(
            _build_diagnostics_payload(
                trials=study.trials,
                config=config,
                train_period=train_period,
                validation_period=validation_period,
                test_period=test_period,
                target_metric=config.target_metric,
                timeframe=timeframe,
                symbols=required_symbols,
                universe_provenance=universe_provenance,
                universe_coverage=universe_coverage,
                subperiods=stability_subperiods,
                selected_parameters=best_params,
                backtest_start=backtest_start,
                backtest_end=backtest_end,
            )
        ),
    }

    OPTIMIZATIONS_DIR.mkdir(parents=True, exist_ok=True)
    write_text_atomically(output_path, json.dumps(_json_safe(payload), indent=2))
    _emit_progress(progress_callback, 100.0, f"Optimization finished: {output_path.name}")
    return payload


# Canonical platform timeframe labels (as returned by normalize_timeframe) ->
# bar duration in seconds. Expressed directly rather than via pandas offsets
# because pd.Timedelta rejects non-fixed frequencies such as "D" and "W".
#
# normalize_timeframe passes through both the "5m" and the "m5" spellings, so
# both families are listed.
_TIMEFRAME_BAR_SECONDS: dict[str, float] = {
    "1s": 1.0,
    "5s": 5.0,
    "10s": 10.0,
    "15s": 15.0,
    "30s": 30.0,
    "1m": 60.0,
    "3m": 180.0,
    "5m": 300.0,
    "15m": 900.0,
    "30m": 1800.0,
    "1h": 3600.0,
    "2h": 7200.0,
    "3h": 10800.0,
    "4h": 14400.0,
    "6h": 21600.0,
    "8h": 28800.0,
    "12h": 43200.0,
    "1d": 86400.0,
    "1w": 604800.0,
    # "<unit><n>" spellings that normalize_timeframe leaves untouched.
    "s1": 1.0,
    "s5": 5.0,
    "s10": 10.0,
    "s15": 15.0,
    "s30": 30.0,
    "m1": 60.0,
    "m3": 180.0,
    "m5": 300.0,
    "m15": 900.0,
    "m30": 1800.0,
    "h1": 3600.0,
    "h2": 7200.0,
    "h3": 10800.0,
    "h4": 14400.0,
    "h6": 21600.0,
    "h8": 28800.0,
    "h12": 43200.0,
    "d1": 86400.0,
    "w1": 604800.0,
}


def _timeframe_bar_duration(timeframe: str | None) -> pd.Timedelta | None:
    """Bar duration for a timeframe label, used to express the embargo in bars."""
    seconds = _TIMEFRAME_BAR_SECONDS.get(normalize_timeframe(timeframe))
    if seconds is None:
        return None
    return pd.Timedelta(seconds=seconds)


#: Optuna user attr holding each trial's realized result per stability
#: sub-period. A dedicated attr (rather than a derived field of train_stats)
#: because the sub-period split is a reporting decision: it must not change what
#: the study optimises, only what can be said about the choice afterwards.
SUBPERIOD_PNL_ATTR = "subperiod_pnl"

#: Optuna user attrs recording a trial's VALIDATION result (#164). Written
#: once, after the search, for every completed trial; selection reads them and
#: the test holdout is never stored here.
VALIDATION_STATS_ATTR = "validation_stats"
VALIDATION_MOMENTS_ATTR = "validation_moments"
VALIDATION_SCORE_ATTR = "validation_score"
VALIDATION_METRIC_ATTR = "validation_metric_value"
VALIDATION_FAILURE_ATTR = "validation_failure_reason"


def _stability_subperiods(
    train_period: Mapping[str, str],
    config: OptimizationConfig,
    timeframe: str | None,
) -> list[dict[str, Any]]:
    """The sub-periods a parameter's stability is measured over.

    Taken from the walk-forward folds of the TRAIN window — the window the
    trials were actually scored on — so the comparison is like-for-like. The
    reported walk-forward folds cover train+validation (never the test
    holdout), so the sub-periods here are deliberately a different, narrower
    set; both are reported, and each carries its own bounds.

    Returns [] when the window is too short for a fold. That is a reportable
    absence, not a failure: the stability view then says "unknown".
    """
    from runner.optimization_diagnostics import (
        subperiods_from_walk_forward,
        walk_forward_splits,
    )

    try:
        folds = walk_forward_splits(
            train_period["start"],
            train_period["end"],
            n_splits=int(getattr(config, "walk_forward_splits", 3) or 3),
            train_fraction=config.train_split_pct,
            embargo_pct=float(getattr(config, "embargo_pct", 0.0) or 0.0),
            bar_duration=_timeframe_bar_duration(timeframe),
        )
        return subperiods_from_walk_forward(folds)
    except (ValueError, KeyError, TypeError) as exc:
        logger.info("No stability sub-periods available: %s", exc)
        return []


def _trade_realization_time(trade: Any) -> str:
    """When a trade's result was realized: its last exit, else its entry.

    Attribution follows the exit, not the entry: a sub-period must be charged
    with the result it actually produced, and a trade opened in one sub-period
    and closed in the next belongs to the second.
    """
    exits = list(getattr(trade, "exits", None) or [])
    for exit_ in reversed(exits):
        stamp = str(getattr(exit_, "time", "") or "").strip()
        if stamp:
            return stamp
    return str(getattr(trade, "entry_time", "") or "").strip()


def _trade_net_pnl(trade: Any) -> float:
    """Net PnL of a trade, using the same gross-minus-fees convention as the CLI.

    An unreadable PnL becomes NaN rather than 0.0: 0.0 would be charged to a
    sub-period as a real result, while NaN is dropped by the attribution, so an
    unreadable number cannot quietly become a flat performance.
    """
    net_pnl = getattr(trade, "net_pnl", None)
    try:
        if net_pnl is not None:
            return float(net_pnl)
        pnl = float(getattr(trade, "pnl", 0.0) or 0.0)
        fees = getattr(trade, "fees", None)
        return pnl - (float(fees) if fees is not None else 0.0)
    except (TypeError, ValueError):
        return float("nan")


def _trial_subperiod_pnl(result: BacktestResult, subperiods: list[dict[str, Any]]) -> dict[str, float]:
    """Realized net PnL of one trial, per stability sub-period."""
    if not subperiods:
        return {}
    from runner.optimization_diagnostics import realized_pnl_by_subperiod

    return realized_pnl_by_subperiod(
        ((_trade_realization_time(trade), _trade_net_pnl(trade)) for trade in result.trades),
        subperiods,
    )


def _run_period_backtest(
    *,
    strategy_slug: str,
    strategy_path: Path,
    symbols: Sequence[str],
    timeframe: str,
    period: Mapping[str, str],
    initial_capital: float,
    parameters: Mapping[str, Any],
    base_runtime: Mapping[str, Any],
) -> BacktestResult:
    """Run one trial's parameter set over one segment."""
    return _run_vectorbt_backtest(
        BacktestConfig(
            strategy_name=strategy_slug,
            symbols=list(symbols),
            timeframe=timeframe,
            start_date=period["start"],
            end_date=period["end"],
            initial_capital=initial_capital,
            parameters=dict(parameters),
            **dict(base_runtime),
        ),
        strategy_path,
    )


def _trial_effective_parameters(
    trial: Any,
    base_parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """The full parameter set a trial ran with.

    Read from the attr the objective recorded, so a validation re-run uses
    exactly the configuration the study scored (including the derived symbol
    universe). Falls back to base + suggested params for a study that did not
    record it.
    """
    stored = getattr(trial, "user_attrs", {}).get("effective_parameters")
    if isinstance(stored, Mapping) and stored:
        return dict(stored)
    combined = dict(base_parameters)
    for key, value in dict(getattr(trial, "params", {}) or {}).items():
        if str(key).startswith(_INTERNAL_PARAM_PREFIX):
            continue
        combined[key] = value
    return combined


def _trial_selection_score(
    stats: Mapping[str, Any],
    config: OptimizationConfig,
) -> tuple[float, float]:
    """Validation score of a trial, oriented so higher is always better.

    Returns ``(score, raw_metric)``. Raw maximize targets get the same
    trade-count/drawdown haircut the objective applies, so a lucky sparse
    validation run cannot outrank a robust one; for a ``minimize`` study
    (p_value) the score is negated, so the pure selection function's
    higher-is-better contract still holds.
    """
    raw_metric = _metric_value(dict(stats), config.target_metric)
    score = raw_metric
    if config.risk_adjust_objective:
        metric_key = _metric_key(config.target_metric, dict(stats))
        score, _terms = _risk_adjusted_metric_value(
            raw_metric,
            dict(stats),
            metric_key=metric_key,
            direction=_study_direction(config.target_metric),
            min_trades_for_full_credit=config.min_trades_for_full_credit,
            drawdown_penalty_weight=config.drawdown_penalty_weight,
        )
    if _study_direction(config.target_metric) == "minimize":
        # p_value: smaller is better, so negate it to keep "higher is better".
        score = -score
    return score, raw_metric


def _evaluate_trials_on_validation(
    *,
    trials: Sequence[Any],
    validation_period: Mapping[str, str],
    config: OptimizationConfig,
    base_parameters: Mapping[str, Any],
    symbols: Sequence[str],
    timeframe: str,
    initial_capital: float,
    base_runtime: Mapping[str, Any],
    strategy_slug: str,
    strategy_path: Path,
    progress_callback: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    """Evaluate every completed trial on the VALIDATION segment (#164).

    The search never sees this window. Each trial's train result is already
    stored by the objective; this adds its validation result, validation score
    and daily-return moments (the deflated Sharpe needs them per trial).

    A trial whose validation run fails, or whose target metric cannot be
    computed, is recorded as unselectable rather than aborting the study: one
    bad parameter region must not hide the rest. Returns one row per completed
    trial; ``selectable`` is False for penalty (no-trade) trials and for
    validation failures.
    """
    rows: list[dict[str, Any]] = []
    total = max(len(trials), 1)
    for index, trial in enumerate(trials, start=1):
        _emit_progress(
            progress_callback,
            100.0,
            f"Evaluating trial {index}/{total} on validation.",
        )
        user_attrs = getattr(trial, "user_attrs", {})
        penalty_applied = bool(user_attrs.get("penalty_applied", False))
        row: dict[str, Any] = {
            "number": int(trial.number),
            "trial": trial,
            "selectable": False,
            "validation_score": None,
            "validation_metric_value": None,
            "validation_stats": {},
            "validation_moments": None,
        }
        try:
            result = _run_period_backtest(
                strategy_slug=strategy_slug,
                strategy_path=strategy_path,
                symbols=user_attrs.get("symbols") or symbols,
                timeframe=timeframe,
                period=validation_period,
                initial_capital=initial_capital,
                parameters=_trial_effective_parameters(trial, base_parameters),
                base_runtime=base_runtime,
            )
            stats = _canonical_stats(result)
            score, raw_metric = _trial_selection_score(stats, config)
            moments = daily_returns_moments(result.equity_curve)
        except (ValueError, RuntimeError, KeyError) as exc:
            trial.set_user_attr(
                VALIDATION_FAILURE_ATTR, f"{type(exc).__name__}: {exc}"
            )
            rows.append(row)
            continue

        trial.set_user_attr(VALIDATION_STATS_ATTR, _json_safe(stats))
        trial.set_user_attr(VALIDATION_MOMENTS_ATTR, _json_safe(moments))
        trial.set_user_attr(VALIDATION_SCORE_ATTR, float(score))
        trial.set_user_attr(VALIDATION_METRIC_ATTR, float(raw_metric))
        row.update(
            {
                # A no-trade trial stays unselectable even if it happens to
                # trade on validation: the penalty marks a parameter region the
                # study must not hand back as its answer.
                "selectable": not penalty_applied,
                "validation_score": float(score),
                "validation_metric_value": float(raw_metric),
                "validation_stats": stats,
                "validation_moments": moments,
            }
        )
        rows.append(row)
    return rows


def _build_diagnostics_payload(
    *,
    trials: Sequence[Any],
    config: OptimizationConfig,
    train_period: Mapping[str, str],
    validation_period: Mapping[str, str],
    test_period: Mapping[str, str],
    target_metric: str,
    timeframe: str | None = None,
    symbols: Sequence[str] | None = None,
    universe_provenance: Mapping[str, Any] | None = None,
    universe_coverage: Mapping[str, Any] | None = None,
    subperiods: Sequence[Mapping[str, Any]] | None = None,
    selected_parameters: Mapping[str, Any] | None = None,
    backtest_start: str = "",
    backtest_end: str = "",
) -> dict[str, Any]:
    """Attach honesty checks to the optimization result.

    Reports how much the selected parameter should be trusted: whether the
    optimum sits on a stable plateau, how much of the score is explained by
    picking the best of many trials, what the split/embargo actually was, and —
    because a universe that is not point-in-time makes all of the above read
    better than it should — what the symbol universe was and how far its
    membership can be trusted. Never changes the selection; only annotates it.

    The universe arguments are optional so a caller with no coverage
    information still gets a payload that says so (the survivorship view
    reports "unknown" rather than silently disappearing).
    """
    from runner.optimization_diagnostics import (
        multiple_testing_correction,
        plateau_analysis,
        subperiod_parameter_stability,
    )

    payload: dict[str, Any] = {}
    bar_duration = _timeframe_bar_duration(timeframe)

    # 1. Split geometry, including both embargo gaps (#164).
    #    The three segments and their gaps are the windows that RAN, not a
    #    recomputation: a re-derived split has drifted from the applied one
    #    before (see the removed `embargo_split` call), and a reader cannot
    #    tell which of two answers in the same artifact to trust.
    try:
        embargo_train_val = {
            "start": train_period["end"],
            "end": validation_period["start"],
        }
        embargo_val_test = {
            "start": validation_period["end"],
            "end": test_period["start"],
        }
        train_validation_gap = pd.Timedelta(
            pd.Timestamp(validation_period["start"]) - pd.Timestamp(train_period["end"])
        )
        reported: dict[str, Any] = {
            "train": dict(train_period),
            "validation": dict(validation_period),
            "test": dict(test_period),
            "embargo_train_val": embargo_train_val,
            "embargo_val_test": embargo_val_test,
            # Legacy key: before #164 the split had one gap, into the segment
            # trial results were judged on. That segment is now validation.
            "embargo": dict(embargo_train_val),
            "embargo_bars_equivalent": (
                float(train_validation_gap / bar_duration) if bar_duration else 0.0
            ),
        }
        payload["split"] = reported
    except (ValueError, KeyError) as exc:
        payload["split"] = {"error": str(exc)}

    # 2. Walk-forward fold BOUNDARIES inside the range before the test holdout
    #    and its embargo. This is the geometry used to cut the sub-periods for
    #    the stability view below; no parameter is re-fitted per fold and no
    #    per-fold performance is computed, so it is not walk-forward evidence.
    #    The key says so (it was ``walk_forward``, which read like a result).
    payload["walk_forward_note"] = (
        "Fold boundaries only, inside the range before the test holdout and its "
        "embargo: no per-fold performance was computed, so this is not "
        "walk-forward evidence."
    )
    try:
        payload["walk_forward_folds"] = walk_forward_train_validation_folds(
            train_period["start"],
            test_period["end"],
            n_splits=int(getattr(config, "walk_forward_splits", 3) or 3),
            train_fraction=config.train_split_pct,
            embargo_pct=float(getattr(config, "embargo_pct", 0.0) or 0.0),
            bar_duration=bar_duration,
            test_fraction=float(config.test_split_pct or 0.0),
            embargo_days=int(getattr(config, "embargo_days", 0) or 0),
        )
    except (ValueError, KeyError) as exc:
        payload["walk_forward_folds"] = {"error": str(exc)}

    # 3. Plateau stability and selection bias over the evaluated trials.
    # Two classes of row are NOT strategy performances and must not reach
    # either statistic:
    #  - no-trade ("penalty") trials, which carry a sentinel value. The
    #    sentinel is *dynamic* (`_no_trade_penalty_value`), so the value-level
    #    `penalty_floor` filter below catches only the very first trial's
    #    -1e9 and lets every later `min(values) - 1.0` through — which put a
    #    sentinel in the sample mean/spread and in the plateau's neighbour
    #    list, where it made `sensitivity` a ratio against a penalty rather
    #    than against the nearest real parameter value. The `penalty_applied`
    #    user attr is the definitive marker, so filter on that.
    #  - internal bookkeeping params (`__internal_symbol_universe`), which are
    #    optimizer plumbing, not strategy parameters: they must not be chosen
    #    as the plateau axis, and a row carrying nothing but internal params is
    #    not a trial at all.
    trial_rows: list[dict[str, Any]] = []
    stability_rows: list[dict[str, Any]] = []
    excluded_no_trade_trials = 0
    for trial in trials or []:
        if bool(getattr(trial, "user_attrs", {}).get("penalty_applied", False)):
            excluded_no_trade_trials += 1
            continue
        params = getattr(trial, "params", None)
        value = getattr(trial, "value", None)
        if not isinstance(params, dict) or not isinstance(value, (int, float)):
            continue
        row = {
            key: item
            for key, item in params.items()
            if not str(key).startswith(_INTERNAL_PARAM_PREFIX)
        }
        if not row:
            continue
        row["metric"] = value
        trial_rows.append(row)
        # Same row filter for the stability view: a no-trade trial's realized
        # PnL is a flat 0.0 in every sub-period, and including it would let a
        # parameter value that never traded "win" a sub-period.
        subperiod_pnl = getattr(trial, "user_attrs", {}).get(SUBPERIOD_PNL_ATTR)
        if isinstance(subperiod_pnl, dict) and subperiod_pnl:
            stability_rows.append({"params": row, "subperiod_pnl": subperiod_pnl})

    # A plateau is only meaningful along a parameter that actually varied.
    varied: dict[str, set[Any]] = {}
    for row in trial_rows:
        for key, value in row.items():
            if key == "metric":
                continue
            varied.setdefault(key, set()).add(value)
    candidates = [name for name, values in varied.items() if len(values) > 1]
    # Pick the parameter explored most broadly: that is where a plateau (or its
    # absence) says the most about robustness — and the same axis the sub-period
    # stability view ranks on, so the two views cannot disagree about which
    # parameter they are talking about.
    parameter = max(candidates, key=lambda name: len(varied[name])) if candidates else None
    if parameter is not None:
        plateau = plateau_analysis(
            trial_rows,
            parameter=parameter,
            metric="metric",
            # The plateau must centre on the trial the study selects, i.e. the
            # same direction the multiple-testing correction below uses.
            direction=METRIC_DIRECTIONS.get(str(target_metric or ""), "maximize"),
        )
        payload["plateau"] = (
            {"parameter": parameter, **plateau.as_dict()} if plateau else None
        )
    else:
        payload["plateau"] = None

    # penalty_floor is the second line of defence for a sentinel that reached
    # the metrics list without the marker (a caller that synthesised trials).
    # The direction has to travel with the numbers: `p_value` is a minimize
    # target, and correcting a minimize study against the maximum end of the
    # sample reports the search bias of its *worst* trial, which inverts the
    # verdict rather than merely mis-scaling it.
    multiple_testing = multiple_testing_correction(
        [float(row["metric"]) for row in trial_rows],
        penalty_floor=_NO_TRADE_PENALTY,
        direction=METRIC_DIRECTIONS.get(str(target_metric or ""), "maximize"),
    )
    # The rows were already filtered above, so the function's own count is zero
    # here; the honest number is how many were kept out.
    multiple_testing["n_penalty_trials_excluded"] = int(
        multiple_testing.get("n_penalty_trials_excluded") or 0
    ) + excluded_no_trade_trials
    payload["multiple_testing"] = multiple_testing
    # Never let the sample shrink silently: a reader has to be able to see that
    # the reported statistics describe fewer rows than the study ran.
    payload["n_no_trade_trials_excluded"] = excluded_no_trade_trials

    # 4. Survivorship bias: what the stored data says about a universe that
    #    may not be point-in-time, and the direction/magnitude of the bias.
    payload["survivorship"] = survivorship_bias_diagnostic(
        symbols=list(symbols or []),
        coverage=universe_coverage,
        window_start=backtest_start or train_period.get("start", ""),
        window_end=backtest_end or test_period.get("end", ""),
        provenance=universe_provenance,
    )

    # 5. Per-sub-period parameter stability: does the value that won the study
    #    also win in each sub-period of the window it was scored on? Reported
    #    against the same axis as the plateau, and "unknown" (never a guess)
    #    when no trial carries sub-period results.
    if parameter is not None and stability_rows:
        observations = [
            {"period": period, "params": row["params"], "metric": period_pnl}
            for row in stability_rows
            for period, period_pnl in row["subperiod_pnl"].items()
        ]
        payload["subperiod_stability"] = subperiod_parameter_stability(
            observations,
            parameter=parameter,
            metric_name="realized_net_pnl_per_subperiod",
            selected_parameters=selected_parameters,
            subperiods=list(subperiods or []),
        )
    else:
        payload["subperiod_stability"] = subperiod_parameter_stability(
            [],
            parameter=parameter or "",
            metric_name="realized_net_pnl_per_subperiod",
            subperiods=list(subperiods or []),
        )
    payload["target_metric"] = target_metric
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
    train_split_pct: float = 0.6,
    validation_split_pct: float | None = None,
    test_split_pct: float | None = None,
    embargo_days: int = 1,
    embargo_pct: float = 0.0,
) -> ThreeWaySplit:
    """Split the base range into train / validation / test windows (#164).

    The split itself is :func:`runner.optimization_diagnostics.three_way_split`:
    train, embargo, validation, embargo, test, with test as the final holdout.
    Callers must not derive a two-way split from the result: the search runs on
    ``train`` only, selection on ``validation``, and ``test`` is read once the
    winner is fixed.

    ``embargo_pct`` drops a gap between each pair of segments, on top of the
    fixed ``embargo_days``. A trade opened just before a split can still be open
    in the next segment, so its label contains that segment's information;
    without the gap that leaks backwards. The gap is applied here, to the
    windows that actually run, rather than only being reported by the
    diagnostics.

    ``validation_split_pct``/``test_split_pct`` default to the remainder
    ``1 - train_split_pct`` split equally, matching ``OptimizationConfig``.
    """
    if embargo_pct < 0.0:
        raise ValueError("embargo_pct must not be negative")

    start_ts = _parse_period_boundary(start_value, is_end=False)
    end_ts = _parse_period_boundary(end_value, is_end=True)
    if end_ts <= start_ts:
        raise ValueError(
            f"Base backtest period is invalid: start={start_value!r}, end={end_value!r}"
        )

    if validation_split_pct is None and test_split_pct is None:
        remainder = 1.0 - train_split_pct
        validation_split_pct = remainder / 2.0
        test_split_pct = remainder / 2.0
    elif validation_split_pct is None or test_split_pct is None:
        raise ValueError(
            "Both validation_split_pct and test_split_pct must be provided together, "
            "or neither (to use the equal-split default)"
        )

    return three_way_split(
        start_ts,
        end_ts,
        train_fraction=train_split_pct,
        validation_fraction=validation_split_pct,
        test_fraction=test_split_pct,
        embargo_days=embargo_days,
        embargo_pct=embargo_pct,
    )


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


def _runtime_config_from_base_result(result: BacktestResult) -> dict[str, Any]:
    data = result.meta.data if isinstance(result.meta.data, dict) else {}
    parameters = result.meta.parameters if isinstance(result.meta.parameters, dict) else {}
    stored_runtime = data.get("_backtest_config", {}) if isinstance(data, dict) else {}

    def value(name: str, default: Any = None) -> Any:
        meta_value = getattr(result.meta, name, None)
        if meta_value is not None:
            return meta_value
        if name in data and data[name] is not None:
            return data[name]
        if name in stored_runtime and stored_runtime[name] is not None:
            return stored_runtime[name]
        return parameters.get(name, default)

    risk_per_trade_pct = getattr(result.meta, "risk_per_trade_pct", None)
    if risk_per_trade_pct is None:
        risk_per_trade_pct = data.get("risk_per_trade_pct")
    if risk_per_trade_pct is None:
        risk_per_trade_pct = stored_runtime.get("risk_per_trade_pct")
    if risk_per_trade_pct is None:
        raise ValueError(
            "Base backtest is a legacy artifact without an explicit "
            "risk_per_trade_pct runtime snapshot. Optimization cannot safely "
            "reuse it; rerun the base backtest with risk_per_trade_pct configured "
            "or provide a complete explicit runtime snapshot. No default risk "
            "was applied."
        )
    try:
        numeric_risk = float(risk_per_trade_pct)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "Base backtest risk_per_trade_pct must be a finite fraction between "
            f"0 and 1, got {risk_per_trade_pct!r}."
        ) from exc
    if not math.isfinite(numeric_risk) or not 0.0 <= numeric_risk <= 1.0:
        raise ValueError(
            "Base backtest risk_per_trade_pct must be a finite fraction between "
            f"0 and 1, got {risk_per_trade_pct!r}."
        )

    return {
        "risk_per_trade_pct": numeric_risk,
        "max_position_exposure_pct": value("max_position_exposure_pct"),
        "max_leverage": value("max_leverage"),
        "modeling_resolution": value("modeling_resolution", "open_close"),
        "signal_exit_fill": value("signal_exit_fill", "close"),
        "allow_fallback_stop": value("allow_fallback_stop", True),
        "reject_missing_stop": value("reject_missing_stop", False),
        "require_explicit_column_roles": value("require_explicit_column_roles", False),
    }


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
    base_runtime: dict[str, Any] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    base_runtime = base_runtime or _runtime_config_from_base_result(base_result)
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
        data_dir=str(base_result.meta.data.get("data_dir") or ""),
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
        BacktestConfig(
            strategy_name=strategy_slug,
            symbols=best_symbols,
            timeframe=timeframe,
            start_date=test_period["start"],
            end_date=test_period["end"],
            initial_capital=initial_capital,
            parameters=effective_best_params,
            **base_runtime,
        ),
        strategy_path,
    )
    test_stats = _canonical_stats(test_result)

    now = datetime.now(timezone.utc)
    optimization_id = (
        f"rp_cov_{config.base_backtest_id}_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}"
    )
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
        "oos_status": "available",
        "risk_parity": _json_safe(selection),
        "top_trials": [],
    }

    OPTIMIZATIONS_DIR.mkdir(parents=True, exist_ok=True)
    write_text_atomically(output_path, json.dumps(_json_safe(payload), indent=2))
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
    # NOTE (C3): the 0.0 here fills STRATEGY-equity days (a day without a
    # trade leaves equity flat, so 0% is exact — not fabricated market data).
    # Market-data bars use intersection instead (see above).
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
        matches = sorted(
            root.rglob(f"**/{symbol}/{timeframe}.parquet"),
            key=lambda item: item.as_posix(),
        )
        path = matches[0] if matches else None
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
        series[str(symbol).upper()] = asset.set_index("time")["close"].pct_change()
    if len(series) < 2:
        return pd.DataFrame()
    # C3: no zero-fill across symbols — a missing bar is missing data, not a
    # 0% return. Rows without full overlap are dropped (intersection); the
    # caller falls back to the trade-PnL frame when nothing overlaps.
    # (The leading NaN of each pct_change row is dropped with them.)
    return pd.DataFrame(series).sort_index().dropna()


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
    direction: str = "maximize",
) -> list[dict[str, Any]]:
    completed_trials = [
        trial
        for trial in trials
        if trial.state == optuna.trial.TrialState.COMPLETE and trial.value is not None
    ]
    # Rank in the study's direction (#82): minimize-metrics (p_value) list
    # their BEST (lowest) trials first.
    ordered_trials = sorted(
        completed_trials,
        key=lambda trial: float(trial.value),
        reverse=(direction != "minimize"),
    )
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
                "raw_metric_value": _safe_float_or_none(
                    trial.user_attrs.get("raw_metric_value")
                ),
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
