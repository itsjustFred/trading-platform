# Phase 1 — Universelles Backtest-Framework

> Ziel: In 1–2 Wochen kann man eine Strategie backtesten (egal in welchem Framework) und die Trades einheitlich im Viewer anschauen.

---

## 1. Trade-Output-Format Spezifikation

Alle Backtester produzieren am Ende **eine JSON-Datei** pro Backtest-Run. Das Format hat zwei Top-Level-Blöcke: `meta` (Backtest-Kontext) und `trades` (Liste).

### 1.1 Backtest-Metadata (`meta`)

```json
{
  "meta": {
    "id": "mr-rsi2-sp500-20260224-143022",
    "strategy": "mean_reversion_rsi2",
    "engine": "lean",
    "version": "1.0.0",
    "created_at": "2026-02-24T14:30:22Z",
    "timeframe": {
      "start": "2021-01-01",
      "end": "2025-06-30"
    },
    "initial_capital": 100000.00,
    "currency": "USD",
    "symbols": ["AAPL", "MSFT", "NVDA"],
    "data_timeframe": "1h",
    "parameters": {
      "rsi_period": 2,
      "rsi_entry_threshold": 10,
      "rsi_exit_threshold": 70,
      "risk_percent": 0.01
    },
    "tags": ["mean-reversion", "equity", "us-stocks"],
    "notes": "SP500 Universe, RSI(2) Pullback"
  }
}
```

Pflichtfelder: `id`, `strategy`, `engine`, `created_at`, `timeframe`, `initial_capital`.
Alles andere optional.

### 1.2 Trade-Liste (`trades`)

Jeder Trade repräsentiert **eine Position** — vom Entry bis zur vollständigen Schließung. Partial Exits (z.B. TP1 50%, Rest Trailing Stop) werden über ein `exits`-Array abgebildet.

> **Design-Entscheidung:** `exits`-Array statt separate Trades mit `parent_id`.
> Begründung: Ein Trade IST eine Position. Das exits-Array bildet die Realität ab (ein Entry, mehrere Exits), vermeidet duplizierte Entry-Daten, und "Anzahl Trades" ist eindeutig. Der einfache Fall (ein Exit) ist ein Array mit einem Element — kein Overhead.

**Beispiel: Einfacher Trade (ein Exit)**

```json
{
  "trades": [
    {
      "id": 1,
      "symbol": "AAPL",
      "direction": "LONG",
      "entry_time": "2021-03-15T10:30:00Z",
      "entry_price": 121.50,
      "quantity": 82,
      "exits": [
        {
          "time": "2021-03-18T15:45:00Z",
          "price": 124.80,
          "quantity": 82,
          "reason": "rsi_target",
          "fees": 1.64
        }
      ],
      "pnl": 270.60,
      "pnl_pct": 2.72,
      "fees": 1.64,
      "net_pnl": 268.96,
      "r_multiple": 1.8,
      "mae": -0.85,
      "mfe": 3.50,
      "holding_bars": 22,
      "tags": [],
      "meta": {}
    }
  ]
}
```

**Beispiel: Partial Exit (TP1 + Trailing Stop)**

```json
{
  "id": 7,
  "symbol": "CRWD",
  "direction": "LONG",
  "entry_time": "2026-02-23T14:35:02Z",
  "entry_price": 376.37,
  "quantity": 138,
  "exits": [
    {
      "time": "2026-02-23T15:20:00Z",
      "price": 387.22,
      "quantity": 69,
      "reason": "tp1",
      "fees": 0.82,
      "tags": ["tp1"]
    },
    {
      "time": "2026-02-23T16:45:00Z",
      "price": 382.10,
      "quantity": 69,
      "reason": "supertrend_flip",
      "fees": 0.82,
      "tags": ["runner"]
    }
  ],
  "pnl": 1143.67,
  "pnl_pct": 2.20,
  "fees": 1.64,
  "net_pnl": 1142.03,
  "r_multiple": 1.57,
  "mae": -2.10,
  "mfe": 12.50,
  "holding_bars": 26,
  "tags": ["tp1_reached"],
  "meta": {
    "sl_price": 369.14,
    "tp1_price": 387.22
  }
}
```

**Pflichtfelder pro Trade:**
- `id`, `symbol`, `direction` (LONG/SHORT)
- `entry_time` (ISO 8601 UTC), `entry_price`
- `quantity` (Gesamtmenge beim Entry)
- `exits` (Array, mindestens 1 Element)
- `pnl` (brutto, Summe über alle Exits)

**Pflichtfelder pro Exit:**
- `time` (ISO 8601 UTC), `price`, `quantity`

**Optionale Felder pro Exit:**
- `reason` (Freitext: "stop_loss", "tp1", "tp2", "trailing_stop", "supertrend_flip", "time_stop", etc.)
- `fees`
- `tags` (Array of Strings)

**Optionale Felder pro Trade:**
- `pnl_pct`, `fees` (Summe), `net_pnl`
- `r_multiple`, `mae` (Max Adverse Excursion), `mfe` (Max Favorable Excursion)
- `holding_bars` (Entry bis letzter Exit)
- `tags` (Array of Strings, frei belegbar — z.B. `["tp1_reached"]`)
- `meta` (freies Dict für engine-spezifische Daten, z.B. SL/TP-Levels)

**Berechnungsregeln:**
- `pnl` = Summe der PnL aller Exits: `Σ (exit.quantity × (exit.price - entry_price) × direction_sign)`
- `fees` = Summe aller Exit-Fees + evtl. Entry-Fee
- `holding_bars` = Entry bis zum **letzten** Exit
- Für Stats (Win-Rate, etc.) zählt der **Gesamt-Trade**, nicht einzelne Exits

### 1.3 Optionaler Block: `equity_curve`

```json
{
  "equity_curve": [
    {"time": "2021-01-01", "equity": 100000.00},
    {"time": "2021-01-02", "equity": 100150.30}
  ]
}
```

Daily granularity reicht. Wird vom Viewer für die Equity-Kurve genutzt.

### 1.4 Optionaler Block: `stats`

Vorberechnete Statistiken. Der Viewer kann sie auch selbst berechnen, aber wenn die Engine sie liefert, werden sie direkt angezeigt.

```json
{
  "stats": {
    "total_trades": 142,
    "winners": 89,
    "losers": 53,
    "win_rate": 62.68,
    "profit_factor": 2.14,
    "total_pnl": 28450.00,
    "max_drawdown_pct": 8.3,
    "sharpe_ratio": 1.45,
    "sortino_ratio": 2.10,
    "avg_winner": 485.30,
    "avg_loser": -295.10,
    "avg_holding_bars": 4.2,
    "max_consecutive_losses": 5,
    "calmar_ratio": 1.72
  }
}
```

### 1.5 Dateiname-Konvention

```
{strategy}_{engine}_{YYYYMMDD-HHmmss}.json
```
Beispiel: `mean_reversion_rsi2_lean_20260224-143022.json`

### 1.6 Kompatibilität mit existierenden Formaten

Es gibt **Converter** für:
- LEAN `results.json` → Universalformat
- stockkit `trades_history_*.csv` → Universalformat
- vectorbt Output → Universalformat

Die Converter sind einfache Python-Funktionen (kein eigener Service).

---

## 2. Data-Verzeichnis Struktur

### 2.1 Pfad

`~/data/` auf der Boot-Disk (72 GB frei). Wenn die zweite Disk kommt (Phase 2), wird `~/data/` zum Symlink auf die neue Disk — **kein Code muss sich ändern**.

### 2.2 Ordnerstruktur

```
~/data/
├── ohlcv/
│   ├── dukascopy/
│   │   └── forex/
│   │       └── XAUUSD/
│   │           ├── 1m.parquet
│   │           ├── 15m.parquet
│   │           └── 1h.parquet
│   ├── yfinance/
│   │   └── equity/
│   │       └── US/
│   │           ├── AAPL/
│   │           │   ├── 5m.parquet
│   │           │   ├── 1h.parquet
│   │           │   └── 1d.parquet
│   │           ├── NVDA/
│   │           └── ...
│   └── alpaca/
│       └── equity/
│           └── US/
│               └── ...
├── tick/
│   └── ctrader/
│       └── forex/
│           └── XAUUSD/
│               └── raw.parquet
├── fundamentals/
│   └── (leer — kommt später)
├── universes/
│   ├── sp500_current.txt
│   └── sp500_changes.csv
├── backtests/
│   ├── mean_reversion_rsi2_lean_20260224-143022.json
│   ├── supertrend_vectorbt_20260225-091500.json
│   └── ...
└── catalog.json
```

### 2.3 Naming Convention

```
{quelle}/{asset_klasse}/{region_oder_markt}/{SYMBOL}/{timeframe}.parquet
```

Timeframes: `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `1d`, `1w` (lowercase, keine Leerzeichen).

### 2.4 Parquet statt CSV

Alle neuen Daten werden als **Parquet** gespeichert. Vorteile:
- 3–5× kleiner als CSV
- Spaltentypen sind fix (kein Parsing)
- Schnelles Lesen mit pandas/polars
- Zeitbereiche können per Row-Group übersprungen werden

Existierende CSVs werden per Migration-Script einmalig konvertiert.

### 2.5 `catalog.json`

Einfaches Manifest das dokumentiert was vorhanden ist:

```json
{
  "updated_at": "2026-02-24T14:00:00Z",
  "datasets": [
    {
      "symbol": "XAUUSD",
      "source": "dukascopy",
      "asset_class": "forex",
      "timeframes": ["15m", "1h"],
      "start": "2020-11-08",
      "end": "2026-02-07",
      "rows": {"15m": 123794, "1h": 31018},
      "path": "ohlcv/dukascopy/forex/XAUUSD/"
    }
  ]
}
```

Wird **automatisch generiert** durch ein Script das `~/data/ohlcv/` scannt. Kein manuelles Pflegen.

---

## 3. Backtest-Runner Architektur

### 3.1 Grundidee

Kein mega-abstraktes Framework. Stattdessen: ein **dünner CLI-Orchestrator** der verschiedene Engines aufruft und das Ergebnis ins Universalformat konvertiert.

```
┌─────────────────────────────────────────────────┐
│              backtest-runner CLI                 │
│  python -m runner run --engine lean             │
│                       --strategy mean_rev       │
│                       --config params.json      │
│                       --period 2021-01:2025-06  │
├─────────────────────────────────────────────────┤
│             Engine Adapter Layer                 │
│  ┌──────────┐  ┌───────────┐  ┌──────────────┐ │
│  │   LEAN   │  │  vectorbt │  │  (PyneCore)  │ │
│  │ Adapter  │  │  Adapter  │  │  Adapter     │ │
│  └─────┬────┘  └─────┬─────┘  └──────┬───────┘ │
│        │             │               │          │
│        ▼             ▼               ▼          │
│   Engine-spezifischer Backtest-Lauf             │
├─────────────────────────────────────────────────┤
│             Output Converter                    │
│   Engine-Output → Universal Trade Format JSON   │
├─────────────────────────────────────────────────┤
│      Speichern in ~/data/backtests/             │
└─────────────────────────────────────────────────┘
```

### 3.2 Engine Adapter Interface

Jeder Adapter implementiert:

```python
class EngineAdapter(ABC):
    """Einheitliches Interface für Backtest-Engines."""

    @abstractmethod
    def run(self, config: BacktestConfig) -> RawResult:
        """Führt den Backtest aus, gibt engine-spezifisches Ergebnis zurück."""

    @abstractmethod
    def to_universal(self, raw: RawResult, config: BacktestConfig) -> UniversalResult:
        """Konvertiert engine-spezifisches Ergebnis ins Universalformat."""
```

`BacktestConfig` ist ein einfaches Dataclass:

```python
@dataclass
class BacktestConfig:
    strategy: str           # Strategie-Name oder Pfad
    engine: str             # "lean", "vectorbt", "pynecore"
    start: date
    end: date
    initial_capital: float = 100_000.0
    symbols: list[str] = field(default_factory=list)
    data_timeframe: str = "1d"
    parameters: dict = field(default_factory=dict)
    data_dir: str = "~/data"
```

### 3.3 LEAN Adapter

Wrapped die existierende `custom_lean_integration`-Logik:
- Nutzt den vorhandenen `LeanBridgeAlgorithm` und `IStrategy`
- Liest Daten aus `~/data/ohlcv/` (oder aus dem bestehenden `data/`-Verzeichnis für Rückwärtskompatibilität)
- Konvertiert LEAN `results.json` → Universalformat

**Kein Neuschreiben der LEAN-Integration** — der vorhandene Code wird nur mit einem Adapter umwickelt.

### 3.4 vectorbt Adapter

Für einfache Strategien die kein volles LEAN brauchen:
- Lädt Daten aus `~/data/ohlcv/` als pandas DataFrame
- Strategie wird als Python-Funktion geschrieben die Signale (entries/exits) zurückgibt
- vectorbt macht die Simulation (Fees, Slippage, Position Sizing)
- Ergebnis → Universalformat

**vectorbt passt gut** weil es:
- Rein Python ist (keine Abhängigkeit zu LEAN/QuantConnect)
- Vektorisiert → schnell
- Für einfache Signal-basierte Strategien ideal (Indikator → Entry/Exit)

### 3.5 PyneCore Adapter (später — Phase 1.5)

Optional, nicht zwingend für Phase 1. Ermöglicht PineScript direkt in Python auszuführen.
Die Architektur blockiert das nicht: einfach ein weiterer Adapter.

### 3.6 Wo liegt der Code

Im neuen Monorepo unter `runner/`:
```
runner/
├── __main__.py          # CLI entry (python -m runner run ...)
├── config.py            # BacktestConfig dataclass
├── result.py            # UniversalResult dataclass + JSON serialization
├── engines/
│   ├── base.py          # EngineAdapter ABC
│   ├── lean_adapter.py  # LEAN wrapper
│   └── vectorbt_adapter.py
├── converters/
│   ├── lean_converter.py      # LEAN results.json → Universal
│   ├── stockkit_converter.py  # stockkit CSV → Universal
│   └── csv_converter.py       # Generic CSV → Universal
└── stats.py             # Statistik-Berechnung (Sharpe, Sortino, etc.)
```

---

## 4. Trade-Viewer Erweiterung

### 4.1 Ausgangspunkt

Der existierende `trade_viewer.html` (1491 Zeilen) + `view_trades_web.py` (Flask-Backend) in `custom_lean_integration` ist **sehr solide**:
- TradingView-artiges Chart (lightweight-charts)
- Trade-Navigation (Prev/Next)
- Indikator-Overlays
- Trade-Markers (Entry/Exit-Pfeile)
- Statistik-Panel

### 4.2 Nötige Änderungen

**Backend (`viewer/server.py` — neuer Standort):**
1. **Backtest-Auswahl**: API-Endpoint der alle JSON-Dateien in `~/data/backtests/` auflistet
2. **File-basiertes Loading**: Statt hartcodiertem Backtest-Pfad → lädt beliebige Universalformat-JSON
3. **OHLCV-Daten aus ~/data/**: Für die Chart-Anzeige werden die Kerzen-Daten aus dem zentralen Data-Verzeichnis geladen (Symbol + Timeframe aus der Meta)
4. **Stats-Berechnung**: Wenn `stats`-Block in der JSON fehlt → aus Trades berechnen

**Frontend (trade_viewer.html):**
1. **Backtest-Picker**: Dropdown/Sidebar oben links → zeigt alle verfügbaren Backtests
2. **Equity-Curve-Tab**: Neuer Tab der die Equity-Curve als Linienchart anzeigt
3. **Stats-Dashboard**: Win-Rate, PnL, Sharpe, Drawdown, Profit Factor etc. (das Stats-Panel existiert bereits teilweise)
4. **Trade-Tabelle**: Sortierbare Liste aller Trades (Symbol, Direction, PnL, Holding-Bars, R-Multiple)

**Was NICHT geändert wird:**
- Chart-Engine bleibt lightweight-charts
- Kein Framework-Wechsel (bleibt Vanilla HTML/JS)
- Kein Build-Step nötig
- Backend bleibt Flask (läuft, kennen wir)

### 4.3 Strategie-Vergleich (Ende Phase 1)

Einfache Version: Zwei Backtests auswählen → Equity-Curves übereinander + Stats-Tabelle nebeneinander.
Kein komplexes Multi-Select nötig für Phase 1.

---

## 5. Ordnerstruktur / Repo-Struktur

### 5.1 Neues Monorepo: `trading-platform`

```
trading-platform/
├── README.md
├── pyproject.toml              # Workspace-Root (uv/pip)
├── .env.example
├── .gitignore
│
├── runner/                     # Backtest-Runner (CLI)
│   ├── __main__.py
│   ├── config.py
│   ├── result.py
│   ├── engines/
│   │   ├── base.py
│   │   ├── lean_adapter.py
│   │   └── vectorbt_adapter.py
│   ├── converters/
│   │   ├── lean_converter.py
│   │   ├── stockkit_converter.py
│   │   └── csv_converter.py
│   └── stats.py
│
├── viewer/                     # Trade-Viewer (Web)
│   ├── server.py               # Flask Backend
│   ├── static/
│   │   └── trade_viewer.html   # Frontend (migriert von custom_lean_integration)
│   └── requirements.txt
│
├── data_tools/                 # Data-Management
│   ├── migrate_csv.py          # CSV → Parquet Migration
│   ├── catalog_builder.py      # Scannt ~/data/ und erzeugt catalog.json
│   ├── download_yfinance.py    # yfinance Downloader
│   └── download_dukascopy.py   # Dukascopy Downloader (von ML_tick_data_prepare)
│
├── shared/                     # Gemeinsame Utilities
│   ├── models.py               # Dataclasses (Trade, BacktestResult, etc.)
│   └── constants.py            # Timeframe-Mappings, etc.
│
├── strategies/                 # Strategie-Sammlung (für vectorbt/standalone)
│   ├── rsi_mean_reversion.py
│   ├── supertrend_basic.py
│   └── ...
│
└── tests/
    ├── test_runner.py
    ├── test_converters.py
    └── test_viewer.py
```

### 5.2 Was passiert mit den existierenden Repos

| Repo | Aktion |
|------|--------|
| **custom_lean_integration** | LEAN-spezifischer Code bleibt dort. `lean_adapter.py` im Monorepo ruft es auf. trade_viewer.html wird ins Monorepo **kopiert und erweitert**. |
| **stockkit_ai_bot** | **Nicht anfassen.** Läuft live. Converter im Monorepo kann dessen Trade-History lesen. |
| **custom_TradingAgents** | Bleibt separat. Nicht Teil von Phase 1. |
| **Indikatoren** | Bleibt separat. Wird bei Bedarf zum Konvertieren referenziert. |
| **Andere GitHub-Repos** | Bleiben wo sie sind. |

### 5.3 Python-Setup

- **uv** als Package-Manager (bereits installiert)
- Ein `pyproject.toml` im Root
- Extras für optionale Engines: `pip install -e ".[lean]"` oder `pip install -e ".[vectorbt]"`
- Python 3.12 (bereits vorhanden)

---

## 6. Aufgabenliste mit Reihenfolge

### Phase 1a — Fundament (zuerst, sequenziell)

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 1 | **Monorepo `trading-platform` initialisieren** — Git-Repo, pyproject.toml, Grundstruktur, .gitignore | S | — |
| 2 | **Trade-Format Dataclasses definieren** — `shared/models.py` mit Trade, BacktestMeta, BacktestResult. JSON-Serialisierung. | S | 1 |
| 3 | **Stats-Modul** — `runner/stats.py`: Berechnet Win-Rate, Sharpe, Sortino, MaxDD, Profit Factor, Calmar aus Trade-Liste | M | 2 |
| 4 | **Data-Verzeichnis aufsetzen** — `~/data/` anlegen, Struktur erstellen, existierende Daten migrieren (CSV → Parquet) | M | 1 |
| 5 | **Catalog-Builder** — `data_tools/catalog_builder.py`: Scannt ~/data/ohlcv/, erzeugt catalog.json | S | 4 |

### Phase 1b — Converter (parallel möglich)

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 6 | **LEAN Converter** — `converters/lean_converter.py`: LEAN results.json → Universalformat | S | 2 |
| 7 | **Stockkit Converter** — `converters/stockkit_converter.py`: stockkit CSV (`;`-separated) → Universalformat | S | 2 |
| 8 | **Converter CLI** — `python -m runner convert --input X --format lean` | S | 6, 7 |

### Phase 1c — Viewer (parallel zu 1b)

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 9 | **Viewer-Backend migrieren** — `viewer/server.py`: Flask-Server der Universalformat-JSONs aus `~/data/backtests/` lädt, OHLCV aus `~/data/ohlcv/` | M | 2, 4 |
| 10 | **Viewer-Frontend migrieren** — trade_viewer.html ins Monorepo kopieren, Backtest-Picker hinzufügen | M | 9 |
| 11 | **Equity-Curve-Ansicht** — Neuer Tab im Viewer: Equity-Curve als Linienchart (lightweight-charts) | S | 10 |
| 12 | **Trade-Tabelle** — Sortierbare Liste aller Trades im Viewer (HTML-Tabelle, kein Framework nötig) | S | 10 |

### Phase 1d — Runner (nach 1a)

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 13 | **EngineAdapter-Interface** — `runner/engines/base.py`: ABC mit run() und to_universal() | S | 2 |
| 14 | **vectorbt Adapter** — `runner/engines/vectorbt_adapter.py`: Nimmt Signale, führt Simulation aus, konvertiert Output | M | 4, 13 |
| 15 | **LEAN Adapter** — `runner/engines/lean_adapter.py`: Wraps custom_lean_integration, konvertiert Output | M | 6, 13 |
| 16 | **Runner CLI** — `python -m runner run --engine vectorbt --strategy rsi_mean_rev --period 2021-01:2025-06` | M | 14 oder 15 |
| 17 | **Beispiel-Strategie (vectorbt)** — RSI Mean Reversion als Proof-of-Concept, rein Python + vectorbt | S | 14 |

### Phase 1e — Polish

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 18 | **Strategie-Vergleich im Viewer** — Zwei Backtests auswählen, Equity-Curves übereinander, Stats nebeneinander | M | 11, 12 |
| 19 | **README + Doku** — Wie man einen Backtest ausführt, Format-Spezifikation, Data-Struktur | S | alles |
| 20 | **Git-Aufräumen** — PAT aus custom_lean_integration Remote entfernen (erledigt laut 01_current_state), eval_results löschen | S | — |

### Reihenfolge-Diagramm

```
1 (Repo-Init)
├──→ 2 (Dataclasses)
│    ├──→ 3 (Stats)
│    ├──→ 6 (LEAN Conv.)  ──→ 8 (Conv. CLI)
│    ├──→ 7 (Stockkit Conv.) ─┘
│    ├──→ 9 (Viewer BE)  ──→ 10 (Viewer FE) ──→ 11 (Equity) ──→ 18 (Vergleich)
│    │                                        └──→ 12 (Tabelle) ─┘
│    └──→ 13 (Adapter IF) ──→ 14 (vectorbt) ──→ 16 (Runner CLI) ──→ 17 (Beispiel)
│                          └──→ 15 (LEAN Adpt.)─┘
├──→ 4 (Data-Dir)
│    └──→ 5 (Catalog)
└──→ 20 (Git Cleanup)

19 (Doku) kommt ganz am Ende.
```

---

## 7. Offene Entscheidungen

### 7.1 vectorbt vs. Alternativen

**vectorbt** ist der Favorit für den "einfachen" Backtest-Adapter. Alternativen:

| Option | Pro | Contra |
|--------|-----|--------|
| **vectorbt** | Schnell (vektorisiert), flexibel, aktiv maintained | Etwas steile Lernkurve, Portfolio-Simulation einfacher als bei LEAN |
| **backtrader** | Bereits in TradingAgents requirements, event-basiert wie LEAN | Langsam, nicht mehr aktiv maintained |
| **eigener Mini-Backtester** | Volle Kontrolle, minimal Dependencies | Muss man selbst bauen und debuggen |

**Empfehlung:** vectorbt. Falls es Probleme macht, ist ein Wechsel einfach weil der Adapter das abstrahiert.

### 7.2 Viewer: Eigenständiger Prozess vs. im Runner?

**Empfehlung:** Eigenständig. `python -m viewer` startet den Flask-Server. Der Runner schreibt JSON-Dateien, der Viewer liest sie. Keine Kopplung.

### 7.3 Daten-Migration: Alles sofort oder on-demand?

**Empfehlung:** Hybrid.
- Schritt 1: Script das die vorhandenen CSVs einmalig migriert (~56 MB, dauert Sekunden)
- Schritt 2: Neue Downloads gehen direkt als Parquet rein
- Bestehende Projekte (stockkit, lean) nutzen weiter ihre eigenen data/-Verzeichnisse. Kein Breaking Change.

### 7.4 Repo: GitHub sofort oder erst lokal?

**Empfehlung:** Sofort ein Private GitHub Repo (`itsjustFred/trading-platform`). Kostet nichts, Backup von Tag 1.

---

## 8. Ausblick Phase 2+

Die Phase-1-Architektur ist **bewusst so gebaut** dass diese Features später draufgesetzt werden können ohne Umbau:

| Feature | Wie Phase 1 das vorbereitet |
|---------|---------------------------|
| **Docker** | Monorepo-Struktur erlaubt einfach ein Dockerfile pro Service (runner, viewer). ~/data/ wird als Volume gemounted. |
| **Auth fürs Frontend** | Flask-Server kann mit Flask-Login/JWT erweitert werden. Aktuell kein Auth nötig (lokaler Server). |
| **Alerts (Telegram/Discord)** | Runner kann nach Backtest-Run ein Event feuern. Alert-Service subscribt darauf. |
| **Risk-Management-Layer** | Neues Modul `risk/` im Monorepo. Liest Trade-Format, prüft Limits. |
| **ML-Integration** | ML-Modelle werden als Strategie registriert, nutzen den gleichen Runner + Output-Format. trading_ml-Repo kann als Engine-Adapter angebunden werden. |
| **Live-Performance-Dashboard** | Viewer erweitern: Neben Backtest-Ergebnissen auch Live-Trades (stockkit-Format wird konvertiert und reingeladen). |
| **Walk-Forward / Parameter-Sensitivity** | Runner CLI erweitern: `--sweep param=1:10:1` → mehrere Backtests, Ergebnisse in einer Multi-Result-JSON. |
| **Zweite Disk** | `~/data/` wird Symlink auf neue Disk. Kein Code-Change. |
| **PyneCore** | Neuer Adapter in `runner/engines/pynecore_adapter.py`. Interface steht. |
| **LRU-Cache für große Datasets** | `data_tools/` erweitern um Cache-Management. catalog.json wird um "cached" vs "remote" Einträge ergänzt. |

---

## Anhang: Bestehende Code-Assets die wiederverwendet werden

| Was | Woher | Wohin |
|-----|-------|-------|
| `trade_viewer.html` (1491 Zeilen) | custom_lean_integration/static/ | viewer/static/ (kopiert + erweitert) |
| `view_trades_web.py` (Flask API) | custom_lean_integration/ | viewer/server.py (refactored) |
| `IStrategy` Interface | custom_lean_integration/src/framework/ | runner/engines/lean_adapter.py (referenziert) |
| `LeanBridgeAlgorithm` | custom_lean_integration/src/framework/core/ | runner/engines/lean_adapter.py (aufgerufen) |
| Stats-Berechnung (teilweise) | stockkit_ai_bot/backtest/metrics.py | runner/stats.py (logik übernommen) |
| CSV-Datendownloader | ML_tick_data_prepare/ | data_tools/ (falls nötig) |
