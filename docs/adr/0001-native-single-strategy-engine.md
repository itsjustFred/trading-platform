# ADR 0001: Consolidate on the native engine as the single strategy engine

- **Status:** Accepted
- **Date:** 2026-11-21
- **Issue:** #318 (maintainer decision, 2026-11-21)
- **Assessment:** engine-consolidation report of 2026-11-21 (main @ 47e4e90f)

## Context

The platform carries four engine adapters under `runner/engines/` plus a
signal-replay product:

- **native** (`native_adapter.py` + `runner/engine_pkg/`): event-based bar-by-bar
  simulation. Viewer default, the MT5 alignment engine, and the producer of
  the MT5 parity artifacts and goldens.
- **vectorbt** (`vectorbt_adapter.py`, 1685 LOC): despite the name it contains
  **no `import vectorbt`** — it is a second hand-written Python bar loop with
  its own fill, stop and sizing code, kept in step with native through
  "mirrors native" comments and `tests/test_vectorbt_native_parity.py`.
  Strategy-registry default; the optimizer requires it for base backtests
  while walk-forward, matrix and grid merely default to it (they still
  accept `--engine native` or any adapter).
- **pynecore** (`pynecore_adapter.py`, 1422 LOC): runs the external `pyne` CLI
  for two demo strategies (`pynecore_orb`, `pynecore_sma_crossover`).
- **LEAN** (`lean_adapter.py`, 185 LOC): a subprocess into an external
  `~/Desktop/custom_lean_integration`; also used as an import/reference path.
- **signal replay** (`signal_replay_adapter.py`, 1431 LOC): a different
  product — it backtests live Discord signal-bot payloads with its own
  documented fill model. Live trading never calls any engine.

Every fill or sizing fix has historically cost 2–4x (#208, #220, #276, #190),
parity drift keeps surfacing as bugs, and — the result-integrity problem —
optimizer and walk-forward numbers are produced by a different engine than
the one validated against MT5.

## Decision (maintainer, #318)

1. **Native is the single strategy engine.** All strategy backtesting
   consolidates onto `native`.
2. **pynecore will be deleted** (phase L; frozen in phase S).
3. **LEAN becomes import-only**: `LeanAdapter.import_result`, the converters
   and the QC importer stay as oracles; the LEAN subprocess run path is
   removed in phase L.
4. **The vectorbt "D2 strict" stop-entry fill semantics are dropped.** Native
   keeps the MT5-aligned next-open behaviour; the stop-level fill used by
   `us100_scalper` does not survive as a native option.
5. **Signal replay moves out of `runner/engines/`** so it no longer counts as
   a strategy engine under the contract (it serves live signal bots, not
   strategy files).
6. **A vectorized pre-screen is acceptable only if the final ranking is
   recomputed in native.** A screening-only fast pass may exist, but reported
   results must come from the native engine.
7. Per-strategy result differences between engines are allowed **only when
   each one is explained in the diff report** before defaults flip.

## Consequences

### Phase S (this change — freeze, no behaviour changes)

- This ADR records the decision.
- `vectorbt`, `pynecore` and the LEAN *run* path are frozen: bug fixes only,
  no new features. Engine selection emits a `DeprecationWarning`
  (`runner/cli.py:get_engine`, `runner/backtest_grid.py:get_engine`,
  `LeanAdapter.run`); `cmd_engines` marks them frozen.
- **No defaults flip.** The registry default stays `vectorbt`, the optimizer
  still requires vectorbt base backtests, and CLI defaults are unchanged —
  those moves belong to phase M.
- `scripts/engine_migration_gate.py` runs native vs the vectorbt adapter over
  every registry strategy and writes a per-strategy trade/stat diff report
  (the go/no-go evidence for phase M). `TP_TEST_OHLCV_DIR` supplies real data
  read-only; synthetic data is generated otherwise.
- `scripts/engine_speed_benchmark.py` times native vs vectorbt on an
  optimizer-like workload (report only) — the speed go/no-go for phase M.
- Stored `engine="vectorbt"` results remain readable.

### Phase M (later)

- Port the vectorbt-only features into native: strategy
  `exit_price`/`exit_reason` series and the optimizer integration; the D2
  strict stop fill is **not** ported (decision 4).
- Allow native base backtests in the optimizer; flip defaults (registry,
  walk-forward, matrix, grid, grid_tester, CLI) only after each strategy's
  diff from the gate report is explained.
- MT5 goldens and parity artifacts must stay byte-identical; any golden
  change is a stop.

### Phase L (later)

- Delete `vectorbt_adapter.py`, the `vectorbt` dependency (dead weight — it
  is never imported), the vectorbt-only tests once redundant, the LEAN run
  path, and `pynecore_adapter.py` with its two demo strategies.
- Move `signal_replay_adapter.py` out of `runner/engines/` and narrow the
  engine contract to native + replay.
- Keep the QC/LEAN and MT5 importers, the goldens and the independent
  reference-library parity tests (#189/#194/#195/#197) as oracles.
- Close #278/#279 as superseded; #279 shrinks to RiskEngine-only.

## Alternatives considered

- **(a) Keep all engines** — rejected: duplicated money-path code, recurring
  parity bugs, and optimizer results diverging from the MT5-validated engine.
- **(c) Native + vectorbt for sweeps** — rejected: the vectorbt path is not
  vectorized, so it buys nothing; a real `vbt.Portfolio` rewrite would add a
  third semantics to keep in parity.
- **(d) Variant of (b)** — adopted in part: signal replay is confirmed a
  separate product (move out of `runner/engines/`), LEAN becomes
  import-only, and pynecore is deleted outright rather than kept as an
  optional extra.

## References

- Issue #318 and the maintainer decision comment (2026-11-21).
- `docs/cross-engine-backtest-contract.md` — engine contract (to be shrunk
  to native + replay in phase L).
- `docs/mt5-backtest-alignment-plan.md` — native is the MT5 alignment engine.
- Related: #317 (numba idea for native speed).
