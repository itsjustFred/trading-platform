# Signal Bots

## Processor-Contract

Jeder Processor implementiert:
```python
process_signal(raw_signal: dict, market_data: pd.DataFrame) -> TradeSignal
```

- `raw_signal`: symbol/action/timestamp + optionale Zusatzdaten
- `market_data`: OHLC-Kontext zur SL/TP-Berechnung (z. B. ATR)
- Rückgabe: normalisiertes `TradeSignal` inkl. `metadata`

## Registry

Zentrale Registry in `signal_bots/registry.py`:

| ID | Klasse | Status |
|----|--------|--------|
| `stockkit` | RamJetSignalProcessor | ✅ Verfügbar |
| `ramjet` | RamJetSignalProcessor | ✅ Verfügbar |
| `rocketfuel_longsonly` | RocketFuelSignalProcessor | ✅ Verfügbar |
| `rocketfuel_longandshort` | RocketFuelSignalProcessor | ✅ Verfügbar |
| `nitro` | NitroSignalProcessor | ✅ Verfügbar (RamJet-Formeln, BUY-only) |

## Bots im Detail

### Nitro
- **Status:** Aktiv, registriert
- **Features:** RamJet-Formeln, BUY-only Signalzusammenführung
- **Last-Run:** Nicht im Scheduler als Job konfiguriert

### RamJet
- **Status:** Aktiv, registriert
- **Features:** ATR + Supertrend BUY-only Setup mit TP1 und trailing metadata
- **Last-Run:** Nicht im Scheduler als Job konfiguriert

### RocketFuel (Long-only & Long+Short)
- **Status:** Aktiv, registriert
- **Features:** Komplexere Entry-Logik, Varianten für Long-only vs Long+Short
- **Last-Run:** Nicht im Scheduler als Job konfiguriert

## Health-Check Status

**Stand 2026-06-17:**
- Alle 3 Bots (`nitro`, `ramjet`, `rocketfuel`) sind registriert und importierbar
- Keine laufenden Scheduler-Jobs gefunden (`scheduler/jobs/` enthält nur `weekly_data_refresh.py`)
- Kein Health-Check-Job implementiert

## Risk-Engine Integration

1. Signal-Processor berechnet `entry_price`, `stop_loss`, `take_profit`
2. Risk-Engine (`risk.engine.RiskEngine`) sized auf Basis von:
   - explizitem `stop_loss` (bevorzugt), oder
   - Fallback (`atr` aus `metadata`, sonst pip-basiert)
3. Ergebnis (`position_size`, `risk_amount`, `risk_percent`) wird im Replay und im Live-Orderflow verwendet

## Empfehlung

Die Bots sind **nicht aktiv im Scheduler**. Optionen:
1. Health-Check-Job erstellen in `scheduler/jobs/signal_bots_health.py`
2. Oder als "available but not scheduled" dokumentieren