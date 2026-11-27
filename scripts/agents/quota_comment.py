#!/usr/bin/env python3
"""The "not run: quota" PR-comment body, in the one place all three agent
workflows read it from.

A quota skip reaches the PR comment from two places: the probe-skip path,
where the post job writes the body itself (``--when probe``), and the
mid-run path, where the agent step writes the same report after the cap hit
during the run (``--when midrun``). The bodies differ per stage - title,
wording, footer - and used to be three hand-copied printf blocks per path.
This module is the single, unit-tested generator; the workflows call it and
post whatever it prints.

Environment: ``BASE_SHA``, ``HEAD_SHA`` (8-char prefixes are used), ``SKILL_PATH``
(for the skill directory name in the footer) and ``MODEL``/``VARIANT`` (the
pre-mortem mid-run footer names the model, not the skill).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SUMMARY = (
    "**Summary:** Not run: quota - the free model was capped, rate-limited, "
    "refused or unavailable {when_clause}; {produced} for this commit.\n\n"
)

# stage -> per-(when) body parts. `title` is None for the untitled mid-run
# mutation body (its Finish step titles every body the same way). `footer`
# is a printf-style template taking either the skill directory name or the
# model-with-variant string, per `footer_arg`.
_STAGES = {
    "review": {
        "title": "# Code Review Action",
        "produced": "no review was produced",
        "noun": "review",
        "probe": {
            "when_clause": "before the agent could start",
            "rerun": "Re-run the workflow after the quota resets.",
            "footer": "\n---\n_Automated review not run: quota (%s skill)_\n",
            "footer_arg": "skill",
        },
        "midrun": {
            "when_clause": "while the agent ran",
            "rerun": "Re-run the job after the quota resets.",
            "footer": "\n---\n_Automated review not run: quota (%s skill)_\n",
            "footer_arg": "skill",
        },
    },
    "mutation": {
        "title": "# Mutation Testing Action",
        "produced": "no mutation testing was produced",
        "noun": "mutation-testing",
        "probe": {
            "when_clause": "before the agent could start",
            "rerun": "Re-run the workflow after the quota resets.",
            "footer": "\n---\n_Automated mutation testing not run: quota (`%s` skill)_\n",
            "footer_arg": "skill",
        },
        "midrun": {
            # Untitled and footerless: the Finish comment step titles it and
            # appends the model footer like any other mutation result.
            "when_clause": "while the agent ran",
            "rerun": "Re-run the job after the quota resets.",
            "footer": None,
            "footer_arg": None,
        },
    },
    "premortem": {
        "title": "# Pre-Mortem Action",
        "produced": "no pre-mortem was produced",
        "noun": "pre-mortem",
        "probe": {
            "when_clause": "before the agent could start",
            "rerun": "Re-run the workflow after the quota resets.",
            "footer": "\n---\n_Automated pre-mortem not run: quota (%s skill)_\n",
            "footer_arg": "skill",
        },
        "midrun": {
            "when_clause": "while the agent ran",
            "rerun": "Re-run the job after the quota resets.",
            # The mid-run body names the model that was capped, not the skill:
            # the agent job already ran, so the rules source is beside the point.
            "footer": "\n---\n_Automated pre-mortem not run: quota (%s)_\n",
            "footer_arg": "model",
        },
    },
}


def build_body(stage: str, when: str, env: dict[str, str]) -> str:
    """The comment body for ``stage`` on the ``probe`` or ``midrun`` path."""
    try:
        spec = _STAGES[stage][when]
    except KeyError:
        raise ValueError(f"unknown stage/when {stage!r}/{when!r}") from None
    base_sha = env.get("BASE_SHA", "")
    head_sha = env.get("HEAD_SHA", "")
    parts = []
    title = _STAGES[stage]["title"]
    # The mid-run mutation body is untitled; every other body opens with its
    # action title so the comment is recognisable among the other bots'.
    if not (when == "midrun" and stage == "mutation"):
        parts.append(f"{title}\n\n")
    parts.append(_SUMMARY.format(when_clause=spec["when_clause"], produced=_STAGES[stage]["produced"]))
    parts.append(spec["rerun"] + "\n")
    parts.append(f"\n_Analysed base `{base_sha[:8]}` ... head `{head_sha[:8]}`._\n")
    if spec["footer"] is not None:
        if spec["footer_arg"] == "skill":
            skill_path = env.get("SKILL_PATH", "")
            arg = Path(skill_path).parent.name
        else:
            model = env.get("MODEL", "")
            variant = env.get("VARIANT", "")
            arg = f"{model.split('/', 1)[-1]}{f' ({variant})' if variant else ''}"
        parts.append(spec["footer"] % arg)
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True, choices=sorted(_STAGES))
    parser.add_argument("--when", required=True, choices=("probe", "midrun"))
    args = parser.parse_args(argv)
    try:
        body = build_body(args.stage, args.when, dict(os.environ))
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
