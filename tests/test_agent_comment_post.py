"""Unit tests for scripts/agents/post_comment.py.

The marker-comment post/patch logic used to be three hand-copied ``gh api``
shell blocks; only the pre-mortem copy had the guards. The Python version is
the single implementation for all three stages, and these tests pin the
guards with a stub ``gh`` on PATH:

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
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "scripts" / "agents"


def _load(name: str, filename: str):
    # post_comment imports parse_verdict from its own directory.
    if str(AGENTS_DIR) not in sys.path:
        sys.path.insert(0, str(AGENTS_DIR))
    spec = importlib.util.spec_from_file_location(name, AGENTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


post_comment = _load("post_comment", "post_comment.py")

HEAD = "b" * 40
OTHER_HEAD = "c" * 40
MARKER = "<!-- actions-pre-mortem -->"

_STUB_GH = """#!/usr/bin/env bash
# Stub `gh api`: serves the pulls head (one value per call, in order),
# comment ids, comment bodies, and records every call.
echo "$@" >> "$STUB_DIR/calls"
args="$*"
if [[ "$args" == *"--method"* ]]; then
  exit 0
fi
if [[ "$args" == *"/pulls/"* ]]; then
  n=$(cat "$STUB_DIR/pull-count" 2>/dev/null || echo 0)
  n=$((n + 1))
  echo "$n" > "$STUB_DIR/pull-count"
  sed -n "${n}p" "$STUB_DIR/heads"
  exit 0
fi
if [[ "$args" == *"/issues/comments/"* ]]; then
  id="${args##*/issues/comments/}"
  id="${id%% *}"
  cat "$STUB_DIR/body-$id" 2>/dev/null
  exit 0
fi
if [[ "$args" == *"/issues/"*"/comments"* ]]; then
  cat "$STUB_DIR/ids" 2>/dev/null
  exit 0
fi
exit 0
"""


def _setup(tmp_path: Path, heads: list[str], ids: list[str] | None = None,
           bodies: dict[str, str] | None = None) -> tuple[dict, Path]:
    stub_dir = tmp_path / "stub"
    bin_dir = tmp_path / "bin"
    stub_dir.mkdir()
    bin_dir.mkdir()
    (stub_dir / "heads").write_text("\n".join(heads) + "\n", encoding="utf-8")
    if ids is not None:
        (stub_dir / "ids").write_text("\n".join(ids) + ("\n" if ids else ""), encoding="utf-8")
    for cid, body in (bodies or {}).items():
        (stub_dir / f"body-{cid}").write_text(body, encoding="utf-8")
    gh = bin_dir / "gh"
    gh.write_text(_STUB_GH, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    work = tmp_path / "work"
    work.mkdir()
    (work / "comment.md").write_text(
        "**Summary:** the body\n**Verdict: PASS**\n", encoding="utf-8"
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_DIR": str(stub_dir),
        "GH_TOKEN": "test-token",
        "GITHUB_REPOSITORY": "itsjustFred/trading-platform",
        "PR_NUMBER": "409",
        "HEAD_SHA": HEAD,
        "QUOTA_SKIP": "false",
    }
    return env, work


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


def _calls(env: dict, work: Path) -> str:
    stub_dir = Path(env["STUB_DIR"])
    log = stub_dir / "calls"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def _method_calls(env: dict, work: Path) -> list[str]:
    return [
        line for line in _calls(env, work).splitlines()
        if "--method" in line
    ]


# ---------------------------------------------------------------------------
# the quota-overwrite guard - the bug this task fixes
# ---------------------------------------------------------------------------


def test_a_quota_skip_does_not_overwrite_a_real_verdict_for_the_same_head(tmp_path) -> None:
    prior = (
        f"{MARKER}\n**Summary:** real\n**Verdict: BLOCK**\n"
        f"\n_Analysed base `{'a' * 8}` ... head `{HEAD[:8]}`._\n"
    )
    env, work = _setup(tmp_path, [HEAD, HEAD], ["42"], {"42": prior})
    env["QUOTA_SKIP"] = "true"
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    assert _method_calls(env, work) == [], "the quota body overwrote the verdict"
    assert "did not overwrite it" in result.stdout


def test_a_quota_skip_still_overwrites_an_older_quota_comment(tmp_path) -> None:
    prior = (
        f"{MARKER}\n**Summary:** Not run: quota - earlier\n\n"
        f"_Analysed base `{'a' * 8}` ... head `{HEAD[:8]}`._\n"
    )
    env, work = _setup(tmp_path, [HEAD, HEAD], ["42"], {"42": prior})
    env["QUOTA_SKIP"] = "true"
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    methods = _method_calls(env, work)
    assert len(methods) == 1 and "PATCH" in methods[0] and "comments/42" in methods[0]


def test_a_quota_skip_still_overwrites_a_verdict_for_another_head(tmp_path) -> None:
    prior = (
        f"{MARKER}\n**Summary:** real\n**Verdict: BLOCK**\n"
        f"\n_Analysed base `{'a' * 8}` ... head `{OTHER_HEAD[:8]}`._\n"
    )
    env, work = _setup(tmp_path, [HEAD, HEAD], ["42"], {"42": prior})
    env["QUOTA_SKIP"] = "true"
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    assert len(_method_calls(env, work)) == 1


def test_a_real_result_overwrites_an_earlier_verdict_for_the_same_head(tmp_path) -> None:
    prior = (
        f"{MARKER}\n**Summary:** old\n**Verdict: PASS**\n"
        f"_Analysed base `{'a' * 8}` ... head `{HEAD[:8]}`._\n"
    )
    env, work = _setup(tmp_path, [HEAD, HEAD], ["42"], {"42": prior})
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    methods = _method_calls(env, work)
    assert len(methods) == 1 and "PATCH" in methods[0]


# ---------------------------------------------------------------------------
# the head-moved guards
# ---------------------------------------------------------------------------


def test_a_moved_head_before_the_write_posts_nothing(tmp_path) -> None:
    env, work = _setup(tmp_path, [OTHER_HEAD, OTHER_HEAD], [])
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    assert _method_calls(env, work) == []
    assert "head moved" in result.stdout


def test_a_head_moving_during_the_post_still_posts_nothing(tmp_path) -> None:
    """First read agrees, second (just before the write) does not."""
    env, work = _setup(tmp_path, [HEAD, OTHER_HEAD], ["42"], {"42": "old"})
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    assert _method_calls(env, work) == []
    assert "while this comment was being posted" in result.stdout


# ---------------------------------------------------------------------------
# posting mechanics
# ---------------------------------------------------------------------------


def test_every_marker_comment_is_updated_not_just_the_newest(tmp_path) -> None:
    env, work = _setup(tmp_path, [HEAD, HEAD], ["7", "42"])
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    methods = _method_calls(env, work)
    assert len(methods) == 2
    assert any("comments/7" in m for m in methods)
    assert any("comments/42" in m for m in methods)
    assert "2 pre-mortem comments carry the marker" in result.stdout


def test_a_pr_with_no_marker_comment_gets_a_post(tmp_path) -> None:
    env, work = _setup(tmp_path, [HEAD, HEAD], [])
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    methods = _method_calls(env, work)
    assert len(methods) == 1 and "POST" in methods[0] and "/issues/409/comments" in methods[0]


def test_the_marker_and_body_make_up_body_md(tmp_path) -> None:
    env, work = _setup(tmp_path, [HEAD, HEAD], [])
    result = _run(env, work)
    assert result.returncode == 0, result.stderr
    body = (work / "body.md").read_text(encoding="utf-8")
    assert body.startswith(MARKER + "\n")
    assert "**Verdict: PASS**" in body
