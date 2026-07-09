# Strategy Validation Framework — Modell-Inventur

**Datum:** 2026-06-24  
**Phase:** 1 — Repository Analysis & Scoping  
**Ziel:** Bestehende Modelle, Converter, Stats und Risk-Engine analysieren, um Mapping und Lücken für das Strategy Validation Framework zu identifizieren.

---

## 1. Übersicht der analysierten Module

| Modul | Pfad | Zweck |
|-------|------|-------|
| Shared Models | `shared/models.py` | Kanonische Datentypen: `BacktestResult`, `Trade`, `BacktestStats`, etc. |
| Stockkit Converter | `runner/converters/stockkit_converter.py` | CSV (semikolon-separiert) → `BacktestResult` |
| LEAN Converter | `runner/converters/lean_converter.py` | LEAN JSON → `BacktestResult` |
| Stats Engine | `runner/stats.py` | Berechnung von Performance-Kennzahlen aus Trades + Equity Curve |
| Risk Engine | `risk/engine.py` | Position Sizing, Stop-Loss, Trailing Stop |
| Risk Models | `risk/models.py` | `TradeSignal`, `SizedOrder` |
| Risk Config | `risk/config.py` | `RiskConfig` (Globale Risk-Parameter) |

---

## 2. Detaillierte Modellanalyse

### 2.1 `shared/models.py` — Kanonische Modelle

#### `StrategyMeta` (Pydantic)
| Feld | Typ | Besonderheit |
|------|-----|--------------|
| `name` | `str` | Pflichtfeld |
| `version` | `str` | Default `"1.0.0"` |
| `parameters` | `dict[str, Any]` | Strategie-Parameter |
| `tags` | `dict[str, Any]` | Kategorisierung (type, source, asset_class, complexity) |

#### `BacktestMeta` (dataclass)
| Feld | Typ | Besonderheit |
|------|-----|--------------|
| `id` | `str` | Eindeutige ID |
| `created_at` | `str` | ISO 8601 UTC |
| `engine` | `str` | `vectorbt`, `lean`, `pynecore`, `stockkit` |
| `strategies` | `list[str]` | Name der Strategie(n) |
| `backtest_start` / `backtest_end` | `str` | Datums-String `YYYY-MM-DD` |
| `initial_capital` | `float` | Default `100_000.0` |
| `data` | `dict` | timeframes, symbols, universe, source |

**Lücke:** `BacktestMeta` kennt kein Feld für die **externe Quelle** des Validierungs-Reports (z.B. MT5-XLSX-Dateipfad, HTML-URL). `ValidationReference` übernimmt diese Provenienz-Information.

#### `Trade` (dataclass)
| Feld | Typ | Besonderheit |
|------|-----|--------------|
| `id` | `int` | Laufende Nummer |
| `symbol` | `str` | Uppercase erwartet |
| `direction` | `str` | `"LONG"` oder `"SHORT"` |
| `entry_time` | `str` | ISO 8601 |
| `entry_price` | `float` | |
| `quantity` | `float` | Gesamtgröße am Entry |
| `exits` | `list[TradeExit]` | Mindestens 1 Element |
| `pnl` | `float` | Brutto-PnL |
| `position_size` | `float \| None` | Optional |
| `risk_amount` | `float \| None` | Optional |
| `risk_percent` | `float \| None` | Optional |
| `fees` | `float \| None` | Optional |
| `net_pnl` | `float \| None` | Optional |
| `r_multiple` | `float \| None` | Optional |
| `mae` / `mfe` | `float \| None` | Max Adverse/Favorable Excursion |
| `holding_bars` | `int \| None` | Dauer in Bars |
| `meta` | `dict[str, Any]` | Extensibler Freiraum |

#### `TradeExit` (dataclass)
| Feld | Typ | Besonderheit |
|------|-----|--------------|
| `time` | `str` | ISO 8601 |
| `price` | `float` | |
| `quantity` | `float` | |
| `pnl` | `float \| None` | PnL dieses Partial Exits |
| `reason` | `str \| None` | z.B. `stop_loss`, `tp1`, `trailing_stop` |
| `fees` | `float \| None` | |

#### `BacktestStats` (dataclass)
| Feld | Typ | Besonderheit |
|------|-----|--------------|
| `total_trades` | `int` | |
| `winners` / `losers` | `int` | |
| `win_rate` | `float` | |
| `profit_factor` | `float \| None` | |
| `total_pnl` | `float` | |
| `avg_winner` / `avg_loser` | `float` | |
| `max_drawdown_pct` | `float` | Positive Zahl |
| `max_drawdown_duration` | `int` | In Perioden |
| `sharpe_ratio` | `float` | |
| `sortino_ratio` | `float` | |
| `calmar_ratio` | `float` | |
| `cagr` | `float` | |
| `total_return` | `float` | |
| `psr` | `float \| None` | Probabilistic Sharpe Ratio |
| `p_value` | `float` | One-sided p-value |
| `extras` | `dict[str, Any]` | Bucket für nicht-standardisierte Kennzahlen |

**Lücke:** `expectancy`, `sqn`, `largest_winner`, `largest_loser`, `recovery_factor` fehlen; können in `extras` landen.

#### `BacktestResult` (dataclass)
| Feld | Typ | Besonderheit |
|------|-----|--------------|
| `meta` | `BacktestMeta` | |
| `trades` | `list[Trade]` | |
| `equity_curve` | `list[EquityPoint]` | Optional |
| `stats` | `dict[str, Any]` | Kann `BacktestStats.to_dict()` enthalten |

---

### 2.2 `runner/converters/stockkit_converter.py`

**Input:** Semikolon-separierte CSV mit festem Schema.

**Schlüssellogik:**
- `_read_csv(path)` → `csv.DictReader` mit `delimiter=";"`
- `_build_trades(rows)` → Parst Partial Exits (TP1 + Runner)
- `convert_stockkit_trades(...)` → Erzeugt `BacktestMeta` mit `engine="stockkit"` und `BacktestResult`
- `BacktestResult.equity_curve` bleibt **leer** (`[]`)
- Zeitstempel: `_parse_datetime` erwartet Format `%Y-%m-%d %H:%M:%S` und setzt UTC

**Mapping-Muster:** externes Format parsen → Rohdaten-Dict → Business-Logik → `Trade` + `TradeExit` → `BacktestMeta` → `BacktestResult`.

---

### 2.3 `runner/converters/lean_converter.py`

**Input:** LEAN Engine JSON (`orders`, `profitLoss`, `charts`).

**Schlüssellogik:**
- `_build_trades(data)`: Orders flatten, Ticker extrahieren, Entry/Exit paaren, PnL aus `profitLoss` zuordnen
- `_build_meta(...)`: `BacktestMeta` mit `engine="lean"`
- `_build_equity(data)`: Equity-Kurve aus LEAN-Charts

**Besonderheiten:**
- Equity-Curve wird aus LEAN-Charts gebaut, sofern vorhanden
- `backtest_start` / `backtest_end` werden nicht extrahiert
- Keine Unterstützung für Partial Exits

---

### 2.4 `runner/stats.py` — Kennzahlenberechnung

**Einstiegspunkt:** `compute_stats(trades, initial_capital, equity_curve, risk_free_rate=0.04)` → `dict[str, Any]`

| Metrik | Datenquelle | Methode |
|--------|-------------|---------|
| `total_trades`, `winners`, `losers`, `win_rate` | Trades | Direkt |
| `profit_factor` | Trades | `gross_profit / gross_loss` |
| `max_drawdown_pct` | Equity Curve | `_max_drawdown_pct_manual()` |
| `sharpe_ratio` | Equity Curve | QuantStats oder `_sharpe_manual()` |
| `sortino_ratio` | Equity Curve | QuantStats oder `_sortino_manual()` |
| `calmar_ratio` | Equity Curve | `_safe_calmar_ratio()` |
| `cagr` | Equity Curve + Trades | `_cagr_from_backtest_period()` |
| `total_return` | Equity Curve | `(1 + returns_series).prod() - 1` |

**Hinweis:** `Recovery Factor` ist nicht in `BacktestStats` enthalten; muss ergänzt werden (entweder im Stats-Dict oder als zusätzliches Feld).

---

### 2.5 `risk/engine.py` — Risk Engine

**Klasse:** `RiskEngine` mit `RiskConfig`

**Methoden:**
- `_fallback_stop_loss(signal)`: Stop-Loss aus ATR oder %-Fallback
- `_risk_reward_ratio(signal, stop_loss)`: Berechnet R:R
- `calculate_position_size(...)`: Kernelement für Position Sizing

**Position-Sizing-Logik:**
1. Prüfung `max_positions` → Reject wenn Limit erreicht
2. Berechnung `remaining_exposure`
3. `risk_percent = min(max_risk_per_trade, remaining_exposure)`
4. Stop-Loss-Auflösung (explizit oder Fallback)
5. `risk_distance = abs(entry_price - stop_loss)`
6. `risk_amount = balance * risk_percent`
7. `raw_size = risk_amount / (risk_distance * pip)`

**Relevanz:** Risk-Engine operiert auf Live-`TradeSignal`-Objekten. Für Backtest-Validation wird ein Adapter benötigt, der aus `ValidationReference.trades` ein `TradeSignal` ableitet und Sizing-Vergleiche ermöglicht.

---

## 3. Lücken und Unterschiede

| Lücke | Auswirkung | Empfehlung |
|-------|------------|------------|
| Kein `source_path` / `source_hash` in `BacktestMeta` | Herkunft externer Reports nicht nachvollziehbar | `ValidationReference` als Wrapper um `BacktestResult` mit Provenienz-Feldern |
| `BacktestStats` fehlt `recovery_factor` | Externe Reports enthalten oft dieses Feld | In `extras` ablegen oder `compute_stats()` ergänzen |
| `Trade` hat kein Feld für `magic_number`, `order_comment` | MT5-spezifische IDs gehen verloren | `Trade.meta` nutzen |
| `BacktestResult` hat kein Feld für `balance_curve` | MT5 unterscheidet Balance vs. Equity | `equity_curve` nutzen; Balance ggf. in `raw_metadata` |
| Kein `ValidationReference`-Modell | Zentrales Konzept fehlt | Neu erstellen |
| Kein MT5-XLSX-Converter | Hauptdatenquelle nicht importierbar | Neu erstellen |
| Kein gemeinsames Converter-Interface | Converter inkonsistent | `BaseConverter`-Protokoll oder `convert(source) -> ValidationReference` |

---

## 4. Empfehlung: Reuse vs. Neuer Layer

### 4.1 Wiederverwenden (Reuse)

| Komponente | Begründung |
|------------|------------|
| `BacktestResult` | Bereits kanonisches Zielformat; `ValidationReference` sollte intern konvertierbar sein |
| `BacktestMeta` | Zeitliche und kapitalbezogene Metadaten; nur minimale Erweiterungen nötig |
| `Trade` + `TradeExit` | Trade-Level-Vergleich erfordert identische Struktur |
| `BacktestStats` | Deckt 90% der externen Reports ab; zusätzliche Felder in `extras` |
| `compute_stats()` | Interne Kennzahlen berechnen und mit externen Stats vergleichen |
| `EquityPoint` | Einheitliches Equity-Kurven-Format |

### 4.2 Neuer Layer (Neu schreiben/erstellen)

| Komponente | Begründung |
|------------|------------|
| `ValidationReference` (Pydantic-Model) | Zentrales Aggregat mit Pflichtfeldern und Provenienz-Informationen |
| `ValidationResult` / `Deviation` | Ergebnis des Vergleichs mit einzelnen Abweichungen |
| `MT5XLSXConverter` / `MT5HTMLConverter` | Neue Converter für primäre Datenquelle |
| `BaseConverter` / `ConverterRegistry` | Gemeinsames Interface für alle Importer |
| `ValidationEngine` | Orchestriert Laden, Paaren, Toleranzprüfung, Report |
| `ToleranceConfig` | Toleranzregeln für Feldabweichungen |

### 4.3 Architektur-Skizze

```
Externer Report (MT5-XLSX/HTML)
        │
        ▼
   [Parser/Converter]  ← MT5XLSXConverter, MT5HTMLConverter
        │
        ▼
ValidationReference (Pydantic)
        │
        ├──→ BacktestResult (internes Format)
        │
        └──→ Stats (intern berechnet)
                │
                ▼
        [ValidationEngine]
                │
        ┌───────┴───────┐
        ▼               ▼
  Trade-Vergleich   Stat-Vergleich
        │               │
        └───────┬───────┘
                ▼
        ValidationResult
        (status, deviations, report)
```

---

## 5. Verify-Checkliste

- [x] `class BacktestResult` in `shared/models.py` vorhanden
- [x] `class RiskEngine` in `risk/engine.py` vorhanden
- [x] `def compute_stats` in `runner/stats.py` vorhanden
