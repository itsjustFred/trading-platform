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

    # GitHub keeps one running and one *pending* run per group: a shared
    # non-PR group would let a newer main push silently drop a queued one, so
    # the group must key PR runs on the PR number and give every other event
    # a group unique to the run itself.
    group = str(concurrency["group"])
    assert "github.event.pull_request.number" in group, group
    assert "github.run_id" in group, (
        "non-PR events must group on the run id: keying them on the ref would "
        "let one queued main push cancel another's pending run"
    )
    assert "github.ref" not in group, group

    cancel = str(concurrency["cancel-in-progress"])
    assert "pull_request" in cancel, (
        "cancel-in-progress must be scoped to PR runs: cancelling a queued "
        "main push would leave a merged commit with no verdict"
    )


def test_ci_uses_setup_uv_with_cache() -> None:
    workflow = _workflow("ci.yml")
    uses = _uses_steps(workflow)
    setup_uv = [u for u in uses if u.startswith("astral-sh/setup-uv@")]
    assert setup_uv, "ci.yml must install uv via astral-sh/setup-uv"
    # Sonar S7637: third-party actions are pinned to a full commit SHA, with
    # the human-readable tag kept as a trailing comment in the workflow.
    ref = setup_uv[0].split("@", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{40}", ref), (
        f"setup-uv must be pinned to a full commit SHA, got {ref!r}"
    )

    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps") or []
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    ]
    assert steps, "ci.yml must configure the setup-uv step"
    enable_cache = steps[0].get("with", {}).get("enable-cache")
    assert enable_cache is True, (
        "the uv cache must be enabled explicitly so `uv sync --frozen` does "
        "not re-fetch the environment on every run"
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


def test_agent_workflows_skip_drafts_and_keep_free_model_defaults() -> None:
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

    # Per-stage default models were considered and deferred to a separate
    # change; until then both workflows must keep a quoted free-tier
    # default, which needs no API key (only an `nvidia/...` stage variable
    # brings ACTIONS_NVIDIA_API_KEY, gated in the probe and agent steps).
    for label, expr in (
        ("REVIEW_MODEL", review["env"]["MODEL"]),
        ("MUTATION_MODEL", mutation["env"]["MODEL"]),
    ):
        match = re.search(r"\|\|\s*'([^']+)'", expr)
        assert match, f"{label} must keep a quoted free-model default: {expr!r}"
        default = match.group(1)
        assert default.endswith("-free"), (
            f"{label} default {default!r} is not a free-tier model; the "
            "workflows hold no API key"
        )


AGENT_WORKFLOWS = (
    "agent-code-review.yml",
    "agent-mutation-testing.yml",
    "agent-pre-mortem.yml",
)


def test_agent_workflow_triggers_and_concurrency() -> None:
    """The agent workflows fire on the pushes that change the diff under
    review - opened, synchronize, reopened - and on a draft's
    ready_for_review. `edited` is the trap: it fires for title, body and
    label edits too, and re-analysing for those spends model quota on a
    verdict that is already current. Review and mutation must not subscribe
    to it at all; the pre-mortem keeps it only because a base retarget
    changes base...head with no other event, and its concurrency group
    gives a non-base edit a bucket of its own so it cannot cancel the run
    it would replace."""
    for name in AGENT_WORKFLOWS:
        workflow = _workflow(name)
        on = _on_block(workflow)
        types = set(on["pull_request"]["types"])
        assert {"opened", "synchronize", "reopened"} <= types, (
            f"{name}: a push that changes the diff must re-run the agent"
        )
        group = str(workflow["concurrency"]["group"])
        assert "pull_request.number" in group or "inputs.pr_number" in group, (
            f"{name}: the concurrency group is not per PR - pushes on "
            "different PRs would cancel each other"
        )

    for name in ("agent-code-review.yml", "agent-mutation-testing.yml"):
        types = set(_on_block(_workflow(name))["pull_request"]["types"])
        assert "edited" not in types, (
            f"{name}: `edited` fires for title/body/label edits and would "
            "spend quota re-analysing an unchanged diff"
        )

    premortem = _workflow("agent-pre-mortem.yml")
    types = set(_on_block(premortem)["pull_request"]["types"])
    assert "edited" in types, (
        "the pre-mortem needs `edited` for the base retarget, the one edit "
        "that changes the diff without any other event"
    )
    group = str(premortem["concurrency"]["group"])
    assert "github.event.action == 'edited'" in group and "changes.base" in group, (
        "a pre-mortem `edited` that changes no base must land in a bucket "
        "of its own: joining the analysed run's group would cancel it with "
        "no replacement and leave the head `pending`"
    )


def test_agent_workflow_status_context_is_stable() -> None:
    """The pre-mortem reports through a commit status, and branch protection
    or a human reads that context by name: renaming it silently detaches the
    verdict from the check it gates."""
    env = _workflow("agent-pre-mortem.yml")["env"]
    assert env.get("PREMORTEM_CONTEXT") == "agent/pre-mortem", (
        f"the pre-mortem status context changed: {env.get('PREMORTEM_CONTEXT')!r}"
    )


def test_agent_workflow_actions_are_sha_pinned() -> None:
    """Sonar S7637 and the drift the consolidation removed both say the same
    thing: a `uses:` that names a tag can be re-pointed upstream without a
    diff here. Every third-party action in the agent workflows - including
    the shared quota probe - must be a full commit SHA with the tag kept as
    a comment; local `./` reusable calls are exempt."""
    for name in (*AGENT_WORKFLOWS, "agent-quota-probe.yml"):
        for used in _uses_steps(_workflow(name)):
            if used.startswith("./"):
                continue
            ref = used.split("@", 1)[1]
            assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                f"{name}: {used} is not pinned to a full commit SHA"
            )


def test_secret_scan_covers_only_the_change_range() -> None:
    workflow = _workflow("secret-scan.yml")
    on = _on_block(workflow)
    assert "pull_request" in on, "the scan must gate every PR"
    assert on["push"]["branches"] == ["main"], "pushes are gated on main only"

    commands = _run_commands(workflow)
    scans = [c for c in commands if "gitleaks git" in c]
    # One scan step derives the range from the event: a trigger added later
    # without a range hits the `*)` branch and fails instead of scanning
    # nothing while reporting green.
    assert len(scans) == 1, scans
    scan = scans[0]
    # --redact keeps a finding's secret out of the public log, and an
    # explicit --log-opts range keeps the scan off the whole history.
    assert "--redact" in scan, scan
    assert "--log-opts" in scan, scan
    # The allowlist must be passed explicitly, not left to auto-discovery.
    assert "--config .gitleaks.toml" in scan, scan
    assert "pull_request" in scan and "push" in scan, scan
    # Fail closed where the range cannot be determined: a push with no
    # 'before' SHA and any event the case does not name.
    assert scan.count("exit 1") >= 2, scan
    assert not any("gitleaks dir" in c for c in commands)

    # The range commits must exist locally: a shallow checkout would make
    # base..head unresolvable.
    checkouts = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps") or []
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkouts and checkouts[0]["with"]["fetch-depth"] == 0

    # The binary is pinned by version AND checksum, like the other pinned
    # tooling in this repo.
    install = [c for c in commands if "gitleaks" in c and "curl" in c]
    assert install and "sha256sum -c" in install[0]


def test_gitleaks_allowlist_stays_path_and_value_scoped() -> None:
    config = tomllib.loads(
        (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    )
    allowlist = config["allowlist"]
    # AND is load-bearing: with the default OR, a matching path or a matching
    # dummy regex alone would suppress real findings. regexTarget="match" is
    # required because generic-api-key captures a substring secret that loses
    # the fixture's distinguishing prefix.
    assert str(allowlist["condition"]).upper() == "AND"
    assert allowlist["regexTarget"] == "match"
    assert allowlist["paths"] and allowlist["regexes"]
    # Anchored paths: an unanchored pattern would also suppress e.g.
    # `foo/tests/x.py` or `tests/x.py.bak`. Values stay specific too - no
    # bare wildcard that would allowlist every secret in those files.
    assert all(p.startswith("^") and p.endswith("$") for p in allowlist["paths"])
    assert all("*" not in p for p in allowlist["paths"]), (
        "path entries name exact files; a wildcard would widen the allowlist"
    )
    assert all("tests/" in p for p in allowlist["paths"]), (
        "the allowlist may only name test fixtures"
    )
    # Value entries are the exact dummy literals the fixtures use: an open
    # character class or quantifier (e.g. `sk-live-[0-9a-f]+`) would also
    # suppress a real key sharing the prefix.
    assert all(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~/-]*", r)
        for r in allowlist["regexes"]
    ), "value patterns must be exact literals, not regexes"
