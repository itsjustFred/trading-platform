"""The agent workflows are independent per-push workflows, and the one that reports a verdict
does it totally.

The three agent workflows (`agent-code-review.yml`, `agent-mutation-testing.yml`,
`agent-pre-mortem.yml`) are hand-maintained YAML that each run on their own on every push to a
PR: none starts another, and each upserts one PR comment of its own. Nothing in CI parses them,
so a one-word regression - a renamed marker, a dropped trigger, a verdict missing from the
`case`, a token added to the agent job, a dispatch left behind by an abandoned design - would
merge silently and either post a red verdict as a green status, run the rules of a commit
nobody asked about, or strand a status `pending` on a SHA with no error anywhere.

The pre-mortem is the only one of the three that reports a result on the commit itself: for the
analysed SHA it sets the commit status `agent/pre-mortem` and upserts one PR comment.

What is pinned here:
- every agent workflow triggers itself on `pull_request` (opened, synchronize, reopened) with a
  concurrency group per PR that cancels superseded runs - the pre-mortem's is keyed by the
  analysed commit, so a manual rerun of an older commit cannot cancel the current head's run -
  and no workflow starts another one - no dispatch call, no `workflow_run` or
  `repository_dispatch` trigger, no `actions: write`;
- the pre-mortem's verdict map is total (`*)` -> `error`), so no verdict reaches an undeclared
  state, and its verdict regex accepts exactly the verdicts the skill documents;
- the verdicts are advisory (decision of 2026-11-20): no verdict reports `failure` on the
  analysed commit - PASS, BLOCK and CHANGES-REQUESTED all report `success` with the verdict in
  the status description and the ranked scenarios in the comment - so a pre-mortem that asks for
  hardening cannot hold up the head it was asked about;
- the status context, the comment marker and the comment title follow the stage convention, a
  marker belongs to exactly one workflow, and no workflow writes another workflow's context;
- the pre-mortem marks the analysed commit pending once, and every path that stops it - a
  cancelled run, a failed run, a failed post step, including the reporter of a failed agent run
  itself - converts a `pending` status this run wrote itself and never touches another run's:
  the commit status API has no conditional write, so the conversion reads the *newest* status of
  the context (sorted by creation time, since the API's own ordering is undocumented) and the run
  that wrote it, and what is left - the window between that read and the write - is stated as
  best effort in the reporters' comments; a head that moved keeps its own status but no longer
  owns the comment;
- every result the pre-mortem writes names the diff it was computed against: the analysed base
  and head, short, in the comment's footer and in every status description, so a verdict left
  behind by a base retarget, a base branch that moved or a head that moved on cannot be read as
  the current one;
- a base retarget re-runs the pre-mortem - `edited` is subscribed for it and only for it, since
  a title or body edit fires the same action and would spend model quota on a verdict that is
  already current;
- the base-commit guard of the skill selection is asserted on its non-comment line, and a copy
  with that line commented out fails the assertion - the substring check it replaced passed with
  the guard disabled in all three workflows at once;
- each workflow prints whose rules ran, so a verdict produced by the PR's own skill copy (the
  fallback while the base branch has none) is never indistinguishable from the base's;
- a manual rerun names a `head_sha` that is a full 40-hex SHA and one of that PR's own commits,
  so no rerun analyses a foreign diff or writes a verdict onto another PR's gate;
- the free-model variables, the draft and fork skips and the read-only trust model follow the
  sibling workflows, whose comments swear to the same guards;
- the duplicated guard blocks the workflows duplicate (summary-line extraction, fence strip,
  tree-clean check, the two-level skill selection) stay identical modulo the names each copy
  substitutes - the selection by a test of its own, which is the only way a copy that is not
  strict can fail it;
- the agent job that reads the checkout holds only `contents: read`, keeps no credentials, and
  fails the run when HEAD moved or the tree is dirty.

The chain this file used to pin (review -> mutation -> pre-mortem, dispatched stage by stage, on
the branch `test/agent-pipeline-chain`) is abandoned: the three are independent workflows now.
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
  behalf. A stage stops no stage but itself now, and the pre-mortem's own stop paths are pinned
  by `test_every_stop_path_leaves_the_pre_mortem_context_terminal`.
- `test_a_cancelled_run_clears_its_pending_stage` - pinned the matrix of contexts each chain
  stage owed downstream, which was the chain's shape. Replaced by
  `test_a_cancelled_run_leaves_no_pending_stage_status`, which pins the invariant instead of
  the matrix: a workflow that marks a status pending owns a reporter that clears it.
- `test_every_agent_workflow_parses_and_declares_its_chain_jobs` - its baseline checks were
  tangled with the chain's (`workflow_dispatch` `pr_number` on every stage). Replaced by
  `test_every_agent_workflow_is_a_standalone_per_push_workflow`.
- `test_every_stop_path_clears_the_pre_mortem_status` - pinned cross-stage clearing, a review
  or mutation stop path writing `agent/pre-mortem`. Replaced by the same-context version above.

Every other test in this file was kept and re-scoped to the workflows it is true of: the
verdict map and regex, the context/marker/title convention, the guard blocks kept in sync, the
opencode install, the resolve reporter, the head-unchanged and tree-clean guards, and the
bash-level verdict extraction.
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

# The workflow that reports its verdict on the commit. The siblings present as comments and are
# pinned that way below, so a chain-era copy-paste of a status write cannot slip in unnoticed.
CONTEXT_OWNER = {"agent/pre-mortem": PREMORTEM}

# marker -> the one workflow that owns it, so two comments never overwrite each other.
MARKERS = {
    "<!-- actions-code-review -->": REVIEW,
    "<!-- actions-mutation-testing -->": MUTATION,
    "<!-- actions-pre-mortem -->": PREMORTEM,
}

# The pre-mortem stage's own names: the context it reports (the value CONTEXT_OWNER maps), its
# comment marker and title, and the skill whose verdict line it must not drift from.
CONTEXT = "agent/pre-mortem"
MARKER = "<!-- actions-pre-mortem -->"
TITLE = "# Pre-Mortem Action"
SKILL_DIR = "actions-pre-mortem"

# Where each workflow takes its free model from. The variables are named after the stage, and
# all three fall back to the same model and variant, so a repo-wide change is one edit.
FREE_MODEL_VARS = {
    REVIEW: "REVIEW",
    MUTATION: "MUTATION",
    PREMORTEM: "PREMORTEM",
}
DEFAULT_MODEL = "opencode/inkling-2-small-free"
DEFAULT_VARIANT = "max"

# The verdicts the skill documents and the status each must get. The pre-mortem is advisory
# (decision of 2026-11-20): every verdict the agent can produce reports `success` on the
# analysed commit, and the verdict is what the status description says - a BLOCK is a ranked
# list of scenarios a human reads and acts on, not a gate that can fail the head under review.
# Only a run that produced no verdict at all is not a success: INCOMPLETE, the skill's own
# "inputs missing or unreadable" outcome, is always an `error`, never a `failure`.
VERDICT_STATUS = {
    "PASS": "success",
    "BLOCK": "success",
    "CHANGES-REQUESTED": "success",
    "INCOMPLETE": "error",
}


def _load(name: str) -> dict:
    path = WORKFLOWS / name
    assert path.is_file(), f"missing workflow {path}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{name} did not parse to a mapping"
    return data


def _parse(text: str) -> dict:
    """The same parse as `_load`, for a copy of a workflow a test has rewritten in memory."""
    data = yaml.safe_load(text)
    assert isinstance(data, dict), "the rewritten workflow did not parse to a mapping"
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


def _selection_lines(job: dict) -> list[str]:
    """The selection step's executable shell lines: stripped, prose comments dropped.

    A check that only exists on a `#` line is a check that does not run, so what the
    selection must contain is asserted against these lines rather than against the raw body -
    a substring over the body is satisfied by a commented-out guard, which is the regression
    `test_the_base_commit_guard_must_not_be_a_comment_line` pins.
    """
    lines = []
    for line in _run(_find_step(job, r"^Select skill revision$")).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _strictness_failures(lines: list[str]) -> list[str]:
    """What a strict skill selection must contain, matched within its executable lines: the
    standalone base-commit check that refuses an unreachable base instead of falling through to
    the PR's own rules, the two levels themselves, and the source both levels record. Matching
    inside the line (a guard written as `git cat-file -e ... 2>/dev/null; then`) keeps the check
    as strong as the substring it replaces, minus the prose that substring happily matched."""
    required = (
        ("set -euo pipefail", "the selection is not strict"),
        ('git cat-file -e "${BASE_SHA}^{commit}"', "does not refuse an unreachable base commit"),
        (
            'git cat-file -e "${BASE_SHA}:${SKILL_PATH}"',
            "does not read the skill from the base branch first",
        ),
        ('cp "$SKILL_PATH"', "no fallback to the PR's own copy"),
        ("SKILL_SOURCE=base branch", "records no skill source"),
    )
    return [message for needle, message in required if not any(needle in line for line in lines)]


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
    # The pre-mortem is the only one with a manual rerun, and it is the one whose group must
    # name the analysed commit: a PR-keyed group cancels the current head's in-flight run
    # when an older commit is re-dispatched, and that run's cancellation reporter then marks
    # the head `error` with no analysis left - a false red on the SHA the PR needs a verdict
    # for. Keyed by the SHA, a rerun of an older commit runs beside it, while a rerun of the
    # same commit still supersedes a stale run of that commit; a rerun that omits `head_sha`
    # shares the `current-head` bucket with its own siblings rather than with the push's run.
    premortem_group = str(_load(PREMORTEM)["concurrency"]["group"])
    assert "pull_request.head.sha" in premortem_group and "inputs.head_sha" in premortem_group, (
        f"the pre-mortem group {premortem_group!r} is not keyed by the analysed commit"
    )


def test_no_agent_workflow_starts_another_workflow() -> None:
    """The abandoned chain connected the stages by hand: a post job ran `gh workflow run`, held
    `actions: write` to allow it, and the next stage took a `workflow_run` trigger. Every one of
    those is now an absence worth pinning - a dispatch that survived a merge would re-run a
    stage nobody asked for, under rules nobody chose, on a head nobody analysed."""
    for name in AGENT_WORKFLOWS:
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
    assert "INPUT_PR" in body and "INPUT_SHA" in body, "the inputs reach the shell by env only"
    assert "${{" not in body, "the resolve step interpolates workflow expressions into a shell"


def test_a_base_retarget_reruns_the_pre_mortem_and_a_text_edit_does_not() -> None:
    """A retarget changes base...head, so the diff the last verdict covered no longer exists:
    a `success` status and a `# Pre-Mortem Action` comment for it are read as current by everyone
    who looks, and nothing else re-runs the stage - GitHub fires `edited` for a retarget and
    nothing else, and a base branch that advances on the PR's behalf fires nothing at all. So
    `edited` is subscribed. A title, body or label edit fires the same action, and re-analysing
    for it spends model quota on a verdict that is already current, so the run is skipped unless
    the event carries a changed base - the one part of the payload every retarget has, whichever
    of `ref`/`sha` GitHub fills inside it. A skipped resolve job skips every job that needs its
    outputs, so a text edit leaves the existing verdict and comment exactly where they were."""
    data = _load(PREMORTEM)
    types = _triggers(data)["pull_request"]["types"]
    assert "edited" in types, "a base retarget does not re-run the pre-mortem"
    guard = str(data["jobs"]["resolve"].get("if", ""))
    assert "draft" in guard, "the draft skip is gone from the resolve guard"
    assert "github.event.changes.base" in guard, (
        "an `edited` event re-runs the pre-mortem whatever changed"
    )
    # The base-change test is only reached for the `edited` action: every other action leaves
    # `changes` absent, so a guard without this comparison would skip all of them too.
    assert "github.event.action != 'edited'" in guard, (
        "the base-change guard is not scoped to the edited action"
    )


def test_a_manual_rerun_refuses_a_head_sha_that_is_not_this_prs_commit() -> None:
    """`head_sha` is the one input a caller types by hand, and it decides which diff is
    analysed and which commit carries the verdict. A well-formed SHA that belongs to another PR
    (a typo, a copy-paste, the head of a different open PR) would post a pre-mortem of a foreign
    diff under this PR's name, and would write a verdict onto another PR's gate - the pre-mortem's
    own scenario for it. So the shape is checked (a full 40-hex SHA) and, when it is not the PR's
    current head, that the commit is one of this PR's own via `pulls/N/commits`; either check
    failing - or a commit list that cannot be read at all - refuses the run instead of
    analysing whatever was typed."""
    data = _load(PREMORTEM)
    resolve = data["jobs"]["resolve"]
    step = _find_step(resolve, r"^Resolve PR$")
    body = _run(step)
    assert "grep -qE" in body and "^[0-9a-f]{40}$" in body, "the head_sha shape is unchecked"
    assert "grep -qxF" in body, "the PR's commit list is not matched exactly"
    assert "pulls/${INPUT_PR}/commits" in body, "the head_sha is not checked against the PR"
    # The list is captured before it is matched. Under `set -o pipefail` a `grep -q` that matches
    # exits at the first hit and closes the pipe, `gh` then dies of SIGPIPE writing the next
    # page, and the whole pipeline is non-zero - so piping straight off `gh api --paginate`
    # refuses a commit this PR does contain. This PR's own documented rerun is that case: an
    # older commit sits on the first page of a commit list that pages at 30.
    assert "commits=$(gh api --paginate" in body, (
        "the commit list is not captured into a variable before it is matched"
    )
    match_line = "printf '%s\\n' \"$commits\" | grep -qxF"
    assert match_line in body, "the captured list is not what gets matched"
    assert "--jq '.[].sha' | grep" not in body, (
        "the commit list is piped straight into grep, so a matching grep -q kills gh with SIGPIPE"
    )
    assert body.count("exit 1") >= 2, "a refused head_sha does not stop the run"
    assert "GH_TOKEN" in _step_env(resolve, step), "the dispatch path reads the API unauthenticated"
    assert resolve.get("permissions", {}).get("contents") == "read", (
        "the resolve job cannot read the PR's commit list, so the check cannot be made"
    )


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
        assert "$MODEL" in bodies and "$VARIANT" in bodies, (
            f"{name}: the agent is not configured from MODEL/VARIANT"
        )


def test_the_verdict_map_covers_every_verdict_the_skill_documents() -> None:
    """A verdict the post job's `case` does not name falls through to `*)` and is reported as
    `error` - a stage that could not run, indistinguishable from an agent failure; a verdict the
    map states but the regex drops never reaches it at all. Both are pinned against the skill's
    own verdict line, so the three cannot drift apart silently. INCOMPLETE is the skill's "inputs
    missing or unreadable" outcome and is always an `error`, never a `failure`."""
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


def test_no_verdict_ever_reports_a_blocking_failure() -> None:
    """The verdict is advisory, by decision of 2026-11-20: BLOCK and CHANGES-REQUESTED must not
    set `failure` on the analysed commit. The pre-mortem ranks what could break later and asks for
    a human judgement; a red status would fail the head the PR is asking to be reviewed, blocking
    a merge over that judgement, and the comment - not the status - is where the scenarios are
    read. What the status must still carry is the verdict itself: it is the first thing the
    description says, so a `success` state never hides which verdict produced it. Only a run that
    produced no verdict (INCOMPLETE, or the infra failure of the run) is not a success."""
    data = _load(PREMORTEM)
    arms = _verdict_arms(_run(_find_step(data["jobs"]["post"], r"^Set status$")))
    assert set(arms) == set(VERDICT_STATUS) - {"INCOMPLETE"}, f"map is {sorted(arms)}"
    assert "failure" not in arms.values(), (
        f"a verdict reports `failure` on the analysed commit: {arms}"
    )
    for verdict in arms:
        assert arms[verdict] == "success", f"{verdict} is not a success state"
    body = _run(_find_step(data["jobs"]["post"], r"^Set status$"))
    assert '-f description="Verdict: ${VERDICT}' in body, (
        "the status description drops the verdict, so a success state would hide it"
    )
    assert "advisory" in body, "the description does not say the verdict is advisory"


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
    makes one overwrite the other's comment. The pre-mortem is also the only one of the three
    that reports a status at all - the siblings present as comments and write no commit status -
    so a status write copied into one of them would make that comment lie about itself."""
    assert len(set(MARKERS.values())) == len(MARKERS), "two workflows share a comment marker"
    owner_of = {name: context for context, name in CONTEXT_OWNER.items()}
    for name in AGENT_WORKFLOWS:
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
    _job_name, agent = _agent_job(_load(PREMORTEM))
    assert TITLE in _job_bodies([agent]), "the comment title is not set by the agent job"


def test_the_pre_mortem_marks_the_analysed_commit_pending_once() -> None:
    """A push shows the stage as running from the first commit, and the mark goes to the commit
    being analysed - not, on a manual rerun, to the PR's current head. One write only: two
    pending marks would race, and a second one in an agent job would hand the agent a token."""
    data = _load(PREMORTEM)
    resolve = data["jobs"]["resolve"]
    mark = _find_step(resolve, r"^Mark running$")
    body = _run(mark)
    assert "-f state=pending" in body and "$PREMORTEM_CONTEXT" in body, "no pending mark"
    assert "steps.pr.outputs.head" in str(mark["env"]["HEAD_SHA"]), "the mark is not on the head"
    assert "same_repo" in str(mark.get("if", "")) and "number" in str(mark.get("if", "")), (
        "the mark runs for every PR, including forks"
    )
    assert _all_run_bodies(data).count("state=pending") == 1, "more than one pending write"


def test_a_superseded_result_does_not_overwrite_a_newer_comment() -> None:
    """A rerun of an older commit (an infra failure, a head that moved past it) must not replace
    the comment of the newer result: the comment is the PR's view of the head, so the poster
    compares the current head with the analysed SHA first and stops. Its status still lands on
    its own commit, which is the point of asking for it."""
    post = _load(PREMORTEM)["jobs"]["post"]
    body = _run(_find_step(post, r"^Post or update pre-mortem comment$"))
    assert '"$HEAD_SHA"' in body and ".head.sha" in body, "no head comparison"
    assert "exit 0" in body, "a superseded result does not stop the job"
    assert "not updating the comment" in body, "no reason is logged"
    assert MARKER in body, "the poster carries no marker of its own"
    assert "gh api" in body, "the poster does not use the REST API"
    # A second comment carrying the marker (a duplicate from an interrupted run, or one
    # created outside the workflow) used to be left alone by `tail -n 1`: the stale
    # `# Pre-Mortem Action` stayed readable next to the current one forever. Every match is
    # now updated, and the duplicate is logged rather than silently dropped.
    assert "for id in $existing" in body, "a duplicate marker comment is left stale"
    assert "::warning::" in body, "a duplicate marker comment is not reported"


def test_the_pre_mortem_status_names_the_pr_it_was_written_for() -> None:
    """`agent/pre-mortem` is one repo-global context, so two open PRs that share a head commit
    (`feat -> main` and `feat -> release`) both write it from analyses of different diffs, and
    the last write wins with no record of whose it was. Commit statuses cannot isolate per PR;
    what they can carry is the PR number in the description, which is the only thing that tells
    the two verdicts apart for a reader of the head's status."""
    post = _load(PREMORTEM)["jobs"]["post"]
    body = _run(_find_step(post, r"^Set status$"))
    assert "PR #${PR_NUMBER}" in body, "the status description does not name the PR"


def test_the_comment_and_the_status_record_the_analysed_shas() -> None:
    """A verdict is current for the diff it was computed against, and that diff is a
    base...head pair. The pair can move under the verdict - a retarget, a base branch that
    advanced, or a head that moved past the commit this run analysed - and neither the comment
    nor the status used to say which pair it was, so a stale verdict read as the current one
    and the only way to tell was the run log. Both now carry the two SHAs, short so they fit:
    the description has GitHub's 140 characters to work with and still has to name the verdict,
    the commit and the PR."""
    data = _load(PREMORTEM)
    body = _run(_find_step(_agent_job(data)[1], r"^Run pre-mortem agent$"))
    lines = body.splitlines()
    footer_at = next(i for i, line in enumerate(lines) if "skill (rules from %s)" in line)
    footer = "\n".join(lines[footer_at - 4 : footer_at + 3])
    assert "${BASE_SHA:0:8}" in footer and "${HEAD_SHA:0:8}" in footer, (
        "the comment footer does not name the analysed base and head"
    )
    assert '>> "$out"' in footer, "the analysed pair is not written to the comment"
    step = _find_step(data["jobs"]["post"], r"^Set status$")
    description = re.search(r'-f description="([^"]+)"', _run(step)).group(1)
    for required in ("${HEAD_SHA:0:8}", "${BASE_SHA:0:8}", "PR #${PR_NUMBER}"):
        assert required in description, f"the status description drops {required}"
    # GitHub truncates a status description at 140 characters, and a truncated description
    # loses the PR number it carries the verdict for.
    longest = (
        description.replace("${VERDICT}", "CHANGES-REQUESTED")
        .replace("${HEAD_SHA:0:8}", "01234567")
        .replace("${BASE_SHA:0:8}", "01234567")
        .replace("${PR_NUMBER}", "1234")
    )
    assert len(longest) <= 140, f"status description is {len(longest)} chars: {longest!r}"


def test_every_error_status_names_the_analysed_commit() -> None:
    """An `error` that names no SHA is read against whatever commit the reader happens to be
    looking at: the context is repo-global, two open PRs can share a head commit, and a run of
    a superseded commit still writes its status. The analysed SHA is what makes the error
    readable, and it costs nothing in the description."""
    data = _load(PREMORTEM)
    reporters = (
        (data["jobs"]["resolve"], r"^Report resolve failure$"),
        (data["jobs"]["post"], r"^Report failed pre-mortem run$"),
        (data["jobs"]["post"], r"^Report post failure$"),
        (data["jobs"]["report-cancelled"], r"^Report cancelled pre-mortem run$"),
    )
    for job, pattern in reporters:
        body = _run(_find_step(job, pattern))
        description = re.search(r'-f description="([^"]+)"', body)
        assert description, f"{pattern} writes no description"
        assert "${HEAD_SHA:0:8}" in description.group(1), (
            f"{pattern}: the error status does not name the commit it was written for"
        )


def test_the_pending_conversion_is_identity_bearing() -> None:
    """Every stop path converts a `pending` status into an `error`, and the commit status API has
    no conditional write - so the conversion is a read-then-write and two runs of one commit can
    collide (a rerun of the same SHA in the same concurrency bucket, or a dispatch running
    beside the push's run). Reading whatever `[0]` happens to be and converting it when it is
    pending converts a concurrent run's status too, which is how a valid verdict ends up labelled
    `Pre-mortem run cancelled`. So the candidates are pinned down as far as the API allows - the
    newest status of the context, sorted by creation time because the API's own ordering is
    undocumented, and the run that wrote it - and only this run's own `pending` mark is
    converted. What is left is the window between that read and the write, which no status API
    can close; the reporters' comments call it best effort rather than claim the invariant."""
    data = _load(PREMORTEM)
    reporters = (
        (data["jobs"]["resolve"], r"^Report resolve failure$"),
        (data["jobs"]["post"], r"^Report failed pre-mortem run$"),
        (data["jobs"]["post"], r"^Report post failure$"),
        (data["jobs"]["report-cancelled"], r"^Report cancelled pre-mortem run$"),
    )
    for job, pattern in reporters:
        body = _run(_find_step(job, pattern))
        assert "sort_by(.created_at)" in body, (
            f"{pattern}: reads the first status the API happens to return, not the newest"
        )
        assert "--arg ctx" in body and "--arg url" in body, (
            f"{pattern}: reads the statuses without the context or without this run's URL"
        )
        assert "target_url" in body and "startswith($url)" in body, (
            f"{pattern}: converts a pending mark this run did not write"
        )
        assert '"$owner" = "true"' in body, (
            f"{pattern}: the ownership of the pending mark is not checked"
        )
    # The mark has to be attributable in the first place: without the run URL on it, no reporter
    # could tell its own pending status from another run's.
    mark = _run(_find_step(data["jobs"]["resolve"], r"^Mark running$"))
    assert '-f target_url="$RUN_URL"' in mark, "the pending mark names no run"


def test_every_stop_path_leaves_the_pre_mortem_context_terminal() -> None:
    """The bookkeeping that keeps the stage from stranding `pending`: a resolve failure, a failed
    agent run, a failed post step and a cancelled run each write `error` for their own context,
    and each converts a `pending` status this run wrote itself rather than any still-pending one -
    including the reporter of the failed agent run, whose family (a rerun of the same commit
    that ran beside this one through the concurrency group) is exactly the case where a terminal
    status is already there. Deleting a reporter, or writing a literal context somewhere, would
    leave a SHA `pending` with no error anywhere - or a typo'd context nothing reads."""
    data = _load(PREMORTEM)
    resolve, post = data["jobs"]["resolve"], data["jobs"]["post"]
    # Every reporter of a stopped run only clears a status that is still pending, so a failure of
    # one run cannot overwrite the terminal verdict of another run of the same commit.
    clear_pending_only = (
        "Report resolve failure",
        "Report failed pre-mortem run",
        "Report post failure",
    )
    for job, pattern in (
        (resolve, r"^Report resolve failure$"),
        (post, r"^Report failed pre-mortem run$"),
        (post, r"^Report post failure$"),
    ):
        step = _find_step(job, pattern)
        body = _run(step)
        assert "state=error" in body, f"{step.get('name')!r} writes no error status"
        assert "$PREMORTEM_CONTEXT" in body, f"{step.get('name')!r} writes no context variable"
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
            assert "$PREMORTEM_CONTEXT" in body, (
                f"{job_name}/{step.get('name')}: writes a status without the context variable"
            )
            assert not re.search(r'-f context="agent/', body), (
                f"{job_name}/{step.get('name')}: writes a literal context"
            )


def test_a_cancelled_run_leaves_no_pending_stage_status() -> None:
    """A cancelled run - a hand-cancel, or a superseding run through the concurrency group -
    skips every failure() reporter and the post job, so without a dedicated job the stage
    stays `pending` on the SHA with no signal. The reporter only converts a pending status it
    wrote itself, so a newer run's terminal verdict survives. A workflow that marks nothing
    pending needs no reporter, which is what keeps this an invariant rather than a chain matrix."""
    for name in AGENT_WORKFLOWS:
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
        assert "$PREMORTEM_CONTEXT" in body, f"{name}: reporter writes no context variable"


def test_the_resolve_reporter_falls_back_to_the_resolved_head() -> None:
    """A first-step failure writes no `head`, yet the run may already have marked the analysed
    commit pending: the reporter then falls back to the commit named by the event, and to the
    dispatch input, so a stranded `pending` still reaches an `error`."""
    step = _find_step(_load(PREMORTEM)["jobs"]["resolve"], r"^Report resolve failure$")
    sha = str(step.get("env", {}).get("HEAD_SHA", ""))
    assert sha.startswith("${{ steps.pr.outputs.head"), (
        f"the reporter must prefer the resolved head: {sha}"
    )
    assert re.search(r"inputs\.head_sha\s*\}\}$", sha), f"no dispatch-input fallback: {sha}"
    assert step.get("if") == "failure()", f"the reporter is skipped on {step.get('if')!r}"


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
    # The reason is only in stderr. A failed run has ended with a bare "exit code 1" and no
    # cause, so a capped model and a broken prompt could not be told apart from the log: the
    # captured output is echoed, bounded so a runaway agent cannot flood the log.
    assert "tail -c" in body and "$out.raw" in body, (
        "the agent's failing output is not echoed, so the run log shows no reason"
    )
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


def test_the_pre_mortem_comment_records_whose_rules_ran() -> None:
    """The rules come from the base branch, and from the PR itself only while the base has none -
    a fallback that exists precisely because a PR may weaken its own rules. Which of the two ran
    is the one thing the report cannot show, so the workflow has to say it: a notice in the run
    log and the source in the comment footer. Without it a verdict produced by a PR's own copy is
    indistinguishable from one the base produced, and the provenance gate this workflow exists for
    has failed silently. This is not a hypothetical ordering: on this very PR the base has no
    skill, so every run so far is the fallback."""
    data = _load(PREMORTEM)
    body = _run(_find_step(_agent_job(data)[1], r"^Run pre-mortem agent$"))
    assert "::notice::Pre-mortem rules from: ${SKILL_SOURCE}" in body, (
        "the run log does not name whose rules ran"
    )
    assert "skill (rules from %s)" in body, "the comment footer drops the rules' source"
    assert "${SKILL_SOURCE}" in body, "the footer is not filled from the skill-source variable"


def test_the_sibling_comments_also_record_whose_rules_ran() -> None:
    """The same gap the pre-mortem's test above pins, in its two siblings: they record
    `SKILL_SOURCE` in their `$GITHUB_ENV`, hand it to the agent, and print nothing - a value
    nothing reads. The fallback it records (the PR's own copy, used while the base branch has
    no skill) is exactly the case a reader has to be able to see, so the siblings now say it in
    the same two places: a notice in the run log, and the source in the comment footer. The
    mutation copy's empty-scope footer is excluded by design: no agent ran, so no rules ran."""
    for name in (REVIEW, MUTATION):
        body = _job_bodies([_agent_job(_load(name))[1]])
        assert re.search(r"::notice::[A-Za-z-]+ rules from: \$\{SKILL_SOURCE\}", body), (
            f"{name}: the run log does not print whose rules ran"
        )
        assert "skill (rules from %s)" in body, (
            f"{name}: the comment footer drops the rules' source"
        )


def test_skill_selection_refuses_an_unreachable_base_and_keeps_two_levels() -> None:
    """The rules come from the base branch, so a PR cannot weaken its own run; the PR's copy is
    used only while the base has none. A base commit that cannot be read must abort the step -
    a silent fall-through would run the PR's own rules while the prompt says "base branch", and
    that is what a base-commit check outside the `if` is for: GitHub runs a `run:` block with
    `bash -e {0}`, so that standalone `git cat-file -e` aborts the step there, while a failing
    command *inside* the `if` condition that follows is only a false branch - which is what lets
    the `elif` take the PR's copy when the base simply has no skill yet. `set -euo pipefail` adds
    `pipefail` and `-u` and states the strictness; it is not the guard, and must not be read as
    one, because deleting the standalone check on that reading would silently run the PR's own
    rules while the prompt says they came from the base branch.

    The assertions are therefore scoped to the selection step of every copy, not to the agent
    job as a whole: a base-commit check that lives in some other step (the mutation workflow's
    scope step) leaves the selection itself able to fall through, which is how the two siblings
    stayed non-strict while this test passed over them."""
    for name in AGENT_WORKFLOWS:
        job = _agent_job(_load(name))[1]
        body = _run(_find_step(job, r"^Select skill revision$"))
        # What the selection must contain is matched on its executable lines, not as a
        # substring of the body: a commented-out guard is not a guard.
        failures = _strictness_failures(_selection_lines(job))
        assert not failures, f"{name}: " + "; ".join(failures)
        # Two levels only: a third (the commit the workflow file came from) would analyse an older
        # commit under rules the branch tip happens to carry.
        assert "GITHUB_SHA" not in body, f"{name}: reads a third skill revision"
        assert body.count("elif") == 1, f"{name}: more than two levels:\n{body}"


def test_the_base_commit_guard_must_not_be_a_comment_line() -> None:
    """The guard is one shell line, and the cheapest way to delete it is to comment it out -
    which a substring assertion over the step body cannot see: it passes with the line disabled,
    and so does the "stays identical" test, which strips `#` lines before it compares. So the
    guard is commented out in a copy of each workflow here and the same check runs over that
    copy: it fails. That is what the assertion it replaced claimed to be."""
    guard = 'git cat-file -e "${BASE_SHA}^{commit}"'
    for name in AGENT_WORKFLOWS:
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        # Every occurrence, not just the first: the mutation copy carries the same line in its
        # scope step, so a copy with only the selection's line commented out would still be
        # analysed with a base whose tip it cannot read.
        mutated = re.sub(rf"(?m)^(\s*){re.escape(guard)}\s*$", rf"\1# {guard}", text)
        assert mutated.count(f"# {guard}") >= 1, f"{name}: the guard is not a line of its own"
        assert mutated != text, f"{name}: the base-commit guard is not a line of its own"
        job = _agent_job(_parse(mutated))[1]
        assert not any(guard in line for line in _selection_lines(job)), (
            f"{name}: a commented-out guard is still an executable line"
        )
        failures = _strictness_failures(_selection_lines(job))
        assert any("unreachable base" in message for message in failures), (
            f"{name}: commenting the guard out does not fail the strictness check: {failures}"
        )


def test_the_skill_selection_stays_identical_across_the_copies() -> None:
    """The two-level selection is duplicated in all three workflows, and it is the block that
    decides whose rules ran. The pre-mortem's copy used to be the strict one and the siblings'
    were not, and nothing compared the bodies - so the divergence that mattered most was the one
    the "kept in sync" test never looked at. Here the three bodies are compared line by line
    after the documented per-copy substitutions (the temp directory, the agent id, the stage and
    the skill noun in the error line), so the next divergence fails here instead of silently
    grading a PR under rules it may have weakened."""
    substitutions = {
        REVIEW: (("review skill", "<skill>"), ("pr-review", "<agent>"), ("review", "<stage>")),
        MUTATION: (
            ("mutation-testing skill", "<skill>"),
            ("pr-mutation", "<agent>"),
            ("mutation", "<stage>"),
        ),
        PREMORTEM: (
            ("pre-mortem skill", "<skill>"),
            ("pr-premortem", "<agent>"),
            ("premortem", "<stage>"),
        ),
    }

    def selection(name: str) -> list[str]:
        step = _find_step(_agent_job(_load(name))[1], r"^Select skill revision$")
        lines = []
        for line in _run(step).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):  # prose, not a selection line
                continue
            for old, new in substitutions[name]:
                line = line.replace(old, new)
            lines.append(line)
        return lines

    reference = selection(REVIEW)
    assert reference, "parsed no selection lines out of the review workflow"
    for other in (MUTATION, PREMORTEM):
        assert selection(other) == reference, f"{other}: skill selection diverged:\n" + "\n".join(
            selection(other)
        )


def test_the_duplicated_comment_guards_stay_in_sync_across_the_copies() -> None:
    """The three workflows duplicate the blocks that decide what gets posted: the summary-line
    extraction, the fence strip, the head-unchanged and tree-clean check, and the output path.
    Nothing forces the three copies to be edited together - the workflow comments do not swear to
    it - so this test is what makes it true mechanically instead of by memory: a guard changed in
    one copy and not in the others fails here.

    The per-copy substitutions below are the documented ones (the temp directory, the agent id),
    applied agent-id first so "pr-review" cannot be eaten by the shorter "review". A guard that
    only one copy needs is not here: the pre-mortem's stricter verdict prelude (it tolerates the
    sign-off note the skill requires) and its head-moved comment guard are pinned separately. The
    two-level skill selection is not one of those exclusions - it is compared by
    `test_the_skill_selection_stays_identical_across_the_copies`, which is where a copy that is
    not strict has to fail.
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
