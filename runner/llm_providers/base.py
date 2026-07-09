from __future__ import annotations

from typing import Any, Protocol


class LLMProvider(Protocol):
    def generate(
        self,
        *,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        ...

