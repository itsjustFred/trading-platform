# Iteration EURUSD H1 Validation

## Ziel
Backtest der Intraday-Volatility-Breakout-Strategie fuer EURUSD/H1 mit der Platform abgleichen gegen MT5-XLSX-Report.

## XLSX-Referenz (analysiert)
- **Symbol**: EURUSD
- **Timeframe**: H1
- **Zeitraum**: 2018-01-04 bis 2026-05-28
- **Trades**: 492
- **Initiales Kapital**: 100000 EUR
- **Hebel**: 1:100
- **Settings** (aus XLSX):
  - ATR_Period=5, ATR_Multiplier=0.25, Stop_ATR_Multiplier=0.25
  - Use_Candle_Close=true, Confirmation_Timeframe=15
  - Regime_Filter_Mode=0 (low), Trade_Direction=0 (both)
  - Fixed_Risk_Amount=500
  - Entry_Time=16:30, Max_Entry_Time=18:05, Exit_Time=22:55
- **Total PnL**: 21868.81 EUR
- **Profit Factor**: 1.1976
- **Win Rate**: 45.12%

## Durchgefuehrte Schritte

### 1. Strategie-Bugfix: Index-Alignment bei Daily-ATR
In `strategies/intraday_volatility_breakout/intraday_volatility_breakout.py` war ein Bug in der `temp_df`-Erstellung:
```python
# VORHER (falsch):
temp_df = pd.DataFrame(
    {"open": open_, "high": df["high"], "low": df["low"], "close": close},
    index=dt_idx,
)
# Dabei haben open_/high/low/close einen RangeIndex (0,1,2...) und dt_idx ist DatetimeIndex.
# pandas aligniert bei DataFrame-Erstellung nach Index -> ALLE WERTE WERDEN NaN!

# NACHHER (korrigiert):
temp_df = pd.DataFrame(
    {"open": open_.values, "high": df["high"].values, "low": df["low"].values, "close": close.values},
    index=dt_idx,
)
```
Dies fuehrte dazu, dass `daily_atr` komplett NaN war und damit auch `upper_level`/`lower_level`.

### 2. erster Backtest mit Standard-Parametern
Kommando:
```bash
DATA_DIR=/mnt/block-volume-data python -m runner.cli run \
  --engine vectorbt \
  --strategy strategies/intraday_volatility_breakout/intraday_volatility_breakout.py \
  --symbols EURUSD --timeframe 1h \
  --start 2018-01-01 --end 2026-06-21 \
  --capital 100000 --risk-pct 0.005 \
  --output data/backtests/intraday_volatility_breakout_eurusd_h1.json \
  --data-dir /mnt/block-volume-data
```
Ergebnis: **0 Trades**.

### 3. Ursachenanalyse fuer 0 Trades
Root Cause: `Use_Candle_Close=true` auf H1-Daten.
- Die Strategie nimmt den vorherigen Close des Confirmation-Timeframes (M15) als Signal-Preis.
- Da nur H1-Daten vorliegen, wird via `resample("15min")` von H1 auf M15 uebersampelt.
- Der shifted M15-Close auf H1-Basis entspricht dem **vorherigen H1-Close**.
- Der Entry-Window ist 16:30-18:05. Auf H1-Daten sind nur die Bars **17:00** und **18:00** im Entry-Window (16:00 ist minute_of_day=960 < 990).
- Der Signal-Preis fuer 17:00 ist der 16:00-Close. Bis 17:00 hat sich der Kurs aber in der Regel bereits zurueck in die Range bewegt.
- Ergebnis: Kein einziger Close bestaetigt einen Breakout waehrend des Entry-Windows.

Bestaetigung:
- `use_candle_close=False` (Immediate-Price-Modus) liefert **125 Trades**.
- `use_candle_close=False` + `entry_time="16:00"` liefert **227 Trades**.
- High/Low-Breakouts im Fenster 16:00-19:00: 129 long + 140 short = 269 moegliche Intrabar-Breakouts.

### 4. Vergleich mit Validation-CLI
- BacktestResult wurde in `ValidationReference`-Format konvertiert (da CLI das erwartet).
- Vergleich ausgefuehrt: `python -m validation.cli -r ...EURUSD_H1.xlsx -p ...eurusd_h1.json -f markdown`
- Ergebnis: **Exit Code 1** (failed), da 492 Trades missing, 0 matched.

### 5. Offene Punkte / naechste Iteration
1. **Timeframe/Parameter-Anpassung**: `use_candle_close=True` auf H1 ist mit vektorisiertem Backtest nicht sinnvoll umsetzbar. Optionen:
   a) `use_candle_close=False` verwenden (unterschiedliche Logik)
   b) `entry_time="16:00"` setzen, um die 16:00-H1-Bar einzubeziehen
   c) Strategie so anpassen, dass sie auf einem gefuellten M15-DataFrame laeuft (nur fuer Signal-Generierung)
   d) Backtest auf M15-Daten laufen lassen (Divergenz zum XLSX-Report)

2. **Zeitzonen-Frage**: MT5-Report von Dukascopy ist typischerweise in EET/EEST (UTC+2/+3). Die Trade-Zeiten im XLSX (z.B. 16:45) koennten EET sein = 14:45 UTC. Die Platform arbeitet mit UTC-Daten. Falls EET korrekt ist, muesste der Entry-Window auf UTC umgerechnet werden (14:30-16:05 UTC statt 16:30-18:05 UTC).

3. **Lot-Sizing/Fixed Risk**: MQL5 nutzt `Fixed_Risk_Amount=500` EUR. Der Vectorbt-Adapter verwendet `risk_per_trade_pct` (Prozentsatz). Mit 100k Kapital und 0.5% Risiko ergibt sich ca. 500 USD/EUR pro Trade, aber das variiert mit dem Equity. Anpassung noetig, entweder durch Adapter-Erweiterung oder custom Sizing-Logik.

4. **Intrabar-Execution**: MT5 Strategy Tester arbeitet tick-basiert innerhalb der H1-Bars. Die Platform fuehrt auf der naechsten Bar-Open aus (1-Hour-Lag). Intrabar-Modellierung via `--intrabar-resolution` hilft nur fuer Stops/TPs, nicht fuer Entries.

## Dateien
- `data/backtests/EURUSD_H1_reference.json` - XLSZ-Import als ValidationReference
- `data/backtests/intraday_volatility_breakout_eurusd_h1.json` - Platform-Backtest (0 Trades)
- `data/backtests/intraday_volatility_breakout_eurusd_h1_report.md` - Validation-Report
- `strategies/intraday_volatility_breakout/intraday_volatility_breakout.py` - Bugfix angewendet
