"""The gate runner must not be able to report a gate it did not run.

`scripts/gate-tests.sh` replaced a runner that printed "ALL 13 GATE SUITES PASSED" while one of
its thirteen entries had no command at all. `run_one` called `if "$@"` with an empty argument
list, which runs nothing, succeeds, and was recorded as `ok`. The suite the entry named had been
pasted under the following gate, so it really ran - the gate that claimed it ran nothing, and the
summary counted it.

The first test below is that exact defect, called the way the script calls it. It fails against
the old `run_one` and passes against the one in `scripts/lib/gate_runner.sh`, which is why the
runner was split into a sourced library: the script can never call `run_one` with no command, so
a regression test that only invoked the script could not pin this at all.

The roster cases are the second half of the same finding: a suite that moves behind a conditional,
or is renamed, must not be reported as passed. The roster is compared by name for that reason.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import sandboxed_env

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = REPO_ROOT / "scripts" / "gate-tests.sh"
GATE_LIB = REPO_ROOT / "scripts" / "lib" / "gate_runner.sh"

needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="the gate runner is a bash script; without bash there is nothing to exercise",
)


def _bash(body: str) -> subprocess.CompletedProcess[str]:
    """Source the runner library, run `body`, and return the result."""
    script = f'set -uo pipefail\n. "{GATE_LIB.as_posix()}"\n{body}\n'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
        env=sandboxed_env(),
    )


@needs_bash
def test_a_gate_without_a_command_fails() -> None:
    """The defect, directly: no command must be a failure, not a pass.

    Before the fix this returned 0, recorded `ok`, and left the failure count at zero - the gate was
    indistinguishable from one that had run and passed.
    """
    result = _bash(
        'run_one "empty gate"\n'
        'printf "rc=%s\\n" "$?"\n'
        'printf "result=%s\\n" "${gate_results[0]}"\n'
        'printf "failed=%s\\n" "$(gate_failed_count)"\n'
    )
    assert result.returncode == 0, result.stderr  # the snippet itself must run to completion
    assert "rc=1" in result.stdout, result.stdout + result.stderr
    assert "result=no-command" in result.stdout, result.stdout
    assert "failed=1" in result.stdout, result.stdout
    assert "NO COMMAND" in result.stderr, result.stderr


@needs_bash
def test_a_passing_command_is_recorded_as_a_pass() -> None:
    """The control: the guard must not turn every gate into a failure."""
    result = _bash(
        'run_one "a gate that passes" true\n'
        'printf "result=%s\\n" "${gate_results[0]}"\n'
        'printf "failed=%s\\n" "$(gate_failed_count)"\n'
    )
    assert result.returncode == 0, result.stderr
    assert "result=ok" in result.stdout, result.stdout
    assert "failed=0" in result.stdout, result.stdout


@needs_bash
def test_a_failing_command_is_recorded_with_its_exit_code() -> None:
    result = _bash(
        'run_one "a gate that fails" false\n'
        'printf "result=%s\\n" "${gate_results[0]}"\n'
        'printf "failed=%s\\n" "$(gate_failed_count)"\n'
    )
    assert "result=rc=1" in result.stdout, result.stdout
    assert "failed=1" in result.stdout, result.stdout


@needs_bash
def test_the_roster_check_passes_when_every_declared_gate_ran() -> None:
    result = _bash(
        'run_one "one" true\n'
        'run_one "two" true\n'
        'gate_roster_check "one" "two"\n'
        'printf "roster_rc=%s\\n" "$?"\n'
    )
    assert "roster_rc=0" in result.stdout, result.stdout + result.stderr


@needs_bash
def test_the_roster_check_fails_when_a_declared_gate_did_not_run() -> None:
    """A gate that moved behind a conditional is missing, and the count alone would not show it.

    The roster is deliberately the same size as the run here, so a count-only check would pass.
    """
    result = _bash(
        'run_one "one" true\n'
        'gate_roster_check "one" "two"\n'
        'printf "roster_rc=%s\\n" "$?"\n'
    )
    assert "roster_rc=2" in result.stdout, result.stdout + result.stderr
    assert "declared but did not run: two" in result.stderr, result.stderr


@needs_bash
def test_the_roster_check_fails_when_a_gate_ran_without_being_declared() -> None:
    result = _bash(
        'run_one "one" true\n'
        'run_one "surprise" true\n'
        'gate_roster_check "one"\n'
        'printf "roster_rc=%s\\n" "$?"\n'
    )
    assert "roster_rc=2" in result.stdout, result.stdout + result.stderr
    assert "ran but was not declared: surprise" in result.stderr, result.stderr


@needs_bash
def test_the_declared_roster_is_not_empty() -> None:
    """Otherwise the runner would print "ALL 0 GATE SUITES PASSED" and exit 0.

    That is not hypothetical: a copy of the runner did exactly that, and the line it printed is the
    exact claim the script exists to make, so a zero-gate run asserted it most confidently of all.
    """
    result = subprocess.run(
        ["bash", str(GATE_SCRIPT), "--list"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
        env=sandboxed_env(),
    )
    assert result.returncode == 0, result.stderr
    declared = [line for line in result.stdout.splitlines() if line.strip()]
    assert declared, "the gate roster is empty"


@needs_bash
def test_the_roster_check_prints_the_mismatch_banner() -> None:
    """The banner is the only thing that names *why* exit 2 happened.

    `test_full_roster` asserts the banner is *absent* on a good run, so a version that never prints
    it passed everything: the exit code still said 2, the per-side lines still said what differed,
    and a reader of the logs would see a bare "Exiting 2" with nothing tying it to the roster. The
    banner is the difference between "the roster moved" and "something, somewhere, failed".
    """
    result = _bash(
        'gate_roster_check "one" "two" > /dev/null 2>&1\n'
        'printf "roster_rc=%s\\n" "$?"\n'
        'gate_roster_check "one" "two" 2>&1 > /dev/null\n'
    )
    assert "roster_rc=2" in result.stdout, result.stdout + result.stderr
    assert "ROSTER MISMATCH" in result.stdout, result.stdout + result.stderr
    assert "describe a roster that does not exist" in result.stdout, result.stdout


@needs_bash
def test_the_roster_check_reports_both_sides_at_once() -> None:
    """A one-sided scan cannot tell "declared and not run" from "ran and not declared".

    Both can be true in the same run - a gate renamed, so the old name is missing and the new one is
    extra - and a check that reports only the first side found sends the reader looking for a gate
    that is right there under a different name.
    """
    result = _bash(
        'run_one "one" true\n'
        'run_one "surprise" true\n'
        'gate_roster_check "one" "two" 2>&1 > /dev/null\n'
        'printf "roster_rc=%s\\n" "$?"\n'
    )
    assert "declared but did not run: two" in result.stdout, result.stdout + result.stderr
    assert "ran but was not declared: surprise" in result.stdout, result.stdout


def _guarded_scripts_roster() -> list[str]:
    """The GUARDED_SCRIPTS entries as written in `scripts/gate-tests.sh`."""
    text = GATE_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"GUARDED_SCRIPTS=\(\n(?P<body>.*?)\n\)", text, re.DOTALL)
    assert match, "GUARDED_SCRIPTS array not found in gate-tests.sh"
    return re.findall(r"^\s*(\S+\.py)\s*$", match.group("body"), re.MULTILINE)


def test_guarded_scripts_roster_keeps_every_entry() -> None:
    """The roster is the F-lint scope; a wholesale overwrite drops entries silently.

    Restoring a file by copying an old version of this script once removed
    `fetch_differential_slice.py` while re-adding `signal_archive_market_features.py`, and
    nothing failed: ruff exits 0 on a shorter list, so the dropped script's lint coverage was
    gone with a green gate. This pins both members and that every listed file exists, so the
    roster can only change by editing this list deliberately.
    """
    roster = _guarded_scripts_roster()
    assert "scripts/fetch_differential_slice.py" in roster
    assert "scripts/signal_archive_market_features.py" in roster
    missing = [entry for entry in roster if not (REPO_ROOT / entry).is_file()]
    assert not missing, f"GUARDED_SCRIPTS lists files that do not exist: {missing}"


@needs_bash
@pytest.mark.timeout(600)  # runs every gate suite in subprocesses; matches their own timeout
def test_full_roster(tmp_path: Path) -> None:
    """End to end: every declared gate exists, runs, and passes.

    The gate runner excludes this case when it runs the harness self-check, so there is no cycle:
    the script runs the unit cases above, and this case runs the script.
    """
    # The sandboxed environment for the runner and every gate it spawns: the allowlist drops
    # BASE_URL and PERMA_ALLOW_LIVE_VIEWER_WRITES, and the data roots are the session sandbox's,
    # which no gate can write the checkout's data through. The child does not share the
    # sandbox: every gate that runs a suite runs it as its own pytest process, and that process
    # must build the suite sandbox a standalone run builds - its own session, its own forced
    # data roots - rather than inherit one it did not establish.
    env = sandboxed_env(PYTHON=sys.executable, shares_sandbox=False)
    result = subprocess.run(
        ["bash", str(GATE_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GATE SUITES PASSED" in result.stdout, result.stdout
    assert "ROSTER MISMATCH" not in result.stdout + result.stderr, result.stdout + result.stderr
