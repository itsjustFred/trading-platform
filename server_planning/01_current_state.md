# Bestandsaufnahme Server & GitHub — 2026-02-24

> **Letzte Aktualisierung:** 2026-02-24 (Offene Fragen geklärt, Kontext ergänzt)

---

## 1. Projekte / Repos

### 1.1 Lokal auf dem Server (~/Desktop/)

| # | Name | Pfad | Beschreibung | Sprache / Framework | Status | Abhängigkeiten |
|---|------|------|-------------|---------------------|--------|----------------|
| 1 | **stockkit_ai_bot** | `~/Desktop/stockkit_ai_bot` | Automatisierter Aktienhandel: liest Discord-Signale, handelt über Alpaca API. Zwei unabhängige Strategien (Ram Jet, Rocket Fuel) als separate Prozesse mit eigenen TUI-Dashboards. | Python 3.12 (alpaca-py, pandas, yfinance, pandas_ta, Rich) | **Läuft live** — beide Bots laufen seit 23. Feb (Paper-Trading via Alpaca). Vorheriger Test nur mit Ram Jet war profitabel, aber ohne gutes Logging. | Discord (Signal-Quelle), Alpaca API (Orderausführung) |
| 2 | **custom_lean_integration** | `~/Desktop/custom_lean_integration` | Eigenes Backtesting-Framework mit LEAN-Engine-Anbindung. Enthält Mean-Reversion-Strategie (RSI-basiert, SP500-Universum), SuperTrend-Strategie, Daten-Downloader, Trade-Viewer und diverse Utilities. | Python 3.12 (lean, quantconnect, mplfinance) | **Backtest** — aktive Entwicklung, letzte Backtests vorhanden. Zwei HTTP-Server laufen (Ports 8000, 5556) für Trade-Viewer HTML. | QuantConnect/LEAN-SDK, Dukascopy-Daten (XAUUSD) |
| 3 | **custom_TradingAgents** | `~/Desktop/custom_TradingAgents` | Performance-optimierter Fork von TauricResearch/TradingAgents. Multi-Agent LLM Trading Framework: Analysten (News, Social, Fundamentals, Market), Risk-Manager, Researcher (Bull/Bear), Trader-Agent. LangGraph-basiert. | Python (LangChain, LangGraph, OpenAI/Anthropic/Google/xAI/OpenRouter APIs, yfinance) | **Experiment/Pausiert** — Eval-Ergebnisse für NVDA vorhanden. Kein Live-Trading. | Diverse LLM-APIs (OpenAI, Anthropic, Google, xAI, OpenRouter, NVIDIA), Alpha Vantage |

### 1.2 Nur auf GitHub (nicht lokal geklont)

| # | Name | URL | Sprache | Größe | Letzter Push | Beschreibung (aus Dateistruktur abgeleitet) |
|---|------|-----|---------|-------|-------------|---------------------------------------------|
| 4 | **Indikatoren** | `github.com/itsjustFred/Indikatoren` | Diverse (Pine, MQL5, Python) | 565 KB | 2026-02-07 | Sammlung von selbst gecodeten Trading-Indikatoren und Strategien: PineScript (v5/v6), MQL5 Swings EA, SuperTrend DEMA, Vix Spike, diverse Swing-Indikatoren, Buy/Sell-Indikatoren. Alle auf TradingView bzw. MT5 getestet. ~28 Dateien/Ordner. |
| 5 | **ML_tick_data_prepare** | `github.com/itsjustFred/ML_tick_data_prepare` | Python | 56 KB | 2026-01-30 | **cTrader Data-Downloader** (Tick-Data und Time-Bars), Gap-Analyse. Feature-Engineering hier ist veraltet (abgelöst durch trading_ml). |
| 6 | **custom_oipd** | `github.com/itsjustFred/custom_oipd` | Python | 21 KB | 2026-01-27 | Fork/Custom von OIPD (Options Implied Probability Distribution). Einzelne Datei `custom_oipd.py` mit yfinance-Cache. |
| 7 | **trading_ml** | `github.com/itsjustFred/trading_ml` | Python | 283 KB | 2025-12-02 | ML-Trading-Pipeline: Bayesian Optimization, Ensemble-Modelle, Train/Replay-Pipelines. **Status: Pausiert**, evtl. später relevant für ML-Integration. |
| 8 | **llm-ai-trading-bot** | `github.com/itsjustFred/llm-ai-trading-bot` | Python | 116 KB | 2025-10-14 | LLM-basierter Trading-Bot. **Status: Abgelöst** durch custom_TradingAgents. |

> Alle Repos sind **privat**.

### 1.3 Externe Quellen (noch nicht lokal)

Zusätzlich zu den eigenen Repos gibt es Strategien aus:
- **Externe GitHub-Repos** — diverse LEAN-Strategien von anderen Accounts
- **Discord-Server** — hauptsächlich NinjaTrader 8 Strategien
- **YouTube/Internet** — Strategie-Ideen die noch umgesetzt werden müssen

Diese werden später katalogisiert wenn sie tatsächlich integriert werden.

---

## 2. Daten

### 2.1 stockkit_ai_bot — `~/Desktop/stockkit_ai_bot/data/`

| Was | Pfad | Format | Inhalt | Zeitraum | Symbole | Größe | Quelle |
|-----|------|--------|--------|----------|---------|-------|--------|
| Kurs-Daten | `data/*.csv` (76 Dateien) | CSV (OHLCV + Dividends/Splits) | 5-Min-OHLCV | ~Nov 2025 – Feb 2026 (~3 Monate) | 76 US-Aktien (AMD, NVDA, CRWD, HOOD, SOFI, SHOP, etc.) | ~37 MB gesamt | yfinance |
| Signale Ram Jet | `data/signals/signals_ram_jet.csv` | CSV (`;`-getrennt) | Discord-Signale (Symbol, Preis, Datum, Zeit) | ab 2026-02-23 | diverse | < 1 KB | Discord-Channel |
| Signale Rocket Fuel | `data/signals/signals_rocket_fuel.csv` | CSV (`;`-getrennt) | Discord-Signale (5 Signaltypen) | ab 2026-02-23 | diverse | < 1 KB | Discord-Channel |
| Trade History | `data/trade_history/live_trades_ram_jet.csv` | CSV (`;`-getrennt) | Live-Trade-Log (Entry, SL, TP, Exit, PnL, R-Multiple) | 2026-02-23 | CRWD, AXSM | < 1 KB | Eigene Aufzeichnung |
| Bot State | `data/bot_state_ram_jet.json`, `bot_state_rocket_fuel.json` | JSON | Aktive Positionen, Pending Signals | Laufzeit | — | < 1 KB | Runtime |

### 2.2 custom_lean_integration — `~/Desktop/custom_lean_integration/data/`

| Was | Pfad | Format | Inhalt | Zeitraum | Symbole | Größe | Quelle |
|-----|------|--------|--------|----------|---------|-------|--------|
| XAUUSD H1 | `data/XAUUSD_H1_*.csv` | CSV (OHLCV) | Stunden-Kerzen Gold | Nov 2020 – Feb 2026 | XAUUSD | 2.2 MB (31k Zeilen) | Dukascopy |
| XAUUSD M15 | `data/XAUUSD_M15_*.csv` | CSV (OHLCV) | 15-Min-Kerzen Gold | Nov 2020 – Feb 2026 | XAUUSD | 8.4 MB (124k Zeilen) | Dukascopy |
| US-Equities H1 | `data/*_H1_730d.csv` | CSV (OHLCV) | Stunden-Kerzen | ~2 Jahre (730d) | AAPL, AMZN, JNJ, JPM, MSFT | je ~528 KB (5k Zeilen) | unbekannt |
| US-Equities M15 | `data/*_M15_*.csv` | CSV (OHLCV) | 15-Min-Kerzen | Nov 2025 – Feb 2026 | AAPL, AMZN, JNJ, JPM, MSFT | je ~160 KB | unbekannt |
| Daily Equities | `data/equity/*_daily.csv` | CSV (OHLCV) | Tages-Kerzen | ~5 Jahre (1255 Zeilen) | AAPL, AMZN, GOOGL, JNJ, JPM, KO, NVDA, PG, XOM | je ~116 KB | yfinance / LEAN |
| Market Hours DB | `data/market-hours/market-hours-database.json` | JSON | Handelszeiten-Definitionen | — | — | — | QuantConnect |
| Symbol Properties | `data/symbol-properties/symbol-properties-database.csv` | CSV | Symbol-Metadaten | — | — | — | QuantConnect |
| Backtest-Ergebnisse | `backtests/*/results.json` | JSON | Backtest-Ergebnisse (Equity Curve, Trades) | — | — | — | Eigenes Framework |

**Gesamt: ~19 MB**

### 2.3 custom_TradingAgents — `~/Desktop/custom_TradingAgents/eval_results/`

| Was | Pfad | Format | Inhalt | Zeitraum | Symbole | Größe | Aktion |
|-----|------|--------|--------|----------|---------|-------|--------|
| Evaluierungs-Logs | `eval_results/NVDA/TradingAgentsStrategy_logs/` | JSON (239 Dateien) | Vollständige Agent-State-Logs pro Handelstag | Jan 2025 – Dez 2025 | NVDA | **~4.1 GB** | **KANN GELÖSCHT WERDEN** — alte Experiment-Logs, nicht mehr benötigt |

### 2.4 SP500 Universe

| Was | Pfad | Format |
|-----|------|--------|
| SP500-Komponentenliste | `~/Desktop/custom_lean_integration/universe_sp500.txt` | Textdatei mit Datumseinträgen (Änderungslog) |

---

## 3. Konfiguration & Secrets

### 3.1 .env-Dateien

| Projekt | Pfad | Enthaltene Services |
|---------|------|---------------------|
| **stockkit_ai_bot** | `~/Desktop/stockkit_ai_bot/.env` | Discord Token, Alpaca API Keys (2× separate Accounts für Ram Jet + Rocket Fuel), Alpaca Endpoints (Paper-Trading) |
| **custom_TradingAgents** | `~/Desktop/custom_TradingAgents/.env` | OpenAI, Google AI, Anthropic, xAI, OpenRouter, NVIDIA, Alpha Vantage |

### 3.2 Weitere Config-Dateien

| Datei | Pfad | Zweck |
|-------|------|-------|
| `config.yaml` | `~/Desktop/stockkit_ai_bot/config/config.yaml` | Strategie-Parameter für beide Bots (ATR, RSI, Risk, Timing) |
| `config.json` | `~/Desktop/custom_lean_integration/config.json` | LEAN local-id |
| `backtest_config.json` | `~/Desktop/custom_lean_integration/backtest_config.json` | Backtest-Parameter (Zeitraum, Cash, Strategie-Config) |
| `strategy_comparison.json` | `~/Desktop/custom_lean_integration/strategy_comparison.json` | Vergleich der Equity Curves verschiedener Strategien |
| `.gitconfig` | `~/.gitconfig` | Git User: itsjustFred, Email: justfred.business@gmail.com |

### 3.3 Genutzte Services (Zusammenfassung)

**Aktiv genutzt:**
- **Alpaca** — Paper-Trading API (2 Accounts: Ram Jet, Rocket Fuel)
- **Discord** — Signal-Quelle (kostenloser Public-Server, StockKit AI Community)
- **QuantConnect** — LEAN lokal (nicht Cloud)
- **GitHub** — Code-Hosting

**Für TradingAgents (pausiert):**
- OpenAI, Anthropic, Google AI, xAI, OpenRouter, NVIDIA NIM, Alpha Vantage

### 3.4 Organisation

- `.env`-Dateien liegen jeweils im Projekt-Root
- `.env.example`-Dateien vorhanden bei stockkit_ai_bot und custom_TradingAgents
- Kein zentrales Secret-Management (kommt später)

---

## 4. Infrastruktur

### 4.1 Server-Basics

| Eigenschaft | Wert |
|-------------|------|
| Instanz | OCI Free Tier (VM.Standard.A1.Flex) |
| Ressourcen | 4 OCPUs, 24 GB RAM, 100 GB Boot-Drive |
| OS | Ubuntu 24.04 LTS |
| Python | 3.12.3 |
| Node.js | 24.13.1 |
| npm | 11.8.0 |
| Docker | 29.2.1 (installiert, noch nicht produktiv genutzt) |
| Remote Desktop | xRDP (Port 3389) mit XFCE4 Desktop |

**Geplant:** Zweite 100 GB Disk für zentrales Data-Directory.

### 4.2 Laufende Services

| Service | Port(s) | Details |
|---------|---------|---------|
| **stockkit Ram Jet Bot** | — | Python-Prozess, läuft seit 23. Feb |
| **stockkit Rocket Fuel Bot** | — | Python-Prozess, läuft seit 23. Feb |
| **HTTP-Server (Trade-Viewer)** | 8000, 5556 | Temporär öffentlich für Dev-Zugriff, wird später abgesichert |
| **xRDP** | 3389 | Remote Desktop |
| **SSH** | 22 | SSH-Zugang |

### 4.3 Cronjobs

- **Keine** Cronjobs konfiguriert

### 4.4 Docker

- Docker **installiert und läuft**
- Noch **nicht produktiv genutzt** — geplant für spätere Containerisierung

### 4.5 Zugriff

- Aktuell über RDP und SSH
- **Geplant:** Domain + Web-Frontend für zentrales Management

---

## 5. Frontend

### 5.1 Vorhandene UI-Komponenten

| Wo | Technologie | Beschreibung |
|----|-------------|-------------|
| `stockkit_ai_bot/src/tui/dashboard.py` | **Rich** (Python Terminal UI) | Terminal-Dashboard für jeden Bot: zeigt Positionen, Signale, PnL in Echtzeit |
| `custom_lean_integration/static/trade_viewer.html` | **Plain HTML/JS** | Web-basierter Trade-Viewer (guter Ausgangspunkt fürs Frontend) |
| `custom_lean_integration/view_trades.py` | **Python** (mplfinance) | Matplotlib-basierte Trade-Visualisierung |
| `stockkit_ai_bot/trade_viewer.py` | **Python** (lightweight-charts) | Trade-Viewer für Stockkit |

### 5.2 Status

Aktuell kein dediziertes Web-Frontend (React/Vue/etc.). Alle UIs sind Terminal-basiert oder einfache HTML-Dateien.

**Geplant:** Separates Frontend das vom Backend getrennt ist, mit `trade_viewer.html` als Ausgangspunkt.

---

## 6. Strategien-Übersicht

### 6.1 Lokale Strategien (auf dem Server)

| # | Name | Pfad | Sprache | Status |
|---|------|------|---------|--------|
| 1 | Ram Jet (ATR/Supertrend) | `stockkit_ai_bot/src/bots/ram_jet/strategy.py` | Python | **Live (Paper)** |
| 2 | Rocket Fuel (RSI Reversal) | `stockkit_ai_bot/src/bots/rocket_fuel/strategy.py` | Python | **Live (Paper)** |
| 3 | Mean Reversion (RSI/Universe) | `custom_lean_integration/src/strategies/mean_reversion/strategy.py` | Python (LEAN) | Backtest |
| 4 | SuperTrend Blob | `custom_lean_integration/src/strategies/supertrend_blob_strat.py` | Python (LEAN) | Backtest |
| 5 | SuperTrend Blob (Original) | `custom_lean_integration/rndm stuff/.../strategy_supertrend_blob.pine` | PineScript | Original |

### 6.2 Strategien auf GitHub (Repo: Indikatoren)

Selbst gecodete Strategien, alle auf TradingView bzw. MT5 getestet:
- PineScript v5/v6: diverse Swing-Indikatoren, Buy/Sell-Indikatoren, Supertrend DEMA, Vix Spike, etc.
- MQL5: Swings EA
- ~28 Dateien/Ordner insgesamt

### 6.3 Weitere eigene Repos

| Repo | Inhalt | Status |
|------|--------|--------|
| **trading_ml** | ML-Pipeline: Bayesian Optimization, Ensemble-Modelle | Pausiert, später relevant |
| **llm-ai-trading-bot** | LLM-basierter Trading-Bot | Abgelöst durch TradingAgents |
| **ML_tick_data_prepare** | Tick-Daten-Aufbereitung, Gap-Analyse | Utility |
| **custom_oipd** | Options Implied Probability Distribution | Utility |

### 6.4 Externe Strategien (noch nicht integriert)

- **~100 Strategien** aus verschiedenen Quellen (externe GitHub-Repos, Discord-Server, YouTube)
- Formate: Pine, MT5, NT8, LEAN
- Status: Noch nicht gesichtet/getestet
- **Nächster Schritt:** Triage — sortieren nach vielversprechend/vielleicht/müll

---

## 7. Notizen & Kontext

### OpenClaw
OpenClaw Gateway läuft auf dem Server als KI-Agent-System. **Nicht relevant für das Trading-Setup** — wird für andere Projekte genutzt.

### Haupt-Schmerz (aktuell)
Strategien liegen in verschiedenen Systemen (Pine, MT5, NT8, LEAN). Umschreiben in ein einheitliches Format funktioniert mit KI oft nicht gut. TradingView Premium fehlt für ordentliche Pine-Backtests.

### Geplante Lösung
- Verschiedene Backtest-Libraries je nach Komplexität (LEAN für aufwendiges, vectorbt/ähnliches für simples, evtl. PyneCore für Pine)
- **Universelles Trade-Output-Format** für Vergleichbarkeit aller Strategien
- Zentraler Trade-Viewer der das Format darstellt

---

## 8. Cleanup-Tasks

- [ ] `~/Desktop/custom_TradingAgents/eval_results/` löschen (~4.1 GB freigeben)
- [ ] `~/Desktop/idee` löschen (leere Datei)
- [x] Git PAT aus Remote-URL entfernt (custom_lean_integration)