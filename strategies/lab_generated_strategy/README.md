# lab_generated_strategy

## Quelle
TradingView Pine Script v5 (Overlay-Strategie)

## Kernlogik

### Entry
**Long Entry** bei bullish Crossover: Die schnelle EMA (9 Perioden) kreuzt die langsame EMA (21 Perioden) nach oben (`ta.crossover`).

### Exit
**Exit** bei bearish Crossunder: Die schnelle EMA (9 Perioden) kreuzt die langsame EMA (21 Perioden) nach unten (`ta.crossunder`). Die Position wird vollständig geschlossen (`strategy.close`).

### Stop-Loss
Kein expliziter Stop-Loss definiert. Die Strategie nutzt ausschließlich das inverse Crossover-Signal als Ausstieg (Trendumkehr-Exit).

## Indikator-Einstellungen

| Indikator | Periode | Berechnung |
|-----------|---------|------------|
| Fast EMA | 9 | `ta.ema(close, 9)` |
| Slow EMA | 21 | `ta.ema(close, 21)` |

**Input-Parameter:**
- `fastLength`: 9 (Integer)
- `slowLength`: 21 (Integer)
- Preisbasis: `close`

## Risk-Management
Kein statisches Risk-Management implementiert. Keine Positionsgrößenkalkulation, keine festen Stop-Loss-Distanzen und kein Take-Profit. Reiner Trend-Following-Ansatz mit Signal-Flip als einziger Ausstiegsmechanismus.

## Framework-Empfehlung
vectorbt
