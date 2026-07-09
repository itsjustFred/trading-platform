

```markdown
# Frontend Redesign — Trading Platform Dashboard

> Ersetzt den aktuellen Trade-Viewer (viewer/static/index.html).
> Referenz-Dateien auf dem Desktop:
> - `~/Desktop/strategy_report_example.html` — Beispiel Strategy Report
> - `~/Desktop/backtest-comparison-example.html` — Beispiel Backtest-Vergleich
>
> **Design-Richtlinie:** Das gesamte Frontend soll sich am Stil der Example-Dateien orientieren (Layout, Spacing, Farben, Typografie). Die Examples definieren den visuellen Standard.

---

## Grundlayout

Zwei-Panel-Layout mit ausklappbarer Sidebar links:

```
┌──────┬──────────────────────────────────────────────┐
│      │                                              │
│  S   │                                              │
│  I   │           CONTENT AREA                       │
│  D   │           (wechselt je nach Navigation)      │
│  E   │                                              │
│  B   │                                              │
│  A   │                                              │
│  R   │                                              │
│      │                                              │
└──────┴──────────────────────────────────────────────┘
```

- **Sidebar:** Links, ein-/ausklappbar (Icon-Only wenn eingeklappt)
- **Content Area:** Rechts, füllt den restlichen Platz
- **Dark Theme** wie die Example-Dateien (TradingView-Style)

---

## Sidebar Navigation

| Icon | Label | Beschreibung |
|------|-------|-------------|
| 📊 | Strategies | Strategie-Übersicht und Details |
| 🧪 | Backtests | Backtest-Ergebnisse, Vergleich, neue Backtests starten |
| 💼 | Portfolio | Portfolio-Zusammenstellung und Analyse |
| 📋 | Logging | Log-Viewer (~/logs/, strukturierte Logs) |
| 👥 | Accounts | Später — User-Management, Broker-Accounts |

Aktiver Menüpunkt ist visuell hervorgehoben. Klick wechselt die Content Area.

---

## 1. Strategies

### 1.1 Übersicht (Content Area)

**Header:**
- Titel "Strategies"
- Toggle: Listenansicht / Kachelansicht
- Suchfeld (Freitext)

**Filter-Bar:**
- Nach Tags filtern (type: mean-reversion, momentum, breakout, etc.)
- Nach Source filtern (zenomtrader-discord, desenyon-github, indikatoren-repo, etc.)
- Nach Asset Class filtern (equities, forex, multi-asset, etc.)
- Nach Engine filtern (vectorbt, pynecore, lean)
- Nach Complexity filtern (simple, medium, complex)
- Filter sind kombinierbar (AND)

**Sortierung:**
- Name (A-Z)
- Anzahl Backtests (meiste zuerst)
- Beste Sharpe (aus allen Backtests)
- Neueste zuerst (letzter Backtest-Zeitpunkt)

**Listenansicht:**

| Name | Type | Source | Engine | Asset Class | Backtests | Best Sharpe |
|------|------|--------|--------|-------------|-----------|-------------|
| Short Term Reversal | mean-reversion | desenyon-github | vectorbt | equities | 3 | 1.45 |
| Gold Momentum | momentum | zenomtrader-discord | vectorbt | forex | 2 | 0.89 |

**Kachelansicht:**
Karten mit:
- Strategiename (groß)
- Tags als farbige Badges
- Source + Engine
- Anzahl Backtests
- Beste Sharpe als Zahl
- Kleine Equity-Curve Preview (vom besten Backtest)

### 1.2 Strategie-Detail (bei Klick auf eine Strategie)

**Oberer Bereich:**
- Strategiename + Version
- Tags als Badges (type, source, asset_class, complexity)
- Link zur Quelle (falls vorhanden)
- Engine
- Beschreibung / Notizen (falls vorhanden)
- Parameter-Tabelle (aus STRATEGY_META.parameters falls vorhanden)

**Bereich "Backtests mit dieser Strategie":**
- Liste aller Backtests die diese Strategie nutzen
- Jeder Eintrag zeigt: Zeitraum, Symbole, Sharpe, PnL, kleine Equity-Preview
- Klick → navigiert zum Backtest (öffnet Backtests-Sektion mit dem Backtest geladen)
- Sortierbar nach Sharpe, PnL, Datum

**Bereich "Portfolios mit dieser Strategie":**
- Liste aller Portfolios die diese Strategie enthalten
- Jeder Eintrag zeigt: Portfolio-Name, Risk per Trade %, Combined Sharpe
- Klick → navigiert zum Portfolio

**Datenquelle:**
- Strategien kommen aus STRATEGY_META in den Strategy-Files
- API: `GET /api/strategies` → Liste aller Strategien mit Tags
- API: `GET /api/strategies/<name>` → Strategie-Details + zugehörige Backtests/Portfolios

---

## 2. Backtests

### 2.1 Übersicht (Content Area)

**Header:**
- Titel "Backtests"
- Button "Create Backtest" (öffnet Dialog zum Starten eines neuen Backtests)
- Button "Compare" (öffnet Vergleichsansicht, aktiv wenn 2+ Backtests ausgewählt)
- Toggle: Listenansicht / Kachelansicht
- Suchfeld

**Filter-Bar:**
- Nach Strategie filtern
- Nach Symbol(en) / Universe filtern
- Nach Timeframe filtern (1m, 5m, 1h, 1d, etc.)
- Nach Engine filtern (vectorbt, pynecore, lean)
- Nach Zeitraum filtern (Backtest Start/End)
- Checkboxen zum Auswählen mehrerer Backtests (für Compare)

**Sortierung:**
- Net Profit (höchste zuerst)
- CAGR
- Sharpe Ratio
- Sortino Ratio
- Max Drawdown (niedrigster zuerst)
- Win Rate
- Profit Factor
- Anzahl Trades
- Datum (neueste zuerst)

**Listenansicht:**

| ☐ | Strategie | Symbole | TF | Trades | Net PnL | Sharpe | MaxDD | Equity Preview |
|---|-----------|---------|-----|--------|---------|--------|-------|----------------|
| ☐ | Short Term Reversal | AAPL, MSFT, ... | 1d | 141 | $307K | 1.45 | -8.3% | 📈 (mini chart) |

**Kachelansicht:**
Karten mit:
- Strategiename
- Symbole (gekürzt wenn viele)
- Timeframe
- Key-Stats: PnL, Sharpe, Win Rate
- Kleine Equity-Curve Preview
- Checkbox für Compare-Auswahl

### 2.2 Backtest-Detail / Strategy Report (bei Klick auf einen Backtest)

**Referenz:** Siehe `~/Desktop/strategy_report_example.html` für das gewünschte Layout und den visuellen Stil. Das Design soll so aussehen wie in dem Example.

Der Strategy Report zeigt alles zu einem einzelnen Backtest:

**Oberer Bereich — Zusammenfassung:**
- Strategiename, Engine, Zeitraum, Symbole
- Key-Stats in Cards: Net Profit, CAGR, Sharpe, Sortino, Max DD, Win Rate, Profit Factor, Calmar

**Chart-Bereich:**
- Equity-Curve (großer Chart, volle Breite)
- Drawdown-Chart darunter (synchronisiert)
- Optional: Monthly Returns Heatmap
- Optional: Underwater Plot (Drawdown über Zeit)

**Trade-Analyse:**
- Trade-Tabelle (sortierbar, filterbar, klickbar)
  - Spalten: #, Symbol, Direction, Entry, Exit, PnL, PnL%, R-Multiple, Duration, Exit Reason
  - Klick auf Trade → zeigt Candlestick-Chart mit Trade-Markern (wie im aktuellen Viewer)
- Trade-Statistiken:
  - Avg Winner vs Avg Loser
  - Avg Holding Period (Winners vs Losers)
  - Consecutive Wins/Losses
  - PnL-Verteilung (Histogram)
  - Win Rate nach Wochentag / Monat (optional)

**Drawing Tools:**
- Beibehalten aus dem aktuellen Viewer (Trendline, Horizontal, Fibonacci, etc.)
- Verfügbar wenn ein Trade-Chart angezeigt wird

### 2.3 Create Backtest (Dialog/Modal)

**Felder:**
- Strategie auswählen (Dropdown, aus verfügbaren Strategy-Files)
- Engine auswählen (vectorbt, pynecore, lean)
- Symbol(e) eingeben oder Universe auswählen
- Timeframe auswählen (1m, 5m, 15m, 1h, 4h, 1d)
- Zeitraum (Start / End Datum)
- Initial Capital
- Parameter (dynamisch aus STRATEGY_META.parameters, falls vorhanden)
- Button "Run Backtest"

**Verhalten:**
- Startet Backtest im Backend via API
- Zeigt Fortschritt / Loading
- Wenn fertig → navigiert automatisch zum neuen Backtest-Report

**API:** `POST /api/backtests/run` mit Config als JSON Body

### 2.4 Compare Backtests

**Referenz:** Siehe `~/Desktop/backtest-comparison-example.html` für das gewünschte Layout und den visuellen Stil. Das Design soll so aussehen wie in dem Example.

Wird geöffnet wenn 2+ Backtests in der Übersicht ausgewählt sind und "Compare" geklickt wird.

**Inhalte:**
- Equity-Curves aller ausgewählten Backtests übereinander (verschiedene Farben)
- Combined Portfolio Equity (mit konfigurierbarem Risk-Sizing)
- Stats-Vergleichstabelle (alle Strategien nebeneinander)
- Drawdown-Vergleich
- Korrelationsmatrix der Equity-Curves
- Combined Stats (Portfolio-Sharpe, Portfolio-MaxDD, etc.)

**API:** `GET /api/compare?ids=id1,id2,...` (existiert bereits)

---

## 3. Portfolio

### 3.1 Konzept

Ein Portfolio ist eine Zusammenstellung mehrerer Strategien die **unabhängig voneinander laufen** — so wie sie auch auf einem echten Account laufen würden.

**Keine Prozent-Allocation**, sondern **Risk-per-Trade pro Strategie:**
- Strategie A: 1% Risk per Trade
- Strategie B: 0.5% Risk per Trade
- Strategie C: 2% Risk per Trade

Jede Strategie handelt unabhängig mit ihrem eigenen Risk-Sizing. Das Portfolio ist die Summe aller Strategien auf einem Account.

**Portfolio-Definition:**
- Referenziert Backtests (nicht Strategien direkt)
- Pro Backtest: Risk-per-Trade (%), optional max. gleichzeitige Positionen
- Gesamtkapital des Accounts
- Combined Equity = Summe aller einzelnen Equity-Curves (mit jeweiligem Risk-Sizing)

**Analyse:**
- Combined Equity Curve (alle Strategien zusammen auf einem Account)
- Combined Stats: Sharpe, MaxDD, CAGR des Gesamtportfolios
- Korrelationsanalyse: Wie korreliert sind die Drawdowns? (Ziel: negativ korreliert = flache Kurve)
- Beitrag jeder Strategie: PnL-Anteil, Drawdown-Anteil

### 3.2 Übersicht (Content Area)

**Header:**
- Titel "Portfolios"
- Button "Create Portfolio"
- Button "Compare Portfolios" (aktiv wenn 2+ ausgewählt)
- Toggle: Listenansicht / Kachelansicht
- Suchfeld

**Filter-Bar:**
- Nach enthaltenen Strategien filtern
- Nach Anzahl Strategien filtern

**Sortierung:**
- Combined Sharpe
- Combined CAGR
- Combined MaxDD
- Net Profit
- Anzahl Strategien
- Datum (neueste zuerst)

**Listenansicht:**

| ☐ | Name | Strategien | Sharpe | CAGR | MaxDD | Equity Preview |
|---|------|-----------|--------|------|-------|----------------|
| ☐ | Diversified Alpha | Reversal (1%), Momentum (0.5%), Breakout (2%) | 2.1 | 18% | -5.2% | 📈 |

### 3.3 Portfolio-Detail (bei Klick)

**Oberer Bereich:**
- Portfolio-Name
- Account-Kapital
- Strategie-Tabelle (Strategie → Risk per Trade % → Max Positions)
- Key-Stats: Combined Sharpe, CAGR, MaxDD, Net Profit, Korrelation

**Charts:**
- Combined Equity Curve (groß)
- Einzelne Equity Curves (überlagert, verschiedene Farben)
- Drawdown-Chart
- Korrelationsmatrix (Heatmap)

**Strategie-Beiträge:**
- Tabelle: Welche Strategie trägt wie viel zum Gesamt-PnL bei
- Welche Strategie verursacht den meisten Drawdown

### 3.4 Create Portfolio (Dialog/Modal)

**Felder:**
- Portfolio-Name
- Account-Kapital (z.B. $100.000)
- Backtests auswählen (Multi-Select aus verfügbaren Backtests)
- Pro Backtest:
  - Risk per Trade % (z.B. 1%, 0.5%, 2%)
  - Max gleichzeitige Positionen (optional, z.B. 3)
- Button "Create"

**Verhalten:**
- Berechnet für jeden Backtest die Trades mit dem angegebenen Risk-Sizing neu
- Summiert alle Equity-Curves zum Combined Portfolio
- Berechnet Combined Stats, Korrelationen
- Speichert als JSON in ~/data/portfolios/
- Navigiert zum neuen Portfolio-Detail

**API:** `POST /api/portfolios` mit:
```json
{
  "name": "Diversified Alpha",
  "account_capital": 100000.0,
  "strategies": [
    {
      "backtest_id": "short_term_reversal_vectorbt_20260225-...",
      "risk_per_trade_pct": 1.0,
      "max_positions": 3
    },
    {
      "backtest_id": "gold_momentum_vectorbt_20260226-...",
      "risk_per_trade_pct": 0.5,
      "max_positions": 2
    }
  ]
}
```

### 3.5 Compare Portfolios

Wie Compare Backtests, aber für Portfolios:
- Combined Equity-Curves übereinander
- Stats-Vergleichstabelle
- Welches Portfolio hat das beste Risk-Adjusted Return

---

## 4. Logging

**Content Area zeigt:**
- Dropdown: Log-File auswählen (viewer.log, runner.log, etc. aus ~/logs/)
- Live-Tail der Logs (auto-scroll, neue Einträge erscheinen automatisch)
- Filter nach Log-Level (DEBUG, INFO, WARNING, ERROR)
- Suchfeld (Freitext)
- Farbcodierung nach Level (grün=INFO, gelb=WARNING, rot=ERROR)
- Timestamp + Service + Message als Spalten

**API:**
- `GET /api/logs` → Liste verfügbarer Log-Files
- `GET /api/logs/<name>?lines=100&level=ERROR` → Letzte N Zeilen, optional gefiltert
- Optional WebSocket für Live-Tail: `ws://localhost:5556/ws/logs/<name>`

---

## 5. Accounts (später, Phase 4+)

Platzhalter in der Sidebar. Nicht implementieren, nur den Menüpunkt anzeigen (ausgegraut).

Spätere Features:
- Broker-Accounts verwalten (Alpaca, cTrader)
- API-Key Management
- User-Rollen (Admin, Viewer)

---

## Technische Anforderungen

### Stack
- **Frontend:** React (Vite) — Komponenten-basiert, ideal für Multi-Page-Dashboard mit State-Management
- **Charts:** TradingView lightweight-charts (React-Wrapper: lightweight-charts-react-wrapper)
- **Styling:** Tailwind CSS oder CSS Modules, Dark Theme (TradingView-Style)
- **Backend:** Flask (wie bisher), neue Endpoints für Strategies/Portfolios
- **Routing:** React Router (Sidebar-Navigation → Content Area wechselt)
- **State:** React Context oder Zustand (leichtgewichtig, kein Redux nötig)
- **Build:** Vite (schnell, einfaches Setup)
- **Responsive:** Desktop-first, Sidebar collapse für kleinere Screens

**Hinweis:** Der aktuelle Viewer (Vanilla HTML/JS) wird komplett ersetzt. Die Drawing Tools und Chart-Logik werden als React-Komponenten neu gebaut, die Backend-Logik (lightweight-charts Integration, Trade-Marker, etc.) kann übernommen werden.

### Neue Backend-Endpoints

| Endpoint | Methode | Beschreibung |
|----------|---------|-------------|
| `/api/strategies` | GET | Liste aller Strategien (aus STRATEGY_META in Strategy-Files) |
| `/api/strategies/<name>` | GET | Strategie-Details + zugehörige Backtests |
| `/api/backtests` | GET | Liste aller Backtests (existiert, ggf. erweitern) |
| `/api/backtests/run` | POST | Neuen Backtest starten |
| `/api/portfolios` | GET | Liste aller Portfolios |
| `/api/portfolios` | POST | Neues Portfolio erstellen |
| `/api/portfolios/<id>` | GET | Portfolio-Details mit Combined Stats |
| `/api/portfolios/<id>` | DELETE | Portfolio löschen |
| `/api/logs` | GET | Liste verfügbarer Log-Files |
| `/api/logs/<name>` | GET | Log-Inhalt (letzte N Zeilen, filterbar) |

### Neues Datenformat: Portfolio JSON

Gespeichert in `~/data/portfolios/<id>.json`:

```json
{
  "id": "diversified-alpha-20260226",
  "name": "Diversified Alpha",
  "created_at": "2026-02-26T14:00:00Z",
  "account_capital": 100000.0,
  "strategies": [
    {
      "backtest_id": "short_term_reversal_vectorbt_20260225-...",
      "strategy_name": "Short Term Reversal",
      "risk_per_trade_pct": 1.0,
      "max_positions": 3
    },
    {
      "backtest_id": "gold_momentum_vectorbt_20260226-...",
      "strategy_name": "Gold Momentum",
      "risk_per_trade_pct": 0.5,
      "max_positions": 2
    }
  ],
  "combined_stats": {
    "sharpe": 2.1,
    "cagr": 0.18,
    "max_drawdown": -0.052,
    "net_profit": 145000.0,
    "correlation_matrix": {}
  },
  "combined_equity": [
    {"time": "2023-03-01", "equity": 100000.0}
  ],
  "strategy_contributions": [
    {
      "strategy_name": "Short Term Reversal",
      "pnl_contribution": 87000.0,
      "pnl_contribution_pct": 60.0,
      "max_drawdown_contribution": -3.1
    }
  ]
}
```

### Bestehendes beibehalten
- Drawing Tools (Trendline, Horizontal, Fibonacci, etc.)
- Keyboard Shortcuts
- Trade-Navigation (Prev/Next mit Pfeiltasten)
- Chart-Funktionalität (Candles, Volume, Markers)
- Comparison-Logik (Backend existiert)

### Referenz-Dateien
Die KI soll sich diese Dateien anschauen und das Design daran orientieren:
- `~/Desktop/strategy_report_example.html` — Strategy Report Design (visueller Standard)
- `~/Desktop/backtest-comparison-example.html` — Comparison Design (visueller Standard)
- `~/Desktop/trading-platform/viewer/static/index.html` — Aktueller Viewer (Code-Referenz für Chart-Logik)
