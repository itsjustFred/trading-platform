from __future__ import annotations

import os
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
        resolved_api_key = (api_key or os.getenv("NVIDIA_API_KEY") or "").strip()
        if not resolved_api_key:
            raise RuntimeError("NVIDIA_API_KEY environment variable is not set.")

        self._client = client or OpenAI(
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
        selected_model = str(model or "").strip()
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
        return content

