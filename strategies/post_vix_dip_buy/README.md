# Post VIX Dip Buy

## Quelle
- Original: Python-Strategie im Repository, inspiriert durch `zenomtrader-discord`
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry, wenn VIX oberhalb des Regime-Schwellenwerts notiert, RSI oversold ist und SPY unter seiner SMA handelt.
Signal: `VIX > 20`, `RSI(14) < 35` und `close < SMA(20)`.
Die VIX-Reihe wird aus lokalem `VIX/1d.parquet` auf die SPY-Bars gemappt.

### Exit
Vollausstieg bei einem von vier Events:
1. Stop-Loss-Hit auf dem ATR-Stop.
2. RSI-Recovery: `RSI(14) > 60`.
3. Mean-Reversion zur SMA: `close > SMA(20)`.
4. Time-Exit nach 20 Bars.

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
| VIX Filter | Threshold | 20 |
| RSI | Period | 14 |
| RSI Entry | Threshold | 35 |
| RSI Exit | Threshold | 60 |
| SMA | Period | 20 |
| Max Hold | Bars | 20 |
| ATR Stop | Period | 14 |
| ATR Stop | Multiplier | 2.0 |

## Risk-Management
- Stop-Distanz: ATR-basiert
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — die Strategie ist datengetrieben und serielastig; die externe VIX-Reihe laesst sich ohne event-getriebene Orders direkt in einem vektorisierten Workflow verwenden.
