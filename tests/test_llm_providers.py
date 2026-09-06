from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from runner.llm_providers import get_llm_provider
from runner.llm_providers.copilot_cli_provider import CopilotCLIProvider
from runner.llm_providers.copilot_sdk_provider import (
    AUTH_SESSION_ERROR,
    CopilotSDKProvider,
    _extract_content,
)
from runner.llm_providers.nim_provider import NIMProvider


def test_get_llm_provider_selects_nim(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_: None))

    monkeypatch.setattr("runner.llm_providers.nim_provider.OpenAI", FakeOpenAI)
    provider = get_llm_provider("nim")
    assert isinstance(provider, NIMProvider)


def test_get_llm_provider_selects_copilot_sdk():
    provider = get_llm_provider("copilot_sdk", timeout_seconds=123)
    assert isinstance(provider, CopilotSDKProvider)
    assert provider._timeout_seconds == 123


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


def test_copilot_cli_provider_output_failure_records_error_metadata(monkeypatch):
    """An output-extraction failure must land in the metadata, not stay "running".

    _extract_output runs inside the generate() try block, so its
    CliInvocationError goes through the same translation path as _run_cli
    failures: status "error" plus the error message on the metadata dict.
    """
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
    metadata: dict[str, object] = {}

    with pytest.raises(RuntimeError, match="empty response"):
        provider.generate(prompt="hello", metadata=metadata)

    assert metadata["status"] == "error"
    assert "empty response" in str(metadata["error"])
    assert isinstance(metadata["duration_seconds"], float)


def test_copilot_sdk_provider_missing_sdk(monkeypatch):
    provider = CopilotSDKProvider(timeout_seconds=5)

    def _raise_import_error():
        raise ModuleNotFoundError("copilot")

    monkeypatch.setattr(
        "runner.llm_providers.copilot_sdk_provider._load_copilot_sdk",
        _raise_import_error,
    )

    with pytest.raises(RuntimeError, match="github-copilot-sdk"):
        provider.generate(prompt="hello")


def test_copilot_sdk_provider_auth_error(monkeypatch):
    provider = CopilotSDKProvider(timeout_seconds=5)

    class FakePermissionHandler:
        approve_all = object()

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            raise RuntimeError("Not signed in to GitHub Copilot")

        async def stop(self):
            return None

    monkeypatch.setattr(
        "runner.llm_providers.copilot_sdk_provider._load_copilot_sdk",
        lambda: (FakeClient, FakePermissionHandler),
    )

    with pytest.raises(RuntimeError, match=AUTH_SESSION_ERROR):
        provider.generate(prompt="hello")


def test_copilot_sdk_provider_extracts_response_and_metadata(monkeypatch):
    provider = CopilotSDKProvider(timeout_seconds=5)

    class FakePermissionHandler:
        approve_all = object()

    class FakeResponseData:
        def __init__(self, content: str):
            self.content = content

    class FakeResponse:
        def __init__(self, content: str):
            self.data = FakeResponseData(content)

    class FakeSession:
        async def send_and_wait(self, _prompt: str):
            return FakeResponse("hello from copilot sdk")

        async def disconnect(self):
            return None

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            return None

        async def list_models(self):
            return [{"id": "gpt-5"}, {"id": "gpt-4.1"}]

        async def create_session(self, **_kwargs):
            return FakeSession()

        async def stop(self):
            return None

    monkeypatch.setattr(
        "runner.llm_providers.copilot_sdk_provider._load_copilot_sdk",
        lambda: (FakeClient, FakePermissionHandler),
    )

    metadata: dict[str, object] = {}
    output = provider.generate(
        prompt="hello",
        model="gpt-5",
        metadata=metadata,
    )
    assert output == "hello from copilot sdk"
    assert metadata["provider"] == "copilot_sdk"
    assert metadata["model"] == "gpt-5"
    assert metadata["status"] == "success"
    assert isinstance(metadata["duration_seconds"], float)


def test_copilot_sdk_provider_unsupported_model(monkeypatch):
    provider = CopilotSDKProvider(timeout_seconds=5)

    class FakePermissionHandler:
        approve_all = object()

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            return None

        async def list_models(self):
            return [{"id": "gpt-5"}]

        async def stop(self):
            return None

    monkeypatch.setattr(
        "runner.llm_providers.copilot_sdk_provider._load_copilot_sdk",
        lambda: (FakeClient, FakePermissionHandler),
    )

    with pytest.raises(ValueError, match="Unsupported model 'does-not-exist'"):
        provider.generate(prompt="hello", model="does-not-exist")


def test_copilot_sdk_provider_timeout(monkeypatch):
    provider = CopilotSDKProvider(timeout_seconds=0.05)

    class FakePermissionHandler:
        approve_all = object()

    class FakeSession:
        async def send_and_wait(self, _prompt: str):
            await asyncio.sleep(0.2)
            return {"data": {"content": "late"}}

        async def disconnect(self):
            return None

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            return None

        async def list_models(self):
            return [{"id": "gpt-5"}]

        async def create_session(self, **_kwargs):
            return FakeSession()

        async def stop(self):
            return None

    monkeypatch.setattr(
        "runner.llm_providers.copilot_sdk_provider._load_copilot_sdk",
        lambda: (FakeClient, FakePermissionHandler),
    )

    with pytest.raises(RuntimeError, match="timed out"):
        provider.generate(prompt="hello", model="gpt-5")


def test_copilot_sdk_provider_maps_gpt5_alias(monkeypatch):
    provider = CopilotSDKProvider(timeout_seconds=5)

    class FakePermissionHandler:
        approve_all = object()

    class FakeSession:
        async def send_and_wait(self, _prompt: str):
            return {"data": {"content": "mapped model response"}}

        async def disconnect(self):
            return None

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            self.session_kwargs: dict[str, object] = {}

        async def start(self):
            return None

        async def list_models(self):
            return [{"id": "gpt-5.2"}]

        async def create_session(self, **kwargs):
            self.session_kwargs = kwargs
            return FakeSession()

        async def stop(self):
            return None

    monkeypatch.setattr(
        "runner.llm_providers.copilot_sdk_provider._load_copilot_sdk",
        lambda: (FakeClient, FakePermissionHandler),
    )

    metadata: dict[str, object] = {}
    output = provider.generate(prompt="hello", model="gpt-5", metadata=metadata)
    assert output == "mapped model response"
    assert metadata["requested_model"] == "gpt-5"
    assert metadata["model"] == "gpt-5.2"


def test_extract_content_handles_nested_payload():
    payload = {"data": {"message": {"content": "nested response"}}}
    assert _extract_content(payload) == "nested response"
