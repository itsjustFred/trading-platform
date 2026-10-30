# Code Review — PR #107 (two-axis)

Fixed point: `main`. Diff command: `git diff main...HEAD`.
Commits: `ed48dbe`, `26d4cfe`, `77b60a1`, `3d991fc`, `7e12f1c`, `ac62a27`,
`8daa586`, `870ab56`, `c96b0c5` (merge), `b3c8258`, `fe23c15`.

Standards sources: `AGENTS.md` (the only documented standard; `CLAUDE.md` is a
pointer to it). Repo rules that bind: never weaken/remove/bypass a test to pass;
conventional commits; never push to main; keep paths portable; mention model and
harness in the PR/issue body. No `CONTRIBUTING.md` / `CODING_STANDARDS.md`.

Spec source: no issue tracker (`docs/agents/issue-tracker.md` is absent). The PR
body itself is the spec. Commits reference `#84` (net/gross daily bucket) in
test docstrings only; the PR body is treated as the spec of record.

---

## Standards

**Hard violations of a documented standard: none.**

The one documented rule with teeth here is "Do NOT weaken, remove, bypass or
rewrite a test just to achieve a passing result". Checked every test-file hunk in
the diff:

- No `xfail`, `skip`, `pytest.mark.skip`, `# type: ignore`, or deleted
  assertions anywhere in the diff.
- `tests/test_daily_equity.py` changes the `make_trade` helper's hard-coded
  `id=1` into an `id=1` **parameter** (diff hunk at ~line 47). That is a
  strictly-wider factory, not a weakened assertion; the default preserves every
  existing caller's behaviour.
- `tests/test_daily_equity.py::test_rejects_endpoint_only_curve_with_matching_endpoints`
  updates the `_preserves_engine_curve` call to the new three-argument
  signature (`..., []`). Signature follow-through, not a weakened test.
- `tests/test_stats.py` only adds; nothing removed.

Conventional commits: all ten commits parse as `type(scope): subject` with
`fix`/`test`/`merge` types. Compliant. My two additions are `fix(accounting):`
and `test(accounting):`.

Portability: no new OS-specific paths; `datetime`/`pathlib`/`pd.Timedelta` only.

**Baseline smells (judgement calls, none blocking):**

- **Duplicated Code** — the `pd.to_datetime(..., utc=True, format="mixed", ...)`
  incantation now appears twice in `_preserves_engine_curve`
  (`runner/daily_equity.py` ~302 and ~310) and once in `_curve_frequency`
  (~372) and once in `runner/stats.py` (~466). Four sites, same intent. This
  predates the PR only partly — the PR touches three of the four. A tiny
  `_parse_mixed_index(values)` helper would collapse them and would have made the
  `format="mixed"` regression the PR fixes impossible to miss in one of the four.
  Judgement call, out of the PR's minimal scope to fix now; recorded as a
  follow-up rather than a blocker.
- **Primitive Obsession** — the equity curve is `list[EquityPoint]` but the daily
  view is `list[dict[str, Any]]` with stringly keys `"date" / "return" /
  "equity"`. The two views describe the same instants and the PR's whole
  contribution is making them agree, yet the dict side is untyped. A
  `TypedDict`/dataclass would let a type checker enforce the equality the new
  tests assert by hand. Judgement call; the dict shape is a persisted wire format
  consumed by `runner/backtest_matrix.py` and the viewer, so changing it is a
  cross-cutting change and correctly out of scope here.
- **Divergent Change** — `runner/daily_equity.py` is edited by this PR for
  window scoping, cent quantisation, timestamp normalisation, fallback
  validation and stale-field cleanup. Five reasons in one module. It is the
  module that owns exactly this concern (`attach_daily_equity` is the single
  entry point), so the cohesion holds; noted, not a violation.
- **Mysterious Name** — `closed_equity` (my addition) reads as "the equity at the
  close of `day`", which is what it is. `reconciled_through` /
  `reconciled_equity` in `_window_starting_equity` are less obvious but predate
  this cycle and are documented in that function's docstring.

**Tooling already enforces:** line length and import order — `ruff check` is CI's
only formatter/linter command, and it passes on every changed file (the one
`E501` at `tests/test_stats.py:31` predates the PR and is not in its diff).

---

## Spec

Spec = the PR body's Summary + Tests bullets.

**(a) Spec requirements missing or partial: none found.**

Each Summary bullet maps to a reachable code path:

| PR body claim | Where |
|---|---|
| hard 1-cent absolute endpoint measure (was `max(0.01, abs(final)*1e-9)`) | `_preserves_engine_curve` return, `abs(...) <= 0.01` |
| normalise exit days to UTC | `equity_by_day[parsed.astimezone(timezone.utc).date().isoformat()]` |
| reconstruct window-start equity from prior net PnL, 1-cent reconciliation | `_window_starting_equity` + `abs_tol=0.01` |
| inception at requested start instant; clamp last close; coalesce/shift 1 µs | `inception`/`end_instant` block + `_strictly_increasing_curve` |
| reject engine curve ending before last trade exit | `trade_exits` block returning `False` |
| fail loudly on invalid/reversed/zero-width bounds, non-finite starting equity, overflow | four `raise ValueError` guards in `attach_daily_equity` + overflow check |
| stop publishing stale `daily_returns` / `daily_return_count` | three cleanup branches setting `daily_returns = []` and popping the meta count |
| scope the no-daily-points fallback and validate it | `_normalize_engine_curve(scoped_result.equity_curve or [])` |
| `stats.py` `format="mixed"` for CAGR window | `_resolve_backtest_period_bounds` |

The two commits I added (`b3c8258`, `fe23c15`) are scope-consistent with the
"Fails loudly / consistent cent handling" theme and add no new production
behaviour beyond quantising `daily_returns["equity"]` — which the PR body already
implies via "the cent drift at its source".

**(b) Behaviour not asked for (scope creep): none material.**

- `_trade_net_pnl` / `_net_pnl_before` extraction is a refactor in service of the
  net-basis bullet, not new behaviour.
- `logger.debug(...)` in `_window_starting_equity` is new, but the PR body's
  "Risk / rollback" section explicitly frames the divergence as observable, and
  the docstring explains the debug-level choice. Within scope.
- `.agents/session-files/pr-107/pre-mortem-report.md` (my commit) is a session
  artifact. It matches the existing convention (`pr-102`, `pr-103`, `pr-119` are
  already tracked), so it is housekeeping, not creep.

**(c) Requirements implemented but looking wrong: none found.**

The one place worth stating explicitly, because it is the subtle part of the
spec and I re-derived it: `_preserves_engine_curve` reconciles **only the
endpoint** and the docstring justifies it (interior points carry unrealized PnL
for open positions at a bar close). That is consistent with the claim, and the
`drift=0.005 → preserved=True` parametrisation correctly encodes the boundary.
The `_window_starting_equity` tolerance is also `abs_tol=0.01` with `rel_tol=0`,
matching the "one cent is absolute" claim — I verified the trillion-dollar-account
test (`test_window_starting_equity_cent_tolerance_is_absolute_on_high_priced_account`)
would fail under the old relative formula, so the claim is genuinely pinned.

---

## Summary

Standards: 0 hard violations; 4 baseline judgement calls (2 worth a follow-up:
the four-way `format="mixed"` duplication and the untyped daily-return dict).
Worst Standards issue: the duplicated `format="mixed"` parse incantation, which
this PR had to touch in three of four sites and left inlined.

Spec: 0 missing, 0 creep, 0 wrong. Worst Spec issue: none.
