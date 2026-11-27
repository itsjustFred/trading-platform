#!/usr/bin/env python3
"""Post or update the one marker comment an agent workflow keeps per PR.

This is the shared, unit-tested version of what used to be three hand-copied
``gh api`` shell blocks in the post jobs. It keeps every guard the
pre-mortem copy grew, now for all three stages:

- the PR head is read before the body is assembled and again immediately
  before the write; a head that moved means this result describes a diff
  nobody is looking at, so nothing is posted (the comment footer names the
  base...head it was computed from either way),
- a "not run: quota" body (``QUOTA_SKIP=true``) never overwrites an earlier
  real verdict for the same head: the existing comment is left alone. A
  quota body over an older quota comment, or over a verdict for a different
  head, still writes,
- every comment carrying the marker is updated with the same result, not
  just the newest - a duplicate left by an interrupted run would otherwise
  stay readable forever.

Environment: ``GH_TOKEN`` (passed through to ``gh``), ``GITHUB_REPOSITORY``,
``PR_NUMBER``, ``HEAD_SHA`` and ``QUOTA_SKIP`` (``true`` when the body being
posted is a quota report). ``comment.md`` in the working directory is the
body; ``body.md`` is the marker plus body that actually gets posted.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# `parse_verdict` sits next to this file; it is on sys.path both when the
# workflow runs `python3 scripts/agents/post_comment.py` (the script's own
# directory is sys.path[0]) and when the tests load this file by path.
from parse_verdict import comment_names_head

# The stage only names the comment kind in notices; the logic is identical.
_NOUN = {"review": "review", "mutation": "mutation-testing", "premortem": "pre-mortem"}


def _notice(message: str) -> None:
    print(f"::notice::{message}")


def _warning(message: str) -> None:
    print(f"::warning::{message}")


def _gh(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        print(f"::error::gh api {' '.join(args[:3])} failed: {result.stderr.strip()}", file=sys.stderr)
        raise SystemExit(1)
    return result


def _head_sha(repo: str, pr: str) -> str:
    return _gh(f"repos/{repo}/pulls/{pr}", "--jq", ".head.sha").stdout.strip()


def _comment_ids(repo: str, pr: str, marker: str) -> list[str]:
    jq = (
        '.[] | select(.user.login == "github-actions[bot]" '
        f'and (.body | startswith("{marker}"))) | .id'
    )
    out = _gh("--paginate", f"repos/{repo}/issues/{pr}/comments", "--jq", jq).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _comment_body(repo: str, comment_id: str) -> str:
    return _gh(f"repos/{repo}/issues/comments/{comment_id}", "--jq", ".body").stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True, choices=sorted(_NOUN))
    parser.add_argument("--marker", required=True)
    parser.add_argument("--body-file", default="comment.md")
    args = parser.parse_args(argv)
    noun = _NOUN[args.stage]

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr = os.environ.get("PR_NUMBER", "")
    head = os.environ.get("HEAD_SHA", "")
    quota_skip = os.environ.get("QUOTA_SKIP", "") == "true"
    if not (repo and pr and head):
        print("::error::GITHUB_REPOSITORY, PR_NUMBER and HEAD_SHA are required.", file=sys.stderr)
        return 1

    body_path = Path(args.body_file)
    body_text = body_path.read_text(encoding="utf-8") if body_path.exists() else ""

    current = _head_sha(repo, pr)
    if current != head:
        _notice(f"PR head moved to {current}; not updating the comment for {head}.")
        return 0

    full_body = args.marker + "\n" + body_text
    Path("body.md").write_text(full_body, encoding="utf-8")

    existing = _comment_ids(repo, pr, args.marker)

    # The head is read again with the body in hand, immediately before the
    # write. The guard above ran before the body was assembled and before the
    # comment-list fetch, and a synchronize event landing inside that window
    # would still have been written over: a comment describing a diff nobody
    # is looking at any more, presented as the current result. Reading it here
    # narrows the window to the write itself, which no API closes - there is
    # no conditional comment update - and the comment's footer names the
    # base...head it was computed from, so a result that still slips through
    # is readable rather than silently current.
    moved = _head_sha(repo, pr)
    if moved != head:
        _notice(
            f"PR head moved to {moved} while this comment was being posted; "
            f"not updating the comment for {head}."
        )
        return 0

    # A quota skip must not overwrite a real verdict for this same head: a
    # manual re-run of an unchanged head after the cap resets is exactly the
    # case where the "not run: quota" body would replace a verdict that is
    # still current. A quota body over an older quota comment, or any real
    # verdict for another head, still writes.
    if quota_skip and existing:
        for comment_id in existing:
            prior = _comment_body(repo, comment_id)
            if "Verdict:" in prior and comment_names_head(prior, head):
                _notice(
                    f"The existing {noun} comment is a real verdict for {head}; "
                    "the quota skip did not overwrite it."
                )
                return 0

    if existing:
        # More than one comment can carry the marker (a duplicate from an
        # interrupted run, or one created outside the workflow). Patching only
        # the newest would leave the stale one readable forever, so say it and
        # update every match with the same current result.
        if len(existing) > 1:
            _warning(
                f"{len(existing)} {noun} comments carry the marker; "
                "updating all of them with the current result."
            )
        for comment_id in existing:
            _gh(
                "--method", "PATCH",
                f"repos/{repo}/issues/comments/{comment_id}",
                "-F", "body=@body.md",
            )
    else:
        _gh(
            "--method", "POST",
            f"repos/{repo}/issues/{pr}/comments",
            "-F", "body=@body.md",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
