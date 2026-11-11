"""Central trailing-stop mode contract (issue #150).

One definition of the trailing modes that exist and of which executing path
supports which mode. Everything downstream (risk engine, signal-bot
processors, replay adapter, live runner) validates against these constants
instead of hard-coding its own list.

Fail closed: a mode that is unknown, or that the executing path cannot
execute, is rejected with a ``ValueError`` — at configuration/deployment
validation time and again at signal time. There is no path where a requested
trailing mode is dropped with only a log line while the trade happens anyway.

Live support is per broker (issue #167): trailing a live stop means amending
the broker-side protective stop, so a mode is only supported on a broker whose
adapter can amend a stop without ever leaving the position unprotected. The
per-broker table lives here (``LIVE_SUPPORTED_TRAILING_MODES_BY_BROKER``);
the live runner additionally requires the adapter instance to declare the
``supports_stop_amendment`` capability at signal time.

``supertrend_flip`` (#168) is executed by the replay backtest only. Its
parameters are validated by ``parse_supertrend_flip_plan`` (fail closed, no
invented defaults). The live path keeps rejecting it: the Alpaca entry is a
full-quantity bracket whose take-profit leg would close the whole position at
TP1, so a TP1 *partial* exit needs a different entry order structure first
(see ``docs/live-deploy-runbook.md``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: Metadata key a signal processor uses to request trailing-stop behaviour.
TRAILING_SL_CONFIG_METADATA_KEY = "trailing_sl_config"

#: Explicit "initial stop only" — no trailing requested or performed.
TRAILING_MODE_NONE = "none"
#: Stop trails the market price at a fixed relative distance (0 < percent < 1).
TRAILING_MODE_PERCENT = "percent"
#: Supertrend-flip workflow (#168): TP1 partial exit, breakeven stop, then exit
#: on the first red Supertrend bar after the first green one. Replay only.
TRAILING_MODE_SUPERTREND_FLIP = "supertrend_flip"

#: Every trailing mode this codebase knows. A config value outside this set is
#: a typo/mistake and is rejected wherever it is read.
KNOWN_TRAILING_MODES: frozenset[str] = frozenset(
    {
        TRAILING_MODE_NONE,
        TRAILING_MODE_PERCENT,
        TRAILING_MODE_SUPERTREND_FLIP,
    }
)

#: Modes the replay backtest adapter (`runner/engines/signal_replay_adapter.py`)
#: executes. ``supertrend_flip`` is run by the adapter itself (it needs the bar
#: series and partial exits), not by ``RiskEngine.apply_trailing_stop``.
REPLAY_SUPPORTED_TRAILING_MODES: frozenset[str] = frozenset(
    {TRAILING_MODE_NONE, TRAILING_MODE_PERCENT, TRAILING_MODE_SUPERTREND_FLIP}
)

#: Modes a live adapter runs when it cannot amend a broker-side stop, and the
#: fail-closed answer for an unknown broker: the explicit "no trailing" only.
LIVE_BASELINE_TRAILING_MODES: frozenset[str] = frozenset({TRAILING_MODE_NONE})

#: Modes the live path executes, per broker (``LiveAccountConfig.broker``).
#: - ``alpaca``: ``percent`` is trailed by `live_runner/trailing_stops.py`,
#:   which replaces the protective stop order in place (#167).
#: - ``ctrader``: the daemon has no cTrader market data to trail on and the
#:   adapter declares no stop-amendment capability, so only ``none``.
#: ``supertrend_flip`` is on no broker: its TP1 partial exit cannot coexist
#: with the full-quantity entry bracket (module docstring, #168).
LIVE_SUPPORTED_TRAILING_MODES_BY_BROKER: dict[str, frozenset[str]] = {
    "alpaca": frozenset({TRAILING_MODE_NONE, TRAILING_MODE_PERCENT}),
    "ctrader": LIVE_BASELINE_TRAILING_MODES,
}

#: Modes at least one live broker executes. This is NOT a gate for trading a
#: signal — it only bounds what a deployment may request before its account's
#: broker is known (model validation). Every later stage validates per broker.
LIVE_ANY_BROKER_TRAILING_MODES: frozenset[str] = frozenset().union(
    *LIVE_SUPPORTED_TRAILING_MODES_BY_BROKER.values()
)

#: Modes ``RiskEngine.apply_trailing_stop`` can execute. This is the engine's
#: own executable set (a backstop behind the per-path gates), not a path's.
#: ``supertrend_flip`` is deliberately absent: it is a position workflow
#: (partial exit, bar-close decisions, next-open fill) that the executing path
#: runs itself, never a per-price stop recomputation.
RISK_ENGINE_SUPPORTED_TRAILING_MODES: frozenset[str] = frozenset(
    {TRAILING_MODE_NONE, TRAILING_MODE_PERCENT}
)


def normalize_trailing_mode(value: Any) -> str:
    """Normalize a configured trailing mode value (strip + lowercase)."""
    return str(value or "").strip().lower()


def validate_trailing_mode(
    value: Any,
    *,
    supported: frozenset[str],
    context: str,
) -> str:
    """Validate one trailing mode value and return it normalized.

    Raises ``ValueError`` — with a distinct message for a typo (unknown mode)
    and for a known mode the executing path does not support — so a requested
    trailing stop is never silently dropped.
    """
    mode = normalize_trailing_mode(value)
    if mode not in KNOWN_TRAILING_MODES:
        known = ", ".join(sorted(KNOWN_TRAILING_MODES))
        raise ValueError(
            f"Unknown trailing stop mode {value!r} in {context}. "
            f"Known modes: {known}."
        )
    if mode not in supported:
        supported_list = ", ".join(sorted(supported))
        raise ValueError(
            f"Trailing stop mode {mode!r} is not supported by {context} "
            f"(supported: {supported_list}). Refusing to run without the "
            "trailing stop the configuration requests; enable a supported "
            "mode or extend the path's supported set explicitly."
        )
    return mode


def validate_trailing_sl_config(
    trailing_cfg: Any,
    *,
    supported: frozenset[str],
    context: str,
) -> str | None:
    """Validate a signal's ``trailing_sl_config`` metadata value.

    Returns the normalized mode, or ``None`` when no trailing is requested
    (key absent or explicitly ``None``). Raises ``ValueError`` on malformed
    configs, unknown modes, and modes the executing path does not support —
    the caller must then refuse to trade the signal.
    """
    if trailing_cfg is None:
        return None
    if not isinstance(trailing_cfg, dict):
        raise ValueError(
            f"trailing_sl_config in {context} must be a mapping, got "
            f"{type(trailing_cfg).__name__}."
        )
    if not normalize_trailing_mode(trailing_cfg.get("mode")):
        raise ValueError(
            f"trailing_sl_config in {context} must declare a 'mode' "
            f"({', '.join(sorted(KNOWN_TRAILING_MODES))})."
        )
    return validate_trailing_mode(
        trailing_cfg.get("mode"),
        supported=supported,
        context=context,
    )


def live_supported_trailing_modes(
    broker: Any,
    *,
    supports_stop_amendment: bool = True,
) -> frozenset[str]:
    """Trailing modes the live path executes for one broker.

    Fail closed: an unknown/blank broker, or an adapter that cannot amend a
    broker-side stop (``supports_stop_amendment`` is not literally ``True``),
    gets the baseline ``none`` only. Deployment validation has no adapter
    instance and relies on the broker table alone; the live runner passes the
    adapter's own capability flag at signal time.
    """
    if supports_stop_amendment is not True:
        return LIVE_BASELINE_TRAILING_MODES
    normalized = str(broker or "").strip().lower()
    return LIVE_SUPPORTED_TRAILING_MODES_BY_BROKER.get(
        normalized, LIVE_BASELINE_TRAILING_MODES
    )


class _AnyBroker:
    """Sentinel type: the deployment's broker is not known at this stage."""


ANY_BROKER: Any = _AnyBroker()


def validate_live_deployment_trailing_override(
    signal_config: Any,
    *,
    context: str,
    broker: Any = ANY_BROKER,
) -> None:
    """Validate a live deployment's trailing-mode override, if it sets one.

    A deployment selects the trailing mode through its signal-processor
    config override (``signal_config.signal_processor_config.trailing_mode``).

    Two stages, both raising ``ValueError`` on a deployment-configuration
    error:

    - ``broker`` omitted (model validation, where only the account id is
      known): the mode must be one that at least one live broker executes.
    - ``broker`` given (deployment creation, account re-point, YAML parse —
      wherever the account is resolved): the mode must be supported by that
      broker. ``None``/unknown brokers fail closed to ``none`` only.

    Deployments that do not set the override inherit the processor's own
    (validated) default; the signal-time gates still apply to every signal.
    """
    if not isinstance(signal_config, dict):
        return
    processor_config = signal_config.get("signal_processor_config")
    if not isinstance(processor_config, dict) or "trailing_mode" not in processor_config:
        return
    if broker is ANY_BROKER:
        supported = LIVE_ANY_BROKER_TRAILING_MODES
        where = f"{context} signal_processor_config.trailing_mode (any live broker)"
    else:
        supported = live_supported_trailing_modes(broker)
        broker_label = str(broker or "").strip().lower() or "unknown"
        where = (
            f"{context} signal_processor_config.trailing_mode "
            f"on broker {broker_label!r}"
        )
    validate_trailing_mode(
        processor_config.get("trailing_mode"),
        supported=supported,
        context=where,
    )


# ── supertrend_flip workflow parameters (#168) ───────────────────────────────

#: The only activation/exit phases implemented. A config that names another
#: phase asks for a workflow nothing runs and is rejected.
SUPERTREND_FLIP_ACTIVATION_PHASE = "wait_for_first_green_after_tp1"
SUPERTREND_FLIP_EXIT_PHASE = "exit_on_first_red_after_activation"


@dataclass(frozen=True)
class SupertrendFlipPlan:
    """Validated parameters of one ``supertrend_flip`` position.

    Workflow (long entries only, as emitted by the RamJet processor):

    1. TP1: exit ``tp1_exit_fraction`` of the entry quantity at ``tp1_price``.
    2. After TP1: move the stop to the entry price if ``breakeven_after_tp1``.
    3. Activation: the first closed bar after TP1 whose Supertrend is ready
       and green.
    4. Exit: the remainder on the first closed bar after activation whose
       Supertrend is ready and red; filled at the next bar's open.
    """

    tp1_price: float
    tp1_exit_fraction: float
    breakeven_after_tp1: bool
    supertrend_period: int
    supertrend_multiplier: float


def _finite_float(value: Any, *, field: str, context: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"supertrend_flip in {context} requires numeric {field}, got {value!r}.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"supertrend_flip in {context} requires numeric {field}, got {value!r}."
        ) from exc
    if not math.isfinite(number):
        raise ValueError(f"supertrend_flip in {context} requires a finite {field}, got {value!r}.")
    return number


def parse_supertrend_flip_plan(
    trailing_cfg: Any,
    metadata: Any,
    *,
    action: str,
    take_profit: float | None,
    context: str,
) -> SupertrendFlipPlan:
    """Validate a ``supertrend_flip`` signal and return its plan.

    Fail closed, no invented defaults: every parameter must be present and
    valid, otherwise ``ValueError`` and the signal is not traded.

    - ``trailing_cfg``: ``supertrend_period`` (integer >= 1),
      ``supertrend_multiplier`` (> 0), ``breakeven_after_tp1`` (bool); the
      optional ``activation_phase`` / ``exit_phase`` descriptions must name
      the implemented phases.
    - ``metadata`` (the signal metadata): ``tp1_price`` (> 0) and
      ``tp1_exit_percent`` — a *fraction* in (0, 1] despite its name, as the
      RamJet processor emits it (0.5 = half the position).
    - ``action`` must be ``buy``: the workflow (green activates, red exits) is
      defined for long positions only.
    - ``take_profit``, when set, must equal ``tp1_price``. The workflow has no
      full-position take profit; a different level would be ambiguous.
    """
    if not isinstance(trailing_cfg, dict):
        raise ValueError(f"trailing_sl_config in {context} must be a mapping.")
    if normalize_trailing_mode(trailing_cfg.get("mode")) != TRAILING_MODE_SUPERTREND_FLIP:
        raise ValueError(f"trailing_sl_config in {context} is not a supertrend_flip config.")
    if str(action or "").strip().lower() not in {"buy", "long"}:
        raise ValueError(
            f"supertrend_flip in {context} is defined for long entries only, got action "
            f"{action!r}."
        )
    if not isinstance(metadata, dict):
        raise ValueError(f"supertrend_flip in {context} requires signal metadata.")

    raw_period = trailing_cfg.get("supertrend_period")
    period_value = _finite_float(raw_period, field="supertrend_period", context=context)
    if period_value < 1 or not period_value.is_integer():
        raise ValueError(
            f"supertrend_flip in {context} requires an integer supertrend_period >= 1, got "
            f"{raw_period!r}."
        )
    multiplier = _finite_float(
        trailing_cfg.get("supertrend_multiplier"), field="supertrend_multiplier", context=context
    )
    if multiplier <= 0:
        raise ValueError(
            f"supertrend_flip in {context} requires supertrend_multiplier > 0, got {multiplier!r}."
        )
    breakeven = trailing_cfg.get("breakeven_after_tp1")
    if not isinstance(breakeven, bool):
        raise ValueError(
            f"supertrend_flip in {context} requires a boolean breakeven_after_tp1, got "
            f"{breakeven!r}."
        )
    for key, expected in (
        ("activation_phase", SUPERTREND_FLIP_ACTIVATION_PHASE),
        ("exit_phase", SUPERTREND_FLIP_EXIT_PHASE),
    ):
        if key in trailing_cfg and trailing_cfg[key] != expected:
            raise ValueError(
                f"supertrend_flip in {context} declares {key}={trailing_cfg[key]!r}; only "
                f"{expected!r} is implemented."
            )

    tp1_price = _finite_float(metadata.get("tp1_price"), field="tp1_price", context=context)
    if tp1_price <= 0:
        raise ValueError(f"supertrend_flip in {context} requires tp1_price > 0, got {tp1_price!r}.")
    fraction = _finite_float(
        metadata.get("tp1_exit_percent"), field="tp1_exit_percent", context=context
    )
    if not (0.0 < fraction <= 1.0):
        raise ValueError(
            f"supertrend_flip in {context} requires tp1_exit_percent as a fraction in (0, 1], "
            f"got {fraction!r}."
        )
    if take_profit is not None and not math.isclose(
        float(take_profit), tp1_price, rel_tol=1e-9, abs_tol=1e-12
    ):
        raise ValueError(
            f"supertrend_flip in {context}: take_profit {take_profit!r} differs from tp1_price "
            f"{tp1_price!r}; the workflow exits only part of the position at TP1 and has no "
            "full-position take profit."
        )
    return SupertrendFlipPlan(
        tp1_price=tp1_price,
        tp1_exit_fraction=fraction,
        breakeven_after_tp1=breakeven,
        supertrend_period=int(period_value),
        supertrend_multiplier=multiplier,
    )
