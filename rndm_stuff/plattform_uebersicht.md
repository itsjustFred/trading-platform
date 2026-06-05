# Trading Platform Uebersicht

## Kurzfassung

Diese Plattform ist eine integrierte Research-, Backtesting-, Daten- und Live-Ops-Umgebung fuer systematisches Trading. Der aktuelle Code-Stand zeigt bereits eine relativ breite Abdeckung:

- mehrere Backtest-Engines
- ein gemeinsames Trade-/Backtest-Datenmodell
- eine FastAPI-API als Backend
- ein React-Frontend fuer Analyse, Betrieb und Monitoring
- Datenmanagement fuer OHLCV-Parquets aus mehreren Quellen
- Portfolio-Zusammenstellung ueber mehrere Strategien
- erste Live-Komponenten fuer Accounts, Deployments, Signale und Ausfuehrungen
- AI-gestuetzte Strategy-Conversion im "Strategy Lab"

Die Plattform ist damit nicht nur ein Backtester, sondern entwickelt sich in Richtung eines kompletten Betriebssystems fuer den Lebenszyklus quantitativer Strategien.

## Nordstern und langfristiges Ziel

Das langfristige Ziel der Plattform ist klar:

> Viele profitable Strategien aufbauen und betreiben, deren Drawdowns moeglichst unkorreliert oder negativ korreliert sind, um hohe Returns bei moeglichst geringem Gesamtdrawdown zu erreichen. Der eigentliche Nordstern ist also ein robustes, diversifiziertes Multi-Strategy-Portfolio mit hohem Sharpe.

Daraus ergeben sich vier strategische Leitlinien fuer die Plattform:

1. Strategien muessen schnell erzeugt, getestet und verglichen werden koennen.
2. Ergebnisse muessen in einem einheitlichen Format gespeichert werden, damit Engine-, Strategie- und Portfolio-Vergleiche sauber moeglich sind.
3. Korrelation, Drawdown-Verhalten und Portfoliokombinationen sind mindestens so wichtig wie Einzelstrategie-Return.
4. Gute Strategien sollen spaeter nicht im Research stecken bleiben, sondern kontrolliert in Live-Deployments ueberfuehrt werden koennen.

## Was die Plattform heute schon ist

Im aktuellen Repo ist die Plattform im Kern in sechs Faehigkeiten aufgeteilt:

### 1. Strategy Research und Backtesting

- `runner/` ist die zentrale Execution-Schicht.
- Strategien koennen ueber mehrere Engines ausgefuehrt werden:
  - `native`
  - `vectorbt`
  - `lean`
  - `pynecore`
- Die CLI in `runner/cli.py` baut die Backtest-Konfiguration, startet die passende Engine und persistiert Ergebnisse.
- `runner/stats.py` berechnet die wichtigsten Kennzahlen wie Sharpe, Sortino, Calmar, CAGR, Max Drawdown, PSR und p-Value.

### 2. Einheitliche Ergebnis- und Analyseebene

- `shared/models.py` definiert das kanonische Format fuer:
  - Trades
  - Partial Exits
  - Equity Curves
  - Backtest-Metadaten
  - Backtest-Statistiken
  - Portfolio-Statistiken
- Dadurch koennen unterschiedliche Engines trotzdem in derselben UI und in denselben Portfolio-Workflows landen.

### 3. Datenplattform fuer Markt- und Research-Daten

- `data_tools/` standardisiert historische Daten in ein einheitliches Parquet-Layout.
- Unterstuetzte bzw. angebundene Quellen sind aktuell unter anderem:
  - Dukascopy
  - DoltHub
  - HuggingFace Datasets
  - HistData
  - cTrader CSV-Import
  - Legacy-yfinance/CSV-Migration
- Das kanonische Ablageformat ist:

```text
$DATA_DIR/ohlcv/{source}/{asset_class}/[{region}/]{symbol}/{timeframe}.parquet
```

- Ein `catalog.json` dient als Uebersicht ueber verfuegbare Daten.

### 4. Analyse- und Operations-UI

- `viewer/main.py` stellt eine grosse FastAPI-Oberflaeche bereit.
- `frontend/` liefert eine React/Vite-SPA fuer:
  - Strategien
  - Backtests
  - Trade-Details
  - Vergleiche
  - Optimierungen
  - Portfolios
  - Data Management
  - Strategy Lab
  - Live Hub
  - Services
  - Task Hub
  - Logging

### 5. Portfolio-Konstruktion

- Portfolios sind in diesem Repo kein Nebengedanke, sondern ein eigener Produktbereich.
- Die API und das Frontend koennen mehrere Backtests bzw. Strategie-Komponenten kombinieren.
- Persistiert werden:
  - Komponenten und Gewichte
  - kombinierte Equity
  - kombinierte Stats
  - Strategie- und Komponenten-Contributions
  - Korrelationen im Portfolio-Stats-Modell

Das ist direkt anschlussfaehig an das langfristige Ziel eines Multi-Strategy-Stacks mit verbessertem Sharpe und kontrolliertem Drawdown.

### 6. Live-Betrieb und Signalverarbeitung

- `live_runner/` enthaelt Broker- und Signalinfrastruktur.
- Bereits im Repo sichtbar:
  - Alpaca Live Adapter
  - cTrader Live Adapter
  - Account Discovery
  - Live Market Data Stream
  - Discord-Signalquellen
- `risk/` enthaelt Risk- und Sizing-Logik fuer Signale und Orders.
- `scheduler/` bringt zeitgesteuerte Services mit APScheduler ins System.

## Technischer Stack

### Backend

- Python 3.12
- FastAPI
- Uvicorn
- SQLModel
- APScheduler
- WebSockets

### Quant / Research

- pandas
- numpy
- pyarrow
- quantstats
- optuna
- plotly

### Backtest- und Strategie-Layer

- native event-based engine
- vectorbt
- QuantConnect LEAN Adapter
- PyneCore Adapter

### Frontend

- React 19
- TypeScript
- Vite
- React Router
- TanStack React Query
- Tailwind CSS v4
- Recharts
- lightweight-charts
- AG Grid

### Daten- und Broker-Anbindungen

- aiohttp
- yfinance
- alpaca-py
- discord.py
- optionale Integrationen fuer JForex/JPype, PyneCore und cTrader

### AI / Strategie-Generierung

- `runner/auto_agent.py`
- OpenAI-kompatible Schnittstellen
- NVIDIA NIM Provider
- GitHub Copilot SDK / CLI Provider

## Repo-Aufbau

```text
shared/        Gemeinsame Modelle, Konstanten, Logging, DB-Layer
runner/        CLI, Engines, Stats, Optimizer, AI-Agent, Converter
viewer/        FastAPI-App und API-Endpunkte
frontend/      React-SPA
data_tools/    Downloader, Kataloge, Validierung, Resampling, Migration
strategies/    Einzelne Strategien mit Code und Doku
risk/          Risk Engine, Signal-Sizing, Risk-Modelle
live_runner/   Live Adapter, Signalquellen, Discovery, Streaming
scheduler/     Zeitgesteuerte Jobs/Services
tests/         Unit-, Integrations- und Frontend-nahe Tests
docs/          Architektur- und Daten-Dokumentation
```

## End-to-end Flows

### 1. Klassischer Backtest-Flow

```text
Strategiecode
-> runner/cli.py
-> passende Engine
-> gemeinsames BacktestResult
-> Speicherung in JSON/SQLite
-> viewer/main.py API
-> frontend Analyse-Seiten
```

### 2. Daten-Flow

```text
externe Datenquelle
-> data_tools downloader/importer
-> kanonische Parquet-Ablage
-> catalog.json / Data API
-> Backtest- und Viewer-Nutzung
```

### 3. Strategy-Lab-Flow

```text
Rohcode oder Ideeninput
-> AI Conversion Agent
-> Strategieordner unter strategies/<slug>/
-> Validierungs-Backtest
-> Sichtbarkeit in Strategie-Katalog und UI
```

### 4. Portfolio-Flow

```text
mehrere Backtests oder Strategie-Bausteine
-> Portfolio-Erstellung
-> kombinierte Equity / Stats / Contributions
-> Vergleich in Portfolio-Seiten
```

### 5. Live-Flow

```text
Live Deployment
-> Signalquelle oder interne Strategie
-> Risk Sizing
-> Broker Adapter
-> Execution / Balance / Position Tracking
-> API + WebSocket Streams
-> Live UI
```

## Strategien im aktuellen Repo

Im Repo liegen aktuell 21 Strategieordner. Die Bibliothek deckt bereits mehrere Stilrichtungen ab, was gut zum langfristigen Diversifikationsziel passt:

- Momentum / Trend:
  - `trend_following`
  - `gold_momentum`
  - `cross_asset_momentum`
- Mean Reversion / Short-Term:
  - `short_term_reversal`
  - `mean_reversion_rsi_bb`
  - `post_vix_dip_buy`
- Breakout:
  - `channel_breakout`
  - `volatility_breakout`
  - `qg_30min_es_breakout_strategy`
  - `pynecore_orb`
- Relative-Value / Allocation:
  - `stat_arb_pairs`
  - `risk_parity`
  - `sector_rotation`
- Weitere / experimentelle bzw. konvertierte Strategien:
  - `pine_supertrend_dema`
  - `pine_superosc_vumanchu`
  - `pynecore_sma_crossover`
  - `custom_swings_supertrend`
  - `lab_generated_strategy`
  - `example_sma`
  - `dema_rsi`
  - `ema_crossoiver_conversion_test`

Wichtig daran: Schon die aktuelle Struktur zeigt, dass die Plattform nicht auf eine einzige Edge setzt, sondern auf ein moeglichst breites Spektrum unterschiedlicher Marktlogiken.

## Datenmodell als Rueckgrat

Ein staerkerer architektonischer Punkt der Plattform ist das gemeinsame Datenmodell:

- Strategiemetadaten sind standardisiert.
- Backtests werden in ein gemeinsames `BacktestResult` ueberfuehrt.
- Trades unterstuetzen Partial Exits, Fees, Risk-Felder, Tags und Meta-Daten.
- Equity, Indicators und Drawings koennen fuer die Analyse-UI mitgespeichert werden.
- Portfolio-Stats besitzen bereits ein Feld fuer `correlation_matrix`.

Das ist besonders wichtig fuer das Langfristziel, weil genau diese Standardisierung spaeter robuste Vergleiche zwischen:

- Strategien
- Engines
- Maerkten
- Timeframes
- Portfolio-Kombinationen

ermoeglicht.

## API-Oberflaeche und Produktreife

`viewer/main.py` ist bereits deutlich mehr als ein einfacher Read-Only-Viewer. Die API deckt heute unter anderem ab:

- Strategie-Katalog und Strategie-Details
- Backtest-Listen, Details, Compare, Rename, Delete, Dedupe
- Trade-Replay und OHLCV-Abruf
- Backtests direkt aus der UI starten
- Signal-Replay-Backtests
- Optimierungsjobs
- Portfolio-Erstellung, Vergleich und Analyse
- Data Overview, Download, Harvest, Resample, Validate, Calibrate
- Task-Management fuer Hintergrundjobs
- Services und geplante Jobs
- Live Accounts, Live Deployments, Balances, Trades, Signals, Positions
- Logging
- WebSocket-Streams fuer Tasks und Live-Daten

Das zeigt: Die Plattform ist bereits in Richtung "Research + Ops Console" unterwegs und nicht nur in Richtung "lokaler Backtest-Skriptkasten".

## Persistenz und Betriebsmodell

Die Plattform speichert Laufzeit- und Research-Zustaende vor allem in:

- SQLite via SQLModel in `$DATA_DIR/backtests.db`
- JSON-Kompatibilitaetsartefakten fuer Backtests
- Parquet-Dateien fuer OHLCV und abgeleitete Daten
- Filesystem-Artefakten fuer Strategien, Optimierungen, Logs und Kataloge

Im DB-Layer sind nicht nur Backtests modelliert, sondern auch:

- Trades
- Equity Points
- Indicator-/Drawing-Daten
- Portfolios und Portfolio-Komponenten
- Live Signals
- Live Executions
- Live Balances
- Live Accounts
- Services
- Live Deployments
- Background Tasks
- Verification Evidence

Das ist ein starkes Signal dafuer, dass die Plattform zunehmend als persistente Betriebsplattform gedacht ist.

## Risiko- und Portfolio-Perspektive

Aus Sicht des langfristigen Ziels ist besonders relevant, dass das Repo bereits Bausteine enthaelt, die ueber isolierte Strategie-Performance hinausgehen:

- `risk/engine.py` fuer positionsgroessenbasierte Risk-Steuerung
- `risk/live.py` fuer Live-Signal-Sizing
- Portfolio-Modelle mit kombinierter Equity und Korrelation
- PSR- und p-Value-Betrachtung in den Statistiken
- CAPM-Auswertung fuer Benchmark-/Regressionssicht

Das passt gut zu einer reiferen Bewertungslogik:

- Nicht nur "Welche Strategie macht am meisten Return?"
- Sondern:
  - Welche Strategie verbessert die Portfolio-Statistik?
  - Welche Strategie bringt neue, unkorrelierte Return-Streams?
  - Welche Strategie reduziert den Gesamt-Drawdown?
  - Welche Strategie ist statistisch belastbar?

## Aktueller Reifegrad

Auf Basis des aktuellen Repos wirkt die Plattform wie ein fortgeschrittener Prototyp bzw. eine fruehe interne Produktplattform mit bereits breitem Funktionsumfang.

Besonders stark:

- klare Trennung von Daten-, Runner-, API- und Frontend-Schichten
- gemeinsames Datenmodell ueber mehrere Engines hinweg
- sichtbarer Fokus auf Portfolios statt nur Einzelbacktests
- vorhandene Live- und Service-Bausteine
- Strategy Lab als Hebel fuer schnelleren Strategiezuwachs
- fuer ein privates/kleines Quant-System bereits erstaunlich breite Produktflaeche

Noch typische Baustellen, die sich aus der Struktur ableiten:

- weitere Haertung der Live-Strecke
- staerkere systematische Portfolio-Selektion nach Korrelation/Regime
- robustere Walk-Forward- und Out-of-Sample-Prozesse
- klarere Governance zwischen experimentellen und produktionsreifen Strategien
- noch mehr Automatisierung fuer Promotion von Research nach Live

## Zahlen aus dem aktuellen Repo

Zum Zeitpunkt dieser Uebersicht:

- ca. 21 Strategieordner
- ca. 67 Python-Testdateien
- 18 Frontend-Seiten unter `frontend/src/pages`
- 10 Downloader-/Import-Module unter `data_tools/downloaders`
- ca. 396 Dateien in den Kernbereichen der Plattform

Das unterstreicht, dass hier bereits kein kleines Einzelskript mehr vorliegt, sondern ein wachsendes Plattform-Repository.

## Wie man die Plattform sinnvoll lesen kann

Wenn jemand neu ins Repo kommt, ist diese Reihenfolge am sinnvollsten:

1. `README.md` fuer Setup und Grundnutzung
2. `docs/ARCHITECTURE.md` fuer das grobe Gesamtbild
3. `shared/models.py` fuer das gemeinsame Datenmodell
4. `runner/cli.py` und `runner/engines/*` fuer den Research-Kern
5. `viewer/main.py` fuer die API und die Produktoberflaeche
6. `frontend/src/App.tsx` und `frontend/src/pages/*` fuer die Nutzeroberflaeche
7. `data_tools/*` fuer Datenversorgung
8. `risk/*`, `live_runner/*`, `scheduler/*` fuer den Uebergang Richtung Betrieb

## Einordnung in einem Satz

Die Plattform ist aktuell am besten als modularer Quant-Research- und Trading-Workspace zu verstehen, dessen eigentliche Mission nicht im einzelnen Backtest liegt, sondern im Aufbau eines skalierbaren Multi-Strategy-Portfolios mit moeglichst hohem Sharpe, stabilen Returns und moeglichst geringem, idealerweise diversifiziertem Drawdown.
