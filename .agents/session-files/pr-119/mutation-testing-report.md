# Mutation Testing Report — PR #119 (`fix/jpype-shutdown-deadlock`)

**Scope:** changed production code of PR #119 vs `main`:
`data_tools/downloaders/jforex_dl.py` (the `jpype.config.onexit = False` guard),
`viewer/main.py` (`_DaemonThreadPoolExecutor`, `_run_background_job`,
`_run_background_job_async`, and the rerouted lifespan prewarms).
**Date:** 2026-10-30
**Method:** one mutation applied at a time, targeted tests run, mutation reverted,
`git diff` confirmed clean before the next. Tests: `tests/test_jpype_shutdown.py`
and `tests/test_viewer_shutdown_nonblocking.py`.

## Result table

| # | File | Mutation | Result | Diagnostic | Notes |
|---|------|----------|--------|------------|-------|
| 1 | `data_tools/downloaders/jforex_dl.py` | `onexit = False` → `True` (core fix disabled) | **Killed** | Clear | JDK-gated subprocess test aborts/hangs at exit |
| 2 | `data_tools/downloaders/jforex_dl.py` | Delete the `_jpype_config` guard block | **Killed** | Clear | Import assertion + subprocess exit test |
| 3 | `viewer/main.py` | `daemon=True` → `False` in `_adjust_thread_count` | **Killed** | Clear | In-flight prewarm pins `TestClient` lifespan exit |
| 4 | `viewer/main.py` | `run_in_executor(_BACKGROUND_PREWARM_EXECUTOR, …)` → default executor | **Killed** | Clear | Lifespan shutdown blocks join |
| 5 | `viewer/main.py` | prewarm pool `max_workers=4` → `1` | Survived | — | Worker count is not behaviourally load-bearing for the daemon/join contract |

**Mutation score: 4 / 5 killed (80%).**

## Notes on methodology

Mutations 1 and 2 initially **survived** when the tests ran in an environment without
`jpype` installed (the worktree's first `uv run` env lacked the optional extras): the
JDK-gated subprocess tests are `skipif(_java_available())`, so with no usable JVM they
skip and the guards are unobserved. After installing the project's optional extras into
the worktree env (`uv sync --frozen --all-extras --no-install-project`), which resolves
`jpype==1.7.1` and `jpype.getDefaultJVMPath()`, mutations 1 and 2 are **killed**.

This is the expected behaviour of the skip guard (documented in the test docstring): the
guarded failure is a hang in the *exit* path, which cannot occur when no JVM ever starts.
CI installs the extras and has a JDK, so the coverage is real there (`2663 passed`, no
skips among the JPype tests).

## Survived mutations

**M5 — prewarm pool `max_workers` 4 → 1.** No test binds the worker count. This is
acceptable: the fixed contract is that prewarm workers are *daemons* and are *not joined*
at loop/process teardown — both of which M3/M4 kill. A single worker would still satisfy
it; the count is a throughput choice, not a correctness invariant. No test recommended.

## Diagnostic quality

All killed mutations were **Clear**: the subprocess return code (`-6` / non-zero) and the
absence of the sentinel print immediately localise the failure.

## Recommended tests

None. The 4/5 killed set covers every load-bearing behaviour introduced by the PR
(the `onexit` guard, the import-time wiring, the daemon flag, and the executor routing).

---

## Round 2 (after review hardening)

The `onexit` guard moved out of import into
`jforex_dl._disable_jpype_onexit()`, called from `_ensure_jvm_started` (both the
start and already-started branches). Re-verified the equivalents of M1/M2
against the new shape:

| # | File | Mutation | Result | Diagnostic |
|---|------|----------|--------|------------|
| 6 | `data_tools/downloaders/jforex_dl.py` | Remove `_disable_jpype_onexit()` from the start branch | **Killed** | Clear — `test_ensure_jvm_started_disables_onexit_before_start` |
| 7 | `data_tools/downloaders/jforex_dl.py` | Remove `_disable_jpype_onexit()` from the already-started branch | **Killed** | Clear — same test, reuse-path assertion |
| 8 | `viewer/main.py` | Drop the `sys.version_info` guard in `__init__` | Survived | — no test; guard is a loud-failure aid, not behaviour |

Finding 8 is expected: the version guard is a diagnostic, not a behavioural
contract, so nothing pins it. It is covered by inspection and by the import
smoke test (`Lint packaging and import seams` in CI executes the module).

Updated score for the changed logic: 6 / 8 killed; the two survivors are
non-load-bearing (`max_workers` count and the version assert).
