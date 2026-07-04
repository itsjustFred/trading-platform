# Hermes Overnight Fix Report

**Branch:** `hermes-overnight` (from `kimchi-kimi-continue`)
**Started:** 2026-07-03
**Target:** Fix all critical/high/medium issues from code reviews

---

## Phase 1: P0 Merge-Blocker (Critical/High) — ✅ COMPLETE

| ID | Fix | File(s) | Status | Commit |
|----|-----|---------|--------|--------|
| **K1** | `discord_message_id = metadata.get("discord_message_id")` (no `str()`) → stores real `NULL` | `shared/database.py:337` + `save_live_signal` | ✅ Done | `227dc73` |
| **H1** | Flip correlation penalty sign: `score = sharpe - 5.0 * avg_off_diag` | `runner/portfolio_optimizer.py:257` | ✅ Done | `227dc73` |
| **H2** | On dedup hit: if `existing.broker_order_id` empty && `record.broker_order_id` set → update instead of skip | `shared/database.py:945` | ✅ Done | `227dc73` |
| **M3** | `math.isfinite` filter on PnL; fix fallback frequency metadata | `runner/daily_equity.py` | ✅ Done | `227dc73` |

---

## Phase 2: P1 High Robustness (M1–M10) — ✅ COMPLETE

| ID | Fix | File(s) | Status |
|----|-----|---------|--------|
| **M1** | Catch `subprocess.TimeoutExpired` per grid cell, mark `failed`, continue | `runner/backtest_grid.py` | ✅ Done |
| **M2** | Pass resolved `start`/`end` to `attach_daily_equity` (not original params) | `runner/backtest_grid.py` | ✅ Done |
| **M3** | `math.isfinite` filter on PnL; fix fallback frequency metadata | `runner/daily_equity.py` | ✅ Done |
| **M4** | Guard `weights.sum() == 0`; align on overlap window only | `runner/portfolio_optimizer.py` | ✅ Done |
| **M5** | `grid_id` = timestamp + `%f` + `uuid4().hex[:8]` | `runner/grid_tester.py` | ✅ Done |
| **M6** | Add `30m` to sort list; validate timeframes early per symbol/tf | `data_tools/download_grid_market_data.py` | ✅ Done |
| **M7** | `path.chmod(0o600)` after token write; parent dir `0o700` | `live_runner/ctrader_adapter.py:117` | ✅ Done |
| **M8** | Replace real Alpaca account numbers with placeholders | `.accounts.env.example` | ✅ Done |
| **M9** | Git-ignore generated artifacts (`*.trades.json`, CSV in `reports/`); remove from index | `.gitignore` + `git rm --cached` | ✅ Done |
| **M10** | Backfill Discord-Dedupe & Equity-Curves in migration (idempotent) | `shared/database.py` migration | ✅ Done |

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
| Core logic tests | ✅ 168 passed |
| DB migration tests | ✅ 2 passed |
| CLI tests | ⬜ Pending |
| Validation tests | ⬜ Pending |

---

## Progress Log

| Timestamp | Phase | Action | Commit SHA |
|-----------|-------|--------|------------|
| 2026-07-03 | Setup | Created `hermes-overnight` branch from `kimchi-kimi-continue` | |
| 2026-07-03 | P0 | Fixed K1, H1, H2, M3 (DB discord_message_id, optimizer correlation sign, broker_order_id reconciliation, daily_equity finite checks) | `227dc73` |
| 2026-07-03 | P1 | M1, M2: Timeout handling & resolved dates in backtest_grid | |
| 2026-07-03 | P1 | M4: Portfolio optimizer weight guard & overlap alignment | |
| 2026-07-03 | P1 | M5: grid_id collision fix with microseconds + UUID | |
| 2026-07-03 | P1 | M6: 30m timeframe support & early validation | |
| 2026-07-03 | P1 | M7: cTrader token file permissions (0o600/0o700) | |
| 2026-07-03 | P1 | M8: Alpaca account number placeholders | |
| 2026-07-03 | P1 | M9: Git-ignore generated artifacts + remove from index | |
| 2026-07-03 | P1 | M10: Discord-Dedupe & Equity-Curves backfill migration | |