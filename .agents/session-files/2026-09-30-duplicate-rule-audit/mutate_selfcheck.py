#!/usr/bin/env python3
"""Self-check for `mutate.sh`. The harness is ground truth, so it gets the strictest test.

Every other tool in this audit got a self-check after its own flaw was found. This one
matters more than the rest, because its output is what every finding in the audit is *based
on*: a mutation reported KILLED becomes a committed test, a mutation reported SURVIVED becomes
a new test to write. A wrong verdict here does not just mislead, it writes the wrong code.

**The rule being enforced: `rc=0` must mean the claim is proven.**

That was not true until this script existed. The verdict used to be `rc != 0` and nothing
else, so an unrelated failure anywhere in the suite was reported as a kill - proven with a
verified equivalent mutant. The next hole was subtler: once attribution was added, an
*unattributed* kill still exited 0 when `--expect` was omitted, so a caller checking the exit
status - which every caller here does - was told a verdict held when the harness had just
printed "this mutation is NOT proven caught".

So the exit codes are now part of the contract, not an implementation detail:

| rc | meaning |
|---|---|
| 0 | applied, suite ran, reverted, and the verdict is **backed by a named failing test** (or SURVIVED, which needs no attribution) |
| 2 | the mutation text was not uniquely present - nothing was mutated |
| 3 | applied but the file did not change |
| 4 | no verdict: the suite did not run, a file failed to collect, **or pytest exited non-zero without any test failing** (fixture teardown, warning-as-error, plugin hook) - measured to have been reported `KILLED ... failed=0` |
| 5 | the verdict did not match `--expect` |
| 6 | the revert failed - working tree dirty |
| 7 | KILLED-UNATTRIBUTED: a test failed but not the one that should have. **Wrong**, not merely unverified. Never masked by `--expect` |
| 8 | KILLED with no `--detector`: some test **did** fail, but nothing proves it was this mutation. **Unverified**. Never masked by `--expect` |

A kill requires a failing test. `rc != 0` is not sufficient and never was sufficient: pytest
also exits non-zero for a fixture teardown error, a warning promoted to an error, and a
plugin hook failing after the tests ran - each of which has every test passing. Those were
reported `KILLED`, which is the "green summary read as a pass" mistake one level further
down.

Each row below is proved on a throwaway repo copy, never on the real one, so a deliberate
breakage can never be mistaken for a real failure in a test run. The cases that need a known
survivor and a known killer use mutations verified by hand in earlier batches, not guesses.

Run:  python3 mutate_selfcheck.py
Exit: 0 all cases hold; 1 otherwise.
"""
from __future__ import annotations

import importlib
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path("/home/ubuntu/perma-mut")
HARNESS = REPO / ".agents/session-files/2026-09-30-duplicate-rule-audit/mutate.sh"

#: Verified equivalent mutant: a match already requires both sides equal, so the leading
#: `login_hint` truthiness test can never change the outcome. Confirmed SURVIVED by hand.
SURVIVOR_OLD = "        if login_hint and normalized_login and login_hint == normalized_login:"
SURVIVOR_NEW = "        if normalized_login and login_hint == normalized_login:"

#: Verified killed: removing the fail-closed return lets a database error through.
KILLER_OLD = ('            extra={"data": {"account_id": account_id, "error": repr(exc)}},\n'
              '        )\n        return False\n    if block_reason is not None:')
KILLER_NEW = ('            extra={"data": {"account_id": account_id, "error": repr(exc)}},\n'
              '        )\n        return True\n    if block_reason is not None:')

UNRELATED_TEST = '''"""Deliberately failing, unrelated to any mutation under test."""


def test_an_unrelated_failure() -> None:
    assert 1 == 2, "this failure has nothing to do with any mutation"
'''


#: A test that PASSES, plus a fixture whose teardown raises. pytest exits 1 and prints
#: `1 passed, 1 error` with **no FAILED line**. This is the shape that must never be read as
#: a kill: a non-zero exit means "something went wrong", and a teardown error is not a test
#: failure. It is the class that also covers a warning promoted to an error by config and a
#: plugin hook returning non-zero - all rc=1, all with every test passing.
TEARDOWN_TEST = '''"""Passes, then fails on teardown."""

import pytest


@pytest.fixture
def explodes_on_teardown():
    yield "value"
    raise RuntimeError("teardown exploded")


def test_passes_first(explodes_on_teardown):
    assert explodes_on_teardown == "value"
'''


def run(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HARNESS), *args],
        cwd=root, capture_output=True, text=True, timeout=900,
    )


def sandbox() -> tuple[pathlib.Path, subprocess.CompletedProcess | None]:
    """A throwaway git repo with a minimal live_runner + tests, so nothing real is touched."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mutate-selfcheck-"))
    (tmp / "probe_pkg").mkdir()
    (tmp / "tests").mkdir()
    (tmp / "probe_pkg" / "mod.py").write_text(
        "def guarded(value):\n"
        "    if value < 0:\n"
        "        return False\n"
        "    return True\n"
        "\n"
        "\n"
        "def hint_login(a, b):\n"
        "    if b and a == b:\n"
        "        return a\n"
        "    return None\n",
        encoding="utf-8",
    )
    (tmp / "tests" / "test_mod.py").write_text(
        "from probe_pkg.mod import guarded, hint_login\n"
        "\n"
        "\n"
        "def test_guarded_refuses_negative():\n"
        "    assert guarded(-1) is False\n"
        "\n"
        "\n"
        "def test_hint_matches():\n"
        "    assert hint_login('x', 'x') == 'x'\n",
        encoding="utf-8",
    )
    # The sandbox needs the same import plumbing the real repo gets from its own conftest,
    # otherwise every case dies with ModuleNotFoundError before reaching the harness at all.
    # The first version of this script did exactly that and reported nine failures, all of
    # them really one: a broken sandbox, not a broken harness.
    (tmp / "conftest.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n",
        encoding="utf-8",
    )
    # `__pycache__` is untracked noise; ignoring it keeps the "tree is clean afterwards"
    # check about the harness's revert rather than about Python's import cache.
    (tmp / ".gitignore").write_text("__pycache__/\n.pytest_cache/\noutside/\n", encoding="utf-8")
    proc = subprocess.run(["git", "init", "-q"], cwd=tmp, capture_output=True, text=True)
    proc2 = subprocess.run(["git", "add", "-A"], cwd=tmp, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
        cwd=tmp, capture_output=True, text=True,
    )
    return tmp, proc if proc.returncode or proc2.returncode else None


def case(name: str, expected_rc: int, root: pathlib.Path, *args: str) -> bool:
    proc = run(root, *args)
    ok = proc.returncode == expected_rc
    tail = [l for l in proc.stdout.splitlines() if l.strip()][:1]
    print("  %-46s rc=%-2s want=%-2s %s"
          % (name, proc.returncode, expected_rc, "OK" if ok else "FAIL"))
    if tail:
        print("      %s" % tail[0].strip()[:104])
    if not ok:
        print("      stdout: %s" % proc.stdout.strip()[-200:])
        print("      stderr: %s" % proc.stderr.strip()[-200:])
    return ok


def main() -> int:
    if not HARNESS.exists():
        print("  harness not found: %s" % HARNESS)
        return 1

    results = []
    root, failed_setup = sandbox()
    print("  sandbox: %s" % root)
    if failed_setup is not None:
        print("  could not initialise the sandbox git repo")
        return 1
    try:
        old = "    if b and a == b:"
        new = "    if a == b:"

        # 0 - a genuine kill WITH the detector that should catch it.
        results.append(case(
            "attributed kill", 0, root,
            "--file", "probe_pkg/mod.py", "--suite", "tests/test_mod.py",
            "--detector", "test_guarded_refuses_negative",
            "--label", "attributed kill", "--expect", "killed",
            "--old", "    if value < 0:\n        return False",
            "--new", "    return True",
        ))

        # 1 - a genuine survivor needs no attribution, so rc=0 is honest.
        results.append(case(
            "genuine survivor", 0, root,
            "--file", "probe_pkg/mod.py", "--suite", "tests/test_mod.py",
            "--label", "survivor", "--expect", "survived",
            "--old", old, "--new", new,
        ))

        # 2 - a kill with no --detector is UNVERIFIED: rc=8, not 0.
        (root / "tests" / "test_unrelated.py").write_text(UNRELATED_TEST, encoding="utf-8")
        results.append(case(
            "kill with no --detector -> unverified", 8, root,
            "--file", "probe_pkg/mod.py",
            "--suite", "tests/test_mod.py tests/test_unrelated.py",
            "--label", "no detector", "--old", old, "--new", new,
        ))

        # 3 - a kill whose failing test is not the detector is WRONG: rc=7.
        results.append(case(
            "kill by an unrelated test -> wrong", 7, root,
            "--file", "probe_pkg/mod.py",
            "--suite", "tests/test_mod.py tests/test_unrelated.py",
            "--detector", "test_guarded_refuses_negative",
            "--label", "misattributed", "--old", old, "--new", new,
        ))

        # 4 - and it must NOT be masked by --expect killed.
        results.append(case(
            "--expect killed cannot mask a wrong verdict", 7, root,
            "--file", "probe_pkg/mod.py",
            "--suite", "tests/test_mod.py tests/test_unrelated.py",
            "--detector", "test_guarded_refuses_negative",
            "--label", "misattributed with expect", "--expect", "killed",
            "--old", old, "--new", new,
        ))
        (root / "tests" / "test_unrelated.py").unlink()

        # 5 - a non-unique old string mutates nothing: rc=2, and the tree is clean.
        results.append(case(
            "non-unique mutation text -> rc=2", 2, root,
            "--file", "probe_pkg/mod.py", "--suite", "tests/test_mod.py",
            "--label", "ambiguous", "--old", "    return", "--new", "    pass",
        ))

        # 6 - a suite path that does not exist: INFRA, rc=4.
        results.append(case(
            "missing suite path -> INFRA", 4, root,
            "--file", "probe_pkg/mod.py", "--suite", "tests/test_nope.py",
            "--label", "no such path", "--old", old, "--new", new,
        ))

        # 7 - a file that fails to COLLECT, with others still collected: INFRA, rc=4.
        #    The old logic read this as a kill, because rc was non-zero.
        (root / "tests" / "test_broken.py").write_text("def test_x(:\n    pass\n", encoding="utf-8")
        results.append(case(
            "one file fails to collect -> INFRA", 4, root,
            "--file", "probe_pkg/mod.py",
            "--suite", "tests/test_mod.py tests/test_broken.py",
            "--label", "collection error", "--old", old, "--new", new,
        ))
        (root / "tests" / "test_broken.py").unlink()

        # 7b - rc != 0 with ZERO failing tests is INFRA, not a kill.
        #     Measured before this case existed: the harness printed
        #     `KILLED ... rc=1 collected=1 failed=0` for this exact scenario. Nothing was
        #     caught - the suite said `1 passed, 1 error` - and the printed word said KILLED.
        #     `FAILURES` was computed two lines above the verdict and used only for the log.
        (root / "tests" / "test_teardown.py").write_text(TEARDOWN_TEST, encoding="utf-8")
        results.append(case(
            "rc!=0 but no test failed -> INFRA", 4, root,
            "--file", "probe_pkg/mod.py",
            "--suite", "tests/test_mod.py tests/test_teardown.py",
            "--label", "teardown error, not a failure", "--old", old, "--new", new,
        ))
        # The same suite WITH a detector is still INFRA (rc=4), not rc=7. That was the
        # expectation I first wrote, and the harness was right and I was wrong: the
        # zero-failures check runs before the attribution logic, so there is nothing to
        # attribute and "no verdict" is more honest than "wrong test caught it". Recorded
        # because "KILLED-UNATTRIBUTED" would have been a much worse answer here - it at
        # least implies a test failed.
        results.append(case(
            "...and with --detector, still no verdict", 4, root,
            "--file", "probe_pkg/mod.py",
            "--suite", "tests/test_mod.py tests/test_teardown.py",
            "--detector", "test_hint_matches",
            "--label", "teardown error, detector given", "--old", old, "--new", new,
        ))
        (root / "tests" / "test_teardown.py").unlink()

        # 7c - and the new check must NOT swallow real kills. A mutation that a test catches,
        #      in a suite that also has a teardown error, is still a genuine kill: the failing
        #      test is what makes it one.
        (root / "tests" / "test_teardown.py").write_text(TEARDOWN_TEST, encoding="utf-8")
        results.append(case(
            "a real kill alongside a teardown error", 0, root,
            "--file", "probe_pkg/mod.py",
            "--suite", "tests/test_mod.py tests/test_teardown.py",
            "--detector", "test_guarded_refuses_negative",
            "--label", "real kill plus teardown error", "--expect", "killed",
            "--old", "    if value < 0:\n        return False",
            "--new", "    return True",
        ))
        (root / "tests" / "test_teardown.py").unlink()

        # 8 - a wrong expectation is reported: rc=5.
        results.append(case(
            "wrong --expect is reported", 5, root,
            "--file", "probe_pkg/mod.py", "--suite", "tests/test_mod.py",
            "--label", "wrong expect", "--expect", "killed",
            "--old", old, "--new", new,
        ))

        # 9 - a package whose import does NOT come from this worktree is refused. The
        #    venv has an editable install of a second, divergent `live_runner`; a run
        #    rooted elsewhere would mutate one repo and report on the other. Here the
        #    shadowed name stands in for that.
        (root / "outside").mkdir()
        (root / "outside" / "probe_pkg").mkdir()
        (root / "outside" / "probe_pkg" / "mod.py").write_text(
            "def guarded(value):\n    return True\n", encoding="utf-8"
        )
        sys.path.insert(0, str(root / "outside"))
        for mod in list(sys.modules):
            if mod.startswith("probe_pkg"):
                del sys.modules[mod]
        shadowed = importlib.import_module("probe_pkg")
        results.append(case(
            "import from outside the worktree -> refused", 9, root,
            "--file", "probe_pkg/mod.py", "--suite", "tests/test_mod.py",
            "--label", "import mismatch", "--old", old, "--new", new,
        ) if str(shadowed.__file__).startswith(str(root / "outside")) else True)
        sys.path.remove(str(root / "outside"))
        for mod in list(sys.modules):
            if mod.startswith("probe_pkg"):
                del sys.modules[mod]

        # 10 - the tree must be clean after every one of the above.
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
        ).stdout.strip()
        clean = dirty == ""
        print("  %-46s %-13s %s" % ("working tree clean afterwards", "dirty" if dirty else "clean",
                                    "OK" if clean else "FAIL"))
        if not clean:
            print("      %s" % dirty[:200])
        results.append(clean)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if all(results):
        print("  ALL %d CASES HOLD: rc=0 means the verdict is proven, and the tree is clean"
              % len(results))
        return 0
    print("  SELF-CHECK FAILED: do not treat this harness as ground truth")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
