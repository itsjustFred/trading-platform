# Trend Following

## Quelle
- Original: Python-Strategie im Repository, inspiriert durch `desenyon/trend_following_cta`
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry, wenn die schnelle SMA die mittlere SMA von unten nach oben kreuzt und die mittlere SMA oberhalb der langsamen SMA liegt.
Signal: `SMA(20) > SMA(50)` und `SMA(20)[-1] <= SMA(50)[-1]` sowie `SMA(50) > SMA(200)`.

### Exit
Vollausstieg bei einem von zwei Events:
1. `SMA(20)` kreuzt die `SMA(50)` von oben nach unten.
2. Der Schlusskurs faellt unter den aktiven ATR-Trailing-Stop.

### Stop-Loss
Initialer Stop-Loss beim Entry: `sl_price = entry_close - ATR(14) * 2.0`.

### Trailing Stop (falls vorhanden)
Ja. Solange die Position offen ist, wird der Stop auf `max(previous_stop, close - ATR(14) * 2.0)` angehoben.
Beim Exit wird das Stop-Level in `trailing_stop_at_exit` geschrieben.

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| SMA Fast | Length | 20 |
| SMA Mid | Length | 50 |
| SMA Slow | Length | 200 |
| ATR | Period | 14 |
| ATR Stop | Multiplier | 2.0 |

## Risk-Management
- Stop-Distanz: ATR-basiert
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — die Strategie ist ein klassisches vektorisiertes SMA/ATR-System ohne Event-Driven-Orderlogik.
