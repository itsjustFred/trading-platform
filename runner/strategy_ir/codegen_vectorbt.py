"""Deterministic vectorbt strategy code generation from StrategyIR."""

from __future__ import annotations

import re
from pprint import pformat
from typing import Any, NoReturn

from runner.strategy_ir.models import Condition, ExitSignal, IndicatorRef, StrategyIR


def _safe_identifier(raw: str, *, fallback: str, used: set[str]) -> str:
    base = re.sub(r"[^a-zA-Z0-9_]+", "_", raw.strip().lower()).strip("_")
    if not base:
        base = fallback
    if base[0].isdigit():
        base = f"_{base}"

    # Collapse dunder sequences: an IR-provided name must never be able to
    # smuggle dunder attributes (e.g. "__import__") into generated code, not
    # even as an inert identifier.
    while "__" in base:
        base = base.replace("__", "_")

    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _sorted_tags(tags: Any) -> dict[str, Any]:
    if not isinstance(tags, dict):
        return {}
    return {key: tags[key] for key in sorted(tags)}


def _parameters_dict(strategy_ir: StrategyIR) -> dict[str, Any]:
    ordered = sorted(strategy_ir.parameters, key=lambda item: item.name.lower())
    return {parameter.name: parameter.default for parameter in ordered}


def _strategy_meta_from_ir(strategy_ir: StrategyIR) -> dict[str, Any]:
    metadata = strategy_ir.metadata if isinstance(strategy_ir.metadata, dict) else {}
    version = str(metadata.get("version") or "1.0.0")
    tags = _sorted_tags(metadata.get("tags"))
    return {
        "name": strategy_ir.name,
        "version": version,
        "parameters": _parameters_dict(strategy_ir),
        "tags": tags,
    }


def _indicator_defaults(indicator: IndicatorRef) -> tuple[str, int]:
    # C6: explicit window mapping with loud failure. A single positive
    # numeric param is unambiguous; MULTIPLE candidates (e.g. "window" plus
    # "smooth") raise instead of silently picking the alphabetical first.
    # No numeric params fall back to the documented default window=14.
    candidates: list[tuple[str, int]] = []
    for key in sorted(indicator.params):
        value = indicator.params[key]
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            window = int(value)
            if window > 0:
                candidates.append((str(key), window))
    if len(candidates) > 1:
        raise ValueError(
            f"Ambiguous window for indicator {indicator.name!r}: "
            f"{[key for key, _ in candidates]} all look like windows. "
            "Declare the window explicitly instead of relying on guessing."
        )
    if candidates:
        return candidates[0]
    return "window", 14


def _select_signal_roles(
    indicators: list[IndicatorRef],
) -> tuple[IndicatorRef | None, IndicatorRef | None]:
    """Resolve primary/secondary signal roles (C6).

    Explicit `role == "primary"/"secondary"` declarations win; contradictory
    declarations (two primaries, or one indicator as both) fail loudly.
    With no declarations at all, the legacy alphabetical fallback applies
    (documented, not silent — callers see the emitted slugs).
    """
    by_role: dict[str, list[IndicatorRef]] = {}
    for indicator in indicators:
        role = str(indicator.role or "").strip().lower()
        if role in {"primary", "secondary"}:
            by_role.setdefault(role, []).append(indicator)
    primaries = by_role.get("primary", [])
    secondaries = by_role.get("secondary", [])
    if len(primaries) > 1:
        raise ValueError(
            "Multiple indicators declare role 'primary': "
            f"{[item.name for item in primaries]}"
        )
    if len(secondaries) > 1:
        raise ValueError(
            "Multiple indicators declare role 'secondary': "
            f"{[item.name for item in secondaries]}"
        )
    primary = primaries[0] if primaries else None
    secondary = secondaries[0] if secondaries else None
    if primary is not None and secondary is not None and primary is secondary:
        raise ValueError(
            f"Indicator {primary.name!r} declares both primary and secondary roles"
        )
    return primary, secondary


def _untranslatable(text: str, *, source: str) -> NoReturn:
    """F4: unmatched condition text must fail loudly, never fabricate rules."""
    raise ValueError(
        "Cannot deterministically translate condition text into vectorbt logic: "
        f"{source}. Extend the StrategyIR with an explicit expression instead of "
        "relying on keyword guessing."
    )


def _rule_from_text(
    text: str,
    *,
    primary_signal: str,
    secondary_signal: str,
    source: str = "",
) -> str:
    normalized = (text or "").strip().lower()
    # An empty condition is just as untranslatable as prose: falling back to a
    # directional rule here would fabricate trading logic (same rule as F4).
    if not normalized:
        _untranslatable(text, source=source or repr(text))

    if "crossunder" in normalized:
        return f"_cross_under({primary_signal}, {secondary_signal})"
    if "crossover" in normalized:
        return f"_cross_over({primary_signal}, {secondary_signal})"

    if "cross" in normalized and any(token in normalized for token in ("under", "below", "down")):
        return f"_cross_under({primary_signal}, {secondary_signal})"
    if "cross" in normalized and any(token in normalized for token in ("over", "above", "up")):
        return f"_cross_over({primary_signal}, {secondary_signal})"

    if any(token in normalized for token in (">", "above", "over", "greater", "higher")):
        return f"({primary_signal} > {secondary_signal})"
    if any(token in normalized for token in ("<", "below", "under", "less", "lower")):
        return f"({primary_signal} < {secondary_signal})"

    _untranslatable(text, source=source or text)


def _entry_rule_expressions(
    conditions: list[Condition],
    *,
    primary_signal: str,
    secondary_signal: str,
) -> list[str]:
    expressions: list[str] = []
    for condition in conditions:
        expression = _rule_from_text(
            condition.expression,
            primary_signal=primary_signal,
            secondary_signal=secondary_signal,
            source=f"entry condition {condition.expression!r}",
        )
        expressions.append(expression)
    return expressions


def _exit_rule_expressions(
    exit_signals: list[ExitSignal],
    *,
    primary_signal: str,
    secondary_signal: str,
) -> list[str]:
    expressions: list[str] = []
    for signal in exit_signals:
        expression = _rule_from_text(
            signal.condition,
            primary_signal=primary_signal,
            secondary_signal=secondary_signal,
            source=f"exit signal {signal.condition!r}",
        )
        expressions.append(expression)
    return expressions


def generate_vectorbt_strategy_code(strategy_ir: StrategyIR) -> str:
    """Generate deterministic vectorbt-compatible strategy code from StrategyIR."""
    meta = _strategy_meta_from_ir(strategy_ir)
    indicators = sorted(strategy_ir.indicators, key=lambda item: item.name.lower())

    used_identifiers: set[str] = set()
    indicator_specs: list[dict[str, Any]] = []
    for index, indicator in enumerate(indicators, start=1):
        slug = _safe_identifier(indicator.name, fallback=f"indicator_{index}", used=used_identifiers)
        key, default_window = _indicator_defaults(indicator)
        indicator_specs.append(
            {
                "label": indicator.name,
                "slug": slug,
                "window_key": key,
                "window_default": default_window,
                "is_ema": "ema" in indicator.name.lower(),
                "is_rsi": "rsi" in indicator.name.lower(),
            }
        )

    declared_primary, declared_secondary = _select_signal_roles(
        [indicator for indicator in indicators]
    )
    by_name = {spec["label"]: spec for spec in indicator_specs}
    secondary_slug = (
        by_name[declared_secondary.name]["slug"]
        if declared_secondary is not None and declared_secondary.name in by_name
        else None
    )
    primary_signal = "close"
    secondary_signal = "close.shift(1)"
    if declared_primary is not None:
        primary_signal = f"indicator_{by_name[declared_primary.name]['slug']}"
    elif indicator_specs:
        # CodeRabbit round: the fallback must not pick the indicator
        # already assigned to secondary. When every spec IS the secondary
        # indicator, keep the "close" default instead of wiring both
        # roles to the same line.
        first_other = next(
            (spec for spec in indicator_specs if spec["slug"] != secondary_slug),
            None,
        )
        if first_other is not None:
            primary_signal = f"indicator_{first_other['slug']}"
    if declared_secondary is not None:
        secondary_signal = f"indicator_{by_name[declared_secondary.name]['slug']}"
    elif len(indicator_specs) > 1 and declared_primary is None:
        secondary_signal = f"indicator_{indicator_specs[1]['slug']}"

    entry_expressions = _entry_rule_expressions(
        strategy_ir.entry_conditions,
        primary_signal=primary_signal,
        secondary_signal=secondary_signal,
    )
    exit_expressions = _exit_rule_expressions(
        strategy_ir.exit_signals,
        primary_signal=primary_signal,
        secondary_signal=secondary_signal,
    )

    lines: list[str] = [
        '"""Auto-generated deterministic vectorbt strategy from StrategyIR."""',
        "",
        "from __future__ import annotations",
        "",
        "import pandas as pd",
        "",
        f"STRATEGY_META = {pformat(meta, width=100, sort_dicts=False)}",
        "",
        "",
        "def _resolve_window(params: dict, key: str, default: int) -> int:",
        "    try:",
        "        value = int(params.get(key, default))",
        "    except (TypeError, ValueError):",
        "        value = int(default)",
        "    return max(1, value)",
        "",
        "",
        "def _cross_over(left: pd.Series, right: pd.Series) -> pd.Series:",
        "    return ((left > right) & (left.shift(1) <= right.shift(1))).fillna(False)",
        "",
        "",
        "def _cross_under(left: pd.Series, right: pd.Series) -> pd.Series:",
        "    return ((left < right) & (left.shift(1) >= right.shift(1))).fillna(False)",
        "",
        "",
        "def _calc_rsi(series: pd.Series, *, window: int) -> pd.Series:",
        "    delta = series.diff()",
        "    gain = delta.clip(lower=0.0)",
        "    loss = -delta.clip(upper=0.0)",
        "    avg_gain = gain.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()",
        "    avg_loss = loss.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()",
        "    rs = avg_gain / avg_loss.replace(0, pd.NA)",
        "    return (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)",
        "",
        "",
        "def generate_signals(df: pd.DataFrame, params: dict):",
        "    if \"close\" not in df.columns:",
        "        raise ValueError(\"Input DataFrame must include 'close' column.\")",
        "",
        "    close = pd.to_numeric(df[\"close\"], errors=\"coerce\")",
        "    indicators_dict: dict[str, pd.Series] = {}",
    ]

    if indicator_specs:
        for spec in indicator_specs:
            lines.append(
                f"    window_{spec['slug']} = _resolve_window(params, {spec['window_key']!r}, {spec['window_default']})"
            )
            if spec["is_rsi"]:
                lines.append(
                    f"    indicator_{spec['slug']} = _calc_rsi(close, window=window_{spec['slug']})"
                )
            elif spec["is_ema"]:
                lines.append(
                    f"    indicator_{spec['slug']} = close.ewm(span=window_{spec['slug']}, adjust=False).mean()"
                )
            else:
                lines.append(
                    f"    indicator_{spec['slug']} = close.rolling(window=window_{spec['slug']}, min_periods=window_{spec['slug']}).mean()"
                )
            lines.append(
                f"    indicators_dict[{spec['label']!r}] = indicator_{spec['slug']}"
            )
    else:
        lines.extend(
            [
                "    baseline_window = _resolve_window(params, 'window', 14)",
                "    baseline = close.rolling(window=baseline_window, min_periods=baseline_window).mean()",
                "    indicators_dict['baseline'] = baseline",
            ]
        )
        primary_signal = "baseline"
        secondary_signal = "close"

    lines.extend(
        [
            "",
            "    entry_rules: list[pd.Series] = []",
        ]
    )
    if entry_expressions:
        for expression in entry_expressions:
            lines.append(f"    entry_rules.append(({expression}).fillna(False))")
    else:
        lines.append(f"    entry_rules.append(_cross_over({primary_signal}, {secondary_signal}))")

    lines.extend(
        [
            "",
            "    entries = entry_rules[0]",
            "    for rule in entry_rules[1:]:",
            "        entries = entries & rule",
            "    entries = entries.fillna(False).astype(bool)",
            "",
            "    exit_rules: list[pd.Series] = []",
        ]
    )
    if exit_expressions:
        for expression in exit_expressions:
            lines.append(f"    exit_rules.append(({expression}).fillna(False))")
    else:
        lines.append(f"    exit_rules.append(_cross_under({primary_signal}, {secondary_signal}))")

    lines.extend(
        [
            "",
            "    exits = exit_rules[0]",
            "    for rule in exit_rules[1:]:",
            "        exits = exits | rule",
            "    exits = exits.fillna(False).astype(bool)",
            "",
            "    drawings: list[dict[str, object]] = []",
            "    if 'timestamp' in df.columns:",
            "        entry_times = df.loc[entries, 'timestamp']",
            "        for timestamp in entry_times.head(25):",
            "            drawings.append(",
            "                {",
            "                    'type': 'marker',",
            "                    'time': timestamp,",
            "                    'position': 'belowBar',",
            "                    'shape': 'arrowUp',",
            "                    'color': '#16a34a',",
            "                    'text': 'Entry',",
            "                }",
            "            )",
            "",
            "        exit_times = df.loc[exits, 'timestamp']",
            "        for timestamp in exit_times.head(25):",
            "            drawings.append(",
            "                {",
            "                    'type': 'marker',",
            "                    'time': timestamp,",
            "                    'position': 'aboveBar',",
            "                    'shape': 'arrowDown',",
            "                    'color': '#dc2626',",
            "                    'text': 'Exit',",
            "                }",
            "            )",
            "",
            "    if drawings:",
            "        return entries, exits, indicators_dict, drawings",
            "    return entries, exits, indicators_dict",
            "",
        ]
    )

    return "\n".join(lines).rstrip() + "\n"