# Gold Momentum

## Quelle
- Original: Python-Strategie im Repository, inspiriert durch `zenomtrader-discord`
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry, wenn MACD bullish kreuzt, RSI ueber dem Momentum-Filter liegt und der Schlusskurs oberhalb der Bollinger-Mitte notiert.
Signal: `MACD_Line > MACD_Signal`, `MACD_Line[-1] <= MACD_Signal[-1]`, `RSI(14) > 50` und `close > BB_Mid(20)`.

### Exit
Vollausstieg bei einem von drei Events:
1. Stop-Loss-Hit auf dem ATR-Stop.
2. Bearisher MACD-Cross.
3. Momentum-Verlust: `RSI(14) < 40`.

### Stop-Loss
Retrofit fuer den Agent-Standard: ATR-basierter Schutzstop beim Entry.
Formel: `sl_price = entry_close - ATR(14) * 2.0`.

### Trailing Stop (falls vorhanden)
Kein Trailing Stop implementiert.

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| MACD | Fast EMA | 12 |
| MACD | Slow EMA | 26 |
| MACD | Signal | 9 |
| RSI | Period | 14 |
| RSI Entry | Threshold | 50 |
| RSI Exit | Threshold | 40 |
| Bollinger Mid | Period | 20 |
| ATR Stop | Period | 14 |
| ATR Stop | Multiplier | 2.0 |

## Risk-Management
- Stop-Distanz: ATR-basiert
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — MACD-, RSI-, Bollinger- und ATR-Berechnungen sind rein vektorisierbar und benoetigen keine Pine- oder LEAN-spezifischen Features.
