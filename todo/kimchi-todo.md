# Kimchi Strategy Validation Framework — TODO

> **Projekt:** Strategy Validation Framework  
> **Branch:** `kimchi-kimi-continue`  
> **Repo:** `/home/ubuntu/Desktop/trading-platform`  
> **Stand:** Phase 1 läuft  
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

- [ ] `ValidationReference`-Domänenmodell implementieren (Pflichtfelder: `strategy_name`, `source_framework`, `symbol`, `timeframe`, `settings`, `trades`, `stats`, `raw_metadata`)
  - Verifikation: `pytest tests/validation/test_validation_reference.py -v` läuft grün
- [x] MT5-XLSX-Importer implementieren und auf `ValidationReference` normalisieren
  - Verifikation: `pytest tests/validation/test_mt5_xlsx_importer.py -v` läuft grün
- [ ] MT5-HTML-Fallback-Importer implementieren
  - Verifikation: `pytest tests/validation/test_mt5_html_importer.py -v` läuft grün
- [ ] Mindestens drei Golden-/Fixture-Dateien pro unterstütztem Framework anlegen
  - Verifikation: `find tests/fixtures/validation -type f | wc -l` ≥ 3 pro Framework
- [ ] Basistests für Importer schreiben
  - Verifikation: `pytest tests/validation/test_importers.py -v` läuft grün

# Phase 3 – Trade Comparison Engine

- [ ] Trade-Matcher implementieren (Entry/Exit Time + Symbol)
  - Verifikation: `pytest tests/validation/test_trade_matcher.py -v` läuft grün
- [ ] Vergleichslogik für Entry/Exit Time, Direction, Entry/Exit Price, Volume, Position Size, PnL, SL, TP
  - Verifikation: `pytest tests/validation/test_trade_comparison.py -v` läuft grün
- [ ] Missing Trades, Extra Trades und Trade Mismatches erkennen
  - Verifikation: `pytest tests/validation/test_trade_anomalies.py -v` läuft grün
- [ ] Delta-Report-Modell mit erklärten Abweichungen erstellen
  - Verifikation: `python -c "from validation.comparison import TradeDeltaReport; TradeDeltaReport"` funktioniert

# Phase 4 – Risk Separation

- [ ] Risk Sizing aus bestehender Engine isolieren und testbar machen
  - Verifikation: `pytest tests/validation/test_risk_adapter.py -v` läuft grün
- [ ] Fixed-Risk-Modell implementieren
  - Verifikation: `pytest tests/validation/test_fixed_risk_model.py -v` läuft grün
- [ ] Risk-%-Modell implementieren
  - Verifikation: `pytest tests/validation/test_percent_risk_model.py -v` läuft grün
- [ ] Risk-Vergleich (Stop Distance, Risk Per Trade, Position Size) implementieren
  - Verifikation: `pytest tests/validation/test_risk_comparison.py -v` läuft grün

# Phase 5 – Statistics & Regression

- [ ] Statistiken aus Trades berechnen (Win Rate, Profit Factor, Net Profit, Gross Profit, Gross Loss, Max Drawdown, Sharpe, Sortino, Recovery Factor)
  - Verifikation: `pytest tests/validation/test_trade_statistics.py -v` läuft grün
- [ ] Framework-Statistik-Vergleich implementieren
  - Verifikation: `pytest tests/validation/test_stats_comparison.py -v` läuft grün
- [ ] Golden Files für Regression erzeugen
  - Verifikation: `find tests/fixtures/validation/goldens -name '*.json' | wc -l` ≥ 3 pro Framework
- [ ] Regression-Suite bauen, die Änderungen an Risk/Execution/Stats nachvollziehbar zeigt
  - Verifikation: `pytest tests/validation/test_regression.py -v` läuft grün

# Phase 6 – Reporting

- [ ] Agentenfreundlichen JSON-/strukturierten Report implementieren
  - Verifikation: `pytest tests/validation/test_report_structured.py -v` läuft grün
- [ ] Menschenlesbaren Markdown-Report mit erklärten Abweichungen implementieren
  - Verifikation: `pytest tests/validation/test_report_markdown.py -v` läuft grün
- [ ] Report-Einstiegspunkt (CLI oder Funktion) bereitstellen
  - Verifikation: `python -c "from validation.reporting import generate_report; generate_report"` funktioniert

# Phase 7 – Paper Readiness

- [ ] Paper-Readiness-Checkliste definieren
  - Verifikation: `docs/paper-readiness.md` listet Checks (Delta-Schwellen, Regressionsstatus, Risk-Konsistenz)
- [ ] Paper-Readiness-Validator implementieren
  - Verifikation: `pytest tests/validation/test_paper_readiness.py -v` läuft grün
- [ ] Dokumentation der nächsten Schritte (echtes Paper Trading) ergänzen
  - Verifikation: `docs/paper-readiness.md` enthält klare nächste Schritte

# Globale Akzeptanz

- [ ] `pytest tests/` bleibt grün
  - Verifikation: `pytest tests/ -q` exit code 0

---

# Blocker / Annahmen

- **Annahme:** MT5-Reports liegen als XLSX vor (`strategy-comparisons/intraday_volatility_breakout/`); XML war eine Begriffsverwechslung.
- **Annahme:** XLSX-Reports enthalten ausreichend Spalten für Entry/Exit Time, Direction, Entry/Exit Price, Volume, PnL, SL, TP.
- **Annahme:** Bestehende `shared.models.BacktestResult` / `Trade` und `risk.engine.RiskEngine` werden wiederverwendet, wo sie passen.
- **Blocker:** Keine echten MT5-HTML-Exports im Repo → HTML-Fallback wird mit synthetischem Fixture getestet.
