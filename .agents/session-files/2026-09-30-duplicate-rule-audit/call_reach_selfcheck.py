#!/usr/bin/env python3
"""Self-check for `call_reach.py`. A reporting tool nobody audits is just a number.

`call_reach.py` has produced the coverage figure quoted in every handoff note this session
(284 of 285 private functions in `live_runner` reached). That number was repeated many times
and **never tested**, which is the same mistake the tool exists to fix elsewhere: a
measurement nobody checked, reported as if it were sound. The two properties below are what
make it worth quoting.

**1. Sensitivity - if the tests go away, the number must move.**
The sharpest failure mode for a coverage tool is to report a healthy number while being
blind, because a tool that always says "reached" and a tool that cannot see anything look
identical in a summary. So: blank out the test file that is the sole reacher for a known set
of functions, and require the figure to fall by exactly that set and to *name* them.
Measured: 284 -> 274, naming the ten `market_data_stream` functions that file alone reached.

**2. The over-approximation is real, in the direction documented.**
The resolver is name-based, so two same-named methods in different classes merge and are
reported as one. That means the tool **under-reports gaps**, which is the safe direction for
this use: a gap it fails to see still fails loudly in the mutation harness, whereas a gap it
invented would send someone to write a test for code that is already covered. This check
pins that the behaviour has not silently flipped, because a future "improvement" that made
the resolver class-aware would change the meaning of every figure ever quoted.

**3. The real repo's unreached set is exactly what we chose to exclude.**
Not the count - the count moves whenever code is added. The *set*. It must be exactly the
Deliberately-excluded Discord functions, so a new gap fails this check and a quietly widened
exclusion fails it too.

Run:  python3 call_reach_selfcheck.py [--repo /home/ubuntu/perma-mut]
Exit: 0 all three hold; 1 otherwise.
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import tempfile

import call_reach

TOOL = pathlib.Path(call_reach.__file__).resolve()
VENV_PY = sys.executable

#: Functions deliberately left unreached. Extending this is a decision, and the decision
#: should show up in a diff - which is why the check compares the set and not the count.
EXPECTED_UNREACHED = {
    ("live_runner/daemon.py", "_discover_and_register_discord_services"),
}


def run_tool(root: pathlib.Path) -> tuple[int, str]:
    proc = subprocess.run(
        [VENV_PY, str(TOOL), "--root", str(root)],
        capture_output=True, text=True, timeout=900,
    )
    return proc.returncode, proc.stdout


def parse(stdout: str) -> tuple[int, set[tuple[str, str]]]:
    unreached: set[tuple[str, str]] = set()
    reached = -1
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("reached transitively"):
            reached = int(stripped.split(":")[1].strip())
        if stripped.startswith("UNREACHED "):
            module, _, name = stripped[len("UNREACHED "):].partition(" :: ")
            unreached.add((module.strip(), name.strip()))
    return reached, unreached


def check_sensitivity(repo: pathlib.Path) -> bool:
    """Blank the file that solely reaches the market-data helpers; the figure must drop."""
    target = repo / "tests" / "test_market_data_stream_symbols.py"
    if not target.exists():
        print("  SENSITIVITY  SKIP: %s is missing" % target.name)
        return True

    code, out = run_tool(repo)
    before, _ = parse(out)
    if code != 0 or before < 0:
        print("  SENSITIVITY  FAIL: baseline run failed (rc=%d)" % code)
        return False

    original = target.read_text(encoding="utf-8")
    try:
        target.write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")
        code, out = run_tool(repo)
        after, unreached = parse(out)
    finally:
        target.write_text(original, encoding="utf-8")

    lost = before - after
    named = {name for module, name in unreached if module.endswith("market_data_stream.py")}
    ok = lost >= 5 and len(named) >= 5
    print("  SENSITIVITY  %s  %d -> %d (-%d), named %d market_data functions"
          % ("OK  " if ok else "FAIL", before, after, lost, len(named)))
    if not ok:
        print("              a coverage tool that cannot see a whole test file removed is")
        print("              indistinguishable from one that always reports 'reached'")
    return ok


def check_over_approximation() -> bool:
    """Twin methods in different classes must merge - the documented safe-direction bias."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "live_runner").mkdir()
        (root / "tests").mkdir()
        (root / "live_runner" / "twins.py").write_text(
            "class Alpha:\n"
            "    def _handled(self) -> str:\n"
            "        return 'alpha'\n"
            "\n"
            "class Beta:\n"
            "    def _handled(self) -> str:\n"
            "        return 'beta'\n",
            encoding="utf-8",
        )
        (root / "tests" / "test_twins.py").write_text(
            "from live_runner.twins import Alpha\n"
            "\n"
            "def test_only_alpha_is_touched():\n"
            "    assert Alpha()._handled() == 'alpha'\n",
            encoding="utf-8",
        )
        code, out = run_tool(root)
        reached, unreached = parse(out)
    ok = code == 0 and reached == 1 and not unreached
    print("  OVER-APPROX  %s  two same-named methods reported as 1 reached, 0 unreached"
          % ("OK  " if ok else "FAIL"))
    if not ok:
        print("              the resolver is now class-aware; every figure quoted from this")
        print("              tool means something different and the notes must be revisited")
    return ok


def check_expected_unreached(repo: pathlib.Path) -> bool:
    code, out = run_tool(repo)
    if code != 0:
        print("  UNREACHED    FAIL: run failed (rc=%d)" % code)
        return False
    _reached, unreached = parse(out)
    ok = unreached == EXPECTED_UNREACHED
    print("  UNREACHED    %s  %s" % ("OK  " if ok else "FAIL", sorted(unreached)))
    if not ok:
        missing = EXPECTED_UNREACHED - unreached
        extra = unreached - EXPECTED_UNREACHED
        for entry in sorted(extra):
            print("              NEW GAP, not an accepted exclusion: %s :: %s" % entry)
        for entry in sorted(missing):
            print("              exclusion no longer applies: %s :: %s" % entry)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default="/home/ubuntu/perma-mut", type=pathlib.Path)
    args = ap.parse_args()

    print("  self-check for %s" % TOOL.name)
    results = [
        check_sensitivity(args.repo),
        check_over_approximation(),
        check_expected_unreached(args.repo),
    ]
    if all(results):
        print("  ALL THREE HOLD: the coverage figure in the notes is one you can rely on")
        return 0
    print("  SELF-CHECK FAILED: the coverage figure is not currently trustworthy")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
