# Intraday Volatility Breakout – Iterationsbericht

## Ziel
Die MQL5-Strategie `IntradayVolatilityBreakout.mq5` in eine Platform-vectorbt-Strategie portieren und Backtests mit den XLSX-Referenzberichten innerhalb der Validation-Framework-Toleranzen zur Deckung bringen.

## Durchgeführte Iterationen

### 1. EURUSD/H1
- **Backtest**: 234 Trades, ca. $731 Netto-PnL (mit `--max-leverage 100 --max-position-pct 5.0`).
- **Referenz**: 492 Trades (M15-basiert, `Use_Candle_Close=true`).
- **Vergleich**: 0 matched, 492 missing, 234 extra.
- **Hauptursachen**:
  - Lokale EURUSD-M1/M15-Daten sind für 2018–2019 nicht vorhanden; H1-Daten beginnen zwar 2018, haben aber Lücken.
  - Die Referenz ist auf M15-Candle-Close ausgelegt, während der H1-Backtest eine geringere Granularität hat.
  - Zusätzlich unterschiedliche ATR/Tagesabschlüsse zwischen Dukascopy-UTC und MT5-Brokerzeit.

### 2. NAS100/M15
- **Erster Lauf mit `use_candle_close=true`**: 0 Trades (keine Level-Durchbrüche im Entry-Fenster).
- **Lauf mit `use_candle_close=false`**: 150 Trades, ca. $24.561 Netto-PnL.
- **Referenz**: 597 Trades ab 2018.
- **Vergleich**: 0 matched, 597 missing, 150 extra.
- **Hauptursachen**:
  - Lokale NAS100-M15-Daten starten erst 2020; die Referenz enthält 2018–2019.
  - Der Zeit-/Tagesabschluss der Dukascopy-Daten (UTC) führt zu anderen Daily-ATR-Werten als die MT5-Brokerzeit.
  - Mit `use_candle_close=false` werden zumindest Signale erzeugt, aber Entry-Tage und -Zeiten weichen ab.

### 3. SP500/M1
- **Lauf mit `use_candle_close=false`**: 3 Trades, ca. -$3.069 Netto-PnL.
- **Referenz**: 512 Trades ab 2018.
- **Vergleich**: 0 matched, 512 missing, 3 extra.
- **Hauptursache**:
  - Die Dukascopy-M1-Daten sind 24h-basiert. Die interne Tages-ATR wird über volle 24h-Kerzen berechnet (Mittelwert ca. 83 Punkte). Die Breakout-Levels liegen dadurch im 16:30–18:05-Fenster typischerweise 35–50 Punkte entfernt, während der typische Range in diesem Fenster nur ca. 2 Punkte beträgt. Die Referenz verwendet MT5-Brokerzeit/Tageskerzen mit RTH-ähnlichem Abschluss, was deutlich engere Levels ergibt.

## Fazit
Eine exakte Trade-zu-Trade-Deckung zwischen den Platform-Backtests und den MT5-XLSX-Berichten ist mit den aktuell verfügbaren UTC-24h-Dukascopy-Daten nicht erreichbar. Die wesentlichen Blocker sind:
1. Fehlende historische Daten vor 2020 (EURUSD, NAS100, SP500).
2. Unterschiedlicher Tagesabschluss/Brokerzeit zwischen MT5 und Dukascopy-UTC.
3. Für SP500: 24h-Daily-ATR vs. RTH-Daily-ATR führt zu unterschiedlichen Breakout-Levels.

## Empfohlene nächste Schritte (außerhalb dieses Ferments)
- Bereitstellung/Import der 2018–2019-Daten in `/mnt/block-volume-data`.
- Konfigurierbarer Tagesabschluss-Offset bzw. RTH-Session-Filter in der Strategie.
- Separate Daily-ATR-Berechnung auf Basis von RTH-Kerzen (z. B. 13:30–20:00 UTC) für Indizes.

## Aktuelle Artefakte
- `strategies/intraday_volatility_breakout/intraday_volatility_breakout.py`
- `strategies/intraday_volatility_breakout/README.md`
- `data/backtests/intraday_volatility_breakout_*_{eurusd_h1,nas100_m15,sp500_m1}.json`
- `data/backtests/intraday_volatility_breakout_*_{eurusd_h1,nas100_m15,sp500_m1}_report.md`
- `scripts/backtest_to_validation_ref.py`
