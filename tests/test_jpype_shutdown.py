"""Regression: a JPype-started JVM must not deadlock interpreter exit.

JPype registers ``atexit.register(_JTerminate)`` at import. That hook calls
``_jpype.shutdown(destroy_jvm, False)`` from *inside* ``Py_FinalizeEx``, and by
then HotSpot's ``Threads::destroy_vm()`` blocks forever on the Threads lock
waiting for Java threads (JPype's own "Python Reference Queue" among them) that
can no longer make progress. The interpreter therefore never exits: the whole
``pytest`` run hangs after printing its summary and the CI ``Python
ubuntu-latest`` job burns its 60-minute timeout.

``data_tools.downloaders.jforex_dl`` disables ``jpype.config.onexit`` at import
so JPype stays out of interpreter teardown; the JVM is left for the OS to
reclaim at process exit. These tests pin that contract with real subprocesses,
because the failure only manifests at interpreter exit and cannot be observed
in-process. The subprocess tests are skipped without a usable JDK: the guard is
a hang in the *exit* path, which cannot occur when no JVM ever starts.
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


def test_jforex_dl_disables_jpype_onexit_at_import():
    """Importing the downloader must keep JPype out of interpreter teardown.

    ``onexit`` defaults to True in ``jpype/config.py``; the module lowers it on
    import. Assert through the module's own ``jpype`` handle so the check fails
    if the wiring is dropped, and confirm that handle really is the global
    instance ``_JTerminate``/``shutdownJVM`` consult.
    """
    jpype_config = pytest.importorskip("jpype.config")

    from data_tools.downloaders import jforex_dl

    assert jforex_dl.jpype is not None
    assert jforex_dl.jpype.config is jpype_config
    assert jforex_dl.jpype.config.onexit is False


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
