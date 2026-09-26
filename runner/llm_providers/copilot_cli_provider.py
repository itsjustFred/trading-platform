from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

from runner.agent_process import build_backtest_child_env

# The full prompt is piped via stdin (`-p -`) instead of being passed as a
# single argv element: prompts here routinely exceed platform argv limits
# (Windows ~32k chars) and would otherwise be readable process-wide via `ps`.


EMPTY_RESPONSE_ERROR = "copilot_cli provider returned an empty response."


class CliInvocationError(RuntimeError):
    """CLI invocation or response failure, with the metadata status to record."""

    def __init__(self, *, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def _compose_prompt(*, user_prompt: str, system_prompt: str | None) -> str:
    if not system_prompt or not system_prompt.strip():
        return user_prompt
    return (
        "System instructions:\n"
        f"{system_prompt.strip()}\n\n"
        "User prompt:\n"
        f"{user_prompt}"
    )


_COPILOT_CLI_EVENT_TYPES = frozenset(
    {
        "session.mcp_servers_loaded",
        "session.tools_updated",
        "user.message",
        "assistant.turn_start",
        "assistant.message_delta",
        "assistant.message",
        "assistant.turn_end",
        "session.task_complete",
        "tool.execution_start",
        "tool.execution_complete",
        "session.warning",
        "session.info",
        "result",
    }
)


def _extract_content(payload: Any) -> str:
    if not isinstance(payload, dict) or payload.get("type") != "assistant.message":
        return ""
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    content = data.get("content")
    return content.strip() if isinstance(content, str) else ""


def _extract_jsonl_content(stdout: str) -> str:
    content = ""
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CliInvocationError(
                status="error",
                message="copilot_cli provider returned an invalid JSON event envelope.",
            ) from exc
        if not isinstance(payload, dict):
            raise CliInvocationError(
                status="error",
                message="copilot_cli provider returned a non-object JSON event envelope.",
            )
        event_type = payload.get("type")
        if not isinstance(event_type, str):
            raise CliInvocationError(
                status="error",
                message="copilot_cli provider returned an unknown JSON event envelope.",
            )
        if event_type not in _COPILOT_CLI_EVENT_TYPES:
            continue
        if event_type == "result":
            exit_code = payload.get("exitCode")
            if exit_code is not None and (not isinstance(exit_code, int) or exit_code != 0):
                raise CliInvocationError(
                    status="error",
                    message="copilot_cli provider returned a failed result event.",
                )
        extracted = _extract_content(payload)
        if extracted:
            content = extracted
    if not content:
        raise CliInvocationError(status="error", message=EMPTY_RESPONSE_ERROR)
    return content


class CopilotCLIProvider:
    def __init__(
        self,
        *,
        executable: str | None = None,
        timeout_seconds: float = 600.0,
        output_format: str = "json",
    ) -> None:
        self._executable = (
            executable or os.getenv("COPILOT_CLI_EXECUTABLE") or "copilot"
        ).strip() or "copilot"
        self._timeout_seconds = float(timeout_seconds)
        self._output_format = str(output_format or "json").strip().lower() or "json"
        if self._output_format not in {"json", "text"}:
            raise ValueError("output_format must be 'json' or 'text'.")

    def generate(
        self,
        *,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        started_at = time.perf_counter()
        user_prompt = str(prompt or "").strip()
        if not user_prompt:
            raise ValueError("copilot_cli provider requires a non-empty prompt.")

        composed_prompt = _compose_prompt(user_prompt=user_prompt, system_prompt=system_prompt)
        command = self._build_command(model=model)
        selected_model = str(model or "").strip()
        _apply_metadata(
            metadata,
            model=selected_model or "default",
            status="running",
            temperature=temperature,
        )

        try:
            completed = self._run_cli(command, composed_prompt)
            # Output extraction can also fail the invocation (non-zero exit,
            # empty response): same CliInvocationError path, same metadata.
            content = self._extract_output(completed)
        except CliInvocationError as exc:
            _apply_metadata(
                metadata,
                model=selected_model or "default",
                status=exc.status,
                duration_seconds=time.perf_counter() - started_at,
                error=str(exc),
                temperature=temperature,
            )
            raise RuntimeError(str(exc)) from exc.__cause__
        _apply_metadata(
            metadata,
            model=selected_model or "default",
            status="success",
            duration_seconds=time.perf_counter() - started_at,
            temperature=temperature,
        )
        return content

    def _build_command(self, *, model: str | None) -> list[str]:
        """argv for the CLI call: the prompt is piped, never passed as argv."""
        command = [self._executable, "-p", "-", "--output-format", self._output_format]
        selected_model = str(model or "").strip()
        if selected_model:
            command.extend(["--model", selected_model])
        return command

    def _run_cli(self, command: list[str], composed_prompt: str) -> subprocess.CompletedProcess:
        """Run the CLI once and translate launcher/timeout failures."""
        try:
            return subprocess.run(
                command,
                input=composed_prompt,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout_seconds,
                env=build_backtest_child_env(),
            )
        except FileNotFoundError as exc:
            raise CliInvocationError(
                status="error",
                message=(
                    f"copilot_cli provider executable not found: '{self._executable}'. "
                    "Install GitHub Copilot CLI or set COPILOT_CLI_EXECUTABLE."
                ),
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CliInvocationError(
                status="timeout",
                message=f"copilot_cli provider timed out after {self._timeout_seconds:.0f}s.",
            ) from exc

    def _extract_output(
        self,
        completed: subprocess.CompletedProcess,
    ) -> str:
        """Decode CLI output into response content, failing loudly on errors."""
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            details = stderr or stdout or "unknown error"
            raise CliInvocationError(
                status="error",
                message=(
                    f"copilot_cli provider failed with exit code "
                    f"{completed.returncode}: {details}"
                ),
            )

        if not stdout:
            raise CliInvocationError(status="error", message=EMPTY_RESPONSE_ERROR)
        if self._output_format == "json":
            return _extract_jsonl_content(stdout)
        if not stdout.strip():
            raise CliInvocationError(status="error", message=EMPTY_RESPONSE_ERROR)
        return stdout


def _apply_metadata(
    metadata: dict[str, Any] | None,
    *,
    model: str,
    status: str,
    duration_seconds: float | None = None,
    error: str | None = None,
    temperature: float | None = None,
) -> None:
    if not isinstance(metadata, dict):
        return
    metadata["provider"] = "copilot_cli"
    metadata["model"] = model
    metadata["status"] = status
    if duration_seconds is not None:
        metadata["duration_seconds"] = round(float(duration_seconds), 3)
    if error:
        metadata["error"] = error
    if temperature is not None:
        metadata["temperature"] = float(temperature)
