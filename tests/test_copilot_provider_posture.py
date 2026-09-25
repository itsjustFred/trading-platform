"""Trust-posture tests for the Copilot LLM providers (F27, F29).

Seams under test:
- ``runner.llm_providers.copilot_cli_provider.CopilotCLIProvider.generate``:
  the prompt must reach the CLI via stdin, not as a single argv element
  (Windows argv limit, ``ps``-readable credentials context).
- ``runner.llm_providers.copilot_sdk_provider``: the deliberate
  ``PermissionHandler.approve_all`` posture must be documented where the
  handler is installed, and surfaced in the session metadata.
"""

from __future__ import annotations

import inspect
import json
import subprocess

import pytest

from runner.llm_providers.copilot_cli_provider import CopilotCLIProvider
from runner.llm_providers.copilot_sdk_provider import PERMISSION_POSTURE_NOTE


def _assistant_event(content: str = "ok") -> str:
    return json.dumps(
        {
            "type": "assistant.message",
            "data": {"messageId": "m1", "content": content, "toolRequests": []},
        }
    )


def _completed(*, stdout: str | None = None, returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["copilot"],
        returncode=returncode,
        stdout=_assistant_event() if stdout is None else stdout,
        stderr="",
    )


def test_cli_provider_passes_prompt_via_stdin(monkeypatch):
    captured: dict[str, object] = {}
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=5)

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["input"] = kwargs.get("input")
        return _completed()

    monkeypatch.setattr("subprocess.run", fake_run)
    provider.generate(prompt="long prompt " * 5000, system_prompt="system instructions")

    command = captured["command"]
    assert "-p" in command
    assert command[command.index("-p") + 1] == "-"
    assert captured["input"] is not None
    assert "long prompt" in str(captured["input"])
    joined = " ".join(command)
    assert "long prompt" not in joined


def test_cli_provider_sanitizes_child_environment(monkeypatch):
    monkeypatch.setenv("CTRADER_APP_SECRET", "do-not-leak")
    monkeypatch.setenv("ALPACA_API_SECRET", "do-not-leak")
    captured: dict[str, object] = {}
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=5)

    def fake_run(command, **kwargs):
        captured["env"] = kwargs.get("env")
        return _completed()

    monkeypatch.setattr("subprocess.run", fake_run)
    provider.generate(prompt="hello")

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "CTRADER_APP_SECRET" not in child_env
    assert "ALPACA_API_SECRET" not in child_env
    assert "PATH" in child_env


def test_cli_provider_still_parses_json_output(monkeypatch):
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=5)

    def fake_run(command, **kwargs):
        assert kwargs.get("input") == "hello"
        return _completed(stdout=_assistant_event("generated"))

    monkeypatch.setattr("subprocess.run", fake_run)
    assert provider.generate(prompt="hello") == "generated"


def test_cli_provider_reads_the_assistant_message_event_from_jsonl(monkeypatch):
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=5)
    stdout = "\n".join(
        [
            json.dumps({"type": "user.message", "data": {"content": "hello"}}),
            _assistant_event("generated"),
            json.dumps({"type": "result", "exitCode": 0}),
        ]
    )

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: _completed(stdout=stdout),
    )
    assert provider.generate(prompt="hello") == "generated"


@pytest.mark.parametrize(
    "stdout",
    [
        "raw generated response",
        '{"content":"generated"}',
        '["generated"]',
    ],
)
def test_cli_provider_rejects_raw_or_malformed_json_envelopes(monkeypatch, stdout):
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=5)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: _completed(stdout=stdout),
    )

    with pytest.raises(RuntimeError, match="JSON event envelope"):
        provider.generate(prompt="hello")


def test_cli_provider_skips_unknown_json_event_types(monkeypatch):
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=5)
    stdout = "\n".join(
        [
            json.dumps({"type": "future.event", "data": {"content": "ignored"}}),
            _assistant_event("generated"),
            json.dumps({"type": "result", "exitCode": 0}),
        ]
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: _completed(stdout=stdout),
    )

    assert provider.generate(prompt="hello") == "generated"


def test_cli_provider_keeps_processing_after_observable_unknown_event(monkeypatch):
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=5)
    stdout = "\n".join(
        [
            _assistant_event("before"),
            json.dumps({"type": "future.event", "data": {"content": "ignored"}}),
            _assistant_event("after"),
            json.dumps({"type": "result", "exitCode": 0}),
        ]
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: _completed(stdout=stdout),
    )

    assert provider.generate(prompt="hello") == "after"


@pytest.mark.parametrize("exit_code", [1, "0"])
def test_cli_provider_rejects_failed_result_events(monkeypatch, exit_code):
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=5)
    stdout = json.dumps({"type": "result", "exitCode": exit_code})
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: _completed(stdout=stdout),
    )

    with pytest.raises(RuntimeError, match="failed result event"):
        provider.generate(prompt="hello")


def test_cli_provider_rejects_malformed_jsonl(monkeypatch):
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=5)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: _completed(stdout='{"type":"assistant.message"\n'),
    )

    with pytest.raises(RuntimeError, match="invalid JSON event envelope"):
        provider.generate(prompt="hello")


def test_cli_provider_rejects_unknown_output_format():
    with pytest.raises(ValueError, match="output_format"):
        CopilotCLIProvider(output_format="raw")


def test_permission_posture_is_documented():
    """approve_all is a deliberate, documented posture (F27)."""
    assert "approve_all" in PERMISSION_POSTURE_NOTE
    assert "generated" in PERMISSION_POSTURE_NOTE.lower()

    from runner.llm_providers import copilot_sdk_provider

    assert "PERMISSION_POSTURE_NOTE" in inspect.getsource(copilot_sdk_provider)
