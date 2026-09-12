# Offene Punkte aus Repo-Cleanup-Verifikation

Stand: 2026-09-08 · Branch: `repo-cleanup` · Quelle: Zeile-für-Zeile-Prüfung von
`overnight_report.md`, `todo/todo.md`, `todo/kimchi-todo.md`, `reports/` und
`server_planning/` gegen den Code. Alles als erledigt Markierte, das im Code
bestätigt wurde, steht hier NICHT drin – nur Abweichungen und Fehlendes.

## 1. overnight_report.md – Abweichungen

- [ ] **M7 unvollständig**: `live_runner/ctrader_adapter.py:223` setzt `chmod 0o600`
  für die Token-Datei, aber `0o700` für das Parent-Dir steht nirgends im Code
  (`grep 0o700` leer). Entweder nachholen oder Report korrigieren.
- [ ] **KM1 falsch**: Report behauptet `validation/statistics/` sei gelöscht –
  `validation/statistics/cross_platform.py` existiert. Entscheiden: wirklich
  löschen oder Report korrigieren.
- [ ] **N1 überholt**: Report meldet PENDING (fester `/tmp`-Pfad), Code nutzt aber
  längst `mktemp` (`scripts/cron_wakeup_watchdog.sh:25` + `trap` in `:26`).
  Nur Report-Status aktualisieren, kein Code nötig.

## 2. todo/todo.md – unbelegte Behauptungen

- [ ] **Punkt 6 (Viewer-UI)**: „Python-3.11-kompatibler SciPy-Testpfad“ in
  `tests/test_viewer_features.py` per Grep nicht auffindbar – Beleg nachtragen
  oder Satz streichen.
- [ ] **Punkt 9 (Health-Check)**: „5/5 OK“-Lauf und `health`-CLI sind aus dem Code
  nicht nachweisbar (`signal_bots/health_check.py` existiert, aber kein
  `health`-Subcommand in `runner/cli.py`). Lauf-Log oder Korrektur nachtragen.
- [ ] **Punkt 10 (Discovery)**: Output-JSON `discovery_20260618_0924.json` lag auf
  `/mnt/block-volume-data/discovered_strategies/` und ist im Repo nicht
  verifizierbar; Default ist Dry-Run (`publish=False`). Bei Bedarf mit
  `--publish` regenerieren.

## 3. todo/kimchi-todo.md – fehlende Artefakte

- [ ] **Phase 5 (Goldens)**: „≥3 Goldens pro Framework“ nicht erfüllt –
  `tests/fixtures/validation/goldens/` enthält nur 3 JSON gesamt
  (`eurusd_h1`, `nas100_m15`, `sp500_m1`).
- [ ] **Phase 2 (Fixtures)**: Für XLSX liegt nur 1 Datei vor
  (`tests/fixtures/validation/mt5_xlsx_split_fills.xlsx`); MT5-HTML hat 5,
  QC-Referenz 4 Dateien.
- [ ] **Ferment Phase 1**: `data/catalog.json` fehlt (nur
  `data_tools/catalog_builder.py` + Tests vorhanden). Generieren oder Referenz
  streichen.
- [ ] **Ferment Phase 2/3**: `data/backtests/intraday_volatility_breakout_*_report.md`
  (3×), `data/backtests/risk_parity_yfinance.trades.json` und
  `data/backtests/risk_parity_final_report.md` fehlen (nur gleichnamige `.json`
  ohne `_report` bzw. `risk_parity_quantconnect_reimplementation.json` da);
  `.kimchi/ferments/019e*/docs/*_iteration_report.md` (2×) fehlen komplett
  (kein `.kimchi/` im Repo).

## 4. reports/live-deploy-runbook.md – veraltet

- [ ] **Daemon-Befehl (Z. 42)**: `live_runner/daemon.py:2541` hat `def main() -> None`
  ohne argparse – `--config live_deployments.yaml` wird nicht akzeptiert (Config
  läuft über `list_live_deployments()`). Befehl im Runbook korrigieren.
- [ ] **Stale Refs in docs/**: `docs/mt5-backtest-alignment-plan.md` und
  `docs/mt5-backtest-alignment-handoff-prompt.md` verweisen auf nicht existente
  `reports/strategy-comparison-platform-check-2026-06-23.md` und
  `reports/strategy_comparison_backtest_*.trades.json`. Links fixen oder Dateien
  als bewusst entfernt markieren.

## 5. Stale Verweise durch Todo-Umzug

- [ ] `docs/mt5_xlsx_schema.md:263` und `docs/validation_scope.md:55` verweisen auf
  `todo/kimchi-todo.md`. Nach Löschen von `todo/` auf `docs/todo/todo.md`
  umbiegen oder streichen.
