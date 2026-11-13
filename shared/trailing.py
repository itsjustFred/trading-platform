"""Central trailing-stop mode contract (issue #150).

One definition of the trailing modes that exist and of which executing path
supports which mode. Everything downstream (risk engine, signal-bot
processors, replay adapter, live runner) validates against these constants
instead of hard-coding its own list.

Fail closed: a mode that is unknown, or that the executing path cannot
execute, is rejected with a ``ValueError`` — at configuration/deployment
validation time and again at signal time. There is no path where a requested
trailing mode is dropped with only a log line while the trade happens anyway.

Follow-ups extend the support sets in one place:
- #167 makes the live path trail (extends ``LIVE_SUPPORTED_TRAILING_MODES``).
- #168 implements ``supertrend_flip`` (extends the replay/live/engine sets).
"""
from __future__ import annotations

from typing import Any

#: Metadata key a signal processor uses to request trailing-stop behaviour.
TRAILING_SL_CONFIG_METADATA_KEY = "trailing_sl_config"

#: Explicit "initial stop only" — no trailing requested or performed.
TRAILING_MODE_NONE = "none"
#: Stop trails the market price at a fixed relative distance (0 < percent < 1).
TRAILING_MODE_PERCENT = "percent"
#: Two-phase Supertrend-flip workflow (breakeven after TP1, flip exit). Known
#: but not implemented anywhere yet — see #168.
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
#: executes.
REPLAY_SUPPORTED_TRAILING_MODES: frozenset[str] = frozenset(
    {TRAILING_MODE_NONE, TRAILING_MODE_PERCENT}
)

#: Modes the live path executes. Live trailing does not exist yet (#167): the
#: live path never calls ``RiskEngine.apply_trailing_stop``, so only the
#: explicit "no trailing" mode is supported.
LIVE_SUPPORTED_TRAILING_MODES: frozenset[str] = frozenset({TRAILING_MODE_NONE})

#: Modes ``RiskEngine.apply_trailing_stop`` can execute. This is the engine's
#: own executable set (a backstop behind the per-path gates), not a path's.
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


def validate_live_deployment_trailing_override(
    signal_config: Any,
    *,
    context: str,
) -> None:
    """Validate a live deployment's trailing-mode override, if it sets one.

    A deployment selects the trailing mode through its signal-processor
    config override (``signal_config.signal_processor_config.trailing_mode``).
    The live path supports ``none`` only (#167), so any other value is a
    deployment-configuration error and raises ``ValueError``. Deployments that
    do not set the override inherit the processor's own (validated) default.
    """
    if not isinstance(signal_config, dict):
        return
    processor_config = signal_config.get("signal_processor_config")
    if not isinstance(processor_config, dict) or "trailing_mode" not in processor_config:
        return
    validate_trailing_mode(
        processor_config.get("trailing_mode"),
        supported=LIVE_SUPPORTED_TRAILING_MODES,
        context=f"{context} signal_processor_config.trailing_mode",
    )
