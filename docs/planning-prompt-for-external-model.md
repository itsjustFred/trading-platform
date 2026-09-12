# Trading Platform Planning Prompt (für ChatGPT Thinking / Deep Research)

## Kontext
Fred arbeitet an einer automatisierten Trading-Plattform unter `/home/ubuntu/Desktop/trading-platform` (Branch `codex_rewrite`). Ziel: profitable Strategien finden, backtesten, portfoliomäßig kombinieren und live/demo über Alpaca + cTrader + Discord-Signale handeln. Viel Code ist vibecoded und muss stabilisiert werden.

**WICHTIG:** Bevor du einen Plan erstellst, stelle erst Rückfragen, um Freds genaue Erwartungen und Prioritäten zu verstehen. Wenn du unsicher bist, frage nach — erst wenn das Ziel klar ist, starte die Deep-Research-Phase.

---

## Oberziel
**Maximaler Sharpe bei hohem CAGR durch viele profitable Strategien mit negativ oder unkorreliertem Drawdown.**
Das bedeutet:
- Suche nicht nur einzelne „beste“ Strategien, sondern einen **Strategie-Pool**.
- Zeichne dich durch **Diversifikation** aus: Drawdowns sollen möglichst nicht gleichzeitig auftreten (negative/unkorrelierte Drawdowns).
- Später: Portfolio-Optimierung über diesen Pool, um die **beste Allokation** für maximales Sharpe-Ratio zu finden.
- Anti-Overfitting ist zentral (Walk-Forward, OOS, harte Constraints).

---

## Verfügbare Modelle / Provider (Usage + Kosten)
1. **Kimchi** (custom:llm.kimchi.dev)
   - ca. **140 $ verfügbar** (50+50+45 Free-Credits; wenn man den Agent runterlädt gibt es weitere ~200 $, aber unklar wie genau; realisticher sind ~75–145 $ nutzbar)
   - 7 Credentials im Pool, Strategy: fill_first
   - Nutzung: Open-Modelle, ggf. Coding/Review

2. **Commandcode go plan**
   - ca. **2 $**
   - Nutzung: Planung

3. **GitHub Copilot Student**
   - ca. **1,90 $**
   - Nutzung: Code-Vorschläge, falls verfügbar

4. **Lightning.ai**
   - ca. **14 $** (Free-Credits)
   - Proprietäre Modelle
   - Geht in Hermes aktuell nicht (kp warum)
   - Nutzung: Evtl. separat prüfen, falls nicht fixbar, ignorieren

5. **Aerolink** (aerolink.lat)
   - **10 $/5h** und **70 $/Woche** Limit (Starter Tier)
   - Claude Models
   - Cloudflare blockt aktuell direkte Hermes-Provider-Tests (HTTP 403/1010)
   - Nutzung: Falls über Claude Code nutzbar, für Code-Reviews/Refactoring

6. **FreeModel** (freemodel.dev)
   - **10 $ Bonus-Credits**, **10 $/5h** und **66,67 $/Woche** Limit (Free-Signup-Pro-Plan)
   - **Ablaufdatum: 17. Juli** — kurz davor daran erinnern, ggf. mit Referral-Link und zweiter Email verlängern falls möglich
   - 1 Credential im Pool
   - Nutzung: Backup für gpt-5.5, wenn BluesMinds leer ist

7. **BluesMinds** (custom:api.bluesminds.com)
   - Bereits **~96 $ von 100 $ verbraucht**, nur noch **~4 $ übrig** (sehr knapp)
   - 2 Credentials im Pool
   - Nutzung: gpt-5.5 nur für Architektur/tricky Debugging/Final Review — **sparsam einsetzen!**

8. **OpenAI ChatGPT (Codex OAuth)**
   - 2 Free-Accounts verwendet, beide aufgebraucht (jeweils Limits erreicht)
   - Nicht mehr verfügbar bis Reset

9. **NVIDIA NIM** (nvidia NIM Provider)
   - Keine Dollar-Limits, kostenlose Open Models
   - Rate Limits möglich
   - Beispiele: Kimi 2.6
   - Nutzung: Coding/Recherche/Review, wenn Quality reicht

10. **Pi / commandcode / MiniMax-M3** (pi/commandcode/MiniMaxAI/MiniMax-M3)
    - Praktisch unlimited usage laut aktuellem Stand
    - Vision-Fähigkeiten
    - Nutzung: Implementierung, Playwright/UI-Tests, repetitive Arbeiten

---

## Verfügbare Skills / Workflows
- the-fool (Pre-Mortem)
- systematic-debugging
- test-driven-development
- subagent-driven-development
- mutation-testing
- requesting-code-review
- CodeRabbit CLI (max 3 Reviews/Stunde)
- Plan-Mode (starke Modelle für Planung, schwächere für Implementierung)

---

## Strategie-Vergleiche — Daten-Anforderungen

Im Repo existieren bereits ausgearbeitete Strategie-Vergleiche unter `strategy-comparisons/` mit HTML-Reports und Charts. Diese müssen mit OHLCV-Daten unterfüttert werden, damit die Strategien in der Plattform lauffähig sind.

### 1. Intraday Volatility Breakout
- **Pfade:** `strategy-comparisons/intraday_volatility_breakout/`
- **Benötigte Daten:**
  - `EURUSD` — H1 (1-Stunden-Candles), Zeitraum: 2018-01-01 bis heute
  - `SP500` — M1 (1-Minuten-Candles), Zeitraum: 2018-01-01 bis heute
  - `US100` — M15 (15-Minuten-Candles), Zeitraum: 2018-01-01 bis heute
- **Datenquellen:**
  - `EURUSD H1`: **Dukascopy** (kostenlos, native .bi5 → Parquet).
  - `SP500 M1`: **Dukascopy** Index-CFD, Alias `SP500`/`US500` → `usa500idxusd`.
  - `US100 M15`: **Dukascopy** Index-CFD, Alias `US100`/`NAS100`/`USTEC` → `usatechidxusd`.
- **Geschätzte Dauer:**
  - Dukascopy EURUSD H1 (8 Jahre): ~5–10 Minuten
  - Dukascopy SP500/US100 Intraday-CFDs: Minuten bis länger je nach Zeitraum; native Downloader mit Batch-Pipeline nutzen.
- **Hinweis:** Nicht auf yfinance-Proxies ausweichen, solange die Dukascopy-CFDs verfügbar sind.

### 2. Risk Parity
- **Pfad:** `strategy-comparisons/risk_parity/`
- **Benötigte Daten:**
  - Täglich (D1) für: `SPY`, `QQQ`, `EFA`, `EEM`, `TLT`, `IEF`, `LQD`, `TIP`, `GLD`, `DBC`, `VNQ`
  - Zeitraum: 2016-01-01 bis 2024-12-31
- **Datenquellen:**
  - **yfinance** — alle 11 ETFs frei verfügbar, Daily unlimitiert.
  - Alternativ Dukascopy für: `SPY.US/USD`, `QQQ.US/USD`, `EFA.US/USD`, `EEM.US/USD`, `TLT.US/USD`, `IEF.US/USD`, `GLD.US/USD`, `VNQ.US/USD` (Start 2017/2018, also nicht vollständig für 2016).
  - `LQD`, `TIP`, `DBC` sind **nicht** auf Dukascopy → yfinance verwenden.
- **Geschätzte Dauer:** ~5 Minuten für alle 11 Symbole (Daily, 9 Jahre).
- **Status:** ✅ **Bereits heruntergeladen** via `data_tools/downloaders/yfinance_dl.py` unter `/mnt/block-volume-data/ohlcv/yfinance/equity/US/{SYMBOL}/1d.parquet`.

### 3. Dukascopy EURUSD H1
- **Status:** ✅ **Download abgeschlossen** (trotz 503-Warnungen für einzelne Tage).
  - **Datei:** `/mnt/block-volume-data/ohlcv/dukascopy/forex/EURUSD/1h.parquet`
  - **Zeitraum:** 2008-01-01 bis 2026-06-21
  - **Zeilen:** 52.575
  - **Quelle:** Dukascopy native `.bi5` → Parquet

---

## Projekt-Phasen (Priorisiert)

Phase 1: Live Trading Smoke Tests (Alpaca + cTrader Demo Orders)
Phase 2: Discord Signal Routing end-to-end
Phase 3: Portfolio Optimizer & Grid Tester produktiv machen
Phase 4: Walk-Forward / OOS Validation
Phase 5: Viewer UI / Playwright Tests
Phase 6: DB-Konsolidierung + Dukascopy Speed

---

## Aufgabe

Erstelle einen detaillierten, ausführbaren Plan, der für jede Phase empfiehlt:
- Welches Model / welcher Provider genutzt werden soll
- Warum (Quality vs. Cost vs. Rate Limits)
- Welche Skills / Workflows angewendet werden sollen
- Welche Tests verpflichtend sind
- Welche Risiken es gibt (z.B. BluesMinds leer, Kimchi exhausted, Cloudflare bei Aerolink)

Berücksichtige dabei:
- Hochwertige Planung/Architektur/Review → starke Modelle (gpt-5.5 via BluesMinds oder NVIDIA NIM)
- Repetitive Implementierung/UI-Tests → MiniMax-M3
- Open-Source-Alternativen → Kimchi oder NVIDIA NIM
- Claude-spezifische Aufgaben → Aerolink nur, wenn über Claude Code nutzbar

**WICHTIG — Daten-Beschaffung:**
- Prüfe zuerst, welche OHLCV-Daten bereits im Repo vorhanden sind (`data/ohlcv/`, `data/catalog.json`).
- Falls Daten fehlen: Lade sie via vorhandene Downloader (`data_tools/downloaders/yfinance_dl.py`, `dukascopy_native.py`) herunter.
- Für Strategy-Comparisons unbedingt die benötigten Symbol/Timeframe-Kombinationen sicherstellen.
- Dokumentiere Limitationen (z.B. yfinance M1 nur 60 Tage frei; Dukascopy-US-Index-CFDs sind für Intraday verfügbar und sollen bevorzugt werden).
- Gib bei jedem Daten-Download den Befehl, die erwartete Dauer und das Zielverzeichnis an.

**WICHTIG — Setup-Dokumentation:**
- Recherchiere in den vorhandenen Docs (`docs/`, `README.md`, `docs/reports/` — früher `reports/`, im Repo-Cleanup entfernt), was jeweils zum Setup/Skalieren/Deployment benötigt wird.
- Gib konkrete Prompts/Checklisten, die Fred oder ein Agent direkt ausführen kann.

**WICHTIG — Branching:**
- Alle neuen Arbeiten, Prompts und Skripte sollen auf den Branch **`codex-rewrite-chatgpt-continue`** gelegt werden.
- Falls bereits Inhalte dort liegen, integriere sie sinnvoll.

Gib den Plan als strukturierte Markdown-Tabelle/Liste zurück, mit:
1. Phase
2. Konkrete Tasks
3. Empfohlenes Model
4. Fallback-Model
5. Verpflichtende Skills
6. Test-Commitment
7. Risiko/Hinweis
8. Datenbedarf (falls zutreffend)
