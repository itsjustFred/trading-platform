# Strategy Validation Framework — Scope & Grenzen

> **Historical planning note (Phase 1, 2026-06-24).** Kept for the record. It describes the
> scoping of the validation framework as it stood then, so several statements no longer match
> the repository: the branch `kimchi-kimi-continue` it names no longer exists; `runner/engine.py`
> and `live_runner/` have changed many times since (execution logic is no longer out of
> bounds); and the external frameworks once listed as later extensions now have engine
> adapters (`runner/engines/lean_adapter.py`, `runner/engines/pynecore_adapter.py`). Read
> `docs/validation_architecture.md`, `docs/validation_inventory.md` and
> `docs/mt5_xlsx_schema.md` for the current state.

**Datum:** 2026-06-24
**Branch:** `kimchi-kimi-continue`
**Phase:** 1 — Repository Analysis & Scoping

---

## In-Scope

| Bereich | Beschreibung |
|---------|--------------|
| **Import & Normalisierung** | MT5-XLSX-Reports (primär) und MT5-HTML-Reports (Fallback) einlesen und in `ValidationReference` normalisieren. |
| **Domänenmodell** | `ValidationReference` mit Pflichtfeldern: `strategy_name`, `source_framework`, `symbol`, `timeframe`, `settings`, `trades`, `stats`, `raw_metadata`. |
| **Trade Matching** | Vergleich von Entry/Exit Time, Direction, Entry/Exit Price, Volume, Position Size, PnL, Stop Loss, Take Profit zwischen externer Referenz und internem `BacktestResult`. |
| **Trade Anomalies** | Erkennung von Missing Trades, Extra Trades und Mismatches. |
| **Risk Matching** | Isolierter, unabhängig testbarer Risk-Layer mit Fixed-Risk- und Risk-%-Modellen; Vergleich von Stop Distance, Risk Per Trade und Position Size. |
| **Statistics Comparison** | Berechnung von Kennzahlen aus Trades: Win Rate, Profit Factor, Net Profit, Gross Profit, Gross Loss, Max Drawdown, Sharpe, Sortino, Recovery Factor. Plausibilitäts-/Regressionsebene gegen externe Report-Stats. |
| **Regression Testing** | Mindestens drei Golden-Files pro unterstütztem Framework; Regressionen zeigen nachvollziehbar auf, welche Trades/Kennzahlen sich bei Änderungen an Risk/Execution/Stats-Logik ändern. |
| **Reporting** | Agentenfreundlicher JSON-Report und menschenlesbarer Markdown-Report mit erklärten Abweichungen. |
| **Paper-Trading-Readiness** | Checkliste und Validator, die feststellen, ob ein Backtest-Ergebnis für Paper Trading bereit ist — ohne Paper-Trading-Implementierung auszubauen. |
| **Tests** | Alle neuen Module werden durch `pytest`-Tests abgedeckt; `pytest tests/` bleibt grün. |

---

## Out-of-Scope

| Bereich | Begründung |
|---------|------------|
| **Live-Trading-Implementierung** | Nicht Teil dieses Frameworks; es geht ausschließlich um Validierung und Vergleich von Backtest-Ergebnissen. |
| **Paper-Trading-Implementierung** | Die Readiness wird geprüft, aber keine Order-Routing-, Broker- oder Live-Ausführungslogik wird gebaut. |
| **Änderung bestehender Ausführungslogik** | `runner/engine.py`, `live_runner/` und Broker-Integrationen bleiben unverändert. |
| **Frontend / UI** | Reports werden als Dateien (JSON/Markdown) erzeugt; keine Web-Oberfläche. |
| **Weitere externe Frameworks außer MT5** | LEAN, Stockkit, QuantConnect etc. sind als spätere Erweiterungen vorgesehen, werden aber nicht in diesem Ferment implementiert. |
| **Echtzeit-Validierung während des Backtests** | Das Framework vergleicht fertige Backtest-Reports, nicht laufende Backtests. |

---

## Annahmen

1. MT5-Reports liegen als XLSX-Dateien vor; HTML ist Fallback.
2. XLSX-Reports enthalten mindestens Orders- und Trades/Deals-Tabellen mit den benötigten Spalten.
3. Interne `BacktestResult`-Objekte können aus dem Vergleichs-Runner oder einer Datei geladen werden.
4. Zeitzonen werden soweit möglich normalisiert (ISO 8601 UTC); bei fehlender Zeitzoneninfo wird Broker-Serverzeit angenommen.
5. Commission/Swap sind in MT5-Reports optional oder 0 (Demo); die rohen MT5-Werte (mit Vorzeichen) liegen pro Trade in `Trade.meta.commission` / `Trade.meta.swap`, die normalisierten Kosten in `Trade.fees` (positiv = Kosten, negativ = Netto-Swap-Gutschrift). `raw_metadata` trägt die report-level Metadaten (Kopf-Block, `position_fills`-Evidence) und blockiert den Import nicht. Siehe Regel 5 in `docs/mt5_xlsx_schema.md`.

---

## Akzeptanzkriterien

- `docs/validation_scope.md` existiert und dokumentiert In-Scope/Out-of-Scope.
- `docs/validation_architecture.md` dokumentiert Architektur und Reuse-vs-Neu-Entscheidung.
- `docs/validation_inventory.md` listet bestehende relevante Module auf.
- `docs/mt5_xlsx_schema.md` beschreibt das XLSX-Spaltenschema.
- Offene Punkte aus der Verifikation sind in den GitHub-Issues getrackt.
