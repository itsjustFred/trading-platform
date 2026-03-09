# Cross-Asset Momentum

## Quelle
- Original: Python-Strategie im Repository, inspiriert durch `desenyon/CrossAssetMomentumAlpha`
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry, wenn der gewichtete Zeitreihen-Momentum-Score positiv ist und nach Volatilitaet weiterhin positiv bleibt.
Signal: `Composite_Momentum > 0` und `Vol_Adjusted_Momentum > 0`.
Der Composite-Score nutzt Lookbacks `[21, 63, 126, 252]` mit Gewichten `[0.4, 0.3, 0.2, 0.1]`.

### Exit
Vollausstieg bei einem von drei Events:
1. Stop-Loss-Hit auf dem ATR-Schutzstop.
2. Momentum-Bruch: `Composite_Momentum < -0.01`.
3. Drawdown-Schutz: `Rolling_Drawdown < -0.08`.

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
| Momentum Lookbacks | Bars | 21, 63, 126, 252 |
| Momentum Weights | Weights | 0.4, 0.3, 0.2, 0.1 |
| Volatility Window | Period | 60 |
| Drawdown Exit | Threshold | -8 % |
| ATR Stop | Period | 14 |
| ATR Stop | Multiplier | 2.0 |

## Risk-Management
- Stop-Distanz: ATR-basiert
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Hinweis zur Vereinfachung
Das Original adressiert mehrere Asset-Klassen parallel. In der vorliegenden Python-Fassung wird die Time-Series-Momentum-Logik pro Lauf auf ein einzelnes Instrument angewendet.

## Framework-Empfehlung
vectorbt — gewichtete Momentum- und Drawdown-Filter sind rein serienbasiert und passen direkt in den Vektor-Workflow.
