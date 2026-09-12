# TODO — Trading-Platform (Repo-Cleanup-Nachlese)

Stand: 2026-09-12 · Branch: `repo-cleanup` · Worktree: `/home/ubuntu/worktrees/repo-cleanup`
Quelle: Verifikation der Alt-Todos (`todo/todo.md`, `todo/kimchi-todo.md`, `overnight_report.md`,
`reports/`, `server_planning/`) gegen den Code + Sichtung von Home-Quellen
(`~/tmp/`, `~/codex-goals/`, Home-Root-`*.md`, `.claude/.codex/.commandcode/.hermes/.opencode`).
Alles als erledigt Markierte, das im Code bestätigt wurde, steht hier NICHT drin.

Detail-Unterordner:
- `cleanup-kampagne/` — Orchestrator-Triage, Merge-Auftrag (26 PRs), Reviews, Run-State
- `muse-findings/` — Bug-Findings main@`6caf461` + PRs #5–#13 + Kampagnen-Reste
- `pr-vorbereitung/` — PR #6–#13 Vorbereitung (Vision-Goals)
- `architektur/` — `src/`-Layout/Vela/PineTS-Analyse + Research-Papers
- `sonar/` — SonarQube-CLI-Cheatsheet
- Reports: `../reports/pr-prep/`, `../reports/cleanup-kampagne/`, `../reports/hermes-overnight-report.md`

Legende: `[ ]` offen · `[x]` erledigt (nur mit Code-Beleg abhaken) · Quelle in Klammern = Detaildatei.

## 1. Verifikations-Reste (aus Alt-Todos, Code-geprüft 2026-09-12)

- [ ] **M7**: `live_runner/ctrader_adapter.py:223` setzt `chmod 0o600` für Token-Datei,
  `0o700` für Parent-Dir steht nirgends (`grep 0o700` leer). Nachholen oder Alt-Report korrigieren.
- [ ] **KM1**: Alt-Report behauptet `validation/statistics/` sei gelöscht —
  `validation/statistics/cross_platform.py` existiert. Entscheiden: löschen oder als falsch markieren.
- [ ] **N1**: Nur Doku — Alt-Report meldet PENDING (fester `/tmp`-Pfad), Code nutzt längst
  `mktemp` (`scripts/cron_wakeup_watchdog.sh:25` + `trap` in `:26`). Report-Status korrigieren.
- [ ] **Viewer-Beleg (Punkt 6)**: „Python-3.11-kompatibler SciPy-Testpfad" in
  `tests/test_viewer_features.py` per Grep nicht auffindbar — Beleg nachtragen oder Satz streichen.
- [ ] **Health-Check (Punkt 9)**: `signal_bots/health_check.py` existiert, aber kein
  `health`-Subcommand in `runner/cli.py` (`:469` nur `run/engines/walkforward/grid/matrix`).
  Lauf-Log oder Korrektur nachtragen.
- [ ] **Discovery (Punkt 10)**: Output-JSON lag auf `/mnt/block-volume-data/…`, im Repo nicht
  verifizierbar; Default ist Dry-Run (`scripts/overnight_strategy_discovery.py:77`
  `publish: bool = False`). Bei Bedarf mit `--publish` regenerieren.
- [ ] **Goldens (Phase 5)**: „≥3 Goldens pro Framework" nicht erfüllt —
  `tests/fixtures/validation/goldens/` enthält nur 3 JSON gesamt
  (`eurusd_h1`, `nas100_m15`, `sp500_m1`). Auffüllen oder Anspruch senken.
- [ ] **Fixtures (Phase 2)**: XLSX nur 1 Datei (`tests/fixtures/validation/mt5_xlsx_split_fills.xlsx`);
  MT5-HTML 5, QC-Referenz 4. XLSX-Fixtures ergänzen oder dokumentieren warum 1 reicht.
- [ ] **Ferment Phase 1**: `data/catalog.json` fehlt (nur `data_tools/catalog_builder.py` + Tests).
  Generieren oder Referenz streichen. (`data/backtests/` enthält nur 4 JSON.)
- [ ] **Ferment Phase 2/3**: `data/backtests/intraday_volatility_breakout_*_report.md` (3×),
  `risk_parity_yfinance.trades.json`, `risk_parity_final_report.md` fehlen (nur gleichnamige
  `.json` ohne `_report` bzw. `risk_parity_quantconnect_reimplementation.json` da);
  `.kimchi/ferments/019e*/docs/*_iteration_report.md` (2×) fehlen (kein `.kimchi/` im Repo).
  Generieren oder Referenzen streichen.
- [x] **Runbook-Daemon (erledigt 2026-09-12)**: `docs/live-deploy-runbook.md` nutzt kein
  `--config` mehr (`live_runner/daemon.py:2541` `main()` ohne argparse, Config via
  `shared/database.py:1993 list_live_deployments`); `reports/`-Pfade → `docs/reports/`.
- [x] **Stale Refs (erledigt 2026-09-12)**: `docs/mt5_xlsx_schema.md`, `docs/validation_scope.md`
  auf `docs/todo/todo.md` umgebogen; `docs/mt5-backtest-alignment-*.md`,
  `docs/planning-prompt-for-external-model.md`, `docs/signal-risk-execution-contract.md`
  als historisch (`reports/` entfernt) markiert.

## 2. Security / Money-Path (aus `muse-findings/`, Stand main@`6caf461` — gegen aktuellen Code re-validieren!)

- [ ] WS-Log Path-Traversal + Full-File-Read (`viewer/main.py:10881`) — kritisch
- [ ] Live-Execution Check-then-Insert → Doppel-Fill (`shared/database.py`) — kritisch
- [ ] cTrader `submit_order` → `""` als Erfolg; `daemon.py:566` Exit-only schließt Shorts nie — kritisch
- [ ] Alpaca-Stream-Leak (Handler-Akkumulation, Executor-Blockade) — kritisch
- [ ] Startup-RCE via DB-Recovery (`viewer/main.py:469`) — kritisch
- [ ] Default All-in `risk_per_trade=None → 100%` — kritisch
- [ ] Discord `int()`-Crash + 20er-Fenster-Dup-Orders; `connect() → RUNNING` ohne Netz
- [ ] Fees/Slippage ≠ 0, CAGR/Sortino/Optimizer-Penalty korrekt rechnen
- [ ] `trading_engine.py:70` deprecated `max_position_pct`-Fallback entfernen (vgl. `cleanup-kampagne/cleanup-kampagne-reste-2026-09-06.md`)
- [ ] `viewer/main.py` `response_model`-Dups + 14× S3776 ≥ 40 aufräumen
- [ ] 5 Fails `test_auto_agent_backtest_timeout.py` (Namespace-Drift PR #66 → `agent_process.py`)

## 3. Cleanup-Kampagne fertig fahren (aus `cleanup-kampagne/`)

- [ ] Baseline-Tag `cleanup-baseline` + `reports/baseline-cleanup.md` (Basis-Metriken) anlegen
- [ ] Regel-Tabelle fix/suppress/won't-fix aus Sonar-Export ableiten (1190 Issues, S3776×225, S8415/09×254)
- [ ] Tracking-Ticket #68 Metadata-Bypass + Sonar-Gate `cancelled` klären
- [ ] Merge-Sequenz der 26 PRs laut `merge-auftrag-26-prs.md` abarbeiten (strikt sequenziell, STOPP bei live/risk/engine-Konflikten)
- [ ] Money-Path im Zweifel won't-fix (live_runner/risk/engines nur mit Tests anfassen)

## 4. Live-Integration / Deploy (aus `pr-vorbereitung/`, Home-Root-Goals, Codex-Goals)

- [ ] `scripts/live_smoke.py` realen `live-stockkit-e2e`-Modus (Discord-Parse → Stockkit-Route → Paper-Buy → Fill-Poll → Flatten), nie FakeAdapter (`~/codex_task.txt`)
- [ ] Discord → Alpaca-Demo E2E + Daemon-Deploy mit `live_deployments.yaml`; Draft-PR nie mergen, CodeRabbit `findings:0`
- [ ] PR-Reihenfolge #11 (float→`pytest.approx`) → #10 (ESLint+Video) → #9 (Ruff44/Sonar-C) → #13 (Ruff220 E501) → #6 read-only
- [ ] P0 zuerst: Discord-Erstorderfix + Adversarial-Checks, Sonar-PR#6-Triage (Master-Goal `~/hermes-trading-platform-master-goal.md`)
- [ ] Fix-Goal PR7/PR8/PR13 (QC-Fees, MT5-ATR, Downloader) bis Gates grün
- [ ] Codex-Goals (54 Dateien): P0-Live-Safety/Retry/Cancellation, P1-Optimizer-OOS/Promotion/Portfolio, QC-Dedup/TDD, MT5-ATR-Seeds abarbeiten
- [ ] **ENTSCHEIDUNG OFFEN**: `live_deployments.yaml` enthält echte Alpaca-Account-IDs (3 Paper-Accounts) — aus Git entfernen und nur `.example` behalten? (Vorschlag Muse-Review #7)

## 5. Backtest-Parity / Importer (aus Home-Root-Evidenz, Codex-Goals)

- [ ] MT5-Parity: Mismatch-Fenster brauchen Tick/M1-Bid-Ask-Referenz (Evidenz `~/mt5-trade-level-mapper-20260716.*`: 492 Log/XLSX-Matches)
- [ ] QC-Importer: Fee-Reconciliation + CSV-Precision + Dedup/Empty-Trade-ID fail-closed (TDD-Slices `~/codex-prompts/qc-empty-trade-id.md`)
- [ ] `strategy-comparisons/` ist von Skripten referenced (`compare_strategy_metrics.py`, `generate_golden_files.py`) — NICHT löschen
- [ ] Hermes-Overnight-Branch (`hermes-overnight-new-main-prework`, 54 exklusive Dateien) prüfen: `routing.py`, `risk_parity_optimizer.py`, llm_providers cherry-picken (Details: `../reports/hermes-overnight-report.md`); verwerfen: `explicit_qty`-Skalierung, `mt5_xlsx`-Key

## 6. Architektur-Entscheide (aus `architektur/`)

- [ ] Lightweight-Charts behalten (Vela nur isolierter Prototype, Watermark-Pflicht)
- [ ] PyneCore behalten (PineTS = AGPL + kein Python; nur ggf. Frontend-Preview hinter Flag)
- [ ] `src/`-Move phasenweise (hatch-Fix → `git mv` → `PROJECT_ROOT`/`DATA_DIR`-Fixups)
- [ ] Research-Papers (`research-papers-trading-strategies.md`): WFO-Doppel-OOS, SPY-Intraday-Momentum, VWAP/ADX-FX, Risk-Parity als Optimizer-Referenz nutzen

## 7. Repo-Hygiene (Rest aus Cleanup-Verifikation)

- [x] `100`, `.scannerwork/`, `rndm_stuff/`, `PR_REVIEW_SUMMARY.md`, artifacts-Videos, `overnight_report.md`, `todo/`, `reports/`, `server_planning/` entfernt (Commit `468f03a`)
- [ ] **ENTSCHEIDUNG OFFEN**: `docs/codex-rewrite-notes.md`, `docs/planning-prompt-for-external-model.md`, `docs/hermes-sessions/` löschen? (abgeschlossene Sessions, keine externen Refs per Grep)
- [ ] **ENTSCHEIDUNG OFFEN**: `.graphifyignore` (ungenutzt) + `.gitattributes` (schützt nur QC-CSV-Fixtures) behalten?
- [ ] **ENTSCHEIDUNG OFFEN**: `sonar-project.properties` (enthält `reports/**`, `rndm_stuff/**`, `100` in exclusions — jetzt tote Pfade) bereinigen?
- [ ] `.gitignore` neu sortieren (Coding-Agent-Block, `docs/` immer committet) — siehe Commit-Historie
- [ ] `data/` (nur 4 JSON, `.gitignore`-Regel `data/` greift nicht für Getracktes) — ent-tracken oder bewusst behalten?
- [ ] `~/tmp/review.md` ist Fremdthema (Bewerbungen) — NICHT ins Repo
