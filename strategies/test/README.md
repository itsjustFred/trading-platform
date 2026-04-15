# Test

## Quelle
TradingView Pine Script v5, Strategie "Impulse Cross"

## Kernlogik
Trendfolgende Strategie basierend auf dem Crossing zweier Exponential Moving Averages (EMA). Generiert Kaufsignale beim Überschreiten des kurzfristigen über den langfristigen Durchschnitt (Bullish Momentum) und schließt Positionen beim Unterschreiten (Bearish Reversal).

### Entry
**Bedingung:** Crossover von EMA(9) über EMA(21)
```pine
longCondition = ta.crossover(fast, slow)
if longCondition
    strategy.entry("Long", strategy.long)
```
**Ausführung:** Market Order Long bei Bestätigung der Kreuzung im aktuellen Candle.

### Exit
**Bedingung:** Crossunder von EMA(9) unter EMA(21)
```pine
exitCondition = ta.crossunder(fast, slow)
if exitCondition
    strategy.close("Long")
```
**Ausführung:** Schließung der kompletten Long-Position bei Inverted Cross.

### Stop-Loss
Nicht implementiert. Strategie nutzt ausschließlich den inversen Crossunder als Exit-Trigger ohne definiertes Preis-Level-Risk.

## Indikator-Einstellungen

| Indikator | Parameter | Typ |
|-----------|-----------|-----|
| Fast EMA | Länge: 9 | Exponential Moving Average auf Close |
| Slow EMA | Länge: 21 | Exponential Moving Average auf Close |

**Berechnungsmethode:** Standard EMA-Formel mit Gewichtung auf aktuellere Preise.

## Risk-Management

**Position Sizing:** Nicht definiert (Standard: 100% Equity oder contract(1) je nach Kontext)
**Max Drawdown Protection:** Nicht vorhanden
**Daily/Weekly Loss Limits:** Nicht implementiert
**Leverage:** Nicht spezifiziert

## Framework-Empfehlung
**vectorbt**
