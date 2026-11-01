# Pre-Mortem Report — fix-jpype-shutdown-deadlock (PR-119 scope)

**Scope:** CHANGED production code on branch `fix/jpype-shutdown-deadlock` vs `origin/main`, restricted to:
`data_tools/downloaders/jforex_dl.py` (the import-time `_jpype_config.onexit = False` block, lines 58–78),
`viewer/main.py` (`_DaemonThreadPoolExecutor` lines 399–443, the `_BACKGROUND_PREWARM_EXECUTOR`
singleton 448–450, `_run_background_job` 453–468, `_run_background_job_async` 471–472, the rerouted
prewarm coroutines 494–538, and the `lifespan` reroute at 1137–1139).
**Date:** 2026-02-14
**Method:** read the PR diff plus the full surrounding regions of both files, the JPype start/prewarm
path (`_ensure_jvm_started` 743–784, `prewarm_jvm` 824–832), the lifespan teardown 1168–1200, the
prewarm bodies (`_prewarm_data_views_once` 7968–7981, `_refresh_backtest_meta_cache` 3048–3071,
`_hydrate_task_cache_once` 4280–4293), and the new `tests/test_jpype_shutdown.py`. No code edited;
nothing here is observed failing today — every item below is a **fictional** future incident that one
small, review-passable edit would detonate.

## Summary

Five post-mortems below. The PR's dominant theme is **shutdown correctness bought with global and
private-API coupling**. Three of the five fragilities are not about the fix being wrong — they are
about the fix being *unobservable*: the process now exits in a state (`JPype onexit` off, daemon
workers killed mid-flight) that no test or log distinguishes from a clean one, so the next developer
edits the surface without the evidence that would tell them they broke it. The two sharpest items are
PM1 (a single import-time line in one *downloader* silently disables JVM teardown for the **entire**
process, including any future code that genuinely needs `shutdownJVM` for lock/resource release) and
PM2 (the class re-implements CPython `ThreadPoolExecutor` private internals with no guard, no version
pin in code, and a comment as its only defence against a patch-release internals change). PM3 and PM4
cover the "daemon threads are the correct trade-off" claim: it is correct for caches, and was written
as if all workers were caches, but the pool is a shared singleton that the next caller may hand a
*durable* job. PM5 covers the fact that the JVM is now never shut down at all, making the first
future caller of `shutdownJVM` (or any code that assumes process exit is orderly) a latent hazard.

## Post-Mortems

### 1. A new JVM-using module silently gets teardown disabled by importing the downloader

**Severity:** High
**Component:** `data_tools/downloaders/jforex_dl.py:77-78` (import-time `_jpype_config.onexit = False`); consumers anywhere that `import jpype`
**Fragility type:** global mutation with cross-module reach

#### What happened

A second JPype entry point was added to the platform — a standalone FX tick-replay tool that started
its own JVM, wrote its output through a JVM-finalizer-backed resource (a native library holding a
replay cache file with a lock, and an SDK connection object whose `close()` runs during JVM
teardown), and relied on `shutdownJVM()`/`atexit` to release that lock. Because the tool shared a
utility import chain that reached `data_tools.downloaders.jforex_dl`, importing the tool had already
set `onexit = False` process-wide *before* the tool started its JVM. The process exited 0 with the
JVM reclaimed by the OS, the lock file was never released, and the next run of the same tool hung
waiting on a lock held by a dead process. A CI job pinned for 30 minutes on the second run.

#### The change that caused it

A developer added `from data_tools.downloaders.jforex_dl import JAVA_LONG_MAX` (or a similar
"obviously harmless" constant/helper import) to the new module's header, to avoid duplicating a
constant. That single import line executed the top-level `if _jpype_config is not None:
_jpype_config.onexit = False` in the *importer's* process. Nothing in the new module mentioned
JPype teardown, and nothing in the downloader signalled that importing it has a process-global side
effect — the comment explains *why* onexit is lowered, but never says *"this runs on import and
affects every JPype consumer in this process."*

#### Why it broke

`jpype.config` is a module-global configuration object consulted by `_JTerminate` and
`shutdownJVM`. Lowering `onexit` at import time is a **process-wide, irreversible, order-dependent
side effect** of importing one downloader. The failure is entirely invisible in the module that
caused it: it imports cleanly, its own tests pass, and its JVM-start assertions
(`jpype.isJVMStarted()`) still pass because `onexit` has nothing to do with *starting* a JVM — only
with tearing one down. The new `test_jforex_dl_disables_jpype_onexit_at_import`
(`tests/test_jpype_shutdown.py:72-86`) *pins* this global mutation as desired behaviour, so it would
actively resist a future fix that moved the assignment inside the JForex code path where it belongs.

#### How it was caught

A stale-lock hang on the *second* invocation of the new tool; the investigation found no writer,
only a `share.lock` owned by the (long-exited) previous process. Bisect pointed at the innocuous
import. No test failed — a test for the new tool in isolation would never exercise the *importer's*
`onexit` state, and the downloader's test asserts the flag is off.

#### Hardening suggestions

1. Move the assignment off import and into the path that actually starts the JForex JVM —
   `_ensure_jvm_started` (jforex_dl.py:743) immediately before `startJVM` — so the flag is lowered
   only when this process is *actually* about to run JForex. Guard the test on
   `_ensure_jvm_started()` having been called.
2. If the import-time form is kept for the CI hang, make the side effect loud and documented at the
   call site: a module docstring note plus a `logger.debug`/comment phrase like "PROCESS-GLOBAL: any
   JPype consumer in this interpreter will no longer run its exit hook", and a lint/`noqa`-style
   marker so a reviewer sees it on the import line.
3. Provide an explicit opt-in (`jforex_dl.DISABLE_JPYPE_ONEXIT = True` or an env var) so a
   consumer that needs `shutdownJVM` can keep it, instead of the flip being unconditional.

### 2. A Python patch upgrade changes `ThreadPoolExecutor` internals and the daemon pool stops spawning workers

**Severity:** High
**Component:** `viewer/main.py:420-442` (`_DaemonThreadPoolExecutor._adjust_thread_count`, mirroring `concurrent.futures.thread` privates)
**Fragility type:** mirrored CPython internals / coincidental correctness

#### What happened

After a base-image refresh moved the viewer from CPython 3.12.x to 3.13, every fire-and-forget
startup prewarm stopped running. Startup logs showed none of the "Data views prewarm / Backtest
metadata cache warmed / Task cache warmed" lines, the viewer ran cold (first request served a full
database scan) for weeks, and load tests showed multi-second first-hit latency. Nothing raised:
`_adjust_thread_count` simply never created a worker, so `_BACKGROUND_PREWARM_EXECUTOR` sat empty
and every `run_in_executor` future stayed pending.

#### The change that caused it

The class re-implements CPython's private `_adjust_thread_count` to flip one flag (daemon=True). The
upstream implementation was refactored (thread creation moved behind a helper/`_threads` handling
changed), so the hand-copied body no longer matches: the `if self._idle_semaphore.acquire(...)` /
`num_threads < self._max_workers` bookkeeping disagrees with the new contract and the mirror
silently short-circuits. The docstring says the mirror "is validated against CPython 3.12 … would
need re-checking before widening that range" (main.py:414-417), but nothing in code enforces 3.12:
no version assert, no runtime check of `sys.version_info`, no test that fails on an unsupported
interpreter. The interpreter bump was a base-image edit made by someone who never read `viewer/main.py`.

#### Why it broke

Overriding a *private* method to change one attribute couples the class to a specific CPython
implementation. The mirror is only correct **coincidentally** — it holds as long as CPython's
`_worker` signature, `_threads` container semantics, `_idle_semaphore` accounting and weakref
callback contract stay byte-compatible. Two failure modes are available and both are silent:
(i) worker never spawns (work is never done, look-and-feel of a cold cache); (ii) worker spawns but
bookkeeping drifts (threads leak, or `_max_workers` is exceeded). Neither raises, and the "warmed"
log lines are the *only* evidence, so a version bump that breaks the mirror looks like a
performance regression, not a bug.

#### How it was caught

A latency investigation into cold-start times, traced back to missing prewarm log lines and an
empty executor; the mirror was bisected against the interpreter version. The new tests
(`tests/test_jpype_shutdown.py`) assert only exit behaviour, not that the pool ever executes work —
so no test failed.

#### Hardening suggestions

1. Add a fail-fast guard at class definition or first use: assert `sys.version_info[:2] == (3, 12)`
   (or a supported set) with a loud message naming `_DaemonThreadPoolExecutor._adjust_thread_count`,
   so an interpreter bump breaks the build instead of silently disabling prewarms.
2. Prefer the *supported* mechanism over the mirror: call `super()._adjust_thread_count()` once to
   let upstream create the worker, then flip `thread.daemon = True` on `self._threads` (or use a
   `threading.Thread` factory / `concurrent.futures.thread` monkeypatch confined to the pool) — any
   approach that stops re-implementing the whole method body.
3. Add a test that submits a job to `_BACKGROUND_PREWARM_EXECUTOR`, asserts it runs and that
   `thread.daemon is True`, and fails if no worker is created. That converts the silent
   never-spawns failure into a red test.

### 3. A durable write is routed onto the daemon pool and is killed mid-transaction

**Severity:** High
**Component:** `viewer/main.py:448-450` (`_BACKGROUND_PREWARM_EXECUTOR`, max_workers=4, shared singleton); `_run_background_job` 453-468; daemon workers 430-442
**Fragility type:** load-bearing assumption ("these jobs are fire-and-forget")

#### What happened

A developer needed to move a startup step off the event loop and reused the obvious helper,
`await _run_background_job(_rebuild_some_index)`. That step was *not* a cache warm — it wrote a
derived table into SQLite (an index/aggregate rebuild) that other code treated as authoritative.
When the process shut down during the rebuild (a `TestClient` lifespan exit in the test suite, or a
uvicorn drain), the daemon worker was killed at an arbitrary instruction: `sqlite3` was interrupted
mid-transaction, leaving either a partially-built table or a rolled-back write combined with an
in-memory "done" flag. A later request read the *partial* table and served wrong aggregates. The
"Backtest equity preview" surfaced stale/missing rows for a subset of ids, and a report built on it
was wrong.

#### The change that caused it

The class docstring justifies the daemon trade-off explicitly — "a half-finished warm only means a
cold cache" (main.py:406-409). That justification is scoped to *prewarms*, but the pool it justifies
is a **module-level shared singleton** any caller can reach via `_run_background_job`. A developer
who trusts the helper name and the docstring ("fire-and-forget startup prewarms") routes a job that
actually mutates persistent state. The reroute in `lifespan` (main.py:1137-1139) and the prewarm
coroutines (494-538) established the pattern; the next reader copies it.

#### Why it broke

Daemon threads get no teardown guarantee — the interpreter does not join them, does not run their
`finally` blocks reliably at exit, and does not wait for OS resources they hold. That is *safe* only
for work whose partial completion is equivalent to never having started. `_run_background_job`'s
signature (`coro_factory` — an untyped callable) and its return (an `asyncio.Future`) carry no
indication that the pool is teardown-unsafe, and there is no naming or type barrier separating
"cache warm, safe to truncate" from "durable write, must complete". The old code path
(`asyncio.to_thread`) at least joined at loop close, so a regression here is a *silent* loss of a
guarantee the previous implementation accidentally provided.

#### How it was caught

Bad aggregates in a downstream report; the table contained a partial row set and a `journal`
indicating an interrupted transaction. Root cause found by grepping for `_run_background_job` call
sites and asking whether each was idempotent+cache-only. No test asserts that a job on the daemon
pool completes before interpreter exit (that would contradict the intent), so nothing went red.

#### Hardening suggestions

1. Rename and narrow the surface: `_run_prewarm_job(CACHE_ONLY)` and a docstring that says
   *"durable writes, file writes, and any job whose `finally` must run belong on
   `asyncio.to_thread`/the loop's default executor, never here."* Add a matching "do not use for
   durable work" note at every call site in the prewarm block.
2. Make the pool private-by-typing: accept a typed `PrewarmJob` (a small wrapper whose only
   constructors are the known prewarm functions), so a new caller cannot pass an arbitrary callable
   without a review-visible change to the wrapper type.
3. Keep a registry of the known-safe prewarm jobs and log at warning if `_run_background_job` is
   called with a callable not in it, so the pattern-matching copy is visible in review/logs.

### 4. A prewarm task's cancel path changes future/return semantics and silently drops the result

**Severity:** Medium
**Component:** `viewer/main.py:466-468` (`_run_background_job` returns a `Future`, not the awaited value); `_run_background_job_async` 471-472; callers at 519, 527, 535 that read the result
**Fragility type:** changed return semantics / invisible invariants

#### What happened

A developer "cleaned up" the prewarm coroutines by dropping `_run_background_job_async` and awaiting
the future directly, or by changing `_run_background_job` to return the *value* (awaited inline).
The result was that `_run_background_job` became an `async` function that awaited its own future,
which changed cancellation semantics: when `lifespan` cancels the prewarm task at teardown
(main.py:1171-1196), the cancellation was now delivered to the *event-loop task* waiting on the
future rather than leaving the future running on the daemon worker. In the JSON→SQLite migration
case specifically, cancellation aborted the coroutine *wrapper* while the daemon worker kept
running — but a follow-up edit that cancelled the future itself left `_JSON_MIGRATION_STATUS =
"running"` (jforex_dl-adjacent global at main.py:480) with no task to ever flip it to
`completed`/`failed`. Subsequent calls to `_run_json_to_sqlite_migration` returned immediately
(`if _JSON_MIGRATION_STATUS in {"running", "completed"}: return`) and the migration never finished;
the viewer ran against the legacy JSON store indefinitely.

#### The change that caused it

Someone noticed that `_run_background_job_async(coro_factory)` is a thin `await`-wrapper around
`_run_background_job` and "removed the redundant indirection", collapsing the two into one function
that awaits the future internally. That is a natural simplification — the two-function split looks
like leftover scaffolding. It changes the type of what `_run_background_job` returns from
`Future` to `coroutine[Any, Any, T]`, and therefore *where* cancellation lands, and it quietly breaks
the invariant that the `_JSON_MIGRATION_STATUS` state machine is only advanced by a worker that runs
to completion or to an exception — cancellation is a third terminal state (neither "completed" nor
"failed") that the state machine does not model.

#### Why it broke

The PR deliberately splits "schedule on the pool" (sync-ish, returns a `Future`) from "await it"
(async). Nothing in the code names that distinction or asserts it, so it reads as removable
duplication. `_run_json_to_sqlite_migration`'s status machine (475-492) models exactly two terminal
outcomes from an exception/progress standpoint and treats `running` as sticky. A cancelled worker
leaves `running` sticky forever. The cancellation in `lifespan` uses `task.cancel()` then
`await task` under `suppress(CancelledError)` (1182-1196) — it is designed to abandon in-flight
prewarms, which is fine for caches but interacts badly with any state machine that assumes forward
progress.

#### How it was caught

Data inconsistency between the JSON store and SQLite after a deploy; the migration status was
`running` with no live task. Found by inspecting the in-memory status and the absence of
`Startup JSON→SQLite migration finished`. No test exercised lifespan cancellation of an in-flight
migration.

#### Hardening suggestions

1. Keep the two functions and name the distinction in the docstring: `_run_background_job` returns
   the `Future` (do not await here); `_run_background_job_async` awaits. Say explicitly that
   collapsing them changes cancellation semantics.
2. Make `_run_json_to_sqlite_migration` cancellation-aware: on `except BaseException` /
   `CancelledError`, set `_JSON_MIGRATION_STATUS = "cancelled"` (a new state) or leave it `pending`
   so the next process retries, instead of leaving `running` sticky.
3. Add a test that cancels an in-flight migration and asserts a subsequent invocation retries
   rather than no-ops.

### 5. The JVM is never shut down, so the first code that needs an orderly teardown is a latent hazard

**Severity:** Medium
**Component:** `data_tools/downloaders/jforex_dl.py:67-78` (`onexit = False`, no compensating shutdown); `jpype.shutdownJVM()` call sites (none exist today)
**Fragility type:** silent no-op / resource-leak-by-design

#### What happened

A future feature — a long-running JForex download worker or a CLI that runs a batch and must exit
cleanly under a process supervisor — needed to release JVM-held resources at process end (the
Dukascopy SDK's socket pool, a JVM-side file handle). The developer added
`jpype.shutdownJVM()` to the shutdown path, reasoning "we're done, close the JVM." It hit the *same*
`Threads::destroy_vm()` deadlock the PR was written to avoid — because once the JVM has run with
threads, a controlled `shutdownJVM()` deadlocks exactly like the exit hook (jforex_dl.py:67-71). The
supervisor's SIGTERM timeout fired and the batch job was SIGKILLed, corrupting an in-progress output
file. The regression came from a *reasonable* addition that the PR's own comment warns against, but
that warning lives only in a code comment in a different file from the new caller.

#### The change that caused it

No change to this PR's code — a *new* caller added `jpype.shutdownJVM()` (or a worker relied on
`atexit` ordering) because the JVM is now never closed and someone noticed the missing teardown. The
PR documents the hazard at jforex_dl.py:67-72 and cites the JPype source warning, but exposes no API
that makes the correct behaviour (`do not call shutdownJVM; let the OS reclaim`) enforceable. The
docstring's claim "the JVM is left for the OS to reclaim at process exit" (tests/test_jpype_shutdown.py:11-13)
reads like an accepted design; the *reason it must never be manually shut down* is a comment.

#### Why it broke

Removing the exit hook and declining to call `shutdownJVM` is a correct fix for the deadlock, but it
converts "JVM teardown is broken" into "JVM teardown does not happen". That is fine while the JVM
owns nothing that outlives the process. The moment a future caller needs orderly teardown (a
supervised worker, a test asserting no JVM threads leak, a `multiprocessing` fork after JVM start),
the safe-looking remedy (`shutdownJVM()`) is exactly the deadlock. Nothing in code prevents it, and
no test asserts the *absence* of a `shutdownJVM()` call, so the guard is a comment only.

#### How it was caught

A supervised batch job timed out on SIGTERM and was SIGKILLed; the log stopped at the new
`shutdownJVM()` call. Root cause identified by reading the PR comment the new author hadn't seen.

#### Hardening suggestions

1. Put the invariant in code, not a comment: provide `jforex_dl.shutdown_jvm_or_abandon()` (or a
   module-level documented helper) that asserts/logs "do not call shutdownJVM after threads have
   run" and does nothing, and reference it wherever a future author might reach for `shutdownJVM`.
2. Add a test/guard that fails if `jpype.shutdownJVM` is called anywhere while a JVM this process
   started is running (e.g. wrap it in a helper that raises with a pointer to the deadlock comment).
3. Document in the JForex module docstring (top of file) — not only inline — that the JVM is
   intentionally never torn down and why, so a new consumer reading the module's public surface
   sees it.

## Themes and Recommendations

**T1 — Global state changed at import.** PM1 is the sharp end: `jforex_dl` mutating
`jpype.config.onexit` at import time is a process-global, irreversible side effect reachable by any
`import`, and the new test *pins* it. Structural fix: lower the flag inside `_ensure_jvm_started`
(the only place a JForex JVM actually starts) rather than at module import, so the blast radius is
the code that actually runs JForex. Keep an explicit escape hatch for consumers that need
`shutdownJVM`.

**T2 — Re-implemented CPython privates with a comment as the only guard.** PM2: the daemon executor
mirrors `_adjust_thread_count`, `_worker`, `_threads`, `_idle_semaphore` and the weakref callback.
The "validated against 3.12" claim is prose; there is no version assertion, no fail-fast, and no test
that a worker ever spawns. Structural fix: stop mirroring the body — delegate to `super()` and flip
`daemon` on the created thread — and add a version assertion plus a "job actually runs and is a
daemon" test.

**T3 — "Fire-and-forget" is an assumption about the *job*, enforced only by a docstring.** PM3/PM4:
the daemon pool is a shared singleton with an untyped callable parameter, so any future caller can
route durable or state-machine-advancing work onto threads that are killed at arbitrary points.
Structural fix: type/name the pool (`_run_prewarm_job`, typed jobs), keep a registry of known-safe
jobs, and say at the helper that durable writes and `finally`-must-run work belong on the default
executor.

**T4 — Two terminal states modelled for a job that can also be cancelled.** PM4: the
`_JSON_MIGRATION_STATUS` machine (main.py:475-492) treats `running` as sticky; lifespan cancellation
(1171-1196) can abandon a worker mid-flight and leave a permanent `running`. Structural fix: model a
`cancelled`/retryable state and make cancellation of in-flight prewarms observable (log + status),
so "the prewarm was cut off" is distinguishable from "the prewarm is still going".

**T5 — Shutdown correctness is unobservable.** The fix makes the process exit in a state that no log
or assertion distinguishes from a clean shutdown: JPype onexit is off (no exit hook runs), daemon
workers die silently, and the JVM is reclaimed rather than closed. Every fragility above is easier to
introduce *because* the correct behaviour and the broken behaviour look the same at runtime.
Recommendation: add cheap observability — at prewarm start/finish, log job name and thread identity;
at interpreter exit, log that the JVM is being abandoned by design; assert the unsupported-Python
case loudly. The next developer editing this surface should see the invariant in logs and types, not
only in comments.
