# Risk Parity

## Quelle
- Original: Python-Strategie im Repository, inspiriert durch `desenyon/RiskParityAlpha`
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry auf dem Single-Symbol-Proxy nur in ruhigen Marktphasen und ueber dem Trendfilter.
Signal: annualisierte `Rolling_Vol(60)` liegt unter `Vol_Median(252) * 1.5` und `close > SMA(100)`.
Neue Entries werden nur auf Rebalance-Bars (`rebalance_period = 21`) zugelassen.

### Exit
Vollausstieg bei einem von drei Events:
1. Stop-Loss-Hit auf dem ATR-Schutzstop.
2. Regimebruch: Volatilitaet steigt ueber die Calm-Regime-Schwelle.
3. Trendbruch: `close < SMA(100)`.

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
| Rolling Volatility | Period | 60 |
| Volatility Median | Period | 252 |
| Calm-Regime Multiplier | Threshold | 1.5 |
| Trendfilter | SMA | 100 |
| Rebalance | Bars | 21 |
| ATR Stop | Period | 14 |
| ATR Stop | Multiplier | 2.0 |

## Risk-Management
- Stop-Distanz: ATR-basiert
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Hinweis zur Vereinfachung
Das Original ist ein Multi-Asset-Risk-Parity-Modell. Diese Python-Version validiert die implementierte Single-Symbol-Proxy-Logik, nicht eine echte Portfolio-Gewichtung ueber mehrere Beine.

## Framework-Empfehlung
vectorbt — die Strategie ist ein regelbasierter Zeitreihenfilter ohne echte Multi-Asset-Rebalancing-Engine im aktuellen Codepfad.
