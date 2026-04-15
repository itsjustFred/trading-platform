"""
Autonomous strategy-conversion worker backed by NVIDIA NIM.

This module converts raw strategy code into a runnable repository strategy using
three smaller LLM stages (analyst, architect, coder), writes the generated
Python + Markdown artifacts into per-strategy folders, and validates the result
by launching a backtest through runner.cli. If validation fails, only the coder
stage is retried with the latest error context.
"""

from __future__ import annotations

import ast
import inspect
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

from openai import OpenAI
import pandas as pd

from data_tools.catalog_builder import scan_ohlcv
from runner.cli import _detect_date_range
from runner.strategy_ir.builder import build_strategy_ir
from runner.strategy_ir.codegen_vectorbt import generate_vectorbt_strategy_code
from runner.strategy_ir.routing import IR_ROLLOUT_DEFAULT, resolve_codegen_route
from runner.strategy_ir.normalizer import normalize_strategy_meta_payload
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


def _get_ir_codegen_generators() -> dict[str, Callable[[Any], str]]:
    """Return currently supported deterministic IR code generators by engine."""
    return {
        "vectorbt": generate_vectorbt_strategy_code,
    }


def _resolve_ir_rollout_mode(*, log: TaskLogger | None) -> str:
    raw = (os.getenv("AUTO_AGENT_IR_ROLLOUT") or "").strip()
    if not raw:
        return IR_ROLLOUT_DEFAULT

    normalized = raw.lower()
    aliases = {
        "legacy-only": "legacy",
        "legacy_llm": "legacy",
        "llm-only": "legacy",
        "ir_first": "ir-first",
        "deterministic": "ir-first",
        "ir_only": "ir-only",
        "deterministic-only": "ir-only",
        "strict-ir": "ir-only",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in {"legacy", "ir-first", "ir-only"}:
        return normalized

    _log(
        log,
        f"⚠️ Unknown AUTO_AGENT_IR_ROLLOUT='{raw}'. Falling back to '{IR_ROLLOUT_DEFAULT}'.",
        level="warning",
    )
    return IR_ROLLOUT_DEFAULT


def _build_preflight_dataframe() -> pd.DataFrame:
    timestamp = pd.date_range("2024-01-01", periods=120, freq="h", tz="UTC")
    close = pd.Series([100.0 + (idx * 0.2) for idx in range(len(timestamp))], dtype=float)
    return pd.DataFrame(
        {
            "timestamp": timestamp,
            "open": close + 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000,
        }
    )


def _preflight_uses_kwargs_style(func: Callable[..., Any]) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    parameters = list(signature.parameters.values())
    if len(parameters) < 2:
        return False
    if parameters[1].name == "params":
        return False
    return any(parameter.default is not inspect.Parameter.empty for parameter in parameters[1:])


def _preflight_call_generate_signals(
    generate_signals: Callable[..., Any],
    df: pd.DataFrame,
    parameters: dict[str, Any],
) -> Any:
    if _preflight_uses_kwargs_style(generate_signals):
        signature = inspect.signature(generate_signals)
        accepted = {parameter.name for parameter in list(signature.parameters.values())[1:]}
        filtered = {key: value for key, value in parameters.items() if key in accepted}
        return generate_signals(df, **filtered)
    return generate_signals(df, parameters)


def _preflight_validate_series_length(
    *,
    label: str,
    value: Any,
    expected_length: int,
    issues: list[str],
) -> None:
    try:
        series = pd.Series(value)
    except Exception as exc:  # pragma: no cover - defensive conversion guard
        issues.append(f"PREFLIGHT[SHAPE] Could not convert '{label}' to a series: {exc}")
        return
    if len(series) != expected_length:
        issues.append(
            f"PREFLIGHT[SHAPE] '{label}' length mismatch: expected {expected_length}, got {len(series)}."
        )


def _preflight_validate_vectorbt_output(
    *,
    output: Any,
    expected_length: int,
    issues: list[str],
) -> None:
    if isinstance(output, pd.DataFrame):
        if "entries" not in output.columns or "exits" not in output.columns:
            issues.append("PREFLIGHT[CONTRACT] DataFrame output must include 'entries' and 'exits' columns.")
            return
        if len(output) != expected_length:
            issues.append(
                f"PREFLIGHT[SHAPE] DataFrame output length mismatch: expected {expected_length}, got {len(output)}."
            )
        return

    if not isinstance(output, (tuple, list)):
        issues.append(
            "PREFLIGHT[CONTRACT] generate_signals() must return DataFrame or tuple/list of "
            "(entries, exits[, indicators_dict[, drawings]])."
        )
        return

    if len(output) not in {2, 3, 4}:
        issues.append(
            "PREFLIGHT[CONTRACT] Tuple/list output must have length 2, 3, or 4."
        )
        return

    entries, exits = output[0], output[1]
    _preflight_validate_series_length(
        label="entries",
        value=entries,
        expected_length=expected_length,
        issues=issues,
    )
    _preflight_validate_series_length(
        label="exits",
        value=exits,
        expected_length=expected_length,
        issues=issues,
    )

    if len(output) >= 3 and not isinstance(output[2], dict):
        issues.append("PREFLIGHT[CONTRACT] Third tuple element must be indicators_dict (dict).")
    if len(output) == 4 and not isinstance(output[3], list):
        issues.append("PREFLIGHT[CONTRACT] Fourth tuple element must be drawings (list).")


def _preflight_validate_vectorbt_strategy(
    *,
    namespace: dict[str, Any],
    expected_parameters: dict[str, Any],
    issues: list[str],
) -> None:
    generate_signals = namespace.get("generate_signals")
    if not callable(generate_signals):
        issues.append("PREFLIGHT[CONTRACT] Missing callable generate_signals(df, params).")
        return

    try:
        signature = inspect.signature(generate_signals)
        parameters = list(signature.parameters.values())
    except (TypeError, ValueError) as exc:
        issues.append(f"PREFLIGHT[CONTRACT] Could not inspect generate_signals signature: {exc}")
        return

    if len(parameters) < 2:
        issues.append("PREFLIGHT[CONTRACT] generate_signals must accept at least df and params/kwargs.")
        return

    run_parameters: dict[str, Any] = {}
    if isinstance(expected_parameters, dict):
        run_parameters.update(expected_parameters)
    strategy_meta = namespace.get("STRATEGY_META")
    if isinstance(strategy_meta, dict):
        meta_parameters = strategy_meta.get("parameters")
        if isinstance(meta_parameters, dict):
            for key, value in meta_parameters.items():
                run_parameters.setdefault(str(key), value)

    df = _build_preflight_dataframe()
    try:
        output = _preflight_call_generate_signals(generate_signals, df, run_parameters)
    except Exception as exc:
        issues.append(f"PREFLIGHT[EXECUTION] generate_signals dry-run failed: {exc.__class__.__name__}: {exc}")
        return

    _preflight_validate_vectorbt_output(
        output=output,
        expected_length=len(df),
        issues=issues,
    )


def _format_preflight_issues(issues: list[str]) -> str:
    lines = ["Preflight validation failed:"]
    lines.extend(f"- {issue}" for issue in issues)
    return "\n".join(lines)


def _run_strategy_preflight(
    *,
    strategy_path: Path,
    engine: str,
    expected_parameters: dict[str, Any],
    log: TaskLogger | None,
) -> tuple[bool, str]:
    issues: list[str] = []

    try:
        source = strategy_path.read_text(encoding="utf-8")
    except OSError as exc:
        message = f"PREFLIGHT[IO] Could not read generated strategy file: {exc}"
        _log(log, message, level="error")
        return False, _format_preflight_issues([message])

    try:
        parsed = ast.parse(source, filename=str(strategy_path))
    except SyntaxError as exc:
        issues.append(
            f"PREFLIGHT[SYNTAX] {exc.msg} at line {exc.lineno}:{exc.offset}."
        )
        message = _format_preflight_issues(issues)
        _log(log, message, level="error")
        return False, message

    namespace: dict[str, Any] = {
        "__name__": "__auto_agent_preflight__",
        "__file__": str(strategy_path),
    }
    try:
        exec(compile(parsed, str(strategy_path), "exec"), namespace)
    except Exception as exc:
        issues.append(
            f"PREFLIGHT[IMPORT] Import/definition phase failed: {exc.__class__.__name__}: {exc}"
        )
        message = _format_preflight_issues(issues)
        _log(log, message, level="error")
        return False, message

    strategy_meta = namespace.get("STRATEGY_META")
    if strategy_meta is not None and not isinstance(strategy_meta, dict):
        issues.append("PREFLIGHT[META] STRATEGY_META must be a dict.")
    elif isinstance(strategy_meta, dict):
        parameters = strategy_meta.get("parameters", {})
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            issues.append("PREFLIGHT[META] STRATEGY_META['parameters'] must be a dict.")
        elif expected_parameters:
            missing = sorted(str(key) for key in expected_parameters if key not in parameters)
            if missing:
                issues.append(
                    "PREFLIGHT[META] STRATEGY_META['parameters'] is missing expected keys: "
                    + ", ".join(missing)
                )

    if engine == "vectorbt":
        _preflight_validate_vectorbt_strategy(
            namespace=namespace,
            expected_parameters=expected_parameters,
            issues=issues,
        )

    if issues:
        message = _format_preflight_issues(issues)
        _log(log, message, level="error")
        return False, message

    _log(log, "✅ Preflight: generated strategy passed contract checks.")
    return True, ""


def _read_env_file_value(key: str, env_path: Path) -> str | None:
    """Read a single key from a dotenv-style file without external dependencies."""
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        current_key, raw_value = line.split("=", 1)
        if current_key.strip() != key:
            continue
        value = raw_value.strip()
        if value and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value or None

    return None


def _resolve_env_key(key: str) -> str | None:
    value = (os.getenv(key) or "").strip()
    if value:
        return value
    return _read_env_file_value(key, PROJECT_ROOT / ".env")


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
    ir_path: Path
    output_path: Path


def run_auto_agent(
    code: str,
    *,
    strategy_name: str | None = None,
    requested_engine: str | None = None,
    model: str = DEFAULT_MODEL_NAME,
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

    selected_model = (model or DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME
    if selected_model not in SUPPORTED_MODEL_NAMES:
        raise ValueError(f"Unsupported model: {selected_model}")

    rollout_mode = _resolve_ir_rollout_mode(log=log)

    line_count = source_code.count("\n") + 1
    if line_count >= LONG_CODE_LINE_THRESHOLD:
        message = f"Code is very long ({line_count} lines). Expect longer generation time."
        logger.info(message, extra={"data": {"line_count": line_count, "timeout_seconds": timeout_seconds}})
        _log(log, message)

    api_key = _resolve_env_key("NVIDIA_API_KEY")
    if not api_key:
        raise RuntimeError(
            "NVIDIA_API_KEY environment variable is not set (and was not found in .env)."
        )

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
    _log(log, f"Selected AI model: {selected_model}")

    client = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=api_key,
        timeout=float(timeout_seconds),
    )

    system_prompt = _load_system_prompt()

    _log(log, f"Step 1/3: Requesting Markdown documentation from {selected_model}.")
    markdown_response = _run_llm_step(
        client,
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

    _log(log, f"Step 2/3: Requesting STRATEGY_META from {selected_model}.")
    meta_response = _run_llm_step(
        client,
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

    ir_artifact_written = False
    strategy_ir = None
    try:
        strategy_ir = build_strategy_ir(
            strategy_slug=artifacts.resolved_name,
            engine=engine,
            source_code=source_code,
            markdown_doc=markdown_doc,
            strategy_meta=meta_dict,
        )
        artifacts.ir_path.write_text(strategy_ir.to_json(indent=2) + "\n", encoding="utf-8")
        ir_artifact_written = True
        _log(log, f"Wrote IR artifact file: {artifacts.ir_path}")
        _log(log, "✅ IR Seam: StrategyIR artifact created.")
    except Exception as exc:
        # Keep legacy generation path operational while IR seam is incrementally rolled out.
        _log(log, f"⚠️ IR Seam fallback: continuing without IR artifact ({exc})", level="warning")

    last_error = "Unknown auto-agent failure."
    route = resolve_codegen_route(
        engine=engine,
        strategy_ir=strategy_ir,
        generators=_get_ir_codegen_generators(),
        rollout_mode=rollout_mode,
    )
    _log(log, f"Codegen rollout mode: {route.rollout_mode}")
    _log(log, f"IR generator routing: {route.reason}")

    if not route.use_ir_generator and not route.allow_legacy_fallback:
        raise RuntimeError(
            "IR-only rollout mode is enabled but deterministic IR codegen route is unavailable: "
            f"{route.reason}"
        )

    first_llm_attempt = route.first_llm_attempt
    if route.use_ir_generator and strategy_ir is not None and route.generator is not None:
        _log(
            log,
            f"Step 3/3: Attempting deterministic {route.generator_id} code generation from IR.",
        )
        try:
            deterministic_code = route.generator(strategy_ir)
            artifacts.strategy_path.write_text(deterministic_code.rstrip() + "\n", encoding="utf-8")
            _log(log, f"Wrote strategy file: {artifacts.strategy_path}")
            _log(
                log,
                f"✅ Step 3/3: Deterministic {route.generator_id} code generated from IR.",
            )

            preflight_ok, preflight_error = _run_strategy_preflight(
                strategy_path=artifacts.strategy_path,
                engine=engine,
                expected_parameters=meta_dict.get("parameters", {}),
                log=log,
            )
            if not preflight_ok:
                last_error = preflight_error
                if not route.allow_legacy_fallback:
                    raise RuntimeError(
                        "Deterministic IR codegen preflight failed in ir-only rollout mode: "
                        f"{preflight_error}"
                    )
                _log(
                    log,
                    "⚠️ Deterministic IR codegen preflight failed. Falling back to legacy LLM codegen.",
                    level="warning",
                )
            else:
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
                    return _build_success_response(
                        artifacts=artifacts,
                        target=target,
                        engine=engine,
                        attempts=1,
                        include_ir_file=ir_artifact_written,
                        rollout_mode=route.rollout_mode,
                        codegen_path=f"ir:{route.generator_id or 'unknown'}",
                    )

                last_error = combined_output or "Deterministic IR codegen validation failed without output."
                if not route.allow_legacy_fallback:
                    raise RuntimeError(
                        "Deterministic IR codegen validation failed in ir-only rollout mode: "
                        f"{last_error}"
                    )
                _log(
                    log,
                    f"⚠️ Deterministic IR codegen fallback: {last_error}",
                    level="warning",
                )
        except Exception as exc:
            last_error = f"Deterministic IR codegen failed: {exc}"
            if not route.allow_legacy_fallback:
                raise RuntimeError(
                    f"Deterministic IR codegen failed in ir-only rollout mode: {exc}"
                ) from exc
            _log(log, f"⚠️ Deterministic IR codegen fallback: {exc}", level="warning")

    if route.use_ir_generator and not route.allow_legacy_fallback:
        raise RuntimeError(
            "IR-only rollout mode requires deterministic validation success before backtest handoff. "
            f"Last error: {last_error}"
        )

    for attempt in range(first_llm_attempt, max_iterations + 1):
        if attempt == 1:
            _log(log, f"Step 3/3: Requesting Python code from {selected_model}.")
        else:
            _log(
                log,
                f"Step 3/3: Retry {attempt}/{max_iterations} with validation feedback using {selected_model}.",
            )

        python_response = _run_llm_step(
            client,
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

        preflight_ok, preflight_error = _run_strategy_preflight(
            strategy_path=artifacts.strategy_path,
            engine=engine,
            expected_parameters=meta_dict.get("parameters", {}),
            log=log,
        )
        if not preflight_ok:
            last_error = preflight_error
            _log(log, f"Iteration {attempt} preflight failed.", level="error")
            continue

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
            return _build_success_response(
                artifacts=artifacts,
                target=target,
                engine=engine,
                attempts=attempt,
                include_ir_file=ir_artifact_written,
                rollout_mode=route.rollout_mode,
                codegen_path="legacy-llm",
            )

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
        ir_path=strategy_dir / f"{resolved_name}.ir.json",
        output_path=BACKTESTS_DIR
        / f"{resolved_name}_auto_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.trades.json",
    )


def _build_success_response(
    *,
    artifacts: StrategyArtifacts,
    target: BacktestTarget,
    engine: str,
    attempts: int,
    include_ir_file: bool,
    rollout_mode: str,
    codegen_path: str,
) -> dict[str, str | int | list[str]]:
    response: dict[str, str | int | list[str]] = {
        "strategy_file": str(artifacts.strategy_path),
        "documentation_file": str(artifacts.documentation_path),
        "backtest_output": str(artifacts.output_path),
        "engine": engine,
        "symbols": target.symbols,
        "timeframe": target.timeframe,
        "attempts": attempts,
        "rollout_mode": rollout_mode,
        "codegen_path": codegen_path,
    }
    if include_ir_file and artifacts.ir_path.exists():
        response["ir_file"] = str(artifacts.ir_path)
    return response


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
    client: OpenAI,
    *,
    model: str,
    developer_prompt: str,
    user_prompt: str,
    log: TaskLogger | None,
    step_label: str,
) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "developer", "content": developer_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = response.choices[0].message.content or ""
    _log(log, f"{step_label}: Received {len(content)} characters from model.")
    if not content.strip():
        raise RuntimeError(f"{step_label}: model returned an empty response.")
    return content


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
    return normalize_strategy_meta_payload(
        meta,
        requested_name=requested_name,
        markdown_doc=markdown_doc,
    )


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
    finally:
        returncode = proc.wait()

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
