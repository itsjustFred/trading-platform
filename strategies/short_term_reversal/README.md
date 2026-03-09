# Short Term Reversal

## Quelle
- Original: Python-Strategie im Repository, inspiriert durch `desenyon/short_term_reversal_strategy`
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry, wenn die 5-Tages-Rendite stark negativ ist und RSI im Oversold-Bereich liegt.
Signal: `Return_5D < -5%` und `RSI(14) < 35`.

### Exit
Vollausstieg bei einem von drei Events:
1. Stop-Loss-Hit auf dem ATR-Stop.
2. Return-Reversion: `Return_5D > +3%`.
3. Time-Exit nach 10 Bars.

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
| Return | Lookback | 5 Bars |
| Entry Threshold | Return_5D | -0.05 |
| Exit Threshold | Return_5D | 0.03 |
| RSI | Period | 14 |
| RSI Entry | Threshold | 35 |
| Max Hold | Bars | 10 |
| ATR Stop | Period | 14 |
| ATR Stop | Multiplier | 2.0 |

## Risk-Management
- Stop-Distanz: ATR-basiert
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — die Strategie besteht aus Return-, RSI- und ATR-Serien ohne komplexe Orderverwaltung.
