"""The live services must run code the audit has already seen.

Measured on 2026-10-05, and this is the operationally sharpest thing found today:

* the services run from `/home/ubuntu/Desktop/trading-platform`, on branch
  `perma/validation-live-trading-2026-09-25`, at `b4c58ed`;
* the audited code is `perma/mutation-gap-tests` at `f259cb1`;
* `b4c58ed` **is an ancestor** of `f259cb1` - so the running code is strictly behind, never ahead,
  by **308 commits and 427 files**;
* the checkout settled on `b4c58ed` at 2026-09-26 20:37 and the daemon started 2026-10-01 06:21,
  *after* that, so the process really is running `b4c58ed` and not some earlier tree.

**The property worth protecting is not the distance, it is the direction.** Being 308 commits behind
is a known, dated fact the operator can plan around. Being *ahead* would mean live
trading on code no audit has seen, which is the failure this test exists to catch.
audit has seen, which is the failure this test exists to catch. So the assertion is on ancestry, and
So the assertion is on ancestry, and the divergence size is reported rather than
pinned - pinning a number would make this test fail every time the operator does
the very thing it exists to permit.
time the operator does the thing it exists to permit.

It also makes the ordering constraint explicit. The running code predates the `broker_fill_id`
* the audited code is `perma/mutation-gap-tests` at `f259cb1`;
would activate 308 commits of new behaviour against the old schema, and the very first cTrader fill
would fail on `no such column: broker_fill_id`. Migrate, then restart - in that order, and the
failure mode for getting it backwards is a write on the first live fill.

Everything here is read-only: `systemctl show` and `git`. Nothing is restarted or written, and the
test skips with a stated reason whenever the service is not running or the path is not a repository,
  after that, so the process really is running `b4c58ed` and not some earlier tree.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVICE = "trading-daemon.service"
RUNNING_FROM = Path("/home/ubuntu/Desktop/trading-platform")


def _prop(name: str) -> str:
    if shutil.which("systemctl") is None:
        pytest.skip("systemctl is not available, so the running service cannot be inspected")
    result = subprocess.run(
        ["systemctl", "show", "-p", name, "--value", SERVICE],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip()


def _git(repo: Path, *args: str) -> str:
    if not (repo / ".git").exists():
        pytest.skip(f"{repo} is not a git repository, so the running code cannot be identified")
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_the_services_are_actually_running_so_this_check_is_not_vacuous() -> None:
    """A test that skips because the service is down cannot protect the service being up."""
    pid = _prop("MainPID")
    if not pid or pid == "0":
        pytest.skip(f"{SERVICE} is not running, so there is no live code to compare")
    assert Path(f"/proc/{pid}").is_dir(), (
        f"{SERVICE} reports MainPID {pid} but /proc/{pid} does not exist, so the process "
        f"state and the unit disagree and this file's premise is unsafe"
    )


def test_the_running_code_is_an_ancestor_of_the_audited_code() -> None:
    """The direction, not the distance. Behind is planned; ahead is unaudited trading."""
    if not RUNNING_FROM.exists():
        pytest.skip(f"{RUNNING_FROM} does not exist, so the running checkout cannot be identified")
    running_head = _git(RUNNING_FROM, "rev-parse", "HEAD")
    audited_head = _git(REPO, "rev-parse", "HEAD")
    behind = subprocess.run(
        ["git", "-C", str(REPO), "rev-list", "--count", f"{running_head}..{audited_head}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    ahead = subprocess.run(
        ["git", "-C", str(REPO), "rev-list", "--count", f"{audited_head}..{running_head}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    print(f"\n  services run {running_head[:7]}, {behind} commits behind "
          f"the audited {audited_head[:7]}")
    assert ahead == "0", (
        f"the services run {running_head[:8]}, which is {ahead} commit(s) AHEAD of the audited "
        f"{audited_head[:8]}. That is live trading on code no audit has seen - either the audit "
        f"branch is behind the services, or the services were restarted onto an unaudited branch."
    )
    print(f"\n  services run {running_head[:8]}, {behind} commits behind the audited "
          f"{audited_head[:8]}")


def test_the_running_checkout_is_clean_so_the_tree_matches_the_commit() -> None:
    """The process loaded from this directory, so an uncommitted edit is code nobody reviewed."""
    if not RUNNING_FROM.exists():
        pytest.skip(f"{RUNNING_FROM} does not exist")
    dirty = _git(RUNNING_FROM, "status", "--porcelain")
    assert not dirty, (
        "the checkout the services run from has uncommitted changes:\n"
        f"{dirty}\nthe running process loaded these bytes, not the commit"
    )


def test_the_credential_lock_file_is_gitignored() -> None:
    """A running service's own bookkeeping must not read as a dirty checkout.

    The guard above fails when the checkout the services run from is dirty, which is the right
    behaviour - it means "the running process loaded these bytes, not the commit". But the
    credential writer creates `<name>.lock` beside the file it is rewriting
    (`live_runner/ctrader_adapter.py`), so a live service drops a 0-byte file into its own
    checkout on every credential write.

    That file is a transient advisory lock: no data, no content, recreated each write. Treating it
    as a change makes the guard fail for a reason that has nothing to do with code, and the fix has
    to be deliberate rather than "rm the file" - because the service creates it again on its next
    write, and the next run of this suite fails for the same reason.

    So the rule is pinned rather than left to a well-meaning future deletion: if `.accounts.env.lock`
    is ever un-ignored, this fails here with the reason, instead of the clean-checkout guard
    failing later with a confusing message about "uncommitted changes" that are not changes.
    """
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".accounts.env.lock"],
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert ignored.returncode == 0, (
        ".accounts.env.lock is not gitignored, so a running service's transient credential "
        "lock will make the clean-checkout guard fail on a 0-byte file that is not a code change"
    )
