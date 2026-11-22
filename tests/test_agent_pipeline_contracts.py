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
  the guard disabled in this workflow at once - and the selection itself is *executed* against a
  real repository with a real base commit, for every branch it documents and both failures it
  must refuse: the base path lookup is a plain assignment, so `errexit` applies to it, and only an
  empty listing takes the PR's own copy. A string assertion cannot tell a lookup that stops the job
  from one that quietly falls through, which is the defect that round found;
- the extraction is diagnosable: a comment that lost its opening sections (the anchor is a
  heuristic, shared with the siblings) has a logged byte count and the raw stream beside it as an
  artifact, so a truncated report is a run someone can reconstruct and read; and the head is read
  again *immediately before* the comment write, with the work that builds the write behind that
  read - the window is the write itself, which no API can close; and the documented rerun for the
  current head is the bare `pr_number` form the refusal names, not the `head_sha` one it refuses;
- whose rules ran is printed by the pre-mortem, so a verdict produced by the PR's own skill copy
  (the fallback while the base branch has none) is never indistinguishable from the base's - the
  siblings are pinned by the PR that owns their hunks, which this PR no longer touches;
- a manual rerun names a `head_sha` that is a full 40-hex SHA and one of that PR's own commits,
  so no rerun analyses a foreign diff or writes a verdict onto another PR's gate;
- the free-model variables, the draft and fork skips and the read-only trust model follow the
  sibling workflows, whose comments swear to the same guards;
- the duplicated guard blocks the pre-mortem duplicates (summary-line extraction, fence strip,
  tree-clean check, the two-level skill selection) stay identical modulo the names each copy
  substitutes - the selection by a test of its own, which is the only way a copy that is not
  strict can fail it;
- the agent job that reads the checkout holds only `contents: read` plus the
  `statuses: write` its pending mark needs, keeps no credentials, and fails the
  run when HEAD moved or the tree is dirty;
- a quota, rate-limit or region refusal is never red (decision of 2026-11-21,
  issue #296): the `quota-probe` job pings the same model before the agent job
  installs anything and only a positive quota signature skips the agent - a
  dead probe fails open so a genuine failure still fails - the mid-run quota
  path in the agent step reports "not run: quota" instead of failing, the post
  job comments the skip and warns, and the commit status is `pending` with the
  reason or absent entirely, never `error` and never a verdict `success`. The
  probe's classification, the agent step's quota branch and the gating
  expressions are executed here against stubs, not just read.

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
            'listing=$(git ls-tree "$BASE_SHA" -- "$SKILL_PATH")',
            "the base path lookup runs as an `if` condition, where a failure is a false branch",
        ),
        ('if [ -n "$listing" ]', "the branch is not taken on the lookup's result"),
        (
            'git show "${BASE_SHA}:${SKILL_PATH}" > "$skill"',
            "does not read the skill from the base branch first",
        ),
        ('cp "$SKILL_PATH"', "no fallback to the PR's own copy"),
        ("SKILL_SOURCE=base branch", "records no skill source"),
        ("SKILL_SOURCE=this PR (not on the base branch yet)", "records no fallback source"),
    )
    return [message for needle, message in required if not any(needle in line for line in lines)]


# ---- the pre-mortem's skill selection, executed against a real repository ----

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

SKILL_BODY = "---\nname: actions-pre-mortem\n---\n\n# base rules\n"


@pytest.fixture
def probe_repo(tmp_path):
    """A two-commit repository the selection block can be run against: a base whose skill sits at
    `SKILL_PATH`, an older commit identical but for that one path, and a working tree (the PR head)
    of its own."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "probe",
        "GIT_AUTHOR_EMAIL": "probe@example.invalid",
        "GIT_COMMITTER_NAME": "probe",
        "GIT_COMMITTER_EMAIL": "probe@example.invalid",
    }

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=repo,
            env={**os.environ, **env},
            check=True,
            capture_output=True,
            text=True,
        )
        return done.stdout.strip()

    git("init", "-q", "--initial-branch=base", ".")
    skill = repo / ".agents/skills/actions-pre-mortem/SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(SKILL_BODY, encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base with a skill")
    with_skill = git("rev-parse", "HEAD")
    # A base that is the same commit with the one path removed: the lookup has to report "no
    # skill at that path" rather than "nothing to read".
    git("checkout", "-q", with_skill)
    skill.unlink()
    git("add", "-A")
    git("commit", "-qm", "base without the skill")
    without_skill = git("rev-parse", "HEAD")
    git("checkout", "-q", "-b", "head")
    return {
        "repo": repo,
        "with_skill": with_skill,
        "without_skill": without_skill,
        "unreachable": "0" * 40,
    }


def _run_selection(probe: dict, tmp_path, *, base: str, skill_path: str | None) -> tuple:
    """Execute the selection step's own `run:` body, and return (exit status, SKILL_SOURCE, text).

    The body is the workflow's own and the repository is real, so every `git` call in it does what
    it does in CI: a lookup that is a plain assignment has `errexit` applied to it, and one that
    is a command in an `if` condition does not - which is the whole difference between the two
    forms, and the reason the step is run here rather than read.
    """
    body = _run(_find_step(_agent_job(_load(PREMORTEM))[1], r"^Select skill revision$"))
    runner_temp = tmp_path / "premortem"
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")
    # A set-but-empty SKILL_PATH, which is what a workflow expression resolving to nothing leaves.
    environment = {**os.environ, "SKILL_PATH": skill_path or ""}
    environment.update(
        {
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_ENV": str(github_env),
            "BASE_SHA": base,
        }
    )
    script = probe["repo"] / "selection.sh"
    script.write_text(body, encoding="utf-8")
    result = subprocess.run(
        [shutil.which("bash") or "bash", str(script)],
        cwd=probe["repo"],
        env=environment,
        capture_output=True,
        text=True,
    )
    source = ""
    for line in github_env.read_text(encoding="utf-8").splitlines():
        if line.startswith("SKILL_SOURCE="):
            source = line.split("=", 1)[1]
    written = runner_temp / "premortem" / "skill.md"
    return result, source, (written.read_text(encoding="utf-8") if written.exists() else "")


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

# Every stop-path reporter of the pre-mortem: (job name, step name). The three that turn a
# `pending` status into an `error`. The resolve job has none: it marks nothing - the pending
# mark is the agent job's, written only once the quota probe has cleared the run to happen -
# so a resolve failure can strand no status and needs no reporter.
REPORTERS = (
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
    # Every case runs only its own pages. A two-page case leaves `page-2.json` behind, and the
    # stub's `--paginate` branch then serves it to the one-page cases that follow in the same
    # test: the near-miss run id, which is meant to be the newest status of that case, was
    # decided by a stale page nobody wrote for it - so the case passed for the wrong reason and a
    # prefix-match regression would not have failed it.
    for stale in sorted(pagedir.glob("page-*.json")):
        stale.unlink()
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


@needs_bash
def test_the_pending_mark_is_converted_only_when_this_run_wrote_it(tmp_path) -> None:
    """The three stop-path reporters exist to keep the stage from stranding `pending`, and each one
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


@needs_bash
def test_a_cancelled_dispatch_is_not_reported_as_a_fork_or_as_nothing_marked(tmp_path) -> None:
    """A cancelled job publishes no outputs, and a `workflow_dispatch` payload carries no
    `pull_request`, so the cancellation reporter's ownership fallback resolves to false and its
    SHA fallback to nothing - while "Mark running" ran under both, having already confirmed
    same-repo ownership. The mark can therefore strand: finding it means reading `pulls/N` by
    number, which needs a scope this job does not hold. The reporter says that instead of the
    opposite, because both of the old messages were false in exactly that case - "a fork PR was
    not pre-mortemed" (the run was a same-repo dispatch) and "this run marked nothing pending"
    ("Mark running" had just run). Executed, so the messages are the reporter's own."""
    step_name = "Report cancelled pre-mortem run"
    for why, override in (
        ("no ownership confirmation", {"SAME_REPO": "", "HEAD_SHA": REPORTER_ENV["HEAD_SHA"]}),
        ("no head SHA", {"SAME_REPO": REPORTER_ENV["SAME_REPO"], "HEAD_SHA": ""}),
    ):
        result, calls = _run_reporter("report-cancelled", step_name, tmp_path, [], override)
        assert result.returncode == 0, f"{why}: reporter failed: {result.stderr}"
        assert "fork" not in result.stdout, f"{why}: claims a fork PR ran:\n{result.stdout}"
        assert "marked nothing pending" not in result.stdout, (
            f"{why}: claims nothing was marked pending:\n{result.stdout}"
        )
        assert "left pending" in result.stdout, f"{why}: does not say the mark can strand"
        assert "state=error" not in calls, f"{why}: wrote a status it cannot place"


@needs_bash
def test_a_reporter_case_is_not_decided_by_a_previous_cases_pages(tmp_path) -> None:
    """The stub serves the status list from `page-N.json` files, and the four reporters are run
    several times per test against the same `tmp_path`. A two-page case leaves `page-2.json`
    behind, and the stub's paginating branch then serves it to every one-page case that follows:
    those cases decide "another run's mark is the newest" against a page nobody wrote for them,
    so the run they are testing leaves its own pending mark stranded. Pinned here by running a
    two-page case and then a one-page case that must be decided on its own page alone - this run's
    own pending mark, which the stale page would otherwise have covered."""
    job_name, step_name = REPORTERS[0]
    _run_reporter(
        job_name,
        step_name,
        tmp_path,
        [
            _status_page([(CONTEXT, "pending", OWN, T1)]),
            _status_page([(CONTEXT, "pending", OTHER, T2)]),
        ],
    )
    result, calls = _run_reporter(
        job_name, step_name, tmp_path, [_status_page([(CONTEXT, "pending", OWN, T1)])]
    )
    assert result.returncode == 0, f"{job_name}/{step_name}: reporter failed: {result.stderr}"
    assert "state=error" in calls, (
        "a one-page case was decided by a previous case's pages: this run's own pending mark was"
        " left stranded"
    )


@needs_bash
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
      |     (?P<ne>!=)
      |     (?P<lparen>\()
      |     (?P<rparen>\))
      |     (?P<comma>,)
      |     (?P<string>'(?:[^']*)'|"[^"]*")
      |     (?P<number>-?\d+)
      |     (?P<path>[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)
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
        if self._peek() and self._peek()[0] == "ne":
            self._eat("ne")
            return left != self._atom()
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
                if text == "format":
                    assert arguments, "format() takes a template"
                    return str(arguments[0]).format(*arguments[1:])
                # The status-check functions a job `if` may call. The scenarios
                # below are all non-cancelled runs, so the defaults answer what
                # GitHub would for them; a test that needs another answer puts it
                # in the context's "fns" map.
                assert text in ("cancelled", "success", "failure", "always"), (
                    f"unknown function {text}()"
                )
                assert not arguments, f"{text}() takes no arguments"
                defaults = {"cancelled": False, "success": True, "failure": False, "always": True}
                return self.context.get("fns", {}).get(text, defaults[text])
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


def test_the_documented_current_head_rerun_is_the_form_the_refusal_names() -> None:
    """The header documents how to re-run the current head, and the resolve step refuses naming
    it: an operator who copies the documented command straight after reading that refusal hits
    the refusal itself. The sentence and the command that follows it are pinned against each
    other, and against the remedy the refusal prints.

    The command that used to follow "Re-running the current head is `-f pr_number=<N>` alone:"
    was the older-commit form, `-f pr_number=<N> -f head_sha=<head-sha>` - the very form the
    workflow refuses for the current head - so the documented remedy for a current-head re-run
    could not be used. The bare form now follows the sentence, and the SHA form is labelled as
    the older-commit one so it is not read as the current head's."""
    text = (WORKFLOWS / PREMORTEM).read_text(encoding="utf-8")
    sentence = re.search(
        r"^#.*Re-running the current head is `-f pr_number=<N>` alone:$", text, re.M
    )
    assert sentence, "the header no longer documents the bare current-head rerun"
    documented = re.findall(r"^#\s+(gh workflow run \S.*)$", text[sentence.end() :], re.M)
    assert documented, "no rerun command follows that sentence"
    assert "head_sha" not in documented[0], (
        "the documented current-head rerun names a head_sha, which the workflow refuses:\n"
        + documented[0]
    )
    # The older-commit form is still documented, and says which commit it is for: dropping it
    # would leave the one case a dispatch exists for undocumented.
    with_sha = [command for command in documented if "head_sha" in command]
    assert with_sha, "the older-commit rerun form is no longer documented"
    leading = text[sentence.end() : text.index(with_sha[-1])]
    assert "older commit" in leading, (
        "the head_sha form is not labelled as the older-commit one, so it reads as the current"
        " head's:\n" + leading
    )
    # And the refusal names the same remedy the header documents.
    resolve = _load(PREMORTEM)["jobs"]["resolve"]
    body = _run(_find_step(resolve, r"^Resolve PR$"))
    assert "omit head_sha" in body, "the refusal no longer names the bare form"


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


def test_the_pre_mortem_checklist_agrees_with_the_time_budget() -> None:
    """The procedure stops at about 20 minutes and asks for a partial report, so the final
    checklist may demand that every hunk *reached* was covered - and must not demand that every
    changed hunk was: on a large diff that is impossible, and a pre-mortem that is told to claim
    coverage it does not have is being asked to invent exactly the thing it must never invent. The
    unreached work belongs under "Not verified", and the checklist says so."""
    skill = (SKILLS / SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    section = skill.split("## Before you output", 1)[1]
    assert "budget" in section, "the checklist does not mention the time budget it must respect"
    assert "Not verified" in section, (
        "the checklist does not defer unreached hunks to 'Not verified'"
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
    # The agent job also writes the pending mark, so it holds `statuses: write`
    # on top of the read scope.
    assert agent.get("permissions") == {"contents": "read", "statuses": "write"}, (
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
            # The pre-mortem's "Mark running" is the one status write the agent
            # job owns: it moved here from resolve so a quota-skipped run never
            # strands a `pending` mark. Its token is step-scoped - the agent step
            # never sees it - which is what the secrets check below still pins.
            if step.get("name") == "Mark running" and job_name == agent_name:
                continue
            assert job_name != agent_name, (
                f"the agent job posts a status or a comment ({step.get('name')!r})"
            )
            assert "GH_TOKEN" in _step_env(job, step), (
                f"{job_name}/{step.get('name')}: posts without the repo token"
            )
    for step in _steps(agent):
        if step.get("name") == "Mark running":
            continue
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


def test_a_new_agent_stage_cannot_go_unregistered() -> None:
    """`AGENT_WORKFLOWS` is three literal names, so the fourth agent workflow - a copy of one of
    these, which is how the next one will be written - reports under this file's context, marker or
    conventions with no test failing. The file globs nothing: every workflow in `.github/` that
    names an agent context or marker must therefore be one of the registered three, and a new one
    fails here until it is registered - which is the point, since registering it means deciding
    which stage owns which marker and which status context."""
    registered = set(AGENT_WORKFLOWS)
    known = set(CONTEXT_OWNER.values()) | set(MARKERS.values())
    assert known == registered, "the registered workflows and the owned names disagree"
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if any(f"{name}" in text for name in list(CONTEXT_OWNER) + list(MARKERS)):
            assert path.name in registered, (
                f"{path.name}: names an agent stage's context or marker and is not registered here"
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
    """A push shows the stage as running while the agent job works, and the mark goes to the
    commit being analysed - not, on a manual rerun, to the PR's current head. One write only:
    two pending marks would race, and a second one in an agent job would hand the agent a
    token. The mark lives in the agent job, not resolve: a run the quota probe skips never
    reaches it, so a quota skip leaves no `pending` status behind at all - the "not run:
    quota" path is genuinely neutral, which is what the decision of 2026-11-21 asks for."""
    data = _load(PREMORTEM)
    agent_name, agent = _agent_job(data)
    assert agent_name == "premortem", f"the agent job is {agent_name!r}"
    mark = _find_step(agent, r"^Mark running$")
    body = _run(mark)
    assert "-f state=pending" in body and "$PREMORTEM_CONTEXT" in body, "no pending mark"
    assert "statuses/${HEAD_SHA}" in body, "the mark is not on the analysed head"
    assert "needs.resolve.outputs.head" in str(agent["env"]["HEAD_SHA"]), (
        "the mark's HEAD_SHA is not the resolved analysed head"
    )
    assert _all_run_bodies(data).count("state=pending") == 2, (
        "the pending writes are not the mark and the quota reporter's rewrite"
    )
    # Its description is "Running": there is no result yet, so it names no analysed pair - which
    # is why the header promises the pair on every *terminal* status description and not on this
    # one. What it carries instead is the run computing it, so a reader of a pending status has
    # somewhere to go.
    assert '-f target_url="$RUN_URL"' in body, "the pending mark links no run"
    # And resolve marks nothing: a quota-skipped run must not strand a pending status.
    assert "statuses/" not in _job_bodies([data["jobs"]["resolve"]]), (
        "resolve writes a commit status, so a skipped run can strand a pending mark"
    )


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
    # ... and that claim is only true if the re-read is the *last* thing the step does before the
    # write. Both reads used to sit above the body assembly and the comment-list fetch, so the
    # "window of the write itself" the comment swore to was the whole of that work - two API
    # round trips wide - and a synchronize landing inside it was still written over. So the work
    # that builds the write has to be *behind* the re-read, and a test that only compares the
    # re-read with the write cannot see the difference.
    for prepared in ("> body.md", "existing=$(gh api --paginate"):
        assert body.index(prepared) < reread, (
            f"the re-read does not gate the write: '{prepared}' runs after the head was last read"
        )
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
    mark = _run(_find_step(data["jobs"]["premortem"], r"^Mark running$"))
    assert '-f target_url="$RUN_URL"' in mark, "the pending mark names no run"


def test_every_stop_path_leaves_the_pre_mortem_context_terminal() -> None:
    """The bookkeeping that keeps the stage from stranding `pending`: a failed agent run, a
    failed post step and a cancelled run each write `error` for their own context, and each
    converts a `pending` status this run wrote itself rather than any still-pending one -
    including the reporter of the failed agent run, whose family (a rerun of the same commit
    that ran beside this one through the concurrency group) is exactly the case where a terminal
    status is already there. There is no resolve-failure reporter: resolve marks nothing, so a
    resolve failure strands no status. Deleting a reporter, or writing a literal context
    somewhere, would leave a SHA `pending` with no error anywhere - or a typo'd context nothing
    reads."""
    data = _load(PREMORTEM)
    post = data["jobs"]["post"]
    # Every reporter of a stopped run only clears a status that is still pending, so a failure of
    # one run cannot overwrite the terminal verdict of another run of the same commit.
    clear_pending_only = (
        "Report failed pre-mortem run",
        "Report post failure",
    )
    for job, pattern in (
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


def test_the_cancelled_reporter_falls_back_to_the_resolved_head() -> None:
    """A cancelled job publishes no outputs, so without the fallback chain a run cancelled
    after "Mark running" strands the mark on its SHA with nothing but a warning in a log
    nobody opens: the reporter falls back to the commit named by the event, and to the
    dispatch input."""
    job = _load(PREMORTEM)["jobs"]["report-cancelled"]
    step = _find_step(job, r"^Report cancelled pre-mortem run$")
    sha = str(_step_env(job, step).get("HEAD_SHA", ""))
    assert sha.startswith("${{ needs.resolve.outputs.head"), (
        f"the cancelled reporter must prefer the resolved head: {sha}"
    )
    assert "github.event.pull_request.head.sha" in sha and re.search(
        r"inputs\.head_sha\s*\}\}$", sha
    ), (
        "the cancelled reporter has no fallback chain to the event's head and the "
        f"dispatch input: {sha}"
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


def test_the_free_model_quota_is_a_skip_not_a_failure() -> None:
    """The free Zen models cap usage per period, and an exhausted cap must not read as a
    pre-mortem result: by the decision of 2026-11-21 (issue #296) a quota, rate-limit or
    region refusal is reported as "not run: quota" - a warning and a comment - and never as
    a red check or an `error` status, while a genuine failure still fails. Two paths carry
    it: the `quota-probe` job pings the model before the agent job installs anything and a
    positive quota signature skips the agent job (grey), and the agent step's own classifier
    takes the same path for a cap that only appears mid-run. A transient failure and a
    misconfiguration still need opposite remedies, so they are not one classifier: "re-run
    after the quota resets" is the fix for a cap, a rate limit or a region refusal, and a lie
    for a model or variant that cannot be resolved at all - there the run cannot succeed
    until PREMORTEM_MODEL / PREMORTEM_VARIANT changes."""
    data = _load(PREMORTEM)
    agent = _agent_job(data)[1]
    body = _run(_find_step(agent, r"^Run pre-mortem agent$"))
    classes = re.findall(r"grep -qiE '([^']+)'", body)
    assert len(classes) >= 2, (
        "the failure classifier is one grep, so it cannot tell a cap from a misconfiguration"
    )
    transient, misconfigured = classes[0], classes[1]
    for known in ("usage limit", "rate.?limit", "quota"):
        assert known in transient, f"{known!r} is not recognised as transient"
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
            "would take the quota path"
        )
    # The transient class takes the quota path: a warning that names the remedy, the
    # "not run: quota" comment body, the `quota` output the post job reads, and a clean
    # exit - the step does not fail, so the job does not go red. The body lives in
    # `report_quota_skip`, reached from the non-zero-exit classifier and from the two
    # zero-exit dead ends (no summary line, no valid verdict) where the cap has been
    # seen arriving on a zero exit.
    quota_branch = body.split("grep -qiE '" + transient + "'", 1)[1]
    head = quota_branch.split("elif", 1)[0]
    assert "report_quota_skip" in head, "the transient class does not take the quota path"
    func_body = body.split("report_quota_skip() {", 1)[1].split("\n          }", 1)[0]
    assert "::warning::" in func_body and "not run: quota" in func_body, (
        "the transient class does not warn and name the quota skip"
    )
    assert 'echo "quota=true" >> "$GITHUB_OUTPUT"' in func_body, (
        "the agent step does not report the quota skip to the post job"
    )
    assert "Not run: quota" in func_body and "comment.md" in func_body, (
        "the mid-run quota path writes no 'not run: quota' comment"
    )
    assert "exit 0" in func_body, "the mid-run quota path still fails the step"
    # The zero-exit dead ends classify before they fail: the transient signature is
    # grepped over the raw stream and stderr at both, so a cap on a zero exit takes
    # the quota path instead of going red. Only on those paths - a produced report's
    # own text may mention "quota".
    dead_end_grep = f"grep -qiE '{transient}' \"$out.raw\" \"$out.stderr\""
    assert body.count(dead_end_grep) == 2, (
        "the two zero-exit dead ends do not both classify the quota signature"
    )
    # The reason is only in stderr. A failed run has ended with a bare "exit code 1" and no
    # cause, so a capped model and a broken prompt could not be told apart from the log: the
    # captured output is echoed, bounded so a runaway agent cannot flood the log.
    assert "tail -c" in body and "$out.raw" in body, (
        "the agent's failing output is not echoed, so the run log shows no reason"
    )
    # A genuine failure still fails: the misconfiguration and the unclassified classes exit 1,
    # and the post job still reports a failed run as `error`.
    assert body.count("exit 1") >= 1, "a genuine agent failure does not fail the run"
    post = data["jobs"]["post"]
    report = _find_step(post, r"^Report failed pre-mortem run$")
    assert "state=error" in _run(report), "a failed run is not reported as an error"
    assert "needs.premortem.result == 'failure'" in str(post.get("if", "")), (
        "the post job does not run for a failed agent job"
    )
    # The verdict status is not written for a quota skip, and the quota reporter rewrites
    # this run's own pending mark as `pending` - never `error`, never a verdict `success`.
    set_status = _find_step(post, r"^Set status$")
    assert "needs.premortem.outputs.quota != 'true'" in str(set_status.get("if", "")), (
        "Set status maps an empty quota-path verdict to `error`"
    )
    quota_report = _find_step(post, r"^Report quota skip$")
    quota_body = _run(quota_report)
    assert "state=pending" in quota_body and "state=error" not in quota_body, (
        "the quota skip writes a red or terminal status"
    )
    assert "Not run: quota" in quota_body, "the quota status does not say why nothing ran"
    assert '"$owner" = "true"' in quota_body, "the quota reporter rewrites another run's status"


def _job_if(data: dict, job_name: str) -> str:
    """The job's `if` expression, without the `${{ }}` wrapper."""
    expr = str(data["jobs"][job_name].get("if", ""))
    match = re.fullmatch(r"\$\{\{(.*)\}\}", expr.strip())
    assert match, f"{job_name}: if is not an expression: {expr!r}"
    return match.group(1)


def _run_context_for(needs: dict) -> dict:
    """The expression context a job `if` is evaluated against: `needs.<job>.*`."""
    return {"needs": needs}


def test_the_quota_probe_gates_the_agent_job() -> None:
    """The decision's grey check is a job shape, not a status write: the probe runs after
    resolve and before the agent job installs anything, and only its positive `available=
    false` skips the agent - a probe that failed, was skipped or never ran leaves the output
    empty, which is not 'false', so the agent still runs and a genuine failure still fails.
    The `if` is executed against the needs contexts, because a string assertion cannot tell
    a gate that skips on quota from one that also skips on a dead probe."""
    data = _load(PREMORTEM)
    jobs = data["jobs"]
    probe = jobs["quota-probe"]
    assert probe.get("needs") == "resolve" or probe.get("needs") == ["resolve"], (
        "the probe does not wait for the PR to be resolved"
    )
    assert "same_repo" in str(probe.get("if", "")), (
        "the probe pings the model for a fork PR or a skipped resolve"
    )
    assert probe.get("permissions") == {}, "the probe holds a token scope it does not need"
    assert not any(str(s.get("uses", "")).startswith("actions/checkout") for s in _steps(probe)), (
        "the probe checks out the PR it is only meant to price"
    )
    assert jobs["premortem"].get("needs") == ["resolve", "quota-probe"] or set(
        jobs["premortem"].get("needs") or []
    ) >= {"resolve", "quota-probe"}, "the agent job does not need the probe"
    agent_if = _job_if(data, "premortem")
    post_if = _job_if(data, "post")

    def evaluate(expr: str, needs: dict):
        return _Expr(expr, _run_context_for(needs)).evaluate()

    resolved = {"result": "success", "outputs": {"same_repo": "true", "number": "1"}}
    scenarios = {
        "quota available": (
            agent_if,
            {
                "resolve": resolved,
                "quota-probe": {
                    "result": "success",
                    "outputs": {"available": "true"},
                },
            },
            True,
        ),
        "quota exhausted": (
            agent_if,
            {
                "resolve": resolved,
                "quota-probe": {
                    "result": "success",
                    "outputs": {"available": "false"},
                },
            },
            False,
        ),
        "probe job itself failed": (
            agent_if,
            {"resolve": resolved, "quota-probe": {"result": "failure", "outputs": {}}},
            True,
        ),
        "probe skipped with the resolve": (
            agent_if,
            {
                "resolve": {"result": "skipped", "outputs": {}},
                "quota-probe": {"result": "skipped", "outputs": {}},
            },
            False,
        ),
        "fork PR": (
            agent_if,
            {
                "resolve": {"result": "success", "outputs": {"same_repo": "false"}},
                "quota-probe": {"result": "skipped", "outputs": {}},
            },
            False,
        ),
        "post on a quota skip": (
            post_if,
            {
                "resolve": resolved,
                "premortem": {"result": "skipped", "outputs": {}},
                "quota-probe": {"result": "success", "outputs": {"available": "false"}},
            },
            True,
        ),
        "post on a fork": (
            post_if,
            {
                "resolve": {"result": "success", "outputs": {"same_repo": "false"}},
                "premortem": {"result": "skipped", "outputs": {}},
                "quota-probe": {"result": "skipped", "outputs": {}},
            },
            False,
        ),
        "post on a failed agent job": (
            post_if,
            {
                "resolve": resolved,
                "premortem": {"result": "failure", "outputs": {}},
                "quota-probe": {"result": "success", "outputs": {"available": "true"}},
            },
            True,
        ),
    }
    for why, (expr, needs, expected) in scenarios.items():
        assert evaluate(expr, needs) is expected, f"{why}: the gate decided wrong"


_STUB_OPENCODE = """#!/usr/bin/env bash
# A stand-in for the `opencode` the quota probe and the agent step call.
# $STUB_OPENCODE_MODE selects the reply: ok, quota (a cap signature on stderr,
# exit 1), quota-out / quota-err / quota-zero (the same signature on a ZERO
# exit - the shape run 44962236033 showed the probe cannot currently see),
# quota-verdict (a started report that never reaches a verdict, cap signature
# on stdout, exit 0), garbage (a zero exit with no signature and no report),
# no-ok (a zero exit whose reply carries no OK token), other (a non-quota
# failure, exit 1).
set -u
case "${STUB_OPENCODE_MODE:-ok}" in
  ok) printf 'OK\\n'; exit 0 ;;
  quota) printf 'Free usage limit reached for this model\\n' >&2; exit 1 ;;
  quota-out) printf 'Free usage limit reached for this model\\n'; exit 0 ;;
  quota-err) printf 'Free usage limit reached for this model\\n' >&2; exit 0 ;;
  quota-zero) printf 'Free usage limit reached for this model\\n'; exit 0 ;;
  quota-verdict)
    printf '**Summary:** the agent started.\\nFree usage limit reached for this model\\n'
    exit 0
    ;;
  garbage) printf 'The agent said something unrelated.\\n'; exit 0 ;;
  no-ok) printf 'Here is the answer you asked for.\\n'; exit 0 ;;
  other) printf 'Error: no provider for model\\n' >&2; exit 1 ;;
esac
"""


def _run_probe_step(tmp_path, mode: str):
    """Execute the probe step's own `run:` body against a stub `opencode`."""
    job = _load(PREMORTEM)["jobs"]["quota-probe"]
    step = _find_step(job, r"^Probe model quota$")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "opencode"
    stub.write_text(_STUB_OPENCODE, encoding="utf-8")
    stub.chmod(0o755)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    output = tmp_path / "github-output"
    output.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": os.pathsep.join([str(bindir), os.environ["PATH"]]),
        "STUB_OPENCODE_MODE": mode,
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_OUTPUT": str(output),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    }
    script = tmp_path / "probe.sh"
    script.write_text(_run(step), encoding="utf-8")
    result = subprocess.run(
        [shutil.which("bash") or "bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
    )
    written = output.read_text(encoding="utf-8")
    return result, written


@needs_bash
def test_the_quota_probe_reports_only_a_positive_quota_signature(tmp_path) -> None:
    """The probe's whole contract is its output: `available=false` only when the model's
    own reply carries a quota signature. Executed against a stub `opencode`, because the
    classification is a grep over stderr and a string assertion cannot tell a pattern that
    matches from one that does not. A success and a non-quota failure both fail open - the
    agent job runs and the genuine failure surfaces there."""
    result, written = _run_probe_step(tmp_path, "ok")
    assert result.returncode == 0, f"probe step failed: {result.stderr}"
    assert "available=true" in written, "a reachable model did not clear the agent job"
    # The probe's output is logged on every path, bounded: run 44962236033
    # passed the probe and hit "Free usage limit reached" 26 s later, and with
    # the output unlogged there was no way to tell a real reach from a capped
    # one - the success path used to log nothing at all.
    assert "::group::Quota probe output" in result.stdout, (
        "the probe does not log its output on the success path"
    )

    result, written = _run_probe_step(tmp_path, "quota")
    assert result.returncode == 0, f"probe step failed on a quota reply: {result.stderr}"
    assert "available=false" in written, "a quota reply did not skip the agent job"
    assert "::warning::" in result.stdout, "the quota skip does not warn"
    assert "not run: quota" in result.stdout, "the quota skip does not name itself"

    # The cap has been observed arriving on a ZERO exit, on either stream: the
    # signature is grepped over stdout and stderr whatever the status, so a
    # capped model cannot report `available=true`.
    for mode in ("quota-out", "quota-err"):
        result, written = _run_probe_step(tmp_path, mode)
        assert result.returncode == 0, f"probe step failed on {mode}: {result.stderr}"
        assert "available=false" in written, (
            f"{mode}: a quota signature on a zero exit did not skip the agent job"
        )
        assert "not run: quota" in result.stdout, f"{mode}: the skip does not name itself"

    result, written = _run_probe_step(tmp_path, "no-ok")
    assert result.returncode == 0, f"probe step failed on a no-OK reply: {result.stderr}"
    assert "available=true" in written, (
        "a zero exit without the OK token skipped the agent job - only a quota "
        "signature may do that"
    )
    assert "::warning::" in result.stdout, (
        "the inconclusive probe (no OK, no signature) fails open silently"
    )

    result, written = _run_probe_step(tmp_path, "other")
    assert result.returncode == 0, f"probe step failed on a non-quota reply: {result.stderr}"
    assert "available=true" in written, (
        "a non-quota probe failure skipped the agent job - a genuine failure would go grey"
    )
    assert "::warning::" in result.stdout, "the fail-open path does not say what it did"


def test_the_quota_probe_agent_is_denied_every_tool() -> None:
    """The probe pings the model with a one-word prompt; it needs no tool at
    all. Its agent config is a deny-all, so a misbehaving model reply cannot
    make the probe act - the earlier copy allowed shell, read and
    external_directory for a ping that uses none of them."""
    job = _load(PREMORTEM)["jobs"]["quota-probe"]
    body = _run(_find_step(job, r"^Configure quota probe agent$"))
    assert "effect: allow" not in body, (
        "the quota probe agent is allowed tools it does not need"
    )
    assert 'effect: deny' in body, "the quota probe agent has no deny rule at all"


def test_the_quota_probe_is_identical_in_every_workflow_that_has_one() -> None:
    """The probe job is copied into each agent workflow; a fix to the
    classification that lands in one copy and not the others is the drift the
    bundle-26 review found. Every workflow that defines `quota-probe` must
    carry the same probe agent config and the same probe step body."""
    seen: dict[str, dict[str, str]] = {}
    for name in AGENT_WORKFLOWS:
        job = _load(name)["jobs"].get("quota-probe")
        if job is None:
            continue
        for step_name in ("Configure quota probe agent", "Probe model quota"):
            body = _run(_find_step(job, rf"^{re.escape(step_name)}$"))
            seen.setdefault(step_name, {})[name] = body
    for step_name, by_workflow in seen.items():
        unique = set(by_workflow.values())
        assert len(unique) == 1, (
            f"{step_name} differs across {sorted(by_workflow)} - the probe "
            "classification must not drift between the agent workflows"
        )


def _agent_step_env(tmp_path, head: str) -> dict:
    """The environment the 'Run pre-mortem agent' step reads, with every file it touches."""
    premortem = tmp_path / "runner-temp" / "premortem"
    premortem.mkdir(parents=True, exist_ok=True)
    (premortem / "meta.json").write_text("{}", encoding="utf-8")
    env = {
        "RUNNER_TEMP": str(tmp_path / "runner-temp"),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "step-summary"),
        "PR_NUMBER": "185",
        "BASE_SHA": "1" * 40,
        "HEAD_SHA": head,
        "SKILL_SOURCE": "base branch",
        "SKILL_PATH": ".agents/skills/actions-pre-mortem/SKILL.md",
        "MODEL": "opencode/inkling-2-small-free",
        "VARIANT": "max",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "PREMORTEM_CONTEXT": CONTEXT,
    }
    for path in (env["GITHUB_OUTPUT"], env["GITHUB_STEP_SUMMARY"]):
        Path(path).write_text("", encoding="utf-8")
    return env


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "probe",
        "GIT_AUTHOR_EMAIL": "probe@example.invalid",
        "GIT_COMMITTER_NAME": "probe",
        "GIT_COMMITTER_EMAIL": "probe@example.invalid",
    }
    done = subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


@needs_bash
def test_the_mid_run_quota_skip_reports_instead_of_failing(tmp_path) -> None:
    """The probe clears the agent job to run, but the cap can still appear mid-run - the
    decision covers that too. Executed against a stub `opencode` that fails with a quota
    signature: the step must write the "not run: quota" comment and the `quota` output and
    exit 0, so the job stays green-ish and the post job comments the skip. A misconfigured
    model is the opposite case and must still exit 1."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main", ".")
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "head")
    head = _git(repo, "rev-parse", "HEAD")

    agent = _agent_job(_load(PREMORTEM))[1]
    body = _run(_find_step(agent, r"^Run pre-mortem agent$"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "opencode"
    stub.write_text(_STUB_OPENCODE, encoding="utf-8")
    stub.chmod(0o755)

    # The cap has been seen arriving on a ZERO exit: the signature is grepped
    # over the raw stream and stderr at both dead ends, so a capped model that
    # exits 0 - with no report at all, or with a report that never reaches a
    # verdict - takes the quota path instead of failing the run. A zero exit
    # with no signature is still a failure.
    for mode, expect_ok in (
        ("quota", True),
        ("quota-zero", True),
        ("quota-verdict", True),
        ("garbage", False),
        ("other", False),
    ):
        case = tmp_path / mode
        case.mkdir()
        env = {**os.environ, **_agent_step_env(case, head)}
        env["PATH"] = os.pathsep.join([str(bindir), os.environ["PATH"]])
        env["STUB_OPENCODE_MODE"] = mode
        script = case / "step.sh"
        script.write_text(body, encoding="utf-8")
        result = subprocess.run(
            [shutil.which("bash") or "bash", str(script)],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )
        if expect_ok:
            assert result.returncode == 0, (
                f"{mode}: the quota path failed the step:\n{result.stderr}"
            )
            comment = (case / "runner-temp" / "premortem" / "comment.md").read_text(
                encoding="utf-8"
            )
            assert "Not run: quota" in comment, f"{mode}: no 'not run: quota' comment"
            assert "# Pre-Mortem Action" in comment, f"{mode}: the comment has no title"
            assert "quota=true" in Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8"), (
                f"{mode}: the post job is not told this was a quota skip"
            )
            assert "::warning::" in result.stdout and "::error::" not in result.stdout, (
                f"{mode}: the quota path does not warn without erroring"
            )
        else:
            assert result.returncode == 1, f"{mode}: a genuine failure did not fail the step"
            assert "quota=true" not in Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8"), (
                f"{mode}: a non-quota failure took the quota path"
            )


@needs_bash
def test_the_quota_reporter_rewrites_only_this_runs_pending_mark(tmp_path) -> None:
    """'Report quota skip' is the mid-run quota path's status write, executed against the
    stub `gh` like the other reporters: this run's own pending mark is rewritten as
    `pending` - the commit status API has no neutral state, so pending with the reason is
    the non-green, non-red answer - and never as `error` or `success`. Another run's mark,
    or no mark at all (the probe path, where the agent job never started), is left alone."""
    own_pending = [_status_page([(CONTEXT, "pending", OWN, T2)])]
    result, calls = _run_reporter("post", "Report quota skip", tmp_path, own_pending)
    assert result.returncode == 0, f"quota reporter failed: {result.stderr}"
    assert "state=pending" in calls, "this run's pending mark was not rewritten"
    assert "state=error" not in calls and "state=success" not in calls, (
        "the quota skip wrote a red or green status"
    )
    assert "Not run: quota" in calls, "the rewritten status does not say why nothing ran"
    assert f"context={CONTEXT}" in calls, "the rewrite carries no context"

    other_pending = [_status_page([(CONTEXT, "pending", OTHER, T2)])]
    result, calls = _run_reporter("post", "Report quota skip", tmp_path, other_pending)
    assert result.returncode == 0, f"quota reporter failed: {result.stderr}"
    assert "--method" not in calls, "the quota reporter rewrote another run's mark"
    assert "::notice::" in result.stdout, "the quota reporter is silent about the mark it left"

    result, calls = _run_reporter("post", "Report quota skip", tmp_path, [])
    assert result.returncode == 0, f"quota reporter failed with no status: {result.stderr}"
    assert "--method" not in calls, "the quota reporter wrote a status where none exists"


# A stand-in for the `gh` the comment-posting step calls: it serves the PR head
# read, the marker comment list and one comment body, and records every write.
_STUB_GH_COMMENTS = """#!/usr/bin/env bash
set -u
printf '%s\\n' "$*" >> "$STUB_LOG"
# Any write (POST/PATCH) is recorded by the log line above and acknowledged.
if [[ "$*" == *"--method"* ]]; then exit 0; fi
case "$*" in
  *"/pulls/"*) printf '%s\\n' "$STUB_HEAD_SHA" ;;
  *"/issues/comments/"*) cat "$STUB_COMMENT_BODY" ;;
  *"/issues/"*"/comments"*) cat "$STUB_COMMENT_IDS" ;;
  *) : ;;
esac
exit 0
"""


def _run_comment_post(tmp_path, *, quota_skip: bool, prior_body: str | None):
    """Execute 'Post or update pre-mortem comment' against the comments stub.

    `prior_body` is the body of the one existing marker comment (None: no
    comment yet). Returns (result, calls log)."""
    step = _find_step(_load(PREMORTEM)["jobs"]["post"], r"^Post or update pre-mortem comment$")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "gh"
    stub.write_text(_STUB_GH_COMMENTS, encoding="utf-8")
    stub.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "comment.md").write_text(
        "# Pre-Mortem Action\n\n**Summary:** Not run: quota\n", encoding="utf-8"
    )
    ids = tmp_path / "ids"
    ids.write_text("42\n" if prior_body is not None else "", encoding="utf-8")
    body = tmp_path / "prior-body"
    body.write_text(prior_body or "", encoding="utf-8")
    log = tmp_path / "calls.log"
    log.unlink(missing_ok=True)
    env = {
        "PATH": os.pathsep.join([str(bindir), os.environ["PATH"]]),
        "STUB_LOG": str(log),
        "STUB_HEAD_SHA": REPORTER_ENV["HEAD_SHA"],
        "STUB_COMMENT_IDS": str(ids),
        "STUB_COMMENT_BODY": str(body),
        "GITHUB_REPOSITORY": REPORTER_ENV["GITHUB_REPOSITORY"],
        "PR_NUMBER": REPORTER_ENV["PR_NUMBER"],
        "HEAD_SHA": REPORTER_ENV["HEAD_SHA"],
        "BASE_SHA": REPORTER_ENV["BASE_SHA"],
        "QUOTA_SKIP": "true" if quota_skip else "false",
    }
    script = tmp_path / "post.sh"
    script.write_text(_run(step), encoding="utf-8")
    result = subprocess.run(
        [shutil.which("bash") or "bash", str(script)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return result, calls


@needs_bash
def test_a_quota_skip_does_not_overwrite_a_real_verdict_for_the_same_head(tmp_path) -> None:
    """A manual re-run of the same head after a real verdict must not replace it
    with 'not run: quota'. Executed against the comments stub: when the body
    being posted is a quota skip and the existing marker comment is a real
    verdict for this head, the write is skipped; every other combination still
    writes."""
    head8 = REPORTER_ENV["HEAD_SHA"][:8]
    verdict_body = (
        "<!-- actions-pre-mortem -->\n# Pre-Mortem Action\n\n"
        "**Summary:** scenarios\n\nVerdict: PASS\n\n"
        f"_Analysed base `{'1' * 8}` ... head `{head8}`._\n"
    )
    quota_body = (
        "<!-- actions-pre-mortem -->\n# Pre-Mortem Action\n\n"
        "**Summary:** Not run: quota\n\n"
        f"_Analysed base `{'1' * 8}` ... head `{head8}`._\n"
    )

    result, calls = _run_comment_post(tmp_path, quota_skip=True, prior_body=verdict_body)
    assert result.returncode == 0, f"comment post failed: {result.stderr}"
    assert "--method" not in calls, (
        "a quota skip overwrote a real verdict for the same head"
    )
    assert "::notice::" in result.stdout, "the skipped overwrite is silent"

    case = tmp_path / "quota-prior"
    case.mkdir()
    result, calls = _run_comment_post(case, quota_skip=True, prior_body=quota_body)
    assert result.returncode == 0, f"comment post failed: {result.stderr}"
    assert "--method PATCH" in calls, (
        "a quota skip over an older quota comment did not refresh it"
    )

    case = tmp_path / "verdict-prior"
    case.mkdir()
    result, calls = _run_comment_post(case, quota_skip=False, prior_body=verdict_body)
    assert result.returncode == 0, f"comment post failed: {result.stderr}"
    assert "--method" in calls, "a real verdict did not overwrite the prior comment"

    # The guard is scoped to *this* head: a real verdict for a different commit
    # must not pin the comment forever, so a quota skip over it still writes.
    case = tmp_path / "verdict-other-head"
    case.mkdir()
    other_head_body = verdict_body.replace(head8, "f" * 8)
    result, calls = _run_comment_post(case, quota_skip=True, prior_body=other_head_body)
    assert result.returncode == 0, f"comment post failed: {result.stderr}"
    assert "--method PATCH" in calls, (
        "a quota skip refused to refresh a verdict for a different head"
    )

    # And the guard is fed by both quota paths: the probe's `available=false`
    # and the agent step's own `quota` output. Dropping either clause from the
    # QUOTA_SKIP expression unpins the mid-run path, so the expression itself
    # is asserted, not just the shell it feeds.
    step = _find_step(
        _load(PREMORTEM)["jobs"]["post"], r"^Post or update pre-mortem comment$"
    )
    quota_skip_env = str((step.get("env") or {}).get("QUOTA_SKIP", ""))
    for clause in (
        "needs.premortem.outputs.quota",
        "needs.quota-probe.outputs.available",
    ):
        assert clause in quota_skip_env, (
            f"QUOTA_SKIP does not read {clause}: {quota_skip_env!r}"
        )


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

    The assertions are therefore scoped to the selection step of this workflow, not to the agent
    job as a whole: a base-commit check that lives in some other step leaves the selection itself
    able to fall through, which is how the two siblings stayed non-strict while a coarser test
    passed over them - they are hardened, and pinned, by the PR that owns them."""
    for name in (PREMORTEM,):
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


@needs_bash
def test_a_truncated_report_is_diagnosable_from_the_run_log(tmp_path) -> None:
    """The posted report is the tail of the agent's stream, anchored on the last `**Summary:**`
    line anywhere in that stream - the extraction all three agent workflows share, and one that a
    later line at column 0 moves down: the skill's own template quoted back inside a Mitigation
    block starts a comment at the quote, and the report's summary and every severity section before
    it are dropped while the verdict still parses and the status is still written `success`. The
    anchor itself is the siblings' too, so it is not changed in this PR. What is pinned here is
    that the truncation cannot happen unnoticed: the byte counts are logged and the raw stream is
    an artifact of the run, so a comment that lost its opening sections is a run someone can
    reconstruct and read."""
    body = _run(_find_step(_agent_job(_load(PREMORTEM))[1], r"^Run pre-mortem agent$"))
    block = re.search(
        r"^\s*start=\$\(.*?^.*bytes of streamed messages[^\n]*\n", body, flags=re.S | re.M
    )
    assert block, "the extraction and the byte counts are not one contiguous block"
    assert "verdict=" not in block.group(0), "the captured block runs past the byte counts"
    out = tmp_path / "out.md"
    report = (
        "**Summary:** the real summary line.\n\n### Severe\n- the scenario\n\n"
        "**Verdict: BLOCK** - harden. Human sign-off required.\n"
    )
    # A later line at column 0, quoting the skill's own template back: the anchor moves here.
    quoted = (
        "**Summary:** <one line: risk surface>\n\n### Mitigation\n"
        "**Summary:** the template quoted back\n\n"
        "**Verdict: BLOCK** - harden. Human sign-off required.\n"
    )
    (tmp_path / "out.md.raw").write_text(
        "streamed preamble\n\n" + report + "\n---\n\n" + quoted, encoding="utf-8"
    )
    script = f'out="{out.as_posix()}"\n{block.group(0)}'
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    notice = [line for line in result.stdout.splitlines() if "pre-mortem report is" in line]
    assert notice, (
        f"the run log does not record how much of the stream was posted: {result.stdout!r}"
    )
    counts = re.search(r"is (\d+) of the agent's (\d+) bytes", notice[0])
    assert counts, f"the notice names no byte counts: {notice[0]!r}"
    kept, total = (int(counts.group(1)), int(counts.group(2)))
    assert kept == out.stat().st_size, (
        f"the logged kept count {kept} is not the posted report's {out.stat().st_size} bytes"
    )
    assert total == out.with_suffix(".md.raw").stat().st_size, (
        f"the logged total {total} is not the raw stream's size"
    )
    assert kept < total, "the notice would not describe a truncation"
    assert f"{total - kept} bytes" in notice[0], (
        f"the notice does not name how much was dropped: {notice[0]!r}"
    )
    # The raw stream is uploaded beside the comment, so the dropped part is reachable: the
    # comment alone cannot show what the stream carried.
    uploads = [
        step
        for step in _steps(_agent_job(_load(PREMORTEM))[1])
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert uploads, "the agent job uploads no artifact"
    paths = uploads[0]["with"]["path"]
    assert "comment.md" in paths and "comment.md.raw" in paths, (
        f"the uploaded artifact drops the raw stream: {paths!r}"
    )


@needs_bash
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_selection_reads_the_base_skill_when_the_base_has_one(probe_repo, tmp_path) -> None:
    """The documented first level, executed against a real base commit: the base branch's skill is
    what runs, and the log and the comment say so."""
    result, source, written = _run_selection(
        probe_repo,
        tmp_path,
        base=probe_repo["with_skill"],
        skill_path=".agents/skills/actions-pre-mortem/SKILL.md",
    )
    assert result.returncode == 0, f"the selection failed: {result.stderr}"
    assert source == "base branch", f"SKILL_SOURCE is {source!r}"
    assert written == SKILL_BODY, f"wrote {written!r}"


@needs_bash
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_selection_falls_back_to_the_prs_copy_only_when_the_base_has_none(
    probe_repo, tmp_path
) -> None:
    """The documented second level, executed: the PR's own copy runs only when the base has no
    skill at that path - and the recorded source says so, so a verdict produced by rules the PR
    chose for itself is never indistinguishable from the base's."""
    skill_path = ".agents/skills/actions-pre-mortem/SKILL.md"
    (probe_repo["repo"] / skill_path).write_text("PR's own rules", encoding="utf-8")
    result, source, written = _run_selection(
        probe_repo, tmp_path, base=probe_repo["without_skill"], skill_path=skill_path
    )
    assert result.returncode == 0, f"the selection failed: {result.stderr}"
    assert source == "this PR (not on the base branch yet)", f"SKILL_SOURCE is {source!r}"
    assert written == "PR's own rules", f"wrote {written!r}"


@needs_bash
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_selection_stops_on_what_it_cannot_read(probe_repo, tmp_path) -> None:
    """The failures the fail-closed lookup exists for, executed. This is the defect the round
    found: the path lookup used to be the command inside `if git cat-file -e ... 2>/dev/null`,
    where a failure is a false branch and not an abort, so *every* `git` error - a missing object,
    an unreadable repository, a wrong revision syntax - read as "the base has no skill" and the
    PR's own copy ran under a prompt saying the rules came from the base branch. The head carries
    its own copy here, so a fall-through would use it silently."""
    skill_path = ".agents/skills/actions-pre-mortem/SKILL.md"
    (probe_repo["repo"] / skill_path).write_text("PR's own rules", encoding="utf-8")
    result, source, _ = _run_selection(
        probe_repo, tmp_path, base=probe_repo["unreachable"], skill_path=skill_path
    )
    assert result.returncode != 0, "an unreachable base commit did not stop the selection"
    assert not source, f"recorded a skill source on a failure: {source!r}"

    # A set-but-empty SKILL_PATH: the base commit is readable and the path is not one the
    # repository can carry, which `git cat-file -e "$BASE_SHA:"` as an `if` condition swallowed.
    result, source, _ = _run_selection(
        probe_repo, tmp_path, base=probe_repo["with_skill"], skill_path=None
    )
    assert result.returncode != 0, "a skill path the repository cannot carry did not stop the job"
    assert not source, f"recorded a skill source on a failure: {source!r}"


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
    two-level skill selection is not one of those exclusions either: it is strict in this
    workflow and pinned by `test_skill_selection_refuses_an_unreachable_base_and_keeps_two_levels`
    and `test_the_base_commit_guard_must_not_be_a_comment_line`, and the two siblings' copy of it
    is pinned by the PR that owns their hunks (`test_agent_sibling_hardening.py`), which is where
    a sibling copy that is not strict has to fail. This PR touches neither sibling file.
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
        # The pre-mortem's agent job also writes the pending mark and reads no
        # credential itself; the siblings' agent jobs write nothing.
        expected = {"contents": "read"} | ({"statuses": "write"} if name == PREMORTEM else {})
        assert permissions == expected, f"{name}: agent job permissions are {permissions}"
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


@needs_bash
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
