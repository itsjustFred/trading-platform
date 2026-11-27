"""Unit tests for scripts/agents/quota_comment.py.

The "not run: quota" comment body used to be three hand-copied printf blocks
per path (probe-skip in the post job, mid-run in the agent step) across the
three agent workflows. The generator is now the single source; these tests
pin the exact body each stage/path produces, byte for byte, against the
wording the workflows carried before the extraction.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "agents" / "quota_comment.py"


def _load():
    spec = importlib.util.spec_from_file_location("quota_comment", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


quota_comment = _load()

BASE = "a" * 40
HEAD = "b" * 40
ENV = {
    "BASE_SHA": BASE,
    "HEAD_SHA": HEAD,
    "SKILL_PATH": ".agents/skills/actions-code-review/SKILL.md",
    "MODEL": "opencode/inkling-2-small-free",
    "VARIANT": "max",
}

ANALYSED = f"\n_Analysed base `{'a' * 8}` ... head `{'b' * 8}`._\n"


def _env(**overrides: str) -> dict[str, str]:
    env = dict(ENV)
    env.update(overrides)
    return env


def test_review_probe_body() -> None:
    assert quota_comment.build_body("review", "probe", _env()) == (
        "# Code Review Action\n\n"
        "**Summary:** Not run: quota - the free model was capped, rate-limited, "
        "refused or unavailable before the agent could start; no review was "
        "produced for this commit.\n\n"
        "Re-run the workflow after the quota resets.\n"
        f"{ANALYSED}"
        "\n---\n_Automated review not run: quota (actions-code-review skill)_\n"
    )


def test_mutation_probe_body() -> None:
    env = _env(SKILL_PATH=".agents/skills/actions-mutation-testing/SKILL.md")
    assert quota_comment.build_body("mutation", "probe", env) == (
        "# Mutation Testing Action\n\n"
        "**Summary:** Not run: quota - the free model was capped, rate-limited, "
        "refused or unavailable before the agent could start; no mutation "
        "testing was produced for this commit.\n\n"
        "Re-run the workflow after the quota resets.\n"
        f"{ANALYSED}"
        "\n---\n_Automated mutation testing not run: quota (`actions-mutation-testing` skill)_\n"
    )


def test_premortem_probe_body() -> None:
    env = _env(SKILL_PATH=".agents/skills/actions-pre-mortem/SKILL.md")
    assert quota_comment.build_body("premortem", "probe", env) == (
        "# Pre-Mortem Action\n\n"
        "**Summary:** Not run: quota - the free model was capped, rate-limited, "
        "refused or unavailable before the agent could start; no pre-mortem was "
        "produced for this commit.\n\n"
        "Re-run the workflow after the quota resets.\n"
        f"{ANALYSED}"
        "\n---\n_Automated pre-mortem not run: quota (actions-pre-mortem skill)_\n"
    )


def test_review_midrun_body() -> None:
    assert quota_comment.build_body("review", "midrun", _env()) == (
        "# Code Review Action\n\n"
        "**Summary:** Not run: quota - the free model was capped, rate-limited, "
        "refused or unavailable while the agent ran; no review was produced for "
        "this commit.\n\n"
        "Re-run the job after the quota resets.\n"
        f"{ANALYSED}"
        "\n---\n_Automated review not run: quota (actions-code-review skill)_\n"
    )


def test_mutation_midrun_body_is_untitled_and_has_no_footer() -> None:
    """The mutation Finish step titles every body and appends the model
    footer, so the mid-run quota body stays bare like the agent's report."""
    env = _env(SKILL_PATH=".agents/skills/actions-mutation-testing/SKILL.md")
    assert quota_comment.build_body("mutation", "midrun", env) == (
        "**Summary:** Not run: quota - the free model was capped, rate-limited, "
        "refused or unavailable while the agent ran; no mutation testing was "
        "produced for this commit.\n\n"
        "Re-run the job after the quota resets.\n"
        f"{ANALYSED}"
    )


def test_premortem_midrun_body_names_the_model_not_the_skill() -> None:
    env = _env(SKILL_PATH=".agents/skills/actions-pre-mortem/SKILL.md")
    assert quota_comment.build_body("premortem", "midrun", env) == (
        "# Pre-Mortem Action\n\n"
        "**Summary:** Not run: quota - the free model was capped, rate-limited, "
        "refused or unavailable while the agent ran; no pre-mortem was produced "
        "for this commit.\n\n"
        "Re-run the job after the quota resets.\n"
        f"{ANALYSED}"
        "\n---\n_Automated pre-mortem not run: quota (inkling-2-small-free (max))_\n"
    )


def test_premortem_midrun_body_without_variant() -> None:
    env = _env(SKILL_PATH=".agents/skills/actions-pre-mortem/SKILL.md", VARIANT="")
    body = quota_comment.build_body("premortem", "midrun", env)
    assert "(inkling-2-small-free)_" in body


def test_unknown_stage_or_when_is_refused() -> None:
    import pytest

    with pytest.raises(ValueError):
        quota_comment.build_body("bogus", "probe", _env())
    with pytest.raises(ValueError):
        quota_comment.build_body("review", "bogus", _env())
