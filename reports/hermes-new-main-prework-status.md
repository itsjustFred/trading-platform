# CodeRabbit Findings Cleanup — Status

**Branch:** `hermes-overnight-new-main-prework`
**Ziel:** Alle CodeRabbit-Findings für den Branch bereinigen (4 Batches à 150/150/150/72 Dateien). Pro Batch: CR-Review → Findings fixen → Re-Run bis `findings:0`.
**Stand:** 2026-07-11 (laufend)

---

## Fortschritt

### Batch 1 (150 Dateien) — `findings: 51 → 4 → 3 → ?`
| Schritt | Findings | Commit | Notes |
|--------|----------|--------|-------|
| Initial-Review | 51 (1 critical, 29 major, 21 minor) | — | |
| Fix-Runde 1 | — | `ff695ee` | 41 Dateien; u.a. `histdata_dl` 12-Monats-Loop, `alpaca_dl` Client-Pagination |
| Re-Run 1 | 4 | `fc23d21` | `resample.py` output_dir, `Modal.tsx` focus-ref, `OptimizationsPage.tsx` a11y/String |
| Re-Run 2 | 3 | `8cb7be0` | alle in `data_tools/dolthub_manager.py`: `DOLT_BIN` statt hardcoded `dolt`, returncode-Check, `normalize_timeframe('1d')` |
| Re-Run 3 | läuft (proc_0bbdbf08e89c) | — | **CR-Free-Tier-Rate-Limit** → Harness retried automatisch um 08:17 UTC |

### Batch 2 (150 Dateien) — `findings: 50 → fix`
| Schritt | Findings | Commit | Notes |
|--------|----------|--------|-------|
| Initial-Review | 50 (3 critical, 26 major, 21 minor) | — | |
| Fix-Runde 1 | — | `4be53d3` | alle 50 gefixt (scripts/runner/risk/shared/scheduler/server_planning/reports) |
| Test-Align | — | `2d4fe89`, `cbebf74` | Tests an CR-verändertes Verhalten angepasst (walkforward + ctrader) — keine Code-Regressionen |
| Re-Run | pending | — | wartet auf CR-Rate-Limit-Reset |

Critical Findings behandelt:
- `runner/engines/lean_adapter.py` — fehlendes `import os` (CRITICAL)
- `risk/live.py` — `entry_price` eager-convert von `signal.theoretical_price` entfernt (2× CRITICAL)

Hinweis: G4-Subagent hatte `server_planning/01_current_state.md` irrtümlich komplett in CRLF umgewandelt (472-Zeilen-Spamm-Diff). Revertet; echter Finding (klare E-Mail im `.gitconfig`-Eintrag) manuell redigiert.

### Batch 3 (150 Dateien)
- Temp-Repo gebaut (`/tmp/cr-b03`, 150 Dateien, leerer Base).
- CR-Initial-Review **pending** (CR-Rate-Limit).

### Batch 4 (72 Dateien)
- Temp-Repo gebaut (`/tmp/cr-b04`, 72 Dateien, leerer Base).
- CR-Initial-Review **pending** (CR-Rate-Limit).

---

## Verifikation
- `python3 -m py_compile` aller geänderten `.py`-Dateien: **GRÜN**
- `npm --prefix frontend run build`: **GRÜN**
- `pytest` (valide Subset, 15 broken Module ausgeschlossen):
  - Run 1: 826 passed, 44 failed.
  - Walkforward-Regressionen (3) behoben in `2d4fe89` → Run 2: 829 passed, 41 failed.
  - **4 Failures waren echte Regressionen** durch CR-korrekte Code-Änderungen, alle behoben:
    - `walkforward.py` (3): `step_months` aus Portfolio-Config entfernt, `n_profitable_folds`
      zählt OOS-Return statt Sharpe, kollisions-sicheres Dateiformat `wf_..._<micro>_<uuid8>`.
      Fix `2d4fe89` (Tests angepasst + `oos_return` in Portfolio-Fold propagiert) → 14/14 grün.
    - `ctrader_adapter.py` (1): Re-Auth nach Token-Refresh (`send_account_auth_request`)
      vor Retry. Fix `cbebf74` (Re-Auth-Step im Test gemockt) → alle ctrader-Tests grün.
  - Verbleibende **40 failures + 15 collection errors sind präexistente Umgebungs-/Logikfehler**
    (keine Regression durch die CR-Fixes): fehlendes `apscheduler`/`sentry_sdk`/`yfinance` im
    Test-`.venv`; Tests importieren nie committetes `shared.database._normalize_optional_id` und
    `validation.reporting._sanitize_json_numbers`; `portfolio_optimizer.align_equity_curves`/
    `_normalize_weights` (Datei von mir **nicht** geändert) schlägt mit Shape-Assertions fehl
    (präexistente Logik/NumPy-Version); `grid_tester`/`live_smoke` via `sentry_sdk`/`apscheduler` env.
  - Finaler Re-Run (proc_55c…) bestätigt: nur noch env/präexistente Fehler.

---

## Blocker
**CodeRabbit Free-Tier-Rate-Limit:** ~3 Reviews pro 42-Minuten-Fenster. Nach Erschöpfung
schläft der Harness (`run_cr.sh`) automatisch und retried. CR-Reviews werden sequenziell
nach Reset getriggert — erwartetes Gesamttempo ~3 Reviews / 42 min.

---

## Nächste Schritte
1. Batch-1-Re-Run 3 abwarten (Retry 08:17 UTC) → ggf. Rest-Findings fixen bis `findings:0`.
2. Batch-2-Re-Run nach Rate-Limit-Reset → bei Rest-Findings fixen + erneut.
3. Batch-3/4 Initial-Reviews + Fix-Runden + Re-Runs bis clean.
4. Finaler Abschlussreport, sobald alle 4 Batches `findings:0`.
