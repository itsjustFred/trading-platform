"""
Autonomous strategy-conversion worker with pluggable LLM providers.

This module converts raw strategy code into a runnable repository strategy using
three smaller LLM stages (analyst, architect, coder), writes the generated
Python + Markdown artifacts into per-strategy folders, and validates the result
by launching a backtest through runner.cli. If validation fails, only the coder
stage is retried with the latest error context.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pprint import pformat
from typing import Any, Callable

from data_tools.catalog_builder import scan_ohlcv
from runner.cli import _detect_date_range
from runner.llm_providers import (
    DEFAULT_LLM_PROVIDER_NAME,
    SUPPORTED_LLM_PROVIDER_NAMES,
    get_llm_provider,
)
from runner.llm_providers.base import LLMProvider
from shared.constants import DATA_DIR
from shared.logging import get_logger
from shared.strategy_registry import DEFAULT_STRATEGIES_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR
PROMPT_PATH = PROJECT_ROOT / ".github" / "agents" / "strategy-conversion.md"
BACKTESTS_DIR = DATA_DIR / "backtests"

DEFAULT_MODEL_NAME = "moonshotai/kimi-k2.5"
SUPPORTED_MODEL_NAMES = (
    DEFAULT_MODEL_NAME,
    "meta/llama-3.3-70b-instruct",
    "deepseek-ai/deepseek-v3.2",
    "qwen/qwen2.5-coder-32b-instruct",
)
MAX_ITERATIONS = 3
DEFAULT_CAPITAL = 100_000.0
DEFAULT_RISK_PCT = 0.01
ERROR_CONTEXT_LIMIT = 8_000
OPENAI_TIMEOUT_SECONDS = 600.0
LONG_CODE_LINE_THRESHOLD = 1000

TIMEFRAME_RANK = {
    "1h": 0,
    "4h": 1,
    "1d": 2,
    "30m": 3,
    "15m": 4,
    "5m": 5,
    "1m": 6,
}
ASSET_CLASS_RANK = {
    "equities": 0,
    "equity": 0,
    "etf": 1,
    "etfs": 1,
    "forex": 2,
    "indices": 3,
    "crypto": 4,
}
SYMBOL_RANK = {
    "AAPL": 0,
    "SPY": 1,
    "MSFT": 2,
    "QQQ": 3,
    "EURUSD": 4,
    "XAUUSD": 5,
    "BTCUSD": 6,
}

TaskLogger = Callable[..., None]
logger = get_logger("auto-agent")


@dataclass
class BacktestTarget:
    symbols: list[str]
    timeframe: str
    start: str
    end: str


@dataclass(frozen=True)
class StrategyArtifacts:
    resolved_name: str
    engine: str
    strategy_dir: Path
    strategy_path: Path
    documentation_path: Path
    output_path: Path


def run_auto_agent(
    code: str,
    *,
    strategy_name: str | None = None,
    requested_engine: str | None = None,
    provider: str = DEFAULT_LLM_PROVIDER_NAME,
    model: str | None = None,
    symbols: list[str] | None = None,
    timeframe: str | None = None,
    start: str | None = None,
    end: str | None = None,
    capital: float = DEFAULT_CAPITAL,
    max_iterations: int = MAX_ITERATIONS,
    timeout_seconds: float = OPENAI_TIMEOUT_SECONDS,
    log: TaskLogger | None = None,
) -> dict[str, str | int | list[str]]:
    """Convert raw strategy code into a runnable strategy and validate it via backtest."""
    source_code = (code or "").strip()
    if not source_code:
        raise ValueError("Strategy source code is required.")

    selected_provider = str(provider or DEFAULT_LLM_PROVIDER_NAME).strip().lower() or DEFAULT_LLM_PROVIDER_NAME
    if selected_provider not in SUPPORTED_LLM_PROVIDER_NAMES:
        raise ValueError(f"Unsupported provider: {selected_provider}")

    selected_model = (model or "").strip()
    if selected_provider == "nim":
        selected_model = selected_model or DEFAULT_MODEL_NAME
        if selected_model not in SUPPORTED_MODEL_NAMES:
            raise ValueError(f"Unsupported model: {selected_model}")
    elif not selected_model:
        selected_model = None

    line_count = source_code.count("\n") + 1
    if line_count >= LONG_CODE_LINE_THRESHOLD:
        message = f"Code is very long ({line_count} lines). Expect longer generation time."
        logger.info(message, extra={"data": {"line_count": line_count, "timeout_seconds": timeout_seconds}})
        _log(log, message)

    target = _resolve_backtest_target(
        symbols=symbols,
        timeframe=timeframe,
        start=start,
        end=end,
    )
    _log(
        log,
        (
            f"Resolved validation target: symbols={','.join(target.symbols)} "
            f"timeframe={target.timeframe} range={target.start}..{target.end}"
        ),
    )
    provider_instance = get_llm_provider(
        selected_provider,
        timeout_seconds=float(timeout_seconds),
    )
    _log(
        log,
        f"Selected LLM provider: {selected_provider}"
        + (f" (model: {selected_model})" if selected_model else " (default model)"),
    )

    system_prompt = _load_system_prompt()

    _log(log, f"Step 1/3: Requesting Markdown documentation from {_provider_step_target(selected_provider, selected_model)}.")
    markdown_response = _run_llm_step(
        provider_instance,
        model=selected_model,
        developer_prompt=_step_one_developer_prompt(requested_engine=requested_engine),
        user_prompt=_step_one_user_prompt(source_code=source_code, strategy_name=strategy_name),
        log=log,
        step_label="Step 1/3",
    )
    markdown_doc = _normalize_markdown_doc(markdown_response, strategy_name=strategy_name)
    resolved_name = _resolve_strategy_name(markdown_doc, requested_name=strategy_name)
    engine = _extract_engine(markdown_doc, requested_engine=requested_engine)
    artifacts = _build_strategy_artifacts(
        resolved_name=resolved_name,
        engine=engine,
    )
    artifacts.strategy_dir.mkdir(parents=True, exist_ok=True)
    BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)
    artifacts.documentation_path.write_text(markdown_doc.rstrip() + "\n", encoding="utf-8")
    _log(log, f"Wrote documentation file: {artifacts.documentation_path}")
    _log(log, f"Selected engine: {engine}")
    _log(log, "✅ Step 1/3: Markdown Documentation created.")

    _log(log, f"Step 2/3: Requesting STRATEGY_META from {_provider_step_target(selected_provider, selected_model)}.")
    meta_response = _run_llm_step(
        provider_instance,
        model=selected_model,
        developer_prompt=_step_two_developer_prompt(),
        user_prompt=_step_two_user_prompt(
            source_code=source_code,
            markdown_doc=markdown_doc,
            strategy_name=resolved_name,
        ),
        log=log,
        step_label="Step 2/3",
    )
    meta_source, meta_dict = _extract_strategy_meta(
        meta_response,
        requested_name=strategy_name,
        markdown_doc=markdown_doc,
    )
    _log(log, f"Step 2/3: Strategy Meta includes {len(meta_dict.get('parameters', {}))} parameters.")
    _log(log, "✅ Step 2/3: Strategy Meta defined.")

    last_error = "Unknown auto-agent failure."
    for attempt in range(1, max_iterations + 1):
        if attempt == 1:
            _log(log, f"Step 3/3: Requesting Python code from {_provider_step_target(selected_provider, selected_model)}.")
        else:
            _log(
                log,
                f"Step 3/3: Retry {attempt}/{max_iterations} with validation feedback using "
                f"{_provider_step_target(selected_provider, selected_model)}.",
            )

        python_response = _run_llm_step(
            provider_instance,
            model=selected_model,
            developer_prompt=_step_three_developer_prompt(system_prompt=system_prompt, engine=engine),
            user_prompt=_step_three_user_prompt(
                source_code=source_code,
                markdown_doc=markdown_doc,
                meta_source=meta_source,
                engine=engine,
                attempt_error=None if attempt == 1 else last_error,
            ),
            log=log,
            step_label="Step 3/3",
        )
        python_body = _extract_python_response(python_response)
        python_code = _assemble_strategy_module(meta_source, python_body)
        artifacts.strategy_path.write_text(python_code.rstrip() + "\n", encoding="utf-8")
        _log(log, f"Wrote strategy file: {artifacts.strategy_path}")
        if attempt == 1:
            _log(log, "✅ Step 3/3: Python Code generated.")
        else:
            _log(log, f"✅ Step 3/3: Python Code regenerated (attempt {attempt}/{max_iterations}).")

        cmd = _build_backtest_command(
            strategy_path=artifacts.strategy_path,
            engine=engine,
            target=target,
            output_path=artifacts.output_path,
            capital=capital,
        )
        returncode, combined_output = _run_backtest(cmd, log=log)
        if returncode == 0 and artifacts.output_path.exists():
            _log(log, f"✅ Validation loop: Backtest succeeded. Output saved to {artifacts.output_path}")
            return {
                "strategy_file": str(artifacts.strategy_path),
                "documentation_file": str(artifacts.documentation_path),
                "backtest_output": str(artifacts.output_path),
                "engine": engine,
                "symbols": target.symbols,
                "timeframe": target.timeframe,
                "attempts": attempt,
                "provider": selected_provider,
                "model": selected_model or "",
            }

        last_error = combined_output or "Backtest failed without output."
        _log(log, f"Iteration {attempt} failed: {last_error}", level="error")

    raise RuntimeError(f"Auto-agent failed after {max_iterations} iterations: {last_error}")


def _load_system_prompt() -> str:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"System prompt not found: {PROMPT_PATH}")
    return PROMPT_PATH.read_text(encoding="utf-8")


def _build_strategy_artifacts(*, resolved_name: str, engine: str) -> StrategyArtifacts:
    strategy_dir = STRATEGIES_DIR / resolved_name
    return StrategyArtifacts(
        resolved_name=resolved_name,
        engine=engine,
        strategy_dir=strategy_dir,
        strategy_path=strategy_dir / f"{resolved_name}.py",
        documentation_path=strategy_dir / "README.md",
        output_path=BACKTESTS_DIR
        / f"{resolved_name}_auto_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.trades.json",
    )


def _step_one_developer_prompt(*, requested_engine: str | None) -> str:
    engine_line = (
        f"Wenn ein Framework explizit vorgegeben wurde, dokumentiere es in '## Framework-Empfehlung' als {requested_engine}."
        if requested_engine
        else "Füge einen Abschnitt '## Framework-Empfehlung' mit genau einem Framework aus vectorbt, pynecore oder lean hinzu."
    )
    return (
        "Du bist The Analyst. Antworte ausschliesslich mit Markdown, ohne Erklaerungen ausserhalb der Dokumentation. "
        "Dokumentiere die Strategie strukturiert fuer dieses Repository. "
        "Die Doku muss mindestens folgende Abschnitte enthalten: '# <Name>', '## Quelle', '## Kernlogik', "
        "'### Entry', '### Exit', '### Stop-Loss', optional '### Trailing Stop'/'### Partial Exits', "
        "'## Indikator-Einstellungen', '## Risk-Management', '## Framework-Empfehlung'. "
        f"{engine_line}"
    )


def _step_one_user_prompt(*, source_code: str, strategy_name: str | None) -> str:
    name_line = f"Bevorzugter Strategie-Name: {strategy_name}\n" if strategy_name else ""
    return (
        "Analysiere diesen Trading-Code. Extrahiere Entry/Exit-Logik, Indikatoren und Risk-Management. "
        "Antworte AUSSCHLIESSLICH mit einer strukturierten Markdown-Dokumentation.\n"
        f"{name_line}"
        "Quellcode:\n"
        "```text\n"
        f"{source_code}\n"
        "```"
    )


def _step_two_developer_prompt() -> str:
    return (
        "Du bist The Architect. Antworte ausschliesslich mit genau einer Python-Zuweisung 'STRATEGY_META = {...}'. "
        "Die Antwort darf keine weiteren Erklaerungen enthalten. Das Dictionary muss mindestens die Schluessel "
        "name, version, parameters und tags enthalten. In parameters muessen alle aus dem Quellcode ableitbaren "
        "tunebaren Parameter mit sinnvollen Default-Werten enthalten sein."
    )


def _step_two_user_prompt(
    *,
    source_code: str,
    markdown_doc: str,
    strategy_name: str,
) -> str:
    return (
        "Basierend auf der Doku: Generiere ausschliesslich das Python `STRATEGY_META` Dictionary fuer diese Strategie, "
        "inklusive aller benoetigten Parameter in `parameters`.\n"
        f"Strategie-Slug: {strategy_name}\n\n"
        "Markdown-Dokumentation:\n"
        "```markdown\n"
        f"{markdown_doc}\n"
        "```\n\n"
        "Original-Quellcode:\n"
        "```text\n"
        f"{source_code}\n"
        "```"
    )


def _step_three_developer_prompt(*, system_prompt: str, engine: str) -> str:
    engine_guidance = (
        "Fuer vectorbt muss generate_signals(df, params) mindestens (entries, exits, indicators_dict) "
        "und optional als viertes Element drawings zurueckgeben. "
        "Alle logikrelevanten Entry-/Exit-/Stop-/Trailing-Indikatoren muessen in indicators_dict landen."
        if engine == "vectorbt"
        else (
            "Halte dich strikt an den Contract des gewaehlten Frameworks. Wenn pynecore genutzt wird, liefere bei Bedarf "
            "zusaetzlich build_trade_context(df, params) und optional build_drawings(df, params, trades, symbol), "
            "damit Trade-Meta und Chart-Drawings angereichert werden koennen."
        )
    )
    return (
        f"{system_prompt}\n\n"
        "Du bist The Coder. Antworte ausschliesslich mit Python-Code oder einem ```python```-Block. "
        "Keine Markdown-Erklaerungen. STRATEGY_META wird separat injiziert; definiere STRATEGY_META NICHT erneut. "
        "Nutze nur Bibliotheken, die in diesem Repository bereits installiert sind. "
        f"Gewaehltes Framework: {engine}. {engine_guidance}"
    )


def _step_three_user_prompt(
    *,
    source_code: str,
    markdown_doc: str,
    meta_source: str,
    engine: str,
    attempt_error: str | None,
) -> str:
    repair_section = ""
    if attempt_error:
        repair_section = (
            "\n\nDer vorherige Backtest-/Validierungsversuch ist mit folgendem Fehler fehlgeschlagen. "
            "Korrigiere NUR den Python-Code. Markdown und STRATEGY_META bleiben unveraendert.\n"
            "```text\n"
            f"{_truncate(attempt_error, ERROR_CONTEXT_LIMIT)}\n"
            "```"
        )
    return (
        "Schreibe nun den vollstaendigen Python Code (mit imports). Die Hauptfunktion MUSS `entries, exits, indicators_dict` "
        "und optional `drawings` als viertes Rueckgabe-Element liefern, wenn das Framework vectorbt ist. "
        "Halte dich strikt an die Logik im Markdown.\n"
        f"Framework: {engine}\n\n"
        "Verwende dieses STRATEGY_META unveraendert:\n"
        "```python\n"
        f"{meta_source}\n"
        "```\n\n"
        "Markdown Ground Truth:\n"
        "```markdown\n"
        f"{markdown_doc}\n"
        "```\n\n"
        "Original-Quellcode:\n"
        "```text\n"
        f"{source_code}\n"
        "```"
        f"{repair_section}"
    )


def _run_llm_step(
    provider: LLMProvider,
    *,
    model: str | None,
    developer_prompt: str,
    user_prompt: str,
    log: TaskLogger | None,
    step_label: str,
) -> str:
    request_metadata: dict[str, Any] = {"step_label": step_label}
    content = provider.generate(
        prompt=user_prompt,
        system_prompt=developer_prompt,
        model=model,
        metadata=request_metadata,
    )
    provider_name = str(request_metadata.get("provider") or "")
    provider_model = str(request_metadata.get("model") or "")
    provider_status = str(request_metadata.get("status") or "")
    provider_duration = request_metadata.get("duration_seconds")
    if provider_name or provider_model or provider_status or provider_duration is not None:
        details: list[str] = []
        if provider_name:
            details.append(f"provider={provider_name}")
        if provider_model:
            details.append(f"model={provider_model}")
        if provider_status:
            details.append(f"status={provider_status}")
        if provider_duration is not None:
            details.append(f"duration={provider_duration}s")
        _log(log, f"{step_label}: Provider metadata: {', '.join(details)}.")
    _log(log, f"{step_label}: Received {len(content)} characters from model.")
    if not content.strip():
        raise RuntimeError(f"{step_label}: model returned an empty response.")
    return content


def _provider_step_target(provider: str, model: str | None) -> str:
    if model and model.strip():
        return f"{provider}:{model}"
    return provider


def _normalize_markdown_doc(markdown_doc: str, *, strategy_name: str | None) -> str:
    content = _extract_markdown_response(markdown_doc).strip()
    if not re.search(r"^\s*#\s+.+$", content, flags=re.MULTILINE):
        title = strategy_name or "Auto Strategy"
        content = f"# {title}\n\n{content}".strip()
    return content


def _extract_markdown_response(content: str) -> str:
    for language in ("markdown", "md"):
        match = re.search(
            rf"```{language}\s*(.*?)```",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            return match.group(1).strip()
    return (content or "").strip()


def _extract_python_response(content: str) -> str:
    for language in ("python", "py"):
        match = re.search(
            rf"```{language}\s*(.*?)```",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            return match.group(1).strip()

    generic_match = re.search(r"```\s*(.*?)```", content, flags=re.DOTALL)
    if generic_match:
        return generic_match.group(1).strip()

    return (content or "").strip()


def _extract_strategy_meta(
    content: str,
    *,
    requested_name: str | None,
    markdown_doc: str,
) -> tuple[str, dict[str, Any]]:
    candidate = _extract_python_response(content)

    try:
        module = ast.parse(candidate)
    except SyntaxError:
        module = None

    parsed: Any = None
    if module is not None:
        for node in module.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "STRATEGY_META" for target in node.targets
            ):
                parsed = ast.literal_eval(node.value)
                break
        if parsed is None and len(module.body) == 1 and isinstance(module.body[0], ast.Expr):
            parsed = ast.literal_eval(module.body[0].value)

    if parsed is None:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or start >= end:
            raise ValueError("Missing STRATEGY_META dictionary in model response.")
        parsed = ast.literal_eval(candidate[start : end + 1])

    if not isinstance(parsed, dict):
        raise TypeError("STRATEGY_META must be a dictionary.")

    normalized = _normalize_strategy_meta(parsed, requested_name=requested_name, markdown_doc=markdown_doc)
    return _format_strategy_meta_assignment(normalized), normalized


def _normalize_strategy_meta(
    meta: dict[str, Any],
    *,
    requested_name: str | None,
    markdown_doc: str,
) -> dict[str, Any]:
    normalized = dict(meta)
    resolved_title = _extract_markdown_title(markdown_doc) or requested_name or normalized.get("name") or "Auto Strategy"
    normalized["name"] = str(normalized.get("name") or resolved_title).strip() or str(resolved_title)
    normalized["version"] = str(normalized.get("version") or "1.0.0")

    parameters = normalized.get("parameters")
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise TypeError("STRATEGY_META['parameters'] must be a dict.")
    normalized["parameters"] = parameters

    normalized["tags"] = _normalize_meta_tags(normalized.get("tags"))
    return normalized


def _normalize_meta_tags(tags: Any) -> dict[str, Any]:
    if tags is None:
        normalized_tags: dict[str, Any] = {}
    elif isinstance(tags, dict):
        normalized_tags = dict(tags)
    elif isinstance(tags, str):
        parts = [chunk.strip() for chunk in re.split(r"[;,]", tags) if chunk.strip()]
        normalized_tags = {"type": parts}
    elif isinstance(tags, (list, tuple, set)):
        normalized_tags = {"type": [str(item).strip() for item in tags if str(item).strip()]}
    else:
        raise TypeError("STRATEGY_META['tags'] must be a dict, string, or list of tags.")

    def _as_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    if "type" in normalized_tags:
        normalized_tags["type"] = _as_string_list(normalized_tags.get("type"))
    if "asset_class" in normalized_tags:
        normalized_tags["asset_class"] = _as_string_list(normalized_tags.get("asset_class"))

    for key in ("source", "link", "complexity"):
        if key in normalized_tags and normalized_tags[key] is not None:
            normalized_tags[key] = str(normalized_tags[key]).strip()

    return normalized_tags


def _format_strategy_meta_assignment(meta: dict[str, Any]) -> str:
    return "STRATEGY_META = " + pformat(meta, width=100, sort_dicts=False)


def _resolve_strategy_name(markdown_doc: str, *, requested_name: str | None) -> str:
    if requested_name:
        return _slugify(requested_name)
    title = _extract_markdown_title(markdown_doc)
    if title:
        return _slugify(title)
    return _slugify(f"auto_strategy_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")


def _extract_markdown_title(markdown_doc: str) -> str | None:
    match = re.search(r"^\s*#\s+(.+)$", markdown_doc, flags=re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def _assemble_strategy_module(meta_source: str, python_body: str) -> str:
    body = _strip_top_level_assignment(python_body.strip(), "STRATEGY_META")
    if not body:
        raise ValueError("Generated Python body is empty.")

    try:
        module = ast.parse(body)
    except SyntaxError:
        return f"{meta_source}\n\n{body.rstrip()}\n"

    lines = body.splitlines()
    insert_after = 0
    if module.body and isinstance(module.body[0], ast.Expr):
        value = getattr(module.body[0], "value", None)
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            insert_after = getattr(module.body[0], "end_lineno", 0)

    if insert_after > 0:
        before = "\n".join(lines[:insert_after]).rstrip()
        after = "\n".join(lines[insert_after:]).lstrip("\n")
        pieces = [before, meta_source]
        if after:
            pieces.append(after)
        return "\n\n".join(piece for piece in pieces if piece).rstrip() + "\n"

    return f"{meta_source}\n\n{body.rstrip()}\n"


def _strip_top_level_assignment(source: str, variable_name: str) -> str:
    try:
        module = ast.parse(source)
    except SyntaxError:
        return source

    ranges: list[tuple[int, int]] = []
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == variable_name for target in node.targets
        ):
            ranges.append((node.lineno - 1, getattr(node, "end_lineno", node.lineno) - 1))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == variable_name:
            ranges.append((node.lineno - 1, getattr(node, "end_lineno", node.lineno) - 1))

    if not ranges:
        return source

    lines = source.splitlines()
    filtered: list[str] = []
    for idx, line in enumerate(lines):
        if any(start <= idx <= end for start, end in ranges):
            continue
        filtered.append(line)
    return "\n".join(filtered).strip()


def _resolve_backtest_target(
    *,
    symbols: list[str] | None,
    timeframe: str | None,
    start: str | None,
    end: str | None,
) -> BacktestTarget:
    normalized_symbols = [str(symbol).strip().upper() for symbol in (symbols or []) if str(symbol).strip()]
    normalized_timeframe = (timeframe or "").strip().lower()

    if not normalized_symbols or not normalized_timeframe:
        entries = _load_ohlcv_entries()
        if not entries:
            raise RuntimeError(
                "No local OHLCV data found. Download data before using the auto-convert agent."
            )

        filtered = entries
        if normalized_symbols:
            filtered = [
                entry
                for entry in filtered
                if str(entry.get("symbol", "")).strip().upper() in normalized_symbols
            ]
        if normalized_timeframe:
            filtered = [
                entry
                for entry in filtered
                if str(entry.get("timeframe", "")).strip().lower() == normalized_timeframe
            ]
        if not filtered:
            raise RuntimeError("No matching OHLCV dataset found for the requested auto-agent target.")

        selected = min(filtered, key=_entry_rank)
        if not normalized_symbols:
            normalized_symbols = [str(selected.get("symbol", "")).strip().upper()]
        if not normalized_timeframe:
            normalized_timeframe = str(selected.get("timeframe", "")).strip().lower()

    detected_start, detected_end = _detect_date_range(
        normalized_symbols,
        normalized_timeframe,
        str(DATA_DIR),
    )
    return BacktestTarget(
        symbols=normalized_symbols,
        timeframe=normalized_timeframe,
        start=(start or detected_start).strip(),
        end=(end or detected_end).strip(),
    )


def _load_ohlcv_entries() -> list[dict]:
    catalog_path = DATA_DIR / "catalog.json"
    if catalog_path.exists():
        try:
            payload = json.loads(catalog_path.read_text(encoding="utf-8"))
            entries = payload.get("ohlcv", []) if isinstance(payload, dict) else []
            if isinstance(entries, list) and entries:
                return [entry for entry in entries if isinstance(entry, dict)]
        except Exception:
            pass
    return scan_ohlcv(DATA_DIR)


def _entry_rank(entry: dict) -> tuple[int, int, int, str, str]:
    asset_class = str(entry.get("asset_class", "")).strip().lower()
    timeframe = str(entry.get("timeframe", "")).strip().lower()
    symbol = str(entry.get("symbol", "")).strip().upper()
    source = str(entry.get("source", "")).strip().lower()
    return (
        ASSET_CLASS_RANK.get(asset_class, 99),
        TIMEFRAME_RANK.get(timeframe, 99),
        SYMBOL_RANK.get(symbol, 99),
        source,
        symbol,
    )


def _extract_engine(markdown_doc: str, *, requested_engine: str | None) -> str:
    if requested_engine:
        return requested_engine.strip().lower()

    section_match = re.search(
        r"##\s*Framework-Empfehlung(.*?)(?:\n##|\Z)",
        markdown_doc,
        flags=re.IGNORECASE | re.DOTALL,
    )
    haystack = section_match.group(1) if section_match else markdown_doc
    engine_match = re.search(r"\b(vectorbt|pynecore|lean)\b", haystack, flags=re.IGNORECASE)
    if not engine_match:
        return "vectorbt"
    return engine_match.group(1).lower()


def _slugify(raw_value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", raw_value).strip("_").lower()
    return slug or "auto_strategy"


def _build_backtest_command(
    *,
    strategy_path: Path,
    engine: str,
    target: BacktestTarget,
    output_path: Path,
    capital: float,
) -> list[str]:
    relative_strategy_path = strategy_path.relative_to(PROJECT_ROOT)
    cmd = [
        sys.executable,
        "-m",
        "runner.cli",
        "run",
        "--engine",
        engine,
        "--strategy",
        str(relative_strategy_path),
        "--symbols",
        *target.symbols,
        "--timeframe",
        target.timeframe,
        "--start",
        target.start,
        "--end",
        target.end,
        "--capital",
        str(capital),
        "--risk-pct",
        str(DEFAULT_RISK_PCT),
        "--output",
        str(output_path),
    ]
    return cmd


BACKTEST_TIMEOUT_SECONDS = 1800


def _run_backtest(cmd: list[str], *, log: TaskLogger | None) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    _log(log, "Launching backtest command: " + " ".join(cmd))

    output_lines: list[str] = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
        bufsize=1,
    )

    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                output_lines.append(line)
                _log(log, line.rstrip("\n"), stream="stdout")
        try:
            returncode = proc.wait(timeout=BACKTEST_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)
            raise RuntimeError(
                f"Generated-strategy backtest timed out after "
                f"{BACKTEST_TIMEOUT_SECONDS} seconds: {' '.join(cmd)}"
            ) from None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)

    return returncode, "".join(output_lines)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _log(
    log: TaskLogger | None,
    message: str,
    *,
    stream: str = "task",
    level: str = "info",
) -> None:
    if log is None:
        return
    try:
        log(message, stream=stream, level=level)
    except TypeError:
        log(message)
