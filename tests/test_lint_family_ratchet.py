"""CI ratchet: the blocking lint/type-check steps must match the config they guard.

Two things can silently stop being true while the build stays green:

* the blocking `ruff check --select ...` step drifts from
  `[tool.ruff.lint].select`, so a family the config enables (and this repo drove
  to zero hits) is no longer pinned by any blocking check;
* mypy stops running from the synced dev environment, so the "run `uv run mypy`
  before a PR" instruction no longer describes what CI executes.

Both are checked here from the committed files, not from a live tool run.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
# The families pyproject.toml carried before this change; everything else in
# `select` is a family that was enabled only after every hit was fixed.
PRE_EXISTING = {"E", "F", "I", "W"}
MYPY_STEP = "Type check (mypy)"
RUFF_STEP = "Lint (bug-class families, blocking)"


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _steps() -> list[dict]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict) and jobs, "ci.yml must declare jobs"
    steps: list[dict] = []
    for job in jobs.values():
        steps.extend(job.get("steps") or [])
    return steps


def _step_by_name(name: str) -> dict:
    matches = [step for step in _steps() if step.get("name") == name]
    assert matches, f"ci.yml has no step named {name!r}"
    return matches[0]


def _command(step: dict) -> str:
    run = step.get("run")
    assert isinstance(run, str) and run.strip(), f"step {step.get('name')!r} has no run command"
    return " ".join(run.split())


def test_blocking_ruff_step_pins_every_bug_class_family() -> None:
    selected = set(_project()["tool"]["ruff"]["lint"]["select"])
    bug_class = selected - PRE_EXISTING
    assert bug_class, "pyproject.toml enables no bug-class family; the ratchet is vacuous"

    step = _step_by_name(RUFF_STEP)
    assert step.get("continue-on-error") is not True, (
        "the bug-class ruff step must stay blocking: `ruff check .` is advisory, "
        "so a new B/DTZ/PERF hit would land in a green build"
    )
    command = _command(step)
    assert "ruff check" in command, command

    # An ignore option undoes the pin as surely as dropping a family from
    # `--select`: `--select B,DTZ,... --ignore B` still "selects" every family
    # while silently making bugbear advisory again. Both the space-separated and
    # the `=`-joined forms are rejected, whichever argument position they take.
    ignore_options = {
        token
        for token in command.split()
        if token.split("=", 1)[0] in {"--ignore", "--extend-ignore"}
    }
    assert not ignore_options, (
        f"the blocking ruff step must not carry an ignore option: {sorted(ignore_options)}"
    )

    tokens = command.split()
    assert "--select" in tokens, command
    selected_tokens = tokens[tokens.index("--select") + 1].split(",")
    select_args = [token for token in selected_tokens if token in selected]
    assert set(select_args) == bug_class, (
        "the blocking ruff step and [tool.ruff.lint].select disagree: "
        f"missing={sorted(bug_class - set(select_args))} "
        f"extra={sorted(set(select_args) - bug_class)}"
    )


def test_mypy_step_runs_the_dev_dependency_without_an_overlay() -> None:
    dev = _project()["project"]["optional-dependencies"]["dev"]
    mypy_pins = [entry for entry in dev if entry.replace(" ", "").startswith("mypy")]
    assert mypy_pins, "mypy must be a dev dependency so `uv run mypy` works locally"
    assert any("==" in entry for entry in mypy_pins), (
        f"mypy must stay exactly pinned in the dev extra, got {mypy_pins}"
    )

    step = _step_by_name(MYPY_STEP)
    assert step.get("continue-on-error") is not True, "the mypy step must stay blocking"
    command = _command(step)
    assert command == "uv run --no-sync --no-build mypy", (
        "CI must run mypy from the synced env (the same command AGENTS.md gives); "
        f"got {command!r}"
    )
    assert "--with" not in command, (
        "a --with overlay installs a second copy of mypy outside the lock, so local "
        "and CI runs can disagree"
    )


def test_agents_md_documents_the_dev_install() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    # `--frozen` is part of the contract: without it a local sync can re-resolve
    # a different mypy than the one CI checks out with.
    assert "uv sync --frozen --extra dev" in agents
    assert "uv run mypy" in agents
