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

The GitHub calls are plain REST over ``urllib`` - no subprocess, so no
command line exists for untrusted values to reach. ``GITHUB_API_URL``
overrides the API base (GitHub Enterprise); the tests point it at a local
stub. Environment: ``GH_TOKEN``, ``GITHUB_REPOSITORY``, ``PR_NUMBER``,
``HEAD_SHA`` and ``QUOTA_SKIP`` (``true`` when the body being posted is a
quota report). ``comment.md`` in the working directory is the body;
``body.md`` is the marker plus body that actually gets posted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# `parse_verdict` sits next to this file; it is on sys.path both when the
# workflow runs `python3 scripts/agents/post_comment.py` (the script's own
# directory is sys.path[0]) and when the tests load this file by path.
from parse_verdict import comment_names_head

# The stage only names the comment kind in notices; the logic is identical.
_NOUN = {"review": "review", "mutation": "mutation-testing", "premortem": "pre-mortem"}

_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PAGE = 100


def _notice(message: str) -> None:
    print(f"::notice::{message}")


def _warning(message: str) -> None:
    print(f"::warning::{message}")


def _request(method: str, path: str, payload: dict | None = None):
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("::error::GH_TOKEN is required.", file=sys.stderr)
        raise SystemExit(1)
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base}/{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "trading-platform-agent-comment",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        print(
            f"::error::GitHub API {method} {path} failed: HTTP {exc.code} {detail}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    except urllib.error.URLError as exc:
        print(f"::error::GitHub API {method} {path} failed: {exc.reason}", file=sys.stderr)
        raise SystemExit(1) from exc


def _head_sha(repo: str, pr: str) -> str:
    return str(_request("GET", f"repos/{repo}/pulls/{pr}")["head"]["sha"])


def _marker_comments(repo: str, pr: str, marker: str) -> list[dict[str, str]]:
    """Every comment by the actions bot whose body starts with the marker."""
    found: list[dict[str, str]] = []
    page = 1
    while True:
        comments = _request(
            "GET", f"repos/{repo}/issues/{pr}/comments?per_page={_PAGE}&page={page}"
        )
        for comment in comments:
            body = str(comment.get("body") or "")
            if (
                comment.get("user", {}).get("login") == "github-actions[bot]"
                and body.startswith(marker)
            ):
                found.append({"id": str(comment["id"]), "body": body})
        if len(comments) < _PAGE:
            return found
        page += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True, choices=sorted(_NOUN))
    parser.add_argument("--marker", required=True)
    args = parser.parse_args(argv)
    noun = _NOUN[args.stage]

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr = os.environ.get("PR_NUMBER", "")
    head = os.environ.get("HEAD_SHA", "")
    quota_skip = os.environ.get("QUOTA_SKIP", "") == "true"
    # Every value below lands in a request path or the posted body: anything
    # that is not the exact shape the workflow produces is refused first.
    if not _REPO_RE.fullmatch(repo):
        print("::error::GITHUB_REPOSITORY is not owner/name.", file=sys.stderr)
        return 1
    if not pr.isdigit():
        print("::error::PR_NUMBER is not a number.", file=sys.stderr)
        return 1
    if not head:
        print("::error::HEAD_SHA is required.", file=sys.stderr)
        return 1
    if '"' in args.marker or "\n" in args.marker:
        print("::error::the marker must be a single-line HTML comment.", file=sys.stderr)
        return 1

    body_path = Path("comment.md")
    body_text = body_path.read_text(encoding="utf-8") if body_path.exists() else ""

    current = _head_sha(repo, pr)
    if current != head:
        _notice(f"PR head moved to {current}; not updating the comment for {head}.")
        return 0

    full_body = args.marker + "\n" + body_text
    Path("body.md").write_text(full_body, encoding="utf-8")

    existing = _marker_comments(repo, pr, args.marker)

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
    if quota_skip:
        for comment in existing:
            prior = comment["body"]
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
        for comment in existing:
            _request(
                "PATCH",
                f"repos/{repo}/issues/comments/{comment['id']}",
                {"body": full_body},
            )
    else:
        _request(
            "POST",
            f"repos/{repo}/issues/{pr}/comments",
            {"body": full_body},
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
