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
import subprocess

from runner.llm_providers.copilot_cli_provider import CopilotCLIProvider
from runner.llm_providers.copilot_sdk_provider import PERMISSION_POSTURE_NOTE


def _completed(*, stdout: str = '{"content":"ok"}', returncode: int = 0):
    return subprocess.CompletedProcess(args=["copilot"], returncode=returncode, stdout=stdout, stderr="")


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


def test_cli_provider_still_parses_json_output(monkeypatch):
    provider = CopilotCLIProvider(executable="copilot", timeout_seconds=5)

    def fake_run(command, **kwargs):
        assert kwargs.get("input") == "hello"
        return _completed(stdout='{"content": "generated"}')

    monkeypatch.setattr("subprocess.run", fake_run)
    assert provider.generate(prompt="hello") == "generated"


def test_permission_posture_is_documented():
    """approve_all is a deliberate, documented posture (F27)."""
    assert "approve_all" in PERMISSION_POSTURE_NOTE
    assert "generated" in PERMISSION_POSTURE_NOTE.lower()

    from runner.llm_providers import copilot_sdk_provider

    assert "PERMISSION_POSTURE_NOTE" in inspect.getsource(copilot_sdk_provider)
