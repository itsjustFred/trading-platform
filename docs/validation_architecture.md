# Strategy Validation Framework — Zielarchitektur

**Datum:** 2026-06-24  
**Branch:** `kimchi-kimi-continue`  
**Phase:** 1 — Repository Analysis & Scoping

---

## 1. Scope-Grenzen

### In-Scope
- Import und Normalisierung externer Backtest-Reports (primär MT5-XLSX, HTML-Fallback) in ein gemeinsames `ValidationReference`-Modell.
- Systematischer Vergleich externer Referenzdaten mit internen `BacktestResult`-Daten.
- Trade Matching (Entry/Exit Time, Direction, Entry/Exit Price, Volume, Position Size, PnL, SL, TP).
- Risk Matching (Fixed-Risk, Risk-%, Stop Distance, Risk Per Trade, Position Size).
- Statistics Comparison (Win Rate, Profit Factor, Net Profit, Gross Profit/Gross Loss, Max Drawdown, Sharpe, Sortino, Recovery Factor).
- Regression Testing mit Golden Files.
- Paper-Trading-Readiness-Checks.
- Agentenfreundliche und menschenlesbare Reports.

### Out-of-Scope
- Implementierung von Live-Trading.
- Implementierung von Paper-Trading.
- Änderung der bestehenden Ausführungs- oder Broker-Logik.
- UI/Frontend für das Validation Framework (Reports sind Datei-basiert).

---

## 2. Datenfluss

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Externer Report                                                             │
│  • MT5-XLSX  (primär)                                                       │
│  • MT5-HTML  (Fallback)                                                     │
│  • später: QuantConnect, LEAN, Stockkit                                     │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Importer / Converter Layer                                                  │
│  • MT5XLSXImporter                                                          │
│  • MT5HTMLImporter                                                          │
│  • ConverterRegistry                                                        │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ ValidationReference (Pydantic)                                              │
│  Pflichtfelder:                                                             │
│    strategy_name, source_framework, symbol, timeframe, settings,            │
│    trades, stats, raw_metadata                                              │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Vergleichs-Engine                                                           │
│  • TradeComparator       → matched / missing / extra / mismatch             │
│  • RiskComparator        → stop distance, risk amount, position size        │
│  • StatsComparator       → stat deltas mit Toleranzen                       │
│  • RegressionEngine      → Golden-File-Vergleich                            │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ ValidationResult                                                            │
│  • status (passed / partial / failed)                                       │
│  • deviations[]                                                             │
│  • score                                                                    │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Reporting Layer                                                             │
│  • Structured JSON Report                                                   │
│  • Markdown Report (menschenlesbar)                                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Reuse vs. Neuer Layer — Entscheidung

### Wiederverwendung (Reuse)

| Komponente | Pfad | Begründung |
|------------|------|------------|
| `BacktestResult` | `shared/models.py` | Bereits kanonisches Zielformat; `ValidationReference` kann intern in `BacktestResult` konvertiert werden, um den Trade-Vergleich zu ermöglichen. |
| `BacktestMeta` | `shared/models.py` | Enthält zeitliche und kapitalbezogene Metadaten. Erweiterungen nur über `raw_metadata` in `ValidationReference` nötig. |
| `Trade` + `TradeExit` | `shared/models.py` | Trade-Level-Vergleich erfordert identische Struktur; externe Formate sollen `Trade`-Objekte erzeugen. |
| `BacktestStats` | `shared/models.py` | Deckt 90% der externen Reports ab; zusätzliche Felder (z.B. `recovery_factor`) werden in `extras` abgelegt. |
| `compute_stats()` | `runner/stats.py` | Berechnung interner Kennzahlen; für Validation werden interne Stats neu berechnet und mit externen Referenzwerten verglichen. |
| `EquityPoint` | `shared/models.py` | Einheitliches Equity-Kurven-Format; ggf. für Equity-Vergleich später nutzbar. |

### Neuer Layer (Neu erstellen)

| Komponente | Pfad | Begründung |
|------------|------|------------|
| `ValidationReference` | `validation/models/reference.py` | Zentrales Aggregat mit Pflichtfeldern und Provenienz-Informationen (`source_framework`, `raw_metadata`). |
| `ValidationResult` | `validation/models/result.py` | Ergebnis des Vergleichs mit Status, Score und Abweichungsliste. |
| `Deviation` | `validation/models/result.py` | Einzelne Abweichung: Feld, erwarteter Wert, tatsächlicher Wert, Toleranz, Schweregrad. |
| `MT5XLSXImporter` | `validation/importers/mt5_xlsx.py` | Primärer Importer für MT5-XLSX-Reports. |
| `MT5HTMLImporter` | `validation/importers/mt5_html.py` | Fallback-Importer für MT5-HTML-Reports. |
| `BaseImporter` | `validation/importers/base.py` | Gemeinsames Interface für alle Importer. |
| `TradeComparator` | `validation/comparators/trades.py` | Erkennt matched/missing/extra/mismatch-Trades. |
| `RiskComparator` | `validation/comparators/risk.py` | Vergleicht Stop Distance, Risk Per Trade, Position Size. |
| `StatsComparator` | `validation/comparators/stats.py` | Vergleicht Statistiken mit Toleranzen. |
| `RegressionEngine` | `validation/regression/engine.py` | Golden-File-basierter Regressionstest. |
| `ReportGenerator` | `validation/reporting/` | JSON- und Markdown-Berichte. |
| `PaperReadinessChecker` | `validation/paper/readiness.py` | Bereitet Paper-Trading-Checks vor. |

### Begründung

Das bestehende Modell- und Stats-System ist reif für Wiederverwendung. Der Hauptaufwand liegt in der Erstellung der **Validation-Schicht** (Referenzmodell, Toleranzvergleich, neue MT5-Importer) und nicht in der Änderung bestehender Codebasen. `BacktestResult` bleibt das interne Kanon; `ValidationReference` ist der externe Normalisierungsrahmen, der Provenienz- und Vergleichsmetadaten trägt.

---

## 4. Package-Struktur

```
trading-platform/
├── validation/
│   ├── __init__.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── reference.py      # ValidationReference
│   │   └── result.py         # ValidationResult, Deviation
│   ├── importers/
│   │   ├── __init__.py
│   │   ├── base.py           # BaseImporter
│   │   ├── mt5_xlsx.py       # MT5XLSXImporter
│   │   └── mt5_html.py       # MT5HTMLImporter
│   ├── comparators/
│   │   ├── __init__.py
│   │   ├── trades.py         # TradeComparator
│   │   ├── risk.py           # RiskComparator
│   │   └── stats.py          # StatsComparator
│   ├── statistics/
│   │   ├── __init__.py
│   │   └── trade_stats.py    # Stats aus Trades (Recovery Factor ergänzt)
│   ├── regression/
│   │   ├── __init__.py
│   │   └── engine.py         # Golden-File Regression
│   ├── reporting/
│   │   ├── __init__.py
│   │   ├── structured.py     # JSON Report
│   │   └── markdown.py       # Markdown Report
│   ├── paper/
│   │   ├── __init__.py
│   │   └── readiness.py      # Paper Readiness Checks
│   └── cli.py                # Validation CLI
├── tests/validation/
│   ├── test_validation_reference.py
│   ├── test_mt5_xlsx_importer.py
│   ├── test_mt5_html_importer.py
│   ├── test_trade_matcher.py
│   ├── test_trade_comparison.py
│   ├── test_trade_anomalies.py
│   ├── test_risk_adapter.py
│   ├── test_fixed_risk_model.py
│   ├── test_percent_risk_model.py
│   ├── test_risk_comparison.py
│   ├── test_trade_statistics.py
│   ├── test_stats_comparison.py
│   ├── test_regression.py
│   ├── test_report_structured.py
│   ├── test_report_markdown.py
│   └── test_paper_readiness.py
├── tests/fixtures/validation/
│   ├── mt5_xlsx/
│   │   ├── EURUSD_H1.xlsx
│   │   ├── NAS100_M15.xlsx
│   │   └── SP500_M1.xlsx
│   ├── mt5_html/
│   │   └── sample_report.html
│   └── goldens/
│       ├── eurusd_h1_golden.json
│       ├── nas100_m15_golden.json
│       └── sp500_m1_golden.json
└── docs/
    ├── validation_inventory.md
    ├── mt5_xlsx_schema.md
    ├── validation_architecture.md
    ├── validation_scope.md
    └── paper-readiness.md
```

---

## 5. Nächste Schritte

1. **Phase 2 — Import & Normalization**
   - `ValidationReference` implementieren.
   - `MT5XLSXImporter` bauen (robust gegen variable Header-Offsets).
   - `MT5HTMLImporter` als Fallback.
   - Golden Files erzeugen.

2. **Phase 3 — Trade Comparison Engine**
   - Trade-Matcher auf Entry/Exit Time + Symbol.
   - Delta-Engine für alle relevanten Felder.

3. **Phase 4 — Risk Separation**
   - Adapter zur `risk.engine.RiskEngine`.
   - Fixed-Risk und Risk-%-Modelle isoliert testen.

4. **Phase 5 — Statistics & Regression**
   - Stats aus Trades berechnen (Recovery Factor ergänzen).
   - Golden-File-Regression.

5. **Phase 6 — Reporting**
   - JSON- und Markdown-Reports.

6. **Phase 7 — Paper Readiness**
   - Checkliste und Validator.
