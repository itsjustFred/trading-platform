# Phase 3 — Fortschritt

> Gestartet: 2026-02-25
> Letztes Update: 2026-02-26 (Session 4)

---

## Übersicht

| Bereich | Status | Tasks erledigt |
|---------|--------|---------------|
| Strategie-Triage | ✅ Fertig | 2/2 |
| Strategie-Konvertierung | ✅ Fertig | 16 konvertiert (10 vectorbt + 2 PyneCore + 4 neu) |
| Daten-Infrastruktur | ✅ Fertig | 7/7 (Tasks 3-9) |
| Daten herunterladen | ✅ Teilweise | 25 ETF/VIX-Datasets via yfinance |
| Data-Directory Migration | ✅ Fertig | ~/data/ → /mnt/block-volume-data/ |
| Datenquellen-Mapping | ✅ Fertig | docs/data_sources.md erstellt |
| Strategie-Meta Format | ✅ Fertig | StrategyMeta + STRATEGY_META in 16 Dateien |
| Backtest-Meta Format | ✅ Fertig | BacktestMeta erweitert, 8 JSONs migriert |
| XAUUSD Timestamp-Fix | ✅ Fertig | 1h + 15m Parquet-Dateien korrigiert |
| PyneCore Evaluation | ✅ Fertig | Positiv — Engine-Adapter gebaut |
| PyneCore Adapter | ✅ Fertig | runner/engines/pynecore_adapter.py |
| Neue Strategien | ✅ Fertig | 3 weitere konvertiert (Channel Breakout, Stat Arb, ORB) |
| Viewer-Bug Fix | ✅ Fertig | list_backtests() auf neues BacktestMeta Format umgestellt |
| Strategie-Assessment | ✅ Fertig | 11/12 konvertiert, 1 blockiert (Fundamental-Daten) |

---

## 1. Strategie-Triage

### Gescannte Quellen

| Ordner | Strategien | Format |
|--------|-----------|--------|
| `discord-zenomtrader/` | 5 | NT8 (C#), PineScript v5, MQL5 |
| `github-alexisbouley2/` | 3 | QuantConnect (Python) |
| `github-desenyon/` | 33 | QuantConnect (Python) |
| **Gesamt** | **41** | |

### Triage-Ergebnis

| Kategorie | Anzahl | Details |
|-----------|--------|---------|
| **Vielversprechend** | 12 | Klare Logik, umsetzbar, getestete Konzepte |
| **Vielleicht** | 12 | Interessant aber unklar oder aufwendig |
| **Müll** | 17 | Kein Edge, zu komplex, oder Curve-Fitted |

Vollständige Liste: `~/Desktop/server_planning/strategy_triage.md`

---

## 2. Strategie-Konvertierung (Top 5)

### Konvertierte Strategien

| # | Strategie | Datei | Original-Quelle |
|---|-----------|-------|------------------|
| 1 | Short-Term Reversal | `strategies/short_term_reversal.py` | desenyon — ShortTermReversalAlphaModel |
| 2 | Volatility Breakout | `strategies/volatility_breakout.py` | desenyon — VolatilityBreakoutAlpha |
| 3 | Mean Reversion RSI+BB | `strategies/mean_reversion_rsi_bb.py` | alexisbouley2 — Stratégie RSI+BB |
| 4 | Gold Momentum | `strategies/gold_momentum.py` | zenomtrader — MACD+RSI+BB Gold |
| 5 | Trend Following | `strategies/trend_following.py` | desenyon — Multi-SMA CTA Trend |

Alle konvertiert ins vectorbt-Signal-Format (`generate_signals()` → entries/exits Spalten).

### Backtest-Ergebnisse

| Strategie | Trades | PnL | Win Rate | Profit Factor | Zeitraum |
|-----------|--------|-----|----------|---------------|----------|
| Short-Term Reversal | 141 | $307K | 71.6% | 2.76 | 2023-03 → 2026-02 |
| Volatility Breakout | 1145 | $132K | 41.1% | 1.24 | 2023-03 → 2026-02 |
| Mean Reversion RSI+BB | 269 | $28.7K | 60.6% | 1.14 | 2023-03 → 2026-02 |
| Trend Following | 40 | $76.8K | 45.0% | 2.10 | 2023-03 → 2026-02 |
| Gold Momentum | 589 | $31K | — | — | 2020-11 → 2026-02 (XAUUSD, fixed) |

**Hinweise:**
- PnL-Zahlen inflated wegen All-In Position Sizing (vereinfachtes Phase 1 Modell)
- Gold Momentum jetzt testbar nach XAUUSD Timestamp-Fix
- Backtest-Ergebnisse in `~/data/backtests/`

---

## 3. Daten-Infrastruktur

### Erledigte Tasks

| # | Task | Status | Datei |
|---|------|--------|-------|
| 3 | Downloader-Interface | ✅ | `data_tools/downloaders/base.py` |
| 4 | Histdata-Downloader | ✅ | `data_tools/downloaders/histdata_dl.py` |
| 5 | Dolthub-Downloader | ✅ | `data_tools/downloaders/dolthub_dl.py` |
| 6 | HuggingFace-Downloader | ✅ | `data_tools/downloaders/huggingface_dl.py` |
| 7 | cTrader-Downloader | ✅ | `data_tools/downloaders/ctrader_dl.py` |
| 8 | Data-Validierung | ✅ | `data_tools/validate.py` |
| 9 | Catalog-Erweiterung | ✅ | `data_tools/catalog_builder.py` |

### Downloader-Architektur

Alle Downloader implementieren `Downloader` ABC mit:
- `download(symbol, timeframe, start, end, output_dir)` → `DownloadResult`
- `list_available()` → `list[str]`
- `get_source_meta()` → `SourceMeta`

Output-Pfad-Konvention: `{output_dir}/ohlcv/{source}/{asset_class}/{symbol}/{timeframe}.parquet`

**Hinweis:** Downloaders noch nicht live getestet (kein Internet im Buildprozess). API-Logik implementiert, aber Fehlerfälle und Edge-Cases müssen beim ersten echten Download validiert werden.

### Data-Validierung

97 Dateien validiert:
- ✅ OK: 0
- ⚠️ Warning: 95 (hauptsächlich Gap-Detection — normale Trading-Stunden-Lücken)
- ❌ Error: 2 (XAUUSD 15m + 1h — korrupte Timestamps)

Report gespeichert: `~/data/validation_report.json`

### Catalog-Erweiterung

`catalog_builder.py` erweitert um:
- **`storage`** Feld: `"local"` | `"cached"` | `"remote"` (erkennt automatisch Cache-Verzeichnis)
- **`source_meta`** Feld: Wird aus `~/data/source_meta.json` gelesen (von Downloadern geschrieben)
- **`validation`** Feld: Optional via `--validate` Flag, führt alle 6 Checks durch
- Neuer CLI-Parameter: `python -m data_tools.catalog_builder --validate`

---

## 4. Offene Punkte / Probleme

### Bekannte Probleme

| Problem | Beschreibung | Lösung |
|---------|-------------|--------|
| ~~XAUUSD Timestamps~~ | ~~Dukascopy Parquet-Dateien haben Timestamps als Epoch-Sekunden gespeichert~~ | ✅ **Gelöst** — Timestamps korrekt als datetime konvertiert (epoch-ms) |
| ~~Viewer-Bug~~ | ~~list_backtests() nutzte alte Feldnamen (strategy, symbols statt strategies, data.symbols)~~ | ✅ **Gelöst** — viewer/app.py Zeile 183-184 gefixt |
| Gap-Warnings | Alle 95 yfinance-Dateien haben Gaps — das sind normale Trading-Stunden-Lücken (Nacht, Wochenende) | Gap-Detection verfeinern: US-Market-Hours als Filter hinzufügen |
| Downloader ungetestet | Kein Internet-Zugang während Implementierung | Beim ersten echten Download testen und Fehler fixen |
| PyneCore v5-Limit | Pine v4/v5 Strategien können nicht kompiliert werden (nur v6) | Manuell nach PyneCore-Python übersetzen |
| Fehlende Daten | 5 Strategien blockiert: Sector Rotation (#4), Post VIX (#8), Risk Parity (#10), Cross-Asset (#11), Multi-Factor (#12) | Daten downloaden: ETFs, VIX, Fundamentals |

### Nächste Schritte (Priorität 1 abschließen)

- [x] XAUUSD Timestamp-Fix → Gold Momentum Backtest nachgeholt (589 Trades, $31K PnL)
- [ ] Gap-Detection verfeinern (Market-Hours berücksichtigen)
- [ ] Downloader live testen (Internet nötig)
- [ ] Weitere Strategien aus "Vielleicht"-Kategorie konvertieren

### Offene Prioritäten

| Priorität | Status |
|-----------|--------|
| P2: PyneCore Adapter | ✅ **Fertig** — Evaluation positiv, Adapter gebaut, 2 Strategien getestet |
| P3: LRU-Cache | ⏳ Noch nicht begonnen |
| P4: Alerts + Auto-Restart | ⏳ Noch nicht begonnen |
| P5: Risk-Management | ⏳ Noch nicht begonnen |
| P6: Docker | ⏳ Noch nicht begonnen |
| P7: ML & Advanced | ⏳ Noch nicht begonnen |

---

## 9. XAUUSD Timestamp-Fix (2026-02-26)

### Problem
XAUUSD Dukascopy-Parquet-Dateien hatten korrupte Timestamps. Die Epoch-Millisekunden-Werte (z.B. `1604876400000`) wurden direkt als datetime gespeichert, was zu 1970er Daten führte.

### Lösung
1. Original-CSVs aus `~/Desktop/custom_lean_integration/data/` geladen
2. Timestamps korrekt geparst: `pd.to_datetime(df['timestamp'], unit='ms', utc=True)`
3. Neue Parquet-Dateien geschrieben nach `~/data/ohlcv/dukascopy/forex/XAUUSD/`

### Ergebnis
| Timeframe | Rows | Zeitraum | Status |
|-----------|------|----------|--------|
| 1h | 31.017 | 2020-11-08 → 2026-02-06 | ✅ Validiert |
| 15m | 123.793 | 2020-11-08 → 2026-02-06 | ✅ Validiert |

Gold Momentum Backtest: 589 Trades, $31K PnL ✅

---

## 10. PyneCore Evaluation + Adapter (2026-02-26)

### Evaluation

- **PyneCore Version:** 6.3.5 (pynesys-pynecore, Installation von GitHub)
- **Ergebnis:** ✅ Positiv — funktioniert als Engine-Adapter
- **API-Key:** Nicht nötig für .py Strategien (nur für Pine-Kompilierung)
- **Pine-Kompilierung:** Nur v6 (unsere Strategien sind v5 → manuelle Konvertierung)
- **Performance:** 5.078 Bars in 0.4s (~12.700 Bars/Sekunde)
- **70+ ta.* Funktionen:** sma, ema, rsi, macd, bb, atr, stoch, etc.
- **Vollständiges strategy.*:** entry, exit, close, position sizing, etc.
- **Fehlend:** `request.security()` (Multi-Timeframe)

Dokumentation: `~/Desktop/trading-platform/docs/pynecore_evaluation.md`

### Engine-Adapter

- **Datei:** `runner/engines/pynecore_adapter.py`
- **Pipeline:** Parquet → CSV (temp) → `pyne run` → Trade CSV → Universal Format
- **Registriert in:** `runner/cli.py` als engine `pynecore`
- **Getestet:** SMA Crossover (87 Trades, $33K) + ORB (139 Trades, $20K)

### Verfügbare Engines

```
$ python -m runner.cli engines
  vectorbt     Python vectorized backtesting (simple signal-based strategies)
  lean         QuantConnect LEAN (complex event-driven strategies)
  pynecore     PyneCore Pine-style Python strategies (indicator-rich)
```

---

## 11. Neue Strategien konvertiert (2026-02-26)

### Konvertierte Strategien

| # | Strategie | Datei | Quelle | Engine | Format |
|---|-----------|-------|--------|--------|--------|
| 7 | Intraday Channel Breakout | `strategies/channel_breakout.py` | NT8 C# (zenomtrader) | vectorbt | N-Bar High/Low + EMA + ATR Trailing |
| 9 | Statistical Arbitrage | `strategies/stat_arb_pairs.py` | QC Python (desenyon) | vectorbt | Z-Score Mean Reversion |
| — | SMA Crossover (PyneCore) | `strategies/pynecore_sma_crossover.py` | internal | pynecore | SMA 10/30 Crossover |
| 13 | Opening Range Breakout | `strategies/pynecore_orb.py` | NT8 C# (zenomtrader) | pynecore | SMA Breakout + EMA + ATR TP/SL |

### Backtest-Ergebnisse (neue Strategien)

| Strategie | Engine | Symbole | Trades | PnL | Zeitraum |
|-----------|--------|---------|--------|-----|----------|
| Channel Breakout | vectorbt | AAPL, MSFT | 199 | $26.2K | 2023-2026 |
| Stat Arb Pairs | vectorbt | AAPL, MSFT, JPM | 111 | $55.5K | 2023-2026 |
| SMA Crossover (Pyne) | pynecore | AAPL | 87 | $33.3K | 2023-2026 |
| ORB (Pyne) | pynecore | AAPL | 139 | $19.9K | 2023-2026 |

### Nicht konvertiert (Begründung)

| # | Strategie | Grund |
|---|-----------|-------|
| 4 | Sector Rotation | Braucht 11 Sektor-ETF-Daten (XLE, XLF, XLK, etc.) — nicht vorhanden |
| 8 | Post VIX Dip Buy | Pine v5, braucht `request.security()` (Multi-TF), braucht VIX-Daten |
| 10 | Risk Parity | Braucht 11 Multi-Asset-Daten (ETFs, Bonds, Commodities) — nicht vorhanden |
| 11 | Cross-Asset Momentum | Braucht 27 ETF-Daten — nicht vorhanden |
| 12 | Multi-Factor Equity | Braucht 100+ Aktien + Fundamental-Daten — nicht vorhanden |

**Hinweis:** #2 (Trend Following CTA) ist als `trend_following.py` adaptiert (Aktien statt 13 ETFs). #3 (SPY Mean Reversion) ist als `mean_reversion_rsi_bb.py` adaptiert (RSI+BB auf Einzelaktien).

### Strategie-Übersicht (gesamt)

10 Strategien verfügbar in `strategies/`:

| Datei | Engine | Typ |
|-------|--------|-----|
| `example_sma.py` | vectorbt | trend-following |
| `short_term_reversal.py` | vectorbt | mean-reversion |
| `volatility_breakout.py` | vectorbt | breakout |
| `mean_reversion_rsi_bb.py` | vectorbt | mean-reversion |
| `gold_momentum.py` | vectorbt | momentum |
| `trend_following.py` | vectorbt | trend-following |
| `channel_breakout.py` | vectorbt | breakout |
| `stat_arb_pairs.py` | vectorbt | mean-reversion, pairs-trading |
| `pynecore_sma_crossover.py` | pynecore | trend-following |
| `pynecore_orb.py` | pynecore | breakout |

---

## Dateistruktur (neu erstellt)

```
~/Desktop/trading-platform/
├── strategies/
│   ├── example_sma.py              # existierte bereits
│   ├── short_term_reversal.py      # NEU
│   ├── volatility_breakout.py      # NEU
│   ├── mean_reversion_rsi_bb.py    # NEU
│   ├── gold_momentum.py            # NEU
│   ├── trend_following.py          # NEU
│   ├── channel_breakout.py         # NEU (NT8 C# → vectorbt)
│   ├── stat_arb_pairs.py           # NEU (QC Python → vectorbt)
│   ├── pynecore_sma_crossover.py   # NEU (PyneCore)
│   └── pynecore_orb.py             # NEU (NT8 C# → PyneCore)
├── runner/
│   ├── cli.py                      # ERWEITERT (pynecore Engine registriert)
│   └── engines/
│       ├── pynecore_adapter.py     # NEU (PyneCore Engine-Adapter)
│       └── ...
├── docs/
│   ├── data_sources.md             # NEU (Datenquellen-Mapping)
│   └── pynecore_evaluation.md      # NEU (PyneCore Evaluation)
├── data_tools/
│   ├── catalog_builder.py          # ERWEITERT (source_meta, validation, storage)
│   ├── validate.py                 # NEU
│   └── downloaders/
│       ├── __init__.py             # NEU
│       ├── base.py                 # NEU (Downloader ABC)
│       ├── histdata_dl.py          # NEU
│       ├── dolthub_dl.py           # NEU
│       ├── huggingface_dl.py       # NEU
│       └── ctrader_dl.py           # NEU
```

```
~/Desktop/server_planning/
├── strategy_triage.md              # NEU (41 Strategien kategorisiert)
└── 08_phase3_progress.md           # NEU (dieses Dokument)
```

```
~/data/ → /mnt/block-volume-data/     # SYMLINK (Migration 2026-02-26)
├── catalog.json                    # AKTUALISIERT (mit storage + validation Feldern)
├── validation_report.json          # NEU
└── backtests/
    ├── short_term_reversal_*.trades.json    # NEU, Format migriert
    ├── mean_reversion_rsi_bb_*.trades.json  # NEU, Format migriert
    ├── volatility_breakout_*.trades.json    # NEU, Format migriert
    └── trend_following_*.trades.json        # NEU, Format migriert
```

---

## 5. Data-Directory Migration (2026-02-26)

- **Quelle:** `~/data/` (~20 MB, 97 Parquet + Backtests + catalog.json)
- **Ziel:** `/mnt/block-volume-data/` (98 GB ext4, zweite Disk)
- **Methode:** `rsync -av`, Checksummen verifiziert, Symlink erstellt
- **Ergebnis:** `~/data/ → /mnt/block-volume-data/` (alle Tools funktionieren weiterhin)
- **Dokumentation:** `~/Desktop/server_planning/migration_guide.md`

## 6. Datenquellen-Mapping (2026-02-26)

- Erstellt: `~/Desktop/trading-platform/docs/data_sources.md`
- 7 Datenquellen dokumentiert (Dolthub, HuggingFace, Histdata, cTrader, Alpaca, yfinance, Options/Earnings)
- Hinweise zu HuggingFace-Partitionierung, Dolthub CLI, Histdata manuellem Download

## 7. Strategie-Meta Format (2026-02-26)

### StrategyMeta Dataclass

Neue Dataclass in `shared/models.py`:
- `name: str` — Strategiename
- `version: str` — Versionsnummer (default "1.0.0")
- `tags: dict` — Typ, Quelle, Link, Asset-Klasse, Komplexität

### STRATEGY_META in Strategien

Alle 6 Strategie-Dateien mit `STRATEGY_META` dict am Anfang aktualisiert:

| Datei | Name | Typ | Quelle |
|-------|------|-----|--------|
| `short_term_reversal.py` | Short Term Reversal | mean-reversion | desenyon-github |
| `volatility_breakout.py` | Volatility Breakout | breakout | zenomtrader-discord |
| `mean_reversion_rsi_bb.py` | Mean Reversion RSI+BB | mean-reversion | alexisbouley2-github |
| `trend_following.py` | Trend Following | momentum, trend-following | desenyon-github |
| `gold_momentum.py` | Gold Momentum | momentum | zenomtrader-discord |
| `example_sma.py` | SMA Crossover | trend-following | example |

## 8. Backtest-Meta Format (2026-02-26)

### BacktestMeta Änderungen (Breaking Change)

| Alt | Neu | Grund |
|-----|-----|-------|
| `strategy: str` | `strategies: list[str]` | Multi-Strategy Backtests |
| `timeframe_start/end` | `backtest_start/end` | Klarerer Name |
| `data_timeframe: str` | `data: dict` | Multi-TF, Quellen-Info |
| `symbols: list` | `data.symbols` | Alles unter `data` |
| — | `duration_days: int` | Zeitraum-Dauer |
| — | `total_bars: int` | Bar-Anzahl |
| — | `notes: str` | Freitext-Notizen |

### Migrierte Dateien

8 bestehende Backtest-JSONs auf neues Format umgeschrieben:
- `example_sma_*.trades.json`
- `mean_reversion_rsi_bb_*.trades.json`
- `short_term_reversal_*.trades.json`
- `sma_crossover_*.trades.json`
- `sma_fast_*.trades.json`
- `stockkit_ram_jet.trades.json`
- `trend_following_*.trades.json`
- `volatility_breakout_*.trades.json`

### Aktualisierte Code-Dateien

| Datei | Änderung |
|-------|----------|
| `shared/models.py` | StrategyMeta + BacktestMeta neues Format |
| `runner/engines/vectorbt_adapter.py` | STRATEGY_META lesen, neue Meta-Felder |
| `runner/converters/lean_converter.py` | Neues BacktestMeta Format |
| `runner/converters/stockkit_converter.py` | Neues BacktestMeta Format |
| `runner/converters/cli.py` | meta.strategies statt meta.strategy |
| `viewer/app.py` | Alle API-Endpunkte auf neues Format |
| `tests/test_models.py` | Tests für neues Format + StrategyMeta |
| `tests/test_stats.py` | BacktestMeta Konstruktor aktualisiert |

Alle 30 Tests bestehen. ✅

---

## Nächste Schritte

| Priorität | Task | Status |
|-----------|------|--------|
| P1 | Daten downloaden: Sektor-ETFs, VIX, SPY (für blockierte Strategien) | ⏳ Internet nötig |
| P1 | Gap-Detection verfeinern (Market-Hours) | ⏳ Offen |
| P1 | Downloader live testen (Internet nötig) | ⏳ Offen |
| P3 | LRU-Cache — Daten-Caching für große Datasets | ⏳ Noch nicht begonnen |
| P4 | Alerts + Auto-Restart | ⏳ Noch nicht begonnen |
| P5 | Risk-Management | ⏳ Noch nicht begonnen |
| P6 | Docker | ⏳ Noch nicht begonnen |
| P7 | ML & Advanced | ⏳ Noch nicht begonnen |

---

## 12. Viewer-Bug Fix (2026-02-26)

### Problem
`list_backtests()` in `viewer/app.py` las raw JSON mit alten Feldnamen:
- `meta.get("strategy")` → zeigte "unknown" statt Strategiename
- `meta.get("symbols")` → leere Liste statt Symbole aus `data.symbols`

### Lösung
2 Zeilen in `viewer/app.py` geändert:
- `meta.get("strategy")` → `(meta.get("strategies") or ["unknown"])[0]`
- `meta.get("symbols")` → `meta.get("data", {}).get("symbols", [])`

### Verifiziert
Alle Viewer-Endpoints getestet:
- `/api/backtests` — 15 Backtests, alle mit korrektem Strategienamen
- `/api/backtest/<id>/info` — Strategie, Symbole, Timeframe korrekt
- `/api/backtest/<id>/trades` — Trades laden korrekt
- `/api/backtest/<id>/equity` — Equity-Curve funktioniert
- `/api/backtest/<id>/stats` — Stats werden berechnet
- `/api/backtest/<id>/trade/<nr>` — OHLCV-Kontext wird geladen
- `/api/compare` — Vergleich funktioniert

---

## 13. Strategie-Assessment — Vielversprechend-Liste (2026-02-26)

### Vollständiger Status aller 12 "Vielversprechend" Strategien

| # | Name | Status | Datei / Grund |
|---|------|--------|---------------|
| 1 | Short-Term Reversal | ✅ | `short_term_reversal.py` |
| 2 | Trend Following CTA | ✅ Adaptiert | `trend_following.py` (Aktien statt 13 ETFs) |
| 3 | SPY Mean Reversion | ✅ Adaptiert | `mean_reversion_rsi_bb.py` (RSI+BB auf Einzelaktien) |
| 4 | Sector Rotation | ❌ BLOCKED | Braucht 11 Sektor-ETFs (XLE, XLF, XLK, etc.) |
| 5 | Gold Momentum | ✅ | `gold_momentum.py` |
| 6 | Volatility Breakout | ✅ | `volatility_breakout.py` |
| 7 | Channel Breakout | ✅ | `channel_breakout.py` |
| 8 | Post VIX Dip Buy | ❌ BLOCKED | VIX-Daten + request.security() (Multi-TF) nötig |
| 9 | Stat Arb Pairs | ✅ | `stat_arb_pairs.py` |
| 10 | Risk Parity | ❌ BLOCKED | 11 Multi-Asset-Daten (Bonds, Commodities) nötig |
| 11 | Cross-Asset Momentum | ❌ BLOCKED | 27 ETF-Daten nötig |
| 12 | Multi-Factor Equity | ❌ BLOCKED | 100+ Aktien + Fundamental-Daten nötig |

**Ergebnis:** 7/12 konvertiert (+ 2 Adaptionen), 5 blockiert durch fehlende Daten.

### Benötigte Daten für blockierte Strategien

| Strategie | Benötigte Daten | Download-Methode |
|-----------|----------------|------------------|
| Sector Rotation | XLE, XLF, XLK, XLV, XLI, XLB, XLY, XLP, XLC, XLRE, XLU | yfinance Daily |
| Post VIX Dip Buy | VIX (CBOE), SPY/SPX | yfinance Intraday + Daily |
| Risk Parity | TLT, IEF, GLD, DBC, SPY, EFA, EEM, VNQ, HYG, LQD, TIPS | yfinance Daily |
| Cross-Asset Momentum | 27 ETFs (Equities, Bonds, FX, Commodities) | yfinance Daily |
| Multi-Factor Equity | 100+ US Equities + Fundamental-Daten | Dolthub/yfinance + Earnings |

**Empfehlung:** Sektor-ETFs und VIX via yfinance downloaden → 2 weitere Strategien möglich.

---

## 14. ETF/VIX-Daten Download (2026-02-26, Session 4)

### Installiert
- `yfinance 1.2.0` in .venv installiert

### Heruntergeladene Daten (25 Datasets)

| Gruppe | Symbole | Timeframe | Rows/Symbol | Zeitraum |
|--------|---------|-----------|-------------|----------|
| Sektor-ETFs (11) | XLE, XLF, XLK, XLV, XLI, XLB, XLY, XLP, XLC, XLRE, XLU | 1d | ~1256 | 5 Jahre |
| VIX + SPY | ^VIX, SPY | 1d | ~1256 | 5 Jahre |
| VIX + SPY | ^VIX, SPY | 1h | 3491-7137 | 2 Jahre |
| Multi-Asset (10) | TLT, IEF, GLD, DBC, EFA, EEM, VNQ, HYG, LQD, TIPS | 1d | ~1256 | 5 Jahre |

### Speicherort
`~/data/ohlcv/yfinance/etf/US/{SYMBOL}/` und `~/data/ohlcv/yfinance/index/US/{SYMBOL}/`

### Validierung
- 122 Dateien validiert (97 bestehende + 25 neue)
- Alle ⚠️ Warning (Gap-Detection = normale Trading-Lücken)
- VIX: 100% zero-volume (normal für Index)
- 0 Errors

### Catalog
- `catalog.json` aktualisiert: 122 Datasets (vorher 97)

---

## 15. Blockierte Strategien konvertiert (2026-02-26, Session 4)

### Neu konvertierte Strategien

| # | Name | Datei | Trades | PnL | Symbole |
|---|------|-------|--------|-----|---------|
| 4 | Sector Rotation | `sector_rotation.py` | 323 | $50,677 | 11 Sektor-ETFs |
| 8 | Post VIX Dip Buy | `post_vix_dip_buy.py` | 18 | $22,681 | SPY (mit VIX-Filter) |
| 10 | Risk Parity | `risk_parity.py` | 47 | -$103,256 | 11 Multi-Asset-ETFs |
| 11 | Cross-Asset Momentum | `cross_asset_momentum.py` | 684 | $77,526 | 10 ETFs |

### Hinweise
- **Risk Parity negativ** — vereinfachte Implementierung (inverse Volatility statt echtes Risk Parity). Zeitraum enthält Bond-Crash 2022.
- **Post VIX Dip Buy** — wenige Trades (18), aber hohe Win Rate (72.2%). Seltene Setups.
- **Sector Rotation** — Momentum-basiert pro ETF (nicht cross-sectional). Funktioniert als Trendfolge-Filter.
- **Cross-Asset Momentum** — Time-Series Momentum über 10 ETFs. Gute Diversifikation.
- #12 Multi-Factor Equity — weiterhin blockiert (braucht Fundamental-Daten)

### Aktualisierter Status aller 12 Vielversprechend-Strategien

| # | Name | Status | Datei |
|---|------|--------|-------|
| 1 | Short-Term Reversal | ✅ | `short_term_reversal.py` |
| 2 | Trend Following CTA | ✅ | `trend_following.py` |
| 3 | SPY Mean Reversion | ✅ | `mean_reversion_rsi_bb.py` |
| 4 | Sector Rotation | ✅ NEU | `sector_rotation.py` |
| 5 | Gold Momentum | ✅ | `gold_momentum.py` |
| 6 | Volatility Breakout | ✅ | `volatility_breakout.py` |
| 7 | Channel Breakout | ✅ | `channel_breakout.py` |
| 8 | Post VIX Dip Buy | ✅ NEU | `post_vix_dip_buy.py` |
| 9 | Stat Arb Pairs | ✅ | `stat_arb_pairs.py` |
| 10 | Risk Parity | ✅ NEU | `risk_parity.py` |
| 11 | Cross-Asset Momentum | ✅ NEU | `cross_asset_momentum.py` |
| 12 | Multi-Factor Equity | ❌ BLOCKED | Braucht Fundamental-Daten |

**Ergebnis:** 11/12 konvertiert, nur Multi-Factor noch blockiert.

---

## 16. Indikatoren-Repo Scan (2026-02-26, Session 4)

### Repository
- Geklont: `git clone https://github.com/itsjustFred/Indikatoren ~/Desktop/Indikatoren`

### Scan-Ergebnis

**Strategien (10 Dateien mit strategy.entry/exit):**

| Name | Pine Version | Komplexität | Konvertiert? |
|------|-------------|-------------|--------------|
| Supertrend DEMA | v5 | Einfach (119 Zeilen) | ✅ → `pine_supertrend_dema.py` |
| SuperOSC + VuManChu | v5 | Mittel (203 Zeilen) | ✅ → `pine_superosc_vumanchu.py` |
| Vix Fix + Filter (3 Varianten) | v6 | Komplex (265-392 Zeilen) | ❌ request.security() |
| Swings + SnDr + Stoch | v5 | Komplex | ❌ Swing-basiert, schwer zu vectorisieren |
| ASTS MTF + Entries | v5 | Komplex | ❌ Multi-TF + komplexe State-Maschine |
| MQ5 Swings EA | v5 | Komplex | ❌ Swing-Struktur + Pyramiding |
| ASTS Strategy V2 | v5 | Komplex | ❌ HTF Rebuy/Resell, untested |
| AI-generierte Strategien (2) | Text/Prompts | — | ❌ Prompt-Texte, kein Code |

**Reine Indikatoren (16 Dateien):**
- Swing-Indikatoren (10): Custom Swings, SBS, SFP, Consolidation, Fib, etc.
- Buy/Sell Indikatoren (2): v5
- DOB + Sweeps (2): v5
- Trend Indicator: v6
- ASTS MTF Scaling: v5
- Vix Spike + ASTS + RSM: v5

### Konvertierte Pine-Strategien

| Strategie | Trades | PnL | Symbole | Timeframe |
|-----------|--------|-----|---------|-----------|
| Supertrend + DEMA | 2 | $36,928 | AAPL, NVDA, AMZN | 1d |
| SuperOSC + VuManChu | 27 | $168,976 | AAPL, NVDA, AMZN, JPM | 1d |

**Hinweis:** Supertrend DEMA hat wenige Trades wegen 200-bar Warmup bei Daily-Daten (~4 Jahre). SuperOSC + VuManChu hat extreme -60 Oversold-Filter → nur bei starken Paniken.

---

## 17. DoltHub Downloader Live-Test (2026-02-26, Session 4)

### Ergebnis: Teilweise funktional

**Was funktioniert:**
- DoltHub REST API (`https://www.dolthub.com/api/v1alpha1`) erreichbar ✅
- Repo `post-no-preference/stocks` existiert und hat Branch `master`
- Tabellen: `ohlcv`, `dividend`, `split`, `symbol`
- Schema: date, act_symbol, open(7,2), high(7,2), low(7,2), close(7,2), volume(bigint)
- Kleine Queries (LIMIT 5, enge Datums-Range) funktionieren ✅

**Probleme:**
- Größere Queries (>10 Rows) → **504 Gateway Timeout** (häufig)
- DoltHub Free API hat Timeout-Limits für große Tabellen
- ORDER BY auf voller Tabelle → Timeout

### Fixes am Downloader
- Repo-Default geändert: `dolthub/us-stock-market-daily` → `post-no-preference/stocks`
- Branch-Default: `main` → `master` (konfigurierbar)
- Tabelle-Default: `dolt_history` → `ohlcv`
- Retry-Logik hinzugefügt (3 Versuche mit exponential backoff)
- Error-Handling für `query_execution_status: "Error"` Responses

### Empfehlung
Für größere Downloads besser `dolt clone` (CLI) verwenden statt REST API.
Die REST API eignet sich für kleine Abfragen und Validierung.

---

## 18. Gesamtübersicht Strategien (Stand Session 4)

### Alle 16 Strategien

| # | Name | Typ | Engine | Datei |
|---|------|-----|--------|-------|
| 1 | SMA Crossover | Trend | vectorbt | `example_sma.py` |
| 2 | Short-Term Reversal | Mean-Rev | vectorbt | `short_term_reversal.py` |
| 3 | Volatility Breakout | Breakout | vectorbt | `volatility_breakout.py` |
| 4 | Mean Reversion RSI+BB | Mean-Rev | vectorbt | `mean_reversion_rsi_bb.py` |
| 5 | Gold Momentum | Momentum | vectorbt | `gold_momentum.py` |
| 6 | Trend Following | Trend | vectorbt | `trend_following.py` |
| 7 | Channel Breakout | Breakout | vectorbt | `channel_breakout.py` |
| 8 | Stat Arb Pairs | Arb | vectorbt | `stat_arb_pairs.py` |
| 9 | PyneCore SMA Crossover | Trend | pynecore | `pynecore_sma_crossover.py` |
| 10 | PyneCore ORB | Breakout | pynecore | `pynecore_orb.py` |
| 11 | Sector Rotation | Momentum | vectorbt | `sector_rotation.py` |
| 12 | Post VIX Dip Buy | Mean-Rev | vectorbt | `post_vix_dip_buy.py` |
| 13 | Risk Parity | Portfolio | vectorbt | `risk_parity.py` |
| 14 | Cross-Asset Momentum | Momentum | vectorbt | `cross_asset_momentum.py` |
| 15 | Supertrend + DEMA | Trend | vectorbt | `pine_supertrend_dema.py` |
| 16 | SuperOSC + VuManChu | Momentum | vectorbt | `pine_superosc_vumanchu.py` |

### Daten-Übersicht
- **122 Datasets** in catalog.json
- **111 Symbole** (86 US-Equities + 23 ETFs + VIX + SPY)
- **Quellen:** yfinance (Equities + ETFs), Dukascopy (XAUUSD)
- **Timeframes:** 1d, 1h, 15m, 5m

---

## Nächste Schritte

| Priorität | Task | Status |
|-----------|------|--------|
| P1 | Gap-Detection verfeinern (Market-Hours) | ⏳ Offen |
| P1 | Downloader live testen (Histdata, HuggingFace, cTrader) | ⏳ Offen |
| P3 | LRU-Cache — Daten-Caching für große Datasets | ⏳ Noch nicht begonnen |
| P4 | Alerts + Auto-Restart | ⏳ Noch nicht begonnen |
| P5 | Risk-Management | ⏳ Noch nicht begonnen |
| P6 | Docker | ⏳ Noch nicht begonnen |
| P7 | ML & Advanced | ⏳ Noch nicht begonnen |
