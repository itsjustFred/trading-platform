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

#: Signal timeframes a live broker can trail on, per broker. Trailing is replayed
#: bar by bar (a bar counts as closed at ``open + length``) and the live manager
#: wakes at UTC-aligned bar boundaries, so only timeframes whose bars the broker
#: stamps on UTC-aligned boundaries qualify. Alpaca stamps equity daily bars at
#: New-York midnight (05:00Z/04:00Z), so ``1d`` would trail a session late versus
#: replay; ``4h``/``1w`` are not served by its market-data adapter at all. Proper
#: session-aligned daily trailing is a follow-up. A broker without an entry here
#: trails on no timeframe.
LIVE_TRAILING_TIMEFRAMES_BY_BROKER: dict[str, frozenset[str]] = {
    "alpaca": frozenset({"1m", "5m", "15m", "30m", "1h"}),
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
    mode = validate_trailing_mode(
        trailing_cfg.get("mode"),
        supported=supported,
        context=context,
    )
    if mode == TRAILING_MODE_PERCENT:
        # Checked here, not only in the engine: the replay gate must refuse a
        # bad percent before any bar is traded, not fail later mid-run.
        validate_trailing_percent(trailing_cfg, context=context)
    return mode


def validate_trailing_percent(trailing_cfg: dict[str, Any], *, context: str) -> float:
    """Return the ``percent`` of a ``percent``-mode config, or raise ``ValueError``.

    Requires a finite number strictly between 0 and 1. NaN and infinities are
    rejected explicitly: every comparison with NaN is False, so a NaN percent
    would otherwise make the trailing candidate NaN and the stop never move.
    """
    raw = trailing_cfg.get("percent")
    try:
        percent = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"trailing_sl_config.percent in {context} must be a number between 0 "
            f"and 1 (exclusive), got {raw!r}."
        ) from exc
    if not math.isfinite(percent) or not (0.0 < percent < 1.0):
        raise ValueError(
            f"trailing_sl_config.percent in {context} must be a finite number "
            f"between 0 and 1 (exclusive), got {raw!r}."
        )
    return percent


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
    timeframe: Any = None,
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
    mode = validate_trailing_mode(
        processor_config.get("trailing_mode"),
        supported=supported,
        context=where,
    )
    # With a broker AND a timeframe (the stages that know both) the mode must
    # also be trailable on that timeframe.
    if broker is not ANY_BROKER and timeframe is not None:
        validate_live_trailing_timeframe(
            mode,
            timeframe,
            supported_timeframes=live_trailing_timeframes(broker),
            context=f"{context} on broker {str(broker or '').strip().lower() or 'unknown'!r}",
        )


def live_trailing_timeframes(broker: Any) -> frozenset[str]:
    """Signal timeframes the live path can trail on for one broker (fail closed: none)."""
    normalized = str(broker or "").strip().lower()
    return LIVE_TRAILING_TIMEFRAMES_BY_BROKER.get(normalized, frozenset())


def validate_live_trailing_timeframe(
    mode: Any,
    timeframe: Any,
    *,
    supported_timeframes: frozenset[str],
    context: str,
) -> None:
    """Refuse a trailing mode (other than ``none``) on a timeframe live cannot trail.

    #150: an unservable trail must be refused at entry, never traded untrailed.
    ``mode`` is the already-validated trailing mode (``None`` means no trailing).
    """
    if mode is None or normalize_trailing_mode(mode) == TRAILING_MODE_NONE:
        return
    normalized = str(timeframe or "").strip().lower()
    if normalized not in supported_timeframes:
        allowed = ", ".join(sorted(supported_timeframes)) or "none"
        raise ValueError(
            f"{context}: trailing mode {normalize_trailing_mode(mode)!r} cannot be "
            f"executed live on timeframe {timeframe!r} (trailable timeframes: {allowed}); "
            "refusing to trade it without the requested trailing stop"
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

    1. TP1: exit the whole-share tranche
       :func:`tp1_partial_quantity`(entry quantity, ``tp1_exit_fraction``) at
       ``tp1_price``. A tranche of zero whole shares is skipped entirely (the
       original bot's behaviour): the whole position stays under the original
       stop and the workflow below never runs.
    2. After TP1: move the stop to the entry price if ``breakeven_after_tp1``.
    3. Activation: the first closed bar AFTER the TP1 bar whose Supertrend is
       ready and green. The TP1 bar after the TP1 fill is dead for the
       workflow: nothing that happens at or inside it may move or trigger
       the trail.
    4. Exit: the remainder on the first closed bar after activation whose
       Supertrend is ready and red; filled at the next bar's open.
    """

    tp1_price: float
    tp1_exit_fraction: float
    breakeven_after_tp1: bool
    supertrend_period: int
    supertrend_multiplier: float


def tp1_partial_quantity(quantity: float, fraction: float) -> float:
    """Whole-share TP1 tranche: ``quantity * fraction`` floored to an integer.

    The original bot sizes its TP1 exit as ``int(qty * tp1_exit_percent)``
    (``RamJetStrategy.compute_tp1_qty`` in ``stockkit_ai_bot``): a whole-share
    quantity that is floored, never rounded up. The replay mirrors it so a
    backtest never sells more shares than the bot would, and so a live TP1
    tranche order — which must be whole shares too (``Alpaca`` refuses
    fractional bracket quantities) — matches the replayed one.

    ``0.0`` is a meaningful answer, not an error: the bot then refuses to sell
    anything at TP1 ("No shares to sell at TP1. Skipping."), keeps the whole
    position and never arms the trailing workflow. Callers must handle it.
    """
    shares = float(quantity) * float(fraction)
    if not math.isfinite(shares) or shares <= 0.0:
        return 0.0
    return float(math.floor(shares))


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


def _validate_flip_config(trailing_cfg: Any, *, context: str) -> tuple[int, float, bool]:
    """Return ``(supertrend_period, supertrend_multiplier, breakeven_after_tp1)``
    from a ``supertrend_flip`` config, or raise ``ValueError``."""
    if not isinstance(trailing_cfg, dict):
        raise ValueError(f"trailing_sl_config in {context} must be a mapping.")
    if normalize_trailing_mode(trailing_cfg.get("mode")) != TRAILING_MODE_SUPERTREND_FLIP:
        raise ValueError(f"trailing_sl_config in {context} is not a supertrend_flip config.")

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
    return int(period_value), multiplier, breakeven


def parse_supertrend_flip_plan(
    trailing_cfg: Any,
    metadata: Any,
    *,
    action: str,
    take_profit: float | None,
    stop_loss: float | None,
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
    - ``stop_loss`` must be a finite positive stop: before TP1 it is the only
      protection of the position.
    - ``take_profit``, when set, must equal ``tp1_price``. The workflow has no
      full-position take profit; a different level would be ambiguous.
    """
    if str(action or "").strip().lower() not in {"buy", "long"}:
        raise ValueError(
            f"supertrend_flip in {context} is defined for long entries only, got action "
            f"{action!r}."
        )
    if not isinstance(metadata, dict):
        raise ValueError(f"supertrend_flip in {context} requires signal metadata.")
    period, multiplier, breakeven = _validate_flip_config(trailing_cfg, context=context)

    if stop_loss is None:
        raise ValueError(f"supertrend_flip in {context} requires a stop_loss.")
    _finite_float(stop_loss, field="stop_loss", context=context)
    if float(stop_loss) <= 0:
        raise ValueError(f"supertrend_flip in {context} requires stop_loss > 0, got {stop_loss!r}.")

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
        supertrend_period=period,
        supertrend_multiplier=multiplier,
    )
