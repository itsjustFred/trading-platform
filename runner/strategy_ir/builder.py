"""IR builder for the incremental StrategyIR seam.

This module translates the current LLM outputs (markdown + STRATEGY_META)
into a typed StrategyIR artifact without changing the legacy codegen/backtest path.
"""

from __future__ import annotations

import re
from typing import Any

from runner.strategy_ir.models import (
    Condition,
    ConditionWhen,
    EngineHint,
    ExitSignal,
    ExitSignalKind,
    IndicatorRef,
    ParameterSpec,
    ParameterValueType,
    StrategyIR,
)


def _extract_section(markdown_doc: str, heading: str) -> str:
    pattern = rf"###\s*{re.escape(heading)}\s*\n(.*?)(?=\n###|\n##|\Z)"
    match = re.search(pattern, markdown_doc or "", flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()


def _section_lines(section_text: str) -> list[str]:
    lines: list[str] = []
    for raw in section_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        lines.append(line)
    return lines


def _coerce_scalar(value: str) -> Any:
    text = value.strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"none", "null"}:
        return None

    if text[0] == text[-1] and text[0] in {"'", '"'} and len(text) >= 2:
        return text[1:-1]

    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _infer_parameter_type(value: Any) -> tuple[ParameterValueType, list[Any]]:
    if isinstance(value, bool):
        return ParameterValueType.BOOL, []
    if isinstance(value, int) and not isinstance(value, bool):
        return ParameterValueType.INT, []
    if isinstance(value, float):
        return ParameterValueType.FLOAT, []
    if isinstance(value, str):
        return ParameterValueType.STRING, []
    if isinstance(value, (list, tuple)):
        return ParameterValueType.CHOICE, list(value)
    return ParameterValueType.ANY, []


def _parameters_from_meta(strategy_meta: dict[str, Any]) -> list[ParameterSpec]:
    payload = strategy_meta.get("parameters", {})
    if not isinstance(payload, dict):
        return []

    specs: list[ParameterSpec] = []
    for name, default in payload.items():
        parameter_name = str(name).strip()
        if not parameter_name:
            continue
        value_type, choices = _infer_parameter_type(default)
        spec = ParameterSpec(
            name=parameter_name,
            value_type=value_type,
            default=default,
            choices=choices,
        )
        specs.append(spec)

    return specs


def _indicator_rows_from_markdown(markdown_doc: str) -> list[tuple[str, str, Any]]:
    section_match = re.search(
        r"##\s*Indikator-Einstellungen\s*\n(.*?)(?=\n##|\Z)",
        markdown_doc or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not section_match:
        return []

    section = section_match.group(1)
    rows: list[tuple[str, str, Any]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if set(stripped.replace("|", "").strip()) <= {"-", ":"}:
            continue

        columns = [part.strip() for part in stripped.strip("|").split("|")]
        if len(columns) < 3:
            continue
        indicator, parameter, raw_value = columns[0], columns[1], columns[2]
        if indicator.lower() == "indikator" or parameter.lower() == "parameter":
            continue
        rows.append((indicator, parameter, _coerce_scalar(raw_value)))

    return rows


def _indicators_from_markdown_and_source(markdown_doc: str, source_code: str) -> list[IndicatorRef]:
    by_name: dict[str, dict[str, Any]] = {}

    for indicator_name, parameter_name, parameter_value in _indicator_rows_from_markdown(markdown_doc):
        name = indicator_name.strip()
        if not name:
            continue
        ref = by_name.setdefault(name, {"name": name, "params": {}})
        key = parameter_name.strip()
        if key and key.lower() != "-":
            ref["params"][key] = parameter_value

    if not by_name:
        for token in re.findall(r"\b(?:ta|vbt)\.([A-Za-z_][A-Za-z0-9_]*)", source_code or ""):
            name = token.strip()
            if not name:
                continue
            by_name.setdefault(name, {"name": name, "params": {}})

    return [
        IndicatorRef(
            name=item["name"],
            params=item["params"],
            role="signal",
            source="markdown" if item["params"] else "source",
        )
        for item in by_name.values()
    ]


def _entry_conditions_from_markdown(markdown_doc: str) -> list[Condition]:
    section = _extract_section(markdown_doc, "Entry")
    lines = _section_lines(section)
    return [
        Condition(expression=line, description=line, when=ConditionWhen.ENTRY)
        for line in lines
    ]


def _infer_exit_kind(description: str) -> ExitSignalKind:
    lowered = description.lower()
    if "stop" in lowered and "trail" in lowered:
        return ExitSignalKind.TRAILING_STOP
    if "stop" in lowered:
        return ExitSignalKind.STOP_LOSS
    if "take profit" in lowered or "tp" in lowered:
        return ExitSignalKind.TAKE_PROFIT
    if "time" in lowered:
        return ExitSignalKind.TIME_EXIT
    if "close" in lowered or "exit" in lowered:
        return ExitSignalKind.CLOSE
    return ExitSignalKind.CUSTOM


def _exit_signals_from_markdown(markdown_doc: str) -> list[ExitSignal]:
    section = _extract_section(markdown_doc, "Exit")
    lines = _section_lines(section)
    return [
        ExitSignal(
            name=f"exit_{index}",
            condition=line,
            kind=_infer_exit_kind(line),
        )
        for index, line in enumerate(lines, start=1)
    ]


def _engine_hint(engine: str) -> EngineHint:
    normalized = (engine or "").strip().lower()
    if normalized == EngineHint.VECTORBT.value:
        return EngineHint.VECTORBT
    if normalized == EngineHint.PYNECORE.value:
        return EngineHint.PYNECORE
    if normalized == EngineHint.LEAN.value:
        return EngineHint.LEAN
    return EngineHint.UNKNOWN


def build_strategy_ir(
    *,
    strategy_slug: str,
    engine: str,
    source_code: str,
    markdown_doc: str,
    strategy_meta: dict[str, Any],
) -> StrategyIR:
    """Build a typed StrategyIR artifact from current conversion stage outputs."""
    parameters = _parameters_from_meta(strategy_meta)
    indicators = _indicators_from_markdown_and_source(markdown_doc, source_code)
    entry_conditions = _entry_conditions_from_markdown(markdown_doc)
    exit_signals = _exit_signals_from_markdown(markdown_doc)

    confidence = 0.0
    if parameters:
        confidence += 0.25
    if indicators:
        confidence += 0.25
    if entry_conditions:
        confidence += 0.25
    if exit_signals:
        confidence += 0.25

    return StrategyIR(
        name=strategy_meta.get("name") or strategy_slug,
        parameters=parameters,
        indicators=indicators,
        entry_conditions=entry_conditions,
        exit_signals=exit_signals,
        confidence=confidence,
        fallback_code="",
        engine_hint=_engine_hint(engine),
        source_summary=(markdown_doc or "").splitlines()[0].strip() if markdown_doc else "",
        metadata={
            "strategy_slug": strategy_slug,
            "version": strategy_meta.get("version", "1.0.0"),
            "tags": strategy_meta.get("tags", {}),
        },
    )
