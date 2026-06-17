# Trading Platform — TODO

> **Hinweis für Hermes-Agent**: Vor jedem Punkt zuerst `overnight_report.md`,
> `git log --oneline -20` und den aktuellen Stand der genannten Dateien checken
> (was schon gemacht ist, was halbfertig ist, was kaputt ist). Nicht doppelt
> anfangen, was schon steht. Status-Notizen je Punkt gerne ins `overnight_report.md`.

Stand: 2026-06-17 · Repo: `trading-platform` · Working branch: `feature/portfolio-optimizer-bayesopt`

---

## Reihenfolge (Priorität)

1. **Live cTrader Token-Refresh automatisieren** *(blockiert Live-Trading)*
2. **Portfolio-Optimizer & Grid-Tester produktiv machen** *(Branch halbfertig)*
3. **Walk-Forward / OOS-Validation** *(Anti-Overfitting, Pflicht vor Live-Allocation)*
4. **Dukascopy-Downloader beschleunigen** *(Infrastruktur, betrifft alle Strategien)*
5. **Viewer-UI für Portfolio/Grid-Results** *(sonst unsichtbar)*
6. **DB-Konsolidierung + Tests für neue Module** *(Stabilität)*
7. **Doku & Live-Risk-Konsistenz** *(Aufräumen)*

---

## 1. Dukascopy-Downloader beschleunigen

**Ziel**: Bulk-Download von OHLCV deutlich schneller als der aktuelle
`data_tools/downloaders/dukascopy_native.py`. Downloader ist aktuell
Single-Threaded-Async und bremst jeden größeren Data-Refresh aus.

- **Status prüfen**:
  - Was macht `dukascopy_native.py` heute? Parallelität, Retries, .bi5→Parquet.
  - Wie groß sind typische Bulk-Jobs (siehe `data/ohlcv/dukascopy/`)?
  - Lohnt sich der Aufwand? Wenn `jforex_dl.py` der primäre Pfad ist und schnell
    genug, ggf. nur als Fallback verbessern.
- **Lösungsansatz (Inspiration `dukascopy-node`, JS/TS)**:
  - Parallelisierung über viele Worker / Connection-Pool.
  - HTTP/2 wenn möglich, persistente Verbindungen.
  - Chunked Range-Requests statt einzelner .bi5-Tage.
  - Optional: kleines Node-Skript (`scripts/dukascopy_fast/`) als Sidecar, von
    Python per subprocess aufrufbar. TypeScript-Skill ist im Repo vorhanden,
    bisschen Node ist okay.
  - Alternativ: komplette Python-Reimplementierung mit `aiohttp` + `httpx` HTTP/2.
- **Akzeptanz**:
  - `python -m data_tools.migrate_csv --execute` oder Bulk-Download-Test
    mindestens 3× schneller als vorher (Messung vorher/nachher).
  - Output bleibt Parquet, identisches Schema wie jetzt.
  - Fallback auf alten Downloader funktioniert.
  - Tests: `tests/test_dukascopy_downloader.py` deckt Parallel-Pfad mit Mock ab.

## 2. Automatischer Backtest-Grid

**Ziel**: Pro Strategie eine Matrix (Symbol × Timeframe × ParamSet) laufen lassen
und im Viewer sichtbar machen, welche Kombi am besten läuft.

- **Status prüfen**:
  - `runner/grid_tester.py` existiert (geändert 2026-06-17). Was fehlt:
    - Persistenz in SQLite (`backtests.db`) oder bleibt bei JSON
      (`grid_results/*.json`)?
    - Übernahme der Einzel-Backtests in die Haupt-Backtests-Tabelle oder
      separates Grid-Only-Storage?
    - Tests (`tests/test_*` für `grid_tester.py`?) fehlen aktuell.
  - UI: in `frontend/src/pages/` gibt es schon `OptimizationResultsPage.tsx`
    und `OptimizationsPage.tsx` — sind die schon verdrahtet oder brauchen sie
    Grid-Support?
- **Definition**:
  - Symbole, Timeframes, Parameter-Grid per YAML/JSON konfigurierbar.
  - Output: Heatmap-View im Viewer (Sharpe / CAGR / MaxDD / Win-Rate).
  - Pro Strategie Default-Grid (z.B. Top-20 Liquide Symbole × 1h/4h/1d).
- **Akzeptanz**:
  - Grid für `example_sma` läuft auf ≥ 5 Symbolen × ≥ 3 TFs ohne Crash.
  - Ergebnisse in SQLite abrufbar (`GET /api/grid/{strategy_id}`).
  - UI-Seite zeigt sortierbare Heatmap.
  - Tests für Grid-Result-Parsing + DB-Save.

## 3. Automatische Portfolio-Optimierung (Bayesopt)

**Ziel**: Mehrere Strategien zu einem Portfolio kombinieren mit optimaler
Gewichtung (max Sharpe, Diversifikation, Constraints).

- **Status prüfen**:
  - `runner/portfolio_optimizer.py` existiert (Optuna, lädt `*.trades.json`).
    Offene Punkte:
    - Lädt aktuell nur Legacy-JSON. Läuft `backtests.db` (SQLite) als Quelle
      auch? Falls Daten schon migriert sind, müssen wir beide Pfade
      unterstützen oder migrieren.
    - **Kein** Walk-Forward / OOS-Split. Aktuell ist alles In-Sample → Overfitting.
    - Hard constraints (z.B. `max_drawdown < 10%`) sind als weicher Penalty
      modelliert (`max_dd - 0.3`), nicht als harter Constraint.
    - Time-Series-Annualisierung hardcoded auf `sqrt(252*24*60)` — Annahme
      Minutendaten. Für höhere TFs falsch.
    - Speichert nach `data/optimizations/`. `data/portfolios/` ist leer — UI
      speichert woanders? Klären.
    - Keine Tests (`tests/test_portfolio_optimizer.py` existiert nicht).
- **Definition**:
  - Train/Test-Split z.B. 2018 → 2022 train, 2023 → heute test.
  - Walk-Forward (rollierendes Fenster) als Option.
  - Constraints: MaxDD < X %, Min-Sharpe, Max-Weight pro Strategie.
  - Optimierungs-Ziel: Sharpe, PSR (Probabilistic Sharpe Ratio), oder
    Calmar — konfigurierbar.
  - Output: Gewichte + In-Sample/OOS-Performance nebeneinander.
- **Akzeptanz**:
  - `python -m runner.portfolio_optimizer --backtest-ids ...` läuft end-to-end.
  - OOS-Sharpe wird mit-reportet, nicht nur In-Sample.
  - Constraints werden respektiert (kein Verstoß im Result).
  - UI zeigt Allocation-Pie + Equity-Curve + Korrelationsmatrix
    (siehe `PortfolioDetailPage.tsx`).
  - Tests: Optimizer konvergiert auf bekannten Synth-Daten, OOS-Block
    korrekt ausgeschnitten.

## 4. Walk-Forward / Out-of-Sample Validation (generisch)

**Ziel**: Anti-Overfitting-Layer auf Strategie-Ebene (nicht nur Portfolio).
Pflicht bevor irgendwas live geht.

- **Status prüfen**:
  - Im Code gibt es OOS-Hinweise in `tests/test_optimizer.py` (Param-Opt),
    aber keinen generischen Walk-Forward-Runner.
  - `runner/optimizer.py` und `runner/grid_tester.py` nutzen OOS aktuell nur
    teilweise.
- **Definition**:
  - Rollierendes Window: z.B. 12 Monate Train, 3 Monate Test, Step 1 Monat.
  - Aggregierte Metriken: durchschnittlicher OOS-Sharpe, Worst OOS-Drawdown,
    %-Anteil profitable OOS-Folds.
  - Lässt sich aus jedem `*.trades.json` / SQLite-Backtest ableiten.
- **Akzeptanz**:
  - CLI: `python -m runner.cli walkforward --strategy ... --train-months 12 --test-months 3`
  - Report als JSON + HTML, im Viewer abrufbar.
  - Strategien mit < 50 % profitable OOS-Folds werden geflaggt.

## 5. Viewer-UI: Portfolio & Grid sichtbar machen

**Ziel**: Was im Backend läuft, muss im Frontend benutzbar sein.

- **Status prüfen**:
  - `frontend/src/pages/` hat `PortfoliosPage.tsx`, `PortfolioDetailPage.tsx`,
    `OptimizationResultsPage.tsx`, `OptimizationsPage.tsx`. Sind die echt oder
    Stubs?
  - API-Endpunkte in `viewer/main.py` für `/api/portfolios`, `/api/grid`,
    `/api/optimizations` vorhanden?
- **Definition**:
  - Heatmap-View für Grid (Sharpe/CAGR per Symbol × TF).
  - Pie-Chart für Portfolio-Allokation.
  - Korrelationsmatrix als farbige Tabelle.
  - Walk-Forward-Equity-Curve-Overlay (Train vs Test).
- **Akzeptanz**:
  - Screenshots / Playwright-Tests (`tests/e2e/`): alle 4 Pages rendern ohne
    JS-Errors und zeigen Beispieldaten.

## 6. DB-Konsolidierung & Test-Coverage

**Ziel**: Legacy `*.trades.json` neben neuem `backtests.db` ist Risiko.

- **Status prüfen**:
  - `README.md` sagt: "JSON-Dateien bleiben kompatibel und werden beim Start
    nach SQLite migriert." Läuft die Migration tatsächlich? Wann?
  - `portfolio_optimizer.py` liest **nur** JSON — passt das noch nach
    Migration?
- **Definition**:
  - Single Source of Truth = SQLite.
  - Migration: idempotent, beim Server-Start.
  - JSON nur als Export-Format on-demand.
- **Akzeptanz**:
  - Frischer Clone + `pip install -e ".[all]"` + Server-Start → Migration
    läuft, alle alten Backtests sind in SQLite abrufbar.
  - `portfolio_optimizer.py` und `grid_tester.py` lesen aus SQLite.
  - Tests: Migration-Integration-Test, Optimizer-Test mit SQLite-Fixture.

## 7. Live cTrader Token-Refresh automatisieren

**Ziel**: `CH_ACCESS_TOKEN_INVALID` blockiert Live-Trading (siehe
`overnight_report.md`). Manueller Token-Refresh ist keine Dauerlösung.

- **Status prüfen**:
  - `live_runner/ctrader_adapter.py` (1020+ Zeilen) — wie wird der Token
    aktuell geholt? OAuth-Refresh-Flow schon implementiert oder nur Initial-Login?
  - `.accounts.env` (nicht einchecken!): sind Refresh-Token + Client-Credentials
    vorhanden?
  - Tests: `tests/test_ctrader_adapter.py`, `test_ctrader_live_fix.py`,
    `test_live_execution_safety.py` — 29 passed, decken die Refresh?
- **Definition**:
  - Token wird beim Start und bei 401 automatisch refresht.
  - Refresh schreibt neuen Token zurück nach `.accounts.env` oder Vault.
  - Slack/Log-Alert wenn Refresh 3x hintereinander fehlschlägt.
- **Akzeptanz**:
  - Demo-Account-Run läuft ≥ 24h ohne manuellen Eingriff.
  - Tests: Mock-OAuth-Server simuliert Token-Expiry → Adapter macht
    Refresh + retry automatisch.

## 8. Live-Risk-Konsistenz

**Ziel**: `risk/engine.py` (Backtest) vs. `risk/live.py` (Live) müssen
gleiche Größen berechnen, sonst klaffen Backtest und Realität.

- **Status prüfen**:
  - `risk/engine.py` und `risk/live.py` — gleiche Sizing-Logik? Gleiche
    Defaults? Tests für beide?
  - `live_runner/ctrader_adapter.py` ruft `risk.live.size_live_signal` —
    welche Parameter fließen da rein, gibt's Drift?
- **Definition**:
  - Eine geteilte `size_position(config, signal, account_state)` Funktion.
  - Backtest-Engine nutzt dasselbe Modul.
- **Akzeptanz**:
  - Parametrisierter Test: identische Inputs → identische Outputs in
    `engine.size_*` und `live.size_*`.

## 9. Doku: Portfolio-Opt, Grid, Walk-Forward, Live

**Ziel**: `README.md` ist 6KB groß und erwähnt Portfolio-Opt / Grid nicht.

- **Status prüfen**: `docs/`-Verzeichnis vorhanden, was liegt da?
- **Definition**:
  - Neues `docs/portfolio_optimizer.md`: CLI-Usage, Output-Format, Beispiel.
  - Neues `docs/grid_tester.md`: gleiches.
  - Neues `docs/walk_forward.md`: Konzepte + CLI.
  - README-Abschnitt "Backtest Workflow" mit Verlinkung.
- **Akzeptanz**:
  - Frischer Leser findet ohne Code-Lesen den Einstieg.
  - Alle neuen CLI-Commands sind in `README.md` oder verlinkt.

## 10. Signal-Bots Health-Check

**Ziel**: `signal_bots/{nitro,ramjet,rocketfuel}` — laufen die?

- **Status prüfen**:
  - `signal_bots/README.md` lesen.
  - Letzte Commits / letzte Runs in Logs.
- **Definition**: Wenn ungenutzt → dokumentieren oder archivieren.
  Wenn aktiv → Health-Check-Job im `scheduler/jobs/`.
- **Akzeptanz**: Pro Bot entweder lauffähig + im Scheduler, oder
  dokumentiert "deprecated / not in use".

---

## Out of Scope (erstmal)

- **Strategy Lab / Auto-Conversion-Agent** (NVIDIA NIM) — auf Eis bis Portfolio
  & Grid stabil laufen.

## Quellen / Kontext

- `README.md` — Architektur, CLI-Examples.
- `overnight_report.md` — letzter Status Provider + cTrader + Portfolio-Branch.
- `runner/portfolio_optimizer.py` — Stand 2026-06-17 (Optuna, nur JSON).
- `runner/grid_tester.py` — Stand 2026-06-17 (Matrix-Run).
- `live_runner/ctrader_adapter.py` — Token-Refresh unklar.
- `data_tools/downloaders/dukascopy_native.py` — langsam, Node-Alternative offen.
