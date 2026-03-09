"""
Autonomous strategy-conversion worker backed by NVIDIA NIM.

This module converts raw strategy code into a runnable repository strategy,
writes the generated Python + Markdown artifacts into per-strategy folders,
immediately validates the result by launching a backtest through runner.cli.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from openai import OpenAI

from data_tools.catalog_builder import scan_ohlcv
from runner.cli import _detect_date_range
from shared.constants import DATA_DIR
from shared.logging import get_logger
from shared.strategy_registry import DEFAULT_STRATEGIES_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR
PROMPT_PATH = PROJECT_ROOT / ".github" / "agents" / "strategy-conversion.md"
BACKTESTS_DIR = DATA_DIR / "backtests"

MODEL_NAME = "moonshotai/kimi-k2.5"
MAX_ITERATIONS = 3
DEFAULT_CAPITAL = 100_000.0
DEFAULT_RISK_PCT = 0.01
ERROR_CONTEXT_LIMIT = 8_000
OPENAI_TIMEOUT_SECONDS = 300
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

TaskLogger = Callable[[str], None]
logger = get_logger("auto-agent")


@dataclass
class BacktestTarget:
    symbols: list[str]
    timeframe: str
    start: str
    end: str


def run_auto_agent(
    code: str,
    *,
    strategy_name: str | None = None,
    requested_engine: str | None = None,
    symbols: list[str] | None = None,
    timeframe: str | None = None,
    start: str | None = None,
    end: str | None = None,
    capital: float = DEFAULT_CAPITAL,
    max_iterations: int = MAX_ITERATIONS,
    timeout_seconds: int = OPENAI_TIMEOUT_SECONDS,
    log: TaskLogger | None = None,
) -> dict[str, str | int | list[str]]:
    """Convert raw strategy code into a runnable strategy and validate it via backtest."""
    source_code = (code or "").strip()
    if not source_code:
        raise ValueError("Strategy source code is required.")

    line_count = source_code.count("\n") + 1
    if line_count >= LONG_CODE_LINE_THRESHOLD:
        message = f"Code is very long ({line_count} lines). Expect longer generation time."
        logger.info(message, extra={"data": {"line_count": line_count, "timeout_seconds": timeout_seconds}})
        _log(log, message)

    api_key = os.getenv("NVIDIA_API_KEY")
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY environment variable is not set.")

    system_prompt = _load_system_prompt()
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

    client = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=api_key,
        timeout=timeout_seconds,
    )

    developer_prompt = (
        f"{system_prompt}\n\n"
        "Zusatzregeln fuer den autonomen Worker:\n"
        "1. Antworte ausschliesslich mit genau zwei fenced code blocks.\n"
        "2. Der erste Block MUSS ```python sein und die komplette Strategie-Datei enthalten.\n"
        "3. Der zweite Block MUSS ```markdown sein und die Dokumentation enthalten.\n"
        "4. Die Markdown-Doku MUSS einen Abschnitt 'Framework-Empfehlung' mit exakt einem "
        "Framework-Namen aus vectorbt, pynecore oder lean enthalten.\n"
        "5. Fuer vectorbt-Strategien muss die Python-Datei eine generate_signals(df, params)-"
        "Funktion und STRATEGY_META enthalten.\n"
        "6. Verwende nur Bibliotheken, die in diesem Repository bereits installiert sind.\n"
        "7. Erfinde keine fehlenden Backtest-Ausgaben; liefere nur die zwei Codebloecke."
    )
    user_prompt = _build_user_prompt(
        source_code=source_code,
        strategy_name=strategy_name,
        target=target,
        capital=capital,
        requested_engine=requested_engine,
    )
    messages = [
        {"role": "developer", "content": developer_prompt},
        {"role": "user", "content": user_prompt},
    ]

    last_error = "Unknown auto-agent failure."

    for attempt in range(1, max_iterations + 1):
        _log(log, f"Iteration {attempt}/{max_iterations}: requesting conversion from Kimi 2.5.")
        response = client.chat.completions.create(model=MODEL_NAME, messages=messages)
        content = response.choices[0].message.content or ""
        _log(log, f"Received {len(content)} characters from model.")
        _log(log, "Model response follows:\n" + content)

        attempt_error = ""
        try:
            python_code = _extract_block(content, "python")
            markdown_doc = _extract_markdown_block(content)
            resolved_name = _extract_strategy_name(
                python_code,
                markdown_doc,
                requested_name=strategy_name,
            )
            engine = _extract_engine(markdown_doc, requested_engine=requested_engine)

            strategy_dir = STRATEGIES_DIR / resolved_name
            strategy_path = strategy_dir / f"{resolved_name}.py"
            doc_path = strategy_dir / "README.md"
            output_path = BACKTESTS_DIR / (
                f"{resolved_name}_auto_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.trades.json"
            )

            strategy_dir.mkdir(parents=True, exist_ok=True)
            BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)
            strategy_path.write_text(python_code.rstrip() + "\n", encoding="utf-8")
            doc_path.write_text(markdown_doc.rstrip() + "\n", encoding="utf-8")

            _log(log, f"Wrote strategy file: {strategy_path}")
            _log(log, f"Wrote documentation file: {doc_path}")
            _log(log, f"Selected engine: {engine}")

            cmd = _build_backtest_command(
                strategy_path=strategy_path,
                engine=engine,
                target=target,
                output_path=output_path,
                capital=capital,
            )
            returncode, combined_output = _run_backtest(cmd, log=log)
            if returncode == 0 and output_path.exists():
                _log(log, f"Backtest succeeded. Output saved to {output_path}")
                return {
                    "strategy_file": str(strategy_path),
                    "documentation_file": str(doc_path),
                    "backtest_output": str(output_path),
                    "engine": engine,
                    "symbols": target.symbols,
                    "timeframe": target.timeframe,
                    "attempts": attempt,
                }

            attempt_error = combined_output or "Backtest failed without output."
        except Exception as exc:
            attempt_error = str(exc)

        last_error = attempt_error
        _log(log, f"Iteration {attempt} failed: {attempt_error}", level="error")
        messages.append({"role": "assistant", "content": content})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Der Code wirft diesen Fehler: "
                    f"{_truncate(attempt_error, ERROR_CONTEXT_LIMIT)}\n"
                    "Bitte fixen und erneut nur die beiden Codeblöcke liefern."
                ),
            }
        )

    raise RuntimeError(f"Auto-agent failed after {max_iterations} iterations: {last_error}")


def _load_system_prompt() -> str:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"System prompt not found: {PROMPT_PATH}")
    return PROMPT_PATH.read_text(encoding="utf-8")


def _build_user_prompt(
    *,
    source_code: str,
    strategy_name: str | None,
    target: BacktestTarget,
    capital: float,
    requested_engine: str | None,
) -> str:
    name_line = f"Gewuenschter Strategie-Name: {strategy_name}\n" if strategy_name else ""
    engine_line = (
        f"Vom User angefragtes Framework: {requested_engine}\n" if requested_engine else ""
    )
    return (
        "Bitte konvertiere den folgenden Strategie-Quellcode fuer dieses Repository.\n"
        f"{name_line}"
        f"{engine_line}"
        f"Validierungsziel fuer den automatischen Backtest: {','.join(target.symbols)} "
        f"auf {target.timeframe}, Zeitraum {target.start} bis {target.end}, "
        f"Kapital {capital:.2f}.\n"
        "Der Python-Block muss direkt im Repository lauffaehig sein.\n\n"
        "Quellcode:\n"
        "```text\n"
        f"{source_code}\n"
        "```"
    )


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


def _extract_block(content: str, language: str) -> str:
    match = re.search(
        rf"```{language}\s*(.*?)```",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError(f"Missing ```{language} block in model response.")
    return match.group(1).strip()


def _extract_markdown_block(content: str) -> str:
    for language in ("markdown", "md"):
        match = re.search(
            rf"```{language}\s*(.*?)```",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            return match.group(1).strip()
    raise ValueError("Missing ```markdown block in model response.")


def _extract_strategy_name(
    python_code: str,
    markdown_doc: str,
    *,
    requested_name: str | None,
) -> str:
    metadata_match = re.search(
        r"STRATEGY_META\s*=\s*\{.*?[\"']name[\"']\s*:\s*[\"']([^\"']+)[\"']",
        python_code,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if metadata_match:
        return _slugify(metadata_match.group(1))

    markdown_title = re.search(r"^\s*#\s+(.+)$", markdown_doc, flags=re.MULTILINE)
    if markdown_title:
        return _slugify(markdown_title.group(1))

    if requested_name:
        return _slugify(requested_name)

    return _slugify(f"auto_strategy_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")


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
