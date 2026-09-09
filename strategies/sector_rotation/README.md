# Sector Rotation

## Quelle
- Original: `desenyon/quantconnect-strategies` — `sector_rotation_strategy.py` (11-Sektoren-Portfolio mit Regime-Tabellen)
- Konvertiert: 2026-03-08, korrigiert: 2026-09-09 (erfundener ATR-Stop sowie Mom63/10d-Exits default-OFF, ATR auf Wilder)

## Kernlogik

### Entry
Long-Entry nur dann, wenn das Single-Symbol-Proxy gleichzeitig einen positiven mittel- und kurzfristigen Momentum-Regimefilter erfuellt.
Signal: `Momentum(63) > 0`, `Momentum(21) > 0` und `close > SMA(200)`.

### Exit
Standard (quellnah): Vollausstieg bei Trendbruch `close < SMA(200)` (`trend_break` — Trend-Score-Bein des Quellenfilters).
Opt-in Exits (default OFF, keine Quelllogik — muessen explizit per Parameter aktiviert werden):
1. `use_atr_stop=true`: Stop-Loss-Hit auf dem ATR-Schutzstop.
2. `use_momentum_exit=true`: Regimebruch `Momentum(63) < 0`.
3. `use_weakness_exit=true`: Kurzfristige Schwaeche `close.pct_change(10) < -3 %`.

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
| Momentum | Lookback | 63 |
| Fast Momentum | Lookback | 21 |
| Trendfilter | SMA | 200 |
| Exit-Weakness (Opt-in) | Lookback / Threshold | 10 / -3 % |
| ATR (Info/Sizing) | Wilder Period | 14 |
| ATR Stop (Opt-in) | Multiplier | 2.0 |

## Risk-Management
- Stop-Distanz: keine im Standard (Opt-in ATR)
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` (Faellt ohne `sl_price` auf ATR-Distanz / 2-%-Default zurueck)
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Hinweis zur Vereinfachung (Scope)
Das Original ist ein Portfolio-/Rotation-Ansatz ueber 11 Sektor-ETFs mit Relative-Strength-Ranking gegen SPY, Konjunktur-Regimetabellen (early/late_expansion, peak, contraction, trough), ranggewichteten Allokationsanpassungen (1.2/1.0/0.8/0.5) und defensivem Fallback-Korb (XLP/XLV/XLU) bei monatlichem Rebalancing. Davon ist nichts single-symbol-exprimierbar und nichts davon ist implementiert: pro Lauf wird dieselbe Entry-Logik als Single-Symbol-Momentum-Proxy validiert (long-only), mit taeglichen Signal-Bars statt monatlichem Rebalancing.

## Framework-Empfehlung
vectorbt — die Strategie besteht aus deterministischen Zeitreihenfiltern ohne Multi-Leg-Orderrouting.
