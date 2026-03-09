# Mean Reversion RSI+BB

## Quelle
- Original: Python-Strategie im Repository, inspiriert durch `alexisbouley2/spy_mean_reversion_strategy`
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry, wenn der Schlusskurs unter das untere Bollinger Band faellt und RSI gleichzeitig im Oversold-Bereich liegt.
Signal: `close < BB_Lower(20, 2.0)` und `RSI(14) < 30`.

### Exit
Vollausstieg bei einem von drei Events:
1. Stop-Loss-Hit auf dem ATR-Stop.
2. RSI-Recovery: `RSI(14) > 70`.
3. Mean-Reversion zur Mitte: `close > BB_Mid(20)`.
4. Time-Exit nach 15 Bars.

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
| Bollinger Bands | Period | 20 |
| Bollinger Bands | StdDev | 2.0 |
| RSI | Period | 14 |
| RSI Entry | Threshold | 30 |
| RSI Exit | Threshold | 70 |
| Max Hold | Bars | 15 |
| ATR Stop | Period | 14 |
| ATR Stop | Multiplier | 2.0 |

## Risk-Management
- Stop-Distanz: ATR-basiert
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — RSI-, Bollinger- und ATR-Logik ist rein vektorisierbar und benoetigt keine event-getriebene Engine.
