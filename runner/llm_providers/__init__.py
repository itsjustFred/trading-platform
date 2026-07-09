from __future__ import annotations

from typing import Any

from runner.llm_providers.base import LLMProvider
from runner.llm_providers.copilot_cli_provider import CopilotCLIProvider
from runner.llm_providers.copilot_sdk_provider import CopilotSDKProvider
from runner.llm_providers.nim_provider import NIMProvider

DEFAULT_LLM_PROVIDER_NAME = "nim"
SUPPORTED_LLM_PROVIDER_NAMES: tuple[str, ...] = (
    "nim",
    "copilot_cli",
    "copilot_sdk",
)


def get_llm_provider(provider_name: str, **kwargs: Any) -> LLMProvider:
    normalized = str(provider_name or "").strip().lower()
    if not normalized:
        normalized = DEFAULT_LLM_PROVIDER_NAME

    if normalized == "nim":
        return NIMProvider(**kwargs)
    if normalized == "copilot_cli":
        return CopilotCLIProvider(**kwargs)
    if normalized == "copilot_sdk":
        return CopilotSDKProvider(**kwargs)
    raise ValueError(f"Unsupported LLM provider: {provider_name}")


__all__ = [
    "LLMProvider",
    "DEFAULT_LLM_PROVIDER_NAME",
    "SUPPORTED_LLM_PROVIDER_NAMES",
    "get_llm_provider",
]
