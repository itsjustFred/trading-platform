from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from runner.llm_providers import get_llm_provider
from runner.llm_providers.copilot_cli_provider import CopilotCLIProvider
from runner.llm_providers.copilot_sdk_provider import CopilotSDKProvider
from runner.llm_providers.nim_provider import NIMProvider


def test_get_llm_provider_selects_nim(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_: None))

    monkeypatch.setattr("runner.llm_providers.nim_provider.OpenAI", FakeOpenAI)
    provider = get_llm_provider("nim")
    assert isinstance(provider, NIMProvider)


def test_get_llm_provider_unknown_raises():
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        get_llm_provider("unknown-provider")


def test_copilot_cli_provider_missing_executable(monkeypatch):
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=5)

    def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError("not found")

    monkeypatch.setattr("subprocess.run", _raise_not_found)
    with pytest.raises(RuntimeError, match="executable not found"):
        provider.generate(prompt="hello")


def test_copilot_cli_provider_timeout(monkeypatch):
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=1)

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["copilot"], timeout=1)

    monkeypatch.setattr("subprocess.run", _raise_timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        provider.generate(prompt="hello")


def test_copilot_cli_provider_non_zero_exit(monkeypatch):
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=5)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["copilot"],
            returncode=2,
            stdout="",
            stderr="boom",
        ),
    )

    with pytest.raises(RuntimeError, match="exit code 2"):
        provider.generate(prompt="hello")


def test_copilot_cli_provider_empty_output(monkeypatch):
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=5)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["copilot"],
            returncode=0,
            stdout="",
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="empty response"):
        provider.generate(prompt="hello")


def test_copilot_sdk_provider_is_stub():
    provider = CopilotSDKProvider()
    with pytest.raises(RuntimeError, match="not configured"):
        provider.generate(prompt="hello")

