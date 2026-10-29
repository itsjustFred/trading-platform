"""Every script in `scripts/` must import `shared` from the checkout it lives in.

Running a script puts `scripts/` on `sys.path[0]`, **not the repo root**, so `import shared` does
not find the checkout the file lives in. It falls through to the installed package, which the
editable install points at a *different* tree. Measured on 2026-10-04: the two were genuinely
different revisions, and the installed one carried a **shorter migration set** - through it,
`init_db()` did not add `services.last_attempt_at` and did not create `live_control_audit`. So the
documented invocation of a script whose entire job is to migrate data applied *an older schema than
the code believes* and then reported success.

So each script refuses instead, naming both paths. Refused rather than silently corrected: quietly
importing a different tree than the one you are editing *is* the hazard, not the fix. The correct
invocation is ``python -m scripts.<name>`` from the repo root, which puts the repo root on the path.

The roster check below is the load-bearing one. The behavioural cases can only exercise a script
someone remembers to name, so a script added tomorrow without the guard would pass all of them.

The installed tree is resolved at run time, not hardcoded: it differs per machine, and a literal
would keep passing after the venv changed. When the two trees are the same - a fresh venv installed
from this checkout - there is nothing to refuse, so the behavioural cases skip instead of pretending
to prove something.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
GUARD_START = "# --- import-origin guard ---"
GUARD_END = "# --- end import-origin guard ---"


def _probe_env(data_dir: Path) -> dict[str, str]:
    """An environment that cannot reach the live platform.

    `BASE_URL` and `PERMA_ALLOW_LIVE_VIEWER_WRITES` are dropped rather than blanked, because an
    empty value is still a value the viewer client may act on.
    """
    env = dict(os.environ)
    for name in ("BASE_URL", "PERMA_ALLOW_LIVE_VIEWER_WRITES"):
        env.pop(name, None)
    env["DATA_DIR"] = str(data_dir)
    env["TRADING_DATA_DIR"] = str(data_dir)
    env["LOG_DIR"] = str(data_dir / "logs")
    return env


def _installed_shared_tree() -> Path | None:
    """Where `import shared` resolves when the repo root is *not* on `sys.path`.

    Run from a directory that is neither the repo root nor `scripts/`, which is exactly the
    situation a directly-invoked script is in.
    """
    with tempfile.TemporaryDirectory() as neutral:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import pathlib, shared; "
                "print(pathlib.Path(next(iter(shared.__path__))).resolve())",
            ],
            capture_output=True,
            text=True,
            cwd=neutral,
            env=_probe_env(Path(neutral)),
            timeout=120,
        )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


INSTALLED_TREE = _installed_shared_tree()
DIVERGENT_TREE = INSTALLED_TREE is not None and INSTALLED_TREE != REPO_ROOT / "shared"

needs_divergent_tree = pytest.mark.skipif(
    not DIVERGENT_TREE,
    reason=(
        "the installed `shared` resolves to this checkout, so there is no wrong tree to refuse"
        + ("" if INSTALLED_TREE is None else f" (resolved: {INSTALLED_TREE})")
    ),
)


def _scripts_importing_shared() -> list[Path]:
    return sorted(
        p for p in SCRIPTS_DIR.glob("*.py")
        if re.search(r"^(?:from|import) shared\b", p.read_text(encoding="utf-8"), re.M)
    )


def _run(argv: list[str], data_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_probe_env(data_dir),
        timeout=600,
    )


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
        # Search for the first `shared` import **outside the guard block**. The guard itself
        # contains `import shared as _shared_package`, so searching the whole file matches the
        # guard's own line and the check is satisfied by the very thing it is meant to test.
        # That made it self-satisfying, and a guard moved below the imports survived - found by
        # mutation, not by reading.
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


@needs_divergent_tree
def test_the_two_trees_really_do_differ() -> None:
    """Otherwise the guard is guarding nothing.

    If the two checkouts are ever brought into line, the behavioural refusal below will start
    failing for the wrong reason - refusing something that is no longer wrong. Better to say so
    loudly than to keep asserting a difference that has been fixed.
    """
    assert INSTALLED_TREE is not None
    repo_db = REPO_ROOT / "shared" / "database.py"
    installed_db = INSTALLED_TREE / "database.py"
    if not repo_db.is_file() or not installed_db.is_file():
        pytest.skip("one of the two trees is missing, so there is nothing to compare")
    repo_digest = hashlib.sha256(repo_db.read_bytes()).hexdigest()
    installed_digest = hashlib.sha256(installed_db.read_bytes()).hexdigest()
    assert repo_digest != installed_digest, (
        "the checkout and the installed package now hold the same shared/database.py "
        f"({repo_digest[:12]}). The guard is still correct, but it is no longer preventing a "
        "difference - worth knowing before treating a refusal as a live problem."
    )


@needs_divergent_tree
def test_running_a_script_directly_is_refused(tmp_path: Path) -> None:
    """The behaviour, end to end, with the real exit code.

    Run directly - `python scripts/x.py` - which is how these scripts used to be documented. The
    refusal must be a real non-zero exit, not a warning: a script that carries on after warning is
    exactly the hazard.
    """
    assert INSTALLED_TREE is not None
    result = _run([sys.executable, "scripts/recalculate_backtest_stats.py", "--dry-run"], tmp_path)
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


@needs_divergent_tree
def test_the_refusal_covers_more_than_the_one_script(tmp_path: Path) -> None:
    """A second script, to show the guard is the mechanism and not a one-file special case.

    The static roster check cannot see whether the block is reachable; this can.
    """
    result = _run([sys.executable, "scripts/backtest_to_validation_ref.py", "--help"], tmp_path)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "refusing to run" in result.stderr, result.stderr


def test_the_documented_invocation_still_works(tmp_path: Path) -> None:
    """The control: the fix must not make the scripts unrunnable.

    `python -m scripts.<name>` from the repo root puts the repo root on `sys.path`, so it resolves
    the intended tree and must proceed. A guard that only ever refuses is not a fix.
    """
    # Initialise the probe database first, with a real (non-dry) run. A dry run against an empty
    # data directory legitimately exits 2, because `init_db` builds the schema - that is the
    # separate dry-run check doing its job, and conflating the two would have had this control
    # assert the wrong thing.
    init = _run(
        [sys.executable, "-m", "scripts.recalculate_backtest_stats", "--skip-json"], tmp_path
    )
    assert init.returncode == 0, init.stdout + init.stderr

    result = _run(
        [sys.executable, "-m", "scripts.recalculate_backtest_stats", "--dry-run"], tmp_path
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "refusing to run" not in result.stderr, result.stderr
    assert "Dry run: yes" in result.stdout, result.stdout
