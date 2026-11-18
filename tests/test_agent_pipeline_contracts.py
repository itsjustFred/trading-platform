"""The agent workflows are independent per-push workflows, and the ones that report a verdict
do it totally.

The four pipeline workflows (`agent-code-review.yml`, `agent-mutation-testing.yml`,
`agent-pre-mortem.yml`, `agent-cubic-review.yml`) are hand-maintained YAML that each run on
their own on every push to a PR: none starts another, and each upserts one PR comment of its
own. Nothing in CI parses them, so a one-word regression - a renamed marker, a dropped
trigger, a verdict missing from the `case`, a token added to the agent job, a dispatch left
behind by an abandoned design - would merge silently and either post a red verdict as a green
status, run the rules of a commit nobody asked about, or strand a status `pending` on a SHA
with no error anywhere.

Two of them report a result on the commit itself: the pre-mortem sets the commit status
`agent/pre-mortem`, and the cubic gate sets `agent/cubic-review`. The siblings present as
comments and are pinned that way, so a chain-era copy-paste of a status write cannot slip in
unnoticed.

What is pinned here:
- every pipeline workflow triggers itself on `pull_request` (opened, synchronize, reopened,
  and, for the two that must run the moment a draft becomes ready, ready_for_review) with a
  concurrency group per PR that cancels superseded runs, and no workflow starts another one -
  no dispatch call, no `workflow_run` or `repository_dispatch` trigger, no `actions: write`;
- the pre-mortem's verdict map is total (`*)` -> `error`), so no verdict reaches an undeclared
  state, and its verdict regex accepts exactly the verdicts the skill documents;
- the status context, the comment marker and the comment title follow the stage convention, a
  marker belongs to exactly one workflow, and no workflow writes another workflow's context;
- the pre-mortem marks the analysed commit pending once, and every path that stops it - a
  cancelled run, a failed run, a failed post step - leaves that context terminal, never
  `pending`; a head that moved keeps its own status but no longer owns the comment;
- the cubic gate waits only for cubic's own review of the analysed commit, counts P0/P1 as
  blocking and P2/P3 as advisory, runs no repo code and no agent, and its every stop path
  leaves its own context terminal too;
- the free-model variables, the draft and fork skips and the read-only trust model follow the
  sibling workflows, whose comments swear to the same guards;
- the duplicated guard blocks the workflows' comments swear to keep in sync (summary-line
  extraction, fence strip, tree-clean check, the two-level skill selection) stay identical
  modulo the names each copy substitutes;
- the agent job that reads the checkout holds only `contents: read`, keeps no credentials, and
  fails the run when HEAD moved or the tree is dirty.

The chain this file used to pin (review -> mutation -> pre-mortem, dispatched stage by stage, on
the branch `test/agent-pipeline-chain`) is abandoned: the four are independent workflows now.
The tests that defended the chain were removed rather than rewritten, because the contract they
pinned no longer exists:

- `test_the_dispatch_chain_is_exactly_review_to_mutation_to_premortem` - pinned the dispatch
  edges and the `actions: write` a dispatching post job holds. With no chain there are no edges
  to pin, and the permission that only bought a dispatch must be gone: pinned as an absence in
  `test_no_agent_workflow_starts_another_workflow`.
- `test_the_push_marks_every_stage_pending_before_the_chain_starts` - pinned the review
  workflow's "Mark stages pending", i.e. stage 1 marking the whole chain on a push. No
  workflow marks another's stage now, so each marks only its own context, which is what the
  pre-mortem's "Mark running" is pinned against
  (`test_the_pre_mortem_marks_the_analysed_commit_pending_once`).
- `test_the_cancel_step_covers_both_downstream_workflows` - pinned the review workflow's
  "Cancel" step cancelling the downstream runs of an older commit. A superseded run is now
  cancelled by its own workflow's concurrency group, which
  `test_every_agent_workflow_is_a_standalone_per_push_workflow` pins instead.
- `test_a_superseded_or_early_cancelled_run_still_closes_its_status` - pinned the chain's
  "superseded, nothing to do" early exits, which closed *another* stage's context on its
  behalf. A stage stops no stage but itself now, and the stop paths that are left are pinned by
  `test_every_stop_path_leaves_the_stage_context_terminal`.
- `test_a_cancelled_run_clears_its_pending_stage` - pinned the matrix of contexts each chain
  stage owed downstream, which was the chain's shape. Replaced by
  `test_a_cancelled_run_leaves_no_pending_stage_status`, which pins the invariant instead of
  the matrix: a workflow that marks a status pending owns a reporter that clears it.
- `test_every_agent_workflow_parses_and_declares_its_chain_jobs` - its baseline checks were
  tangled with the chain's (`workflow_dispatch` `pr_number` on every stage). Replaced by
  `test_every_agent_workflow_is_a_standalone_per_push_workflow`.
- `test_every_stop_path_clears_every_later_stage_status` - pinned cross-stage clearing, a
  review or mutation stop path writing `agent/pre-mortem`. Replaced by the same-context
  version above.
- `test_the_dispatch_chain_is_exactly_review_to_mutation_to_premortem_to_cubic` and
  `test_a_ready_pr_runs_only_when_the_pre_mortem_passed_on_its_head` - pinned the cubic stage
  as the chain's last stage: dispatched by the pre-mortem's post job on PASS, and gated on the
  pre-mortem's verdict when a draft became ready. The cubic gate is a per-push workflow now,
  so the edges it was dispatched on are absent in
  `test_no_agent_workflow_starts_another_workflow`, and its triggers and its own stop paths are
  pinned by the cubic tests below.

Every other test in this file was kept and re-scoped to the workflows it is true of: the
verdict map and regex, the context/marker/title convention, the guard blocks kept in sync, the
opencode install, the resolve reporter, the head-unchanged and tree-clean guards, the
bash-level verdict extraction, and the bash-level cubic classification.
"""

from __future__ import annotations

import json
import os
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
CUBIC = "agent-cubic-review.yml"
AGENT_WORKFLOWS = (REVIEW, MUTATION, PREMORTEM)
PIPELINE_WORKFLOWS = (REVIEW, MUTATION, PREMORTEM, CUBIC)

# The workflows that report their verdict on the commit. The siblings present as comments and
# are pinned that way below, so a chain-era copy-paste of a status write cannot slip in
# unnoticed. Each entry maps its context to the env variable the workflow writes it through.
CONTEXT_OWNER = {
    "agent/pre-mortem": PREMORTEM,
    "agent/cubic-review": CUBIC,
}
CONTEXT_VAR = {
    PREMORTEM: "PREMORTEM_CONTEXT",
    CUBIC: "CUBIC_CONTEXT",
}

# marker -> the one workflow that owns it, so two comments never overwrite each other.
MARKERS = {
    "<!-- actions-code-review -->": REVIEW,
    "<!-- actions-mutation-testing -->": MUTATION,
    "<!-- actions-pre-mortem -->": PREMORTEM,
    "<!-- actions-cubic-review -->": CUBIC,
}

# The pre-mortem stage's own names: the context it reports (the value CONTEXT_OWNER maps), its
# comment marker and title, and the skill whose verdict line it must not drift from.
CONTEXT = "agent/pre-mortem"
MARKER = "<!-- actions-pre-mortem -->"
TITLE = "# Pre-Mortem Action"
SKILL_DIR = "actions-pre-mortem"

# The cubic gate's own names. It runs no agent: cubic reviews the PR itself and the workflow
# only gates on that review, so it has no skill, no agent job, no model and no verdict regex -
# its comment title and marker live in the post job that writes the comment.
CUBIC_CONTEXT = "agent/cubic-review"
CUBIC_MARKER = "<!-- actions-cubic-review -->"
CUBIC_TITLE = "# Cubic Review Action"
CUBIC_SKIP_DESCRIPTION = "skipped: draft PR — cubic reviews only ready PRs"

# Where each workflow takes its free model from. The variables are named after the stage, and
# all three fall back to the same model and variant, so a repo-wide change is one edit.
FREE_MODEL_VARS = {
    REVIEW: "REVIEW",
    MUTATION: "MUTATION",
    PREMORTEM: "PREMORTEM",
}
DEFAULT_MODEL = "opencode/inkling-2-small-free"
DEFAULT_VARIANT = "max"

# The verdicts the skill documents and the status each must get. INCOMPLETE is "the run produced
# no verdict" and is always an `error`, never a `failure`.
VERDICT_STATUS = {
    "PASS": "success",
    "BLOCK": "failure",
    "CHANGES-REQUESTED": "failure",
    "INCOMPLETE": "error",
}


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
    """The job that runs the agent: the one that installs opencode."""
    for job_name, job in data["jobs"].items():
        if any(s.get("name") == "Install opencode" for s in _steps(job)):
            return job_name, job
    raise AssertionError(f"no agent job in {list(data['jobs'])}")


def _all_run_bodies(data: dict) -> str:
    return _job_bodies(data["jobs"].values())


def _job_bodies(jobs) -> str:
    return "\n".join(_run(s) for job in jobs for s in _steps(job))


def _step_env(job: dict, step: dict) -> dict:
    """The env a step sees: its own, over the job's. The token lives here, so a job that posts
    declares it once at the job level and its steps inherit it."""
    env = dict(job.get("env") or {})
    env.update(step.get("env") or {})
    return env


def _verdict_arms(body: str) -> dict[str, str]:
    """The verdict -> status pairs the post job's `case` names, and whether the map is total."""
    case = re.search(r'case "\$VERDICT" in(.*?)\n\s*esac', body, re.S)
    assert case, 'the post job has no `case "$VERDICT"` map'
    arms = {}
    for arm, state in re.findall(r"^\s*([A-Z|-]+)\)\s*state=(\w+)\s*;;", case.group(1), re.M):
        for verdict in arm.split("|"):
            arms[verdict] = state
    assert arms, f"parsed no verdict arms from {case.group(1)!r}"
    # The map is total: the catch-all is `error`, so a verdict the agent produced is never
    # left in an undeclared state.
    assert "*) state=error" in re.sub(r"\s+", " ", case.group(1)), "the map is not total"
    return arms


def test_every_agent_workflow_is_a_standalone_per_push_workflow() -> None:
    """Each workflow starts itself, on every push to a PR, and cancels only its own superseded
    runs: the trigger list, the job shape and the concurrency group are the contract, and a
    workflow that stops running on `synchronize` would silently grade one commit forever."""
    for name in AGENT_WORKFLOWS:
        data = _load(name)
        jobs = data.get("jobs")
        assert isinstance(jobs, dict), f"{name}: jobs is not a mapping"

        triggers = _triggers(data)
        events = triggers["pull_request"]["types"]
        for required in ("opened", "synchronize", "reopened"):
            assert required in events, f"{name}: does not run on {required}"
        # One agent job that produces a comment, and (for the one workflow that reports it)
        # one job that posts it and sets the status. The chain's shape - a post job that
        # dispatched the next stage - is gone; what is left is a poster.
        job_name, agent = _agent_job(data)
        assert set(jobs) >= {"resolve", job_name, "post"}, f"{name}: jobs are {list(jobs)}"
        concurrency = data["concurrency"]
        group = str(concurrency["group"])
        assert group.startswith(name[: -len(".yml")]), f"{name}: group {group!r} is not its own"
        assert "pull_request.number" in group, f"{name}: group {group!r} is not per PR"
        assert concurrency["cancel-in-progress"] is True, f"{name}: keeps superseded runs"


def test_no_agent_workflow_starts_another_workflow() -> None:
    """The abandoned chain connected the stages by hand: a post job ran `gh workflow run`, held
    `actions: write` to allow it, and the next stage took a `workflow_run` trigger. Every one of
    those is now an absence worth pinning - a dispatch that survived a merge would re-run a
    stage nobody asked about, under rules nobody chose, on a head nobody analysed."""
    for name in PIPELINE_WORKFLOWS:
        data = _load(name)
        triggers = _triggers(data)
        for forbidden in ("workflow_run", "repository_dispatch"):
            assert forbidden not in triggers, f"{name}: {forbidden} trigger"
        bodies = _all_run_bodies(data)
        for call in ("gh workflow run", "createWorkflowDispatch", "/dispatches"):
            assert call not in bodies, f"{name}: starts another workflow with {call!r}"
        for job_name, job in data["jobs"].items():
            permissions = job.get("permissions") or {}
            assert permissions.get("actions") != "write", (
                f"{name}/{job_name}: holds actions: write, which only bought a dispatch"
            )


def test_the_pre_mortem_adds_ready_for_review_and_a_manual_rerun() -> None:
    """A draft is not pre-mortemed, and the moment it becomes ready is the first run that should
    happen - so `ready_for_review` is a trigger of its own, not a branch of the others. The
    `workflow_dispatch` covers what a push cannot: a run that failed for an infra reason, one
    superseded before it reported, or an older commit that never got a run. Both inputs reach
    the shell as env, never interpolated."""
    data = _load(PREMORTEM)
    triggers = _triggers(data)
    assert "ready_for_review" in triggers["pull_request"]["types"]
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["pr_number"]["required"] is True, "a rerun must name the PR"
    assert inputs["head_sha"]["required"] is False, "an older commit is optional"
    assert set(inputs) == {"pr_number", "head_sha"}
    resolve = data["jobs"]["resolve"]
    body = _run(_find_step(resolve, r"^Resolve PR$"))
    assert "INPUT_SHA" in body, "the resolve step ignores the head_sha input"
    assert '!= "$INPUT_SHA"' in body, "a rerun does not check the head it was given"
    assert "INPUT_PR" in body, "the pr_number input does not reach the shell by env"
    assert "INPUT_SHA" in body, "the head_sha input does not reach the shell by env"
    assert "${{" not in body, "the resolve step interpolates workflow expressions into a shell"


def test_the_pre_mortem_skips_drafts_and_forks() -> None:
    """The trust model of the read-only stage: fork PRs are skipped outright (no token, no
    checkout of the fork's code, no status on a SHA the repo cannot write to), drafts are
    skipped until they are ready, and the agent job holds `contents: read` only. The PR's own
    text reaches the agent as data - through the metadata JSON a step writes with jq - never
    interpolated into a shell."""
    data = _load(PREMORTEM)
    resolve = data["jobs"]["resolve"]
    assert "draft" in str(resolve.get("if", "")), "a draft PR is not skipped"
    agent_name, agent = _agent_job(data)
    assert agent.get("permissions") == {"contents": "read"}, (
        f"{agent_name} job permissions are {agent.get('permissions')}"
    )
    assert "same_repo" in str(agent.get("if", "")), "fork PRs are not skipped"
    checkout = next(
        s for s in _steps(agent) if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"].get("persist-credentials") is False, "checkout keeps credentials"
    for job_name, job in data["jobs"].items():
        for step in _steps(job):
            body = _run(step)
            if "statuses/" not in body and "issues/" not in body:
                continue
            assert job_name != agent_name, (
                f"the agent job posts a status or a comment ({step.get('name')!r})"
            )
            assert "GH_TOKEN" in _step_env(job, step), (
                f"{job_name}/{step.get('name')}: posts without the repo token"
            )
    for step in _steps(agent):
        assert "secrets." not in str(_step_env(agent, step)) + _run(step), (
            f"the agent job receives a secret ({step.get('name')!r})"
        )


def test_the_agent_workflows_share_one_free_model_configuration() -> None:
    """The variables are named after their stage and default to the same model and variant, so
    a reader can find the pre-mortem's knob next to the siblings' and a repo-wide change is one
    edit per line."""
    for name, prefix in FREE_MODEL_VARS.items():
        env = _load(name)["env"]
        assert env["MODEL"] == (
            f"${{{{ vars.{prefix}_MODEL || '{DEFAULT_MODEL}' }}}}"
        ), f"{name}: {env['MODEL']!r}"
        assert env["VARIANT"] == (
            f"${{{{ vars.{prefix}_VARIANT || '{DEFAULT_VARIANT}' }}}}"
        ), f"{name}: {env['VARIANT']!r}"
        _job_name, job = _agent_job(_load(name))
        bodies = _job_bodies([job])
        assert "$MODEL" in bodies, f"{name}: the agent is not configured from MODEL"
        assert "$VARIANT" in bodies, f"{name}: the agent is not configured from VARIANT"


def test_the_verdict_map_covers_every_verdict_the_skill_documents() -> None:
    """A verdict the post job's `case` does not name falls through to `*)` and is reported as
    `error` - a stage that could not run, indistinguishable from an agent failure; a verdict the
    map states but the regex drops never reaches it at all. Both are pinned against the skill's
    own verdict line, so the three cannot drift apart silently. INCOMPLETE is the "the run
    produced no verdict" outcome and is always an `error`, never a `failure`."""
    skill = (SKILLS / SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    verdicts_line = re.search(r"^- Verdicts:.*$", skill, re.M)
    assert verdicts_line, f"{SKILL_DIR}: the skill documents no verdict line"
    documented = set(re.findall(r"`([A-Z-]+)`", verdicts_line.group(0)))
    assert documented == set(VERDICT_STATUS), (
        f"skill verdicts {sorted(documented)} != map {sorted(VERDICT_STATUS)}"
    )
    post = _load(PREMORTEM)["jobs"]["post"]
    arms = _verdict_arms(_run(_find_step(post, r"^Set status$")))
    expected = {v: s for v, s in VERDICT_STATUS.items() if v != "INCOMPLETE"}
    assert arms == expected, f"status map {arms} != documented {expected}"


def test_the_verdict_regex_accepts_exactly_the_mapped_verdicts() -> None:
    """The regex decides what the workflow will post at all: an alternation wider than the map
    accepts a verdict the post job cannot place, and one that is narrower drops a documented
    verdict on the floor. The trailing annotation is allowed - a verdict line carries the reason
    and, for this stage, the hand-written sign-off note."""
    bodies = _all_run_bodies(_load(PREMORTEM))
    m = re.search(r"sed -nE 's/\^Verdict:\[\[:space:\]\]\*\((.*?)\)", bodies)
    assert m, "no verdict regex in the pre-mortem workflow"
    assert set(m.group(1).split("|")) == set(VERDICT_STATUS), (
        f"regex accepts {sorted(m.group(1).split('|'))}, map has {sorted(VERDICT_STATUS)}"
    )


def test_context_marker_and_title_belong_to_exactly_one_workflow() -> None:
    """A context typo writes a result under a name nothing reads; a marker two workflows share
    makes one overwrite the other's comment. The pre-mortem and the cubic gate are also the
    only workflows that report a status at all - the siblings present as comments, as
    `.agents/skills/platform-pr-verify` says - so a status write copied into one of them would
    make that comment lie about itself."""
    assert len(set(MARKERS.values())) == len(MARKERS), "two workflows share a comment marker"
    owner_of = {name: context for context, name in CONTEXT_OWNER.items()}
    for name in PIPELINE_WORKFLOWS:
        data = _load(name)
        env = data["env"]
        own_context = owner_of.get(name)
        bodies = _all_run_bodies(data)
        # Every context the workflow writes must be its own, through the variable it declares.
        for written in re.findall(r'-f context="([^"$][^"]*)"', bodies):
            assert written == own_context, f"{name}: writes a context it does not own ({written})"
        for var in re.findall(r'-f context="\$([A-Z_]+)"', bodies):
            assert var in env, f"{name}: writes context from undeclared variable ${var}"
            assert env[var] == own_context, f"{name}: ${var} is {env[var]!r}, not {own_context!r}"
        if own_context is None:
            # The workflows without a status write none: no context, no `statuses/` call.
            assert not [v for v in map(str, env.values()) if v.startswith("agent/")], (
                f"{name}: declares a status context"
            )
            assert "statuses/" not in bodies, f"{name}: writes a commit status"
        poster = _find_step(data["jobs"]["post"], r"^Post or update .* comment$")
        poster_body = _run(poster)
        for marker, owner_name in MARKERS.items():
            if owner_name == name:
                assert marker in poster_body, f"{name}: poster does not carry its own marker"
            else:
                assert marker not in poster_body, f"{name}: poster references {marker!r}"
    # The comment titles are set where each comment is written: the pre-mortem's agent job
    # writes its own comment, the cubic gate posts from its post job.
    _job_name, premortem_agent = _agent_job(_load(PREMORTEM))
    assert TITLE in _job_bodies([premortem_agent]), (
        "the pre-mortem comment title is not set by the agent job"
    )
    cubic_poster = _find_step(_load(CUBIC)["jobs"]["post"], r"^Post or update .* comment$")
    assert CUBIC_TITLE in _run(cubic_poster), "the cubic comment title is unset"


def test_the_pre_mortem_marks_the_analysed_commit_pending_once() -> None:
    """A push shows the stage as running from the first commit, and the mark goes to the commit
    being analysed - not, on a manual rerun, to the PR's current head. One write only: two
    pending marks would race, and a second one in an agent job would hand the agent a token."""
    data = _load(PREMORTEM)
    resolve = data["jobs"]["resolve"]
    mark = _find_step(resolve, r"^Mark running$")
    body = _run(mark)
    assert "-f state=pending" in body, "no pending mark"
    assert "$PREMORTEM_CONTEXT" in body, "the pending mark names no context variable"
    assert "steps.pr.outputs.head" in str(mark["env"]["HEAD_SHA"]), "the mark is not on the head"
    assert "same_repo" in str(mark.get("if", "")), "the mark runs for every PR, including forks"
    assert "number" in str(mark.get("if", "")), "the mark runs before the PR is resolved"
    assert _all_run_bodies(data).count("state=pending") == 1, "more than one pending write"


def test_a_superseded_result_does_not_overwrite_a_newer_comment() -> None:
    """A rerun of an older commit (an infra failure, a head that moved past it) must not replace
    the comment of the newer result: the comment is the PR's view of the head, so the poster
    compares the current head with the analysed SHA first and stops. Its status still lands on
    its own commit, which is the point of asking for it."""
    post = _load(PREMORTEM)["jobs"]["post"]
    body = _run(_find_step(post, r"^Post or update pre-mortem comment$"))
    assert '"$HEAD_SHA"' in body, "no head comparison"
    assert ".head.sha" in body, "the poster does not read the current head"
    assert "exit 0" in body, "a superseded result does not stop the job"
    assert "not updating the comment" in body, "no reason is logged"
    assert MARKER in body, "the poster carries no marker of its own"
    assert "gh api" in body, "the poster does not use the REST API"


@pytest.mark.parametrize("name", [PREMORTEM, CUBIC])
def test_every_stop_path_leaves_the_stage_context_terminal(name: str) -> None:
    """The bookkeeping that keeps a stage from stranding `pending`: a resolve failure, a failed
    run, a failed post step and a cancelled run each write `error` for their own context, and
    each converts a status that is still `pending` rather than overwriting a terminal one.
    Deleting a reporter, or writing a literal context somewhere, would leave a SHA `pending`
    with no error anywhere - or a typo'd context nothing reads."""
    data = _load(name)
    resolve, post = data["jobs"]["resolve"], data["jobs"]["post"]
    context = CONTEXT_VAR[name]
    # The two that report a failure of their own bookkeeping only clear a status that is still
    # pending, so a reporter's failure cannot overwrite a terminal verdict.
    clear_pending_only = ("Report resolve failure", "Report post failure")
    for job, pattern in (
        (resolve, r"^Report resolve failure$"),
        (post, r"^Report failed .* run$"),
        (post, r"^Report post failure$"),
    ):
        step = _find_step(job, pattern)
        body = _run(step)
        assert "state=error" in body, f"{step.get('name')!r} writes no error status"
        assert f"${context}" in body, f"{step.get('name')!r} writes no context variable"
        if step.get("name") in clear_pending_only:
            assert '"$state" = "pending"' in body, (
                f"{step.get('name')!r} overwrites a terminal status"
            )
            assert '-z "$state"' not in body, (
                f"{step.get('name')!r} writes a status where none exists"
            )
    for job_name, job in data["jobs"].items():
        for step in _steps(job):
            body = _run(step)
            if "statuses/" not in body:
                continue
            assert f"${context}" in body, (
                f"{job_name}/{step.get('name')}: writes a status without the context variable"
            )
            assert not re.search(r'-f context="agent/', body), (
                f"{job_name}/{step.get('name')}: writes a literal context"
            )


def test_a_cancelled_run_leaves_no_pending_stage_status() -> None:
    """A cancelled run - a hand-cancel, or a superseding run through the concurrency group -
    skips every failure() reporter and the post job, so without a dedicated job the stage
    stays `pending` on the SHA with no signal. The reporter only touches a still-pending
    context, so a newer run's terminal verdict survives. A workflow that marks nothing pending
    needs no reporter, which is what keeps this an invariant rather than a chain matrix."""
    for name in (PREMORTEM, CUBIC):
        data = _load(name)
        jobs = data["jobs"]
        if "state=pending" not in _all_run_bodies(data):
            continue
        job = jobs.get("report-cancelled")
        assert job is not None, f"{name}: writes a pending status with no cancellation reporter"
        assert "cancelled()" in str(job.get("if", "")), f"{name}: reporter is not gated"
        needs = job.get("needs") or []
        assert "post" in needs, f"{name}: reporter does not wait for the post job"
        body = _job_bodies([job])
        assert "state=error" in body, f"{name}: reporter writes no error"
        assert '[ "$state" = "pending" ]' in body, (
            f"{name}: reporter does not convert a pending status"
        )
        assert '-z "$state"' not in body, f"{name}: reporter writes where no status exists"
        assert f"${CONTEXT_VAR[name]}" in body, f"{name}: reporter writes no context variable"


@pytest.mark.parametrize("name", [PREMORTEM, CUBIC])
def test_the_resolve_reporter_falls_back_to_the_resolved_head(name: str) -> None:
    """A first-step failure writes no `head`, yet the run may already have marked the analysed
    commit pending: the reporter then falls back to the commit named by the event, and to the
    dispatch input, so a stranded `pending` still reaches an `error`."""
    step = _find_step(_load(name)["jobs"]["resolve"], r"^Report resolve failure$")
    sha = str(step.get("env", {}).get("HEAD_SHA", ""))
    assert sha.startswith("${{ steps.pr.outputs.head"), (
        f"{name}: the reporter must prefer the resolved head: {sha}"
    )
    assert "inputs.head_sha" in sha, f"{name}: no dispatch-input fallback: {sha}"
    assert "github.event.pull_request.head.sha" in sha, f"{name}: no event-head fallback: {sha}"
    assert step.get("if") == "failure()", f"{name}: the reporter is skipped on {step.get('if')!r}"


def test_the_free_model_usage_limit_is_an_infra_error_not_a_verdict() -> None:
    """The free Zen models cap usage per period, and hitting the cap fails the run. That is an
    infra failure, not a pre-mortem result: the run must fail (no comment is posted from it),
    the post job must report the status as `error`, and the log must name the cause, so the
    remedy - re-run the job - is the one the sibling workflows document."""
    data = _load(PREMORTEM)
    agent = _agent_job(data)[1]
    body = _run(_find_step(agent, r"^Run pre-mortem agent$"))
    patterns = re.search(r"grep -qiE '([^']+)'", body)
    assert patterns, "the agent step does not recognise a usage limit"
    for known in ("usage limit", "rate.?limit", "quota"):
        assert known in patterns.group(1), f"{known!r} is not recognised"
    assert "exit 1" in body, "a usage-limit failure does not fail the run"
    post = data["jobs"]["post"]
    report = _find_step(post, r"^Report failed pre-mortem run$")
    assert "state=error" in _run(report), "a failed run is not reported as an error"
    assert "needs.premortem.result == 'failure'" in str(post.get("if", "")), (
        "the post job does not run for a failed agent job"
    )
    for poster in (r"^Post or update pre-mortem comment$", r"^Set status$"):
        assert "needs.premortem.result == 'success'" in str(
            _find_step(post, poster).get("if", "")
        ), f"{poster} runs on a failed agent job"


def test_skill_selection_refuses_an_unreachable_base_and_keeps_two_levels() -> None:
    """The rules come from the base branch, so a PR cannot weaken its own run; the PR's copy is
    used only while the base has none. A base commit that cannot be read must abort the step -
    a silent fall-through would run the PR's own rules while the comment says "base branch".
    An unreachable base is also caught in the mutation workflow's scope step, which fails first,
    so what is pinned here is the check existing in the agent job, not its exact step."""
    for name in AGENT_WORKFLOWS:
        _job_name, job = _agent_job(_load(name))
        assert 'git cat-file -e "${BASE_SHA}^{commit}"' in _job_bodies([job]), (
            f"{name}: no hard base-commit check in the agent job"
        )
        assert 'git cat-file -e "${BASE_SHA}:${SKILL_PATH}"' in _job_bodies([job]), (
            f"{name}: does not read the skill from the base branch first"
        )
        assert 'cp "$SKILL_PATH"' in _job_bodies([job]), (
            f"{name}: no fallback to the PR's own copy"
        )
    step = _find_step(_agent_job(_load(PREMORTEM))[1], r"^Select skill revision$")
    body = _run(step)
    assert "set -euo pipefail" in body, "the selection is not strict"
    assert "SKILL_SOURCE=base branch" in body, "the selection does not record its source"
    # Two levels only: a third (the commit the workflow file came from) would analyse an older
    # commit under rules the branch tip happens to carry.
    assert "GITHUB_SHA" not in body, "the selection reads a third skill revision"
    assert body.count("elif") == 1, f"the selection has more than two levels:\n{body}"


def test_the_duplicated_comment_guards_stay_in_sync_across_the_copies() -> None:
    """The three workflows duplicate the blocks that decide what gets posted: the summary-line
    extraction, the fence strip, the head-unchanged and tree-clean check, and the output path.
    Their comments swear a change lands in all copies in the same commit; this test makes that
    true mechanically instead of by memory.

    The per-copy substitutions below are the documented ones (the temp directory, the agent id),
    applied agent-id first so "pr-review" cannot be eaten by the shorter "review". A guard that
    only one copy needs is not here: the pre-mortem's stricter verdict prelude (it tolerates the
    sign-off note the skill requires) and its head-moved comment guard are pinned separately.
    """
    substitutions = {
        REVIEW: (("pr-review", "<agent>"), ("review", "<stage>")),
        MUTATION: (("pr-mutation", "<agent>"), ("mutation", "<stage>")),
        PREMORTEM: (("pr-premortem", "<agent>"), ("premortem", "<stage>")),
    }
    guard_tags = ("grep -n", 'tail -n "+${start}"', "git status --porcelain", 'out="$RUNNER_TEMP')

    def guards(name: str) -> list[str]:
        out = []
        for job in _load(name)["jobs"].values():
            for step in _steps(job):
                for line in _run(step).splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):  # prose, not a guard
                        continue
                    if any(tag in line for tag in guard_tags):
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
        extra = [
            g
            for g in other_guards
            if g not in review_guards and not g.startswith('echo "**Summary:**')
        ]
        assert not extra, f"{other} has guard lines the review copy lacks:\n" + "\n".join(extra)


def test_the_agent_jobs_hold_no_token_and_verify_the_tree() -> None:
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


# --------------------------------------------------------------------------------------------
# The cubic gate: a standalone per-push gate on cubic's own review of the PR.
# --------------------------------------------------------------------------------------------

# Two real cubic review bodies, kept byte for byte as the shapes the classifier has to read:
# PR #185's review of the agent workflows (one P1) and PR #193's re-review (one P2, the body
# that says "changes from recent commits"). Their long lines are wrapped across adjacent
# literals only to stay inside ruff's line limit; concatenated, each body is the review text
# cubic posted. The synthetic shape below covers the levels they do not (P0, P3) and cubic's
# "No issues found" body, which lists no violation at all.
REAL_CUBIC_P1_BODY = (
    "<!-- cubic:review-summary:start -->\n"
    "**1 issue found** across 8 files\n"
    "<!-- cubic:review-summary:end -->\n"
    "\n"
    "<details>\n"
    "<summary>Prompt for AI agents (unresolved issues)</summary>\n"
    "\n"
    "```text\n"
    "\n"
    "Check if these issues are valid — if so, understand the root cause of each and fix them. "
    "When an issue isn't valid or won't be fixed in this PR, reply in its thread with the "
    "reason and then resolve the thread. If appropriate, use sub-agents to investigate and "
    "fix each issue separately.\n"
    "\n"
    "\n"
    '<file name=".github/workflows/agent-mutation-testing.yml">\n'
    "\n"
    '<violation number="1" location=".github/workflows/agent-mutation-testing.yml:415">\n'
    "P1: The mutation workflow runs from the PR ref, so this token-bearing `post` job is "
    "PR-controlled; PR-added steps can use `actions: write` to dispatch or cancel workflows. "
    "Move privileged dispatch logic to a trusted-ref workflow and pass the PR SHA as "
    "data.</violation>\n"
    "</file>\n"
    "```\n"
    "\n"
    "</details>\n"
    "\n"
    "<sub>**Tip**: Get a Slack DM when cubic finishes reviewing your PRs. "
    "[Connect Slack](https://www.cubic.dev/settings?tab=integrations&integration=slack"
    "&utm_source=github)<br /><br />[View guided diff]"
    "(https://www.cubic.dev/pr/itsjustFred/trading-platform/pull/185?utm_source=github"
    "&utm_medium=review_footer) | [Turn on auto-fix]"
    "(https://www.cubic.dev/action/auto-fix/pr/itsjustFred/trading-platform/185"
    "?returnTo=https%3A%2F%2Fgithub.com%2FitsjustFred%2Ftrading-platform%2Fpull%2F185) | "
    "[Re-trigger cubic](https://www.cubic.dev/action/re-review/pr/itsjustFred/trading-platform"
    "/185/ai_pr_review_1794587022913_bed0d7fe-4a29-449c-8590-e487f675c124"
    "?returnTo=https%3A%2F%2Fgithub.com%2FitsjustFred%2Ftrading-platform%2Fpull%2F185)</sub>\n"
    "\n"
    "<!-- cubic:review-post:ai_pr_review_1794587022913_bed0d7fe-4a29-449c-8590-e487f675c124"
    ":885e6f4abe078d19d4a79a9d74316cb4245366e1:3a8b138a-d997-5f86-bce2-75743e211b61 -->\n"
)

REAL_CUBIC_P2_BODY = (
    "<!-- cubic:review-summary:start -->\n"
    "**1 issue found across 1 file (changes from recent commits).**\n"
    "<!-- cubic:review-summary:end -->\n"
    "\n"
    "<details>\n"
    "<summary>Prompt for AI agents (unresolved issues)</summary>\n"
    "\n"
    "```text\n"
    "\n"
    "Check if these issues are valid — if so, understand the root cause of each and fix them. "
    "When an issue isn't valid or won't be fixed in this PR, reply in its thread with the "
    "reason and then resolve the thread. If appropriate, use sub-agents to investigate and "
    "fix each issue separately.\n"
    "\n"
    "\n"
    '<file name="scripts/_cubic_probe.py">\n'
    "\n"
    '<violation number="1" location="scripts/_cubic_probe.py:11">\n'
    "P2: `position_size` divides by the sum of entry and stop instead of the stop distance, "
    "so an entry of 100 and stop of 95 sizes against 195 rather than 5 and substantially "
    "under-risks the trade. Use the absolute price difference.</violation>\n"
    "</file>\n"
    "```\n"
    "\n"
    "</details>\n"
    "\n"
    "<sub>Reply with feedback, questions, or to request a fix.<br /><br />[View guided diff]"
    "(https://www.cubic.dev/pr/itsjustFred/trading-platform/pull/193?utm_source=github"
    "&utm_medium=review_footer) | [Turn on auto-fix]"
    "(https://www.cubic.dev/action/auto-fix/pr/itsjustFred/trading-platform/193"
    "?returnTo=https%3A%2F%2Fgithub.com%2FitsjustFred%2Ftrading-platform%2Fpull%2F193) | "
    "[Re-trigger cubic](https://www.cubic.dev/action/re-review/pr/itsjustFred/trading-platform"
    "/193/ai_pr_review_github_delivery_a1c6e09ff5f553669af3a2c82f6fe52a3ecd284bd7f362bff"
    "33ba7fde10a62c8?returnTo=https%3A%2F%2Fgithub.com%2FitsjustFred%2Ftrading-platform"
    "%2Fpull%2F193)</sub>\n"
    "\n"
    "<!-- cubic:review-post:ai_pr_review_github_delivery_a1c6e09ff5f553669af3a2c82f6fe52a3ecd"
    "284bd7f362bff33ba7fde10a62c8:9cb7f447993a2ae7d62881883ac6280ade405707"
    ":f85e2d53-210d-553d-9db6-13e80e2db57b -->\n"
)


def _cubic_body(*violations: str) -> str:
    """A cubic review body in the shape cubic posts: a summary block and the unresolved
    findings with their priority prefix inside the AI-agents prompt."""
    inner = "".join(
        f'<violation number="1" location="a.py:1">\n{v}\n</violation>\n' for v in violations
    )
    return (
        "<!-- cubic:review-summary:start -->\n**x**\n<!-- cubic:review-summary:end -->\n\n"
        "<details>\n<summary>Prompt for AI agents (unresolved issues)</summary>\n\n"
        "```text\n" + inner + "```\n\n</details>\n"
    )


def test_the_cubic_gate_is_a_standalone_per_push_workflow() -> None:
    """The cubic gate is a workflow of its own, not a stage somebody dispatches: it runs on
    every push to the PR itself (opened, a synchronize, a reopen, and the moment a draft
    becomes ready), it cancels only its own superseded runs, and a manual rerun names the PR -
    and, for an older commit, the SHA - through the dispatch inputs, which reach the shell as
    env, never interpolated."""
    data = _load(CUBIC)
    jobs = data["jobs"]
    assert isinstance(jobs, dict), f"{CUBIC}: jobs is not a mapping"
    triggers = _triggers(data)
    events = triggers["pull_request"]["types"]
    for required in ("opened", "synchronize", "reopened", "ready_for_review"):
        assert required in events, f"{CUBIC}: does not run on {required}"
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"pr_number", "head_sha"}, f"{CUBIC}: dispatch inputs {sorted(inputs)}"
    assert inputs["pr_number"]["required"] is True, "a rerun must name the PR"
    assert inputs["head_sha"]["required"] is False, "an older commit is optional"
    assert set(jobs) >= {"resolve", "wait", "post"}, f"{CUBIC}: jobs are {list(jobs)}"
    concurrency = data["concurrency"]
    group = str(concurrency["group"])
    assert group.startswith("agent-cubic-review"), f"{CUBIC}: group {group!r} is not its own"
    assert "pull_request.number" in group, f"{CUBIC}: group {group!r} is not per PR"
    assert concurrency["cancel-in-progress"] is True, f"{CUBIC}: keeps superseded runs"
    resolve = _run(_find_step(jobs["resolve"], r"^Resolve PR$"))
    assert "INPUT_PR" in resolve, "the resolve step ignores the pr_number input"
    assert "INPUT_SHA" in resolve, "the resolve step ignores the head_sha input"
    assert "${{" not in resolve, "the resolve step interpolates workflow expressions into a shell"


def test_the_cubic_gate_holds_no_secret_and_runs_no_repo_code() -> None:
    """The gate reads reviews and writes statuses and one comment; it runs no agent and no
    repo code, so there is nothing from the PR to sandbox and no checkout to make. The only
    secret is the default token the API calls need, and each job's permissions are the ones
    its own steps use - the narrower the better, since the workflow file itself runs from the
    PR's ref on pull_request."""
    data = _load(CUBIC)
    jobs = data["jobs"]
    for job_name, job in jobs.items():
        for step in _steps(job):
            assert not str(step.get("uses", "")).startswith("actions/checkout"), (
                f"{job_name}/{step.get('name')!r}: checks out the PR's code"
            )
    bodies = _all_run_bodies(data)
    for forbidden in ("opencode", "npm install"):
        assert forbidden not in bodies, f"the cubic gate runs {forbidden!r}"
    expected_permissions = {
        "resolve": {"pull-requests": "read", "statuses": "write"},
        "wait": {"pull-requests": "read"},
        "post": {"pull-requests": "write", "statuses": "write"},
        "report-cancelled": {"statuses": "write"},
    }
    for job_name, permissions in expected_permissions.items():
        assert jobs[job_name].get("permissions") == permissions, (
            f"{job_name}: permissions are {jobs[job_name].get('permissions')}"
        )
    for job_name, job in jobs.items():
        for step in _steps(job):
            secrets = [v for v in map(str, _step_env(job, step).values()) if "secrets." in v]
            for secret in secrets:
                assert secret == "${{ secrets.GITHUB_TOKEN }}", (
                    f"{job_name}/{step.get('name')!r}: holds {secret}"
                )


def test_the_cubic_gate_waits_only_for_cubics_own_review_of_the_head() -> None:
    """cubic reviews the PR itself, and the gate only waits for a review of the analysed
    commit: the poll is filtered to the cubic bot, the head SHA and cubic's own review-summary
    marker, so a review of an older or newer commit is not this gate's verdict. The wait is
    bounded (40 attempts, 30 s apart: 20 minutes), it stops the moment the head moves on, and
    the poster refuses to overwrite the comment of a head that moved past it."""
    data = _load(CUBIC)
    jobs = data["jobs"]
    for key in ("verdict", "blocking", "advisory", "review_id"):
        assert key in jobs["wait"].get("outputs", {}), f"the wait job publishes no {key}"

    wait = _run(_find_step(jobs["wait"], r"^Wait for cubic review$"))
    assert "pulls/${PR_NUMBER}/reviews" in wait, "the wait step does not read PR reviews"
    assert "commit_id == $sha" in wait, "the wait step accepts a review of another commit"
    assert "contains($marker)" in wait, "the wait step does not require cubic's summary marker"
    assert "seq 1 40" in wait, "the wait is not bounded to 40 attempts"
    assert "sleep 30" in wait, "the wait does not poll every 30 seconds"
    assert 'if [ "$head" != "$HEAD_SHA" ]' in wait, "the wait step never notices a moved head"

    poster = _run(_find_step(jobs["post"], r"^Post or update cubic-review comment$"))
    assert "not updating the comment" in poster, "the poster does not guard a moved head"
    assert "exit 0" in poster, "a moved head does not stop the poster"


def _cubic_classify(
    tmp_path, reviews: list[dict], found: str = "true", superseded: str = "false"
) -> dict:
    """Run the cubic workflow's own classification step on ``reviews``."""
    step = _find_step(_load(CUBIC)["jobs"]["wait"], r"^Classify cubic findings$")
    (tmp_path / "cubic").mkdir()
    (tmp_path / "cubic" / "reviews.json").write_text(json.dumps(reviews), encoding="utf-8")
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(out),
        "FOUND": found,
        "SUPERSEDED": superseded,
    }
    subprocess.run(
        [shutil.which("bash") or "bash", "-c", _run(step)],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env=env,
    )
    return dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("body", "found", "superseded", "verdict", "blocking", "advisory"),
    [
        # Nothing found: cubic's "No issues found" body has no violation block at all.
        (_cubic_body(), "true", "false", "PASS", "0", "0"),
        # A P1 blocks; a P2 alone does not.
        (_cubic_body("P1: a real defect"), "true", "false", "BLOCK", "1", "0"),
        (_cubic_body("P2: worth a look"), "true", "false", "PASS", "0", "1"),
        # P0 blocks as well, and the advisory levels are still counted.
        (
            _cubic_body("P0: live-trading data loss", "P1: wrong side", "P2: clarity", "P3: nit"),
            "true",
            "false",
            "BLOCK",
            "2",
            "2",
        ),
        # The real bodies: PR #185's P1 review blocks, PR #193's P2 re-review passes with one
        # advisory finding.
        (REAL_CUBIC_P1_BODY, "true", "false", "BLOCK", "1", "0"),
        (REAL_CUBIC_P2_BODY, "true", "false", "PASS", "0", "1"),
        # No cubic review of the head commit: the gate could not run, which is an error.
        (_cubic_body(), "false", "false", "INCOMPLETE", "0", "0"),
        # The head moved on while waiting: the newer push has its own run.
        (_cubic_body(), "false", "true", "SUPERSEDED", "0", "0"),
    ],
)
def test_the_cubic_priorities_map_to_the_stage_status(
    body, found, superseded, verdict, blocking, advisory, tmp_path
) -> None:
    """cubic's priorities are the gate: P0/P1 block, P2/P3 are advisory. The classification runs
    on the newest review of the head commit, and its verdicts must land in the status map the
    post job applies - a P1 reported as success would be the gate lying. The status mapping is
    exactly that: P0/P1 failure, P2/P3 success; the rule that a P2 is still handled like a
    should-fix finding lives in the skill, not in a redder status."""
    result = _cubic_classify(
        tmp_path,
        [{"id": 42, "body": body}] if found == "true" else [],
        found=found,
        superseded=superseded,
    )
    assert result["verdict"] == verdict
    assert result["blocking"] == blocking
    assert result["advisory"] == advisory

    post = _load(CUBIC)["jobs"]["post"]
    step = _run(_find_step(post, r"^Set status$"))
    case = re.search(r'case "\$VERDICT" in(.*?)\n\s*esac', step, re.S)
    assert case, 'the cubic post job has no `case "$VERDICT"` map'
    pairs = {}
    for arm, state in re.findall(r"^\s*([A-Z|]+)\)\s*state=(\w+);", case.group(1), re.M):
        for name in arm.split("|"):
            pairs[name] = state
    # The map is total: INCOMPLETE has no arm and must reach the catch-all, which is an error.
    assert "*) state=error" in re.sub(r"\s+", " ", case.group(1)), "the cubic map is not total"
    expected_state = {"PASS": "success", "BLOCK": "failure", "SUPERSEDED": "error"}.get(verdict)
    if expected_state is None:
        assert verdict not in pairs, f"{verdict} has an arm but is not a documented verdict"
    else:
        assert pairs.get(verdict) == expected_state, (
            f"{verdict} does not map to the documented status: {pairs}"
        )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_cubic_classification_reads_only_the_newest_review_of_the_commit(tmp_path) -> None:
    """cubic posts a new review on a re-review, and the older one stays. The newest review is
    the current verdict, as the newest status would be: a re-review that clears the finding must
    not stay blocked by the body of the review it replaced."""
    result = _cubic_classify(
        tmp_path,
        [
            {"id": 43, "body": _cubic_body()},
            {"id": 42, "body": _cubic_body("P1: already fixed")},
        ],
    )
    assert result["verdict"] == "PASS"
    assert result["blocking"] == "0"
    assert result["review_id"] == "43"


CUBIC_REPO = "itsjustFred/trading-platform"
CUBIC_PR_NUMBER = 201
CUBIC_PR_HEAD = "c" * 40

# A `gh` for the cubic workflow's own steps: a shell function, so no executable has to be found
# on PATH (portable to Git Bash). It logs every call to $GH_LOG, answers GETs of a pull request
# or of a commit status from the fixture files, and accepts every write without sending it.
_GH_STUB = r"""gh() {
  local path jq_expr method src
  printf '%s\n' "$*" >> "$GH_LOG"
  [ "${1:-}" = api ] || return 1
  path=""
  jq_expr=""
  method=GET
  shift
  while [ $# -gt 0 ]; do
    case "$1" in
      --jq) jq_expr=$2; shift 2 ;;
      --method) method=$2; shift 2 ;;
      -f|-F) shift 2 ;;
      -*) shift ;;
      *) path=$1; shift ;;
    esac
  done
  [ "$method" = GET ] || return 0
  case "$path" in
    */pulls/*) src=$GH_PR_JSON ;;
    */status) src=$GH_STATUS_JSON ;;
    *) echo "gh stub: unexpected GET $path" >&2; return 1 ;;
  esac
  if [ -n "$jq_expr" ]; then jq -r "$jq_expr" "$src"; else cat "$src"; fi
}
"""


def _cubic_pr_payload(*, draft: bool) -> dict:
    return {
        "number": CUBIC_PR_NUMBER,
        "state": "open",
        "draft": draft,
        "base": {"sha": "b" * 40},
        "head": {
            "sha": CUBIC_PR_HEAD,
            "ref": "chore/coderabbit-on-demand",
            "repo": {"full_name": CUBIC_REPO},
        },
    }


def _run_cubic_step(
    root: Path, step_body: str, *, env: dict[str, str], pr: dict, statuses: list[dict]
) -> tuple[subprocess.CompletedProcess, dict[str, str], str]:
    """Run one step of the cubic workflow with the stub `gh`. Returns the process, the outputs
    the step wrote to GITHUB_OUTPUT, and the calls it made to `gh`."""
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    (root / "pr.json").write_text(json.dumps(pr), encoding="utf-8")
    (root / "status.json").write_text(json.dumps({"statuses": statuses}), encoding="utf-8")
    (root / "event.json").write_text(json.dumps({"pull_request": pr}), encoding="utf-8")
    (root / "output").write_text("", encoding="utf-8")
    (root / "calls.log").write_text("", encoding="utf-8")
    run_env = {
        **os.environ,
        "CUBIC_CONTEXT": str(_load(CUBIC)["env"]["CUBIC_CONTEXT"]),
        "GH_LOG": str(root / "calls.log"),
        "GH_PR_JSON": str(root / "pr.json"),
        "GH_STATUS_JSON": str(root / "status.json"),
        "GITHUB_OUTPUT": str(root / "output"),
        "GITHUB_EVENT_PATH": str(root / "event.json"),
        "GITHUB_REPOSITORY": CUBIC_REPO,
        "RUNNER_TEMP": str(work),
        **env,
    }
    proc = subprocess.run(
        [shutil.which("bash") or "bash", "-c", _GH_STUB + "\n" + step_body],
        capture_output=True,
        text=True,
        check=False,
        cwd=work,
        env=run_env,
    )
    outputs = dict(
        line.split("=", 1)
        for line in (root / "output").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return proc, outputs, (root / "calls.log").read_text(encoding="utf-8")


NEEDS_BASH_AND_JQ = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None, reason="needs bash and jq"
)


@NEEDS_BASH_AND_JQ
def test_a_draft_head_resolves_without_a_call_and_is_written_as_a_success(tmp_path) -> None:
    """cubic does not review drafts, so a draft's gate must not wait 20 minutes for a review
    that never comes: the resolve step answers from the event payload alone (no API call, so
    there is nothing it could gate on), and the draft exit is a success that says why."""
    jobs = _load(CUBIC)["jobs"]
    resolve = _run(_find_step(jobs["resolve"], r"^Resolve PR$"))
    proc, outputs, calls = _run_cubic_step(
        tmp_path / "resolve",
        resolve,
        env={"GITHUB_EVENT_NAME": "pull_request", "INPUT_PR": "", "INPUT_SHA": ""},
        pr=_cubic_pr_payload(draft=True),
        statuses=[],
    )
    assert proc.returncode == 0, proc.stderr
    assert outputs["current"] == "true"
    assert outputs["draft"] == "true"
    assert calls == "", "resolving a draft calls the API, so it could gate on something"

    skip = _run(_find_step(jobs["resolve"], r"^Skip draft PR$"))
    proc, _outputs, calls = _run_cubic_step(
        tmp_path / "skip",
        skip,
        env={"HEAD_SHA": CUBIC_PR_HEAD},
        pr=_cubic_pr_payload(draft=True),
        statuses=[],
    )
    assert proc.returncode == 0, proc.stderr
    assert f"repos/{CUBIC_REPO}/statuses/{CUBIC_PR_HEAD}" in calls, (
        "the skip writes to another commit"
    )
    assert "-f state=success" in calls
    assert "-f context=agent/cubic-review" in calls
    assert f"-f description={CUBIC_SKIP_DESCRIPTION}" in calls
    assert "state=error" not in calls, "a draft head is written as an error"
    assert "state=pending" not in calls, "a draft head is marked running"


@NEEDS_BASH_AND_JQ
def test_a_draft_head_is_never_waited_for_and_is_written_as_a_success() -> None:
    """The draft exit is a static pin as well as a behaviour: the status the mark-running step
    writes and the wait job both exclude drafts, so a draft cannot sit `pending` for 20 minutes
    and end as `error`."""
    jobs = _load(CUBIC)["jobs"]
    assert "draft" in _find_step(jobs["resolve"], r"^Mark running$")["if"], (
        "Mark running does not exclude drafts"
    )
    assert "draft" in jobs["wait"]["if"], "the wait job does not exclude drafts"
    skip = _find_step(jobs["resolve"], r"^Skip draft PR$")
    assert "draft" in skip["if"], "the skip step runs on ready PRs"
    assert "state=success" in _run(skip), "the skip step does not write success"


@NEEDS_BASH_AND_JQ
@pytest.mark.parametrize(
    ("state", "closes"),
    [
        # A still-pending status of the superseded commit is closed, so it does not strand.
        ("pending", True),
        # A terminal status stands: the reporter converts a pending mark, nothing else.
        ("failure", False),
        ("success", False),
    ],
)
def test_a_superseded_rerun_closes_only_its_own_pending_status(state, closes, tmp_path) -> None:
    """A manual rerun that names a commit the head moved past has nothing to gate: the newer
    push has its own run. It closes what it marked on that commit - only its own context, and
    only while it is still `pending` - so no SHA is stranded, and a status another workflow
    owns is never touched."""
    stale = "d" * 40
    resolve = _run(_find_step(_load(CUBIC)["jobs"]["resolve"], r"^Resolve PR$"))
    proc, outputs, calls = _run_cubic_step(
        tmp_path,
        resolve,
        env={
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "INPUT_PR": str(CUBIC_PR_NUMBER),
            "INPUT_SHA": stale,
        },
        pr=_cubic_pr_payload(draft=False),
        statuses=[{"context": "agent/cubic-review", "state": state}],
    )
    assert proc.returncode == 0, proc.stderr
    assert outputs["current"] == "false", "a superseded rerun still runs the gate"
    assert CUBIC_PR_HEAD not in calls, "the superseded rerun reads or writes the new head"
    assert f"repos/{CUBIC_REPO}/commits/{stale}/status" in calls, (
        "the rerun does not read the status of the commit it was given"
    )
    if closes:
        assert "--method" in calls, "a pending status of the superseded commit is not closed"
        assert "-f state=error" in calls
        assert "-f context=agent/cubic-review" in calls
    else:
        assert "--method" not in calls, f"a {state} status is overwritten"


@NEEDS_BASH_AND_JQ
@pytest.mark.parametrize("head_sha", ["", CUBIC_PR_HEAD])
def test_a_dispatch_of_the_current_head_gates_normally(head_sha, tmp_path) -> None:
    """The manual rerun is how a rate-limited or failed gate runs again: it resolves the PR by
    number, gates the current head whether or not the SHA is named, and reads no other
    workflow's status - there is no chain left to wait for."""
    resolve = _run(_find_step(_load(CUBIC)["jobs"]["resolve"], r"^Resolve PR$"))
    proc, outputs, calls = _run_cubic_step(
        tmp_path,
        resolve,
        env={
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "INPUT_PR": str(CUBIC_PR_NUMBER),
            "INPUT_SHA": head_sha,
        },
        pr=_cubic_pr_payload(draft=False),
        statuses=[],
    )
    assert proc.returncode == 0, proc.stderr
    assert f"repos/{CUBIC_REPO}/pulls/{CUBIC_PR_NUMBER}" in calls, "the rerun does not read the PR"
    assert outputs["current"] == "true", "a rerun of the current head does not gate"
    assert outputs["head"] == CUBIC_PR_HEAD
    assert outputs["number"] == str(CUBIC_PR_NUMBER)
    assert outputs["same_repo"] == "true"
    assert outputs["draft"] == "false"
    assert "--method" not in calls, "resolving writes a status"
    assert "/status" not in calls, "the gate reads another workflow's status"


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
    """The workflow, not the test, decides what a verdict is: the extraction lines are lifted
    out of the YAML and run, so a verdict the regex accepts and a run that posts it cannot
    disagree. The `**Verdict:**` line must be the last non-empty line - a verdict quoted from
    the diff earlier in the output does not count."""
    assert _premortem_verdict(output, tmp_path) == expected
