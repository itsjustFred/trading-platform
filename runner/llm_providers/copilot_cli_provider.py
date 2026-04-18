from __future__ import annotations

import json
import os
import subprocess
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
        self._executable = (executable or os.getenv("COPILOT_CLI_EXECUTABLE") or "copilot").strip() or "copilot"
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
        del temperature, metadata  # not supported by the CLI prompt mode today

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
            raise RuntimeError(
                f"copilot_cli provider executable not found: '{self._executable}'. "
                "Install GitHub Copilot CLI or set COPILOT_CLI_EXECUTABLE."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"copilot_cli provider timed out after {self._timeout_seconds:.0f}s."
            ) from exc

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            details = stderr or stdout or "unknown error"
            raise RuntimeError(
                f"copilot_cli provider failed with exit code {completed.returncode}: {details}"
            )

        if not stdout:
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
            raise RuntimeError("copilot_cli provider returned an empty response.")
        return content

