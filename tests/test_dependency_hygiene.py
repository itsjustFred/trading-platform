"""Contract tests for the dependency-hygiene change (#298).

Each assertion pins a property whose regression would be invisible in a
green build: an unbounded ``requires-python`` would let a 3.13 interpreter
install an environment the pinned ``ThreadPoolExecutor`` mirror in
``viewer/main.py`` rejects at runtime; a Dependabot config without grouping
would open one PR per dependency instead of the agreed weekly batch; and an
audit job without a severity threshold would fail on every low-severity
finding until nobody reads it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_requires_python_is_capped_below_313() -> None:
    spec = _pyproject()["project"]["requires-python"]
    assert ">=3.12" in spec, spec
    assert "<3.13" in spec, (
        "requires-python must stay capped below 3.13: 3.13 is not a tested "
        "interpreter (CI matrix, mypy python_version and ruff target are all "
        "3.12) and viewer/main.py's ThreadPoolExecutor mirror validates "
        "sys.version_info == (3, 12) exactly"
    )


def test_uv_lock_requires_python_matches_pyproject() -> None:
    from packaging.specifiers import SpecifierSet

    spec = SpecifierSet(_pyproject()["project"]["requires-python"])
    lock_head = (ROOT / "uv.lock").read_text(encoding="utf-8")[:400]
    match = re.search(r'^requires-python = "([^"]+)"', lock_head, re.MULTILINE)
    assert match, "uv.lock must declare requires-python"
    lock_spec = SpecifierSet(match.group(1))
    # uv may normalize the specifier text (>=3.12,<3.13 -> ==3.12.*); what
    # must hold is that both admit and reject the same interpreters.
    for version in ("3.11.9", "3.12.0", "3.13.0", "3.14.0"):
        assert spec.contains(version) == lock_spec.contains(version), (
            f"uv.lock requires-python {match.group(1)!r} admits a different "
            f"interpreter set than pyproject on {version}: run `uv lock`"
        )


def test_dependabot_is_weekly_grouped_for_actions_and_uv() -> None:
    path = ROOT / ".github" / "dependabot.yml"
    assert path.is_file(), ".github/dependabot.yml must exist"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    updates = config.get("updates") or []
    ecosystems = {u.get("package-ecosystem") for u in updates}
    assert ecosystems == {"github-actions", "uv"}, ecosystems
    for update in updates:
        assert update.get("directory") == "/", update
        schedule = update.get("schedule") or {}
        assert schedule.get("interval") == "weekly", (
            f"{update.get('package-ecosystem')}: updates must stay weekly"
        )
        groups = update.get("groups") or {}
        assert groups, (
            f"{update.get('package-ecosystem')}: updates must be grouped so "
            "each run opens one PR per ecosystem, not one per dependency"
        )
        for name, group in groups.items():
            patterns = group.get("patterns") or []
            assert "*" in patterns, (
                f"{update.get('package-ecosystem')}/{name}: the group must "
                "cover every dependency or updates fall outside it"
            )


def test_security_audit_workflow_is_weekly_and_fails_on_high() -> None:
    path = WORKFLOWS / "security-audit.yml"
    assert path.is_file(), ".github/workflows/security-audit.yml must exist"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))

    on_block = workflow.get("on", workflow.get(True)) or {}
    schedule = on_block.get("schedule") or []
    assert schedule, "the audit must run on a schedule"
    assert all("cron" in entry for entry in schedule), schedule

    commands = [
        " ".join(step["run"].split())
        for job in workflow["jobs"].values()
        for step in job.get("steps") or []
        if isinstance(step.get("run"), str)
    ]
    assert any("pip-audit" in c for c in commands), (
        "the workflow must run pip-audit against the locked requirements"
    )
    assert any("pip_audit_severity_gate.py" in c for c in commands), (
        "pip-audit findings must pass through the severity gate: bare "
        "pip-audit fails on any vulnerability, not just high severity"
    )
    assert any("npm audit" in c and "--audit-level=high" in c for c in commands), (
        "the frontend audit must fail only on high/critical findings"
    )

    uses = [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job.get("steps") or []
        if "uses" in step
    ]
    for ref in uses:
        if ref.startswith("astral-sh/setup-uv@"):
            sha = ref.split("@", 1)[1]
            assert re.fullmatch(r"[0-9a-f]{40}", sha), (
                f"setup-uv must stay SHA-pinned like ci.yml, got {ref!r}"
            )
