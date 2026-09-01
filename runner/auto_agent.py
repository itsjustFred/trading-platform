"""Autonomous strategy-conversion agent (orchestration).

Converts raw strategy code into a runnable repository strategy using three
smaller LLM stages (analyst, architect, coder), writes the generated Python +
Markdown artifacts into per-strategy folders, and validates the result by
launching a backtest through runner.cli. If validation fails, only the coder
stage is retried with the latest error context.

Companion modules:
- runner/agent_prompts.py          — the per-stage LLM prompts
- runner/agent_response_parsing.py — response extraction + STRATEGY_META normalization
- runner/agent_backtest_target.py  — validation-target resolution + artifact paths
- runner/agent_process.py          — backtest subprocess control + task logging
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from runner.agent_backtest_target import (
    BACKTESTS_DIR,
    PROJECT_ROOT,
    STRATEGIES_DIR,
    BacktestTarget,  # noqa: F401 (re-exported for tests)
    StrategyArtifacts,  # noqa: F401 (re-exported for tests)
    build_strategy_artifacts,
    resolve_backtest_target,
)
from runner.agent_process import (
    TaskLogger,
    build_backtest_command,
    log_message as _log,
    run_backtest as _run_backtest_impl,
)
from runner.agent_prompts import (
    step_one_developer_prompt as _step_one_developer_prompt_impl,
    step_one_user_prompt as _step_one_user_prompt,
    step_three_developer_prompt as _step_three_developer_prompt_impl,
    step_three_user_prompt as _step_three_user_prompt,
    step_two_developer_prompt as _step_two_developer_prompt_impl,
    step_two_user_prompt as _step_two_user_prompt,
)
from runner.agent_response_parsing import (
    assemble_strategy_module as _assemble_strategy_module,
    extract_engine as _extract_engine,
    extract_markdown_title,
    extract_python_response as _extract_python_response,
    extract_strategy_meta as _extract_strategy_meta,
    normalize_markdown_doc as _normalize_markdown_doc,
    slugify as _slugify,
)
from runner.agent_response_parsing import normalize_strategy_meta as _normalize_strategy_meta
from runner.approval_gate import StrategyNotApprovedError, approval_path_for, require_approval
from runner.codegen_safety import screen_strategy_source
from runner.llm_providers import (
    DEFAULT_LLM_PROVIDER_NAME,
    SUPPORTED_LLM_PROVIDER_NAMES,
    get_llm_provider,
)
from runner.llm_providers.base import LLMProvider
from shared.logging import get_logger

PROMPT_PATH = PROJECT_ROOT / ".github" / "agents" / "strategy-conversion.md"

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
OPENAI_TIMEOUT_SECONDS = 600.0
LONG_CODE_LINE_THRESHOLD = 1000

logger = get_logger("auto-agent")

# Legacy in-module aliases kept so tests/callers can patch behavior on this
# module (e.g. monkeypatch.setattr(auto_agent, "_resolve_backtest_target", ...)).
_resolve_backtest_target = resolve_backtest_target


def _step_one_developer_prompt(*, requested_engine: str | None) -> str:
    return _step_one_developer_prompt_impl(requested_engine=requested_engine)


def _step_two_developer_prompt() -> str:
    return _step_two_developer_prompt_impl()


def _step_three_developer_prompt(*, system_prompt: str, engine: str) -> str:
    return _step_three_developer_prompt_impl(system_prompt=system_prompt, engine=engine)



BACKTEST_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "PYTHONUNBUFFERED",
    "PYTHONPATH",
    "PYTHONHASHSEED",
    "VIRTUAL_ENV",
    "TMPDIR",
    "DATA_DIR",
    "LOG_DIR",
)
BACKTEST_ENV_ALWAYS_SET = {"PYTHONUNBUFFERED": "1"}

# Wall-clock bound for the generated-strategy backtest child. Generated code is
# untrusted: without a timeout a hanging strategy wedges the worker thread.
BACKTEST_TIMEOUT_SECONDS = 1800


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
    strategy_dir_check = STRATEGIES_DIR / resolved_name
    if strategy_dir_check.exists():
        raise ValueError(
            f"Strategy slug '{resolved_name}' already exists under {STRATEGIES_DIR}. "
            "Auto-agent conversion refuses to overwrite an existing strategy; "
            "choose another name."
        )
    artifacts = build_strategy_artifacts(
        resolved_name=resolved_name,
        engine=engine,
        strategies_dir=STRATEGIES_DIR,
        backtests_dir=BACKTESTS_DIR,
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
        developer_prompt=_step_two_developer_prompt_impl(),
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

        screening = screen_strategy_source(python_code)
        if not screening.allowed:
            for finding in screening.findings:
                _log(
                    log,
                    f"Screening finding (line {finding.line}): {finding.message}",
                    level="error",
                )
            last_error = (
                "Generated code failed safety screening before execution: "
                + "; ".join(screening.messages)
            )
            _log(log, f"Iteration {attempt} failed: {last_error}", level="error")
            if attempt >= max_iterations:
                import shutil as _shutil
                _shutil.rmtree(artifacts.strategy_dir, ignore_errors=True)
                _log(log, f"Removed failed generated strategy artifacts: {artifacts.strategy_path}")
            continue

        artifacts.strategy_path.write_text(python_code.rstrip() + "\n", encoding="utf-8")
        _log(log, f"Wrote strategy file: {artifacts.strategy_path}")
        if attempt == 1:
            _log(log, "✅ Step 3/3: Python Code generated.")
        else:
            _log(log, f"✅ Step 3/3: Python Code regenerated (attempt {attempt}/{max_iterations}).")

        cmd = build_backtest_command(
            strategy_path=artifacts.strategy_path,
            engine=engine,
            target=target,
            output_path=artifacts.output_path,
            capital=capital,
            default_risk_pct=DEFAULT_RISK_PCT,
            project_root=PROJECT_ROOT,
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
        _discard_failed_attempt(
            attempt=attempt,
            max_iterations=max_iterations,
            artifacts=artifacts,
            log=log,
        )

    raise RuntimeError(f"Auto-agent failed after {max_iterations} iterations: {last_error}")


def _discard_failed_attempt(
    *,
    attempt: int,
    max_iterations: int,
    artifacts: "StrategyArtifacts",
    log: "TaskLogger | None" = None,
) -> None:
    """Remove one failed generation attempt's artifacts.

    Intermediate attempts keep the slug directory and its README: the next
    attempt reuses them. Only after the last attempt has failed is the whole
    slug directory removed — no broken file stays registry-runnable and no
    leftover blocks a later retry of the slug. The SHA256-bound approval (if
    any) is removed together with the file so a stale approval can never
    cover new code.
    """
    strategy_path = artifacts.strategy_path
    try:
        strategy_path.unlink(missing_ok=True)
        approval_path_for(strategy_path).unlink(missing_ok=True)
        # Main contract: the slug directory (README included) stays — only the
        # unreviewed file (and its stale approval) is removed, so the failed
        # attempt remains visible for debugging and cannot silently block a
        # later retry of the same slug.

        _log(log, f"Removed failed generated strategy artifacts: {strategy_path}")
    except OSError as exc:
        _log(log, f"Could not remove failed strategy artifacts {strategy_path}: {exc}")


def _load_system_prompt() -> str:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"System prompt not found: {PROMPT_PATH}")
    return PROMPT_PATH.read_text(encoding="utf-8")


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


def _resolve_strategy_name(markdown_doc: str, *, requested_name: str | None) -> str:
    if requested_name:
        return _slugify(requested_name)
    title = extract_markdown_title(markdown_doc)
    if title:
        return _slugify(title)
    return _slugify(f"auto_strategy_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")


def run_validated_backtest(
    strategy_path: Path | str,
    *,
    engine: str,
    target: BacktestTarget,
    output_path: Path,
    capital: float = DEFAULT_CAPITAL,
    log: TaskLogger | None = None,
) -> tuple[int, str]:
    """Run the backtest for an already-generated strategy behind the review gate.

    LLM-generated strategy code requires a human review gate before the first
    run (Decision Record 2026-08-31). The approval is bound to the file's
    SHA256 hash; if the file changed after approval, the gate refuses to run.
    """
    path = Path(strategy_path)
    try:
        require_approval(path, strategies_root=STRATEGIES_DIR)
    except StrategyNotApprovedError as exc:
        # Only add the how-to-approve action; the gate's own message already
        # carries the why (SHA256-bound approval missing/voided).
        raise StrategyNotApprovedError(
            f"{exc} Approve '{path}' via runner.approval_gate.approve_strategy()."
        ) from exc

    cmd = build_backtest_command(
        strategy_path=path,
        engine=engine,
        target=target,
        output_path=output_path,
        capital=capital,
        default_risk_pct=DEFAULT_RISK_PCT,
        project_root=PROJECT_ROOT,
    )
    return _run_backtest(cmd, log=log)


def build_backtest_child_env() -> dict[str, str]:
    """Build the child environment for generated-strategy backtests.

    The full parent environment is never forwarded: it routinely contains API
    keys and tokens (NIM_API_KEY, GITHUB_TOKEN, ...). Only allowlisted,
    non-secret variables plus the values in ``BACKTEST_ENV_ALWAYS_SET`` are
    inherited.
    """
    env = {key: value for key, value in os.environ.items() if key in set(BACKTEST_ENV_ALLOWLIST)}
    env.update(BACKTEST_ENV_ALWAYS_SET)
    return env


def _kill_process(proc: subprocess.Popen, *, timeout: int = 30) -> None:
    """Kill a child process and reap it, tolerating a stubborn exit."""
    proc.kill()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        pass


def _run_backtest(cmd: list[str], *, log: TaskLogger | None) -> tuple[int, str]:
    env = build_backtest_child_env()
    _log(log, "Launching backtest command: " + " ".join(cmd))

    # The wall-clock bound must cover the whole child lifetime, including the
    # output streaming: a generated strategy that hangs while holding the pipe
    # open would otherwise block the read loop forever and wedge the worker
    # thread. communicate() enforces the timeout across streaming and reaping.
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
        proc_output, _ = proc.communicate(timeout=BACKTEST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_process(proc)
        raise RuntimeError(
            f"Generated-strategy backtest timed out after "
            f"{BACKTEST_TIMEOUT_SECONDS} seconds: {' '.join(cmd)}"
        ) from None
    finally:
        if proc.poll() is None:
            _kill_process(proc)

    output_lines = proc_output.splitlines(keepends=True) if proc_output else []
    for line in output_lines:
        _log(log, line.rstrip("\n"), stream="stdout")
    return proc.returncode, "".join(output_lines)


def _kill(proc: subprocess.Popen) -> None:
    """Kill and reap a child process; safe to call on an exited child."""
    if proc.poll() is not None:
        return
    proc.kill()
    proc.wait(timeout=30)


def _timeout(proc: subprocess.Popen, cmd: list[str]) -> None:
    """Kill a hung child and raise the timeout error for the caller."""
    proc.kill()
    proc.wait(timeout=30)
    raise RuntimeError(
        f"Generated-strategy backtest timed out after "
        f"{_backtest_timeout()} seconds: {' '.join(cmd)}"
    ) from None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]
