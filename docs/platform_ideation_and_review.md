# Platform Ideation & Code Review

Dieses Dokument fasst die Ergebnisse des Code-Reviews zusammen und skizziert eine Roadmap für die Weiterentwicklung zur High-Performance-Trading-Plattform.

## 1. Code-Review & Refactoring-Potenziale

### Code-Qualität & Wartbarkeit
- **Fehlerbehandlung in Data Tools:** `data_tools/catalog_builder.py` fängt Ausnahmen teilweise generisch ab (`except Exception as e`) und loggt nur Warnungen. Kritische Fehler beim Laden von Metadaten könnten übersehen werden.
- **Harte Pfad-Abhängigkeiten:** In `runner/engines/vectorbt_adapter.py` und anderen Modulen wird oft direkt auf Dateipfade zugegriffen. Eine Abstraktionsschicht (Data Access Layer / Repository Pattern) wäre sauberer.
- **Inkonsistente Parameter-Übergabe:** Der `VectorbtAdapter` unterstützt sowohl `dict`- als auch `kwargs`-Style für `generate_signals`. Dies ist flexibel, erschwert aber die statische Analyse und Typsicherheit. Ein striktes Interface (z.B. Pydantic Models für Configs) wäre robuster.
- **Komplexe Statistik-Logik:** `runner/stats.py` implementiert Fallbacks für `quantstats` und eigene Berechnungen (z.B. `_probabilistic_sharpe`, `_psr_formula`). Diese Duplikation ist fehleranfällig und schwer zu warten. Eine Konsolidierung auf eine robuste Bibliothek oder eine zentralisierte, gut getestete interne Library ist ratsam.
- **Frontend/Backend Vertrag:** Die API-Typen im Frontend (`frontend/src/api/types.ts`) müssen manuell mit `shared/models.py` synchron gehalten werden. Code-Generierung (z.B. via OpenAPI/Swagger) würde Diskrepanzen verhindern.

### Architektur & Performance
- **Daten-Laden:** Das Laden von Parquet-Dateien passiert bei jedem Backtest-Start von der Festplatte (`_load_ohlcv`). Bei vielen kleinen Dateien oder großen Backtests wird I/O zum Bottleneck.
  - *Lösung:* In-Memory Caching (Redis/Memcached) oder ein dedizierter Data-Service.
- **Single-Threaded Backtesting:** Der aktuelle Runner scheint Backtests sequenziell oder in einem Prozess auszuführen. Für Portfolio-Simulationen und Optimierungen ist Parallelisierung (Multiprocessing/Celery/Dask) zwingend notwendig.
- **Status-Handling:** `viewer/app.py` hält vermutlich Status im Speicher oder verlässt sich auf das Dateisystem. Für eine skalierbare Plattform (mehrere User/Instanzen) ist eine echte Datenbank (PostgreSQL) für Metadaten, Jobs und User-Status unerlässlich.

---

## 2. Feature-Ideen (Data & Infrastructure)

- **Datenbank-Evolution:**
  - Migration von Flat-Files (Parquet) zu einer spezialisierten **Time-Series Database** (z.B. TimescaleDB, ClickHouse oder ArcticDB) für Tick- und Minutendaten. Dies ermöglicht extrem schnelle Aggregationen und Abfragen beliebiger Zeiträume.
- **Data Ingestion Pipeline:**
  - Implementierung robuster ETL-Prozesse (Extract, Transform, Load) mit Frameworks wie Apache Airflow oder Dagster.
  - Integration von Echtzeit-Datenfeeds (WebSockets) via `ccxt` oder direkter Exchange-APIs, die in einen Message Bus (Kafka/RabbitMQ/NATS) schreiben.
- **Data Quality Gate:**
  - Automatische Checks beim Import: Erkennung von "Flatlines" (Preis ändert sich nicht), Spikes (unrealistische Sprünge), Gaps und Timestamps, die nicht zum Markt-Kalender passen.
- **Symbol Master:**
  - Zentrale Mapping-Tabelle für Ticker-Symbole verschiedener Börsen (z.B. `BTCUSDT` auf Binance vs. `BTC-USD` auf IBKR).

---

## 3. Feature-Ideen (Backtesting & Research)

- **Erweiterte Validierung:**
  - **Walk-Forward Optimization (WFO):** Automatisierte Aufteilung in In-Sample (Training) und Out-of-Sample (Testing) Perioden, um Overfitting zu erkennen.
  - **Monte Carlo Simulation:** Resampling der Trades (Shuffle, Bootstrapping), um die Wahrscheinlichkeit verschiedener Equity-Kurven-Verläufe zu bestimmen.
- **Realistische Simulation:**
  - **Slippage-Modelle:** Statt fixer Prozentsätze Nutzung von Volatilitäts- oder Volumen-abhängigen Slippage-Modellen.
  - **Latenz-Simulation:** Künstliche Verzögerung der Order-Ausführung, um Live-Trading-Bedingungen zu simulieren.
- **Optimierung:**
  - Integration von Hyperparameter-Optimierungs-Frameworks wie **Optuna** (Bayesian Optimization), um effizienter als mit Grid-Search optimale Parameter zu finden.
- **Research Environment:**
  - Integrierte JupyterLab-Umgebung, die direkten Zugriff auf den `Data Access Layer` und die `Backtest Engine` hat, für schnelles Prototyping.

---

## 4. Feature-Ideen (Portfolio & Live Trading)

- **Portfolio Construction Engine:**
  - Ein System, das Equity-Kurven mehrerer Strategien nimmt und sie basierend auf Regeln kombiniert (z.B. "Maximiere Sharpe Ratio", "Risikoparität", "Inverse Volatilität").
- **Order Management System (OMS) & Execution:**
  - **Execution Algos:** TWAP (Time-Weighted Average Price), VWAP (Volume-Weighted), Iceberg-Orders für große Positionen.
  - **Smart Order Routing:** Bei Krypto Assets das Routing zu der Börse mit dem besten Preis/Liquidität.
- **Risk Management Layer (Pre-Trade & Post-Trade):**
  - **Pre-Trade Checks:** Verhindern von Orders, die Limits überschreiten (Max Position Size, Max Leverage, Daily Loss Limit).
  - **Kill Switch:** Globaler Not-Aus bei abnormalem Systemverhalten oder extremen Marktbedingungen.
- **Live Reconciliation:**
  - Automatischer Abgleich zwischen internem System-Status (erwartete Position) und Broker-Status (tatsächliche Position). Alarmierung bei Abweichungen.

---

## 5. Erweiterte Quant-Konzepte

- **Korrelations-Clustering:**
  - Nutzung von Hierarchischem Clustering oder PCA (Principal Component Analysis), um Gruppen von Strategien zu finden, die sich ähnlich verhalten, und gezielt unkorrelierte Cluster auszuwählen.
- **Custom Objective Functions:**
  - Entwicklung eigener Metriken für die Optimierung, z.B. `(CAGR * Sortino) / (MaxDD^2)` oder Metriken, die Tail-Risiken stärker gewichten (VaR, CVaR).
- **Machine Learning Integration:**
  - **Feature Store:** Zentrale Verwaltung von berechneten Features (RSI, Volatilität, On-Chain-Daten), um Training und Inferenz konsistent zu halten.
  - **Modelle:** Nutzung von Gradient Boosting (XGBoost, LightGBM) oder LSTMs/Transformers für Signalgenerierung oder Regime-Detection (z.B. "Ist der Markt gerade Trend oder Range?").
- **Alpha-Faktoren-Bibliothek:**
  - Aufbau einer standardisierten Bibliothek klassischer Alpha-Faktoren (Momentum, Mean Reversion, Carry, Value, Quality), die als Bausteine für Strategien dienen.

