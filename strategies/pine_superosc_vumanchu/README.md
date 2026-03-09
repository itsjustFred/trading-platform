# SuperOSC + VuManChu

## Quelle
- Original: Python-Strategie im Repository, manuelle Pine-Script-Konvertierung aus `indikatoren-repo`
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry, wenn der SuperTrend-Oszillator bullish ist, WaveTrend 1 WaveTrend 2 von unten nach oben kreuzt und WaveTrend 2 im Oversold-Bereich liegt.
Signal: `STO_Osc > 0`, `WT1 > WT2`, `WT1[-1] <= WT2[-1]` und `WT2 <= -60`.

### Exit
Vollausstieg, sobald waehrend einer offenen Position eines der beiden Ziele intrabar erreicht wird:
1. Stop-Loss: `low <= stop_loss`
2. Take-Profit: `high >= take_profit`
Wenn auf derselben Bar beides erreicht wird, behandelt die Implementierung den Exit konservativ als Stop-Loss.

### Stop-Loss
ATR-basierter Initial-Stop beim Entry.
Formel: `risk = ATR(13) * 1.5`, `sl_price = entry_close - risk`.

### Trailing Stop (falls vorhanden)
Kein Trailing Stop implementiert.

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| SuperTrend Oscillator | Length | 10 |
| SuperTrend Oscillator | Multiplier | 8.0 |
| WaveTrend | Channel Length | 9 |
| WaveTrend | Average Length | 12 |
| WaveTrend | Moving Average Length | 3 |
| ATR Stop | Period | 13 |
| ATR Stop | Multiplier | 1.5 |
| Take Profit | R-Multiple | 2.0 |

## Risk-Management
- Stop-Distanz: ATR-basiert
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — obwohl Pine-inspiriert, liegt die Strategie bereits als voll vektorisierte Python-Implementierung vor.
