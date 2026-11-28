# Broad-exception classification

Every `except Exception`, bare `except`, and `except BaseException` in `live_runner/`, `runner/` (including `runner/engines/`) and `risk/` must carry a trailing `# except-kind:` marker so reviewers can see whether the handler is allowed to keep the process alive or must fail closed around a money decision.

- `loop-survival`: log and continue/retry; used for transport, listener, cleanup, batch fan-out, and snapshot paths where the next pass is safe.
- `money-decision`: the handler touches order submission, cancellation, flatten/restore, reconciliation, sizing, or fill persistence; unknown state must not produce a success-shaped continuation.
- `best-effort`: an optional enrichment or persistence whose failure degrades to a documented default (stats fallbacks, SQLite persist, catalog/meta reads).
- `propagate`: the handler classifies, records or enriches the error and always re-raises; nothing is swallowed.

`live_runner/` accepts only `loop-survival`, `money-decision`, and `propagate`; `runner/` and `risk/` additionally accept `best-effort`.

`tests/test_except_kind_gate.py` fails when a broad handler in a gated package lacks a valid marker, and a `propagate` marker whose handler never raises or hands the exception to the caller also fails.

Handlers in files owned by open PRs #192, #271, #272, #274, #277, #313, #316, and #323 are classified here but intentionally left unchanged; the `Current behavior` column marks them `no — after PR #N` (#274, #277).

| File | Line | Handler | Kind | Current behavior |
|---|---:|---|---|---|
| `live_runner/account_discovery.py` | 495 | `_message_to_debug_fields` | loop survival (log and continue) | yes — debug fallback only |
| `live_runner/account_discovery.py` | 727 | `_request_ctrader_account_detail` | loop survival (log and continue) | yes — optional detail enrichment; on failure `ctx.balance` keeps its current value (0.0 until trader details load), host errors still propagate |
| `live_runner/account_discovery.py` | 856 | `_build_ctrader_live_account` | loop survival (log and continue) | yes — discovery enrichment, not an order path |
| `live_runner/account_discovery.py` | 921 | `discover_ctrader_accounts` | loop survival (log and continue) | yes — per-host continue |
| `live_runner/account_discovery.py` | 983 | `discover_configured_accounts` | loop survival (log and continue) | yes — per-credential-block skip |
| `live_runner/account_discovery.py` | 996 | `discover_configured_accounts` | loop survival (log and continue) | yes — per-credential-block skip |
| `live_runner/alpaca_adapter.py` | 815 | `connect` | money decision (must fail closed) | yes — disconnect + raise |
| `live_runner/alpaca_adapter.py` | 830 | `connect` | money decision (must fail closed) | no — after PR #277 |
| `live_runner/alpaca_adapter.py` | 859 | `_reconcile_signal_fill` | money decision (must fail closed) | no — after PR #277 |
| `live_runner/alpaca_adapter.py` | 1058 | `backfill_commissions` | money decision (must fail closed) | yes — stays unknown, retried next pass |
| `live_runner/alpaca_adapter.py` | 1140 | `disconnect` | loop survival (log and continue) | yes — stream stop cleanup |
| `live_runner/alpaca_adapter.py` | 1454 | `connect` (recovery schedule) | money decision (must fail closed) | yes — warning, markers stay pending |
| `live_runner/alpaca_adapter.py` | 1496 | `_run_exit_reprotection_recovery` | money decision (must fail closed) | yes — error log, markers stay pending |
| `live_runner/alpaca_adapter.py` | 1514 | `_cancel_exit_reprotection_recovery` | money decision (must fail closed) | yes — warning, task cancelled |
| `live_runner/alpaca_adapter.py` | 1179 | `_recover_existing_signal_order` | money decision (must fail closed) | yes — raises, refuses duplicate |
| `live_runner/alpaca_adapter.py` | 2003 | `_cancel_open_orders_locked` | money decision (must fail closed) | yes — failure recorded, sweep incomplete |
| `live_runner/alpaca_adapter.py` | 2027 | `_cancel_open_orders_locked` | money decision (must fail closed) | yes — verify failure reported |
| `live_runner/alpaca_adapter.py` | 2157 | `_restore_stop` | money decision (must fail closed) | no — after PR #274 |
| `live_runner/alpaca_adapter.py` | 2186 | `_restore_stop` | money decision (must fail closed) | yes — critical log + False |
| `live_runner/alpaca_adapter.py` | 2269 | `_flatten_symbol_locked` | money decision (must fail closed) | yes — abandon + restore/report |
| `live_runner/alpaca_adapter.py` | 2285 | `_flatten_symbol_locked` | money decision (must fail closed) | yes — abandon + restore/report |
| `live_runner/alpaca_adapter.py` | 2314 | `_flatten_symbol_locked` | money decision (must fail closed) | yes — abandon + restore/report |
| `live_runner/alpaca_adapter.py` | 2403 | `_cancel_orphaned_protections_locked` | money decision (must fail closed) | yes — failure recorded |
| `live_runner/alpaca_adapter.py` | 2509 | `_alpaca_order_state` | money decision (must fail closed) | yes — order stays unproven, returns None |
| `live_runner/alpaca_adapter.py` | 2516 | `_alpaca_order_state` | money decision (must fail closed) | yes — order stays unproven, returns None |
| `live_runner/alpaca_adapter.py` | 2771 | `_on_trade_update` | money decision (must fail closed) | no — after PR #277 |
| `live_runner/alpaca_adapter.py` | 2804 | `listen_executions` | loop survival (log and continue) | yes — reconnect backoff |
| `live_runner/alpaca_adapter.py` | 2831 | `listen_executions` | loop survival (log and continue) | yes — stream stop cleanup |
| `live_runner/base_adapter.py` | 48 | `order_not_submitted` | money decision (must fail closed) | yes — marks and re-raises |
| `live_runner/base_adapter.py` | 51 | `order_not_submitted` | money decision (must fail closed) | yes — marker failure degrades to unknown |
| `live_runner/broker_reconciliation.py` | 425 | `_position_qty_tolerance` | money decision (must fail closed) | yes — falls back to the strict tolerance |
| `live_runner/broker_reconciliation.py` | 484 | `_broker_state` | money decision (must fail closed) | yes — unreadable positions become a mismatch snapshot |
| `live_runner/broker_reconciliation.py` | 503 | `_broker_state` | money decision (must fail closed) | yes — unreadable orders become a mismatch snapshot |
| `live_runner/broker_reconciliation.py` | 528 | `_broker_state` | money decision (must fail closed) | yes — unreadable stops become a mismatch snapshot |
| `live_runner/broker_reconciliation.py` | 837 | `_lookup_order_state` | money decision (must fail closed) | yes — returns None, order stays unproven |
| `live_runner/broker_reconciliation.py` | 952 | `reconcile_account` | money decision (must fail closed) | yes — comparison failure counts as a mismatch pass |
| `live_runner/broker_reconciliation.py` | 1016 | `reconcile_account` | money decision (must fail closed) | yes — halt failure logged, finding recorded |
| `live_runner/broker_reconciliation.py` | 1051 | `reconcile_account` | money decision (must fail closed) | yes — resume failure leaves entries halted |
| `live_runner/broker_reconciliation.py` | 1179 | `run_broker_reconciliation_loop` | loop survival (log and continue) | yes — iteration retried next interval |
| `live_runner/ctrader_adapter.py` | 575 | `_twisted_deferred_timeout_class` | loop survival (log and continue) | yes — string-shape fallback |
| `live_runner/ctrader_adapter.py` | 620 | `_reason_to_text` | loop survival (log and continue) | yes — str(reason) fallback |
| `live_runner/ctrader_adapter.py` | 1089 | `_refresh_access_token` | money decision (must fail closed) | yes — refresh returns False |
| `live_runner/ctrader_adapter.py` | 1180 | `_on_message` | money decision (must fail closed) | yes — execution decode flags reconciliation |
| `live_runner/ctrader_adapter.py` | 1511 | `_load_symbol_props_into_cache` | money decision (must fail closed) | yes — propagates, no fallback scales |
| `live_runner/ctrader_adapter.py` | 1693 | `connect` | money decision (must fail closed) | yes — retry then raise |
| `live_runner/ctrader_adapter.py` | 1719 | `_reconcile_after_connect` | money decision (must fail closed) | yes — reconciliation flag + retry |
| `live_runner/ctrader_adapter.py` | 1901 | `_teardown_client` | loop survival (log and continue) | yes — teardown cleanup |
| `live_runner/ctrader_adapter.py` | 1905 | `_teardown_client` | loop survival (log and continue) | yes — teardown cleanup |
| `live_runner/ctrader_adapter.py` | 2749 | `_unsubscribe_spot` | loop survival (log and continue) | yes — best-effort unsubscribe |
| `live_runner/ctrader_adapter.py` | 2915 | `_asset_name_map` | loop survival (log and continue) | yes — names are log-only |
| `live_runner/ctrader_adapter.py` | 3878 | `listen_executions` | money decision (must fail closed) | yes — requeue or raise |
| `live_runner/ctrader_adapter.py` | 3908 | `_run_pending_reconciliation` | money decision (must fail closed) | yes — retry scheduled, flag kept |
| `live_runner/daemon.py` | 615 | `_live_account_broker_login` | money decision (must fail closed) | yes — returns empty broker login |
| `live_runner/daemon.py` | 1145 | `_check_entry_deviation` | money decision (must fail closed) | yes — raises, entry refused |
| `live_runner/daemon.py` | 1291 | `_resolve_live_pip_value` | money decision (must fail closed) | yes — raises, entry refused |
| `live_runner/daemon.py` | 1364 | `_position_qty_tolerance` | money decision (must fail closed) | yes — strict default |
| `live_runner/daemon.py` | 1478 | `_ensure_no_exit_in_flight` | money decision (must fail closed) | yes — raises ExitInFlightError |
| `live_runner/daemon.py` | 2217 | `_deployment_is_currently_active` | money decision (must fail closed) | yes — returns False |
| `live_runner/daemon.py` | 2445 | `_renew_submission_claim_until_cancelled` | loop survival (log and continue) | yes — renewal retried |
| `live_runner/daemon.py` | 2707 | `_prepare_claim_and_submit_signal` | money decision (must fail closed) | yes — returns before claim |
| `live_runner/daemon.py` | 2773 | `_prepare_claim_and_submit_signal` | money decision (must fail closed) | yes — holds lease on unknown outcome |
| `live_runner/daemon.py` | 2782 | `_prepare_claim_and_submit_signal` | loop survival (log and continue) | yes — lease expiry is safe |
| `live_runner/daemon.py` | 2845 | `_prepare_claim_and_submit_signal` | loop survival (log and continue) | yes — lease expiry is safe |
| `live_runner/daemon.py` | 3297 | `_create_adapter_for_account` | money decision (must fail closed) | yes — no adapter |
| `live_runner/daemon.py` | 3325 | `_create_adapter_for_account` | money decision (must fail closed) | yes — no adapter |
| `live_runner/daemon.py` | 3365 | `_disconnect_adapter` | loop survival (log and continue) | yes — disconnect cleanup |
| `live_runner/daemon.py` | 3936 | `sync_services_loop` | loop survival (log and continue) | yes — next tick retries |
| `live_runner/daemon.py` | 4008 | `_poll_adapter_balance` | loop survival (log and continue) | yes — snapshot only, terminal auth handled |
| `live_runner/daemon.py` | 4105 | `_backfill_one` | money decision (must fail closed) | yes — stays unknown, next pass retries |
| `live_runner/daemon.py` | 4469 | `_enforce_stopped_deployments` | money decision (must fail closed) | yes — stamp owned by status transition |
| `live_runner/daemon.py` | 4542 | `_enforce_stopped_deployments` | money decision (must fail closed) | yes — no stamp, flatten still runs |
| `live_runner/daemon.py` | 4598 | `_enforce_stopped_deployments` | money decision (must fail closed) | yes — no stamp, retried |
| `live_runner/daemon.py` | 4648 | `_enforce_stopped_deployments` | money decision (must fail closed) | yes — no stamp, retried |
| `live_runner/daemon.py` | 4747 | `_enforce_stopped_deployments` | money decision (must fail closed) | yes — no stamp, retried |
| `live_runner/daemon.py` | 4885 | `_ensure_adapter_for_active_account` | money decision (must fail closed) | yes — not ready / terminal removed |
| `live_runner/daemon.py` | 5063 | `sync_deployments_loop` | money decision (must fail closed) | yes — re-enforce rather than skip |
| `live_runner/daemon.py` | 5171 | `sync_deployments_loop` | loop survival (log and continue) | yes — next tick retries, no stamp |
| `live_runner/daemon.py` | 5237 | `run_daemon` | money decision (must fail closed) | yes — logs and re-raises |
| `live_runner/market_data_stream.py` | 146 | `_run` | loop survival (log and continue) | yes — stream reconnect backoff |
| `live_runner/market_data_stream.py` | 213 | `set_symbols` | loop survival (log and continue) | yes — resubscribe continues |
| `live_runner/market_data_stream.py` | 224 | `stop` | loop survival (log and continue) | yes — stop cleanup |
| `live_runner/preflight.py` | 188 | `_connectivity_event_loop` | loop survival (log and continue) | yes — exit cleanup |
| `live_runner/preflight.py` | 228 | `_run_broker_connectivity_check` | money decision (must fail closed) | yes — preflight error |
| `live_runner/preflight.py` | 638 | `run_live_preflight` | money decision (must fail closed) | yes — ok=False |
| `live_runner/preflight.py` | 737 | `_crash_redaction` | money decision (must fail closed) | yes — type-only crash message |
| `live_runner/preflight.py` | 813 | `main` | money decision (must fail closed) | yes — failed result, exit 1 |
| `live_runner/promotion.py` | 291 | `strategy_target` | money decision (must fail closed) | yes — PromotionTargetError |
| `live_runner/trailing_stops.py` | 111 | `adapter_live_trailing_modes` | money decision (must fail closed) | yes — none only |
| `live_runner/trailing_stops.py` | 129 | `adapter_live_trailing_timeframes` | money decision (must fail closed) | yes — empty set |
| `live_runner/trailing_stops.py` | 296 | `run_once` | money decision (must fail closed) | yes — outage recorded, stops unchanged |
| `live_runner/trailing_stops.py` | 407 | `_trail_account` | money decision (must fail closed) | yes — per-position outage recorded |
| `live_runner/trailing_stops.py` | 457 | `_read_stops` | money decision (must fail closed) | yes — stops unavailable this pass |
| `live_runner/trailing_stops.py` | 870 | `run_trailing_stops_loop` | loop survival (log and continue) | yes — next tick retries |

Total: 88 broad handlers.

## Handlers added after this table was generated

These broad handlers landed on main while this classification was in review, and are classified by the same rule (a handler that only keeps the loop alive and logs is `loop-survival`; one whose failure changes whether an order/stop/position action happens is `money-decision` and fails closed). Their line numbers move; the marker in the source and `tests/test_except_kind_gate.py` are authoritative.

| File | Handler | Kind | Why |
|---|---|---|---|
| `alpaca_adapter.py` | venue-calendar resolution fallback (flat staleness age) | loop-survival | falls back to a documented default age |
| `alpaca_adapter.py` | position re-read before restoring a stop | money-decision | not restoring is the bounded failure |
| `alpaca_adapter.py` | working-book re-read after a flatten | money-decision | a book nobody could read proves nothing |
| `alpaca_adapter.py` | `_cancel_already_gone` order read | money-decision | fails closed: an unreadable order is not "gone" |
| `alpaca_adapter.py` | orphan-sweep verification read | money-decision | recorded as `failed`, retried |
| `ctrader_adapter.py` | venue-calendar cache write | loop-survival | cache only |
| `ctrader_adapter.py` | broker-close reconcile of an unreadable deal | money-decision | the deal is skipped, not booked |
| `daemon.py` | decision record on an escaping exception (re-raised) | loop-survival | observational |
| `daemon.py` | signal-archive market features | loop-survival | best effort |
| `daemon.py` | trailing-mode check at deployment load | loop-survival | signal-time gates still refuse |
| `daemon.py` | submission-lease release in a refusal path | loop-survival | the lease expires on its own |
| `kill_switch.py` | halt-state read; order-cancel, flatten and protection sweeps | money-decision | entries refused / sweep retried next tick, nothing liquidated on an error |
| `daemon.py` | loss-breaker equity read in the balance poll | loop-survival | observation skipped, the entry gate still fails closed |
| `daemon.py` | account equity read in the loss-breaker entry gate | money-decision | raises `LossBreakerBreachError`, entry refused |
| `loss_breaker.py` | breach-latch write, state read, deployment pip-value/quote/flows reads | money-decision | unmeasurable loss = entries refused |
| `loss_breaker.py` | untrusted-fill listing for the alert, observation wrapper | loop-survival | alert/observation only |
| `ctrader_adapter.py` | `get_max_leverage` tier/account legs | money-decision | degrades to `None` → the caps' fail-closed 1x ceiling |
| `ctrader_adapter.py` | `_account_leverage_cents` trader read | money-decision | same 1x degradation |
| `pre_submit_caps.py` | `_safe_max_leverage` adapter read | money-decision | same 1x degradation |
| `pre_submit_caps.py` | broker-facts gather (quote/pip/balance/leverage) | money-decision | cancels siblings, refuses the order |
| `pre_submit_caps.py` | `_record_cap_audit` audit write | loop-survival | observational; the cap decision stands |
| `daemon.py` | `_join_default_executor` joiner-thread relay of `executor.shutdown` failures | loop-survival | infrastructure relay; re-raised on the loop thread |
| `daemon.py` | `run_daemon` worker-join `BaseException` (interrupted or budget-expired join) | money-decision | keeps the single-instance lock and re-raises rather than handing it to a second daemon while DB writes may be in flight |
| `broker_order_conflicts.py` | `pause_live_deployment_for_order_conflict` write failure | loop-survival | ERROR alert logged, remaining deployments still re-halted |
| `daemon.py` | `enforce_broker_order_conflict_halts` call in `sync_services_loop` | loop-survival | ERROR alert logged, next tick retries |
| `daemon.py` | startup failure in `_run_daemon_locked` (logs and re-raises) | money-decision | ERROR record for the alert sink, exception propagates |
| `daemon.py` | crash handler in `_run_daemon_locked` (logs, captures, re-raises) | money-decision | ERROR record + Sentry capture, exception propagates |
| `daemon.py` | signal-archive close in the `_run_daemon_locked` shutdown `finally` | loop-survival | WARNING logged during shutdown, nothing trades after |
| `signal_archive.py` | archive enqueue/serialize/write/record, outcome sink | loop-survival | archive is observational; a dropped record trades nothing |

## `runner/` and `risk/` handlers

`risk/` currently has no broad handlers; it is gated so future ones must be classified. The `runner/` handlers (batch backtests, stats, engines, CLI, LLM providers) are classified as `loop-survival` (one cell/strategy/symbol failure is recorded and the batch continues), `best-effort` (optional enrichment or persistence degrades to a documented default) or `propagate` (the handler enriches and always re-raises). Line numbers move; the marker in the source is authoritative.

| File | Kind | Why |
|---|---|---|
| `runner/agent_backtest_target.py` | best-effort | corrupt `catalog.json` falls back to the OHLCV directory scan (debug-logged) |
| `runner/backtest_grid.py` | loop-survival / best-effort | cell failures become `failed` rows with traceback; SQLite persist is secondary to the saved JSON (warning + traceback) |
| `runner/backtest_matrix.py` | loop-survival | per-cell and per-strategy failures are logged and recorded as error/failed rows; unloadable strategy meta becomes `skipped_unavailable` |
| `runner/cli.py` | loop-survival / best-effort | an unreadable OHLCV file is logged with traceback and the all-symbols check refuses to fabricate a date range; SQLite persist is secondary to the saved JSON |
| `runner/engines/base.py` | best-effort | strategy-meta read failure falls back to overrides only (warning + traceback; flagged for maintainer review — failing closed would change runs) |
| `runner/engines/native_adapter.py` | best-effort | unparseable config dates degrade to `duration_days=0` in run meta |
| `runner/engines/vectorbt_adapter.py` | propagate | per-symbol failures are collected and re-raised as one aggregate `RuntimeError` with per-symbol tracebacks |
| `runner/grid_tester.py` | loop-survival | a failing grid cell returns an `error` row; the grid continues |
| `runner/llm_providers/copilot_sdk_provider.py` | propagate | auth/model errors are classified and re-raised wrapped; the thread bridge stores the error for the caller to re-raise |
| `runner/llm_providers/nim_provider.py` | propagate | error metadata is recorded, then the exception re-raises |
| `runner/portfolio_optimizer.py` | propagate | equity-curve load failure is wrapped in `ValueError` naming the backtest id |
| `runner/slippage.py` | best-effort | an undescribable slippage model is treated as cost-declaring (conservative) |
| `runner/stats.py` | best-effort | metric fallbacks degrade to documented sentinels (`None`, `0.0`, `1.0`) with debug-level tracebacks; the plausible-number defaults (`_safe_qs` → `0.0`, `_one_sided_p_value` → `1.0`) are flagged for maintainer review |
