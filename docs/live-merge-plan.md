# Live-trading fixes: merge plan

Branch `integration/live-fixes` merges every open live-trading fix PR on top of `main` in the
order below, with all conflicts already resolved. It is a rehearsal and a reference, **not** a PR:
merge the individual PRs in this order (or fast-forward-compare against this branch to verify the
final tree).

Tracking issue: #184.

## Order

| # | PR | Branch | Depends on |
|---|----|--------|-----------|
| 1 | #183 | `fix/live-alpaca-bracket-entry` | none |
| 2 | #182 | `fix/live-preflight-connectivity` | none |
| 3 | #192 | `fix/live-daemon-graceful-shutdown` | stacked on #182 (retarget to `main` after #182 merges) |
| 4 | #141 | `fix/live-sizing-single-resolver` | none |
| 5 | #187 | `fix/live-ctrader-pip-value` | stacked on #141 (retarget after #141 merges) |
| 6 | #181 | `fix/live-ctrader-volume-step` | **only with or after #187**: the broker's volume-step rejection currently masks the ~1000x oversizing fixed by #187 |
| 7 | #198 | `fix/live-alpaca-cancel-pending` | none |
| 8 | #199 | `fix/live-preflight-accounts-env-expansion` | conflicts with #182 |
| 9 | #200 | `fix/live-preflight-parseable-stdout` | conflicts with #192/#199 (import line) |
| 10 | #202 | `fix/live-discovery-no-reactor` | none |
| 11 | #204 | `fix/live-smoke-fresh-db` | none |
| 12 | #205 | `fix/live-commission-sign` | none |
| 13 | #206 | `fix/live-smoke-sltp` | conflicts with #204 (runbook) |
| 14 | #207 | `fix/live-preflight-db-deployments` | conflicts with #182/#206 (runbook) |

## Conflicts and resolutions

| PR | Files | Resolution |
|----|-------|-----------|
| #181 | `live_runner/ctrader_adapter.py`, `tests/test_live_execution_safety.py` | `SymbolScale` keeps **both** `lot_size_from_broker` (#187) and `step_volume` (#181). `_symbol_scale_from_broker_props` uses #181's helpers (`_optional_int/_optional_float/_fallback_field`) plus #187's `lots_from_broker` flag. In `_resolve_order_volume` the #187 scale-change guard runs first, then `_floor_to_volume_step(_qty_to_broker_volume(qty, scale.volume_per_qty), scale.step_volume)`: the step floors the volume derived from the pip-value-corrected qty. Both test blocks kept; new combo test `test_volume_step_floors_the_pip_value_corrected_volume` in `tests/test_live_pip_value.py`. |
| #199 | `live_runner/preflight.py`, `tests/test_live_preflight.py` | Keep #182's shared event loop (`_connectivity_event_loop`) and #199's `${VAR}` expansion (`expansion_env`) together; `import os` plus `Iterator`; both test blocks kept. |
| #200 | `live_runner/preflight.py`, `tests/test_live_preflight.py` | Import line keeps `release_twisted_reactor_threads` (from #192's `live_runner/shutdown.py`) and `route_console_logs_to_stderr` (#200); both test blocks kept. |
| #206 | `docs/live-deploy-runbook.md` | Keep both smoke paragraphs (#204 fresh-DB account registration, #206 live-price anchored SL/TP). |
| #207 | `docs/live-deploy-runbook.md` | Keep #182's `--check-connectivity` command, then the `--check-db` section; the remaining commands stay in their own code blocks. |

## Integration-only fix

`tests/test_live_daemon_shutdown.py::_GateAdapter` (#192) had no `get_quote` / `get_pip_value`. After
#141 (entry quote gate) and #187 (trusted pip value) the entry is refused before `submit_order`, so
three drain tests timed out. Fixture-only change: the fake now provides both. Whoever merges #192
after #141/#187 needs this (already on the integration branch).

## Verification of the integrated branch

* `pytest -n 2`: 4165 passed, 97 skipped, 0 failed.
* `scripts/gate-tests.sh`: all 4 gate suites pass.

## Post-merge checks

1. `uv run python -m live_runner.preflight --config live_deployments.yaml --accounts-env .accounts.env --check-connectivity --check-db`
   - stdout must be a single JSON document (pipe to `jq`); `ok: true`; `db_deployments.only_in_db` empty.
   - Every cTrader account connects and the process exits on its own (no hang).
2. Demo smoke (never live accounts):
   - `uv run python scripts/live_smoke.py --mode alpaca --accounts-env .accounts.env --reports-dir docs/reports`
   - `uv run python scripts/live_smoke.py --mode ctrader --accounts-env .accounts.env --reports-dir docs/reports`
     (on a fresh DB; market must be open, otherwise it fails with the closed-market message).
3. cTrader demo sizing: submit one risk-sized EURUSD entry through the daemon path; confirm
   - reported `risk_amount` equals broker loss at stop (pip value 1000, not 1.0),
   - volume is a multiple of the symbol's `stepVolume`,
   - stop-loss and take-profit are present at the broker.
4. Start the demo daemon, send SIGTERM mid-run; it must drain in-flight submissions and exit 0.
5. Keep deployments paused until steps 1-4 pass.
