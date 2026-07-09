# Code Review: kimchi-kimi-continue -> dev_fred

**Datum:** 2026-06-28  
**Branch:** `kimchi-kimi-continue`  
**Merge-Ziel:** `dev_fred`  
**Ergebnis:** Nicht merge-ready

## Kurzfazit

`kimchi-kimi-continue` baut auf `codex_rewrite` auf und erweitert die Branch um ein umfangreiches Strategy-Validation-Framework, MT5-Importer, Risk-Sizing-/Comparison-Module, Golden Files und Strategieports. Die neue Validation-Schicht wirkt testseitig deutlich stärker als die Basis-Branch: die relevante Suite lief mit 460 bestandenen Tests durch.

Trotzdem ist die Branch nicht ready für einen Merge auf `dev_fred`, weil sie alle Blocker aus `codex_rewrite` erbt und zusätzlich eigene Merge-Hygiene-Probleme einführt: Rename von `.accounts.example.env`, sehr große `.kimchi`-Runtime-/Backtest-Artefakte im Repo und ein roter Whitespace-Check.

## Geprüfter Umfang

- Gesamt-Diff `dev_fred...kimchi-kimi-continue`
- Inkrementeller Diff `codex_rewrite...kimchi-kimi-continue`
- Merge-Basis zu `dev_fred`: `140f833b610416e4d21e67024abfbee236c1f62f`
- Gesamtumfang gegen `dev_fred`: 211 Dateien, ca. 981k Insertions und 103k Deletions
- Inkrementeller Kimchi-Umfang gegen `codex_rewrite`: 95 Dateien, ca. 619k Insertions

## Blocker

### 1. Alle `codex_rewrite`-Blocker werden geerbt

`kimchi-kimi-continue` enthält `codex_rewrite` vollständig. Damit gelten die dort gefundenen Merge-Blocker weiter:

- cTrader-Refresh retryt account-scoped Requests ohne sichere erneute Account-Auth.
- cTrader-Tokenpaare werden nicht atomar persistiert und parallele Refreshes sind nicht serialisiert.
- Live-Preflight kann durch fehlerhafte Boolean-Env unabsichtlich Live statt Demo verwenden.
- Live-Preflight kann Token in eine andere Accounts-Datei schreiben als die geprüfte.
- SQLite-Migration backfillt neue Dedupe-/Equity-Spalten nicht.
- `save_live_signal` behandelt Unique-Index-Races nicht.
- `data_tools/download_grid_market_data.py` kann bei `m30`/`30m` vor der Fehlerbehandlung abbrechen.

**Merge-Einschätzung:** Solange `codex_rewrite` nicht ready ist, kann `kimchi-kimi-continue` nicht ready sein.

### 2. `.accounts.example.env` wird in `.accounts.env.example` umbenannt

**Diff:** `R100 .accounts.example.env -> .accounts.env.example`

Das ist ein stiller Contract-Break für Setup-/Onboarding-Pfade, die die bisherige Datei erwarten. In `codex_rewrite` wurde gerade `.accounts.env` aus dem Tracking entfernt und `.accounts.example.env` als sichere Vorlage behalten. Kimchi benennt diese Vorlage um, ohne dass aus dem Review ersichtlich ist, dass alle Referenzen, Docs und lokalen Workflows darauf angepasst wurden.

**Merge-Einschätzung:** Blocker für Merge-Hygiene. Entweder Rename rückgängig machen oder alle Referenzen und Setup-Anweisungen konsistent migrieren.

### 3. Große `.kimchi`-Runtime- und Backtest-Artefakte sind committed

**Beispiele aus `codex_rewrite...kimchi-kimi-continue`:**

- `.kimchi/ferments/...events.jsonl`
- `.kimchi/ferments/...runtime.json`
- `.kimchi/ferments/.../reviews/phase-phase-*.json`
- `.kimchi/ferments/019efa92-.../docs/risk_parity_20260316_172727.trades.json`
- `.kimchi/ferments/019efa92-.../docs/risk_parity_yfinance.trades.json`
- `.kimchi/ferments/019efa92-.../docs/risk_parity_yfinance_vref.json`

Der größte einzelne inkrementelle Block ist ein `.trades.json` mit über 522k Zeilen. Das sieht nach Tool-Laufzeitstate bzw. generierten Prüf-Artefakten aus, nicht nach Quellcode oder kuratierten Fixtures. Diese Dateien blähen History, Reviews und zukünftige Merges stark auf.

**Merge-Einschätzung:** Blocker, außer das Team will `.kimchi` bewusst versionieren. Dann sollte es dokumentiert und stark kuratiert werden.

### 4. Whitespace-Check schlägt im Kimchi-Diff fehl

`git diff --check codex_rewrite...kimchi-kimi-continue` meldet trailing whitespace, u.a. in:

- `.agents/strategy-conversion.md`
- `docs/mt5_xlsx_schema.md`
- `docs/validation_architecture.md`
- `docs/validation_inventory.md`
- `docs/validation_scope.md`
- `todo/kimchi-todo.md`

Das ist leicht zu beheben, aber ein roter Git-Hygiene-Check sollte nicht in `dev_fred` gemerged werden.

## Positive Signale

- Das neue `validation/`-Paket kompiliert.
- Die Validation-Testabdeckung ist breit: CLI, Importer, Risk Models, Trade Matching, Delta Reports, Regression Runner, Paper Readiness, Golden Files und Strategy Tests.
- Der inkrementelle Funktionscode ist im Vergleich zu den Artefakten recht kohärent strukturiert:
  - `validation/importers/*`
  - `validation/risk/*`
  - `validation/comparison/*`
  - `validation/reporting/*`
  - `validation/readiness/*`
  - `strategies/intraday_volatility_breakout/*`

## Verifikation

Erfolgreich:

- `python -m compileall validation strategies/intraday_volatility_breakout scripts/run_risk_parity_backtest.py scripts/backtest_to_validation_ref.py scripts/generate_golden_files.py`
- `python -m pytest tests/validation tests/strategies/test_intraday_volatility_breakout.py tests/strategies/test_risk_parity.py`
  - 460 passed

Nicht erfolgreich / nicht ausführbar:

- `git diff --check codex_rewrite...kimchi-kimi-continue`
  - Fehler: trailing whitespace in mehreren Docs/Todo-Dateien
- CodeRabbit-Gesamtreview gegen `dev_fred`
  - durch Größe der Basis-Branch und Rate Limits nicht sinnvoll vollständig ausführbar
- Frontend-Build und Ruff wurden für die Gesamtbranch nicht abschließend validiert; siehe `codex_rewrite`-Report für die Umgebungslimits.

## Empfehlung

Nicht auf `dev_fred` mergen.

Empfohlene Reihenfolge:

1. Erst `codex_rewrite`-Blocker beheben.
2. Danach Kimchi inkrementell bereinigen:
   - `.accounts.example.env` Rename rückgängig machen oder vollständig dokumentiert migrieren.
   - `.kimchi`-Runtime-State und große generierte Trade-Artefakte aus dem Merge-Diff entfernen oder in einen separaten Artefakt-Speicher verschieben.
   - `git diff --check` sauber bekommen.
3. Danach Validation-Suite erneut laufen lassen und CodeRabbit auf den bereinigten inkrementellen Diff anwenden.

Nach diesen Schritten kann `kimchi-kimi-continue` fachlich interessant für `dev_fred` werden. Im aktuellen Zustand ist die Branch wegen geerbter Live-/DB-Risiken und eigener Repo-Hygiene-Probleme nicht merge-ready.