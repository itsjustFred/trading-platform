---
name: signal-bot-converter
description: Konvertiert externe Signal-Bots (Discord/Telegram/Webhook) in wiederverwendbare Signal-Processing-Strategien.
---

# Agent: Signal Bot Converter

## Ziel

Dieser Agent konvertiert Signal-Quellen (z. B. Discord-Bots) in wiederverwendbare Processor-Module mit stabiler Contract-Schnittstelle:

- Input: `raw_signal` + `market_data`
- Output: `risk.models.TradeSignal`
- SL/TP/Trailing-Regeln werden deterministisch aus der Original-Logik abgeleitet

## Pattern für Signal-Processing-Strategien

### Pflichtstruktur

```text
signal_bots/<bot_name>/
  __init__.py
  processor.py
  config.yaml
```

### Pflichtklasse

```python
class <BotName>SignalProcessor:
    def __init__(self, config: dict): ...
    def process_signal(self, raw_signal: dict, market_data: pd.DataFrame) -> TradeSignal: ...
```

### Erwartetes Output-Format

- Rückgabe ist immer `TradeSignal`.
- `trailing_sl_config` wird in `TradeSignal.metadata` gespeichert.
- Metadata muss die für Replays/Validierung benötigten Werte enthalten:
  - `atr`
  - `tp1_price` / `take_profit`-Ableitungen
  - `trailing_sl_config`
  - Original-Signal-Kontext (`signal_timestamp`, `signal_price`, Source-Hinweise)

## Konvertierungs-Checkliste

1. Quelle vollständig lesen (Signal-Parser, Config, SL/TP/Trailing, Tests).
2. Formeln exakt übernehmen (keine heuristischen Vereinfachungen).
3. Config-Defaults 1:1 in `config.yaml` übertragen.
4. `process_signal` deterministisch halten (keine externen Side-Effects).
5. Nur validierte `TradeSignal`-Felder setzen; Zusatzinformationen in `metadata`.
6. Unit-Tests für:
   - SL/TP-Formeln
   - Trailing-Konfiguration
   - Action/Symbol-Normalisierung

## Template

```python
from risk.models import TradeSignal


class ExampleSignalProcessor:
    def __init__(self, config: dict):
        self.config = config

    def process_signal(self, raw_signal: dict, market_data):
        # 1) Markt-Kontext aufbereiten (Indikatoren / ATR / etc.)
        # 2) Entry, SL, TP gemäß Originalbot berechnen
        # 3) Trailing-Regeln in metadata["trailing_sl_config"] ablegen
        return TradeSignal(
            action="buy",
            symbol=str(raw_signal["symbol"]).upper(),
            entry_price=123.45,
            stop_loss=120.0,
            take_profit=130.0,
            timeframe="5m",
            metadata={
                "source": "converted-signal-bot",
                "trailing_sl_config": {
                    "mode": "supertrend_flip",
                },
            },
        )
```

## Unterschied zu Market-Data-Strategien

| Aspekt | Signal-Processing-Strategie | Market-Data-Strategie |
|---|---|---|
| Primärer Input | Externes Raw-Signal + Markt-Kontext | OHLCV-Serie |
| Kernmethode | `process_signal(raw_signal, market_data)` | `generate_signals(df, params)` |
| Ausgabe | Einzelnes `TradeSignal` | Entry/Exit-Serien für Backtest |
| Hauptaufgabe | Signal-Anreicherung (SL/TP/Trailing) | Signal-Generierung aus Preis-/Indikatorlogik |
| Seiteneffekte | Keine (pure Verarbeitung) | Keine (pure Berechnung) |

