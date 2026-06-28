# Code-Review-Report: `codex_rewrite` → `dev_fred`

**Datum:** 2026-06-26
**Reviewer:** Principal Engineer Review (eigene Analyse + CodeRabbit CLI v0.6.3 als Zweitquelle)
**Basis:** `dev_fred` (merge-base `140f833`)
**Methodik:** Eigene strukturierte Review (5 parallele Cluster-Analysen) + unabhängige Verifikation jedes Findings am Quellcode + CodeRabbit CLI (`coderabbit review --agent --type committed --base dev_fred --dir <dir>`).

---

# Executive Summary

`codex_rewrite` ist eine sehr umfangreiche Änderung (118 Dateien, +362.502/−102.830 Zeilen), wovon der weitaus größte Teil generierte Daten-Artefakte sind (~344k Zeilen `*.trades.json`/CSV). Der reviewbare Code-Anteil umfasst ca. 80 Quelldateien und bringt im Kern: einen neuen Grid-/Matrix-Backtest-Runner, Walk-Forward- und Portfolio-Optimizer-Erweiterungen, cTrader-Token-Refresh, Live-Preflight, neue Viewer-Endpunkte sowie zugehörige Frontend-Seiten und eine breite Testsuite.

Die Codequalität ist insgesamt solide, mit guter Test-Disziplin (insb. Live-Execution-Safety) und einer **sicherheitsrelevanten Verbesserung** (`.accounts.env` wird aus dem Tracking entfernt und gitignored). Es wurden jedoch **zwei verifizierte stille Korrektheits-Bugs** gefunden:

1. **Kritisch:** Live-Signale ohne `discord_message_id` werden ab dem zweiten Signal stillschweigend verworfen (`str(None)` → Literal `"None"`).
2. **Hoch:** Das Portfolio-Optimizer-Ziel belohnt **positive** Korrelation statt Diversifikation (falsches Vorzeichen).

Beide sind kleine, klar lokalisierte Fixes, betreffen aber Live-Pfad bzw. ein Kernfeature. **Merge-Empfehlung: Not ready to merge.**

---

# Umfang der Analyse

**Beziehung der Branches:** `kimchi-kimi-continue` ist ein echtes Superset von `codex_rewrite` (alle Codex-Commits + 5 zusätzliche Validation-Commits). Dieser Report behandelt ausschließlich `codex_rewrite`.

**Analysierte Code-Cluster (vollständig):**
- Live-Trading: [live_runner/preflight.py](live_runner/preflight.py), [live_runner/ctrader_adapter.py](live_runner/ctrader_adapter.py), [live_runner/alpaca_adapter.py](live_runner/alpaca_adapter.py), [live_runner/daemon.py](live_runner/daemon.py), [scripts/live_smoke.py](scripts/live_smoke.py)
- Datenbank: [shared/database.py](shared/database.py)
- Backtest/Optimizer: [runner/backtest_grid.py](runner/backtest_grid.py), [runner/backtest_matrix.py](runner/backtest_matrix.py), [runner/walkforward.py](runner/walkforward.py), [runner/portfolio_optimizer.py](runner/portfolio_optimizer.py), [runner/grid_tester.py](runner/grid_tester.py), [runner/optimizer.py](runner/optimizer.py), [runner/cli.py](runner/cli.py), [runner/daily_equity.py](runner/daily_equity.py), [runner/engines/vectorbt_adapter.py](runner/engines/vectorbt_adapter.py)
- Daten-Tools: [data_tools/auto_download.py](data_tools/auto_download.py), [data_tools/download_grid_market_data.py](data_tools/download_grid_market_data.py), [data_tools/downloaders/dukascopy_native.py](data_tools/downloaders/dukascopy_native.py)
- Viewer/API: [viewer/core.py](viewer/core.py), [viewer/main.py](viewer/main.py)
- Scheduler/Bots: [scheduler/jobs/signal_bot_health_check.py](scheduler/jobs/signal_bot_health_check.py), [signal_bots/health_check.py](signal_bots/health_check.py), [scripts/cron_wakeup_watchdog.sh](scripts/cron_wakeup_watchdog.sh)
- Frontend: 14 `.tsx/.ts`-Dateien unter [frontend/src/](frontend/src/)
- Tests: 24 neue/geänderte Testdateien
- Strategie: [strategies/volatility_breakout/volatility_breakout.py](strategies/volatility_breakout/volatility_breakout.py)

**CodeRabbit-Abdeckung:** vollständig für `live_runner` (0 Findings), `runner` (19 Findings), `shared` (1 Critical). Für `data_tools`, `viewer`, `scheduler`, `signal_bots`, `strategies`, `scripts`, `frontend/src` brach CodeRabbit mit **Rate-Limit (Free-Tier, 46-min Cooldown)** ab — diese Bereiche sind ausschließlich durch die eigene Review abgedeckt (siehe *Offene Fragen*).

---

# Positive Beobachtungen

- **Sicherheits-Verbesserung:** `.accounts.env` (in `dev_fred` getrackt, mit echten Konto-Secrets) wird in `codex_rewrite` **gelöscht (`D`) und gitignored** — beseitigt einen bestehenden Secret-in-Git-Mangel. Confidence: High.
- **Aufräumen:** Entfernt stale Backup [viewer/static/index.html.bak](viewer/static/index.html.bak).
- **DB-Migrationen idempotent:** Spalten werden vor `ALTER TABLE` per Existenzprüfung abgesichert, `CREATE UNIQUE INDEX IF NOT EXISTS`. Verifiziert in [shared/database.py](shared/database.py).
- **Credential-Refactor sauber propagiert:** Die cTrader-Credential-Tupel-Erweiterung (4→5 Elemente, `refresh_token`) wurde an *allen* Unpacking-Stellen (`daemon.py`, `preflight.py`) korrekt nachgezogen. Die Alpaca-Credential-Resolution gibt bei mehrdeutiger Konfiguration bewusst `None` zurück; alle Aufrufer prüfen darauf. Verifiziert.
- **Test-Disziplin:** Umfangreiche, mutation-informierte Live-Execution-Safety-Tests und Backtest/Live-Risk-Parity-Tests; keine geskippten/xfail-Tests, keine offensichtlich flaky (kein `sleep`, kein echtes Netzwerk in den Kerntests).

---

# Gefundene Probleme

## Kritisch

### K1 — Live-Signale ohne `discord_message_id` werden ab dem 2. Eintrag stillschweigend verworfen
- **Datei/Funktion:** [shared/database.py:337](shared/database.py#L337) (`LiveSignalModel.from_live_signal`) i.V.m. `save_live_signal` (Z. 933–951)
- **Schweregrad:** Kritisch — **Confidence: High** (Logik), **Medium** (Produktions-Impact, abhängig von Signalquelle)
- **Beschreibung:** `discord_message_id=str(signal.metadata.get("discord_message_id") or None)`. Fehlt der Key (z.B. interne Signale, `source="internal"`), ergibt `... or None` → `None`, und `str(None)` → der **Literal-String `"None"`**, nicht SQL-`NULL`.
- **Begründung:** Der partielle Unique-Index `... WHERE discord_message_id IS NOT NULL` erfasst `"None"` (ist nicht NULL). Das erste Signal ohne Discord-ID wird mit `"None"` gespeichert; jedes weitere trifft in der Dedup-Prüfung (Z. 939–949) auf den existierenden `"None"`-Datensatz → `logger.info("Skipping duplicate live signal ...")` → **`return` ohne `session.add`**. Folge: stille Nicht-Persistierung von Live-Signalen. CodeRabbit hat denselben Defekt unabhängig als **Critical** markiert.
- **Empfehlung:** `discord_message_id = signal.metadata.get("discord_message_id") or None` (ohne `str(...)`), sodass echtes `None` gespeichert wird. Zusätzlich Regression-Test mit zwei Signalen ohne Discord-ID.

## Hoch

### H1 — Portfolio-Optimizer: falsches Vorzeichen der Korrelations-„Penalty“ (belohnt positive Korrelation)
- **Datei/Funktion:** [runner/portfolio_optimizer.py:257](runner/portfolio_optimizer.py#L257) (`objective`)
- **Schweregrad:** Hoch — **Confidence: High** (am Code verifiziert)
- **Beschreibung:** `score = sharpe + (5.0 * avg_off_diag) - (3.0 * max(0, max_dd - 0.3))`. Der Kommentar daneben lautet „encourage negative correlation off-diagonal / minimize positive correlation“, der Code **addiert** jedoch `5.0 * avg_off_diag`.
- **Begründung:** `return -score` mit `direction="minimize"` ⇒ effektiv `score` maximieren. Höhere positive Off-Diagonal-Korrelation erhöht `score` ⇒ der Optimizer **bevorzugt konzentrierte, positiv korrelierte Portfolios** — das Gegenteil des dokumentierten Diversifikationsziels. Stiller Logikfehler in einem Kernfeature. (Von CodeRabbit gefunden, von mir bestätigt; meine eigene Cluster-Analyse hatte ihn übersehen.)
- **Empfehlung:** Vorzeichen umkehren: `score = sharpe - (5.0 * avg_off_diag) - ...`. Unit-Test mit zwei stark positiv vs. negativ korrelierten Kurven, der die bevorzugte Gewichtung prüft.

### H2 — `save_live_signal`-Dedup überschreibt fehlende `broker_order_id`-Verknüpfung nicht
- **Datei/Funktion:** [shared/database.py:945](shared/database.py#L945) (`save_live_signal`)
- **Schweregrad:** Hoch — **Confidence: Medium**
- **Beschreibung:** Bei Treffer auf `discord_message_id` wird das neue Signal komplett verworfen (`return`). Hat der existierende Datensatz noch keine `broker_order_id`, das neue aber schon, geht die Order-Verknüpfung verloren.
- **Begründung:** Reconciliation/Positions-Tracking kann auf einen Signal-Datensatz ohne Broker-Order-Bezug treffen. Relevant im Live-Pfad.
- **Empfehlung:** Bei Duplikat ein Update durchführen, wenn `existing.broker_order_id` leer und `record.broker_order_id` gesetzt ist, statt blind zu überspringen.

## Mittel

### M1 — Grid-Backtest: `subprocess.TimeoutExpired` bricht den gesamten Grid-Lauf ab
- **Datei:** [runner/backtest_grid.py](runner/backtest_grid.py) (~Z. 438–445) — *CodeRabbit, plausibel*
- **Schweregrad:** Mittel — **Confidence: Medium**
- **Beschreibung/Begründung:** `subprocess.run(..., timeout=...)` ohne `try/except TimeoutExpired`; eine einzelne hängende Zelle reißt den ganzen Grid (seriell und im `future.result()`-Pfad) ab.
- **Empfehlung:** Timeout pro Zelle abfangen, Zelle als `failed` markieren, weiterlaufen (CodeRabbit liefert konkreten Patch).

### M2 — Grid-Backtest: `attach_daily_equity` nutzt unaufgelöste `start`/`end`
- **Datei:** [runner/backtest_grid.py](runner/backtest_grid.py) (~Z. 280–325) — *CodeRabbit*
- **Schweregrad:** Mittel — **Confidence: Medium**
- **Beschreibung:** Equity-Fenster wird mit den Original-`start`/`end` statt mit den nach `_detect_date_range` aufgelösten Werten gebildet ⇒ Equity-Fenster kann vom tatsächlichen Backtest-Range abweichen (auch im Skip-Existing-Pfad).
- **Empfehlung:** Durchgängig aufgelöste Datumswerte verwenden.

### M3 — Daily-Equity: nicht-finite PnL & fehlerhafte Frequenz-Kennzeichnung im Fallback
- **Datei:** [runner/daily_equity.py](runner/daily_equity.py) (Z. 70–82) — *CodeRabbit*
- **Schweregrad:** Mittel — **Confidence: Medium**
- **Beschreibung:** `float(trade.pnl or 0.0)` lässt `nan`/`inf` durch (kontaminiert `pnl_by_day`); im Fallback wird die Original-(Nicht-Tages-)Kurve zurückgegeben, aber dennoch als `equity_frequency="daily"` markiert ⇒ Downstream-Statistik/Optimizer klassifiziert falsch.
- **Empfehlung:** `math.isfinite`-Filter; Frequenz-Metadaten im Fallback erhalten.

### M4 — Portfolio-Optimizer: Division durch 0 & Pre-Inception-Synthese in der Kurven-Alignment
- **Datei:** [runner/portfolio_optimizer.py:243](runner/portfolio_optimizer.py#L243), Z. 126–152 — *eigene + CodeRabbit*
- **Schweregrad:** Mittel — **Confidence: Medium**
- **Beschreibung:** (a) `weights = weights / weights.sum()`: Optuna kann theoretisch alle Gewichte `0.0` ziehen ⇒ NumPy liefert `nan`/`inf` (kein Crash, aber kontaminiertes Ziel). (b) `_align_equity_curves` füllt über die Vereinigungsmenge aller Zeitstempel, sodass später startende Strategien flache Pre-Inception-Werte erhalten ⇒ verzerrte Korrelations-/Sharpe-Schätzung.
- **Empfehlung:** Bei `sum()==0` gleichverteilen; Alignment auf gemeinsames Overlap-Fenster beschränken.

### M5 — Optimizer: interner `symbol_universe`-Trial-Parameter & eager Universe-Materialisierung
- **Datei:** [runner/optimizer.py](runner/optimizer.py) (Z. 235–239, 423–427) — *CodeRabbit*
- **Schweregrad:** Mittel — **Confidence: Medium**
- **Beschreibung:** Der interne Optuna-Parameter `symbol_universe` kann mit einem gleichnamigen Strategie-Parameter kollidieren und wird aus `best_params` herausgefiltert. Zudem werden alle Symbol-Subsets vorab materialisiert (Memory-Blow-up bei großen Symbolmengen).
- **Empfehlung:** Internen Parameter namespacen; Universes cappen/lazy generieren.

### M6 — Grid-Tester: `grid_id` nur sekundengenau → Kollisionen bei parallelen Läufen
- **Datei:** [runner/grid_tester.py](runner/grid_tester.py) (Z. 94–96) — *CodeRabbit*
- **Schweregrad:** Mittel — **Confidence: Medium**
- **Empfehlung:** `grid_id` um `%f` + `uuid4().hex[:8]` ergänzen (JSON-Datei + SQLite-Key eindeutig).

### M7 — Viewer Grid-Results: keine `result_id`-Validierung; neuer DELETE-Endpunkt ohne Auth
- **Datei/Funktion:** [viewer/core.py:2365](viewer/core.py#L2365) (`get_grid_result`), [viewer/core.py:2373](viewer/core.py#L2373) (`delete_grid_result`)
- **Schweregrad:** Mittel — **Confidence: Medium**
- **Beschreibung:** `path = GRID_RESULTS_DIR / f"{result_id}.json"` ohne Whitelist/`resolve()`-Containment. **Kontext (verifiziert):** Der gesamte Viewer (`core.py` *und* `main.py`) besitzt **keinerlei Auth** — der fehlende Auth-Schutz am DELETE ist somit *kein Regress*, sondern entspricht dem bestehenden Design eines lokalen Tools. Der Flask-`<string>`-Konverter matcht keine Slashes, was naives Path-Traversal begrenzt; Defense-in-Depth fehlt dennoch.
- **Begründung:** Neuer destruktiver Endpunkt (`path.unlink()`) auf unauthentifizierter Oberfläche; bei abweichendem WSGI-Routing/URL-Decoding Restrisiko.
- **Empfehlung:** `result_id` gegen `^[A-Za-z0-9_-]+$` validieren und `resolved.parent == GRID_RESULTS_DIR.resolve()` prüfen; grundsätzlich klären, ob der Viewer netz-exponiert betrieben wird.

### M8 — cTrader-Token-Persistenz ohne Dateirechte-Härtung
- **Datei/Funktion:** [live_runner/ctrader_adapter.py:117](live_runner/ctrader_adapter.py#L117) (`_write_env_key`)
- **Schweregrad:** Mittel — **Confidence: Medium**
- **Beschreibung:** Nach Token-Refresh werden `CTRADER_ACCESS_TOKEN`/`CTRADER_REFRESH_TOKEN` per `path.write_text()` in `.accounts.env` geschrieben — ohne `chmod 0o600`. Eine neu angelegte Datei erbt die umask (oft `0644`).
- **Begründung:** OAuth-Tokens gewähren vollen Trading-Zugriff. (Hinweis: Plaintext-dotenv ist das bestehende Muster; der Mangel ist das fehlende Permission-Hardening beim Runtime-Write.)
- **Empfehlung:** Nach dem Write `path.chmod(0o600)`; Parent-Verzeichnis ggf. `0o700`.

### M9 — `.accounts.example.env` enthält echte Alpaca-(Paper-)Kontonummern statt Platzhalter
- **Datei:** [.accounts.example.env](.accounts.example.env)
- **Schweregrad:** Mittel — **Confidence: High**
- **Beschreibung:** Hinzugefügt werden u.a. `ALPACA_ACCOUNT_NUMBER_ACC1=PA3E767TJ91D` (sowie ACC2/ACC3) — reale Paper-Konto-IDs in einer getrackten Template-Datei. `CTRADER_REFRESH_TOKEN` ist korrekt als Platzhalter gesetzt; API-Keys/Secrets sind nicht enthalten.
- **Empfehlung:** Kontonummern durch Platzhalter (`your_account_number_here`) ersetzen.

### M10 — Repository-Hygiene: ~344k Zeilen generierte `*.trades.json`/CSV committed
- **Schweregrad:** Mittel — **Confidence: High**
- **Beschreibung:** Der überwiegende Diff-Anteil sind generierte Backtest-Artefakte (`reports/…trades.json`, CSV). Diese sind nicht gitignored.
- **Begründung:** Aufgeblähte Historie, hohe Merge-Kosten/-Konfliktfläche, langsame Clones; CodeRabbit konnte den Full-Diff deshalb nicht verarbeiten („payload too large“).
- **Empfehlung:** Artefakt-Verzeichnisse gitignoren bzw. via Git-LFS/externen Storage; aus dem Merge-Diff herausnehmen.

## Niedrig

### N1 — `cron_wakeup_watchdog.sh`: vorhersagbarer `/tmp`-Pfad (kein `mktemp`)
- **Datei:** [scripts/cron_wakeup_watchdog.sh](scripts/cron_wakeup_watchdog.sh)
- **Schweregrad:** Niedrig — **Confidence: Medium**
- **Klarstellung (verifiziert):** `job_id` wird **gequotet als Argument** an `hermes cron run --accept-hooks "$job_id"` übergeben (kein `eval`) ⇒ **kein** Command-Injection. Verbleibendes Restrisiko: festes `/tmp/cron_failed_ids.txt` (TOCTOU/Predictable-Path auf Mehrbenutzer-Hosts).
- **Empfehlung:** `mktemp` verwenden oder Process-Substitution (`done < <(python3 ...)`); `job_id` zusätzlich gegen `^[A-Za-z0-9_-]+$` prüfen.

### N2 — `_split_equity_curves_by_time`: `train_months` ungenutzt
- **Datei:** [runner/portfolio_optimizer.py:46](runner/portfolio_optimizer.py#L46) — *CodeRabbit*
- **Schweregrad:** Niedrig — **Confidence: High**
- **Beschreibung:** Parameter wird nicht verwendet (alles vor `test_start` = Train). Docstring deklariert dies explizit als „Currently unused – reserved for future“ ⇒ irreführende, aber dokumentierte API. Walk-Forward läuft separat in [runner/walkforward.py](runner/walkforward.py).
- **Empfehlung:** Parameter entfernen oder umsetzen.

### N3 — `grid_tester.py`: `--trials` geparst, aber nicht verdrahtet
- **Datei:** [runner/grid_tester.py](runner/grid_tester.py) (Z. 134–144) — *CodeRabbit*; **Confidence: High**. Flag entfernen oder durchreichen.

### N4 — Frontend-Sammelfindings (kalibriert)
- **Dateien:** [frontend/src/pages/BacktestsPage.tsx](frontend/src/pages/BacktestsPage.tsx), [frontend/src/pages/MatrixGridPage.tsx](frontend/src/pages/MatrixGridPage.tsx), [frontend/src/components/OptimizeModal.tsx](frontend/src/components/OptimizeModal.tsx), [frontend/src/pages/LiveDetail.tsx](frontend/src/pages/LiveDetail.tsx)
- **Schweregrad:** Niedrig — **Confidence: Medium**
- **Beschreibung:** (a) `BacktestMatrixPanel` `useEffect` ohne Cancellation-Flag ⇒ mögliche State-Updates nach Unmount (MatrixGridPage macht es korrekt mit `cancelled`). (b) `OptimizeModal`: `disabled`-Logik (`parameterEntries`) weicht von der Validierung (`parameterRanges`) ab ⇒ aktivierter Button führt ggf. sofort in Validierungsfehler. (c) `Link` als Button gestylt ohne `role="button"` (leichte a11y-Inkonsistenz). *Hinweis:* Der zunächst als „kritisch“ gemeldete MatrixGrid-Filter-`useEffect` ist **kein Bug** — gefiltert wird clientseitig per `useMemo`; lediglich kein Refetch.

### N5 — Breite `except Exception`-Blöcke in `backtest_grid.py`
- **Schweregrad:** Niedrig — **Confidence: Medium**. Fehlerklassen (`json.JSONDecodeError`, `IOError`, `ValueError`) differenzieren, damit Diagnose möglich bleibt.

### N6 — Testabdeckung: Lücken & schwache Tests
- `live_runner/preflight.py` (282 LoC, Live-Sicherheit), `data_tools/download_grid_market_data.py` (Symbol-Aliasing SP500→US500), `runner/daily_equity.py` (Datums-Edge-Cases) **ohne Tests**.
- `tests/test_walkforward.py` prüft nur `inspect.signature` statt Verhalten; Portfolio-Optimizer-OOS-Tests nutzen unrealistische lineare Equity-Kurven; cTrader-Protobuf-Mocks (`SimpleNamespace`) bilden die echte API zu locker ab.
- **Confidence: High** (Coverage-Gaps), **Medium** (Test-Qualität).

---

# CodeRabbit-Findings

**Abgedeckte Bereiche:** `live_runner` (0), `runner` (19), `shared` (1 Critical). Übrige Verzeichnisse: Rate-Limit (nicht abgedeckt).

### Übernommene Findings (eigenständig verifiziert)
- `shared/database.py` `str(None)` → `"None"` (Critical) ⇒ **K1** (deckt sich exakt mit eigener Analyse).
- `portfolio_optimizer` Korrelations-Vorzeichen ⇒ **H1** (am Code als real bestätigt: `score = sharpe + 5*avg_off_diag`).
- `backtest_grid` Subprocess-Timeout ⇒ **M1**; `attach_daily_equity` unaufgelöste Daten ⇒ **M2**.
- `daily_equity` non-finite PnL / Frequenz-Fallback ⇒ **M3**.
- `portfolio_optimizer` Alignment/Trial-Guards ⇒ **M4**; `optimizer` Universe-Parameter/Explosion ⇒ **M5**.
- `grid_tester` `grid_id`-Kollision ⇒ **M6**; `--trials` ungenutzt ⇒ **N3**; `train_months` ungenutzt ⇒ **N2**.

### Verworfene / herabgestufte Findings
- Keine eindeutig falschen Findings im abgedeckten CodeRabbit-Set. **N2** (`train_months`) auf *Niedrig* herabgestuft, da im Docstring als bewusst „reserved“ dokumentiert.
- (Aus eigener Subagent-Analyse, **nicht** CodeRabbit:) „Breaking API change: `/api/compare` entfernt“ — **verworfen/falsch**: Der Endpunkt wurde lediglich von Flask ([viewer/core.py](viewer/core.py)) nach FastAPI portiert und existiert weiterhin unter [viewer/main.py:7383](viewer/main.py#L7383).

### Eigene zusätzliche Findings (nicht von CodeRabbit erfasst – Rate-Limit/Frontend)
- H2 (Dedup-Order-Linkage), M7 (Viewer-Pfad/DELETE), M8 (`chmod` Token-File), M9 (Example-Env-Kontonummern), M10 (Repo-Bloat), N1 (cron `/tmp`), N4 (Frontend), N5 (Exception-Handling), N6 (Coverage).

---

# Risiken

- **Funktional/Live:** Stille Signal-Verluste (K1) und Order-Linkage-Verlust (H2) können im Live-Betrieb zu unvollständiger Persistenz/Reconciliation führen.
- **Analytik:** H1 verfälscht Portfolio-Optimierungs-Ergebnisse systematisch (Anti-Diversifikation), ohne Fehlermeldung.
- **Robustheit:** Grid-/Matrix-Läufe können durch Einzel-Timeouts (M1) komplett scheitern; `grid_id`-Kollisionen (M6) bei Parallelität.
- **Release/Repo:** ~344k generierte Zeilen (M10) blähen Historie & Merge auf; CodeRabbit konnte den Full-Diff nicht verarbeiten.
- **Sicherheit:** Token-File ohne Härtung (M8), reale Paper-Kontonummern im Template (M9), unvalidierte Viewer-Pfade (M7) — auf einem lokalen Single-User-Host begrenzt, bei Netz-Exposition relevant.

# Empfehlungen

1. **Blocker zuerst:** K1 (`str(None)` entfernen) und H1 (Korrelations-Vorzeichen) fixen + je Regressionstest.
2. H2 (Dedup-Update statt Skip), M1 (Timeout pro Zelle), M3/M4 (finite-Checks, Overlap-Alignment) nachziehen.
3. `.accounts.example.env` (M9) bereinigen; `chmod 0600` für Token-File (M8); Viewer-`result_id` validieren (M7).
4. Generierte Artefakte gitignoren / aus dem Merge-Diff nehmen (M10).
5. Tests ergänzen für `preflight.py`, `download_grid_market_data.py`, `daily_equity.py`; `test_walkforward` auf echtes Verhalten umstellen (N6).
6. CodeRabbit-Restbereiche nach Cooldown nachholen (siehe Nächste Schritte).

---

# Merge-Empfehlung

## **Not ready to merge**

**Begründung:** Es liegen zwei **verifizierte, stille Korrektheits-Bugs** vor: ein Live-Pfad-Datenverlust (K1, von CodeRabbit unabhängig als Critical bestätigt) und eine invertierte Optimierungs-Zielfunktion (H1, am Code bestätigt). Beide produzieren keine Fehlermeldung und betreffen Kernfunktionalität bzw. den Echtgeld-nahen Signal-Pfad. Die Fixes sind klein und klar lokalisiert — nach Behebung von K1, H1 (und idealerweise H2, M1) ist eine Hochstufung auf „Ready to merge with minor fixes“ realistisch. Die positiven Aspekte (Secret-Untracking, idempotente Migrationen, saubere Credential-Propagation, starke Safety-Tests) sprechen grundsätzlich für die Branch-Qualität, heben die genannten Blocker aber nicht auf.

---

# Offene Fragen

1. **Signalquellen:** Werden Live-Signale ausschließlich aus Discord erzeugt (immer mit `discord_message_id`), oder auch intern? Davon hängt der reale Produktions-Impact von K1 ab (Logikfehler ist unstrittig).
2. **Viewer-Exposition:** Läuft der Viewer ausschließlich lokal (localhost) oder netz-exponiert? Bestimmt die Severity von M7/M8.
3. **CodeRabbit-Restabdeckung:** `data_tools`, `viewer`, `scheduler`, `signal_bots`, `strategies`, `scripts`, `frontend/src` wurden wegen Free-Tier-Rate-Limit (46-min Cooldown) **nicht** von CodeRabbit reviewt — nur durch eigene Analyse. Soll dies nach Cooldown nachgeholt werden?

# Nächste Schritte

1. K1 + H1 fixen (Blocker), Regressionstests ergänzen.
2. H2, M1, M3, M4 nachziehen.
3. M7–M10 (Security/Hygiene) adressieren.
4. CodeRabbit für die rate-limitierten Verzeichnisse nach Cooldown erneut ausführen und Findings ergänzen.
5. Re-Review der Fixes, dann erneute Merge-Bewertung.
