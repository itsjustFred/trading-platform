# Signal Bots

`signal_bots/` enthält wiederverwendbare Signal-Processing-Strategien, die externe
Raw-Signale (z. B. Discord) in `risk.models.TradeSignal` transformieren.

## Processor-Contract

Jeder Processor implementiert:

```python
process_signal(raw_signal: dict, market_data: pd.DataFrame) -> TradeSignal
```

- `raw_signal`: symbol/action/timestamp + optionale Zusatzdaten.
- `market_data`: OHLC-Kontext zur SL/TP-Berechnung (z. B. ATR).
- Rückgabe: normalisiertes `TradeSignal` inkl. `metadata`.

## Registry

Die zentrale Registry liegt in `signal_bots/registry.py`.

- `ramjet`
- `rocketfuel_longsonly`
- `rocketfuel_longandshort`
- `nitro`

`get_signal_processor(name, config)` liefert die passende Instanz.

## Risk-Engine Integration

1. Signal-Processor berechnet `entry_price`, `stop_loss`, `take_profit`.
2. Risk-Engine (`risk.engine.RiskEngine`) sized auf Basis von:
   - explizitem `stop_loss` (bevorzugt), oder
   - Fallback (`atr` aus `metadata`, sonst pip-basiert).
3. Ergebnis (`position_size`, `risk_amount`, `risk_percent`) wird im Replay und
   im Live-Orderflow verwendet.

### Signal ohne SL

Falls ein Processor keinen SL setzt, nutzt die Risk-Engine den Fallback über
`default_sl_atr_multiplier` (mit `metadata["atr"]`, falls vorhanden).

## Trailing-SL im Live-Trading

- Processor-spezifische Trailing-Regeln werden in
  `signal.metadata["trailing_sl_config"]` übergeben.
- Initiales Sizing erfolgt immer auf dem initialen SL.
- Spätere Trailing-Updates sind ein Broker-Order-Update-Problem:
  bestehende Schutzorders werden nach Regel-Trigger angepasst (z. B. SL nachziehen,
  breakeven setzen, exit-only auf Signalwechsel).

