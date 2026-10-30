"""Regression: a JPype-started JVM must not deadlock interpreter exit.

JPype registers ``atexit.register(_JTerminate)`` at import. That hook calls
``_jpype.shutdown(destroy_jvm, False)`` from *inside* ``Py_FinalizeEx``, and by
then HotSpot's ``Threads::destroy_vm()`` blocks forever on the Threads lock
waiting for Java threads (JPype's own "Python Reference Queue" among them) that
can no longer make progress. The interpreter therefore never exits: the whole
``pytest`` run hangs after printing its summary and the CI ``Python
ubuntu-latest`` job burns its 60-minute timeout.

``data_tools.downloaders.jforex_dl`` disables ``jpype.config.onexit`` at the
point it starts a JVM (``_ensure_jvm_started``) so JPype stays out of
interpreter teardown; the JVM is left for the OS to reclaim at process exit.
These tests pin that contract with real subprocesses, because the failure only
manifests at interpreter exit and cannot be observed in-process. The subprocess
tests are skipped without a usable JDK: the guard is a hang in the *exit* path,
which cannot occur when no JVM ever starts.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = str(Path(__file__).parent.parent)

# A process that started a JVM should exit almost immediately after its work.
# The bound only has to sit well below the JPype deadlock (which is unbounded)
# and above a cold JVM start on a slow runner.
EXIT_BUDGET_SECONDS = 60


def _java_available() -> bool:
    if shutil.which("java") is None:
        return False
    try:
        import jpype  # noqa: F401

        jpype.getDefaultJVMPath()
    except Exception:
        return False
    return True


requires_java = pytest.mark.skipif(
    not _java_available(), reason="no usable JVM/JDK available for a JPype start"
)


def _run(script: str) -> tuple[subprocess.CompletedProcess, float]:
    """Run ``script`` in a fresh interpreter and report its result and runtime.

    ``{repo}`` in the script is substituted with the repo root so the
    subprocess imports the checkout under test rather than an installed copy.
    """
    wrapped = textwrap.dedent(script).format(repo=REPO_ROOT)
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", wrapped],
        capture_output=True,
        text=True,
        timeout=EXIT_BUDGET_SECONDS + 60,
    )
    return result, time.monotonic() - started


def test_import_does_not_mutate_jpype_onexit():
    """Importing the downloader must leave the process-global flag untouched.

    ``onexit`` defaults to True in ``jpype/config.py``; the module must not lower
    it at import, so importing the downloader for a constant cannot strip
    teardown from an unrelated JPype consumer in the same process. Runs in a
    subprocess so it neither reads nor mutates the pytest session's state.
    """
    result, _ = _run(
        """
        import sys
        sys.path.insert(0, {repo!r})
        import jpype.config
        from data_tools.downloaders import jforex_dl

        assert jforex_dl.jpype is not None
        assert jforex_dl.jpype.config is jpype.config
        assert jpype.config.onexit is True, "import must not lower onexit"
        print("IMPORT_LEFT_ONEXIT_TRUE")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "IMPORT_LEFT_ONEXIT_TRUE" in result.stdout, result.stderr


@requires_java
def test_ensure_jvm_started_disables_onexit_before_start():
    """Starting a JVM through the module must arm the guard; a started JVM too.

    Covers both branches of ``_ensure_jvm_started``: the start path arms
    ``onexit`` before ``startJVM``, and the already-started path arms it when a
    JVM is reused. In both cases the interpreter must then exit cleanly.
    """
    result, elapsed = _run(
        """
        import sys
        sys.path.insert(0, {repo!r})
        import jpype
        import jpype.config
        from data_tools.downloaders import jforex_dl

        # Start path: the guard must be armed before startJVM runs.
        jforex_dl._ensure_jvm_started()
        assert jpype.isJVMStarted(), "JVM did not start; test is vacuous"
        assert jpype.config.onexit is False, "start path left onexit enabled"

        # Already-started path: a second call must keep the guard armed.
        jpype.config.onexit = True
        jforex_dl._ensure_jvm_started()
        assert jpype.config.onexit is False, "reuse path left onexit enabled"
        print("ONEXIT_ARMED_ON_BOTH_PATHS")
        """
    )
    assert result.returncode == 0, (
        f"process did not exit cleanly (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ONEXIT_ARMED_ON_BOTH_PATHS" in result.stdout, result.stderr
    assert elapsed < EXIT_BUDGET_SECONDS, f"process took {elapsed:.1f}s to exit"


def test_production_code_never_calls_jpype_shutdownjvm():
    """Pin the documented "one-shot JVM" contract the ``onexit`` fix depends on.

    With ``onexit = False`` the JVM is deliberately left for the OS to reclaim
    at process exit. JPype cannot restart a JVM in the same interpreter, so any
    ``shutdownJVM()`` call would deadlock (it reaches the same
    ``Threads::destroy_vm()`` path as the removed exit hook) or, worse, leave
    ``_ensure_jvm_started`` retrying ``startJVM`` on a torn-down VM and failing
    with a misleading diagnostics error. The contract is documented in the
    module comment and ``docs/DATA_SOURCES.md``; this asserts no production
    module calls it, so the invariant cannot rot silently.
    """
    repo_root = Path(__file__).parent.parent
    offenders: list[str] = []
    for path in sorted(repo_root.rglob("*.py")):
        rel = path.relative_to(repo_root)
        if rel.parts[0] in {"tests", ".venv", ".git"} or ".venv" in rel.parts:
            continue
        source = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), start=1):
            code = line.split("#", 1)[0]
            if "shutdownJVM" in code:
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "production code must not call jpype.shutdownJVM() (one-shot JVM contract):\n"
        + "\n".join(offenders)
    )


@requires_java
def test_process_with_started_jvm_exits_cleanly_and_promptly():
    """A process that started a JVM must exit 0 quickly, not hang.

    This is the exact failure mode that pinned the CI ``Python ubuntu-latest``
    job for 60 minutes: the tests passed, then the interpreter deadlocked in
    JPype's exit hook.
    """
    result, elapsed = _run(
        """
        import sys
        sys.path.insert(0, {repo!r})
        from data_tools.downloaders import jforex_dl

        jforex_dl._ensure_jvm_started()
        assert jforex_dl.jpype.isJVMStarted(), "JVM did not start; test is vacuous"
        print("JVM_STARTED")
        """
    )

    assert result.returncode == 0, (
        f"process did not exit cleanly (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "JVM_STARTED" in result.stdout, result.stderr
    assert elapsed < EXIT_BUDGET_SECONDS, (
        f"process took {elapsed:.1f}s to exit after starting a JVM"
    )


@requires_java
def test_viewer_lifespan_started_jvm_exits_cleanly():
    """The path the suite takes: the viewer lifespan JForex prewarm.

    The lifespan prewarm starts the JVM through ``_ensure_jvm_started``; the
    subprocess must still exit 0 afterwards. The subprocess fails (rather than
    vacuous-passing) if the JVM never started, so this cannot pass by accident
    on a machine where the prewarm silently no-ops.
    """
    result, elapsed = _run(
        """
        import sys
        sys.path.insert(0, {repo!r})
        from fastapi.testclient import TestClient
        from viewer import main as viewer_main

        with TestClient(viewer_main.app):
            pass

        from data_tools.downloaders import jforex_dl
        assert jforex_dl.JPYPE_AVAILABLE, "jpype missing; test env is incomplete"
        assert jforex_dl.jpype.isJVMStarted(), "lifespan prewarm did not start the JVM"
        print("LIFESPAN_JVM_STARTED")
        """
    )

    assert result.returncode == 0, (
        f"process did not exit cleanly (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "LIFESPAN_JVM_STARTED" in result.stdout, result.stderr
    assert elapsed < EXIT_BUDGET_SECONDS, (
        f"process took {elapsed:.1f}s to exit after the viewer lifespan"
    )
