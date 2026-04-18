from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any


def _extract_content(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        for key in ("content", "text", "response", "output", "message"):
            candidate = payload.get(key)
            extracted = _extract_content(candidate)
            if extracted:
                return extracted
        return ""
    if isinstance(payload, list):
        for item in payload:
            extracted = _extract_content(item)
            if extracted:
                return extracted
    return ""


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

        composed_prompt = user_prompt
        if system_prompt and system_prompt.strip():
            composed_prompt = (
                "System instructions:\n"
                f"{system_prompt.strip()}\n\n"
                "User prompt:\n"
                f"{user_prompt}"
            )

        command = [self._executable, "-p", composed_prompt, "--output-format", self._output_format]
        selected_model = str(model or "").strip()
        _apply_metadata(
            metadata,
            model=selected_model or "default",
            status="running",
            temperature=temperature,
        )
        if selected_model:
            command.extend(["--model", selected_model])

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            _apply_metadata(
                metadata,
                model=selected_model or "default",
                status="error",
                duration_seconds=time.perf_counter() - started_at,
                error=(
                    f"copilot_cli provider executable not found: '{self._executable}'. "
                    "Install GitHub Copilot CLI or set COPILOT_CLI_EXECUTABLE."
                ),
                temperature=temperature,
            )
            raise RuntimeError(
                f"copilot_cli provider executable not found: '{self._executable}'. "
                "Install GitHub Copilot CLI or set COPILOT_CLI_EXECUTABLE."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            _apply_metadata(
                metadata,
                model=selected_model or "default",
                status="timeout",
                duration_seconds=time.perf_counter() - started_at,
                error=f"copilot_cli provider timed out after {self._timeout_seconds:.0f}s.",
                temperature=temperature,
            )
            raise RuntimeError(
                f"copilot_cli provider timed out after {self._timeout_seconds:.0f}s."
            ) from exc

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            details = stderr or stdout or "unknown error"
            _apply_metadata(
                metadata,
                model=selected_model or "default",
                status="error",
                duration_seconds=time.perf_counter() - started_at,
                error=(
                    f"copilot_cli provider failed with exit code "
                    f"{completed.returncode}: {details}"
                ),
                temperature=temperature,
            )
            raise RuntimeError(
                f"copilot_cli provider failed with exit code {completed.returncode}: {details}"
            )

        if not stdout:
            _apply_metadata(
                metadata,
                model=selected_model or "default",
                status="error",
                duration_seconds=time.perf_counter() - started_at,
                error="copilot_cli provider returned an empty response.",
                temperature=temperature,
            )
            raise RuntimeError("copilot_cli provider returned an empty response.")

        if self._output_format == "json":
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                content = stdout
            else:
                content = _extract_content(payload) or stdout
        else:
            content = stdout

        if not content.strip():
            _apply_metadata(
                metadata,
                model=selected_model or "default",
                status="error",
                duration_seconds=time.perf_counter() - started_at,
                error="copilot_cli provider returned an empty response.",
                temperature=temperature,
            )
            raise RuntimeError("copilot_cli provider returned an empty response.")
        _apply_metadata(
            metadata,
            model=selected_model or "default",
            status="success",
            duration_seconds=time.perf_counter() - started_at,
            temperature=temperature,
        )
        return content


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
