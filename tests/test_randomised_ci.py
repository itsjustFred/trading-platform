"""Pin the weekly randomised-order CI run (#293).

Two properties keep the tripwire honest:

* ``pytest-randomly`` must stay in the dev extra, or the weekly workflow syncs
  an environment without the plugin and every run is a silent no-op;
* the ``ci`` workflow must keep disabling the plugin, or PR runs start
  shuffling and an order-dependent failure lands on an unrelated PR instead of
  on the weekly run that exists to catch it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


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


def test_pytest_randomly_is_a_dev_dependency() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    assert any(entry.startswith("pytest-randomly") for entry in dev), (
        "pytest-randomly must stay in the dev extra: the randomised-tests "
        "workflow installs --all-extras, so dropping it there turns the weekly "
        "run into a no-op"
    )


def test_weekly_randomised_workflow_shuffles_and_replays_seeds() -> None:
    workflow = _workflow("randomised-tests.yml")
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
        # A replay seed must reach pytest as an argument, not only via env.
        assert "--randomly-seed" in command or 'extra+=(--randomly-seed=' in command, (
            f"seed replay path missing from: {command}"
        )


def test_ci_workflow_keeps_declared_order() -> None:
    workflow = _workflow("ci.yml")
    python_jobs = [
        job
        for job in workflow["jobs"].values()
        if "PYTEST_ADDOPTS" in (job.get("env") or {})
        or any("pytest" in (s.get("run") or "") for s in job.get("steps") or [])
    ]
    assert python_jobs, "ci.yml must still run pytest somewhere"
    for job in python_jobs:
        env = job.get("env") or {}
        addopts = env.get("PYTEST_ADDOPTS", "")
        commands = [
            " ".join((s.get("run") or "").split())
            for s in job.get("steps") or []
            if "pytest" in (s.get("run") or "")
        ]
        disabled = "-p no:randomly" in addopts or any(
            "-p no:randomly" in c for c in commands
        )
        assert disabled, (
            "ci.yml pytest runs must pass -p no:randomly (directly or via "
            "PYTEST_ADDOPTS): random order belongs to the weekly workflow only"
        )


def test_seed_reproduction_is_documented() -> None:
    """The workflow header is the runbook; pin the command shape it promises."""
    text = (WORKFLOWS / "randomised-tests.yml").read_text(encoding="utf-8")
    assert "--randomly-seed" in text
    assert re.search(r"Using --randomly-seed", text)
