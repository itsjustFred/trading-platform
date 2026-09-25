# Pre-Mortem Report

**Scope:** code changed on `perma/validation-live-trading-2026-09-25` — `live_runner/daemon.py`, `live_runner/stockkit_routing.py`, `live_runner/signal_sources/discord_source.py`, `live_runner/base_adapter.py`, `live_runner/alpaca_adapter.py`, `live_runner/ctrader_adapter.py`, `shared/database.py`, `runner/optimizer.py`, `runner/optimization_diagnostics.py`, `viewer/main.py`, `viewer/core.py`
**Date:** 2026-09-25

## Summary

Seven post-mortems. The dominant theme is **stringly-typed contracts that now
span multiple modules**: the task-status vocabulary lives in `viewer/core.py`,
`viewer/main.py` and the frontend; the Discord credential scheme is resolved in
both `live_runner/daemon.py` and `live_runner/signal_sources/discord_source.py`;
the timeframe vocabulary has two accepted spellings and a hand-maintained
translation table. Each of these is individually small, but a future edit to one
side silently desynchronises the other. The second theme is **fail-open
reasoning inside fail-closed code** — the STOPPED enforcement path contains a
branch that treats "no adapter" as "nothing to do", which is exactly wrong after
a restart.

One finding during this read is a **real, current bug**, not a hypothetical; it
is reported separately at the end rather than written up as fiction.

## Post-Mortems

### 1. Pausing a task makes the resume button disappear from the API contract

**Severity:** Medium
**Component:** `viewer/core.py::_task_controls_payload`, `viewer/main.py::pause_data_task`
**Fragility type:** Stringly-typed contract / invisible invariant

#### What happened

An operator opened a running data task, clicked Pause, and the row immediately
switched to offering "Cancel" only. The task could not be resumed for the rest of
its life from the UI, and the only recovery was a page reload after the task
happened to be re-listed. Support received three tickets in a day describing it
as "the pause button broke my download".

#### The change that caused it

A developer added a `paused` status to the task state machine and, for
consistency, added `"paused"` to the tuple in `_task_controls_payload`:

```python
"can_pause": supports_control and status in {"starting", "running", "paused"},
```

The motivation was reasonable — "a paused task is under operator control, so the
pause affordance can stay visible". Nothing in the reviewer context mentioned the
endpoint.

#### Why it broke

The two sides of the control contract live in different files. `_task_controls_payload`
in `viewer/core.py` decides what the UI offers; `pause_data_task` in
`viewer/main.py` decides what is accepted. `PAUSABLE_TASK_STATUSES` in
`viewer/main.py` documents that they must stay in step, but the comment is in
one file while the flag literal is in another, so an edit to the literal looks
entirely local. The endpoint's own guard would have rejected the request with a
409, so nothing crashed — the button just quietly did nothing.

#### How it was caught

Not by any existing test. The pre-existing `test_data_tasks_list_and_controls`
only exercised `cancel`. It was caught by a newly written invariant test that
asserts, for each status, that the endpoint's response code matches the advertised
flag. Without that test this would have shipped as a silent UI-only regression.

#### Hardening suggestions

- Derive `PAUSABLE_TASK_STATUSES` from a single shared constant that
  `_task_controls_payload` also consumes, instead of two literals in two modules.
- Add a helper `_pausable_statuses()` in `viewer/core.py` and import it in
  `viewer/main.py`, so the compiler enforces the coupling.
- Keep the invariant test (`test_task_control_flags_agree_with_endpoint_acceptance`)
  and run it whenever either file changes.

### 2. The Discord credential scheme is resolved in two places with different precedence

**Severity:** High
**Component:** `live_runner/daemon.py::_resolve_discord_auth_scheme`, `live_runner/signal_sources/discord_source.py::DiscordSignalSource.__init__`
**Fragility type:** Duplicated logic / invisible invariant

#### What happened

After a routine upgrade, one channel's Discord listener stopped ingesting while
the other two kept working. The daemon log showed `Discord REST returned non-200`
with `status_code: 401` for exactly one bot. The token had not changed and the
channel had not moved. Diagnosis took half a day.

#### The change that caused it

To support a second broker setup, a developer added a per-bot override key:

```python
DISCORD_BOT_NITRO_AUTH_SCHEME=account
```

The intent was "let one bot use an account token". The constructor already read
`DISCORD_AUTH_SCHEME` from `os.environ`; the new per-bot lookup was added in the
daemon only, because that is where the accounts-env mapping lives.

#### Why it broke

`DiscordSignalSource.__init__` resolves the scheme from `os.environ` directly,
while `_resolve_discord_auth_scheme` in the daemon resolves it from the
file-first `accounts_env_values` mapping and passes it explicitly. The daemon path
honours the per-bot key; the standalone-service path
(`_ensure_discord_bot_source`) and any direct construction in a script or test
falls back to `os.environ`, where the key does not exist. The daemon never
mutates `os.environ` — that is a deliberate rule in this codebase — so the two
resolution paths can never agree. Whether a given bot uses an account token
therefore depends on which code path constructed its source.

#### How it was caught

The failure is loud (a 401 on every poll) but indistinguishable from an expired
token, which is what caused the slow diagnosis. No test covered the precedence
order between the per-bot and global keys, and no test asserted that the
constructor's env fallback and the daemon's file-first resolution agree.

#### Hardening suggestions

- Resolve the scheme in exactly one function and have the constructor require it
  as an argument rather than falling back to `os.environ`. Make the parameter
  mandatory so a new call site cannot silently get a different answer.
- If the env fallback must stay for scripts, add a test that constructs the
  source both ways with a per-bot key set and asserts the two agree.
- Log the resolved scheme once per source at construction, so the log states
  `auth_scheme=account` rather than leaving it to be inferred from a 401.

### 3. A paused-task resume guard is bypassed by the restart path

**Severity:** High
**Component:** `viewer/main.py::resume_data_task`, `viewer/core.py::_resume_interrupted_tasks`
**Fragility type:** Implicit ordering dependency / non-atomic compound operation

#### What happened

An operator paused a long backtest, left the tab open, and returned the next
morning. The task was gone from the "active" list and had to be found under
"needs attention". Clicking Resume returned 409, and the only way to continue the
work was to start it again from scratch, losing roughly two hours of download
progress.

#### The change that caused it

A developer tightened the resume endpoint to require that the task actually be
paused, adding:

```python
if str(task.get("status") or "") != "paused":
    raise HTTPException(status_code=409, ...)
```

The motivation was sound: previously `starting` and `queued` tasks could be
promoted to `running`, reporting work as executing when it had not begun.

#### Why it broke

`DATA_TASKS` is in-memory and rebuilt from the `background_tasks` table by
`_resume_interrupted_tasks` at startup. That function reconciles records in
`interrupted` and `queued` only — a task persisted as `paused` is reloaded with
`status="paused"` but its `pause_event` is a fresh, *unset* `asyncio.Event`. The
new guard passes on status, then `task["pause_event"].clear()` is a no-op on an
event that was already clear, and the task thread — which has not been started
for a `paused` record — never runs. The endpoint returns 200 with a payload that
claims the task is running, and nothing happens. The status guard alone is not
sufficient: it validates the *label* without validating that a resumable worker
actually exists.

#### How it was caught

`test_paused_task_resumes_and_runs_to_completion` drives the full journey inside
one process, where the pause event and worker thread are both live, so it passes.
Nothing exercises a restart between pause and resume, which is the actual failure
mode. This is the most likely bug in this report to reach production.

#### Hardening suggestions

- Have `_resume_interrupted_tasks` include `paused` in the statuses it
  reconciles, and make it re-create both the worker and the pause event for those
  records.
- Have `resume_data_task` verify a live worker exists for the task before
  reporting 200; return 409 with a distinct message when the record is
  resumable-but-not-restarted.
- Add a test that writes a `paused` record to the database, simulates a restart
  (rebuild `DATA_TASKS` from storage), and then resumes.

### 4. The StockKit route table can silently drift back to stale account numbers

**Severity:** High
**Component:** `live_runner/stockkit_routing.py::expected_account_number_for_route`, `STOCKKIT_ACCOUNT_ROUTES`
**Fragility type:** Load-bearing default / stringly-typed contract

#### What happened

After Alpaca re-issued paper account credentials, every StockKit signal was
dropped with a single line in the daemon log:
`Blocking misrouted StockKit deployment`. No alert, no failed job, no signal
recorded. The system looked healthy because every task reported success; the
trading simply never happened.

#### The change that caused it

A developer tidied `STOCKKIT_ACCOUNT_ROUTES` by reordering it alphabetically and
moving the constants above `_ALIASES`. The values were untouched.

#### Why it broke

The dict is a fallback consulted only when neither `STOCKKIT_ACCOUNT_NUMBER_<ROUTE>`
nor a matching `ALPACA_ACCOUNT_NAME_ACC{N}` label exists in accounts-env. Its
correctness is entirely load-bearing and completely undocumented: the comment
says only that the values are account numbers. The bug it caused earlier in this
session was that the dict held numbers from a previous set of paper accounts
entirely, and every deployment was rejected because of it. A future edit that
touches the dict — even a reformat — re-exposes the same total-failure mode with
no test that would notice, because the unit tests supply their own env mapping
and never exercise the fallback.

#### How it was caught

Only by comparing the dict against the live configured accounts by hand. No test
fails when the fallback disagrees with the real environment, and no runtime log
distinguishes "no route configured" from "route configured and mismatched".

#### Hardening suggestions

- Log the resolved account number for each route once at daemon start, next to
  the "Discovered N Discord bots" line, so a mismatch is visible without reading
  the code.
- Add a test that asserts every value in `STOCKKIT_ACCOUNT_ROUTES` is a
  syntactically valid Alpaca account number, and document in the module docstring
  that the dict is a fallback that must be kept in sync with accounts-env.
- Consider deleting the fallback and failing closed with an explicit error, so a
  missing mapping is a loud configuration failure rather than a stale number.

### 5. The activation freshness window is compared against a naive UTC clock

**Severity:** Medium
**Component:** `shared/database.py::_activation_block_reason`, `DEMO_VERIFICATION_TTL_SECONDS`
**Fragility type:** Coincidental correctness / version-coupled assumption

#### What happened

After a daylight-saving transition, arming a deployment failed with
`account demo verification is older than 900s; re-verify with the broker` even
though the operator had just completed the verification dialog. The same operator
on a different machine armed the identical deployment without difficulty. The
issue was reported as "the arm button is broken about twice a year".

#### The change that caused it

A developer increased `DEMO_VERIFICATION_TTL_SECONDS` from 900 to 3600 "to reduce
how often operators have to re-verify". This is a pure constant change that passed
review and touched no logic.

#### Why it broke

`_activation_block_reason` computes the age as
`(_utcnow_naive() - verified_at).total_seconds()`. Both values come from
`_utcnow_naive()`, so the comparison is internally consistent and the constant is
not the direct cause — the fragility is that correctness depends on every writer
of `demo_verified_at` and every reader using the same clock and the same naive
UTC convention. `record_live_account_demo_verification` writes `_utcnow_naive()`,
but the column is a naive `DATETIME` and a future developer adding an
Alembic-style migration, a seeding script, or a cross-process writer may well use
`datetime.now()` (local) or an aware UTC value. An aware value compared against a
naive one raises `TypeError`, which surfaces as a 500 on the activation path
rather than a clean 409.

#### How it was caught

No test covers a clock skew, a local-time writer, or an aware timestamp. The
existing tests write and read within the same process, so the coincidence holds.
This is a latent failure that only appears under a DST boundary, a different host
timezone, or a new writer.

#### Hardening suggestions

- Reject aware timestamps at the boundary in `_activation_block_reason` and
  convert them explicitly, rather than letting a `TypeError` escape the
  activation path.
- Add a test that stores an aware UTC `demo_verified_at` and asserts the endpoint
  answers 409 with the re-verify message rather than 500.
- Make `demo_verified_at` timezone-aware end to end, or add a comment recording
  that the column is naive UTC and every writer must use `_utcnow_naive()`.

### 6. Two accepted spellings for every timeframe, hand-maintained

**Severity:** Medium
**Component:** `runner/optimizer.py::_TIMEFRAME_BAR_SECONDS`, `shared/constants.py::normalize_timeframe`
**Fragility type:** Stringly-typed contract / invisible invariant

#### What happened

A new timeframe alias, `2h`, was added to `normalize_timeframe` and began working
for backtests. Optimization reports silently reported
`embargo_bars_equivalent: 0.0` for every 2-hour study, which operators read as
"the embargo is negligible" rather than "the bar duration is unknown". The
diagnostics looked authoritative while being computed from a missing lookup.

#### The change that caused it

A developer added `"2h"` to the `TIMEFRAME_ALIASES` map in `shared/constants.py`
and ran the relevant backtest tests, which passed because `_TIMEFRAME_BAR_SECONDS`
is only consumed by the optimizer diagnostics.

#### Why it broke

`normalize_timeframe` returns `"15m"` and `"1d"`, but also passes through `"m5"`
and `"h4"` unchanged. `_TIMEFRAME_BAR_SECONDS` must therefore list both families
for every timeframe — an invariant enforced by nothing and documented only by a
comment. A new alias added to one table and not the other fails silently: the
function returns `None`, `_build_diagnostics_payload` passes `bar_duration=None`,
and the embargo is still computed correctly in wall-clock terms but can no longer
be expressed in bars. There is no error and no warning.

#### How it was caught

Not by any test. `test_bar_duration_covers_both_timeframe_spellings` enumerates
the spellings known at the time it was written, so adding a new timeframe does not
extend its coverage.

#### Hardening suggestions

- Derive the seconds table from the same alias map that `normalize_timeframe`
  uses, so a new alias cannot be added without a duration.
- Make `_timeframe_bar_duration` return a sentinel and log a warning when a
  recognisable-but-unmapped timeframe is passed, so the missing translation is
  visible in the optimizer log.
- Turn the enumeration in `test_bar_duration_covers_both_timeframe_spellings`
  into a property test over every key in `TIMEFRAME_ALIASES`.

### 7. The STOPPED enforcement ledger is process-local

**Severity:** Medium
**Component:** `live_runner/daemon.py::_enforce_stopped_deployments`, `sync_services_loop`
**Fragility type:** Implicit resource lifecycle / non-atomic compound operation

#### What happened

An operator stopped a deployment while the daemon was down — by editing the
database directly, because the API was unreachable. On the next daemon start the
status endpoint reported the deployment as `STOPPED`, and the dashboard showed it
as such. The positions it had opened were never closed, and no log line said so,
because the enforcement ledger started empty and there was no adapter to act on.

#### The change that caused it

A developer added `already_enforced: set[str]` to `_enforce_stopped_deployments` so
that a persistently stopped deployment does not re-issue cancel and flatten calls
on every sync iteration. The motivation — avoiding broker spam — is sound.

#### Why it broke

The ledger is an in-memory `set` created fresh on each `sync_services_loop`
iteration's enclosing scope and never persisted. Combined with the
`if adapter is None: already_enforced.add(...)` branch, a stop that was never
acted on is recorded as enforced for the life of the process. A restart clears
the ledger, which is the only thing that would have caused a retry — so the one
event that most needs a retry is the one the design makes unrecoverable. The
`adapter is None` branch is the specific defect: it reasons "no adapter means
nothing is open", which is true only if the daemon created the position itself in
this process.

#### How it was caught

The unit tests exercise the in-process transitions and pass. There is no test that
starts enforcement with an empty adapter map, restarts, and asserts the
flatten is retried — and in the current code it would not be.

#### Hardening suggestions

- Do not mark a stopped deployment as enforced when no adapter was available.
  Leave it out of the ledger so the next sync retries once a connection exists.
- Persist the enforcement result (a timestamp on the deployment record) so a
  restart cannot re-issue broker calls for a stop it already honoured, and does
  not lose the need to enforce one it never did.
- Add a test that calls `_enforce_stopped_deployments` with an empty adapter map
  and asserts the deployment id is absent from `already_enforced`.

## Themes and Recommendations

**Stringly-typed contracts crossing module boundaries are the dominant risk.**
Post-mortems 1, 2, 4 and 6 are all the same shape: a vocabulary — task statuses,
auth scheme, route/account numbers, timeframe labels — is defined as bare string
literals in one place and consumed as literals in another. None of these
crossings is enforced by a type, a shared constant, or a schema; all four are
enforced only by a comment, and the comments are in different files from the
literals in two of the four cases.

The single highest-leverage change would be to make these four vocabularies
importable values rather than duplicated literals:

- one `pausable_statuses()` in `viewer/core.py`, consumed by both the flag
  builder and the endpoint guard;
- one resolver for the Discord auth scheme, with a required argument instead of
  an `os.environ` fallback;
- route-to-account mapping resolved from configuration only, with the static
  table either removed or validated against accounts-env at startup;
- timeframe duration derived from the same alias map that normalises the label.

**Fail-open reasoning inside fail-closed code is the second theme.** The STOPPED
path's `adapter is None` branch (post-mortem 7) and the "final status returns
200" behaviour in the task endpoints both share a shape: a branch that treats
"we could not act" as "there was nothing to do". In a safety-critical path these
are opposites, and both are the kind of thing that survives review because the
code looks defensive. Where a control cannot be applied, the honest outcome is an
explicit refusal plus a retry, never a success response.

## Real bug found during this read (not hypothetical)

In `live_runner/daemon.py::_enforce_stopped_deployments`, the `adapter is None`
branch adds the deployment to `already_enforced` and logs
`"Stopped deployment has no active adapter; nothing to cancel or flatten"`. That
reasoning is only valid if the daemon holds no open position or resting order for
that account. It is false in the case that matters most: **immediately after a
daemon restart, `active_adapters` is empty for every account, so a genuinely
stopped deployment with open positions is marked as enforced and never flattened.**
The correct behaviour is to leave the deployment out of the ledger so the next
sync, once the adapter connects, performs the cancel and flatten.

This is described as post-mortem 7 above because the *shape* of the fragility is
the point, but the current behaviour is already wrong and the fix is small:
remove the `already_enforced.add(deployment_id)` from that branch. The daemon
would then retry on each sync until an adapter is available, which is the intended
behaviour and is bounded because the deployment is not ACTIVE and cannot submit
anything in the meantime.
