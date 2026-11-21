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
  concurrency group per PR that cancels superseded runs - the pre-mortem's carries an analysed
  commit only when a caller names one, so a manual rerun of an older commit cannot cancel the
  current head's run while two pushes still supersede one another instead of accumulating one
  30-minute agent session each - and no workflow starts another one - no dispatch call, no
  `workflow_run` or `repository_dispatch` trigger, no `actions: write`;
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
  the context (sorted by creation time, since the API's own ordering is undocumented) of every
  page of the status list, and the run that wrote it compared by exact target URL - and the
  reporters' own `run:` bodies are executed here against a stub `gh`, because `gh api` has no
  `--arg` of its own and a string assertion cannot tell a flag the binary accepts from one it
  does not: that broken flag was pinned by this suite for three rounds. What is left - the
  window between that read and the write - is stated as
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


# ---- the pre-mortem's pending->error reporters, executed against a stub `gh` ----

# The values the workflow fills in from expressions. The reporters are run here (below), and the
# harness below refuses to run one whose env it does not know - a reporter that reads a variable
# the harness never sets is a reporter whose behaviour is not being tested.
REPORTER_ENV = {
    "GH_TOKEN": "stub-token",
    "GITHUB_REPOSITORY": "itsjustFred/trading-platform",
    "PR_NUMBER": "185",
    "BASE_SHA": "1" * 40,
    "HEAD_SHA": "0" * 40,
    "SAME_REPO": "true",
    "PREMORTEM_CONTEXT": CONTEXT,
    "RUN_URL": "https://github.com/itsjustFred/trading-platform/actions/runs/123",
}

# Every stop-path reporter of the pre-mortem: (job name, step name). The four that turn a
# `pending` status into an `error`.
REPORTERS = (
    ("resolve", "Report resolve failure"),
    ("post", "Report failed pre-mortem run"),
    ("post", "Report post failure"),
    ("report-cancelled", "Report cancelled pre-mortem run"),
)

_STUB_GH = """#!/usr/bin/env bash
# A stand-in for the `gh` this repo's workflows call, because a reporter that cannot run is not
# a reporter: `gh api` has no `--arg`, and a test that only reads the YAML cannot tell a flag the
# binary accepts from one it does not (the four reporters were written with `gh api --arg` and
# the contract test asserted the string was there). So the stub refuses what the real gh refuses,
# serves every page of the commit-status list when it is asked to paginate and only the first
# page when it is not - which is what makes the API's paging visible here - and records every
# write the reporter makes.
set -u
printf '%s\\n' "$@" >> "$STUB_LOG"
for arg in "$@"; do
  case "$arg" in
    --arg|--arg=*) echo "unknown flag: --arg" >&2; exit 1 ;;
  esac
done
case "$*" in
  *"/commits/"*"/status"*)
    if [[ "$*" == *"--paginate"* ]]; then
      n=1
      while [ -f "$STUB_PAGES/page-$n.json" ]; do
        cat "$STUB_PAGES/page-$n.json"
        n=$((n + 1))
      done
    elif [ -f "$STUB_PAGES/page-1.json" ]; then
      cat "$STUB_PAGES/page-1.json"
    else
      printf '{"state":"unset","statuses":[]}\\n'
    fi
    ;;
  *) : ;;
esac
exit 0
"""


def _step_variables(step: dict) -> set[str]:
    """The shell variables a step reads from its environment - every `$NAME` the body uses.

    The workflow-level and job-level `env:` blocks are GitHub's, so a step's *own* `env:` is not
    the whole picture: `GITHUB_REPOSITORY` and `PREMORTEM_CONTEXT` are declared once for the whole
    workflow and every reporter inherits them. What matters is what the body actually reads, and
    the assignment of a value (`state=`, `owner=`) is not a read - those are the step's own.
    """
    body = _run(step)
    assigned = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=", body, flags=re.M))
    return set(re.findall(r"\$\{?([A-Z_][A-Z0-9_]*)", body)) - assigned


def _status_page(states: list[tuple[str, str, str, str]]) -> str:
    """One page of the commit status response: (context, state, target_url, created_at)."""
    return json.dumps(
        {
            "state": "success",
            "sha": REPORTER_ENV["HEAD_SHA"],
            "statuses": [
                {"context": ctx, "state": state, "target_url": url, "created_at": at}
                for ctx, state, url, at in states
            ],
        }
    )


def _run_reporter(job_name, step_name, tmp_path, pages, env_override=None):
    """Execute one reporter step's own `run:` body against the stub `gh`.

    The body is lifted out of the YAML and run with the environment the step declares, so what is
    tested is the workflow's own shell - every flag, pipe and jq expression it contains.
    """
    job = _load(PREMORTEM)["jobs"][job_name]
    step = _find_step(job, re.compile(f"^{step_name}$"))
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "gh"
    stub.write_text(_STUB_GH, encoding="utf-8")
    stub.chmod(0o755)
    pagedir = tmp_path / "pages"
    pagedir.mkdir(exist_ok=True)
    for index, page in enumerate(pages, start=1):
        (pagedir / f"page-{index}.json").write_text(page, encoding="utf-8")
    log = tmp_path / "calls.log"
    log.unlink(missing_ok=True)
    env = {
        "PATH": os.pathsep.join([str(bindir), os.environ["PATH"]]),
        "STUB_LOG": str(log),
        "STUB_PAGES": str(pagedir),
    }
    for key in _step_variables(step):
        assert key in REPORTER_ENV, f"{job_name}/{step_name}: the harness sets no {key}"
        env[key] = REPORTER_ENV[key]
    env.update(env_override or {})
    script = tmp_path / "step.sh"
    script.write_text(_run(step), encoding="utf-8")
    result = subprocess.run(
        [shutil.which("bash") or "bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
    )
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return result, calls


OWN = REPORTER_ENV["RUN_URL"]
OTHER = "https://github.com/itsjustFred/trading-platform/actions/runs/999"
T1 = "2026-11-20T10:00:00Z"
T2 = "2026-11-20T11:00:00Z"


def test_the_pending_mark_is_converted_only_when_this_run_wrote_it(tmp_path) -> None:
    """The four stop-path reporters exist to keep the stage from stranding `pending`, and each one
    is the workflow's own shell run against a stub `gh` - a string assertion on `--arg ctx` pinned
    the broken `gh api --arg` for three rounds, because nothing here ever executed a reporter.
    Three behaviours are pinned, each with the failure it prevents:

    - the read reaches the `jq` binary (`gh api` has no `--arg`), so the reporter runs at all;
    - the mark is the *newest* of the context across **every page** of the status list - the API's
      ordering is undocumented, so a reporter that reads the first page converts whichever run's
      mark happens to be on it;
    - the run that wrote it is this one, by exact `target_url` - a prefix match lets run
      `.../runs/123` claim the mark of run `.../runs/1234` and turn a live analysis into an error;
    - a `success` or a status of another run is left alone, and every error names the base...head
      pair and links to the run that wrote it.
    """
    for job_name, step_name in REPORTERS:
        label = f"{job_name}/{step_name}"
        # This run's pending mark is the newest status of the context, and it is on the second
        # page: only a paginating read finds it, and only this run's mark is converted.
        result, calls = _run_reporter(
            job_name,
            step_name,
            tmp_path,
            [
                _status_page([(CONTEXT, "pending", OTHER, T1)]),
                _status_page([(CONTEXT, "pending", OWN, T2)]),
            ],
        )
        assert result.returncode == 0, f"{label}: reporter failed: {result.stderr}"
        assert "state=error" in calls, f"{label}: this run's own pending mark was not converted"
        assert f"context={CONTEXT}" in calls, f"{label}: the error carries no context"
        assert f"target_url={OWN}" in calls, f"{label}: the error links no run"
        description = re.search(r"description=(.*)", calls)
        assert description, f"{label}: the error carries no description"
        for token in (REPORTER_ENV["HEAD_SHA"][:8], REPORTER_ENV["BASE_SHA"][:8]):
            assert token in description.group(1), f"{label}: the error status does not name {token}"


def test_no_reporter_touches_a_mark_another_run_wrote(tmp_path) -> None:
    """The other half of the ownership check, on the same executed bodies: a newest mark that is
    another run's - by run id, or because it is already terminal - is not converted, and the
    reporter says what it left alone instead of failing silently. The near-miss run id
    (`.../runs/1234` when this run is `.../runs/123`) is the prefix match: converting it marks a
    live analysis `error` while it still runs."""
    near_miss = OWN + "4"
    cases = {
        "another run's pending mark is the newest": [
            _status_page([(CONTEXT, "pending", OWN, T1)]),
            _status_page([(CONTEXT, "pending", OTHER, T2)]),
        ],
        "the newest mark's run id only starts with this run's": [
            _status_page([(CONTEXT, "pending", near_miss, T2)])
        ],
        "the newest mark is already terminal": [_status_page([(CONTEXT, "success", OWN, T2)])],
        "no status of this context exists": [_status_page([("other/context", "pending", OWN, T1)])],
    }
    for job_name, step_name in REPORTERS:
        label = f"{job_name}/{step_name}"
        for why, pages in cases.items():
            result, calls = _run_reporter(job_name, step_name, tmp_path, pages)
            assert result.returncode == 0, f"{label} ({why}): reporter failed: {result.stderr}"
            assert "state=error" not in calls, f"{label} ({why}): converted {why}"
            assert "::notice::" in result.stdout, f"{label} ({why}): silent about it"


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
    # The pre-mortem is the only one with a manual rerun, and it is the one whose bucket must
    # name an analysed commit - but only a commit a *dispatch* names. A push must not carry
    # its head SHA: every push would land in a group of its own, so N pushes on one PR would
    # run N 30-minute agent sessions side by side and exhaust the free model's usage cap, after
    # which the stage produces no verdict for any PR. A dispatch naming an older commit does
    # carry it, which is what keeps that rerun from cancelling the current head's in-flight
    # run (a PR-keyed group would, and its cancellation reporter would then mark the head
    # `error` with no analysis left). A rerun that omits `head_sha` shares the `current-head`
    # bucket with the push's run, so it supersedes it instead of doubling the analysis of the
    # same diff.
    premortem_group = str(_load(PREMORTEM)["concurrency"]["group"])
    assert "pull_request.head.sha" not in premortem_group, (
        f"the pre-mortem group {premortem_group!r} is keyed by the pushed head, so every push "
        "runs a session of its own"
    )
    assert "inputs.head_sha" in premortem_group and "'current-head'" in premortem_group, (
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
        # The workflow-level block first: a job-level `permissions:` replaces it, so iterating
        # the jobs and never reading the workflow's own block left a scope nothing here looks
        # at - and a workflow with no block runs on the repository's default token scopes, which
        # are whatever Actions is configured to grant, not what this workflow needs.
        workflow_permissions = data.get("permissions") or {}
        assert workflow_permissions.get("actions") != "write", (
            f"{name}: holds workflow-level actions: write, which only bought a dispatch"
        )
        for job_name, job in data["jobs"].items():
            permissions = job.get("permissions") or {}
            assert permissions.get("actions") != "write", (
                f"{name}/{job_name}: holds actions: write, which only bought a dispatch"
            )


def test_the_pre_mortem_narrows_the_default_token_scopes() -> None:
    """A workflow with no workflow-level `permissions` block runs on the repository's default
    token scopes - whatever Actions is configured to grant, which is write-everything for as long
    as it is - so a job added later without its own block would inherit them. The pre-mortem's
    jobs are the four it has: the agent job reads, the resolve job reads the PR and writes a
    status, the post job writes a comment and a status, the cancellation reporter writes a status.
    None of them needs more than reading the repository, so the workflow says that once, and the
    jobs that write declare it themselves - a per-job block *replaces* the workflow's, so the
    narrow default cannot widen anything."""
    data = _load(PREMORTEM)
    permissions = data.get("permissions")
    assert isinstance(permissions, dict), "no workflow-level permissions block"
    assert set(permissions) <= {"contents", "pull-requests", "statuses"}, (
        f"the workflow-level token scopes are broader than the jobs it runs: {sorted(permissions)}"
    )
    assert not [scope for scope, value in permissions.items() if value == "write"], (
        f"the workflow-level default writes: {permissions}"
    )
    assert permissions.get("contents") == "read", (
        f"the agent job's checkout needs contents: read by default: {permissions}"
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


# The expression syntax the concurrency group needs, and nothing else: if the group carries
# something this evaluator does not know, the test below fails on the token rather than guessing.
_EXPR_TOKEN = re.compile(
    r"""
    \s*(?:   (?P<or>\|\|)
      |     (?P<and>&&)
      |     (?P<not>!)(?!=)
      |     (?P<eq>==)
      |     (?P<lparen>\()
      |     (?P<rparen>\))
      |     (?P<comma>,)
      |     (?P<string>'(?:[^']*)'|"[^"]*")
      |     (?P<number>-?\d+)
      |     (?P<path>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
      |     (?P<unknown>\S)
    )""",
    re.VERBOSE,
)


def _truthy(value) -> bool:
    """GitHub's truthiness, which a Python `bool` gets wrong: '' and 0 and null are false, a
    non-empty string (even 'false') is true."""
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


class _Expr:
    """A recursive-descent evaluator for the `${{ }}` expressions this file asserts on.

    The concurrency bucket is a workflow behaviour - which run cancels which - and a test that
    reads the YAML cannot tell a group that isolates a text edit from the one that used to let it
    cancel the running analysis, which is exactly what happened: a title edit joined the
    `current-head` bucket, cancelled the analysis of the head, and was then skipped itself, so no
    verdict was posted and the stage stayed `pending`. The expression is therefore evaluated
    against the payloads GitHub actually delivers, with GitHub's own operator rules (an `||`
    returns the first truthy operand, an `&&` the first falsy one, and the last if none)."""

    def __init__(self, text: str, context: dict) -> None:
        text = text.strip()
        self.tokens: list[tuple[str, str]] = []
        position = 0
        while position < len(text):
            match = _EXPR_TOKEN.match(text, position)
            assert match, f"{text!r}: the evaluator cannot read {text[position:]!r}"
            position = match.end()
            kind = match.lastgroup
            assert kind != "unknown", f"{text!r}: unknown token {match.group()!r}"
            self.tokens.append((str(kind), match.group(kind)))
        self.index = 0
        self.context = context

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _eat(self, kind: str) -> str:
        token = self._peek()
        assert token and token[0] == kind, f"expected {kind}, got {token}"
        self.index += 1
        return token[1]

    def evaluate(self):
        value = self._or()
        assert self._peek() is None, f"unread tokens: {self.tokens[self.index :]}"
        return value

    def _or(self):
        value = self._and()
        while self._peek() and self._peek()[0] == "or":
            self._eat("or")
            right = self._and()
            value = value if _truthy(value) else right
        return value

    def _and(self):
        value = self._unary()
        while self._peek() and self._peek()[0] == "and":
            self._eat("and")
            right = self._unary()
            value = value if not _truthy(value) else right
        return value

    def _unary(self):
        if self._peek() and self._peek()[0] == "not":
            self._eat("not")
            return not _truthy(self._unary())
        return self._comparison()

    def _comparison(self):
        left = self._atom()
        if self._peek() and self._peek()[0] == "eq":
            self._eat("eq")
            return left == self._atom()
        return left

    def _atom(self):
        token = self._peek()
        assert token, "the expression ends early"
        kind, text = token
        if kind == "lparen":
            self._eat("lparen")
            value = self._or()
            self._eat("rparen")
            return value
        if kind == "string":
            self._eat("string")
            return text[1:-1]
        if kind == "number":
            self._eat("number")
            return int(text)
        if kind == "path":
            self._eat("path")
            if self._peek() and self._peek()[0] == "lparen":  # format('...', a.b)
                self._eat("lparen")
                arguments = []
                while self._peek() and self._peek()[0] != "rparen":
                    arguments.append(self._or())
                    if self._peek() and self._peek()[0] == "comma":
                        self._eat("comma")
                self._eat("rparen")
                assert text == "format", f"unknown function {text}()"
                assert arguments, "format() takes a template"
                return str(arguments[0]).format(*arguments[1:])
            value = self.context
            for part in text.split("."):
                # A path the payload does not carry is null, exactly as GitHub resolves it:
                # `github.event.changes.base` is null for every action but a retarget, and that
                # null is what makes `!github.event.changes.base` the text-edit case.
                if value is None:
                    return None
                assert isinstance(value, dict), f"{text}: {part} is read from a non-object"
                value = value.get(part)
            return value
        raise AssertionError(f"unexpected token {token}")


def _workflow_expressions(text: str, context: dict) -> str:
    """Render every `${{ ... }}` in a workflow string the way GitHub does for one run."""

    def replace(match: re.Match) -> str:
        value = _Expr(match.group(1), context).evaluate()
        return "" if value is None else str(value)

    return re.sub(r"\$\{\{(.*?)\}\}", replace, text)


def _run_context(event: dict, inputs: dict | None = None, run_id: int = 44828760524) -> dict:
    """The `github` and `inputs` contexts one run's expressions are evaluated against.

    `github.event.inputs` is where a `workflow_dispatch` keeps its inputs and where a
    `pull_request` run finds nothing; the workflow-level `inputs` context is the same map. So one
    empty map serves both, exactly as GitHub hands it to both expression contexts.
    """
    inputs = inputs or {}
    return {
        "github": {"event": {"inputs": inputs, **event}, "run_id": run_id},
        "inputs": inputs,
    }


def _dispatch_group(group_template: str, sha: str) -> str:
    """Render the concurrency group of a manual dispatch that names `sha`."""
    return _workflow_expressions(
        group_template,
        _run_context({}, {"pr_number": 185, "head_sha": sha}),
    )


def test_a_text_edit_cannot_cancel_the_running_analysis() -> None:
    """The concurrency bucket decides which run cancels which, and this is a behaviour, not a
    string: a `pull_request` `edited` run whose event carries no changed base (a title, body or
    label edit) reaches the resolve job, finds it has nothing to do - the guard skips it - and
    therefore posts no verdict. In the bucket it used to share, `cancel-in-progress: true` made
    it cancel the analysis of the head it just invalidated on paper: no verdict, no comment, the
    stage left `pending` on the one commit the PR is built on. So the bucket for such a run is
    its own, named after this run, and the only runs that still share a bucket are the ones that
    analyse the same diff (a push, a retarget that re-analyses, a rerun of the same commit)."""
    data = _load(PREMORTEM)
    concurrency = data["concurrency"]
    group_template = str(concurrency["group"])
    assert concurrency.get("cancel-in-progress") is True, "a superseded run is no longer cancelled"
    base = "a" * 40
    head = "b" * 40
    events = {
        # A push, and the rerun the workflow documents for it: the same analysed diff, so the
        # same bucket and one supersedes the other instead of running twice side by side.
        "a push": {"action": "synchronize", "pull_request": {"number": 185, "head": {"sha": head}}},
        "a retarget": {
            "action": "edited",
            "pull_request": {"number": 185, "head": {"sha": head}},
            "changes": {"base": {"ref": "x", "sha": base}},
        },
        # The run that must not cancel anything: its resolve job is skipped, so it analyses nothing.
        "a title edit": {
            "action": "edited",
            "pull_request": {"number": 185, "head": {"sha": head}},
            "changes": {"title": {"from": "old", "to": "new"}},
        },
    }
    buckets = {
        label: _workflow_expressions(group_template, _run_context(event))
        for label, event in events.items()
    }
    assert buckets["a push"] == "agent-pre-mortem-185-current-head", buckets
    # A retarget re-analyses the same pair, so it belongs in the bucket it supersedes.
    assert buckets["a retarget"] == buckets["a push"], buckets
    assert buckets["a title edit"] == "agent-pre-mortem-185-edited-44828760524", buckets
    # And the buckets are per run, so two edits on one PR neither cancel each other nor the
    # analysis that is already running.
    reruns = {
        _workflow_expressions(group_template, _run_context(events["a title edit"], run_id=run_id))
        for run_id in (1, 2)
    }
    assert len(reruns) == 2, reruns
    assert buckets["a title edit"] not in reruns & {buckets["a push"]}


def test_the_current_head_rerun_supersedes_the_push_run() -> None:
    """The documented rerun of the current head (`-f head_sha=<head>`) is the one a capped run
    tells the operator to make, and a bucket of its own would run a second 30-minute session on
    the diff the push is analysing right now: every repetition of the remedy spends the free
    model's quota for nothing, and the cap is what stops the stage from producing a verdict for
    any PR at all. Naming the current head is therefore refused by the resolve step, so the only
    dispatch that runs is one of a commit this workflow does not already analyse - and an older
    commit keeps the bucket the keying exists for, beside the current head's run."""
    data = _load(PREMORTEM)
    group_template = str(data["concurrency"]["group"])
    body = _run(_find_step(data["jobs"]["resolve"], r"^Resolve PR$"))
    assert 'if [ "$head_now" = "$INPUT_SHA" ]' in body, (
        "a dispatch of the current head runs a second analysis of the same diff"
    )
    head = "b" * 40
    assert _dispatch_group(group_template, head) == f"agent-pre-mortem-185-{head}"
    assert _dispatch_group(group_template, "c" * 40) == "agent-pre-mortem-185-" + "c" * 40


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


def test_a_dispatch_refuses_a_fork_a_draft_and_the_current_head() -> None:
    """A dispatch is the only path that reaches what a push never opens, and all three of its
    refusals are the difference between a run that says why it did nothing and a green run that
    graded nothing:

    - a fork PR: `same_repo` skips the agent and post jobs, so without a word the operator reads
      "no findings" from a request the workflow refused;
    - a draft: the `pull_request` triggers never fire for one, so a dispatch is the only way in,
      and pre-morteming a draft spends quota on a diff nobody has approved for review;
    - the current head as `head_sha`: the bucket carries the commit a dispatch names, so this
      runs a second 30-minute session *beside* the push analysing that same diff, and every
      repetition of the remedy a capped run prints spends the free model's quota for nothing -
      the cap being what then stops the stage producing a verdict for any PR at all."""
    data = _load(PREMORTEM)
    body = _run(_find_step(data["jobs"]["resolve"], r"^Resolve PR$"))
    assert ".draft" in body and "still a draft" in body, "a dispatch pre-mortems a draft PR"
    assert ".head.repo.full_name" in body and "fork PR" in body, (
        "a dispatch of a fork PR is a green run with no comment, no status and no refusal"
    )
    assert 'if [ "$head_now" = "$INPUT_SHA" ]' in body, (
        "a dispatch of the current head runs a second analysis of the diff the push is analysing"
    )
    assert "omit head_sha" in body, "the refusal does not say what re-runs the current head"


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
        assert env["MODEL"] == (f"${{{{ vars.{prefix}_MODEL || '{DEFAULT_MODEL}' }}}}"), (
            f"{name}: {env['MODEL']!r}"
        )
        assert env["VARIANT"] == (f"${{{{ vars.{prefix}_VARIANT || '{DEFAULT_VARIANT}' }}}}"), (
            f"{name}: {env['VARIANT']!r}"
        )
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
    # The comparison is made twice, and the second one gates the write: the first ran before the
    # marker search and the body was assembled, and a synchronize event inside that window still
    # had a comment describing a diff nobody looks at posted as the current result. What is left
    # is the window of the write itself, which no API can close - there is no conditional comment
    # update - so the footer's base...head pair is what keeps that result readable.
    assert body.count("--jq .head.sha") == 2, "the head is read once, before the write"
    reread = body.index("moved=$(gh api")
    written = body.index("-F body=@body.md")
    assert reread < written, "the write does not happen under the re-read head"
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
        # The base goes with it: a status is current for a base...head pair, and a failed or
        # cancelled run names only the head when nothing ever told it the base - a resolve
        # failure publishes none of this job's own outputs, and a dispatch names no base at all.
        assert "${BASE_SHA:0:8}" in description.group(1) or "${pair}" in description.group(1), (
            f"{pattern}: the error status does not name the base it was written for"
        )
        assert '-f target_url="$RUN_URL"' in body, (
            f"{pattern}: the error status links no run, so no reader can tell it from another's"
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
        # The read is `gh api --paginate ... | jq -sr --arg ctx ... --arg url ...`, and the shape
        # is pinned because it is what was wrong: `gh api` has no `--arg` of its own, so the four
        # reporters aborted on their first command and left the stage `pending` for three rounds
        # while a string assertion insisted the broken flag was there. The status list is
        # paginated (one page per 30 entries, and the mark to convert is not guaranteed to be on
        # the first) and the run identity is compared for equality: `.../runs/123` is a prefix of
        # `.../runs/1234`. What the shell does with this is executed, not read:
        # `test_the_pending_mark_is_converted_only_when_this_run_wrote_it` runs all four bodies.
        assert (
            'gh api --paginate "repos/${GITHUB_REPOSITORY}/commits/${HEAD_SHA}/status"' in body
        ), f"{pattern}: the status read is not paginated"
        assert '| jq -sr --arg ctx "$PREMORTEM_CONTEXT" --arg url "$RUN_URL"' in body, (
            f"{pattern}: the read does not go through the jq binary, which alone takes --arg"
        )
        assert "[.[].statuses[]" in body, f"{pattern}: the pages are read as one status list"
        assert '((($s.target_url // "") == $url)' in body, (
            f"{pattern}: the run identity is a prefix match, so runs/123 claims runs/1234"
        )
        assert "startswith" not in body, f"{pattern}: the run identity is still a prefix match"
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
    dispatch input, so a stranded `pending` still reaches an `error`. The cancellation reporter
    needs the same chain for the same reason and is pinned with it here - a cancelled job
    publishes no outputs, so without the fallback a run cancelled after "Mark running" strands
    the mark on its SHA with nothing but a warning in a log nobody opens."""
    reporters = (
        (_load(PREMORTEM)["jobs"]["resolve"], r"^Report resolve failure$"),
        (_load(PREMORTEM)["jobs"]["report-cancelled"], r"^Report cancelled pre-mortem run$"),
    )
    for job, pattern in reporters:
        step = _find_step(job, pattern)
        sha = str(_step_env(job, step).get("HEAD_SHA", ""))
        assert sha.startswith(("${{ needs.resolve.outputs.head", "${{ steps.pr.outputs.head")), (
            f"{pattern} must prefer the resolved head: {sha}"
        )
        assert "github.event.pull_request.head.sha" in sha and re.search(
            r"inputs\.head_sha\s*\}\}$", sha
        ), f"{pattern} has no fallback chain to the event's head and the dispatch input: {sha}"
    resolve_step = _find_step(_load(PREMORTEM)["jobs"]["resolve"], r"^Report resolve failure$")
    assert resolve_step.get("if") == "failure()", (
        f"the reporter is skipped on {resolve_step.get('if')!r}"
    )


def test_the_cancelled_reporter_skips_a_fork_pr() -> None:
    """Fork PRs are skipped by the resolve job's `same_repo` guard and by the agent job, and a
    fork's head commit is not one this repository can write a status for: the reporter's read
    would 404 on it, so the job fails - fails closed, but with a spurious red job per cancelled
    fork run. The cancellation reporter therefore checks the same value first and stops.

    A cancelled job publishes no outputs, so `needs.resolve.outputs.same_repo` is empty exactly
    when the resolve job was the one cancelled - and an empty value is not "false": taking it as
    one sent a fork run's head SHA to the API and failed the reporter on the 404. So the value
    is confirmed as *same-repo* (from the event's own head repository when the output is gone)
    and the reporter stops on anything short of that."""
    job = _load(PREMORTEM)["jobs"]["report-cancelled"]
    step = _find_step(job, r"^Report cancelled pre-mortem run$")
    same_repo = str(_step_env(job, step).get("SAME_REPO", "")).strip()
    assert "github.event.pull_request.head.repo.full_name == github.repository" in same_repo, (
        f"a cancelled resolve leaves the reporter unable to tell a fork PR: {same_repo}"
    )
    body = _run(step)
    assert '[ "$SAME_REPO" != "true" ]' in body, (
        "the cancellation reporter runs for a fork PR, or for a repository it cannot read"
    )


def test_the_resolve_reporter_skips_a_fork_pr() -> None:
    """The resolve failure reporter runs on the same fork 404, and for the same reason: a
    dispatch that refused a fork PR is a failed resolve, and its reporter reads the fork's head
    commit to find the pending mark that was never written there."""
    job = _load(PREMORTEM)["jobs"]["resolve"]
    step = _find_step(job, r"^Report resolve failure$")
    same_repo = str(_step_env(job, step).get("SAME_REPO", "")).strip()
    assert "github.event.pull_request.head.repo.full_name == github.repository" in same_repo, (
        f"the resolve reporter cannot tell a fork PR: {same_repo}"
    )
    assert '[ "$SAME_REPO" != "true" ]' in _run(step), (
        "the resolve reporter reads a fork commit's status and fails on the 404"
    )


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
    # A transient failure and a misconfiguration need opposite remedies, so they are not one
    # classifier: "re-run this job" is the fix for a cap, a rate limit or a region refusal,
    # and a lie for a model or variant that cannot be resolved at all - there the run cannot
    # succeed until PREMORTEM_MODEL / PREMORTEM_VARIANT changes, and telling an operator to
    # re-run sends them into a loop while the stage stays `error` for everyone. So the two
    # classes are separated, and each message names its own remedy.
    classes = re.findall(r"grep -qiE '([^']+)'", body)
    assert len(classes) >= 2, (
        "the failure classifier is one grep, so it cannot tell a cap from a misconfiguration"
    )
    transient, misconfigured = classes[0], classes[1]
    for misconfiguration in ("no provider", "model (is )?(not (found|available|supported))"):
        assert misconfiguration in misconfigured, (
            f"{misconfiguration!r} is not classified as a configuration error"
        )
    for transient_only in ("usage limit", "not available in your country"):
        assert transient_only not in misconfigured, (
            f"{transient_only!r} is classified as a configuration error"
        )
    for configured_only in ("no provider", "unknown variant"):
        assert configured_only not in transient, (
            f"{configured_only!r} is classified as transient, so a permanent misconfiguration "
            "would tell the operator to re-run"
        )
    errors = [line for line in body.splitlines() if "::error::" in line]
    rerun = [line for line in errors if "re-run" in line]
    assert rerun, "no message states the remedy"
    assert any("PREMORTEM_MODEL" in line for line in errors), (
        "a misconfiguration does not name the variable that has to change"
    )
    assert not any("re-run this job" in line for line in errors if "PREMORTEM_MODEL" in line), (
        "a misconfiguration is told to re-run, which cannot succeed"
    )
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
        # A verdict word with a suffix is not that verdict: the reason follows a
        # space or punctuation, so `PASSed` and `BLOCKED` are refused rather than
        # matched as PASS and BLOCK - a verdict the model never wrote would be posted
        # and mapped to a status state.
        ("**Summary:** x\n**Verdict: PASSed** — looks fine\n", ""),
        ("**Summary:** x\n**Verdict: BLOCKED** — harden\n", ""),
        ("**Summary:** x\n**Verdict: INCOMPLETELY**\n", ""),
        # The documented verdicts still match with each documented annotation shape.
        ("**Summary:** x\n**Verdict: BLOCK** — harden. Human sign-off required.\n", "BLOCK"),
        ("**Summary:** x\n**Verdict: CHANGES-REQUESTED (1 severe)**\n", "CHANGES-REQUESTED"),
        ("**Summary:** x\n**Verdict: INCOMPLETE** — inputs missing.\n", "INCOMPLETE"),
    ],
)
def test_pre_mortem_verdict_extraction(output: str, expected: str, tmp_path) -> None:
    """The workflow, not the test, decides what a verdict is: the extraction lines are lifted
    out of the YAML and run, so a verdict the regex accepts and a run that posts it cannot
    disagree. The `**Verdict:**` line must be the last non-empty line - a verdict quoted from
    the diff earlier in the output does not count."""
    assert _premortem_verdict(output, tmp_path) == expected
