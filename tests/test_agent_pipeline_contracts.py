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
import shutil
import subprocess
from pathlib import Path

import pytest
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
    "code-review": {
        "PASS": "success",
        "BLOCK": "failure",
        "CHANGES-REQUESTED": "failure",
        "INCOMPLETE": "error",
    },
    "mutation-testing": {"PASS": "success", "GAPS": "failure", "INCOMPLETE": "error"},
    "pre-mortem": {
        "PASS": "success",
        "BLOCK": "failure",
        "CHANGES-REQUESTED": "failure",
        "INCOMPLETE": "error",
    },
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
    return _job_bodies(data["jobs"].values())


def _job_bodies(jobs) -> str:
    return "\n".join(_run(s) for job in jobs for s in _steps(job))


def test_every_agent_workflow_parses_and_declares_its_chain_jobs() -> None:
    """The baseline: a workflow that does not parse cannot run at all, and its post job owns the
    status, so a workflow without one is not a stage."""
    for name in AGENT_WORKFLOWS:
        data = _load(name)
        jobs = data.get("jobs")
        assert isinstance(jobs, dict), f"{name}: jobs is not a mapping"
        assert "resolve" in jobs, f"{name}: no resolve job in {list(jobs)}"
        assert "post" in jobs, f"{name}: no post job in {list(jobs)}"
        triggers = _triggers(data)
        assert "pull_request" in triggers or "workflow_dispatch" in triggers, f"{name}: no trigger"
        assert "pr_number" in triggers["workflow_dispatch"]["inputs"], f"{name}: no pr_number input"
        assert isinstance(_agent_job(data)[1], dict), f"{name}: no agent job"
        assert "verdict" in _agent_job(data)[1].get("outputs", {}), (
            f"{name}: agent job publishes no verdict"
        )


def test_the_verdict_map_covers_every_verdict_the_skill_documents() -> None:
    """A verdict the post job's `case` does not name falls through to `*)` and is reported as
    `error` - a stage that could not run, indistinguishable from an agent failure."""
    for stage, mapping in VERDICT_STATUS.items():
        skill = (SKILLS / STAGES[stage][4] / "SKILL.md").read_text(encoding="utf-8")
        verdicts_line = re.search(r"^- Verdicts:.*$", skill, re.M)
        assert verdicts_line, f"{stage}: the skill documents no verdict line"
        documented = set(re.findall(r"`([A-Z-]+)`", verdicts_line.group(0)))
        assert documented == set(mapping), (
            f"{stage}: skill verdicts {sorted(documented)} != map {sorted(mapping)}"
        )

        data = _load(STAGES[stage][0])
        post = data["jobs"]["post"]
        body = _run(_find_step(post, STAGES[stage][5]))
        case = re.search(r'case "\$VERDICT" in(.*?)\n\s*esac', body, re.S)
        assert case, f'{stage}: post job has no `case "$VERDICT"` map'
        pairs = {}
        for arm, state in re.findall(r"^\s*([A-Z|-]+)\)\s*state=(\w+)\s*;;", case.group(1), re.M):
            for verdict in arm.split("|"):
                pairs[verdict] = state
        assert pairs, f"{stage}: parsed no verdict arms from {case.group(1)!r}"
        # The map is total: the catch-all is `error`, and INCOMPLETE reaches it (explicitly or
        # through the catch-all), so a verdict the agent produced can never go unplaced.
        assert "*) state=error" in re.sub(r"\s+", " ", case.group(1)), (
            f"{stage}: the map is not total"
        )
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
        assert accepted == set(mapping), (
            f"{stage}: regex accepts {sorted(accepted)}, map has {sorted(mapping)}"
        )


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
        poster_body = _run(poster)
        others = [s[2] for key, s in STAGES.items() if key != stage]
        assert marker in poster_body, f"{stage}: poster does not carry its own marker"
        assert not any(other in poster_body for other in others), (
            f"{stage}: poster references another stage's marker"
        )
        assert not any(m in bodies.split(marker)[0] for m in others if m != marker), (
            f"{stage}: body references another stage's marker"
        )
        _job_name, job = _agent_job(data)
        assert title in _job_bodies([job]), f"{stage}: comment title {title!r} unset"


def test_the_dispatch_chain_is_exactly_review_to_mutation_to_premortem() -> None:
    """The chain is the contract: a stage that dispatches nothing on PASS ends the pipeline
    silently, and a stage that dispatches the wrong workflow runs the wrong rules on the code.
    Only a stage that dispatches may hold `actions: write`; the last stage must not, because
    `gh workflow run` is the one write that token buys."""
    for stage, target in DISPATCHES.items():
        data = _load(STAGES[stage][0])
        post = data["jobs"]["post"]
        bodies = _job_bodies([data["jobs"]["post"]])
        dispatched = set(re.findall(r"gh workflow run (agent-[\w-]+\.yml)", bodies))
        # Every executed dispatch, in any job (echoed override hints excluded), must be one the
        # post job makes: a dispatch moved elsewhere would slip past the edge check below.
        executed = {
            m
            for line in _all_run_bodies(data).splitlines()
            if "gh workflow run" in line and "echo" not in line
            for m in re.findall(r"gh workflow run (agent-[\w-]+\.yml)", line)
        }
        assert executed == dispatched, (
            f"{stage}: dispatches {sorted(executed - dispatched)} outside the post job"
        )
        permissions = post.get("permissions", {})
        if target is None:
            assert not dispatched, f"{stage}: dispatches {sorted(dispatched)} but is the last stage"
            assert "actions" not in permissions, (
                f"{stage}: holds actions: write but dispatches nothing"
            )
            continue
        assert dispatched == {target}, (
            f"{stage}: dispatches {sorted(dispatched)}, want exactly {{{target}}}"
        )
        assert permissions.get("actions") == "write", f"{stage}: dispatches without actions: write"
        assert "-f pr_number=" in bodies, f"{stage}: dispatch does not pass the PR number"
        assert "-f head_sha=" in bodies, f"{stage}: dispatch does not pass the head SHA"


def test_the_duplicated_guard_blocks_stay_in_sync_across_the_copies() -> None:
    """The three workflows duplicate the blocks that decide what gets posted: the summary-line
    extraction, the verdict prelude, the tree-clean check and the head-moved comment guard.
    Their comments swear a change lands in all copies in the same commit; this test makes that
    true mechanically instead of by memory.

    The per-copy substitutions below are the documented ones (the temp directory, the agent
    id), applied agent-id first so "pr-review" cannot be eaten by the shorter "review".
    Anything else that differs is a fork of the gate.
    """
    substitutions = {
        REVIEW: (("pr-review", "<agent>"), ("review", "<stage>")),
        MUTATION: (("pr-mutation", "<agent>"), ("mutation", "<stage>")),
        PREMORTEM: (("pr-premortem", "<agent>"), ("premortem", "<stage>")),
    }
    guard_tags = (
        "**Summary:",
        "git status --porcelain",
        "not updating the comment",
        "grep -v '^[[:space:]]*$'",
    )

    def guards(name: str) -> list[str]:
        data = _load(name)
        out = []
        for job in data["jobs"].values():
            for step in _steps(job):
                for line in _run(step).splitlines():
                    if any(tag in line for tag in guard_tags):
                        line = line.strip()
                        for old, new in substitutions[name]:
                            line = line.replace(old, new)
                        out.append(line)
        return out

    review_guards = guards(REVIEW)
    assert review_guards, "parsed no guard lines out of the review workflow"
    for other in (MUTATION, PREMORTEM):
        other_guards = guards(other)
        missing = [g for g in review_guards if g not in other_guards]
        assert not missing, f"{other} is missing the shared guard lines:\n" + "\n".join(missing)
        # Both directions: a guard line added to one copy only is a fork as well. A stage's own
        # canned result (mutation testing's "nothing to mutate" summary) is output, not a guard.
        extra = [
            g
            for g in other_guards
            if g not in review_guards and not g.startswith('echo "**Summary:**')
        ]
        assert not extra, f"{other} has guard lines the review copy lacks:\n" + "\n".join(extra)


def test_opencode_is_installed_without_dependency_lifecycle_scripts() -> None:
    """Every `npm install` in the agent jobs passes `--ignore-scripts`, so no dependency's
    install hook runs on the runner; @opencode/cli's own postinstall (it links the platform
    binary) is then run by name. SonarCloud rates a plain `npm install` a security issue, and
    that one rule was what kept the quality gate red on every agent workflow PR."""
    for name in AGENT_WORKFLOWS:
        _job_name, job = _agent_job(_load(name))
        body = _run(_find_step(job, r"^Install opencode$"))
        installs = [line for line in body.splitlines() if "npm install" in line]
        assert installs, f"{name}: the install step runs no npm install"
        for line in installs:
            assert "--ignore-scripts" in line, f"{name}: npm install without --ignore-scripts"
        assert "node ./postinstall.mjs" in body, f"{name}: opencode's postinstall is not run"


def test_the_resolve_reporter_falls_back_to_the_resolved_head() -> None:
    """Every stage's resolve reporter clears the resolved head (the commit its own pending mark
    went to) and falls back to the dispatch input: a first-step failure writes no `head`, yet a
    push may already have marked the stages pending. No copy may skip the reporter on an
    empty step output (the review copy once did, stranding all three statuses)."""
    for name in AGENT_WORKFLOWS:
        step = _find_step(_load(name)["jobs"]["resolve"], r"^Report resolve failure")
        sha = str(step.get("env", {}).get("HEAD_SHA", ""))
        assert "steps.pr.outputs.head || inputs.head_sha" in sha, (
            f"{name}: reporter must prefer the resolved head and fall back to the input: {sha}"
        )
        assert step.get("if") == "failure()", f"{name}: reporter is skipped on {step.get('if')!r}"


def test_every_dispatch_refuses_a_head_that_moved_past_its_sha() -> None:
    """A dispatch that names head_sha is about that commit. The review override skips the
    SonarCloud gate, so once the head moves it must refuse instead of reviewing a commit
    nobody justified; the chained stages stop as superseded. Each copy compares the
    resolved head with the input before doing anything else."""
    for name in AGENT_WORKFLOWS:
        body = _run(_find_step(_load(name)["jobs"]["resolve"], r"^Resolve PR$"))
        assert "INPUT_SHA" in body, f"{name}: the resolve step ignores the head_sha input"
        assert '!= "$INPUT_SHA"' in body, f"{name}: no head-moved check against head_sha"


def test_a_cancelled_run_clears_its_pending_stage() -> None:
    """A cancelled run skips every failure() reporter and the post job, so without a dedicated
    job a hand-cancel or a superseding dispatch strands the stage `pending` on the head. The
    mutation and pre-mortem stages, which mark themselves running, each own a job that runs
    only on cancellation and turns their (and every later) still-pending context into an
    error."""
    owed = {MUTATION: ("MUTATION_CONTEXT", "PREMORTEM_CONTEXT"), PREMORTEM: ("PREMORTEM_CONTEXT",)}
    for name, contexts in owed.items():
        jobs = _load(name)["jobs"]
        job = jobs.get("report-cancelled")
        assert job is not None, f"{name}: no report-cancelled job"
        assert "cancelled()" in str(job.get("if", "")), f"{name}: report-cancelled is not gated"
        needs = job.get("needs", [])
        assert "post" in needs, f"{name}: report-cancelled does not wait for the post job"
        body = _job_bodies([job])
        assert "state=error" in body, f"{name}: report-cancelled writes no error"
        assert '= "pending"' in body, f"{name}: report-cancelled overwrites final statuses"
        for ctx in contexts:
            assert ctx in body, f"{name}: report-cancelled does not clear {ctx}"


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
        assert 'git cat-file -e "${BASE_SHA}^{commit}"' in body, (
            f"{name}: no hard base-commit check"
        )


def test_every_stop_path_clears_the_pre_mortem_status() -> None:
    """The bookkeeping this PR adds is what keeps the last stage from stranding `pending`.

    The review workflow marks all three stages pending on push, and every path that stops the
    chain early - SonarCloud red, the SonarCloud gate step itself failing, the review run
    failing, a verdict that is not PASS, a failed dispatch, a failed post step - must set
    `agent/pre-mortem` to error exactly as it does for the stage that stopped. The mutation
    workflow owes the same on its own stop paths, and the pre-mortem workflow owes it for its
    own status, so deleting any of the reporter steps (or one context out of them) would leave
    the status `pending` on the head SHA with no error anywhere - the failure the chain
    branch's last four commits existed to close. The reporters must therefore *exist*, by
    name, in the resolve and post jobs, and write their stage's context as an error.

    The stage's own starting mark (mutation's and the pre-mortem's "Mark running") is the one
    single-context pending write and is deliberately exempt: it marks the stage that is
    starting, not a chain that is stopping.
    """
    for name in AGENT_WORKFLOWS:
        data = _load(name)
        own = (
            "PREMORTEM_CONTEXT"
            if name == PREMORTEM
            else ("MUTATION_CONTEXT" if name == MUTATION else "REVIEW_CONTEXT")
        )
        resolve, post = data["jobs"]["resolve"], data["jobs"]["post"]
        for job, patterns in (
            (resolve, (r"^Report resolve failure",)),
            (post, (r"^Report failed .* run$", r"^Report post failure$")),
        ):
            for pattern in patterns:
                step = _find_step(job, pattern)
                body = _run(step)
                assert own in body, f"{name}: {step.get('name')!r} does not clear {own}"
                assert "state=error" in body, f"{name}: {step.get('name')!r} writes no error status"
        for job_name, job in data["jobs"].items():
            for step in _steps(job):
                body = _run(step)
                if "state=error" not in body and "state=pending" not in body:
                    continue
                contexts = set(re.findall(r"[A-Z][A-Z_]*_CONTEXT", body))
                assert contexts, (
                    f"{name}/{job_name}/{step.get('name')}: "
                    "writes a status with no context variable"
                )
                if "state=error" in body or len(contexts) > 1:
                    assert "PREMORTEM_CONTEXT" in contexts, (
                        f"{name}/{job_name}/{step.get('name')}: stops the chain for "
                        f"{sorted(contexts)} without clearing PREMORTEM_CONTEXT"
                    )


def test_the_push_marks_every_stage_pending_before_the_chain_starts() -> None:
    """On a push the review stage marks the whole chain pending, so the PR shows the pipeline
    as queued behind stage 1 from the first commit; a stage missing from that mark never shows
    a result at all until it runs."""
    data = _load(REVIEW)
    mark = _find_step(data["jobs"]["resolve"], r"^Mark stages pending$")
    body = _run(mark)
    for context in ("REVIEW_CONTEXT", "MUTATION_CONTEXT", "PREMORTEM_CONTEXT"):
        assert context in body, f"Mark stages pending does not mark {context}"


def test_the_cancel_step_covers_both_downstream_workflows() -> None:
    """A new push makes the downstream runs of older commits worthless; cancelling only the
    mutation runs would leave pre-mortem runs (of a stale SHA) to spend the same minutes."""
    data = _load(REVIEW)
    resolve = data["jobs"]["resolve"]
    cancel = _find_step(resolve, r"^Cancel")
    body = _run(cancel)
    for target in (MUTATION, PREMORTEM):
        assert target in body, f"the cancel step does not cover {target}"
    assert cancel.get("if"), "the cancel step must only run on a push"
    assert "pull_request" in cancel["if"], "the cancel step must only run on a push"


def test_the_agent_job_holds_no_token_and_verifies_the_tree() -> None:
    """The trust model of the read-only stages: the agent has a shell but no credential, and a
    checkout it modified invalidates the result, so the run must fail instead of posting."""
    for name in AGENT_WORKFLOWS:
        _job_name, job = _agent_job(_load(name))
        permissions = job.get("permissions", {})
        assert permissions == {"contents": "read"}, (
            f"{name}: agent job permissions are {permissions}"
        )
        checkout = next(
            s for s in _steps(job) if str(s.get("uses", "")).startswith("actions/checkout")
        )
        assert checkout["with"].get("persist-credentials") is False, (
            f"{name}: checkout keeps credentials"
        )
        bodies = _job_bodies([job])
        assert 'git rev-parse HEAD)" != "$HEAD_SHA"' in bodies, f"{name}: no HEAD-unchanged guard"
        assert "git status --porcelain" in bodies, f"{name}: no tree-clean guard"


def _premortem_verdict(output: str, tmp_path) -> str:
    """Run the pre-mortem workflow's own verdict-extraction lines on ``output``."""
    body = _run(_find_step(_agent_job(_load(PREMORTEM))[1], r"^Run pre-mortem agent$"))
    match = re.search(r"^\s*last=\$\(.*?/p'\)\s*$", body, flags=re.MULTILINE | re.DOTALL)
    assert match, "verdict extraction lines not found in 'Run pre-mortem agent'"
    out = tmp_path / "out.md"
    out.write_text(output, encoding="utf-8")
    script = f'out="{out.as_posix()}"\n{match.group(0)}\nprintf "%s" "$verdict"\n'
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        # The note on its own line after the verdict (seen on a real run, which then
        # refused to post a valid CHANGES-REQUESTED result).
        (
            "**Summary:** x\n\n**Verdict: CHANGES-REQUESTED** — harden.\n"
            "Human sign-off required.\n",
            "CHANGES-REQUESTED",
        ),
        ("**Summary:** x\n**Verdict: PASS** — advisory. Human sign-off required.\n", "PASS"),
        # Anything else after the verdict still invalidates it.
        ("**Summary:** x\n**Verdict: PASS** — ok\ntrailing chatter\n", ""),
        ("**Summary:** x\n> **Verdict: PASS** quoted\n", ""),
    ],
)
def test_pre_mortem_verdict_extraction(output: str, expected: str, tmp_path) -> None:
    assert _premortem_verdict(output, tmp_path) == expected
