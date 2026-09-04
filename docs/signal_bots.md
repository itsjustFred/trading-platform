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

| ID | Klasse | Config | Status |
|----|--------|--------|--------|
| `ramjet` | RamJetSignalProcessor | `signal_bots/ramjet/config.yaml` | ✅ Verfügbar |
| `rocketfuel_longsonly` | RocketFuelSignalProcessor | `signal_bots/rocketfuel/config.yaml` | ✅ Verfügbar |
| `rocketfuel_longandshort` | RocketFuelSignalProcessor | `signal_bots/rocketfuel/config.yaml` | ✅ Verfügbar |
| `nitro` | NitroSignalProcessor | `signal_bots/nitro/config.yaml` | ✅ Verfügbar (RamJet-Formeln, BUY-only) |

**Hinweis:** StockKit ist die Discord-Quelle/der Server, über den die Bots
signale beziehen — kein Processor. Ein ehemaliger `stockkit`-Alias auf den
RamJet-Processor wurde entfernt (Decision Record 2026-08-31): jeder Bot hat
seine eigene Processor-Klasse und ihr eigenes Config-File; gemeinsame
Low-Level-Helfer sind erlaubt, Processor-Semantik und Konfigurationen bleiben
pro Bot.

## Bots im Detail

### Nitro
- **Status:** Aktiv, registriert
- **Features:** RamJet-Formeln, BUY-only Signalzusammenführung
- **Last-Run:** Über Health-Check-Job validierbar

### RamJet
- **Status:** Aktiv, registriert
- **Features:** ATR + Supertrend BUY-only Setup mit TP1 und trailing metadata
- **Last-Run:** Über Health-Check-Job validierbar

### RocketFuel (Long-only & Long+Short)
- **Status:** Aktiv, registriert
- **Features:** Komplexere Entry-Logik, Varianten für Long-only vs Long+Short
- **Last-Run:** Über Health-Check-Job validierbar

### StockKit
- **Status:** Kein Processor — Discord-Quelle/Server (siehe Registry-Hinweis)
- **Routing:** `live_runner/stockkit_routing.py` mappt die Discord-Bots
  (`nitro`, `ramjet`, `rocketfuel`) auf ihre Alpaca-Accounts

## Health-Check Status

**Stand 2026-08-31:**
- Alle 4 registrierten Prozessoren (`ramjet`, `rocketfuel_longsonly`,
  `rocketfuel_longandshort`, `nitro`) sind importierbar und haben gültige
  Konfigurationsdateien.
- Health-Check-CLI:
  ```bash
  python -m signal_bots.health_check
  ```
- Scheduler-Job: `scheduler/jobs/signal_bot_health_check.py` aktualisiert den
  Hintergrundtask-Status und protokolliert Registry-/Config-Fehler.
- Tests:
  - `tests/test_signal_bot_health_check.py`
  - `tests/test_scheduler_signal_bot_health.py`

## Risk-Engine Integration

1. Signal-Processor berechnet `entry_price`, `stop_loss`, `take_profit`
2. Risk-Engine (`risk.engine.RiskEngine`) sized auf Basis von:
   - explizitem `stop_loss` (bevorzugt), oder
   - Fallback (`atr` aus `metadata`, sonst pip-basiert)
3. Ergebnis (`position_size`, `risk_amount`, `risk_percent`) wird im Replay und im Live-Orderflow verwendet

## Empfehlung

Der Health-Check-Job ist implementiert. Für Produktionsbetrieb sollte die
Scheduler-Konfiguration den Job regelmäßig ausführen und bei `failed`-Status
einen Alert auslösen.
