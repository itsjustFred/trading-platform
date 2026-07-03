# Hermes Overnight Fix Report

**Branch:** `hermes-overnight` (from `kimchi-kimi-continue`)
**Started:** 2026-07-03
**Target:** Fix all critical/high/medium issues from code reviews

---

## Phase 1: P0 Merge-Blocker (Critical/High) — IN PROGRESS

| ID | Fix | File(s) | Status | Commit |
|----|-----|---------|--------|--------|
| **K1** | `discord_message_id = metadata.get("discord_message_id")` (no `str()`) → stores real `NULL` | `shared/database.py:337` + `save_live_signal` | ⬜ Pending | |
| **H1** | Flip correlation penalty sign: `score = sharpe - 5.0 * avg_off_diag` | `runner/portfolio_optimizer.py:257` | ⬜ Pending | |
| **H2** | On dedup hit: if `existing.broker_order_id` empty && `record.broker_order_id` set → update instead of skip | `shared/database.py:945` | ⬜ Pending | |

---

## Phase 2: P1 High Robustness (M1–M10) — PENDING

| ID | Fix | File(s) | Status |
|----|-----|---------|--------|
| **M1** | Catch `subprocess.TimeoutExpired` per grid cell, mark `failed`, continue | `runner/backtest_grid.py` | ⬜ |
| **M2** | Pass resolved `start`/`end` to `attach_daily_equity` (not original params) | `runner/backtest_grid.py` | ⬜ |
| **M3** | `math.isfinite` filter on PnL; fix fallback frequency metadata | `runner/daily_equity.py` | ⬜ |
| **M4** | Guard `weights.sum() == 0`; align on overlap window only | `runner/portfolio_optimizer.py` | ⬜ |
| **M5** | `grid_id` = timestamp + `%f` + `uuid4().hex[:8]` | `runner/grid_tester.py` | ⬜ |
| **M6** | Add `30m` to sort list; validate timeframes early per symbol/tf | `data_tools/download_grid_market_data.py` | `data_tools/download_grid_market_data.py` | ⬜ |
| **M7** | `path.chmod(0o600)` after token write; parent dir `0o700` | `live_runner/ctrader_adapter.py:117` | ⬜ |
| **M8** | Replace real Alpaca account numbers with placeholders | `.accounts.example.env` | ⬜ |
| **M9** | Git-ignore generated artifacts (`*.trades.json`, CSV in `reports/`); remove from index | `.gitignore` + `git rm --cached` | ⬜ |
| **M10** | Backfill Discord-Dedupe & Equity-Curves in migration (idempotent) | `shared/database.py` migration | ⬜ |

---

## Phase 3: P2 Follow-up & Kimchi Validation (N1–N10, KH1, KM1–KM7) — PENDING

---

## CodeRabbit Review Status

| Directory | Review Status | Critical | Warning | Info | Rate-Limit Hit |
|-----------|---------------|----------|---------|------|----------------|
| `live_runner` | ⬜ Pending | | | | |
| `runner` | ⬜ Pending | | | | |
| `shared` | ⬜ Pending | | | | |
| `data_tools` | ⬜ Pending | | | | |
| `validation` | ⬜ Pending | | | | |
| `viewer` | ⬜ Pending | | | | |
| `frontend/src` | ⬜ Pending | | | | |

---

## Test Results Summary

| Test Suite | Status |
|------------|--------|
| Core logic tests | ⬜ Pending |
| DB migration tests | ⬜ Pending |
| CLI tests | ⬜ Pending |
| Validation tests | ⬜ Pending |

---

## Progress Log

| Timestamp | Phase | Action | Commit SHA |
|-----------|-------|--------|------------|
| 2026-07-03 | Setup | Created `hermes-overnight` branch from `kimchi-kimi-continue` | |