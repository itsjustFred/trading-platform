"""CI workflow contract for #297: concurrency, uv cache, seam scope, agent gates.

Each assertion pins a property whose regression would be invisible in a green
build: a dropped `cancel-in-progress` only shows up as wasted runner minutes, a
lost `enable-cache` only as slower syncs, and a draft gate removed from an
agent workflow only as quota spent on unfinished PRs.
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


def _uses_steps(workflow: dict) -> list[str]:
    return [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job.get("steps") or []
        if "uses" in step
    ]


def _run_commands(workflow: dict) -> list[str]:
    return [
        " ".join(step["run"].split())
        for job in workflow["jobs"].values()
        for step in job.get("steps") or []
        if isinstance(step.get("run"), str)
    ]


def test_ci_cancels_only_superseded_pr_runs() -> None:
    workflow = _workflow("ci.yml")
    concurrency = workflow.get("concurrency")
    assert isinstance(concurrency, dict), "ci.yml must declare a concurrency group"
    assert "pull_request" in concurrency["group"], concurrency["group"]
    cancel = concurrency["cancel-in-progress"]
    assert "pull_request" in str(cancel), (
        "cancel-in-progress must be scoped to PR runs: cancelling a queued "
        "main push would leave a merged commit with no verdict"
    )


def test_ci_uses_setup_uv_with_cache() -> None:
    workflow = _workflow("ci.yml")
    uses = _uses_steps(workflow)
    setup_uv = [u for u in uses if u.startswith("astral-sh/setup-uv@")]
    assert setup_uv, "ci.yml must install uv via astral-sh/setup-uv"
    # The action is pinned to a full release tag, not a floating major.
    assert setup_uv[0].count("@") == 1 and "/" not in setup_uv[0].split("@", 1)[1]

    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps") or []
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    ]
    assert steps and steps[0].get("with", {}).get("enable-cache") is True, (
        "the uv cache must be enabled explicitly: the default 'auto' silently "
        "disables it on some event types"
    )
    # The old pip install must not come back next to the action.
    assert not any("pip install" in c and "uv==" in c for c in _run_commands(workflow))


def test_ruff_seam_scope_is_config_not_a_hand_list() -> None:
    seams = ROOT / "ruff-seams.toml"
    assert seams.is_file(), "the seam lint scope must live in ruff-seams.toml"
    config = tomllib.loads(seams.read_text(encoding="utf-8"))
    assert config.get("extend") == "pyproject.toml", (
        "ruff-seams.toml must extend pyproject.toml so select/per-file-ignores "
        "cannot drift from the advisory `ruff check .`"
    )
    include = config.get("include") or []
    assert include, "ruff-seams.toml must name the seam files"
    for pattern in include:
        assert (ROOT / pattern).is_file(), f"seam file missing: {pattern}"

    commands = _run_commands(_workflow("ci.yml"))
    seam_runs = [c for c in commands if "ruff check" in c and "seams" in c]
    assert seam_runs, "ci.yml must run ruff against the seam config"
    assert all("--config ruff-seams.toml" in c for c in seam_runs)
    # The hand-listed form must not return: no ruff invocation may carry the
    # seam filenames as arguments.
    assert not any("live_runner/__init__.py" in c for c in commands)


def test_agent_workflows_skip_drafts_and_split_default_models() -> None:
    review = _workflow("agent-code-review.yml")
    mutation = _workflow("agent-mutation-testing.yml")

    for name, workflow in (("review", review), ("mutation", mutation)):
        types = _on_block(workflow)["pull_request"]["types"]
        assert "ready_for_review" in types, (
            f"{name}: without ready_for_review a draft is never analysed"
        )
        resolve = workflow["jobs"]["resolve"]
        assert "draft == false" in str(resolve.get("if")), (
            f"{name}: the resolve job must skip drafts or every downstream "
            "job (and its quota) runs on unfinished PRs"
        )

    review_model = review["env"]["MODEL"]
    mutation_model = mutation["env"]["MODEL"]
    assert review_model != mutation_model, (
        "review and mutation must default to different models so one quota "
        "pool cannot starve both agent workflows"
    )
    for label, expr in (("REVIEW_MODEL", review_model), ("MUTATION_MODEL", mutation_model)):
        match = re.search(r"\|\|\s*'([^']+)'", expr)
        assert match, f"{label} must keep a quoted free-model default: {expr!r}"
        default = match.group(1)
        assert default.endswith("-free"), (
            f"{label} default {default!r} is not a free-tier model; the "
            "workflows hold no API key"
        )
