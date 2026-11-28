"""Unit tests for scripts/agents/post_comment.py.

The marker-comment post/patch logic used to be three hand-copied ``gh api``
shell blocks; only the pre-mortem copy had the guards. The Python version is
the single implementation for all three stages - plain REST over urllib, no
subprocess - and these tests pin the guards against a local HTTP stub of the
GitHub API (``GITHUB_API_URL`` points the script at it):

- a "not run: quota" body never overwrites an earlier real verdict for the
  same head (the bug this task fixes),
- a quota body still overwrites an older quota comment or a verdict for a
  different head,
- a PR head that moved - before the write, or between the comment-list
  fetch and the write - stops the post entirely,
- every comment carrying the marker is updated, not just the newest.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from tests.helpers.github_api_stub import GitHubAPIStub

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "scripts" / "agents"


def _load(name: str, filename: str):
    # post_comment imports parse_verdict from its own directory.
    if str(AGENTS_DIR) not in sys.path:
        sys.path.insert(0, str(AGENTS_DIR))
    spec = importlib.util.spec_from_file_location(name, AGENTS_DIR / filename)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


post_comment = _load("post_comment", "post_comment.py")

HEAD = "b" * 40
OTHER_HEAD = "c" * 40
MARKER = "<!-- actions-pre-mortem -->"


def _setup(
    tmp_path: Path,
    heads: list[str],
    ids: list[str] | None = None,
    bodies: dict[str, str] | None = None,
) -> tuple[dict, Path, GitHubAPIStub]:
    work = tmp_path / "work"
    work.mkdir()
    (work / "comment.md").write_text(
        "**Summary:** the body\n**Verdict: PASS**\n", encoding="utf-8"
    )
    comments = [
        {
            "id": int(cid),
            "user": {"login": "github-actions[bot]"},
            "body": (bodies or {}).get(cid, f"{MARKER}\n**Summary:** prior\n"),
        }
        for cid in (ids or [])
    ]
    api = GitHubAPIStub(heads=heads, comments=comments)
    env = {
        **os.environ,
        "GH_TOKEN": "test-token",
        "GITHUB_API_URL": api.url,
        "GITHUB_REPOSITORY": "itsjustFred/trading-platform",
        "PR_NUMBER": "409",
        "HEAD_SHA": HEAD,
        "QUOTA_SKIP": "false",
    }
    return env, work, api


def _run(env: dict, work: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(AGENTS_DIR / "post_comment.py"),
            "--stage", "premortem",
            "--marker", MARKER,
            *extra,
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# the quota-overwrite guard - the bug this task fixes
# ---------------------------------------------------------------------------


def test_a_quota_skip_does_not_overwrite_a_real_verdict_for_the_same_head(tmp_path) -> None:
    prior = (
        f"{MARKER}\n**Summary:** real\n**Verdict: BLOCK**\n"
        f"\n_Analysed base `{'a' * 8}` ... head `{HEAD[:8]}`._\n"
    )
    env, work, api = _setup(tmp_path, [HEAD, HEAD], ["42"], {"42": prior})
    env["QUOTA_SKIP"] = "true"
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    assert api.writes == [], "the quota body overwrote the verdict"
    assert "did not overwrite it" in result.stdout


def test_a_quota_skip_overwrites_a_verdict_that_names_no_head(tmp_path) -> None:
    """The guard needs both halves of comment_is_real_verdict: a body with a
    verdict line but no analysed-head footer is not provably about this
    commit, so the quota body may refresh it."""
    prior = f"{MARKER}\n**Summary:** real\n**Verdict: BLOCK**\n"
    env, work, api = _setup(tmp_path, [HEAD, HEAD], ["42"], {"42": prior})
    env["QUOTA_SKIP"] = "true"
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    assert len(api.writes) == 1


def test_a_quota_skip_still_overwrites_an_older_quota_comment(tmp_path) -> None:
    prior = (
        f"{MARKER}\n**Summary:** Not run: quota - earlier\n\n"
        f"_Analysed base `{'a' * 8}` ... head `{HEAD[:8]}`._\n"
    )
    env, work, api = _setup(tmp_path, [HEAD, HEAD], ["42"], {"42": prior})
    env["QUOTA_SKIP"] = "true"
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    writes = api.writes
    assert len(writes) == 1
    assert "PATCH" in writes[0]
    assert "comments/42" in writes[0]


def test_a_quota_skip_still_overwrites_a_verdict_for_another_head(tmp_path) -> None:
    prior = (
        f"{MARKER}\n**Summary:** real\n**Verdict: BLOCK**\n"
        f"\n_Analysed base `{'a' * 8}` ... head `{OTHER_HEAD[:8]}`._\n"
    )
    env, work, api = _setup(tmp_path, [HEAD, HEAD], ["42"], {"42": prior})
    env["QUOTA_SKIP"] = "true"
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    assert len(api.writes) == 1


def test_a_real_result_overwrites_an_earlier_verdict_for_the_same_head(tmp_path) -> None:
    prior = (
        f"{MARKER}\n**Summary:** old\n**Verdict: PASS**\n"
        f"_Analysed base `{'a' * 8}` ... head `{HEAD[:8]}`._\n"
    )
    env, work, api = _setup(tmp_path, [HEAD, HEAD], ["42"], {"42": prior})
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    writes = api.writes
    assert len(writes) == 1
    assert "PATCH" in writes[0]


# ---------------------------------------------------------------------------
# the head-moved guards
# ---------------------------------------------------------------------------


def test_a_moved_head_before_the_write_posts_nothing(tmp_path) -> None:
    env, work, api = _setup(tmp_path, [OTHER_HEAD, OTHER_HEAD], [])
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    assert api.writes == []
    assert "head moved" in result.stdout


def test_the_first_head_read_alone_stops_a_moved_head(tmp_path) -> None:
    """The stub serves OTHER_HEAD on the first pulls read and HEAD on every
    later one: the first guard alone must stop the post, so the second read
    never gets to 'rescue' it into a write."""
    env, work, api = _setup(tmp_path, [OTHER_HEAD, HEAD], ["42"])
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    assert api.writes == []
    assert "head moved" in result.stdout


def test_a_head_moving_during_the_post_still_posts_nothing(tmp_path) -> None:
    """First read agrees, second (just before the write) does not."""
    env, work, api = _setup(tmp_path, [HEAD, OTHER_HEAD], ["42"], {"42": "old"})
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    assert api.writes == []
    assert "while this comment was being posted" in result.stdout


# ---------------------------------------------------------------------------
# posting mechanics
# ---------------------------------------------------------------------------


def test_every_marker_comment_is_updated_not_just_the_newest(tmp_path) -> None:
    env, work, api = _setup(tmp_path, [HEAD, HEAD], ["7", "42"])
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    writes = api.writes
    assert len(writes) == 2
    assert any("comments/7" in w for w in writes)
    assert any("comments/42" in w for w in writes)
    assert "2 pre-mortem comments carry the marker" in result.stdout


def test_a_pr_with_no_marker_comment_gets_a_post(tmp_path) -> None:
    env, work, api = _setup(tmp_path, [HEAD, HEAD], [])
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    writes = api.writes
    assert len(writes) == 1
    assert "POST" in writes[0]
    assert "/issues/409/comments" in writes[0]


def test_the_marker_and_body_make_up_body_md(tmp_path) -> None:
    env, work, _api = _setup(tmp_path, [HEAD, HEAD], [])
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    body = (work / "body.md").read_text(encoding="utf-8")
    assert body.startswith(MARKER + "\n")
    assert "**Verdict: PASS**" in body


# ---------------------------------------------------------------------------
# input validation - every env/CLI value lands in a request path or body
# ---------------------------------------------------------------------------


def test_a_marker_that_is_not_a_single_line_html_comment_is_rejected(tmp_path) -> None:
    env, work, api = _setup(tmp_path, [HEAD, HEAD], [])
    result = _run(env, work, "--marker", '<!-- "bad" -->')
    assert result.returncode == 1
    assert api.writes == []


def test_a_repository_that_is_not_owner_slash_name_is_rejected(tmp_path) -> None:
    env, work, api = _setup(tmp_path, [HEAD, HEAD], [])
    env["GITHUB_REPOSITORY"] = "not-a-repo"
    result = _run(env, work)
    assert result.returncode == 1
    assert api.writes == []


def test_a_non_numeric_pr_number_is_rejected(tmp_path) -> None:
    env, work, api = _setup(tmp_path, [HEAD, HEAD], [])
    env["PR_NUMBER"] = "abc"
    result = _run(env, work)
    assert result.returncode == 1
    assert api.writes == []
