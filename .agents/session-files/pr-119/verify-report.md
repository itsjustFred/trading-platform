# PR Verify Pipeline — PR #119 (`fix/jpype-shutdown-deadlock`)

**Scope:** PR #119 vs `main`
**Date:** 2026-10-30
**Pipeline:** `.agents/skills/platform-pr-verify/SKILL.md`
**Reviewers:** SonarQube Cloud (GitHub PR comment), mutation testing, pre-mortem,
CodeRabbit CLI, Matt Pocock `code-review`, plus an independent GLM 5.3 Flash
reviewer (via OpenCode, free tier — the paid tier's credits were exhausted).

## 1. SonarQube Cloud (GitHub PR comment)

**Gate: FAILED** — `new_maintainability_rating = 4` (232 new code smells),
`new_security_rating = 2` (3 MINOR S5145 log-injection).

**Assessment: acceptable failure — no finding is inside this PR.**

All 247 open issues were mapped by hand against the PR's changed line ranges
(`git diff -U0`). **0 of 247 fall inside the diff.** They live on pre-existing
`viewer/main.py` lines (e.g. 197, 805, 9855, 1201) inherited from the
`perma-viewer` lineage, and Sonar counts the whole file as "new" because it is
new relative to `main`. Verified the flagged lines exist on `main` too.

The 3 S5145 "log injection" flags are on lines already wrapped in the module's
`_log_safe()` helper and are not in the diff. The 1 BLOCKER (`S8414`,
`add_middleware` order) is at `viewer/main.py:1201`, also not in the diff.

`main` is **not branch-protected** (`gh api repos/.../branches/main/protection`
→ 404 "Branch not protected"), so the red gate is informational, not a merge
blocker.

## 2. Mutation testing

Harness: `.claude/skills/honnibal-mutation-testing`. Scope: the PR's changed
production logic.

| # | Mutation | Result |
|---|----------|--------|
| 1 | `onexit = False` → `True` (core fix disabled) | **Killed** |
| 2 | Delete the `_jpype_config` guard | **Killed** |
| 3 | `daemon=True` → `False` | **Killed** |
| 4 | Route prewarms back at the default executor | **Killed** |
| 5 | `max_workers` 4 → 1 | Survived (non-load-bearing) |
| 6 | Start branch drops `_disable_jpype_onexit()` (round 2) | **Killed** |
| 7 | Already-started branch drops it (round 2) | **Killed** |
| 8 | Drop the `sys.version_info` assert | Survived (diagnostic only) |

**Score: 6 / 8 killed.** Mutations 1–2 only kill with the real JPype/JDK env;
the JDK-gated subprocess tests skip without a JDK, which is documented in the
test docstring. Full report at
`.agents/session-files/pr-119/mutation-testing-report.md`.

## 3. Pre-mortem

Report: `.agents/session-files/pr-119/pre-mortem-report.md` (5 post-mortems).
Three actuated fixes:

* import-time global mutation of `jpype.config.onexit` → moved into
  `_ensure_jvm_started` (plus the already-started branch);
* the mirrored-CPython-internals risk → documented + a fail-fast
  `sys.version_info[:2] == (3, 12)` assert;
* the "never call `shutdownJVM`" invariant being comment-only → pinned by a
  source-scanning test.

## 4. Code review (Matt Pocock `code-review`, fixed point `main`)

**No material findings.** 2 Standards + 3 Spec items, all low/medium judgement
calls. Two were actioned (one-shot contract enforcement; docstring accuracy).
The reviewer's "worker-name duplication" claim was checked against the installed
CPython 3.12.3 source and is **wrong** — the override matches upstream exactly.

## 5. CodeRabbit CLI

Findings from the first run were all actioned: arm the guard on JVM reuse; arm it
after classpath resolution and restore on total failure; move the contract tests
into subprocesses (they were mutating `jpype.config.onexit` in the pytest
session — which caused a real CI failure, now fixed).

## 6. Independent reviewer — GLM 5.3 Flash (via OpenCode)

Round 1: NOT CONFIDENT (four must-fixes). All were evaluated:

* Sonar gate red → accepted with the verified issue/hunk mapping above;
* executor docstring causal model → corrected (both `daemon` and the
  `_threads_queues` omission are load-bearing);
* `onexit` at import → moved to the start path;
* migration on the daemon pool → investigated; `migrate_json_to_sqlite(
  skip_existing=True)` is documented idempotent and resumes per file, so the
  placement is safe. The reviewer's own follow-up (arm the guard when a JVM is
  already running) was implemented.

Round 3 (final state): **CONFIDENT — MUST-FIX: none.**

## CI

`Python ubuntu-latest` **passes** (2m36s, was a 60-minute timeout). macos,
windows and frontend all pass. The only red check is the SonarCloud baseline
above.

## Verdict

Pipeline complete. No relevant finding outstanding that is inside this PR. The
one red check is a pre-existing baseline artifact outside the diff, on a branch
that is not protected.
