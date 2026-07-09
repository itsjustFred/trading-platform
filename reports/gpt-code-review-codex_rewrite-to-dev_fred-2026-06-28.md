# Code Review: codex_rewrite -> dev_fred

**Datum:** 2026-06-28
**Branch:** `codex_rewrite`
**Merge-Ziel:** `dev_fred`
**Ergebnis:** Nicht merge-ready

## Kurzfazit

`codex_rewrite` bringt viel nützliche Funktionalität mit: Backtest-Grid/Matrix, Portfolio-Optimizer-Erweiterungen, Live-Preflight, cTrader-Refresh, SQLite-Erweiterungen und Frontend-Flächen. Die Branch ist aber noch nicht ready für einen Merge auf `dev_fred`.

Die Hauptgründe sind echte Integrationsrisiken in Live-Trading- und Datenbankpfaden, ein reproduzierbarer Grid-Download-Abbruch für bestimmte Timeframes, viele Whitespace-Check-Fehler und eine sehr große Menge an generierten Artefakten im Diff.

## Geprüfter Umfang

- Diff `dev_fred...codex_rewrite`
- Merge-Basis: `140f833b610416e4d21e67024abfbee236c1f62f`
- Umfang: 118 Dateien, ca. 362k Insertions und 103k Deletions
- CodeRabbit CLI verwendet:
  - Gesamt-Diff: abgebrochen wegen `Payload too large`
  - Teilreviews:
    - `data_tools`: 2 Findings
    - `live_runner`: 8 Findings
    - `shared`: 3 Findings
    - weitere Teilreviews durch CodeRabbit-Rate-Limit blockiert

## Blocker

### 1. cTrader-Refresh kann nach Tokenwechsel account-scoped Requests ohne erneute Account-Auth retryen

**Datei:** `live_runner/ctrader_adapter.py`
**Relevante Stellen laut CodeRabbit:** ca. Zeilen 556-560, 594, 210, 381-387

Nach erfolgreichem Token-Refresh wird der Request einmal wiederholt. Für cTrader account-scoped Requests muss nach einem neuen Access Token aber wieder Account-Authorization erfolgen. Sonst kann Recovery aus Auth-Fehlern im Live-Betrieb weiterhin scheitern oder inkonsistent reagieren.

Zusätzlich schreibt der Code Access- und Refresh-Token getrennt in `.accounts.env`. Ein Crash zwischen beiden Writes kann ein nicht zusammenpassendes Tokenpaar persistieren. CodeRabbit hat außerdem eine fehlende Serialisierung paralleler Refreshes markiert: überlappende Refreshes können alte Refresh Tokens invalidieren und anschließend stale Credentials speichern.

**Merge-Einschätzung:** Blocker, weil Live-Trading/Auth betroffen ist.

### 2. Live-Preflight kann bei fehlerhafter Boolean-Env versehentlich Live statt Demo wählen

**Datei:** `live_runner/preflight.py`
**Relevante Stellen laut CodeRabbit:** ca. Zeilen 55-58 und 70-71

`CTRADER_DEMO` defaultet auf `True`, aber ein Tippfehler wie `tru` wird aktuell als `False` interpretiert. Damit kann ein Connectivity-Check unabsichtlich gegen Live statt Demo laufen.

**Merge-Einschätzung:** Blocker, weil das Risiko operativ teuer werden kann.

### 3. Live-Preflight verwendet bei cTrader-Refresh nicht zwingend die geprüfte Accounts-Datei

**Datei:** `live_runner/preflight.py`
**Relevante Stellen laut CodeRabbit:** ca. Zeilen 74-79, 151-159, 228-233

`run_live_preflight(..., accounts_env_path=...)` liest eine bestimmte Credentials-Datei, erstellt den `CTraderLiveAdapter` aber ohne diesen Pfad. Falls der Connectivity-Check Token refreshen muss, kann eine andere/default `.accounts.env` geschrieben werden als die Datei, die gerade validiert wird.

**Merge-Einschätzung:** Blocker, weil Preflight sonst falsche Sicherheit erzeugt und Credentials am falschen Ort rotiert.

### 4. SQLite-Migration füllt neue Dedupe-/Equity-Spalten nicht zurück

**Datei:** `shared/database.py`
**Relevante Stellen:** ca. Zeilen 904-919 und 1745-1748

Die Migration fügt `live_signals.discord_message_id` hinzu und erstellt einen Unique Index, backfillt aber vorhandene Discord IDs aus `metadata` nicht. Danach findet `save_live_signal` alte Signale nicht und kann Duplikate für bereits verarbeitete Discord Messages erzeugen.

Ähnlich bei `backtests.equity_curve`: bestehende Backtests bekommen `[]`, und `migrate_json_to_sqlite(skip_existing=True)` überspringt existierende IDs. Der Portfolio-Optimizer liest dann leere Equity Curves aus SQLite, obwohl die JSON-Dateien bzw. `equity_points` Daten enthalten.

**Merge-Einschätzung:** Blocker, weil bestehende Daten nach Upgrade funktional schlechter werden können.

### 5. `save_live_signal` hat eine Race Condition trotz Unique Index

**Datei:** `shared/database.py`
**Relevante Stellen:** ca. Zeilen 939-951

Die Dedupe-Prüfung ist ein Pre-Check vor `session.commit()`. Zwei Reconnect-/Worker-Prozesse können beide keinen vorhandenen Datensatz sehen; einer gewinnt, der andere bekommt `IntegrityError`. Anders als `save_live_execution` behandelt `save_live_signal` diesen Fall nicht.

**Merge-Einschätzung:** Blocker für robuste Live-Signal-Verarbeitung.

### 6. Grid-Market-Data-Download bricht bei `m30`/`30m` vor Fehlerbehandlung ab

**Datei:** `data_tools/download_grid_market_data.py`
**Relevante Stellen:** Zeilen 47-53 und 167

`TIMEFRAME_ALIASES` mappt `m30` auf `30m`, aber die Sortierliste in `download_symbol` kennt `30m` nicht:

```python
["1m", "5m", "15m", "1h", "4h", "1d"].index(tf)
```

Das wirft `ValueError` vor dem `try`-Block und kann den gesamten Batch abbrechen, statt das Symbol sauber als failed zu markieren.

**Merge-Einschätzung:** Blocker für die neue Grid-Datenpipeline.

## Weitere Risiken

- `git diff --check dev_fred...codex_rewrite` schlägt fehl. Betroffen sind viele Markdown-Dateien und mehrere Python-Dateien, u.a. `runner/grid_tester.py`, `runner/portfolio_optimizer.py`, `scripts/overnight_strategy_discovery.py`, `signal_bots/health_check.py`, `strategy-comparisons/risk_parity/risk_parity_strategy.py` und diverse Tests.
- `.accounts.env` wird im Diff gelöscht, was grundsätzlich richtig sein kann, aber die Branch enthält weiterhin Credential-bezogene Änderungen an `.accounts.example.env`. Vor Merge sollte geprüft werden, ob keine echten Secrets mehr in Git-Historie und Reports landen.
- Der Diff enthält sehr große generierte Artefakte: `graphify-out/graph.json`, `frontend/public/matrix-grid-overview.json`, mehrere `reports/*.trades.json`, XLSX/PDF-Dateien und alte Report-Dumps. Diese sollten vor Merge kuratiert oder aus dem Produktiv-Branch ausgelagert werden.
- Frontend-Build konnte nicht validiert werden, weil im Review-Environment `tsc` nicht installiert war.
- Ruff konnte nicht validiert werden, weil `ruff` im Review-Environment nicht installiert war.

## Verifikation

Erfolgreich:

- `python -m compileall data_tools live_runner runner scheduler scripts shared signal_bots strategies viewer`
- `python -m pytest tests/test_database_migrations.py tests/test_ctrader_live_fix.py tests/test_live_preflight.py tests/test_grid_market_data_download.py`
  - 18 passed
- `python -m pytest tests/test_runner_cli.py tests/test_backtest_grid.py tests/test_backtest_matrix.py tests/test_auto_download.py`
  - 10 passed

Nicht erfolgreich / nicht ausführbar:

- `coderabbit review --plain --base dev_fred --type committed`
  - Fehler: Payload too large
- mehrere CodeRabbit-Teilreviews
  - teilweise Fehler: Review limit reached
- `python -m ruff check .`
  - Fehler: `No module named ruff`
- `npm run build`
  - Fehler: `tsc: not found`
- `git diff --check dev_fred...codex_rewrite`
  - Fehler: viele trailing-whitespace Treffer

## Empfehlung

Nicht auf `dev_fred` mergen.

Vor Merge sollten mindestens die Live-Trading/Auth-Findings, die SQLite-Migrations-/Dedupe-Findings und der `30m`-Grid-Download-Bug behoben werden. Danach sollten `git diff --check`, Ruff, Frontend-Build und ein erneuter CodeRabbit-Review auf kleineren Diffs oder nach Artefaktbereinigung laufen.