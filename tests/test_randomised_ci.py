"""Pin the weekly randomised-order CI run (#293).

Two properties keep the tripwire honest:

* ``pytest-randomly`` must stay in the dev extra, or the weekly workflow syncs
  an environment without the plugin and every run is a silent no-op;
* ``pytest.ini`` must keep the plugin disabled by default, or local runs, PR
  runs, and agent workflows start shuffling and an order-dependent failure
  lands on an unrelated PR instead of on the weekly run that exists to catch
  it. The weekly workflow is the only place allowed to pass ``-p randomly``.
"""

from __future__ import annotations

import configparser
import re
import shlex
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RANDOMISED_WORKFLOW = "randomised-tests.yml"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _on_block(workflow: dict) -> dict:
    # PyYAML resolves the bare key `on` to True (YAML 1.1); accept both.
    return workflow.get("on", workflow.get(True))


def _run_commands(workflow: dict) -> list[str]:
    commands: list[str] = []
    for job in workflow["jobs"].values():
        for step in job.get("steps") or []:
            run = step.get("run")
            if isinstance(run, str):
                commands.append(" ".join(run.split()))
    return commands


def _pytest_addopts_tokens() -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(ROOT / "pytest.ini", encoding="utf-8")
    return shlex.split(parser["pytest"].get("addopts", ""))


def _disables_randomly(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if token == "-pno:randomly":
            return True
        if token == "-p" and index + 1 < len(tokens) and tokens[index + 1] == "no:randomly":
            return True
    return False


def test_pytest_randomly_is_a_dev_dependency() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    assert any(entry.startswith("pytest-randomly") for entry in dev), (
        "pytest-randomly must stay in the dev extra: the randomised-tests "
        "workflow installs --all-extras, so dropping it there turns the weekly "
        "run into a no-op"
    )


def test_declared_order_is_default_for_pytest() -> None:
    assert _disables_randomly(_pytest_addopts_tokens()), (
        "pytest.ini addopts must pass -p no:randomly: random order belongs to "
        "the weekly workflow only, and the plugin must stay installed but off "
        "for local/CI/agent runs"
    )


def test_weekly_randomised_workflow_shuffles_and_replays_seeds() -> None:
    workflow = _workflow(RANDOMISED_WORKFLOW)
    triggers = _on_block(workflow)
    assert "schedule" in triggers, "the randomised run must be scheduled, not PR-gated"
    assert "workflow_dispatch" in triggers, (
        "a seed replay needs workflow_dispatch with a seed input"
    )
    inputs = triggers["workflow_dispatch"].get("inputs") or {}
    assert "seed" in inputs, "workflow_dispatch must accept the seed to replay"

    pytest_runs = [c for c in _run_commands(workflow) if "pytest" in c]
    assert pytest_runs, "the workflow must actually run pytest"
    for command in pytest_runs:
        assert "-p randomly" in command or "--randomly-seed" in command, command
        assert "-p no:randomly" not in command, command
        # A replay seed must reach pytest through the extra array, not only env.
        assert 'extra+=(--randomly-seed=' in command, (
            f"seed replay path missing from: {command}"
        )
        assert '"${extra[@]}"' in command, f"seed arguments are not passed to pytest: {command}"


def test_only_the_weekly_workflow_enables_randomly() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = _workflow(path.name)
        commands = _run_commands(workflow)
        for job in workflow.get("jobs", {}).values():
            addopts = str((job.get("env") or {}).get("PYTEST_ADDOPTS") or "")
            if path.name != RANDOMISED_WORKFLOW:
                assert "-p randomly" not in addopts, (
                    f"{path.name} re-enables pytest-randomly through PYTEST_ADDOPTS"
                )
                assert "--randomly-seed" not in addopts, path.name
        for command in commands:
            if "pytest" not in command or path.name == RANDOMISED_WORKFLOW:
                continue
            assert "-p randomly" not in command, (
                f"{path.name} must not enable pytest-randomly: {command}"
            )
            assert "--randomly-seed" not in command, (
                f"{path.name} must not pass a pytest-randomly seed: {command}"
            )
            assert "PYTEST_RANDOMLY_SEED" not in command, (
                f"{path.name} must not set a pytest-randomly seed: {command}"
            )


def test_seed_reproduction_is_documented() -> None:
    """The workflow header is the runbook; pin the command shape it promises."""
    text = (WORKFLOWS / RANDOMISED_WORKFLOW).read_text(encoding="utf-8")
    assert "--randomly-seed" in text
    assert re.search(r"Using --randomly-seed", text)
