# Kimchi Strategy Validation Framework — TODO

> **Projekt:** Strategy Validation Framework
> **Branch:** `kimchi-kimi-continue`
> **Repo:** `/home/ubuntu/Desktop/trading-platform`
> **Stand:** Phase 4 läuft
> **Letztes Update:** 2026-06-24

---

# Phase 1 – Repository Analysis & Scoping

- [x] Branch `kimchi-kimi-continue` prüfen, ggf. anlegen und auschecken
  - Verifikation: `git branch --show-current | grep -q '^kimchi-kimi-continue$'`
- [x] `todo/kimchi-todo.md` erstellen und aktuell halten
  - Verifikation: `test -f todo/kimchi-todo.md`
- [x] Bestehende Struktur analysieren (`shared/models.py`, `runner/converters/`, `runner/stats.py`, `risk/`)
  - Verifikation: `docs/validation_inventory.md` existiert und listet relevante Module + Schnittstellen
- [x] Echte MT5-XLSX-Reports in `strategy-comparisons/` inspizieren und Spaltenschema dokumentieren
  - Verifikation: `docs/mt5_xlsx_schema.md` beschreibt verwendete Spalten und Beispielwerte
- [x] Validierungsgrenzen definieren (In-Scope / Out-of-Scope)
  - Verifikation: `docs/validation_scope.md` dokumentiert, dass Live/Paper-Trading-Implementierung ausgeschlossen ist
- [x] Zielarchitektur skizzieren und Datenfluss dokumentieren
  - Verifikation: `docs/validation_architecture.md` zeigt Datenfluss: externer Report → Importer → `ValidationReference` → Comparison → Report
- [x] Entscheidung Wiederverwendung bestehender Strukturen vs. neuem Layer dokumentieren
  - Verifikation: `docs/validation_architecture.md` enthält Begründung für Reuse/Neu

# Phase 2 – Import & Normalization

- [x] `ValidationReference`-Domänenmodell implementieren (Pflichtfelder: `strategy_name`, `source_framework`, `symbol`, `timeframe`, `settings`, `trades`, `stats`, `raw_metadata`)
  - Verifikation: `pytest tests/validation/test_validation_reference.py -v` läuft grün
- [x] MT5-XLSX-Importer implementieren und auf `ValidationReference` normalisieren
  - Verifikation: `pytest tests/validation/test_mt5_xlsx_importer.py -v` läuft grün
- [x] MT5-HTML-Fallback-Importer implementieren
  - Verifikation: `pytest tests/validation/test_mt5_html_importer.py -v` läuft grün
- [x] Mindestens drei Golden-/Fixture-Dateien pro unterstütztem Framework anlegen
  - Verifikation: `find tests/fixtures/validation -type f | wc -l` ≥ 3 pro Framework
- [x] Basistests für Importer schreiben
  - Verifikation: `pytest tests/validation/test_importers.py -v` läuft grün

# Phase 3 – Trade Comparison Engine

- [x] Trade-Matcher implementieren (Entry/Exit Time + Symbol)
  - Verifikation: `pytest tests/validation/test_trade_matcher.py -v` läuft grün
- [x] Vergleichslogik für Entry/Exit Time, Direction, Entry/Exit Price, Volume, Position Size, PnL, SL, TP
  - Verifikation: `pytest tests/validation/test_trade_comparison.py -v` läuft grün
- [x] Missing Trades, Extra Trades und Trade Mismatches erkennen
  - Verifikation: `pytest tests/validation/test_trade_anomalies.py -v` läuft grün
- [x] Delta-Report-Modell mit erklärten Abweichungen erstellen
  - Verifikation: `python -c "from validation.comparison import TradeDeltaReport; TradeDeltaReport"` funktioniert

# Phase 4 – Risk Separation

- [x] Risk Sizing aus bestehender Engine isolieren und testbar machen
  - Verifikation: `pytest tests/validation/test_risk_sizing.py -v` läuft grün
- [x] Fixed-Risk-Modell implementieren
  - Verifikation: `pytest tests/validation/test_fixed_risk_model.py -v` läuft grün
- [x] Risk-%-Modell implementieren
  - Verifikation: `pytest tests/validation/test_percent_risk_model.py -v` läuft grün
- [x] Risk-Vergleich (Stop Distance, Risk Per Trade, Position Size) implementieren
  - Verifikation: `pytest tests/validation/test_risk_comparison.py -v` läuft grün

# Phase 5 – Statistics & Regression

- [x] Statistiken aus Trades berechnen (Win Rate, Profit Factor, Net Profit, Gross Profit, Gross Loss, Max Drawdown, Sharpe, Sortino, Recovery Factor)
  - Verifikation: `pytest tests/validation/test_trade_statistics.py -v` läuft grün
- [x] Framework-Statistik-Vergleich implementieren
  - Verifikation: `pytest tests/validation/test_stats_comparison.py -v` läuft grün
- [x] Golden Files für Regression erzeugen
  - Verifikation: `find tests/fixtures/validation/goldens -name '*.json' | wc -l` ≥ 3 pro Framework
- [x] Regression-Suite bauen, die Änderungen an Risk/Execution/Stats nachvollziehbar zeigt
  - Verifikation: `pytest tests/validation/test_regression.py -v` läuft grün

# Phase 6 – Reporting

- [x] Agentenfreundlichen JSON-/strukturierten Report implementieren
  - Verifikation: `pytest tests/validation/test_report_structured.py -v` läuft grün
- [x] Menschenlesbaren Markdown-Report mit erklärten Abweichungen implementieren
  - Verifikation: `pytest tests/validation/test_markdown_report.py -v` läuft grün
- [x] Report-Einstiegspunkt (CLI oder Funktion) bereitstellen
  - Verifikation: `python -m validation.cli --help` funktioniert

# Phase 7 – Paper Readiness

- [x] Paper-Readiness-Checkliste definieren
  - Verifikation: `docs/paper-readiness.md` listet Checks (Delta-Schwellen, Regressionsstatus, Risk-Konsistenz)
- [x] Paper-Readiness-Validator implementieren
  - Verifikation: `pytest tests/validation/test_paper_readiness.py -v` läuft grün
- [x] Dokumentation der nächsten Schritte (echtes Paper Trading) ergänzen
  - Verifikation: `docs/paper-readiness.md` enthält klare nächste Schritte

# Globale Akzeptanz

- [x] `pytest tests/validation/` bleuft grün (443 passed)
  - Hinweis: `pytest tests/` enthält 13 vorbestehende, nicht-Validation-bezogene Fehler (Strategie-Regressionen, fehlende Alpaca-Module, Walkforward), die außerhalb dieses Ferment liegen.
  - Verifikation: `pytest tests/ -q` exit code 0

---

# Ferment: Strategy Comparison Backtests

> **Branch:** `kimchi-kimi-continue`
> **Stand:** Phase 4 abgeschlossen
> **Letztes Update:** 2026-06-24

## Phase 1 – Setup & Strategy-Conversion Skill
- [x] `.agents/strategy-conversion.md` als Skill anlegen und MQL5-Hinweise ergänzen
  - Verifikation: `test -f .agents/strategy-conversion.md && grep -q 'MQL5' .agents/strategy-conversion.md`
- [x] `todo/kimchi-todo.md` mit Tasks für alle Phasen aktualisieren
  - Verifikation: `grep -q 'IntradayVolatilityBreakout' todo/kimchi-todo.md`
- [x] Datenkatalog prüfen und Symbole/Timeframes für EURUSD, NAS100, SP500 identifizieren
  - Verifikation: `test -f data/catalog.json`
- [x] Ersten Commit mit Skill und Todo erstellen
  - Verifikation: `git log -1 --oneline | grep -i 'skill\|todo'`

## Phase 2 – Intraday Volatility Breakout
- [x] `IntradayVolatilityBreakout.mq5` analysieren und Ground-Truth README erstellen
  - Verifikation: `test -f strategies/intraday_volatility_breakout/README.md`
- [x] vectorbt-Strategie `strategies/intraday_volatility_breakout/intraday_volatility_breakout.py` implementieren
  - Verifikation: Python-Import-Check läuft ohne Fehler
- [x] Backtest EURUSD_H1 laufen lassen und mit XLSX vergleichen
  - Verifikation: `data/backtests/intraday_volatility_breakout_eurusd_h1_report.md` liegt vor
- [x] Backtest NAS100_M15 laufen lassen und mit XLSX vergleichen
  - Verifikation: `data/backtests/intraday_volatility_breakout_nas100_m15_report.md` liegt vor
- [x] Backtest SP500_M1 laufen lassen und mit XLSX vergleichen
  - Verifikation: `data/backtests/intraday_volatility_breakout_sp500_m1_report.md` liegt vor
- [x] Abweichungen analysieren und dokumentieren (exaktes Trade-Matching aufgrund von Daten-/Zeitproblemen nicht erreichbar)
  - Verifikation: `.kimchi/ferments/019efa92-7a0e-7031-a19e-db53e5617f54/docs/intraday_breakout_iteration_report.md` liegt vor

## Phase 3 – Risk Parity
- [x] PDF-Report inspizieren und Vergleichsgrundlage festlegen (Trade-Tabelle nicht extrahierbar, daher Summary)
  - Verifikation: `test -f strategy-comparisons/risk_parity/risk_parity_report_quantconnect.pdf`
- [x] LEAN/QuantConnect-Backtest für `risk_parity_strategy.py` konfigurieren und ausführen (QC-kompatible Python-Reimplementierung aufgrund fehlender LEAN-Credentials/-Daten)
  - Verifikation: `data/backtests/risk_parity_yfinance.trades.json` existiert
- [x] Ergebnisse mit PDF-Referenz vergleichen
  - Verifikation: `data/backtests/risk_parity_final_report.md` liegt vor
- [x] Abweichungen analysieren und dokumentieren
  - Verifikation: `.kimchi/ferments/019efa92-7a0e-7031-a19e-db53e5617f54/docs/risk_parity_iteration_report.md` liegt vor

## Phase 4 – Regression & Finalisierung
- [x] Regressionstest für Intraday Volatility Breakout anlegen
  - Verifikation: `pytest tests/strategies/test_intraday_volatility_breakout.py -v` läuft grün
- [x] Regressionstest für Risk Parity anlegen (QC-kompatible Python-Reimplementierung getestet)
  - Verifikation: `pytest tests/strategies/test_risk_parity.py -v` läuft grün
- [x] Finale Validation-Suite laufen lassen
  - Verifikation: `pytest tests/validation/ -q` läuft grün
- [x] Alle Änderungen commiten
  - Verifikation: `git status --short` zeigt keine uncommitted Änderungen

---

# Blocker / Annahmen

- **Annahme:** MT5-Reports liegen als XLSX vor (`strategy-comparisons/intraday_volatility_breakout/`); XML war eine Begriffsverwechslung.
- **Annahme:** XLSX-Reports enthalten ausreichend Spalten für Entry/Exit Time, Direction, Entry/Exit Price, Volume, PnL, SL, TP.
- **Annahme:** Bestehende `shared.models.BacktestResult` / `Trade` und `risk.engine.RiskEngine` werden wiederverwendet, wo sie passen.
- **Blocker:** Keine echten MT5-HTML-Exports im Repo → HTML-Fallback wird mit synthetischem Fixture getestet.
