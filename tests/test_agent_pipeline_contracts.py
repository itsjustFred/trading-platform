"""The agent pipeline's chain must stay a chain, and its verdict map must stay total.

The three agent workflows (`agent-code-review.yml`, `agent-mutation-testing.yml`,
`agent-pre-mortem.yml`) drive each other with hand-maintained YAML: one commit status per
stage, one comment per stage, a verdict-to-status map in the post job, and a dispatch of the
next workflow on PASS. Until this file existed nothing in CI parsed them, so a one-word
regression - a verdict missing from the `case`, a renamed marker, a dispatch dropped - would
merge silently and either strand a stage `pending` on the head SHA with no error anywhere, or
report a red verdict as a green status: a gate that says PASS for a finding it was shown.

The chain branch closed exactly this class of gap four times by hand (its commits
`fdf92ed`, `4fe3b75`, `55f9af7`, `6c08189`). This test is the mechanical version of those
fixes: it reads the workflows the CI run is about to execute and pins the invariants their
comments claim.

What is pinned here:
- every verdict a skill documents is mapped by the workflow's post job to the documented
  status, and the map is total (`*)` -> `error`), so no verdict reaches an undeclared state;
- the verdict regex accepts exactly the verdicts the map can handle - no more, no fewer;
- contexts, comment markers and comment titles follow the stage convention, and a marker is
  used by exactly one workflow, so comments never overwrite each other;
- the dispatch edges are exactly review -> mutation -> pre-mortem -> (nothing), and only a
  workflow that dispatches holds `actions: write`;
- the duplicated guard blocks the workflows' comments swear to keep in sync (summary-line
  extraction, tree-clean check, head-moved comment guard, strict skill selection) stay
  identical modulo the names each copy substitutes;
- the agent job that reads the checkout holds only `contents: read`, keeps no credentials,
  and fails the run when HEAD moved or the tree is dirty.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SKILLS = REPO_ROOT / ".agents" / "skills"

REVIEW = "agent-code-review.yml"
MUTATION = "agent-mutation-testing.yml"
PREMORTEM = "agent-pre-mortem.yml"
AGENT_WORKFLOWS = (REVIEW, MUTATION, PREMORTEM)
KNOWN_CONTEXTS = {"agent/code-review", "agent/mutation-testing", "agent/pre-mortem"}

# stage -> (workflow, status context, comment marker, comment title, skill dir, post-job step
# names for the status map and the comment poster)
STAGES = {
    "code-review": (
        REVIEW,
        "agent/code-review",
        "<!-- actions-code-review -->",
        "# Code Review Action",
        "actions-code-review",
        r"^Set status",
        r"^Post or update review comment$",
    ),
    "mutation-testing": (
        MUTATION,
        "agent/mutation-testing",
        "<!-- actions-mutation-testing -->",
        "# Mutation Testing Action",
        "actions-mutation-testing",
        r"^Set status",
        r"^Post or update mutation-testing comment$",
    ),
    "pre-mortem": (
        PREMORTEM,
        "agent/pre-mortem",
        "<!-- actions-pre-mortem -->",
        "# Pre-Mortem Action",
        "actions-pre-mortem",
        r"^Set status",
        r"^Post or update pre-mortem comment$",
    ),
}

# The documented verdict -> status map, per stage. INCOMPLETE is the "the run produced no
# verdict" outcome and is always an `error`, never a `failure`.
VERDICT_STATUS = {
    "code-review": {"PASS": "success", "BLOCK": "failure", "CHANGES-REQUESTED": "failure", "INCOMPLETE": "error"},
    "mutation-testing": {"PASS": "success", "GAPS": "failure", "INCOMPLETE": "error"},
    "pre-mortem": {"PASS": "success", "BLOCK": "failure", "CHANGES-REQUESTED": "failure", "INCOMPLETE": "error"},
}

# The chain: which workflow each stage dispatches on PASS. The last stage dispatches nothing;
# the CodeRabbit CLI after it is run by hand.
DISPATCHES = {"code-review": MUTATION, "mutation-testing": PREMORTEM, "pre-mortem": None}


def _load(name: str) -> dict:
    path = WORKFLOWS / name
    assert path.is_file(), f"missing workflow {path}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{name} did not parse to a mapping"
    return data


def _triggers(data: dict) -> dict:
    # PyYAML reads the YAML 1.1 boolean key `on` as True.
    on = data.get(True) or data.get("on")
    assert isinstance(on, dict), "no trigger block"
    return on


def _steps(job: dict) -> list[dict]:
    return [s for s in job.get("steps", []) if isinstance(s, dict)]


def _step_names(job: dict) -> list[str]:
    return [str(s.get("name")) for s in _steps(job)]


def _find_step(job: dict, name_pattern: str) -> dict:
    rx = re.compile(name_pattern)
    for step in _steps(job):
        if isinstance(step.get("name"), str) and rx.search(step["name"]):
            return step
    raise AssertionError(f"no step matching {name_pattern!r}; steps: {_step_names(job)}")


def _run(step: dict) -> str:
    return str(step.get("run", ""))


def _agent_job(data: dict) -> tuple[str, dict]:
    """The job that runs the agent: the one that installs opencode. In the review workflow that
    is `review`, not the `sonar-gate` job it also owns."""
    for job_name, job in data["jobs"].items():
        if any(s.get("name") == "Install opencode" for s in _steps(job)):
            return job_name, job
    raise AssertionError(f"no agent job in {list(data['jobs'])}")


def _all_run_bodies(data: dict) -> str:
    return "\n".join(_run(s) for job in data["jobs"].values() for s in _steps(job))


def test_every_agent_workflow_parses_and_declares_its_chain_jobs() -> None:
    """The baseline: a workflow that does not parse cannot run at all, and its post job owns the
    status, so a workflow without one is not a stage."""
    for name in AGENT_WORKFLOWS:
        data = _load(name)
        jobs = data.get("jobs")
        assert isinstance(jobs, dict) and "resolve" in jobs and "post" in jobs, f"{name}: {list(jobs)}"
        triggers = _triggers(data)
        assert "pull_request" in triggers or "workflow_dispatch" in triggers, f"{name}: no trigger"
        assert "pr_number" in triggers["workflow_dispatch"]["inputs"], f"{name}: no pr_number input"
        assert isinstance(_agent_job(data)[1], dict), f"{name}: no agent job"
        assert "verdict" in _agent_job(data)[1].get("outputs", {}), f"{name}: agent job publishes no verdict"


def test_the_verdict_map_covers_every_verdict_the_skill_documents() -> None:
    """A verdict the post job's `case` does not name falls through to `*)` and is reported as
    `error` - a stage that could not run, indistinguishable from an agent failure."""
    for stage, mapping in VERDICT_STATUS.items():
        skill = (SKILLS / STAGES[stage][4] / "SKILL.md").read_text(encoding="utf-8")
        verdicts_line = re.search(r"^- Verdicts:.*$", skill, re.M)
        assert verdicts_line, f"{stage}: the skill documents no verdict line"
        documented = set(re.findall(r"`([A-Z-]+)`", verdicts_line.group(0)))
        assert documented == set(mapping), f"{stage}: skill verdicts {sorted(documented)} != map {sorted(mapping)}"

        data = _load(STAGES[stage][0])
        post = data["jobs"]["post"]
        body = _run(_find_step(post, STAGES[stage][5]))
        case = re.search(r'case "\$VERDICT" in(.*?)\n\s*esac', body, re.S)
        assert case, f"{stage}: post job has no `case \"$VERDICT\"` map"
        pairs = {}
        for arm, state in re.findall(r"^\s*([A-Z|-]+)\)\s*state=(\w+)\s*;;", case.group(1), re.M):
            for verdict in arm.split("|"):
                pairs[verdict] = state
        assert pairs, f"{stage}: parsed no verdict arms from {case.group(1)!r}"
        # The map is total: the catch-all is `error`, and INCOMPLETE reaches it (explicitly or
        # through the catch-all), so a verdict the agent produced can never go unplaced.
        assert "*) state=error" in re.sub(r"\s+", " ", case.group(1)), f"{stage}: the map is not total"
        assert pairs.get("INCOMPLETE", "error") == "error", f"{stage}: INCOMPLETE is not an error"
        explicit = {v: s for v, s in mapping.items() if v != "INCOMPLETE"}
        assert pairs == explicit, f"{stage}: arms {pairs} != documented {explicit}"


def test_the_verdict_regex_accepts_exactly_the_mapped_verdicts() -> None:
    """The regex decides what the workflow will post at all: an alternation wider than the map
    accepts a verdict the post job cannot place, and one that is narrower drops a documented
    verdict on the floor."""
    for stage, mapping in VERDICT_STATUS.items():
        data = _load(STAGES[stage][0])
        m = re.search(r"sed -nE 's/\^Verdict:\[\[:space:\]\]\*\((.*?)\)", _all_run_bodies(data))
        assert m, f"{stage}: no verdict regex"
        accepted = set(m.group(1).split("|"))
        assert accepted == set(mapping), f"{stage}: regex accepts {sorted(accepted)}, map has {sorted(mapping)}"


def test_contexts_markers_and_titles_follow_the_stage_convention() -> None:
    """A context typo writes a stage's result under a name nothing reads; the markers are how
    each workflow finds its own comment to update, so they must be unique per stage."""
    markers = [s[2] for s in STAGES.values()]
    assert len(set(markers)) == len(markers), "two stages share a comment marker"
    for stage, (name, context, marker, title, _skill, _map_step, poster_step) in STAGES.items():
        data = _load(name)
        env = data.get("env", {})
        assert context in env.values(), f"{stage}: {context} is not declared in env"
        bodies = _all_run_bodies(data)
        # Every context the workflow writes must be one it declares (or the `$ctx` loop
        # variable over declared contexts); a literal typo fails here.
        for written in re.findall(r'-f context="([^"$][^"]*)"', bodies):
            assert written in KNOWN_CONTEXTS, f"{stage}: writes undeclared context {written!r}"
        for var in re.findall(r'-f context="\$([A-Z_]+)"', bodies):
            assert var in env, f"{stage}: writes context from undeclared variable ${var}"
        poster = _find_step(data["jobs"]["post"], poster_step)
        assert marker in _run(poster), f"{stage}: poster does not carry its own marker"
        others = [s[2] for key, s in STAGES.items() if key != stage]
        assert marker not in _run(poster).replace(marker, ""), f"{stage}: poster carries another marker"
        assert not any(m in bodies.split(marker)[0] for m in others if m != marker), (
            f"{stage}: body references another stage's marker"
        )
        _job_name, job = _agent_job(data)
        assert title in _run_body_of_all(job), f"{stage}: comment title {title!r} unset"


def test_the_dispatch_chain_is_exactly_review_to_mutation_to_premortem() -> None:
    """The chain is the contract: a stage that dispatches nothing on PASS ends the pipeline
    silently, and a stage that dispatches the wrong workflow runs the wrong rules on the code.
    Only a stage that dispatches may hold `actions: write`; the last stage must not, because
    `gh workflow run` is the one write that token buys."""
    for stage, target in DISPATCHES.items():
        data = _load(STAGES[stage][0])
        post = data["jobs"]["post"]
        bodies = _all_run_bodies({"jobs": {"post": post}})
        dispatched = set(re.findall(r"gh workflow run (agent-[\w-]+\.yml)", bodies))
        permissions = post.get("permissions", {})
        if target is None:
            assert not dispatched, f"{stage}: dispatches {sorted(dispatched)} but is the last stage"
            assert "actions" not in permissions, f"{stage}: holds actions: write but dispatches nothing"
            continue
        assert dispatched == {target}, f"{stage}: dispatches {sorted(dispatched)}, want exactly {{{target}}}"
        assert permissions.get("actions") == "write", f"{stage}: dispatches without actions: write"
        assert "-f pr_number=" in bodies and "-f head_sha=" in bodies, (
            f"{stage}: dispatch does not pass the PR number and the head SHA"
        )


def test_the_duplicated_guard_blocks_stay_in_sync_across_the_copies() -> None:
    """The three workflows duplicate the blocks that decide what gets posted: the summary-line
    extraction, the verdict prelude, the tree-clean check and the head-moved comment guard.
    Their comments swear a change lands in all copies in the same commit; this test makes that
    true mechanically instead of by memory.

    The per-copy substitutions below are the documented ones (the temp directory, the agent
    id). Anything else that differs is a fork of the gate.
    """
    substitutions = {
        REVIEW: ("review", "pr-review"),
        MUTATION: ("mutation", "pr-mutation"),
        PREMORTEM: ("premortem", "pr-premortem"),
    }
    guard_tags = ("**Summary:", "git status --porcelain", "not updating the comment", "grep -v '^[[:space:]]*$'")

    def guards(name: str) -> list[str]:
        data = _load(name)
        out = []
        for job in data["jobs"].values():
            for step in _steps(job):
                for line in _run(step).splitlines():
                    if any(tag in line for tag in guard_tags):
                        line = line.strip()
                        for old, new in ((substitutions[name][0], "<stage>"), (substitutions[name][1], "<agent>")):
                            line = line.replace(old, new)
                        out.append(line)
        return out

    review_guards = guards(REVIEW)
    assert review_guards, "parsed no guard lines out of the review workflow"
    for other in (MUTATION, PREMORTEM):
        other_guards = guards(other)
        missing = [g for g in review_guards if g not in other_guards]
        assert not missing, f"{other} is missing the shared guard lines:\n" + "\n".join(missing)


def test_skill_selection_runs_under_strict_mode_in_every_copy() -> None:
    """Without `set -euo pipefail` an unreachable base commit would not abort the step: the
    selection would fall through to the PR's own copy of the skill, letting a PR weaken its
    own review. GitHub's default `bash -eo pipefail` covers `e` and `pipefail`, not `u` - the
    copies promise the strict form, and the promise is what a reader relies on."""
    for name in AGENT_WORKFLOWS:
        _job_name, job = _agent_job(_load(name))
        step = _find_step(job, r"^Select skill revision$")
        body = _run(step)
        assert "set -euo pipefail" in body, f"{name}: skill selection is not strict"
        assert 'git cat-file -e "${BASE_SHA}^{commit}"' in body, f"{name}: no hard base-commit check"


def test_the_agent_job_holds_no_token_and_verifies_the_tree() -> None:
    """The trust model of the read-only stages: the agent has a shell but no credential, and a
    checkout it modified invalidates the result, so the run must fail instead of posting."""
    for name in AGENT_WORKFLOWS:
        _job_name, job = _agent_job(_load(name))
        permissions = job.get("permissions", {})
        assert permissions == {"contents": "read"}, f"{name}: agent job permissions are {permissions}"
        checkout = next(s for s in _steps(job) if str(s.get("uses", "")).startswith("actions/checkout"))
        assert checkout["with"].get("persist-credentials") is False, f"{name}: checkout keeps credentials"
        bodies = _run_body_of_all(job)
        assert 'git rev-parse HEAD)" != "$HEAD_SHA"' in bodies, f"{name}: no HEAD-unchanged guard"
        assert "git status --porcelain" in bodies, f"{name}: no tree-clean guard"


def _run_body_of_all(job: dict) -> str:
    return "\n".join(_run(s) for s in _steps(job))
