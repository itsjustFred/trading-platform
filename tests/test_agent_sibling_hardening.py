"""The two sibling agent workflows take their rules from the base branch, so a PR cannot weaken
the job that grades it. Both select the skill the same way, and the selection is the one block in
the file that decides *whose* rules ran - which is exactly what a PR has an interest in changing.

Nothing in CI parses these workflows. A one-line regression - the standalone base-commit check
commented out, the path lookup put back inside an `if` where a `git` failure is only a false
branch, the notice of whose rules ran dropped - would merge silently, and the symptom is a verdict
produced by rules nobody chose, presented as one produced by the base branch's.

What is pinned here:
- the skill selection is strict in both copies, against its *executable* lines: a commented-out
  guard is not a guard, and the base path lookup is a plain assignment rather than a command in an
  `if` condition, so `errexit` applies to it - only an empty listing (the base genuinely has no
  skill at that path) takes the PR's own copy, and every other `git` failure stops the job;
- the selection is the same block in both copies, modulo the names each substitutes, so the two
  cannot drift apart and leave one of them grading under the weaker rules;
- both say whose rules ran, in the run log and in the comment footer, so a verdict produced by the
  PR's own skill copy (the fallback while the base branch has none) is never indistinguishable
  from one the base produced;
- the selection is *executed* here, against a real repository with a real base commit, for every
  branch it can take: the base's skill, the PR's own copy, neither, an unreachable base, and a
  skill path the job was configured with but the repository does not carry. A string assertion
  cannot tell a lookup that stops the job from one that quietly falls through, which is the
  defect this hardening fixes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

REVIEW = "agent-code-review.yml"
MUTATION = "agent-mutation-testing.yml"
SIBLINGS = (REVIEW, MUTATION)

# The names each copy substitutes for its own: (what the review copy carries, what the mutation
# copy carries, what both normalize to) - applied to the selection step's executable lines.
SUBSTITUTIONS = (
    (("review skill", "<skill>"), ("review", "<stage>")),
    (("mutation-testing skill", "<skill>"), ("mutation", "<stage>")),
)


def _load(name: str) -> dict:
    path = WORKFLOWS / name
    assert path.is_file(), f"missing workflow {path}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{name} did not parse to a mapping"
    return data


def _parse(text: str) -> dict:
    data = yaml.safe_load(text)
    assert isinstance(data, dict), "the rewritten workflow did not parse to a mapping"
    return data


def _agent_job(data: dict) -> tuple[str, dict]:
    """The job that runs the agent: the one that installs opencode."""
    for job_name, job in data["jobs"].items():
        if any(s.get("name") == "Install opencode" for s in job.get("steps", [])):
            return job_name, job
    raise AssertionError(f"no agent job in {list(data['jobs'])}")


def _find_step(job: dict, name_pattern: str) -> dict:
    rx = re.compile(name_pattern)
    for step in job.get("steps", []):
        if isinstance(step, dict) and isinstance(step.get("name"), str) and rx.search(step["name"]):
            return step
    raise AssertionError(f"no step matching {name_pattern!r}")


def _run(step: dict) -> str:
    return str(step.get("run", ""))


def _selection(job: dict) -> tuple[str, list[str]]:
    """The selection step's body, and its executable shell lines (stripped, prose comments
    dropped). A check that only exists on a `#` line is a check that does not run, so the
    strictness assertions below read these lines and never the raw body."""
    body = _run(_find_step(job, r"^Select skill revision$"))
    lines = []
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return body, lines


def _strictness_failures(lines: list[str], body: str) -> list[str]:
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
        ('cp "$SKILL_PATH" "$skill"', "no fallback to the PR's own copy"),
        ("SKILL_SOURCE=base branch", "records no skill source"),
    )
    failures = [
        message for needle, message in required if not any(needle in line for line in lines)
    ]
    # The suppressed-failure form is what the assignment above replaces: inside
    # `if git cat-file -e path 2>/dev/null` every git failure - a missing object, an unreadable
    # repository, a path the job was misconfigured with - reads as "the base has no skill", and
    # the PR's own copy runs under a prompt that says the rules came from the base.
    if 'git cat-file -e "${BASE_SHA}:${SKILL_PATH}"' in body:
        failures.append("the base path lookup still suppresses its own failure")
    return failures


def test_the_skill_selection_is_strict_in_both_siblings() -> None:
    """The rules come from the base branch, so a PR cannot weaken its own run; the PR's copy is
    used only while the base has none. A base commit that cannot be read must abort the step -
    GitHub runs a `run:` block with `bash -e {0}`, so the standalone `git cat-file -e` before the
    `if` is the guard, while a failing command *inside* an `if` condition is only a false branch.
    Any other `git` failure must stop the job too: that is what reading the path with `git ls-tree`
    into a variable buys, and the assertions below are what pins it."""
    for name in SIBLINGS:
        _, job = _agent_job(_load(name))
        body, lines = _selection(job)
        failures = _strictness_failures(lines, body)
        assert not failures, f"{name}: " + "; ".join(failures)
        # Two levels only: a third (the commit the workflow file came from) would analyse an older
        # commit under rules the branch tip happens to carry.
        assert "GITHUB_SHA" not in body, f"{name}: reads a third skill revision"
        assert body.count("elif") == 1, f"{name}: more than two levels:\n{body}"


def test_the_base_commit_guard_must_not_be_a_comment_line() -> None:
    """The guard is one shell line, and the cheapest way to delete it is to comment it out - which
    a substring assertion over the step body cannot see. So the line is commented out in a copy of
    each workflow here and the same executable-line check runs over that copy: it fails."""
    guard = 'git cat-file -e "${BASE_SHA}^{commit}"'
    for name in SIBLINGS:
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        mutated = re.sub(rf"(?m)^(\s*){re.escape(guard)}\s*$", rf"\1# {guard}", text)
        assert mutated != text, f"{name}: the guard is not a line of its own"
        _, job = _agent_job(_parse(mutated))
        failures = _strictness_failures(*_selection(job))
        assert any("unreachable base" in message for message in failures), (
            f"{name}: commenting the guard out does not fail the strictness check: {failures}"
        )


def test_the_two_selections_stay_identical() -> None:
    """The selection is duplicated in the two siblings, and it is the block that decides whose
    rules ran. One copy used to be strict while the other silently fell through to the PR's own
    copy on any `git` failure, and nothing compared the bodies. Here both are compared line by
    line after the per-copy name substitutions, so the next divergence fails here instead of
    grading a PR under rules it may have weakened."""
    selections = []
    for name, pair in zip(SIBLINGS, SUBSTITUTIONS):
        _, job = _agent_job(_load(name))
        _, lines = _selection(job)
        for old, new in pair:
            lines = [line.replace(old, new) for line in lines]
        selections.append(lines)
    for name, lines in zip(SIBLINGS, selections):
        assert lines, f"{name}: parsed no selection lines"
    assert selections[1] == selections[0], (
        f"{SIBLINGS[1]}: skill selection diverged:\n" + "\n".join(selections[1])
    )


def test_both_siblings_print_whose_rules_ran() -> None:
    """Both record `SKILL_SOURCE` in their `$GITHUB_ENV` and hand it to the agent, and a value
    nothing prints is a value nobody sees: the fallback it records (the PR's own copy, used while
    the base branch has none) is exactly the case a reader has to be able to tell apart. The
    mutation copy's empty-scope footer is excluded by design - no agent ran, so no rules ran."""
    for name in SIBLINGS:
        _, job = _agent_job(_load(name))
        bodies = "\n".join(_run(step) for step in job.get("steps", []))
        assert re.search(r"::notice::[A-Za-z-]+ rules from: \$\{SKILL_SOURCE\}", bodies), (
            f"{name}: the run log does not print whose rules ran"
        )
        assert "skill (rules from %s)" in bodies, (
            f"{name}: the comment footer drops the rules' source"
        )


SKILL_BODY = "---\nname: probe\n---\n\n# probe rules\n"


@pytest.fixture
def probe_repo(tmp_path):
    """A two-commit repository the selection block can be run against: `base` with a skill at
    `SKILL_PATH`, an older commit without it, and a working tree (the PR head) of its own."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "probe",
        "GIT_AUTHOR_EMAIL": "probe@example.invalid",
        "GIT_COMMITTER_NAME": "probe",
        "GIT_COMMITTER_EMAIL": "probe@example.invalid",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=repo,
            env={**os.environ, **env},
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-q", "--initial-branch=base", ".")
    # Both stages' skill directory, so either sibling's selection can be run against this repo.
    for stage in ("review", "mutation"):
        (repo / ".agents/skills" / stage / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
        (repo / ".agents/skills" / stage / "SKILL.md").write_text("base rules", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base with a skill")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    # One base per stage with that stage's skill absent, so each sibling's own
    # "the base has none" case is a base where the *other* stage's skill is still there.
    missing: dict[str, str] = {}
    for stage in ("review", "mutation"):
        git("checkout", "-q", base_sha)
        (repo / ".agents/skills" / stage / "SKILL.md").unlink()
        git("add", "-A")
        git("commit", "-qm", f"base without the {stage} skill")
        missing[stage] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
    git("checkout", "-q", "-b", "head")
    return {
        "repo": repo,
        "with_skill": base_sha,
        "missing": missing,
        "unreachable": "0" * 40,
    }


def _stage(name: str) -> str:
    """The stage a workflow is: the name its skill directory and its temp directory carry."""
    return "review" if name == REVIEW else "mutation"


def _path(stage: str) -> str:
    return f".agents/skills/{stage}/SKILL.md"


def _probe(probe_repo: dict, stage: str, tmp_path) -> dict:
    """The probe once the stage it is running for is known: the runner temp and the env file the
    selection block writes, and the file it writes the skill to."""
    probe_repo.update(
        {
            "runner_temp": tmp_path / stage,
            "github_env": tmp_path / f"{stage}-env",
            "skill_dir": stage,
        }
    )
    return probe_repo


def _run_selection(job: dict, probe: dict, *, base: str, skill_path: str | None):
    """Execute one sibling's selection block, and return (exit status, SKILL_SOURCE, skill text).

    The block is the workflow's own: the environment is the one it declares (the workflow-level
    `env:` of file-level names, filled in from the probe), and the repository is real, so every
    `git` call in it does what it does in CI."""
    body, _ = _selection(job)
    runner_temp = probe["runner_temp"]
    github_env = probe["github_env"]
    github_env.write_text("", encoding="utf-8")
    if skill_path is None:
        # Set but empty, as a workflow expression that resolves to nothing leaves it.
        environment = {**os.environ, "SKILL_PATH": ""}
    else:
        environment = {**os.environ, "SKILL_PATH": skill_path}
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
    written = runner_temp / probe["skill_dir"] / "skill.md"
    return result, source, (written.read_text(encoding="utf-8") if written.exists() else "")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize("name", SIBLINGS)
def test_the_selection_reads_the_base_skill_when_the_base_has_one(
    name, probe_repo, tmp_path
) -> None:
    """The documented first level, executed: the base branch's skill is what runs, and the log and
    the comment say so."""
    stage = _stage(name)
    probe = _probe(probe_repo, stage, tmp_path)
    result, source, written = _run_selection(
        _agent_job(_load(name))[1], probe, base=probe_repo["with_skill"], skill_path=_path(stage)
    )
    assert result.returncode == 0, f"{name}: {result.stderr}"
    assert source == "base branch", f"{name}: SKILL_SOURCE is {source!r}"
    assert written == "base rules", f"{name}: wrote {written!r}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize("name", SIBLINGS)
def test_the_selection_falls_back_to_the_prs_copy_only_when_the_base_has_none(
    name, probe_repo, tmp_path
) -> None:
    """The documented second level, executed: the PR's own copy runs only when the base has no
    skill at that path - and the recorded source says so, so a verdict produced by rules the PR
    chose for itself is never indistinguishable from the base's."""
    stage = _stage(name)
    probe = _probe(probe_repo, stage, tmp_path)
    (probe_repo["repo"] / _path(stage)).write_text("PR's own rules", encoding="utf-8")
    result, source, written = _run_selection(
        _agent_job(_load(name))[1],
        probe,
        base=probe_repo["missing"][stage],
        skill_path=_path(stage),
    )
    assert result.returncode == 0, f"{name}: {result.stderr}"
    assert source == "this PR (not on the base branch yet)", f"{name}: SKILL_SOURCE is {source!r}"
    assert written == "PR's own rules", f"{name}: wrote {written!r}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize("name", SIBLINGS)
def test_the_selection_stops_on_what_it_cannot_read(name, probe_repo, tmp_path) -> None:
    """The failures the hardening exists for, executed: an unreachable base commit, and a skill
    path that is not one the repository can carry. The second is the defect - the path lookup used
    to run as the `if` condition with its failure suppressed, so *every* `git` error read as "the
    base has no skill" and the PR's own copy ran under a prompt saying the rules came from the
    base branch. The head carries its own copy here, so a fall-through would use it silently."""
    stage = _stage(name)
    probe = _probe(probe_repo, stage, tmp_path)
    skill_path = _path(stage)
    (probe_repo["repo"] / skill_path).write_text("PR's own rules", encoding="utf-8")
    result, source, _ = _run_selection(
        _agent_job(_load(name))[1], probe, base=probe_repo["unreachable"], skill_path=skill_path
    )
    assert result.returncode != 0, f"{name} (unreachable base): the selection did not stop"
    assert not source, f"{name} (unreachable base): recorded a skill source on a failure"

    # A set-but-empty SKILL_PATH: the base commit is readable and the path is not one the
    # repository can carry, which `git cat-file -e "$BASE_SHA:"` as an `if` condition swallowed.
    result, source, _ = _run_selection(
        _agent_job(_load(name))[1], probe, base=probe_repo["with_skill"], skill_path=None
    )
    assert result.returncode != 0, f"{name} (empty skill path): the selection did not stop"
    assert not source, f"{name} (empty skill path): recorded a skill source on a failure"
