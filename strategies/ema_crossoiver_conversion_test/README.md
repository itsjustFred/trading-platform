# ema crossoiver conversion test

## Quelle
- Original: Pine Script v5 (`Impulse Cross`)
- Konvertiert: 2025-01-20

## Kernlogik

### Entry
EMA(9) cross über EMA(21) (`ta.crossover(fast, slow)`):
- `fast_ema[0] > slow_ema[0]` UND `fast_ema[1] <= slow_ema[1]`
- Entry-Preis: Close der aktuellen Bar

### Exit
EMA(9) cross unter EMA(21) (`ta.crossunder(fast, slow)`):
- `fast_ema[0] < slow_ema[0]` UND `fast_ema[1] >= slow_ema[1]`
- Alternativ: Stop-Loss Trigger (siehe unten)

### Stop-Loss
ATR(14) * 2.0 unter Entry-Preis (hinzugefügt für Risk-Sizing):
- Formel: `sl_price = close - (atr(14) * 2.0)`
- Notwendig für konsistentes 1%-Risiko-Management, da Original-Strategie keinen expliziten SL definiert

### Trailing Stop
Nicht vorhanden.

### Partial Exits
Nicht vorhanden.

## Indikator-Einstellungen

| Indikator | Parameter | Wert |
|-----------|-----------|------|
| Fast EMA | Länge | 9 |
| Slow EMA | Länge | 21 |
| ATR | Periode | 14 |
| ATR Multiplikator | Faktor | 2.0 |

## Risk-Management
- Stop-Distanz: ATR-basiert (2x ATR(14))
- Position Sizing: 1% Risiko pro Trade (`--risk-pct 0.01`)
- Keine Teil-Ausstiege, keine Trailing Stops

## Framework-Empfehlung
vectorbt
