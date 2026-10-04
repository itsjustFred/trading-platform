"""Every script in `scripts/` must import `shared` from the checkout it lives in.

Running a script puts `scripts/` on `sys.path[0]`, **not the repo root**, so `import shared`
does not find the checkout the file lives in. It falls through to the installed package, which
the editable install points at a *different* tree. Measured on 2026-10-04:

    sys.path[0]                        /home/ubuntu/perma-mut/scripts
    shared resolves to                 /home/ubuntu/Desktop/trading-platform/shared/database.py
    that copy has _add_service_columns  False

The two files are genuinely different revisions (md5 ``6a72c6cb4dd4`` vs ``3b3f3d27182e``), and the
installed one carries a **shorter migration set**: through it, ``init_db()`` does not add
``services.last_attempt_at`` and does not create ``live_control_audit``. So the documented
invocation of a script whose entire job is to migrate data would apply *an older schema than the
code believes* and then report success.

That is precisely the failure `continuation/merge-rehearsal.sh` was written to catch - its own
header names it "passed against the wrong data", and it resolves the import and refuses unless it
came from the intended tree. **The rehearsal guarded itself and left all ten scripts unguarded.**

So each script refuses instead, naming both paths. Refused rather than silently corrected: quietly
importing a different tree than the one you are editing *is* the hazard, not the fix. The correct
invocation is ``python -m scripts.<name>`` from the repo root, which puts the repo root on the path.

The roster check below is the load-bearing one. The behavioural cases can only exercise a script
someone remembers to name, so a script added tomorrow without the guard would pass all of them.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
GUARD_START = "# --- import-origin guard ---"
GUARD_END = "# --- end import-origin guard ---"
PROBE_DIR = "/tmp/opencode/import-origin-probe"

# The tree the editable install points at, which is *not* this repo. Named so a failure says what
# happened rather than just "mismatch".
INSTALLED_TREE = Path("/home/ubuntu/Desktop/trading-platform/shared")

needs_installed = pytest.mark.skipif(
    not INSTALLED_TREE.is_dir(), reason="the installed tree is not present on this host"
)


def _scripts_importing_shared() -> list[Path]:
    return sorted(
        p for p in SCRIPTS_DIR.glob("*.py")
        if re.search(r"^(?:from|import) shared\b", p.read_text(encoding="utf-8"), re.M)
    )


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/ubuntu",
        "DATA_DIR": PROBE_DIR,
        "TRADING_DATA_DIR": PROBE_DIR,
    }
    return subprocess.run(argv, capture_output=True, text=True, cwd=str(REPO_ROOT),
                          env=env, timeout=600)


def test_every_script_importing_shared_carries_the_guard() -> None:
    """The roster check, and the one that matters over months rather than seconds.

    The behavioural cases can only exercise a script someone remembers to name. A new script that
    imports `shared` and forgets the guard would pass every one of them, and would then quietly
    apply the installed tree's migration set. So each script is checked for the guard, and for it
    being positioned **before** the first `shared` import - a guard below the import it is meant
    to verify has already lost.
    """
    scripts = _scripts_importing_shared()
    assert scripts, (
        "no script imports `shared` any more, so this file is testing nothing. If the scripts "
        "moved, the import-origin problem moved with them and this file should say where."
    )
    missing, misplaced = [], []
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        if GUARD_START not in text:
            missing.append(script.name)
            continue
        # Search for the first `shared` import **outside the guard block**. The guard itself contains
        # `import shared as _shared_package`, so searching the whole file matches the guard's own
        # line and the check is satisfied by the very thing it is meant to test. That made it
        # self-satisfying, and a guard moved below the imports survived - found by mutation, not by
        # reading.
        # If the guard sits below every `shared` import, nothing matches in what follows it. That is
        # the whole test, and it is why searching only *after* the guard is the correct direction:
        # anything found there is, by construction, imported after the guard has already run.
        _, after_guard = text.split(GUARD_START, 1)[1].split(GUARD_END, 1)
        if not re.search(r"^(?:from|import) shared\b", after_guard, re.M):
            misplaced.append(f"{script.name} (no `shared` import after the guard)")
    assert not missing, (
        "these scripts import `shared` with no import-origin guard, so run directly they will "
        f"silently use the installed package instead of this checkout: {missing}"
    )
    assert not misplaced, (
        f"the guard sits below the first `shared` import, so it runs too late: {misplaced}"
    )


def test_the_guard_compares_against_the_scripts_own_checkout() -> None:
    """Not a hardcoded path.

    A guard comparing against a literal would keep passing after the repo moved, which is the same
    failure as the wiring test that located its guard by bare substring: it stops tracking the thing
    it is protecting.
    """
    text = (SCRIPTS_DIR / "recalculate_backtest_stats.py").read_text(encoding="utf-8")
    guard = text[text.index(GUARD_START):text.index(GUARD_END)]
    assert 'Path(__file__).resolve().parent.parent / "shared"' in guard, (
        "the guard no longer derives the expected tree from its own location; a literal would "
        "keep passing after the repo moved"
    )


@needs_installed
def test_the_two_trees_really_do_differ() -> None:
    """Otherwise the guard is guarding nothing.

    If the two checkouts are ever brought into line, the behavioural refusal below will start
    failing for the wrong reason - refusing something that is no longer wrong. Better to say so
    loudly than to keep asserting a difference that has been fixed.
    """
    repo_db = REPO_ROOT / "shared" / "database.py"
    installed_db = INSTALLED_TREE / "database.py"
    if not repo_db.is_file() or not installed_db.is_file():
        pytest.skip("one of the two trees is missing, so there is nothing to compare")
    repo_text = repo_db.read_text(encoding="utf-8")
    installed_text = installed_db.read_text(encoding="utf-8")
    repo_hash = hashlib.md5(repo_db.read_bytes()).hexdigest()
    installed_hash = hashlib.md5(installed_db.read_bytes()).hexdigest()
    assert repo_hash != installed_hash, (
        "the checkout and the installed package now hold the same shared/database.py "
        f"({repo_hash}). The guard is still correct, but it is no longer preventing a "
        "difference - worth knowing before treating a refusal as a live problem."
    )
    # And the difference must be a real one, not whitespace: the installed copy is missing a
    # migration step, which is the whole reason this matters.
    assert "_add_service_columns" in repo_text, (
        "this checkout no longer has _add_service_columns, so the example in the guard's comment "
        "is stale even though the trees still differ"
    )
    assert "_add_service_columns" not in installed_text, (
        "the installed copy now has _add_service_columns too, so it would apply the same "
        "migrations and the guard's stated reason is stale"
    )


@needs_installed
def test_running_a_script_directly_is_refused() -> None:
    """The behaviour, end to end, with the real exit code.

    Run directly - `python scripts/x.py` - which is how these scripts are documented to be invoked.
    The refusal must be a real non-zero exit, not a warning: a script that carries on after warning
    is exactly the hazard.
    """
    result = _run([sys.executable, "scripts/recalculate_backtest_stats.py", "--dry-run"])
    assert result.returncode != 0, (
        f"running the script directly was allowed to proceed (rc={result.returncode}); it would "
        "apply the installed tree's migration set\n" + result.stdout + result.stderr
    )
    assert "refusing to run" in result.stderr, result.stderr
    # Both paths must be named, so the reader can tell which tree they got without guessing.
    assert str(REPO_ROOT / "shared") in result.stderr, result.stderr
    assert str(INSTALLED_TREE) in result.stderr, result.stderr
    # And it must not have pretended to do the work.
    assert "updated=" not in result.stdout, result.stdout


@needs_installed
def test_the_refusal_covers_more_than_the_one_script() -> None:
    """A second script, to show the guard is the mechanism and not a one-file special case.

    The static roster check cannot see whether the block is reachable; this can.
    """
    result = _run([sys.executable, "scripts/backtest_to_validation_ref.py", "--help"])
    assert result.returncode != 0, result.stdout + result.stderr
    assert "refusing to run" in result.stderr, result.stderr


def test_the_documented_invocation_still_works() -> None:
    """The control: the fix must not make the scripts unrunnable.

    `python -m scripts.<name>` from the repo root puts the repo root on `sys.path`, so it resolves
    the intended tree and must proceed. A guard that only ever refuses is not a fix.
    """
    # Initialise the probe database first, with a real (non-dry) run. A dry run against an empty
    # data directory legitimately exits 2, because `init_db` builds the schema - that is the
    # separate dry-run check doing its job, and conflating the two would have had this control
    # assert the wrong thing.
    init = _run([sys.executable, "-m", "scripts.recalculate_backtest_stats", "--skip-json"])
    assert init.returncode == 0, init.stdout + init.stderr

    result = _run([sys.executable, "-m", "scripts.recalculate_backtest_stats", "--dry-run"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "refusing to run" not in result.stderr, result.stderr
    assert "Dry run: yes" in result.stdout, result.stdout
    assert "schema unchanged by this run" in result.stdout, result.stdout
