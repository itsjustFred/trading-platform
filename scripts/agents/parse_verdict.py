#!/usr/bin/env python3
"""The one verdict parser the three agent CI workflows share.

The agent workflows post a PR comment that must end with a verdict line, and
the pre-mortem also maps that verdict onto a commit status. The verdict used
to be read by a per-workflow shell regex; one of the copies accepted
``Verdict: PASSed`` as PASS. This module is the single, unit-tested reader:

- ``verdict`` extracts the final ``**Summary:**`` report from the agent's raw
  stream, requires the verdict to be the last non-empty line (a trailing
  "Human sign-off required." note is tolerated, everything else invalidates
  it), and accepts exactly the verdict tokens the stage's skill documents -
  an exact-token match, so ``PASSed`` and ``BLOCKED`` are refused.
- ``check-head`` verifies that a posted comment names the commit it was
  computed for, so a stale ``head_sha`` cannot pass for the current one.

Stdlib only: the workflows call it with the runner's system ``python3``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# stage -> (temp dir name, allowed verdict tokens). The tokens are the ones
# each skill's "Verdicts:" line documents; tests/test_agent_verdict_parser.py
# pins them against the SKILL.md files so the two cannot drift.
STAGES = {
    "review": ("review", frozenset({"BLOCK", "CHANGES-REQUESTED", "PASS", "INCOMPLETE"})),
    "mutation": ("mutation", frozenset({"PASS", "GAPS", "INCOMPLETE"})),
    "premortem": ("premortem", frozenset({"BLOCK", "CHANGES-REQUESTED", "PASS", "INCOMPLETE"})),
}

# A line that carries only the sign-off note the skills ask for on its own
# line after the verdict (the pre-mortem tolerates it; harmless for the rest).
_SIGNOFF_ONLY = re.compile(r"^[\s*_]*Human sign-off required\.[\s*_]*$")

# `Verdict: TOKEN` after the bold markers are removed (the shell original ran
# `tr -d '*'` first), where TOKEN must be followed by end-of-line or a
# character that cannot extend it into a different word: not a letter, an
# underscore or a hyphen. `PASSed`, `BLOCKED` and `INCOMPLETELY` therefore do
# not match, while `Verdict: PASS — reason` and `**Verdict: PASS**` do.
def _verdict_pattern(verdicts: frozenset[str]) -> re.Pattern[str]:
    tokens = "|".join(sorted(verdicts, key=len, reverse=True))
    return re.compile(rf"^Verdict:\s*({tokens})(?![A-Za-z_-])(.*)$")


def extract_report(raw: str) -> str:
    """Keep only the final comment: the tail starting at the last column-0
    ``**Summary:**`` line, minus a closing code fence the model may have
    wrapped around it."""
    lines = raw.splitlines()
    start = -1
    for index, line in enumerate(lines):
        if line.startswith("**Summary:"):
            start = index
    if start < 0:
        return ""
    tail = lines[start:]
    if tail and tail[-1].strip() == "```":
        tail = tail[:-1]
    return "\n".join(tail) + "\n"


def parse_verdict(report: str, stage: str) -> str | None:
    """The verdict token of ``report``, or None when it does not end with a
    valid verdict line for ``stage``."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {sorted(STAGES)}")
    verdicts = STAGES[stage][1]
    pattern = _verdict_pattern(verdicts)
    # The verdict must be the last non-empty line; a sign-off-only line after
    # it is skipped, and anything else means the model kept talking.
    candidates = [
        line
        for line in report.splitlines()
        if line.strip() and not _SIGNOFF_ONLY.match(line)
    ]
    if not candidates:
        return None
    # The shell original ran `tr -d '*'` on the line first, so the bold
    # markers around `**Verdict:` and the token do not reach the match.
    match = pattern.match(candidates[-1].replace("*", "").strip())
    return match.group(1) if match else None


_HEAD_REF = re.compile(r"head[`'\s]*`?([0-9a-f]{7,40})`?", re.IGNORECASE)


def comment_names_head(text: str, head_sha: str) -> bool:
    """True when ``text`` carries an analysed-head reference for ``head_sha``.

    The agent comments end with ``_Analysed base `xxxxxxxx` ... head
    `yyyyyyyy`._`` using the 8-character prefix; a full SHA also matches. A
    comment that names no head at all does not match - a quota or verdict
    body without the pair cannot be proven to be about this commit.
    """
    head_sha = head_sha.lower()
    for found in _HEAD_REF.findall(text.lower()):
        if head_sha.startswith(found) or found.startswith(head_sha):
            return True
    return False


def _fail(message: str) -> int:
    print(f"::error::{message}", file=sys.stderr)
    return 1


def _cmd_verdict(args: argparse.Namespace) -> int:
    raw = Path(args.file).read_text(encoding="utf-8", errors="replace")
    report = extract_report(raw)
    if not report:
        return _fail("agent output has no '**Summary:' report line; not posting.")
    verdict = parse_verdict(report, args.stage)
    if verdict is None:
        return _fail(
            f"agent output does not end with a valid verdict line "
            f"({', '.join(sorted(STAGES[args.stage][1]))}); not posting."
        )
    print(verdict)
    return 0


def _cmd_check_head(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    if not comment_names_head(text, args.expect_head):
        return _fail(
            f"the comment does not name the analysed head {args.expect_head[:8]}; "
            "refusing to treat it as a result for this commit."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    verdict_cmd = sub.add_parser("verdict", help="print the report's verdict token")
    verdict_cmd.add_argument("--stage", required=True, choices=sorted(STAGES))
    verdict_cmd.add_argument("file", help="the extracted report (or raw agent output)")
    verdict_cmd.set_defaults(func=_cmd_verdict)
    head_cmd = sub.add_parser(
        "check-head", help="verify the comment names the analysed head"
    )
    head_cmd.add_argument("--expect-head", required=True)
    head_cmd.add_argument("file")
    head_cmd.set_defaults(func=_cmd_check_head)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
