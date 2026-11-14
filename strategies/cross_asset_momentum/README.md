# Cross-Asset Momentum

## Quelle
- Original: `desenyon/quantconnect-strategies` — `cross_asset_momentum.py` (multi-asset)
- Konvertiert: 2026-03-08, korrigiert: 2026-09-09 (Gewichte auf Quelle zurueckgesetzt, erfundene Stops/Exits default-OFF)

## Kernlogik

### Entry
Long-Entry, wenn der gewichtete Zeitreihen-Momentum-Score positiv ist und nach Volatilitaet weiterhin positiv bleibt.
Signal: `Composite_Momentum > 0` und `Vol_Adjusted_Momentum > 0`.
Der Composite-Score nutzt Lookbacks `[21, 63, 126, 252]` mit Gewichten `[0.35, 0.30, 0.20, 0.15]` (exakt wie `momentum_weights` in der Quelle).

### Exit
Standard (Quellverhalten, long-only): Vollausstieg, sobald das Entry-Signal nicht mehr gilt (`signal_break`, d. h. Composite-Score oder Vol-adjustiertes Momentum nicht mehr positiv — entspricht dem "positive signals only"-Filter der Quelle).
Opt-in Exits (default OFF, keine Quelllogik — muessen explizit per Parameter aktiviert werden):
1. `use_atr_stop=true`: Stop-Loss-Hit auf dem ATR-Schutzstop.
2. `use_momentum_exit=true`: Momentum-Bruch `Composite_Momentum < -0.01`.
3. `use_drawdown_exit=true`: Drawdown-Schutz `Rolling_Drawdown < -0.08`.

**Fill-Konvention (Version 1.2.0):** Marktausstiege auf Close-Basis (Signal-, Momentum- und Drawdown-Exit) tragen kein `exit_price` mehr (kein Close-Fill) und fuellen am naechsten Bar-Open (`signal_exit_fill="next_open"`). Der ATR-Stop bleibt ein explizites Level (`exit_price`).

### Stop-Loss
Kein Stop-Loss im Standard (die Quelle nutzt keinen ATR-Stop).
Opt-in via `use_atr_stop=true`: `sl_price = entry_close - ATR_Wilder(14) * 2.0`.
Position Sizing ohne Stop faellt auf die Engine-Fallbacks zurueck (ATR-Distanz bzw. 2-%-Default).

### Trailing Stop (falls vorhanden)
Kein Trailing Stop implementiert (die Quelle hat keinen).

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| Momentum Lookbacks | Bars | 21, 63, 126, 252 |
| Momentum Weights | Weights | 0.35, 0.30, 0.20, 0.15 |
| Volatility Window | Period | 60 |
| ATR (Info/Sizing) | Wilder Period | 14 |
| ATR Stop (Opt-in) | Multiplier | 2.0 |
| Momentum-Bruch (Opt-in) | Threshold | -0.01 |
| Drawdown-Exit (Opt-in) | Threshold | -8 % |

## Risk-Management
- Stop-Distanz: keine im Standard (Opt-in ATR)
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` (Faellt ohne `sl_price` auf ATR-Distanz / 2-%-Default zurueck)
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Hinweis zur Vereinfachung (Scope)
Das Original ist ein MULTI-ASSET-System (Aktien, Anleihen, Rohstoffe, Real Assets, FX) mit Cross-Sectional-Ranking, invers-volatilitaetsgewichteter Risk-Parity-Allokation (15-%-Asset-Cap, 40-%-Klassen-Cap), 12-%-Volatilitaets-Targeting und SPY-basiertem Crash-Schutz (defensives TLT/IEF/GLD/TIP-Portfolio). Davon ist nichts single-symbol-exprimierbar und nichts davon ist implementiert: pro Lauf wird die Time-Series-Momentum-Logik auf ein einzelnes Instrument angewendet (long-only). Das woechentliche Rebalancing ist durch taegliche Signal-Bars approximiert.

## Framework-Empfehlung
vectorbt — gewichtete Momentum-Filter sind rein serienbasiert und passen direkt in den Vektor-Workflow.
