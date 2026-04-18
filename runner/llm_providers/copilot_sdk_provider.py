from __future__ import annotations

from typing import Any


class CopilotSDKProvider:
    def generate(
        self,
        *,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        del prompt, system_prompt, model, temperature, metadata
        raise RuntimeError("copilot_sdk provider is not configured in this environment")

