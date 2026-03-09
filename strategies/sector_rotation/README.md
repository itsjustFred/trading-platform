# Sector Rotation

## Quelle
- Original: Python-Strategie im Repository, inspiriert durch `desenyon/SectorMomentumAlpha`
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry nur dann, wenn das Single-Symbol-Proxy gleichzeitig einen positiven mittel- und kurzfristigen Momentum-Regimefilter erfuellt.
Signal: `Momentum(63) > 0`, `Momentum(21) > 0` und `close > SMA(200)`.

### Exit
Vollausstieg bei einem von drei Events:
1. Stop-Loss-Hit auf dem ATR-Schutzstop.
2. Regimebruch: `Momentum(63) < 0`.
3. Kurzfristige Schwaeche: `close.pct_change(10) < -3 %`.

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
| Momentum | Lookback | 63 |
| Fast Momentum | Lookback | 21 |
| Exit-Weakness | Lookback | 10 |
| Exit-Weakness | Threshold | -3 % |
| Trendfilter | SMA | 200 |
| ATR Stop | Period | 14 |
| ATR Stop | Multiplier | 2.0 |

## Risk-Management
- Stop-Distanz: ATR-basiert
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Hinweis zur Vereinfachung
Das Original ist ein Portfolio-/Rotation-Ansatz ueber mehrere Sektor-ETFs. In dieser Python-Umsetzung wird dieselbe Logik als Single-Symbol-Momentum-Proxy validiert, weil der Adapter pro Lauf nur ein Instrument verarbeitet.

## Framework-Empfehlung
vectorbt — die Strategie besteht aus deterministischen Zeitreihenfiltern ohne Multi-Leg-Orderrouting.
