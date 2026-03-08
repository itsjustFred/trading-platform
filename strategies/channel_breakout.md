# Channel Breakout

## Quelle
- Original: Python-Strategie im Repository, inspiriert durch `zenomtrader-discord`
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry, wenn der Schlusskurs ueber den vorherigen oberen N-Bar-Channel ausbricht und optional oberhalb der EMA(200) liegt.
Signal: `close > upper_channel[-1]` und (falls aktiviert) `close > EMA(200)`.
Standardparameter: Channel 5 Bars, EMA-Filter aktiv.

### Exit
Vollausstieg, wenn der Schlusskurs den aktiven ATR-Trailing-Stop erreicht oder unterschreitet.
Signal: `close <= trailing_stop_value`.

### Stop-Loss
Initialer Stop-Loss beim Entry: `sl_price = entry_close - ATR(14) * 2.0`.

### Trailing Stop (falls vorhanden)
Ja. Solange die Position offen ist, wird der Stop auf `max(previous_stop, close - ATR(14) * 2.0)` nachgezogen.
Das Exit-Level wird in `trailing_stop_at_exit` dokumentiert.

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| Channel | Length | 5 |
| EMA Filter | Length | 200 |
| EMA Filter | Enabled | true |
| ATR | Period | 14 |
| ATR Trailing Stop | Multiplier | 2.0 |

## Risk-Management
- Stop-Distanz: ATR-basiert
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — Channel-, EMA- und ATR-Berechnung ist voll vektorisierbar und benoetigt keine Pine- oder LEAN-spezifische Laufzeit.
