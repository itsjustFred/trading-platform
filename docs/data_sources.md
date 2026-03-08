# Datenquellen-Mapping

> Stand: 2026-02-26

---

## Übersicht

| Datenart | Quelle | Priorität |
|----------|--------|-----------|
| Daily Stock Data | Dolthub (`post-no-preference/stocks`) | Primär |
| Intraday Stock Data (1m–1h) | HuggingFace (`mito0o852/OHLCV-1m`) | Primär |
| Forex Minute/Tick | Histdata | Primär |
| Forex Live | cTrader | Für Live-Trading |
| Stock Live | Alpaca / yfinance | Alpaca für Trading, yfinance für schnelle Daten |
| Options | Dolthub (`post-no-preference/options`) | Bei Bedarf |
| Earnings/Fundamentals | Dolthub (`post-no-preference/earnings`) | Bei Bedarf |

---

## Details & Hinweise

### HuggingFace — Intraday Stock Data

- **Dataset:** `mito0o852/OHLCV-1m`
- **Format:** Parquet, nach Monat partitioniert (`ohlcv_YYYY-MM.parquet`)
- **Zeitraum:** US-Equities, 1-Minuten OHLCV
- **Download:** Einzelne Monate können direkt heruntergeladen werden:
  ```python
  from huggingface_hub import hf_hub_download

  path = hf_hub_download(
      repo_id="mito0o852/OHLCV-1m",
      filename="ohlcv_2024-01.parquet",
      repo_type="dataset"
  )
  ```
- **Downloader:** `data_tools/downloaders/huggingface_dl.py`

### Dolthub — Historische Daten

- **Repos:**
  - `post-no-preference/stocks` — Daily OHLCV für US-Equities
  - `post-no-preference/options` — Optionsdaten (Calls/Puts, Greeks)
  - `post-no-preference/earnings` — Earnings, Fundamentals
- **Zugang:** Benötigt `dolt` CLI oder SQL-Export
  ```bash
  # Repo klonen
  dolt clone post-no-preference/stocks
  cd stocks

  # SQL-Query
  dolt sql -q "SELECT * FROM stocks WHERE symbol = 'AAPL' LIMIT 10"

  # CSV-Export
  dolt table export stocks stocks.csv
  ```
- **Downloader:** `data_tools/downloaders/dolthub_dl.py`

### Histdata — Forex Minute/Tick

- **URL:** https://www.histdata.com/
- **Format:** CSV (manueller Download, dann Konvertierung zu Parquet)
- **Verfügbare Paare:** Alle Majors (EURUSD, GBPUSD, USDJPY, etc.) + XAUUSD
- **Kosten:** Kostenlos, aber manueller Download nötig (kein API)
- **Zeitraum:** Bis zu 20+ Jahre Tick-Daten
- **Downloader:** `data_tools/downloaders/histdata_dl.py`

### cTrader — Forex Live/Tick

- **Verwendung:** Live-Trading und Tick-Daten-Export
- **Format:** CSV (via ML_tick_data_prepare Tool)
- **Hinweis:** Bestehender Code aus `ML_tick_data_prepare` portiert
- **Downloader:** `data_tools/downloaders/ctrader_dl.py`

### Alpaca / yfinance — Stock Live

- **Alpaca:** Für Live-Trading (API-Key nötig, Paper + Live Trading)
- **yfinance:** Für schnelle historische Daten (kostenlos, kein API-Key)
  - Begrenzt auf ~2 Jahre Intraday (5m), unbegrenzt Daily
  - Aktuell 97 Parquet-Dateien aus yfinance in `~/data/ohlcv/`

---

## Downloader-Architektur

Alle Downloader implementieren das `Downloader` ABC aus `data_tools/downloaders/base.py`:

```
data_tools/downloaders/
├── base.py              # Downloader ABC + DownloadResult + SourceMeta
├── histdata_dl.py       # Forex Minute/Tick
├── dolthub_dl.py        # Dolt-Repos (Stocks, Options, Earnings)
├── huggingface_dl.py    # HF Datasets API
└── ctrader_dl.py        # cTrader Tick-Daten
```

Output-Pfad-Konvention:
```
~/data/ohlcv/{source}/{asset_class}/{symbol}/{timeframe}.parquet
```
