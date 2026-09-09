# Gold Momentum

## Quelle
- Original: `desenyon/quantconnect-strategies` — `gold_momentum_strategy.py` (GLD + TLT, continuous-score mit Vol-Targeting und Regime-Multiplikatoren)
- Konvertiert: 2026-03-08, korrigiert: 2026-09-09 (Wilder-RSI/ATR, kontinuierlicher Score gemaess Quelle, erfundener ATR-Stop default-OFF, Provenienz korrigiert)

## Kernlogik

### Entry
Long-Entry, sobald der kontinuierliche Momentum-Score positiv wird.
Signal: `Momentum_Score > 0`.
Der Score summiert exakt wie `CalculateMomentumScore` in der Quelle: EMA(12/26)-Trend ±0.25, MACD-vs-Signal ±0.20, MACD-Histogramm +0.10, kurz+mittelfristiges Momentum ±0.25, RSI-Baender (+0.10 / +0.15 / −0.10), Bollinger-Position (+0.05 / +0.10); geclippt auf [−1, 1].

### Exit
Standard (Quellverhalten): Vollausstieg, sobald der Score nicht mehr positiv ist (`score_reversal`, d. h. `Momentum_Score <= 0` — entspricht dem kontinuierlichen Rebalancing der Quelle in Richtung Target-Weight).
Opt-in (default OFF, keine Quelllogik): `use_atr_stop=true` fuer Stop-Loss-Hit auf dem ATR-Schutzstop.

### Stop-Loss
Kein Stop-Loss im Standard (die Quelle nutzt keinen ATR-Stop).
Opt-in via `use_atr_stop=true`: `sl_price = entry_close - ATR_Wilder(14) * 2.0`.

### Trailing Stop (falls vorhanden)
Kein Trailing Stop implementiert (die Quelle hat keinen).

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| MACD | Fast / Slow / Signal (EMA) | 12 / 26 / 9 |
| RSI | Wilder Period | 14 |
| ATR (Info/Sizing) | Wilder Period | 14 |
| ATR Stop (Opt-in) | Multiplier | 2.0 |
| Bollinger | Period / Mult | 20 / 2.0 |
| Momentum kurz / mittel | Bars | 20 / 60 |
| Score Entry / Exit | Threshold | 0.0 / 0.0 |

## Risk-Management
- Stop-Distanz: keine im Standard (Opt-in ATR)
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` (Faellt ohne `sl_price` auf ATR-Distanz / 2-%-Default zurueck)
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Hinweis zur Vereinfachung (Scope)
Das Original handelt ZWEI Beine (GLD + TLT als Risk-off-Ausgleich), skaliert Positionen auf 15 % Volatilitaets-Target und multipliziert je nach Regime (bullish_low_vol 1.2, bullish_high_vol 0.8, bearish_low_vol 0.6, bearish_high_vol 0.4, neutral 0.7) bei max. 40 % Positionsgroesse. Davon ist nichts single-symbol-exprimierbar und nichts davon ist implementiert: gehandelt wird nur das GLD-Bein long-only, Sizing liegt bei der Engine. Die stundenbasierten Rolling-Windows der Quelle sind in Bars des Lauf-Timeframes ausgedrueckt.

## Framework-Empfehlung
vectorbt — Score-, MACD-, RSI-, Bollinger- und ATR-Berechnungen sind rein vektorisierbar und benoetigen keine LEAN-spezifischen Features.
