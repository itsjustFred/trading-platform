"""Indicator registry skeleton (issue #317, phase P0).

``@register_indicator`` will give custom indicators (WaveTrend, the
supertrend oscillator, future ones) a single declaration point: params
schema, warm-up length and display hint (panel/overlay). In P4 the
strategy-IR codegen and ``runner/engines/_indicator_series.py`` will
consume this registry instead of name-sniffing; P0 only fixes the
registration mechanics and their fail-closed validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class IndicatorSpec:
    """Registered custom indicator.

    - ``name``: registry key, e.g. ``"wavetrend"``.
    - ``version``: bump on any output-affecting change; consumers pin it.
    - ``params``: mapping of parameter name -> JSON-schema-ish spec
      (``{"type": "int", "default": ..., "min": ..., "max": ...}``).
    - ``warmup_bars``: number of leading bars whose output is not yet
      meaningful (must equal what the implementation produces).
    - ``display``: ``"overlay"`` (price pane) or ``"panel"`` (own pane).
    - ``compute``: ``fn(df, params) -> dict[str, pd.Series]``.
    """

    name: str
    version: int
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    warmup_bars: int = 0
    display: str = "panel"
    compute: Callable[..., Any] | None = None


_REGISTRY: dict[tuple[str, int], IndicatorSpec] = {}


def register_indicator(
    name: str,
    *,
    version: int = 1,
    params: dict[str, dict[str, Any]] | None = None,
    warmup_bars: int = 0,
    display: str = "panel",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``fn`` as custom indicator ``name`` at ``version``.

    Re-registering the same ``(name, version)`` with a different function
    raises ``ValueError`` — a silent overwrite would let two code paths
    disagree about what ``name`` means.
    """
    if display not in {"overlay", "panel"}:
        raise ValueError(f"display must be 'overlay' or 'panel', got {display!r}")
    if version < 1:
        raise ValueError(f"version must be >= 1, got {version}")
    if warmup_bars < 0:
        raise ValueError(f"warmup_bars must be >= 0, got {warmup_bars}")

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        key = (name, version)
        existing = _REGISTRY.get(key)
        if existing is not None and existing.compute is not fn:
            raise ValueError(
                f"indicator {name!r} version {version} is already registered"
            )
        _REGISTRY[key] = IndicatorSpec(
            name=name,
            version=version,
            params=dict(params or {}),
            warmup_bars=warmup_bars,
            display=display,
            compute=fn,
        )
        return fn

    return decorator


def get_indicator(name: str, version: int | None = None) -> IndicatorSpec:
    """Look up a registered indicator; latest version when omitted."""
    if version is not None:
        try:
            return _REGISTRY[(name, version)]
        except KeyError as exc:
            raise KeyError(f"no indicator {name!r} version {version}") from exc
    versions = [v for (n, v) in _REGISTRY if n == name]
    if not versions:
        raise KeyError(f"no indicator named {name!r}")
    return _REGISTRY[(name, max(versions))]


def list_indicators() -> list[IndicatorSpec]:
    """All registered indicators, sorted by ``(name, version)``."""
    return [ _REGISTRY[k] for k in sorted(_REGISTRY) ]
