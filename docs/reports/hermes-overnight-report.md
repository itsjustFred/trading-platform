# Hermes-Overnight-Branch: Review-Report

**Branch:** `hermes-overnight-new-main-prework` (HEAD `85c6664`, 2026-07-11)
**Basis:** `d953df5` ("Anchor intraday stops to market fills")
**Worktree:** `/home/ubuntu/Desktop/trading-platform-hermes`
**Umfang Branch:** 25 Commits, 151 Dateien, +2132 / −878
**Umfang main seit selber Basis:** 443 Dateien, +240871 / −107253
**Überschneidung:** 97 von 151 Branch-Dateien wurden auch auf main geändert → direkter Merge nicht sinnvoll.
**Exklusiv Branch (main seit Basis unberührt):** 54 Dateien → konfliktfrei cherry-pickbar.
**Report-Stand:** 2026-09-12, verifiziert per `git diff` beider Seiten + `py_compile` im Worktree (grün).

---

## 1. Was wurde gemacht

Ein nächtlicher Agent-Lauf hat CodeRabbit-Findings in 4 Batches (150/150/150/72 Dateien) abgearbeitet:
Review → fixen → Re-Run bis `findings:0`. Laut `reports/hermes-new-main-prework-status.md`
sind alle 4 Batches CLEAN, `py_compile` + Frontend-Build grün, pytest 831 passed / 65 skipped,
40 failed + 113 errors als "präexistent (ENV/Fixtures), keine Regression" deklariert.

Thematisch sind das ca. 150 echte Code-Fixes (Kategorien):
- **Bugs:** `lean_adapter` fehlendes `import os`, `vectorbt` falsche `total_bars`, `backtest_grid`
  `build_run_id`-Kollisionen + fehlendes `keep_indicators`-Plumbing + Timeout ohne Subprocess,
  `walkforward` profitable-Folds auf falscher Metrik + Dateinamen-Kollisionen, `pynecore`
  silent-`continue` statt fail-fast, `signal_replay` fehlender Entry-Guard.
- **Live-Sicherheit:** cTrader-Refresh-Lock + persist-before-adopt, Alpaca-Credential-Precedence
  + Fill-Guards, Daemon-None-Guards, Preflight-Timeframe-Check, `service_discovery` Leer-Token.
- **Validierung:** `code_validator` Alias-Auflösung + erweiterte Verbotsliste, `delta_report`
  `total_matched`-Semantik, MT5-Importer-Härtung, Cross-Platform-Guards.
- **Hygiene:** `_parse_bool_env` strikt, SQL-Quoting/`DOLT_BIN`, Timeframe-Normalisierung,
  Frontend-a11y/Validierung, Skript-Guards (root-Verweigerung, cwd-Fixes).

---

## 2. Kernproblem: main ist weitergezogen

Seit der Branch-Basis hat main u.a. gelandet: Cleanup-Kampagnen 14–20 (Sonar mechanisch +
semantisch), PRs #6–#13, komplette Umbauten von `viewer/main.py`, `viewer/core.py`,
`live_runner/daemon.py`, `runner/backtest_grid.py`, dazu `risk/`-Vertrag (20-A),
Import-Härtung (19-C), QC-Importer, `shared.sql`-Quoting-Helfer.

Folge: **~2/3 des Branches sind auf main bereits erledigt (oft besser), ein Teil
widerspricht neuem main-Verhalten, ein Teil ist weiterhin offen.** Details unten.

---

## 3. Bereits auf main erledigt → verwerfen

| Hermes-Fix | Stand auf main |
|---|---|
| `lean_adapter`: `import os` | drin (Z. 17) |
| `vectorbt`: echte `total_bars` | anders gelöst (Cleanup 16-A, `total_loaded_bars`, F6) |
| `risk/models.py` + `risk/config.py` Finite-Checks | überlegen gelöst (20-A: `_finite_float`-Helfer, Units-Vertrag, `max_notional_per_trade`) |
| `risk/live.py` eager-convert-Guard | Datei komplett neu (reduce-only-Pfad) |
| `viewer/main.py`: CORS `allow_credentials=False` | drin (Z. 897) |
| `viewer`: Path-Traversal `is_relative_to` | drin (3 Stellen) |
| `viewer`: Backtest-stdout-Deadline | drin + besser (Watchdog `BACKTEST_WATCHDOG_*`) |
| `viewer`: `_update_task_fields`-Einrückung | korrekt (Z. 9009 ff.) |
| `cross_platform.py` Missing-Guards | eigene Guards drin (Z. 167 ff.) |
| `database.py` IntegrityError-Guard `save_live_signal` | drin (Z. 1477/1511) |
| `resample.py` `output_dir`-Param | drin (Z. 115 ff.) |
| `service_discovery` existiert, `preflight` asyncio-Single-Call | drin (eigene Form) |
| `signal_replay` Fortschrittsmeldung | drin (`_REPLAY_PROGRESS_MESSAGE`) |
| Frontend aus Cleanup 14-A / 18-B | großteils abgedeckt, Rest siehe §5 |

---

## 4. Widerspricht aktuellem main → NICHT übernehmen (bewusst verwerfen)

1. **`daemon.py`: `explicit_qty`-Skalierung.** Hermes lässt Signal-Metadaten die
   Positionsgröße mitbestimmen. Main Cleanup 20-A verbietet genau das ("no metadata
   bypass", sizing-Keys-Guard, exit-only via `reduce_only_qty`). Übernahme wäre ein
   Sicherheits-Regression.
2. **`pynecore_adapter`: `min()` vs `max()`.** Hermes cappt strikt (`min`), main nutzt
   `max()` mit umbenannter Semantik (`max_position_exposure_pct`, 16-E). Erst Semantik
   klären, dann ggf. neu entscheiden — kein blindes Zurückportieren.
3. **`mt5_xlsx.py`: Key `max_drawdown_max_abs`.** Existiert auf main nicht (main schreibt
   `max_drawdown_abs` via `_apply_max_drawdown_max`). Hermes-Key wäre inkompatibel zu
   Verbrauchern. Verwerfen.
4. **`database.py`: Backfill-Löschung.** Hermes löscht den `equity_curve`-Backfill; main
   hat ihn als Funktion behalten. Verwerfen.
5. **`database.py`: `session.exec` → `session.execute`.** Main nutzt `session.exec`
   (SQLModel-Stil, 3 Stellen). Hermes-"Fix" vermutlich falsch. Nicht übernehmen.
6. **`alpaca_dl.py`: Paginierungs-Umbau.** Hermes behauptet, `StockBarsRequest` habe kein
   `page_token` und `alpaca-py` paginiere automatisch; main nutzt `next_page_token`-Schleife.
   Installiert ist alpaca-py 0.43.5, Signatur `(self, **data)` — unbewiesen. **Nicht
   übernehmen ohne Verifikation gegen SDK-Doku/Verhalten.**
7. **`test_models.py` PnL 1143.67 → 1144.02.** Reine Erwartungsanpassung an Hermes-Verhalten;
   auf main gelten alte Werte. Nur relevant, falls der zugehörige Fix portiert wird.

---

## 5. Weiterhin offen → Portieren lohnt sich (nach Priorität)

### P1 — Sicherheit / Korrektheit
- **`tests/validators/code_validator.py`** (exklusiv-Form, aber main hat eigene
  Weiterentwicklung + 286-Zeilen-Testdatei → Rebase nötig): Alias-Auflösung
  (`import os as o`, `from os import system as s`), `getattr`-Indirekt-Erkennung,
  Verbote `compile`/`os.popen`/`subprocess.*`, Subprocess-Check erst nach bestandenem
  Security-Check. Größter Einzelgewinn des Branches.
- **`live_runner/ctrader_adapter.py`**: `_REFRESH_TOKEN_LOCK`, Adopt-bei-gleichzeitigem-
  Refresh, persist-before-adopt, Re-Auth nach Refresh. Manuell portieren (main hat
  15-A/B/D in derselben Datei).
- **`live_runner/alpaca_adapter.py`**: striktes `_parse_bool_env` (ValueError statt silent
  False), ACC-Block-vor-Default-Precedence, Action-Validierung, Fill-Guard
  (Preis/Menge > 0). Gegen 15-B diffen.
- **`runner/strategy_ir/routing.py`** (exklusiv): unbekannter Rollout-Mode → ValueError
  statt silent Default. Klein, fail-closed. Direkt cherry-pickbar.
- **`scripts/setup_systemd.sh` / `setup_services.sh`** (exklusiv): Root-Verweigerung +
  User-Existenz-Check. Policy-Entscheidung, technisch sauber.

### P2 — Backtest-Korrektheit
- **`runner/backtest_grid.py`**: `build_run_id` + Config-Hash, Timeout-Guard (ValueError
  ohne Subprocess), Dedupe, `keep_indicators`-Plumbing. **Beim Portieren auf
  `max_position_exposure_pct` (16-E) umbenennen.**
- **`runner/walkforward.py`**: profitable-Folds auf `oos_return` (Achtung,
  Verhaltensänderung — Branch hat Tests mit angepasst), O_EXCL-Write + kollisionsfreie
  Dateinamen, `--engine`-Flag. Bewusst entscheiden, mit Test.
- **`runner/risk_parity_optimizer.py`** (exklusiv): `MAX_CANDIDATE_UNIVERSES`-Cap,
  infeasible `max_weight` → ValueError statt silent `upper=1.0`, `dropna(any)` statt
  `fillna(0)`, Vol-Epsilon entfernt (ändert Sharpe=0-Fall). Mit Test portieren.
- **`runner/engines/pynecore_adapter.py`**: fail-fast statt silent-`continue`,
  intraday-inklusives End-Datum, `all_stats` pro Symbol, Equity nach `exit_time`,
  Sidecar-Deps, Sizing nach Schleife. **Ohne die `min()`-Zeile** (siehe §4.2).
- **`runner/engines/signal_replay_adapter.py`**: `entry_price`-Required-Guard,
  ValueError→None vor Metadata-Merge, Progress-Emit im Skip-Zweig. Klein, portieren.
- **`validation/comparison/delta_report.py`** (exklusiv): `total_matched` nur bei
  `anomaly_type is None` (main zählt alles außer `duplicate_match`). Einzeiler mit
  Semantikwirkung → entscheiden + Test festnageln.
- **`scripts/backtest_to_validation_ref.py`** (exklusiv): Guards gegen leere
  `symbols`/`timeframes` statt `KeyError`/`IndexError`. Direkt cherry-pickbar.
- **`live_runner/preflight.py`**: Timeframe-Normalisierung + `VALID_TIMEFRAMES`-Check,
  Credential-ValueError-Guard, ein Event-Loop für cTrader-Check. Portieren.
- **`live_runner/service_discovery.py`**: leere Token überspringen. Einzeiler, portieren.

### P3 — Hygiene / klein
- **`runner/llm_providers/*`** (exklusiv): Timeout-Validierung, nim-Client-Injection,
  stale `error`/`temperature`-Keys poppen. Cherry-pickbar.
- **`data_tools/dolthub_manager.py`**: `DOLT_BIN`-Idee + Returncode-Check prüfen —
  main hat 19-C-Quoting via `shared.sql`, aber weiter hardcoded `"dolt"`-Strings.
  Nur das übernehmen, was main nicht hat.
- **`data_tools/*`** (Rest: histdata-Loop, dukascopy_native, jforex, `download_grid_*`
  Dedupe/Sort, `cache_manager`, `dukascopy_discovery`, `migrate_csv`, `signal_import`,
  `validate`): pro Datei gegen main diffen, vieles ist CR-Kosmetik.
- **`scripts/compare_strategy_metrics.py`, `cron_wakeup_watchdog.sh`,
  `recalculate_backtest_stats.py`, `start_viewer.*`** (alle exklusiv): PROJECT_ROOT-Defaults,
  mktemp/trap-Änderung (Geschmackssache — `trap EXIT` in Dauerschleife war tatsächlich
  fragwürdig), cwd-Fixes. Cherry-pickbar, einzeln entscheiden.
- **Frontend** (`CreateBacktestModal` Boolean-Params + Kapital/Risk-Validierung,
  `DataTable` Sort/Nulls/Keys/a11y, `Modal` Focus, `LoadingSpinner` aria,
  `TaskManager` react-query-Umbau, `useOptimizations`-Hook): pro Datei gegen 14-A/18-B
  diffen; a11y/Validierung wahrscheinlich übernehmen, `TaskManager`-Umbau Geschmackssache.
- **Tests**: neue `test_intraday_volatility_breakout.py`-Fälle + `test_risk_parity.py`-
  Ergänzungen prüfen (falls main sie nicht hat); `test_walkforward`/`test_ctrader`-
  Anpassungen nur zusammen mit den zugehörigen Fixes.
- **`validation/readiness/*`**: Hermes entfernte lediglich einen doppelt deklarierten
  `require_*`-Block (Basis hatte ihn zweimal) — main hat zusätzlich eine doppelte
  Prüfung (Helper + Inline). Optional angleichen, Verhalten identisch.

---

## 6. Überflüssig / nicht mergen

- `reports/hermes-new-main-prework-status.md` — Prozessartefakt, gehört nicht auf main.
- `reports/strategy_metric_comparison_matrix_20260630.json` — regeneriertes Artefakt.
- `.env.example`-Kommentar, `docs/*`-Einzeiler, `*-README.md`-Fixes, `rndm_stuff/Qodo Report Test.md`,
  `todo/kimchi-todo.md`-Hinweis, `viewer/static/index.html`-Null-Guard (gegen neue Viewer-Architektur prüfen, vermutlich obsolet).
- Alles aus §3 (bereits besser auf main) und §4 (widersprüchlich).

---

## 7. Empfohlene Vorgehensweise

1. **Kein Merge, kein Rebase des Branches.** 97/151 Dateien kollidieren mit neuerem main,
   teils mit widersprüchlicher Semantik (§4). Branch als Referenz liegen lassen
   (Worktree `/home/ubuntu/Desktop/trading-platform-hermes`).
2. **Konfliktfrei cherry-pickbar** (54 exklusive Dateien, davon relevant): `routing.py`,
   `llm_providers/*`, `risk_parity_optimizer.py`, `backtest_to_validation_ref.py`,
   `service_discovery.py` (klein, liegt in Overlap-Datei — manuell), Skript-Guards,
   Frontend-a11y/Validierung nach Diff.
3. **Manuell portieren** (Overlap mit main-Umbau): `code_validator.py`, `ctrader_adapter.py`,
   `alpaca_adapter.py`, `daemon.py` (nur Guards, ohne explicit_qty), `backtest_grid.py`
   (mit 16-E-Namen), `walkforward.py` (mit Test-Entscheid), `pynecore_adapter.py`
   (ohne min-Zeile), `signal_replay_adapter.py`, `preflight.py`, `delta_report.py` (mit Test).
4. **Pro Port:** Test-first wo Verhalten ändert (`tdd`-Slices), danach `/code-review`-Gate
   und Scanner-Lauf über betroffene Areas — passend zum laufenden Orchestrator/Fixer-Setup.
5. **Worktree nach Auswertung aufräumen:** `git worktree remove
   /home/ubuntu/Desktop/trading-platform-hermes` (Branch behalten als Referenz oder löschen).

## 8. Offene Fragen (needs-decision)

- `oos_return` vs `oos_sharpe` als profitabel-Definition (walkforward)?
- `total_matched`-Semantik (delta_report)?
- `min` vs `max` beim Positions-Cap (pynecore, nach 16-E-Umbenennung)?
- Alpaca-Pagination: SDK-Verhalten erst verifizieren?
- `dropna(any)` vs `fillna(0)` + Vol-Epsilon (risk_parity)?
- Root-Guards in Setup-Skripten als Policy übernehmen?
