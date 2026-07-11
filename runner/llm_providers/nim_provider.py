from __future__ import annotations

import os
import time
from typing import Any

try:
    from openai import OpenAI
except ModuleNotFoundError:  # pragma: no cover - optional dependency for auto-agent mode
    class OpenAI:  # type: ignore[no-redef]
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise ModuleNotFoundError("openai package is required for NIM provider execution")


class NIMProvider:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://integrate.api.nvidia.com/v1",
        timeout_seconds: float = 600.0,
        client: Any | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            resolved_api_key = (api_key or os.getenv("NVIDIA_API_KEY") or "").strip()
            if not resolved_api_key:
                raise RuntimeError("NVIDIA_API_KEY environment variable is not set.")
            self._client = OpenAI(
                base_url=base_url,
                api_key=resolved_api_key,
                timeout=float(timeout_seconds),
            )

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
        selected_model = str(model or "").strip()
        _apply_metadata(
            metadata,
            model=selected_model or "",
            status="running",
            temperature=temperature,
        )
        try:
            if not selected_model:
                raise ValueError("NIM provider requires a model.")

            messages: list[dict[str, str]] = []
            if system_prompt and system_prompt.strip():
                messages.append({"role": "developer", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            request_payload: dict[str, Any] = {
                "model": selected_model,
                "messages": messages,
            }
            if temperature is not None:
                request_payload["temperature"] = float(temperature)

            response = self._client.chat.completions.create(**request_payload)
            content = response.choices[0].message.content or ""
            if not content.strip():
                raise RuntimeError("NIM provider returned an empty response.")

            _apply_metadata(
                metadata,
                model=selected_model,
                status="success",
                duration_seconds=time.perf_counter() - started_at,
                temperature=temperature,
            )
            return content
        except Exception as exc:
            _apply_metadata(
                metadata,
                model=selected_model or "",
                status="error",
                duration_seconds=time.perf_counter() - started_at,
                error=str(exc),
                temperature=temperature,
            )
            raise


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
    metadata["provider"] = "nim"
    metadata["model"] = model
    metadata["status"] = status
    if duration_seconds is not None:
        metadata["duration_seconds"] = round(float(duration_seconds), 3)
    if error:
        metadata["error"] = error
    else:
        metadata.pop("error", None)
    if temperature is not None:
        metadata["temperature"] = float(temperature)
