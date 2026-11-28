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

``supertrend_flip`` was removed (maintainer decision 2026-11-26): the live
workflow and its replay half are gone, and a config that still names the mode
or the ``live_supertrend_flip`` deployment opt-in is refused with a
``ValueError`` that says the mode was removed — never silently ignored.
``REMOVED_TRAILING_MODES`` names those retired modes so the refusal is
explicit rather than an "unknown mode" typo error.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, annotations only
    from shared.models import LiveDeploymentConfig

#: Metadata key a signal processor uses to request trailing-stop behaviour.
TRAILING_SL_CONFIG_METADATA_KEY = "trailing_sl_config"

#: Explicit "initial stop only" — no trailing requested or performed.
TRAILING_MODE_NONE = "none"
#: Stop trails the market price at a fixed relative distance (0 < percent < 1).
TRAILING_MODE_PERCENT = "percent"

#: Every trailing mode this codebase knows. A config value outside this set is
#: a typo/mistake and is rejected wherever it is read.
KNOWN_TRAILING_MODES: frozenset[str] = frozenset(
    {
        TRAILING_MODE_NONE,
        TRAILING_MODE_PERCENT,
    }
)

#: Modes that once existed and were removed. A config naming one is refused
#: with a "removed" message on every path — distinct from an unknown-mode
#: typo so a stale config is diagnosed correctly.
REMOVED_TRAILING_MODES: frozenset[str] = frozenset({"supertrend_flip"})

#: ``LiveDeploymentConfig.settings`` key that used to opt a deployment into the
#: removed live ``supertrend_flip`` workflow. A truthy value is now a
#: configuration error, not an inert flag.
REMOVED_SUPERTREND_FLIP_OPT_IN_KEY = "live_supertrend_flip"

_TRUE_OPT_IN_TOKENS = frozenset({"1", "true", "yes", "on"})

#: Modes the replay backtest adapter (`runner/engines/signal_replay_adapter.py`)
#: executes.
REPLAY_SUPPORTED_TRAILING_MODES: frozenset[str] = frozenset(
    {TRAILING_MODE_NONE, TRAILING_MODE_PERCENT}
)

#: Modes a live adapter runs when it cannot amend a broker-side stop, and the
#: fail-closed answer for an unknown broker: the explicit "no trailing" only.
LIVE_BASELINE_TRAILING_MODES: frozenset[str] = frozenset({TRAILING_MODE_NONE})

#: Modes the live path executes, per broker (``LiveAccountConfig.broker``).
#: - ``alpaca``: ``percent`` is trailed by `live_runner/trailing_stops.py`,
#:   which replaces the protective stop order in place (#167).
#: - ``ctrader``: no trailable mode remains — ``supertrend_flip`` was removed
#:   (maintainer decision 2026-11-26); ``percent`` stays unsupported because
#:   cTrader declares no ``supports_stop_amendment``.
LIVE_SUPPORTED_TRAILING_MODES_BY_BROKER: dict[str, frozenset[str]] = {
    "alpaca": frozenset({TRAILING_MODE_NONE, TRAILING_MODE_PERCENT}),
    "ctrader": frozenset({TRAILING_MODE_NONE}),
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
RISK_ENGINE_SUPPORTED_TRAILING_MODES: frozenset[str] = frozenset(
    {TRAILING_MODE_NONE, TRAILING_MODE_PERCENT}
)


def normalize_trailing_mode(value: Any) -> str:
    """Normalize a configured trailing mode value (strip + lowercase)."""
    return str(value or "").strip().lower()


def validate_no_removed_flip_opt_in(settings: Any, *, context: str) -> None:
    """Refuse a deployment that still opts into the removed flip workflow.

    Only an explicit truthy ``live_supertrend_flip`` value asks for the
    deleted workflow; absent, false, blank, or unrecognized values are inert
    and pass, so a stale key cannot silently change behaviour either way.
    """
    if not isinstance(settings, Mapping):
        return
    value = settings.get(REMOVED_SUPERTREND_FLIP_OPT_IN_KEY)
    if isinstance(value, bool):
        opted_in = value
    elif value is None:
        opted_in = False
    else:
        opted_in = str(value).strip().lower() in _TRUE_OPT_IN_TOKENS
    if opted_in:
        raise ValueError(
            f"Deployment setting '{REMOVED_SUPERTREND_FLIP_OPT_IN_KEY}' in {context} "
            "opts into the 'supertrend_flip' workflow, which was removed; "
            "remove the setting (the mode no longer exists on any path)."
        )


def validate_trailing_mode(
    value: Any,
    *,
    supported: frozenset[str],
    context: str,
) -> str:
    """Validate one trailing mode value and return it normalized.

    Raises ``ValueError`` — with a distinct message for a typo (unknown mode),
    for a removed mode, and for a known mode the executing path does not
    support — so a requested trailing stop is never silently dropped.
    """
    mode = normalize_trailing_mode(value)
    if mode in REMOVED_TRAILING_MODES:
        raise ValueError(
            f"Trailing stop mode {mode!r} in {context} was removed; the "
            f"{mode} workflow no longer exists on any path. Refusing to run "
            "without the trailing stop the configuration requests; remove the "
            "mode from the configuration."
        )
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


def _validate_trailing_mode_for_stage(
    value: Any,
    *,
    broker: Any,
    timeframe: Any,
    context: str,
) -> None:
    """Validate one trailing mode against a broker, and a timeframe when known.

    The two stages of the deployment contract, in one place so every gate that
    reads a deployment's mode decides identically:

    - ``ANY_BROKER`` (model validation, where only the account id is known —
      and the daemon's load path, which must not refuse more than the
      signal-time gates do): the mode must be one that at least one live broker
      executes. The timeframe is unknown there, so it is not gated.
    - a concrete broker (deployment creation, account re-point, YAML parse —
      wherever the account is resolved): the mode must be supported by that
      broker (``None``/unknown brokers fail closed to ``none`` only) and, when
      a timeframe is given, trailable on it.

    A removed mode is refused on both stages by ``validate_trailing_mode``
    before this function's broker logic runs.
    """
    if broker is ANY_BROKER:
        mode = validate_trailing_mode(
            value,
            supported=LIVE_ANY_BROKER_TRAILING_MODES,
            context=context,
        )
    else:
        mode = validate_trailing_mode(
            value,
            supported=live_supported_trailing_modes(broker),
            context=context,
        )
    if broker is not ANY_BROKER and timeframe is not None:
        validate_live_trailing_timeframe(
            mode,
            timeframe,
            supported_timeframes=live_trailing_timeframes(broker),
            context=f"{context} on broker {str(broker or '').strip().lower() or 'unknown'!r}",
        )


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
    where = f"{context} signal_processor_config.trailing_mode"
    _validate_trailing_mode_for_stage(
        processor_config.get("trailing_mode"),
        broker=broker,
        timeframe=timeframe,
        context=where,
    )


def validate_live_deployment_trailing_mode(
    deployment: "LiveDeploymentConfig",
    *,
    context: str,
    broker: Any = ANY_BROKER,
    timeframe: Any = None,
) -> None:
    """Validate the trailing mode a live deployment would run, fail closed.

    #150, the deployment-validation stage: a mode that is unknown, removed, or
    that the live path cannot execute on this broker and timeframe, is
    rejected with a ``ValueError`` before the deployment exists — it is never
    created and then refused signal by signal. Callers that only validate a
    stored deployment (the daemon's load path) catch the error and log it.

    The mode reaches a deployment through one of two places and both are
    checked here:

    - an explicit ``signal_processor_config.trailing_mode`` override
      (:func:`validate_live_deployment_trailing_override`), and
    - the signal processor's own default, read from the processor the
      deployment actually runs — the bot config merged with the deployment's
      overrides, exactly as the daemon builds it. The mode is a setting of the
      *bot* (``signal_bots/<bot>/config.yaml``) and the processor emits it with
      every signal, which is where the issue's defect came from: a bot default
      that live cannot execute used to be invisible until the first signal was
      refused at runtime.

    A deployment without a signal processor, and a processor that declares no
    trailing mode at all (e.g. RocketFuel), pass untouched.

    A deployment still carrying the removed ``live_supertrend_flip`` settings
    opt-in is refused here too: the flag asked for a workflow that no longer
    exists, so leaving it would be a silently-dropped request.
    """
    validate_no_removed_flip_opt_in(
        getattr(deployment, "settings", None), context=context
    )
    validate_live_deployment_trailing_override(
        deployment.signal_config,
        context=context,
        broker=broker,
        timeframe=timeframe,
    )
    processor_name = deployment.signal_processor_name
    if not processor_name:
        return
    processor_config = deployment.signal_config.get("signal_processor_config")
    if isinstance(processor_config, Mapping) and "trailing_mode" in processor_config:
        # The override check above already owns this deployment's mode; building
        # its processor for it would only repeat the work.
        return
    if not isinstance(processor_config, Mapping):
        processor_config = {}
    # Imported here, not at module level: the signal processors import
    # ``shared.trailing``, so a module-level import of the registry would be a
    # cycle (shared.trailing -> signal_bots.registry -> shared.trailing).
    from signal_bots.registry import get_signal_processor

    try:
        processor = get_signal_processor(processor_name, dict(processor_config))
    except Exception as exc:
        # An unregistered id or a bot config that no longer loads is a client
        # error (the API answers 400 with this detail), not a 500; at the
        # daemon's load path it is a configuration error that must not cost the
        # whole deployment list its sync tick.
        raise ValueError(
            f"{context}: its signal processor '{processor_name}' could not be "
            f"built to check the trailing mode it would emit: {exc}"
        ) from exc
    mode = getattr(processor, "trailing_mode", None)
    if mode is None:
        return
    _validate_trailing_mode_for_stage(
        mode,
        broker=broker,
        timeframe=timeframe,
        context=(
            f"{context}: its signal processor '{processor_name}' requests "
            f"trailing mode {normalize_trailing_mode(mode)!r}"
        ),
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
